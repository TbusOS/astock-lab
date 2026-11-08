#!/usr/bin/env python3
"""account —— 从借款看板读持仓、可取现金、融资负债和每月要还的钱。只读,不写那个仓库。

它是什么:看板首页要告诉 user「这周卖多少、还是持有」,得知道手里有多少股、能取多少现金、
每个月要还多少外债。这些数 user 已经在借款看板(另一个仓库,路径是配置里的 dashboard_dir)里记着:
    券商数据.js   window.BROKER_SEED = {snapshots: [{date, totalAssets, marketValue, marginDebt,
                  netAssets, ratio, cashOut, holdings: [{name, code, shares, price, cost}]}]}
    债务数据.js   window.DEBT_SEED = {items: [{category, …}]},每种 category 用哪些字段见那个文件的头注释
两边看板共用这一份,不另抄一份 —— 抄出来的那份迟早和原件对不上。

没有它会怎样:动作只能拿配置里写死的每月还款数和旧股数算,user 卖了股票、借款还清了一笔,
这边还按老数叫他卖。

入口:
    read_js_seed(path, var)              node 跑这个 .js(只给一个空的 window),把 window[var] 读回来
    latest_snapshot(broker)              日期最新的那一笔
    monthly_schedule(debt, start, months) [("YYYY-MM", 要还的钱), …],从 start 那个月起排 months 个月
    month_need_of(schedule)              这个月要还的钱;这个月已经付完就看下个月
    free_cash(cash, schedule, months=6)  不影响还款的闲钱 = 可取现金 − 往后 months 个月要还的钱
    account_state(config, today)         上面几样合成一份,看板和动作直接读;本函数不抛出

每月要还的钱照抄 总看板.html 的算法(scheduleUntil + monthBuckets),两边必须算出同一个数:
    installment  从 startDate 起每月一笔 monthly,共 periods 期;还款日取 startDate 那天,月底不够取月底
    interest     每月 interestDay 还 monthInterest(从 startDate 和起点里较晚的那天起,到 dueDate 为止),
                 dueDate 那天还本金 principal
    lump         dueDate 那天还 principal;过了 dueDate 还没还的,算在起点那天
    pending      待到账的额度,不用还
    cleared 的一律不算;分期全部期数都过了(自动扣款)或 paidPeriods 已满(不自动扣款)也当还清。
    起点那个月只算起点当天及以后的。
总看板遇到不认识的类别不出声地跳过,这里改成报错:类别不认识说明文件格式变了,不能悄悄少算一笔。
"""
from __future__ import annotations

import calendar
import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import fb_common

BROKER_FILE = "券商数据.js"
DEBT_FILE = "债务数据.js"
STALE_DAYS = 7          # 最新一笔早于今天超过这么多天算过期(总看板顶栏同一个数)
SCHEDULE_MONTHS = 12    # account_state 排几个月的还款
FREE_CASH_MONTHS = 6    # 闲钱按往后几个月的还款算

# 每种类别算月供要用到的字段(按 债务数据.js 头注释;dueDay 总看板不用,还款日按 startDate 那天)
CATEGORY_FIELDS = {
    "installment": ("periods", "monthly", "startDate"),
    "interest": ("principal", "monthInterest", "interestDay", "dueDate"),
    "lump": ("principal", "dueDate"),
    "pending": (),
}

# node 里跑的那段:新开一个 vm 环境,在环境里面建一个空的 window,跑完把 window[name] 转成 JSON 打出来。
# vm 只是让文件里不能直接用 require / process 这些全局变量(防误用),**不是安全隔离** ——
# 存心写的代码照样能跳出来。这没关系:读的是 user 自己的文件,要防的是「文件里多了一行别的代码却没人发现」
# 这种意外,不是防坏人。出错只打一行(错误名 + 信息 + 位置),不打调用栈。
_NODE_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const [file, name, ms] = process.argv.slice(1);
try {
  const ctx = vm.createContext({});
  vm.runInContext('var window = {};', ctx);
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, {filename: file, timeout: Number(ms)});
  const v = ctx.window[name];
  if (v === null || typeof v !== 'object') {
    process.stderr.write('window.' + name + ' 没有定义或不是对象');
    process.exit(3);
  }
  process.stdout.write(JSON.stringify(v));
} catch (e) {
  const first = String((e && e.stack) || '').split('\n')[0];
  const where = first.startsWith(file) ? ' (' + first + ')' : '';
  process.stderr.write(((e && e.name) || 'Error') + ': ' + ((e && e.message) || e) + where);
  process.exit(2);
}
"""


class SeedError(RuntimeError):
    """借款看板的 .js 文件读不出来。信息里一定有文件路径。"""


# ── 读 .js ──────────────────────────────────────────────────────────────────
def read_js_seed(path: Path, var: str, timeout: float = 10.0, node: str | None = None) -> dict:
    """用 node 跑 path(vm 环境里只有一个空的 window 对象,不能直接用 require 等全局变量),返回 window[var](dict)。

    为什么不用正则把 JSON 抠出来:文件里键不带引号、有尾逗号和注释,不是 JSON;
    自己写解析器迟早和浏览器的理解对不上,交给 JS 引擎最稳。
    node 按 fb_common.ORIG_ENV 的 PATH 找(也可以传 node= 指定),子进程环境也用 ORIG_ENV。
    timeout 秒跑不完算失败。任何失败都抛 SeedError,信息里写出文件路径。"""
    path = Path(path)
    if not path.is_file():
        raise SeedError(f"读不出 {path}:文件不存在")
    exe = node or shutil.which("node", path=fb_common.ORIG_ENV.get("PATH"))
    if not exe:
        raise SeedError(f"读不出 {path}:PATH 里找不到 node")
    cmd = [exe, "-e", _NODE_SCRIPT, "--", str(path), var, str(int(timeout * 1000))]   # -- 以后都是参数
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout + 5, env=fb_common.ORIG_ENV)
    except subprocess.TimeoutExpired:
        raise SeedError(f"读不出 {path}:node 超过 {timeout + 5:g} 秒没跑完") from None
    except OSError as e:
        raise SeedError(f"读不出 {path}:node 起不来({e})") from e
    if p.returncode != 0:
        why = (p.stderr or "").strip().splitlines()
        raise SeedError(f"读不出 {path}:{why[0] if why else f'node 退出码 {p.returncode}'}")
    try:
        obj = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise SeedError(f"读不出 {path}:node 打出来的不是 JSON({e})") from e
    if not isinstance(obj, dict):
        raise SeedError(f"读不出 {path}:window.{var} 不是对象")
    return obj


def latest_snapshot(broker: dict) -> dict:
    """date 最新的那一笔(新字典)。和总看板一样按日期字符串排,不按在文件里的位置。"""
    snaps = [s for s in (broker or {}).get("snapshots") or [] if isinstance(s, dict)]
    if not snaps:
        raise ValueError("券商数据里没有 snapshots")
    bad = [s.get("date") for s in snaps if not _is_iso(s.get("date"))]
    if bad:
        raise ValueError(f"券商数据里有 date 不是 YYYY-MM-DD 的记录:{bad}")
    return dict(max(snaps, key=lambda s: s["date"]))


def _is_iso(s) -> bool:
    try:
        date.fromisoformat(s)
        return isinstance(s, str) and len(s) == 10
    except (TypeError, ValueError):
        return False


# ── 日期(照抄总看板的 addMonths / nextMonthlyOnOrAfter)────────────────────
def _clamp(y: int, m: int, day: int) -> date:
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def _add_months(d: date, n: int) -> date:
    """d 往后 n 个月的同一天,那个月没有这一天就取月底。"""
    y, m = divmod(d.month - 1 + n, 12)
    return _clamp(d.year + y, m + 1, d.day)


def _next_monthly_on_or_after(day: int, frm: date) -> date:
    """frm 当天或以后,第一个「每月 day 号」(月底不够取月底)。"""
    d = _clamp(frm.year, frm.month, day)
    if d < frm:
        nxt = _add_months(date(frm.year, frm.month, 1), 1)
        d = _clamp(nxt.year, nxt.month, day)
    return d


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ── 每月要还的钱 ────────────────────────────────────────────────────────────
def _check_item(it: dict) -> str:
    """类别认识、字段齐全就返回类别;否则 ValueError,写出哪一笔。
    还清的(cleared)只查类别认不认识(不认识说明文件格式变了),不查字段:它不算钱,
    缺字段也不该让整份持仓变过期(总看板对还清的也是直接跳过)。"""
    cat = it.get("category")
    name = it.get("name") or f"id={it.get('id')}"
    if cat not in CATEGORY_FIELDS:
        raise ValueError(f"借款「{name}」的类别 {cat!r} 没有写规则(认识的:{', '.join(CATEGORY_FIELDS)})")
    if it.get("cleared"):
        return cat
    lack = [f for f in CATEGORY_FIELDS[cat] if it.get(f) in (None, "")]
    if lack:
        raise ValueError(f"借款「{name}」({cat})缺 {', '.join(lack)}")
    return cat


def _paid_periods(it: dict, today: date) -> int:
    periods = int(it.get("periods") or 0)
    if not it.get("autoPaid"):
        return max(0, min(periods, int(it.get("paidPeriods") or 0)))
    st = date.fromisoformat(it["startDate"])
    return sum(1 for i in range(periods) if _add_months(st, i) <= today)


def _cleared(it: dict, cat: str, today: date) -> bool:
    if it.get("cleared"):
        return True
    periods = int(it.get("periods") or 0)
    return cat == "installment" and periods > 0 and _paid_periods(it, today) >= periods


def _payments(it: dict, cat: str, start: date, end: date) -> list[tuple[date, float]]:
    """这一笔在 [start, end] 里要付的每一次 (日期, 金额)。"""
    if cat == "installment":
        st, amt = date.fromisoformat(it["startDate"]), float(it.get("monthly") or 0)
        days = (_add_months(st, i) for i in range(int(it.get("periods") or 0)))
        return [(d, amt) for d in days if start <= d <= end]
    if cat == "interest":
        due = date.fromisoformat(it["dueDate"])
        out = []
        if it.get("monthInterest") and it.get("interestDay"):
            st = date.fromisoformat(it["startDate"]) if it.get("startDate") else start
            day = int(it["interestDay"])
            d = _next_monthly_on_or_after(day, max(st, start))
            while d <= end and d <= due:
                out.append((d, float(it["monthInterest"])))
                d = _next_monthly_on_or_after(day, _add_months(date(d.year, d.month, 1), 1))
        if start <= due <= end:
            out.append((due, float(it.get("principal") or 0)))
        return out
    if cat == "lump":
        d = max(date.fromisoformat(it["dueDate"]), start)   # 过期没还的算在起点那天
        return [(d, float(it.get("principal") or 0))] if d <= end else []
    return []                                                  # pending:待到账,不用还


def monthly_schedule(debt: dict, start: date, months: int) -> list[tuple[str, float]]:
    """从 start 那个月起排 months 个月,每月要还的钱 [("YYYY-MM", 元), …](元保留 2 位小数)。
    起点那个月只算 start 当天及以后的(start 就是总看板里的「今天」)。规则见模块说明。
    类别不认识、字段缺了 → ValueError(写出哪一笔、哪个类别)。"""
    if months < 0:
        raise ValueError(f"months 不能是负数,得到 {months}")
    items = (debt or {}).get("items")
    if not isinstance(items, list):
        raise ValueError("债务数据里没有 items 列表")
    keys = [_ym(_add_months(date(start.year, start.month, 1), i)) for i in range(months)]
    if not keys:
        return []
    last = _add_months(date(start.year, start.month, 1), months - 1)
    end = _clamp(last.year, last.month, 31)
    sums = dict.fromkeys(keys, 0.0)
    for it in items:
        cat = _check_item(it)
        if _cleared(it, cat, start):
            continue
        for d, amt in _payments(it, cat, start, end):
            sums[_ym(d)] += amt
    return [(k, round(v, 2)) for k, v in sums.items()]


def month_need_of(schedule: list) -> tuple[str | None, float | None]:
    """这个月要还的钱;这个月已经付完(0)就看下个月 —— 和总看板券商页「要动多少」一样。"""
    if not schedule:
        return None, None
    first = schedule[0]
    if first[1] <= 0 and len(schedule) > 1:
        first = schedule[1]
    return first[0], float(first[1])


def free_cash(cash: float, schedule: list, months: int = FREE_CASH_MONTHS) -> float:
    """不影响还款的闲钱 = 可取现金 − 往后 months 个月要还的钱。可以是负数(差多少)。"""
    return round(float(cash) - sum(float(a) for _, a in schedule[:months]), 2)


# ── 合成一份 ────────────────────────────────────────────────────────────────
def _holdings(snap: dict) -> dict:
    out = {}
    for h in snap.get("holdings") or []:
        code = str(h.get("code") or "").strip()
        if not code:
            raise ValueError(f"持仓「{h.get('name')}」没写代码")
        if code in out:
            raise ValueError(f"持仓里 {code} 出现了两次")
        shares = float(h.get("shares") or 0)
        if shares != int(shares) or shares < 0:
            raise ValueError(f"持仓 {code} 的股数 {h.get('shares')!r} 不是非负整数")
        price = h.get("price")
        out[code] = {"shares": int(shares), "cost": float(h.get("cost") or 0),
                     "price": float(price) if isinstance(price, (int, float)) else None}   # 截图那天的价,重算净资产用
    return out


def _read_broker(base: Path) -> dict:
    snap = latest_snapshot(read_js_seed(base / BROKER_FILE, "BROKER_SEED"))
    return {"asof": snap["date"], "holdings": _holdings(snap),
            # 和总看板「要动多少」一样用可取现金:有融资时账户里那点现金取不出来,不能拿去还外债
            "cash": float(snap.get("cashOut") or 0), "margin_debt": float(snap.get("marginDebt") or 0),
            # 仓位上限按「占净资产多少」算,要这个数(position_limit.py 按现在的收盘重算);看板没写就是 None,上限算不出来
            "net_assets": float(snap["netAssets"]) if isinstance(snap.get("netAssets"), (int, float)) else None}


def _debt_notes(debt: dict) -> list[str]:
    """债务数据里两样会让「要还多少」不准的东西,照总看板总览页的提示写出来。"""
    out = []
    todo = [str(x).strip() for x in debt.get("todo") or [] if str(x).strip()]
    if todo:
        out.append(f"借款看板里还有没录进来的借款({';'.join(todo)}),每月要还的钱会少算")
    bank = debt.get("cashInBank")
    if isinstance(bank, (int, float)) and bank > 0:
        out.append(f"银行卡里还有 {bank:,.2f} 元(借款看板 cashInBank),没算进可取现金")
    return out


def account_state(config: dict, today: date) -> dict:
    """借款看板 → 动作要用的一份数。不抛出:读不出来就 stale=True,error 写原因。

    返回 {"asof", "holdings": {code: {"shares", "cost", "price"(截图价)}}, "cash"(可取现金), "margin_debt",
          "net_assets"(那一笔的净资产), "stale",
          "schedule"(今天这个月起 12 个月), "month_need", "need_month", "free_cash",
          "free_cash_until"(闲钱算到的最后一个月,含这个月,YYYY-MM),
          "error", "notes"}
    stale = 最新一笔早于 today 超过 STALE_DAYS 天、日期晚于 today(写错了),或者任何一个文件读不出来
            (借款坏了也算:要还多少不对,卖多少就不对)。
    持仓读不出来时 holdings 是空的、cash 等是 None,不拿旧数顶。
    借款读不出来时 month_need 用配置的 monthly_cash_need_fallback,并在 notes 里注明;free_cash 是 None。
    债务数据里有 todo(还没录进来的借款)或 cashInBank(银行卡现金)时也写进 notes。"""
    config = config or {}
    errors, notes = [], []
    broker = {"asof": None, "holdings": {}, "cash": None, "margin_debt": None, "net_assets": None}
    schedule, need_month, month_need = [], None, None
    base = config.get("dashboard_dir")
    if not base:
        errors.append("配置里没有 dashboard_dir(借款看板在哪)")
    else:
        try:
            broker = _read_broker(Path(base))
        except Exception as e:          # 文件、node、格式……什么都可能,记下来,不抛
            errors.append(f"持仓:{e}")
        try:
            debt = read_js_seed(Path(base) / DEBT_FILE, "DEBT_SEED")
            schedule = monthly_schedule(debt, today, SCHEDULE_MONTHS)
            need_month, month_need = month_need_of(schedule)
            notes += _debt_notes(debt)
        except Exception as e:
            errors.append(f"借款:{e}")
    if not schedule:
        fb = config.get("monthly_cash_need_fallback")
        need_month, month_need = _ym(today), (float(fb) if fb is not None else None)
        notes.append(f"借款数据读不出来,这个月要还的钱先用配置里的 {fb}" if fb is not None
                     else "借款数据读不出来,配置里也没有 monthly_cash_need_fallback,这个月要还多少不知道")
    asof = broker["asof"]
    age = (today - date.fromisoformat(asof)).days if asof is not None else None
    old = age is not None and age > STALE_DAYS
    if age is not None and age < 0:     # 日期写到了今天以后:多半是手误,不当成「最新」,不然永远不过期
        errors.append(f"持仓:最新一笔的日期 {asof} 晚于今天 {today},先核对借款看板")
    ok_cash = broker["cash"] is not None and bool(schedule)
    # 写最后一个算进去的月份(含):今天这个月起共 FREE_CASH_MONTHS 个月。原来写的是「下一个月」(不含),
    # 页面写成「到 X 为止」、AI 读成「到 X 那个月」,都多算了一个月(一份 AI 答卷核对时发现)
    until = _add_months(date(today.year, today.month, 1), FREE_CASH_MONTHS - 1)
    return {**broker, "stale": bool(errors) or asof is None or old, "schedule": schedule,
            "month_need": month_need, "need_month": need_month,
            "free_cash": free_cash(broker["cash"], schedule) if ok_cash else None,
            "free_cash_until": _ym(until), "error": ";".join(errors) or None, "notes": notes}
