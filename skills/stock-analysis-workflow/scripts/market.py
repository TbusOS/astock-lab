#!/usr/bin/env python3
"""market —— 大盘这一层:个股的领先指标管不到大盘。

用法:
    market regime                      # 现在:创业板指 vs 60 日线 + 全市场融资余额方向
    market backtest --since 2014-01-01 # 这条规则过去十几年会怎样:少跌多少、来回几次
    market peer 300308 300502          # 同行两只近 10 / 20 个交易日走势差多少
    market rescue 300308 300502        # 问「会不会救市」:指数离高点多远,和过去汇金出手那天比;
                                       # 出手后指数和这几只走了多少
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

# 汇金(国家队)公开表态增持的日子 = 公告发出的那个交易日(盘中发的,当天涨跌已含公告反应)。
# 只收「有官方公告」的;悄悄买、事后从 ETF 份额才看出来的不在这里(那种看 ETF 份额变动)。
# 出处(2026-11-16 WebSearch 核过):
#   2023-10-11 增持四大行、2023-10-23 宣布买入 ETF:财联社 https://www.cls.cn/detail/1591772
#   2024-02-06 「已扩大 ETF 增持范围」:每日经济新闻 https://www.nbd.com.cn/articles/2024-02-06/3239191.html
#   2025-04-07 盘中「已再次增持 ETF」,央行同时说给再贷款:财联社 https://www.cls.cn/detail/1995001
# 新的公开表态出来就往这里加一行。
RESCUE_DAYS = [("2023-10-11", "增持四大行股份"),
               ("2023-10-23", "宣布买入 ETF"),
               ("2024-02-06", "小盘股流动性危机后,宣布扩大 ETF 增持"),
               ("2025-04-07", "关税冲击当天(上证 -7.34%),盘中宣布再次增持 ETF")]
RESCUE_INDEX = [("sh000001", "上证"), ("sh000300", "沪深300"),
                ("sz399006", "创业板"), ("sh000688", "科创50")]
RESCUE_AFTER = (1, 5, 20, 60)


# ── 取数 ────────────────────────────────────────────────────────────────────
# 只连腾讯、东财两个国内站,一律直连。上面清环境变量关不掉 macOS 系统代理:
# urllib 照样读系统设置走 127.0.0.1:7897,腾讯握手超时 / SSL EOF(2026-11-16 笔记本 ssh 实测,
# getproxies() 回 7897;同一 URL 用空 ProxyHandler 直连 200)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with _OPENER.open(urllib.request.Request(url, headers=_UA)) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_index(sym):
    """指数代码:创业板指 sz399xxx、上证系 sh000xxx。"""
    return sym.startswith(("sz399", "sh000"))


def fetch_daily(sym, start, end=None, fq=""):
    """腾讯日线 [(日期, 收盘)]。fq 取 "" / "qfq" / "hfq"(不复权 / 前复权 / 后复权)。
    腾讯每次最多回约 640 根(前复权、后复权都一样),所以按两年一段取;
    一次取 5 年只会回最后 640 根,前面整年静默丢失(2026-11-02 实测)。
    复权键缺失/为空时:个股若同一响应里有不复权 day 数据,抛 ValueError(不能顶替,
    结算比较会错);day 也没有就当这段没数据(上市前),继续取下一段;
    指数(sz399xxx / sh000xxx)本来就没有复权,腾讯在 fq=qfq 时只回 day(2026-11-02
    本机实测 sz399006),此时读 day。ETF 实测回 qfqday,按个股处理。
    只能走 https —— http 回 302(2026-10-29 本机实测)。"""
    end = end or dt.date.today().isoformat()
    key = {"qfq": "qfqday", "hfq": "hfqday"}.get(fq, "day")
    out, y0 = [], int(start[:4])
    while y0 <= int(end[:4]):
        s = max(start, f"{y0}-01-01")
        e = min(end, f"{y0 + 1}-12-31")
        d = _get_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                      {"param": f"{sym},day,{s},{e},640,{fq}"})["data"]
        if isinstance(d, dict):
            bars = d.get(sym) or {}
            k = bars.get(key) or []
            if not k and key != "day":
                day = bars.get("day") or []
                if day and not _is_index(sym):
                    # 有不复权数据却没有复权数据:最危险的情况,不能顶替
                    raise ValueError(f"{sym} 没有 {fq} 日线(缺 {key}),"
                                     "不用不复权数据顶替")
                k = day   # 指数不复权,复权前后相同;都空 = 这段没数据(上市前)
            out += [(x[0], float(x[2])) for x in k]
        y0 += 2
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


def off_high(rows, i, win):
    """rows[i] 收盘离它之前 win 个交易日(含当天)最高收盘多远 → (最高日, 最高价, 百分比)。"""
    w = rows[max(0, i - win + 1):i + 1]
    hd, hv = max(w, key=lambda x: x[1])
    return hd, hv, (rows[i][1] / hv - 1) * 100


def after_event(rows, day, ns=RESCUE_AFTER):
    """从 day 收盘起算,之后 n 个交易日的涨跌 % 和之后最大 n 天内的最低点。
    day 不是交易日(或没数据)返回 None;往后不够 n 天的那一项是 None。"""
    ds = [d for d, _ in rows]
    if day not in ds:
        return None
    i = ds.index(day)
    base = rows[i][1]
    out = {n: ((rows[i + n][1] / base - 1) * 100 if i + n < len(rows) else None) for n in ns}
    tail = rows[i:i + max(ns) + 1]
    ld, lv = min(tail, key=lambda x: x[1])
    return {"day_chg": (base / rows[i - 1][1] - 1) * 100 if i > 0 else None,
            "after": out, "low_day": ld, "low": (lv / base - 1) * 100}


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


def rescue_lines(codes=(), title=None):
    """「会不会救市」的原始数,返回 markdown 行。只出数,不出概率 —— 概率要结合当天消息判断。"""
    sym = lambda c: c if c[:2] in ("sh", "sz") else ("sh" if c.startswith(("6", "9")) else "sz") + c
    items = list(RESCUE_INDEX) + [(sym(c), c) for c in codes]
    # 最早那次之前要够 60 个交易日算「距 60 日高」,往前多取 150 个自然日
    start = (dt.date.fromisoformat(min(d for d, _ in RESCUE_DAYS)) - dt.timedelta(days=150)).isoformat()
    L = [title or "## 救市对照:现在离高点多远,过去汇金公开出手那天离高点多远", "",
         "| 标的 | 最新收盘 | 当天 | 距 60 日最高 | 距 250 日最高 |"
         + "".join(f" {d} 那天距 60 日高 |" for d, _ in RESCUE_DAYS),
         "|---|---|---|---|---|" + "---|" * len(RESCUE_DAYS)]
    after, fails, lastd = [], [], {}
    for s, nm in items:
        try:
            r = fetch_daily(s, start, fq="qfq")
        except Exception as e:
            fails.append(f"{nm}({s})取数失败 {type(e).__name__}")
            continue
        if len(r) < 2:
            fails.append(f"{nm}({s})没有日线")
            continue
        i = len(r) - 1
        lastd[nm] = r[-1][0]
        _, _, p60 = off_high(r, i, 60)
        hd, hv, p250 = off_high(r, i, 250)
        cells = []
        ds = [d for d, _ in r]
        for d, _ in RESCUE_DAYS:
            cells.append(f" {off_high(r, ds.index(d), 60)[2]:+.1f}% |" if d in ds else " — |")
        L.append(f"| {nm} | {r[-1][0]} {r[-1][1]:,.2f} | {(r[-1][1] / r[-2][1] - 1) * 100:+.2f}% | "
                 f"{p60:+.1f}% | {p250:+.1f}%({hd} {hv:,.2f}) |" + "".join(cells))
        for d, _ in RESCUE_DAYS:
            ev = after_event(r, d)
            if ev:
                after.append((nm, d, ev))
    L += ["", "### 汇金公开出手之后(从出手日收盘算)", "",
          "| 标的 | 出手日 | 当天 |" + "".join(f" {n} 日后 |" for n in RESCUE_AFTER) + " 之后最低 |",
          "|---|---|---|" + "---|" * len(RESCUE_AFTER) + "---|"]
    f = lambda v: "—" if v is None else f"{v:+.1f}%"
    for nm, d, ev in after:
        L.append(f"| {nm} | {d} | {f(ev['day_chg'])} |"
                 + "".join(f" {f(ev['after'][n])} |" for n in RESCUE_AFTER)
                 + f" {ev['low_day']} {ev['low']:+.1f}% |")
    L += ["", "出手日的背景:" + ";".join(f"{d} {why}" for d, why in RESCUE_DAYS) + "。",
          "",
          "读法:",
          f"- 样本只有 {len(RESCUE_DAYS)} 次;对比时看「当天」那列和距高点那列,分清是急跌后出手还是阴跌中出手。",
          "- 汇金买的是宽基 ETF:指数被托住时,个股可以照跌(2026-11-16 上证 -0.87%、银行创新高,中际 -12.3%)。",
          "- 出手后个股涨多少,要看当时这个行业自己的需求在往哪走,别把出手后的涨幅算到救市头上。",
          "- 有没有人悄悄在买,看第二天早上财联社电报里的「股票型 ETF 份额变动」(510300 / 510330 份额增加才算);"
          "尾盘成交放大分不出是在买还是在卖。"]
    if lastd and len(set(lastd.values())) > 1:
        newest = max(lastd.values())
        old = [f"{nm} {d}" for nm, d in lastd.items() if d < newest]
        # 2026-11-16 收盘后实测:个股前复权按日期段取,当天那根还没出,指数已经有了
        L += ["", f"⚠ 最新一根日期不一样(最新 {newest}):" + "、".join(old)
              + " —— 这几只的「最新收盘 / 距高点」是旧一天的,当天的看 astock 快照。"]
    if fails:
        L += ["", "取数失败:" + ";".join(fails)]
    return L + [""]


# ── 命令 ────────────────────────────────────────────────────────────────────
def cmd_rescue(a):
    print("\n".join(rescue_lines(a.codes)))
    return 0


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
    # 救市对照:100 涨到 120 再跌到 90
    rr = [("d%02d" % i, float(v)) for i, v in enumerate(list(range(100, 121)) + list(range(119, 89, -1)))]
    hd, hv, p = off_high(rr, len(rr) - 1, 60)
    ok("距高点:120 → 90 是 -25%", hd == "d20" and hv == 120 and abs(p + 25) < 1e-9, (hd, hv, p))
    _, hv5, _ = off_high(rr, len(rr) - 1, 5)
    ok("距高点:窗口只看最近 5 天,高点是 94", hv5 == 94, hv5)
    ev = after_event(rr, "d20", (1, 5))
    ok("出手后:120 起 1 日 119、5 日 115", ev and abs(ev["after"][1] - (119 / 120 - 1) * 100) < 1e-9
       and abs(ev["after"][5] - (115 / 120 - 1) * 100) < 1e-9, ev)
    ok("出手后:往后不够天数的那项是 None", after_event(rr, "d48", (1, 5))["after"][5] is None)
    ok("出手后:日期不在数据里返回 None", after_event(rr, "2099-01-01") is None)
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
    rs = sub.add_parser("rescue", help="会不会救市:指数离高点多远,和过去汇金出手那天比")
    rs.add_argument("codes", nargs="*", help="再加几只个股看出手后怎么走,如 300308 300502")
    a = p.parse_args()
    if not a.cmd:
        print(__doc__)
        return 1
    return {"regime": cmd_regime, "backtest": cmd_backtest, "peer": cmd_peer,
            "rescue": cmd_rescue}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
