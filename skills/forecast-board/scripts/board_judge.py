#!/usr/bin/env python3
"""board_judge —— 看板第一屏三块的数:「怎么看」、「价格可能晃多大」那张图、「对照」。

它是什么:board_data.stock_block 拼每只股票时调这里,结果放在 stock["judge"]、stock["band"]、stock["ref"];
render_judge.py 只管排版。
没有它会怎样:第一屏只能摆各层的概率表,回答不了「手上的卖不卖、要不要买回、行业还值不值得待」;
也说不清规则和系统算法现在各说什么、它们过去检验得怎样。
为什么这样拼:
  - 「怎么看」以 Claude 的为主(Claude 这一块没写出来才用 codex 的),codex 三件事选的不一样时写出来,
    按固定选项(pick)比,不按措辞比 —— 措辞不同不算不同意。
  - 卖出线已触发时,「手上的」那格由程序按规则写(和结论条「要马上注意的」同一个判断),不用 AI 的:
    规则是事先定的,AI 说「拿着」也不改规则。手里没有、持仓读不出来也由程序写。
  - 「对照」的现状由程序算(系统算法 3 个月涨的可能、本股和创业板指离 60 日线、离一年高点);
    过去检验的结果从存档目录旁边的 history/<日期>/rules.json、thinking.json 读(最新那天),
    没有就不显示那几句 —— 机器路径不进公开仓,只按存档目录往上找。
  - 图只画历史波动参考(统计底线):起点是判断那天的收盘,每天的范围 = 起点 × exp(σ√k × 分位系数),
    σ 用底线答卷的年化波动 ÷ √250。它回答「会晃多大」,不判断方向。
检查:scripts/check_board_judge.py。
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import judgment
import system_forecast

NEAR_LINE = 0.05
DIP_LINE = -50.0                # 离一年高点跌超这么多(%)算「跌超一半」
SIGNAL_TEXT = {"regime": "大盘位置", "peer": "同行涨跌差", "consensus": "券商预测", "capex": "大型云服务公司投资",
               "valuation": "估值"}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _block(ans: dict, code: str) -> dict:
    if not isinstance(ans, dict) or ans.get("failed"):
        return {}
    b = (ans.get("stocks") or {}).get(code)
    return b if isinstance(b, dict) else {}


# ── 怎么看 ──────────────────────────────────────────────────────────────────
def _low_text(v) -> str:
    r = judgment.low_range(v)
    if r is None:
        return "没写"
    lo, hi = r
    f = lambda x: f"{x:.0f}" if x >= 100 else f"{x:.1f}".rstrip("0").rstrip(".")  # noqa: E731
    return f"约 {f(lo)}" if abs(hi - lo) < 1e-9 else f"约 {f(lo)}~{f(hi)}"


def holding_rule(shares, close, line) -> dict | None:
    """「手上的」那格要不要由程序写:持仓读不出来 / 手里没有 / 卖出线已触发 → 程序写的那一格;否则 None。"""
    if shares is None:
        return {"answer": "持仓读不出来", "when": "先更新借款看板",
                "why": "持仓数据读不出来,不知道手里有没有;先更新借款看板。", "pick": None}
    if shares == 0:
        return {"answer": "手里没有这只", "when": "这一格不用答", "why": "手里没有;要不要买回看下一行。", "pick": "none"}
    if _num(close) and _num(line) and line > 0 and close < line:
        return {"answer": "卖(待执行)", "when": f"卖出线 {line:g} 已触发(收盘 {close:g})",
                "why": f"卖出线 {line:g}:收盘 {close:g} 已跌破,按规则全部卖出。这条规则是事先定的,跟下面的判断无关。",
                "pick": "sell"}
    return None


def first_sentence(s: str, limit: int = 40) -> str:
    """旧答卷没有 when 时,拿理由的第一句顶上(太长截断)。"""
    t = str(s or "").strip()
    for sep in ("。", ";", ";"):
        if sep in t:
            t = t.split(sep, 1)[0]
            break
    return t if len(t) <= limit else t[:limit] + "…"


def _near_note(close, line) -> str:
    if _num(close) and _num(line) and line > 0 and close >= line and (close - line) / line <= NEAR_LINE:
        return f"(收盘 {close:g},离卖出线 {line:g} 只差 {(close - line) / line * 100:.1f}%)"
    return ""


def _three(j: dict, other: dict | None, other_author: str | None, rule: dict | None, near: str) -> list[dict]:
    out = []
    for k in judgment.THREE:
        b = j[k]
        why = str(b.get("why") or "")
        cell = {"key": k, "q": judgment.THREE_TEXT[k], "answer": str(b.get("answer") or ""),
                "when": (str(b.get("when") or "") or first_sentence(why)) + (near if k == "holding" else ""),
                "why": why, "pick": b.get("pick"), "by": "ai", "other": None}
        if k == "holding" and rule:
            cell.update(answer=rule["answer"], when=rule["when"], why=rule["why"], pick=rule["pick"], by="rule")
        ob = (other or {}).get(k) if judgment.usable(other) else None
        if isinstance(ob, dict) and cell["by"] == "ai" and ob.get("pick") in judgment.PICKS[k] \
                and ob.get("pick") != cell["pick"]:
            cell["other"] = {"author": other_author, "answer": str(ob.get("answer") or ""),
                             "pick_text": judgment.PICKS[k][ob["pick"]]}
        out.append(cell)
    return out


def _dates(j: dict, today: str) -> tuple[list[dict], dict | None]:
    rows = [{"date": str(x.get("date") or ""), "what": str(x.get("what") or ""), "why": str(x.get("why") or "")}
            for x in j.get("key_dates") or [] if isinstance(x, dict)]
    t = date.fromisoformat(today)
    rows.sort(key=lambda r: judgment.when_of(r["date"]) or date.max)
    nxt = next((r for r in rows if (judgment.when_of(r["date"]) or date.min) >= t), None)
    return rows, nxt


def judge_block(code: str, answers: dict, *, shares, close, line, today: str, verified: dict) -> dict:
    """一只股票的「怎么看」。answers = {层: 答卷};verified = {层: 有没有验证过}(data["validated"])。"""
    js = {a: _block(answers.get(a), code).get("judgment") for a in ("claude", "codex")}
    main = next((a for a in ("claude", "codex") if judgment.usable(js[a])), None)
    rule = holding_rule(shares, close, line)
    if main is None:
        why = [e for a in ("claude", "codex") if isinstance(js[a], dict) for e in js[a].get("errors") or []]
        return {"ok": False, "author": None, "errors": why[:3], "rule": rule}
    j, other = js[main], "codex" if main == "claude" else "claude"
    rows, nxt = _dates(j, today)
    return {"ok": True, "author": main, "verified": bool(verified.get(main)), "fact": str(j.get("fact") or ""),
            "view": str(j.get("view") or ""),
            "three": _three(j, js[other], other, rule, _near_note(close, line)),
            "scenarios": [{"label": str(x.get("label")), "p": x.get("p"), "what": str(x.get("what") or ""),
                           "low_when": str(x.get("low_when") or ""), "low_text": _low_text(x.get("low_price")),
                           "how": str(x.get("how") or "")} for x in j["scenarios"] if isinstance(x, dict)],
            "change_if": str(j.get("change_if") or ""), "key_dates": rows, "next": nxt,
            "facts": [{"kind": str(f.get("kind") or ""), "text": str(f.get("text") or ""),
                       "source": str(f.get("source") or "")} for f in j.get("facts") or [] if isinstance(f, dict)],
            "review": str(j.get("review") or ""), "rule": rule}


# ── 价格可能晃多大 ──────────────────────────────────────────────────────────
def band_params(code: str, answers: dict, start_close, history: list, as_of: str, ends: dict) -> dict | None:
    """画图要的:起点价、每天的波动 σ(底线答卷的年化波动 ÷ √250)、过去的收盘、各期限到期日。"""
    vol = _block(answers.get("baseline"), code).get("vol_annual")
    if not (_num(vol) and vol > 0 and _num(start_close) and start_close > 0):
        return None
    return {"s0": float(start_close), "sigma": float(vol) / math.sqrt(250), "as_of": as_of,
            "history": [[d, float(c)] for d, c in history if _num(c)], "ends": dict(ends)}


def band_q(p: dict, k: int, z: float) -> float:
    return p["s0"] * math.exp(p["sigma"] * math.sqrt(k) * z)


# ── 对照 ────────────────────────────────────────────────────────────────────
def _gap(a, b):
    return round((a / b - 1) * 100, 1) if _num(a) and _num(b) and b else None


def ref_now(code: str, answers: dict, layers: dict, series: list, regime, now=None) -> dict:
    """对照的现状:系统算法 3 个月涨的可能和这次用到的信号、本股和创业板指离 60 日线、离一年高点。
    now = [日期, 收盘]:比存档更新的收盘(刷新过行情时),有就接在日线后面算,和「手上的」那格同一个收盘。
    system_stopped:这次存档里用到、但现在的权重已经是 0 的信号(存档早于改权重时会有)。"""
    sysb = _block(answers.get("system"), code)
    w = ((sysb.get("signals") or {}).get("weights") or {}) if isinstance(sysb.get("signals"), dict) else {}
    w = w or dict(system_forecast.SIGNAL_WEIGHTS)
    used = [k for k, v in w.items() if _num(v) and v > 0]
    active = [SIGNAL_TEXT.get(k, k) for k in used]
    stopped = [SIGNAL_TEXT.get(k, k) for k in used if system_forecast.SIGNAL_WEIGHTS.get(k, 0) == 0]
    p63 = ((layers.get("system") or {}).get("horizons") or {}).get("63", {}).get("p_up")
    pts = [[str(d), c] for d, c in series]
    if isinstance(now, (list, tuple)) and len(now) == 2 and _num(now[1]) and (not pts or str(now[0]) > pts[-1][0]):
        pts.append([str(now[0]), float(now[1])])
    cl = [float(c) for _, c in pts if _num(c)]
    n = system_forecast.BUY_MA
    ma = sum(cl[-n:]) / n if len(cl) >= n else None
    hi = max(cl[-250:]) if cl else None
    r = regime if isinstance(regime, dict) else {}
    sgap, igap = _gap(cl[-1] if cl else None, ma), _gap(r.get("close"), r.get("ma60"))
    both = None if sgap is None or igap is None else (sgap > 0 and igap > 0)
    dd = _gap(cl[-1] if cl else None, hi)
    return {"system_p63": p63 if isinstance(p63, int) else None, "system_active": active, "system_stopped": stopped,
            "stock_ma_gap": sgap, "index_ma_gap": igap, "t60i_ok": both,
            "dd_1y": dd, "dip_hit": None if dd is None else dd <= DIP_LINE}


def latest_history(forecasts_root: Path) -> Path | None:
    """<forecasts>/history/<日期>/ 里最新、带 rules.json 或 thinking.json 的那天;没有 → None。"""
    h = Path(forecasts_root) / "history"
    if not h.is_dir():
        return None
    days = sorted((d for d in h.iterdir() if d.is_dir() and ((d / "rules.json").is_file()
                                                             or (d / "thinking.json").is_file())), reverse=True)
    return days[0] if days else None


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pc(x) -> str:
    return f"{x * 100:.0f}%"


def _rules_text(r: dict) -> dict:
    out = {}
    try:
        g = r["groups"]["main"]["summary"]
        h0, t0 = g["H"]["early"]["portfolio"], g["T60I"]["early"]["portfolio"]
        h1, t1 = g["H"]["late"]["portfolio"], g["T60I"]["late"]["portfolio"]
        out["t60i"] = (f"拿过去数据检验:2018–22 年中途最大亏损从一直拿着的 {_pc(h0['mdd'])} 压到 {_pc(t0['mdd'])};"
                       f"但 2023 年以后年化只有 {_pc(t1['cagr'])}(一直拿着 {_pc(h1['cagr'])})")
    except (KeyError, TypeError):
        pass
    try:
        m, c = r["groups"]["main"]["dip50"]["63"]["dip"], r["groups"]["control"]["dip50"]["63"]["dip"]
        nm, nc = len(r["meta"]["main"]), len(r["meta"]["control"])
        out["dip50"] = (f"拿过去数据检验:这样买,之后 3 个月,看板这批的 {nm} 只里 100 次有 {round(m['up'] * 100)} 次涨、"
                        f"同行业另外 {nc} 只 {round(c['up'] * 100)} 次;中途中位还要再跌 {abs(m['median_further']) * 100:.0f}%。"
                        "这是过去的样本,不代表这次的胜率")
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return out


def idea_counts(t: dict) -> dict | None:
    """逐条考想法的结果 → 几条有一点用、几条反了、几条跟碰巧分不开。
    一条想法只要在某一格(主池或对照组、2018–22 或 2023 起、3 个月或半年)看涨减看跌之差为正、
    且错开月份配出同样大的比例 ≤ 5%,算「有一点用」;为负且 ≤ 5% 算「反了」;都不是算「分不开」。
    同一条想法的另一种算法(名字带「线上口径」)不重复数。"""
    ideas = (t or {}).get("ideas")
    if not isinstance(ideas, dict):
        return None
    cnt = {"useful": 0, "reversed": 0, "chance": 0}
    for name, g in ideas.items():
        if "线上口径" in name:
            continue
        cells = [s.get("shift") or {} for grp in g.values() for p in ("early", "late")
                 for h, s in (grp.get(p) or {}).items() if h in ("63", "126")]
        sig = [c for c in cells if _num(c.get("chance")) and c["chance"] <= 0.05 and _num(c.get("spread"))]
        if any(c["spread"] > 0 for c in sig):
            cnt["useful"] += 1
        elif any(c["spread"] < 0 for c in sig):
            cnt["reversed"] += 1
        else:
            cnt["chance"] += 1
    return cnt


def cover_text(h: dict) -> dict:
    """历史补考(history_test)里历史波动参考的八成范围实际接住多少:{期限: (全部, 2023 年以来)}。"""
    out = {}
    for hz, x in (((h.get("summary") or {}).get("baseline") or {}).items()):
        try:
            out[str(hz)] = (_pc(x["cover80"]), _pc(x["by_period"]["late"]["cover80"]))
        except (KeyError, TypeError):
            continue
    return out


def ref_past(forecasts_root: Path) -> dict:
    """过去检验的几句话;找不到文件或读不出来的那几句不给。"""
    d = latest_history(forecasts_root)
    if d is None:
        return {}
    out = _rules_text(_read(d / "rules.json") or {})
    cov = cover_text(_read(d / "history.json") or {})
    if cov:
        out["cover"] = cov
    cnt = idea_counts(_read(d / "thinking.json") or {})
    if cnt:
        n = sum(cnt.values())
        out["ideas"] = (f"拿过去数据检验时,能写成简单规则的 {n} 条想法里,{cnt['chance']} 条跟碰巧分不开、"
                        f"{cnt['reversed']} 条是反的、{cnt['useful']} 条只在部分样本里有一点用")
    out["from"] = d.name if out else None
    return out
