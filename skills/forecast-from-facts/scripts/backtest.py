#!/usr/bin/env python3
"""回测 —— 拿季报实际值,回头核我们当时说的对不对。

    backtest.py [--raw data/raw] [--predictions private/predictions]
                [--md 回测.md]

## 这个脚本回答两个不同的问题

| 档案 | 存什么 | 回答什么 |
|---|---|---|
| `private/predictions/<代码>-<季度>.json` | 当时推的**营收 / 净利区间** + 每个方法的依据和失效条件 | **推得准不准** —— 实际值落在区间里没有,偏了多少 |
| `data/journal.jsonl` | 当时的**动作 / 三情景价位 / 概率 / 期望值 / 可证伪条件** | **判断得准不准** —— 期望值预期的涨跌和实际涨跌差多少,概率给得偏不偏 |

**两件事必须分开。** 区间推准了但仓位建议是错的,和区间推错了但方向蒙对了,
是完全不同的两种情况,而混在一起看会得出「我们大概还行」这种没用的结论。

## 概率校准比单次对错重要

单次「bull 兑现了」不说明概率给对了 —— 25% 的事情发生了很正常。
**概率只能在样本上检验**:记 20 次 bull 25%,实际发生了 10 次,
那不是运气好,是概率系统性给低了。所以这个脚本按**概率档位分组**统计,
不看单条。样本不够时如实说样本不够,**不给一个「命中率 60%」的假结论**。

## 第三件事:`--walkforward` 走前回测

    backtest.py --walkforward --raw data/raw

把时间倒回去,**只用截至目标季前一季的数据**跑同样这三个方法,再和后来真实
发生的比。它回答的是上面两件事都回答不了的一个问题:**这套推法本身,
在过去这些季度里到底准不准。** 结果写进 `<raw 的上一级>/backtest/
revenue-summary.json`,报告第 3 节从那个文件读 —— **一个百分比都不写死在报告里**。

两条纪律:
  · **回测程序和生产程序是同一套代码**(直接调 `forecast.m_inventory` 等),
    另写一份的话量的就不是生产上真正在跑的东西
  · 截断要干净:最后一行的「下季营收÷期末存货」必须清成 None ——
    那一格用的是目标季当季的营收,是未来信息,不清等于给回测开后门

## 它现在能做什么

预测的都是 2026Q3,而三季报的法定披露截止日是 2026-10-31 —— **还没到**。
所以现在跑它只会打印「还没有一条到期」。这不是白跑:
**核对的机制要在需要它之前就能跑通**,等到季报出来那天再写脚本,
那天要同时干两件事,而其中一件会被草草做掉。

依赖:无(只读盘上的 json / jsonl,不联网)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import statistics
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

import quarterly


def load_predictions(d: Path) -> list:
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        j["_文件"] = f.name
        out.append(j)
    return out


def load_journal(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def actual_quarter(raw: Path, code: str, period: str):
    """那一季的实际单季营收和归母净利。**没披露就返回 None,不拿别的季顶替。**"""
    try:
        qs = quarterly.build(raw, code, since="2022")["quarters"]
    except SystemExit:
        return None
    for r in qs:
        if r["period"] == period:
            return {"营收": r.get("单季营收"), "归母净利": r.get("单季归母净利")}
    return None


def hit(band: dict | None, actual):
    """实际值落没落在区间里 → (命中?, 相对中枢偏了多少 %)。"""
    if not band or actual is None:
        return None, None
    inside = band["low"] <= actual <= band["high"]
    dev = (actual / band["mid"] - 1) * 100 if band.get("mid") else None
    return inside, dev


def grade(inside, dev, ref):
    """「落在区间里」和「偏中枢多少」合起来给一句评价。ref = 走前回测的中枢误差中位数(%)。

    只看区间会两头看错(2026-10-30 第一次核三季度):区间宽(中位 45%)时落在里面说明不了
    什么;区间窄时差一点就判「没中」—— 杰瑞、深科达只偏中枢 4%~6%,因为只有 1~2 个
    方法能用、区间太窄被判错。分界线用走前回测的历史中位数，不拍一个 10%。
    """
    if inside is None or dev is None:
        return "—"
    if ref is None:
        return ("在区间里" if inside else "落在区间外") + "(没有走前回测参照，不判准不准)"
    near = abs(dev) <= ref
    if inside:
        return "✅ 准" if near else "◐ 在区间里，但偏中枢超过历史中位 —— 是区间宽救了它"
    return "◐ 中枢准、区间太窄" if near else "❌ 错"


def _width(band: dict | None):
    return None if not band or not band.get("low") else (band["high"] / band["low"] - 1) * 100


def _ref_dev(raw: Path):
    """走前回测(--walkforward)存下的营收中枢误差中位数;没跑过就返回 None。"""
    p = raw.parent / "backtest" / "revenue-summary.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["并集"]["中位绝对偏差"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def price_on(raw: Path, code: str, on_or_after: str):
    """某天(或之后第一个交易日)的前复权收盘价。找不到返回 None。"""
    fs = sorted(glob.glob(str(raw / f"quotes/{code}/*-qfq.json")))
    if not fs:
        return None
    try:
        j = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for env in j.values():
        if not (env.get("ok") and env.get("data")):
            continue
        for row in env["data"]:
            d, close = row[0], row[4]
            if d >= on_or_after:
                try:
                    return {"date": d, "close": float(close)}
                except (TypeError, ValueError):
                    return None
    return None


def build(raw: Path, pred_dir: Path, today: str | None = None) -> str:
    today = today or dt.date.today().isoformat()
    preds = load_predictions(pred_dir)
    jr = load_journal(raw.parent / "journal.jsonl")
    L = ["# 回测:我们说过的话,后来对了没有", ""]
    L.append(f"跑于 {today} · 预测存档 {len(preds)} 份 · 决策日志 {len(jr)} 条")
    L.append("")
    L.append("> **两件事分开看**:预测存档回答「区间推得准不准」,"
             "决策日志回答「判断和概率给得准不准」。区间推准了但仓位建议是错的,"
             "和区间推错了但方向蒙对了,是完全不同的两种情况 —— "
             "混在一起只会得出「我们大概还行」这种没用的结论。")
    L.append("")

    # ── 一 预测区间 ────────────────────────────────────────────────
    L.append("## 1 预测区间:实际值落在里面了吗")
    L.append("")
    rows, pending = [], []
    for p in preds:
        code, period = p.get("code"), p.get("预测期")
        act = actual_quarter(raw, code, period) if code and period else None
        if not act or act.get("营收") is None:
            pending.append((code, period, p.get("_文件")))
            continue
        rv_in, rv_dev = hit(p.get("营收区间"), act["营收"])
        ni_in, ni_dev = hit(p.get("归母净利区间"), act["归母净利"])
        rows.append({"code": code, "period": period, "act": act,
                     "rv": (rv_in, rv_dev), "ni": (ni_in, ni_dev),
                     "w": _width(p.get("营收区间")), "n": p.get("可用方法数")})
    ref = _ref_dev(raw)
    if rows:
        L.append("| 代码 | 预测期 | 用了几个方法 | 实际营收(亿) | 区间宽 | 落在区间? | 偏中枢 "
                 "| 评价 | 实际归母净利(亿) | 落在区间? | 偏中枢 |")
        L.append("|---|---|---:|---:|---:|:-:|---:|---|---:|:-:|---:|")
        for r in rows:
            L.append(f"| {r['code']} | {r['period']} | {r['n']} "
                     f"| {r['act']['营收'] / 1e8:,.1f} "
                     + (f"| {r['w']:.0f}% " if r["w"] is not None else "| — ")
                     + f"| {'✅' if r['rv'][0] else '❌'} "
                     f"| {r['rv'][1]:+.1f}% "
                     f"| {grade(r['rv'][0], r['rv'][1], ref)} "
                     f"| {(r['act']['归母净利'] or 0) / 1e8:,.1f} "
                     f"| {'✅' if r['ni'][0] else '❌'} "
                     + (f"| {r['ni'][1]:+.1f}% |" if r["ni"][1] is not None else "| — |"))
        L.append("")
        ok_n = sum(1 for r in rows if r["rv"][0])
        devs = [abs(r["rv"][1]) for r in rows if r["rv"][1] is not None]
        L.append(f"**{ok_n}/{len(rows)} 只的营收落在我们的区间里。**")
        if devs and ref is not None:
            near = sum(1 for x in devs if x <= ref)
            L.append("")
            L.append(f"**{near}/{len(devs)} 只的营收偏中枢不超过历史中位({ref:.1f}%)**,"
                     f"这一批中枢误差中位 {statistics.median(devs):.1f}%。"
                     f"「落在区间里」和「偏中枢多少」要一起看:区间宽时前者说明不了什么,"
                     f"区间窄时差一点就判没中。")
        elif devs:
            L.append("")
            L.append(f"这一批营收中枢误差中位 {statistics.median(devs):.1f}%。"
                     f"还没跑过 `--walkforward`,没有历史参照，判不了算准还是不准。")
        if len(rows) < 10:
            L.append("")
            L.append(f"⚠ **只有 {len(rows)} 个样本,这个比例说明不了什么。** "
                     f"区间命中率要几十个样本才有意义 —— 现在它只是一个记录,"
                     f"不是一个结论。**不要拿它去调阈值**:用 5 个点挑规则,"
                     f"几乎必然挑出噪声。")
    else:
        L.append("**还没有一份预测到了可以核的时候。**")
    if pending:
        L.append("")
        L.append("等季报的:")
        L.append("")
        L.append("| 代码 | 预测期 | 存档文件 | 什么时候能核 |")
        L.append("|---|---|---|---|")
        for code, period, fn in pending:
            q = (period or "")[4:]
            due = {"Q1": "4-30", "Q2": "8-31", "Q3": "10-31", "Q4": "次年 4-30"}.get(q, "?")
            L.append(f"| {code} | {period} | `{fn}` | {period[:4]} 年 {due}"
                     f"(法定披露截止日) |")
        L.append("")

    # ── 二 概率校准 ────────────────────────────────────────────────
    L.append("## 2 概率校准:期望值预期的涨跌 vs 实际涨跌")
    L.append("")
    L.append("**单次对错不说明概率给对了** —— 25% 的事情发生了很正常。"
             "概率只能在样本上检验:记 20 次 bull 25%,实际发生了 10 次,"
             "那不是运气好,是概率系统性给低了。所以这里按**概率档位分组**统计,"
             "不看单条。")
    L.append("")
    due, waiting = [], []
    for r in jr:
        if r.get("status") != "open":
            continue
        ck = r.get("check_date") or "9999-99-99"
        (due if ck <= today else waiting).append(r)
    if due:
        L.append("| # | 代码 | 记于 | 当时价 | 动作 | 当时的期望值 | 复核日的价 "
                 "| 实际涨跌 | 差 |")
        L.append("|---|---|---|---:|---|---:|---:|---:|---:|")
        for r in due:
            px = price_on(raw, r["code"], r["check_date"])
            if not px or not r.get("price"):
                L.append(f"| #{r['id']} | {r['code']} | {r['logged_at'][:10]} "
                         f"| {r.get('price')} | {r.get('action')} | {r.get('ev')}% "
                         f"| **拿不到复核日的价** | — | — |")
                continue
            real = (px["close"] / r["price"] - 1) * 100
            ev = r.get("ev")
            L.append(f"| #{r['id']} | {r['code']} | {r['logged_at'][:10]} "
                     f"| {r['price']:,.2f} | {r.get('action')} | {ev:+.1f}% "
                     f"| {px['close']:,.2f}({px['date']}) | **{real:+.1f}%** "
                     f"| {real - ev:+.1f}pp |")
        L.append("")
        L.append("⚠ **「实际涨跌」是复核日那一天的价,不是这段时间的最好或最差价。** "
                 "期望值说的就是「到复核日为止平均能赚多少」,所以要拿那一天的价比。"
                 "拿区间最高价比会让每条记录都显得判断得很好。")
    else:
        L.append("**还没有一条到复核日。**")
    L.append("")
    if waiting:
        earliest = min(r.get("check_date") or "9999" for r in waiting)
        L.append(f"未结 {len(waiting)} 条,**最早复核日 {earliest}**,待季报。")
        L.append("")
        L.append("| # | 代码 | 记于 | 动作 | 期望值 | bear/base/bull | 复核日 |")
        L.append("|---|---|---|---|---:|---|---|")
        for r in sorted(waiting, key=lambda x: (x.get("check_date") or "", x["id"])):
            pr = r.get("probs") or {}
            probs = ("—" if not pr else
                     f"{(pr.get('bear') or 0) * 100:.0f}/"
                     f"{(pr.get('base') or 0) * 100:.0f}/"
                     f"{(pr.get('bull') or 0) * 100:.0f}")
            ev = r.get("ev")
            L.append(f"| #{r['id']} | {r['code']} | {r['logged_at'][:10]} "
                     f"| {r.get('action')} "
                     + (f"| {ev:+.1f}% " if ev is not None else "| — ")
                     + f"| {probs} | {r.get('check_date')} |")
        L.append("")
    L.append("## 3 这个回测自己会错在哪")
    L.append("")
    L.append("- **样本太少。** 每个季度只产生 5 个新观测点。用 5 个点挑规则,"
             "几乎必然挑出噪声 —— 所以**数据只能否决规则,不能提出规则**:"
             "新规则先要有第一性原理的解释,再看数据支不支持。")
    L.append("- **只核开着的记录。** 已经 close 掉的不再进来 —— "
             "统计准确率时要把 close 的一起算,否则等于只统计还没被证伪的那些。")
    L.append("- **拿复核日当天的价比,不是区间最高价。** 后者会让每条记录"
             "都显得判断得很好,那是在骗自己。")
    L.append("- **期望值说的是「平均」,不是「一定」。** 单条差 20 个百分点很正常,"
             "要看的是几十条加起来的偏向:如果实际涨跌**系统性**低于期望值,"
             "那说明概率给得偏乐观,该改的是概率的算法,不是某一只票的判断。")
    L.append("")
    return "\n".join(L) + "\n"


# ── 走前回测:把时间倒回去,只用截至目标季前一季的数据 ──────────────────
WF_CODES = {"300308": "中际旭创", "300502": "新易盛", "300476": "胜宏科技",
            "002353": "杰瑞股份", "688328": "深科达"}
WF_FROM, WF_TO = "2024Q1", "2026Q2"


def _targets():
    out = []
    for y in range(int(WF_FROM[:4]), int(WF_TO[:4]) + 1):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            p = f"{y}{q}"
            if WF_FROM <= p <= WF_TO:
                out.append(p)
    return out


def truncate(qs, target: str):
    """只留 t-1 及之前,并且**把最后一行的「下季营收÷期末存货」清成 None**。

    那一格用的是 target 当季的营收 —— 是未来信息。生产上跑最新一季时它本来
    就是 None(下季还没披露),不清掉等于给回测开了后门:
    存货法会拿到一个当时不可能有的比值,算出来的成绩是假的。
    """
    ti = quarterly.qidx(target)
    out = [dict(r) for r in qs if quarterly.qidx(r["period"]) < ti]
    if out:
        out[-1]["下季营收÷期末存货"] = None
    return out


def _agg(items):
    """items: [(真实, low, high, mid)] → 命中率 / 偏差 / 宽度。"""
    if not items:
        return None
    hit = sum(1 for r, lo, hi, m in items if lo <= r <= hi)
    bias = [r / m - 1 for r, lo, hi, m in items if m]
    width = [hi / lo - 1 for r, lo, hi, m in items if lo > 0]
    return {"n": len(items), "命中": hit, "命中率": hit / len(items) * 100,
            "平均偏差": statistics.mean(bias) * 100,
            "中位绝对偏差": statistics.median([abs(b) for b in bias]) * 100,
            "中位宽度": statistics.median(width) * 100,
            "偏高": sum(1 for r, lo, hi, m in items if r > hi),
            "偏低": sum(1 for r, lo, hi, m in items if r < lo)}


def walkforward(raw: Path) -> dict:
    """跑一遍走前回测,返回可以直接落成 json 的汇总。

    **回测程序和生产程序是同一套代码** —— 直接调 `forecast.m_inventory` /
    `m_yoy` / `m_qoq` 和 `forecast.build` 里那段合成逻辑,不另写一份。
    另写一份的话,量的就不是生产上真正在跑的东西。

    窗口:生产代码写死 `since="2022"`,对 2026Q3 正好等于「往回四年」,
    所以这里用「目标季年份 − 4」把同样长度的窗口平移到历史上。
    """
    import forecast as F
    rows = []
    for code, name in WF_CODES.items():
        full = quarterly.build(raw, code, since="2015")["quarters"]
        actual = {r["period"]: r["单季营收"] for r in full}
        for t in _targets():
            real = actual.get(t)
            if real is None:
                continue
            qs_all = quarterly.build(raw, code,
                                     since=str(int(t[:4]) - 4))["quarters"]
            qs = truncate(qs_all, t)
            if len(qs) < 4:
                continue
            ms = [F.m_inventory(qs, t), F.m_yoy(qs, t), F.m_qoq(qs, t)]
            rec = {"code": code, "name": name, "target": t, "real": real,
                   "methods": {m["name"]: m for m in ms}}
            good = [m for m in ms if m.get("ok")]
            rec["ngood"] = len(good)
            if good:
                lo = min(m["low"] for m in good)
                hi = max(m["high"] for m in good)
                rec["overlap"] = (max(m["low"] for m in good)
                                  <= min(m["high"] for m in good))
                rec["band"] = {"low": lo, "high": hi,
                               "mid": statistics.median([m["mid"] for m in good])}
            rows.append(rec)
    # 汇总
    band = [(r["real"], r["band"]["low"], r["band"]["high"], r["band"]["mid"])
            for r in rows if r.get("band")]
    contra = [(r["real"], r["band"]["low"], r["band"]["high"], r["band"]["mid"])
              for r in rows if r.get("band") and not r.get("overlap")]
    inter = []
    for r in rows:
        if not r.get("band") or not r.get("overlap"):
            continue
        good = [m for m in r["methods"].values() if m.get("ok")]
        inter.append((r["real"], max(m["low"] for m in good),
                      min(m["high"] for m in good), r["band"]["mid"]))
    per_method = {}
    for nm in ("存货法", "同比法", "环比法"):
        items = [(r["real"], r["methods"][nm]["low"], r["methods"][nm]["high"],
                  r["methods"][nm]["mid"])
                 for r in rows if r["methods"].get(nm, {}).get("ok")]
        per_method[nm] = _agg(items)
    # ⚠ **宽度分档要在「宽窄都有」的样本上量。** 改成并集之后所有区间都变宽了,
    #   <10% 那一档只剩一个样本 —— 拿它说事等于用一个点下结论。
    #   所以这一张表用**旧的生产取法**(先交集、交集为空才退并集)那 46 个区间来算:
    #   那批区间宽窄俱全,正是它证明了「窄区间几乎必错」,也正是它让我们改掉交集。
    old = []
    for r in rows:
        if not r.get("band"):
            continue
        if r.get("overlap"):
            good = [m for m in r["methods"].values() if m.get("ok")]
            old.append((r["real"], max(m["low"] for m in good),
                        min(m["high"] for m in good), r["band"]["mid"]))
        else:
            old.append((r["real"], r["band"]["low"], r["band"]["high"],
                        r["band"]["mid"]))
    buckets = [("<10%", 0, 10), ("10%~25%", 10, 25),
               ("25%~50%", 25, 50), (">50%", 50, 1e9)]
    by_width = []
    for lab, a, b in buckets:
        items = [x for x in old
                 if x[1] > 0 and a <= (x[2] / x[1] - 1) * 100 < b]
        g = _agg(items)
        if g:
            by_width.append({"档": lab, "n": g["n"], "命中": g["命中"],
                             "命中率": g["命中率"]})
    # 大幅低估的样本:实际比中枢高 50% 以上。回测说它们**全在增速跳升那一季**。
    big = [{"code": r["code"], "name": r["name"], "target": r["target"],
            "偏差": (r["real"] / r["band"]["mid"] - 1) * 100}
           for r in rows if r.get("band") and r["band"]["mid"]
           and r["real"] / r["band"]["mid"] - 1 > 0.5]
    return {"生成于": dt.date.today().isoformat(),
            "窗口": "目标季年份 − 4(和生产的 since=2022 等长)",
            "样本": {"票数": len(WF_CODES), "季度": f"{WF_FROM}~{WF_TO}",
                     "样本数": len(rows), "有区间": len(band)},
            "并集": _agg(band), "交集(仅重叠的)": _agg(inter),
            "旧生产取法(先交集空了退并集)": _agg(old),
            "按宽度用的样本": "旧的生产取法那 46 个区间(宽窄俱全);"
                              "并集之后区间普遍变宽,<10% 那一档只剩 1 个样本,"
                              "量不出宽度和命中率的关系",
            "矛盾退并集": _agg(contra),
            "方法": per_method, "按宽度": by_width,
            "拐点": {"说明": "实际值比中枢高 50% 以上的季度", "样本": big},
            "逐季": [{"code": r["code"], "name": r["name"], "target": r["target"],
                      "可用方法数": r["ngood"], "重叠": r.get("overlap"),
                      "real": r["real"],
                      "band": r.get("band")} for r in rows]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--predictions", default="private/predictions")
    ap.add_argument("--md", help="把结果写到这个文件")
    ap.add_argument("--walkforward", action="store_true",
                    help="跑走前回测,把汇总写进 `<raw 的上一级>/backtest/"
                         "revenue-summary.json`(报告第 3 节读它)")
    ap.add_argument("--out", help="走前回测汇总写到哪;默认 "
                                  "`<raw 的上一级>/backtest/revenue-summary.json`")
    a = ap.parse_args()
    raw = Path(a.raw)
    if a.walkforward:
        s = walkforward(raw)
        out = Path(a.out) if a.out else raw.parent / "backtest" / "revenue-summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        u = s["并集"]
        print(f"→ {out}")
        print(f"   样本 {s['样本']['样本数']} 个「票 × 季度」,{u['n']} 个有区间")
        print(f"   并集命中 {u['命中']}/{u['n']} = {u['命中率']:.0f}%,"
              f"中枢误差中位 {u['中位绝对偏差']:.1f}%,"
              f"平均偏差 {u['平均偏差']:+.1f}%,中位宽度 {u['中位宽度']:.0f}%")
        for nm, m in s["方法"].items():
            if m:
                print(f"   {nm}:通过自检 {m['n']} 次,命中 {m['命中率']:.0f}%,"
                      f"中枢误差中位 {m['中位绝对偏差']:.1f}%")
        return 0
    md = build(raw, Path(a.predictions))
    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(md, encoding="utf-8")
        print(f"→ {a.md}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
