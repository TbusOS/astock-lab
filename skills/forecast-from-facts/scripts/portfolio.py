#!/usr/bin/env python3
"""五只并排 —— 单看一只看不出相对贵贱,并排才能排序。

    portfolio.py 300502:新易盛 300308:中际旭创 … [--raw data/raw] [--out 组合.md]

为什么要并排:
    「现价对应 3+1 PE 36.3x」单看是个中性的数。放到这只票**自己**的历史带里
    (p50=19.8x)才知道它贵;再和另外四只的「相对自己历史的贵贱」比,
    才知道该先减哪一只。绝对 PE 跨行业比是没有意义的 ——
    油服和光模块的合理倍数本来就不在一个量级。

排序用什么:
    现价 3+1 PE ÷ 这只票自己历史 3+1 PE 的 p50。
    这个比值消掉了行业差异,回答的是「相对它自己,现在贵了几倍」。

3+1 PE 是什么:
    市值 ÷(最近三季已披露归母净利 + 下一季归母净利)。历史带里那个「下一季」
    是事后实际,现价那一格是我们的预测 —— 两边唯一的差别就是最后那一季。
    **乘数和利润必须算同一段时间**,否则增速越快的票偏得越多,而数字看着都正常。

依赖:无。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# source_catalog 住在 data-sources 那个 skill 下 —— 数据源目录属于取数,不属于预测。
# 两个 skill 的 scripts 目录都加进 sys.path,tools/ 软链和仓内真实路径都能跑。
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent.parent / "data-sources" / "scripts"))


import advice
import quarterly
import source_catalog as catalog
import valuation


def yi(v, nd=1):
    return "—" if v is None else f"{v / 1e8:,.{nd}f}"


def one(code: str, name: str, raw: Path, position: dict | None = None):
    v = valuation.build(code, raw, None)
    f = v["预测"]
    d = quarterly.build(raw, code, since="2023")
    hist = [r["单季归母净利"] for r in d["quarters"] if r.get("单季归母净利") is not None]
    our = v["我们预测的未来四季净利"]
    b, spot = v["前瞻PE带"], v["现价"]
    if not (our and spot):
        return None
    cap = spot["close"] * v["总股本"]
    pe = cap / our["mid"] if our["mid"] else None
    p50 = b["p50"] if b.get("ok") else None
    prev4 = sum(hist[-7:-3]) if len(hist) >= 7 else None
    return {
        "code": code, "name": name, "价": spot["close"], "日": spot["date"],
        "市值": cap, "预测净利": our, "pe31": pe, "带": b,
        "相对自己历史": pe / p50 if pe and p50 else None,
        "净利同比": (our["mid"] / prev4 - 1) * 100 if prev4 else None,
        "方法数": f["可用方法数"], "预测期": f["预测期"],
        "产业链": f.get("产业链位置"), "上游说明": f.get("上游说明"),
        "预测原文": f,
        "建议": advice.build(v, f, position or {}),
        "持仓": position or {},
        "单季": d["quarters"],
    }


def build(pairs, raw: Path, holdings: Path | None = None) -> str:
    import sections_position as sp
    rows = [r for r in (one(c, n, raw, sp.load_position(holdings, c))
                        for c, n in pairs) if r]
    # ⚠ **按期望值从差到好排,不再按「相对自己历史贵多少」。**
    #   贵不贵只是三个情景之一的输入;期望值把贵贱、增长、领先信号和概率
    #   全折算成同一个数,才是「该先动哪只」的依据。
    #   算不出期望值的排最后 —— 那是「不知道」,不是「不好不坏」。
    def ev_key(r):
        ev = (r["建议"].get("决策") or {}).get("EV")
        return (1, 0.0) if ev is None else (0, ev)
    rows.sort(key=ev_key)

    L = ["# 五只持仓 · 并排对照", ""]
    L.append(f"生成于 {date.today().isoformat()} · 收盘价截至 "
             f"{max(r['日'] for r in rows)}")
    L.append("")
    L.append("> 全部结论建在公司自己披露的报表和 SEC 申报原值上,"
             "**不引用任何机构的盈利预测**。每只票的完整推导见各自那份报告。")
    L.append("")

    L.append("## 1 五只并排:行业 · 三情景 · 概率 · 期望值 · 仓位上限 · 动作")
    L.append("")
    L.append("**按期望值从差到好排** —— 期望值把贵贱、增长、领先信号和概率"
             "全折算成同一个数,所以它才是「该先动哪只」的依据。"
             "「相对自己历史贵多少」只是其中一个输入,单看它会漏掉另外三样。")
    L.append("")
    L.append("| 股票 | 行业 | bear | base | bull | 概率 "
             "| **期望值** | 确信度 | 上限 vs 现仓 | **动作** |")
    L.append("|---|---|---:|---:|---:|---|---:|---|---|---|")
    for r in rows:
        a_ = r["建议"]
        d = a_.get("决策") or {}
        if not d.get("ok"):
            L.append(f"| {r['name']} {r['code']} | {a_.get('行业') or '—'} "
                     f"| — | — | — | — | — | — | — | **{a_['动作']}** |")
            continue
        sc, pr = d["三情景"]["情景"], d["概率"]["probs"]
        pc = d["仓位上限"]
        cap_txt = "算不出"
        if pc.get("ok"):
            cap_txt = (f"{pc['上限pct']:.1f}% vs "
                       + (f"**{pc['现仓位pct']:.1f}%**"
                          if pc.get("现仓位pct") is not None else "—"))
            if d.get("超限"):
                cap_txt += " ⚠超"
        L.append(f"| {r['name']} {r['code']} | {a_.get('行业') or '—'} "
                 f"| {sc['bear']['价']:,.2f}({sc['bear']['涨跌']:+.1f}%) "
                 f"| {sc['base']['价']:,.2f}({sc['base']['涨跌']:+.1f}%) "
                 f"| {sc['bull']['价']:,.2f}({sc['bull']['涨跌']:+.1f}%) "
                 f"| {pr['bear'] * 100:.0f}/{pr['base'] * 100:.0f}/{pr['bull'] * 100:.0f} "
                 f"| **{d['EV']:+.1f}%** | {d['确信度']['分']}/{d['确信度']['满分']} {d['确信度']['档']} "
                 f"| {cap_txt} | **{a_['动作']}** |")
    L.append("")
    L.append(f"**五只的复核日都是 {rows[0]['建议']['决策'].get('复核日', '—')}** —— "
             f"那是三季报的法定披露截止日。那天回来拿实际值对各自报告第 3 节的区间、"
             f"第 10 节的条件、第 11.1 的三个价位,逐条核。")
    L.append("")
    import decision as _d
    L.append(f"**「概率」那一列是 bear/base/bull 三个数**,四步算出来的:"
             f"起点 {_d.START_PROBS['bear']*100:.0f}/{_d.START_PROBS['base']*100:.0f}"
             f"/{_d.START_PROBS['bull']*100:.0f} → 领先层(每个 ↑ 把 "
             f"{_d.LEAD_STEP_PP:.0f} 个百分点从 bear 挪到 bull,"
             f"合计最多挪 {_d.LEAD_MAX_PP:.0f})→ 方法层(只剩一个方法或方法互相矛盾时,"
             f"base 往两边各拨 {_d.METHOD_SPREAD_PP:.0f})→ ③b 尾部不超过中枢 "
             f"→ 基准率封顶(bull ≤ 历史上同类公司 4 季后仍 ≥50% 的比例)。"
             f"**每只的四步中间值在各自报告的第 11.2 节整张印出来。**")
    L.append("")
    L.append("**动作只由期望值决定**:≤−20% 清仓 · −20~−8% 减仓 · "
             "−8~+8% 持有不加仓 · +8~+20% 持有 · >+20% 且确信度比例 ≥ 2/3 可加仓(比例 = 得分 ÷ 本行业可得分)。"
             "**但超过仓位上限的,不论期望值先减到上限** —— "
             "上限从 bear 情景倒推,期望为正不代表你扛得住最坏那一档。")
    L.append("")
    L.append("**三情景走哪条路,按各行业声明的估值尺分派**(不按「成长 / 周期」这个标签):"
             "尺是 `pe31` 的走利润 —— 3+1 PE 带的 p25/p50/p75 乘我们预测的 "
             "3+1 净利下沿/中枢/上沿;尺是 `pb` / `ps31+pb` 的走净资产 —— "
             "价位 =(最近归母净资产 + 下季预测净利)÷ 总股本 × 目标 PB,"
             "目标 PB 取 bear = 自身历史 p50、base = **现值**、"
             "bull = 现值(已高于 p75 时)或 p75。"
             "**周期股不能看 PE**:盈利低谷时利润接近 0,PE 飙到几百倍看着「很贵」,"
             "而那正是该买的位置;高峰反过来。两次都是反的,数字却完全正常。"
             "**尺声明为 None 的行业、以及行业只靠宽泛板块名弱匹配出来的票,一律不给动作。**")
    L.append("")
    flat = [r for r in rows
            if (((r["建议"].get("决策") or {}).get("三情景") or {}).get("路径") == "PB"
                and ((r["建议"].get("决策") or {}).get("三情景") or {}).get("档") == "高于 p75")]
    if flat:
        L.append("> ⚠ **" + "、".join(r["name"] for r in flat)
                 + " 现在的 PB 已经高于自己历史的 p75**,所以 bull 的目标 PB 只能取现值 ——"
                 "和 base 是同一个倍数。**这几只现在没有「估值继续扩张」这个乐观情景**:"
                 "bull 和 base 的差别只剩下「净利落在预测区间上沿还是中枢」。"
                 "比这更高的价位需要报表之外的新信息,而这套方法看不到那些。")
        L.append("")
    L.append("### 1.1 同一行业合计的仓位上限")
    L.append("")
    L.append("同行业的票会一起跌。三只光模块分开看各自不超限,合起来在 bear 情景下"
             "能让净资产掉三成 —— **「分散到三只」并没有分散风险,"
             "只是把同一个赌注写成了三行。**")
    L.append("")
    acct0 = next((r["持仓"].get("账户") for r in rows if r["持仓"].get("账户")), None)
    if acct0:
        import decision
        sec = decision.sector_cap(
            [{"行业": r["建议"].get("行业"),
              "市值": r["持仓"].get("market_value") or 0,
              "bear跌幅": (((r["建议"].get("决策") or {}).get("三情景") or {})
                          .get("情景", {}).get("bear", {}).get("涨跌"))}
             for r in rows if r["持仓"].get("market_value")], acct0)
        if sec.get("ok"):
            L.append("| 行业 | 几只 | 合计市值 | 加权 bear 跌幅 | 模型上限 | 硬线上限 "
                     "| **实际上限** | 哪条起作用 | 上限金额 | 现在占净资产 | 超出 |")
            L.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")
            for name, x in sorted(sec["按行业"].items(),
                                  key=lambda kv: -(kv[1].get("市值") or 0)):
                if x.get("ok"):
                    L.append(f"| **{name}** | {x['只数']} | {x['市值']:,.0f} "
                             f"| {x['加权bear跌幅']:+.1f}% | {x['模型上限pct']:.1f}% "
                             + (f"| {x['硬上限pct']:.0f}% " if x.get("硬上限pct") is not None
                                else "| 没设 ")
                             + f"| **{x['上限pct']:.1f}%** | {x['起作用的']} "
                             f"| {x['上限金额']:,.0f} | **{x['现仓位pct']:.1f}%** | "
                             + (f"**{x['超出金额']:,.0f}**" if x["超出金额"] > 0 else "—")
                             + " |")
                else:
                    L.append(f"| {name} | — | {x.get('市值', 0):,.0f} | — | 算不出 | — "
                             f"| 算不出 | — | — | — | {x.get('why', '')} |")
            L.append("")
            L.append(f"**模型上限** = 净资产的 {sec['可承受亏损pct']}% ÷ "
                     f"|该行业市值加权 bear 跌幅|;**硬线上限**是一条和估值模型"
                     f"完全无关的集中度线。两条取小的那条起作用。"
                     f"这几个百分比都是 `account.json` 里的显式参数,"
                     f"**你可以改,改了要在同一个文件里写理由**。")
        else:
            L.append(f"算不出来:{sec.get('why')}")
    else:
        L.append("没有账户数据(没给 `--holdings`),这一节算不了。")
    L.append("")
    _per_arrow(rows, L)
    L.append("## 2 按「相对自己历史贵多少」排序")
    L.append("")
    L.append("绝对 PE 跨行业比没有意义 —— 油服和光模块的合理倍数本来就不在一个量级。"
             "所以这里用**现价 3+1 PE ÷ 这只票自己历史 3+1 PE 的 p50**,"
             "消掉行业差异,回答「相对它自己,现在贵了几倍」。")
    L.append("")
    L.append("> **3+1 PE = 市值 ÷(最近三季已披露归母净利 + 下一季归母净利)。**"
             "历史带里那个「下一季」是**事后实际**,现价那一格是**我们的预测** —— "
             "两边唯一的差别就是最后那一季,前三季两边都是已披露的实际值。"
             "两边算的不是同一段时间的话,增速越快的票偏得越多,而数字看着全都正常。")
    L.append("")
    L.append("| 排 | 代码 | 名称 | 现价 | 市值(亿) | 我们的 3+1 净利(亿) "
             "| 现价 3+1 PE | 自己历史p50 | **相对自己** | 预测把握 |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(rows, 1):
        b = r["带"]
        rel = r["相对自己历史"]
        L.append(f"| {i} | {r['code']} | {r['name']} | {r['价']:.2f} | {yi(r['市值'], 0)} "
                 f"| {yi(r['预测净利']['low'])} ~ {yi(r['预测净利']['high'])} "
                 f"| {r['pe31']:.1f}x | "
                 + (f"{b['p50']:.1f}x" if b.get("ok") else "—") + " | "
                 + (f"**{rel:.2f}x**" if rel else "—") + " | "
                 + (f"{r['方法数']}/3 个方法" if r["方法数"] else "**0/3,推不出**") + " |")
    L.append("")
    L.append("**「预测把握」那一列是什么意思** —— 我们用三个互相独立的方法推下季营收,"
             "每个方法先自检适不适用这只票,不适用就退出:")
    L.append("")
    L.append("| 方法 | 怎么算 | 什么时候判定不适用 |")
    L.append("|---|---|---|")
    L.append("| **存货法** | 上季末存货 × 历史「下季营收÷上季末存货」比值区间。"
             "看的是**供给侧已经花出去的钱** | 近四季比值离散度 > 30%,"
             "多半被季节性交付压过去了 |")
    L.append("| **同比法** | 去年同期营收 × 近三季同比增速区间。"
             "看的是**需求侧的增长趋势**,同比自动消掉季节性 | "
             "近三季同比跨度 > 100 个百分点,增速在剧烈拐弯,外推没意义 |")
    L.append("| **环比法** | 上季营收 × 历年**同一个季度转换**的环比倍数。"
             "看的是**这门生意的季节形状** | 历年同一转换的环比离散度 > 45%,"
             "或样本少于 2 个 |")
    L.append("")
    L.append("三者共用的只有营收这一个观测量,**出错的原因互不相同** —— "
             "所以三个都指向同一区间才算真的有把握。只剩 1 个的,"
             "那个区间没有任何东西可以印证它,要打折看。")
    L.append("")
    L.append("| 代码 | 名称 | 适用的方法 | 不适用的 |")
    L.append("|---|---|---|---|")
    for r in rows:
        f_ = r["预测原文"]
        ok = "、".join(m["name"] for m in f_["methods"] if m.get("ok")) or "**无**"
        bad = "、".join(m["name"] for m in f_["methods"] if not m.get("ok")) or "—"
        L.append(f"| {r['code']} | {r['name']} | {ok} | {bad} |")
    L.append("")

    L.append("## 3 每只在产业链里的位置")
    L.append("")
    L.append("| 代码 | 名称 | 上游是谁 | 期间 | 增速倍数变化 | 判断 |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        cp = r["产业链"]
        if cp:
            L.append(f"| {r['code']} | {r['name']} | {cp['上游']} | {cp['期间']} "
                     f"| {cp['起点']:.2f}x → {cp['终点']:.2f}x | {cp['判断']} |")
        else:
            # ⚠ 别再写死「没有对应数据源」—— 算不了有三种原因(没登记 / 数据缺 /
            #   可比点不够),它们该做的事完全不同。原因从 forecast 的说明里取。
            why = (r.get("上游说明") or "算不了").replace("\n", " ")
            L.append(f"| {r['code']} | {r['name']} | — | — | — | 算不了 —— {why} |")
    L.append("")
    L.append("> 倍数 >1 = 增速快于上游总开支(在得份额);倍数在降 = 这个优势在收窄。"
             "**水平和趋势是两件事**,可以同时成立。"
             "这个倍数本身不稳(历史离散度 70% 以上),**不能拿来推营收**,只看方向。")
    L.append("")

    L.append("## 4 最近一季的实际经营(全部是公司自己报的数)")
    L.append("")
    L.append("| 代码 | 名称 | 上季营收(亿) | 同比 | 毛利率 | 期末存货(亿) | 预付款(亿) | 财务费用(亿) |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        q = r["单季"][-1]
        L.append(f"| {r['code']} | {r['name']} | {yi(q['单季营收'])} "
                 + (f"| {q['单季营收同比']:.1f}% " if q['单季营收同比'] is not None else "| — ")
                 + (f"| {q['单季毛利率']:.1f}% " if q['单季毛利率'] is not None else "| — ")
                 + f"| {yi(q['期末存货'])} | {yi(q['期末预付款'], 2)} "
                 + f"| {yi(q['单季财务费用'], 2)} |")
    L.append("")
    L.append("> **预付款**值得单独看:它是抢上游产能、提前锁料的证据,"
             "是已经花出去的钱,不是任何人的说法。"
             "**财务费用**主要是汇兑,和生意好坏无关,但能吃掉几个点的净利率。")
    L.append("")

    L.append("## 5 这批预测已经存档,等季报验")
    L.append("")
    L.append("| 代码 | 名称 | 预测期 | 单季营收(亿) | 单季归母净利(亿) | 用了哪些方法 |")
    L.append("|---|---|---|---:|---:|---|")
    for r in rows:
        f_ = r["预测原文"]
        rev, ni = f_.get("营收区间"), f_.get("归母净利区间")
        ok = "、".join(m["name"] for m in f_["methods"] if m.get("ok")) or "**没有方法适用**"
        L.append(f"| {r['code']} | {r['name']} | {r['预测期']} | "
                 + (f"{yi(rev['low'])} ~ {yi(rev['high'])} " if rev else "— ")
                 + (f"| {yi(ni['low'])} ~ {yi(ni['high'])} " if ni else "| — ")
                 + f"| {ok} |")
    L.append("")
    L.append("存档文件:`private/predictions/<代码>-" + rows[0]["预测期"] + ".json`,"
             "里面记着每个方法当时给的区间、依据和失效条件。")
    L.append("")
    L.append("**存档后不覆盖** —— 预测存档后改它就等于没预测过。"
             "季报出来后对比实际值、记录误差。几个季度之后就知道哪条推法靠谱、"
             "哪条是自己臆想的。没有这个记录,预测准不准永远说不清。")
    L.append("")

    L.append("## 6 这份对照不做的事")
    L.append("")
    L.append("- **不做技术分析。** 建议只建立在估值、份额、确定性三个维度上,"
             "不看 K 线走势、不看资金流 —— 所以它不回答「今天买还是下周买」。")
    L.append("- **不引用机构的盈利预测作为输入。** 各只报告的**第 9 节**"
             "「市场怎么想」有卖方一致预期、券商评级和外部投行研报,"
             "那一节的数据没有进入前面任何一步。")
    L.append("- **不用新闻和市场情绪。** 没有一个数来自新闻。")
    L.append("- **给仓位上限,不给仓位建议。** 上限是从 bear 情景倒推的:"
             "「最坏那一档兑现时,最多亏净资产的 10%」→ 反推最多能拿多少。"
             "它回答的是「最多不能超过多少」,**不回答「该买多少」** —— "
             "后者取决于你还有多少别的钱、这笔钱多久要用,那些不在数据里。")
    L.append("")

    L.append("## 7 数据来源:每条都能点开核对")
    L.append("")
    L.append(catalog.markdown(
        ["行情", "公司报表", "公司自己说的", "海外·事实", "行业量价",
         "★ 别人的预测", "人工投喂"], None, heading=None,
        root=raw.parent.parent).lstrip("\n"))
    L.append("整份对照重跑:")
    L.append("")
    L.append("```bash")
    L.append("cd ~/astock-lab-private")
    L.append("tools/portfolio.py " + " ".join(f"{r['code']}:{r['name']}" for r in rows)
             + " \\")
    L.append("    --out private/reports/<日期>/00-五只并排对照.md")
    L.append("```")
    L.append("")
    L.append("**上游分母是谁**:光模块和 PCB 三只对北美四大云厂资本开支(SEC 申报原值);"
             "杰瑞股份对三桶油(601857/600028/600938);"
             "深科达对国内 7 家面板厂与封测厂(京东方/TCL 科技/深天马/维信诺/"
             "长电科技/通富微电/华天科技)。后两组走各家自己的现金流量表,"
             "和公司营收出自同一套报表。")
    L.append("")
    L.append("> ⚠ 深科达那 7 家是**按它主营(显示模组设备 + 半导体封测设备)推断的客户群**,"
             "不是从年报客户名单来的 —— 它衡量的是这两个下游行业在不在扩产能,"
             "不等于深科达的实际订单来源。")
    L.append("")
    return "\n".join(L) + "\n"


def _per_arrow(rows, L) -> None:
    """**「每箭头 EV 变动」的横向对照 —— 当轮现算,不写死。**

    这个量是 ±8% 那条阈值的校准依据:一个领先指标翻面,概率挪 5 个百分点,
    EV 跟着变 `5% × (bull 涨跌 − bear 涨跌)`。它回答的是
    「这只票的 EV 有多容易被一条新信息推翻」。

    ⚠ 为什么必须在这里现算,而不是写在 `decision.py` 的注释和各只报告里:
    2026-09-02 那一版把五只的实测值**写死**成常量,印在每份个股报告的 11.3 节。
    改过 PB 情景之后那组数当场过期 —— 杰瑞同一段落上一行算出 2.8、
    下一行写着 0.8,深科达 3.4 vs 0.7,而且**过期时不报错**。
    横向对照只能在手上有全部五只的地方算,那就是这份文档。
    """
    got = []
    for r in rows:
        d = r["建议"].get("决策") or {}
        if not d.get("ok"):
            continue
        sc = d["三情景"]["情景"]
        span = sc["bull"]["涨跌"] - sc["bear"]["涨跌"]
        got.append((r["name"], span, decision_step() / 100 * span, d["EV"]))
    if not got:
        return
    cut = abs(_ev_cut())
    L.append(f"### 1.2 一条领先信息翻面,能把期望值推动多少(±{cut:.0f}% 那条线的校准)")
    L.append("")
    L.append("阈值 ±8% 不是拍的,是拿「一条新信息能把 EV 推动多少」来校的:"
             "一个领先指标翻面,概率挪 5 个百分点,"
             "**EV 跟着变 `5% × (bull 涨跌 − bear 涨跌)`**。"
             "阈值定在「两条新信息就能推翻」的量级,意思是 —— "
             "**这么大的 EV 不算有方向**,那一档叫「持有,不加仓」。")
    L.append("")
    L.append("| 股票 | 情景跨度(bull − bear) | 每箭头 EV 变动 | 现在的 EV "
             "| 几个箭头能跨过阈值 |")
    L.append("|---|---:|---:|---:|---:|")
    for name, span, per, ev in sorted(got, key=lambda x: -x[2]):
        n_arrow = abs(cut / per) if per else None
        L.append(f"| {name} | {span:.1f} 个百分点 | **{per:.1f}** | {ev:+.1f}% | "
                 + (f"{n_arrow:.1f} 个" if n_arrow else "—") + " |")
    L.append("")
    L.append("> **这张表当轮现算。** 以前它是写死在代码里的一组常量,"
             "改过三情景的算法之后当场过期 —— 同一段落里前后两个数打架,"
             "而没有任何东西会报错。**横向对照只能在手上有全部五只的地方算**,"
             "个股报告里只印它自己那一个数。")
    L.append("")


def decision_step() -> float:
    import decision
    return decision.LEAD_STEP_PP


def _ev_cut() -> float:
    import decision
    return decision.EV_CUTS[2][0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="+", help="形如 300502:新易盛")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--holdings", default="private/portfolio/account.json",
                    help="持仓与账户。**不给的话仓位上限整列算不出** —— 而超限是「先动哪只」的第一条规则")
    ap.add_argument("--out")
    a = ap.parse_args()
    pairs = []
    for x in a.pairs:
        c, _, n = x.partition(":")
        pairs.append((c.strip(), n.strip() or c.strip()))
    hp = Path(a.holdings) if a.holdings else None
    md = build(pairs, Path(a.raw), hp if hp and hp.exists() else None)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
