#!/usr/bin/env python3
"""history_test —— 历史补考:站在过去每个月底,只用那天以前的收盘出题,跟后来的真实走势比。

它是什么:把统计底线和系统算法放回过去重跑。每个月最后一个交易日收盘出一次题,
3 个月 / 半年 / 一年三个期限一起出;到期后拿真实收盘打分。
没有它会怎样:上线以后的新题要等 3 个月才有第一批成绩,一年的要等一年;
在那之前说不出「这套程序以前准不准、哪个期限更有用」。

为什么这样设计:
- 出题日按规则定(每个月底),不挑日子 —— 挑出来的日子成绩会偏好看。
- 只测两层程序。AI 训练时可能见过这几年的行情,只给它旧数据也挡不住它记得结果,
  它的补考成绩不能当证明(所以 AI 补考先不做)。
- 系统算法是「两信号版」:大盘位置(regime)、同行涨跌差(peer)只用行情,能还原当时的值;
  券商预测、云厂开支、估值三个信号没有按发布日存的历史,记 0 并写进 missing。
  所以这里的成绩不能代表完整的五信号系统。
- 2026-11-03 起大盘信号权重是 0(就是这份补考查出来它让方向得分低于 50%),
  所以现在补考的系统层只剩同行涨跌差,而且只有配了对的股票有;没配对的和底线逐位相同。
  每份存档的 meta.weights 记着当时用的权重。
- 「信号检验过没有」也只拿出题日以前的数据重算(walk_forward_stats 只喂到那天),
  不拿今天的结论回填到过去。
- 这是「今天的程序 + 当时的数据」补考,不是当年真的做过的预测;页面上两者分开写。

带子的公式:两层都是「每天对数收益 = 漂移 + σ × 标准正态」,所以第 k 天的 q 分位
= 起点 × exp(漂移 × k + σ × √k × z_q),和模拟出来的只差抽样误差。
页面画逐日的带子用 band(),不用把每天的模拟结果都存下来。

用法:
    STOCK_LAB=<内部仓> python3 history_test.py --out <文件> [--codes 300308,300502,…]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist

import fb_common  # noqa: F401  先于 prices(market.py 会删代理变量)
import baseline
import prices
import scoring
import system_forecast as sf

HORIZONS = (63, 126, 250)
LOW_WINDOW = 126                # 期间最低看多少个交易日,跟 baseline 的 bottom_window 一样
LOOKBACK = 120                  # 波动按最近多少天算,跟 baseline 一样
MIN_HISTORY = LOOKBACK + 1
MIN_PAST_WINDOWS = 20           # 「按以前涨的比例猜」至少要这么多个历史窗口,不够就当 50
SPLIT_YEAR = "2023"             # 成绩分前后两段:出题日早于这一年 / 这一年及以后
LAYERS = ("baseline", "system")
DEFAULT_PAIRS = (("300308", "300502"),)
SYSTEM_NOTE = ("两信号版:只有大盘位置(regime)、同行涨跌差(peer)两个信号有当时的数据;"
               "券商预测、云厂开支、估值三个信号记 0;权重见 meta.weights(大盘 2026-11-03 起是 0)")
NOTE = "今天的程序 + 当时的数据补考,不是当年真的做过的预测"


# ── 出题日、信号 ─────────────────────────────────────────────────────────────
def month_end_indices(cal: list[str], start: str) -> list[int]:
    """cal 里每个月最后一个交易日的下标(cal[i] ≥ start)。最后一个月还没过完的不算。"""
    return [i for i in range(len(cal) - 1) if cal[i] >= start and cal[i][:7] != cal[i + 1][:7]]


def holidays_from(cal: list[str]) -> set[str]:
    """cal 首尾之间不开市的工作日。只用来给未来的日子标月份,不用来结算。"""
    d, end, have, out = date.fromisoformat(cal[0]), date.fromisoformat(cal[-1]), set(cal), set()
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.add(d.isoformat())
        d += timedelta(days=1)
    return out


def regime_value(index_closes, t: int) -> int:
    """那天指数收盘 vs 含当天的 60 日均线:高于 +1、低于 −1、相等 0(和 walk_forward_stats 同一算法)。"""
    win = index_closes[t - sf.BUY_MA + 1:t + 1]
    ma, close = sum(win) / len(win), index_closes[t]
    return (close > ma) - (close < ma)


def peer_gap10(pa: dict, pb: dict, cal: list[str], t: int) -> float | None:
    """(A 的 10 日涨幅 − B 的 10 日涨幅) × 100;两只在 cal[t] 和 cal[t−10] 都要有收盘。"""
    if t < 10:
        return None
    d0, d1 = cal[t - 10], cal[t]
    if not all(d in s for d in (d0, d1) for s in (pa, pb)):
        return None
    return 100 * (pa[d1] / pa[d0] - pb[d1] / pb[d0])


def band_peer(gap: float) -> int:
    return sf._band(gap, -sf.PEER_GAP, sf.PEER_GAP)


def check_drop(drop) -> tuple[str, ...]:
    """要关掉的信号名;不认识的名字直接报错,免得拼错了等于什么都没关。"""
    drop = tuple(drop or ())
    unknown = sorted(set(drop) - set(sf.SIGNAL_WEIGHTS))
    if unknown:
        raise ValueError(f"不认识的信号:{unknown},只有 {list(sf.SIGNAL_WEIGHTS)}")
    return drop


def signals_at(code: str, t: int, cal: list[str], px: dict, index_px: dict, pairs, drop=()) -> dict:
    """出题日 cal[t] 那天五个信号的值(没有当时数据的记 0),格式同 system_forecast 要的 signals。
    drop 里的信号也记 0、写进 missing:用来单独关掉一个信号,看成绩是不是它拖的。"""
    idx = [index_px[d] for d in cal[:t + 1]]
    values = {n: 0 for n in sf.SIGNAL_WEIGHTS}
    missing = ["consensus", "capex", "valuation"]
    values["regime"] = regime_value(idx, t)
    gaps = []
    for a, b in pairs:
        if code in (a, b) and a in px and b in px:
            g = peer_gap10(px[a], px[b], cal, t)
            if g is not None:
                gaps.append(g if code == a else -g)
    if gaps:
        values["peer"] = band_peer(sum(gaps) / len(gaps))
    else:
        missing.append("peer")
    for n in check_drop(drop):
        values[n] = 0
        if n not in missing:
            missing.append(n)
    return {"values": values, "missing": missing,
            "sources": {"regime": "创业板指 vs 60 日线(出题日收盘)", "peer": "10 日涨跌差(出题日收盘)"}}


def validated_at(t: int, cal: list[str], index_px: dict, px: dict, pairs) -> dict[str, bool]:
    """只用 cal[:t+1] 重算「哪些信号检验过」。"""
    days = cal[:t + 1]
    codes = sorted({c for p in pairs for c in p if c in px})
    stocks = {c: [px[c].get(d) for d in days] for c in codes}
    use = [p for p in pairs if all(c in px for c in p)]
    return sf.validate_price_signals(days, [index_px[d] for d in days], stocks, use)


# ── 出题、结算、打分 ─────────────────────────────────────────────────────────
def _next_day(cal: list[str], t: int, holidays: set[str]) -> date:
    if t + 1 < len(cal):
        return date.fromisoformat(cal[t + 1])
    d = date.fromisoformat(cal[t]) + timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d += timedelta(days=1)
    return d


def past_up_freq(closes: list[float], h: int) -> int:
    """以前所有 h 天窗口里涨了的比例(0~100);窗口不够 MIN_PAST_WINDOWS 个就当 50。"""
    n = len(closes) - h
    if n < MIN_PAST_WINDOWS:
        return 50
    return round(100 * sum(1 for i in range(n) if closes[i + h] > closes[i]) / n)


def _layer(fc: dict, sigma: float, drift: float) -> dict:
    return {"sigma": sigma, "drift": drift,
            "h": {h: {"p_up": v["p_up"], "q": v["q"]} for h, v in fc["horizons"].items()},
            "low_q": fc["bottom"]["price_q"]}


def forecast_at(code: str, t: int, cal: list[str], px: dict, index_px: dict, pairs, holidays: set[str],
                n_paths: int = 4000, seed: int = 20261102, validated: dict | None = None,
                drop=()) -> dict | None:
    """站在 cal[t] 收盘出题。只读 cal[t] 及以前的收盘;那天停牌或历史不够返回 None。"""
    s = px.get(code) or {}
    if cal[t] not in s:
        return None
    days = [d for d in cal[:t + 1] if d in s]
    if len(days) < MIN_HISTORY or t + 1 < sf.BUY_MA:
        return None
    closes = [s[d] for d in days]
    idx = [index_px[d] for d in days]
    start = _next_day(cal, t, holidays)
    val = validated if validated is not None else validated_at(t, cal, index_px, px, pairs)
    sig = signals_at(code, t, cal, px, index_px, pairs, drop)
    b = baseline.baseline_forecast(closes, start, holidays, n_paths=n_paths, seed=seed)
    y = sf.system_forecast(closes, idx, start, holidays, sig, val, seed=seed, n_paths=n_paths)
    sigma = baseline.daily_sigma(closes, LOOKBACK)
    sys_layer = _layer(y, sigma, y["tilt"] / baseline.TRADING_DAYS_PER_YEAR)
    sys_layer.update({"tilt": y["tilt"], "signals": sig["values"], "validated": val})
    return {"code": code, "t": t, "origin": cal[t], "s0": closes[-1],
            "layers": {"baseline": _layer(b, sigma, 0.0), "system": sys_layer,
                       "past_freq": {"h": {str(h): {"p_up": past_up_freq(closes, h)} for h in HORIZONS}}}}


def outcome_at(series: dict, cal: list[str], t: int, horizons=HORIZONS, low_window: int = LOW_WINDOW) -> dict:
    """到期那天(cal[t+h])的收盘;那天停牌用之前最后一个收盘。还没到期的记 None。
    low:出题日以后 low_window 个交易日里的最低收盘,窗口没走完 complete=False。"""
    out = {}
    for h in horizons:
        if t + h >= len(cal):
            out[str(h)] = None
            continue
        got = prices.close_on_or_before({d: v for d, v in series.items() if d <= cal[t + h]}, cal[t + h])
        out[str(h)] = {"date": cal[t + h], "used": got[0], "close": got[1]}
    win = [series[d] for d in cal[t + 1:t + 1 + low_window] if d in series]
    out["low"] = {"close": min(win) if win else None, "complete": t + low_window < len(cal)}
    return out


def _dir_hit(p: int, up: bool) -> float:
    return 0.5 if p == 50 else float((p > 50) == up)


def score_layer(layer: dict, s0: float, out: dict) -> dict:
    """一层在一道题上的成绩。区间得分用对数比值,和 scoring.interval_score 同一口径。"""
    res = {}
    for h, f in layer["h"].items():
        o = out.get(h)
        if not o:
            continue
        a = o["close"]
        up = a > s0
        p = f["p_up"]
        r = {"up": up, "p_up": p, "brier": scoring.brier(p / 100, int(up)), "dir_hit": _dir_hit(p, up)}
        q = f.get("q")
        if q:
            x = math.log(a / s0)
            lg = {k: math.log(v / s0) for k, v in q.items()}
            r.update({"in80": q["10"] <= a <= q["90"], "in50": q["25"] <= a <= q["75"],
                      "above90": a > q["90"], "below10": a < q["10"],
                      "is80": scoring.interval_score(lg["10"], lg["90"], x, 0.2),
                      "is50": scoring.interval_score(lg["25"], lg["75"], x, 0.5)})
        res[h] = r
    lq, lo = layer.get("low_q"), out.get("low") or {}
    if lq and lo.get("complete") and lo.get("close") is not None:
        res["low"] = {"in80": lq["10"] <= lo["close"] <= lq["90"], "below10": lo["close"] < lq["10"]}
    return res


def band(s0: float, sigma: float, drift: float, k: int, q: int) -> float:
    """第 k 个交易日的 q 分位价:起点 × exp(漂移 × k + σ × √k × z_q)。"""
    return s0 * math.exp(drift * k + sigma * math.sqrt(k) * NormalDist().inv_cdf(q / 100))


# ── 汇总 ────────────────────────────────────────────────────────────────────
def nonoverlap(ts: list[int], h: int) -> int:
    """出题日下标(升序)里,两两间隔至少 h 个交易日的有几个(从第一个起贪心数)。"""
    n, last = 0, None
    for t in sorted(ts):
        if last is None or t - last >= h:
            n, last = n + 1, t
    return n


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _stats(items: list[tuple[dict, dict]], h: str) -> dict:
    sc = [s for _, s in items]
    by_code: dict[str, list[int]] = {}
    for r, _ in items:
        by_code.setdefault(r["code"], []).append(r["t"])
    out = {"n": len(sc), "n_nonoverlap": sum(nonoverlap(ts, int(h)) for ts in by_code.values()),
           "dir_hit": _mean(s["dir_hit"] for s in sc), "brier": _mean(s["brier"] for s in sc),
           "up_rate": _mean(float(s["up"]) for s in sc)}
    if sc and "in80" in sc[0]:
        out.update({k: _mean(float(s[k]) for s in sc) for k in ("in80", "in50", "above90", "below10")})
        out.update({"cover80": out.pop("in80"), "cover50": out.pop("in50"),
                    "is80": _mean(s["is80"] for s in sc), "is50": _mean(s["is50"] for s in sc)})
    return out


def summarize(rows: list[dict]) -> dict:
    hs = [str(h) for h in HORIZONS]
    summary = {}
    for layer in LAYERS + ("past_freq",):
        summary[layer] = {}
        for h in hs:
            items = [(r, r["scores"][layer][h]) for r in rows if h in r["scores"][layer]]
            st = _stats(items, h)
            st["by_period"] = {name: _stats([(r, s) for r, s in items if cond(r["origin"])], h)
                               for name, cond in (("early", lambda d: d < SPLIT_YEAR),
                                                  ("late", lambda d: d >= SPLIT_YEAR))}
            summary[layer][h] = st
        if layer in LAYERS:
            lows = [r["scores"][layer]["low"] for r in rows if "low" in r["scores"][layer]]
            summary[layer]["low"] = {"n": len(lows), "cover80": _mean(float(x["in80"]) for x in lows),
                                     "below10": _mean(float(x["below10"]) for x in lows)}
    for h in hs:
        s, b = summary["system"][h], summary["baseline"][h]
        s["skill_is80"] = None if not b.get("is80") else 1 - s["is80"] / b["is80"]
        s["skill_brier"] = None if not b.get("brier") else 1 - s["brier"] / b["brier"]
    summary["always_up"] = {h: {"dir_hit": summary["baseline"][h]["up_rate"]} for h in hs}
    return summary


# ── 取数和整体流程 ───────────────────────────────────────────────────────────
class NetFetch:
    """联网取数:日历和创业板指用 prices.CALENDAR_INDEX 的不复权日线,个股用后复权。"""

    def __init__(self, now: datetime | None = None):
        self.now = now

    def index(self, start: str) -> dict[str, float]:
        bars = prices.market.fetch_daily(prices.CALENDAR_INDEX, start, fq="")
        return dict(sorted(prices.drop_live_bar(bars, self.now)))

    def calendar(self, start: str) -> list[str]:
        return sorted(self.index(start))

    def hfq(self, code: str, start: str) -> dict[str, float]:
        return prices.hfq_closes(code, start, now=self.now)

    def last_raw(self, code: str) -> float:
        begin = (date.today() - timedelta(days=40)).isoformat()
        bars = prices.drop_live_bar(prices.market.fetch_daily(prices.tencent_symbol(code), begin, fq=""), self.now)
        return float(bars[-1][1])


def _commit() -> str:
    root = Path(__file__).resolve().parents[3]
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("+未提交改动" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run(codes, start: str = "2018-01-01", data_start: str = "2016-06-01", pairs=DEFAULT_PAIRS,
        n_paths: int = 4000, seed: int = 20261102, fetch=None, chart_codes=("300308", "300502"),
        drop=()) -> dict:
    drop = check_drop(drop)
    fetch = fetch or NetFetch()
    index_px = fetch.index(data_start)
    cal = [d for d in fetch.calendar(data_start) if d in index_px]
    px = {c: fetch.hfq(c, data_start) for c in codes}
    hol = holidays_from(cal)
    pairs = [tuple(p) for p in pairs]
    rows = []
    for t in month_end_indices(cal, start):
        val = validated_at(t, cal, index_px, px, pairs)
        for c in codes:
            fc = forecast_at(c, t, cal, px, index_px, pairs, hol, n_paths, seed, validated=val, drop=drop)
            if fc is None:
                continue
            out = outcome_at(px[c], cal, t)
            fc["outcome"] = out
            fc["scores"] = {L: score_layer(fc["layers"][L], fc["s0"], out) for L in fc["layers"]}
            rows.append(fc)
    keep = [c for c in chart_codes if c in px]
    return {"meta": {"made_at": datetime.now().isoformat(timespec="seconds"), "as_of": cal[-1],
                     "start": start, "origin_rule": "每月最后一个交易日收盘出题", "codes": list(codes),
                     "pairs": [list(p) for p in pairs], "n_paths": n_paths, "seed": seed,
                     "horizons": list(HORIZONS), "low_window": LOW_WINDOW, "lookback": LOOKBACK,
                     "layers": list(LAYERS), "system_note": SYSTEM_NOTE, "note": NOTE, "commit": _commit(),
                     "dropped_signals": list(drop), "weights": dict(sf.SIGNAL_WEIGHTS),
                     "price": "后复权;画图时乘 price_factor 换成今天的价位(前复权)"},
            "questions": rows, "summary": summarize(rows),
            "series": {c: [[d, px[c][d]] for d in sorted(px[c])] for c in keep},
            "price_factor": {c: fetch.last_raw(c) / px[c][max(px[c])] for c in keep}}


# ── 命令行 ──────────────────────────────────────────────────────────────────
def _pct(x):
    return "  —" if x is None else f"{100 * x:3.0f}%"


def print_summary(res: dict) -> None:
    s = res["summary"]
    names = {"63": "3 个月", "126": "半年  ", "250": "一年  "}
    print(f"出题 {len(res['questions'])} 道(每只每月一道),截至 {res['meta']['as_of']};{res['meta']['note']}")
    if res["meta"].get("dropped_signals"):
        print(f"系统算法关掉了:{'、'.join(res['meta']['dropped_signals'])}(记 0)")
    print("期限   层        题数 不重叠  10~90接住 25~75接住  方向对  比底线少错")
    for h, nm in names.items():
        for layer, label in (("baseline", "统计底线"), ("system", "系统两信号")):
            x = s[layer][h]
            sk = "" if layer == "baseline" else _pct(x.get("skill_is80"))
            print(f"{nm} {label:<6} {x['n']:5d} {x['n_nonoverlap']:6d}   {_pct(x.get('cover80'))}"
                  f"      {_pct(x.get('cover50'))}    {_pct(x['dir_hit'])}   {sk}")
        print(f"{nm} 每次猜涨   方向对 {_pct(s['always_up'][h]['dir_hit'])} · "
              f"按以前涨的比例猜 方向对 {_pct(s['past_freq'][h]['dir_hit'])}")
    for layer in LAYERS:
        lw = s[layer]["low"]
        print(f"期间最低价(半年) {layer}: {lw['n']} 道,10~90 接住 {_pct(lw['cover80'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="历史补考:每月底出题,统计底线 + 系统算法两信号版")
    ap.add_argument("--out", required=True, help="结果写到哪个 json 文件")
    ap.add_argument("--codes", default="300308,300502", help="逗号分隔的 6 位代码")
    ap.add_argument("--start", default="2018-01-01", help="最早的出题日")
    ap.add_argument("--data-start", default="2016-06-01", help="从哪天起取收盘(要比 start 早半年以上)")
    ap.add_argument("--pairs", default="300308:300502", help="同行配对,逗号分隔 A:B")
    ap.add_argument("--n-paths", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--drop-signals", default="", help="关掉哪些信号(记 0),逗号分隔,如 regime")
    a = ap.parse_args(argv)
    drop = check_drop([n.strip() for n in a.drop_signals.split(",") if n.strip()])
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    pairs = [tuple(p.split(":")) for p in a.pairs.split(",") if ":" in p]
    res = run(codes, a.start, a.data_start, pairs, a.n_paths, a.seed, drop=drop)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print_summary(res)
    print(f"写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
