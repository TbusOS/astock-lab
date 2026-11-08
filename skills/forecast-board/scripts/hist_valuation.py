#!/usr/bin/env python3
"""hist_valuation —— 估值信号的「当时能看到的」历史表,给历史补考考「估值太贵 → 之后涨得少」用。

它是什么:每只股票每个交易日一条,记那天收盘时**已经公开**的市盈率(TTM)和市净率(MRQ)。
补考在过去某个出题日问「那天估值在自己过去 3 年里算贵还是便宜」,只能用这张表里 known ≤ 出题日的记录。
没有它会怎样:估值信号只有今天的值,补考里只能记 0,「估值太贵 → 之后涨得少」这条想法考不了。
为什么这么设计:
- 源用 baostock 每日 peTTM / pbMRQ,和线上 holdings_check.valuation_percentile 同一个源,
  补考和线上看的是同一种数。
- 偷看排查(2026-11-03 在 mini 上实测,两只股票 6 个报告期):baostock 的隐含每股收益(收盘 ÷ peTTM)
  是在**公告日**那天变,不是在报告期末变 —— 中际 2023 年报和 2024 一季报都在 2024-04-22 公告,
  PE 在 04-22 跳;光迅中报 2024-08-24(周六)公告,PE 在下一个交易日 08-26 跳;业绩快报也算
  (中际 2024-02-29)。东财公告列表的日期和这些跳点一致。
- 但公告日当天几点发查不到(东财列表只有日期),很多报告是收盘后才发。所以这里保守处理:
  每股收益 / 每股净资产一变,**晚一个交易日**才算当时看得到;除权除息日(送转股)例外 ——
  那天股价和股本一起变,同一天换才不会凭空造出一天的低 PE。代价最多是晚用一天,不会偷看。
- 分位:线上是「2020 年起到今天」的正 PE 里比现值低的天数占比;补考的出题日从 2018 年起,
  用不了「2020 年起」,改成**出题日之前 750 个交易日(约 3 年,含当天)**的正 PE 窗口;
  窗口里正 PE 不到 250 个记 None。现值照线上取窗口里最后一个正 PE(当天亏损时就是之前最后一个正值),
  另给 pe_today_positive 让打分时能区分。
什么场景用:主对话统一打分时 import 本模块,对每个出题日调 signal_at;也可以重跑 main 刷新表。
机制:
    python3 hist_valuation.py --out <signals 目录>/valuation.json [--codes …] [--start 2013-01-01]
    value_at(table, code, day) → 那天(含)以前最后一条记录;signal_at(table, code, day) → −1/0/+1/None
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import date, datetime
from pathlib import Path

import fb_common  # noqa: F401  先于取数模块(market.py 会删代理变量)
import system_forecast as sf

WINDOW = 750                # 分位窗口:出题日之前多少个交易日(含当天)
MIN_POSITIVE = 250          # 窗口里正 PE 至少这么多个才给分位
CHANGE_TOL = 0.002          # 隐含每股收益相对变化超过 0.2% 才算「变了」(PE 只给到小数点后几位)
DEFAULT_START = "2013-01-01"

# 送到 mini 上跑的取数脚本:baostock 每日 收盘 / peTTM / pbMRQ(不复权)+ 除权除息日
MINI_SCRIPT = r'''
import json, sys
import baostock as bs
codes, start, end = sys.argv[1].split(","), sys.argv[2], sys.argv[3]
out = {}
bs.login()
try:
    for c in codes:
        sym = ("sh." if c[0] == "6" else "sz.") + c
        rs = bs.query_history_k_data_plus(sym, "date,close,peTTM,pbMRQ", start_date=start, end_date=end,
                                          frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        rs = bs.query_adjust_factor(code=sym, start_date=start, end_date=end)
        ex = []
        while rs.error_code == "0" and rs.next():
            ex.append(rs.get_row_data()[1])
        out[c] = {"rows": rows, "ex": ex, "error": None if rows else rs.error_msg}
finally:
    bs.logout()
print(json.dumps(out, separators=(",", ":")))
'''


def _num(s) -> float | None:
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    return x


def _known_ratio(rows: list[tuple[str, float, float | None]], ex: set[str]) -> list[float | None]:
    """rows = [(日期, 收盘, 比值)],比值是 peTTM 或 pbMRQ。返回每天「当时看得到」的
    分母(每股收益 / 每股净资产 = 收盘 ÷ 比值)。分母一变就晚一个交易日才用;除权日当天就用。"""
    implied = [None if not r or r == 0 else c / r for _, c, r in rows]
    known: list[float | None] = []
    for i, (d, _, _) in enumerate(rows):
        cur, prev = implied[i], implied[i - 1] if i else None
        changed = cur is not None and prev is not None and abs(cur / prev - 1) > CHANGE_TOL
        known.append(prev if changed and d not in ex else cur)
    return known


def build_series(raw: dict) -> tuple[list[dict], list[str]]:
    """mini 返回的一只股票 → (按日的记录, 缺口说明)。记录 = {known, pe, pb, close, pe_bs}。
    pe_bs 是 baostock 原值(没推迟),只留着核对。比值缺 / 收盘缺的那天跳过并计数。"""
    rows, bad = [], 0
    for r in raw.get("rows") or []:
        if len(r) < 4:
            bad += 1
            continue
        d, c, pe, pb = r[0], _num(r[1]), _num(r[2]), _num(r[3])
        if not d or c is None or c <= 0:
            bad += 1
            continue
        rows.append((d, c, pe, pb))
    ex = set(raw.get("ex") or [])
    eps = _known_ratio([(d, c, pe) for d, c, pe, _ in rows], ex)
    bps = _known_ratio([(d, c, pb) for d, c, _, pb in rows], ex)
    recs = []
    for (d, c, pe, _), e, b in zip(rows, eps, bps):
        recs.append({"known": d, "close": c, "pe": None if not e else round(c / e, 4),
                     "pb": None if not b else round(c / b, 4), "pe_bs": pe})
    gaps = [f"{bad} 行收盘或日期缺,跳过"] if bad else []
    return recs, gaps


def build_table(raw_all: dict, start: str, end: str) -> dict:
    keys, gaps = {}, []
    for code in sorted(raw_all):
        recs, g = build_series(raw_all[code])
        if not recs:
            gaps.append(f"{code}:baostock 没给数据({raw_all[code].get('error')})")
            continue
        keys[code] = recs
        gaps += [f"{code}:{x}" for x in g]
    return {"meta": {
        "name": "valuation", "source": "baostock query_history_k_data_plus(peTTM、pbMRQ、收盘,不复权)"
                                       " + query_adjust_factor(除权除息日);在 mini 上取",
        "rule": (f"分位 = 出题日之前 {WINDOW} 个交易日(含当天)的正 PE 里比现值低的天数占比 × 100,"
                 f"正 PE 不到 {MIN_POSITIVE} 个记 None;现值 = 窗口里最后一个正 PE(同线上);"
                 f"信号 = −_band(分位, PE_LOW={sf.PE_LOW}, PE_HIGH={sf.PE_HIGH}),同 system_forecast._sig_valuation。"
                 "线上窗口是「2020 年起」,补考出题日从 2018 年起用不了,改成滚动 3 年"),
        "built_at": datetime.now().isoformat(timespec="seconds"), "range": [start, end],
        "known_rule": "那天收盘时已公开:baostock 的每股收益在公告日变,这里再晚一个交易日才用"
                      "(公告日几点发查不到,收盘后发的当天用不了);除权除息日当天就换(股价、股本同时变)",
        "notes": ["偷看排查 2026-11-03:中际 300308、光迅 002281 的隐含每股收益跳点 = 公告日(年报、季报、业绩快报),"
                  "不是报告期末;东财公告列表日期一致(中际快报 2024-02-29、年报+一季报 2024-04-22,光迅中报 2024-08-24 周六 → 08-26 跳)",
                  "pe_bs 是 baostock 原值(没推迟一天),只用来核对"],
        "gaps": gaps}, "keys": keys}


# ── 查表 ────────────────────────────────────────────────────────────────────
def _upto(recs: list[dict], day: str) -> int:
    """recs 里 known ≤ day 的条数(recs 按 known 排好)。"""
    return bisect.bisect_right([r["known"] for r in recs], day)


def value_at(table: dict, key: str, day: str) -> dict | None:
    recs = table["keys"].get(key) or []
    n = _upto(recs, day)
    return recs[n - 1] if n else None


def pe_pctl_at(table: dict, key: str, day: str, window: int = WINDOW, min_n: int = MIN_POSITIVE) -> dict | None:
    """{pctl, pe, pe_today_positive, n} 或 None(没数据 / 正 PE 不够)。"""
    recs = table["keys"].get(key) or []
    n = _upto(recs, day)
    if not n:
        return None
    win = [r["pe"] for r in recs[max(0, n - window):n] if r["pe"] is not None and r["pe"] > 0]
    if len(win) < min_n:
        return None
    cur = win[-1]
    today = recs[n - 1]["pe"]
    return {"pctl": round(100 * sum(1 for x in win if x < cur) / len(win), 1), "pe": cur,
            "pe_today_positive": today is not None and today > 0, "n": len(win)}


def signal_at(table: dict, key: str, day: str) -> int | None:
    got = pe_pctl_at(table, key, day)
    if got is None:
        return None
    return -sf._band(got["pctl"], sf.PE_LOW, sf.PE_HIGH)     # 分位高 = 贵 → 看空,同线上


# ── 取数、命令行 ────────────────────────────────────────────────────────────
def fetch_mini(codes: list[str], start: str, end: str, host: str = "mini") -> dict:
    import inputs
    p = inputs.events.ssh_python(MINI_SCRIPT, [",".join(codes), start, end], host=host,
                                 env=fb_common.ORIG_ENV, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f"mini 上 baostock 取数失败(退出码 {p.returncode}):{(p.stderr or '').strip()[-300:]}")
    return inputs.events.last_json(p.stdout)


def default_codes() -> list[str]:
    import os
    lab = Path(os.environ.get("STOCK_LAB", "."))
    meta = json.loads((lab / "data/forecasts/history/2026-11-03/rules.json").read_text(encoding="utf-8"))["meta"]
    return list(meta["main"]) + list(meta["control"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="估值信号的当时历史表(baostock,在 mini 上取)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--codes", help="逗号分隔;不写 = 规则补考存档里的主池 + 对照组")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=date.today().isoformat())
    a = ap.parse_args(argv)
    codes = [c.strip() for c in a.codes.split(",")] if a.codes else default_codes()
    table = build_table(fetch_mini(codes, a.start, a.end), a.start, a.end)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(table, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for k, recs in table["keys"].items():
        print(f"{k}  {recs[0]['known']} ~ {recs[-1]['known']}  {len(recs)} 条")
    print("缺口:", table["meta"]["gaps"] or "无")
    print(f"写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
