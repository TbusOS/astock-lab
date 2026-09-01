#!/usr/bin/env python3
"""买卖建议 —— 三个维度打分,规则写死在代码里。

为什么要写成规则而不是每次现想:
    每次现想的问题不是想得不对,是**五只票用的标准不一样**,而且下次跑
    换个心情就变了。写成规则有三个好处:五只同一把尺、可以被质疑
    (「你凭什么给估值 -2 分」是个能回答的问题)、下季度重跑能看出
    是数据变了还是我改了标准。

三个维度,每个都来自前面几节算出来的数,不引入新信息:

    ① 估值   = 现价前瞻 PE ÷ 这只票**自己**历史前瞻 PE 的 p50
       用比值不用绝对 PE —— 油服和光模块的合理倍数不在一个量级。
    ② 基本面 = 相对上游开支的增速倍数(水平 + 趋势)
       水平决定得没得份额,趋势决定这个优势在扩大还是收窄。
    ③ 确定性 = 三个预测方法里有几个通过了自己的适用性检查
       只有 1 个的时候,那个区间没有任何东西可以印证它。

★ 这份建议会错的地方:
    · 三个维度**都不含估值以外的价格信息** —— 不看 K 线走势、不看资金流,
      所以它不会告诉你「今天买还是下周买」
    · 基本面这一维只看**相对上游的份额变化**,不看毛利率、不看新产品 ——
      那些在报告的第 2、3 节,要自己看
    · 分数是离散的,1.79x 和 1.81x 会掉进不同档。**边界附近别当真**
"""
from __future__ import annotations

# ── 三把尺 ────────────────────────────────────────────────────────────
def score_valuation(rel: float | None):
    """rel = 现价前瞻 PE ÷ 自己历史 p50。"""
    if rel is None:
        return 0, "算不出自己的历史前瞻 PE 带,估值这一维给 0 分"
    if rel < 0.8:
        return 2, f"现价只有自己历史中位的 {rel:.2f} 倍 —— **便宜**"
    if rel < 1.2:
        return 1, f"现价是自己历史中位的 {rel:.2f} 倍 —— **合理**"
    if rel < 1.8:
        return 0, f"现价是自己历史中位的 {rel:.2f} 倍 —— **偏贵**"
    if rel < 2.5:
        return -1, f"现价是自己历史中位的 {rel:.2f} 倍 —— **贵**"
    return -2, f"现价是自己历史中位的 {rel:.2f} 倍 —— **很贵**"


def score_chain(cp: dict | None):
    if not cp:
        return 0, "没有对应的上游数据源,这一维给 0 分(但确定性要降一档)"
    lv, tr = cp["终点"], cp["趋势"]
    if lv > 1.15 and tr == "优势在扩大":
        return 2, f"增速是上游开支的 {lv:.2f} 倍且**还在扩大** —— 在抢份额,而且越抢越快"
    if lv > 1.15:
        return 1, f"增速是上游开支的 {lv:.2f} 倍 —— 在得份额,但{tr}"
    if lv >= 0.85:
        return 0, f"增速倍数 {lv:.2f} —— 和上游基本同步,没有超额,{tr}"
    if tr == "优势在收窄":
        return -2, f"增速只有上游开支的 {lv:.2f} 倍且**还在往下** —— 行业在扩它没跟上"
    return -1, f"增速只有上游开支的 {lv:.2f} 倍 —— 慢于行业"


def score_confidence(n: int, has_chain: bool):
    base = {3: 1, 2: 0, 1: -1, 0: -2}.get(n, 0)
    why = f"三个预测方法里 {n} 个适用"
    if not has_chain:
        base -= 1
        why += ",而且产业链位置也算不了 —— 再降一档"
    return base, why


# ── 打分 → 动作 ───────────────────────────────────────────────────────
ACTIONS = [
    (3, "买入 / 加仓", "长期(1 年以上,跨多个季报)",
     "基本面在变好而且估值不贵,这种组合不常见"),
    (1, "持有", "长期(1 年以上)", "值得拿着,但现在不是加仓的价位"),
    (0, "持有,但不加仓", "中期(2~4 个季度)",
     "没有明显的错,也没有明显的超额 —— 拿着看下个季报"),
    (-2, "减仓", "短期执行(1 个月内),保留底仓",
     "估值或基本面有一条明确变差,先把仓位降下来"),
    (-99, "清仓", "短期执行(1 个月内)",
     "估值和基本面同时不利,没有继续持有的理由"),
]


def decide(total: int):
    for cut, act, horizon, why in ACTIONS:
        if total >= cut:
            return act, horizon, why
    return ACTIONS[-1][1:]


def build(v: dict, f: dict, position: dict) -> dict:
    """三维打分 + 动作。所有输入都来自前面几节,不引入新信息。"""
    band, spot, our = v["前瞻PE带"], v["现价"], v["我们预测的未来四季净利"]
    rel = None
    if band.get("ok") and spot and our and our.get("mid"):
        pe = spot["close"] * v["总股本"] / our["mid"]
        rel = pe / band["p50"] if band["p50"] else None
    cp = f.get("产业链位置")
    sv, wv = score_valuation(rel)
    sc, wc = score_chain(cp)
    sf, wf = score_confidence(f["可用方法数"], bool(cp))
    total = sv + sc + sf
    act, horizon, why = decide(total)
    return {"估值": (sv, wv), "基本面": (sc, wc), "确定性": (sf, wf),
            "总分": total, "动作": act, "期限": horizon, "档位理由": why,
            "rel": rel, "有融资负债": bool(position.get("margin_debt"))}


# ── 渲染 ──────────────────────────────────────────────────────────────
def render(a: dict, v: dict, f: dict, position: dict, n: int, L: list) -> None:
    spot, our = v["现价"], v["我们预测的未来四季净利"]
    band = v["前瞻PE带"]
    L.append(f"## {n} 买卖建议")
    L.append("")
    L.append(f"# → **{a['动作']}**　·　**{a['期限']}**")
    L.append("")
    L.append(f"{a['档位理由']}。下面是这个结论怎么来的 —— 三个维度各打一次分,"
             f"**规则写死在代码里,五只票用同一把尺**。")
    L.append("")
    L.append("| 维度 | 分 | 依据 |")
    L.append("|---|---:|---|")
    for k in ("估值", "基本面", "确定性"):
        s, w = a[k]
        L.append(f"| {k} | **{s:+d}** | {w} |")
    L.append(f"| **合计** | **{a['总分']:+d}** | ≥3 买入 · 1~2 持有 · 0 持有不加仓 · "
             f"−1~−2 减仓 · ≤−3 清仓 |")
    L.append("")

    # 具体怎么做
    L.append("### 具体怎么做")
    L.append("")
    cost = position.get("cost")
    px = spot["close"] if spot else None
    if a["动作"].startswith("清仓"):
        L.append(f"- **分 2~3 笔卖完**,不要一次挂市价 —— "
                 f"这几只日均成交额够,但一次性大单会自己把价格砸下去")
        if cost and px:
            pl = (px / cost - 1) * 100
            L.append(f"- 你的成本 {cost:,.3f},现价 {px:,.2f},"
                     f"卖出会**实现 {pl:+.1f}% 的{'亏损' if pl < 0 else '盈利'}**。"
                     + ("浮亏变实亏心理上难,但**留着它的理由已经不成立了** —— "
                        "继续拿是在赌一个我们没有依据支持的反弹。"
                        if pl < 0 else "这笔盈利可以变成账户的安全垫。"))
    elif a["动作"].startswith("减仓"):
        L.append("- **先减一半**,剩下的等下个季报再定 —— "
                 "我们的判断有明确的验证时点,不用现在就赌到底")
        if position.get("margin_debt"):
            L.append(f"- **这只带 {position['margin_debt']:,.0f} 元融资负债**,"
                     f"减仓换来的现金优先还债,不要转去买别的 —— "
                     f"降杠杆的收益是确定的,换股的收益不是")
        L.append("- 不要等反弹再减。「等回本再走」是最贵的一句话:"
                 "它把卖出的条件从「判断变了」换成了「价格回到某个数」,"
                 "而价格不知道你的成本是多少")
    else:
        L.append("- **不加仓。** 现价对应我们算的前瞻 PE 已经"
                 + (f"是自己历史中位的 {a['rel']:.2f} 倍" if a["rel"] else "偏高")
                 + ",这个位置买入是在赌乐观档兑现")
        if band.get("ok") and our and v.get("总股本"):
            import valuation
            pick = valuation.pick_multiple(band, v.get("未来四季净利同比"))
            if pick.get("ok"):
                tp = our["low"] * pick["低"] / v["总股本"]
                L.append(f"- 想加仓的话,**参考价位是 {tp:,.0f} 元**"
                         f"(我们最谨慎那一档:净利下沿 × 倍数下沿)。"
                         + (f"现价 {px:,.2f} 比它高 {(px / tp - 1) * 100:.0f}%。"
                            if px and px > tp else "现价已经在它下方。"))
        L.append(f"- **下一个决策点是 {f['预测期']} 季报**。"
                 f"届时拿实际值对第 3 节的区间和第 10 节的条件,逐条核。")
    L.append("")

    L.append("### 什么情况下这条建议就错了")
    L.append("")
    L.append("- **估值那一维**:自己的历史前瞻 PE 带只有 "
             + (f"{band['n']} 个交易日、{band['从'][:4]}~{band['到'][:4]} 年"
                if band.get("ok") else "算不出来")
             + "。如果这段时间只覆盖了一轮景气周期,带的上下沿都会偏窄,"
               "「贵」这个判断就站不住")
    L.append("- **基本面那一维**:只看相对上游开支的份额变化,"
             "**不看毛利率、不看新产品**。这两样在第 2、3 节,要自己看 —— "
             "一家公司完全可能份额在丢但利润率在升")
    L.append("- **确定性那一维**:方法数少不等于结论错,"
             "只是没有第二个来印证。它降的是**把握**,不是**方向**")
    L.append("- **整套规则不含任何价格信息**(除了估值比值)—— "
             "不看 K 线走势、不看资金流,所以它不回答「今天买还是下周买」")
    L.append("- 分数是离散的,1.79x 和 1.81x 会掉进不同档。**边界附近别当真**")
    L.append("")
    L.append("> 这是**基于上面这些数据的建议**,依据和失效条件都写出来了,"
             "你可以逐条质疑。最终决定和后果是你的。")
    L.append("")
