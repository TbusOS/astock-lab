#!/usr/bin/env python3
"""报告第 9 节 —— 市场怎么想。**整节都是别人的预测,只作对照。**

从 `report.py` 拆出来的理由:那边是报告的骨架和「我们自己算出来的东西」,
这边全部是**外部意见**(卖方一致预期、券商评级、人工投喂的投行研报)。
两边的规矩正好相反 —— 那边的每个数都要能追到公司报表或 SEC 申报原值,
这边的每个数都**不许进任何一步推导**。放同一个文件里,
下一个人很容易顺手把这边的数拿去用一下。分开放,界线是物理的。

`report.py` 那边也已经顶到单文件行数上限,这一批还要往里加东西。

依赖:无。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path


def envs(p):
    if not p or not Path(p).exists():
        return {}
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def newest(raw: Path, pat: str):
    fs = sorted(glob.glob(str(raw / pat)))
    return Path(fs[-1]) if fs else None


def yi(v, nd=1):
    return "—" if v is None else f"{v / 1e8:,.{nd}f}"


def market_view(raw: Path, code: str) -> dict:
    """市场怎么想。**整节都是别人的预测**,只作对照,不进上面任何一步。"""
    out = {"一致预期": None, "评级": [], "研报": [], "来源": {}}
    for env in envs(newest(raw, f"consensus/{code}/*-ths.json")).values():
        if env.get("ok") and env.get("data"):
            out["一致预期"] = env["data"]
            out["来源"]["一致预期"] = env.get("url") or "同花顺 stock_profit_forecast_ths"
    for env in envs(newest(raw, f"ratings/{code}/*-cninfo.json")).values():
        if env.get("ok") and env.get("data"):
            out["评级"] = env["data"]
            out["来源"]["评级"] = env.get("url") or "巨潮 stock_rank_forecast_cninfo"
    for env in envs(newest(raw, f"research/{code}/*-list.json")).values():
        if env.get("ok") and env.get("data"):
            out["研报"] = env["data"]
            out["来源"]["研报"] = env.get("url") or "东财 reportapi"
    return out


def extern_research(root: Path, code: str) -> list[dict]:
    """人工投喂的外部研报(`private/extern_research/*/filled.json`),按代码筛。

    **为什么必须接进个股报告**:2026-09-02 那一版里,唯一一份国际投行研报
    (高盛给新易盛 Buy、目标价 639)只出现在另一个文件里 ——
    读者只看个股报告,根本不知道存在一份和我们结论**方向相反**的外部意见。
    把反面意见架构性地隔在别的文件里,和没写没有区别。

    它在不适用时会怎样:目录不存在、或这只票没有对应的研报时返回空,
    第 9 节写「没有外部研报」——**说清楚是「没有」而不是「没看」**。
    """
    d = root / "private" / "extern_research"
    if not d.is_dir():
        return []
    out = []
    for sub in sorted(x for x in d.glob("*") if x.is_dir()):
        fp = sub / "filled.json"
        if not fp.exists():
            continue
        try:
            j = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        tk = str((j.get("source") or {}).get("ticker") or "")
        if code not in tk:
            continue
        j["_dir"] = sub.name
        out.append(j)
    return out


def sec_extern(reports, our_ev, our_act, spot, L) -> None:
    """9.3 外部研报:评级 / 目标价 / 和我们差在哪 / 我们为什么不采信。"""
    L.append("### 9.3 外部机构研报(人工投喂的 PDF)")
    L.append("")
    if not reports:
        L.append("**这只票没有外部研报。** `private/extern_research/` 下没有代码对得上的目录 —— "
                 "是「没有」,不是「没看」。放一份进去再跑 "
                 "`tools/ingest_report.py batch <目录>` 就会出现在这里。")
        L.append("")
        return
    px = spot["close"] if spot else None
    for j in reports:
        s, c = j.get("source") or {}, j.get("call") or {}
        va = j.get("valuation_anchor") or {}
        tp, rating = c.get("target_price"), c.get("rating")
        L.append(f"**{s.get('publisher', '?')}**({s.get('report_date', '?')},"
                 f"目录 `{j['_dir']}`)")
        L.append("")
        L.append("| 项 | 它说什么 |")
        L.append("|---|---|")
        L.append(f"| 评级 | **{rating or '—'}** |")
        L.append(f"| 目标价 | **{tp:,.2f} {c.get('currency', '')}**"
                 f"(报告日股价 {c.get('price_at_report', '—')},"
                 f"隐含 {c.get('upside_pct', 0):+.1f}%,{c.get('horizon_months', '?')} 个月)"
                 f" |" if tp else "| 目标价 | — |")
        if va:
            L.append(f"| 它的估值锚 | {va.get('method', '—')} —— "
                     f"{va.get('multiple', '—')}x 乘在 {va.get('base_year', '—')} 的利润上,"
                     f"依据是 {va.get('multiple_basis', '—')} |")
        if j.get("company_guidance"):
            L.append(f"| 它引用的公司自己的话 | {j['company_guidance']} |")
        if s.get("conflict_disclosure"):
            L.append(f"| 利益冲突披露 | {s['conflict_disclosure']} |")
        L.append("")
        if tp and px:
            gap = (tp / px - 1) * 100
            L.append(f"**和我们差多少**:它的目标价 {tp:,.2f} 元 vs 现价 {px:,.2f} 元 = "
                     f"**{gap:+.1f}%**;我们的期望值是 "
                     + (f"**{our_ev:+.1f}%**" if our_ev is not None else "**没给**")
                     + f",动作「{our_act}」。"
                     + (f"**方向相反,差 {abs(gap - (our_ev or 0)):.0f} 个百分点。**"
                        if our_ev is not None and gap * our_ev < 0 else ""))
            L.append("")
        L.append("**我们为什么不把它当输入**(原则,不是针对这一家):")
        L.append("")
        L.append("1. **它是结论不是证据。** 用了它,我们就只是在转述,没有自己的判断,"
                 "而且它错的时候我们不会知道是哪一步错了。")
        L.append("2. **它的失效条件不写在里面。** 我们这套东西每一步都写了"
                 "「什么时候不能用」,一份目标价没有这一层 —— 没法被证伪的东西不能当地基。")
        L.append("3. **它会污染判断。** 先看到 " + (f"{tp:,.0f} " if tp else "") +
                 "再去算,很难不往那个数靠。所以它只出现在这一节,而且是最后一节。")
        L.append("")
        # ⚠ **只印这四类,`notes` / `备注` 一律不进报告。**
        #   `notes` 是抽取这份 PDF 时留的**我们自己的工作笔记**:有的是字段说明
        #   (「单位:net_income 为 Rmb 百万」),有的是我们当时的想法
        #   (「我们此前用的是 TTM PE 的历史分位」—— 那还是一条已经改掉的旧做法)。
        #   原样印进报告,读者会以为那是这份研报说的话。
        #   研报**自己引用的公司报表数**住在 `report_facts`,那是研报内容,照印。
        L.append("**但它的依据里有可以被检验的部分,那些值得单独拿出来看**:")
        L.append("")
        for fact in (j.get("report_facts") or [])[:3]:
            L.append(f"- **它引用的公司报表数**:{fact} "
                     f"—— 这几个数在资产负债表上直接就有,我们自己能核。")
        fc = [x for x in (j.get("forecast") or []) if x.get("is_estimate")]
        if fc:
            unit = (j.get("units") or {}).get("net_income", "")
            head = "、".join(
                "{} 归母净利 {:,.0f}".format(x["year"], x.get("net_income") or 0)
                for x in fc[:3])
            L.append(f"- 它自己的盈利预测({head};单位 {unit or '见 filled.json'})"
                     f" —— **这些是预测,不能核**;"
                     f"但**季报会一年年给出实际值**,那时候可以逐年回来对它准不准。"
                     f"我们不采信它,不等于不记录它。")
        risks = j.get("key_risks") or []
        if risks:
            L.append(f"- **它自己列的风险**:{'、'.join(risks[:5])} —— "
                     f"风险条款是这份研报里唯一写了「什么情况下我这套会错」的地方,"
                     f"和我们每一节的「什么时候不能用」是同一类东西,"
                     f"值得逐条拿去对我们自己的失效条件。")
        L.append(f"- 它的估值锚**能被核**:{va.get('multiple', '—')}x 这个倍数"
                 f"是不是真的等于这只票 {va.get('band', {}).get('mid', '—')}x 的历史中枢,"
                 f"我们自己那条 3+1 PE 带就能量。**倍数用的是哪一年的利润**"
                 f"({va.get('base_year', '—')})尤其要看 —— "
                 f"2026-09-01 我们自己就栽在「乘数和利润不是同一段时间」上。")
        L.append("")
    L.append("> ⚠ **这一节不改变上面任何一步的结论。** 它在这里是为了让读者知道"
             "「有一份和我们方向相反的外部意见存在」—— "
             "把反面意见放到另一个文件里,和没写没有区别。")
    L.append("")


def sec_market(mkt, L, gap=None):
    L.append("## 9 市场怎么想(仅作对照,**没有进入上面任何一步**)")
    L.append("")
    L.append("上面每一节的结论都不依赖这一节。放在这里是为了回答一个问题:"
             "**我们和市场差在哪,谁的依据更硬。**")
    L.append("")
    c = mkt.get("一致预期")
    if c:
        L.append("### 9.1 卖方一致预期(别人的预测)")
        L.append("")
        L.append("| 年度 | 预测机构数 | 最小 | 均值 | 最大 |")
        L.append("|---|---:|---:|---:|---:|")
        for r in c:
            L.append(f"| {r.get('年度')} | {r.get('预测机构数')} | {r.get('最小值')} "
                     f"| {r.get('均值')} | {r.get('最大值')} |")
        L.append("")
        L.append(f"来源:{mkt['来源'].get('一致预期', '')}。单位随源,通常是亿元归母净利。")
        L.append("")
        if gap:
            L.append("**我们和市场差在哪** —— 先把两边算的时间段调成一样:")
            L.append("")
            for line in gap:
                L.append(f"- {line}")
            L.append("")
    r = mkt.get("评级") or []
    if r:
        L.append("### 9.2 近期评级(别人的判断)")
        L.append("")
        L.append("| 发布日 | 机构 | 评级 | 变化 | 目标价下限 | 上限 |")
        L.append("|---|---|---|---|---:|---:|")
        for x in r[:12]:
            L.append(f"| {x.get('发布日期')} | {x.get('研究机构简称')} | {x.get('投资评级')} "
                     f"| {x.get('评级变化')} | {x.get('目标价格-下限') or '—'} "
                     f"| {x.get('目标价格-上限') or '—'} |")
        L.append("")
        L.append(f"共 {len(r)} 条,列出最近 {min(12, len(r))} 条。"
                 f"来源:{mkt['来源'].get('评级', '')}")
        L.append("")
    L.append("> **为什么这一节放最后而不是放开头**:先看到一致预期再去算,"
             "很难不往那个数靠。判断要先自己做完,再拿别人的当对照。"
             "两边差得远的时候,该问的是「谁的依据能被检验」,不是「谁的名气大」。")
    L.append("")


def consensus_gap(v, d, mkt) -> list[str]:
    """把我们的预测和一致预期放到**同一段时间**上再比。

    直接并排是错的:一致预期给的是**日历年全年**,我们预测的是**滚动四季**,
    两个时间段不一样。调法是拿今年已披露的季度当共同基数,
    反解出「市场隐含的剩余季度」,再和我们预测的那一季比 ——
    这样比的才是同一段时间。
    """
    c = mkt.get("一致预期") or []
    our = v.get("我们预测的未来四季净利")
    f = v.get("预测") or {}
    tgt = f.get("预测期")                      # 如 2026Q3
    if not (c and our and tgt):
        return []
    yr = tgt[:4]
    row = next((r for r in c if str(r.get("年度")) == yr), None)
    if not row or not row.get("均值"):
        return []
    done = [r for r in d["quarters"]
            if r["period"][:4] == yr and r["period"] < tgt
            and r.get("单季归母净利") is not None]
    if not done:
        return []
    got = sum(r["单季归母净利"] for r in done)
    mean = float(row["均值"]) * 1e8            # 一致预期单位是亿元
    rest = mean - got                          # 市场隐含的「今年剩下的季度」合计
    n_rest = 4 - len(done)
    p = f.get("归母净利区间") or {}
    lines = [
        f"一致预期 {yr} 全年归母净利均值 **{row['均值']} 亿**"
        f"({row.get('预测机构数')} 家),{yr} 已披露 "
        f"{'、'.join(r['period'] for r in done)} 合计 **{yi(got)} 亿**,"
        f"两者相减 = 市场隐含今年剩下 {n_rest} 个季度合计 **{yi(rest)} 亿**,"
        f"平均每季 **{yi(rest / n_rest)} 亿**。",
    ]
    if p.get("mid"):
        lines.append(
            f"我们对 {tgt} 单季的预测是 **{yi(p['low'])} ~ {yi(p['high'])} 亿**"
            f"(中枢 {yi(p['mid'])} 亿)。和上面那个「平均每季 {yi(rest / n_rest)} 亿」比,"
            + ("**我们更低**" if p["mid"] < rest / n_rest else "**我们更高**")
            + f",差 {abs(p['mid'] - rest / n_rest) / (rest / n_rest) * 100:.0f}%。")
        lines.append(
            "差在哪是可以查的:要么是**营收**(我们的方法在第 3 节写了依据和失效条件),"
            "要么是**净利率**(我们用近四季实际区间,没有假设它改善)。"
            f"{tgt} 季报出来时,这条差值会有一个明确的对错。")
    return lines
