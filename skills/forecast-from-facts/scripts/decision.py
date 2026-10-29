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

## 三情景走哪条路:**按 `sector_rules` 里那个行业声明的估值尺分派**

2026-09-02 改:以前按「成长 / 周期」这个 `nature` 分派,而 `nature_of()`
对任何已定义但不在成长名单里的行业一律返回「周期」——
消费和医药明写「本流程还没定尺」,却会安安静静走完 PB 路径出三个价位。
一份报告里第 0.5 节说「没有可用的尺」、第 11 节给一个按 PB 算的动作,两句话打架。
现在直接读 `估值尺`:

| 估值尺 | 走哪条 | 价位 |
|---|---|---|
| `pe31` | 利润 | 3+1 归母净利 × 3+1 PE 带的 p25 / p50 / p75 ÷ 总股本 |
| `pb` / `ps31+pb` | 净资产 | (最近归母净资产 + 下季净利预测)÷ 总股本 × 目标 PB |
| `None` | **不给动作** | 这个行业还没定尺 —— 随便挑一把会给出一个看着正常的错数 |

**行业是弱匹配推断出来的时候也不给动作。** 弱匹配 = 只靠「专用设备」这种
宽泛的申万二级名命中(见 `sector_rules.weak_match`)。以前弱匹配只影响文字
不影响算术:报告第 0.5 节印一句警告,第 11 节照样出「清仓」。
**警告不影响算术等于没有警告。**

### PB 那条路 2026-09-02 重写:加上「这一年公司还在赚钱」

原来的写法是「价位 = 现价 × 目标 PB ÷ 现 PB」,净资产当成不变。
它在现 PB 高于自身 p75 那一档会退化成一个**恒等式**:
bear = p50、base = p75、bull = 现值,三个目标全部 ≤ 现值 → 三个价位全部 ≤ 现价
→ **EV 恒为负,而且现 PB 越高 EV 越负**。任何一只完成过重估的设备股 / 周期股
走进来都会拿到「清仓」,而这个结论里没有一点来自这家公司的经营。

现在:

    价位 =(最近归母净资产 + 下季预测净利 low/mid/high)÷ 总股本 × 目标 PB
    目标 PB:bear = 自身历史 p50 · base = **现值** · bull = 现值(现值 > p75 时)/ p75

两处变化:① 分子带上下一季赚的钱,所以 base 不再天然是「不涨」;
② **base 不再假设估值回到 p75** —— 估值回归是假设不是证据。

### 清仓要有盈利侧的证据

回测里 PB 分位对后 1~4 季涨跌**没有可检验的信号**(样本太小,方向还相反)。
所以 EV ≤ −20% 只够推出「减仓」;要推出**清仓**,还得同时满足
「领先层净方向 ≤ −1」或「预测中枢同比 < 0」其中一条 ——
那两条是盈利侧的、能被下一份季报证伪的东西。
不满足就降级为减仓,并在报告里写明是被这一条挡下来的。

## 概率怎么动:四步,每步都印出来

1. 起点 bear/base/bull = 25/50/25
2. 领先层:每个 ↑ 把 5 个百分点从 bear 挪到 bull,每个 ↓ 反过来,**合计最多挪 10**
   (2026-09-02 从 15 降到 10:四家云厂的 capex 指引已经合成一个箭头,
   剩下的几个箭头相关性仍然不低,15 个百分点给多了)
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
# ±8% ≈ **情景铺得宽的票挪两个箭头** —— 也就是「两条领先信息翻面就能把 EV
# 从正翻到负」的量级。这样的 EV 不该当成方向,该叫「持有,不加仓」。
# 每只票自己的这个数印在报告 11.3,读者能看出它离阈值有多近;
# **五只的横向对照由 `portfolio.py` 当轮现算**,不在这里写死 ——
# 写死的那一版 2026-09-02 就已经和同一段落里当场算出来的数打架了
# (写着「杰瑞 0.8」,而实算是 2.8),而且过期时不报错。
EV_CUTS = [(-20.0, "清仓"), (-8.0, "减仓"), (8.0, "持有,不加仓"),
           (20.0, "持有"), (float("inf"), "可加仓")]
# 「可加仓」额外要求的确信度下限。**2026-09-03 改成比例,不再是固定的 2 分。**
# 确信度的分母现在按行业变(见 `confidence`),拿一个变化的分子去比固定的 2,
# 等于让**登记项少的行业结构上永远够不到「可加仓」** —— 而那正是分母改成
# 「本行业可得分」要修掉的东西,改了一半等于没改。
# 2/3 是原来那条线在满分 3 时的等价值;可得分为 1 的行业,拿满那一分就够。
ADD_MIN_CONF_RATIO = 2 / 3

START_PROBS = {"bear": 0.25, "base": 0.50, "bull": 0.25}
LEAD_STEP_PP = 5.0              # 每个 ↑/↓ 挪几个百分点
# 领先层合计最多挪几个百分点。2026-09-02 由 15 降到 10 ——
# 四家云厂的 capex 指引已经在 `guidance.py` 里合成一个箭头(它们相关性接近 1,
# 当四票投等于一件事投四次);剩下的几个箭头(云厂指引 / 台湾月营收 / 上游 capex)
# 仍然都指向同一批客户的同一笔开支,不是互相独立的证据,15 个百分点给多了。
LEAD_MAX_PP = 10.0
METHOD_SPREAD_PP = 5.0          # 方法只剩一个 / 互相矛盾时,base 往两边各拨几个

# 「减仓」这个动作到底减多少。**写死在这一处**,个股报告的「具体怎么做」和
# 账户文档第 7 节的执行量读的是同一个数 —— 两处各写一个,今天恰好一样,
# 改一处就分叉,而且分叉了不报错。
# 为什么是一半:减仓的依据是「EV 落在 −20%~−8% 这一档」,那是一个**有方向但不极端**
# 的判断,而且它有明确的验证时点(复核日)。减一半 = 把一次性的赌注拆成两次,
# 中间还有一份季报可以改主意。清仓不一样 —— 那一档的意思是「没有继续持有的理由」。
TRIM_FRACTION = 0.5

# 确信度 → 仓位系数。**确信度只管大小,不管方向。**
# 2026-09-02 改成按比例算(见 `confidence`):系数 = 0.5 + 0.5 × 得分÷本行业可得分。
# 原来是固定的 {3:1.0, 2:0.75, 1:0.5, 0:0.5},那让**数据基础设施最薄弱的行业**
# 自动拿低系数、仓位上限直接砍半 —— 工具缺口变成了看空理由。
CONF_LABEL = [(0.99, "高"), (0.66, "中"), (0.0, "低")]

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
LENS_PE = "pe31"
LENS_PB = ("pb", "ps31+pb")


def _finish(out: dict, px: float) -> None:
    """把每个情景的两个因子记下来 —— 期望值分解(报告 11.3)要用。

    `盈利因子` = 这一档的分子 ÷ base 的分子;`倍数因子` = 这一档的倍数 ÷ base 的倍数。
    价位 = base 价 × 盈利因子 × 倍数因子,所以两维的贡献可以分开算。
    **为什么要分**:概率修正的证据(领先层、基准率)全是盈利侧的,
    而情景跨度大半来自倍数那一维 —— 不分开印,读者看不出这个错配。
    """
    b = out["base"]
    for k, s in out.items():
        s["涨跌"] = (s["价"] / px - 1) * 100
        s["盈利因子"] = s["_分子"] / b["_分子"] if b["_分子"] else 1.0
        s["倍数因子"] = s["_倍数"] / b["_倍数"] if b["_倍数"] else 1.0


def scenarios(v: dict, lens: str | None, weak: bool = False) -> dict:
    """三个情景的价位。算不出来就返回 ok=False 和原因,**不给一个凑出来的价**。

    `lens` 是 `sector_rules.SECTOR_RULES[行业]["估值尺"]`,**不是 `nature`** ——
    按 nature 分派会让「声明没有尺」的行业静默走 PB 路径出三个价位。
    `weak` 是「行业只靠宽泛提示词命中」,这时候连尺都不敢用。
    """
    spot = v.get("现价")
    share = v.get("总股本")
    if not spot or not share:
        return {"ok": False, "why": "没有现价或总股本"}
    if weak:
        return {"ok": False,
                "why": "**行业是弱匹配推断出来的**(只命中了「专用设备」这类宽泛的"
                       "申万二级名,不是「光通信模块」那种精确命中)—— "
                       "这套尺是挂在行业上的,行业本身都只是推断,尺不敢用。"
                       "要给动作,先把这只票的主营弄清楚、在 `sectors.py` 里"
                       "给它一个精确的行业。"}
    if lens is None:
        return {"ok": False,
                "why": "**这个行业本流程还没定估值尺**(`sector_rules` 里 "
                       "`估值尺 = None`)。不知道该看利润还是看净资产,"
                       "三情景就没有倍数来源 —— 随便挑一把会给出一个看着正常的错数。"}
    px = spot["close"]
    if lens == LENS_PE:
        band, our = v["前瞻PE带"], v.get("我们预测的未来四季净利")
        if not band.get("ok"):
            return {"ok": False, "why": f"3+1 PE 带算不出来:{band.get('why')}"}
        if not our:
            return {"ok": False, "why": "没有 3+1 归母净利预测(三个方法一个都不适用),"
                                        "利润这条路径的分子就没有了"}
        rows = [("bear", our["low"], band["p25"], "3+1 净利下沿 × 带 p25"),
                ("base", our["mid"], band["p50"], "3+1 净利中枢 × 带 p50"),
                ("bull", our["high"], band["p75"], "3+1 净利上沿 × 带 p75")]
        out = {}
        for k, ni, mult, how in rows:
            out[k] = {"价": ni * mult / share, "净利": ni, "倍数": mult,
                      "怎么算": how, "_分子": ni, "_倍数": mult}
        _finish(out, px)
        return {"ok": True, "路径": "PE", "尺": lens, "现价": px, "情景": out,
                "说明": "价位 = 3+1 归母净利 × 3+1 PE ÷ 总股本。"
                        "**两端是三重叠加**:bear 同时假设「营收落在区间下沿」"
                        "**且**「净利率取近四季最低」**且**「市场只给带的 p25 倍数」,"
                        "三个偏空条件一起成立;bull 反过来。"
                        "所以两端比中枢罕见得多 —— 它们是边界,不是预期。"}
    if lens not in LENS_PB:
        return {"ok": False, "why": f"这个行业声明的估值尺是 `{lens}`,"
                                    f"而三情景只实现了 `pe31` 和 `pb` 两条路径 —— "
                                    f"**声明和实现对不上时不给动作**,不拿另一把尺顶替"}
    # ── 净资产那条路 ─────────────────────────────────────────────
    pb = v.get("PB带") or {}
    if not pb.get("ok"):
        return {"ok": False, "why": f"PB 带算不出来:{pb.get('why')}"}
    cur = pb["现值"]
    f = v.get("预测") or {}
    eq = f.get("最近归母净资产")
    p = f.get("归母净利区间") or {}
    if not eq or eq <= 0 or not p:
        return {"ok": False,
                "why": "净资产这条路径要「最近归母净资产 + 下季预测净利」两样,"
                       + ("最近归母净资产没取到" if not eq or eq <= 0
                          else "下季净利预测算不出来(三个方法一个都不适用)")
                       + " —— 少一样就只能退回「净资产不变」那个假设,"
                         "而那正是这次要修掉的东西"}
    # ⚠ **bear 必须是最坏那一档**,整条仓位上限都建在这个前提上。
    #   现 PB 已经低于自身 p50 时,「回到 p50」是往上走 —— 那时候夹回现值,
    #   并且如实说明(不夹的话 bear 跌幅会算成正数,`position_cap` 直接拒绝出上限,
    #   报告只会写一句「算不出」,没人知道是因为情景倒过来了)。
    bear_tgt = min(pb["p50"], cur)
    bear_note = ""
    if pb["p50"] > cur:
        bear_note = (f"⚠ 现在的 PB({cur:.2f})**低于自己历史的 p50**({pb['p50']:.2f}),"
                     f"「回到 p50」在这只票上是往上走。bear 必须是最坏那一档,"
                     f"所以夹回现值 —— 也就是说**这套方法给不出比「估值不动」更坏的情景**,"
                     f"由此倒推出来的仓位上限会偏松,读的时候要知道这一点。")
    bull_tgt = cur if cur > pb["p75"] else pb["p75"]
    rows = [("bear", p["low"], bear_tgt,
             f"(净资产 + 下季净利下沿)× PB {bear_tgt:.2f}"
             + ("(自身历史 p50)" if bear_tgt == pb["p50"] else "(现值,见下)")),
            ("base", p["mid"], cur,
             "(净资产 + 下季净利中枢)× **现在这个 PB**(不假设估值回归)"),
            ("bull", p["high"], bull_tgt,
             f"(净资产 + 下季净利上沿)× PB {bull_tgt:.2f}"
             + ("(现值 —— 已经高于自身 p75,再高就没有历史依据了)"
                if cur > pb["p75"] else "(自身历史 p75)"))]
    out = {}
    for k, ni, tgt, how in rows:
        bv = (eq + ni) / share                       # 下季末每股净资产
        out[k] = {"价": bv * tgt, "目标PB": tgt, "净利": ni,
                  "每股净资产": bv, "怎么算": how, "_分子": eq + ni, "_倍数": tgt}
    _finish(out, px)
    g = p["mid"] / eq * 100
    band_note = ""
    if cur > pb["p75"]:
        band_note = ("⚠ **现在的 PB 已经高于自己历史的 p75**,所以 bull 的目标 PB 取现值 ——"
                     "和 base 是同一个数。**这只票现在没有「估值继续扩张」这个乐观情景**:"
                     "bull 和 base 的差别只剩下「净利落在预测区间上沿还是中枢」。"
                     "比这更高的价位需要报表之外的新信息(新业务、在手订单、行业周期拐点),"
                     "**这套方法看不到那些** —— 它只读已经披露的报表。")
    return {"ok": True, "路径": "PB", "尺": lens, "现价": px, "现PB": cur, "情景": out,
            "档": ("高于 p75" if cur > pb["p75"] else "p50~p75" if cur > pb["p50"]
                   else "p25~p50" if cur > pb["p25"] else "低于 p25"),
            "带说明": (band_note + ("\n\n" + bear_note if bear_note else "")),
            "净资产增长": (f"**分子里那一季的钱有多少**:预测 {f.get('预测期', '下一季')} "
                           f"单季归母净利中枢 {p['mid'] / 1e8:,.2f} 亿,最近披露的归母净资产 "
                           f"{eq / 1e8:,.1f} 亿 —— 一个季度净资产长 **{g:.1f}%**。"
                           f"base 情景就是「估值不动、净资产长这么多」,"
                           f"所以它的涨跌幅约等于这个数,而不是 0。"),
            "说明": "价位 =(最近披露的归母净资产 + 我们预测的下季净利)÷ 总股本 × 目标 PB。"
                    "**2026-09-02 改**:原来的写法是「现价 × 目标 PB ÷ 现 PB」,"
                    "净资产当成不变,而且 base 假设估值回到 p75 —— "
                    "那让「现 PB 高于自身 p75」这一档的 EV **恒为负**,"
                    "任何一只完成过重估的周期股走进来都会拿到同一个结论。"
                    "现在分子带上下一季赚的钱,base 的倍数用**现值**:"
                    "**估值回归是假设不是证据**,不把它塞进中性情景。"
                    + ("\n\n⚠ 这个行业声明的尺是 `ps31+pb`,但三情景只用了 PB 那一半 —— "
                       "3+1 PS 带算出来了、印在第 5 节,**没有进决策**。"
                       "声明和实现的这个差距如实写在这里。" if lens == "ps31+pb" else "")}


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
    # ⚠ **文案要按「这一步之后实际是什么样」判,不能只看 moved_back。**
    #   2026-09-02 破坏测试:把整个 ③b 拿掉之后,`moved_back` 保持 0,
    #   于是这一行照常打印「两端都不高于中枢,不用调」—— 而实际上胜宏的
    #   bull 45% 已经高过 base 40%。**报告主动给了一句假的放心话**,
    #   两道检查全绿。所以这里重新看一眼实际值,不信中间变量。
    ok_now = p["bull"] <= p["base"] + 1e-9 and p["bear"] <= p["base"] + 1e-9
    steps.append({"步骤": "③b 尾部不超过中枢", "bear": p["bear"], "base": p["base"],
                  "bull": p["bull"],
                  "为什么": (f"上一步之后尾部比中枢还厚,把超出的 "
                             f"{moved_back * 100:.0f} 个百分点划回 base —— "
                             f"**base 是「最可能」那一档**,尾部超过它说明这套情景"
                             f"本身该重画,而不是让概率去承认它。"
                             if moved_back else
                             "两端都不高于中枢,不用调。" if ok_now else
                             f"⚠ **这一步没有生效,而尾部仍然高于中枢**"
                             f"(bear {p['bear'] * 100:.0f}% / base {p['base'] * 100:.0f}% / "
                             f"bull {p['bull'] * 100:.0f}%)—— 这说明 ③b 这条规则"
                             f"被改坏了或者被绕过了,**这份报告的概率不能用**。")})

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


KS = ("bear", "base", "bull")


def ev_parts(sc: dict, probs: dict) -> dict:
    """把 EV 拆成三块:base 基线 + 盈利维贡献 + 倍数维贡献(+ 交叉项)。

    **为什么要拆**:概率那四步用的证据全是**盈利侧**的(云厂下季还花不花钱、
    历史上高增速能保持几成),而三个情景的跨度大半来自**倍数**那一维。
    证据加在 A 上,权重却落在 B 上 —— 不拆开印,读者看不出这个错配,
    只会看到一个「经过四步修正」的期望值。

    怎么拆:价位 = base 价 × 盈利因子 × 倍数因子(两个因子在 `scenarios` 里记好)。
      · 基线   = base 价相对现价的涨跌
      · 盈利维 = 只让盈利因子动、倍数固定在 base 时的期望涨跌 − 基线
      · 倍数维 = 只让倍数因子动、盈利固定在 base 时的期望涨跌 − 基线
      · 交叉项 = EV − 基线 − 盈利维 − 倍数维(两维同时动才有的那部分)
    """
    px, s = sc["现价"], sc["情景"]
    pbase = s["base"]["价"]
    r0 = (pbase / px - 1) * 100
    ev = sum(probs[k] * s[k]["涨跌"] for k in KS)
    only_p = sum(probs[k] * ((pbase * s[k]["盈利因子"]) / px - 1) * 100 for k in KS)
    only_m = sum(probs[k] * ((pbase * s[k]["倍数因子"]) / px - 1) * 100 for k in KS)
    dp, dm = only_p - r0, only_m - r0
    return {"EV": ev, "基线": r0, "盈利维": dp, "倍数维": dm,
            "交叉项": ev - r0 - dp - dm,
            "倍数维占比": (abs(dm) / (abs(dp) + abs(dm)) * 100
                           if (abs(dp) + abs(dm)) > 1e-9 else None)}


# ── 确信度 ───────────────────────────────────────────────────────────
def confidence(f: dict, lead: dict, outside: dict, capped: bool,
               rules: dict | None = None) -> dict:
    """确信度 = **得分 ÷ 本行业可得分**。系数 = 0.5 + 0.5 × 这个比例。

    三条各回答一个不同的问题,所以可以叠加:
      ① 我们的推法有没有旁证(方法一致且 ≥2 个)
      ② 外面的信号是不是指着同一个方向(领先指标 ≥2 个同向)
      ③ 我们的乐观程度有没有超出历史先例(基准率适用且 bull 没被压回来)

    ⚠ **2026-09-02 改:分母不再固定是 3。** ② 要求「≥2 个指标同向」,而半导体设备
    只登记了 1 个领先指标、油气设服的第二个(油价)按设计不给方向 ——
    这两个行业**结构上永远拿不到第二分**;③ 在这两个行业写死「基准率不适用」,
    也永远拿不到。结果是确信度上限 1/3、系数 0.5、仓位上限直接砍半、
    而且永远不可能「可加仓」。**数据基础设施最薄弱的行业被自动判成看空**,
    这和本流程反复强调的「缺数据是我们的工作缺口,不是『上游没动静』」直接冲突。
    现在拿不到的那一条标「本行业拿不到,不计分也不扣分」,从分母里去掉。

    它在不适用时会怎样:分母为 0(理论上不会 —— ① 任何行业都拿得到)时
    比例记 0、系数取最低档 0.5,并在明细里写清楚是分母为 0,不做除法。

    ⚠ **它不改变买还是卖。** 一只 EV −25% 的票,确信度满分也是清仓,
    只是清得更干脆。把确信度掺进方向,就等于「我不太确定,所以少卖点」——
    那不是风险管理,是拖延。
    """
    rules = rules or {}
    lead_regs = [x for x in (rules.get("领先指标") or []) if x != "oil_price"]
    items = []
    n, consistent = f.get("可用方法数", 0), f.get("方法一致", True)
    ok_names = [m["name"] for m in f.get("methods", []) if m.get("ok")]
    a = 1 if (n >= 2 and consistent) else 0
    why_a = (f"{n} 个方法适用" + ("、区间重叠" if consistent else "、但区间不重叠")
             + ("" if a else " —— 不给分"))
    if ok_names == ["同比法"]:
        # 走前回测:同比法通过自检最多、中枢误差却是另外两个的两三倍。
        # 「唯一能用的方法」听起来像「至少还有一个」,实际是「只剩最不准的那个」。
        a = 0
        why_a = ("唯一能用的是**同比法**,而它是三个方法里最不准的那个"
                 "(走前回测:通过自检次数最多、中枢误差最大)—— "
                 "**这一条判 0**,不是因为「只有一个」,是因为剩下的这一个本身就不准")
    items.append(("方法互相印证", a, 1, why_a))
    ups = sum(1 for x in lead.get("方向", []) if x["方向"] == "↑")
    downs = sum(1 for x in lead.get("方向", []) if x["方向"] == "↓")
    if len(lead_regs) >= 2:
        b, avail_b = (1 if max(ups, downs) >= 2 else 0), 1
        why_b = (f"{ups} 个 ↑、{downs} 个 ↓ —— "
                 + ("至少两个指向同一边" if b else
                    "没有两个指向同一边(要么数量不够,要么互相打架)"))
    else:
        b, avail_b = 0, 0
        why_b = (f"**本行业只登记了 {len(lead_regs)} 个会给方向的领先指标**"
                 f"({'、'.join(lead_regs) or '一个都没有'}),"
                 f"「≥2 个同向」这一条结构上就拿不到 —— "
                 f"**不计分也不扣分**,从分母里去掉。"
                 f"这是我们的取数缺口,不是这只票的问题")
    items.append(("领先指标同向", b, avail_b, why_b))
    if rules.get("基准率"):
        c, avail_c = (1 if (outside.get("ok") and not capped) else 0), 1
        why_c = ("基准率适用,而且我们给的 bull 概率没有超过历史同类的比例"
                 if c else
                 "bull 被基准率压回来了 —— 我们本来给的比历史先例还乐观"
                 if outside.get("ok") else
                 f"基准率适用于本行业,但这次算不出来"
                 f"({outside.get('why', '')[:40]}…),不给分")
    else:
        c, avail_c = 0, 0
        why_c = (f"**本行业不适用基准率**({(rules.get('基准率不适用原因') or '')[:48]}…)"
                 f"—— 不计分也不扣分,从分母里去掉")
    items.append(("乐观程度在历史先例之内", c, avail_c, why_c))
    total = sum(x[1] for x in items)
    avail = sum(x[2] for x in items)
    ratio = total / avail if avail else 0.0
    coef = round(0.5 + 0.5 * ratio, 3)
    label = next(lab for cut, lab in CONF_LABEL if ratio >= cut)
    return {"分": total, "满分": avail, "比例": ratio, "档": label, "系数": coef,
            "明细": items}


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
    model_cap = raw_cap * conf["系数"]
    # ── 硬线:和估值模型**完全无关**的一条集中度线 ────────────────────
    # 为什么要有:上面那条从 bear 倒推的上限会随估值模型漂 —— 哪天 PE 带变宽、
    # bear 跌幅算成 −20%,上限就跳到 37.5%,而那时候杠杆和集中度一点没变。
    # **风控结论不该随估值模型漂。** 硬线丑但稳:模型出错时它仍然成立。
    hard = acct.get("单票硬上限_pct")
    cap, binding = model_cap, "bear 倒推"
    if hard is not None and hard < model_cap:
        cap, binding = float(hard), "硬线"
    return {"ok": True, "上限pct": cap, "未打折上限pct": raw_cap,
            "模型上限pct": model_cap, "硬上限pct": hard, "起作用的": binding,
            "可承受亏损pct": tol, "bear跌幅": drop,
            "系数": conf["系数"], "档": conf["档"],
            "分": conf["分"], "满分": conf["满分"], "净资产": equity,
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
        model_cap = tol / abs(wdrop) * 100
        hard = acct.get("行业硬上限_pct")
        cap, binding = model_cap, "bear 倒推"
        if hard is not None and hard < model_cap:
            cap, binding = float(hard), "硬线"
        out[name] = {"ok": True, "市值": mv, "加权bear跌幅": wdrop,
                     "上限pct": cap, "模型上限pct": model_cap, "硬上限pct": hard,
                     "起作用的": binding, "上限金额": equity * cap / 100,
                     "现仓位pct": mv / equity * 100,
                     "超出金额": max(0.0, mv - equity * cap / 100),
                     "只数": len(xs), "缺跌幅的": len(xs) - len(usable)}
    return {"ok": True, "可承受亏损pct": tol, "净资产": equity, "按行业": out}


# ── 动作 ─────────────────────────────────────────────────────────────
def profit_side(lead: dict, f: dict) -> dict:
    """清仓的第二道门:**盈利侧有没有一条能被下一份季报证伪的坏消息。**

    为什么清仓要多这一道:EV 主要由估值倍数那一维拉开,而估值回归是**假设**
    不是证据 —— 回测里 PB 分位对后 1~4 季涨跌没有可检验的信号(n 太小,
    方向还相反)。减仓是可逆的,做错了再买回来只损失一点摩擦;
    清仓不可逆,而且踏空的代价没有上限。所以清仓要有盈利侧的证据。

    两条任一成立就算有:
      · 领先层净方向 ≤ −1(上游或同链的量在往下)
      · 预测中枢同比 < 0(我们自己推的下一季比去年同期还少)

    它在不适用时会怎样:两条都拿不到数(没登记领先指标 + 同比算不出来)时
    返回「拿不到」,而拿不到**不构成证据** → 不清仓,降级为减仓。
    方向是「少做一次不可逆的动作」,而且报告里会写明是被这一条挡下来的。
    """
    net = lead.get("净方向")
    yoy = f.get("中枢同比pct")
    basis = f.get("中枢同比口径") or "中枢同比"
    hits, seen = [], []
    if net is not None:
        seen.append(f"领先层净方向 {net:+d}")
        if net <= -1:
            hits.append(f"领先层净方向 {net:+d} ≤ −1")
    if yoy is not None:
        seen.append(f"{basis}中枢同比 {yoy:+.1f}%")
        if yoy < 0:
            hits.append(f"{basis}中枢同比 {yoy:+.1f}% < 0")
    return {"成立": bool(hits), "命中": hits, "看过": seen,
            "拿不到": not seen}


def act_from_ev(ev: float, conf: dict, profit: dict) -> tuple[str, str]:
    """EV → 动作。**方向只看 EV**,另外两道门:

    · 「可加仓」额外要确信度**比例** ≥ 2/3(得分 ÷ 本行业可得分)
    · 「清仓」额外要盈利侧有证据(见 `profit_side`),否则降级为减仓
    """
    ratio = conf.get("比例", 0.0)
    for cut, name in EV_CUTS:
        if ev <= cut:
            if name == "可加仓" and ratio < ADD_MIN_CONF_RATIO - 1e-9:
                return ("持有",
                        f"EV {ev:+.1f}% 够到「可加仓」那一档,但确信度只有 "
                        f"{conf.get('分')}/{conf.get('满分')} = {ratio * 100:.0f}%"
                        f"(要 ≥{ADD_MIN_CONF_RATIO * 100:.0f}%)—— "
                        f"**期望高不等于该重押**。确信度低的时候期望值本身"
                        f"的误差也大,加仓等于在一个我们自己都没把握的数上放大赌注。"
                        f"**门槛用的是比例不是分数**:分母按行业变,"
                        f"拿变化的分子去比一个固定的 2,登记项少的行业就永远够不到。")
            if name == "清仓" and not profit["成立"]:
                return ("减仓",
                        f"EV {ev:+.1f}% 落在「清仓」那一档,**但降级为减仓** —— "
                        f"清仓要有盈利侧的证据,而这次两条都不成立"
                        + (f"({'、'.join(profit['看过'])})。"
                           if profit["看过"] else
                           "(两条都拿不到数,而拿不到不构成证据)。")
                        + "EV 这么负,大半来自「估值该回到历史某个分位」这个**假设** —— "
                          "回测里估值分位对后 1~4 季涨跌没有可检验的信号。"
                          "减仓做错了还能买回来,清仓不可逆,门槛该更高。")
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

    # text 必须是函数:条件为假时不能去格式化 —— 2026-10-30 胜宏「应收同比」为空,
    # f-string 先于条件求值，整条决策链崩在这里(check_flow_contract ⑨ 抓到)。
    def add(cond, text):
        if cond:
            out.append(text())

    cl, cly = cash.get("合同负债"), cash.get("合同负债同比")
    pre, prey = cash.get("预付款"), cash.get("预付款同比")
    conv = cash.get("现金转化率")
    inv_y, rev_y = cash.get("存货同比"), cash.get("营收同比")
    ar_y = cash.get("应收同比")
    if bearish_act:
        add(cly is not None and cly > 10 and cl,
            lambda: f"**合同负债 {cl / 1e8:,.2f} 亿,同比 {cly:+.0f}%** —— "
            f"合同负债是客户已经打进来的预收款,它在涨说明**订单在增**,"
            f"方向和「减」相反")
        add(prey is not None and prey > 10 and pre,
            lambda: f"**预付款 {pre / 1e8:,.2f} 亿,同比 {prey:+.0f}%** —— "
            f"预付款是抢上游产能已经付出去的钱,是行为不是说法,"
            f"公司自己在为后面的量做准备")
        add(conv is not None and conv > 80,
            lambda: f"**现金转化率 {conv:.0f}%** —— 利润是真收到钱的,"
            f"这一条不支持「基本面在恶化」")
        add(ar_y is not None and rev_y is not None and ar_y < rev_y - 10,
            lambda: f"**应收同比 {ar_y:+.0f}% 慢于营收 {rev_y:+.0f}%** —— 收款质量在改善")
    else:
        add(cly is not None and cly < -10 and cl,
            lambda: f"**合同负债 {cl / 1e8:,.2f} 亿,同比 {cly:+.0f}%** —— 预收款在缩,"
            f"方向和「拿着」相反")
        add(conv is not None and conv < 50,
            lambda: f"**现金转化率 {conv:.0f}%** —— 利润没有同等地变成现金")
        add(ar_y is not None and rev_y is not None and ar_y > rev_y + 20,
            lambda: f"**应收同比 {ar_y:+.0f}% 快于营收 {rev_y:+.0f}%** —— "
            f"收入增长里有一部分还只是账面上的")
        add(inv_y is not None and rev_y is not None and inv_y > rev_y + 40,
            lambda: f"**存货同比 {inv_y:+.0f}% 远快于营收 {rev_y:+.0f}%** —— 可能在积压")
    return out


def build(v: dict, f: dict, position: dict, sector: dict,
          lead: dict, outside: dict, cash: dict | None = None) -> dict:
    """把上面几步串起来。**任何一步不成立,整个决策退出并说明原因** ——
    不给一个「数据不全但看着正常」的动作。"""
    import outside_view
    import sector_rules
    nature = sector.get("性质", "未定义")
    rules = sector.get("规则") or {}
    lens = rules.get("估值尺")
    weak = sector_rules.weak_match(sector.get("命中板块") or [])
    acct = position.get("账户") or {}
    ck, ck_what = next_check_date()
    out = {"复核日": ck, "复核日是什么报": ck_what, "行业": sector.get("行业"),
           "性质": nature, "估值尺": lens, "弱匹配": weak,
           # **不给动作的时候也要留下当时的价**。没有它,journal 那条记录
           # 连「当时它多少钱」都答不上来 —— 下次回来看等于什么都没记。
           "现价": (v.get("现价") or {}).get("close")}

    if nature == "未定义":
        out.update({"ok": False,
                    "why": f"行业未定义({sector.get('原因', '')})—— "
                           f"不知道该用利润的尺还是净资产的尺,三情景就没有倍数来源。"
                           f"**这时候给一个动作,等于用一把不知道对不对的尺量完再下结论。**",
                    "动作": "不给动作(行业未定义)", "EV": None})
        return out

    # ⚠ **按 `估值尺` 分派,不按 `nature`。** nature_of() 对任何已定义但不在成长
    #   名单里的行业一律返回「周期」,于是消费/医药这种明写「还没定尺」的行业
    #   会安安静静走完 PB 路径出三个价位 —— 第 0.5 节说没有尺,第 11 节给动作。
    sc = scenarios(v, lens, weak)
    out["三情景"] = sc
    if not sc.get("ok"):
        why_short = ("行业是弱匹配推断的,尺不敢用" if weak else
                     "这个行业还没定估值尺" if lens is None else "三情景算不出来")
        out.update({"ok": False,
                    "why": f"{sc['why']} 没有价位就没有期望值,没有期望值就不该有动作。",
                    "动作": f"不给动作({why_short})", "EV": None})
        return out

    cap, cap_why = outside_view.bull_cap(outside)
    pr = probabilities(lead, f, cap, cap_why)
    ev = expected_value(sc, pr["probs"])
    parts = ev_parts(sc, pr["probs"])
    conf = confidence(f, lead, outside, pr["封顶生效"], rules)
    pcap = position_cap(sc, conf, acct)
    profit = profit_side(lead, f)
    act, act_note = act_from_ev(ev, conf, profit)

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

    out.update({"ok": True, "概率": pr, "EV": ev, "EV分解": parts, "确信度": conf,
                "仓位上限": pcap, "超限": over, "动作": act, "动作补充": act_note,
                "盈利侧证据": profit,
                "反向证据": counter_evidence(cash, act)})
    return out
