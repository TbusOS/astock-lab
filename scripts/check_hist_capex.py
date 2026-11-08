#!/usr/bin/env python3
"""check_hist_capex —— hist_capex.py 的离线检查:合成数据 + 假的 SEC 返回,不联网。

查什么:
1. 解析 companyconcept 的返回格式(字段名照 SEC 真实返回写的 fixture)
2. 同一期多次披露:只认当天以前最晚的那份;几个标签合在一起不断档
3. known = filed + 1 天;不偷看:加上当天以后才交的文件(新季度、改旧季度),当天的信号不变;
   反向自测:一个按季度末算 known 的版本,同一道检查必须抓到
4. 信号阈值和 system_forecast 一致(±CAPEX_GAP,边界含等号)
5. 早年没有融资租赁标签时退回现金购建口径,两季口径一致;数太旧不给信号
"""
from __future__ import annotations

import copy
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401
import hist_capex as hc  # noqa: E402
import system_forecast as sf  # noqa: E402

FAILS = 0


def ok(cond, what, got=""):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what + ("" if cond else f"  得到:{got!r}"[:300]))
    if not cond:
        FAILS += 1


QEND = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
QSTART = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}


def _fact(y, q, val, filed, form="10-Q", accn=None):
    """一条单季事实,字段照 SEC companyconcept 的 units.USD[i]。"""
    return {"start": f"{y}-{QSTART[q]}", "end": f"{y}-{QEND[q]}", "val": val, "accn": accn or f"000-{y}-{q}",
            "fy": y, "fp": f"Q{q}", "form": form, "filed": filed}


def _filed(y, q, lag=30):
    return (date.fromisoformat(f"{y}-{QEND[q]}") + timedelta(days=lag)).isoformat()


def _concept(facts):
    return {"cik": 1, "taxonomy": "us-gaap", "tag": "X", "label": "x", "entityName": "Fake", "units": {"USD": facts}}


def world(growth=None, lease_from=2017):
    """四家公司 2015Q1~2019Q4 单季现金购建,每季末后 30 天披露;MSFT / META 从 lease_from 年起有融资租赁。
    growth(y, q) → 当季比上年同季的倍数,用来造出已知的同比。"""
    growth = growth or (lambda y, q: 1.2)
    con = {}
    for i, tk in enumerate(hc.TICKERS):
        base = {(2015, q): 10.0 + i for q in range(1, 5)}
        cash, lease = [], []
        for y in range(2015, 2020):
            for q in range(1, 5):
                v = base[(y, q)] if y == 2015 else base[(y - 1, q)] * growth(y, q)
                base[(y, q)] = v
                cash.append(_fact(y, q, v * 1e9, _filed(y, q)))
                if tk in hc.capex.LEASE_TAGS and y >= lease_from:
                    lease.append(_fact(y, q, 1e9, _filed(y, q)))
        con[tk] = {"PaymentsToAcquirePropertyPlantAndEquipment": _concept(cash)}
        if tk in hc.capex.LEASE_TAGS:
            con[tk][hc.capex.LEASE_TAGS[tk]] = _concept(lease)
    return {"concepts": con}


def check_parse_and_vintage():
    print("== 解析 SEC 返回、同一期取最晚披露")
    js = _concept([_fact(2018, 1, 5e9, "2018-04-27"), _fact(2018, 1, 6e9, "2019-04-26", accn="later"),
                   {"start": "2018-01-01", "end": "2018-03-31", "val": None, "filed": "2018-05-01"}])
    fs = hc.facts(js)
    ok(len(fs) == 2 and fs[0]["accn"] == "000-2018-1" and fs[0]["form"] == "10-Q", "缺值的丢掉,保留 filed / form / accn", fs)
    ok(hc.vintage(fs, "2018-12-31") == [("2018-01-01", "2018-03-31", 5e9)], "改数之前:用第一次披露的数")
    ok(hc.vintage(fs, "2019-04-26") == [("2018-01-01", "2018-03-31", 6e9)], "改数那天起:换成改过的数")
    ok(hc.vintage(fs, "2018-04-26") == [], "第一次披露之前:没有这个数")
    raw_tk = {"PaymentsToAcquirePropertyPlantAndEquipment": _concept([_fact(2017, 1, 1e9, "2017-04-28")]),
              "PaymentsForProceedsFromProductiveAssets": _concept([_fact(2017, 2, 2e9, "2017-07-28")]),
              "PaymentsToAcquireProductiveAssets": _concept([_fact(2017, 3, 3e9, "2017-10-27")])}
    c = hc.company_at(raw_tk, "AMZN", "2017-12-31")
    ok(sorted(c["q"]) == ["2017-03-31", "2017-06-30", "2017-09-30"], "三个标签合起来,季度不断档", c and sorted(c["q"]))
    ok(hc.company_at(raw_tk, "AMZN", "2017-01-01") is None, "那天一个数都没有 → None")


def check_known_and_no_peek():
    print("== known = filed + 1 天;不偷看(带反向自测)")
    raw = world()
    t = hc.build_table(raw)
    recs = t["keys"][hc.KEY]
    r = next(x for x in recs if x["q"] == "2018-06-30")
    ok(r["filed"] == _filed(2018, 2) and r["known"] == (date.fromisoformat(r["filed"]) + timedelta(days=1)).isoformat(),
       "known 是 filed 的第二天", r)
    ok(hc.value_at(t, hc.KEY, r["filed"])["q"] == "2018-03-31", "filed 当天(美东)出题还看不到这一季")
    want_t = (r["sum_total_b"] / (r["sum_cash_b"] / 1.2 + (r["sum_total_b"] - r["sum_cash_b"])) - 1) * 100
    ok(abs(r["yoy_cash"] - 20.0) < 0.01 and abs(r["yoy_total"] - want_t) < 0.05 and r["basis"] == "total",
       "现金购建每季比上年 ×1.2 → 同比 20%;租赁每季固定,含租赁的同比按同样的数算得出", (r["yoy_cash"], r["yoy_total"], want_t))

    def trunc(rw, day):
        """只留 day 以前交的文件 —— 当天真能看到的世界。"""
        out = copy.deepcopy(rw)
        for tk in out["concepts"].values():
            for js in tk.values():
                js["units"]["USD"] = [x for x in js["units"]["USD"] if x["filed"] <= day]
        return out

    def peeks(build, day):
        return hc.signal_at(build(raw), hc.KEY, day) != hc.signal_at(build(trunc(raw, day)), hc.KEY, day) or \
            hc.value_at(build(raw), hc.KEY, day) != hc.value_at(build(trunc(raw, day)), hc.KEY, day)

    days = [f"{y}-{m:02d}-15" for y in (2017, 2018, 2019) for m in (1, 4, 7, 10)]
    ok(not any(peeks(hc.build_table, d) for d in days), "正经版本:去掉当天以后的文件,当天的值和信号都不变")
    restated = copy.deepcopy(raw)
    restated["concepts"]["GOOGL"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"].append(
        _fact(2018, 1, 99e9, "2019-06-01", form="10-K", accn="restate"))
    ok(hc.value_at(hc.build_table(restated), hc.KEY, "2019-05-31") == hc.value_at(t, hc.KEY, "2019-05-31")
       and hc.value_at(hc.build_table(restated), hc.KEY, "2019-06-03") != hc.value_at(t, hc.KEY, "2019-06-03"),
       "后来改旧季度的数:改之前不受影响,改之后才变")

    def leaky(rw):
        """反向:known 按季度末 + 1 天算(等于提前一个月知道)。"""
        tb = hc.build_table(rw)
        tb["keys"][hc.KEY] = sorted(({**x, "known": (date.fromisoformat(x["q"]) + timedelta(days=1)).isoformat()}
                                     for x in tb["keys"][hc.KEY]), key=lambda x: x["known"])
        return tb
    ok(any(peeks(leaky, d) for d in days), "反向自测:按季度末算 known 的版本被同一道检查抓到")


def check_signal_rule():
    print("== 信号阈值 = system_forecast.CAPEX_GAP(边界含等号)")
    g = sf.CAPEX_GAP

    def tb(acc):
        return {"keys": {hc.KEY: [{"known": "2020-01-01", "accel": acc}]}}
    cases = [(g, 1), (g - 0.01, 0), (0.0, 0), (-g + 0.01, 0), (-g, -1), (-30.0, -1), (30.0, 1)]
    got = [(a, hc.signal_at(tb(a), hc.KEY, "2020-02-01")) for a, _ in cases]
    ok(all(s == want for (_, want), (_, s) in zip(cases, got)), f"±{g} 边界:{got}")
    ok(hc.signal_at(tb(None), hc.KEY, "2020-02-01") is None, "加速度算不出 → None")
    ok(hc.signal_at(tb(10.0), hc.KEY, "2019-12-31") is None, "出题日早于第一条 → None")
    ok(hc.signal_at(tb(10.0), hc.KEY, (date(2020, 1, 1) + timedelta(days=hc.MAX_AGE_DAYS + 1)).isoformat()) is None,
       f"最近一条比出题日早 {hc.MAX_AGE_DAYS} 天以上 → None(当成缺数)")
    acc = (lambda y, q: 1.5 if (y, q) == (2018, 3) else 1.2)
    t = hc.build_table(world(acc))
    r = next(x for x in t["keys"][hc.KEY] if x["q"] == "2018-09-30")
    ok(r["accel"] > g and hc.signal_at(t, hc.KEY, r["known"]) == 1, "合成数据:当季同比从约 20% 跳到约 50% → +1", r)


def check_basis():
    print("== 早年没有融资租赁:两季都退回现金购建口径")
    t = hc.build_table(world(lease_from=2018))
    by = {x["q"]: x for x in t["keys"][hc.KEY]}
    early, mid, late = by["2017-06-30"], by["2018-03-31"], by["2019-06-30"]
    ok(early["basis"] == "cash" and early["yoy_total"] is None and early["accel"] is not None,
       "2017 年还没有租赁数:含租赁的同比为空,用现金购建的同比算加速度", early)
    ok(mid["basis"] == "cash", "2018Q1 有租赁但上年同季没有:两季口径凑不齐含租赁的 → 都用现金购建", mid)
    ok(late["basis"] == "total", "2019 年两季含租赁的同比都有 → 用含租赁的(和线上一样)", late)
    ok(any("融资租赁缺" in gp for gp in t["meta"]["gaps"]), "缺租赁的季度写进 meta.gaps")


def main():
    check_parse_and_vintage()
    check_known_and_no_peek()
    check_signal_rule()
    check_basis()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
