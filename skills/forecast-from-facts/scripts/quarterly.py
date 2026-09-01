#!/usr/bin/env python3
"""单季还原 + 领先量 —— 只用公司自己报表里的事实,不含任何人的预测。

    quarterly.py <code> [--raw data/raw] [--since 2023] [--json out.json]

为什么要自己还原单季:
    A 股三大表报的是**累计数**(一季报=Q1,半年报=H1,三季报=Q1-3,年报=全年)。
    直接看累计数会把季节性和拐点全抹平 —— 2026H1 营收 209 亿看不出
    Q2 单季 125.7 亿比 Q1 的 83.4 亿跳了 51%。拐点只在单季数据里。

为什么盯存货和预付款:
    这两项是**钱已经花出去了**,是行为不是说法。对制造业,
    期末存货是下一两个季度出货的物质基础;预付款是抢上游产能的证据。
    两者都领先营收,而且是财报里的硬数,不需要相信任何人。

    ⚠ 这条关系**不是普适规律**,是每家公司自己的历史关系,必须逐只算出来看。
    周转天数一变它就失效 —— 所以脚本输出的是**历史比值的分布**,
    不是一个拍出来的系数。用的时候看分布宽不宽,宽就说明这条链在这只票上不可靠。

依赖:无(只读 fetch_all 落下来的 json)。
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# 利润表:要绝对值,不要比率
PL = ["营业总收入", "营业收入", "营业成本", "销售费用", "管理费用", "研发费用",
      "财务费用", "营业利润", "利润总额", "净利润", "归属于母公司所有者的净利润"]
# 资产负债表:时点数,不做差分
BS = ["存货", "预付款项", "合同负债", "应收票据及应收账款", "在建工程",
      "固定资产", "货币资金", "应付票据及应付账款"]
CF = ["经营活动产生的现金流量净额", "购建固定资产、无形资产和其他长期资产支付的现金"]

QEND = {"0331": "Q1", "0630": "Q2", "0930": "Q3", "1231": "Q4"}
PREV = {"0630": "0331", "0930": "0630", "1231": "0930"}


def num(v):
    """报表里空值有好几种写法(''、'--'、None、'nan'),统一成 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load(raw: Path, code: str, name: str):
    """取最新一次抓到的那份。同名文件按日期排,取最后一个。"""
    files = sorted((raw / "financials" / code).glob(f"*-{name}.json"))
    for f in reversed(files):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for env in j.values():
            if env.get("ok") and env.get("data"):
                return env["data"], f.name, env.get("url", "")
    return None, None, ""


def by_period(rows, keys):
    """报告日 → {字段: 值}。报告日形如 20260630。"""
    out = {}
    for r in rows or []:
        d = str(r.get("报告日") or r.get("报告期") or "").strip()[:8]
        if len(d) != 8 or d[4:8] not in QEND:
            continue
        out[d] = {k: num(r.get(k)) for k in keys if k in r}
    return out


def to_single_quarter(cum: dict, keys) -> dict:
    """累计 → 单季。Q1 直接用;其余减去上一期累计。

    上一期缺失时**返回 None 而不是拿累计数顶替** —— 顶替出来的数看着正常
    (量级对、正负对),但它是半年数混在季度序列里,会把后面所有比值算歪。
    """
    out = {}
    for d in sorted(cum):
        q = d[4:8]
        if q == "0331":
            out[d] = dict(cum[d])
            continue
        pk = d[:4] + PREV[q]
        if pk not in cum:
            out[d] = {k: None for k in keys}
            continue
        out[d] = {k: (cum[d].get(k) - cum[pk].get(k))
                  if cum[d].get(k) is not None and cum[pk].get(k) is not None else None
                  for k in keys}
    return out


def build(raw: Path, code: str, since: str = "2023") -> dict:
    pl_rows, pl_file, pl_url = load(raw, code, "利润表")
    bs_rows, bs_file, bs_url = load(raw, code, "资产负债表")
    cf_rows, cf_file, cf_url = load(raw, code, "现金流量表")
    if not pl_rows or not bs_rows:
        raise SystemExit(f"{code}: 缺利润表或资产负债表。先跑\n"
                         f"  tools/fetch_all.py --codes {code} --group statements")

    pl_c, bs_p = by_period(pl_rows, PL), by_period(bs_rows, BS)
    cf_c = by_period(cf_rows, CF) if cf_rows else {}
    pl_q = to_single_quarter(pl_c, PL)
    cf_q = to_single_quarter(cf_c, CF) if cf_c else {}

    periods = sorted(d for d in pl_q if d >= f"{since}0101")
    rec = []
    for i, d in enumerate(periods):
        p, b = pl_q[d], bs_p.get(d, {})
        rev = p.get("营业总收入") or p.get("营业收入")
        cost = p.get("营业成本")
        exp = sum(x for x in (p.get("销售费用"), p.get("管理费用"),
                              p.get("研发费用")) if x is not None) or None
        nxt = None
        if i + 1 < len(periods):
            n = pl_q[periods[i + 1]]
            nxt = n.get("营业总收入") or n.get("营业收入")
        inv = b.get("存货")
        yoy_key = f"{int(d[:4]) - 1}{d[4:]}"
        rev_yoy = pl_q.get(yoy_key, {}).get("营业总收入") or pl_q.get(yoy_key, {}).get("营业收入")
        rec.append({
            "period": f"{d[:4]}{QEND[d[4:8]]}",
            "报告日": d,
            "单季营收": rev,
            "单季营收同比": (rev / rev_yoy - 1) * 100 if rev and rev_yoy else None,
            "单季毛利率": (1 - cost / rev) * 100 if rev and cost is not None else None,
            "三费率": exp / rev * 100 if rev and exp else None,
            "单季财务费用": p.get("财务费用"),
            "单季归母净利": p.get("归属于母公司所有者的净利润"),
            "期末存货": inv,
            "期末预付款": b.get("预付款项"),
            "期末合同负债": b.get("合同负债"),
            "期末应收": b.get("应收票据及应收账款"),
            "期末在建工程": b.get("在建工程"),
            "期末固定资产": b.get("固定资产"),
            "单季经营现金流": cf_q.get(d, {}).get("经营活动产生的现金流量净额"),
            "单季资本开支": cf_q.get(d, {}).get("购建固定资产、无形资产和其他长期资产支付的现金"),
            "下季营收÷期末存货": nxt / inv if nxt and inv else None,
        })

    ratios = [r["下季营收÷期末存货"] for r in rec if r["下季营收÷期末存货"]]
    recent = ratios[-4:] if len(ratios) >= 4 else ratios
    stats = None
    if len(recent) >= 2:
        stats = {
            "近四季比值": [round(x, 3) for x in recent],
            "低": round(min(recent), 3), "高": round(max(recent), 3),
            "中位": round(statistics.median(recent), 3),
            "离散度": round((max(recent) - min(recent)) / statistics.median(recent) * 100, 1),
            "全历史样本数": len(ratios),
        }
    return {
        "code": code,
        "since": since,
        "来源": {"利润表": {"文件": pl_file, "url": pl_url},
                 "资产负债表": {"文件": bs_file, "url": bs_url},
                 "现金流量表": {"文件": cf_file, "url": cf_url}},
        "quarters": rec,
        "存货领先关系": stats,
    }


def render(d: dict) -> str:
    """打成表。金额一律折算成亿元 —— 报表原值是元,读起来全是零。"""
    L = [f"### {d['code']} 单季实际(全部来自公司自己的报表)", ""]
    L.append("| 报告期 | 单季营收 | 同比 | 毛利率 | 三费率 | 财务费用 | 归母净利 | "
             "期末存货 | 预付款 | 合同负债 | 下季营收÷存货 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    def y(v, nd=1):
        return "—" if v is None else f"{v / 1e8:,.{nd}f}"

    def ratio(v):
        return "—" if v is None else f"{v:.2f}x"

    def p(v, nd=1):
        return "—" if v is None else f"{v:.{nd}f}%"

    for r in d["quarters"]:
        L.append(f"| {r['period']} | {y(r['单季营收'])} | {p(r['单季营收同比'])} "
                 f"| {p(r['单季毛利率'])} | {p(r['三费率'])} | {y(r['单季财务费用'], 2)} "
                 f"| {y(r['单季归母净利'])} | {y(r['期末存货'])} | {y(r['期末预付款'], 2)} "
                 f"| {y(r['期末合同负债'], 2)} "
                 f"| {ratio(r['下季营收÷期末存货'])} |")
    L.append("")
    L.append("金额单位:亿元。")
    s = d.get("存货领先关系")
    if s:
        L.append("")
        L.append(f"**存货→下季营收**:近四季比值 {s['近四季比值']},"
                 f"中位 {s['中位']}x,区间 {s['低']}~{s['高']}x,"
                 f"离散度 {s['离散度']}%(全历史 {s['全历史样本数']} 个样本)。")
        if s["离散度"] > 30:
            L.append("")
            L.append("> ⚠ 离散度超过 30%,这条关系在这只票上**不稳**,"
                     "不能拿来推下季营收。要么周转天数在变,要么业务结构在变。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--since", default="2023")
    ap.add_argument("--json", help="把结构化结果写到这个文件")
    a = ap.parse_args()

    all_out = {}
    for c in a.codes:
        d = build(Path(a.raw), c, a.since)
        all_out[c] = d
        print(render(d))
        print()
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(all_out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"→ {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
