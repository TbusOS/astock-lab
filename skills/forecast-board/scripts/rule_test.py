#!/usr/bin/env python3
"""rule_test —— 按规则买卖的历史补考:把几条买卖规则放回 2018 年以来真做一遍,跟一直拿着比。

它是什么:每条规则在每个交易日收盘看一次「该不该拿着」,第二个交易日收盘照做(扣成本),
得到一条资金曲线;再算年化收益、中途最多亏过多少(最大回撤)、买卖了几次。
另有一类「事件」统计:从一年高点跌超一半那天买,之后 3 / 6 / 12 个月怎么走。

没有它会怎样:history_test 只考「价格会落在哪个范围、涨跌各几成」。
预测猜不准方向时,我们说不出「照某条规则买卖,历史上比一直拿着好还是差」——
而用的人真正要决定的是什么时候买、什么时候卖。

为什么这样设计:
- 收盘出信号、下一个交易日收盘成交:出信号那天的收盘已经知道,同一个收盘成交等于偷看。
- 每次买或卖扣 0.1%(佣金 + 印花税的粗估),来回频繁的规则不能白占便宜。
- 卖了以后要等「本股和创业板指都站上 60 日线」重新成立(先跌破过一次)才买回:
  不然一卖完条件还亮着,第二天又买回去,规则等于没卖。
- 加一组对照股票:主池是今天还在、这几年涨得多的光模块股,「跌深了就买」在这批股票上一定好看。
  对照组用东财行业板块里同行业、没进主池的股票,只按上市时间和是不是 ST 筛,不看后来涨跌。
  退市的股票取不到日线,对照组也还是今天还在的股票,偏差只是变小,没有消失。
- 分 2018–2022 / 2023 起两段报:2023 年起这批股票大涨,两段方向不一样时结论要打折扣。

什么场景用:改了买回条件、卖出线这类规则以后跑一次,看历史上它帮你躲过了多少回撤、
又少赚了多少;页面上回答「现在该不该买 / 卖」时引用这里的成绩。

机制:
- 时间线用每只股票自己的交易日(停牌那几天没有日线,不补);创业板指按日期查。
- 60 日线含当天;「站上」= 收盘严格高于均线(和 system_forecast 的买点一样)。
- 资金曲线第 i 天的涨跌 = 第 i−1 天收盘时手上拿没拿着 × 当天涨跌;成交在收盘,当天先算涨跌再换仓。
- 规则函数只拿到第 i 天及以前的收盘;check_board_rules.py 用「改掉以后的价格,信号不变」查这一点。

用法:
    STOCK_LAB=<内部仓> python3 rule_test.py --out <文件> [--codes …] [--controls …]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import fb_common  # noqa: F401  先于 prices(market.py 会删代理变量)
import history_test as ht

COST = 0.001                    # 每次买或卖扣的比例
MA = 60
SIGMA_DAYS = 120                # BEAR 用的波动:买入那天往前 120 个日对数收益
BEAR_DAYS = 63                  # BEAR 的线 = 买入价 × exp(−σ × √(63/250)),一个季度一倍标准差
DD_LIMIT = 0.30                 # DD30:从持有以来最高收盘回撤这么多就卖
DIP = 0.50                      # DIP50:比之前 250 日最高收盘低这么多
DIP_LOOK = 250
DIP_GAP = 63                    # 同一只股票两次 DIP50 事件至少隔这么多个交易日(否则在 −50% 附近来回穿会算很多次)
THIN_YEAR = 200                 # 一年里日线少于这么多根(上市那年、今年除外)就在 meta 里标出来
FWD = (63, 126, 250)
SPLIT = "2023-01-01"            # 分两段:之前 / 这天及以后
MIN_HISTORY = SIGMA_DAYS + 1    # 起点之前至少要这么多根日线
RULES = ("H", "T60", "T60I", "DD30", "BEAR")
RULE_TEXT = {"H": "一直拿着", "T60": "本股在 60 日线上才拿着", "T60I": "本股和创业板指都在 60 日线上才拿着",
             "DD30": "拿着,从持有以来最高回撤 30% 卖,等 T60I 重新成立买回",
             "BEAR": "拿着,跌破买入价 × exp(−σ√(63/250)) 卖,等 T60I 重新成立买回",
             "DCA": "每月最后一个交易日投同样的钱"}
# 主池:东财「CPO 概念」里主业是光模块的 10 只(和 history_test 补考同一批)
MAIN_POOL = ("300308", "300502", "300394", "002281", "000988", "603083", "300620", "300548", "300570", "002902")
# 对照组:东财行业板块「通信网络设备及器件」(主池 10 只里 8 只在这个板块)+「通信线缆及配套」(光纤光缆)
# 的成分股里,2017-01-01 以前上市、不在主池、名字不带 ST 的全部(2026-11-03 取的成分,不看涨跌)
CONTROL_POOL = ("000063", "002194", "002296", "002396", "002583", "002792", "002796", "300098", "300134",
                "300565", "600498", "603421",                                    # 通信网络设备及器件
                "000070", "000586", "002491", "300563", "600105", "600487", "600522")  # 通信线缆及配套
CONTROL_NOTE = ("东财行业板块「通信网络设备及器件」+「通信线缆及配套」成分股里 2017-01-01 以前上市、"
                "不在主池、不是 ST 的全部;退市股取不到,对照组仍是今天还在的股票")


# ── 均线、波动 ───────────────────────────────────────────────────────────────
def moving_avg(closes: list[float], n: int) -> list[float | None]:
    """含当天的 n 日均线;前 n−1 天没有(None)。"""
    out, run = [], 0.0
    for i, c in enumerate(closes):
        run += c - (closes[i - n] if i >= n else 0.0)
        out.append(run / n if i >= n - 1 else None)
    return out


def above(closes: list[float], ma: list[float | None], i: int) -> bool:
    return ma[i] is not None and closes[i] > ma[i]


def ann_sigma(closes: list[float], i: int, n: int = SIGMA_DAYS) -> float | None:
    """第 i 天往前 n 个日对数收益(用到第 i 天收盘)的年化波动;不够 n 个返回 None。"""
    if i < n:
        return None
    r = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    return statistics.pstdev(r) * math.sqrt(250)


# ── 一只股票的时间线 ─────────────────────────────────────────────────────────
def make_view(series: dict[str, float], index_px: dict[str, float]) -> dict:
    """股票自己的交易日、收盘、60 日线,和同一天创业板指在不在它自己的 60 日线上。"""
    days = sorted(d for d in series if d in index_px)
    closes = [series[d] for d in days]
    idays = sorted(index_px)
    icl = [index_px[d] for d in idays]
    ima = moving_avg(icl, MA)
    iup = {d: above(icl, ima, k) for k, d in enumerate(idays)}
    return {"days": days, "closes": closes, "ma": moving_avg(closes, MA), "index_up": [iup[d] for d in days]}


def both_up(v: dict, i: int) -> bool:
    return above(v["closes"], v["ma"], i) and v["index_up"][i]


# ── 规则:看第 i 天收盘,说下一个交易日收盘后该不该拿着 ─────────────────────────
# state 由 simulate 维护:hold(现在拿没拿着)、entry(买入价)、entry_i、peak(持有以来最高收盘)、armed
def rule_h(v, i, st):
    return True


def rule_t60(v, i, st):
    return above(v["closes"], v["ma"], i)


def rule_t60i(v, i, st):
    return both_up(v, i)


def _rebuy(v, i, st) -> bool:
    """卖出以后:T60I 先不成立过一次(armed),再成立才买回。"""
    return st["armed"] and both_up(v, i)


def rule_dd30(v, i, st):
    if st["hold"]:
        return v["closes"][i] > (1 - DD_LIMIT) * st["peak"]
    return _rebuy(v, i, st)


def rule_bear(v, i, st):
    if st["hold"]:
        sig = ann_sigma(v["closes"], st["entry_i"]) or 0.0
        return v["closes"][i] >= st["entry"] * math.exp(-sig * math.sqrt(BEAR_DAYS / 250))
    return _rebuy(v, i, st)


RULE_FN = {"H": rule_h, "T60": rule_t60, "T60I": rule_t60i, "DD30": rule_dd30, "BEAR": rule_bear}
STARTS_HOLDING = {"H": True, "T60": False, "T60I": False, "DD30": True, "BEAR": True}


def simulate(v: dict, start: int, decide, hold0: bool, cost: float = COST) -> dict:
    """从第 start 天收盘起按 decide 买卖。返回 {"eq": [每天收盘后的资金], "held": [这一天的涨跌算不算],
    "trades": [成交那天的下标], "wants": [每天收盘时规则的回答]}。hold0 = 起点那天收盘就买入(扣成本)。"""
    c = v["closes"]
    st = {"hold": hold0, "entry": c[start], "entry_i": start, "peak": c[start], "armed": False}
    eq, held, trades, wants = [], [], [], []
    e, pending = (1 - cost) if hold0 else 1.0, None
    if hold0:
        trades.append(start)
    for i in range(start, len(c)):
        h_before = st["hold"]
        if i > start:
            e *= c[i] / c[i - 1] if h_before else 1.0
        held.append(h_before and i > start)
        if pending is not None:
            e *= 1 - cost
            trades.append(i)
            st = {**st, "hold": pending, "entry": c[i], "entry_i": i, "peak": c[i], "armed": False}
            pending = None
        if st["hold"]:
            st = {**st, "peak": max(st["peak"], c[i])}
        elif not both_up(v, i):
            st = {**st, "armed": True}
        want = bool(decide(v, i, st))
        wants.append(want)
        if want != st["hold"]:
            pending = want
        eq.append(e)
    return {"eq": eq, "held": held, "trades": trades, "wants": wants}


def dca(v: dict, start: int, end: int, cost: float = COST) -> dict:
    """[start, end] 里每个月最后一个交易日投 1 份。返回最终市值 ÷ 投入、期间最差的市值 ÷ 投入、投了几次。"""
    days, c = v["days"], v["closes"]
    shares = invested = 0.0
    worst, n = None, 0
    for i in range(start, end + 1):
        last_of_month = i == len(days) - 1 or days[i][:7] != days[i + 1][:7]
        if last_of_month and i < len(days) - 1:
            shares += (1 - cost) / c[i]
            invested += 1.0
            n += 1
        if invested:
            r = shares * c[i] / invested
            worst = r if worst is None else min(worst, r)
    final = shares * c[end] / invested if invested else None
    return {"multiple": final, "worst": worst, "n": n}


# ── 成绩 ────────────────────────────────────────────────────────────────────
def curve_stats(dates: list[str], eq: list[float]) -> dict:
    """一段资金曲线的年化、最大回撤。dates[0] 那天的值当起点。"""
    if len(eq) < 2:
        return {"cagr": None, "mdd": None}
    years = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365.25
    peak, mdd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 else None
    return {"cagr": cagr, "mdd": mdd}


def segments(days: list[str], start: int) -> dict[str, tuple[int, int]]:
    """全段 / 2018–2022 / 2023 起,各自的 (起点下标, 终点下标)。起点取段前最后一天,让第一天的涨跌算进去。"""
    n = len(days)
    cut = next((i for i in range(start, n) if days[i] >= SPLIT), n)
    out = {"all": (start, n - 1)}
    if cut - 1 > start:
        out["early"] = (start, cut - 1)
    if cut < n - 1:
        out["late"] = (max(start, cut - 1), n - 1)
    return out


def seg_stats(v: dict, start: int, sim: dict, a: int, b: int) -> dict:
    """sim 是从 start 开始的模拟,取 [a, b] 这一段。"""
    eq = sim["eq"][a - start:b - start + 1]
    held = sim["held"][a - start + 1:b - start + 1]
    s = curve_stats(v["days"][a:b + 1], eq)
    return {**s, "trades": sum(1 for t in sim["trades"] if a < t <= b or (t == a == start)),
            "held": sum(held) / len(held) if held else None}


def start_index(v: dict, begin: str) -> int | None:
    """第一个 ≥ begin、前面有够 MIN_HISTORY 根日线的交易日。"""
    return next((i for i, d in enumerate(v["days"]) if d >= begin and i >= MIN_HISTORY), None)


def stock_report(v: dict, begin: str, cost: float = COST) -> dict | None:
    start = start_index(v, begin)
    if start is None:
        return None
    segs = segments(v["days"], start)
    sims = {r: simulate(v, start, RULE_FN[r], STARTS_HOLDING[r], cost) for r in RULES}
    rules = {r: {k: seg_stats(v, start, sims[r], a, b) for k, (a, b) in segs.items()} for r in RULES}
    for r in RULES:
        for k in segs:
            h, x = rules["H"][k], rules[r][k]
            rules[r][k] = {**x, "cagr_vs_h": _diff(x["cagr"], h["cagr"]), "mdd_saved": _diff(h["mdd"], x["mdd"])}
    # 定投不需要「段前最后一天」当起点:2023 起那段从 2023 年第一个交易日开始投
    rules["DCA"] = {k: dca(v, a + (k == "late" and v["days"][a] < SPLIT), b, cost) for k, (a, b) in segs.items()}
    curves = {r: dict(zip(v["days"][start:], sims[r]["eq"])) for r in RULES}
    return {"start": v["days"][start], "end": v["days"][-1], "rules": rules, "curves": curves}


def _diff(a, b):
    return None if a is None or b is None else a - b


# ── DIP50 事件 ──────────────────────────────────────────────────────────────
def dip_events(v: dict, begin: str) -> list[int]:
    """收盘比之前 250 日最高收盘低 50% 以上的第一天;之后要先回到 −50% 以上、
    并且离上一次至少 DIP_GAP 个交易日,才算新事件。"""
    c, out, armed = v["closes"], [], True
    for i in range(DIP_LOOK, len(c)):
        hi = max(c[i - DIP_LOOK:i])
        deep = c[i] <= (1 - DIP) * hi
        spaced = not out or i - out[-1] >= DIP_GAP
        if deep and armed and spaced and v["days"][i] >= begin:
            out.append(i)
        armed = not deep
    return out


def forward(v: dict, i: int, h: int) -> dict | None:
    """第 i 天出信号、第 i+1 天收盘买,之后 h 天的涨跌、途中最低还要再跌多少(没再跌记 0)、
    最低那天离买入几个交易日(没再跌记 0)。"""
    c = v["closes"]
    if i + 1 + h >= len(c):
        return None
    base = c[i + 1]
    win = c[i + 2:i + 2 + h]
    k = min(range(len(win)), key=win.__getitem__)
    lower = win[k] < base
    return {"ret": c[i + 1 + h] / base - 1, "further": min(0.0, win[k] / base - 1),
            "days_to_low": k + 1 if lower else 0}


def fwd_summary(items: list[dict | None]) -> dict:
    xs = [x for x in items if x]
    if not xs:
        return {"n": 0}
    worse = sorted(x["further"] for x in xs)
    return {"n": len(xs), "up": sum(x["ret"] > 0 for x in xs) / len(xs),
            "median_ret": statistics.median(x["ret"] for x in xs),
            "median_further": statistics.median(worse),
            "q10_further": worse[int(0.1 * (len(worse) - 1))],          # 十次里最差那一次大概再跌多少
            "median_days_to_low": statistics.median(x["days_to_low"] for x in xs)}


def dip_report(views: dict[str, dict], begin: str) -> dict:
    ev = {c: dip_events(v, begin) for c, v in views.items()}
    events = [{"code": c, "date": views[c]["days"][i]} for c, idx in ev.items() for i in idx]
    out = {"events": events}
    for h in FWD:
        hit = [forward(views[c], i, h) for c, idx in ev.items() for i in idx]
        every = [forward(v, i, h) for c, v in views.items()
                 for i in range((start_index(v, begin) or len(v["days"])), len(v["days"]))]
        out[str(h)] = {"dip": fwd_summary(hit), "any_day": fwd_summary(every)}
    return out


# ── 汇总 ────────────────────────────────────────────────────────────────────
def portfolio(reports: dict[str, dict], rule: str) -> dict[str, float]:
    """等权组合:每天取当天有日线的股票的涨跌平均(每天再平衡)。返回 {日期: 资金}。"""
    rets: dict[str, list[float]] = {}
    for rep in reports.values():
        cur = rep["curves"][rule]
        ds = sorted(cur)
        for d0, d1 in zip(ds, ds[1:]):
            rets.setdefault(d1, []).append(cur[d1] / cur[d0] - 1)
    e, out = 1.0, {}
    for d in sorted(rets):
        e *= 1 + sum(rets[d]) / len(rets[d])
        out[d] = e
    return out


def group_summary(reports: dict[str, dict]) -> dict:
    out = {}
    for r in RULES:
        curve = portfolio(reports, r)
        ds = sorted(curve)
        seg = {"all": ds, "early": [d for d in ds if d < SPLIT], "late": [d for d in ds if d >= SPLIT]}
        out[r] = {}
        for k, dd in seg.items():
            med = {f: _median([rep["rules"][r].get(k, {}).get(f) for rep in reports.values()])
                   for f in ("cagr", "mdd", "trades", "held", "cagr_vs_h", "mdd_saved")}
            out[r][k] = {"median": med, "portfolio": curve_stats(dd, [curve[d] for d in dd])}
    out["DCA"] = {k: {f: _median([rep["rules"]["DCA"].get(k, {}).get(f) for rep in reports.values()])
                      for f in ("multiple", "worst")} for k in ("all", "early", "late")}
    return out


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


# ── 取数、主流程 ─────────────────────────────────────────────────────────────
def bars_by_year(days: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in days:
        out[d[:4]] = out.get(d[:4], 0) + 1
    return out


def thin_years(days: list[str], as_of: str) -> list[str]:
    """日线根数明显不够的年份(上市那年、今年除外)—— 取数漏了整段时会出现,要标出来。"""
    by = bars_by_year(days)
    first = min(by) if by else None
    return [y for y, n in sorted(by.items()) if n < THIN_YEAR and y not in (first, as_of[:4])]


def run(codes, controls, begin: str = "2018-01-01", data_start: str = "2016-06-01", fetch=None,
        cost: float = COST) -> dict:
    fetch = fetch or ht.NetFetch()
    index_px = fetch.index(data_start)
    groups, failed = {}, {}
    for name, pool in (("main", codes), ("control", controls)):
        views = {}
        for c in pool:
            try:
                views[c] = make_view(fetch.hfq(c, data_start), index_px)
            except (ValueError, OSError) as e:      # 取不到的写进 failed,不顶替
                failed[c] = str(e)[:200]
        reps = {c: r for c, r in ((c, stock_report(v, begin, cost)) for c, v in views.items()) if r}
        thin = {c: t for c, t in ((c, thin_years(v["days"], max(index_px))) for c, v in views.items()) if t}
        groups[name] = {"stocks": {c: {**{k: x for k, x in r.items() if k != "curves"},
                                       "bars_by_year": bars_by_year(views[c]["days"])} for c, r in reps.items()},
                        "thin_years": thin,
                        "summary": group_summary(reps) if reps else {}, "dip50": dip_report(views, begin)}
    return {"meta": {"as_of": max(index_px), "begin": begin, "cost": cost, "split": SPLIT, "rules": RULE_TEXT,
                     "main": list(codes), "control": list(controls), "control_note": CONTROL_NOTE,
                     "failed": failed, "commit": ht._commit(),
                     "timing": "收盘出信号,下一个交易日收盘成交;每次买或卖扣 cost"},
            "groups": groups}


def _p(x, w=5):
    return " " * (w - 2) + "—" if x is None else f"{100 * x:{w}.0f}%"


def print_summary(res: dict) -> None:
    m = res["meta"]
    print(f"截至 {m['as_of']},起点 {m['begin']};{m['timing']}")
    for g, label in (("main", "主池"), ("control", "对照组")):
        s = res["groups"][g]["summary"]
        print(f"\n== {label}({len(res['groups'][g]['stocks'])} 只)等权组合:年化 / 最大回撤;各股中位数:交易次数")
        for seg, sl in (("early", "2018–22"), ("late", "2023 起")):
            print(f"  {sl}")
            for r in RULES:
                x = s[r][seg]
                print(f"    {r:<5} {_p(x['portfolio']['cagr'])} / {_p(x['portfolio']['mdd'])}"
                      f"  交易 {x['median']['trades'] or 0:.0f}")
            d = s["DCA"][seg]
            print(f"    DCA   最终 {d['multiple'] or 0:.2f} 倍投入,最差时 {d['worst'] or 0:.2f} 倍")
        dp = res["groups"][g]["dip50"]
        months = len({e["date"][:7] for e in dp["events"]})
        print(f"  DIP50 事件 {len(dp['events'])} 次,落在 {months} 个不同月份;日线不全的:"
              f"{res['groups'][g]['thin_years'] or '没有'}")
        for h in FWD:
            a, b = dp[str(h)]["dip"], dp[str(h)]["any_day"]
            if a["n"]:
                print(f"    {h} 天:事件 {a['n']} 次 涨 {_p(a['up'])} 中位 {_p(a['median_ret'])} 再跌中位 "
                      f"{_p(a['median_further'])} 最差一成 {_p(a['q10_further'])} 到最低中位 "
                      f"{a['median_days_to_low']:.0f} 天 | 随便哪天 涨 {_p(b['up'])} 中位 {_p(b['median_ret'])}"
                      f" 再跌中位 {_p(b['median_further'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按规则买卖的历史补考(主池 + 对照组)")
    ap.add_argument("--out", required=True, help="结果写到哪个 json 文件")
    ap.add_argument("--codes", default=",".join(MAIN_POOL))
    ap.add_argument("--controls", default=",".join(CONTROL_POOL))
    ap.add_argument("--begin", default="2018-01-01")
    ap.add_argument("--data-start", default="2016-06-01")
    ap.add_argument("--cost", type=float, default=COST)
    a = ap.parse_args(argv)
    split = lambda s: [c.strip() for c in s.split(",") if c.strip()]  # noqa: E731
    res = run(split(a.codes), split(a.controls), a.begin, a.data_start, cost=a.cost)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print_summary(res)
    print(f"\n写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
