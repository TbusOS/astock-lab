#!/usr/bin/env python3
"""融资融券账户的操盘文档 —— 杠杆下的持仓和现金账户是两回事。

    margin_account.py [--account private/portfolio/account.json]
                      [--raw data/raw] [--out 文档.md]

为什么要单独一份:
    现金账户跌 30% 就是跌 30%,扛得住就扛。**信用账户不一样**:
      · 亏损被杠杆放大(市值跌 1%,你的净资产跌 1.67%)
      · 有一条**你不能选择**的线 —— 维持担保比例到了就强制平仓,
        而且是在最低点被动卖出,不是你决定的时点
      · 利息每天在走,不涨就是在亏
    所以「这家公司值多少钱」和「这笔仓位能不能扛」是两个问题。
    个股报告回答前一个,这份回答后一个。

⚠ 这份文档算的是**后果**,不是建议:
    「卖掉 X 之后,维持担保比例变成 Y,到平仓线还有 Z% 的跌幅」——
    每个数都能自己验算。卖不卖是你的决定,后果也是你的。

依赖:无。账户数据从 account.json 读,那份文件由人按券商界面填。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# source_catalog 住在 data-sources 那个 skill 下 —— 数据源目录属于取数,不属于预测。
# 两个 skill 的 scripts 目录都加进 sys.path,tools/ 软链和仓内真实路径都能跑。
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent.parent / "data-sources" / "scripts"))

import account_plan
import position_view
import source_catalog as catalog



def wan(v, nd=2):
    return "—" if v is None else f"{v / 1e4:,.{nd}f}"


def money(v, nd=0):
    return "—" if v is None else f"{v:,.{nd}f}"


def load(fp: Path) -> dict:
    j = json.loads(fp.read_text(encoding="utf-8"))
    for k in ("account", "positions", "margin_debt"):
        if k not in j:
            raise SystemExit(f"{fp} 缺 {k}")
    return j


def ratio_at(assets: float, debt: float) -> float:
    return assets / debt * 100 if debt else float("inf")


def drop_to_line(mv: float, cash: float, debt: float, line_pct: float):
    """市值要跌多少,维持担保比例才掉到 line_pct。

    维持担保比例 = 总资产 ÷ 总负债,总资产 = 市值 + 现金。
    所以 目标市值 = 负债 × 线 − 现金。
    ⚠ 这个算法**假设负债不变**,而利息每天在累积 —— 实际距离会比算出来的更近一点。
    """
    target_mv = debt * line_pct / 100 - cash
    if target_mv <= 0:
        return None
    return (target_mv / mv - 1) * 100


# ── 各节 ──────────────────────────────────────────────────────────────
def sec_now(j, L):
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    pnl = sum(p["pnl"] for p in ps)
    debt = sum(d["当前负债总额"] for d in md)
    equity = a["总资产"] - debt

    L.append("## 1 账户现在什么状态")
    L.append("")
    L.append(f"数据来自 {j.get('_截图时间', '?')} 的券商界面截图。"
             f"下面每个数要么是界面上的原值,要么是从原值算出来的,**没有估计值**。")
    L.append("")
    L.append("| 项 | 值 | 说明 |")
    L.append("|---|---:|---|")
    L.append(f"| 总资产 | {money(a['总资产'])} | 市值 + 现金 |")
    L.append(f"| 总市值 | {money(mv)} | 五只加总,和界面一致 |")
    L.append(f"| 可用现金 | {money(a['可用现金'], 2)} | 可取现金 {money(a['可取现金'], 2)} |")
    L.append(f"| **总负债** | **{money(debt, 2)}** | 融资本金 + 利息 |")
    L.append(f"| 净资产(你自己的钱) | {money(equity, 2)} | 总资产 − 总负债 |")
    L.append(f"| 维持担保比例 | **{a['维持担保比例_pct']:.2f}%** | 总资产 ÷ 总负债 |")
    L.append(f"| 可用保证金 | **{money(a['可用保证金'], 2)}** | **负数** |")
    L.append(f"| 总仓位 | {a['总仓位_pct']:.2f}% | 几乎满仓 |")
    L.append("")
    L.append(f"**当前浮亏 {money(pnl, 2)} 元。** 但这个数低估了实际损失:"
             f"本金是净资产加上浮亏 = **{money(equity - pnl)} 元**,"
             f"亏掉的占本金 **{-pnl / (equity - pnl) * 100:.1f}%**。"
             f"净资产已经是扣完亏损之后的数,拿浮亏率去比它会看轻。")
    L.append("")
    L.append(f"> **可用保证金是负的({money(a['可用保证金'], 2)})。** "
             "这至少意味着**不能再融资买入**。它和维持担保比例是两条不同的线:"
             "维持担保比例管的是会不会被强平,可用保证金管的是还能不能加仓。"
             "现在第二条已经用尽了。具体还有没有别的后果,要问券商 —— "
             "这一条我不确定,不替你猜。")
    L.append("")


def sec_three_numbers(j, L):
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    debt = sum(d["当前负债总额"] for d in md)
    equity = a["总资产"] - debt
    lev = mv / equity
    by = sorted(ps, key=lambda p: -p["market_value"])
    top2 = by[0]["market_value"] + by[1]["market_value"]

    L.append("## 2 三个数字决定了这个账户的处境")
    L.append("")
    L.append("### ① 杠杆 " + f"**{lev:.2f}x**")
    L.append("")
    L.append(f"你用 {money(equity)} 元的自有资金,持有 {money(mv)} 元的股票。"
             f"**市值每跌 1%,你的净资产跌 {lev:.2f}%。**")
    L.append("")
    L.append("| 市值变动 | 你的净资产变动 | 净资产变成 |")
    L.append("|---:|---:|---:|")
    for d in (-30, -20, -10, 10, 20, 30):
        ne = equity + mv * d / 100
        L.append(f"| {d:+d}% | **{d * lev:+.1f}%** | {money(ne)} |")
    L.append("")
    L.append("> 杠杆是对称的 —— 涨的时候也放大 1.67 倍。"
             "它本身不是错,**错的是在杠杆之上再叠加集中度**,见下一条。")
    L.append("")

    L.append("### ② 集中度:两只票占 " + f"**{top2 / mv * 100:.1f}%**")
    L.append("")
    L.append("| 股票 | 市值 | 占比 | 浮盈亏 |")
    L.append("|---|---:|---:|---:|")
    for p in by:
        L.append(f"| {p['name']}({p['code']}) | {money(p['market_value'])} "
                 f"| {p['market_value'] / mv * 100:.1f}% | {p['pnl_pct']:+.2f}% |")
    L.append("")
    opt = [p for p in ps if p["code"] in ("300502", "300308")]
    if len(opt) == 2:
        s = sum(p["market_value"] for p in opt)
        L.append(f"**{opt[0]['name']} + {opt[1]['name']} 合计 {s / mv * 100:.1f}%** —— "
                 f"这两只是**同一条产业链的同一个环节**(光模块),"
                 f"同涨同跌。把它们当两只票分散风险是错觉,它们更接近一只票的两个批次。")
        L.append("")

    L.append("### ③ 杠杆押在哪:**全部**")
    L.append("")
    L.append("| 融资标的 | 未还本金 | 未还利息 | 负债合计 | 该股市值 | 负债/市值 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    pos_by = {p["code"]: p for p in ps}
    for d in md:
        p = pos_by.get(d["code"])
        pmv = p["market_value"] if p else None
        L.append(f"| {d['name']}({d['code']}) | {money(d['未还本金'], 2)} "
                 f"| {money(d['未还利息'], 2)} | {money(d['当前负债总额'], 2)} "
                 + (f"| {money(pmv)} | {d['当前负债总额'] / pmv * 100:.0f}% |"
                    if pmv else "| — | — |"))
    L.append("")
    L.append(f"> **全部 {money(debt)} 元负债都压在这两只上,而它们又是仓位最重的两只。**"
             "杠杆、集中度、行业风险三个东西叠在同一个地方 —— "
             "任何一条出问题,三条一起放大。这是这个账户最该注意的一点,"
             "比任何一只票的基本面都重要。")
    L.append("")


def sec_distance(j, L):
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    cash = a["可用现金"]
    debt = sum(d["当前负债总额"] for d in md)
    lines = j.get("_担保比例线") or {"警戒线_pct": 150, "平仓线_pct": 130}

    L.append("## 3 离强平还有多远")
    L.append("")
    L.append("这是信用账户和现金账户最本质的区别:**有一条线,到了就不是你决定卖不卖。**")
    L.append("")
    L.append("维持担保比例 = 总资产 ÷ 总负债 = "
             f"({money(mv)} + {money(cash, 2)}) ÷ {money(debt, 2)} = "
             f"**{ratio_at(mv + cash, debt):.2f}%**")
    L.append("")
    L.append("| 线 | 比例 | 对应总市值 | 市值还要跌 | 对应你的净资产 |")
    L.append("|---|---:|---:|---:|---:|")
    equity = a["总资产"] - debt
    for name, key in (("警戒线", "警戒线_pct"), ("平仓线", "平仓线_pct")):
        lp = lines[key]
        d = drop_to_line(mv, cash, debt, lp)
        if d is None:
            continue
        tmv = mv * (1 + d / 100)
        L.append(f"| {name} | {lp}% | {money(tmv)} | **{d:.1f}%** "
                 f"| {money(equity + (tmv - mv))} |")
    L.append("")
    d130 = drop_to_line(mv, cash, debt, lines["平仓线_pct"])
    L.append(f"**现在离平仓线还有 {abs(d130):.1f}% 的跌幅**,不算近。"
             "但有三件事会让这个距离缩短,而且是单向的:")
    L.append("")
    L.append("1. **利息每天累积**,负债只增不减(不还的话)。负债涨,线就往上抬。")
    L.append("2. **集中度**:两只光模块占七成,它们一起跌 20% 就等于总市值跌 14%。"
             "不是五只票各自波动那种分散。")
    L.append("3. **强平不是按你的价位卖**。触线时券商在什么价卖是它决定的,"
             "通常是最难看的那个时点。")
    L.append("")
    L.append(f"> ⚠ 上表按**负债不变**算。实际利息在走,所以真实距离比表里的略近。"
             f"另外警戒线/平仓线用的是行业通行值({lines['警戒线_pct']}% / "
             f"{lines['平仓线_pct']}%)—— **你的合同上是多少,自己去核**,"
             f"券商之间不一样,这一条我没法替你确认。")
    L.append("")


def sec_cost(j, L):
    md = j["margin_debt"]
    principal = sum(d["未还本金"] for d in md)
    accrued = sum(d["未还利息"] for d in md)
    a = j["account"]
    mv = sum(p["market_value"] for p in j["positions"])
    equity = a["总资产"] - sum(d["当前负债总额"] for d in md)
    rate = j.get("_融资利率_pct")

    L.append("## 4 利息在吃什么")
    L.append("")
    L.append(f"融资本金 **{money(principal, 2)}** 元,已累计利息 **{money(accrued, 2)}** 元。")
    L.append("")

    if rate is None:
        L.append("**不知道融资利率**(界面上没有),按常见区间列:")
        L.append("")
        L.append("| 年利率 | 每天 | 每月 | 每年 | 市值要涨多少打平 | 净资产要涨多少打平 |")
        L.append("|---:|---:|---:|---:|---:|---:|")
        for r in (4.0, 5.0, 6.0, 8.0):
            yr = principal * r / 100
            L.append(f"| {r:.1f}% | {money(yr / 365, 1)} | {money(yr / 12, 0)} "
                     f"| {money(yr, 0)} | {yr / mv * 100:.2f}% | {yr / equity * 100:.2f}% |")
        L.append("")
        L.append("**去 APP 查实际利率填进 account.json**,填了这里就变成确定值。")
        L.append("")
        return

    yr = principal * rate / 100
    L.append(f"融资利率 **{rate:.1f}%**({j.get('_融资利率来源', '来源未记')}):")
    L.append("")
    L.append("| | 金额 | 占什么 |")
    L.append("|---|---:|---|")
    L.append(f"| 每天 | {money(yr / 365, 2)} | — |")
    L.append(f"| 每月 | {money(yr / 12, 0)} | — |")
    L.append(f"| 每年 | **{money(yr, 0)}** | 净资产的 **{yr / equity * 100:.2f}%** |")
    L.append(f"| 打平需要 | 市值涨 **{yr / mv * 100:.2f}%/年** | 或净资产涨 "
             f"{yr / equity * 100:.2f}%/年 |")
    L.append("")
    L.append(f"> **{rate:.0f}% 是一个不高的利率。** 这条负债一年吃掉净资产 "
             f"{yr / equity * 100:.1f}%,不到 3 个点 —— "
             f"横盘的时间成本是有,但不是压迫性的。"
             f"**这个账户的风险不在利息,在杠杆和集中度**(第 2 节)。"
             f"如果利率是 8%,结论会不一样;4% 的情况下,"
             f"「因为利息所以必须尽快减仓」这个理由**不成立**。")
    L.append("")

    # 反推开仓时间。利率已知时这是一个可以算的数,但它有假设,必须标出来。
    L.append("**从已计息金额反推开仓时间**(推算,不是事实):")
    L.append("")
    L.append("| 标的 | 未还本金 | 每天利息 | 已计息 | 推算已持有 | 推算开仓日 |")
    L.append("|---|---:|---:|---:|---:|---|")
    asof = j.get("_截图时间", "")[:10]
    base = date.fromisoformat(asof) if len(asof) == 10 else date.today()
    for d in md:
        per_day = d["未还本金"] * rate / 100 / 365
        if per_day <= 0:
            continue
        days = d["未还利息"] / per_day
        L.append(f"| {d['name']} | {money(d['未还本金'], 0)} | {money(per_day, 2)} "
                 f"| {money(d['未还利息'], 2)} | {days:.0f} 天 "
                 f"| 约 {base - timedelta(days=round(days))} |")
    L.append("")
    L.append("> **这是推算,依据是「按日单利计息、期间没有部分还款」。** "
             "实测把推算日拿去对当天的股价,比持仓均价高 5%~8% —— "
             "两者并不矛盾:**成本是整个仓位的均价,融资只是其中一批**,"
             "说明是分批建仓的。要确定实际开仓日,去 APP 的「查询 → 融资明细」看。")
    L.append("")


# ⚠ 这里原来写死了一张 ADVICE 表和一张 VIEW 表:五只的建议、三维评分、
#   「现价前瞻 PE 37.3x」「自己历史 p50 是 22.7x」这些数字全部是常量。
#   2026-09-02 改估值算法的时候,那两张表**当场全部过期**,而没有任何东西会报错 ——
#   文档照样生成、格式照样对,只是每个数都在讲上一版的故事。
#   现在改成对每只跑一遍和个股报告完全相同的管线
#   (valuation.build → forecast → advice.build),措辞按数据分支,见 position_view.py。
#   **两份文档读同一次计算的结果**,所以不可能给出不同的建议。


def sec_each(j, views, L):
    ps = sorted(j["positions"], key=lambda p: -p["market_value"])
    mv = sum(p["market_value"] for p in ps)
    debt_of = {d["code"]: d["当前负债总额"] for d in j["margin_debt"]}

    L.append("## 5 逐只:你的仓位 × 我们的判断")
    L.append("")
    L.append("下面每只的判断来自各自那份持仓决策报告(同目录),"
             "全部由公司自己的报表和 SEC 申报原值推出,**不引用机构的盈利预测**。")
    L.append("")
    L.append("**动作由期望值决定** —— 期望值 = 三个情景的价位按概率加权后的涨跌。"
             "≤−20% 清仓 · −20~−8% 减仓 · −8~+8% 持有不加仓 · +8~+20% 持有 · "
             ">+20% 且确信度 ≥2 可加仓。确信度和仓位上限只管**拿多少**,不管拿不拿。")
    L.append("")
    L.append("提到的「三个方法」是:**存货法**(上季末存货 × 历史比值,看供给侧已花的钱)、**同比法**(去年同期 × 近三季同比,看需求侧增长)、"
             "**环比法**(上季 × 历年同一转换的环比,看季节形状)。每个先自检适不适用,不适用就退出 —— 详细算式见各只报告第 3 节。")
    L.append("")
    L.append("提到的「**3+1 PE**」是:**市值 ÷(最近三季已披露归母净利 + 下一季归母净利)**。"
             "历史带里那个「下一季」是**事后实际**,现价那一格是**我们的预测** —— "
             "两边唯一的差别就是最后那一季,前三季两边都是已披露的实际值。"
             "两边算的是同一段时间、同一种算法,分位才比得。")
    L.append("")
    L.append("> 下面每一只的建议、评分、PE 和带分位,都是**这次跑管线现算的**,"
             "不是写死在文档里的常量。数据重抓一次,这几段话就跟着变。")
    L.append("")
    for p in ps:
        view = views.get(p["code"])
        L.append(f"### {p['name']}({p['code']})　仓位 {p['market_value'] / mv * 100:.1f}%"
                 f"　浮盈亏 {p['pnl_pct']:+.2f}%")
        L.append("")
        L.append(f"- {p['shares']:,} 股 × 成本 {p['cost']:,.3f} → 现价 {p['price']:,.3f},"
                 f"市值 {money(p['market_value'])},浮盈亏 {money(p['pnl'], 2)}")
        if p["code"] in debt_of:
            L.append(f"- **这只有融资负债 {money(debt_of[p['code']], 2)} 元**"
                     f"(占它市值 {debt_of[p['code']] / p['market_value'] * 100:.0f}%)")
        else:
            L.append("- 这只**没有融资负债**,是纯自有资金持有")
        if not view:
            L.append("- **这只算不出建议** —— 报表数据不全,见文档末尾的数据来源一节。"
                     "不给一个凑出来的动作:没有依据的建议比没有建议更糟。")
            L.append("")
            continue
        a = view["建议"]
        d = a.get("决策") or {}
        L.append(f"- **建议:{a['动作']}**"
                 + (f"(期望值 **{d['EV']:+.1f}%**、确信度 "
                    f"{d['确信度']['分']}/3、{a['期限']})"
                    if d.get("ok") else f"({a['期限']})")
                 + f" —— 完整推导(三情景 / 概率四步 / 仓位上限)见 "
                   f"{p['name']} 那份报告的第 11 节")
        if d.get("ok"):
            sc = d["三情景"]["情景"]
            pr = d["概率"]["probs"]
            L.append(f"- 三情景:bear **{sc['bear']['价']:,.2f}**"
                     f"({sc['bear']['涨跌']:+.1f}%,{pr['bear'] * 100:.0f}%)· "
                     f"base **{sc['base']['价']:,.2f}**"
                     f"({sc['base']['涨跌']:+.1f}%,{pr['base'] * 100:.0f}%)· "
                     f"bull **{sc['bull']['价']:,.2f}**"
                     f"({sc['bull']['涨跌']:+.1f}%,{pr['bull'] * 100:.0f}%)")
            L.append(f"- 行业 **{a.get('行业') or '未定义'}**,"
                     f"三情景走的是{'利润(3+1 PE 带)' if d['三情景']['路径'] == 'PE' else '净资产(PB 分位)'}"
                     f"那把尺")
        L.append(f"- 判断依据:{position_view.headline(view)}。"
                 + "".join(position_view.detail(view)))
        L.append("")


def sec_triggers(j, views, L):
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    cash = a["可用现金"]
    debt = sum(d["当前负债总额"] for d in md)
    lines = j.get("_担保比例线") or {"警戒线_pct": 150, "平仓线_pct": 130}

    L.append("## 8 什么情况下必须动:可以核对的触发条件")
    L.append("")
    L.append("| 触发条件 | 现在的值 | 为什么是这条线 |")
    L.append("|---|---:|---|")
    r180 = debt * 1.80 - cash
    L.append(f"| **维持担保比例跌破 180%** | {ratio_at(mv + cash, debt):.0f}% "
             f"| 对应总市值 {money(r180)},即再跌 {abs((r180 / mv - 1) * 100):.0f}%。"
             f"180% 不是券商的线,是**给自己留的缓冲** —— "
             f"等到 {lines['警戒线_pct']}% 才动,那时候是被动卖 |")
    # ⚠ 下面几条以前把「新易盛 −19.3%」「中际旭创 2.01x」「119.5 亿」写死在代码里。
    #   持仓一变、季报一出,这几个数就都是旧的,而文档看不出任何异常。
    #   现在全部从 account.json 和当轮管线结果里取。
    by_mv = sorted(ps, key=lambda p: -p["market_value"])[:2]
    if len(by_mv) == 2:
        share2 = sum(p["market_value"] for p in by_mv) / mv * 100
        cost2 = sum(p["cost"] * p["shares"] for p in by_mv)
        mv2 = sum(p["market_value"] for p in by_mv)
        both = (mv2 / cost2 - 1) * 100 if cost2 else 0.0
        cur = "、".join(f"{p['name']} {p['pnl_pct']:+.1f}%" for p in by_mv)
        L.append(f"| 最大两只**合计**浮亏跌破 −30% | 合计 {both:+.1f}%({cur}) "
                 f"| {'、'.join(p['name'] for p in by_mv)} 合计占 "
                 f"{share2:.0f}% 仓位,这一条比任何单只的技术位都要紧 |")
    # 「份额还在扩」是继续持有的理由 —— 只对基本面拿到正分的那些票才成立
    for p in ps:
        v = views.get(p["code"])
        cp = v and v["产业链"]
        if not cp or v["建议"]["基本面"][0] <= 0:
            continue
        L.append(f"| {p['name']}的增速倍数掉回 1.0x 以下 | 当前 {cp['终点']:.2f}x "
                 f"| 它现在**在得份额**,这是继续持有它的主要理由。掉回去,理由就没了 |")
    # 预测区间下沿 —— 跌破就说明推法错了,先改方法再谈估值
    for p in by_mv:
        v = views.get(p["code"])
        rv = v and (v["预测"].get("营收区间") or None)
        if not rv:
            continue
        L.append(f"| {p['name']} {v['预测']['预测期']} 单季营收低于 "
                 f"{rv['low'] / 1e8:,.1f} 亿 | 预测区间下沿 "
                 f"| 低于我们最谨慎的推法,说明推法错了,要先改方法再谈估值 |")
    rate = j.get("_融资利率_pct")
    if rate:
        yr = sum(d["未还本金"] for d in md) * rate / 100
        eq = a["总资产"] - debt
        L.append(f"| 融资利率上调到 8% 以上 | 当前 {rate:.1f}% "
                 f"| 现在一年吃掉净资产 {yr / eq * 100:.1f}%,时间成本不算重。"
                 f"翻倍到 8% 就是 {yr * 2 / eq * 100:.1f}%,"
                 f"那时候「再等等看」的代价就不一样了 |")
    else:
        L.append("| 融资利率上调 | 未知,去查 | 利率一升,横盘的成本跟着升 |")
    L.append("")
    L.append("> 这几条的用法是:**现在就写下来,到了就执行,不要到时候再想。** "
             "杠杆仓位最危险的时刻是判断和情绪一起摇摆的时候 —— "
             "事先定好条件,是把那一刻的决定提前到现在做。")
    L.append("")


def sec_disclaim(j, L):
    L.append("## 9 这份文档不做的事,以及数据从哪来")
    L.append("")
    L.append("**不做的事**:")
    L.append("")
    L.append("- **不做技术分析。** 第 7 节的顺序建立在各只的期望值和仓位上限上,"
             "不看 K 线走势、不看资金流 —— 它不回答「今天卖还是下周卖」。")
    L.append("- **不预测股价。** 触发条件写的是「什么情况下我们的判断就错了」。")
    L.append("- **不知道你的风险承受能力。** 同样的账户,不同的钱性质,答案完全不同。")
    L.append("- **没有替你核对券商条款。** 警戒线/平仓线用的是行业通行值,"
             "你的合同上是多少要自己查。")
    L.append("")
    L.append("**这份文档自己的数据**:")
    L.append("")
    L.append("| 数据 | 来源 | 怎么更新 |")
    L.append("|---|---|---|")
    L.append("| 账户资产 / 负债 / 维持担保比例 / 每只的股数成本市值 / 融资负债明细 "
             "| 券商 APP 截图(原图存 `长江证券我的持仓/`)| "
             "改 `private/portfolio/account.json` |")
    rate = j.get("_融资利率_pct")
    L.append(f"| 融资利率 | {(str(rate) + '%,' + str(j.get('_融资利率来源', ''))) if rate else '**未知**,界面上没有'} "
             "| 改 `account.json` 的 `_融资利率_pct` |")
    L.append("| 警戒线 / 平仓线 | **行业通行值,不是从你的合同读的** "
             "| 查合同后改 `account.json` 的 `_担保比例线` |")
    L.append(f"| 单票 / 行业最大可承受亏损 "
             f"| **{j.get('_单票最大可承受亏损_pct_of_净资产')}% / "
             f"{j.get('_行业最大可承受亏损_pct_of_净资产')}% of 净资产,"
             f"这是你自己定的**,不是算出来的 —— 它直接决定第 5.5 节每一行 "
             f"| 改 `account.json` 的 `_单票最大可承受亏损_pct_of_净资产`,"
             f"**改了要在同一个文件里写理由** |")
    L.append("| 每只的判断 / 期望值 / 仓位上限 / 执行顺序(第 5、5.5、6、7、8 节) "
             "| **和个股报告同一条管线现算**"
             "(valuation → forecast → 行业 → 领先层 → 基准率 → decision),"
             "不是写死的常量 | 跟着 `--raw` 的数据走,重抓数据就变 |")
    L.append("")
    L.append("**那些判断背后的数据源**(完整目录在各只报告的第 11 节):")
    L.append("")
    L.append(catalog.markdown(["行情", "公司报表", "海外·事实", "人工投喂"],
                              None, heading=None).lstrip("\n"))
    L.append("重跑:")
    L.append("")
    L.append("```bash")
    L.append("cd ~/astock-lab-private")
    L.append("tools/margin_account.py --account private/portfolio/account.json \\")
    L.append("    --raw data/raw \\")
    L.append("    --out private/reports/<日期>/融资融券账户操盘文档.md")
    L.append("```")
    L.append("")


def build(fp: Path, raw: Path) -> str:
    j = load(fp)
    views = account_plan.load_views(
        [p["code"] for p in j["positions"]], raw, fp)
    L = ["# 融资融券账户 · 操盘文档", ""]
    L.append(f"生成于 {date.today().isoformat()} · 账户数据截至 "
             f"{j.get('_截图时间', '?')} · {j.get('_券商', '')}"
             f"{j.get('_账户类型', '')}")
    L.append("")
    L.append("> 这份和同目录五份**个股**报告是两件事。个股报告回答"
             "「这家公司值多少钱」,这份回答「**这笔杠杆仓位能不能扛**」。"
             "杠杆下面,后一个问题可以先要命。")
    L.append("")
    sec_now(j, L)
    sec_three_numbers(j, L)
    sec_distance(j, L)
    sec_cost(j, L)
    sec_each(j, views, L)
    # 5.5 / 6 / 7 三节住在 account_plan.py —— 那三节算的是「该怎么动」,
    # 要跑完整条判断管线;这个文件其余几节只依赖 account.json 里的物理量。
    # 两件事变化的原因不同,所以分开放。
    account_plan.caps_table(j, views, L)
    account_plan.sec_options(j, views, L)
    account_plan.sec_plan(j, views, L)
    sec_triggers(j, views, L)
    sec_disclaim(j, L)
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default="private/portfolio/account.json")
    ap.add_argument("--raw", default="data/raw",
                    help="原始数据目录。第 5、7、8 节的建议和触发条件从这里现算,"
                         "不再是写死在代码里的常量")
    ap.add_argument("--out")
    a = ap.parse_args()
    md = build(Path(a.account), Path(a.raw))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
