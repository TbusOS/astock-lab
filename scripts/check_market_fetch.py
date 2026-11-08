#!/usr/bin/env python3
"""检查 market.fetch_daily:腾讯每次只回最后 640 根,是否因此丢整年数据。

不联网:把模块里的 _get_json 换成假服务器,按请求的起止日期生成工作日序列,
只返回最后 640 根,键名按复权方式用 day / qfqday / hfqday。

用法:
    python3 scripts/check_market_fetch.py [--market <market.py 路径>]
"""
import argparse
import datetime as dt
import importlib.util
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "skills/stock-analysis-workflow/scripts/market.py"
CAP = 640
FAILS = 0


def ok(name, cond, got=None):
    global FAILS
    print(("✅" if cond else "❌"), name, "" if cond else f"-> {got}")
    if not cond:
        FAILS += 1


VALUES = {"day": "10.0", "qfqday": "20.0", "hfqday": "30.0"}
# 假服务器模式:正常 / 缺复权键(只回 day)
MODE = {"missing_adjusted": False, "listed": None}


def fake_get_json(url, params=None):
    sym, _, s, e, n, fq = params["param"].split(",")
    key = {"qfq": "qfqday", "hfq": "hfqday"}.get(fq, "day")
    # 指数没有复权:腾讯只回 day(2026-11-02 实测 sz399006)
    if MODE["missing_adjusted"] or sym.startswith(("sz399", "sh000")):
        key = "day"
    d, end, rows = dt.date.fromisoformat(s), dt.date.fromisoformat(e), []
    while d <= end:
        if d.weekday() < 5 and (not MODE["listed"] or d.isoformat() >= MODE["listed"]):
            rows.append([d.isoformat(), "1", VALUES[key], "10.0", "10.0", "100"])
        d += dt.timedelta(days=1)
    return {"data": {sym: {key: rows[-CAP:]}}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=str(DEFAULT))
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("market_under_test", a.market)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._get_json = fake_get_json

    k = m.fetch_daily("sz300308", "2014-01-01", "2025-12-31", fq="qfq")
    years = Counter(d[:4] for d, _ in k)
    ok("2014~2025 每年都 ≥ 240 根",
       all(years[str(y)] >= 240 for y in range(2014, 2026)), dict(sorted(years.items())))
    ok("日期不重复且递增", [d for d, _ in k] == sorted(set(d for d, _ in k)))
    h = m.fetch_daily("sz300308", "2024-01-01", "2024-12-31", fq="hfq")
    ok("hfq 读 hfqday,收盘价来自 hfqday", len(h) >= 240 and {c for _, c in h} == {30.0}, h[:1])
    q = m.fetch_daily("sz300308", "2024-01-01", "2024-12-31", fq="qfq")
    ok("qfq 收盘价来自 qfqday", {c for _, c in q} == {20.0}, q[:1])
    u = m.fetch_daily("sz399006", "2014-01-01", "2025-12-31")
    ok("不复权读 day,每年都 ≥ 240 根,收盘价来自 day",
       all(Counter(d[:4] for d, _ in u)[str(y)] >= 240 for y in range(2014, 2026))
       and {c for _, c in u} == {10.0})
    s = m.fetch_daily("sh515880", "2024-01-01", "2024-12-31", fq="qfq")
    ok("ETF 的 qfq 读 qfqday", {c for _, c in s} == {20.0}, s[:1])
    ix = m.fetch_daily("sz399006", "2024-01-01", "2024-12-31", fq="qfq")
    ok("指数 qfq 只有 day 时读 day", len(ix) >= 240 and {c for _, c in ix} == {10.0}, ix[:1])

    MODE["missing_adjusted"] = True
    for fq in ("hfq", "qfq"):
        try:
            r = m.fetch_daily("sz300308", "2024-01-01", "2024-12-31", fq=fq)
            ok(f"个股缺 {fq} 键时必须抛 ValueError", False, r[:1])
        except ValueError as e:
            ok(f"个股缺 {fq} 键时抛 ValueError 且写明代码和复权方式",
               "sz300308" in str(e) and fq in str(e), str(e))
    ix = m.fetch_daily("sh000300", "2024-01-01", "2024-12-31", fq="hfq")
    ok("sh000 指数缺复权键时读 day", len(ix) >= 240 and {c for _, c in ix} == {10.0}, ix[:1])
    n = m.fetch_daily("sz300308", "2024-01-01", "2024-12-31", fq="")
    ok("fq 为空时仍读 day", len(n) >= 240 and {c for _, c in n} == {10.0}, n[:1])
    MODE["missing_adjusted"] = False

    # 上市前的窗口什么都没有,不能报错;只返回上市后的数据
    MODE["listed"] = "2016-03-01"
    for fq in ("hfq", "qfq"):
        try:
            L = m.fetch_daily("sz300502", "2010-01-01", "2025-12-31", fq=fq)
            ok(f"起始日早于上市日,{fq} 只返回上市后的数据且不报错",
               L and L[0][0] >= "2016-03-01" and {c for _, c in L} == {float(VALUES[fq + "day"])}
               and Counter(d[:4] for d, _ in L)["2017"] >= 240, L[:1])
        except ValueError as e:
            ok(f"起始日早于上市日,{fq} 不报错", False, str(e))
    MODE["listed"] = None
    print("失败" if FAILS else "全部通过", FAILS)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
