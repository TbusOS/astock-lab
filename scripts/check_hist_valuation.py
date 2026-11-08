#!/usr/bin/env python3
"""check_hist_valuation —— 估值历史表(hist_valuation.py)的离线检查,不联网。

查三件事:
① 不偷看:公告带来的每股收益变化晚一个交易日才用;出题日以后的记录不影响那天的信号。
   反向自测:不推迟的版本、按报告期末就换的版本,同一道检查必须抓到。
② 规则和线上一致:分位 = 窗口里正 PE 比现值低的占比;分位 ≥ PE_HIGH → −1、≤ PE_LOW → +1(阈值取 system_forecast)。
③ 取数结果解析:mini 返回的形状(行、除权日、缺值)用写死的样例测。
"""
from __future__ import annotations

import copy
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401
import hist_valuation as hv  # noqa: E402
import system_forecast as sf  # noqa: E402

FAILS = 0


def ok(cond, what):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILS += 1


def _weekdays(n, start=date(2020, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _weekdays(900)
R = 600                      # 第 R 天公告,每股收益 0.5 → 1.0
X = 700                      # 第 X 天除权(10 送 10:股价、每股收益都减半)
PERIOD_END = R - 40          # 报告期末(比公告早)


def _raw():
    rows = []
    for i, d in enumerate(DAYS):
        close = 10.0 if i < X else 5.0
        eps = 0.5 if i < R else (1.0 if i < X else 0.5)
        rows.append([d, f"{close}", f"{close / eps:.6f}", f"{close / 5:.6f}"])
    rows[100][2] = ""                 # 某天 PE 缺
    rows.insert(200, ["坏行"])         # 字段不全
    return {"A": {"rows": rows, "ex": [DAYS[X]], "error": None}}


def _pe_on(table, i):
    return hv.value_at(table, "A", DAYS[i])["pe"]


def known_ok(table) -> bool:
    """公告前、公告当天都还用旧的每股收益(PE = 10 / 0.5 = 20);公告后第一个交易日才是 10。"""
    return (abs(_pe_on(table, PERIOD_END + 1) - 20) < 1e-6 and abs(_pe_on(table, R) - 20) < 1e-6
            and abs(_pe_on(table, R + 1) - 10) < 1e-6)


def check_parse():
    print("== ③ 解析 mini 返回的样例")
    t = hv.build_table(_raw(), DAYS[0], DAYS[-1])
    recs = t["keys"]["A"]
    ok(len(recs) == 900, f"字段不全的行跳过,其余 900 天都在:{len(recs)}")
    ok([r["known"] for r in recs] == sorted(r["known"] for r in recs), "按 known 排好")
    ok(any("跳过" in g for g in t["meta"]["gaps"]), f"跳过的行写进 gaps:{t['meta']['gaps']}")
    ok(recs[100]["pe"] is None and recs[100]["pe_bs"] is None, "PE 缺的那天记 None,不拿别的数顶")
    ok(set(recs[0]) == {"known", "close", "pe", "pb", "pe_bs"}, f"每条的字段:{sorted(recs[0])}")
    for k in ("name", "source", "rule", "built_at", "known_rule", "notes", "gaps"):
        ok(k in t["meta"], f"meta 有 {k}")


def check_no_peek():
    print("== ① 不偷看")
    honest = hv.build_table(_raw(), DAYS[0], DAYS[-1])
    ok(known_ok(honest), "公告当天还用旧的每股收益,下一个交易日才换")

    leak_same_day = copy.deepcopy(honest)          # 不推迟:直接用 baostock 原值
    for r in leak_same_day["keys"]["A"]:
        r["pe"] = None if r["pe_bs"] is None else float(r["pe_bs"])
    ok(not known_ok(leak_same_day), "反向自测:不推迟一天的版本被抓到")

    raw = _raw()                                   # 按报告期末就换:每股收益从期末起就是 1.0
    for row in raw["A"]["rows"]:
        if len(row) == 4:
            i = DAYS.index(row[0])
            if PERIOD_END <= i < R:
                row[2] = f"{10.0:.6f}"
    leak_period = hv.build_table(raw, DAYS[0], DAYS[-1])
    ok(not known_ok(leak_period), "反向自测:按报告期末就换的版本被抓到")

    day = DAYS[650]
    s0 = hv.signal_at(honest, "A", day)
    later = copy.deepcopy(honest)
    for r in later["keys"]["A"]:
        if r["known"] > day:
            r["pe"] = 9999.0
    ok(hv.signal_at(later, "A", day) == s0 and hv.pe_pctl_at(later, "A", day) == hv.pe_pctl_at(honest, "A", day),
       "出题日以后的 PE 全改成 9999,那天的分位和信号不变")

    ok(abs(_pe_on(honest, X) - 10) < 1e-6, "除权日当天就换(股价、每股收益同时减半,PE 不凭空掉一天)")
    no_ex = _raw()
    no_ex["A"]["ex"] = []
    ok(abs(_pe_on(hv.build_table(no_ex, DAYS[0], DAYS[-1]), X) - 5) < 1e-6,
       "反向自测:不认除权日的版本,除权那天 PE 会凭空掉一半 → 这条检查抓得到")


def _table(pes):
    days = _weekdays(len(pes))
    return {"keys": {"A": [{"known": d, "pe": p, "pb": None, "close": 1.0, "pe_bs": p} for d, p in zip(days, pes)]}}, days[-1]


def check_rule():
    print("== ② 分位和阈值跟线上一致")
    n_hi = round(sf.PE_HIGH / 100 * hv.WINDOW)            # 比现值低的个数正好让分位 = PE_HIGH
    n_lo = round(sf.PE_LOW / 100 * hv.WINDOW)

    def window(n_less):
        return [1.0] * n_less + [9.0] * (hv.WINDOW - 1 - n_less) + [5.0]

    t, d = _table(window(n_hi))
    ok(hv.pe_pctl_at(t, "A", d)["pctl"] == sf.PE_HIGH and hv.signal_at(t, "A", d) == -1,
       f"分位正好 {sf.PE_HIGH} → −1(贵)")
    t, d = _table(window(n_hi - 1))
    ok(hv.signal_at(t, "A", d) == 0, f"分位刚低于 {sf.PE_HIGH} → 0")
    t, d = _table(window(n_lo))
    ok(hv.pe_pctl_at(t, "A", d)["pctl"] == sf.PE_LOW and hv.signal_at(t, "A", d) == 1, f"分位正好 {sf.PE_LOW} → +1(便宜)")
    t, d = _table(window(n_lo + 1))
    ok(hv.signal_at(t, "A", d) == 0, f"分位刚高于 {sf.PE_LOW} → 0")
    t, d = _table([100.0] * 300 + window(n_hi))
    ok(hv.pe_pctl_at(t, "A", d)["n"] == hv.WINDOW and hv.signal_at(t, "A", d) == -1,
       f"窗口只看最近 {hv.WINDOW} 个交易日,更早的不算")
    t, d = _table([1.0] * (hv.MIN_POSITIVE - 1) + [-3.0] * 400 + [5.0])
    ok(hv.pe_pctl_at(t, "A", d)["n"] == hv.MIN_POSITIVE, "亏损(负 PE)的日子不进窗口")
    t, d = _table([1.0] * (hv.MIN_POSITIVE - 2) + [5.0])
    ok(hv.signal_at(t, "A", d) is None, f"正 PE 不到 {hv.MIN_POSITIVE} 个 → None")
    t, d = _table([1.0] * 400 + [5.0, -2.0])
    got = hv.pe_pctl_at(t, "A", d)
    ok(got["pe"] == 5.0 and got["pe_today_positive"] is False,
       "当天亏损:现值照线上取最后一个正 PE,并标出当天不是正的")
    ok(hv.value_at(t, "A", "2019-01-01") is None and hv.signal_at(t, "A", "2019-01-01") is None, "表开始以前 → None")


def main():
    check_parse()
    check_no_peek()
    check_rule()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
