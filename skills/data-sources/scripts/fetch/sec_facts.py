#!/usr/bin/env python3
"""SEC XBRL —— 美国上市公司申报原值,全历史,免费,不需要注册 key。

    sec_facts.py capex [--out data/raw] [--tickers MSFT,AMZN,GOOGL,META]
    sec_facts.py revenue [...]

为什么要它:
    yfinance 的季度表只给 5~7 期,凑不出一条同比序列 —— 而我们要的正是同比。
    SEC 的 companyconcept 接口给的是**公司自己申报的原值**,从 2009 年至今,
    每条都带申报期间(start/end)和来源表单(10-Q / 10-K)。

    北美云厂的季度资本开支是光模块需求的直接来源 —— 是已经花掉的钱,
    不是任何人的观点。这条序列是我们判断行业景气的锚。

单季怎么还原:
    10-Q 报的常是**本财年累计**(如 MSFT 的「2025-07-01→2025-12-31」= 上半财年)。
    所以只取**期间长度 85~95 天**的那些条目当单季,累计条目直接丢掉。
    这比"用累计相减"稳:XBRL 里两种都有,挑短的那种不需要做任何算术。

    ⚠ 财年不同的公司,季度边界也不同(MSFT 财年 7 月起,Q1 是 7-9 月)。
    所以按**日历季度末**(自然月 3/6/9/12)归档,不按公司自己的 fp 标签,
    否则四家的 Q1 根本不是同一段时间,加起来没有意义。

依赖:无(标准库 urllib)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "astock-lab research (personal use)"          # SEC 要求带 User-Agent,否则 403

CIK = {
    "MSFT": "0000789019", "AMZN": "0001018724", "GOOGL": "0001652044",
    "META": "0001326801", "ORCL": "0001341439", "AAPL": "0000320193",
    "COHR": "0000820318", "LITE": "0001633978", "FN": "0001408710",
    "CRDO": "0001807794", "MRVL": "0001835632", "AVGO": "0001730168",
    "ANET": "0001596532", "CIEN": "0000936395",
}

CONCEPT = {
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues"],
}


def get(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(1.5 * (i + 1))
    return None


def quarters(concept_json) -> dict:
    """还原单季 → {日历季度末: 金额}。

    ⚠ 直接筛「期间 85~95 天」只能拿到一部分,这是 2026-09-02 实测踩到的:
    **日历年公司(GOOGL / AMZN / META)只有 Q1 的 10-Q 报三个月**,
    Q2 / Q3 的 10-Q 报的是 start 同为 1 月 1 日的**累计**(6 个月 / 9 个月),
    Q4 只在 10-K 里以全年出现。所以只筛短期间的话,这三家每年只剩 Q1 一个点,
    而且**不会报错** —— 序列看着正常,只是稀疏得没法算同比。

    两步:
      ① 直接的单季条目(85~95 天)最权威,先收下
      ② 剩下的按「同一个 start」分组,组内按 end 排序,相邻累计值相减 ——
         这一步不需要知道公司财年从哪个月开始,start 自己说明了
    """
    rows = []
    for x in (concept_json.get("units", {}).get("USD") or []):
        st, e = x.get("start"), x.get("end")
        if not st or not e:
            continue
        rows.append((st, e, float(x["val"]),
                     (dt.date.fromisoformat(e) - dt.date.fromisoformat(st)).days))

    # ⚠ 累计表必须**包含**三个月那条 —— 它同时也是"累计到第一季"。
    # 2026-09-02 踩到:把它排除在外,GOOGL / META 的 Q2 就减不出来
    # (那一组只剩 Jan1→Jun30 一条,没有前一期可减),序列停在 3 月且不报错。
    direct, cum = {}, {}
    for st, e, v, days in rows:
        if 85 <= days <= 95:
            direct[e] = v
        cum.setdefault(st, {})[e] = v

    derived = {}
    for st, ends in cum.items():
        seq = sorted(ends)
        prev_end, prev_val = st, 0.0
        # 起点:同一 start 下最短的那个累计值本身就含第一季,
        # 但第一季通常已在 direct 里;这里只用相邻差补后面的季度
        for e in seq:
            gap = (dt.date.fromisoformat(e) - dt.date.fromisoformat(prev_end)).days
            if 85 <= gap <= 95:
                derived.setdefault(e, ends[e] - prev_val)
            prev_end, prev_val = e, ends[e]

    out = {**derived, **direct}                    # direct 覆盖 derived
    return {e: v for e, v in sorted(out.items())
            if e[5:7] in ("03", "06", "09", "12")}


def fetch(kind: str, tickers: list[str], out: Path) -> int:
    from datetime import datetime
    today = dt.date.today().isoformat()
    d = out / "overseas_facts"
    d.mkdir(parents=True, exist_ok=True)
    payload, miss = {}, []
    for tk in tickers:
        cik = CIK.get(tk)
        if not cik:
            miss.append(f"{tk}(没登记 CIK)")
            continue
        # ⚠ **所有概念都取,合并**,不能取到第一个有数就停 ——
        # AMZN 早年用 PaymentsToAcquirePropertyPlantAndEquipment,后来换成
        # PaymentsToAcquireProductiveAssets。只取第一个会拿到一条停在 2017 年的
        # 序列,而且完全不报错(它确实有数据,只是十年前的)。
        merged, used, urls = {}, [], []
        for c in CONCEPT[kind]:
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{c}.json"
            j = get(url)
            if j:
                q = quarters(j)
                if q:
                    merged.update(q)               # 后面的概念覆盖前面的同期值
                    used.append(c)
                    urls.append(url)
            time.sleep(0.2)
        got = ({"concept": "+".join(used), "label": kind, "url": urls,
                "quarters": dict(sorted(merged.items()))} if merged else None)
        if got:
            payload[tk] = got
            print(f"  ✓ {tk:6s} {got['concept'][:46]:46s} {len(got['quarters'])} 个单季 (最新 {list(got['quarters'])[-1]})")
        else:
            miss.append(f"{tk}(概念都取不到)")
            print(f"  ✗ {tk:6s} 取不到")
        time.sleep(0.3)

    env = {"source": f"SEC XBRL companyconcept · {kind}",
           "url": "https://data.sec.gov/api/xbrl/companyconcept/",
           "fetched_at": datetime.now().isoformat(timespec="seconds"),
           "params": {"tickers": tickers, "concepts": CONCEPT[kind]},
           "ok": bool(payload), "rows": len(payload),
           "error": "；".join(miss) or None, "data": payload}
    p = d / f"{today}-{kind}.json"
    p.write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {p}  ({len(payload)}/{len(tickers)} 家)")
    return 0 if payload else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=sorted(CONCEPT))
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--tickers", default="MSFT,AMZN,GOOGL,META")
    a = ap.parse_args()
    return fetch(a.kind, [x.strip().upper() for x in a.tickers.split(",") if x.strip()],
                 Path(a.out))


if __name__ == "__main__":
    raise SystemExit(main())
