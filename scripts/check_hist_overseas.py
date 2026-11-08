#!/usr/bin/env python3
"""check_hist_overseas —— 美股同行历史表(hist_overseas.py)的离线检查,不联网。

查三件事:
① 不偷看:美股某天的收盘,A 股要到下一个自然日才能用;出题日以后的价格不影响那天的信号。
   反向自测:known 写成美股交易日当天的版本,同一道检查必须抓到。
② 规则和阈值:篮子 10 日涨跌 = 各家简单涨幅的平均;±PEER_GAP 边界(取 system_forecast)。
③ 解析:收盘字典 → 表的形状;家数不够记 None;单日大涨跌写进 notes。
"""
from __future__ import annotations

import copy
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401
import hist_overseas as ho  # noqa: E402
import system_forecast as sf  # noqa: E402

FAILS = 0


def ok(cond, what):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILS += 1


def _weekdays(n, start=date(2021, 3, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = _weekdays(30)


def _closes(last_ret=0.0):
    """四家前 19 天都是 100;第 20 天起涨到 100 × (1 + last_ret)。"""
    return {t: {d: (100.0 if i < 20 else 100.0 * (1 + last_ret)) for i, d in enumerate(DAYS)} for t in ho.TICKERS}


def check_parse():
    print("== ③ 解析")
    t = ho.build_table(_closes(0.05), DAYS[0], DAYS[-1])
    recs = t["keys"][ho.KEY]
    ok(len(recs) == len(DAYS), f"每个美股交易日一条:{len(recs)}")
    ok(all(r["ret10"] is None for r in recs[:ho.WINDOW]), f"前 {ho.WINDOW} 天不够算 10 日涨跌 → None")
    ok(abs(recs[20]["ret10"] - 5.0) < 1e-9 and recs[20]["n"] == 4, f"四家都涨 5% → 篮子 10 日涨跌 5.0:{recs[20]['ret10']}")
    ok(abs(recs[20]["basket"] - 105.0) < 1e-6, f"等权点位从 100 起,涨 5% 后 105:{recs[20]['basket']}")
    c = _closes(0.05)
    for t_ in ho.TICKERS[1:]:
        del c[t_][DAYS[20 - ho.WINDOW]]
    t1 = ho.build_table(c, DAYS[0], DAYS[-1])["keys"][ho.KEY][20]
    ok(t1["ret10"] is None and t1["n"] == 1, f"10 天前只有 1 家有数(少于 {ho.MIN_MEMBERS})→ None")
    c = _closes(0.0)
    c["AAOI"][DAYS[25]] = 160.0
    notes = ho.build_table(c, DAYS[0], DAYS[-1])["meta"]["notes"]
    ok(any("AAOI" in n and DAYS[25] in n for n in notes), "单日涨跌超过 40% 写进 notes(数据错或代码换公司会这样)")
    c = _closes(0.0)
    del c["LITE"]
    ok(any("LITE" in g for g in ho.build_table(c, DAYS[0], DAYS[-1])["meta"]["gaps"]), "某家没取到写进 gaps")


def check_no_peek():
    print("== ① 不偷看")
    t = ho.build_table(_closes(0.05), DAYS[0], DAYS[-1])

    def same_day_hidden(table):
        """美股 DAYS[20] 那天的数,A 股同一个日期还看不到,下一个自然日才看得到。"""
        d = DAYS[20]
        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        return ho.value_at(table, ho.KEY, d)["us_date"] == DAYS[19] and ho.value_at(table, ho.KEY, nxt)["us_date"] == d

    ok(same_day_hidden(t), "known = 美股交易日 + 1 天")
    leak = copy.deepcopy(t)
    for r in leak["keys"][ho.KEY]:
        r["known"] = r["us_date"]
    ok(not same_day_hidden(leak), "反向自测:known 写成美股当天的版本被抓到")

    day = DAYS[22]
    s0 = ho.signal_at(t, ho.KEY, day)
    c = _closes(0.05)
    for tk in ho.TICKERS:
        for d in DAYS:
            if d >= day:
                c[tk][d] = 1.0
    ok(ho.signal_at(ho.build_table(c, DAYS[0], DAYS[-1]), ho.KEY, day) == s0,
       "出题日当天(美股还没收盘到 A 股能用)及以后的美股价格全改掉,那天的信号不变")


def check_rule():
    print("== ② 阈值跟 system_forecast 一致")
    g = sf.PEER_GAP / 100

    def sig(r):
        t = ho.build_table(_closes(r), DAYS[0], DAYS[-1])
        return ho.signal_at(t, ho.KEY, ho.known_of(DAYS[20]))

    ok(sig(g) == 1, f"10 日涨 {sf.PEER_GAP}% 正好 → +1")
    ok(sig(g - 0.0001) == 0, f"差一点不到 {sf.PEER_GAP}% → 0")
    ok(sig(-g) == -1, f"跌 {sf.PEER_GAP}% → −1")
    ok(sig(-g + 0.0001) == 0, f"跌得差一点不到 {sf.PEER_GAP}% → 0")
    c = _closes(0.0)
    for i, d in enumerate(DAYS):
        c["COHR"][d] = 100.0 if i < 20 else 130.0           # 一家 +30%,三家不动 → 平均 +7.5%
    r = ho.value_at(ho.build_table(c, DAYS[0], DAYS[-1]), ho.KEY, ho.known_of(DAYS[20]))
    ok(abs(r["ret10"] - 7.5) < 1e-9, f"篮子涨跌是各家涨幅的平均,不是加起来:{r['ret10']}")
    ok(ho.signal_at({"keys": {ho.KEY: []}}, ho.KEY, DAYS[5]) is None, "表是空的 → None")


def main():
    check_parse()
    check_no_peek()
    check_rule()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
