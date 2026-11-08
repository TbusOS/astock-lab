#!/usr/bin/env python3
"""hist_overseas —— 美股光通信同行的「当时能看到的」历史表,给历史补考考「海外同行股价先动 → A 股跟着动」用。

它是什么:美股四家光通信同行(COHR、LITE、AAOI、FN)每个美股交易日一条:各自收盘、等权篮子点位、
篮子过去 10 个美股交易日的涨跌(%)。known = 美股交易日 + 1 天 —— 美股收盘时 A 股已经收盘了,
A 股最早下一个自然日开盘后才能用这个数。
没有它会怎样:「海外同行先动」只是一句话,补考里没有它的值,考不了。
为什么这么设计:
- 四家是 A 股光模块的直接同行(光模块 / 光器件 / 代工),上市时间都早于 2016 年,
  补考期内一直有数。CIEN 是光网络设备商,离光模块远一层,不放进篮子。
- COHR 的来历:2022-07 II-VI 收购了老 Coherent,2022-09 II-VI 改名 Coherent 并接用 COHR 代码。
  yfinance 的 COHR 历史在 2022-09 以前就是 II-VI 的价格(2021-06-30 收盘 72.59,与 II-VI 当时一致;
  老 Coherent 那时约 260 美元),一条线接下来,不用拼接。IIVI 代码在 yfinance 已取不到。
- 篮子涨跌用「各家 10 日简单涨幅的平均」,和 A 股同行信号(gap10)一样用简单涨幅;
  点位用每日等权再平衡,存下来让打分时能换别的窗口。
- 信号:_band(10 日涨跌, −PEER_GAP, +PEER_GAP),阈值取 system_forecast 的 PEER_GAP,不另写数字。
  这是新加的想法,线上系统里没有这个信号;阈值借同行信号的 ±10,写进 meta.rule。
什么场景用:主对话统一打分时 import 本模块,对每个出题日调 signal_at(table, "sector", 出题日)。
机制:
    python3 hist_overseas.py --out <signals 目录>/overseas.json [--start 2015-06-01]
    value_at(table, "sector", day) → known ≤ day 的最后一条;signal_at → −1/0/+1/None
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # noqa: F401
import system_forecast as sf

TICKERS = ("COHR", "LITE", "AAOI", "FN")
WINDOW = 10                 # 篮子涨跌看过去多少个美股交易日
MIN_MEMBERS = 2             # 至少几家有数才算篮子涨跌
JUMP_FLAG = 0.40            # 单日涨跌超过这个比例写进 notes(数据错或代码换了公司会这样)
DEFAULT_START = "2015-06-01"
KEY = "sector"


def known_of(us_day: str) -> str:
    """美股某天收盘 → A 股哪天起能用:下一个自然日。"""
    return (date.fromisoformat(us_day) + timedelta(days=1)).isoformat()


def build_table(closes: dict[str, dict[str, float]], start: str, end: str) -> dict:
    """closes = {代码: {美股日期: 收盘}} → 表。日历 = 各家日期的并集。"""
    days = sorted({d for s in closes.values() for d in s})
    recs, level, notes = [], 100.0, []
    for i, d in enumerate(days):
        px = {t: closes[t][d] for t in TICKERS if d in closes.get(t, {})}
        if i:
            prev = days[i - 1]
            rets = [px[t] / closes[t][prev] - 1 for t in px if prev in closes[t]]
            for t in px:
                if prev in closes[t] and abs(px[t] / closes[t][prev] - 1) > JUMP_FLAG:
                    notes.append(f"{t} {d} 单日 {100 * (px[t] / closes[t][prev] - 1):+.0f}%")
            if rets:
                level *= 1 + sum(rets) / len(rets)
        ret10, n = None, 0
        if i >= WINDOW:
            base = days[i - WINDOW]
            r = [px[t] / closes[t][base] - 1 for t in px if base in closes[t]]
            n = len(r)
            if n >= MIN_MEMBERS:
                ret10 = round(100 * sum(r) / n, 4)
        recs.append({"known": known_of(d), "us_date": d, "close": px, "basket": round(level, 4),
                     "ret10": ret10, "n": n})
    gaps = [f"{t}:没取到" for t in TICKERS if not closes.get(t)]
    for t in TICKERS:
        s = closes.get(t) or {}
        if s and min(s) > days[0]:
            gaps.append(f"{t}:{min(s)} 才有数(其余从 {days[0]})")
    return {"meta": {
        "name": "overseas", "source": "yfinance 日线(auto_adjust,本机取);四家都没分过红,复权价 ≈ 收盘价",
        "tickers": list(TICKERS),
        "rule": (f"篮子 {WINDOW} 日涨跌 = 各家 {WINDOW} 个美股交易日简单涨幅的平均 × 100,少于 {MIN_MEMBERS} 家有数记 None;"
                 f"信号 = _band(涨跌, −{sf.PEER_GAP}, +{sf.PEER_GAP}),阈值借 system_forecast.PEER_GAP。"
                 "线上没有这个信号,这是补考新加的想法"),
        "built_at": datetime.now().isoformat(timespec="seconds"), "range": [start, end],
        "known_rule": "known = 美股交易日 + 1 个自然日(美股收盘时 A 股已收盘,最早下一天才能用)",
        "notes": ["COHR 在 2022-09 以前是 II-VI 的价格(II-VI 2022-07 收购老 Coherent、2022-09 改名并接用 COHR 代码),"
                  "yfinance 一条线接下来,2021-06-30 收盘 72.59 与 II-VI 一致;IIVI 代码已取不到"] + notes,
        "gaps": gaps}, "keys": {KEY: recs}}


def value_at(table: dict, key: str, day: str) -> dict | None:
    recs = table["keys"].get(key) or []
    n = bisect.bisect_right([r["known"] for r in recs], day)
    return recs[n - 1] if n else None


def signal_at(table: dict, key: str, day: str) -> int | None:
    r = value_at(table, key, day)
    if r is None or r.get("ret10") is None:
        return None
    return sf._band(r["ret10"], -sf.PEER_GAP, sf.PEER_GAP)


def fetch_yf(start: str, end: str) -> dict[str, dict[str, float]]:
    """yfinance 日线 → {代码: {日期: 收盘}}。end 不含当天(yfinance 的 end 是开区间)。"""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    out = {}
    for t in TICKERS:
        h = yf.Ticker(t).history(start=start, end=end, auto_adjust=True)
        out[t] = {ix.strftime("%Y-%m-%d"): round(float(c), 4) for ix, c in h["Close"].items() if c == c}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="美股光通信同行的当时历史表")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=date.today().isoformat(), help="不含这一天")
    a = ap.parse_args(argv)
    table = build_table(fetch_yf(a.start, a.end), a.start, a.end)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(table, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    recs = table["keys"][KEY]
    print(f"{KEY}  美股 {recs[0]['us_date']} ~ {recs[-1]['us_date']}  {len(recs)} 条")
    print("缺口:", table["meta"]["gaps"] or "无")
    print("单日大涨跌:", table["meta"]["notes"][1:] or "无")
    print(f"写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
