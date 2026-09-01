#!/usr/bin/env python3
"""A 股行情速查(新浪源) —— 只在本进程内去代理,不影响 shell / Claude Code 全局环境。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/astock.py 300308              # 行情快照
    $P $LAB/tools/astock.py 300308 600519 000001  # 多只
    $P $LAB/tools/astock.py 300308 --l5         # 附五档盘口
    $P $LAB/tools/astock.py 300308 --tick       # 附最近逐笔成交(默认 20 笔)
    $P $LAB/tools/astock.py 300308 --tick=40    # 逐笔改看 40 笔
    $P $LAB/tools/astock.py 300308 --daily      # 附最近日线

说明:
- 行情用新浪源(hq.sinajs.cn)直连。东财 push2/push2his 也能连,但连续请求会被
  限流(从 200 掉到 502/连接失败),不适合当行情主源;东财报表类
  (datacenter-web,龙虎榜等)在本机稳定可用。2026-08-27 实测。
- 代理只对本脚本关闭(os.environ.pop),Claude Code / 其它命令仍走代理,互不影响。
- 五档/价量都来自新浪同一条快照;盘后看到的是残单,盘中(9:30-15:00)才实时跳动。
"""
import os
import sys

# 只清本进程的代理变量,shell 全局不受影响
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

# ── 全局 socket 超时(必须在 import akshare/efinance 之前设)──────────────
# akshare / efinance 内部的请求普遍不带 timeout。对端瞬时抖动时会**永久挂起**
# 而不是报错(2026-08-30 实测:连接全在 CLOSE_WAIT,卡 10 分钟没动)。
# requests/urllib3 未显式指定 timeout 时会退回 socket 全局默认值,所以这里设一次
# 就能兜住所有下游库。**挂起比报错更糟** —— 报错能被重试,挂起会静默卡死流水线。
import socket  # noqa: E402

socket.setdefaulttimeout(float(os.environ.get("ASTOCK_SOCKET_TIMEOUT", "30")))

import requests  # noqa: E402  (必须放在去代理之后)

SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}


def to_sina_symbol(code: str) -> str:
    """把 6 位代码补上市场前缀(sh/sz/bj)。已带前缀则原样返回。"""
    code = code.strip().lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if not (code.isdigit() and len(code) == 6):
        raise ValueError(f"非法股票代码: {code}")
    if code[0] == "6":                        # 60 主板 / 688 科创板
        return "sh" + code
    if code[0] in ("0", "3"):                 # 00/002 深主板 / 300 创业板
        return "sz" + code
    if code[0] in ("4", "8") or code[:2] == "92":  # 北交所
        return "bj" + code
    raise ValueError(f"无法判断市场: {code}")


def fetch_raw(symbol: str) -> list:
    r = requests.get(f"https://hq.sinajs.cn/list={symbol}",
                     headers=SINA_HEADERS, timeout=15)
    r.encoding = "gbk"
    payload = r.text.split('"')
    if len(payload) < 2 or not payload[1]:
        raise RuntimeError(f"{symbol} 无数据(代码错误或非交易品种)")
    return payload[1].split(",")


def parse_quote(symbol: str, f: list) -> dict:
    name, o, prev, cur, hi, lo = (
        f[0], float(f[1]), float(f[2]), float(f[3]), float(f[4]), float(f[5]))
    vol, amt, date, tm = int(f[8]), float(f[9]), f[30], f[31]
    chg = cur - prev
    pct = chg / prev * 100 if prev else 0.0
    return dict(symbol=symbol, name=name, cur=cur, prev=prev, open=o,
                high=hi, low=lo, chg=chg, pct=pct, vol=vol, amt=amt,
                date=date, time=tm)


def parse_level5(f: list) -> dict:
    """新浪快照字段:10-19 买五档(量,价),20-29 卖五档(量,价)。"""
    buys = [(float(f[11 + 2 * i]), int(f[10 + 2 * i])) for i in range(5)]   # (价,量股)
    sells = [(float(f[21 + 2 * i]), int(f[20 + 2 * i])) for i in range(5)]
    return dict(buys=buys, sells=sells)


def print_quote(q: dict) -> None:
    arrow = "▲" if q["chg"] > 0 else ("▼" if q["chg"] < 0 else "—")
    print(f"{q['name']} ({q['symbol']})  {q['date']} {q['time']}")
    print(f"  最新/收盘 : {q['cur']:.2f} 元   {arrow} "
          f"{q['chg']:+.2f} ({q['pct']:+.2f}%)")
    print(f"  今开/昨收 : {q['open']:.2f} / {q['prev']:.2f}")
    print(f"  最高/最低 : {q['high']:.2f} / {q['low']:.2f}")
    print(f"  成交量    : {q['vol']/1e4:.1f} 万股   成交额: {q['amt']/1e8:.2f} 亿元")


def print_level5(l5: dict) -> None:
    print("  ---- 五档盘口(量:手) ----")
    for i in range(4, -1, -1):   # 卖5 -> 卖1
        p, v = l5["sells"][i]
        print(f"    卖{i+1}  {p:8.2f}   {v//100:>6d}")
    for i in range(5):           # 买1 -> 买5
        p, v = l5["buys"][i]
        print(f"    买{i+1}  {p:8.2f}   {v//100:>6d}")


def show_daily(symbol: str, rows: int = 10) -> None:
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=symbol)  # 新浪源日线
    print(f"  ---- 最近 {rows} 日 ----")
    print(df.tail(rows).to_string(index=False))


def show_tick(symbol: str, rows: int = 20) -> None:
    """腾讯源逐笔成交(全天),取最后 rows 笔。"""
    import akshare as ak
    df = ak.stock_zh_a_tick_tx_js(symbol=symbol)
    print(f"  ---- 逐笔成交(最近 {rows} 笔,腾讯源)----")
    print(df.tail(rows).to_string(index=False))


def main() -> int:
    # ⚠ 同 holdings_check:不用 argparse,要手动认 --help。
    #   不认的话 --help 会落进 flags,走到「没给代码」分支打出文档但**退出码 1** ——
    #   看着像出错了,脚本化调用还会误判成失败。
    if any(a in ("-h", "--help", "help") for a in sys.argv[1:]):
        print(__doc__)
        return 0
    codes = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    want_l5 = "--l5" in flags or "--level5" in flags
    want_daily = "--daily" in flags
    # --tick 或 --tick=N
    want_tick = any(a == "--tick" or a.startswith("--tick=") for a in flags)
    tick_rows = 20
    for a in flags:
        if a.startswith("--tick="):
            try:
                tick_rows = int(a.split("=", 1)[1])
            except ValueError:
                pass
    if not codes:
        print(__doc__)
        return 1
    ok = True
    for code in codes:
        try:
            sym = to_sina_symbol(code)
            f = fetch_raw(sym)
            print_quote(parse_quote(sym, f))
            if want_l5:
                print_level5(parse_level5(f))
            if want_tick:
                show_tick(sym, tick_rows)
            if want_daily:
                show_daily(sym)
        except Exception as e:   # 单只失败不影响其余
            ok = False
            print(f"[{code}] 失败: {e}")
        print()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
