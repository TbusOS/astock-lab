#!/usr/bin/env python3
"""出一只票的投资分析报告 —— 结论建在事实上,市场预期只作对照。

    report.py <code> --name 新易盛 [--raw data/raw] [--out 报告.md]

报告的骨架就是我们的判断顺序,不是抄来的目录:
    0   你手上这笔(成本/浮盈亏)
    0.5 这只票是什么生意 ← **先判行业,后面用哪把尺由它决定**
    1   结论(动作 · 期望值 · 三情景 · 概率 · 确信度 · 仓位上限 · 复核日)
    2   事实(单季实际 + 2.1 现金与订单)
    3   我们的预测
    3.5 外部视角(基准率)← 历史上同样位置的公司后来怎么样了
    4   产业链位置
    4.5 上游怎么说(领先层)← 指引 / 台湾月营收 / 上游 capex / 油价
    5   估值(按行业该用的那把尺)
    6   筹码与杠杆      7 限售解禁与价格位置      8 海外同业对照
    9   市场怎么想 ← 单独放这里
    10  判断依据与复核(含这次写进决策日志的那条)
    11  买卖建议(完整推导)                    12 数据来源

**0.5 为什么在最前面**:同一套尺套所有行业会出错,而且错得看不出来 ——
成长股的 PE 分位有意义,周期股的 PE 在盈利低谷给「便宜」的假信号。
先判行业不是背景介绍,是**后面每一节用哪把尺的前置条件**。

第 0 节放最前面:**没有成本就没法谈该怎么办**。一份不知道你买在哪的报告,
只能回答「这家公司值多少钱」,回答不了「你该拿还是该减」。

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
import sys
from pathlib import Path

# tools/ 下是软链,__file__ 指向真实目录,但 sys.path[0] 是软链所在目录 ——
# 于是 import quarterly 找不到。**把脚本真实所在目录加进 sys.path**,
# 这样 `tools/report.py` 和 `skills/.../report.py` 两条路径都能直接跑,
# 不需要调用方设 PYTHONPATH(别人 clone 下来第一件事就会卡在这)。
# source_catalog 住在 data-sources 那个 skill 下 —— 数据源目录属于取数,不属于预测。
# 两个 skill 的 scripts 目录都加进 sys.path,tools/ 软链和仓内真实路径都能跑。
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent.parent / "data-sources" / "scripts"))


import advice
import forecast
import guidance
import journal_log
import outside_view
import quarterly
import sector_rules
import sections_position as pos
import report_market as rm
import source_catalog as catalog
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


def _low_conf_note(d: dict, L: list) -> None:
    """确信度很低却给出减仓/清仓时,**在结论段就把这层意思说清楚**。

    「确信度 0、动作清仓」这个组合读起来像「我们很确定它会跌」,而实际意思相反:
    确信度低说明**我们对自己这个判断没什么把握**,动作是减/清仓的理由是
    「没把握守住,而且估值在自己历史极值」。这两句给读者的印象差得很远,
    以前它埋在 11.4 节,读者要翻十页才拼得出来。

    它在不适用时会怎样:确信度拿到一半以上、或者动作不是减/清仓时,这一段不出现。
    """
    conf = d.get("确信度") or {}
    act = d.get("动作") or ""
    if not act.startswith(("清仓", "减仓")) or conf.get("比例", 1) > 0.5:
        return
    L.append(f"> ⚠ **确信度只有 {conf['分']}/{conf['满分']}"
             + (f"(分母是本行业可得分,不是 3)" if conf["满分"] < 3 else "")
             + f" —— 这个"
             f"「{act}」不是「我们确定它会跌」,是「我们没把握它守得住,"
             f"而且它的估值在自己历史的极值附近」。** 两句话给人的印象差很远:"
             f"前者听起来像一个强判断,后者说的是**我们对自己这个判断都没什么把握**。"
             f"确信度低的时候,期望值本身的误差也大。"
             f"确信度是怎么算出来的、缺的那几分缺在哪,见第 11.4 节。")
    L.append("")


def sec_conclusion(v, f, mkt, name, code, L, cost=None, adv=None):
    """第 1 节。**先给动作和期望值,再给三情景**,然后才是支撑它的几个数。

    为什么动作放最前面:读者要的是「我该怎么办」。把它埋在第 11 节,
    等于让人读完十节才知道结论 —— 而中间每一节都在影响他的印象,
    读到结论时他已经有自己的看法了。先给结论,再让他逐节质疑。
    """
    band, spot, our = v["前瞻PE带"], v["现价"], v["我们预测的未来四季净利"]
    L.append("## 1 结论")
    L.append("")
    d = (adv or {}).get("决策") or {}
    if d.get("ok"):
        L.append(f"# → **{adv['动作']}**")
        L.append("")
        cf = d["确信度"]
        # ⚠ **分母是「本行业可得分」,不是固定的 3。** 印成 `1/3(高)` 会让人读不懂:
        #   得分看着只有三分之一,档位却是「高」。第一次出现的地方要把分母解释掉。
        L.append(f"**期望值 {d['EV']:+.1f}%**　·　确信度 "
                 f"**{cf['分']}/{cf['满分']}({cf['档']})**"
                 + (f"(分母是**本行业可得分** —— 这个行业只登记了 {cf['满分']} 个"
                    f"可检验项,拿不到的那几条不计分也不扣分,见第 11.4 节)"
                    if cf["满分"] < 3 else "")
                 + f"　·　复核日 **{d['复核日']}**({d['复核日是什么报']})")
        L.append("")
        if adv.get("档位理由"):
            L.append(adv["档位理由"])
            L.append("")
        _low_conf_note(d, L)
        sc = d["三情景"]["情景"]
        pr = d["概率"]["probs"]
        L.append("| 情景 | 价位 | 相对现价 | 概率 |")
        L.append("|---|---:|---:|---:|")
        for k, lab in (("bear", "bear 悲观"), ("base", "base 中性"), ("bull", "bull 乐观")):
            L.append(f"| {lab} | {sc[k]['价']:,.2f} 元 | **{sc[k]['涨跌']:+.1f}%** "
                     f"| {pr[k] * 100:.0f}% |")
        L.append(f"| **期望值** | | **{d['EV']:+.1f}%** | 三行加权 |")
        L.append("")
        L.append(f"**动作只由期望值决定**(≤−20% 清仓 · −20~−8% 减仓 · "
                 f"−8~+8% 持有不加仓 · +8~+20% 持有 · >+20% 且确信度比例 ≥ 2/3 可加仓);"
                 f"确信度和仓位上限只管**拿多少**,不管拿不拿。"
                 f"**四步概率怎么来的、每一步动了多少,第 11.2 整张印出来。**")
        L.append("")
        pc = d["仓位上限"]
        if pc.get("ok"):
            if d.get("超限"):
                o = d["超限"]
                L.append(f"- ⚠ **仓位超限**:占净资产 {o['现仓位pct']:.1f}%,"
                         f"上限 {o['上限pct']:.1f}%(从 bear 情景 "
                         f"{sc['bear']['涨跌']:+.1f}% 倒推),**超 {o['超出']:,.0f} 元**。"
                         f"这一条**不看期望值** —— "
                         + ("期望值本身也是负的,两条都指向减仓。"
                            if d["EV"] < 0 else
                            "期望为正不代表你扛得住最坏那一档。"))
            else:
                L.append(f"- **仓位上限 {pc['上限pct']:.1f}% of 净资产**"
                         + (f",你现在 {pc['现仓位pct']:.1f}%,没有超"
                            if pc.get("现仓位pct") is not None else "")
                         + f"(从 bear 情景 {sc['bear']['涨跌']:+.1f}% 倒推)")
        else:
            L.append(f"- **仓位上限这次算不出来**:{pc.get('why')}")
    else:
        L.append(f"# → **{(adv or {}).get('动作', '数据不足,不给动作')}**")
        L.append("")
        L.append(f"**为什么不给动作**:{d.get('why') or '前面几步有一步算不出来。'}")
        L.append("")
        L.append("> 不给动作和「持有」不是一回事。「持有」是一个结论;"
                 "这里是**没有结论**。给一个猜的动作比不给更糟,"
                 "因为它看着和有依据的动作一模一样。")
    L.append("")
    if cost and spot:
        pl = (spot["close"] / cost - 1) * 100
        L.append(f"- **成本 {cost:,.3f},现价 {spot['close']:,.2f}"
                 f"({spot['date']}),浮盈亏 {pl:+.1f}%**"
                 + (f",回本需涨 {(cost / spot['close'] - 1) * 100:+.1f}%。"
                    if pl < 0 else "。")
                 + f" 市值 {yi(spot['close'] * v['总股本'], 0)} 亿。")
    if not (spot and our):
        L.append("- 我们自己的 3+1 净利预测这次推不出来,第 3 节写了原因。")
        L.append("")
        return
    cap = spot["close"] * v["总股本"]
    pe_mid = cap / our["mid"] if our["mid"] else None
    if not cost:
        L.append(f"- **现价 {spot['close']:.2f} 元({spot['date']}),市值 {yi(cap, 0)} 亿。**")
    if pe_mid:
        L.append(f"- 按**我们自己**推的 3+1 归母净利 {yi(our['low'])}~{yi(our['high'])} 亿"
                 f"(中枢 {yi(our['mid'])} 亿),现价对应 **3+1 PE {pe_mid:.1f}x**。"
                 + ("**⚠ 这只票的行业不用 PE 这把尺**(见第 0.5 节),"
                    "这个数只当背景,不进决策。"
                    if (adv or {}).get("估值维失效") else ""))
    if band.get("ok") and pe_mid and not (adv or {}).get("估值维失效"):
        slot = ("**高于历史 p90**" if pe_mid > band["p90"] else
                "在历史 p75~p90 之间" if pe_mid > band["p75"] else
                "在历史 p50~p75 之间" if pe_mid > band["p50"] else
                "在历史 p25~p50 之间" if pe_mid > band["p25"] else "**低于历史 p25**")
        L.append(f"- 这只票**自己**的历史 3+1 PE 带:p25={band['p25']:.1f}x、"
                 f"p50={band['p50']:.1f}x、p75={band['p75']:.1f}x、p90={band['p90']:.1f}x。"
                 f"现价 {slot}。")
    pb = v.get("PB带") or {}
    if (adv or {}).get("估值维失效") and pb.get("ok"):
        L.append(f"- 这只票该看的是 **PB**:现在 {pb['现值']:.2f}"
                 f"(在自己历史的 {pb['现值分位']:.0f}% 分位),p25={pb['p25']:.2f}、"
                 f"p50={pb['p50']:.2f}、p75={pb['p75']:.2f}。"
                 # ⚠ **「该用哪把尺」和「这次用没用上」是两件事。** 不给动作的时候
                 #   三情景根本没算,还写「价位就是按它算的」就是一句假话。
                 + ("三情景的价位就是按它算的。" if d.get("ok") else
                    "**但这次没有用它算价位** —— 上面说了为什么不给动作,"
                    "这个分位只当背景。"))
    cp = f.get("产业链位置")
    if cp:
        L.append(f"- 相对{cp['上游']},这家公司的增速倍数在 {cp['期间']} 之间从 "
                 f"{cp['起点']:.2f}x 变到 {cp['终点']:.2f}x —— **{cp['判断']}**。")
    elif f.get("上游说明"):
        L.append(f"- 产业链位置**算不了**:{f['上游说明']}")
    lead = (adv or {}).get("领先") or {}
    if lead.get("方向"):
        L.append(f"- 领先层 {lead['↑数']} 个 ↑、{lead['↓数']} 个 ↓(净 "
                 f"{lead['净方向']:+d}):"
                 + "、".join(f"{x['来自']} **{x['方向']}**" for x in lead["方向"])
                 + "。第 4.5 节逐条给依据。")
    n = f["可用方法数"]
    ok_names = [m["name"] for m in f["methods"] if m.get("ok")]
    bad_names = [m["name"] for m in f["methods"] if not m.get("ok")]
    # ⚠ **这一句必须看 `方法一致`,不能只数个数。** 胜宏 3 个方法都通过自检、
    #   区间却不重叠,原来这里照样写「多个方法互相印证」,而同一份报告第 3 节写的是
    #   「互相矛盾」、11.4 节给 0 分。**第 1 节是读者第一眼看的地方,而它说的是反话。**
    consistent = f.get("方法一致", True)
    L.append(f"- 预测的把握程度:**{n}/3 个方法适用**"
             + (f"({'、'.join(ok_names)})" if ok_names else "")
             + (f",不适用的是 {'、'.join(bad_names)}" if bad_names else "")
             + "。"
             + ("三个都不适用,下季营收我们没有可靠的推法。" if n == 0 else
                "只有一个,没有第二个来交叉验证,区间要打折看。" if n == 1 else
                "多个方法适用而且区间重叠。" if consistent else
                f"**{n} 个方法适用,但它们给的区间不重叠** —— "
                f"这不是互相印证,是这只票当期的形状不稳定,"
                f"区间因此取并集、开得更宽。")
             + (f"区间宽度 {f['区间宽度pct']:.0f}%。"
                if f.get("区间宽度pct") is not None else ""))
    L.append("  三个方法分别是**存货法**(看供给侧已经花出去的钱)、"
             "**同比法**(看需求侧的增长趋势)、**环比法**(看这门生意的季节形状),"
             "每个的算式和失效条件见第 3 节。")
    L.append("")
    L.append("> 上面每一个数都来自公司自己的报表、SEC 申报原值,"
             "或者云厂在自己电话会上说的下期预算。"
             "**没有任何一个数来自机构的盈利预测。** 市场怎么看见第 9 节。")
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


def sec_forecast(f, L, outside=None, lead=None):
    """第 3、3.5、4、4.5 节。

    `forecast.render` 把预测和产业链位置连着输出,这里在产业链那一段切开 ——
    它是**判断**不是预测,混在一起会被当成预测的一部分。切开之后顺序是:

        3 我们的预测(内部视角)
        3.5 外部视角(基准率)—— 紧跟在预测后面,因为它就是给那个预测泼冷水的
        4 产业链位置(份额在得还是在丢)
        4.5 上游怎么说(领先层)—— 紧跟产业链,因为它给的是同一批上游的下期计划

    **3.5 为什么必须挨着 3**:内部视角刚说完「我们推 47.6 亿」,
    下一句就该是「历史上这么推的人,四个季度后还剩几成」。
    隔得远了,读者已经把那个数当成事实了。
    """
    body = forecast.render(f).split("\n", 2)[2]
    head, sep, chain = body.partition(MARK)
    L.append("## 3 我们的预测")
    L.append("")
    if not sep:
        L.append(body)
        L.append("")
        if outside is not None:
            outside_view.render(outside, "3.5", L)
        return
    # 「怎么被推翻」那段在产业链后面,要拨回第 3 节
    chain_body, brk, refute = chain.partition("**这份预测怎么被推翻**")
    L.append(head.rstrip())
    L.append("")
    if brk:
        L.append("**这份预测怎么被推翻**" + refute.rstrip())
        L.append("")
    if outside is not None:
        outside_view.render(outside, "3.5", L)
    L.append("## 4 在产业链里的位置")
    L.append("")
    L.append("这一节是**判断**不是预测。它回答的是:上游把蛋糕做大的时候,"
             "这家公司分到的那块在变大还是变小。")
    L.append("")
    L.append(MARK + chain_body.rstrip())
    L.append("")
    if lead is not None:
        guidance.render(lead, "4.5", L)


def sec_valuation(v, f, L, sector=None, rules=None):
    lens_name = {"pe31": "3+1 PE 带", "pb": "PB 全历史分位",
                 "ps31+pb": "3+1 PS 带 + PB 分位"}.get((rules or {}).get("估值尺"))
    L.append(f"## 5 估值:这只票该用哪把尺"
             + (f"(**{lens_name}**)" if lens_name else "(**这个行业还没定尺**)"))
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
    L.append("同一个错第二天换个样子又犯了一次:**2026-09-02 第一版自建带,"
             "历史那条带的分母用的是「季度末之后四季」的实际净利(四季全在未来),"
             "现价那一格的分母却是「最近三季已披露 + 预测一季」** —— 两边差三个季度。"
             "对增速 100%+ 的票,现价 PE 被系统性抬高,三只 AI 票的估值分因此错了一档"
             "(中际旭创现价算出 37.3x,而按同样算法做出来的带 p50 是 35.2x,不是 22.7x)。")
    L.append("")
    L.append("> **乘数和利润必须算同一段时间** —— 这是这两次错的共同教训。"
             "所以现在带和现价都用「3+1」:最近三季已披露 + 下一季。"
             "两边唯一的差别是最后那一季在历史里是实际、在今天是预测。")
    L.append("")
    L.append("### 5.3 我们怎么算这条带")
    L.append("")
    L.append("对历史上每一个交易日 t:")
    L.append("")
    L.append("```")
    L.append("市值(t)     = 前复权收盘价(t) × 当前总股本")
    L.append("3+1 PE(t)   = 市值(t) ÷ ( t 那天最近三季【已披露实际】归母净利")
    L.append("                          + 它们的下一季【实际】归母净利 )")
    L.append("```")
    L.append("")
    L.append("**为什么是 3+1,不是四季全在未来。** 现价那一格的分母永远只能是"
             "「3 季实际 + 1 季预测」—— 我们没有能力预测四个季度。"
             "带若做成纯前瞻,它的分母比现价那一格整整往后挪三个季度;"
             "票增长越快,同一个价格算出的 PE 差得越远,而两个数摆在一起看着都很正常。"
             "所以带也做成 3+1:**两边唯一的差别就是最后那一季在历史里是实际、"
             "在今天是我们的预测**,前三季两边都是已披露的实际值。")
    L.append("")
    L.append("两点让它成为**纯事实**:")
    L.append("")
    L.append("1. 用**前复权**价乘当前股本,等于把历史价格换算到今天的股本上。"
             "送转不再制造假的价格跳空 —— 这和「拿未复权目标价去比未复权日线」是同一类坑。")
    L.append("2. 分母**四季全是事后真实发生**的净利,不是当时任何人的预测。"
             "所以这条带回答的是:**市场当年为这家公司这四个季度的真实利润,"
             "实际付了多少倍。**")
    L.append("")
    sh = v.get("股本来源") or {}
    L.append(f"**这条带用的总股本:{v['总股本'] / 1e8:,.4f} 亿股**"
             f"(来源:{sh.get('来源', '?')},截至 {v.get('股本截止日', '?')})。")
    if sh.get("有股本变动"):
        L.append("")
        L.append(f"> ⚠ **季报之后有过股本变动。** 最近一次定期报告(截至 "
                 f"{sh.get('季报截止')})披露的是 {sh['季报股本'] / 1e8:,.4f} 亿股,"
                 f"比现在少 {abs(sh['与季报差pct']):.2f}%(增发 / 可转债转股 / "
                 f"股权激励行权都会造成这个差)。"
                 f"**这里按最新股本算** —— 用季报那个数会让市值和 PE 系统性偏低 "
                 f"{abs(sh['与季报差pct']):.1f}%,而这只票在横向比估值时就会显得"
                 f"比实际便宜。股本从日线的 `peTTM` 反推:"
                 f"`peTTM × 最近四季归母净利 ÷ 收盘价`,"
                 f"因为 `peTTM` 是**每天**按当天股本重算的。")
    L.append("")
    L.append("> ⚠ **这条带有一个已知的近似**:历史市值用「前复权价 × **今天**的股本」算,"
             "所以它没有还原历次现金分红,也没有还原期间的增发与转股。"
             "分红那一项对这几只的影响在 2% 以内;增发那一项会让早年的历史市值偏高"
             "(拿今天更大的股本去乘早年的价),量级在 10% 上下,方向和分红相反。"
             "两者都远小于带本身的宽度,但「纯事实」这个说法要打这个折扣。")
    L.append("")
    b = v["前瞻PE带"]
    if not b.get("ok"):
        L.append(f"> 这只票的 3+1 PE 带算不出来:{b['why']}")
        L.append("")
        return
    L.append(f"**代价**:**最近一个季度算不出来**({b['算不了的交易日']} 个交易日被排除)"
             f"—— 它的「下一季」还没发生,分母缺一块。**不拿预测去填**,"
             f"填了这条带就不再是事实。所以带的区间只到 {b['到']}。"
             f"现价那一格能用预测补,是因为它本来就是我们的预测;"
             f"往带里补就等于拿预测当历史。")
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
    L.append(f"我们的 3+1 归母净利:**{yi(our['low'])} ~ {yi(our['high'])} 亿**"
             f"(中枢 {yi(our['mid'])} 亿)。构成:{our['说明']}。"
             f"和上面那条带**算的是同一种东西** —— 差别只在这里的最后一季是预测。")
    L.append("")
    L.append("| | 3+1 净利 | 现价对应 3+1 PE | 在自己历史带的位置 |")
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

    L.append("### 5.5 这只票该用哪把尺,以及另外两把长什么样")
    L.append("")
    L.append("**目标价不在这一节。** 三个情景的价位在第 11.1 节 —— "
             "那里连概率一起给,才是完整的判断。"
             "在这里再出一套「谨慎/中枢/乐观目标价」会变成两套价位并存,"
             "读者只能挑一个信,而两套的算法其实不一样。")
    L.append("")
    if rules:
        lens = {"pe31": "3+1 PE 带", "pb": "PB 全历史分位",
                "ps31+pb": "3+1 PS 带 + PB 全历史分位", None: "**没有可用的尺**"}
        L.append(f"按第 0.5 节,这只票的行业(**{sector.get('行业') or '未定义'}**)"
                 f"该用的是 **{lens.get(rules.get('估值尺'), rules.get('估值尺'))}**。"
                 f"{rules.get('估值尺说明', '')}")
        L.append("")
    ps = v.get("PS带") or {}
    pb2 = v.get("PB带") or {}
    orev = v.get("我们预测的未来四季营收")
    L.append("| 尺 | 现在的值 | p25 | p50 | p75 | p90 | 样本 |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    if b.get("ok") and our and spot:
        pe_now = spot["close"] * v["总股本"] / our["mid"] if our["mid"] else None
        L.append(f"| 3+1 PE | " + (f"{pe_now:.1f}x" if pe_now else "—")
                 + f" | {b['p25']:.1f}x | {b['p50']:.1f}x | {b['p75']:.1f}x "
                 f"| {b['p90']:.1f}x | {b['n']} 个交易日 |")
    if ps.get("ok") and orev and spot:
        ps_now = spot["close"] * v["总股本"] / orev["mid"] if orev["mid"] else None
        L.append(f"| 3+1 PS | " + (f"{ps_now:.2f}x" if ps_now else "—")
                 + f" | {ps['p25']:.2f}x | {ps['p50']:.2f}x | {ps['p75']:.2f}x "
                 f"| {ps['p90']:.2f}x | {ps['n']} 个交易日 |")
    if pb2.get("ok"):
        L.append(f"| PB | {pb2['现值']:.2f}(**{pb2['现值分位']:.0f}% 分位**) "
                 f"| {pb2['p25']:.2f} | {pb2['p50']:.2f} | {pb2['p75']:.2f} "
                 f"| {pb2['p90']:.2f} | {pb2['n']} 个交易日 |")
    L.append("")
    L.append("**三把尺分别在回答什么**:PE 问「市场为一块钱**利润**付多少倍」;"
             "PS 问「为一块钱**营收**付多少倍」,它不管赚不赚钱,"
             "所以只在利润这把尺因为低基数失效时顶上;"
             "PB 问「为一块钱**净资产**付多少倍」,它的分母不随景气一年翻几倍,"
             "所以是周期股唯一稳的尺。")
    L.append("")
    for k, why in (rules or {}).get("展示尺", []):
        lab = {"pe31": "3+1 PE", "pb": "PB", "ps31": "3+1 PS"}.get(k, k)
        L.append(f"> ⚠ **{lab} 在这个行业上只展示不打分**:{why}")
        L.append("")
    L.append("**这套估值怎么被推翻**:")
    L.append("")
    L.append("- 净利(或营收)区间错了 —— 见第 3 节各方法的失效条件")
    L.append("- 倍数选错了 —— 我们假设市场会按这只票**自己**的历史规律给倍数。"
             "如果行业的定价逻辑变了(比如从成长股变成周期股),历史带就不再适用。"
             "**这正是第 0.5 节存在的理由**:先判行业,才知道该拿哪条历史来比")
    L.append(f"- 历史带本身样本不够 —— 现在是 {b['n']} 个交易日、"
             f"{b['从'][:4]}~{b['到'][:4]} 年。这段时间如果只覆盖了一轮景气周期,"
             f"带的上下沿都会偏窄")
    L.append("")


def sec_how(code, v, raw, L):
    L.append("## 12 数据来源:每条都能点开核对")
    L.append("")
    L.append("**下面只列这一轮盘上真有文件的源。** 按每条的「落在哪」路径 glob 过一遍,"
             "查无文件的挪到表尾单列 —— 列一条打不开的来源比不列更糟,"
             "它让人以为核对过了。")
    L.append("")
    L.append(catalog.markdown(
        ["行情", "公司报表", "公司自己说的", "筹码", "海外·事实", "行业量价",
         "★ 别人的预测", "人工投喂"], code, heading=None,
        root=raw.parent.parent).lstrip("\n"))
    L.append("**这一版新增的三类数据**:")
    L.append("")
    L.append("| 用在哪 | 数据 | 落在哪 | 怎么重跑 |")
    L.append("|---|---|---|---|")
    L.append(f"| 第 0.5 节 行业 | 东财所属板块 → `sectors.classify()` "
             f"| `data/raw/sectors/{code}/<日期>.json` "
             f"| `tools/fetch_all.py --codes {code} --group sectors` |")
    L.append("| 第 3.5 节 基准率 | 东财全市场季度业绩(12 期) "
             "| `data/perf_cache/*.parquet` "
             "| `$VENV/bin/python tools/baserate.py growth` |")
    L.append("| 第 4.5 节 云厂指引 | 电话会纪要正文 "
             "| `data/raw/transcripts/<公司>/<日期>.txt` "
             "| `tools/fetch_all.py --transcripts <URL>` |")
    L.append("")
    L.append("**这份报告不做的事**:不引用任何机构的盈利预测作为输入;"
             "不用新闻和市场情绪;不做技术分析。"
             "第 11 节的建议**只建立在这份报告里的数据上** —— "
             "依据和失效条件都写出来了,可以逐条质疑。")
    L.append("")
    return


def build(code: str, name: str, raw: Path, target: str | None,
          holdings: Path | None = None) -> str:
    position = pos.load_position(holdings, code)
    cost = position.get("cost")
    v = valuation.build(code, raw, target)
    f = v["预测"]
    d = quarterly.build(raw, code, since="2023")
    mkt = rm.market_view(raw, code)

    # 3+1 净利相对再往前四季的同比 —— 选倍数要用。
    # dict 的 key 沿用旧名「未来四季净利同比」,只改用户看得到的文字。
    hist = [r["单季归母净利"] for r in d["quarters"] if r.get("单季归母净利") is not None]
    our = v["我们预测的未来四季净利"]
    if our and len(hist) >= 7:
        prev4 = sum(hist[-7:-3])
        v["未来四季净利同比"] = (our["mid"] / prev4 - 1) * 100 if prev4 else None
    else:
        v["未来四季净利同比"] = None

    spot = v["现价"]
    L = [f"# {name}({code}) 持仓决策报告", ""]
    L.append(f"生成于 {date.today().isoformat()} · 最新已披露 **{f['最新已披露']}**"
             + (f" · 收盘价 {spot['close']:.2f} 元({spot['date']})" if spot else ""))
    L.append("")
    L.append("> **这份报告的结论全部建在公司自己披露的报表和 SEC 申报原值上,"
             "不引用任何机构的盈利预测。** 市场怎么看单独放在第 9 节,只作事后对照。")
    L.append("")
    pos.sec_position(raw, code, position, spot, L)
    # 0.5 先判行业 —— **后面每一节用哪把尺由它决定**,所以它必须在结论之前。
    # (`check_flow_contract.py` 卡住这个顺序:行业层在结论节之前被调用。)
    sector = sector_rules.load(raw, code)
    rules = sector["规则"]
    sector_rules.render(sector, "0.5", L)
    adv = advice.build(v, f, position)
    sec_conclusion(v, f, mkt, name, code, L, cost, adv)
    sec_facts(d, L)
    quarterly.render_cash_orders(quarterly.cash_and_orders(d), "2.1", L)
    sec_forecast(f, L, outside=adv["基准率"], lead=adv["领先"])
    sec_valuation(v, f, L, sector, rules)
    pos.sec_chips(raw, code, 6, L)
    pos.sec_technical(raw, code, cost, spot, 7, L)
    # ⚠ **同业 ≠ 上游。** 这里借上游的分组键去找同业组,只是因为两者恰好一一对应,
    #   不是同一件事:SLB/HAL/BKR 是杰瑞的**同行**(第 8 节对照它们),
    #   而杰瑞的**上游**是三桶油(第 4 节拿它们当分母)。改一边不要顺手改另一边。
    #   深科达(panel_osat)没有对应的海外同业组 → 传 None,第 8 节会说明原因。
    grp = forecast.UPSTREAM_OF.get(code)
    pos.sec_peers(raw, {"cloud": "AI算力链-同业", "oil_cn": "油气设服"}.get(grp), 8, L)
    rm.sec_market(mkt, L, rm.consensus_gap(v, d, mkt))
    # 9.3 外部研报 —— **有就必须列,而且要正面回应方向差**。
    _dd = adv.get("决策") or {}
    rm.sec_extern(rm.extern_research(raw.parent.parent, code),
               _dd.get("EV"), adv.get("动作", "—"), spot, L)
    # ⚠ `pick_multiple` 只剩第 5 节没用到的历史包袱,第 10 节的参考价位改成
    #   直接用第 11.1 节那三个情景价 —— 一份报告里不能有两套「最谨慎价」。
    pos.sec_action(v, f, d, cost, spot, None, 10, L, dec=adv.get("决策"))
    # 第 10 节末尾把这次的判断写进决策日志,并把那条记录印出来。
    # **写在这里而不是最后**:第 10 节讲的就是「怎么核对」,
    # 而 journal 那条就是复核时要拿出来对的东西 —— 两者是同一件事。
    cash = quarterly.cash_and_orders(d)
    gm4 = [r["单季毛利率"] for r in d["quarters"] if r["单季毛利率"] is not None][-4:]
    rec = journal_log.write(raw, code, adv, f, position, cash,
                            min(gm4) if gm4 else None)
    journal_log.render(rec, "10.1", L)
    advice.render(adv, v, f, position, 11, L)
    sec_how(code, v, raw, L)
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("code")
    ap.add_argument("--name", default="")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--target")
    ap.add_argument("--out")
    ap.add_argument("--holdings", default="private/portfolio/account.json",
                    help="持仓文件。优先给 account.json(有股数/仓位/融资负债),"
                         "退而求其次 holdings.json(只有成本)")
    a = ap.parse_args()
    hp = Path(a.holdings) if a.holdings else None
    md = build(a.code, a.name or a.code, Path(a.raw), a.target,
               hp if hp and hp.exists() else None)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
