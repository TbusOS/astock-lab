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
                     "n": p.get("可用方法数")})
    if rows:
        L.append("| 代码 | 预测期 | 用了几个方法 | 实际营收(亿) | 落在区间? | 偏中枢 "
                 "| 实际归母净利(亿) | 落在区间? | 偏中枢 |")
        L.append("|---|---|---:|---:|:-:|---:|---:|:-:|---:|")
        for r in rows:
            L.append(f"| {r['code']} | {r['period']} | {r['n']} "
                     f"| {r['act']['营收'] / 1e8:,.1f} "
                     f"| {'✅' if r['rv'][0] else '❌'} "
                     f"| {r['rv'][1]:+.1f}% "
                     f"| {(r['act']['归母净利'] or 0) / 1e8:,.1f} "
                     f"| {'✅' if r['ni'][0] else '❌'} "
                     + (f"| {r['ni'][1]:+.1f}% |" if r["ni"][1] is not None else "| — |"))
        L.append("")
        ok_n = sum(1 for r in rows if r["rv"][0])
        L.append(f"**{ok_n}/{len(rows)} 只的营收落在我们的区间里。**")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--predictions", default="private/predictions")
    ap.add_argument("--md", help="把结果写到这个文件")
    a = ap.parse_args()
    md = build(Path(a.raw), Path(a.predictions))
    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(md, encoding="utf-8")
        print(f"→ {a.md}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
