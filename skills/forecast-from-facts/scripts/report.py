#!/usr/bin/env python3
"""出一只票的投资分析报告 —— 结论建在事实上,市场预期只作对照。

    report.py <code> --name 新易盛 [--raw data/raw] [--out 报告.md]

报告的骨架就是我们的判断顺序,不是抄来的目录:
    1 结论      2 事实(单季实际)  3 我们的预测  4 产业链位置
    5 我们算的估值   6 市场怎么想 ← 单独放最后   7 怎么核对

为什么市场预期放最后:
    放前面会污染判断 —— 先看到「20 家机构均值 198 亿」再去算,很难不往那个数靠。
    放最后它就只是事后对照:我们和市场差多少、差在哪、谁的依据更硬。

依赖:无。
"""
from __future__ import annotations

import argparse
import glob
import json
from datetime import date
from pathlib import Path

import forecast
import quarterly
import valuation


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


def sec_conclusion(v, f, mkt, name, code, L):
    band, spot, our = v["前瞻PE带"], v["现价"], v["我们预测的未来四季净利"]
    L.append("## 1 结论")
    L.append("")
    if not (spot and our):
        L.append("数据不足,给不出结论。缺的部分见第 7 节。")
        return
    cap = spot["close"] * v["总股本"]
    pe_mid = cap / our["mid"] if our["mid"] else None
    L.append(f"- **现价 {spot['close']:.2f} 元({spot['date']}),市值 {yi(cap, 0)} 亿。**")
    if pe_mid:
        L.append(f"- 按**我们自己**推的未来四季归母净利 {yi(our['low'])}~{yi(our['high'])} 亿"
                 f"(中枢 {yi(our['mid'])} 亿),现价对应前瞻 PE **{pe_mid:.1f}x**。")
    if band.get("ok") and pe_mid:
        pos = ("**高于历史 p90**" if pe_mid > band["p90"] else
               "在历史 p75~p90 之间" if pe_mid > band["p75"] else
               "在历史 p50~p75 之间" if pe_mid > band["p50"] else
               "在历史 p25~p50 之间" if pe_mid > band["p25"] else "**低于历史 p25**")
        L.append(f"- 这只票**自己**的历史前瞻 PE 带:p25={band['p25']:.1f}x、"
                 f"p50={band['p50']:.1f}x、p75={band['p75']:.1f}x、p90={band['p90']:.1f}x。"
                 f"现价 {pos}。")
    cp = f.get("产业链位置")
    if cp:
        L.append(f"- 相对北美四大云厂资本开支,这家公司近四季的增速倍数从 "
                 f"{cp['近四季首']:.2f}x 变到 {cp['近四季末']:.2f}x —— **{cp['判断']}**。")
    n = f["可用方法数"]
    L.append(f"- 预测的把握程度:三个方法里 **{n} 个适用**。"
             + ("三个都不适用,下季营收我们没有可靠推法。" if n == 0 else
                "只有一个,没有第二个交叉验证,区间要打折看。" if n == 1 else
                "两个以上互相印证。"))
    L.append("")
    L.append("> 上面每一个数都来自公司自己的报表或 SEC 申报原值。"
             "**没有任何一个数来自机构的盈利预测。** 市场怎么看见第 6 节。")
    L.append("")


def sec_facts(d, L):
    L.append("## 2 事实:公司自己报表里的单季数")
    L.append("")
    L.append("A 股三大表报的是**累计数**,直接看会把拐点抹平 —— "
             "比如 2026 上半年营收合计看不出 Q2 单季比 Q1 跳了多少。下表全部还原成单季。")
    L.append("")
    L.append(quarterly.render(d).split("\n", 2)[2])
    L.append("")


MARK = "**在产业链里的位置:"


def sec_forecast(f, L):
    """第 3、4 节。forecast.render 把预测和产业链位置连着输出,
    这里在产业链那一段切开单独成节 —— 它是**判断**不是预测,混在一起会被当成预测的一部分。"""
    body = forecast.render(f).split("\n", 2)[2]
    head, sep, chain = body.partition(MARK)
    L.append("## 3 我们的预测")
    L.append("")
    if not sep:
        L.append(body)
        L.append("")
        return
    # 「怎么被推翻」那段在产业链后面,要拨回第 3 节
    chain_body, brk, refute = chain.partition("**这份预测怎么被推翻**")
    L.append(head.rstrip())
    L.append("")
    if brk:
        L.append("**这份预测怎么被推翻**" + refute.rstrip())
        L.append("")
    L.append("## 4 在产业链里的位置")
    L.append("")
    L.append("这一节是**判断**不是预测。它回答的是:上游把蛋糕做大的时候,"
             "这家公司分到的那块在变大还是变小。")
    L.append("")
    L.append(MARK + chain_body.rstrip())
    L.append("")


def sec_valuation(v, f, L):
    L.append("## 5 估值:我们自己算的前瞻 PE 带")
    L.append("")
    L.append("### 5.1 PE 到底是什么(这一步不讲清楚,后面的数就只能被相信)")
    L.append("")
    L.append("PE = 市值 ÷ 净利 = **市场愿意为一块钱利润付多少倍**。它由三件事决定:")
    L.append("")
    L.append("| 决定因素 | 怎么影响 |")
    L.append("|---|---|")
    L.append("| 增长速度 | 利润明年会变多少 —— 增长减速时 PE 必然下移,"
             "这是数学不是情绪:同样一块钱当期利润,背后的未来现金流少了,值的钱就少 |")
    L.append("| 增长的确定性 | 同样的增速,能看见订单的和只能看见趋势的,市场给的倍数不一样 |")
    L.append("| 资本回报 | 赚这份利润要占用多少资本 —— 要不断砸钱才能维持的增长,值钱程度打折 |")
    L.append("")
    L.append("### 5.2 为什么不能用「过去十二个月 PE」的历史分位")
    L.append("")
    L.append("这是我们自己 2026-09-01 栽过的跟头:拿**过去十二个月** PE 的历史分位,"
             "去乘**明年**的预测利润。分子是明年的利润,乘数却是用今年利润算出来的 —— "
             "**同一段增长被算了两遍**。单只票的隐含空间能差 45 个百分点,"
             "而数字看着全都合理,方向却系统性偏乐观。")
    L.append("")
    L.append("### 5.3 我们怎么算这条带")
    L.append("")
    L.append("对历史上每一个交易日 t:")
    L.append("")
    L.append("```")
    L.append("市值(t)    = 前复权收盘价(t) × 当前总股本")
    L.append("前瞻PE(t)  = 市值(t) ÷ Σ(t 之后四个季度的【实际】归母净利)")
    L.append("```")
    L.append("")
    L.append("两点让它成为**纯事实**:")
    L.append("")
    L.append("1. 用**前复权**价乘当前股本,等于把历史价格换算到今天的股本上。"
             "送转不再制造假的价格跳空 —— 这和「拿未复权目标价去比未复权日线」是同一类坑。")
    L.append("2. 分母是**事后真实发生**的净利,不是当时任何人的预测。"
             "所以这条带回答的是:**市场当年为这家公司未来一年的真实利润,实际付了多少倍。**")
    L.append("")
    b = v["前瞻PE带"]
    if not b.get("ok"):
        L.append(f"> 这只票的前瞻带算不出来:{b['why']}")
        L.append("")
        return
    L.append(f"**代价**:最近四个季度算不出来({b['算不了的交易日']} 个交易日被排除)——"
             f"未来的净利还没发生,分母不存在。**不拿预测去填**,填了这条带就不再是事实。"
             f"所以带的区间只到 {b['到']}。")
    L.append("")
    L.append(f"| 样本 | p10 | p25 | p50 | p75 | p90 | 均值 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    L.append(f"| {b['n']} 个交易日({b['从']} ~ {b['到']}) | {b['p10']:.1f}x | "
             f"{b['p25']:.1f}x | {b['p50']:.1f}x | {b['p75']:.1f}x | {b['p90']:.1f}x "
             f"| {b['均值']:.1f}x |")
    L.append("")

    spot, our = v["现价"], v["我们预测的未来四季净利"]
    if not (spot and our):
        return
    cap = spot["close"] * v["总股本"]
    L.append("### 5.4 现价隐含了什么")
    L.append("")
    L.append(f"我们预测的未来四季归母净利:**{yi(our['low'])} ~ {yi(our['high'])} 亿**"
             f"(中枢 {yi(our['mid'])} 亿)。构成:{our['说明']}。")
    L.append("")
    L.append("| | 净利 | 现价对应前瞻 PE | 在自己历史带的位置 |")
    L.append("|---|---:|---:|---|")
    for k, lab in (("high", "乐观"), ("mid", "中枢"), ("low", "谨慎")):
        ni = our[k]
        pe = cap / ni if ni else None
        if pe is None:
            continue
        pos = ("> p90" if pe > b["p90"] else "p75~p90" if pe > b["p75"] else
               "p50~p75" if pe > b["p50"] else "p25~p50" if pe > b["p25"] else "< p25")
        L.append(f"| {lab} | {yi(ni)} 亿 | {pe:.1f}x | {pos} |")
    L.append("")

    L.append("### 5.5 我们的目标价怎么出来的")
    L.append("")
    L.append("```")
    L.append("目标价 = (我们预测的未来四季净利 ÷ 总股本) × 我们选的前瞻 PE")
    L.append("```")
    L.append("")
    g = v.get("未来四季净利同比")
    pick = valuation.pick_multiple(b, g)
    if not pick.get("ok"):
        L.append(f"> 选不了倍数:{pick.get('why')}")
        L.append("")
        return
    L.append(f"**选哪一档由增长决定** —— {pick['理由']}")
    L.append("")
    L.append("| 情景 | 未来四季净利 | 前瞻 PE | 目标价 | 相对现价 |")
    L.append("|---|---:|---:|---:|---:|")
    rows = [("谨慎", our["low"], pick["低"]), ("中枢", our["mid"], pick["中"]),
            ("乐观", our["high"], pick["高"])]
    for lab, ni, mult in rows:
        tp = ni * mult / v["总股本"]
        L.append(f"| {lab} | {yi(ni)} 亿 | {mult:.1f}x | {tp:,.0f} 元 "
                 f"| {(tp / spot['close'] - 1) * 100:+.1f}% |")
    L.append("")
    L.append("> ⚠ **两端是双重叠加,不是等概率的三种可能。** 谨慎档同时假设"
             "「净利落在区间下沿」**且**「市场只给带的下半段倍数」,"
             "两个偏空的条件一起成立;乐观档反过来。"
             "所以两端比中枢罕见得多 —— 它们是边界,不是预期。")
    L.append("")
    L.append("**这套估值怎么被推翻**:")
    L.append("")
    L.append("- 净利区间错了 —— 见第 3 节各方法的失效条件")
    L.append("- 倍数选错了 —— 我们假设市场会按这只票**自己**的历史规律给倍数。"
             "如果行业的定价逻辑变了(比如从成长股变成周期股),历史带就不再适用")
    L.append(f"- 历史带本身样本不够 —— 现在是 {b['n']} 个交易日、"
             f"{b['从'][:4]}~{b['到'][:4]} 年。这段时间如果只覆盖了一轮景气周期,"
             f"带的上下沿都会偏窄")
    L.append("")


def sec_market(mkt, L, gap=None):
    L.append("## 6 市场怎么想(仅作对照,**没有进入上面任何一步**)")
    L.append("")
    L.append("上面五节的每个结论都不依赖这一节。放在这里是为了回答一个问题:"
             "**我们和市场差在哪,谁的依据更硬。**")
    L.append("")
    c = mkt.get("一致预期")
    if c:
        L.append("### 6.1 卖方一致预期(别人的预测)")
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
        L.append("### 6.2 近期评级(别人的判断)")
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


def sec_how(code, v, L):
    L.append("## 7 每个数怎么核对 / 怎么重跑")
    L.append("")
    L.append("| 这份报告里的 | 从哪来 | 怎么重跑 |")
    L.append("|---|---|---|")
    L.append(f"| 单季营收/毛利率/存货/预付款 | 新浪转录的公司三大表 `data/raw/financials/{code}/*-利润表.json` 等 "
             f"| `tools/fetch_all.py --codes {code} --group statements` |")
    L.append(f"| 收盘价、前复权序列 | baostock 日线 `data/raw/quotes/{code}/*-qfq.json` "
             f"| `tools/fetch_all.py --codes {code} --group quotes` |")
    L.append(f"| 总股本 | baostock query_profit_data `data/raw/financials/{code}/` "
             f"| `tools/fetch_all.py --codes {code} --group financials` |")
    L.append("| 北美四大云厂季度资本开支 | **SEC XBRL 申报原值** `data/raw/overseas_facts/*-capex.json` "
             "| `tools/sec_facts.py capex` |")
    L.append(f"| 一致预期 / 评级(仅第 6 节) | 同花顺、巨潮 `data/raw/consensus/`、`data/raw/ratings/` "
             f"| `tools/fetch_all.py --codes {code} --group consensus,ratings` |")
    L.append("")
    L.append("整份报告重跑:")
    L.append("")
    L.append("```bash")
    L.append("cd ~/astock-lab-private")
    L.append(f"tools/report.py {code} --name <名称> --out private/reports/<日期>/{code}.md")
    L.append("```")
    L.append("")
    L.append("**这份报告不做的事**:不引用任何机构的盈利预测作为输入;"
             "不用新闻和市场情绪;不给买卖建议。它只做一件事 —— "
             "把公开披露的事实推到一个可以被证伪的结论上。")
    L.append("")


def build(code: str, name: str, raw: Path, target: str | None) -> str:
    v = valuation.build(code, raw, target)
    f = v["预测"]
    d = quarterly.build(raw, code, since="2023")
    mkt = market_view(raw, code)

    # 未来四季净利同比 —— 选倍数要用
    hist = [r["单季归母净利"] for r in d["quarters"] if r.get("单季归母净利") is not None]
    our = v["我们预测的未来四季净利"]
    if our and len(hist) >= 7:
        prev4 = sum(hist[-7:-3])
        v["未来四季净利同比"] = (our["mid"] / prev4 - 1) * 100 if prev4 else None
    else:
        v["未来四季净利同比"] = None

    L = [f"# {name}({code}) 投资分析", ""]
    spot = v["现价"]
    L.append(f"生成于 {date.today().isoformat()} · 最新已披露 **{f['最新已披露']}**"
             + (f" · 收盘价 {spot['close']:.2f} 元({spot['date']})" if spot else ""))
    L.append("")
    L.append("> **这份报告的结论全部建在公司自己披露的报表和 SEC 申报原值上,"
             "不引用任何机构的盈利预测。** 市场怎么看单独放在第 6 节,只作事后对照。")
    L.append("")
    sec_conclusion(v, f, mkt, name, code, L)
    sec_facts(d, L)
    sec_forecast(f, L)
    sec_valuation(v, f, L)
    sec_market(mkt, L, consensus_gap(v, d, mkt))
    sec_how(code, v, L)
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("code")
    ap.add_argument("--name", default="")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--target")
    ap.add_argument("--out")
    a = ap.parse_args()
    md = build(a.code, a.name or a.code, Path(a.raw), a.target)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
