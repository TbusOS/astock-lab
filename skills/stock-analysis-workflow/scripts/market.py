#!/usr/bin/env python3
"""market —— 大盘这一层:个股的领先指标管不到大盘。

用法:
    market regime                      # 现在:创业板指 vs 60 日线 + 全市场融资余额方向
    market backtest --since 2014-01-01 # 这条规则过去十几年会怎样:少跌多少、来回几次
    market peer 300308 300502          # 同行两只近 10 / 20 个交易日走势差多少
    market --self-test                 # 用假数据检查计算本身

为什么要有这一层(2026-10-29 复核时发现):
    8/27 第一次给中际、新易盛「持有」时，创业板指已经在 60 日均线下面 7 周
    (7/10 跌破，到 10/29 一天没站回去)。之后两只跌 32%~44%，同期创业板指
    −24%~−27% —— 大头是大盘。系统当时只看云厂资本开支这类**行业**指标，
    行业指标一直向上，所以一直说持有。**没有任何一层在看大盘。**

为什么同行背离要单独看:
    新易盛三季报前两周就比中际多跌了 14 个点，三季报果然差。行业指标是整个行业的数，
    看不出哪一家这个季度会掉;同行走势突然分开，往往是市场先看到了公司自己的事。

⚠ 均线规则会来回触发(7/9 站上一天又跌破)。用之前先看 backtest 的「来回次数」,
   并且它只是「先降杠杆」的信号，不是「清仓」的信号。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

socket.setdefaulttimeout(20)

INDEX = "sz399006"            # 创业板指:光模块等成长股主要在创业板，比上证更贴
MA_N = 60
GAP_PP = 10.0                 # 同行两周差 10 个点以上就标出来
_UA = {"User-Agent": "Mozilla/5.0"}


# ── 取数 ────────────────────────────────────────────────────────────────────
def _get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA)) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_daily(sym, start, end=None, fq=""):
    """腾讯日线 [(日期, 收盘)]。单次最多约 2000 根，按 5 年一段取。
    只能走 https —— http 回 302(2026-10-29 本机实测)。"""
    end = end or dt.date.today().isoformat()
    out, y0 = [], int(start[:4])
    while y0 <= int(end[:4]):
        s = max(start, f"{y0}-01-01")
        e = min(end, f"{y0 + 4}-12-31")
        d = _get_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                      {"param": f"{sym},day,{s},{e},2000,{fq}"})["data"]
        if isinstance(d, dict):
            k = d[sym].get("qfqday") or d[sym].get("day") or []
            out += [(x[0], float(x[2])) for x in k]
        y0 += 5
    seen, uniq = set(), []
    for x in out:
        if x[0] not in seen:
            seen.add(x[0])
            uniq.append(x)
    return uniq


def fetch_margin(pages=1, size=500):
    """全市场(沪深合计)融资余额 [(日期, 亿元)],旧→新。东财 RPTA_RZRQ_LSHJ。"""
    rows = []
    for pg in range(1, pages + 1):
        j = _get_json("https://datacenter-web.eastmoney.com/api/data/v1/get",
                      {"reportName": "RPTA_RZRQ_LSHJ", "columns": "DIM_DATE,RZYE",
                       "sortColumns": "DIM_DATE", "sortTypes": "-1",
                       "pageSize": size, "pageNumber": pg})
        res = j.get("result") or {}
        rows += [(r["DIM_DATE"][:10], r["RZYE"] / 1e8) for r in res.get("data") or []]
        if pg >= (res.get("pages") or 1):
            break
    return sorted(rows)


# ── 计算(纯函数，--self-test 查的就是这些) ───────────────────────────────────
def moving_avg(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def regime(closes, n=MA_N, confirm=1):
    """每天的状态:'below' / 'above' / None(均线还没算出来)。
    连续 confirm 天站在另一边才算切换 —— 用来压住来回触发。"""
    ma = moving_avg(closes, n)
    state, run, out = None, 0, []
    for c, m in zip(closes, ma):
        if m is None:
            out.append(None)
            continue
        side = "below" if c < m else "above"
        if state is None:
            state = side
        elif side != state:
            run += 1
            if run >= confirm:
                state, run = side, 0
        else:
            run = 0
        out.append(state)
    return out


def pct_change(series, back):
    """series [(日期, 值)] 最后一个比 back 个之前变了多少 %。"""
    if len(series) <= back:
        return None
    return (series[-1][1] / series[-1 - back][1] - 1) * 100


def peer_gap(a, b, days):
    """两只 [(日期, 收盘)] 只取两只都有的日期，近 days 个交易日 a 比 b 多涨(少跌)几个点。"""
    db = dict(b)
    common = [(d, c) for d, c in a if d in db]
    if len(common) <= days:
        return None
    (d0, a0), (d1, a1) = common[-1 - days], common[-1]
    ra, rb = (a1 / a0 - 1) * 100, (db[d1] / db[d0] - 1) * 100
    return {"from": d0, "to": d1, "a": ra, "b": rb, "gap": ra - rb}


def max_drawdown(curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def backtest(dates, closes, states):
    """前一天收盘是 above 就拿着指数，below 就空仓。返回策略与一直拿着的对比。"""
    hold, strat, switches, prev = [1.0], [1.0], 0, None
    for i in range(1, len(closes)):
        r = closes[i] / closes[i - 1]
        hold.append(hold[-1] * r)
        s = states[i - 1]
        strat.append(strat[-1] * (r if s in ("above", None) else 1.0))
        if s and prev and s != prev:
            switches += 1
        prev = s or prev
    yrs = max((dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days / 365.25, 1e-9)
    cagr = lambda c: (c[-1] ** (1 / yrs) - 1) * 100
    below = sum(1 for s in states if s == "below")
    return {"years": yrs, "hold_cagr": cagr(hold), "strat_cagr": cagr(strat),
            "hold_mdd": max_drawdown(hold), "strat_mdd": max_drawdown(strat),
            "switches": switches, "per_year": switches / yrs,
            "below_share": below / max(1, sum(1 for s in states if s)) * 100}


def episodes(dates, closes, states, depth=25.0):
    """指数从高点跌超 depth% 的每一段:高点、低点、规则第一次说 below 时已经跌了多少。

    一段在什么时候结束:涨回前高，**或者从低点反弹超过同样的幅度**。
    只认「涨回前高」的话，2015 年 6 月的高点到 2026 年才被突破，
    中间 2018、2021~2024 几轮大跌全被并成一段(2026-10-29 第一版就是这样)。
    """
    out, i, n = [], 0, len(closes)
    while i < n:
        pk = i
        j = i
        while j < n and closes[j] / closes[pk] - 1 > -depth / 100:
            if closes[j] > closes[pk]:
                pk = j
            j += 1
        if j >= n:
            break
        tr = j
        while j < n and closes[j] < closes[pk] and closes[j] / closes[tr] - 1 < depth / 100:
            if closes[j] < closes[tr]:
                tr = j
            j += 1
        sig = next((k for k in range(pk, tr + 1) if states[k] == "below"
                    and (k == 0 or states[k - 1] != "below")), None)
        if sig is None and states[pk] == "below":
            sig = pk
        out.append({"peak": dates[pk], "peak_v": closes[pk], "trough": dates[tr],
                    "trough_v": closes[tr], "drop": (closes[tr] / closes[pk] - 1) * 100,
                    "signal": dates[sig] if sig is not None else None,
                    "drop_at_signal": (closes[sig] / closes[pk] - 1) * 100 if sig is not None else None})
        i = j
    return out


# ── 出表(命令行和 preport 共用，格式只写一份) ────────────────────────────────
def regime_lines(index=INDEX, n=MA_N, confirm=1, title=None):
    """现在大盘偏不偏弱，返回 markdown 行。取数失败的那一项写明失败，不整段放弃。"""
    k = fetch_daily(index, (dt.date.today() - dt.timedelta(days=400)).isoformat())
    dates, closes = [d for d, _ in k], [c for _, c in k]
    st, ma = regime(closes, n, confirm), moving_avg(closes, n)
    since = len(st) - 1
    while since > 0 and st[since - 1] == st[-1]:
        since -= 1
    hi = max(range(len(closes)), key=lambda i: closes[i])
    L = [title or f"## 大盘:{index} vs {n} 日均线 + 全市场融资余额", "",
         "| 看什么 | 现在 |", "|---|---|",
         f"| 指数收盘 {dates[-1]} | {closes[-1]:,.2f}(近一年最高 {dates[hi]} {closes[hi]:,.2f},"
         f"{(closes[-1] / closes[hi] - 1) * 100:+.1f}%) |",
         f"| {n} 日均线 | {ma[-1]:,.2f},指数{'在下面' if st[-1] == 'below' else '在上面'},"
         f"从 {dates[since]} 起已 {len(st) - since} 个交易日 |"]
    c20 = None
    try:
        m = fetch_margin()
        pk = max(m[-250:], key=lambda x: x[1])
        c20 = pct_change(m, 20)
        L.append(f"| 融资余额 {m[-1][0]} | {m[-1][1]:,.0f} 亿(一年内最高 {pk[0]} {pk[1]:,.0f},"
                 f"{(m[-1][1] / pk[1] - 1) * 100:+.1f}%);近 20 个交易日 {c20:+.1f}% |")
    except Exception as e:
        L.append(f"| 融资余额 | 取数失败 {type(e).__name__} |")
    weak = (st[-1] == "below") + (c20 is not None and c20 < 0)
    verdict = {2: "🔴 两条都偏弱:有融资的先降杠杆，不加仓",
               1: "🟡 一条偏弱:不加杠杆，盯另一条",
               0: "🟢 两条都正常"}[weak]
    L += ["", f"**{verdict}**。这是降杠杆信号，不是清仓信号;"
              "规则过去的表现跑 `market backtest`。", ""]
    return L


def peer_lines(code_a, code_b, gap=GAP_PP, title=None):
    """同行两只近 10 / 20 个交易日差多少，返回 markdown 行。"""
    start = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    sym = lambda c: ("sh" if c.startswith(("6", "9")) else "sz") + c
    ka, kb = fetch_daily(sym(code_a), start, fq="qfq"), fetch_daily(sym(code_b), start, fq="qfq")
    L = [title or f"## 同行背离:{code_a} vs {code_b}(前复权)", "",
         f"| 区间 | 交易日 | {code_a} | {code_b} | 差 |", "|---|---|---|---|---|"]
    flagged = False
    for days in (10, 20):
        g = peer_gap(ka, kb, days)
        if g is None:
            continue
        mark = " 🔺" if abs(g["gap"]) >= gap else ""
        flagged |= bool(mark)
        L.append(f"| {g['from']} → {g['to']} | {days} | {g['a']:+.1f}% | {g['b']:+.1f}% | "
                 f"**{g['gap']:+.1f} 个点**{mark} |")
    if flagged:
        L += ["", f"🔺 差超过 {gap:.0f} 个点:先查跌得多的那只是不是公司自己出了事"
                  "(季报、订单、客户、现金流),别默认是行业的事。"]
    return L + [""]


# ── 命令 ────────────────────────────────────────────────────────────────────
def cmd_regime(a):
    print("\n".join(regime_lines(a.index, a.ma, a.confirm)))
    return 0


def cmd_backtest(a):
    k = fetch_daily(a.index, a.since)
    dates, closes = [d for d, _ in k], [c for _, c in k]
    print(f"## 回测:{a.index} {dates[0]} ~ {dates[-1]}({len(dates)} 个交易日)\n")
    print("规则:前一天收盘在均线下面(连续 N 天确认)就空仓，否则拿着指数。不算手续费。\n")
    print("| 确认天数 | 一直拿着 年化/最大回撤 | 按规则 年化/最大回撤 | 来回次数(每年) | 在均线下的时间 |")
    print("|---|---|---|---|---|")
    for c in a.confirm:
        st = regime(closes, a.ma, c)
        r = backtest(dates, closes, st)
        print(f"| {c} 天 | {r['hold_cagr']:+.1f}% / {r['hold_mdd']:.0f}% | "
              f"{r['strat_cagr']:+.1f}% / {r['strat_mdd']:.0f}% | "
              f"{r['switches']}({r['per_year']:.1f}) | {r['below_share']:.0f}% |")
    st = regime(closes, a.ma, a.confirm[-1])
    print(f"\n### 跌超 {a.depth:.0f}% 的几段(按 {a.confirm[-1]} 天确认)\n")
    print("| 高点 | 低点 | 全段跌幅 | 规则第一次说「在均线下」 | 那时已跌 |")
    print("|---|---|---|---|---|")
    for e in episodes(dates, closes, st, a.depth):
        sig = e["signal"] or "没触发"
        at = "—" if e["drop_at_signal"] is None else f"{e['drop_at_signal']:+.0f}%"
        print(f"| {e['peak']} {e['peak_v']:,.0f} | {e['trough']} {e['trough_v']:,.0f} | "
              f"{e['drop']:+.0f}% | {sig} | {at} |")
    print("\n读法:规则躲不开第一段下跌(要等跌破均线),能躲的是后面那一大段;"
          "代价是来回次数 —— 每次来回都是一次「卖了又涨回去」。")
    return 0


def cmd_peer(a):
    print("\n".join(peer_lines(a.a, a.b, a.gap)))
    return 0


# ── 自检 ────────────────────────────────────────────────────────────────────
def self_test():
    fails = 0

    def ok(name, cond, got=""):
        nonlocal fails
        fails += not cond
        print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"(得到 {got})"))

    print("== market 自检(假数据)")
    ok("均线:[1..5] 的 3 日均线", moving_avg([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4],
       moving_avg([1, 2, 3, 4, 5], 3))
    up_then_down = list(range(1, 11)) + [6, 5, 4]
    st1 = regime(up_then_down, 3, 1)
    ok("跌破 3 日线当天就切到 below(确认 1 天)", st1[10] == "below" and st1[9] == "above", st1)
    st2 = regime(up_then_down, 3, 2)
    ok("确认 2 天:第一天还算 above,第二天才切", st2[10] == "above" and st2[11] == "below", st2)
    whip = [10] * 5 + [9, 11, 9, 11, 9, 11]
    ok("来回穿:确认 3 天不跟着来回切", len(set(s for s in regime(whip, 5, 3) if s)) == 1,
       regime(whip, 5, 3))
    a = [("d%d" % i, 100 + i) for i in range(12)]
    b = [("d%d" % i, 100.0) for i in range(12)]
    g = peer_gap(a, b, 10)
    ok("同行差:a 从 101 到 111 涨 9.9%,b 不动 → 差 +9.9", g and abs(g["gap"] - 9.90) < 0.01, g)
    ok("同行差:日期对不上的天不算", peer_gap(a, [("x", 1.0)], 10) is None)
    crash = [100 + i for i in range(80)] + [179 - 3 * i for i in range(40)]
    ds = [(dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(len(crash))]
    r = backtest(ds, crash, regime(crash, 20, 1))
    ok("大跌一段:按规则的最大回撤比一直拿着小", r["strat_mdd"] > r["hold_mdd"], r)
    ep = episodes(ds, crash, regime(crash, 20, 1), 25)
    ok("跌超 25% 的段找得到，且信号在低点之前", len(ep) == 1 and ep[0]["signal"]
       and ep[0]["signal"] < ep[0]["trough"], ep)
    # 200 → 120(−40%)→ 170(反弹 +42%,没回到 200)→ 100(−41%):是两段，不是一段。
    # 2026-10-29 第一版只认「涨回前高才算结束」,把创业板 2015~2026 的全部下跌并成了一段。
    two = ([100 + i for i in range(101)] + [200 - 2 * i for i in range(41)]
           + [120 + 2 * i for i in range(26)] + [170 - 2 * i for i in range(36)])
    ds2 = [(dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(len(two))]
    ep2 = episodes(ds2, two, regime(two, 20, 1), 25)
    ok("反弹超过同样幅度但没回前高 → 算两段", len(ep2) == 2, [(e["peak_v"], e["trough_v"]) for e in ep2])
    print("✅ 全过" if not fails else f"❌ {fails} 项不过")
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    p = argparse.ArgumentParser(prog="market", description="大盘这一层:均线 + 融资余额 + 同行背离")
    sub = p.add_subparsers(dest="cmd")
    rg = sub.add_parser("regime", help="现在大盘偏不偏弱")
    rg.add_argument("--index", default=INDEX)
    rg.add_argument("--ma", type=int, default=MA_N)
    rg.add_argument("--confirm", type=int, default=1, help="连续几天才算切换(默认 1)")
    bt = sub.add_parser("backtest", help="均线规则过去的表现")
    bt.add_argument("--index", default=INDEX)
    bt.add_argument("--since", default="2014-01-01")
    bt.add_argument("--ma", type=int, default=MA_N)
    bt.add_argument("--confirm", type=lambda s: [int(x) for x in s.split(",")], default=[1, 3, 5],
                    help="逗号分隔，如 1,3,5")
    bt.add_argument("--depth", type=float, default=25.0, help="列出跌超多少 %% 的段")
    pr = sub.add_parser("peer", help="同行两只近 10 / 20 个交易日差多少")
    pr.add_argument("a")
    pr.add_argument("b")
    pr.add_argument("--gap", type=float, default=GAP_PP)
    a = p.parse_args()
    if not a.cmd:
        print(__doc__)
        return 1
    return {"regime": cmd_regime, "backtest": cmd_backtest, "peer": cmd_peer}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
