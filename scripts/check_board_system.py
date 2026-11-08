#!/usr/bin/env python3
"""check_board_system —— 预测看板「系统算法」(skills/forecast-board/scripts/system_forecast.py)的检查。

用法:
    python3 scripts/check_board_system.py        # 退出码 0 才算过

原来在 check_board_engine.py 里,那个文件超过 800 行时原样拆出来,用例一条没改。
不联网、不碰真实数据:合成行情(固定种子)、合成输入包。
合成价格的两个小工具(_days、_random_walk)和日期 NOV3 用 check_board_engine.py 里的那份,不另抄。
"""
import math
import random
import sys
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402,F401  先于 baseline(market.py 会删代理变量,见 fb_common.ORIG_ENV)
import baseline  # noqa: E402
import system_forecast  # noqa: E402
from check_board_engine import NOV3, _days, _random_walk  # noqa: E402

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


# ── system_forecast ─────────────────────────────────────────────────────────
def _item(key, value, status="ok"):
    """输入包里的一项(inputs.py 的结构,这里只填用得到的几个键)。"""
    return {"key": key, "value": value, "status": status}


def _world(seed, n_days=4000, block=126, drift=0.006, vol=0.01, follow=(1, 1, 1)):
    """合成行情:每 block 天随机定成涨势或跌势,指数顺着走;
    follow 里每只股票 1 = 跟大盘同向、-1 = 反向。drift=0 就是纯随机游走。
    返回 (日期, 指数收盘, {代码: 收盘})。"""
    rng = np.random.default_rng(seed)
    state = np.repeat(rng.choice([1, -1], size=n_days // block + 1), block)[:n_days]

    def walk(sign):
        r = sign * state * drift + vol * rng.standard_normal(n_days)
        return (100 * np.exp(np.cumsum(r))).tolist()

    idx = walk(1)
    return _days(n_days), idx, {f"S{i}": walk(f) for i, f in enumerate(follow)}


def _ou_index(seed, n_days=8000, phi=0.97, vol=0.01):
    """均值回归的指数:对数价每天往 0 拉回 3%,涨上去就往回落 ——
    站上 60 日线往往意味着接下来要跌,用来查「方向反了不算验证过」。"""
    rng = np.random.default_rng(seed)
    x, out = 0.0, []
    for e in rng.standard_normal(n_days):
        x = phi * x + vol * e
        out.append(100 * math.exp(x))
    return out


def check_system_forecast():
    print("== system_forecast:信号给漂移、买点条件模拟、价格信号走前检验")
    sf = system_forecast
    names = list(sf.SIGNAL_WEIGHTS)
    ok("五个信号、权重:计划里的初值;大盘、券商、云厂开支 2026-11-03 改成 0(补考里没显出用处)",
       sf.SIGNAL_WEIGHTS == {"regime": 0.0, "peer": 0.05, "consensus": 0.0, "capex": 0.0,
                             "valuation": 0.05} and sf.TILT_CAP == 0.40, (sf.SIGNAL_WEIGHTS, sf.TILT_CAP))

    # 计划里写定的六条
    px = _random_walk(1)
    idx = _random_walk(2)
    b1 = baseline.baseline_forecast(px, NOV3, set(), seed=7)
    neg = {"values": dict.fromkeys(names, -1), "missing": []}
    allv = dict.fromkeys(names, True)
    s = sf.system_forecast(px, idx, NOV3, set(), neg, allv, seed=7)
    ok("全看空 = −0.10(权重加起来 0.10:同行 0.05 + 估值 0.05)", abs(s["tilt"] + 0.10) < 1e-9, s["tilt"])
    ok("看空时涨的种数比底线少", s["horizons"]["126"]["p_up"] < b1["horizons"]["126"]["p_up"],
       (s["horizons"]["126"]["p_up"], b1["horizons"]["126"]["p_up"]))
    half = sf.system_forecast(px, idx, NOV3, set(), neg, dict.fromkeys(allv, False), seed=7)
    ok("都没验证:权重减半", abs(half["tilt"] + 0.05) < 1e-9, half["tilt"])
    zero = {"values": dict.fromkeys(names, 0), "missing": []}
    z = sf.system_forecast(px, idx, NOV3, set(), zero, allv, seed=7)
    ok("没信号时和底线一样", z["horizons"] == b1["horizons"] and z["bottom"] == b1["bottom"],
       (z["horizons"]["63"], b1["horizons"]["63"]))
    ok("买点月份加起来 100", sum(s["buy"]["month_probs"].values()) == 100, s["buy"]["month_probs"])
    up_idx = [100 + i for i in range(200)]
    up_px = [50 + i * 0.5 for i in range(200)]
    u = sf.system_forecast(up_px, up_idx, NOV3, set(), zero, allv, seed=7)
    ok("起点已满足条件:第一个月 ≥ 90", list(u["buy"]["month_probs"].values())[0] >= 90,
       u["buy"]["month_probs"])

    # 输出结构
    cats = baseline.categories(NOV3, set())
    ok("layer = system", s["layer"] == "system", s.get("layer"))
    ok("tilt=0 时年化波动、起点价也和底线一样",
       z["vol_annual"] == b1["vol_annual"] and z["start_price"] == b1["start_price"],
       (z["vol_annual"], b1["vol_annual"]))
    ok("tilt=0 写成 0.0(不是 -0.0)", z["tilt"] == 0.0 and math.copysign(1, z["tilt"]) == 1, z["tilt"])
    ok("买点月份的键 == categories 的 buy_months(顺序也一样,最后是 later)",
       list(s["buy"]["month_probs"]) == cats["buy_months"], list(s["buy"]["month_probs"]))
    ok("买点条件两条,照计划原文",
       s["buy"]["conditions"] == ["创业板指收盘在 60 日线上", "本股收盘在 60 日线上"], s["buy"]["conditions"])
    ok("见底那块的类别和底线一样(月份、价位分段)",
       list(s["bottom"]["month_probs"]) == cats["bottom_months"]
       and [[lo, hi] for lo, hi, _ in s["bottom"]["price_bins"]] == cats["price_bins"], s["bottom"])
    ok("见底月份、价位分段各自加起来 100",
       sum(s["bottom"]["month_probs"].values()) == 100
       and sum(p for _, _, p in s["bottom"]["price_bins"]) == 100, s["bottom"])
    ok("都验证过时 unvalidated 为空,都没验证时五个都列出",
       s["unvalidated"] == [] and half["unvalidated"] == names, (s["unvalidated"], half["unvalidated"]))
    ok("signals 带出信号值、缺的项、实际用的权重(没验证的减半)",
       half["signals"]["values"] == neg["values"] and half["signals"]["missing"] == []
       and half["signals"]["weights"] == {k: w / 2 for k, w in sf.SIGNAL_WEIGHTS.items()}, half["signals"])
    pos = sf.system_forecast(px, idx, NOV3, set(), {"values": dict.fromkeys(names, 1), "missing": []},
                             allv, seed=7)
    ok("全看多 +0.10,涨的种数比底线多",
       abs(pos["tilt"] - 0.10) < 1e-9 and pos["horizons"]["126"]["p_up"] > b1["horizons"]["126"]["p_up"],
       (pos["tilt"], pos["horizons"]["126"]["p_up"]))
    want_mid = px[-1] * math.exp(-0.10 / 250 * 250)
    ok("漂移按 tilt/250 每步加:全看空 250 天后中位价 ≈ 起点 × e^-0.10(差不到 3%)",
       abs(s["horizons"]["250"]["q"]["50"] / want_mid - 1) < 0.03, (s["horizons"]["250"]["q"]["50"], want_mid))
    ok("同一种子结果相同", sf.system_forecast(px, idx, NOV3, set(), neg, allv, seed=7) == s)
    ok("换种子结果不同", sf.system_forecast(px, idx, NOV3, set(), neg, allv, seed=8) != s)

    # 漂移怎么算
    one = {"values": {"valuation": 1}, "missing": []}
    t1 = sf.system_forecast(px, idx, NOV3, set(), one, {"valuation": True}, seed=7, n_paths=200)["tilt"]
    t2 = sf.system_forecast(px, idx, NOV3, set(), one, {}, seed=7, n_paths=200)["tilt"]
    ok("只有估值一个信号:验证过 0.05,没给验证结果按没验证算 0.025",
       abs(t1 - 0.05) < 1e-9 and abs(t2 - 0.025) < 1e-9, (t1, t2))
    mix = {"values": {"valuation": 1, "peer": -1}, "missing": []}
    t3 = sf.system_forecast(px, idx, NOV3, set(), mix, {"valuation": True}, seed=7, n_paths=200)["tilt"]
    ok("估值 +1(验证过)、同行 −1(没验证):0.05 − 0.025 = 0.025", abs(t3 - 0.025) < 1e-9, t3)
    off = sf.tilt_from({"values": {"consensus": 1, "capex": 1}, "missing": []}, {"consensus": True, "capex": True})
    ok("券商、云厂开支照算照记,但权重 0:两个都 +1 也不偏", off["tilt"] == 0.0 and off["values"]["capex"] == 1, off)
    reg = sf.tilt_from({"values": {"regime": -1}, "missing": []}, {"regime": True})
    ok("大盘信号照算照记,但权重 0:−1 也不让方向偏",
       reg["tilt"] == 0.0 and reg["values"]["regime"] == -1 and reg["weights"]["regime"] == 0.0, reg)
    keep = dict(sf.SIGNAL_WEIGHTS)
    try:                         # 现在的权重加起来只有 0.10,碰不到上限;临时放大权重查截断还灵不灵
        sf.SIGNAL_WEIGHTS.update(dict.fromkeys(keep, 0.2))
        capped = sf.tilt_from({"values": dict.fromkeys(keep, 1), "missing": []}, dict.fromkeys(keep, True))["tilt"]
    finally:
        sf.SIGNAL_WEIGHTS.clear()
        sf.SIGNAL_WEIGHTS.update(keep)
    ok("权重加起来超过 0.40 时截到 +0.40(上限还在起作用)", abs(capped - 0.40) < 1e-9, capped)
    ok("临时放大的权重已经改回", sf.SIGNAL_WEIGHTS == keep, sf.SIGNAL_WEIGHTS)
    ok("不认识的信号名报错",
       raises(lambda: sf.system_forecast(px, idx, NOV3, set(), {"values": {"regim": 1}}, {}, n_paths=50),
              ValueError) is not None)
    ok("信号值不是 −1/0/1 报错",
       raises(lambda: sf.system_forecast(px, idx, NOV3, set(), {"values": {"regime": 2}}, {}, n_paths=50),
              ValueError) is not None)
    ok("validated 里有不认识的名字报错",
       raises(lambda: sf.system_forecast(px, idx, NOV3, set(), zero, {"pear": True}, n_paths=50),
              ValueError) is not None)
    ok("不认识的参数报错",
       raises(lambda: sf.system_forecast(px, idx, NOV3, set(), zero, allv, n_path=50), TypeError) is not None)
    ok("指数收盘不够 lookback+1 个报错",
       raises(lambda: sf.system_forecast(px, idx[:100], NOV3, set(), zero, allv, n_paths=50), ValueError)
       is not None)

    # 底线层的买点 = 全 0 信号、同一种子的系统算法
    ok("baseline_buy == 全 0 信号的 buy", sf.baseline_buy(px, idx, NOV3, set(), seed=7) == z["buy"])

    # 期限比买点短:参数照传给底线那部分,买点照样看 250 天
    short = sf.system_forecast(px, idx, NOV3, set(), zero, allv, seed=7, horizons=(63,), bottom_window=63)
    bs = baseline.baseline_forecast(px, NOV3, set(), horizons=(63,), bottom_window=63, seed=7)
    ok("horizons/bottom_window 照传:tilt=0 时和同参数的底线一样",
       short["horizons"] == bs["horizons"] and short["bottom"] == bs["bottom"], short["horizons"])
    ok("模拟步数比 250 少时买点仍按 250 天分月",
       list(short["buy"]["month_probs"]) == cats["buy_months"]
       and sum(short["buy"]["month_probs"].values()) == 100, short["buy"]["month_probs"])

    # 一路往下走、又全看空到上限 −0.40:250 天里两条都满足不了 → later。
    # 查的是买点那块的逻辑,不是现在的权重 —— 现在权重加起来只有 0.10,压不到 −0.40,
    # 所以临时放大权重,查完改回(同上面查上限的做法)。
    dn_px = [150 - i * 0.5 for i in range(200)]
    keep = dict(sf.SIGNAL_WEIGHTS)
    try:
        sf.SIGNAL_WEIGHTS.update(dict.fromkeys(keep, 0.2))
        dn = sf.system_forecast(dn_px, up_idx, NOV3, set(), neg, allv, seed=7, n_paths=2000)
    finally:
        sf.SIGNAL_WEIGHTS.clear()
        sf.SIGNAL_WEIGHTS.update(keep)
    ok("本股一直在跌、全看空到 −0.40:later ≥ 90",
       dn["tilt"] == -0.40 and dn["buy"]["month_probs"]["later"] >= 90, (dn["tilt"], dn["buy"]["month_probs"]))
    ok("放大的权重已经改回", sf.SIGNAL_WEIGHTS == keep, sf.SIGNAL_WEIGHTS)

    # 指数路径:另一个随机数发生器(seed+1),按相关系数混进个股的随机数
    rho = sf.return_corr(px, idx, 120)
    rx = np.diff(np.log(px[-121:]))
    ri = np.diff(np.log(idx[-121:]))
    ok("相关系数 = 最近 120 天两者日对数收益的相关系数",
       abs(rho - float(np.corrcoef(rx, ri)[0, 1])) < 1e-12, rho)
    ok("两条线性上涨的序列相关接近 1,截到 0.99", sf.return_corr(up_px, up_idx, 120) == 0.99,
       sf.return_corr(up_px, up_idx, 120))
    inv = [1000.0 / p for p in up_px]      # 对数收益正好是 up_px 的相反数
    ok("对数收益正好相反的序列截到 −0.99", sf.return_corr(up_px, inv, 120) == -0.99,
       sf.return_corr(up_px, inv, 120))
    ok("有一边一点不动(波动 0)时相关记 0", sf.return_corr(up_px, [10.0] * 200, 120) == 0.0,
       sf.return_corr(up_px, [10.0] * 200, 120))
    zs = baseline.draw_normals(7, 4000, 250)
    zi = sf.index_normals(zs, 7, 0.6)
    manual = 0.6 * zs + math.sqrt(1 - 0.36) * np.random.default_rng(8).standard_normal((4000, 250))
    ok("指数随机数 = ρ·个股 + √(1−ρ²)·default_rng(seed+1) 抽的", np.array_equal(zi, manual))
    emp = float(np.corrcoef(zs.ravel(), zi.ravel())[0, 1])
    ok("混出来的相关系数 ≈ 0.6(差不到 0.01)", abs(emp - 0.6) < 0.01, emp)
    ok("index_normals 不改动传进去的个股随机数",
       np.array_equal(zs, baseline.draw_normals(7, 4000, 250)))

    # 买点:手造路径(均线取 3 天),查「均线用历史尾巴 + 模拟值」「严格高于」「第 k 步对第 k 个交易日」
    # 2026-11-27 周五起 3 个交易日:11-27、11-30、12-01
    sp = np.array([[11.0, 12, 13], [9, 9, 12], [9, 12, 12], [11, 12, 13]])
    ip = np.array([[11.0, 12, 13], [11, 11, 11], [9, 9, 12], [9, 12, 8]])
    hb = sf.buy_block(sp, ip, [1.0, 10, 10], [1.0, 10, 10], date(2026, 11, 27), set(), 3, ma=3)
    # 第 1 条第 1 步就满足 → 11 月;第 2 条第 3 步指数 11 = 均线 11,不算站上 → later;
    # 第 3 条第 3 步(12-01)两条同时满足 → 12 月;第 4 条第 2 步(11-30)→ 11 月
    ok("手造路径:11 月 50、12 月 25、later 25",
       hb["month_probs"] == {"2026-11": 50, "2026-12": 25, "later": 25}, hb["month_probs"])
    ok("手造路径:条件文字跟着均线天数走",
       hb["conditions"] == ["创业板指收盘在 3 日线上", "本股收盘在 3 日线上"], hb["conditions"])
    ok("历史不够凑一条均线报错",
       raises(lambda: sf.buy_block(sp, ip, [10.0], [1.0, 10, 10], date(2026, 11, 27), set(), 3, ma=3),
              ValueError) is not None)

    # signals_from_inputs(输入包里各项的结构)
    inp = {
        "market.regime": _item("market.regime", {"index": "sz399006", "close": 2100.0, "ma60": 2200.0,
                                                 "state": "below", "days": 5}),
        "peer.300308.300502": _item("peer.300308.300502", {"gap10": -12.5, "gap20": -3.0}),
        "consensus.300308": _item("consensus.300308", {"profit": {}, "ratings": {"up": 1, "keep": 5, "down": 3}}),
        "capex": _item("capex", {"total": [{"q": "2026-03-31", "sum": 1.0, "yoy": 60.0, "cash_yoy": 50.0},
                                           {"q": "2026-06-30", "sum": 2.0, "yoy": 107.6, "cash_yoy": 90.0},
                                           {"q": "2026-09-30", "sum": 3.0, "yoy": 112.6, "cash_yoy": 95.0}],
                                 "guidance": []}, status="stale"),
        "valuation.300308": _item("valuation.300308", {"pe_pctl": 85.0, "pb_pctl": 70.0, "dyn_pe": 40.0},
                                  status="partial"),
    }
    g = sf.signals_from_inputs(inp, "300308")
    ok("300308:大盘线下 −1、落后同行 12.5 个点 −1、调低多 −1、资本开支同比高 5 个点 +1、PE 分位 85 −1",
       g == {"values": {"regime": -1, "peer": -1, "consensus": -1, "capex": 1, "valuation": -1}, "missing": [],
             "sources": {"consensus": "ratings"}}, g)
    g2 = sf.signals_from_inputs(inp, "300502")
    ok("300502:同行那项取反 +1;没有它的券商、估值 → 记 0 并列进 missing(按信号顺序)",
       g2 == {"values": {"regime": -1, "peer": 1, "consensus": 0, "capex": 1, "valuation": 0},
              "missing": ["consensus", "valuation"], "sources": {"consensus": None}}, g2)
    ok("stale / partial 的值照样用", g["values"]["capex"] == 1 and g["values"]["valuation"] == -1, g)
    failed = {**inp, "market.regime": _item("market.regime", None, status="failed")}
    gf = sf.signals_from_inputs(failed, "300308")
    ok("status=failed 当缺:记 0 并列进 missing", gf["values"]["regime"] == 0 and gf["missing"] == ["regime"], gf)
    ok("一项都没有:五个都是 0、都列进 missing",
       sf.signals_from_inputs({}, "300308") == {"values": dict.fromkeys(names, 0), "missing": names,
                                                "sources": {"consensus": None}})
    ok("system_forecast 把信号来源原样带进输出",
       sf.system_forecast(px, idx, NOV3, set(), g, allv, seed=7, n_paths=200)["signals"]["sources"]
       == {"consensus": "ratings"})
    ok("代码不在任何同行那项里 → peer 缺", "peer" in sf.signals_from_inputs(inp, "002353")["missing"])

    def one_sig(name, key, value, code="300308"):
        return sf.signals_from_inputs({key: _item(key, value)}, code)["values"][name]

    peer = {gap: one_sig("peer", "peer.300308.300502", {"gap10": gap, "gap20": 0.0})
            for gap in (-10.0, -9.99, 0.0, 9.99, 10.0)}
    ok("同行涨跌差边界:−10 算 −1、±9.99 算 0、+10 算 +1",
       peer == {-10.0: -1, -9.99: 0, 0.0: 0, 9.99: 0, 10.0: 1}, peer)
    ok("同行那项本股是 B:差取反(B 落后 = A 领先 12 个点 → −1)",
       one_sig("peer", "peer.300502.300308", {"gap10": 12.0, "gap20": 0.0}) == -1)
    pe = {p: one_sig("valuation", "valuation.300308", {"pe_pctl": p, "pb_pctl": None, "dyn_pe": None})
          for p in (80.0, 79.9, 50.0, 20.1, 20.0)}
    ok("PE 分位边界:80 算 −1、20 算 +1、中间 0", pe == {80.0: -1, 79.9: 0, 50.0: 0, 20.1: 0, 20.0: 1}, pe)
    ok("PE 分位是 None → 缺",
       "valuation" in sf.signals_from_inputs({"valuation.300308": _item(
           "valuation.300308", {"pe_pctl": None, "pb_pctl": 30.0, "dyn_pe": 20.0})}, "300308")["missing"])
    val = "valuation.300308"
    e = raises(lambda: sf.signals_from_inputs(
        {val: _item(val, {"pe_pctl": 120.0, "pb_pctl": 30.0, "dyn_pe": 20.0})}, "300308"), ValueError)
    ok("PE 分位超出 0~100 报错,写出是哪一项、哪个字段",
       e is not None and val in str(e) and "pe_pctl" in str(e), e)
    e = raises(lambda: sf.signals_from_inputs(
        {val: _item(val, {"pe_pctl": 50.0, "pb_pctl": -1.0, "dyn_pe": 20.0})}, "300308"), ValueError)
    ok("PB 分位超出 0~100 也报错", e is not None and val in str(e) and "pb_pctl" in str(e), e)
    ok("分位正好 0、100 不报错(0 → +1,100 → −1)",
       one_sig("valuation", val, {"pe_pctl": 0.0, "pb_pctl": 100.0, "dyn_pe": None}) == 1
       and one_sig("valuation", val, {"pe_pctl": 100.0, "pb_pctl": 0.0, "dyn_pe": None}) == -1)
    ok("取数失败那项里的分位不查(不用它)",
       raises(lambda: sf.signals_from_inputs({val: _item(val, {"pe_pctl": 500.0}, status="failed")}, "300308"),
              ValueError) is None)

    # 券商信号先看盈利预测上下调(eps_revisions.<code>),缺了才退回评级
    def cons(eps=None, ratings=None, eps_status="ok"):
        d = {}
        if eps is not None:
            d["eps_revisions.300308"] = _item("eps_revisions.300308", eps, status=eps_status)
        if ratings is not None:
            d["consensus.300308"] = _item("consensus.300308", {"profit": {}, "ratings": ratings})
        return sf.signals_from_inputs(d, "300308")

    r_up = {"up": 3, "keep": 0, "down": 0}
    c1 = cons({"up30": 1, "down30": 4}, r_up)
    ok("盈利预测调低 4 > 调高 1 → −1(评级调高也不看),来源 eps_revisions",
       c1["values"]["consensus"] == -1 and c1["sources"]["consensus"] == "eps_revisions", c1)
    c2 = cons({"up30": 2, "down30": 2}, r_up)
    ok("盈利预测调高 = 调低 → 0,不算缺,也不退回评级",
       c2["values"]["consensus"] == 0 and "consensus" not in c2["missing"]
       and c2["sources"]["consensus"] == "eps_revisions", c2)
    ok("盈利预测调高多 → +1", cons({"up30": 5, "down30": 0})["values"]["consensus"] == 1)
    c4 = cons({"up30": 0, "down30": 3}, r_up, eps_status="failed")
    ok("盈利预测那项取数失败 → 退回评级(调高多 +1),来源 ratings",
       c4["values"]["consensus"] == 1 and c4["sources"]["consensus"] == "ratings", c4)
    c5 = cons(None, {"up": 0, "keep": 2, "down": 1})
    ok("没有盈利预测那项 → 退回评级(调低多 −1),来源 ratings",
       c5["values"]["consensus"] == -1 and c5["sources"]["consensus"] == "ratings", c5)
    c6 = cons({"up30": None, "down30": 1})
    ok("盈利预测那项形状不对、又没有评级 → 缺,来源 None",
       c6["values"]["consensus"] == 0 and "consensus" in c6["missing"] and c6["sources"]["consensus"] is None, c6)
    ok("券商调高 = 调低 → 0",
       one_sig("consensus", "consensus.300308", {"profit": {}, "ratings": {"up": 2, "keep": 0, "down": 2}}) == 0)
    ok("券商调高多 → +1",
       one_sig("consensus", "consensus.300308", {"profit": {}, "ratings": {"up": 3, "keep": 0, "down": 1}}) == 1)

    def capex(*yoys):
        return {"total": [{"q": f"q{i}", "sum": 1.0, "yoy": y, "cash_yoy": None} for i, y in enumerate(yoys)],
                "guidance": []}

    ok("资本开支同比低 5 个点(112.6 → 107.6)→ −1", one_sig("capex", "capex", capex(112.6, 107.6)) == -1)
    ok("资本开支同比差 4.9 个点 → 0", one_sig("capex", "capex", capex(100.0, 104.9)) == 0)
    ok("浮点 8.2 − 3.2 = 4.9999… 也算高 5 个点 → +1", one_sig("capex", "capex", capex(3.2, 8.2)) == 1,
       8.2 - 3.2)
    ok("最近两季有一个同比是 None → 缺",
       "capex" in sf.signals_from_inputs({"capex": _item("capex", capex(50.0, None))}, "x")["missing"])
    ok("只有一季 → 缺", "capex" in sf.signals_from_inputs({"capex": _item("capex", capex(50.0))}, "x")["missing"])
    weird = {"market.regime": _item("market.regime", "看不懂"), "capex": _item("capex", {"total": "x"}),
             "valuation.300308": _item("valuation.300308", {"pe_pctl": True}),
             "consensus.300308": _item("consensus.300308", {"ratings": None})}
    gw = raises(lambda: sf.signals_from_inputs(weird, "300308"), Exception)
    ok("值的形状不对不抛异常", gw is None, gw)
    ok("值的形状不对一律当缺",
       gw is None and set(sf.signals_from_inputs(weird, "300308")["missing"]) == set(names),
       sf.signals_from_inputs(weird, "300308") if gw is None else gw)

    # validate_price_signals:走前检验(每个信号只在一条序列上检验,样本日子不重复;
    # 两边各 ≥ 30 个、单边两比例 z 检验 z ≥ 1.96、上涨比例差 ≥ 3 个点)
    ds, ix, st = _world(11, n_days=8000)
    v = sf.validate_price_signals(ds, ix, st)
    ok("返回五个信号,券商、资本开支、估值固定 False",
       sorted(v) == sorted(names) and not v["consensus"] and not v["capex"] and not v["valuation"], v)
    ok("指数有涨势跌势(站上 60 日线后接着涨)→ regime 验证过", v["regime"] is True, v)
    stats = sf.walk_forward_stats(ds, ix, st)["regime"]
    ok("有用的那组:线上涨的比例比线下高 20 个点以上,z ≥ 1.96",
       stats["diff"] > 20 and stats["z"] >= 1.96 and stats["n_plus"] >= 30 and stats["n_minus"] >= 30, stats)
    ok("regime 只看指数一条序列:t = 59 + 63k 且 t + 63 ≤ 7999,k = 0..125 共 126 个,不乘股票数",
       stats["n"] == 126, stats["n"])
    so = sf.walk_forward_stats(ds, _ou_index(3), st)["regime"]
    ok("均值回归的指数(站上 60 日线后反而跌)→ 方向反了,不算验证过",
       so["validated"] is False and so["up_plus"] < so["up_minus"], so)
    ds3, ix3, st3 = _world(11, n_days=1000)
    s3 = sf.walk_forward_stats(ds3, ix3, st3)["regime"]
    ok("同样有用、但样本不到 30 → 不算验证过",
       s3["validated"] is False and min(s3["n_plus"], s3["n_minus"]) < 30, s3)
    ds4, ix4, st4 = _world(5, n_days=8000, drift=0.0, follow=(1, 1))
    v4 = sf.validate_price_signals(ds4, ix4, st4)
    ok("纯随机游走 → regime、peer 都不算验证过", v4["regime"] is False and v4["peer"] is False,
       sf.walk_forward_stats(ds4, ix4, st4))
    ds5, ix5, st5 = _world(11, n_days=10000, follow=(1, -1))
    p5 = sf.walk_forward_stats(ds5, ix5, st5)["peer"]
    ok("两只股票一涨一跌轮换:领先同行的接着涨 → peer 验证过(只看 A 一边,日子不重复)",
       p5["validated"] is True and list(p5["pairs"]) == ["S0.S1"], p5)
    ok("没给 pairs:按 stocks 的顺序两两配对",
       list(sf.walk_forward_stats(ds5, ix5, {**st5, "S2": st5["S0"]})["peer"]["pairs"]) == ["S0.S1", "S0.S2", "S1.S2"])
    ds6, ix6, st6 = _world(11, n_days=10000, follow=(1, -1, 1))
    p6 = sf.walk_forward_stats(ds6, ix6, st6, pairs=[("S0", "S1"), ("S0", "S2")])["peer"]
    ok("配了两对:一对过、一对不过(S0、S2 同涨同跌,差拉不开)→ peer 不算验证过",
       p6["pairs"]["S0.S1"]["validated"] and not p6["pairs"]["S0.S2"]["validated"] and p6["validated"] is False,
       p6)
    dup = sf.walk_forward_stats(ds5, ix5, st5, pairs=[("S0", "S1"), ("S0", "S1")])["peer"]["pairs"]
    ok("同一对写两遍只算一遍(样本不翻倍)", dup == p5["pairs"], dup)
    ok("只有一只股票:没有同行可比,peer 不算验证过",
       sf.validate_price_signals(ds, ix, {"S0": st["S0"]})["peer"] is False)
    ok("同行那对里的代码不在 stocks 里报错",
       raises(lambda: sf.validate_price_signals(ds5, ix5, st5, pairs=[("S0", "X")]), ValueError) is not None)
    late = {**st5, "S0": [None] * 3000 + st5["S0"][3000:]}
    e5 = raises(lambda: sf.walk_forward_stats(ds5, ix5, late), Exception)
    ok("股票上市晚(前面是 None):不报错,少算那几个样本",
       e5 is None and sf.walk_forward_stats(ds5, ix5, late)["peer"]["pairs"]["S0.S1"]["n"]
       < p5["pairs"]["S0.S1"]["n"], e5)
    ok("股票收盘和指数日期个数对不上报错",
       raises(lambda: sf.validate_price_signals(ds, ix, {"S0": st["S0"][:-1]}), ValueError) is not None)
    # 手造 123 根:只有 t = 59 一个样本日(前面正好够 60 日线),往后第 63 根是第 122 根
    hand = [100.0] * 121 + [99.0, 101.0]          # 第 121 根跌、第 122 根涨
    hs_ = sf.walk_forward_stats(_days(123), hand, {"A": hand, "B": [100.0] * 123})
    ok("样本日从第 59 根起、结果看第 59+63 根(101 涨)不看第 121 根(99 跌)",
       hs_["regime"]["n"] == 1 and hs_["regime"]["up_all"] == 100.0
       and hs_["peer"]["pairs"]["A.B"]["n"] == 1 and hs_["peer"]["pairs"]["A.B"]["up_all"] == 100.0, hs_)

    # 检验本身:z 检验把「差得不少但样本少、可能是碰巧」的挡掉
    lucky = [(1, True)] * 22 + [(1, False)] * 18 + [(-1, True)] * 18 + [(-1, False)] * 22
    t1 = sf.signal_test(lucky)
    ok("两边各 40 个、55% 对 45%:差 10 个点但 z ≈ 0.89 < 1.96 → 不算(旧规则「各比全部差 3 个点」会算过)",
       t1["validated"] is False and t1["diff"] == 10.0
       and abs(t1["z"] - 0.1 / math.sqrt(0.25 * 2 / 40)) <= 5e-4, t1)   # z 报出来保留 3 位
    good = [(1, True)] * 30 + [(1, False)] * 10 + [(-1, True)] * 15 + [(-1, False)] * 25
    t2 = sf.signal_test(good)
    z2 = (0.75 - 0.375) / math.sqrt(0.5625 * 0.4375 * (2 / 40))
    ok("两边各 40 个、75% 对 37.5%:z ≈ 3.38 → 算验证过",
       t2["validated"] is True and abs(t2["z"] - z2) <= 5e-4, t2)
    ok("差 100 个点但每边只有 29 个 → 不算",
       sf.signal_test([(1, True)] * 29 + [(-1, False)] * 29)["validated"] is False)
    ok("一边一个样本都没有:z 记 None,不算",
       sf.signal_test([(1, True)] * 40)["z"] is None and sf.signal_test([(1, True)] * 40)["validated"] is False)
    ok("两边全涨(合并比例 100%,z 没定义):z 记 0,不算",
       sf.signal_test([(1, True)] * 40 + [(-1, True)] * 40)["z"] == 0.0)


CHECKS = [check_system_forecast]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
