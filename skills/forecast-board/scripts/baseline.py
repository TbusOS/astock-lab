#!/usr/bin/env python3
"""baseline —— 统计底线:只按最近的波动幅度做随机模拟,不带方向。

它是什么:每一层预测(系统算法、两个 AI)都要拿来比的那条线。
没有它会怎样:说「系统算法 3 个月涨的概率 40」时,没法判断这是看空了,
还是这只股票波动大、谁来算都差不多是这个数。
为什么零漂移:设计要求「不带方向」。如果每步加 −σ²/2(几何布朗运动常见写法,
让平均价不变),中位价会一路往下走 —— 年化波动 79% 的股票一年后只剩约 35/100 涨,
看着像在看空。这里每步对数收益均值取 0,中位价 = 起点价,每个期限涨的种数都约 50。
(代价:平均价会往上偏 e^{σ²t/2};要改回去,把 baseline_forecast 里 drift 换掉就行。)

怎么算:
  σ = 最近 lookback 天日对数收益的样本标准差(ddof=1);
  第 k 步(k = 1..steps)的价 = 起点价 × exp(σ × (z_1 + … + z_k)),z 是标准正态。

★ 随机数的约定(系统算法 system_forecast.py 要逐位复现,别改):
  rng = np.random.default_rng(seed)
  z   = rng.standard_normal((n_paths, steps))        # 一次抽完,就这一个数组、这个顺序
  steps = max(max(horizons), bottom_window)
  第 i 条路径第 k 步用 z[i, k-1]。价格路径由 price_paths(s0, sigma, z, drift) 算;
  系统算法在漂移为 0 时用同一个函数、同一个 z,结果就和本层逐位相同。

日子怎么对(categories / bottom_block 共用):
  start 是预测期的第一个交易日(最后一个已知收盘的下一个交易日);
  days = prices.future_trading_days(start, n, holidays),第 k 步落在 days[k-1]。
  「起点就是最低」(后面每一步都比起点高)并进第一个月,不单列。

closes 用后复权价:不复权价在除权那天会跳一截,σ 会被算大。
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

import prices

TRADING_DAYS_PER_YEAR = 250
QUANTILES = (10, 25, 50, 75, 90)
# 最低价 ÷ 起点价 的分段。前五段左闭右开,最后一段 [0.9, 1.0] 两头都闭,
# 包含「起点就是最低」(比值正好 1.0)。最低价不会高于起点,所以 1.0 就是上限。
PRICE_BINS = ((0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0))
LATER = "later"     # 买点在期限内都没出现


# ── 分类(各层共用) ────────────────────────────────────────────────────────
def _day_months(start: date, n: int, holidays) -> list[str]:
    """往后 n 个交易日各自的月份 'YYYY-MM'。"""
    return [d.strftime("%Y-%m") for d in prices.future_trading_days(start, n, set(holidays))]


def _distinct(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def categories(start: date, holidays: set[str], bottom_window: int = 126,
               buy_horizon: int = 250) -> dict:
    """统计底线、系统算法、两个 AI 共用的分类。分类题按类别打分,各层的类别必须一模一样。

    bottom_months  往后 bottom_window 个交易日覆盖到的月份,按时间排
    buy_months     往后 buy_horizon 个交易日覆盖到的月份,最后加 "later"(期限内都没出现)
    price_bins     最低收盘 ÷ 起点收盘 的分段(见 PRICE_BINS)
    """
    if bottom_window < 1 or buy_horizon < 1:
        raise ValueError(f"bottom_window、buy_horizon 要 ≥ 1,得到 {bottom_window}、{buy_horizon}")
    return {"bottom_months": _distinct(_day_months(start, bottom_window, holidays)),
            "buy_months": _distinct(_day_months(start, buy_horizon, holidays)) + [LATER],
            "price_bins": [list(b) for b in PRICE_BINS]}


# ── 取整 ────────────────────────────────────────────────────────────────────
def largest_remainder(counts: list[int], total: int = 100) -> list[int]:
    """按比例把 total 分给各项,取整后加起来正好 total(最大余数法)。

    先每项向下取整,差的几个依次给小数部分最大的项;小数部分一样大时给排在前面的。
    全用整数算,没有浮点误差。counts 要是非负整数、至少一个大于 0。"""
    if not counts or any(c < 0 or c != int(c) for c in counts):
        raise ValueError(f"counts 要是非负整数,得到 {counts!r}")
    cs = [int(c) for c in counts]
    n = sum(cs)
    if n == 0:
        raise ValueError("counts 全是 0,没法按比例分")
    base = [c * total // n for c in cs]
    rem = [c * total % n for c in cs]
    order = sorted(range(len(cs)), key=lambda i: (-rem[i], i))
    extra = set(order[:total - sum(base)])
    return [b + (1 if i in extra else 0) for i, b in enumerate(base)]


def _pct_up(hits: int, n: int) -> int:
    """hits / n 换成 0~100 的整数,四舍五入(0.5 进位)。"""
    return (200 * hits + n) // (2 * n)


def _quantiles(values: np.ndarray) -> dict[str, float]:
    qs = np.percentile(values, QUANTILES)
    return {str(q): round(float(v), 3) for q, v in zip(QUANTILES, qs)}


# ── 模拟 ────────────────────────────────────────────────────────────────────
def daily_sigma(closes: list[float], lookback: int = 120) -> float:
    """最近 lookback 天日对数收益的样本标准差(要 lookback+1 个收盘)。
    样本标准差自带减均值,所以历史上的涨跌趋势不会带进来。"""
    if lookback < 2:
        raise ValueError(f"lookback 至少 2,得到 {lookback}")
    px = np.asarray(closes, dtype=float)
    if px.ndim != 1 or len(px) < lookback + 1:
        raise ValueError(f"要至少 {lookback + 1} 个收盘,只有 {len(px)} 个")
    tail = px[-(lookback + 1):]
    if not np.all(np.isfinite(tail)) or np.any(tail <= 0):
        raise ValueError("最近的收盘里有 0、负数或空值,算不了对数收益")
    return float(np.std(np.diff(np.log(tail)), ddof=1))


def draw_normals(seed: int, n_paths: int, steps: int) -> np.ndarray:
    """标准正态 (n_paths, steps),一次抽完。见文件头「随机数的约定」。"""
    return np.random.default_rng(seed).standard_normal((n_paths, steps))


def price_paths(s0: float, sigma: float, z: np.ndarray, drift: float = 0.0) -> np.ndarray:
    """价格路径 (n_paths, steps):第 k 列是第 k+1 步的价。
    每步对数收益 = drift + sigma × z。不改动传进来的 z(系统算法还要用它造指数路径)。"""
    inc = z * sigma                       # 新数组,下面原地累加不碰 z
    if drift:
        inc += drift
    np.cumsum(inc, axis=1, out=inc)
    np.exp(inc, out=inc)
    inc *= s0
    return inc


def horizon_block(paths: np.ndarray, s0: float, horizons) -> dict:
    """每个期限:期末高于起点的种数(0~100)、期末价的五个分位(绝对价位)。"""
    n = paths.shape[0]
    out = {}
    for h in horizons:
        end = paths[:, h - 1]
        out[str(h)] = {"p_up": _pct_up(int(np.count_nonzero(end > s0)), n), "q": _quantiles(end)}
    return out


def bottom_block(paths: np.ndarray, s0: float, start: date, holidays, window: int) -> dict:
    """期间最低点:落在哪个月、最低价 ÷ 起点在哪一段、最低价的分位。

    看起点和往后 window 步,共 window+1 个价;起点最低(后面每步都更高)并进第一个月。
    month_probs、price_bins 的类别和 categories() 一模一样,各自加起来 100。"""
    if paths.ndim != 2 or paths.shape[1] < window:
        raise ValueError(f"路径要至少 {window} 步,得到形状 {paths.shape}")
    day_months = _day_months(start, window, holidays)
    months = _distinct(day_months)
    pos = {m: i for i, m in enumerate(months)}
    # 第 0 步(起点)→ 第一个月;第 k 步 → day_months[k-1] 那个月
    step_month = np.array([0] + [pos[m] for m in day_months])
    fut = paths[:, :window]
    fut_min = fut.min(axis=1)
    step = np.where(fut_min < s0, fut.argmin(axis=1) + 1, 0)
    low = np.minimum(fut_min, s0)
    month_pct = largest_remainder(np.bincount(step_month[step], minlength=len(months)).tolist())
    edges = [lo for lo, _ in PRICE_BINS[1:]]
    bin_idx = np.searchsorted(edges, low / s0, side="right")
    bin_pct = largest_remainder(np.bincount(bin_idx, minlength=len(PRICE_BINS)).tolist())
    return {"window": window,
            "month_probs": dict(zip(months, month_pct)),
            "price_bins": [[lo, hi, p] for (lo, hi), p in zip(PRICE_BINS, bin_pct)],
            "price_q": _quantiles(low)}


def _check_args(horizons, n_paths: int, bottom_window: int) -> tuple[int, ...]:
    hs = tuple(int(h) for h in horizons)
    if not hs or any(h < 1 for h in hs):
        raise ValueError(f"horizons 要是正整数,得到 {horizons!r}")
    if n_paths < 1 or bottom_window < 1:
        raise ValueError(f"n_paths、bottom_window 要 ≥ 1,得到 {n_paths}、{bottom_window}")
    return hs


def baseline_forecast(closes: list[float], start: date, holidays: set[str],
                      horizons=(63, 126, 250), n_paths: int = 20000, seed: int = 20261102,
                      lookback: int = 120, bottom_window: int = 126) -> dict:
    """统计底线。返回
    {"layer": "baseline", "vol_annual": 年化波动, "start_price": 起点价(最后一个收盘),
     "horizons": {"63": {"p_up": int, "q": {"10": 价, …, "90": 价}}, "126": …, "250": …},
     "bottom": {"window": 126, "month_probs": {"YYYY-MM": int, …},
                "price_bins": [[lo, hi, int], …], "price_q": {"10": 价, …, "90": 价}}}
    同一组参数、同一个 seed,结果完全相同。"""
    hs = _check_args(horizons, n_paths, bottom_window)
    sigma = daily_sigma(closes, lookback)
    s0 = float(closes[-1])
    steps = max(max(hs), bottom_window)
    # drift = 0:不带方向(见文件头「为什么零漂移」)
    paths = price_paths(s0, sigma, draw_normals(seed, n_paths, steps), drift=0.0)
    return {"layer": "baseline",
            "vol_annual": round(sigma * math.sqrt(TRADING_DAYS_PER_YEAR), 4),
            "start_price": s0,
            "horizons": horizon_block(paths, s0, hs),
            "bottom": bottom_block(paths, s0, start, holidays, bottom_window)}
