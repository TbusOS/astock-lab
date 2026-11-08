#!/usr/bin/env python3
"""hist_earnings —— 还原「当时能看到的」A 股公司每季业绩,给历史补考用。

它是什么:每只股票每个报告期一条:单季营收、单季归母净利润、各自同比,以及同比比上一季
快了还是慢了;按「哪天公告」排好。
没有它会怎样:补考要回答「站在 2019 年某天,这家公司业绩在加速还是减速」。报告期末那天
数还没出来(一季报 4 月底才发,年报最晚 4 月底);按报告期末算就是提前一两个月知道了答案。
为什么这么设计:
- known = 东财业绩报表的 NOTICE_DATE(首次公告日)+ 1 天。A 股定期报告多在收盘后发,
  公告日当天收盘出题时还看不到。
- 东财只给累计数(一季度、上半年、前三季、全年),单季 = 本期累计 − 同一年上一期累计。
- 单季同比用本表自己的单季数算(两期都用各自公告时的数)。东财给的累计同比(YSTZ / SJLTZ)
  是公司按调整后的上年数算的,另存一列供对照。
- 东财每个报告期只存一份数,而且第二年同期报告出来时会回头刷新这一行(UPDATE_DATE 几乎都等于
  第二年同期的公告日),少数公司的数被换成了调整后的上年数,首次披露的原数取不回来。
  UPDATE_DATE 判断不了改没改;能对照的只有提前发的快报:正式数和快报营收差超过 FLASH_TOL 的,
  记录里 flash_rev_diff 写出差多少,并列进 meta.gaps。
- 业绩预告 / 快报比正式报告早,另存 known_preview 和 preview(只记录,不进信号)。
  快报表里也有「上年同期」行,公告日比正式报告还晚,不算提前知道,丢掉。
- 信号是补考新定的(线上没有):最近一季营收同比 − 上一季营收同比,≥ CAPEX_GAP 个百分点 +1、
  ≤ −CAPEX_GAP −1、中间 0,门槛借用云厂开支那条的同一个数。
什么场景用:想法 2a(云厂开支加速 → 业绩跟着加速)的终点、2b(业绩加速 → 股价涨)的起点。
机制:quarters() 把累计数折成单季并算同比、加速度;build_table() 按股票出记录;
value_at / signal_at 只看 known ≤ 那天的记录。

用法:
    STOCK_LAB=<内部仓> python3 hist_earnings.py --fetch --raw <原始.json> --codes 300308,300502,…
    python3 hist_earnings.py --raw <原始.json> --out <表.json>
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # noqa: F401  先于 inputs(取数脚本会删代理变量)
import inputs
import system_forecast as sf

START = "2014-01-01"           # 从这个报告期起折单季(2015 起的同比要 2014 做基期)
MAX_AGE_DAYS = 200             # 最近一条比出题日早这么多天 → 当成缺数(停牌、数据断档),不给信号
GAP = sf.CAPEX_GAP
FLASH_TOL = 1.0                # 正式数和提前快报的营收差超过这么多个百分点,列进 gaps
LATE_NOTICE_DAYS = 130         # 公告日比报告期末晚这么多天:上市前补披露的,或东财拿第二年的对比数补的(known 偏晚、数可能是调整后的)

RULE = (f"补考新定(线上没有这个信号):最近一季单季营收同比 − 上一季单季营收同比,≥ {GAP} 个百分点 +1、"
        f"≤ −{GAP} −1、中间 0(门槛借用 system_forecast.CAPEX_GAP)。最近一条比出题日早 {MAX_AGE_DAYS} 天以上不给信号。"
        "业绩预告 / 快报只记录在 preview,不进信号。")
KNOWN_RULE = "known = 东财业绩报表 NOTICE_DATE(首次公告日)+ 1 天;known_preview = 比正式报告早的预告 / 快报公告日 + 1 天"


def _day(s) -> str | None:
    return str(s)[:10] if s else None


def _plus1(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _num(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _pct(cur, base) -> float | None:
    """同比百分数;基期不是正数(亏损、为 0)时同比没有意义,返回 None。"""
    if cur is None or base is None or base <= 0:
        return None
    return round((cur / base - 1) * 100, 2)


def _prev_period(p: str, years: int = 0, quarters: int = 0) -> str:
    """报告期往前推:p 形如 2025-09-30。"""
    d = date.fromisoformat(p)
    idx = d.year * 4 + (d.month - 1) // 3 - years * 4 - quarters
    y, q = divmod(idx, 4)
    m = 3 * (q + 1)
    return date(y, m, 31 if m in (3, 12) else 30).isoformat()


def _qn(p: str) -> int:
    return (date.fromisoformat(p).month - 1) // 3 + 1


# ── 累计 → 单季 ──────────────────────────────────────────────────────────────
def periods(cpd_rows: list[dict]) -> dict[str, dict]:
    """东财业绩报表行 → {报告期: {rev_cum, np_cum, notice, update, ystz, sjltz}}。没公告日的丢掉。"""
    out = {}
    for r in cpd_rows or []:
        p, n = _day(r.get("REPORTDATE")), _day(r.get("NOTICE_DATE"))
        if not p or not n:
            continue
        out[p] = {"rev_cum": _num(r.get("TOTAL_OPERATE_INCOME")), "np_cum": _num(r.get("PARENT_NETPROFIT")),
                  "notice": n, "update": _day(r.get("UPDATE_DATE")) or n,
                  "ystz": _num(r.get("YSTZ")), "sjltz": _num(r.get("SJLTZ"))}
    return out


def single(ps: dict[str, dict], p: str, field: str) -> float | None:
    """单季 = 本期累计 − 同一年上一期累计;一季度就是累计本身。缺哪一期就 None。"""
    cur = (ps.get(p) or {}).get(field)
    if cur is None:
        return None
    if _qn(p) == 1:
        return cur
    prev = (ps.get(_prev_period(p, quarters=1)) or {}).get(field)
    return None if prev is None else cur - prev


def previews(flash: list[dict], predict: list[dict], p: str, formal_notice: str) -> dict | None:
    """比正式报告早公告的快报 / 预告(净利润那行,代码 004)里最早的一条。"""
    cands = []
    for r in flash or []:
        n = _day(r.get("NOTICE_DATE"))
        if _day(r.get("REPORT_DATE")) == p and n and n < formal_notice:
            cands.append({"kind": "快报", "notice": n, "rev": _num(r.get("TOTAL_OPERATE_INCOME")),
                          "np": _num(r.get("PARENT_NETPROFIT"))})
    for r in predict or []:
        n = _day(r.get("NOTICE_DATE"))
        if (_day(r.get("REPORT_DATE")) == p and n and n < formal_notice
                and str(r.get("PREDICT_FINANCE_CODE")) == "004"):
            cands.append({"kind": "预告", "notice": n, "np_lower": _num(r.get("PREDICT_AMT_LOWER")),
                          "np_upper": _num(r.get("PREDICT_AMT_UPPER")), "type": r.get("PREDICT_TYPE")})
    return min(cands, key=lambda c: (c["notice"], c["kind"])) if cands else None


def quarters(cpd_rows, flash=None, predict=None, start: str = START) -> list[dict]:
    """一只股票的全部记录,按 (known, 报告期) 排好。"""
    ps = periods(cpd_rows)
    recs = []
    for p in sorted(ps):
        if p < start:
            continue
        row = ps[p]
        rev_q, np_q = single(ps, p, "rev_cum"), single(ps, p, "np_cum")
        b = _prev_period(p, years=1)
        rev_yoy, np_yoy = _pct(rev_q, single(ps, b, "rev_cum")), _pct(np_q, single(ps, b, "np_cum"))
        pq = _prev_period(p, quarters=1)
        pb = _prev_period(pq, years=1)
        rev_yoy_prev = _pct(single(ps, pq, "rev_cum"), single(ps, pb, "rev_cum"))
        np_yoy_prev = _pct(single(ps, pq, "np_cum"), single(ps, pb, "np_cum"))
        known = _plus1(row["notice"])
        pv = previews(flash, predict, p, row["notice"])
        fl = [r for r in flash or [] if _day(r.get("REPORT_DATE")) == p and _num(r.get("TOTAL_OPERATE_INCOME"))
              and (_day(r.get("NOTICE_DATE")) or "9") < row["notice"]]
        fdiff = None
        if fl and row["rev_cum"] is not None:      # 快报单独对照,不管它是不是最早的那条提前公告
            fdiff = round((row["rev_cum"] / _num(fl[0]["TOTAL_OPERATE_INCOME"]) - 1) * 100, 2)
        recs.append({
            "known": known, "notice": row["notice"], "update": row["update"], "period": p,
            "q": f"{p[:4]}Q{_qn(p)}", "rev_cum": row["rev_cum"], "np_cum": row["np_cum"],
            "rev_q": rev_q, "np_q": np_q, "rev_yoy": rev_yoy, "np_yoy": np_yoy,
            "rev_yoy_prev": rev_yoy_prev, "np_yoy_prev": np_yoy_prev,
            "rev_accel": None if rev_yoy is None or rev_yoy_prev is None else round(rev_yoy - rev_yoy_prev, 2),
            "np_accel": None if np_yoy is None or np_yoy_prev is None else round(np_yoy - np_yoy_prev, 2),
            "rev_yoy_cum_em": row["ystz"], "np_yoy_cum_em": row["sjltz"],
            "flash_rev_diff": fdiff,
            "known_preview": _plus1(pv["notice"]) if pv else None, "preview": pv})
    return sorted(recs, key=lambda r: (r["known"], r["period"]))


def build_table(raw: dict, codes=None) -> dict:
    codes = list(codes or raw["cpd"])
    keys, gaps = {}, []
    for c in codes:
        recs = quarters(raw["cpd"].get(c), (raw.get("flash") or {}).get(c), (raw.get("predict") or {}).get(c))
        keys[c] = recs
        if not recs:
            gaps.append(f"{c} 没有业绩报表数据")
            continue
        off = [f"{r['q']} {r['flash_rev_diff']:+.1f}%" for r in recs
               if r["flash_rev_diff"] is not None and abs(r["flash_rev_diff"]) > FLASH_TOL]
        if off:
            gaps.append(f"{c} 正式营收和提前快报差超过 {FLASH_TOL}%(东财可能存的是调整后的数):{'、'.join(off)}")
        late_n = [r["q"] for r in recs
                  if (date.fromisoformat(r["notice"]) - date.fromisoformat(r["period"])).days > LATE_NOTICE_DAYS]
        if late_n:
            gaps.append(f"{c} 有 {len(late_n)} 期公告日比报告期末晚 {LATE_NOTICE_DAYS} 天以上"
                        f"(上市前补披露,或东财用第二年的对比数补的;known 只会偏晚):{'、'.join(late_n[:8])}")
        miss = [r["q"] for r in recs if r["rev_accel"] is None and r["period"] >= "2016-01-01"]
        if miss:
            gaps.append(f"{c} 有 {len(miss)} 期算不出营收加速度(缺期或基期不是正数):{'、'.join(miss[:6])}")
    n_pv = sum(1 for v in keys.values() for r in v if r["known_preview"])
    return {"meta": {"name": "earnings", "built_at": datetime.now().isoformat(timespec="seconds"),
                     "source": "东财 datacenter RPT_LICO_FN_CPD(业绩报表)+ RPT_FCI_PERFORMANCEE(快报)"
                               "+ RPT_PUBLIC_OP_NEWPREDICT(预告)",
                     "rule": RULE, "known_rule": KNOWN_RULE, "gaps": (raw.get("errors") or []) + gaps,
                     "notes": f"金额单位元;同比、加速度是百分数 / 百分点;有预告或快报提前公告的 {n_pv} 期"},
            "keys": keys}


# ── 按日查 ───────────────────────────────────────────────────────────────────
def value_at(table: dict, key: str, day: str) -> dict | None:
    """known ≤ day 的最后一条(同一天公告年报和一季报时取报告期靠后的);没有返回 None。"""
    recs = table["keys"].get(key) or []
    i = bisect.bisect_right([r["known"] for r in recs], day)
    return recs[i - 1] if i else None


def signal_at(table: dict, key: str, day: str) -> int | None:
    r = value_at(table, key, day)
    if r is None or r.get("rev_accel") is None:
        return None
    if (date.fromisoformat(day) - date.fromisoformat(r["known"])).days > MAX_AGE_DAYS:
        return None
    return sf._band(r["rev_accel"], -GAP, GAP)


# ── 取数(在 mini 上跑) ───────────────────────────────────────────────────────
_CPD_COLS = "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,NOTICE_DATE,UPDATE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,YSTZ,SJLTZ,QDATE"
_FLASH_COLS = "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,UPDATE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,QDATE"
_PRED_COLS = "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,PREDICT_FINANCE_CODE,PREDICT_AMT_LOWER,PREDICT_AMT_UPPER,PREDICT_TYPE,IS_LATEST"


def remote_script(codes: list[str]) -> str:
    return f'''
import json, time, urllib.parse, urllib.request
CODES = {json.dumps(codes)}
REQS = {{"cpd": ("RPT_LICO_FN_CPD", {_CPD_COLS!r}, "REPORTDATE"),
         "flash": ("RPT_FCI_PERFORMANCEE", {_FLASH_COLS!r}, "NOTICE_DATE"),
         "predict": ("RPT_PUBLIC_OP_NEWPREDICT", {_PRED_COLS!r}, "NOTICE_DATE")}}
def get(rpt, cols, sort, code, page):
    q = {{"reportName": rpt, "columns": cols, "filter": '(SECURITY_CODE="%s")' % code, "pageNumber": str(page),
          "pageSize": "500", "sortColumns": sort, "sortTypes": "-1", "source": "WEB", "client": "WEB"}}
    u = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(q)
    for i in range(5):
        time.sleep(0.6 + 2 ** i - 1)
        try:
            r = urllib.request.Request(u, headers={{"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}})
            return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
        except Exception as e:
            err = e
    raise err
out = {{k: {{}} for k in REQS}}
out["errors"] = []
for c in CODES:
    for k, (rpt, cols, sort) in REQS.items():
        rows, page = [], 1
        try:
            while True:
                j = get(rpt, cols, sort, c, page)
                res = j.get("result") or {{}}
                rows += res.get("data") or []
                if page >= int(res.get("pages") or 1):
                    break
                page += 1
        except Exception as e:
            out["errors"].append("%s %s 取数失败:%s" % (c, k, type(e).__name__))
        out[k][c] = rows
print(json.dumps(out))
'''


def fetch_raw(codes: list[str], host: str = "mini") -> dict:
    p = inputs.events.ssh_python(remote_script(codes), [], host=host, env=fb_common.ORIG_ENV, timeout=1500)
    if p.returncode != 0:
        raise RuntimeError(f"mini 取东财失败,退出码 {p.returncode}:{(p.stderr or '')[-300:]}")
    return inputs.events.last_json(p.stdout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="还原当时能看到的 A 股每季业绩")
    ap.add_argument("--raw", required=True, help="原始数据 JSON(--fetch 时写到这里,否则从这里读)")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--codes", help="逗号分隔的 6 位代码(--fetch 时必填)")
    ap.add_argument("--host", default="mini")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.fetch:
        if not a.codes:
            ap.error("--fetch 要配 --codes")
        raw = fetch_raw([c.strip() for c in a.codes.split(",") if c.strip()], a.host)
        Path(a.raw).parent.mkdir(parents=True, exist_ok=True)
        Path(a.raw).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        print(f"原始数据写到 {a.raw};出错 {raw.get('errors')}")
    raw = json.loads(Path(a.raw).read_text(encoding="utf-8"))
    if a.out:
        t = build_table(raw)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
        empty = [k for k, v in t["keys"].items() if not v]
        print(f"{len(t['keys'])} 只,{sum(len(v) for v in t['keys'].values())} 条,没数据的 {empty};写到 {a.out}")
        for g in t["meta"]["gaps"]:
            print("  缺口:", g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
