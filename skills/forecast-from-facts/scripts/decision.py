#!/usr/bin/env python3
"""决策层 —— 三情景 → 概率 → 期望值 → 动作 → 仓位上限。

## 为什么把「方向」和「大小」拆开

原来那套三维打分把两件事混在一个总分里:**该不该拿**和**该拿多少**。
混在一起的毛病是「确定性 −1」既能把动作从买入推到持有,又说不清
到底该少买多少。这一版拆成两条:

- **方向只由期望值(EV)决定** —— EV 是三个情景的价位按概率加权后的涨跌。
  它回答「这笔钱押下去,平均能赚多少」,是一个能被季报证伪的数。
- **大小只由确信度和仓位上限决定** —— 确信度不改变买还是卖,
  只改变买多少;仓位上限是从 bear 情景倒推出来的,回答
  「万一 bear 兑现,我最多亏掉净资产的百分之几」。

**为什么这比打分好**:三维总分 +3 和 +1 之间那一分,没有任何东西能验证对错。
EV 有 —— 三个季度之后拿实际涨跌回来对,概率给高了给低了一目了然。
所以这一层的每个中间量都存进 `data/journal.jsonl`,不只是存结论。

## 三情景的倍数从哪来:按行业分两条路

| 情景 | 净利 | 倍数(成长行业) | 倍数(周期 / 设备行业) |
|---|---|---|---|
| bear | 3+1 下沿 | 3+1 PE 带 p25 | PB 回到自身历史 p50(现 PB 高于 p50 时);低于 p50 时取 p25 |
| base | 3+1 中枢 | 带 p50 | 现 PB 不变 |
| bull | 3+1 上沿 | 带 p75 | PB 到 p75 |

**成长那条**:价位 = 净利 × 倍数 ÷ 总股本。
**周期那条**:价位 = 现价 × 目标 PB ÷ 现 PB。

⚠ **不再按「增长 ≥60% 就取带上半段」选档。** 对增长的看法只放进概率,
不同时放进倍数 —— 放两处等于把同一件事算两遍,而两次都看着合理。
这和 2026-09-01 那次「TTM PE 分位乘前瞻利润」是同一类错。

⚠ **周期那条路径不用净利区间**:它假设净资产不变、只有估值倍数回归。
这**漏掉了「这一年公司还会赚钱、净资产会增长」**,所以三个价位都偏保守。
换句话说它算的是「如果明天估值就回到历史中位,价格是多少」,不是一年后的价格。

## 概率怎么动:四步,每步都印出来

1. 起点 bear/base/bull = 25/50/25
2. 领先层:每个 ↑ 把 5 个百分点从 bear 挪到 bull,每个 ↓ 反过来,合计最多挪 15
3. 方法层:可用方法 ≥2 且一致 → 不动;只 1 个 → base 各拨 5 给 bear 和 bull
   (更散);方法互相矛盾 → 在此基础上再各拨 5
4. 基准率封顶:bull ≤「同类公司 4 季后仍 ≥50%」那条基准率,超出部分划给 base

**每一步都要能被读者自己复算** —— 所以 `steps` 里记着每一步之后的三个数,
报告把它整张印出来,不是只印最后结果。

依赖:无(读 valuation / forecast / guidance / outside_view 的输出)。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ── 写死的三把尺 ─────────────────────────────────────────────────────
# EV → 动作。**阈值写死在这一处**,五只用同一把尺。
#
# ±8% 这条线怎么定的:拿「一个领先指标翻面能让 EV 变多少」来校。
# 一个箭头挪 LEAD_STEP_PP(5)个百分点的概率,EV 变动 = 5% × (bull 涨跌 − bear 涨跌)。
# 2026-09-02 五只实测:中际旭创 3.8、新易盛 4.0、胜宏 3.1、杰瑞 0.8、深科达 0.7 个百分点。
# 所以 ±8% ≈ **情景铺得宽的票挪两个箭头** —— 也就是「两条领先信息翻面就能把 EV
# 从正翻到负」的量级。这样的 EV 不该当成方向,该叫「持有,不加仓」。
# 每只票自己的这个数印在报告 11.3,读者能看出它离阈值有多近。
EV_CUTS = [(-20.0, "清仓"), (-8.0, "减仓"), (8.0, "持有,不加仓"),
           (20.0, "持有"), (float("inf"), "可加仓")]
ADD_MIN_CONFIDENCE = 2          # 「可加仓」额外要求的确信度下限

START_PROBS = {"bear": 0.25, "base": 0.50, "bull": 0.25}
LEAD_STEP_PP = 5.0              # 每个 ↑/↓ 挪几个百分点
LEAD_MAX_PP = 15.0              # 领先层合计最多挪几个百分点
METHOD_SPREAD_PP = 5.0          # 方法只剩一个 / 互相矛盾时,base 往两边各拨几个

# 确信度 → 仓位系数。**确信度只管大小,不管方向。**
CONF_COEF = {3: ("高", 1.0), 2: ("中", 0.75), 1: ("低", 0.5), 0: ("低", 0.5)}

# 三情景铺得太窄时,仓位上限的分母接近 0,算出来的上限会飙到几百个百分点 ——
# 那不是「可以重仓」,是「这个算法在这只票上没有含义」。
MIN_BEAR_DROP_PP = 5.0

# 法定报告的披露截止日。复核日就取下一个 ——
# **不写「长期 / 短期」**,那种词没法核对;写一个具体日期,那天回来逐条对。
DISCLOSURE_DEADLINES = [("04-30", "年报 + 一季报"), ("08-31", "半年报"),
                        ("10-31", "三季报")]


def next_check_date(today: date | None = None) -> tuple[str, str]:
    """下一个法定披露截止日。返回 (日期, 那天该到的是什么报)。

    为什么用披露截止日而不是「预计披露日」:截止日是法定的、确定的;
    预计披露日公司可以改。复核日要是个**不会变的日子**,
    否则「到时候再看」会一直往后推。
    """
    t = today or date.today()
    for md, what in DISCLOSURE_DEADLINES:
        d = date(t.year, int(md[:2]), int(md[3:]))
        if d > t:
            return d.isoformat(), what
    md, what = DISCLOSURE_DEADLINES[0]
    return date(t.year + 1, int(md[:2]), int(md[3:])).isoformat(), what


# ── 三情景 ───────────────────────────────────────────────────────────
def scenarios(v: dict, nature: str) -> dict:
    """三个情景的价位。算不出来就返回 ok=False 和原因,**不给一个凑出来的价**。"""
    spot = v.get("现价")
    share = v.get("总股本")
    if not spot or not share:
        return {"ok": False, "why": "没有现价或总股本"}
    px = spot["close"]
    if nature == "成长":
        band, our = v["前瞻PE带"], v.get("我们预测的未来四季净利")
        if not band.get("ok"):
            return {"ok": False, "why": f"3+1 PE 带算不出来:{band.get('why')}"}
        if not our:
            return {"ok": False, "why": "没有 3+1 归母净利预测(三个方法一个都不适用),"
                                        "成长股这条路径的分子就没有了"}
        rows = [("bear", our["low"], band["p25"], "3+1 净利下沿 × 带 p25"),
                ("base", our["mid"], band["p50"], "3+1 净利中枢 × 带 p50"),
                ("bull", our["high"], band["p75"], "3+1 净利上沿 × 带 p75")]
        out = {}
        for k, ni, mult, how in rows:
            tp = ni * mult / share
            out[k] = {"价": tp, "涨跌": (tp / px - 1) * 100, "净利": ni,
                      "倍数": mult, "怎么算": how}
        return {"ok": True, "路径": "PE", "现价": px, "情景": out,
                "说明": "价位 = 3+1 归母净利 × 3+1 PE ÷ 总股本。"
                        "**两端是双重叠加**:bear 同时假设「净利落在区间下沿」"
                        "**且**「市场只给带的 p25 倍数」,两个偏空条件一起成立;"
                        "bull 反过来。所以两端比中枢罕见得多 —— 它们是边界,不是预期。"}
    # 周期 / 设备:走 PB
    pb = v.get("PB带") or {}
    if not pb.get("ok"):
        return {"ok": False, "why": f"PB 带算不出来:{pb.get('why')}"}
    cur = pb["现值"]
    # ⚠ **三个情景必须 bear ≤ base ≤ bull。** 第一版按字面写成
    #   「bear=p50 或 p25 · base=现值 · bull=p75」,结果在现 PB 已经高于 p75 的票上
    #   算出 bull 比 base 还低(杰瑞 bull −40.7% vs base 0%)—— 那样 EV 就没有含义了:
    #   概率往 bull 挪反而会**拉低**期望值,「更乐观」变成了「更悲观」。
    #   所以按现 PB 落在历史带的哪一格,分四段取目标 PB。
    if cur > pb["p75"]:
        rows = [("bear", pb["p50"], "PB 回到自身历史 p50"),
                ("base", pb["p75"], "PB 回到自身历史 p75(离现在最近的那条历史线)"),
                ("bull", cur, "PB 停在现在这个位置不再回归")]
        band_note = ("⚠ **现在的 PB 已经高于自己历史的 p75**,所以这只票的乐观情景"
                     "**不是「估值继续扩张」,而是「估值停在历史极值处不再回归」**。"
                     "比这更高的价位需要报表之外的新信息(新业务、在手订单、行业周期拐点),"
                     "**这套方法看不到那些** —— 它只读已经披露的报表。")
    elif cur > pb["p50"]:
        rows = [("bear", pb["p50"], "PB 回到自身历史 p50"),
                ("base", cur, "PB 不变(估值不动,价格就不动)"),
                ("bull", pb["p75"], "PB 到自身历史 p75")]
        band_note = ""
    elif cur > pb["p25"]:
        rows = [("bear", pb["p25"], "PB 回到自身历史 p25"),
                ("base", cur, "PB 不变(估值不动,价格就不动)"),
                ("bull", pb["p75"], "PB 到自身历史 p75")]
        band_note = ""
    else:
        rows = [("bear", cur, "PB 停在现在这个位置(它已经低于自己历史 p25)"),
                ("base", pb["p50"], "PB 回到自身历史 p50"),
                ("bull", pb["p75"], "PB 到自身历史 p75")]
        band_note = ("现在的 PB **低于自己历史的 p25**,所以 bear 取「不再往下」——"
                     "这会让 bear 跌幅算成 0,仓位上限那一条因此给不出数(如实标出来),"
                     "不是「可以随便重仓」。")
    out = {}
    for k, tgt, how in rows:
        tp = px * tgt / cur
        out[k] = {"价": tp, "涨跌": (tp / px - 1) * 100, "目标PB": tgt,
                  "怎么算": how}
    # 「净资产不变」这个省略有多大:下一季预测净利 ÷ 最近披露的归母净资产。
    # 不量化的话,「三个价位偏保守」只是一句安慰话,读者不知道偏多少。
    f = v.get("预测") or {}
    eq = f.get("最近归母净资产")
    ni = (f.get("归母净利区间") or {}).get("mid")
    growth_note = ""
    if eq and ni and eq > 0:
        g = ni / eq * 100
        growth_note = (f"**这个省略有多大**:我们预测 {f.get('预测期', '下一季')} 单季"
                       f"归母净利中枢 {ni / 1e8:,.1f} 亿,最近披露的归母净资产 "
                       f"{eq / 1e8:,.1f} 亿 —— 一个季度净资产大约长 **{g:.1f}%**"
                       f"(四个季度约 {g * 4:.0f}%,不考虑分红和增发)。"
                       f"也就是说三个价位大约**偏低 {g:.1f}%**(按一季算);"
                       f"要看一年后的位置,把它们各往上抬这个幅度。")
    return {"ok": True, "路径": "PB", "现价": px, "现PB": cur, "情景": out,
            "档": ("高于 p75" if cur > pb["p75"] else "p50~p75" if cur > pb["p50"]
                   else "p25~p50" if cur > pb["p25"] else "低于 p25"),
            "带说明": band_note, "净资产增长": growth_note,
            "说明": "价位 = 现价 × 目标 PB ÷ 现 PB。"
                    "**这条路径不用净利区间** —— 它假设净资产不变、只有估值倍数回归,"
                    "算的是「如果估值回到历史某个分位,价格是多少」。"
                    "**它漏掉了这一年公司还会赚钱、净资产会增长**,所以三个价位都偏保守。"}


# ── 概率 ─────────────────────────────────────────────────────────────
def probabilities(lead: dict, f: dict, cap: int | None, cap_why: str = "") -> dict:
    """四步算概率,**每一步之后的三个数都记下来**,报告整张印出来。

    为什么要把中间步骤留下:结论是 25/50/25 还是 20/50/30,读者没法判断对错;
    但「因为 META 和 AMZN 都上调了指引,所以从 bear 挪了 10 个百分点到 bull」
    是可以被质疑的 —— 他可以说「AMZN 那次上调是因为内存涨价,不是需求变好」。
    只给结论等于不给推理。
    """
    p = dict(START_PROBS)
    steps = [{"步骤": "① 起点", "bear": p["bear"], "base": p["base"], "bull": p["bull"],
              "为什么": "不带任何信息的先验:中性情景一半,两端各四分之一。"
                        "这不是从数据里估出来的,是一个**故意保守的起点** —— "
                        "让后面每一步的修正都看得见是从哪来的。"}]

    ups = sum(1 for x in lead.get("方向", []) if x["方向"] == "↑")
    downs = sum(1 for x in lead.get("方向", []) if x["方向"] == "↓")
    net = ups - downs
    move = max(-LEAD_MAX_PP, min(LEAD_MAX_PP, net * LEAD_STEP_PP)) / 100
    # 不能把 bear 或 bull 挪成负数
    move = max(-p["bull"], min(p["bear"], move))
    p["bear"] -= move
    p["bull"] += move
    steps.append({"步骤": "② 领先层", "bear": p["bear"], "base": p["base"],
                  "bull": p["bull"],
                  "为什么": (f"{ups} 个 ↑、{downs} 个 ↓,净 {net:+d} → "
                             f"从 bear 挪 {move * 100:+.0f} 个百分点到 bull"
                             f"(每个箭头 {LEAD_STEP_PP:.0f} 个百分点,"
                             f"合计最多 {LEAD_MAX_PP:.0f} 个)。"
                             if lead.get("方向") else
                             "没有任何领先指标给出方向(这个行业没登记,或者数据没抓到)"
                             " —— 不动。**「没数据」不等于「持平」**,"
                             "所以这里是「不修正」,不是「修正为 0」。")})

    n, consistent = f.get("可用方法数", 0), f.get("方法一致", True)
    d = 0.0
    if n <= 1:
        d += METHOD_SPREAD_PP / 100
    if n >= 2 and not consistent:
        d += METHOD_SPREAD_PP / 100
    if d:
        d = min(d, p["base"] / 2)
        p["base"] -= 2 * d
        p["bear"] += d
        p["bull"] += d
    steps.append({"步骤": "③ 方法层", "bear": p["bear"], "base": p["base"],
                  "bull": p["bull"],
                  "为什么": (f"三个方法只有 {n} 个适用,没有第二个来印证 → "
                             f"把 base 各拨 {d * 100:.0f} 个百分点给 bear 和 bull"
                             f"(**分布变散,不改变方向**)。"
                             if n <= 1 and d else
                             f"{n} 个方法适用但区间不重叠、互相矛盾 → "
                             f"再各拨 {d * 100:.0f} 个百分点"
                             f"(至少有一个方法在这只票上是错的,而我们不知道是哪一个)。"
                             if d else
                             f"{n} 个方法适用且区间重叠、互相印证 → 不动。")})

    # ③b **尾部不能超过中枢。** ② 和 ③ 都只往两端加,加着加着可能出现
    #    bull 45% > base 40% —— 那时候 base 就不再是「最可能的那一档」了,
    #    而三个情景的构造(bear/base/bull = 保守/中性/乐观)预设了中枢最厚。
    #    尾部真的比中枢厚,该做的是重画情景,不是让概率去承认它。
    moved_back = 0.0
    for tail in ("bull", "bear"):
        if p[tail] > p["base"]:
            back = p[tail] - p["base"]
            p[tail] -= back
            p["base"] += back
            moved_back += back
    steps.append({"步骤": "③b 尾部不超过中枢", "bear": p["bear"], "base": p["base"],
                  "bull": p["bull"],
                  "为什么": (f"上一步之后尾部比中枢还厚,把超出的 "
                             f"{moved_back * 100:.0f} 个百分点划回 base —— "
                             f"**base 是「最可能」那一档**,尾部超过它说明这套情景"
                             f"本身该重画,而不是让概率去承认它。"
                             if moved_back else
                             "两端都不高于中枢,不用调。")})

    capped = False
    if cap is not None and p["bull"] > cap / 100:
        excess = p["bull"] - cap / 100
        p["bull"] = cap / 100
        p["base"] += excess
        capped = True
    steps.append({"步骤": "④ 基准率封顶", "bear": p["bear"], "base": p["base"],
                  "bull": p["bull"],
                  "为什么": (f"历史上同类公司 4 季后仍 ≥50% 的比例是 {cap}%,"
                             f"bull 超出的 {excess * 100:.0f} 个百分点划给 base。"
                             if capped else
                             f"bull 已经不高于基准率上限 {cap}%,不用降。"
                             if cap is not None else
                             f"**不封顶** —— {cap_why}。"
                             f"这一步没有做,不是做了之后发现不用降。")})
    tot = sum(p.values())
    return {"probs": p, "steps": steps, "封顶": cap, "封顶生效": capped,
            "不封顶原因": cap_why if cap is None else "",
            "↑数": ups, "↓数": downs, "净方向": net, "和": tot}


def expected_value(sc: dict, probs: dict) -> float:
    return sum(probs[k] * sc["情景"][k]["涨跌"] for k in ("bear", "base", "bull"))


# ── 确信度 ───────────────────────────────────────────────────────────
def confidence(f: dict, lead: dict, outside: dict, capped: bool) -> dict:
    """0~3 分。**只定仓位大小,不定方向。**

    三条各回答一个不同的问题,所以可以叠加:
      ① 我们的推法有没有旁证(方法一致且 ≥2 个)
      ② 外面的信号是不是指着同一个方向(领先指标 ≥2 个同向)
      ③ 我们的乐观程度有没有超出历史先例(基准率适用且 bull 没被压回来)

    ⚠ **它不改变买还是卖。** 一只 EV −25% 的票,确信度 3 也是清仓,
    只是清得更干脆;确信度 0 也是清仓,只是留一点余地看下个季报。
    把确信度掺进方向,就等于「我不太确定,所以少卖点」——
    那不是风险管理,是拖延。
    """
    items = []
    n, consistent = f.get("可用方法数", 0), f.get("方法一致", True)
    a = 1 if (n >= 2 and consistent) else 0
    items.append(("方法互相印证", a,
                  f"{n} 个方法适用" + ("、区间重叠" if consistent else "、但区间不重叠")
                  + ("" if a else " —— 不给分")))
    ups = sum(1 for x in lead.get("方向", []) if x["方向"] == "↑")
    downs = sum(1 for x in lead.get("方向", []) if x["方向"] == "↓")
    b = 1 if max(ups, downs) >= 2 else 0
    items.append(("领先指标同向", b,
                  f"{ups} 个 ↑、{downs} 个 ↓ —— "
                  + ("至少两个指向同一边" if b else
                     "没有两个指向同一边(要么数量不够,要么互相打架)")))
    c = 1 if (outside.get("ok") and not capped) else 0
    items.append(("乐观程度在历史先例之内", c,
                  ("基准率适用,而且我们给的 bull 概率没有超过历史同类的比例"
                   if c else
                   "bull 被基准率压回来了 —— 我们本来给的比历史先例还乐观"
                   if outside.get("ok") else
                   f"基准率这次用不了({outside.get('why', '')[:40]}…),"
                   f"没有外部参照,不给分")))
    total = sum(x[1] for x in items)
    label, coef = CONF_COEF[total]
    return {"分": total, "档": label, "系数": coef, "明细": items}


# ── 仓位上限 ─────────────────────────────────────────────────────────
def position_cap(sc: dict, conf: dict, acct: dict) -> dict:
    """单票仓位上限(占**净资产**的百分比)。

        上限% = 可承受亏损% ÷ |bear 情景跌幅%| × 确信度系数

    这个式子在说:**先定「最坏情况我最多亏净资产的百分之几」,再倒推能拿多少。**
    传统做法是反过来的 —— 先拍一个「单票不超过 20%」,那个 20% 和这只票
    到底能跌多少没有关系。同样是 20% 仓位,一只 bear 跌 30% 的票让你亏 6%,
    一只 bear 跌 70% 的票让你亏 14%,风险差一倍多。

    ⚠ **bear 跌幅太小时这个式子没有含义**:分母接近 0,上限会飙到几百个百分点。
    那不是「可以重仓」,是「这只票的三情景铺得太窄,倒推不出仓位」。
    这时候不给上限,写明原因 —— 给一个 300% 的上限比不给更糟。
    """
    tol = acct.get("单票最大可承受亏损_pct")
    equity = acct.get("净资产")
    if tol is None:
        return {"ok": False, "why": "`account.json` 里没有 "
                                    "`_单票最大可承受亏损_pct_of_净资产`,"
                                    "倒推不出上限"}
    if not equity:
        return {"ok": False, "why": "`account.json` 里没有净资产,"
                                    "上限的分母就没有了"}
    if not sc.get("ok"):
        return {"ok": False, "why": f"三情景算不出来({sc.get('why')}),"
                                    f"没有 bear 跌幅就倒推不出上限"}
    drop = sc["情景"]["bear"]["涨跌"]
    if drop >= 0:
        return {"ok": False, "why": f"bear 情景算出来是 {drop:+.1f}%(不跌反涨),"
                                    f"用它当分母没有含义 —— 这多半说明现价已经"
                                    f"低于我们最悲观的估算"}
    if abs(drop) < MIN_BEAR_DROP_PP:
        return {"ok": False, "why": f"bear 情景只跌 {abs(drop):.1f}%,"
                                    f"低于 {MIN_BEAR_DROP_PP:.0f}% 这条线 —— "
                                    f"分母太小,算出来的上限会飙到几百个百分点,"
                                    f"那不是「可以重仓」,是这个式子在这只票上没有含义"}
    raw_cap = tol / abs(drop) * 100
    cap = raw_cap * conf["系数"]
    return {"ok": True, "上限pct": cap, "未打折上限pct": raw_cap,
            "可承受亏损pct": tol, "bear跌幅": drop,
            "系数": conf["系数"], "档": conf["档"], "净资产": equity,
            "上限金额": equity * cap / 100}


def sector_cap(rows, acct: dict) -> dict:
    """同一行业加起来的上限。rows = [{行业, 市值, bear跌幅}]。

    **为什么单只卡完还要卡行业**:同行业的票会一起跌。三只光模块分开看
    各自不超限,合起来在 bear 情景下能让净资产掉三成 ——
    「分散到三只」并没有分散风险,只是把同一个赌注写成了三行。
    """
    tol = acct.get("行业最大可承受亏损_pct")
    equity = acct.get("净资产")
    if tol is None or not equity:
        return {"ok": False, "why": "`account.json` 里缺 "
                                    "`_行业最大可承受亏损_pct_of_净资产` 或净资产"}
    by = {}
    for r in rows:
        by.setdefault(r["行业"] or "未定义行业", []).append(r)
    out = {}
    for name, xs in by.items():
        mv = sum(x["市值"] for x in xs)
        usable = [x for x in xs if x.get("bear跌幅") is not None and x["bear跌幅"] < 0]
        if not usable or not mv:
            out[name] = {"ok": False, "市值": mv,
                         "why": "这个行业里没有一只算得出 bear 跌幅,加权跌幅无从算起"}
            continue
        wmv = sum(x["市值"] for x in usable)
        wdrop = sum(x["市值"] * x["bear跌幅"] for x in usable) / wmv
        if abs(wdrop) < MIN_BEAR_DROP_PP:
            out[name] = {"ok": False, "市值": mv,
                         "why": f"加权 bear 跌幅只有 {abs(wdrop):.1f}%,分母太小"}
            continue
        cap = tol / abs(wdrop) * 100
        out[name] = {"ok": True, "市值": mv, "加权bear跌幅": wdrop,
                     "上限pct": cap, "上限金额": equity * cap / 100,
                     "现仓位pct": mv / equity * 100,
                     "超出金额": max(0.0, mv - equity * cap / 100),
                     "只数": len(xs), "缺跌幅的": len(xs) - len(usable)}
    return {"ok": True, "可承受亏损pct": tol, "净资产": equity, "按行业": out}


# ── 动作 ─────────────────────────────────────────────────────────────
def act_from_ev(ev: float, conf_score: int) -> tuple[str, str]:
    """EV → 动作。**方向只看 EV**,确信度只在「可加仓」那一档当额外门槛。"""
    for cut, name in EV_CUTS:
        if ev <= cut:
            if name == "可加仓" and conf_score < ADD_MIN_CONFIDENCE:
                return ("持有",
                        f"EV {ev:+.1f}% 够到「可加仓」那一档,但确信度只有 "
                        f"{conf_score}/3(要 ≥{ADD_MIN_CONFIDENCE})—— "
                        f"**期望高不等于该重押**。确信度低的时候期望值本身"
                        f"的误差也大,加仓等于在一个我们自己都没把握的数上放大赌注。")
            return name, ""
    return "持有,不加仓", ""


def counter_evidence(cash: dict | None, act: str) -> list[str]:
    """**和结论方向相反**的那几条事实,从第 2.1 节原样抄过来。

    为什么要专门抄一遍:一份说「清仓」的报告,读者读完只会看到支持清仓的数。
    可是第 2.1 节里可能明摆着写着「合同负债 +27%、预付款翻倍」——
    那是订单在增,方向和结论相反。**不抄过来不等于没有,只等于读者没看见。**
    藏起反面证据的报告不是更有说服力,是更不可信:它让人无法判断
    我们到底是权衡过还是压根没看见。

    只做一件事:按动作的方向,把 2.1 里指向反面的项挑出来。不加解释、不打折。
    """
    if not cash or not cash.get("ok"):
        return []
    bearish_act = act.startswith(("清仓", "减仓"))
    out = []

    def add(cond, text):
        if cond:
            out.append(text)

    cl, cly = cash.get("合同负债"), cash.get("合同负债同比")
    pre, prey = cash.get("预付款"), cash.get("预付款同比")
    conv = cash.get("现金转化率")
    inv_y, rev_y = cash.get("存货同比"), cash.get("营收同比")
    ar_y = cash.get("应收同比")
    if bearish_act:
        add(cly is not None and cly > 10 and cl,
            f"**合同负债 {cl / 1e8:,.2f} 亿,同比 {cly:+.0f}%** —— "
            f"合同负债是客户已经打进来的预收款,它在涨说明**订单在增**,"
            f"方向和「减」相反")
        add(prey is not None and prey > 10 and pre,
            f"**预付款 {pre / 1e8:,.2f} 亿,同比 {prey:+.0f}%** —— "
            f"预付款是抢上游产能已经付出去的钱,是行为不是说法,"
            f"公司自己在为后面的量做准备")
        add(conv is not None and conv > 80,
            f"**现金转化率 {conv:.0f}%** —— 利润是真收到钱的,"
            f"这一条不支持「基本面在恶化」")
        add(ar_y is not None and rev_y is not None and ar_y < rev_y - 10,
            f"**应收同比 {ar_y:+.0f}% 慢于营收 {rev_y:+.0f}%** —— 收款质量在改善")
    else:
        add(cly is not None and cly < -10 and cl,
            f"**合同负债 {cl / 1e8:,.2f} 亿,同比 {cly:+.0f}%** —— 预收款在缩,"
            f"方向和「拿着」相反")
        add(conv is not None and conv < 50,
            f"**现金转化率 {conv:.0f}%** —— 利润没有同等地变成现金")
        add(ar_y is not None and rev_y is not None and ar_y > rev_y + 20,
            f"**应收同比 {ar_y:+.0f}% 快于营收 {rev_y:+.0f}%** —— "
            f"收入增长里有一部分还只是账面上的")
        add(inv_y is not None and rev_y is not None and inv_y > rev_y + 40,
            f"**存货同比 {inv_y:+.0f}% 远快于营收 {rev_y:+.0f}%** —— 可能在积压")
    return out


def build(v: dict, f: dict, position: dict, sector: dict,
          lead: dict, outside: dict, cash: dict | None = None) -> dict:
    """把上面几步串起来。**任何一步不成立,整个决策退出并说明原因** ——
    不给一个「数据不全但看着正常」的动作。"""
    import outside_view
    nature = sector.get("性质", "未定义")
    acct = position.get("账户") or {}
    ck, ck_what = next_check_date()
    out = {"复核日": ck, "复核日是什么报": ck_what, "行业": sector.get("行业"),
           "性质": nature}

    if nature == "未定义":
        out.update({"ok": False,
                    "why": f"行业未定义({sector.get('原因', '')})—— "
                           f"不知道该用利润的尺还是净资产的尺,三情景就没有倍数来源。"
                           f"**这时候给一个动作,等于用一把不知道对不对的尺量完再下结论。**",
                    "动作": "不给动作(行业未定义)", "EV": None})
        return out

    sc = scenarios(v, nature)
    out["三情景"] = sc
    if not sc.get("ok"):
        out.update({"ok": False,
                    "why": f"三情景算不出来:{sc['why']}。没有价位就没有期望值,"
                           f"没有期望值就不该有动作。",
                    "动作": "不给动作(三情景算不出来)", "EV": None})
        return out

    cap, cap_why = outside_view.bull_cap(outside)
    pr = probabilities(lead, f, cap, cap_why)
    ev = expected_value(sc, pr["probs"])
    conf = confidence(f, lead, outside, pr["封顶生效"])
    pcap = position_cap(sc, conf, acct)
    act, act_note = act_from_ev(ev, conf["分"])

    # 超限优先:**不论 EV 多好,先减到上限。**
    over = None
    mv, equity = position.get("market_value"), acct.get("净资产")
    if pcap.get("ok") and mv and equity:
        now_pct = mv / equity * 100
        limit_amt = pcap["上限金额"]
        if mv > limit_amt:
            over = {"现仓位pct": now_pct, "上限pct": pcap["上限pct"],
                    "现市值": mv, "上限金额": limit_amt, "超出": mv - limit_amt}
        pcap["现仓位pct"] = now_pct
    if over:
        if act in ("清仓", "减仓"):
            act_note = (act_note + " 另外**这只已经超过仓位上限**,"
                        "减仓的量至少要减到上限以下。").strip()
        else:
            act = "减仓(减到仓位上限)"
            act_note = ("EV 本身不要求减,**但仓位超过上限** —— "
                        "上限是从 bear 情景倒推的:按现在这个仓位,"
                        "bear 一旦兑现,亏掉的净资产超过你设定的可承受线。"
                        "**这一条不看 EV**:期望为正不代表你扛得住最坏那一档。")

    out.update({"ok": True, "概率": pr, "EV": ev, "确信度": conf,
                "仓位上限": pcap, "超限": over, "动作": act, "动作补充": act_note,
                "反向证据": counter_evidence(cash, act)})
    return out


# ── 渲染 ─────────────────────────────────────────────────────────────
def _fmt_px(x):
    return f"{x:,.2f}" if x < 100 else f"{x:,.0f}"


def render_scenarios(d: dict, L: list) -> None:
    """三情景表 —— 价位、涨跌、怎么算出来的,三列缺一不可。

    只给价位不给「怎么算」的表是不能被质疑的:读者看到「bear 568 元」,
    没法判断这个数是保守还是激进,只能选择信或不信。
    """
    sc = d["三情景"]
    L.append("| 情景 | 价位 | 相对现价 | 怎么算出来的 |")
    L.append("|---|---:|---:|---|")
    for k, lab in (("bear", "bear 悲观"), ("base", "base 中性"), ("bull", "bull 乐观")):
        s = sc["情景"][k]
        L.append(f"| **{lab}** | {_fmt_px(s['价'])} 元 | **{s['涨跌']:+.1f}%** "
                 f"| {s['怎么算']}"
                 + (f"(净利 {s['净利'] / 1e8:,.1f} 亿 × {s['倍数']:.1f}x)"
                    if "净利" in s else
                    f"(目标 PB {s['目标PB']:.2f} ÷ 现 PB {sc['现PB']:.2f})")
                 + " |")
    L.append("")
    L.append(f"> {sc['说明']}")
    L.append("")
    for key in ("带说明", "净资产增长"):
        if sc.get(key):
            L.append(sc[key])
            L.append("")


def render(d: dict, v: dict, f: dict, position: dict, n: int, L: list) -> None:
    """第 11 节:完整推导。**每一个中间量都印出来,读者能自己复算。**"""
    L.append(f"## {n} 买卖建议(完整推导)")
    L.append("")
    if not d.get("ok"):
        L.append(f"# → **{d['动作']}**")
        L.append("")
        L.append(f"**为什么不给**:{d['why']}")
        L.append("")
        L.append("> 不给动作和「持有」不是一回事。「持有」是一个结论;"
                 "这里是**没有结论** —— 缺的那一块补上之前,任何动作都是猜的。"
                 "给一个猜的动作比不给更糟,因为它看着和有依据的动作一模一样。")
        L.append("")
        return

    sc, pr, conf, pcap = d["三情景"], d["概率"], d["确信度"], d["仓位上限"]
    L.append(f"# → **{d['动作']}**　·　期望值 **{d['EV']:+.1f}%**"
             f"　·　确信度 **{conf['分']}/3({conf['档']})**"
             f"　·　复核日 **{d['复核日']}**")
    L.append("")
    if d.get("动作补充"):
        L.append(d["动作补充"])
        L.append("")
    L.append("这个结论分四步得出,**每一步的中间数都在下面,你可以自己复算**。"
             "方向只由期望值决定;确信度和仓位上限只管**拿多少**,不管**拿不拿**。")
    L.append("")

    L.append("### 11.1 三个情景各值多少钱")
    L.append("")
    render_scenarios(d, L)

    L.append("### 11.2 每个情景的概率(四步,逐步可复算)")
    L.append("")
    L.append("| 步骤 | bear | base | bull | 为什么这么动 |")
    L.append("|---|---:|---:|---:|---|")
    for s in pr["steps"]:
        L.append(f"| {s['步骤']} | {s['bear'] * 100:.0f}% | {s['base'] * 100:.0f}% "
                 f"| {s['bull'] * 100:.0f}% | {s['为什么']} |")
    L.append("")
    if pr["封顶"] is None:
        L.append(f"⚠ **bull 概率这次没有被基准率封顶** —— {pr['不封顶原因']}。"
                 f"「没校准」和「校准过、结果是不用降」是两件事:"
                 f"前者意味着我们对 bull 的乐观程度**没有任何外部约束**,"
                 f"这一档的概率完全来自我们自己的判断。")
        L.append("")

    L.append("### 11.3 期望值")
    L.append("")
    parts = " + ".join(
        f"{pr['probs'][k] * 100:.0f}% × ({sc['情景'][k]['涨跌']:+.1f}%)"
        for k in ("bear", "base", "bull"))
    L.append("```")
    L.append(f"EV = {parts}")
    L.append(f"   = {d['EV']:+.1f}%")
    L.append("```")
    L.append("")
    L.append("| EV | 动作 |")
    L.append("|---|---|")
    for lo, hi, name in (("—", "≤ −20%", "清仓"), ("−20%", "~ −8%", "减仓"),
                         ("−8%", "~ +8%", "持有,不加仓"), ("+8%", "~ +20%", "持有"),
                         ("> +20%", "且确信度 ≥ 2", "可加仓")):
        mark = " ←" if name == d["动作"] or d["动作"].startswith(name) else ""
        L.append(f"| {lo} {hi} | {name}{mark} |")
    L.append("")
    # ±8% 这条线怎么校:一个领先指标翻面 = 概率挪 LEAD_STEP_PP 个百分点,
    # 而 EV 跟着变动 = 挪的比例 × (bull 涨跌 − bear 涨跌)。
    # 这个量才是「EV 有多容易被一条新信息推翻」,拿它跟阈值比才是同一把尺。
    span = sc["情景"]["bull"]["涨跌"] - sc["情景"]["bear"]["涨跌"]
    per_arrow = LEAD_STEP_PP / 100 * span
    n_arrow = abs(EV_CUTS[2][0] / per_arrow) if per_arrow else None
    L.append(f"**±8% 这条线是什么量级**:一个领先指标翻面,概率挪 "
             f"{LEAD_STEP_PP:.0f} 个百分点,EV 跟着变 "
             f"`{LEAD_STEP_PP:.0f}% × (bull {sc['情景']['bull']['涨跌']:+.1f}% − "
             f"bear {sc['情景']['bear']['涨跌']:+.1f}%)` = **{per_arrow:.1f} 个百分点**。"
             + (f"也就是说,**{n_arrow:.1f} 个领先指标翻面就能让这只票的 EV 跨过 8% 这条线**。"
                if n_arrow else "")
             + "阈值定在 ±8%,意思就是:**一两条新信息就能推翻的 EV,不算有方向** —— "
               "那一档叫「持有,不加仓」,不叫「买」也不叫「卖」。")
    L.append("")
    L.append("> 五只实测的「每箭头 EV 变动」:中际旭创 3.8、新易盛 4.0、胜宏 3.1、"
             "杰瑞 0.8、深科达 0.7 个百分点。"
             "**情景铺得宽的票(前三只)两个箭头就跨过 8%**,所以它们的 EV 在中性带里"
             "本来就该被当成「没有方向」;后两只情景窄,EV 要跨线得靠情景本身变,"
             "而它们现在的 EV(−24%、−37%)离阈值远得多,不是边界情况。")
    L.append("")
    L.append("")

    L.append("### 11.4 确信度:只管拿多少,不管拿不拿")
    L.append("")
    L.append("| 这一条问什么 | 得分 | 依据 |")
    L.append("|---|---:|---|")
    for name, sc_, why in conf["明细"]:
        L.append(f"| {name} | **{sc_:+d}** | {why} |")
    L.append(f"| **合计** | **{conf['分']}/3** | 档位 **{conf['档']}**,"
             f"仓位系数 **{conf['系数']}**(高 1.0 / 中 0.75 / 低 0.5) |")
    L.append("")
    L.append("> **它不改变买还是卖。** 一只 EV −25% 的票,确信度 3 也是清仓,"
             "只是清得更干脆;确信度 0 也是清仓,只是留一点余地看下个季报。"
             "把确信度掺进方向,就等于「我不太确定,所以少卖点」—— "
             "那不是控制风险,是拖延。")
    L.append("")

    L.append("### 11.5 仓位上限:从最坏那一档倒推")
    L.append("")
    if pcap.get("ok"):
        L.append("```")
        L.append(f"单票上限% = 可承受亏损 {pcap['可承受亏损pct']:.0f}%"
                 f" ÷ |bear 跌幅 {abs(pcap['bear跌幅']):.1f}%|"
                 f" × 确信度系数 {pcap['系数']}")
        L.append(f"         = {pcap['未打折上限pct']:.1f}% × {pcap['系数']}"
                 f" = {pcap['上限pct']:.1f}%  of 净资产")
        L.append(f"         = {pcap['上限金额']:,.0f} 元"
                 f"(净资产 {pcap['净资产']:,.0f} 元)")
        L.append("```")
        L.append("")
        L.append("这个式子在说:**先定「最坏情况我最多亏净资产的百分之几」,"
                 "再倒推能拿多少**。传统做法是反过来的 —— 先拍一个「单票不超过 20%」,"
                 "而那个 20% 和这只票能跌多少毫无关系:同样 20% 仓位,"
                 "一只 bear 跌 30% 的票让你亏 6%,一只 bear 跌 70% 的票让你亏 14%。")
        L.append("")
        if d.get("超限"):
            o = d["超限"]
            L.append(f"⚠ **你现在占净资产 {o['现仓位pct']:.1f}%,"
                     f"上限 {o['上限pct']:.1f}% —— 超 {o['超出']:,.0f} 元。**")
            L.append("")
            L.append(f"市值 {o['现市值']:,.0f} 元,上限对应 {o['上限金额']:,.0f} 元。"
                     f"**这一条不看 EV**:期望为正不代表你扛得住最坏那一档。"
                     f"减到上限之前,这只票在账户层面就是超配的。")
        elif pcap.get("现仓位pct") is not None:
            L.append(f"**你现在占净资产 {pcap['现仓位pct']:.1f}%,上限 "
                     f"{pcap['上限pct']:.1f}% —— 没有超。**")
        else:
            L.append("**没有持仓数据**(没给 `--holdings`),所以只有上限,"
                     "没有「你现在占多少」。")
    else:
        L.append(f"**这次算不出上限**:{pcap['why']}")
        L.append("")
        L.append("> 算不出就明说。给一个凑出来的上限比不给更糟 —— "
                 "它会被当成一条经过计算的约束,而它其实只是一个数字。")
    L.append("")
    L.append("**行业合计上限**(同行业的票会一起跌,分散到三只不等于分散了风险)"
             "在《融资融券账户 · 操盘文档》第 7 节和《五只并排对照》里算 —— "
             "那两份文档手上有全部持仓,单只报告没有。")
    L.append("")

    L.append("### 11.6 什么时候回来核:" + d["复核日"])
    L.append("")
    L.append(f"**复核日 {d['复核日']}** —— 那是 {d['复核日是什么报']} 的法定披露截止日。")
    L.append("")
    L.append("**为什么不写「长期持有」「短期执行」**:那种词没法核对。"
             "三个月后回头看,「长期」是对是错谁也说不清,于是每次都能解释成对的。"
             "写一个具体日期,那天回来拿实际值对第 3 节的区间、第 10 节的条件、"
             "还有这一节的三个价位 —— 每一条都有明确的对错。")
    L.append("")
    L.append("这次的判断已经记进 `data/journal.jsonl`(见第 10 节),"
             "里面存着三情景价位、概率、EV 和可证伪条件。"
             "**存了才能算准不准** —— 没有记录,预测准不准永远说不清。")
    L.append("")

    L.append("### 11.7 这条建议会在哪里错")
    L.append("")
    L.append("- **三情景的倍数来自这只票自己的历史带**。如果这门生意的定价逻辑变了"
             "(从成长股变成周期股、或者反过来),历史带就不再适用,"
             "三个价位一起失真。")
    if sc["路径"] == "PB":
        L.append("- **PB 这条路径假设净资产不变** —— 它漏掉了这一年公司还会赚钱、"
                 "净资产会增长。所以三个价位都偏保守,EV 偏低。"
                 "它算的是「如果估值现在就回到历史某个分位,价格是多少」。")
        # PB 路径专属:估值在历史极值,而报表没有同等幅度的变化 —— 这个缺口
        # 本身就是「市场看见了我们看不见的东西」的可能性,必须写出来。
        pctl = (v.get("PB带") or {}).get("现值分位")
        L.append(f"- **这一条最要紧:现价的 PB "
                 + (f"在自己历史的 {pctl:.0f}% 分位" if pctl is not None
                    else "已经高于自己历史的 p75")
                 + f",而报表里的利润和订单没有同等幅度的变化。**"
                   f"这个缺口有两种解释:① 估值透支,要回归(这套方法采信的是这条);"
                   f"② **市场在为报表之外的信息定价** —— 新客户、新产品、行业周期"
                   f"拐点、并购,这些东西不在已披露的三张表里,**这套方法看不到**。"
                   f"所以这条建议的前提是「没有我们看不见的新信息」。"
                   f"**怎么验**:去看第 3 节预测期那份季报的合同负债和新签订单同比、"
                   f"交易所公告(重大合同 / 中标 / 定增投向)、机构调研纪要 —— "
                   f"那几处若出现报表还没反映的量,这条建议就该重估。")
    else:
        L.append("- **两端是双重叠加**:bear 同时假设净利落在下沿且倍数只给 p25。"
                 "两个偏空条件一起成立的概率,比我们给 bear 的那个数还低 —— "
                 "所以 bear 价位是边界不是预期,而 EV 里它按 25% 左右在算。")
    L.append("- **概率的起点 25/50/25 是拍的**,不是估出来的。后面三步的修正都有依据,"
             "但那个起点没有。它的作用是让修正看得见,不是它本身有多准。")
    L.append("- **领先层的方向标记是离散的**:云厂指引上调 1% 和上调 20% 都算一个 ↑。"
             "方向对了不等于幅度对了。")
    L.append("- **整套东西不含任何价格走势信息** —— 不看 K 线、不看资金流,"
             "所以它不回答「今天动还是下周动」。")
    if d.get("超限"):
        L.append("- **仓位上限那一条比 EV 硬**,但它依赖 `account.json` 里那个"
                 "「单票最大可承受亏损 10%」。那个数是你自己定的,"
                 "改它会直接改掉这一节的结论 —— 改之前先想清楚为什么改,"
                 "并且把理由写进 `account.json`。")
    ce = d.get("反向证据") or []
    if ce:
        L.append("")
        L.append("**和这个结论方向相反的事实**(从第 2.1 节原样抄来,不加折扣):")
        L.append("")
        for x in ce:
            L.append(f"- {x}")
        L.append("")
        L.append("> 列在这里不是为了显得客观,是因为**不列出来读者就看不见** —— "
                 "一份只摆支持自己那半边证据的报告,让人无法判断我们到底是"
                 "权衡过还是压根没看见。这几条不改变结论(结论由三情景和期望值定),"
                 "但它们是**下一次复核时第一个该看的地方**:如果它们继续朝这个方向走,"
                 "而我们的判断没变,那多半是我们错了。")
    L.append("")
    L.append("> 这是**基于上面这些数据的建议**,依据和失效条件都写出来了,"
             "你可以逐条质疑。最终决定和后果是你的。")
    L.append("")
