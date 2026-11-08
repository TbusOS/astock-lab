#!/usr/bin/env python3
"""inputs —— 预测看板的输入包:每项数据带来源、时间、取数结果、用途,出预测前冻结。

它是什么:出一次预测要用到的全部数据,一项一条:
    {"key", "value", "source", "as_of"(数据截至哪天), "public_at"(哪天公开,不知道记 None),
     "fetched_at"(取得时间,北京时间), "status", "used_by", "note"}
    status    ok 取到 · partial 只取到一部分 · failed 取不到 · stale 这次没取到、沿用上次的值
    used_by   system 参与系统计算 · ai 只给 AI 参考 · display 只展示(页面按它标「这项数据进了哪里」)

没有它会怎样:事后核对一道错题,分不清是「当时数据就缺了 / 是旧的」还是「算法错了」;
重跑旧题也没法保证用的是当时那份数。所以每项都记下来源和时间,失败也照实记,不悄悄跳过。

入口:
    collect(config, prev, collectors=None) → {key: 一项}   平铺的字典,system_forecast 直接读它
    missing_required(items, config) → [str]                本次检查缺什么:取不到的必需项、行业要看但还没
                                                           工具的指标(gap:<指标>)、没配行业(sector.<代码>),
                                                           页面「本次检查的数据缺什么」读它
    两者分开返回 —— 看板存输入包时自己决定怎么放(例如 {"items": …, "missing_required": …})。
    DEFAULT_COLLECTORS                                    每项数据怎么取(键里的 <code>、<a>.<b> 按配置展开)
    LEAD_INDICATORS / TOOL_GAPS                           行业表里每个领先指标:由哪个取数器取,或为什么还没取
                                                           (scripts/check_inputs_contract.py 查两边对得上)

在哪取:本机能通的(腾讯日线、东财、台湾 OpenAPI、纳斯达克)本机取;同花顺、巨潮、SEC、baostock
本机不通,把本仓的脚本从标准输入送到 mini 上跑(mini 上那份 astock-lab 是旧的,不用它的代码)。
account(持股、可取现金、每月还款)从借款看板的两个 .js 文件读(account.py),只给 AI 参考,
值只进内部仓的存档。

⚠ import 顺序:先 fb_common(拍下带代理的环境 ORIG_ENV)再 prices(它加载 market.py,
   market.py 一 import 就删掉代理变量)。ssh 子进程用 ORIG_ENV;纳斯达克直连失败时只给那一个请求加代理。
检查:scripts/check_board_store.py(不联网)、scripts/check_inputs_contract.py。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import re
import tempfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from functools import lru_cache, partial
from pathlib import Path

import fb_common  # 必须在 prices 之前,见上面
import prices
import account

_SKILLS = Path(__file__).resolve().parents[2]
FETCH_DIR = _SKILLS / "data-sources" / "scripts" / "fetch"
HOLDINGS_CHECK = _SKILLS / "astock-quote" / "scripts" / "holdings_check.py"
_PATHS = {"events": FETCH_DIR / "events.py", "industry": FETCH_DIR / "industry.py",
          "research": FETCH_DIR / "research.py", "eps_revisions": FETCH_DIR / "eps_revisions.py",
          "sectors": _SKILLS / "stock-analysis-workflow" / "scripts" / "sectors.py"}

ITEM_KEYS = ("key", "value", "source", "as_of", "public_at", "fetched_at", "status", "used_by", "note")
INDEX_KEY = f"index.{prices.CALENDAR_INDEX}"
HISTORY_KEY = "history.long"    # 长日线(检验信号用)取不到时 board_forecast 在输入包里记的那一项;取到了不放(太大)
MIN_BARS = 200                  # 一整年交易日约 242 天,少于 200 根说明缺了一大段
CAPEX_ARGS = ["--quarters", "8", "--guidance", "--json"]
REVISION_DAYS = 30
TAIL_DAYS = 30                  # 日线最后几根对不上时,换窗口重取往前取多少个自然日(c_prices)
CALENDAR_DAYS = 21              # 财报日历看几个自然日:14 天会漏掉 15 天后才发财报的英伟达这种


@lru_cache(maxsize=None)
def _module(name: str):
    """按文件路径加载别的 skill 里的脚本(不走 sys.path,免得撞上别处的同名模块)。用到才加载。"""
    path = _PATHS[name]
    spec = importlib.util.spec_from_file_location(f"fb_{name}", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise ImportError(f"找不到 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


events = _module("events")
market = prices.market


def eps_revisions_module():
    return _module("eps_revisions")


# ── 一项数据 ────────────────────────────────────────────────────────────────
USED_BY = {"prices": "system", "index": "system", "market.regime": "system", "market.margin": "ai",
           "peer": "system", "consensus": "system", "eps_revisions": "system", "valuation": "system",
           "capex": "system", "taiwan": "ai", "sectors": "display", "events.calendar": "ai",
           "events.sec": "ai", "account": "ai", "history": "system"}


def used_by_for(key: str) -> str:
    """按键查用途:先整键,再逐级去掉最后一段(prices.300308 → prices)。表里没有的记 ai。"""
    parts = key.split(".")
    for n in range(len(parts), 0, -1):
        hit = USED_BY.get(".".join(parts[:n]))
        if hit:
            return hit
    return "ai"


def _item(key: str, got, fetched_at: str) -> dict:
    """取数器的返回 → 完整的一项。取数器只能报 ok / partial,取不到要抛异常。"""
    if not isinstance(got, dict) or "value" not in got:
        raise ValueError(f"取数器要返回带 value 的 dict,得到 {type(got).__name__}")
    status = got.get("status", "ok")
    if status not in ("ok", "partial"):
        raise ValueError(f"取数器只能报 ok / partial,得到 {status!r}")
    return {"key": key, "value": got["value"], "source": got.get("source", ""), "as_of": got.get("as_of"),
            "public_at": got.get("public_at"), "fetched_at": fetched_at, "status": status,
            "used_by": got.get("used_by") or used_by_for(key), "note": got.get("note", "")}


PARTIAL_MARK = "那份旧值当时就只取到一部分"


def _carry_partial(old: dict) -> str:
    """沿用的旧值当时就不全(partial,或者它本身也是沿用来的、记着不全):把那句接在新 note 后面。
    不接的话,旧值的 note 被「这次取数失败」整句换掉,页面上看不出这份值本来就缺一块。"""
    note = str(old.get("note") or "")
    if old.get("status") == "partial":
        return f";{PARTIAL_MARK}" + (f"({note})" if note else "")
    return ";" + note[note.index(PARTIAL_MARK):] if PARTIAL_MARK in note else ""


def _fallback(key: str, prev: dict | None, err: Exception, fetched_at: str) -> dict:
    """取数失败:上次有值就 stale(值、as_of、来源、取得时间都沿用上次的),没有就 failed。"""
    why = f"{type(err).__name__}: {str(err)[:200]}"
    old = (prev or {}).get(key)
    if isinstance(old, dict) and old.get("status") != "failed" and old.get("value") is not None:
        return {**{k: old.get(k) for k in ITEM_KEYS}, "key": key, "status": "stale",
                "used_by": old.get("used_by") or used_by_for(key),
                "note": f"这次取数失败({why}),沿用 {old.get('as_of')} 的旧值" + _carry_partial(old)}
    return {"key": key, "value": None, "source": "", "as_of": None, "public_at": None,
            "fetched_at": fetched_at, "status": "failed", "used_by": used_by_for(key), "note": f"取数失败:{why}"}


def collect(config: dict, prev: dict | None, collectors: dict | None = None, progress=None) -> dict:
    """逐个跑取数器,返回 {key: 一项}(平铺,新字典,不改 prev)。
    collectors 不给 = expand(config) 展开的 DEFAULT_COLLECTORS;给了就照它跑(键是展开后的,值是 fn(config))。
    某项抛任何异常:prev 里有它的值 → stale 沿用;没有 → failed。本函数不抛出。
    progress(str) 每项跑完调一次(命令行打进度用),可不给。"""
    config = config or {}
    collectors = expand(config) if collectors is None else collectors
    out = {}
    for key, fn in collectors.items():
        fetched_at = fb_common.now_cn().isoformat(timespec="seconds")
        try:
            out[key] = _item(key, fn(config), fetched_at)
        except Exception as e:          # 网络、解析、mini 不通……什么都可能,一律记下来
            out[key] = _fallback(key, prev, e, fetched_at)
        if progress:
            progress(f"{key}: {out[key]['status']}")
    return out


def _codes(config: dict) -> list[str]:
    return [str(s["code"]) for s in config.get("stocks") or []]


def expand(config: dict, templates: dict | None = None) -> dict:
    """把 DEFAULT_COLLECTORS 的键按配置展开:<code> → 每只股票,<a>.<b> → 每对同行。"""
    out = {}
    for tkey, fn in (DEFAULT_COLLECTORS if templates is None else templates).items():
        if "<code>" in tkey:
            out.update({tkey.replace("<code>", c): partial(fn, code=c) for c in _codes(config)})
        elif "<a>.<b>" in tkey:
            out.update({tkey.replace("<a>.<b>", f"{a}.{b}"): partial(fn, a=str(a), b=str(b))
                        for a, b in config.get("peers") or []})
        else:
            out[tkey] = fn
    return out


# ── 行业必需项 ──────────────────────────────────────────────────────────────
def missing_required(items: dict, config: dict) -> list[str]:
    """本次检查缺了什么(按出现顺序,去重),三种:
      <键>          行业必需项没取到:没有这一项、或 status=failed(stale、partial 不算缺,页面另列)。
                    必需项 = 每只股票的日线 + 交易日历指数 + 它所在行业每个领先指标对应的取数器。
      gap:<指标>    它所在行业要看、但还没有取数工具的领先指标;原因查 TOOL_GAPS[指标]。
                    不列出来,页面会显示「什么都不缺」,读的人以为该看的都看过了。
      sector.<代码> 配置里这只股票没写 sector,或写的名字不在行业表里 —— 不知道要什么,就不能说什么都不缺。
    行业只读配置 stocks[i].sector,不联网查(板块接口本机取不到时会悄悄返回空)。"""
    table = _module("sectors").SECTORS
    given = {str(s["code"]): s.get("sector") for s in config.get("stocks") or []}
    need = [f"prices.{c}" for c in _codes(config)] + [INDEX_KEY]
    for code in _codes(config):
        spec = table.get(given.get(code) or "")
        if spec is None:
            need.append(f"sector.{code}")
            continue
        for row in spec["leading"]:
            if row[0] in LEAD_INDICATORS:
                need.append(LEAD_INDICATORS[row[0]].replace("<code>", code))
            elif row[0] in TOOL_GAPS:
                need.append(f"gap:{row[0]}")
    return [k for k in dict.fromkeys(need)
            if k.startswith(("sector.", "gap:")) or (items.get(k) or {}).get("status") in (None, "failed")]


# ── 解析 ────────────────────────────────────────────────────────────────────
def _float(s):
    try:
        x = float(str(s).replace(",", ""))
    except ValueError:
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def parse_consensus_md(text: str) -> dict:
    """consensus.py 的 markdown → {"profit": {"2026": {"n", "min", "mean", "max"}, …},
    "ratings": {"up", "keep", "down", "n"} 或 None(近 N 天没有评级记录),
    "rating_failed_days": 巨潮按天查时抛异常的天数(consensus.py 打的那行,没有就是 0)}。
    只读「### 净利润(亿元)」那张表,不碰每股收益那张(每股收益会被送转带偏)。"""
    profit, in_profit = {}, False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            in_profit = bool(re.match(r"#+\s*净利润", s))
            continue
        if in_profit and s.startswith("|"):
            cells = [c.strip().strip("*").strip() for c in s.strip("|").split("|")]
            if len(cells) >= 5 and re.fullmatch(r"20\d\d", cells[0]):
                n = _float(cells[1])
                profit[cells[0]] = {"n": None if n is None else int(n), "min": _float(cells[2]),
                                    "mean": _float(cells[3]), "max": _float(cells[4])}
    m = re.search(r"共\s*\**(\d+)\**\s*家机构发布.*?调高\s*\**(\d+)\**.*?维持\s*\**(\d+)\**.*?调低\s*\**(\d+)",
                  text or "")
    ratings = None if not m else {"up": int(m.group(2)), "keep": int(m.group(3)), "down": int(m.group(4)),
                                  "n": int(m.group(1))}
    fail = re.search(r"巨潮评级[::]\s*(\d+)\s*天取数失败", text or "")
    return {"profit": profit, "ratings": ratings, "rating_failed_days": int(fail.group(1)) if fail else 0}


def parse_capex_json(obj: dict) -> dict:
    """capex.py --json → {"total": [{"q", "sum", "yoy", "cash_yoy"}](从旧到新), "guidance": [str],
    "gaps": [...], "failed": [...]}。sum = 含融资租赁的合计(十亿美元),yoy = 它的同比(缺口季为 None),
    cash_yoy = 只算现金购建的同比。"""
    qs = sorted(obj.get("quarters") or [], key=lambda q: q["q"])
    return {"total": [{"q": q["q"], "sum": q.get("sum_total"), "yoy": q.get("yoy_total"),
                       "cash_yoy": q.get("yoy_cash")} for q in qs],
            "guidance": [f"{g.get('ticker', '')} {g.get('date', '')}:{g.get('text', '')}"
                         for g in obj.get("guidance") or []],
            "gaps": list(obj.get("gaps") or []), "failed": list(obj.get("failed") or [])}


def function_source(path: Path, name: str) -> str:
    """某个文件里一个函数的源码(原样),送到 mini 上执行用。"""
    src = Path(path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise LookupError(f"{path} 里没有函数 {name}")


# ── 算值(纯函数,检查脚本直接测) ────────────────────────────────────────────
def thin_years(series, min_bars: int = MIN_BARS) -> list[tuple[int, int]]:
    """第一年和最后一年之间(不含两头)根数少于 min_bars 的年份 [(年, 根数)]。整年没有也算(0 根)。"""
    years = [int(d[:4]) for d, _ in series]
    if not years:
        return []
    count = {y: years.count(y) for y in set(years)}
    return [(y, count.get(y, 0)) for y in range(min(years) + 1, max(years)) if count.get(y, 0) < min_bars]


def regime_value(dates: list[str], closes: list[float], index: str) -> dict:
    """{"index", "close", "ma60", "state": "above"|"below", "days": 已经连续几个交易日}。
    规则用 market.regime(收盘等于均线算 above),跟 market regime 命令同一份代码。"""
    st = market.regime(closes, market.MA_N, 1)
    if not st or st[-1] is None:
        raise ValueError(f"{index} 只有 {len(closes)} 根,算不出 {market.MA_N} 日线")
    ma = market.moving_avg(closes, market.MA_N)
    since = len(st) - 1
    while since > 0 and st[since - 1] == st[-1]:
        since -= 1
    return {"index": index, "close": closes[-1], "ma60": round(ma[-1], 2), "state": st[-1], "days": len(st) - since}


def margin_value(rows) -> dict:
    """全市场融资余额 [(日期, 亿元)] → {"balance_yi", "chg20_pct"}(近 20 个交易日变化 %)。"""
    rows = sorted(rows)
    chg = market.pct_change(rows, 20)
    if chg is None:
        raise ValueError(f"融资余额只有 {len(rows)} 天,算不出近 20 个交易日变化")
    return {"balance_yi": round(rows[-1][1], 2), "chg20_pct": round(chg, 2)}


def peer_value(a, b) -> dict:
    """两只前复权 [(日期, 收盘)] → {"gap10", "gap20"} = (A 的涨幅 − B 的涨幅) × 100,
    只用两只都有的日期(market.peer_gap),跟 system_forecast 走前检验同一种算法。"""
    out = {}
    for days in (10, 20):
        g = market.peer_gap(sorted(a), sorted(b), days)
        if g is None:
            raise ValueError(f"两只共同的交易日不到 {days + 1} 个")
        out[f"gap{days}"] = round(g["gap"], 2)
    return out


def eps_value(rows: list[dict], today: str, fy: int | None = None) -> dict:
    """研报(已读出年份和净利润的 rows)→ {"up30", "down30", "fy", "eps_basis_skipped30"}。
    只数近 30 天按归母净利润比的调高 / 调低;只能按每股收益比的(可能受送转影响)不算,另记条数。
    eps_revisions.summarize 的 up30 / down30 两种都算,所以这里自己筛。
    fy 默认看明年:3 个月到 1 年的预测,股价主要看明年的利润;今年只剩一两个季度。[设计取舍]"""
    fy = fy or int(today[:4]) + 1
    t = date.fromisoformat(today)
    revs = [r for r in eps_revisions_module().summarize(rows, fy, today)["revisions"]
            if (t - date.fromisoformat(r["date"])).days <= REVISION_DAYS]
    prof = [r for r in revs if r["basis"] == "net_profit"]
    return {"up30": sum(r["label"] == "调高" for r in prof), "down30": sum(r["label"] == "调低" for r in prof),
            "fy": fy, "eps_basis_skipped30": len(revs) - len(prof)}


def valuation_value(raw: dict) -> dict:
    """mini 上 valuation_percentile 的结果 + 动态市盈率 → {"pe_pctl", "pb_pctl", "dyn_pe"}。
    分位按 0~100 给;不在 0~100 报 ValueError(多半是哪一层换成了 0~1 或字段放错)。缺的记 None。"""
    out = {}
    for key in ("pe", "pb"):
        t = raw.get(key)
        p = None if not t else float(t[1])
        if p is not None and not 0 <= p <= 100:
            raise ValueError(f"{key} 历史分位 = {p},不在 0~100")
        out[f"{key}_pctl"] = None if p is None else round(p, 1)
    d = _float(raw.get("dyn_pe")) if raw.get("dyn_pe") is not None else None
    out["dyn_pe"] = None if d is None else round(d, 2)
    return out


def _roc_month(s) -> str:
    s = str(s)
    return f"{int(s[:-2]) + 1911}-{s[-2:]}"         # 民国 11509 → 2026-09


def _roc_date(s) -> str:
    s = str(s)
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"   # 1151017 → 2026-10-17


def taiwan_value(rows: list[dict], expected: dict) -> dict:
    """industry.fetch_taiwan 的命中行 → 一项(不含 key 等):value = {"ym", "companies": […], "expected", "got"}。
    as_of = 营收月份;public_at = 接口的出表日期(台湾法定每月 10 日前公告,这是能拿到的最近似的公开时间)。
    营收单位千元新台币;同比用 industry.py 自己算的(源里的百分比列各接口不一致)。"""
    if not rows:
        raise ValueError("台湾光通信月营收一家都没取到")
    comps = [{"code": str(r.get("公司代號")), "name": str(r.get("公司名稱") or ""), "ym": _roc_month(r["資料年月"]),
              "rev_k_twd": _float(r.get("營業收入-當月營收")), "rev_last_year_k_twd": _float(r.get("營業收入-去年當月營收")),
              "yoy": r.get("_同比%"), "why": r.get("_我们为什么看它", "")} for r in rows]
    yms = sorted({c["ym"] for c in comps})
    pubs = sorted({_roc_date(r["出表日期"]) for r in rows if r.get("出表日期")})
    got = {c["code"] for c in comps}
    notes = [f"没取到:{'、'.join(c for c in expected if c not in got)}"] if set(expected) - got else []
    if len(yms) > 1:
        notes.append(f"各家月份不一致 {yms}")
    return {"value": {"ym": yms[-1], "companies": comps, "expected": len(expected), "got": len(comps)},
            "source": "台湾证交所 / 柜买 OpenAPI t187ap05(industry.py taiwan)", "as_of": yms[-1],
            "public_at": pubs[-1] if pubs else None, "status": "partial" if notes else "ok", "note": ";".join(notes)}


def etf_value(name: str, series) -> dict:
    """板块 ETF 前复权 [(日期, 收盘)] → {"name", "m1", "m3", "dd_1y"}:
    近 21 / 63 个交易日涨跌 %、离最近 250 个交易日最高点 %。"""
    s = sorted(series)
    pc = lambda back: None if market.pct_change(s, back) is None else round(market.pct_change(s, back), 2)  # noqa: E731
    hi = max(c for _, c in s[-250:])
    return {"name": name, "m1": pc(21), "m3": pc(63), "dd_1y": round((s[-1][1] / hi - 1) * 100, 2)}


# ── 取数器:本机 ────────────────────────────────────────────────────────────
def _today(today: date | None) -> date:
    return today or fb_common.now_cn().date()


def _tencent(sym: str, start: str, fq: str):
    return market.fetch_daily(sym, start, fq=fq)


def _two_years_ago(t: date) -> str:
    try:
        return t.replace(year=t.year - 2).isoformat()
    except ValueError:                      # 2 月 29 日
        return t.replace(year=t.year - 2, day=28).isoformat()


def _bars(rows) -> list[list]:
    return [[d, float(c)] for d, c in sorted(rows)]


def _thin_notes(parts: dict) -> list[str]:
    return [f"{y} 年只有 {n} 根({name})" for name, s in parts.items() for y, n in thin_years(s)]


def _patch_tail(fetch, sym: str, fq: str, rows: list[list], upto: str) -> tuple[list[list], str]:
    """rows 的最后一天比另一种复权早:换一个短窗口(最后一天往前 TAIL_DAYS 个自然日起)再取一次,接上后面几根。
    为什么:腾讯按请求参数缓存,同一组参数的结果可能停在前一个交易日(2026-11-02 实测 300308:
    2026-01-01 起那段后复权停在 10-30,起点换成 2026-01-02 就有 11-02)。接上的还是腾讯给的数,不是自己算的。
    重叠的日子两次的数要一样才接(前复权在除权后会整段重算,不一样说明两次不是同一个基准,不能拼)。"""
    name = {"hfq": "后复权", "qfq": "前复权"}[fq]
    last = rows[-1][0]
    head = f"{name}最后一天 {last} 比另一种复权早"
    try:                    # 重取只是补救:它出错不能把两年那次已经取到的丢掉
        tail = _bars(fetch(sym, (date.fromisoformat(last) - timedelta(days=TAIL_DAYS)).isoformat(), fq))
    except Exception as e:  # 网络、接口……照旧 partial,由「日期对不上」让 forecast 不出预测
        return rows, f"{head},换窗口重取失败,没接:{type(e).__name__}: {str(e)[:120]}"
    old = dict((d, c) for d, c in rows)
    same = [d for d, c in tail if d in old and abs(c - old[d]) <= 1e-6 * max(1.0, abs(c))]
    if not same or len(same) != sum(1 for d, _ in tail if d in old):
        return rows, f"{head},换窗口重取的数和原来那段对不上,没接"
    new = [[d, c] for d, c in tail if last < d <= upto]
    if not new:
        return rows, f"{head},换窗口重取还是停在 {tail[-1][0] if tail else last}"
    return rows + new, f"{head}(数据源缓存没更新),换窗口重取接上 {len(new)} 根"


def c_prices(cfg: dict, code: str, fetch=None, today: date | None = None) -> dict:
    """近两年后复权 + 前复权日线 {"hfq": [[日期, 收盘]], "qfq": […]};中间年份少于 200 根 → partial。
    两种复权最后一天不一样时,先把早的那种换窗口重取一次(_patch_tail);还不一样就 partial「日期对不上」,
    board forecast 见到这种情况不出预测。"""
    fetch, start, sym = fetch or _tencent, _two_years_ago(_today(today)), prices.tencent_symbol(code)
    hfq, qfq = _bars(fetch(sym, start, "hfq")), _bars(fetch(sym, start, "qfq"))
    if not hfq or not qfq:
        raise ValueError(f"{code} 后复权 {len(hfq)} 根、前复权 {len(qfq)} 根,有一种是空的")
    patched = ""
    if hfq[-1][0] < qfq[-1][0]:
        hfq, patched = _patch_tail(fetch, sym, "hfq", hfq, qfq[-1][0])
    elif qfq[-1][0] < hfq[-1][0]:
        qfq, patched = _patch_tail(fetch, sym, "qfq", qfq, hfq[-1][0])
    notes = _thin_notes({"后复权": hfq, "前复权": qfq})
    if [d for d, _ in hfq] != [d for d, _ in qfq]:
        notes.append(f"后复权 {len(hfq)} 根、前复权 {len(qfq)} 根,日期对不上")
    return {"value": {"hfq": hfq, "qfq": qfq}, "source": "腾讯日线 web.ifzq.gtimg.cn(后复权 + 前复权,近两年)",
            "as_of": hfq[-1][0], "public_at": hfq[-1][0], "status": "partial" if notes else "ok",
            "note": ";".join(x for x in [patched, *notes] if x)}


def c_index(cfg: dict, fetch=None, today: date | None = None) -> dict:
    """交易日历指数(创业板指)近两年不复权日线 {"day": [[日期, 收盘]]}。"""
    day = _bars((fetch or _tencent)(prices.CALENDAR_INDEX, _two_years_ago(_today(today)), ""))
    if not day:
        raise ValueError(f"{prices.CALENDAR_INDEX} 没取到日线")
    notes = _thin_notes({"不复权": day})
    return {"value": {"day": day}, "source": f"腾讯日线 web.ifzq.gtimg.cn({prices.CALENDAR_INDEX} 不复权,近两年)",
            "as_of": day[-1][0], "public_at": day[-1][0], "status": "partial" if notes else "ok",
            "note": ";".join(notes)}


def c_regime(cfg: dict, fetch=None, today: date | None = None) -> dict:
    k = (fetch or _tencent)(market.INDEX, (_today(today) - timedelta(days=400)).isoformat(), "")
    dates, closes = [d for d, _ in sorted(k)], [float(c) for _, c in sorted(k)]
    return {"value": regime_value(dates, closes, market.INDEX), "as_of": dates[-1], "public_at": dates[-1],
            "source": f"腾讯日线({market.INDEX} vs {market.MA_N} 日均线,market.py 的规则)"}


def c_margin(cfg: dict, fetch=None) -> dict:
    rows = sorted((fetch or market.fetch_margin)())
    return {"value": margin_value(rows), "as_of": rows[-1][0], "public_at": None,
            "source": "东财 datacenter RPTA_RZRQ_LSHJ(沪深合计融资余额,market.py)"}


def c_peer(cfg: dict, a: str, b: str, fetch=None, today: date | None = None) -> dict:
    fetch, start = fetch or _tencent, (_today(today) - timedelta(days=90)).isoformat()
    sa, sb = fetch(prices.tencent_symbol(a), start, "qfq"), fetch(prices.tencent_symbol(b), start, "qfq")
    to = max(set(d for d, _ in sa) & set(d for d, _ in sb), default=None)
    return {"value": peer_value(sa, sb), "as_of": to, "public_at": to,
            "source": f"腾讯日线前复权({a} − {b},近 10 / 20 个交易日涨幅差,百分点)"}


def c_taiwan(cfg: dict, fetch=None) -> dict:
    """台湾光通信月营收。industry.fetch_taiwan 会顺手写一份 JSON,这里让它写到临时目录(用完就删)。"""
    ind = _module("industry")
    printed = io.StringIO()
    if fetch is None:
        with tempfile.TemporaryDirectory() as d, redirect_stdout(printed):
            rows = ind.fetch_taiwan(Path(d))
    else:
        rows = fetch()
    item = taiwan_value(rows, ind.TW_OPTICAL)
    warn = [ln.strip() for ln in printed.getvalue().splitlines() if "⚠" in ln]
    if warn:
        item = {**item, "status": "partial", "note": ";".join([item["note"], *warn]).strip(";")}
    return item


def c_eps_revisions(cfg: dict, code: str, today: date | None = None) -> dict:
    """近一年研报 → 读 PDF 预测表里的年份和归母净利润(缓存在工作台 data/research/<代码>/)→ 近 30 天上下调。"""
    import pymupdf  # 没装就取不到,按失败记
    research, er = _module("research"), eps_revisions_module()
    t = _today(today)
    rows = research.fetch_list(code, (t - timedelta(days=365)).isoformat(), (t + timedelta(days=1)).isoformat())
    fetch = lambda info: research._get(research.PDF_URL.format(info),  # noqa: E731
                                       referer="https://data.eastmoney.com/", timeout=40, binary=True)
    text = lambda b: "\n".join(pg.get_text() for pg in pymupdf.open(stream=b, filetype="pdf"))  # noqa: E731
    got = er.resolve(rows, research._lab_root() / "data" / "research" / code, fetch, text)
    return eps_item(got, t.isoformat())


def eps_item(rows: list[dict], today: str) -> dict:
    """已读过 PDF 的研报 → 一项(不含 key 等)。value = eps_value 的结果 + unread30。
    有盈利预测、但 PDF 没读出年份(fy 是 None)的研报不进上下调统计 —— 近 30 天有这种 → partial,
    note 写篇数(更早的也提一句);一篇研报都没有也写进 note。"""
    t = date.fromisoformat(today)
    v = eps_value(rows, today)
    lost = [r for r in rows if (r.get("eps") or [None])[0] and r.get("fy") is None]
    recent = [r for r in lost if (t - date.fromisoformat(r["date"])).days <= REVISION_DAYS]
    notes = [f"近一年研报 {len(rows)} 篇" if rows else "近一年东财研报列表里没有研报",
             f"看 {v['fy']} 年;另有 {v['eps_basis_skipped30']} 条只能按每股收益比的没算"]
    if recent:
        notes.append(f"近 30 天有 {len(recent)} 篇研报的 PDF 没读出年份,没算进上下调")
    if len(lost) > len(recent):
        notes.append(f"更早的另有 {len(lost) - len(recent)} 篇没读出")
    return {"value": {**v, "unread30": len(recent)}, "as_of": max((r["date"] for r in rows), default=today),
            "public_at": None, "source": "东财研报 reportapi + PDF 预测表(eps_revisions.py,按归母净利润比)",
            "status": "partial" if recent else "ok", "note": ";".join(notes)}


def _etf_series(fetch, code: str, start: str):
    """ETF 日线:先要前复权;腾讯对没分过红的 ETF 只回不复权(market 报 ValueError、不肯顶替),
    这时改要不复权 —— 这一项只展示,1~3 个月的涨跌差一次分红影响不大。返回 (日线, 是否不复权)。"""
    sym = prices.tencent_symbol(code)
    try:
        return fetch(sym, start, "qfq"), False
    except ValueError:
        return fetch(sym, start, ""), True


def c_sectors(cfg: dict, fetch=None, today: date | None = None) -> dict:
    etfs = cfg.get("sector_etfs") or {}
    if not etfs:
        raise ValueError("配置里没有 sector_etfs")
    fetch, start = fetch or _tencent, (_today(today) - timedelta(days=400)).isoformat()
    value, bad, raw, last = {}, [], [], []
    for code, name in etfs.items():
        try:
            s, unadjusted = _etf_series(fetch, code, start)
            value[code] = etf_value(name, s)
            last.append(max(d for d, _ in s))
            raw += [f"{code} {name}"] if unadjusted else []
        except Exception as e:
            bad.append(f"{code} {name}({type(e).__name__})")
    if not value:
        raise RuntimeError(f"板块 ETF 一只都没取到:{'、'.join(bad)}")
    notes = ([f"没取到:{'、'.join(bad)}"] if bad else []) + \
            ([f"腾讯没给前复权、按不复权算:{'、'.join(raw)}"] if raw else [])
    return {"value": value, "as_of": max(last), "public_at": max(last),
            "source": "腾讯日线前复权(板块 ETF,近 21 / 63 个交易日涨跌、离一年高点)",
            "status": "partial" if bad else "ok", "note": ";".join(notes)}


def _orig_proxy() -> str | None:
    """进程启动时的 http(s) 代理(all_proxy 常是 socks,urllib 用不了,不取)。"""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        if fb_common.ORIG_ENV.get(k):
            return fb_common.ORIG_ENV[k]
    return None


def c_calendar(cfg: dict, fetch=None, today: date | None = None, days: int | None = None) -> dict:
    """关注名单未来 N 个自然日(美国日期,默认 CALENDAR_DAYS = 21,配置 calendar_days 可改)的财报。
    先直连(market.py 已删掉代理变量);失败了只给这个请求加上启动时的代理再试一次。"""
    fetch = fetch or events.nasdaq_fetch
    day_list = events.next_days(days or int(cfg.get("calendar_days", CALENDAR_DAYS)), today)
    watch = set(cfg.get("watch_us") or events.WATCH_US)
    how = "直连"
    try:
        rows = events.nasdaq_calendar(day_list, watch, fetch=partial(fetch, proxy=None))
    except Exception as e:
        proxy = _orig_proxy()
        if not proxy:
            raise
        rows = events.nasdaq_calendar(day_list, watch, fetch=partial(fetch, proxy=proxy))
        how = f"直连失败({type(e).__name__}),经启动时的代理取到"
    return {"value": rows, "as_of": day_list[0], "public_at": None,
            "source": "纳斯达克财报日历 api.nasdaq.com(events.py)",
            "note": f"{how};{day_list[0]} ~ {day_list[-1]}(美国日期),{len(watch)} 家关注"}


# ── 取数器:送到 mini 上跑 ──────────────────────────────────────────────────
def _mini(cfg: dict):
    host = cfg.get("mini_host", "mini")
    return lambda script, args, timeout=900: events.ssh_python(script, args, host=host,
                                                               env=fb_common.ORIG_ENV, timeout=timeout)


def _need_ok(p, what: str, codes=(0,)):
    if p.returncode not in codes:
        raise RuntimeError(f"{what} 退出码 {p.returncode}:{(p.stderr or '').strip()[-300:]}")
    return p


def c_consensus(cfg: dict, code: str, run=None) -> dict:
    run = run or _mini(cfg)
    p = _need_ok(run((FETCH_DIR / "consensus.py").read_text(encoding="utf-8"), [code, "--days", "30"], timeout=600),
                 "consensus.py")
    value = parse_consensus_md(p.stdout)
    if not value["profit"]:
        raise ValueError("没读到净利润预测(同花顺那一节取数失败?)")
    m = re.search(r"生成时间\s*(\d{4}-\d{2}-\d{2})", p.stdout)
    notes = []
    if value["rating_failed_days"]:
        notes.append(f"巨潮评级有 {value['rating_failed_days']} 天取数失败,评级家数可能偏少")
    if value["ratings"] is None:
        notes.append("近 30 天巨潮没有评级记录(可能没人发,也可能巨潮缺数)")
    return {"value": value, "as_of": m.group(1) if m else fb_common.now_cn().date().isoformat(), "public_at": None,
            "source": "mini 上跑 consensus.py:同花顺盈利预测 + 巨潮评级(近 30 天)",
            "status": "partial" if notes else "ok", "note": ";".join(notes)}


def c_capex(cfg: dict, run=None) -> dict:
    """mini 上跑本仓的 capex.py --json。退出码 1(一家都没取到)→ 失败;2 或 JSON 里有 gaps / failed → partial。"""
    run = run or _mini(cfg)
    p = _need_ok(run((FETCH_DIR / "capex.py").read_text(encoding="utf-8"), CAPEX_ARGS, timeout=900),
                 "capex.py", codes=(0, 2))
    value = parse_capex_json(events.last_json(p.stdout))
    if not value["total"]:
        raise ValueError("capex.py 没给出任何季度")
    notes = []
    if value["gaps"]:
        notes.append("融资租赁没取到、按 0 计(该季和下一年同季的合计同比置空):"
                     + "、".join(f"{g.get('ticker')} {g.get('q')}" for g in value["gaps"]))
    if value["failed"]:
        notes.append("没取到:" + "、".join(f"{x.get('ticker')}({x.get('why')})" for x in value["failed"]))
    if p.returncode == 2 and not value["failed"]:
        notes.append("capex.py 退出码 2(有公司没取到)")
    return {"value": value, "as_of": value["total"][-1]["q"], "public_at": None,
            "source": "mini 上跑 capex.py --json:SEC EDGAR XBRL(现金购建 + 融资租赁)+ 8-K 指引",
            "status": "partial" if notes else "ok", "note": ";".join(notes)}


_DYN_PE = '''
def dyn_pe(code6):
    """腾讯行情第 52 项:市盈率(动) = 总市值 ÷ 最新累计净利润折成全年。"""
    import urllib.request
    sym = ("sh" if code6[0] in "69" else "sz") + code6
    req = urllib.request.Request("https://qt.gtimg.cn/q=" + sym,
                                 headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        f = r.read().decode("gbk", "replace").split("~")
    try:
        return float(f[52])
    except (IndexError, ValueError):
        return None
'''


def valuation_script(code: str) -> str:
    """送到 mini 的估值脚本:holdings_check.valuation_percentile 的源码原样 + 取动态市盈率,打一行 JSON。"""
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError(f"代码要 6 位数字,得到 {code!r}")
    return "\n".join([
        "import json, socket", "socket.setdefaulttimeout(30)",
        function_source(HOLDINGS_CHECK, "valuation_percentile"), _DYN_PE,
        f"vp = valuation_percentile({code!r}) or {{}}",
        "try:", f"    d = dyn_pe({code!r})", "except Exception:", "    d = None",
        "print(json.dumps({**vp, 'dyn_pe': d}, default=float))", ""])


def c_valuation(cfg: dict, code: str, run=None) -> dict:
    """PE / PB 历史分位(baostock,2020 年起,0~100)+ 动态市盈率(腾讯)。两样都是 preport / hcheck 用的源;
    本机连不上 baostock 和腾讯行情(2026-11-02 实测),所以在 mini 上跑。"""
    run = run or _mini(cfg)
    raw = events.last_json(_need_ok(run(valuation_script(code), [], timeout=300), "估值脚本").stdout)
    value = valuation_value(raw)
    if value["pe_pctl"] is None and value["pb_pctl"] is None:
        raise ValueError("baostock 没给出 PE / PB 历史")
    missing = [k for k, v in value.items() if v is None]
    return {"value": value, "as_of": raw.get("end"), "public_at": raw.get("end"),
            "source": "mini 上跑 holdings_check.valuation_percentile(baostock 估值历史,2020 年起)"
                      "+ 腾讯行情第 52 项(市盈率·动)",
            "status": "partial" if missing else "ok", "note": f"缺 {'、'.join(missing)}" if missing else ""}


def c_sec(cfg: dict, run=None, today: date | None = None) -> dict:
    """关注名单近 30 天交给 SEC 的文件 + 「年份 + 资本开支」原句(events.py 整份送到 mini 上跑)。
    整家没取到、或某份文件的正文 / 附件没读到(有 note 或 read 为空)→ partial:没读到的正文里可能正好有那句。"""
    run, t = run or _mini(cfg), _today(today)
    since = (t - timedelta(days=30)).isoformat()
    ciks = cfg.get("watch_sec") or events.SEC_CIKS
    res = events.sec_scan(ciks, since,
                          run_remote=lambda s, a: _need_ok(run(s, a, timeout=900), "events.py sec-here").stdout)
    if res["failed"] and len(res["failed"]) >= len(ciks):
        raise RuntimeError("一家都没取到:" + "、".join(x["ticker"] for x in res["failed"]))
    bad = [f"{x['ticker']} 没取到({x['why']})" for x in res["failed"]]
    unread = [f"{f.get('ticker')} {f.get('form')}" for f in res["filings"] if f.get("note") or not f.get("read")]
    if unread:
        bad.append(f"{len(unread)} 份正文没读到:{'、'.join(unread)}")
    return {"value": res["filings"], "as_of": t.isoformat(), "public_at": None,
            "source": "mini 上跑 events.py sec-here:SEC EDGAR submissions + 正文摘句",
            "status": "partial" if bad else "ok",
            "note": ";".join([*bad, f"{since} 以来 {len(res['filings'])} 份"])}


def c_account(cfg: dict, today: date | None = None) -> dict:
    """借款看板里的持股、可取现金、融资负债、每月还款(account.account_state,只读那个仓库)。
    这些是个人数据:完整的值只进内部仓的存档;给 AI 的输入包里只有持股数、还款数、可取现金和后续缺口
    (账户主人同意给的这几样),由 ai_answer.account_for_ai 筛,成本、截图价、净资产、备注不给。
    account_state 不抛出;持仓过期或读不出来 → partial,原因写进 note。"""
    st = account.account_state(cfg, _today(today))
    why = st.get("error") or (f"持仓数据截至 {st['asof']},超过 {account.STALE_DAYS} 天没更新" if st["stale"] else "")
    return {"value": st, "source": f"借款看板 {account.BROKER_FILE} 最新一笔 + {account.DEBT_FILE}",
            "as_of": st.get("asof"), "status": "partial" if st["stale"] else "ok", "used_by": "ai",
            "note": ";".join([why, *st.get("notes", [])]).strip(";")}

# ── 登记表 ──────────────────────────────────────────────────────────────────
DEFAULT_COLLECTORS = {
    "prices.<code>": c_prices,
    INDEX_KEY: c_index,
    "market.regime": c_regime,
    "market.margin": c_margin,
    "peer.<a>.<b>": c_peer,
    "consensus.<code>": c_consensus,
    "eps_revisions.<code>": c_eps_revisions,
    "valuation.<code>": c_valuation,
    "capex": c_capex,
    "taiwan": c_taiwan,
    "sectors": c_sectors,
    "events.calendar": c_calendar,
    "events.sec": c_sec,
    "account": c_account,
}

# 行业表(sectors.py)里的领先指标 → 取数器的键。不在这里的必须在 TOOL_GAPS 里写原因。
LEAD_INDICATORS = {
    "北美云厂 Capex 同比": "capex",
    "云厂下季度 Capex 指引": "capex",          # capex 取数器带 --guidance;微软、亚马逊在电话会口头给,取不到
}

TOOL_GAPS = {
    "海外同业股价": "输入包还没取美股同业日线(preport 第 7 层能取,没接进看板);财报日期在 events.calendar",
    "券商研报里的出货/产能": "要读研报 PDF 正文抽句(research --dig),输入包只收了盈利预测上下调(eps_revisions)",
    "下游晶圆厂资本开支": "capex 取数器只取四家云厂;要另跑 capex --tickers TSM,INTC,MU",
    "设备商同业资本开支/景气": "capex 取数器只取四家云厂;要另跑 capex --tickers AMAT,LRCX,KLAC",
    "面板厂资本开支（做面板模组设备时看这个）": "京东方 / TCL 科技现金流量表里的数要手工取,没有工具",
    "在手订单 / 合同负债": "要手工看财报「合同负债」科目,没有工具",
    "国际油价（Brent/WTI）": "commodity.py 能取,还没接进看板(看板目前只看 AI 算力链)",
    "全球油气资本开支": "capex 取数器只取四家云厂;要另跑 capex --tickers SLB,HAL,BKR,XOM,CVX",
    "北美钻机数（Baker Hughes rig count）": "rigcount.py 能取,还没接进看板",
    "海外油服同业股价": "输入包还没取美股同业日线",
    "渠道库存与批价": "没有工具(草根调研 / 经销商反馈)",
    "预收款 / 合同负债": "要手工看财报,没有工具",
    "社零同比": "akshare 宏观能取,还没接进看板",
    "集采/医保谈判结果": "没有工具(政策公告)",
    "临床进度 / 获批": "efdata ann 能看到公告,没有结构化工具",
    "海外 CXO 订单（若是 CXO）": "要看 Lonza / Catalent 财报,没有工具",
}
