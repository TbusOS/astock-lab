#!/usr/bin/env python3
"""system_forecast —— 系统算法层:几个领先指标给统计底线加一个方向,再模拟买点条件哪个月满足。

它是什么:统计底线(baseline.py)只按最近的波动幅度做随机模拟,不带方向。
这一层在同一套模拟上每步加一个漂移(tilt / 250,tilt 是一年的对数收益偏多少),
tilt 由五个信号各投 −1 / 0 / +1、乘权重加起来。输出和统计底线同一套数,另加:
  tilt / signals / unvalidated    方向偏了多少、每个信号投了什么、哪些信号还没检验过
  buy                             「创业板指和本股同时站上 60 日线」最早落在哪个月

没有它会怎样:看板上只有「不带方向」的底线和 AI 的判断,中间缺一个按固定规则算、
能逐条追到输入的数。AI 错了,分不清是 AI 想歪了,还是这些领先指标本身就指错了方向。

为什么是加漂移、不动波动:信号说的是「偏涨还是偏跌」,不是「波动变大变小」;
只动漂移,tilt = 0 时就和底线逐位相同,两层的差别全部来自信号,好对照。
为什么截到 ±0.40:五个权重加起来 0.45;全部同向时也只让一年的中位价偏 e^±0.40
(约 +49% / −33%),不让几个粗糙的信号压过波动本身。
为什么没检验过的信号权重减半:设计要求「检验不出效果的信号往统计底线方向收,但照样出数」。

★ 随机数的约定(改了 tilt=0 就和底线对不上):
  个股:baseline.draw_normals(seed, n_paths, steps),steps 和底线一样 = max(max(horizons), bottom_window)。
  指数:另开一个发生器 default_rng(seed + 1) 抽同样形状的独立正态 e,
        指数的 z = ρ·个股的 z + √(1−ρ²)·e,ρ = 最近 lookback 天两者日对数收益的相关系数(截到 ±0.99)。
  买点要看 buy_horizon(250)天;steps 比它短时,个股多出来的几列从 default_rng(seed + 2) 抽,
  前 steps 列不变,所以期限题和见底题照样和底线逐位相同。
指数路径不加漂移:tilt 是对这只股票的判断,不是对大盘的判断。[设计取舍]

收盘的约定:closes 用后复权价;index_closes 用创业板指不复权收盘;
两条的第 i 个要是同一个交易日、最后一个都是 as_of 那天(这里只拿到数字,日子对不对得上查不出来)。
"""
from __future__ import annotations

import inspect
import itertools
import math
from datetime import date
from fractions import Fraction

import numpy as np

import baseline

# 年化对数漂移的权重(第一版的初值,会按成绩调)
# regime 2026-11-03 从 0.15 改成 0:历史补考 10 只光模块股 2018–2026 共 1051 题,
# 开着它方向得分 3 个月 / 半年 / 一年 = 43% / 45% / 40%,关掉回到 50%(不判断方向);
# 2018–22、2023+ 两段开着都低于 50%。指数在 60 日线下出题,之后 3 个月个股反而更常涨。
# 不反过来用:那是拿同一批题调参数,且 2023+ 反用也只有约 55%,不如每次猜涨的 59%。
# 信号值照算照记(页面、存档里还看得到它投了什么),只是不再影响方向。
# 出处:history_test.py 跑两次(一次照常、一次 --drop-signals regime),成绩用 summary 里的 dir_hit 比
# consensus、capex 同日从 0.10 改成 0:thinking_test.py 逐条考想法(29 只、2018-01~2026-10 每月底,
# 只用当时已公开的研报 / SEC 文件),「券商上调 → 涨」「云厂开支加速 → 涨」两条的看涨看跌之差
# 都跟碰巧分不开(错开月份检验,碰巧能这么大的占 11%~94%),券商那条 2018–22 年主池还是反的。
# 估值那条只在光模块主池有一点,留着;同行涨跌差还没单独考,先留着。
SIGNAL_WEIGHTS = {"regime": 0.0, "peer": 0.05, "consensus": 0.0, "capex": 0.0, "valuation": 0.05}
TILT_CAP = 0.40
UNVALIDATED_FACTOR = 0.5        # 没检验过的信号权重乘这个数

# 信号阈值
PEER_GAP = 10.0                 # 同行 10 日涨跌差,百分点
CAPEX_GAP = 5.0                 # 云厂资本开支同比的变化,百分点
PE_HIGH, PE_LOW = 80.0, 20.0    # PE 历史分位(0~100)

# 买点
BUY_MA = 60
BUY_HORIZON = 250
RHO_CLIP = 0.99

# 走前检验
PRICE_SIGNALS = ("regime", "peer")
WF_STEP = 63                    # 样本间隔 = 往后看的天数,窗口不重叠
WF_MIN_SIDE = 30                # 信号 +1、−1 各自至少这么多个样本
WF_MIN_DIFF = 3                 # +1 那边比 −1 那边上涨比例至少高这么多个百分点
WF_Z = 1.96                     # 单边两比例 z 检验的门槛

_BASE_OPTS = ("horizons", "n_paths", "lookback", "bottom_window")
_BASE_DEFAULTS = {k: inspect.signature(baseline.baseline_forecast).parameters[k].default for k in _BASE_OPTS}


# ── 输入包 → 信号 ────────────────────────────────────────────────────────────
def _value(inputs: dict, key: str):
    """输入包里某项的 value;没有这项、或取数失败(status=failed)返回 None。
    stale(沿用上次)、partial(不全)的值照样用。"""
    item = inputs.get(key)
    if not isinstance(item, dict) or item.get("status") == "failed":
        return None
    return item.get("value")


def _num(x) -> float | None:
    """有限的数字转 float;布尔、None、字符串、NaN 一律 None。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _field(value, *path):
    """value[path[0]][path[1]]…,中间哪层不是 dict 就返回 None。"""
    for k in path:
        if not isinstance(value, dict):
            return None
        value = value.get(k)
    return value


def _band(x: float, lo: float, hi: float) -> int:
    """x ≤ lo → −1,x ≥ hi → +1,中间 0。先取 6 位小数,免得 112.6 − 107.6 = 4.99999… 落到线下。"""
    x = round(x, 6)
    return -1 if x <= lo else (1 if x >= hi else 0)


def _peer_gaps(inputs: dict, code: str) -> list[float]:
    """本股相对同行的 10 日涨跌差(百分点)。peer.<A>.<B> 存的是 A − B:本股是 A 原样用,是 B 取反。
    gap10 = (A 的 10 日涨幅 − B 的 10 日涨幅) × 100,涨幅用简单收益 —— 走前检验也这么算,inputs.peer_value 要一致。"""
    gaps = []
    for key in sorted(inputs):
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "peer" or parts[1] == parts[2] or code not in parts[1:]:
            continue
        g = _num(_field(_value(inputs, key), "gap10"))
        if g is not None:
            gaps.append(g if parts[1] == code else -g)
    return gaps


def _sig_regime(inputs: dict, code: str) -> int | None:
    return {"above": 1, "below": -1}.get(_field(_value(inputs, "market.regime"), "state"))


def _sig_peer(inputs: dict, code: str) -> int | None:
    gaps = _peer_gaps(inputs, code)
    if not gaps:
        return None
    # 只配了一对时就是那一对;配了几对取平均
    return _band(sum(gaps) / len(gaps), -PEER_GAP, PEER_GAP)


def _net_sign(up, down) -> int | None:
    up, down = _num(up), _num(down)
    if up is None or down is None:
        return None
    return (up > down) - (up < down)


def _consensus(inputs: dict, code: str) -> tuple[int | None, str | None]:
    """券商信号和它的来源。先看盈利预测上下调:eps_revisions.<code> 的
    up30 / down30(近 30 天按净利润比调高、调低的篇数);这项缺了、取数失败或形状不对,
    退回评级:consensus.<code>.ratings 的 up − down。评级很少变,所以只当后备。"""
    ev = _value(inputs, f"eps_revisions.{code}")
    v = _net_sign(_field(ev, "up30"), _field(ev, "down30"))
    if v is not None:
        return v, "eps_revisions"
    ratings = _field(_value(inputs, f"consensus.{code}"), "ratings")
    v = _net_sign(_field(ratings, "up"), _field(ratings, "down"))
    return (v, "ratings") if v is not None else (None, None)


def _sig_consensus(inputs: dict, code: str) -> int | None:
    return _consensus(inputs, code)[0]


def _sig_capex(inputs: dict, code: str) -> int | None:
    total = _field(_value(inputs, "capex"), "total")
    if not isinstance(total, list) or len(total) < 2:
        return None
    prev, last = _num(_field(total[-2], "yoy")), _num(_field(total[-1], "yoy"))
    if prev is None or last is None:
        return None
    return _band(last - prev, -CAPEX_GAP, CAPEX_GAP)


def _sig_valuation(inputs: dict, code: str) -> int | None:
    key = f"valuation.{code}"
    v = _value(inputs, key)
    for f in ("pe_pctl", "pb_pctl"):
        x = _num(_field(v, f))
        if x is not None and not 0 <= x <= 100:
            raise ValueError(f"{key} 的 {f} = {x},不在 0~100(历史分位按 0~100 给)")
    p = _num(_field(v, "pe_pctl"))
    if p is None:
        return None
    return -_band(p, PE_LOW, PE_HIGH)      # 分位高 = 贵 → 看空


_SIGNAL_RULES = {"regime": _sig_regime, "peer": _sig_peer, "consensus": _sig_consensus,
                 "capex": _sig_capex, "valuation": _sig_valuation}


def signals_from_inputs(inputs: dict, code: str) -> dict:
    """输入包(inputs.py)→ 五个信号
    {"values": {名: −1|0|+1}, "missing": [名], "sources": {"consensus": "eps_revisions"|"ratings"|None}}。
      regime     market.regime.state:above → +1,below → −1
      peer       peer.<A>.<B>.gap10(A − B,百分点):≤ −10 → −1,≥ +10 → +1
      consensus  eps_revisions.<code> 的 up30 − down30 < 0 → −1,> 0 → +1;
                 这项用不了退回 consensus.<code>.ratings 的 up − down;用的哪个写进 sources
      capex      capex.total 最后两季的 yoy:最新比上一季高 ≥ 5 个点 → +1,低 ≥ 5 个点 → −1
      valuation  valuation.<code>.pe_pctl(0~100):≥ 80 → −1,≤ 20 → +1
    缺这项、取数失败、值的形状不对 → 记 0 并列进 missing(按上面的顺序),不抛异常;
    只有一种情况报 ValueError:pe_pctl / pb_pctl 是数字但不在 0~100(多半是字段放错了)。
    注意:给成 0~1 的分位(0.85)落在 0~100 里,这里查不出来,要靠取数那一侧的检查。"""
    inputs = inputs or {}
    values, missing = {}, []
    for name in SIGNAL_WEIGHTS:
        v = _SIGNAL_RULES[name](inputs, code)
        if v is None:
            missing.append(name)
        values[name] = 0 if v is None else int(v)
    return {"values": values, "missing": missing, "sources": {"consensus": _consensus(inputs, code)[1]}}


# ── 信号 → 漂移 ──────────────────────────────────────────────────────────────
def _signal_values(signals: dict) -> dict[str, int]:
    vals = (signals or {}).get("values") or {}
    unknown = sorted(set(vals) - set(SIGNAL_WEIGHTS))
    if unknown:
        raise ValueError(f"不认识的信号:{unknown},只有 {list(SIGNAL_WEIGHTS)}")
    out = {}
    for name in SIGNAL_WEIGHTS:
        v = vals.get(name, 0)
        if isinstance(v, bool) or v not in (-1, 0, 1):
            raise ValueError(f"信号 {name} 要是 −1、0、1,得到 {v!r}")
        out[name] = int(v)
    return out


def tilt_from(signals: dict, validated: dict) -> dict:
    """tilt = Σ 权重 × 信号值,没检验过的信号权重减半,再截到 ±TILT_CAP。
    validated 里没写的信号按没检验过算。返回 {"tilt", "values", "weights", "unvalidated"}。"""
    unknown = sorted(set(validated or {}) - set(SIGNAL_WEIGHTS))
    if unknown:
        raise ValueError(f"validated 里有不认识的信号:{unknown}")
    values = _signal_values(signals)
    passed = {n: bool((validated or {}).get(n, False)) for n in SIGNAL_WEIGHTS}
    weights = {n: w if passed[n] else w * UNVALIDATED_FACTOR for n, w in SIGNAL_WEIGHTS.items()}
    raw = sum(weights[n] * values[n] for n in SIGNAL_WEIGHTS)
    tilt = round(max(-TILT_CAP, min(TILT_CAP, raw)), 6) + 0.0     # + 0.0 把 −0.0 变成 0.0
    return {"tilt": tilt, "values": values, "weights": weights,
            "unvalidated": [n for n in SIGNAL_WEIGHTS if not passed[n]]}


# ── 指数路径 ─────────────────────────────────────────────────────────────────
def _tail_log_returns(closes, lookback: int, what: str) -> np.ndarray:
    px = np.asarray(closes, dtype=float)
    if px.ndim != 1 or len(px) < lookback + 1:
        raise ValueError(f"{what}要至少 {lookback + 1} 个收盘,只有 {len(px)} 个")
    tail = px[-(lookback + 1):]
    if not np.all(np.isfinite(tail)) or np.any(tail <= 0):
        raise ValueError(f"{what}最近的收盘里有 0、负数或空值,算不了对数收益")
    return np.diff(np.log(tail))


def return_corr(closes, index_closes, lookback: int = 120) -> float:
    """最近 lookback 天个股、指数日对数收益的相关系数,截到 ±RHO_CLIP。
    有一边完全不动(波动 0,相关系数没定义)记 0:当成互不相关。
    截到 0.99 是因为 ρ = ±1 时指数的独立部分系数 √(1−ρ²) = 0,指数就成了个股的复制品。"""
    a = _tail_log_returns(closes, lookback, "个股")
    b = _tail_log_returns(index_closes, lookback, "指数")
    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    r = float(np.corrcoef(a, b)[0, 1])
    if not math.isfinite(r):
        return 0.0
    return max(-RHO_CLIP, min(RHO_CLIP, r))


def index_normals(z_stock: np.ndarray, seed: int, rho: float) -> np.ndarray:
    """指数的标准正态:ρ·个股的 z + √(1−ρ²)·default_rng(seed+1) 抽的独立正态。
    混出来仍是标准正态,和个股的相关系数是 ρ。不改动 z_stock。"""
    indep = np.random.default_rng(seed + 1).standard_normal(z_stock.shape)
    return rho * z_stock + math.sqrt(1.0 - rho * rho) * indep


# ── 买点 ─────────────────────────────────────────────────────────────────────
def _above_ma(paths: np.ndarray, hist, ma: int) -> np.ndarray:
    """每条路径每一步:收盘是否严格高于 ma 日均线。均线 = 历史最后 ma−1 个收盘 + 模拟值,
    第 k 步的均线是「第 k 步和它前面 ma−1 个收盘」的平均。返回布尔 (n_paths, steps)。"""
    n, h = paths.shape
    tail = np.asarray(hist, dtype=float)[len(hist) - (ma - 1):] if ma > 1 else np.empty(0)
    if len(tail) < ma - 1 or not np.all(np.isfinite(tail)):
        raise ValueError(f"算 {ma} 日线要至少 {ma - 1} 个历史收盘,只有 {len(hist)} 个(或有空值)")
    full = np.concatenate([np.broadcast_to(tail, (n, ma - 1)), paths], axis=1)
    cs = np.zeros((n, full.shape[1] + 1))
    np.cumsum(full, axis=1, out=cs[:, 1:])
    # 第 k 步(1 起)在 full 的第 ma−2+k 列;窗口 = 第 k−1 到 ma−2+k 列 → cs[ma−1+k] − cs[k−1]
    return paths > (cs[:, ma:] - cs[:, :h]) / ma


def buy_block(stock_paths: np.ndarray, index_paths: np.ndarray, stock_hist, index_hist,
              start: date, holidays, horizon: int = BUY_HORIZON, ma: int = BUY_MA) -> dict:
    """「指数收盘在 ma 日线上 且 本股收盘在 ma 日线上」第一次同时成立落在哪个月。
    只看往后第 1..horizon 步(第 k 步落在第 k 个交易日);horizon 天里都没成立记 later。
    类别和 baseline.categories() 的 buy_months 一模一样,加起来 100。"""
    if horizon < 1 or ma < 1:
        raise ValueError(f"horizon、ma 要 ≥ 1,得到 {horizon}、{ma}")
    if stock_paths.shape[1] < horizon or index_paths.shape[1] < horizon:
        raise ValueError(f"路径要至少 {horizon} 步,得到 {stock_paths.shape}、{index_paths.shape}")
    both = (_above_ma(index_paths[:, :horizon], index_hist, ma)
            & _above_ma(stock_paths[:, :horizon], stock_hist, ma))
    day_months = baseline._day_months(start, horizon, holidays)
    months = baseline._distinct(day_months)
    pos = {m: i for i, m in enumerate(months)}
    step_month = np.array([pos[m] for m in day_months])
    bucket = np.where(both.any(axis=1), step_month[both.argmax(axis=1)], len(months))
    pct = baseline.largest_remainder(np.bincount(bucket, minlength=len(months) + 1).tolist())
    return {"conditions": [f"创业板指收盘在 {ma} 日线上", f"本股收盘在 {ma} 日线上"],
            "month_probs": dict(zip(months + [baseline.LATER], pct))}


# ── 系统算法 ─────────────────────────────────────────────────────────────────
def _options(baseline_kwargs: dict) -> dict:
    unknown = sorted(set(baseline_kwargs) - set(_BASE_OPTS))
    if unknown:
        raise TypeError(f"不认识的参数:{unknown},只收 {list(_BASE_OPTS)}")
    return {**_BASE_DEFAULTS, **baseline_kwargs}


def system_forecast(closes: list[float], index_closes: list[float], start: date, holidays: set[str],
                    signals: dict, validated: dict, seed: int = 20261102,
                    buy_horizon: int = BUY_HORIZON, **baseline_kwargs) -> dict:
    """系统算法。baseline_kwargs 收 horizons / n_paths / lookback / bottom_window,
    默认值取 baseline_forecast 的,两层用同样的参数才能对照。返回
    {"layer": "system", "vol_annual", "start_price", "horizons", "bottom"(这四项结构同底线),
     "tilt": float, "signals": {"values", "missing", "sources"(原样带出), "weights"(实际用的,没检验过的已减半)},
     "unvalidated": [名],
     "buy": {"conditions": [str, str], "month_probs": {"YYYY-MM": int, …, "later": int}, "rho": float}}
    同一组参数、同一个 seed,结果完全相同;tilt = 0 时 horizons、bottom 和底线逐位相同。
    closes 和 index_closes 由调用方先按日期取交集,第 i 个要是同一天(这里查不出错位)。"""
    opts = _options(baseline_kwargs)
    hs = baseline._check_args(opts["horizons"], opts["n_paths"], opts["bottom_window"])
    if buy_horizon < 1:
        raise ValueError(f"buy_horizon 要 ≥ 1,得到 {buy_horizon}")
    t = tilt_from(signals, validated)
    n_paths, lookback = opts["n_paths"], opts["lookback"]

    sigma = baseline.daily_sigma(closes, lookback)
    rho = return_corr(closes, index_closes, lookback)      # 先查两条收盘,报错时写明是哪条
    i_sigma = baseline.daily_sigma(index_closes, lookback)
    s0 = float(closes[-1])
    drift = t["tilt"] / baseline.TRADING_DAYS_PER_YEAR

    steps = max(max(hs), opts["bottom_window"])
    z = baseline.draw_normals(seed, n_paths, steps)
    paths = baseline.price_paths(s0, sigma, z, drift=drift)
    if buy_horizon > steps:      # 期限都比买点短:后面几列另抽,前 steps 列不动
        extra = np.random.default_rng(seed + 2).standard_normal((n_paths, buy_horizon - steps))
        z_buy = np.hstack([z, extra])
        buy_paths = baseline.price_paths(s0, sigma, z_buy, drift=drift)
    else:
        z_buy, buy_paths = z[:, :buy_horizon], paths[:, :buy_horizon]
    i_paths = baseline.price_paths(float(index_closes[-1]), i_sigma, index_normals(z_buy, seed, rho))
    buy = buy_block(buy_paths, i_paths, closes, index_closes, start, holidays, buy_horizon)

    return {"layer": "system",
            "vol_annual": round(sigma * math.sqrt(baseline.TRADING_DAYS_PER_YEAR), 4),
            "start_price": s0,
            "horizons": baseline.horizon_block(paths, s0, hs),
            "bottom": baseline.bottom_block(paths, s0, start, holidays, opts["bottom_window"]),
            "tilt": t["tilt"],
            "signals": {"values": t["values"], "missing": list((signals or {}).get("missing") or []),
                        "sources": dict((signals or {}).get("sources") or {}), "weights": t["weights"]},
            "unvalidated": t["unvalidated"],
            "buy": {**buy, "rho": round(rho, 4)}}


def baseline_buy(closes: list[float], index_closes: list[float], start: date, holidays: set[str],
                 seed: int = 20261102, **kw) -> dict:
    """统计底线层的买点题:五个信号全 0、同一个种子跑系统算法,只取 buy 那块。
    底线本身不模拟指数,买点题又要每层都有一个可比的数,就借系统算法的模拟、不加方向。"""
    zero = {"values": dict.fromkeys(SIGNAL_WEIGHTS, 0), "missing": []}
    return system_forecast(closes, index_closes, start, holidays, zero, {}, seed=seed, **kw)["buy"]


# ── 走前检验 ─────────────────────────────────────────────────────────────────
def _series(xs, n: int, what: str) -> np.ndarray:
    if len(xs) != n:
        raise ValueError(f"{what}有 {len(xs)} 个收盘,指数日期有 {n} 个,要按日期一一对应")
    return np.array([np.nan if x is None else float(x) for x in xs])


def _good(*xs) -> bool:
    return all(math.isfinite(x) and x > 0 for x in xs)


def _z_two_prop(h1: int, n1: int, h2: int, n2: int) -> float | None:
    """两比例 z 值:(h1/n1 − h2/n2) ÷ √(p(1−p)(1/n1 + 1/n2)),p = 两边合起来的比例。
    一边没样本返回 None;p 是 0 或 1(两边全涨或全跌,看不出差别)返回 0。"""
    if not n1 or not n2:
        return None
    p = (h1 + h2) / (n1 + n2)
    if p in (0.0, 1.0):
        return 0.0
    return (h1 / n1 - h2 / n2) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))


def signal_test(samples: list[tuple[int, bool]]) -> dict:
    """一组 (信号, 往后 63 天涨没涨) 的检验。算验证过要同时满足:
      信号 +1、−1 两边各 ≥ 30 个;
      +1 那边上涨比例 − −1 那边 ≥ 3 个百分点;
      单边两比例 z 检验 z ≥ 1.96(纯碰巧能过的机会约 2.5%)。
    没有 z 检验时,两边各三四十个样本、差 10 个点也常常只是碰巧:纯随机数据 200 次能过 48 次。
    差几个点用分数精确算;报出来的比例、差保留 1 位小数,z 保留 3 位。"""
    def rate(xs):
        return Fraction(100 * sum(xs), len(xs)) if xs else None

    def show(r, nd=1):
        return None if r is None else round(float(r), nd)

    every = [up for _, up in samples]
    plus = [up for s, up in samples if s == 1]
    minus = [up for s, up in samples if s == -1]
    r_plus, r_minus = rate(plus), rate(minus)
    diff = None if r_plus is None or r_minus is None else r_plus - r_minus
    z = _z_two_prop(sum(plus), len(plus), sum(minus), len(minus))
    validated = (len(plus) >= WF_MIN_SIDE and len(minus) >= WF_MIN_SIDE
                 and diff >= WF_MIN_DIFF and z >= WF_Z)
    return {"n": len(every), "n_plus": len(plus), "n_minus": len(minus),
            "up_all": show(rate(every)), "up_plus": show(r_plus), "up_minus": show(r_minus),
            "diff": show(diff), "z": show(z, 3), "validated": bool(validated)}


def _pairs(stocks: dict, pairs) -> list[tuple[str, str]]:
    if pairs is None:
        return list(itertools.combinations(stocks, 2))
    out = list(dict.fromkeys((str(a), str(b)) for a, b in pairs))     # 同一对写两遍只算一遍
    for a, b in out:
        if a == b or a not in stocks or b not in stocks:
            raise ValueError(f"同行那对 {a}/{b} 不对:两只要不同,并且都在 stocks 里({list(stocks)})")
    return out


def walk_forward_stats(index_dates: list[str], index_closes: list[float],
                       stocks: dict[str, list[float]], pairs=None, step: int = WF_STEP) -> dict:
    """regime、peer 两个只用价格的信号在历史上的成绩。
    每个信号只在一条序列上检验,同一天只出一个样本(几只股票同一天的涨跌高度相关,
    合在一起算会让样本显得比实际多,纯随机也容易「验证过」):
      regime  指数自己:那天指数收盘 vs 含当天的 60 日均线(高于 +1、低于 −1、相等 0),
              结果 = 往后 63 天指数涨没涨
      peer    每一对 A/B 分开:gap10 = (A 的 10 日涨幅 − B 的 10 日涨幅) × 100(简单收益,
              跟输入包 peer.<A>.<B>.gap10 同一种算法),≤ −10 → −1、≥ +10 → +1;
              结果 = 往后 63 天 A 涨没涨。配了几对就要每一对都过
    样本日 t = 59, 59+step, …,且 t+63 还在数据里;step = 63 时前后窗口不重叠。
    信号只用 t 当天及以前的数,结果只用 t 以后的数,不偷看。
    pairs 不给 = stocks 按顺序两两配对;调用方应传配置里的 peers。
    stocks 每条都要和 index_dates 一样长;没上市、停牌的位置填 None,用到它的样本跳过。
    返回 {"regime": signal_test 的结果, "peer": {"validated": bool, "pairs": {"A.B": signal_test 的结果}}}。"""
    n = len(index_dates)
    idx = _series(index_closes, n, "指数")
    px = {c: _series(s, n, f"股票 {c} ") for c, s in stocks.items()}
    prs = _pairs(px, pairs)
    h = WF_STEP
    regime: list[tuple[int, bool]] = []
    peer: dict[str, list[tuple[int, bool]]] = {f"{a}.{b}": [] for a, b in prs}
    for t in range(BUY_MA - 1, n - h, step):
        win = idx[t - BUY_MA + 1:t + 1]
        if _good(*win, idx[t + h]):
            ma, close = float(win.mean()), float(idx[t])
            regime.append(((close > ma) - (close < ma), bool(idx[t + h] > close)))
        for a, b in prs:
            pa, pb = px[a], px[b]
            if _good(pa[t], pa[t - 10], pb[t], pb[t - 10], pa[t + h]):
                gap = 100 * (pa[t] / pa[t - 10] - pb[t] / pb[t - 10])
                peer[f"{a}.{b}"].append((_band(gap, -PEER_GAP, PEER_GAP), bool(pa[t + h] > pa[t])))
    pair_stats = {k: signal_test(v) for k, v in peer.items()}
    return {"regime": signal_test(regime),
            "peer": {"validated": bool(pair_stats) and all(x["validated"] for x in pair_stats.values()),
                     "pairs": pair_stats}}


def validate_price_signals(index_dates: list[str], index_closes: list[float],
                           stocks: dict[str, list[float]], pairs=None) -> dict[str, bool]:
    """五个信号谁检验过:regime、peer 按 walk_forward_stats 的结果;
    consensus、capex、valuation 没有能对上日期的历史数据,固定 False(权重减半,照样出数)。"""
    stats = walk_forward_stats(index_dates, index_closes, stocks, pairs)
    done = {"regime": stats["regime"]["validated"], "peer": stats["peer"]["validated"]}
    return {n: done.get(n, False) for n in SIGNAL_WEIGHTS}
