#!/usr/bin/env python3
"""check_board_history —— 预测看板「历史补考」(skills/forecast-board/scripts/history_test.py)的检查。

用法:
    python3 scripts/check_board_history.py        # 退出码 0 才算过

不联网、不碰真实数据:合成行情(固定种子),取数用一个假的 fetch 对象顶替。
最要紧的两条:
  1. 不偷看 —— 出题日以后的价格全部改掉,那天的预测和「检验过没有」必须一个字不变;
     并且故意写一个偷看的版本,同一道检查必须抓得到(否则这道检查是摆设)。
  2. 带子的数对得上 —— 纯随机、波动不变的合成行情上,10~90 的带子应接住约八成;
     故意把波动乘 0.4 再量,必须明显接不住。
"""
import copy
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401  先于 baseline(market.py 会删代理变量,见 fb_common.ORIG_ENV)
import history_test as ht  # noqa: E402
import scoring  # noqa: E402

FAILS = 0


def ok(cond, what):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILS += 1


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _walk(cal, s0, sd, seed, drift=0.0):
    rng = random.Random(seed)
    px, p = {}, s0
    for d in cal:
        p *= math.exp(drift + sd * rng.gauss(0, 1))
        px[d] = p
    return px


class FakeFetch:
    """顶替联网取数:日历、创业板指、后复权收盘、最新不复权收盘。"""

    def __init__(self, cal, index_px, px, raw_last):
        self.cal, self.index_px, self.px, self.raw = cal, index_px, px, raw_last

    def calendar(self, start):
        return [d for d in self.cal if d >= start]

    def index(self, start):
        return {d: v for d, v in self.index_px.items() if d >= start}

    def hfq(self, code, start):
        return {d: v for d, v in self.px[code].items() if d >= start}

    def last_raw(self, code):
        return self.raw[code]


CAL = _weekdays(date(2017, 1, 2), 1600)            # 约 6 年多
INDEX = _walk(CAL, 2000.0, 0.015, 1)
PX = {"A": _walk(CAL, 100.0, 0.03, 2), "B": _walk(CAL, 50.0, 0.03, 3)}


def check_month_ends():
    print("== 出题日:每月最后一个交易日")
    cal = ["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02", "2020-03-03"]
    got = [cal[i] for i in ht.month_end_indices(cal, "2020-01-01")]
    ok(got == ["2020-01-31", "2020-02-28"], f"1、2 月的月末,3 月还没过完不算:{got}")
    got2 = [cal[i] for i in ht.month_end_indices(cal, "2020-02-01")]
    ok(got2 == ["2020-02-28"], f"start 之前的月末不算:{got2}")


def check_signals():
    print("== 两个行情信号")
    idx = [100.0] * 59 + [130.0]
    ok(ht.regime_value(idx, 59) == 1, "最后一天收盘高于含当天的 60 日均线 → +1")
    idx2 = [100.0] * 59 + [70.0]
    ok(ht.regime_value(idx2, 59) == -1, "低于 → −1")
    ok(ht.regime_value([100.0] * 60, 59) == 0, "相等 → 0")
    cal = _weekdays(date(2020, 1, 1), 12)
    pa = {d: 100.0 for d in cal}
    pb = {d: 100.0 for d in cal}
    pa[cal[11]] = 120.0                             # A 10 日 +20%,B 0%
    g = ht.peer_gap10(pa, pb, cal, 11)
    ok(g is not None and abs(g - 20.0) < 1e-9, f"gap10 = (A 涨幅 − B 涨幅) × 100 = 20:{g}")
    del pb[cal[1]]
    ok(ht.peer_gap10(pa, pb, cal, 11) is None, "10 天前那天 B 没有收盘 → None,不拿别的日子顶")
    sig = ht.signals_at("A", 200, CAL, PX, INDEX, [("A", "B")])
    ok(set(sig["values"]) == {"regime", "peer", "consensus", "capex", "valuation"},
       "五个信号都有值(没有当时数据的三个记 0)")
    ok(set(sig["missing"]) == {"consensus", "capex", "valuation"}, f"缺的三个写进 missing:{sig['missing']}")
    sb = ht.signals_at("B", 200, CAL, PX, INDEX, [("A", "B")])
    ga = ht.peer_gap10(PX["A"], PX["B"], CAL, 200)
    ok(sig["values"]["peer"] == ht.band_peer(ga) and sb["values"]["peer"] == ht.band_peer(-ga),
       "同一对里 B 用的是取反的 gap10")
    sc = ht.signals_at("C", 200, CAL, {**PX, "C": PX["A"]}, INDEX, [("A", "B")])
    ok(sc["values"]["peer"] == 0 and "peer" in sc["missing"], "没配对的股票同行信号记 0、写进 missing")


def _future_changed(px, t):
    """出题日以后的价格全部乘 3。"""
    cut = CAL[t]
    return {c: {d: (v * 3 if d > cut else v) for d, v in s.items()} for c, s in px.items()}


def check_no_peek():
    print("== 不偷看:改掉出题日以后的价格,预测不变")
    t = 700
    hol = ht.holidays_from(CAL)
    a = ht.forecast_at("A", t, CAL, PX, INDEX, [("A", "B")], hol, n_paths=1500, seed=7)
    px2 = _future_changed(PX, t)
    idx2 = {d: (v * 3 if d > CAL[t] else v) for d, v in INDEX.items()}
    b = ht.forecast_at("A", t, CAL, px2, idx2, [("A", "B")], hol, n_paths=1500, seed=7)
    ok(a is not None and a == b, "预测(两层的分位、涨的种数、波动、漂移)逐位相同")
    va = ht.validated_at(t, CAL, INDEX, PX, [("A", "B")])
    vb = ht.validated_at(t, CAL, idx2, px2, [("A", "B")])
    ok(va == vb, f"「检验过没有」也只用出题日以前的数:{va}")

    # 反向:一个偷看 5 天的版本,同一道检查必须抓到
    def leaky(px, idx):
        cal5 = CAL[:t + 6]
        return ht.forecast_at("A", t + 5, cal5, px, idx, [("A", "B")], hol, n_paths=1500, seed=7)
    ok(leaky(PX, INDEX) != leaky(px2, idx2), "反向自测:偷看出题日以后 5 天的版本,结果会变 → 这道检查抓得到")


def check_outcome():
    print("== 结算:到期那天的收盘、停牌顺延、期间最低")
    cal = _weekdays(date(2020, 1, 1), 300)
    s = {d: 100.0 + i for i, d in enumerate(cal)}
    s[cal[63]] = 999.0
    out = ht.outcome_at(s, cal, 0, (63, 126, 250), 126)
    ok(out["63"]["close"] == 999.0 and out["63"]["date"] == cal[63], "第 63 个交易日的收盘")
    del s[cal[126]]
    out = ht.outcome_at(s, cal, 0, (63, 126, 250), 126)
    ok(out["126"]["close"] == 100.0 + 125 and out["126"]["used"] == cal[125], "停牌那天顺延用之前最后一个收盘")
    ok(out["low"]["close"] == 101.0 and out["low"]["complete"], "期间最低 = 出题日以后 126 天里的最低收盘")
    short = ht.outcome_at(s, cal, 200, (63, 126, 250), 126)
    ok(short["250"] is None and short["63"] is not None, "日历还没走到的期限记 None(待结算)")
    ok(short["low"]["complete"] is False, "期间最低的窗口没走完 → complete=False")


def check_score():
    print("== 每道题的打分")
    layer = {"h": {"63": {"p_up": 70, "q": {"10": 80.0, "25": 90.0, "50": 100.0, "75": 110.0, "90": 120.0}}},
             "low_q": {"10": 70.0, "25": 80.0, "50": 85.0, "75": 90.0, "90": 95.0}}
    out = {"63": {"close": 125.0}, "126": None, "250": None, "low": {"close": 75.0, "complete": True}}
    sc = ht.score_layer(layer, 100.0, out)
    x = sc["63"]
    ok(x["up"] is True and abs(x["brier"] - 0.09) < 1e-9, "涨了、说 70 种涨 → 概率误差 0.09")
    ok(x["dir_hit"] == 1.0, "说偏涨、真涨 → 方向对")
    ok(x["in80"] is False and x["above90"] is True and x["in50"] is False, "125 在 90 分位 120 外面")
    want = scoring.interval_score(math.log(0.8), math.log(1.2), math.log(1.25), 0.2)
    ok(abs(x["is80"] - want) < 1e-12, "区间得分用 scoring.interval_score(对数比值、alpha 0.2)")
    ok("126" not in sc, "没结算的期限不打分")
    ok(sc["low"]["in80"] is True, "期间最低 75 落在最低价 10~90(70~95)里")
    half = ht.score_layer({"h": {"63": {**layer["h"]["63"], "p_up": 50}}, "low_q": layer["low_q"]}, 100.0, out)
    ok(half["63"]["dir_hit"] == 0.5, "说 50 种 → 方向算半对")


def check_band_and_coverage():
    print("== 逐日带子的公式,和纯随机行情上的覆盖率")
    hol = ht.holidays_from(CAL)
    fc = ht.forecast_at("A", 700, CAL, PX, INDEX, [("A", "B")], hol, n_paths=20000, seed=11)
    base = fc["layers"]["baseline"]
    sim90 = base["h"]["63"]["q"]["90"]
    calc90 = ht.band(fc["s0"], base["sigma"], base["drift"], 63, 90)
    ok(abs(sim90 / calc90 - 1) < 0.02, f"公式算的第 63 天 90 分位和模拟差不到 2%:{sim90:.2f} vs {calc90:.2f}")

    res = ht.run(["A", "B"], start="2017-08-01", data_start="2017-01-01", pairs=[("A", "B")],
                 n_paths=1500, seed=5, fetch=FakeFetch(CAL, INDEX, PX, {"A": 10.0, "B": 5.0}),
                 chart_codes=["A"])
    s = res["summary"]["baseline"]["63"]
    ok(s["n"] >= 100, f"题数够多:{s['n']}")
    ok(0.68 <= s["cover80"] <= 0.92, f"纯随机行情上 10~90 接住约八成:{s['cover80']:.2f}")
    ok(0.38 <= s["cover50"] <= 0.62, f"25~75 接住约五成:{s['cover50']:.2f}")

    # 反向:波动乘 0.4,同一批结算结果必须明显接不住
    hits = []
    for r in res["questions"]:
        o = r["outcome"]["63"]
        if not o:
            continue
        b = r["layers"]["baseline"]
        lo = ht.band(r["s0"], b["sigma"] * 0.4, 0.0, 63, 10)
        hi = ht.band(r["s0"], b["sigma"] * 0.4, 0.0, 63, 90)
        hits.append(lo <= o["close"] <= hi)
    rate = sum(hits) / len(hits)
    ok(rate < 0.6, f"反向自测:波动乘 0.4(理论上约接住 39%)后只接住 {rate:.2f},明显低于八成 → 覆盖率这道检查是灵的")

    ok(res["summary"]["system"]["63"]["n"] == s["n"], "两层的题一一对应")
    ok("skill_is80" in res["summary"]["system"]["63"], "系统算法有「比统计底线少错多少」")
    ok(set(res["summary"]["baseline"]["63"]["by_period"]) <= {"early", "late"}, "分前后两段")
    ok(res["price_factor"]["A"] == 10.0 / PX["A"][CAL[-1]], "换算到今天价位的系数 = 最新不复权 ÷ 最新后复权")
    ok(list(res["series"]) == ["A"], "只给要画图的股票带整段收盘")
    ok(res["meta"]["layers"] == ["baseline", "system"] and "两信号" in res["meta"]["system_note"],
       "存档里写明只测了两层,系统算法是两信号版")


def check_drop_signal():
    print("== 关掉一个信号(--drop-signals)")
    hol = ht.holidays_from(CAL)
    pairs = [("A", "B")]
    t = next(i for i in range(300, 1000) if ht.signals_at("A", i, CAL, PX, INDEX, pairs)["values"]["peer"] != 0)
    on = ht.forecast_at("A", t, CAL, PX, INDEX, pairs, hol, n_paths=1500, seed=9)
    off = ht.forecast_at("A", t, CAL, PX, INDEX, pairs, hol, n_paths=1500, seed=9, drop=("peer",))
    ok(on["layers"]["system"]["h"] != on["layers"]["baseline"]["h"],
       "反向自测:同行信号开着时系统层和底线不同 → 下面那条相等不是碰巧")
    ok(off["layers"]["system"]["signals"]["peer"] == 0 and off["layers"]["system"]["tilt"] == 0,
       "关掉后同行信号记 0、方向偏移 0(大盘信号权重本来就是 0)")
    ok(off["layers"]["system"]["h"] == off["layers"]["baseline"]["h"],
       "关掉同行信号后,系统层和底线逐位相同")
    sig = ht.signals_at("A", t, CAL, PX, INDEX, pairs, drop=("regime",))
    ok("regime" in sig["missing"] and sig["values"]["regime"] == 0
       and sig["values"]["peer"] == ht.signals_at("A", t, CAL, PX, INDEX, pairs)["values"]["peer"],
       "关掉的信号记 0、写进 missing,别的信号不受影响")
    try:
        ht.check_drop(["regim"])
        ok(False, "拼错的信号名要报错")
    except ValueError:
        ok(True, "拼错的信号名直接报错,不会悄悄什么都没关")

def check_nonoverlap():
    print("== 不重叠窗口数")
    ok(ht.nonoverlap([0, 21, 42, 63, 84, 126], 63) == 3, "间隔够 63 天的才另算一次:0、63、126")
    ok(ht.nonoverlap([], 63) == 0, "没有题 → 0")


def main():
    check_month_ends()
    check_signals()
    check_no_peek()
    check_outcome()
    check_score()
    check_band_and_coverage()
    check_drop_signal()
    check_nonoverlap()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
