#!/usr/bin/env python3
"""judgment —— AI 答卷里「怎么看」那一块:答卷骨架、校验、三件事的选项。

它是什么:看板第一屏要回答三件事 —— 手上的该不该卖、要不要买回、这个行业还值不值得待。
AI 每只股票除了概率题,还要按当天的事实写一段判断:已知事实一句、据此判断一句、三件事各一个答案、
2~4 种情况(主观概率、这段时间最低收盘价何时何价)、什么情况改判、关键日期、带出处的事实、复核日。
没有它会怎样:第一屏只能给「100 种走法里涨 50 种」这种数,回答不了「该怎么做、凭什么」;
判断写成一段散文也没法事后核对(哪条事实、哪种情况、哪天该看)。
为什么这样校验(和 stock-analysis-workflow 的 check_judgment.py 同一口径,公开仓不依赖它):
  - 事实至少 3 条,每条写明出自输入包哪一项(或查证笔记)—— 只有波动模拟、没有事实的不算判断;
  - 几种情况的概率加起来 100 —— 概率是主观估计,但要自洽,事后才能打分;
  - 关键日期至少一个在判断日之后、复核日在判断日之后 —— 不然没法知道哪天能看出对错;
  - 要写改判条件 —— 看到什么就承认判断错。
  三件事另带一个固定选项(pick),两个 AI 意见不同时页面按选项判断,不拿文字比(措辞不同不算不同意)。
不合格怎么办(ai_answer 里):第一次不合格和别的错误一起重问;第二次还不合格,只把这一块
换成 {"failed": true, "errors": [...]},答卷其余部分照用 —— 不让这一块把整份答卷拖成失败。
检查:scripts/check_board_judge.py。
"""
from __future__ import annotations

import re
from datetime import date

THREE = ("holding", "rebuy", "industry")
THREE_TEXT = {"holding": "手上的", "rebuy": "要不要买回", "industry": "这个行业"}
PICKS = {"holding": {"sell": "卖", "trim": "减一部分", "hold": "拿着", "none": "手里没有"},
         "rebuy": {"buy": "买", "wait": "先等等", "no": "不买"},
         "industry": {"stay": "值得待", "watch": "值得跟踪", "leave": "不值得待"}}
FACT_KINDS = ("需求", "供给", "情绪", "资金", "估值", "公司", "其他")
MIN_FACTS = 3
MIN_SCEN, MAX_SCEN = 2, 4
MIN_CHANGE = 10                 # 改判条件至少这么多个字
# 输入包里不在 inputs 下、但可以当出处的几项
EXTRA_SOURCES = ("查证笔记", "research_notes", "account", "rules", "baseline", "system")
_YM = re.compile(r"\d{4}-\d{2}")
_YMD = re.compile(r"\d{4}-\d{2}-\d{2}")


def skeleton() -> dict:
    """提示词里答卷骨架的 judgment 那一块。"""
    pick = {k: " / ".join(v) + " 选一个" for k, v in PICKS.items()}
    return {"fact": "已知:…(只写事实)", "view": "据此判断:…",
            **{k: {"pick": pick[k], "answer": "…(一句话)", "when": "…(一句适用条件)", "why": "…"} for k in THREE},
            "scenarios": [{"label": lab, "p": None, "what": "…", "low_when": "…", "low_price": [None, None],
                           "how": "价位怎么来的"} for lab in "ABC"],
            "change_if": "…会削弱…的判断,要重新检查…", "key_dates": [{"date": "YYYY-MM-DD 或 YYYY-MM", "what": "…", "why": "…"}],
            "facts": [{"kind": " / ".join(FACT_KINDS) + " 选一个", "text": "…", "source": "输入包里的键名或「查证笔记」"}],
            "review": "YYYY-MM-DD"}


def known_sources(package: dict) -> set[str]:
    """能当出处的名字:输入包 inputs 的键,加上 EXTRA_SOURCES。"""
    return set((package.get("inputs") or {}).keys()) | set(EXTRA_SOURCES)


def _text(x) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _prob(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 100


def _pos(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def when_of(s) -> date | None:
    """YYYY-MM-DD → 那天;YYYY-MM → 那个月 1 号;别的写法 → None。"""
    s = str(s or "").strip()
    try:
        if _YMD.fullmatch(s):
            return date.fromisoformat(s)
        if _YM.fullmatch(s):
            return date.fromisoformat(s + "-01")
    except ValueError:
        return None
    return None


def low_range(v) -> tuple[float, float] | None:
    """low_price 写一个数或 [下限, 上限] → (下限, 上限);写法不对 → None。"""
    if _pos(v):
        return float(v), float(v)
    if isinstance(v, (list, tuple)) and len(v) == 2 and _pos(v[0]) and _pos(v[1]) and v[0] <= v[1]:
        return float(v[0]), float(v[1])
    return None


def _check_three(w: str, j: dict) -> list[str]:
    out = []
    for k in THREE:
        b = j.get(k)
        if not isinstance(b, dict):
            out.append(f"{w}.{k} 缺了(要 pick、answer、when、why)")
            continue
        if b.get("pick") not in PICKS[k]:
            out.append(f"{w}.{k}.pick 要是 {' / '.join(PICKS[k])} 之一,得到 {b.get('pick')!r}")
        out += [f"{w}.{k}.{f} 要写" for f in ("answer", "when", "why") if not _text(b.get(f))]
    return out


def _check_scen(w: str, sc) -> list[str]:
    if not (isinstance(sc, list) and MIN_SCEN <= len(sc) <= MAX_SCEN and all(isinstance(x, dict) for x in sc)):
        return [f"{w}.scenarios 要 {MIN_SCEN}~{MAX_SCEN} 种情况"]
    out = []
    labels = [x.get("label") for x in sc]
    if not all(_text(x) for x in labels) or len(set(labels)) != len(labels):
        out.append(f"{w}.scenarios 每种情况要有不重复的 label(A、B、C…)")
    if not all(_prob(x.get("p")) for x in sc):
        out.append(f"{w}.scenarios 的 p 要是 0~100 的整数")
    elif sum(x["p"] for x in sc) != 100:
        out.append(f"{w}.scenarios 的概率加起来 {sum(x['p'] for x in sc)},要正好 100")
    for x in sc:
        lab = x.get("label") or "?"
        out += [f"{w}.scenarios.{lab}.{f} 要写" for f in ("what", "low_when", "how") if not _text(x.get(f))]
        if low_range(x.get("low_price")) is None:
            out.append(f"{w}.scenarios.{lab}.low_price 要是大于 0 的数,或 [下限, 上限](下限不大于上限)")
    return out


def _check_facts(w: str, facts, known: set[str]) -> list[str]:
    if not isinstance(facts, list) or len(facts) < MIN_FACTS:
        return [f"{w}.facts 至少 {MIN_FACTS} 条事实"]
    out = []
    for i, f in enumerate(facts):
        if not isinstance(f, dict) or not _text(f.get("text")):
            out.append(f"{w}.facts 第 {i + 1} 条要有 text")
            continue
        if f.get("kind") not in FACT_KINDS:
            out.append(f"{w}.facts 第 {i + 1} 条的 kind 要是 {' / '.join(FACT_KINDS)} 之一")
        s = str(f.get("source") or "")
        if not any(k in s for k in known):
            out.append(f"{w}.facts 第 {i + 1} 条没写出处(source 要写输入包里的键名,或「查证笔记」):{s!r}")
    return out


def _check_dates(w: str, j: dict, as_of: date) -> list[str]:
    out = []
    kd = j.get("key_dates")
    if not isinstance(kd, list) or not kd or not all(isinstance(x, dict) and _text(x.get("what")) for x in kd):
        out.append(f"{w}.key_dates 至少一条 {{date, what, why}}")
    elif not any((when_of(x.get("date")) or as_of) > as_of for x in kd):
        out.append(f"{w}.key_dates 要至少一个在判断日 {as_of} 之后(写 YYYY-MM-DD 或 YYYY-MM)")
    rv = when_of(j.get("review"))
    if rv is None or not _YMD.fullmatch(str(j.get("review"))) or rv <= as_of:
        out.append(f"{w}.review 要是判断日 {as_of} 之后的 YYYY-MM-DD,得到 {j.get('review')!r}")
    return out


def check(code: str, j, as_of: str, known: set[str]) -> list[str]:
    """一只股票的 judgment 的错误清单,空 = 合格。每条错误以「代码.judgment」开头。"""
    w = f"{code}.judgment"
    if not isinstance(j, dict):
        return [f"{w} 缺了"]
    day = date.fromisoformat(as_of)
    out = [f"{w}.{f} 要写" for f in ("fact", "view") if not _text(j.get(f))]
    out += _check_three(w, j)
    out += _check_scen(w, j.get("scenarios"))
    if not (_text(j.get("change_if")) and len(j["change_if"].strip()) >= MIN_CHANGE):
        out.append(f"{w}.change_if 要写什么情况改判(至少 {MIN_CHANGE} 个字,写成以后能核对的条件)")
    out += _check_dates(w, j, day)
    out += _check_facts(w, j.get("facts"), known)
    return out


def check_all(obj: dict, codes, as_of: str, known: set[str]) -> list[str]:
    stocks = (obj or {}).get("stocks") or {}
    return [e for c in codes if isinstance(stocks.get(c), dict) for e in check(c, stocks[c].get("judgment"), as_of, known)]


def mark_failed(obj: dict, errors: list[str]) -> dict:
    """把有错的那几只的 judgment 换成失败记录;不改原对象。"""
    bad = {}
    for e in errors:
        bad.setdefault(e.split(".judgment", 1)[0], []).append(e)
    stocks = {c: ({**s, "judgment": {"failed": True, "errors": bad[c]}} if c in bad and isinstance(s, dict) else s)
              for c, s in (obj.get("stocks") or {}).items()}
    return {**obj, "stocks": stocks}


def usable(j) -> bool:
    """页面能不能用这一块:是 dict、没标失败、三件事和情况都在(旧答卷没有这一块 → False)。"""
    return isinstance(j, dict) and not j.get("failed") and all(isinstance(j.get(k), dict) for k in THREE) \
        and isinstance(j.get("scenarios"), list) and bool(j["scenarios"])
