#!/usr/bin/env python3
"""analogs —— 历史同类情形:股价跌破一年高点 50% 以后,过去几次是怎么走的。

它是什么:看板「什么时候见底」那一块的参考数,例「两只跌破一年高点 50% 共 N 次,
跌破后见底用时:一周内几次、一到三个月几次……」。
没有它会怎样:只有统计底线的模拟,模拟不知道「已经跌了一半」这件事,
历史上同样跌了一半的股票后来怎么走,是另一种依据。
局限:样本是看板关注的那几只,都是后来成了龙头的,有幸存者偏差(跌下去再没起来的公司不在样本里);
段数少,只能当参考,不当概率用。

一段怎么算:
  一年高点 = 当天往前 lookback 个交易日(含当天)的最高收盘;
  某天收盘 ≤ 一年高点 × (1 − threshold),而前一天还在线上 → 这天「跌破」;
  跟上一段的跌破日相隔不到 gap 个交易日的跌破不另算(同一段大跌在线附近来回不重复计);
  一直趴在线下也不重复算(要先回到线上、再跌破才是新的一次)。
  数据开头就已经在线下的不算 —— 看不到是哪天跌破的。
  要统计从某年起的段,取数从前一年起(前 lookback 天只用来算一年高点)。

每段的字段(比例都是小数,四位):
  date            跌破那天
  dd              跌破那天离一年高点跌了多少(正数,0.5 = 跌了一半)
  fwd             跌破后 63 / 126 / 250 个交易日的涨跌(收盘比跌破价 − 1),数据不够为 None
  days_to_bottom  跌破那天(第 0 天)到窗口内最低收盘隔了几个交易日
  bottom_drop     窗口内最低收盘离跌破那天的一年高点跌了多少(正数,跟 dd 同一个量法)
  days_back       从跌破那天数,最低点之后第一次收回跌破价是第几个交易日;到数据末尾都没回来为 None
  max_in_window   窗口内(跌破当天 + 往后 window 天)最高收盘比跌破价 − 1(≥ 0)
  min_in_window   窗口内最低收盘比跌破价 − 1(≤ 0)
  complete        往后 window 天的数据齐了没有;没齐的段,最低点可能还在后面
"""
from __future__ import annotations

from statistics import median

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

FWD = (63, 126, 250)
# 见底用时分桶(交易日):一周 5 天、一个月 21 天、三个月 63 天
BUCKETS = ((5, "一周内"), (21, "一周到一个月"), (63, "一到三个月"), (None, "三个月以上"))


def _r4(x: float) -> float:
    return round(float(x), 4)


def _check_series(dates: list[str], closes: list[float]) -> np.ndarray:
    if len(dates) != len(closes):
        raise ValueError(f"日期 {len(dates)} 个、收盘 {len(closes)} 个,对不上")
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError("日期要严格升序、不重复")
    px = np.asarray(closes, dtype=float)
    if len(px) and (not np.all(np.isfinite(px)) or np.any(px <= 0)):
        raise ValueError("收盘里有 0、负数或空值")
    return px


def _episode(dates: list[str], px: np.ndarray, t: int, high: float, window: int) -> dict:
    n = len(px)
    seg = px[t:min(t + window, n - 1) + 1]
    b = t + int(seg.argmin())
    back = np.nonzero(px[b + 1:] >= px[t])[0]
    return {"date": dates[t],
            "dd": _r4(1 - px[t] / high),
            "fwd": {str(h): (_r4(px[t + h] / px[t] - 1) if t + h < n else None) for h in FWD},
            "days_to_bottom": b - t,
            "bottom_drop": _r4(1 - px[b] / high),
            "days_back": int(b + 1 + back[0] - t) if len(back) else None,
            "max_in_window": _r4(seg.max() / px[t] - 1),
            "min_in_window": _r4(seg.min() / px[t] - 1),
            "complete": t + window <= n - 1}


def drawdown_episodes(dates: list[str], closes: list[float], threshold: float = 0.5,
                      lookback: int = 250, gap: int = 120, window: int = 126) -> list[dict]:
    """找出每一次「跌破一年高点 threshold」,规则和字段见文件头。
    dates 是 'YYYY-MM-DD' 升序,closes 用后复权收盘(不复权价除权那天的跳空会被当成下跌)。
    数据不满 lookback+1 天返回空列表。"""
    if not 0 < threshold < 1:
        raise ValueError(f"threshold 要在 0~1 之间,得到 {threshold}")
    if lookback < 1 or gap < 1 or window < 1:
        raise ValueError(f"lookback、gap、window 要 ≥ 1,得到 {lookback}、{gap}、{window}")
    px = _check_series(dates, closes)
    n = len(px)
    if n < lookback + 1:
        return []
    high = np.full(n, np.nan)
    high[lookback - 1:] = sliding_window_view(px, lookback).max(axis=1)
    below = np.zeros(n, dtype=bool)
    below[lookback - 1:] = px[lookback - 1:] <= high[lookback - 1:] * (1 - threshold)
    out, last = [], None
    for t in range(lookback, n):     # 从第一个能算一年高点那天的下一天起,要看前一天在不在线上
        if below[t] and not below[t - 1] and (last is None or t - last >= gap):
            out.append(_episode(dates, px, t, float(high[t]), window))
            last = t
    return out


def _bucket_counts(days: list[int]) -> dict[str, int]:
    counts = dict.fromkeys((label for _, label in BUCKETS), 0)
    for d in days:
        label = next(lb for hi, lb in BUCKETS if hi is None or d <= hi)
        counts[label] += 1
    return counts


def _med(values: list) -> float | None:
    return _r4(median(values)) if values else None


def _label(e: dict) -> str:
    return f"{e['code']} {e['date']}" if "code" in e else e["date"]


def _stats(eps: list[dict]) -> dict:
    """一组段的汇总。见底用时、最深、回到跌破价只用窗口齐了的段;往后涨跌用有数的段。"""
    done = [e for e in eps if e["complete"]]
    backs = [e["days_back"] for e in done if e["days_back"] is not None]
    fwd = {}
    for h in FWD:
        rs = [e["fwd"][str(h)] for e in eps if e["fwd"].get(str(h)) is not None]
        fwd[str(h)] = {"n": len(rs), "up": sum(1 for r in rs if r > 0), "median": _med(rs)}
    return {"n": len(eps),
            "dates": [_label(e) for e in eps],
            "days_to_bottom": {"buckets": _bucket_counts([e["days_to_bottom"] for e in done]),
                               "median": _med([e["days_to_bottom"] for e in done])},
            "bottom_drop_median": _med([e["bottom_drop"] for e in done]),
            "days_back": {"n_back": len(backs), "n_never": len(done) - len(backs),
                          "median": _med(backs)},
            "fwd": fwd}


def _note(eps: list[dict], n_done: int, n_given: int, already_days: int, already_drop: float) -> str:
    codes = list(dict.fromkeys(e["code"] for e in eps if "code" in e))
    who = f"只看了这几只股票({'、'.join(codes)})" if codes else "只看了这几只股票"
    return (f"历史上共 {len(eps)} 段,其中 {n_done} 段跌破后的观察期已经走完,见底时间只用这 {n_done} 段;"
            f"跟现在一样(跌破后已经过了 {already_days} 个交易日还没见底、离一年高点已经跌了 "
            f"{already_drop:.0%})的有 {n_given} 段。"
            f"{who},而且是后来成了龙头的,有幸存者偏差;段数少,只能当参考,不能当概率用。")


def summarize_episodes(eps: list[dict], already_days: int, already_drop: float) -> dict:
    """汇总多段(可以是几只股票的段合在一起;段上带 "code" 键时说明里会列出代码)。

    already_days  现在这次跌破后已经过了几个交易日、还没见底
    already_drop  到现在为止最深离一年高点跌了多少(正数,0.55 = 跌了 55%,跟 bottom_drop 同一个量法)
    条件子集 given = 窗口齐了、见底用时 ≥ already_days、最深 ≥ already_drop 的段 ——
    跟现在的情况对不上的(那次更早就见底了、或者那次没跌这么深)排除掉。
    返回 {"n", "n_complete", "all": 汇总, "given": {"already_days", "already_drop", **汇总},
          "sample_note": 段数和局限}。"""
    if already_days < 0:
        raise ValueError(f"already_days 不能是负数,得到 {already_days}")
    if not 0 <= already_drop < 1:
        raise ValueError(f"already_drop 是小数(0.55 = 跌了 55%),要在 0~1 之间,得到 {already_drop}")
    done = [e for e in eps if e["complete"]]
    given = [e for e in done
             if e["days_to_bottom"] >= already_days and e["bottom_drop"] >= already_drop]
    return {"n": len(eps),
            "n_complete": len(done),
            "all": _stats(eps),
            "given": {"already_days": already_days, "already_drop": already_drop, **_stats(given)},
            "sample_note": _note(eps, len(done), len(given), already_days, already_drop)}
