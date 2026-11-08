#!/usr/bin/env python3
"""check_hist_earnings —— hist_earnings.py 的离线检查:手写的东财返回行,不联网。

查什么:
1. 解析东财业绩报表行(字段名、日期格式照真实返回);报告期往前推对不对
2. 累计 → 单季:一季度就是累计,其余季度 = 本期累计 − 同年上一期;缺期 → None
3. 单季同比、加速度;基期不是正数 → None
4. known = 公告日 + 1 天;年报和一季报同一天公告时取一季报;
   不偷看:加上当天以后才公告的报告期,当天的信号不变;反向自测:按报告期末算 known 的版本被抓到
5. 信号阈值 ±CAPEX_GAP(边界含等号);数太旧不给信号
6. 业绩预告 / 快报:只认比正式报告早的;快报里「上年同期」那种晚公告的行丢掉;快报对照单独算
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401
import hist_earnings as he  # noqa: E402
import system_forecast as sf  # noqa: E402

FAILS = 0


def ok(cond, what, got=""):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what + ("" if cond else f"  得到:{got!r}"[:300]))
    if not cond:
        FAILS += 1


END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
LAG = {1: 25, 2: 55, 3: 25, 4: 110}          # 一季报 4 月底、半年报 8 月底、三季报 10 月底、年报次年 4 月


def row(y, q, rev_cum, np_cum, notice=None):
    """一行东财 RPT_LICO_FN_CPD,字段和格式照真实返回(日期带 00:00:00)。"""
    p = f"{y}-{END[q]}"
    n = notice or (date.fromisoformat(p) + timedelta(days=LAG[q])).isoformat()
    return {"SECURITY_CODE": "000001", "REPORTDATE": f"{p} 00:00:00", "NOTICE_DATE": f"{n} 00:00:00",
            "UPDATE_DATE": f"{n} 00:00:00", "TOTAL_OPERATE_INCOME": rev_cum, "PARENT_NETPROFIT": np_cum,
            "YSTZ": None, "SJLTZ": None, "QDATE": f"{y}Q{q}"}


def company(single_rev, years=range(2015, 2020)):
    """single_rev(y, q) → 单季营收;净利按营收的一成;累计按年内加总。"""
    out = []
    for y in years:
        cum = 0.0
        for q in range(1, 5):
            cum += single_rev(y, q)
            out.append(row(y, q, cum, cum / 10))
    return out


def check_parse():
    print("== 解析、报告期往前推")
    ps = he.periods([row(2019, 1, 100.0, 10.0), {"REPORTDATE": "2019-06-30 00:00:00", "NOTICE_DATE": None}])
    ok(list(ps) == ["2019-03-31"] and ps["2019-03-31"]["notice"] == "2019-04-25", "没公告日的行丢掉;日期只取前 10 位", ps)
    ok(he._prev_period("2019-03-31", quarters=1) == "2018-12-31" and he._prev_period("2019-09-30", years=1) == "2018-09-30"
       and he._prev_period("2019-06-30", quarters=1) == "2019-03-31", "往前推一季跨年、推一年")


def check_single_and_yoy():
    print("== 累计 → 单季、同比、加速度")
    rows = company(lambda y, q: 100.0 * (1.2 ** (y - 2015)) * (1.5 if (y, q) == (2018, 3) else 1.0))
    t = he.quarters(rows)
    by = {r["period"]: r for r in t}
    ok(by["2016-03-31"]["rev_q"] == by["2016-03-31"]["rev_cum"], "一季度单季 = 累计")
    ok(abs(by["2016-06-30"]["rev_q"] - 120.0) < 1e-6, "二季度单季 = 上半年累计 − 一季度", by["2016-06-30"]["rev_q"])
    ok(abs(by["2017-06-30"]["rev_yoy"] - 20.0) < 1e-6, "单季同比 = 本季 ÷ 上年同季 − 1", by["2017-06-30"]["rev_yoy"])
    r = by["2018-09-30"]
    ok(abs(r["rev_yoy"] - 80.0) < 1e-6 and abs(r["rev_yoy_prev"] - 20.0) < 1e-6 and abs(r["rev_accel"] - 60.0) < 1e-6,
       "2018Q3 单季 ×1.5:同比 80%、上一季 20%、加速度 +60", r)
    ok(he.signal_at({"keys": {"x": t}}, "x", r["known"]) == 1, "加速度 +60 → +1")
    nxt = by["2018-12-31"]
    ok(nxt["rev_accel"] is not None and nxt["rev_accel"] < -5 and he.signal_at({"keys": {"x": t}}, "x", nxt["known"]) == -1,
       "下一季回到 20%:加速度 −60 → −1", nxt["rev_accel"])
    gap = [x for x in rows if x["REPORTDATE"][:10] != "2017-06-30"]
    g = {r["period"]: r for r in he.quarters(gap)}
    ok(g["2017-09-30"]["rev_q"] is None and g["2018-09-30"]["rev_yoy_prev"] is None, "缺 2017 半年报:2017Q3 单季算不出,一年后的基期也没了")
    loss = he.quarters([row(2015, 1, 100.0, -5.0), row(2016, 1, 120.0, 3.0)])
    ok(loss[-1]["np_yoy"] is None and abs(loss[-1]["rev_yoy"] - 20.0) < 1e-6, "上年亏损:净利同比 None,营收照算")


def check_known_and_no_peek():
    print("== known = 公告日 + 1;不偷看(带反向自测)")
    rows = company(lambda y, q: 100.0 * (1.1 if q in (2, 3) and y >= 2017 else 1.0) ** (y - 2015))
    for x in rows:                                  # 2018 年报和 2019 一季报同一天公告
        if x["REPORTDATE"][:10] == "2018-12-31":
            x["NOTICE_DATE"] = "2019-04-25 00:00:00"
    tb = {"keys": {"x": he.quarters(rows)}}
    r = next(x for x in tb["keys"]["x"] if x["period"] == "2018-09-30")
    ok(r["known"] == "2018-10-26" and he.value_at(tb, "x", "2018-10-25")["period"] == "2018-06-30",
       "三季报 10-25 公告:10-26 起才看得到,10-25 收盘出题还用半年报", r["known"])
    ok(he.value_at(tb, "x", "2019-04-26")["period"] == "2019-03-31", "年报和一季报同一天公告 → 取一季报")

    def peeks(build, day):
        full = build(rows)
        cut = build([x for x in rows if x["NOTICE_DATE"][:10] <= day])
        return he.value_at(full, "x", day) != he.value_at(cut, "x", day) or \
            he.signal_at(full, "x", day) != he.signal_at(cut, "x", day)

    honest = lambda rs: {"keys": {"x": he.quarters(rs)}}  # noqa: E731

    def leaky(rs):
        """反向:known 按报告期末 + 1 天(等于提前一两个月知道)。"""
        recs = [{**x, "known": (date.fromisoformat(x["period"]) + timedelta(days=1)).isoformat()} for x in he.quarters(rs)]
        return {"keys": {"x": sorted(recs, key=lambda x: (x["known"], x["period"]))}}
    days = [f"{y}-{m:02d}-15" for y in (2017, 2018, 2019) for m in (1, 2, 4, 5, 7, 8, 10, 11)]
    ok(not any(peeks(honest, d) for d in days), "正经版本:去掉当天以后才公告的,当天的值和信号都不变")
    ok(any(peeks(leaky, d) for d in days), "反向自测:按报告期末算 known 的版本被同一道检查抓到")


def check_signal_rule():
    print("== 信号阈值 = CAPEX_GAP(边界含等号)、数太旧不给")
    g = sf.CAPEX_GAP

    def tb(acc):
        return {"keys": {"x": [{"known": "2020-05-01", "period": "2020-03-31", "rev_accel": acc}]}}
    cases = [(g, 1), (g - 0.01, 0), (-g + 0.01, 0), (-g, -1)]
    got = [he.signal_at(tb(a), "x", "2020-06-01") for a, _ in cases]
    ok(got == [w for _, w in cases], f"±{g}:{got}")
    ok(he.signal_at(tb(None), "x", "2020-06-01") is None, "加速度算不出 → None")
    ok(he.signal_at(tb(10.0), "x", (date(2020, 5, 1) + timedelta(days=he.MAX_AGE_DAYS + 1)).isoformat()) is None,
       f"最近一条比出题日早 {he.MAX_AGE_DAYS} 天以上 → None")


def check_previews():
    print("== 业绩预告 / 快报")
    rows = [row(2018, 4, 400.0, 40.0, notice="2019-04-20"), row(2019, 4, 500.0, 50.0, notice="2020-04-20")]
    flash = [{"REPORT_DATE": "2019-12-31 00:00:00", "NOTICE_DATE": "2020-02-28 00:00:00",
              "TOTAL_OPERATE_INCOME": 480.0, "PARENT_NETPROFIT": 49.0},
             {"REPORT_DATE": "2018-12-31 00:00:00", "NOTICE_DATE": "2020-02-28 00:00:00",   # 快报里的上年同期行
              "TOTAL_OPERATE_INCOME": 401.0, "PARENT_NETPROFIT": 40.0}]
    pred = [{"REPORT_DATE": "2019-12-31 00:00:00", "NOTICE_DATE": "2020-01-20 00:00:00", "PREDICT_FINANCE_CODE": "004",
             "PREDICT_AMT_LOWER": 45.0, "PREDICT_AMT_UPPER": 55.0, "PREDICT_TYPE": "预增"},
            {"REPORT_DATE": "2019-12-31 00:00:00", "NOTICE_DATE": "2020-01-10 00:00:00", "PREDICT_FINANCE_CODE": "005",
             "PREDICT_AMT_LOWER": 1.0, "PREDICT_AMT_UPPER": 2.0, "PREDICT_TYPE": "预增"}]
    by = {r["period"]: r for r in he.quarters(rows, flash, pred, start="2018-01-01")}
    y19, y18 = by["2019-12-31"], by["2018-12-31"]
    ok(y19["preview"]["kind"] == "预告" and y19["known_preview"] == "2020-01-21" and y19["preview"]["np_lower"] == 45.0,
       "最早的提前公告是 1-20 的归母净利润预告(扣非那行 005 不算)", y19["preview"])
    ok(abs(y19["flash_rev_diff"] - (500 / 480 - 1) * 100) < 0.01, "快报对照单独算:正式 500 比快报 480 高 4.17%", y19["flash_rev_diff"])
    ok(y18["preview"] is None and y18["flash_rev_diff"] is None, "快报里 2018 那行是一年后才公告的上年同期,不算提前知道")
    ok(y19["known"] == "2020-04-21", "信号仍按正式报告的公告日,预告不进信号")


def main():
    check_parse()
    check_single_and_yoy()
    check_known_and_no_peek()
    check_signal_rule()
    check_previews()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
