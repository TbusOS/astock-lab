#!/usr/bin/env python3
"""board_scores —— 成绩页要用的一份数:总账、按层成绩、错题原因、要改的地方、两个 AI 比、比对卡、
季度业绩比对、版本链、旧决策日志的复核与执行。

它是什么:board_data.build_board_data 的「scores」那一块,单独一个文件,免得 board_data.py 太长。
没有它会怎样:成绩页只能看到「错了」,看不出错在哪一层、哪个原因、改了以后有没有变准。
读的文件(都可以没有,没有就照实写「还没有」):
  <存档根>/outcomes.jsonl、reviews.jsonl、improvements.jsonl、status.json、各次存档的 questions.json、
  chat_questions.jsonl(board.py add-question 加的题)、replays.jsonl(board.py replay 的结果)
add-question 加的题分两种来源(题目上的 source):
  journal  换算进来的上线前 journal 旧记录:只做原文比对 —— 不进各层成绩、不进两个 AI 比、不进比对卡和错题,
           单独列在 journal_questions(题目、换算说明、概率、结算结果、概率误差)
  chat     之后聊天里给的判断:照常算进作者那一层,按版本分组时归「聊天」(它不是哪版代码算的)
  <工作台>/data/journal.jsonl、data/backtest/revenue-summary.json
reviews.jsonl 里谁写的(who)要分开:
  who 是 user(board.py confirm 的默认值)才算「人确认」,进「按原因汇总(人确认过的)」;
  别的(claude 写的演示、codex 的提议、写了别的名字的)只算提议,页面写「Claude 提议,待 user 确认」,
  note 里有「演示」的写「Claude 提议,演示,待 user 确认」;概率和程序初值一模一样、或 note 写了
  「照抄程序初值」的写「演示记录(概率照抄程序初值),待 user 确认」;都不进汇总。
  为什么:原因概率要人看过证据才算数,AI 写进去的数要是也显示成「人确认」,
  user 会以为自己确认过;照抄初值的记录要是写成「提议」,user 会以为 AI 自己判断过。
  一道题用哪一条:有 user 确认的取最后一次 user 的(后来 AI 的提议不盖掉它);没有就取最后一次提议。
文件格式见 board_data.py 开头。检查:scripts/check_board_pages.py、check_board_scoring.py。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import fb_common  # noqa: F401  先于 archive:archive 会加载 market.py,它一 import 就删掉代理变量
import archive
import attribution
import scoring

OUTCOMES = "outcomes.jsonl"
CHAT = "chat_questions.jsonl"
REPLAYS = "replays.jsonl"
REPLAY_LIMIT = 20                   # 旧题重跑最多列多少次(最近的在前)
HZ_TEXT = {"63": "3 个月", "126": "半年", "250": "一年"}
JOURNAL_SRC, CHAT_SRC = "journal", "chat"
CHAT_VERSION = "聊天"               # 聊天题按版本分组时的名字
CARD_LIMIT = 30                     # 比对卡最多列多少张(最近结算的在前)
LINK_MARK = "预测存档"              # archive.link_journal 写的决策日志,thesis 以这个开头
MUST_DO = ("buy", "add", "trim", "sell")
_ZH_DO = ("清仓", "减仓", "加仓", "可加仓", "买")
TYPE_TEXT = {"up": "期末涨没涨", "interval": "期末落没落进区间", "bottom_month": "最低点在哪个月",
             "bottom_price_bin": "最低价在哪一段", "buy_signal_month": "买点信号在哪个月",
             "buy_vs_now": "信号日买比当天买多赚没有", "touch": "期间碰没碰到某价",
             "above_on": "某天收盘高没高于某价", "multiple_by": "期限内到没到某个倍数"}
LAYER_TEXT = {"baseline": "统计底线", "system": "系统算法", "claude": "Claude", "codex": "codex", "rule": "买点规则"}
HUMAN_WHO = "user"                  # board.py confirm --who 的默认值;只有它写的算人确认
HUMAN_TEXT = "人确认"
DEMO_MARK = "演示"
COPIED_MARK = "照抄程序初值"
SUGGEST_TEXT = "程序给的初值,待人确认"
NO_RULE_TEXT = "没有哪条规则触发,七个原因按均分给"
NO_WHY_TEXT = "这道题是以前结算的,那时没存程序给初值的依据,要人自己看"


def _jsonl(path: Path) -> list[dict]:
    """一行一条 JSON;读不出来的行跳过(页面只是展示,坏行由写它的那一侧负责报)。"""
    if not path.is_file():
        return []
    out = []
    for raw in path.read_bytes().split(b"\n"):
        try:
            rec = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (UnicodeDecodeError, ValueError):
            rec = None
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _questions(run: Path) -> list[dict]:
    q = _json(run / "questions.json")
    qs = q.get("questions") if isinstance(q, dict) else q
    return [x for x in qs or [] if isinstance(x, dict) and x.get("id")]


def _run_keys(root: Path, run: Path) -> list[str]:
    """题目里的 run 可能写成「日期/时分秒」或相对工作台的路径,两种都认。"""
    rel = f"{run.parent.name}/{run.name}"
    return [rel, f"data/forecasts/{rel}", f"{root.name}/{rel}"]


# ── 题目说了什么、实际怎样 ──────────────────────────────────────────────────
# 价位线写成「30.00 元(起点 50.00 的 60.0%,也就是跌 40.0%)」:人读得懂的是价和跌了多少,
# 「起点的 -40.0%」这种写法要停下来想半天。起点价(不复权)从哪来,按顺序:
#   题目上的 p0_close(add-question 记的)→ 结算时记的 start_close → 那次存档 run_meta 的 start_close
#   (scores_data 用 _with_start 填进 p0_close)。都没有 → 只写「起点价的 60.0%(也就是跌 40.0%)」。
def _price(x: float) -> str:
    return f"{float(x):,.2f}"


def _vs(r) -> str:
    """比值 → 跌 x% / 涨 x% / 持平(按一位小数)。"""
    d = (float(r) - 1) * 100
    return "持平" if abs(d) < 0.05 else f"{'跌' if d < 0 else '涨'} {abs(d):.1f}%"


def _start(q: dict):
    v = q.get("p0_close") or (q.get("resolution") or {}).get("start_close")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _line(r, start) -> str:
    """价位线:有起点价 → 「 30.00 元(起点 50.00 的 60.0%,也就是跌 40.0%)」(前面带空格);没有 → 「起点价的 60.0%(…)」。"""
    r = float(r)
    if start:
        return f" {_price(r * start)} 元(起点 {_price(start)} 的 {r * 100:.1f}%,也就是{_vs(r)})"
    return f"起点价的 {r * 100:.1f}%(也就是{_vs(r)})"


def said(q: dict) -> str:
    """一道题当时怎么说的,一句话。价位线写价、起点价、跌或涨多少(见上)。"""
    p, f, t, s = q.get("params") or {}, q.get("forecast"), q.get("type"), _start(q)
    tail = f":100 种里 {(f or {}).get('pct')} 种"
    if t == "up":
        return f"第 {p.get('n')} 个交易日收盘高于起点" + (f" {_price(s)} 元" if s else "价") + tail
    if t == "interval":
        lo, hi = float(f["lo"]), float(f["hi"])
        span = (f"{_price(lo * s)}~{_price(hi * s)} 元(起点 {_price(s)} 的 {lo * 100:.1f}%~{hi * 100:.1f}%)" if s
                else f"起点价的 {lo * 100:.1f}%~{hi * 100:.1f}%")
        return f"第 {p.get('n')} 个交易日收盘落在 {p.get('lo')}~{p.get('hi')} 分位,也就是{' ' if s else ''}{span}" + tail
    if t == "touch":
        way = "往上涨到" if p.get("dir") == "up" else "往下跌到"
        span = f"{p['n']} 个交易日内" if "n" in p else f"{p.get('end')} 以前"
        return f"{span}收盘{way}{_line(p['ratio'], s)}" + tail
    if t == "above_on":
        return f"{p.get('day')} 收盘高于{_line(p['ratio'], s)}" + tail
    if t == "multiple_by":
        span = f"{p['n']} 个交易日内" if "n" in p else f"{p.get('end')} 以前"
        return f"{span}涨到起点的 {p.get('k')} 倍" + (f"({_price(float(p['k']) * s)} 元)" if s else "") + tail
    if t == "buy_vs_now":
        return "买点规则:信号日买、拿 63 天,比当天买多赚(没有概率,只数胜负)"
    if isinstance(f, dict) and f:
        top = sorted(f.items(), key=lambda kv: -kv[1])[:3]
        return f"{TYPE_TEXT.get(t, t)}:" + "、".join(f"{k} {v} 种" for k, v in top)
    return TYPE_TEXT.get(t, str(t))


def actual(q: dict) -> str:
    """实际怎样:结算值是后复权比值(含分红,不等于两天收盘价之比),所以一律写「后复权算是起点价的 x%」;
    结算时记了那天的不复权收盘(end_close)就在前面写「收盘 x 元」。"""
    res = q.get("resolution") or {}
    o, v, t = res.get("outcome"), res.get("value"), q.get("type")
    day = res.get("end_day") or "?"
    if t in ("up", "interval", "touch", "above_on", "multiple_by"):
        word = {1: "发生了", 0: "没发生"}.get(o, "没结论")
        move = ""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            c = res.get("end_close")
            head = (f",收盘 {_price(c)} 元,后复权算是起点价的" if isinstance(c, (int, float)) and c > 0
                    else ",收盘后复权算是起点价的")
            move = f"{head} {float(v) * 100:.1f}%({_vs(v)})"
        return f"{day}:{word}{move}"
    if t == "buy_vs_now":
        return f"{day}:" + ({1: "信号日买更好", 0: "当天买更好"}.get(o, "期限内没出现买点,不计分"))
    return f"{day}:实际是 {o}"


def _scorable(r: dict, today: str) -> bool:
    res = r.get("resolution") or {}
    sa = str(res.get("scorable_after") or "")[:10]
    return bool(sa) and sa <= today and res.get("outcome") is not None


def is_wrong(r: dict) -> bool:
    """错题:给实际那一项的概率不到一半(是非题、区间题),或者分类题里实际那一类不是给得最多的那类。"""
    f, o = r.get("forecast"), (r.get("resolution") or {}).get("outcome")
    p = attribution.p_hit(f, o)
    if p is None:
        return False
    if isinstance(f, dict) and "pct" in f:
        return p < 50
    return p < max(v for v in f.values() if isinstance(v, int))


def _causes(probs: dict | None) -> list[list]:
    if not isinstance(probs, dict):
        return []
    return [[attribution.CAUSE_LABELS.get(c, c), int(probs.get(c, 0))] for c in attribution.CAUSES]


def is_human(rv) -> bool:
    """这条原因记录是 user 自己确认的(who 是 user,不分大小写)。"""
    return isinstance(rv, dict) and str(rv.get("who") or "").strip().lower() == HUMAN_WHO


def _all_causes(probs) -> dict:
    return {c: int((probs or {}).get(c, 0)) for c in attribution.CAUSES} if isinstance(probs, dict) else {}


def review_from(rv: dict, suggest: dict | None = None) -> str:
    """页面上「原因概率 · … ·」中间那段,写谁给的(平铺,不套括号):
    人确认 / 演示记录(概率照抄程序初值),待 user 确认 / Claude 提议,演示,待 user 确认 / codex 提议,待 user 确认。
    suggest = 这道题的程序初值;记录的概率和它七项都一样,就算照抄。"""
    if is_human(rv):
        return HUMAN_TEXT
    who = str(rv.get("who") or "").strip()
    name = {"claude": "Claude", "codex": "codex"}.get(who.lower(), who or "没写是谁")
    note = str(rv.get("note") or "")
    copied = COPIED_MARK in note or (bool(suggest) and _all_causes(rv.get("probs")) == _all_causes(suggest))
    if copied:
        return f"{'演示记录' if DEMO_MARK in note else name + ' 记录'}(概率{COPIED_MARK}),待 user 确认"
    return f"{name} 提议" + (f",{DEMO_MARK}" if DEMO_MARK in note else "") + ",待 user 确认"


def suggest_evidence(r: dict | None) -> list[str]:
    """程序给的原因初值凭什么:结算时存下的 suggest_why(触发了的规则,一条一句)。
    一条都没触发 → 说均分;以前结的题没存 → 照实说没存。没有初值 → 空列表。"""
    r = r or {}
    if not isinstance(r.get("suggest"), dict):
        return []
    why = r.get("suggest_why")
    if not isinstance(why, list):
        return [NO_WHY_TEXT]
    return [str(x) for x in why] or [NO_RULE_TEXT]


def evidence_of(r: dict | None, rv: dict | None) -> list[str]:
    """一道错题页面上列的证据:有原因记录用记录里的 evidence;没有就用程序初值的依据(suggest_evidence)。"""
    if rv:
        return [str(x) for x in rv.get("evidence") or []]
    return suggest_evidence(r)


def review_bits(r: dict | None, rv: dict | None) -> tuple[dict | None, str | None, str]:
    """一道题的原因概率用哪份、来源怎么写、谁哪天:(概率, 来源, 谁和日期)。
    有原因记录用记录的;没有就用结算时程序给的初值(r["suggest"]);都没有 (None, None, "")。"""
    sug = (r or {}).get("suggest") if isinstance((r or {}).get("suggest"), dict) else None
    if rv:
        day = str(rv.get("at") or "")[:10]
        return rv["probs"], review_from(rv, sug), (f"{rv.get('who')} {day}" if is_human(rv) else day)
    if sug:
        return sug, SUGGEST_TEXT, ""
    return None, None, ""


def pick_reviews(reviews: list[dict]) -> dict[str, dict]:
    """每道题用哪一条原因记录:有 user 确认的取最后一次 user 的;没有就取最后一次提议的。"""
    out: dict[str, dict] = {}
    for r in reviews:
        q = r.get("qid")
        if q and (is_human(r) or not is_human(out.get(q))):
            out[q] = r
    return out


# ── 各块 ────────────────────────────────────────────────────────────────────
def _calib(scored: list[dict]) -> dict:
    """是非题按说的概率分 10 档,每档实际发生的比例。"""
    out: dict[str, dict] = {}
    for r in scored:
        f = r.get("forecast")
        if r.get("type") == "interval" or not isinstance(f, dict) or "pct" not in f:
            continue
        b = min(9, int(f["pct"]) // 10)
        cell = out.setdefault(r["author"], {}).setdefault(b, [0, 0])
        cell[0] += int(r["resolution"]["outcome"] == 1)
        cell[1] += 1
    return {a: [[b * 10 + 5, round(hit / n, 3), n] for b, (hit, n) in sorted(bins.items())] for a, bins in out.items()}


def _wrong_list(scored: list[dict], reviews: dict) -> list[dict]:
    out = []
    for r in scored:
        if r.get("author") == "rule" or not is_wrong(r):
            continue
        rv = reviews.get(r["id"])
        probs, src, who = review_bits(r, rv)
        out.append({"id": r["id"], "code": r.get("code"), "author": LAYER_TEXT.get(r.get("author"), r.get("author")),
                    "type": TYPE_TEXT.get(r.get("type"), r.get("type")), "said": said(r), "actual": actual(r),
                    "causes": _causes(probs), "from": src, "who": who,
                    "evidence": evidence_of(r, rv), "note": (rv or {}).get("note") or ""})
    return out


def _ai_compare(records: list[dict], today: str) -> dict:
    """两个 AI 只比两边都答了的题(同一次存档、同一份输入包;键见 scoring.question_key)。"""
    key = scoring.question_key
    keys = {a: {key(r) for r in records if r.get("author") == a and _scorable(r, today)} for a in ("claude", "codex")}
    shared = keys["claude"] & keys["codex"]
    sub = [r for r in records if r.get("author") in ("claude", "codex") and key(r) in shared]
    lay = scoring.layer_report(sub, today)["layers"] if sub else {}
    pick = ("n_eff", "brier", "brier_multi", "interval_score", "direction_hit")
    return {"n": len(shared), **{a: {k: (lay.get(a) or {}).get(k) for k in pick} for a in ("claude", "codex")}}


def basis(answer, code: str) -> list[str]:
    """一层答卷里这只股票的依据(当时为什么这么说),给比对卡用。
    AI:动作理由 + 支持 / 反对 / 还没验证;系统算法:一年偏多少 + 五个信号各投了什么;
    答卷里没有这些字段 → 「这一层没写依据」;连答卷都没有 → 写查不到。"""
    if not isinstance(answer, dict):
        return ["这次存档里没有这一层的答卷,依据查不到"]
    b = (answer.get("stocks") or {}).get(code)
    if not isinstance(b, dict):
        return ["这一层没写依据"]
    out = []
    why = (b.get("action") or {}).get("why") if isinstance(b.get("action"), dict) else None
    if why:
        out.append(f"理由:{why}")
    ev = b.get("evidence") if isinstance(b.get("evidence"), dict) else {}
    for k, t in (("support", "支持"), ("against", "反对"), ("unverified", "还没验证")):
        out += [f"{t}:{x}" for x in ev.get(k) or []]
    vals = (b.get("signals") or {}).get("values") if isinstance(b.get("signals"), dict) else None
    wts = (b.get("signals") or {}).get("weights") if isinstance(b.get("signals"), dict) else None
    wts = wts if isinstance(wts, dict) else {}
    if isinstance(b.get("tilt"), (int, float)) or vals:
        sig = "、".join(f"{k} {v:+d}" + ("(权重 0,不计)" if wts.get(k) == 0 else "")
                       for k, v in (vals or {}).items() if isinstance(v, int))
        out.append(f"信号给的方向:一年偏 {b.get('tilt', 0):+.2f}(对数)" + (f",各信号 {sig}" if sig else ""))
    return out or ["这一层没写依据"]


def excess(res: dict):
    """扣掉同期大盘:本股后复权之比 − 创业板指收盘之比(board.py resolve 记的,同一段时间);没记就 None。"""
    sr, ir = res.get("stock_ratio"), res.get("index_ratio")
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (sr, ir)):
        return round(sr - ir, 6)
    return res.get("excess")


def _basis(r: dict, answers) -> list[str]:
    if r.get("author") == "rule":
        return ["买点规则本身(入场条件第一次满足那天买),不属于哪一层,没有概率"]
    if r.get("source") == "chat":
        return [f"聊天里给的判断:{r.get('note') or '没写说明'}"]
    return basis(answers(r.get("run"), r.get("author")), r.get("code"))


def _cards(scored: list[dict], reviews: dict, run_missing: dict, answers) -> list[dict]:
    out = []
    for r in sorted(scored, key=lambda x: str((x.get("resolution") or {}).get("end_day") or ""), reverse=True)[:CARD_LIMIT]:
        rv = reviews.get(r["id"])
        probs, src, who = review_bits(r, rv)
        out.append({"id": r["id"], "code": r.get("code"), "author": LAYER_TEXT.get(r.get("author"), r.get("author")),
                    "opened": str(r.get("opened"))[:10], "type": TYPE_TEXT.get(r.get("type"), r.get("type")),
                    "said": said(r), "actual": actual(r), "note": (r.get("resolution") or {}).get("note") or "",
                    "wrong": is_wrong(r), "missing": run_missing.get(r.get("run"), []),
                    "basis": _basis(r, answers), "excess": excess(r.get("resolution") or {}), "causes": _causes(probs),
                    "from": src, "who": who, "evidence": evidence_of(r, rv) if probs else []})
    return out


def _quarters(data_dir: Path):
    s = _json(data_dir / "backtest" / "revenue-summary.json")
    if not isinstance(s, dict):
        return None
    rows = []
    for r in s.get("逐季") or []:
        b = r.get("band") if isinstance(r, dict) else None
        if not isinstance(b, dict) or not all(isinstance(b.get(k), (int, float)) for k in ("low", "high", "mid")) \
                or not isinstance(r.get("real"), (int, float)):
            continue
        rows.append({"code": str(r.get("code")), "name": r.get("name") or "", "target": r.get("target"),
                     "low": round(b["low"] / 1e8, 2), "high": round(b["high"] / 1e8, 2), "mid": round(b["mid"] / 1e8, 2),
                     "real": round(r["real"] / 1e8, 2)})
    return {"made": s.get("生成于"), "rows": sorted(rows, key=lambda x: (x["code"], str(x["target"])))}


def replay_summary(cmp: dict) -> list[str]:
    """replay.compare 的结果 → 几句话:哪层哪只的涨的种数、见底月份变了多少;都没变写「能复现」。"""
    out = []
    for layer, rows in (cmp or {}).items():
        for code, d in sorted((rows or {}).items()):
            name = f"{LAYER_TEXT.get(layer, layer)} {code}"
            if not isinstance(d, dict):
                continue
            if "missing_in" in d:
                out.append(f"{name}:只有{'重跑结果' if d['missing_in'] == 'a' else '原来那份'}里有这只")
                continue
            ups = [f"{HZ_TEXT.get(h, h + ' 天')} {v:+d}" for h, v in (d.get("p_up") or {}).items() if v]
            big = max((d.get("bottom_months") or {}).items(), key=lambda kv: abs(kv[1]), default=None)
            parts = (["涨的种数 " + "、".join(ups)] if ups else []) + \
                ([f"见底月份最多差 {big[1]:+d} 种({big[0]})"] if big and big[1] else [])
            if parts:
                out.append(f"{name}:" + ";".join(parts))
    return out or ["两层的结果和原来一样(能复现)"]


def _replays(root: Path) -> list[dict]:
    out = []
    for r in reversed(_jsonl(root / REPLAYS)[-REPLAY_LIMIT:]):
        out.append({"at": str(r.get("at") or "")[:10], "run": r.get("run"), "code_ref": r.get("code_ref"),
                    "commit": str(r.get("commit") or "")[:7] or None, "frozen": bool(r.get("frozen")),
                    "corrected": r.get("corrected"), "changed": list(r.get("changed") or []),
                    "lines": replay_summary(r.get("compare"))})
    return out


def _runs(root: Path) -> list[dict]:
    out = []
    for r in reversed(archive.list_runs(root, complete_only=False)):
        m = _json(r / "manifest.json") or {}
        sup = _json(r / "superseded.json") or {}
        ans = {a: _json(r / "answers" / f"{a}.json") for a in ("baseline", "system", "claude", "codex")}
        out.append({"run": f"{r.parent.name}/{r.name}", "as_of": r.parent.name, "complete": bool(m),
                    "head": (m.get("git_head") or "")[:7] or None, "clean": m.get("clean"),
                    "superseded_by": sup.get("by"),
                    "failed": [a for a, v in ans.items() if isinstance(v, dict) and v.get("failed")],
                    "no_answer": [a for a, v in ans.items() if not isinstance(v, dict)]})
    return out


def _is_do(action) -> bool:
    s = str(action or "")
    return s in MUST_DO or s.startswith(_ZH_DO)


def _journal(data_dir: Path, today: str) -> dict:
    rows = [r for r in _jsonl(data_dir / "journal.jsonl") if not str(r.get("thesis") or "").startswith(LINK_MARK)]
    due = []
    for r in rows:
        chk = str(r.get("check_date") or "9999")
        st = r.get("status")
        if chk <= today and (st == "open" or (st == "superseded" and not r.get("outcome"))):
            due.append({"id": r.get("id"), "code": r.get("code"), "logged": str(r.get("logged_at") or "")[:10],
                        "check_date": chk, "action": r.get("action"), "price": r.get("price"),
                        "thesis": r.get("thesis") or "", "falsify": r.get("falsify") or "",
                        "superseded": st == "superseded"})
    ex = [{"id": r.get("id"), "code": r.get("code"), "action": r.get("action"), "deadline": r.get("deadline"),
           "done": (r.get("executed") or {}).get("result"), "note": (r.get("executed") or {}).get("note") or ""}
          for r in rows if _is_do(r.get("action"))]
    return {"n_old": len(rows), "due": due, "execution": ex}


def journal_brier(r: dict | None) -> float | None:
    """一道换算进来的旧记录照是非题评分:是非题的概率误差(Brier),四位小数;没结算、不计分的 None。
    它们不进各层成绩(只做原文比对),但「当时说得准不准」这一个数要照样给。"""
    f, o = (r or {}).get("forecast"), ((r or {}).get("resolution") or {}).get("outcome")
    if not isinstance(f, dict) or not isinstance(f.get("pct"), int) or isinstance(o, bool) or o not in (0, 1):
        return None
    return round(scoring.brier(f["pct"] / 100, o), 4)


def _journal_questions(root: Path, last: dict, reviews: dict) -> list[dict]:
    """换算进来的 journal 旧记录:结没结都列(没结的 actual、brier 是 None)。"""
    qs = {q["id"]: q for q in _jsonl(root / CHAT) if q.get("id") and q.get("source") == JOURNAL_SRC}
    qs.update({i: r for i, r in last.items() if r.get("source") == JOURNAL_SRC and i not in qs})
    out = []
    for q in sorted(qs.values(), key=lambda x: (str(x.get("opened")), x["id"])):
        r, rv = last.get(q["id"]), reviews.get(q["id"])
        probs, src, who = review_bits(r, rv)
        # 题目说法用题目记录合上结算记录:结算时补记的起点价(resolution.start_close)也能用上
        out.append({"id": q["id"], "code": q.get("code"), "author": LAYER_TEXT.get(q.get("author"), q.get("author")),
                    "opened": str(q.get("opened"))[:10], "said": said({**q, **(r or {})}), "note": q.get("note") or "",
                    "prob": (q.get("forecast") or {}).get("pct"), "actual": actual(r) if r else None,
                    "wrong": is_wrong(r) if r else None, "brier": journal_brier(r), "causes": _causes(probs),
                    "from": src, "who": who, "evidence": evidence_of(r, rv) if probs else []})
    return out


def _with_start(r: dict, starts: dict) -> dict:
    """存档里的题没有 p0_close 时,用那次存档 run_meta 的 start_close(不复权起点价)补上,只给页面写价位用。"""
    if r.get("p0_close"):
        return r
    sc = (starts.get(r.get("run")) or {}).get(r.get("code"))
    return {**r, "p0_close": sc} if isinstance(sc, (int, float)) and sc > 0 else r


def _is_chat(r: dict) -> bool:
    return r.get("source") == CHAT_SRC or str(r.get("run") or "").startswith(f"{CHAT_SRC}/")


def scores_data(root: Path, data_dir: Path, today: date) -> dict:
    """成绩页的数。root = 存档根(data/forecasts),data_dir = 它上一级(data)。"""
    t = today.isoformat()
    runs = archive.list_runs(root, complete_only=False)
    versions, run_missing, qids, run_dir, starts = {}, {}, set(), {}, {}
    for r in runs:
        head = (_json(r / "manifest.json") or {}).get("git_head") or scoring.UNKNOWN_VERSION
        inp = _json(r / "inputs.json") or {}
        miss = inp.get("missing_required") or []
        sc = (inp.get("run_meta") or {}).get("start_close") or {}
        for k in _run_keys(root, r):
            versions[k], run_missing[k], run_dir[k], starts[k] = head[:7], list(miss), r, sc
        qids |= {q["id"] for q in _questions(r)}
    qids |= {q["id"] for q in _jsonl(root / CHAT) if q.get("id")}
    last = {}
    for rec in _jsonl(root / OUTCOMES):
        if rec.get("id") and isinstance(rec.get("resolution"), dict):
            last[rec["id"]] = _with_start(rec, starts)
    for rec in last.values():
        if _is_chat(rec):
            versions[rec["run"]] = CHAT_VERSION
    records = [r for r in last.values() if r.get("source") != JOURNAL_SRC]    # journal 旧记录只做原文比对
    report = scoring.layer_report(records, t, versions)
    scored = [r for r in records if _scorable(r, t)]
    status = _json(root / "status.json") or {}
    # 已到期和已结算一样,算上换算进来的旧记录(总账一行里三个数要数同一批题)
    due = {r["id"] for r in last.values() if str(r["resolution"].get("scorable_after") or "9999")[:10] <= t}
    due |= {str(q) for q in status.get("due") or []}
    all_reviews = attribution.load_reviews(root) if (root / attribution.REVIEWS).is_file() else []
    reviews = pick_reviews(all_reviews)
    imps = attribution.load_improvements(root) if (root / attribution.IMPROVEMENTS).is_file() else []
    jr = _journal(data_dir, t)
    jsettled = [r for r in last.values() if r.get("source") == JOURNAL_SRC]
    jall = {q["id"] for q in _jsonl(root / CHAT) if q.get("source") == JOURNAL_SRC} | {r["id"] for r in jsettled}
    return {"totals": {"questions": len(qids | set(last)), "due": len(due), "resolved": len(last),
                       "journal_old": jr["n_old"], "journal_due": len(jr["due"]),
                       "journal_settled": len(jsettled), "journal_wrong": sum(1 for r in jsettled if is_wrong(r)),
                       "journal_total": len(jall), "journal_due_n": sum(1 for r in jsettled if r["id"] in due)},
            "report": report, "min_neff_chart": scoring.MIN_NEFF_FOR_CHART,
            "max_neff": max([v.get("n_eff") or 0 for v in report["layers"].values()] + [0]),
            "calib": _calib(scored), "wrong": _wrong_list(scored, reviews),
            "causes": [[attribution.CAUSE_LABELS[c], v] for c, v in
                       attribution.aggregate([r for r in all_reviews if is_human(r)]).items()],
            "improvements": [{"cause": attribution.CAUSE_LABELS.get(i.get("cause"), i.get("cause")), "file": i.get("file"),
                              "desc": i.get("desc"), "status": attribution.IMPROVEMENT_LABELS.get(i.get("status"), i.get("status")),
                              "commit": i.get("commit"), "at": str(i.get("at") or "")[:10]} for i in imps],
            "ai_compare": _ai_compare(records, t), "chain": [],
            "cards": _cards(scored, reviews, run_missing,
                            lambda run, a: _json(run_dir[run] / "answers" / f"{a}.json") if run in run_dir else None),
            "quarters": _quarters(data_dir), "runs": _runs(root), "replays": _replays(root),
            "journal_questions": _journal_questions(root, last, reviews), "journal_due": jr["due"],
            "execution": jr["execution"]}
