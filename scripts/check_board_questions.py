#!/usr/bin/env python3
"""check_board_questions —— 预测看板「题目与结算」(skills/forecast-board/scripts/questions.py)的检查。

用法:
    python3 scripts/check_board_questions.py        # 退出码 0 才算过

不联网、不碰真实数据:日历、后复权收盘、指数收盘都是手造的;
统计底线、系统算法那一组用合成收盘和固定种子现算(路径数取小,几秒跑完)。

结构:每块一个函数,登记进 CHECKS(和 check_board_engine.py 同一种写法)。
"""
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FB_SCRIPTS = ROOT / "skills" / "forecast-board" / "scripts"
# 按本仓路径找模块,不走 tools/ 软链或已安装的副本
sys.path.insert(0, str(FB_SCRIPTS))

import fb_common  # noqa: E402,F401  先于 prices(market.py 会删代理变量,见 fb_common.ORIG_ENV)
import baseline  # noqa: E402
import prices  # noqa: E402
import questions as Q  # noqa: E402
import system_forecast  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:400]))
    if not cond:
        FAILS += 1


def raises(fn, exc):
    """跑 fn,返回抛出的异常(类型对得上才算);没抛返回 None。"""
    try:
        fn()
    except exc as e:
        return e
    return None


# ── 合成数据 ────────────────────────────────────────────────────────────────
CATS = {"bottom_months": ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05"],
        "buy_months": ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05",
                       "2027-06", "2027-07", "2027-08", "2027-09", "2027-10", "2027-11", "later"],
        "price_bins": [[0.0, 0.5], [0.5, 0.6], [0.6, 0.7], [0.7, 0.8], [0.8, 0.9], [0.9, 1.0]]}
META = {"run": "2026-11-02/153000", "as_of": "2026-11-02", "start_close": {"300308": 100.0, "300502": 50.0},
        "p0_hfq": {"300308": 500.0, "300502": 80.0}, "categories": CATS}
D = [f"d{i:03d}" for i in range(300)]          # 不带真日期的日历:d000 是 as_of


def _stock(p0: float) -> dict:
    """一只股票的答卷(合成数字,价位用 p0 那种价)。"""
    hz = {h: {"p_up": p, "q": {k: round(p0 * m, 2) for k, m in zip(("10", "25", "50", "75", "90"), ms)}}
          for h, p, ms in (("63", 45, (0.7, 0.85, 0.98, 1.12, 1.3)),
                           ("126", 48, (0.6, 0.8, 1.0, 1.2, 1.45)),
                           ("250", 52, (0.5, 0.75, 1.05, 1.4, 1.8)))}
    return {"horizons": hz,
            "bottom": {"month_probs": dict(zip(CATS["bottom_months"], [30, 25, 15, 10, 8, 7, 5])),
                       "price_bins": [[lo, hi, p] for (lo, hi), p in zip(CATS["price_bins"], [5, 10, 20, 30, 25, 10])]},
            "buy": {"month_probs": dict(zip(CATS["buy_months"], [5, 10, 10, 10, 10, 10, 10, 5, 5, 5, 5, 5, 5, 5])),
                    "zones": [[round(p0 * 0.9, 2), 60], [round(p0 * 0.8, 2), 35], [round(p0 * 0.7, 2), 15]],
                    "conditions": ["创业板指收盘回到 60 日线上"]},
            "action": {"kind": "hold"}, "evidence": {"support": [], "against": [], "unverified": []}}


def _answer(author="claude") -> dict:
    return {"author": author, "model": "m", "as_of": "2026-11-02",
            "stocks": {"300308": _stock(100.0), "300502": _stock(50.0)}}


def _cal_from(as_of: date, n: int) -> list:
    """as_of 加上往后 n 个交易日(去周末)。"""
    later = prices.future_trading_days(as_of + timedelta(days=1), n, set())
    return [as_of.isoformat()] + [d.isoformat() for d in later]


def _by(qs, qtype, **params):
    """按题型和参数挑出一道题。"""
    hit = [q for q in qs if q["type"] == qtype and all(q["params"].get(k) == v for k, v in params.items())]
    assert len(hit) == 1, (qtype, params, len(hit))
    return hit[0]


def _flat(cal=D, v=100.0):
    return {d: v for d in cal}


# ── brief 里的用例 ──────────────────────────────────────────────────────────
def check_brief():
    print("== brief 写定的用例")
    hfq = {**_flat(), "d063": 110.0}
    q = {"type": "up", "opened": "d000", "p0_hfq": 100.0, "params": {"n": 63}}
    r = Q.resolve(q, D, hfq)
    ok("63 天后涨了 → 1", r["outcome"] == 1 and r["end_day"] == "d063" and abs(r["value"] - 1.1) < 1e-9, r)
    ok("返回六个键(含 scorable_after)",
       sorted(r) == ["end_day", "note", "outcome", "resolved_at", "scorable_after", "value"], r)
    ok("没到期 → None", Q.resolve({**q, "params": {"n": 400}}, D, hfq) is None)
    del hfq["d063"]
    hfq["d062"] = 90.0
    r = Q.resolve(q, D, hfq)
    ok("停牌顺延:期末没日线、后面有日线 → 用前一天收盘", r["outcome"] == 0
       and "停牌顺延,用 d062 收盘" in r["note"] and r["end_day"] == "d063", r)
    ok("持平算不涨(比值正好 1.0 → 0)", Q.resolve(q, D, _flat())["outcome"] == 0)

    dcal = _cal_from(date(2026, 11, 3), 130)
    dh = {d: 100.0 + i * 0.01 for i, d in enumerate(dcal)}
    dec = [d for d in dcal if d.startswith("2026-12")][5]
    dh[dec] = 70.0
    qb = {"type": "bottom_month", "opened": "2026-11-03", "p0_hfq": 100.0, "params": {"window": 126}}
    rb = Q.resolve(qb, dcal, dh)
    ok("见底月份:2026-11-03 起的日历,12 月最低 → '2026-12'", rb["outcome"] == "2026-12"
       and abs(rb["value"] - 0.7) < 1e-9 and dec in rb["note"] and rb["scorable_after"] == dcal[126], rb)
    ok("见底月份:日历没走到第 126 个交易日 → None", Q.resolve(qb, dcal[:100], dh) is None)

    th = {**_flat(), "d030": 121.0}
    qt = {"type": "touch", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 1.2, "end": "d063"}}
    rt = Q.resolve(qt, D, th)
    ok("碰价:第 30 天比值 1.21 ≥ 1.2 → 1,当天就结,计分日仍是截止日 d063", rt["outcome"] == 1
       and rt["end_day"] == "d030" and rt["scorable_after"] == "d063" and "提前结算" in rt["note"], rt)
    ok("碰价:日历只到碰到那天也能结(结果不会再变)", Q.resolve(qt, D[:31], th)["scorable_after"] == "d063")


# ── 本股日线还没走到期末:别当停牌 ─────────────────────────────────────────
def check_lagging_data():
    print("== 本股日线还没走到期末:再等,等太久才按停牌结")
    lag = {d: 100.0 for d in D[:51]}             # 本股日线只到 d050
    q = {"type": "up", "opened": "d000", "p0_hfq": 100.0, "params": {"n": 63}}
    ok("日线只到 d050、日历刚过期末 → None(可能只是数据慢了)", Q.resolve(q, D[:80], lag) is None)
    ok("日历走过期末 39 个交易日 → 还等", Q.resolve(q, D[:103], lag) is None)
    r = Q.resolve(q, D[:104], lag)
    ok("走过期末 40 个交易日 → 按停牌结,两句说明都有", r is not None and "按停牌结算" in r["note"]
       and "停牌顺延,用 d050 收盘" in r["note"], r)
    ok("max_wait_days 可以改", Q.resolve(q, D[:80], lag, max_wait_days=10) is not None)
    qb = {"type": "bottom_month", "opened": "d000", "p0_hfq": 100.0, "params": {"window": 126}}
    lag2 = {d: 100.0 for d in D[:121]}
    ok("见底:日线没走到第 126 天 → None", Q.resolve(qb, D[:140], lag2) is None)
    ok("见底:日线走到第 126 天 → 结", Q.resolve(qb, D[:140], {d: 100.0 for d in D[:127]}) is not None)
    qt = {"type": "touch", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 0.9, "n": 63}}
    ok("碰价没碰到:日线没走到窗口末尾 → None", Q.resolve(qt, D[:80], lag) is None)
    ok("碰价已经碰到:日线后面没来也照结", Q.resolve(qt, D[:80], {**lag, "d020": 85.0})["outcome"] == 1)
    qa = {"type": "above_on", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 1.05, "day": "d063"}}
    ok("某天高于:日线没走到那天 → None", Q.resolve(qa, D[:80], lag) is None)


# ── 各题型 ──────────────────────────────────────────────────────────────────
def check_types():
    print("== 各题型的结算")
    base = _flat()
    qi = {"type": "interval", "opened": "d000", "p0_hfq": 100.0, "params": {"n": 63, "lo": "25", "hi": "75"},
          "forecast": {"lo": 0.9, "hi": 1.1, "pct": 50}}
    for px, want in ((100.0, 1), (90.0, 1), (110.0, 1), (89.9, 0), (110.1, 0)):
        r = Q.resolve(qi, D, {**base, "d063": px})
        ok(f"区间 [0.9, 1.1]:期末比值 {px / 100} → {want}", r["outcome"] == want and r["scorable_after"] == "d063", r)
    ok("区间:没到期 → None", Q.resolve(qi, D[:60], base) is None)

    qa = {"type": "above_on", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 1.05, "day": "d063"}}
    ra = Q.resolve(qa, D, {**base, "d063": 110.0})
    ok("某天高于:比值 1.10 > 1.05 → 1,计分日是那一天", ra["outcome"] == 1 and ra["scorable_after"] == "d063", ra)
    ok("某天高于:正好 1.05 → 0(要严格高于)", Q.resolve(qa, D, {**base, "d063": 105.0})["outcome"] == 0)
    ok("某天高于:日历没到那天 → None", Q.resolve(qa, D[:63], base) is None)
    sus = {d: v for d, v in {**base, "d062": 120.0}.items() if d != "d063"}
    ok("某天高于:那天停牌 → 用前一天收盘", "停牌顺延,用 d062 收盘" in Q.resolve(qa, D, sus)["note"])
    dcal = _cal_from(date(2026, 10, 26), 20)               # 2026-10-31 是周六
    qs_ = {"type": "above_on", "opened": "2026-10-26", "p0_hfq": 100.0, "params": {"ratio": 1.05, "day": "2026-10-31"}}
    rs = Q.resolve(qs_, dcal, {**_flat(dcal), "2026-10-30": 110.0})
    ok("某天高于:那天不开市 → 看 10-30;计分日写原来那天 10-31", rs["outcome"] == 1
       and rs["end_day"] == "2026-10-30" and rs["scorable_after"] == "2026-10-31" and "不开市" in rs["note"], rs)

    qm = {"type": "multiple_by", "opened": "d000", "p0_hfq": 100.0, "params": {"k": 2.0, "end": "d100"}}
    rm1 = Q.resolve(qm, D, {**base, "d050": 201.0})
    ok("倍数:第 50 天 2.01 倍 → 1,计分日仍是 d100", rm1["outcome"] == 1 and rm1["scorable_after"] == "d100", rm1)
    rm = Q.resolve(qm, D, {**base, "d050": 199.0})
    ok("倍数:最高 1.99 倍、到了截止日 → 0", rm["outcome"] == 0 and abs(rm["value"] - 1.99) < 1e-9, rm)
    ok("倍数:没到截止日、也还没到 → None", Q.resolve(qm, D[:80], base) is None)

    qt = {"type": "touch", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 1.2, "end": "d063"}}
    rt0 = Q.resolve(qt, D, {**base, "d030": 119.0})
    ok("往上碰:没碰到、到截止日 → 0,value 是期间最高", rt0["outcome"] == 0 and abs(rt0["value"] - 1.19) < 1e-9, rt0)
    ok("往上碰:正好等于阈值算碰到", Q.resolve(qt, D, {**base, "d020": 120.0})["outcome"] == 1)
    qd = {"type": "touch", "opened": "d000", "p0_hfq": 100.0, "params": {"ratio": 0.9, "n": 63}}
    ok("比值 < 1 没写方向 → 往下碰", Q.resolve(qd, D, {**base, "d040": 89.0})["outcome"] == 1)
    rd0 = Q.resolve(qd, D, {**base, "d040": 91.0})
    ok("往下碰:一直没跌到 → 0,value 是期间最低,计分日第 63 天", rd0["outcome"] == 0
       and abs(rd0["value"] - 0.91) < 1e-9 and rd0["scorable_after"] == "d063", rd0)
    ok("往下碰:第 64 天才跌到不算", Q.resolve(qd, D, {**base, "d064": 50.0})["outcome"] == 0)
    ok("往下碰:第 63 天跌到算", Q.resolve(qd, D, {**base, "d063": 89.0})["outcome"] == 1)
    ok("往下碰:没碰到、窗口没走完 → None(不去估计分日)", Q.resolve(qd, D[:40], base) is None)
    zcal = _cal_from(date(2026, 11, 2), 70)
    rz = Q.resolve({**qd, "opened": "2026-11-02"}, zcal[:21], {**_flat(zcal), zcal[20]: 85.0})
    ok("往下碰:第 20 天碰到、日历只到第 20 天 → 结,计分日按节假日表估成第 63 天",
       rz["outcome"] == 1 and rz["scorable_after"] == zcal[63] and "估" in rz["note"], rz)

    bins = CATS["price_bins"]
    qp = {"type": "bottom_price_bin", "opened": "d000", "p0_hfq": 100.0, "params": {"window": 126, "bins": bins}}
    ok("最低价分段:最低 0.6 → '0.6-0.7'", Q.resolve(qp, D, {**base, "d100": 60.0})["outcome"] == "0.6-0.7")
    ok("最低价分段:最低 0.45 → '0.0-0.5'", Q.resolve(qp, D, {**base, "d100": 45.0})["outcome"] == "0.0-0.5")
    rp = Q.resolve(qp, D, {d: 100.0 + i for i, d in enumerate(D)})
    ok("最低价分段:起点就是最低(比值 1.0)→ '0.9-1.0'", rp["outcome"] == "0.9-1.0" and rp["value"] == 1.0, rp)
    ok("最低价分段:第 127 天更低不算", Q.resolve(qp, D, {**base, "d127": 10.0})["outcome"] == "0.9-1.0")
    fl = Q.resolve({**qp, "p0_hfq": 2.9}, D, {**_flat(D, 2.9), "d000": 2.9, "d100": 2.32})
    ok("比值先取 9 位小数:2.32 ÷ 2.9 = 0.7999999999999999 算 0.8 → '0.8-0.9'", fl["outcome"] == "0.8-0.9", fl)

    rr = Q.resolve({"type": "up", "opened": "d000", "p0_hfq": 50.0, "params": {"n": 63}}, D, base)
    ok("起点后复权价变了:按这次取到的比(不涨),note 写明", rr["outcome"] == 0 and "存档时不同" in rr["note"], rr)
    rn = Q.resolve({"type": "up", "opened": "d000", "p0_hfq": 50.0, "params": {"n": 63}}, D,
                   {d: v for d, v in base.items() if d != "d000"})
    ok("这次没取到起点那天:用存档的 p0_hfq(100 ÷ 50 → 涨),note 写明", rn["outcome"] == 1
       and "用存档的 p0_hfq" in rn["note"], rn)
    ok("不认识的题型报错", raises(lambda: Q.resolve({"type": "x", "opened": "d000", "p0_hfq": 1.0, "params": {}},
                                                       D, base), ValueError) is not None)
    ok("index_closes、signal_day 只能按名字传", raises(lambda: Q.resolve(qd, D, base, {}), TypeError) is not None)


# ── 见底的日子和统计底线一致 ───────────────────────────────────────────────
def check_r15():
    print("== 见底的日子怎么对:和 baseline.bottom_block 同一条路径分到同一个月、同一段")
    as_of = date(2026, 10, 30)          # 周五;as_of 在 10 月,第 1 个交易日 11-02 在 11 月
    start = date(2026, 11, 2)
    cal = _cal_from(as_of, 126)
    fut = cal[1:]
    cats = baseline.categories(start, set())
    ok("手造日历的第 1~126 天 == future_trading_days(start, 126)",
       fut == [d.isoformat() for d in prices.future_trading_days(start, 126, set())])

    def both(path, name):
        hb = baseline.bottom_block(np.array([path]), 100.0, start, set(), 126)
        b_month = [m for m, p in hb["month_probs"].items() if p == 100]
        b_bin = [Q.bin_key(lo, hi) for lo, hi, p in hb["price_bins"] if p == 100]
        hfq = {"2026-10-30": 100.0, **dict(zip(fut, path))}
        qm = {"type": "bottom_month", "opened": as_of.isoformat(), "p0_hfq": 100.0, "params": {"window": 126}}
        qp = {"type": "bottom_price_bin", "opened": as_of.isoformat(), "p0_hfq": 100.0,
              "params": {"window": 126, "bins": cats["price_bins"]}}
        rm, rp = Q.resolve(qm, cal, hfq), Q.resolve(qp, cal, hfq)
        ok(f"见底 {name}:月份 {rm['outcome']} == 底线的 {b_month},分段 {rp['outcome']} == {b_bin}",
           [rm["outcome"]] == b_month and [rp["outcome"]] == b_bin and rm["outcome"] in cats["bottom_months"],
           (rm, rp, hb))
        return rm

    up = [100.0 + 0.5 * (k + 1) for k in range(126)]
    r = both(up, "起点最低")
    ok("起点最低算第一个预测月 2026-11,不是 as_of 所在的 2026-10", r["outcome"] == "2026-11", r)
    dec = up[:]
    dec[fut.index("2026-12-15")] = 60.0
    both(dec, "12-15 最低、正好 0.6")
    tie = up[:]
    tie[fut.index("2026-11-20")] = 80.0
    tie[fut.index("2027-01-20")] = 80.0
    ok("两天一样低取早的那天(11 月)", both(tie, "两天一样低")["outcome"] == "2026-11")
    same = up[:]
    same[fut.index("2027-02-01")] = 100.0
    r = both(same, "后面有一天正好等于起点")
    ok("后面有一天正好等于起点:仍算起点最低", r["outcome"] == "2026-11" and r["value"] == 1.0, r)
    last = up[:]
    last[-1] = 50.0
    both(last, "第 126 天最低")
    rng = random.Random(11)
    for i in range(5):
        px, path = 100.0, []
        for _ in range(126):
            px *= math.exp(rng.gauss(0, 0.03))
            path.append(px)
        both(path, f"随机路径 {i}")


# ── 买点;信号日买 vs 当天买 ────────────────────────────────────────────────
def _buy_world():
    days = [d.isoformat() for d in prices.future_trading_days(date(2026, 6, 1), 420, set())]
    a = 100                                     # as_of = days[100],前面 100 个交易日够算 60 日线
    idx = {d: (110.0 if i >= a + 21 else 100.0) for i, d in enumerate(days)}        # 第 21 天起在线上
    st = {d: 50.0 for d in days}
    for k in range(5, 11):
        st[days[a + k]] = 60.0                  # 第 5~10 天本股在线上,但指数不在
    for k in range(31, 420 - a):
        st[days[a + k]] = 55.0                  # 第 31 天起本股回到线上
    return days, a, idx, st


def check_buy():
    print("== 买点:创业板指和本股同一天收盘都在各自 60 日线上")
    days, a, idx, st = _buy_world()
    as_of, sig = days[a], days[a + 31]
    qb = {"type": "buy_signal_month", "opened": as_of, "p0_hfq": 50.0, "params": {"horizon": 250}}
    r = Q.resolve(qb, days, st, index_closes=idx)
    ok("第 21 天指数在线上、第 5~10 天本股在线上,第一次同一天都在 = 第 31 天;计分日第 250 天",
       r["outcome"] == sig[:7] and r["end_day"] == sig and r["value"] == 31.0
       and r["scorable_after"] == days[a + 250] and "提前结算" in r["note"], r)
    re = Q.resolve(qb, days[:a + 40], st, index_closes=idx)
    ok("日历只到第 40 天:照样结,第 250 天按节假日表估(这份日历没节假日,估的就是真的那天)",
       re["scorable_after"] == days[a + 250] and "估" in re["note"], re)
    ok("buy_signal_day 给出同一天", Q.buy_signal_day(days, as_of, st, idx) == sig)
    ok("日历只到第 25 天、还没出现 → None", Q.resolve(qb, days[:a + 26], st, index_closes=idx) is None)
    ok("没给 index_closes 报错", raises(lambda: Q.resolve(qb, days, st), ValueError) is not None)
    short = {d: v for d, v in idx.items() if d >= days[a - 30]}
    ok("指数历史不够 60 根报错,不悄悄当成不在线上",
       raises(lambda: Q.resolve(qb, days, st, index_closes=short), ValueError) is not None)
    idx31 = {d: (110.0 if i >= a + 31 else 100.0) for i, d in enumerate(days)}
    st30 = {**st, days[a + 30]: 55.0}
    st_sus = {d: v for d, v in st30.items() if d != sig}
    # 本股第 30 天起在线上、指数第 31 天才上线;第 31 天停牌:买不进,那天不算,顺到第 32 天
    ok("那天本股停牌 → 不算那天,顺到下一个交易日(不拿前一天收盘顶)",
       Q.resolve(qb, days, st_sus, index_closes=idx31)["end_day"] == days[a + 32]
       and Q.resolve(qb, days, st30, index_closes=idx31)["end_day"] == sig)
    lag = {d: v for d, v in st.items() if d < days[a + 20]}
    ok("本股日线只到第 19 天、指数第 21 天起在线上 → None(不判成没有买点)",
       Q.resolve(qb, days[:a + 50], lag, index_closes=idx) is None)
    flat_lag = {d: 50.0 for d in days[:a + 231]}          # 本股一直不在线上,日线只到第 230 天
    ok("日线只到第 230 天、日历到第 260 天 → None(不判成 250 天没买点)",
       Q.resolve(qb, days[:a + 261], flat_lag, index_closes=idx) is None)
    rlag = Q.resolve(qb, days[:a + 291], flat_lag, index_closes=idx)
    ok("日历走过第 231 天 40 个交易日以上 → 当停牌,记 later,写出停了几天",
       rlag["outcome"] == "later" and "期间 20 个交易日本股没有日线" in rlag["note"], rlag)
    early_i = {d: (110.0 if i >= a - 5 else 100.0) for i, d in enumerate(days)}
    early_s = {d: (55.0 if i >= a - 5 else 50.0) for i, d in enumerate(days)}
    r1 = Q.resolve(qb, days, early_s, index_closes=early_i)
    ok("as_of 当天条件就成立 → 不算 as_of,记第 1 个交易日", r1["end_day"] == days[a + 1] and r1["value"] == 1.0, r1)
    flat = {d: 50.0 for d in days}
    rl = Q.resolve(qb, days, flat, index_closes=idx)
    ok("本股一直不在线上(收盘 = 均线不算),250 天到了 → later", rl["outcome"] == "later"
       and rl["end_day"] == days[a + 250] == rl["scorable_after"] and "250 天内没出现买点信号" in rl["note"], rl)
    ok("还没到 250 天 → None", Q.resolve(qb, days[:a + 200], flat, index_closes=idx) is None)
    ok("直接给 signal_day 不用指数", Q.resolve(qb, days, st, signal_day=sig)["outcome"] == sig[:7])

    rq = Q.rule_questions(META)
    ok("buy_vs_now 每只股票一道、作者 rule、没有概率",
       [q["id"] for q in rq] == ["2026-11-02/153000/rule/300308/buy_vs_now/n63",
                                 "2026-11-02/153000/rule/300502/buy_vs_now/n63"]
       and all(q["forecast"] is None and q["params"] == {"n": 63, "horizon": 250} for q in rq), rq)
    qv = {**rq[0], "opened": as_of, "p0_hfq": 50.0}
    st2 = {**st, days[a + 94]: 66.0}            # 信号日 55 → 66:+20%;as_of 50 → 第 63 天 55:+10%
    rv = Q.resolve(qv, days, st2, index_closes=idx)
    ok("信号日买赚 20%、当天买赚 10% → 1;计分日 = 信号日 + 63", rv["outcome"] == 1
       and rv["end_day"] == days[a + 94] == rv["scorable_after"] and abs(rv["value"] - 0.1) < 1e-9, rv)
    ok("信号日买只赚 5% → 0", Q.resolve(qv, days, {**st, days[a + 94]: 57.75}, index_closes=idx)["outcome"] == 0)
    ok("信号后还没满 63 天 → None", Q.resolve(qv, days[:a + 90], st2, index_closes=idx) is None)
    rvl = Q.resolve(qv, days, flat, index_closes=idx)
    ok("250 天没信号:outcome None、不计分,计分日第 250 天", rvl is not None and rvl["outcome"] is None
       and "250 天内没出现买点信号" in rvl["note"] and rvl["scorable_after"] == days[a + 250], rvl)
    ok("signal_day='later' 也一样", Q.resolve(qv, days, flat, signal_day="later")["outcome"] is None)


# ── 答卷 → 题目 ─────────────────────────────────────────────────────────────
def check_build():
    print("== 答卷拆成题目")
    qs = Q.questions_from_answer(_answer(), META)
    ok("每只 15 道:3 涨跌 + 6 区间 + 见底月份 + 见底分段 + 买点月份 + 3 个买入价位", len(qs) == 30, len(qs))
    ids = [q["id"] for q in qs]
    ok("题号不重复", len(set(ids)) == len(ids))
    ok("同一份答卷再拆一次,题目一模一样", Q.questions_from_answer(_answer(), META) == qs)
    ok("题号格式 <run>/<author>/<code>/<type>/<参数>",
       {"2026-11-02/153000/claude/300308/interval/n63-q25-q75", "2026-11-02/153000/claude/300502/up/n250",
        "2026-11-02/153000/claude/300308/bottom_month/w126",
        "2026-11-02/153000/claude/300308/touch/down-r0.9-n63"} <= set(ids), ids)
    want = ["author", "code", "forecast", "id", "opened", "p0_hfq", "params", "run", "type"]
    ok("题目九个键", all(sorted(q) == want for q in qs), sorted(qs[0]))
    q8 = [q for q in qs if q["code"] == "300308"]
    up = _by(q8, "up", n=63)
    ok("涨跌题:forecast 只有 pct(0~100 整数);opened = as_of;p0_hfq 取 run_meta",
       up["forecast"] == {"pct": 45} and up["opened"] == "2026-11-02" and up["p0_hfq"] == 500.0, up)
    ok("没有 buy_vs_now(它归 rule_questions)", not any(q["type"] == "buy_vs_now" for q in qs))

    iv = _by(q8, "interval", n=63, lo="25", hi="75")
    ok("区间:85 ÷ start_close 100 = 0.85,不是 ÷ 后复权 500", iv["forecast"] == {"lo": 0.85, "hi": 1.12, "pct": 50}, iv)
    ok("10~90 区间 pct = 80", _by(q8, "interval", n=63, lo="10", hi="90")["forecast"]["pct"] == 80)
    z = _by(q8, "touch", ratio=0.9)
    ok("买入价位 90 → 往下碰 0.9、63 个交易日内,概率 60",
       z["params"] == {"ratio": 0.9, "dir": "down", "n": 63} and z["forecast"] == {"pct": 60}, z)
    q5 = [q for q in qs if q["code"] == "300502"]
    ok("另一只按自己的 start_close 50 换算", _by(q5, "touch", ratio=0.9)["params"]["ratio"] == 0.9
       and _by(q5, "interval", n=63, lo="25", hi="75")["forecast"]["lo"] == 0.85)
    cal = _cal_from(date(2026, 11, 2), 130)
    hfq = {**_flat(cal, 500.0), cal[63]: 475.0, cal[40]: 449.0}     # 后复权 500 → 475:0.95;449:0.898
    ok("结算:后复权比值 0.95 落在 [0.85, 1.12] → 1;不涨 → 0;0.898 碰到 0.9 → 1",
       Q.resolve(iv, cal, hfq)["outcome"] == 1 and Q.resolve(up, cal, hfq)["outcome"] == 0
       and Q.resolve(z, cal, hfq)["outcome"] == 1)

    bogus = _answer()
    bogus["stocks"]["300308"]["start_price"] = 500.0          # AI 从参考层抄来的后复权起点
    bq = [q for q in Q.questions_from_answer(bogus, META) if q["code"] == "300308"]
    ok("AI 答卷带了 start_price 也不用,照样 ÷ start_close", _by(bq, "interval", n=63, lo="25", hi="75")
       ["forecast"]["lo"] == 0.85, bq[1])
    layer = {"layer": "baseline", "start_price": 500.0, **_stock(500.0)}
    lq = Q.questions_from_answer({"author": "baseline", "stocks": {"300308": layer}}, META)
    ok("统计底线那一块 ÷ 自己的 start_price:425 ÷ 500 = 0.85",
       _by(lq, "interval", n=63, lo="25", hi="75")["forecast"]["lo"] == 0.85, lq[:4])
    nosp = {k: v for k, v in layer.items() if k != "start_price"}
    ok("统计底线那一块缺 start_price 报错(不拿 start_close 顶)", raises(lambda: Q.questions_from_answer(
        {"author": "system", "stocks": {"300308": nosp}}, META), ValueError) is not None)
    cat = _by(lq, "bottom_month")
    ok("分类题 forecast = {类别: 整数},按 categories 的顺序",
       list(cat["forecast"]) == CATS["bottom_months"] and cat["forecast"]["2026-11"] == 30, cat)
    pb = _by(lq, "bottom_price_bin")
    ok("最低价分段的类别键 '0.0-0.5' … '0.9-1.0',分段写进 params",
       list(pb["forecast"]) == ["0.0-0.5", "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0"]
       and pb["params"]["bins"] == CATS["price_bins"], pb)

    hi = _answer()
    hi["stocks"]["300308"]["buy"]["zones"] = [[100.0, 90], [95.0, 70], [104.0, 95]]
    got = Q.build_questions(hi, META)
    sk = got["skipped"]
    ok("买入价位不低于起点价(比值 1.0、1.04)不出题,记进 skipped;0.95 照出",
       [s["ratio"] for s in sk] == [1.0, 1.04] and sk[0]["where"] == "claude.300308.buy.zones[0]"
       and [q["params"]["ratio"] for q in got["questions"] if q["code"] == "300308" and q["type"] == "touch"]
       == [0.95], sk)
    ok("questions_from_answer 只返回题目", Q.questions_from_answer(hi, META) == got["questions"])

    ok("作答失败的答卷 → 没有题", Q.questions_from_answer({"author": "codex", "failed": True, "errors": []}, META) == [])
    bad = _answer()
    mp = bad["stocks"]["300308"]["bottom"]["month_probs"]
    mp["2027-06"] = mp.pop("2027-05")           # 换成类别里没有的月份,加起来仍是 100
    ok("见底月份的类别和 categories 不一样报错", raises(lambda: Q.questions_from_answer(bad, META), ValueError) is not None)
    bad2 = _answer()
    bad2["stocks"]["300308"]["buy"]["month_probs"]["later"] = 6
    ok("分类概率加起来不是 100 报错", raises(lambda: Q.questions_from_answer(bad2, META), ValueError) is not None)
    ok("run_meta 缺某只的 p0_hfq 报错", raises(lambda: Q.questions_from_answer(
        _answer(), {**META, "p0_hfq": {"300308": 500.0}}), ValueError) is not None)
    ok("答卷的 as_of 和 run_meta 不一样报错", raises(lambda: Q.questions_from_answer(
        {**_answer(), "as_of": "2026-10-30"}, META), ValueError) is not None)
    nob = {"author": "baseline", "stocks": {"300308": {k: v for k, v in layer.items() if k != "buy"}}}
    ok("缺 buy 块报错", raises(lambda: Q.questions_from_answer(nob, META), ValueError) is not None)
    ok("作者名 rule 留给 buy_vs_now", raises(lambda: Q.questions_from_answer(_answer("rule"), META), ValueError)
       is not None)

    mq = Q.make_question("2026-07-15/000000", "claude", "300308", "above_on", "2026-07-15", 1234.5,
                         {"ratio": 0.6123456, "day": "2026-12-30"}, Q.binary_forecast(70))
    ok("加题:比值存 6 位小数,题号里是同一个数", mq["params"]["ratio"] == 0.612346
       and mq["id"] == "2026-07-15/000000/claude/300308/above_on/r0.612346-d2026-12-30"
       and mq["forecast"] == {"pct": 70}, mq)
    mt = Q.make_question("r", "codex", "300502", "touch", "2026-11-02", 80.0, {"ratio": 1.2345678, "end": "2027-03-31"},
                         Q.binary_forecast(30))
    ok("加题:touch 没写方向按比值补上;比值 ≥ 1 时题号和存的数也一样",
       mt["params"] == {"ratio": 1.234568, "end": "2027-03-31", "dir": "up"}
       and mt["id"].endswith("/touch/up-r1.234568-e2027-03-31"), mt)
    mk = Q.make_question("r", "claude", "300502", "multiple_by", "2026-11-02", 80.0, {"k": 2, "end": "2027-06-30"},
                         Q.binary_forecast(10))
    ok("加题:multiple_by 题号", mk["id"].endswith("/multiple_by/k2-e2027-06-30"), mk)
    ok("加题:比值不是正数报错", raises(lambda: Q.make_question("r", "c", "1", "touch", "2026-11-02", 1.0,
                                                            {"ratio": 0, "n": 5}, {"pct": 1}), ValueError) is not None)
    ok("加题:概率不是 0~100 整数报错", raises(lambda: Q.binary_forecast(101), ValueError) is not None
       and raises(lambda: Q.binary_forecast(0.5), ValueError) is not None)


def check_real_layers():
    print("== 统计底线、系统算法的真实输出拆成题")
    rng = random.Random(5)
    px, ix = [100.0], [2000.0]
    for _ in range(300):
        g = rng.gauss(0, 0.02)
        px.append(px[-1] * math.exp(g + rng.gauss(0, 0.01)))
        ix.append(ix[-1] * math.exp(g * 0.5))
    start = date(2026, 11, 3)
    kw = {"n_paths": 400, "seed": 3}
    b = baseline.baseline_forecast(px, start, set(), **kw)
    b["buy"] = system_forecast.baseline_buy(px, ix, start, set(), **kw)
    zero = {"values": dict.fromkeys(system_forecast.SIGNAL_WEIGHTS, 0), "missing": []}
    s = system_forecast.system_forecast(px, ix, start, set(), {**zero, "values": {**zero["values"], "valuation": 1}},
                                        {}, **kw)
    meta = {"run": "r", "as_of": "2026-11-02", "start_close": {"300308": px[-1] / 7},   # 不复权价随便设一个
            "p0_hfq": {"300308": px[-1]}, "categories": baseline.categories(start, set())}
    for author, layer in (("baseline", b), ("system", s)):
        qs = Q.questions_from_answer({"author": author, "stocks": {"300308": layer}}, meta)
        iv = _by(qs, "interval", n=63, lo="25", hi="75")
        ok(f"{author}:12 道(没有买入价位)、比值按 start_price 换算(中间 50% 包住 1 附近)",
           len(qs) == 12 and 0.7 < iv["forecast"]["lo"] < 1.0 < iv["forecast"]["hi"] < 1.4
           and abs(iv["forecast"]["lo"] - layer["horizons"]["63"]["q"]["25"] / px[-1]) < 1e-6, (len(qs), iv))
        ok(f"{author}:三组分类的类别和 categories 一致", list(_by(qs, "buy_signal_month")["forecast"])
           == meta["categories"]["buy_months"] and list(_by(qs, "bottom_month")["forecast"])
           == meta["categories"]["bottom_months"])


CHECKS = [check_brief, check_lagging_data, check_types, check_r15, check_buy, check_build, check_real_layers]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
