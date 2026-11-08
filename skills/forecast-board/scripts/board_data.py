#!/usr/bin/env python3
"""board_data —— 从一次存档和它旁边的文件,拼出看板页面要用的一份数(build_board_data)。

它是什么:页面只管排版,算数都在这里:价位换成起点那天的价、离卖出线多远、领先指标一句话、
缺了哪些数据、动作(actions.plan_actions)、成绩(scoring.layer_report)、原因份量……
render.render_board / render_scores 只读这里返回的 dict。检查用的 fixture
(scripts/fixtures/board/board_data.json)就是它的输出,换成合成数字。

没有它会怎样:页面模板里到处是取数和换算,改一个字段名要翻三个模板;
也没法拿一份固定的数检查页面。

文件约定(board.py 各子命令照这个写;缺了的文件当「还没有」,页面照实写,不报错):
  run = <工作台>/data/forecasts/<日期>/<时分秒>/
      manifest.json      archive.write_manifest 写的(提交号、工作区干不干净)
      inputs.json        {"items": {键: 一项}, "run_meta": {...}, "missing_required": [...]}
                         run_meta 里用到 as_of、start、holidays、start_close(没有就取前复权最后一根)
      answers/<层>.json  四份答卷 baseline / system / claude / codex(作答失败的是 {"failed": true, "errors"};
                         没有这份文件 = 这次没让它答,比如 forecast --no-codex,页面写「没让它答」不写「作答失败」)
      questions.json     这次出的题:[题目] 或 {"questions": [题目], …}
      analogs.json       (可选)历史同类情形 {代码 或 "all": analogs.summarize_episodes 的结果}
      superseded.json    被新版取代时 archive.supersede 写的
  配置(board.json)里可选的 key_dates = [{"date", "what"}]:手工登记的关键日期,页面标「手工登记」
  <工作台>/data/forecasts/
      outcomes.jsonl     结算记录,一行一条 {**题目, "resolution": {...}},同一题号以最后一行为准;
                         可带 "suggest": {原因: 整数}(程序给的原因初值,等人确认);
                         resolution 里 board.py resolve 另加 index_ratio / stock_ratio(从开题日到 end_day,
                         创业板指收盘之比、本股后复权之比),比对卡用两者之差写「扣掉同期大盘」
      chat_questions.jsonl  board.py add-question 加的题,结构同 questions.json 里的题,另带 source、note、added_at;
                         source="journal"(开题日早于第一次存档:换算进来的上线前旧记录,run 是 "journal/<开题日>",
                         只做原文比对)或 "chat"(之后聊天里给的判断,run 是 "chat/<开题日>")
      replays.jsonl      board.py replay 的结果 {"at", "run", "code_ref", "commit", "frozen", "corrected", "changed",
                         "compare"(replay.compare(存档两层, 重跑两层))},成绩页「旧题重跑」读它
      reviews.jsonl、improvements.jsonl   attribution.py 写的
      events.json        board.py update 刷新的财报日历和 SEC 新文件 {"saved_at", "run", "items": {键: 一项}}
                         (整份重写);run 是刷新时用的那次存档,和正在画的这次是同一次才用 ——
                         每日检查重画页面时接着用它,不退回存档那天的日历;出了新预测就用新存档里的
      actions_seen.json  每次画页面记的每只股票的动作种类和从哪天起 {"run", "saved_at", "stocks": {代码: {"kind", "since"}}}
                         (整份重写的状态文件,board_io.record_actions_seen 写);卖出、减仓的最晚日期靠它,
                         见 actions.py 开头。没有或读不出来:从今天算,顶栏写一句
      status.json        每日检查 {"checked_at", "triggered": [{"code","what","detail","kind"}], "due": [题号],
                         "unchecked": [{"code","what","detail"}](这次没查成的,可以没有这个键)}
                         kind = sell_line(卖出线)/ change_if(某次存档里 AI 的改主意条件)。
                         检查早于这次存档:change_if 那几条是旧答卷的条件,不再显示(新预测不该顶着旧 AI 的「已触发」);
                         文件在但读不出来:标 unreadable,顶栏写出来(不能当成「什么都没触发」)
  <工作台>/data/journal.jsonl                决策日志(journal.py)
  <工作台>/data/backtest/revenue-summary.json 季度营收走前回测(backtest.py --walkforward)
个人数据(持股、成本、卖出线)只在运行时从配置和借款看板读进来,输出写进内部仓指定的目录,
不进公开仓。返回的 dict 里不放任何本机路径。
检查:scripts/check_board_pages.py。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # noqa: F401  先于 prices:prices 会加载 market.py,它一 import 就删掉代理变量
import account
import actions
import attribution
import board_judge
import inputs
import position_limit
import prices
import scoring
import system_forecast
from board_scores import _jsonl, scores_data

LAYERS = ("baseline", "system", "claude", "codex")
AI = ("claude", "codex")
QUANTS = ("10", "25", "50", "75", "90")
HISTORY_BARS = 126          # 价格图画过去多少根(约 6 个月)
NEAR_LINE = 0.05            # 收盘离卖出线 5% 以内算「快触发」
USED_BY_TEXT = {"system": "参与系统计算", "ai": "只给 AI 参考", "display": "只展示"}
STATUS_TEXT = {"ok": "取到", "partial": "只取到一部分", "stale": "用了旧值", "failed": "取不到"}
KEY_TEXT = {"prices": "日线", "index": "创业板指日线", "market.regime": "大盘 60 日线", "market.margin": "融资余额",
            "peer": "同行涨跌差", "consensus": "机构盈利预测", "eps_revisions": "研报盈利预测上下调",
            "valuation": "估值分位", "capex": "云厂资本开支", "taiwan": "台湾光通信月营收", "sectors": "板块 ETF",
            "events.calendar": "财报日历", "events.sec": "SEC 新文件", "account": "持仓和还款",
            "history.long": "长日线(检验信号、算历史同类情形用)"}
_CODE = re.compile(r"\b\d{6}\b")
NOT_ASKED_TEXT = "这次没让它答(存档里没有这份答卷,比如出预测时加了 --no-codex)"
EVENTS_FILE = "events.json"
SEEN_FILE = "actions_seen.json"
SEEN_KINDS = ("sell", "trim", "buy_batches")      # 这几种动作有「最晚哪天做」


# ── 读文件 ──────────────────────────────────────────────────────────────────
def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def key_text(key: str) -> str:
    """输入项的键 → 中文名:consensus.300308 → 机构盈利预测 300308。"""
    parts = key.split(".")
    for n in range(len(parts), 0, -1):
        hit = KEY_TEXT.get(".".join(parts[:n]))
        if hit:
            rest = " ".join(parts[n:])
            return f"{hit} {rest}".strip()
    return key


def _meta(item, key: str | None = None) -> dict:
    """一项输入的出处信息(不含值)。数据去向:这一项记了 used_by 就用它;没记(老存档、手写的项)
    按键查 inputs.used_by_for —— 不能一律写「只给 AI 参考」,日线、大盘那几项是参与系统计算的。"""
    it = item if isinstance(item, dict) else {}
    st = it.get("status") or "failed"
    used = it.get("used_by") or inputs.used_by_for(str(key or it.get("key") or ""))
    return {"as_of": it.get("as_of"), "source": it.get("source") or "", "used_by": USED_BY_TEXT.get(used, "只给 AI 参考"),
            "status": st, "status_text": STATUS_TEXT.get(st, st), "note": it.get("note") or ""}


def _value(items: dict, key: str):
    it = items.get(key)
    return it.get("value") if isinstance(it, dict) and it.get("status") != "failed" else None


# ── 答卷 → 页面用的价 ───────────────────────────────────────────────────────
def _answer(run: Path, author: str) -> dict:
    a = read_json(run / "answers" / f"{author}.json")
    if not isinstance(a, dict):
        return {"author": author, "failed": True, "not_asked": True, "errors": [NOT_ASKED_TEXT]}
    return a


def _empty_view(errors: list, not_asked: bool = False) -> dict:
    return {"failed": True, "not_asked": not_asked, "errors": [str(e) for e in errors], "model": None, "horizons": {},
            "bottom": {}, "buy": {}, "action": None, "evidence": None, "tilt": None, "unvalidated": []}


PRICE_LAYERS = ("baseline", "system")   # 答卷里的价是后复权价、要按 start_price 换算的两层


def layer_view(ans: dict, code: str, close: float | None, author: str) -> dict:
    """一层答卷里这只股票那块 → 页面用的数,价位一律换成 close(起点那天的不复权收盘)那种价。
    统计底线、系统算法的价是后复权价,乘 close ÷ start_price;AI 的价本来就是这种价,不换算 ——
    AI 答卷里就算多写了一个 start_price(照抄输入包里那两层的),也不认(和 questions._base_price 一样)。
    最低价分段是比值,乘 close 变成价。"""
    if ans.get("failed"):
        return _empty_view(ans.get("errors") or ["作答失败"], bool(ans.get("not_asked")))
    block = (ans.get("stocks") or {}).get(code)
    if not isinstance(block, dict):
        return _empty_view([f"答卷里没有 {code}"])
    sp = block.get("start_price") if author in PRICE_LAYERS else None
    k = close / sp if _num(sp) and sp > 0 and _num(close) and close else 1.0
    hz = {h: {"p_up": b.get("p_up"), "q": {q: round(float(v) * k, 2) for q, v in (b.get("q") or {}).items() if _num(v)}}
          for h, b in (block.get("horizons") or {}).items() if isinstance(b, dict)}
    bottom = block.get("bottom") or {}
    scale = close if _num(close) and close else 1.0
    bins = [[round(float(r[0]) * scale, 2), round(float(r[1]) * scale, 2), r[2]] for r in bottom.get("price_bins") or []
            if isinstance(r, (list, tuple)) and len(r) == 3]
    buy = block.get("buy") or {}
    sys_buy = buy.get("conditions") if isinstance(buy.get("conditions"), list) else []
    return {"failed": False, "not_asked": False, "errors": [], "model": ans.get("model"), "horizons": hz,
            "bottom": {"month_probs": dict(bottom.get("month_probs") or {}), "price_bins": bins},
            "buy": {"month_probs": dict(buy.get("month_probs") or {}),
                    "zones": [[float(z[0]), z[1]] for z in buy.get("zones") or [] if isinstance(z, (list, tuple))
                              and len(z) == 2 and _num(z[0])],
                    "conditions": [str(c) for c in sys_buy]},
            "action": block.get("action") if isinstance(block.get("action"), dict) else None,
            "evidence": block.get("evidence") if isinstance(block.get("evidence"), dict) else None,
            "tilt": block.get("tilt"), "unvalidated": list(block.get("unvalidated") or [])}


def _start_close(code: str, meta: dict, items: dict) -> float | None:
    sc = (meta.get("start_close") or {}).get(code)
    if _num(sc) and sc > 0:
        return float(sc)
    qfq = (_value(items, f"prices.{code}") or {}).get("qfq") or []
    return float(qfq[-1][1]) if qfq and _num(qfq[-1][1]) else None


# ── 现在怎样 ────────────────────────────────────────────────────────────────
def _pct(a, b):
    return round((a / b - 1) * 100, 2) if _num(a) and _num(b) and b else None


def facts(series: list) -> dict:
    """前复权日线 → 离一年高点、近 1 / 3 个月涨跌(%)、买点规则那条均线(system_forecast.BUY_MA 日)
    和收盘在不在线上(ma / above_ma;入场条件「本股收盘在均线上」读它)。"""
    n = system_forecast.BUY_MA
    cl = [float(c) for _, c in series if _num(c)]
    if not cl:
        return {"high_1y": None, "dd_1y": None, "m1": None, "m3": None, "ma": None, "above_ma": None}
    hi = max(cl[-250:])
    ma = sum(cl[-n:]) / n if len(cl) >= n else None
    return {"high_1y": round(hi, 2), "dd_1y": _pct(cl[-1], hi),
            "m1": _pct(cl[-1], cl[-22]) if len(cl) > 21 else None,
            "m3": _pct(cl[-1], cl[-64]) if len(cl) > 63 else None,
            "ma": None if ma is None else round(ma, 2), "above_ma": None if ma is None else cl[-1] > ma}


def _row(label, text, tag, item) -> dict:
    return {"label": label, "text": text, "tag": tag, **_meta(item)}


def leading(items: dict, code: str) -> list[dict]:
    """领先指标:每项一句话 + 身份(已公布 / 机构的预测)+ 数据去向 + 日期来源。取不到的照样列。"""
    rows = []
    cap = _value(items, "capex")
    if items.get("capex") is not None:
        tot = (cap or {}).get("total") or []
        if tot:
            last = tot[-1]
            prev = f",上一季同比 {tot[-2]['yoy']:+.0f}%" if len(tot) > 1 and _num(tot[-2].get("yoy")) else ""
            yoy = f"同比 {last['yoy']:+.0f}%" if _num(last.get("yoy")) else "同比算不出(融资租赁缺口)"
            txt = f"{last.get('q')} 那季四家云厂资本开支合计 {last.get('sum')} 十亿美元,{yoy}{prev}"
        else:
            txt = "这次没取到"
        rows.append(_row("云厂资本开支", txt, "已公布", items.get("capex")))
    key = f"eps_revisions.{code}"
    if items.get(key) is not None:
        v = _value(items, key) or {}
        txt = (f"近 30 天券商盈利预测调高 {v.get('up30')} 篇、调低 {v.get('down30')} 篇(看 {v.get('fy')} 年净利润)"
               if v else "这次没取到")
        rows.append(_row("研报盈利预测上下调", txt, "券商研报 近 30 天发布的预测", items[key]))
    key = f"consensus.{code}"
    if items.get(key) is not None:
        prof = (_value(items, key) or {}).get("profit") or {}
        yrs = sorted(prof)
        if yrs:
            y = yrs[1] if len(yrs) > 1 else yrs[0]
            p = prof[y]
            txt = f"{y} 年净利润预测均值 {p.get('mean')} 亿({p.get('min')}~{p.get('max')})"
            tag = f"{p.get('n')} 家机构 截至 {items[key].get('as_of')} 发布的预测"
        else:
            txt, tag = "这次没取到", "机构 发布的预测"
        rows.append(_row("机构盈利预测", txt, tag, items[key]))
    tw = _value(items, "taiwan")
    if items.get("taiwan") is not None:
        comps = (tw or {}).get("companies") or []
        txt = (f"{tw.get('ym')} 月营收同比:" + "、".join(f"{c.get('name')} {c['yoy']:+.0f}%" for c in comps if _num(c.get("yoy")))
               if comps else "这次没取到")
        rows.append(_row("台湾光通信月营收", txt, "已公布", items["taiwan"]))
    key = f"valuation.{code}"
    if items.get(key) is not None:
        v = _value(items, key) or {}
        def show(x, suf=""):
            return "没取到" if x is None else f"{x}{suf}"
        txt = (f"市盈率历史分位 {show(v.get('pe_pctl'), '%')},市净率分位 {show(v.get('pb_pctl'), '%')},"
               f"动态市盈率 {show(v.get('dyn_pe'))}") if v else "这次没取到"
        rows.append(_row("估值", txt, "已公布", items[key]))
    for k in sorted(items):
        parts = k.split(".")
        if parts[0] == "peer" and len(parts) == 3 and code in parts[1:]:
            v = _value(items, k) or {}
            txt = (f"近 10 个交易日 {parts[1]} 减 {parts[2]} 的涨幅差 {v['gap10']:+.1f} 个百分点(近 20 日 {v['gap20']:+.1f})"
                   if _num(v.get("gap10")) and _num(v.get("gap20")) else "这次没取到")
            rows.append(_row("同行涨跌差", txt, "已公布", items[k]))
    return rows


def market_rows(items: dict) -> dict:
    out = {}
    r = _value(items, "market.regime")
    if items.get("market.regime") is not None:
        out["regime"] = {**_meta(items["market.regime"], "market.regime"), "value": r,
                         "text": (f"创业板指收盘 {r['close']},60 日均线 {r['ma60']},已连续 {r['days']} 个交易日"
                                  f"{'高于' if r['state'] == 'above' else '低于'} 60 日均线"
                                  if isinstance(r, dict) and r.get("state") else "这次没取到")}
    m = _value(items, "market.margin")
    if items.get("market.margin") is not None:
        out["margin"] = {**_meta(items["market.margin"], "market.margin"), "value": m,
                         "text": (f"两市融资余额 {m['balance_yi']:,.0f} 亿,近 20 个交易日 {m['chg20_pct']:+.1f}%"
                                  if isinstance(m, dict) and _num(m.get("balance_yi")) else "这次没取到")}
    return out


def missing_list(items: dict, missing_req: list) -> list[dict]:
    """「本次检查的数据缺什么」:取不到的必需项、还没工具的指标、没配行业,再加只取到一部分 / 用了旧值 /
    取不到的其他项。required 只标在 missing_required 里那些取不到的必需项上 —— 顶栏只为它们变色。"""
    out, seen = [], set()
    for k in missing_req or []:
        if k.startswith("gap:"):
            out.append({"key": k, "kind": "gap", "text": f"还没有取数工具:{k[4:]}"})
        elif k.startswith("sector."):
            out.append({"key": k, "kind": "sector", "text": f"{k[7:]} 没配行业,不知道该看哪些领先指标"})
        else:
            out.append({"key": k, "kind": "failed", "required": True, "text": f"取不到(必需):{key_text(k)}"})
        seen.add(k)
    for k in sorted(items):
        it = items[k]
        st = it.get("status") if isinstance(it, dict) else "failed"
        if k in seen or st not in ("partial", "stale", "failed"):
            continue
        note = (it.get("note") or "") if isinstance(it, dict) else ""
        out.append({"key": k, "kind": st, "text": f"{STATUS_TEXT[st]}:{key_text(k)}" + (f"({note})" if note else "")})
    return out


def _for_code(rows: list[dict], code: str) -> list[dict]:
    return [r for r in rows if code in r["key"] or not _CODE.search(r["key"])]


# ── 见底、反弹、买点 ────────────────────────────────────────────────────────
def rebound(items: dict, code: str, as_of: str) -> list[dict]:
    """反弹开始的确认信号:发生了才看得到,不是预测。lit = 现在亮没亮,None = 没取到数据。
    as_of 用来定「新一年开支计划」的目标年(plan_year)。"""
    out = []
    r = _value(items, "market.regime")
    state = r.get("state") if isinstance(r, dict) else None
    out.append({"name": "创业板指收盘回到 60 日线上", "lit": None if state is None else state == "above",
                "text": "" if state is None else f"现在在线{'上' if state == 'above' else '下'},已 {r.get('days')} 个交易日"})
    for k in sorted(items):
        parts = k.split(".")
        if parts[0] == "peer" and len(parts) == 3 and code in parts[1:]:
            v = _value(items, k) or {}
            g10, g20 = v.get("gap10"), v.get("gap20")
            ok = _num(g10) and _num(g20)
            out.append({"name": "同行涨跌差收窄", "lit": (abs(g10) < abs(g20)) if ok else None,
                        "text": f"近 10 日差 {g10:+.1f}、近 20 日差 {g20:+.1f} 个百分点" if ok else ""})
    e = _value(items, f"eps_revisions.{code}")
    ok = isinstance(e, dict) and _num(e.get("up30")) and _num(e.get("down30"))
    out.append({"name": "券商盈利预测转为调高", "lit": (e["up30"] > e["down30"]) if ok else None,
                "text": f"近 30 天调高 {e['up30']}、调低 {e['down30']}" if ok else ""})
    out.append(capex_plan_signal(items, as_of))
    return out


# 「云厂给出新一年开支计划」:要有和资本开支连在一起的金额,而且这个金额的年份(分句里离它最近的那个)≥ 目标年。
# 原来只要有一句资本开支指引就算亮 —— 每家每季都会说今年花多少,这个信号就永远亮着,没有意义。
_CAPEX_WORD = re.compile(r"capital expenditures?|capex", re.I)
_MONEY = re.compile(r"\$\s?\d[\d,.]*(?:\s*(?:to|-|–)\s*\$?\s?\d[\d,.]*)?\s*(?:billion|trillion|million)\b", re.I)
_OTHER_MONEY = re.compile(r"free cash flow|cash flow|revenue|income|earnings|margin|dividend|repurchase", re.I)
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)|\bFY ?(\d{2}|20\d{2})\b|\bfiscal (?:year )?(20\d{2})\b", re.I)
_LABEL = re.compile(r"^([A-Z][A-Z.]* \d{4}-\d{2}-\d{2}):(.*)$", re.S)


def plan_year(as_of: str) -> int:
    """「新一年」是哪年:4 月起是明年;1~3 月还是今年(云厂 1 月底~2 月初的四季报才给当年的数)。"""
    d = date.fromisoformat(str(as_of))
    return d.year + 1 if d.month >= 4 else d.year


def _years(text: str) -> set[int]:
    """句子里提到的年份。财年(FY27、fiscal 2027)按数字当成那一年,不换算成日历年 —— 认的限度,页面上写明。"""
    out = set()
    for a, b, c in _YEAR.findall(text):
        y = a or c or b
        out.add(int("20" + y if len(y) == 2 else y))
    return out


def _capex_amounts(text: str) -> set[tuple[int, int]]:
    """和资本开支连在一起的金额的位置 (起, 止):capex 词后面 120 字以内的金额(中间没有自由现金流、收入这类别的财务词),
    或者金额后面 40 字以内就是 capex 词(「$135 to $145 billion of capital expenditures」)。"""
    out = set()
    for m in _CAPEX_WORD.finditer(text):
        for n in _MONEY.finditer(text, m.end(), min(len(text), m.end() + 120)):
            if not _OTHER_MONEY.search(text[m.end():n.start()]):
                out.add(n.span())
    for n in _MONEY.finditer(text):
        gap = text[n.end():n.end() + 40]
        c = _CAPEX_WORD.search(gap)
        if c and not _OTHER_MONEY.search(gap[:c.start()]):
            out.add(n.span())
    return out


# 金额说的是哪一年:先拆分句,金额的年份 = 同一分句里离它最近的年份(按字符距离),分句里别的年份不算。
# 「$220 billion in 2026 and to meaningfully increase in 2027」拆开后金额那句只有 2026;
# 「$220 billion in 2026 with a meaningful increase in 2027」不拆,但离金额最近的是 2026;
# 「…, from the $15 billion of notes due 2031」的 2031 是债的到期年,先去掉再找。
# 分句按逗号、分号、and / but / while 拆;两个金额中间的 and 不拆(「$175 billion and $185 billion」)。
_CLAUSE = re.compile(r"[,;]|\b(?:and|but|while)\b(?!\s+\$)", re.I)
_DEBT_YEAR = re.compile(r"\b(?:due|matur\w*)\s+(?:in\s+)?20\d{2}\b", re.I)


def _year_spans(clause: str) -> list[tuple[int, int, int]]:
    """分句里每个年份的 (起, 止, 年)。债的到期年换成等长的空格去掉,别的字的位置不变。"""
    clean = _DEBT_YEAR.sub(lambda m: " " * len(m.group()), clause)
    return [(m.start(), m.end(), y) for m in _YEAR.finditer(clean) for y in _years(m.group())]


def _amount_year(span: tuple[int, int], years: list[tuple[int, int, int]]) -> int | None:
    """金额的年份:离它最近的那个年份(两者之间隔几个字);一样近取小的那年,宁可不亮。分句里没有年份 → None。"""
    s, e = span
    dist = [(max(ys - e, s - ye, 0), y) for ys, ye, y in years]
    return min(dist)[1] if dist else None


def _numeric_for(text: str, target: int) -> bool:
    """有没有一个和资本开支连着的金额(_capex_amounts),它的年份(_amount_year)≥ target。"""
    for c in _CLAUSE.split(text):
        years = _year_spans(c)
        if any((y := _amount_year(a, years)) is not None and y >= target for a in _capex_amounts(c)):
            return True
    return False


def _capex_sentences(items: dict) -> list[tuple[str, str]]:
    """[(「谁 哪天」, 原句)]:capex 取数器的指引(「AMZN 2026-10-29:原句」)+ SEC 新文件里摘到的全部句子。"""
    out = []
    for g in (_value(items, "capex") or {}).get("guidance") or []:
        m = _LABEL.match(str(g))
        out.append((m.group(1), m.group(2).strip()) if m else ("", str(g)))
    for fl in _value(items, "events.sec") or []:
        if isinstance(fl, dict):
            out += [(f"{fl.get('ticker', '')} {fl.get('date', '')}", str(s)) for s in fl.get("capex_sentences") or []]
    return out


def capex_plan_signal(items: dict, as_of: str) -> dict:
    """lit:True 有公司给了目标年及以后的开支数字;False 没有(只有口头说法时 text 写明);None 两项都没取到。"""
    name = "云厂给出新一年开支计划"
    if items.get("capex") is None and items.get("events.sec") is None:
        return {"name": name, "lit": None, "text": ""}
    target = plan_year(as_of)
    later = [(who, s) for who, s in _capex_sentences(items)
             if _CAPEX_WORD.search(s) and any(y >= target for y in _years(_DEBT_YEAR.sub(" ", s)))]
    num = [(who, s) for who, s in later if _numeric_for(s, target)]
    if num:
        return {"name": name, "lit": True, "text": f"{num[0][0]}:{num[0][1]}"}
    if later:
        return {"name": name, "lit": False, "text": f"有口头说法、还没给数字:{later[0][0]}:{later[0][1]}"}
    return {"name": name, "lit": False, "text": f"还没有云厂给 {target} 年及以后的开支数字"}


def _interp_above(q: dict, x: float) -> tuple[int, str | None]:
    """按五个分位价线性插值:收盘高于 x 的种数。低于 10 分位 → (90, "ge"),高于 90 分位 → (10, "le")。"""
    pts = [(int(k), float(q[k])) for k in QUANTS if _num(q.get(k))]
    if len(pts) < 5:
        return None, None
    if x <= pts[0][1]:
        return 90, "ge"
    if x >= pts[-1][1]:
        return 10, "le"
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if v1 <= x <= v2:
            below = p1 + (p2 - p1) * ((x - v1) / (v2 - v1) if v2 > v1 else 0)
            return int(round(100 - below)), None
    return None, None


def buy_odds(view: dict) -> list[dict]:
    """在几个候选价位买的赔率(模型推算,粗算):按这层第 63 个交易日的分位价,收盘高于买价的种数、
    中间价比买价高多少。假设在这个价买到,看 3 个月那天 —— 实际买入可能在这 3 个月中间,只当参考。"""
    q = ((view.get("horizons") or {}).get("63") or {}).get("q") or {}
    out = []
    for price, _ in view.get("buy", {}).get("zones") or []:
        p, bound = _interp_above(q, price)
        out.append({"price": price, "p_above": p, "bound": bound,
                    "median_pct": _pct(q.get("50"), price) if _num(q.get("50")) else None})
    return out


def _analogs(run: Path, code: str):
    a = read_json(run / "analogs.json")
    if not isinstance(a, dict):
        return None
    hit = a.get(code) or a.get("all")
    return hit if isinstance(hit, dict) else None


def _alerts(code: str, close, line, status: dict | None) -> list[dict]:
    """要马上注意的(最多 2 条):卖出线、每日检查触发的条件、到期没复核的题。"""
    out = []
    if _num(close) and _num(line) and line > 0:
        gap = (close - line) / line
        if close < line:
            out.append({"kind": "line", "text": f"收盘 {close:g} 已跌破卖出线 {line:g},按规则全部卖出"})
        elif gap <= NEAR_LINE:
            out.append({"kind": "line", "text": f"收盘 {close:g},离卖出线 {line:g} 只差 {gap * 100:.1f}%"})
    st = status or {}
    for t in st.get("triggered") or []:
        if str(t.get("code") or "") in ("", code):
            out.append({"kind": "triggered", "text": f"已触发:{t.get('what')}" + (f"({t.get('detail')})" if t.get("detail") else "")})
    due = [q for q in st.get("due") or [] if f"/{code}/" in str(q)]
    if due:
        out.append({"kind": "due", "text": f"有 {len(due)} 道预测到期还没复核(跑 board.py resolve)"})
    return out[:2]


def _view_main(layers: dict) -> str | None:
    return next((a for a in AI if not layers[a]["failed"]), None)


def stock_block(code: str, name: str, ctx: dict) -> dict:
    items, close = ctx["items"], ctx["start_close"].get(code)
    series = (_value(items, f"prices.{code}") or {}).get("qfq") or []
    hold = (ctx["state"].get("holdings") or {}).get(code) or {}
    shares = int(hold.get("shares") or 0) if not ctx["state"].get("stale") or hold else None
    now = ctx["closes"].get(code) or (series[-1] if series else [ctx["as_of"], close])
    layers = {a: layer_view(ctx["answers"][a], code, close, a) for a in LAYERS}
    main = _view_main(layers)
    f = facts(series)
    line = (ctx["config"].get("sell_lines") or {}).get(code)
    return {"code": code, "name": name, "held": bool(shares), "shares": shares,
            "cost": hold.get("cost") if hold else None, "close": now[1], "close_date": now[0],
            "start_close": close, "sell_line": line,
            "history": [[d, c] for d, c in series[-HISTORY_BARS:]], "facts": f,
            "prices_item": _meta(items.get(f"prices.{code}"), f"prices.{code}"), "action": ctx["plan"].get(code) or {},
            "alerts": _alerts(code, now[1], line, ctx["status"]),
            "layers": layers, "main": main, "leading": leading(items, code),
            "missing": _for_code(ctx["missing"], code), "rebound": rebound(items, code, ctx["as_of"]),
            "entry": _entry(layers, f, items), "odds": buy_odds(layers[main]) if main else [],
            "analogs": _analogs(ctx["run"], code),
            # 第一屏三块(board_judge.py):怎么看、价格可能晃多大那张图、对照的现状
            "judge": board_judge.judge_block(code, ctx["answers"], shares=shares, close=now[1], line=line,
                                             today=ctx["today"], verified=ctx["validated"]),
            "band": board_judge.band_params(code, ctx["answers"], close, series[-HISTORY_BARS:], ctx["as_of"],
                                            ctx["horizon_ends"]),
            "ref": board_judge.ref_now(code, ctx["answers"], layers, series, _value(items, "market.regime"), now)}


def _entry(layers: dict, f: dict, items: dict) -> list[dict]:
    """入场条件:系统算法的买点规则(现在满足没满足),再加 AI 写的条件(要人看)。"""
    r = _value(items, "market.regime")
    idx = r.get("state") == "above" if isinstance(r, dict) and r.get("state") else None
    n = system_forecast.BUY_MA          # 和系统算法算买点用的是同一条均线
    rules = layers["system"]["buy"].get("conditions") or [f"创业板指收盘在 {n} 日线上", f"本股收盘在 {n} 日线上"]
    out = []
    for txt in rules:
        okv = idx if "创业板" in txt else (f.get("above_ma") if "本股" in txt else None)
        out.append({"text": txt, "ok": okv, "who": "系统算法的买点规则"})
    for a in AI:
        for txt in layers[a]["buy"].get("conditions") or []:
            out.append({"text": txt, "ok": None, "who": f"{a} 写的"})
    return out


# ── 合起来 ──────────────────────────────────────────────────────────────────
def _closes(closes) -> dict:
    out = {}
    for c, v in (closes or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 2 and _num(v[1]):
            out[str(c)] = [str(v[0]), float(v[1])]
    return out


def _horizon_ends(meta: dict, config: dict, as_of: str) -> dict:
    """每个期限的到期日(约):从 start 起数交易日,节假日用配置里估的。"""
    hol = set(meta.get("holidays") or []) | actions.config_holidays(config)
    try:
        start = date.fromisoformat(meta.get("start")) if meta.get("start") else None
    except ValueError:
        start = None
    start = start or prices.future_trading_days(date.fromisoformat(as_of) + timedelta(days=1), 1, hol)[0]
    return {str(n): prices.future_trading_days(start, n, hol)[-1].isoformat()
            for n in (meta.get("horizons") or (63, 126, 250))}


def _dates(items: dict, note: str | None = None) -> dict:
    """关键日期那块。note = 财报日历、SEC 新文件为什么用的是存档里冻结的那份(没有就 None)。"""
    cal, sec = _value(items, "events.calendar"), _value(items, "events.sec")
    cal, sec = (cal if isinstance(cal, list) else []), (sec if isinstance(sec, list) else [])     # 手改坏了也不崩
    return {"calendar": [{"when": r.get("beijing") or r.get("date_us"), "who": f"{r.get('symbol')} {r.get('name') or ''}".strip(),
                          "relation": r.get("relation") or ""} for r in cal if isinstance(r, dict)],
            "calendar_item": _meta(items.get("events.calendar"), "events.calendar") if "events.calendar" in items else None,
            "sec": [{"date": s.get("date"), "who": f"{s.get('ticker')} {s.get('form')}", "url": s.get("url") or "",
                     "sentences": [str(x) for x in s.get("capex_sentences") or []]} for s in sec if isinstance(s, dict)],
            "sec_item": _meta(items.get("events.sec"), "events.sec") if "events.sec" in items else None,
            "events_note": note}


def key_dates(config: dict) -> list[dict]:
    """配置里手工登记的关键日期 key_dates = [{"date": 自由文字, "what": 什么事}](例:「2027-01 底~02 初」
    四家云厂给下一年资本开支计划)。取数器拿不到、但又要盯着的日子写在这里;格式不对的条目跳过。"""
    out = []
    for r in config.get("key_dates") or []:
        if isinstance(r, dict) and str(r.get("date") or "").strip() and str(r.get("what") or "").strip():
            out.append({"date": str(r["date"]).strip(), "what": str(r["what"]).strip()})
    return out


def _sectors(items: dict):
    v = _value(items, "sectors")
    if not isinstance(v, dict):
        return None
    rows = [{"code": c, "name": r.get("name"), "m1": r.get("m1"), "m3": r.get("m3"), "dd_1y": r.get("dd_1y")}
            for c, r in v.items() if isinstance(r, dict)]
    return {"rows": sorted(rows, key=lambda r: -(r["m3"] if _num(r["m3"]) else -1e9)), **_meta(items["sectors"], "sectors")}


def _validated(answers: dict, report: dict) -> dict:
    """哪一层算验证过:到期打过分的独立样本够 MIN_NEFF_FOR_CHART;系统算法还要信号都检验过。"""
    out = {}
    for a in LAYERS:
        n = ((report.get("layers") or {}).get(a) or {}).get("n_eff") or 0
        out[a] = n >= scoring.MIN_NEFF_FOR_CHART
    unval = any((b or {}).get("unvalidated") for b in (answers["system"].get("stocks") or {}).values())
    out["system"] = out["system"] and not unval and not answers["system"].get("failed")
    return out


def _at(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _is_change_if(t: dict) -> bool:
    return t.get("kind") == "change_if" or (not t.get("kind") and "改主意条件" in str(t.get("what") or ""))


def saved_events(root: Path, run: Path) -> tuple[dict, str | None]:
    """board.py update 存下的财报日历和 SEC 新文件 → (那几项, 没用上的原因)。是给这次存档刷新的才用
    (出了新预测,新存档里冻结的那份就是最新的,旧的 events.json 不再用);没用上时原因写一句,页面照写。"""
    path = root / EVENTS_FILE
    if not path.is_file():
        return {}, "这次存档以后还没刷新过"
    got = read_json(path)
    if not isinstance(got, dict) or not isinstance(got.get("items"), dict):
        return {}, f"{EVENTS_FILE} 读不出来"
    if got.get("run") != f"{run.parent.name}/{run.name}":
        return {}, f"{EVENTS_FILE} 是给别的存档刷新的"
    return got["items"], None


def read_seen(root: Path, run: Path) -> tuple[dict, str | None]:
    """上次画页面时记的动作种类和从哪天起 → ({代码: {"kind", "since"}}, 问题)。
    问题:None / "没有" / "读不出来";是别的存档记的 → ({}, None)(新存档从头记,不算问题)。"""
    path = root / SEEN_FILE
    if not path.is_file():
        return {}, "没有"
    got = read_json(path)
    if not isinstance(got, dict) or not isinstance(got.get("stocks"), dict):
        return {}, "读不出来"
    if got.get("run") != f"{run.parent.name}/{run.name}":
        return {}, None
    return {str(c): v for c, v in got["stocks"].items() if isinstance(v, dict)}, None


def read_status(root: Path, run: Path, made_at) -> dict | None:
    """每日检查的结果 → 页面用的那份;没有这个文件 → None。
    读不出来 → {"unreadable": True, …};检查做在这次存档以前 → before_run = True,
    并去掉改主意条件那几条(那是旧答卷的条件)。"""
    path = next((p for p in (root / "status.json", run / "status.json") if p.is_file()), None)
    if path is None:
        return None
    st = read_json(path)
    if not isinstance(st, dict):
        return {"unreadable": True, "checked_at": None, "triggered": [], "due": [], "unchecked": []}
    checked, made = _at(st.get("checked_at")), _at(made_at)
    try:
        before = bool(checked and made and checked < made)
    except TypeError:               # 一个带时区一个不带,比不了:当成不早于
        before = False
    if not before:
        return st
    return {**st, "before_run": True, "triggered": [t for t in st.get("triggered") or [] if not _is_change_if(t)]}


def _state_view(state: dict, plan_acc: dict, stale: bool) -> dict:
    """stale 用 actions.is_stale 判的那个(和动作同一个判断):state 可能是几天前算好的,
    只看 state["stale"] 会出现「顶栏说没过期、动作却是先更新持仓」。"""
    return {"asof": state.get("asof"), "stale": stale, "error": state.get("error"),
            "used_by": USED_BY_TEXT[inputs.used_by_for("account")],
            "free_cash": plan_acc.get("free_cash"), "free_cash_until": plan_acc.get("free_cash_until"),
            "month_need": plan_acc.get("month_need"), "need_month": plan_acc.get("need_month"),
            "cash": plan_acc.get("cash"), "sell_need": plan_acc.get("sell_need"),
            # 现金要留够几个月的还款(配置 cash_months):第一屏「现在」那句和结论条按它写
            **{k: plan_acc.get(k) for k in ("cover_months", "cover_need", "cover_from", "cover_until")},
            # 借款看板里的提醒(漏录的借款、银行卡现金……):第一屏「现在」那块要露出来
            "reminders": [str(n) for n in state.get("notes") or []],
            "note": plan_acc.get("note") or ""}


def build_board_data(run: Path, config: dict, today: date, *, state: dict | None = None,
                     closes: dict | None = None, events: dict | None = None, notices: list | None = None) -> dict:
    """一次存档 → 页面用的一份数(结构见 scripts/fixtures/board/board_data.json)。
    state  持仓和还款(account.account_state 的结果);不给就现读借款看板
    closes {代码: [日期, 收盘]},比存档里更新的收盘(board.py update 刷新行情时给);不给就用存档那天的
    events {"events.calendar": 一项, "events.sec": 一项},刷新过的财报日历和 SEC 新文件(board.py update 给);
           只用来排「关键日期」那块,不进缺数据清单、不动预测;不给就读 update 存下的 events.json
           (是给这次存档刷新的才用),再没有就用存档里冻结的那两项
    notices 这次刷新没取到的东西(一句一条,board.py update 给):页面顶栏和过期原因列在一起,
           不然取不到的那只悄悄用存档那天的收盘算动作,看着和取到了一样
    读不到的文件当「还没有」;不改任何文件。"""
    run, config = Path(run), config or {}
    t = today if isinstance(today, date) else date.fromisoformat(str(today))
    root, data_dir = run.parent.parent, run.parent.parent.parent
    manifest = read_json(run / "manifest.json", {}) or {}
    raw = read_json(run / "inputs.json", {}) or {}
    items = attribution.items_of(raw)
    meta = raw.get("run_meta") if isinstance(raw.get("run_meta"), dict) else {}
    as_of = meta.get("as_of") or run.parent.name
    codes = [str(s["code"]) for s in config.get("stocks") or []] or [str(c) for c in meta.get("codes") or []]
    names = {str(s["code"]): s.get("name") or str(s["code"]) for s in config.get("stocks") or []}
    miss_req = raw["missing_required"] if isinstance(raw.get("missing_required"), list) else \
        inputs.missing_required(items, config)
    answers = {a: _answer(run, a) for a in LAYERS}
    state = state if state is not None else account.account_state(config, t)
    start_close = {c: _start_close(c, meta, items) for c in codes}
    fresh = _closes(closes)
    px = {c: (fresh[c][1] if c in fresh else start_close[c]) for c in codes}
    claude = None if answers["claude"].get("failed") else answers["claude"]
    # 仓位上限:account.json 的参数 + 借款看板的净资产(按现在的收盘重算);算不出来的写进「本次缺什么」
    acct_cfg = position_limit.load_account_limits(data_dir.parent / position_limit.ACCOUNT_REL)
    lims = position_limit.limits(state, px, acct_cfg, _jsonl(data_dir / "journal.jsonl"))
    # 最晚日期从起点日和「这种动作第一次出现那天」里较晚的那天数(见 actions.py 开头)
    seen, seen_problem = read_seen(root, run)
    plan = actions.plan_actions(state, px, config, claude, t.isoformat(), limits=lims, start=meta.get("start"),
                                seen=seen)
    ev, ev_note = (events, None) if events is not None else saved_events(root, run)
    if not ev and "events.calendar" not in items and "events.sec" not in items:
        ev_note = None                  # 存档里本来就没有这两项,没什么可说的
    status = read_status(root, run, manifest.get("created_at"))
    scores = scores_data(root, data_dir, t)
    # 持仓过期时动作是「先更新持仓」,不按上限算,「本次缺什么」也不列上限那几行
    stale = actions.is_stale(state, t)
    missing = missing_list(items, miss_req) + ([] if stale else position_limit.missing_rows(lims))
    validated, ends = _validated(answers, scores["report"]), _horizon_ends(meta, config, as_of)
    ctx = {"items": items, "start_close": start_close, "state": state, "closes": fresh, "answers": answers,
           "config": config, "plan": plan, "status": status, "missing": missing, "run": run, "as_of": as_of,
           "today": t.isoformat(), "validated": validated, "horizon_ends": ends}
    stamps = [it.get("as_of") for k, it in items.items() if k.startswith(("prices.", "index.")) and isinstance(it, dict)]
    stamps += [v[0] for v in fresh.values()]
    return {"schema": 1, "today": t.isoformat(), "as_of": as_of, "run": f"{run.parent.name}/{run.name}",
            "made_at": manifest.get("created_at"),
            "code": {"head": (manifest.get("git_head") or "")[:7] or None, "clean": manifest.get("clean")},
            "market_asof": max((s for s in stamps if s), default=None),
            "horizon_ends": ends, "market": market_rows(items),
            "status": status if isinstance(status, dict) else None,
            "notices": [str(n) for n in notices or []],
            "account": _state_view(state, plan.get("account") or {}, stale),
            "failed_required": [m["key"] for m in missing if m.get("required")],
            "missing": missing,
            "layers_failed": {a: answers[a].get("errors") or ["作答失败"] for a in LAYERS
                              if answers[a].get("failed") and not answers[a].get("not_asked")},
            "layers_not_asked": [a for a in LAYERS if answers[a].get("not_asked")],
            "validated": validated, "ref_past": board_judge.ref_past(root),
            "stocks": [stock_block(c, names.get(c, c), ctx) for c in codes],
            "dates": _dates({**items, **{k: v for k, v in ev.items() if isinstance(v, dict)}},
                            None if ev else ev_note and f"财报日历和 SEC 新文件用的是存档里冻结的那份({ev_note})"),
            "actions_seen": {"run": f"{run.parent.name}/{run.name}",
                             "stocks": {c: {"kind": plan[c]["kind"], "since": plan[c]["since"]} for c in codes}},
            "seen_problem": seen_problem if any(plan[c]["kind"] in SEEN_KINDS for c in codes) else None,
            "key_dates": key_dates(config), "sectors": _sectors(items), "scores": scores}
