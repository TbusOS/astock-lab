#!/usr/bin/env python3
"""swing —— 做波段要看的四样客观数据,都是当天能取到的原始数,不含任何人的观点。

用法:
    swing us                         # 美股光通信/芯片同行最近一个交易日 + 5 日 + 20 日涨跌(盘中会标出来)
    swing rebound 300308 300502      # 一年高点以来每次反弹涨了多少、几天;这一次走到哪了
    swing margin 300308 300502       # 个股融资余额:最新、5 日、20 日变化、离一年高点
    swing flow 300308                # 个股当日大单 / 特大单净流入(东财的算法)
    swing all 300308 300502 600183   # 上面四样一起出
    swing --self-test                # 用假数据检查反弹统计的算法

为什么要有这一层(2026-11-11):
    下跌趋势里问「反弹到哪卖」,之前只能给均线和缺口。把一年高点以来每一次反弹
    的幅度数出来,才知道「这一次已经比以前哪一次都大」还是「才走了一半」。
    美股同行隔夜行情决定第二天 A 股怎么开盘,原来靠 yfinance,会被限流整批失败,
    这里改用腾讯美股日线(实测笔记本和国内直连两种网络都通)。

怎么算的:
    反弹 = 从一个收盘低点起,收盘涨幅达到 --min(默认 8%)才算开始;
           之后从反弹高点收盘回落达到 --min 算结束。全部按前复权收盘价。
    ⚠ 这是数过去,不是预测。趋势反转的那一次,反弹会远超历史最大值。
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
import statistics  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

socket.setdefaulttimeout(20)
_UA = {"User-Agent": "Mozilla/5.0"}
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 腾讯美股代码要带交易所后缀:.OQ 纳斯达克,.N 纽交所。后缀错了会返回十年前的旧数据
# 而不报错(CIEN.OQ 实测返回 2014 年),所以取回来要检查日期够不够新。
US_PEERS = [
    ("usNVDA.OQ", "英伟达"), ("usAVGO.OQ", "博通"), ("usMRVL.OQ", "Marvell"),
    ("usLITE.OQ", "Lumentum"), ("usCOHR.N", "Coherent"), ("usFN.N", "Fabrinet"),
    ("usAAOI.OQ", "AAOI"), ("usCRDO.OQ", "Credo"), ("usANET.N", "Arista"),
    ("usCIEN.N", "Ciena"), ("usMSFT.OQ", "微软"), ("usSOXX.OQ", "半导体 ETF(SOXX)"),
]


def say(s=""):
    print(s, flush=True)


def _get_json(url, params=None, _try=0):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA)) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        # 一次要连着取十几只,网络抖一下就会缺一行;等一秒再试一次,还不行才报
        if _try:
            raise
        time.sleep(1.0)
        return _get_json(url, None, 1)


def _sym(code):
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


# ── 取数 ────────────────────────────────────────────────────────────────────
def fetch_cn_daily(code, days=260):
    """A 股前复权日线 [(日期, 收盘)],旧→新。"""
    s = _sym(code)
    d = _get_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                  {"param": f"{s},day,,,{days},qfq"})["data"][s]
    k = d.get("qfqday") or d.get("day") or []
    return [(r[0], float(r[2])) for r in k]


def fetch_us_daily(sym, days=30):
    """美股前复权日线 [(日期, 收盘)]。盘前会多出一行最高价为 0 的占位,去掉。

    ⚠ 美股开盘以后,最后一行是当天还在变的数,不是收盘价 —— 见 us_session()。
    """
    d = _get_json("https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
                  {"param": f"{sym},day,,,{days},qfq"})["data"].get(sym) or {}
    k = d.get("qfqday") or d.get("day") or []
    return [(r[0], float(r[2])) for r in k if float(r[3]) > 0]


def ny_time(utc=None):
    """纽约当地时间(不带时区的 datetime)。

    自己算夏令时,不用 zoneinfo(Python 3.8 没有):3 月第二个周日 2:00 到
    11 月第一个周日 2:00 是夏令时(UTC-4),其余是 UTC-5。
    """
    u = utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    mar, nov = dt.datetime(u.year, 3, 8), dt.datetime(u.year, 11, 1)
    start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7, hours=7)   # 当地 2:00 = UTC 7:00
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7, hours=6)     # 当地 2:00 = UTC 6:00
    return u - dt.timedelta(hours=4 if start <= u < end else 5)


def us_session(now=None):
    """(美股常规时段是否正在交易, 纽约今天的日期)。now 是纽约当地时间。

    常规时段 = 纽约时间周一到周五 9:30~16:00。不认识美股节假日;调用方只在
    「最后一行的日期就是纽约今天」时才标盘中,节假日没有当天那一行就不会标。
    """
    n = now or ny_time()
    is_open = n.weekday() < 5 and dt.time(9, 30) <= n.time() < dt.time(16, 0)
    return is_open, n.date().isoformat()


def fetch_margin(code, n=250):
    """个股融资余额 [(日期, 融资余额元, 当日融资净买入元)],旧→新。"""
    j = _get_json(_DC, {
        "reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "DATE,SCODE,RZYE,RZJME",
        "filter": f'(SCODE="{code}")', "sortColumns": "DATE", "sortTypes": "-1",
        "pageSize": n, "pageNumber": 1})
    rows = (j.get("result") or {}).get("data") or []
    return [(r["DATE"][:10], float(r["RZYE"]), float(r["RZJME"] or 0))
            for r in reversed(rows)]


def fetch_flow(code):
    """个股最新一天资金流(东财 RPT_DMSK_TS_STOCKNEW,只有最新一天)。"""
    j = _get_json(_DC, {"reportName": "RPT_DMSK_TS_STOCKNEW", "columns": "ALL",
                        "filter": f'(SECURITY_CODE="{code}")', "pageSize": 1})
    rows = (j.get("result") or {}).get("data") or []
    return rows[0] if rows else None


# ── 反弹统计(纯计算,self-test 查的就是这块)────────────────────────────────
def rebounds(closes, min_move=0.08):
    """closes: [(日期, 收盘)]。返回 [dict(low_date, low, high_date, high, gain, days, open)]。"""
    out = []
    if len(closes) < 2:
        return out
    lo, loi = closes[0][1], 0
    hi = hii = None
    up = False
    for j in range(1, len(closes)):
        c = closes[j][1]
        if not up:
            if c < lo:
                lo, loi = c, j
            elif c >= lo * (1 + min_move):
                up, hi, hii = True, c, j
        else:
            if c > hi:
                hi, hii = c, j
            elif c <= hi * (1 - min_move):
                out.append(_rb(closes, loi, lo, hii, hi, False))
                up, lo, loi = False, c, j
    if up:
        out.append(_rb(closes, loi, lo, hii, hi, True))
    return out


def _rb(closes, loi, lo, hii, hi, is_open):
    return {"low_date": closes[loi][0], "low": lo, "high_date": closes[hii][0],
            "high": hi, "gain": hi / lo - 1, "days": hii - loi, "open": is_open}


def since_high(closes):
    """从区间内最高收盘那天起截取。"""
    i = max(range(len(closes)), key=lambda x: closes[x][1])
    return closes[i:]


# ── 输出 ────────────────────────────────────────────────────────────────────
def _pct(a, b):
    return f"{(a / b - 1) * 100:+.1f}%"


def cmd_us():
    say("## 美股同行(腾讯美股日线,前复权收盘)")
    say()
    say("| 股票 | 最新交易日 | 收盘 | 当日 | 5 日 | 20 日 |")
    say("|---|---|---|---|---|---|")
    today = dt.date.today()
    is_open, ny_today = us_session()
    bad, live = [], 0
    for sym, name in US_PEERS:
        try:
            k = fetch_us_daily(sym)
        except Exception as e:  # 一只失败不影响其它,但要说出来
            bad.append(f"{name}({type(e).__name__})")
            continue
        if len(k) < 21 or (today - dt.date.fromisoformat(k[-1][0])).days > 10:
            bad.append(f"{name}(数据太旧或太少,最新 {k[-1][0] if k else '无'})")
            continue
        c = [x[1] for x in k]
        tag = ""
        if is_open and k[-1][0] == ny_today:
            tag, live = " **盘中**", live + 1
        say(f"| {name} | {k[-1][0]}{tag} | {c[-1]:.2f} | {_pct(c[-1], c[-2])} "
            f"| {_pct(c[-1], c[-6])} | {_pct(c[-1], c[-21])} |")
    say()
    if bad:
        say("没取到:" + "、".join(bad))
        say()
    if live:
        say(f"> ⚠ 美股还没收盘:标「盘中」的 {live} 只是此刻的价(行情可能有延迟),"
            "不是收盘价,收盘后再跑一次。")
    say("> 美股某天的收盘,影响的是 A 股下一个交易日的开盘。")
    say()


def cmd_rebound(code, min_move):
    k = fetch_cn_daily(code)
    seg = since_high(k)
    rb = rebounds(seg, min_move)
    done = [r for r in rb if not r["open"]]
    say(f"## {code} 反弹统计(最高收盘 {seg[0][0]} {seg[0][1]:.2f} 以来,"
        f"共 {len(seg)} 个交易日,门槛 {min_move:.0%})")
    say()
    say("| 低点 | 低点收盘 | 高点 | 高点收盘 | 涨幅 | 交易日 | |")
    say("|---|---|---|---|---|---|---|")
    for r in rb:
        say(f"| {r['low_date']} | {r['low']:.2f} | {r['high_date']} | {r['high']:.2f} "
            f"| {r['gain'] * 100:+.0f}% | {r['days']} | {'进行中' if r['open'] else ''} |")
    say()
    if not done:
        say("已结束的反弹不足 1 次,没法比。")
        say()
        return
    g = sorted(r["gain"] for r in done)
    med, mx = statistics.median(g), g[-1]
    say(f"- 已结束 **{len(done)}** 次:涨幅中位 **{med:.0%}**,最小 {g[0]:.0%},最大 **{mx:.0%}**;"
        f"持续交易日中位 {statistics.median(r['days'] for r in done)}")
    lower = sum(1 for a, b in zip(done, done[1:]) if b["low"] < a["low"])
    if len(done) > 1:
        say(f"- 反弹结束后,下一个低点比上一个低点更低:{len(done) - 1} 次里 **{lower}** 次")
    cur = rb[-1] if rb and rb[-1]["open"] else None
    last = k[-1]
    if cur:
        beat = sum(1 for x in g if cur["gain"] > x)
        say(f"- 这一次:低点 {cur['low_date']} {cur['low']:.2f},最高收盘涨了 **{cur['gain']:.0%}**,"
            f"超过以前 {len(g)} 次里的 {beat} 次")
        say(f"- 按以前的幅度换算成价位:中位 → **{cur['low'] * (1 + med):.1f}**,"
            f"最大 → **{cur['low'] * (1 + mx):.1f}**;最新收盘 {last[0]} {last[1]:.2f}")
    else:
        lo = min(seg[-20:], key=lambda x: x[1])
        say(f"- 现在不在反弹里。近 20 日最低收盘 {lo[0]} {lo[1]:.2f},最新 {last[1]:.2f};"
            f"从那个低点按中位 / 最大幅度算是 {lo[1] * (1 + med):.1f} / {lo[1] * (1 + mx):.1f}")
    say()


def cmd_margin(code):
    m = fetch_margin(code)
    say(f"## {code} 融资余额(东财 RPTA_WEB_RZRQ_GGMX)")
    say()
    if len(m) < 21:
        say(f"只取到 {len(m)} 天,不够算。")
        say()
        return
    v = [x[1] for x in m]
    hi_i = max(range(len(v)), key=lambda i: v[i])
    net5 = sum(x[2] for x in m[-5:])
    say("| 看什么 | 数 |")
    say("|---|---|")
    say(f"| 最新 {m[-1][0]} | {v[-1] / 1e8:.1f} 亿 |")
    say(f"| 近 5 个交易日 | {_pct(v[-1], v[-6])}(净买入合计 {net5 / 1e8:+.1f} 亿) |")
    say(f"| 近 20 个交易日 | {_pct(v[-1], v[-21])} |")
    say(f"| 离 {len(v)} 日内最高({m[hi_i][0]} {v[hi_i] / 1e8:.1f} 亿) | {_pct(v[-1], v[hi_i])} |")
    say()
    say("> 融资余额还在降 = 借钱买的人还在卖;股价涨而融资余额不涨,说明涨是别的钱推的。")
    say()


def cmd_flow(code):
    f = fetch_flow(code)
    say(f"## {code} 当日资金流(东财的算法,只有最新一天)")
    say()
    if not f:
        say("没取到。")
        say()
        return
    sup = (f["SUPERDEAL_INFLOW"] - f["SUPERDEAL_OUTFLOW"]) / 1e8
    big = (f["BIGDEAL_INFLOW"] - f["BIGDEAL_OUTFLOW"]) / 1e8
    say("| 看什么 | 数 |")
    say("|---|---|")
    say(f"| 日期 / 收盘 / 涨跌 | {f['TRADE_DATE'][:10]} / {f['CLOSE_PRICE']} / {f['CHANGE_RATE']:+.2f}% |")
    say(f"| 特大单净流入 | {sup:+.1f} 亿 |")
    say(f"| 大单净流入 | {big:+.1f} 亿 |")
    say(f"| 换手率 | {f['TURNOVERRATE']:.2f}% |")
    say(f"| 东财估的主力成本:当日 / 20 日 / 60 日 | {f['PRIME_COST']:.1f} / "
        f"{f['PRIME_COST_20DAYS']:.1f} / {f['PRIME_COST_60DAYS']:.1f} |")
    say()
    say("> 「主力成本」是东财按大单成交算的估计值,不是交易所数据,只当参考。")
    say()


# ── 自测 ────────────────────────────────────────────────────────────────────
def self_test():
    def mk(vals):
        return [(f"d{i:02d}", float(v)) for i, v in enumerate(vals)]
    # 100 → 90 低点 → 99(+10%)→ 108 高点 → 97(回落 >8%,结束)→ 95 低点 → 104(进行中)
    r = rebounds(mk([100, 95, 90, 99, 108, 97, 95, 104]), 0.08)
    assert len(r) == 2, r
    assert (r[0]["low"], r[0]["high"], r[0]["days"], r[0]["open"]) == (90, 108, 2, False), r[0]
    assert abs(r[0]["gain"] - 0.2) < 1e-9
    assert (r[1]["low"], r[1]["high"], r[1]["open"]) == (95, 104, True), r[1]
    # 反向:涨幅不到门槛的不能算反弹
    assert rebounds(mk([100, 95, 90, 96, 89, 95]), 0.08) == []
    # 反向:一路涨没有回落,只有一次进行中的,不能算成已结束
    r = rebounds(mk([100, 110, 120, 130]), 0.08)
    assert len(r) == 1 and r[0]["open"], r
    # since_high 从最高收盘那天截
    assert since_high(mk([1, 5, 3, 4]))[0] == ("d01", 5.0)
    # 美股时段:周三 10:00 在交易;周三 16:30、周三 9:00、周六 10:00 都不在
    assert us_session(dt.datetime(2026, 11, 11, 10, 0)) == (True, "2026-11-11")
    for hh, mm, day in ((16, 30, 11), (9, 0, 11), (10, 0, 14)):
        assert us_session(dt.datetime(2026, 11, day, hh, mm))[0] is False, (hh, day)
    # 纽约时间换算:冬令时 UTC-5、夏令时 UTC-4;2026 年切换点是 3-08 和 11-01
    U = dt.datetime
    assert ny_time(U(2026, 11, 11, 15, 0)) == U(2026, 11, 11, 10, 0)
    assert ny_time(U(2026, 7, 1, 14, 0)) == U(2026, 7, 1, 10, 0)
    assert ny_time(U(2026, 3, 8, 6, 59)) == U(2026, 3, 8, 1, 59)
    assert ny_time(U(2026, 3, 8, 7, 0)) == U(2026, 3, 8, 3, 0)
    assert ny_time(U(2026, 11, 1, 5, 59)) == U(2026, 11, 1, 1, 59)
    assert ny_time(U(2026, 11, 1, 6, 0)) == U(2026, 11, 1, 1, 0)
    say("self-test 通过(6 组:正常两次反弹 / 不到门槛 / 一路涨 / 截取起点 / 美股时段 / 纽约时间换算)")


def main():
    ap = argparse.ArgumentParser(description="波段要看的客观数据")
    ap.add_argument("cmd", nargs="?", choices=["us", "rebound", "margin", "flow", "all"])
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--min", type=float, default=0.08, help="反弹门槛,默认 0.08")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.cmd:
        ap.error("要给子命令")
    if a.cmd != "us" and not a.codes:
        ap.error("要给股票代码")
    failed = []

    def run(label, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            failed.append(f"{label}({type(e).__name__}: {e})")

    if a.cmd in ("us", "all"):
        run("美股同行", cmd_us)
    for c in a.codes:
        if a.cmd in ("rebound", "all"):
            run(f"{c} 反弹", cmd_rebound, c, a.min)
        if a.cmd in ("margin", "all"):
            run(f"{c} 融资", cmd_margin, c)
        if a.cmd in ("flow", "all"):
            run(f"{c} 资金流", cmd_flow, c)
    if failed:
        say("**没取到的(不是没有数据,是这次取数失败):** " + ";".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
