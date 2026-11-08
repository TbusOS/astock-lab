#!/usr/bin/env python3
"""check_board_rules —— rule_test.py(按规则买卖的历史补考)的离线检查,不联网。

查五件事:
1. 不偷看:改掉第 k 天以后的价格,第 k 天及以前每天的「该不该拿着」不变;
   一个偷看下一天收盘的版本,同一道检查必须抓到(反向自测)。
2. 已知答案:一路上涨时 T60 和一直拿着只差起点那一天;涨一倍再腰斩,DD30 最多亏约三成;
   价格不动时买一次卖一次,资金正好是 (1 − 0.1%)²。
3. 卖了以后要等 T60I 先跌破再站上才买回,不会卖完第二天又买回去。
4. DIP50:一次长跌只算一次事件,回到 −50% 以上再跌下去才算第二次;往后的涨跌从第二天收盘算。
5. run() 接假取数跑通,主池 / 对照组分开汇总,两段都有。
"""
from __future__ import annotations

import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401  先于 prices(market.py 会删代理变量)
import rule_test as rt  # noqa: E402

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


CAL = _weekdays(date(2016, 6, 1), 2000)
UP_INDEX = {d: 1000.0 + i for i, d in enumerate(CAL)}            # 一路涨:指数永远在 60 日线上


def _view(px, index=UP_INDEX):
    return rt.make_view(px, index)


def check_no_peek():
    print("== 不偷看:改掉第 k 天以后的价格,之前的信号不变")
    idx = _walk(CAL, 2000.0, 0.012, 1)
    px = _walk(CAL, 100.0, 0.03, 2)
    a0 = rt.make_view(px, idx)
    # 选一个第 k+1 天原本站在 60 日线上的 k:以后的价格乘 0.3,偷看一天的版本在第 k 天一定会变
    k = next(j for j in range(1200, 1900) if rt.above(a0["closes"], a0["ma"], j + 1))
    px2 = {d: (v * 0.3 if i > k else v) for i, (d, v) in enumerate(sorted(px.items()))}
    idx2 = {d: (v * 0.3 if i > k else v) for i, (d, v) in enumerate(sorted(idx.items()))}
    a, b = a0, rt.make_view(px2, idx2)
    start = rt.start_index(a, "2017-01-01")
    for r in ("T60", "T60I", "DD30", "BEAR"):
        wa = rt.simulate(a, start, rt.RULE_FN[r], rt.STARTS_HOLDING[r])["wants"][:k - start + 1]
        wb = rt.simulate(b, start, rt.RULE_FN[r], rt.STARTS_HOLDING[r])["wants"][:k - start + 1]
        ok(wa == wb and len(wa) == k - start + 1, f"{r}:第 {k} 天及以前每天的回答逐个相同")

    def leaky(v, i, st):                   # 偷看下一天收盘的 T60
        j = min(i + 1, len(v["closes"]) - 1)
        return rt.above(v["closes"], v["ma"], j)
    la = rt.simulate(a, start, leaky, False)["wants"][:k - start + 1]
    lb = rt.simulate(b, start, leaky, False)["wants"][:k - start + 1]
    ok(la != lb, "反向自测:偷看下一天的版本,同一道检查会发现回答变了")


def check_known_answers():
    print("== 已知答案")
    up = {d: 100.0 * 1.001 ** i for i, d in enumerate(CAL)}
    v = _view(up)
    s = rt.start_index(v, "2017-01-01")
    h = rt.simulate(v, s, rt.rule_h, True)
    t = rt.simulate(v, s, rt.rule_t60, False)
    c = v["closes"]
    ok(len(t["trades"]) == 1 and t["trades"][0] == s + 1, "一路涨:T60 只在起点第二天买一次,之后一直拿着")
    ok(abs(t["eq"][-1] / h["eq"][-1] - c[s] / c[s + 1]) < 1e-12, "一路涨:T60 和一直拿着只差起点那一天的涨幅")

    rise, fall = 400, 200                                  # 100 → 200 → 100
    path = [100.0 + 100.0 * i / rise for i in range(rise)] + [200.0 - 100.0 * i / fall for i in range(fall + 1)]
    path = [100.0] * 200 + path
    cal = CAL[:len(path)]
    v2 = _view(dict(zip(cal, path)))
    s2 = rt.start_index(v2, cal[0])
    dd = rt.simulate(v2, s2, rt.rule_dd30, True)
    st = rt.curve_stats(cal[s2:], dd["eq"])
    hh = rt.curve_stats(cal[s2:], rt.simulate(v2, s2, rt.rule_h, True)["eq"])
    step = 100.0 / fall / 200.0
    ok(0.30 <= st["mdd"] <= 0.30 + step + 0.0011, f"涨一倍再腰斩:DD30 最大回撤约三成({st['mdd']:.4f})")
    ok(abs(hh["mdd"] - 0.5) < 1e-9, f"同一段一直拿着回撤一半({hh['mdd']:.4f})")
    ok(len(dd["trades"]) == 2, "DD30:起点买一次、回撤三成卖一次,之后一直跌、不买回")

    flat = _view({d: 50.0 for d in CAL})
    s3 = rt.start_index(flat, "2017-01-01")
    plan = {s3: True, s3 + 10: False}
    seq = {"on": False}

    def scripted(v, i, st):
        seq["on"] = plan.get(i, seq["on"])
        return seq["on"]
    sc = rt.simulate(flat, s3, scripted, False)
    ok(abs(sc["eq"][-1] - (1 - rt.COST) ** 2) < 1e-12 and len(sc["trades"]) == 2,
       "价格不动、买一次卖一次:资金 = (1 − 0.1%)²")
    d = rt.dca(flat, s3, len(CAL) - 1)
    ok(abs(d["multiple"] - (1 - rt.COST)) < 1e-12 and d["n"] > 50, "价格不动时定投:最终 = 投入 × (1 − 0.1%)")


def check_rebuy_needs_fresh_cross():
    print("== 卖了以后:T60I 先跌破过,再站上才买回")
    # 一路涨的股票,DD30 不会卖;造一个「回撤三成时还在 60 日线上」很难,直接用状态测 _rebuy
    v = _view({d: 100.0 * 1.001 ** i for i, d in enumerate(CAL)})
    i = 500
    ok(rt.both_up(v, i), "前提:这天本股和指数都在 60 日线上")
    ok(not rt._rebuy(v, i, {"hold": False, "armed": False}), "刚卖完、条件还没跌破过 → 不买回")
    ok(rt._rebuy(v, i, {"hold": False, "armed": True}), "跌破过一次以后再站上 → 买回")
    # 模拟里:卖出那天如果 T60I 还亮着,armed 不会被置上
    plan = {600: False}
    flag = {"on": True}

    def sell_once(vv, k, st):
        flag["on"] = plan.get(k, flag["on"]) if st["hold"] else rt._rebuy(vv, k, st)
        return flag["on"]
    s = rt.start_index(v, "2017-01-01")
    sim = rt.simulate(v, s, sell_once, True)
    ok(len(sim["trades"]) == 2, "一路涨时被卖掉,条件一直亮着、没跌破过 → 之后不再买回")


def check_dip():
    print("== DIP50 事件")
    one = [100.0] * 300 + [100.0 - 0.5 * i for i in range(150)] + [25.0] * 100
    two = one + [25.0 + i for i in range(60)] + [85.0 - 0.6 * i for i in range(100)]
    for path, want, label in ((one, 1, "一次长跌(跌到 −75%)只算一次"), (two, 2, "回到 −50% 以上又跌下去 → 第二次")):
        cal = CAL[:len(path)]
        v = _view(dict(zip(cal, path)))
        ok(len(rt.dip_events(v, cal[0])) == want, label + f":{len(rt.dip_events(v, cal[0]))}")
    wobble = [100.0] * 300 + [49.0, 51.0] * 20 + [49.0] * 50
    cal = CAL[:len(wobble)]
    n = len(rt.dip_events(_view(dict(zip(cal, wobble))), cal[0]))
    ok(n == 1, f"在 −50% 附近来回穿,63 天内只算一次:{n}")
    cal = CAL[:len(one)]
    v = _view(dict(zip(cal, one)))
    i = rt.dip_events(v, cal[0])[0]
    f = rt.forward(v, i, 63)
    c = v["closes"]
    ok(abs(f["ret"] - (c[i + 64] / c[i + 1] - 1)) < 1e-12, "往后的涨跌从信号第二天收盘算起")
    ok(f["further"] <= 0 and abs(f["further"] - (min(c[i + 2:i + 65]) / c[i + 1] - 1)) < 1e-12,
       "途中再跌 = 之后最低 ÷ 买入价 − 1")
    lo = min(range(i + 2, i + 65), key=c.__getitem__)
    ok(f["days_to_low"] == lo - (i + 1), f"最低那天离买入几个交易日:{f['days_to_low']}")
    rising = _view(dict(zip(CAL[:400], [100.0 + k for k in range(400)])))
    g = rt.forward(rising, 300, 63)
    ok(g["further"] == 0 and g["days_to_low"] == 0, "买了以后一直没再跌:再跌 0、到最低 0 天")
    ok(rt.forward(v, len(c) - 10, 63) is None, "没走够天数的不算")


class FakeFetch:
    def __init__(self, index_px, px):
        self.index_px, self.px = index_px, px

    def index(self, start):
        return {d: v for d, v in self.index_px.items() if d >= start}

    def hfq(self, code, start):
        if code not in self.px:
            raise ValueError(f"取不到 {code}")
        return {d: v for d, v in self.px[code].items() if d >= start}


def check_run():
    print("== run():主池、对照组分开,两段都有")
    idx = _walk(CAL, 2000.0, 0.012, 1)
    px = {c: _walk(CAL, 50.0, 0.03, 10 + k) for k, c in enumerate(("A", "B", "C"))}
    res = rt.run(["A", "B"], ["C", "X"], begin="2017-01-01", data_start=CAL[0], fetch=FakeFetch(idx, px))
    g = res["groups"]
    ok(set(g["main"]["stocks"]) == {"A", "B"} and set(g["control"]["stocks"]) == {"C"}, "两组各算各的")
    ok("X" in res["meta"]["failed"], "取不到的股票写进 failed,不顶替")
    s = g["main"]["summary"]
    ok(all(set(s[r]) == {"all", "early", "late"} for r in rt.RULES), "每条规则都有全段 / 2023 以前 / 2023 起")
    ok(s["H"]["late"]["median"]["cagr_vs_h"] == 0, "一直拿着比自己,差 0")
    ok("curves" not in g["main"]["stocks"]["A"], "存档里不带每天的资金曲线(太大)")
    ok(set(g["main"]["dip50"]) >= {"events", "63", "126", "250"}, "DIP50 有事件列表和三个期限")
    ok(g["main"]["stocks"]["A"]["bars_by_year"]["2017"] >= 250, "每只股票记下每年几根日线")
    holes = [d for i, d in enumerate(CAL) if not ("2018-03-01" <= d <= "2018-12-31")]
    thin = rt.thin_years(holes, CAL[-1])
    ok(thin == ["2018"], f"反向自测:挖掉 2018 年大半年,会被标成日线不全:{thin}")


def main():
    check_no_peek()
    check_known_answers()
    check_rebuy_needs_fresh_cross()
    check_dip()
    check_run()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
