#!/usr/bin/env python3
"""账户文档的第 6、7 节 —— 可以怎么做,以及先动哪只。

## 为什么从 `margin_account.py` 里拆出来

原来这两节和「账户现在什么状态 / 离强平多远 / 利息在吃什么」挤在一个 778 行的
文件里。那几节算的是**账户本身的物理量**(担保比例、距离、利息),
这两节算的是**该怎么动**(顺序、方案、仓位上限)——
前者只依赖 `account.json`,后者要跑完整条判断管线。
两件事变化的原因完全不同:改一个利率不该碰到执行顺序的代码。

## 第 6 节和第 7 节必须是同一个东西

原来第 6 节把 A/B/C/D 四个方案的股票代码**写死**,第 7 节另算一套顺序。
今天恰好一致,建议一变就分叉,而且分叉了不报错:
两张表各自看着都合理,合起来自相矛盾。
现在两节都从同一个 `sell_order()` 取,方案 B = 第 1 步,C = 前 2 步,一一对应。

## 排序规则 2026-09-02 改成三条(原来两条)

| 顺序 | 依据 | 为什么在这个位置 |
|---|---|---|
| ① | **先动超过仓位上限的** | 上限是从 bear 情景倒推的:超限意味着最坏那一档兑现时,亏掉的净资产超过你自己设的可承受线。**这一条不看期望值** —— 期望为正也不代表你扛得住最坏那一档 |
| ② | 再按**期望值从差到好** | 期望值最差的先卖。卖出的理由必须是判断,不是账面亏了多少 |
| ③ | 同为减仓时,**没有融资负债的先卖** | 卖有负债的那只,拿回的钱要先还它自己的债,能腾出的现金反而少;卖没负债的,全额都能拿去还债 |

**三条都不是「哪只亏得多」。**「等回本再走」把卖出条件从「判断变了」
换成了「价格回到某个数」,而价格不知道你的成本是多少。

依赖:无(读 position_view / decision 的输出)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

import decision
import position_view


def wan(v, nd=2):
    return "—" if v is None else f"{v / 1e4:,.{nd}f}"


def money(v, nd=0):
    return "—" if v is None else f"{v:,.{nd}f}"


def ratio_at(assets: float, debt: float) -> float:
    return assets / debt * 100 if debt else float("inf")


def drop_to_line(mv: float, cash: float, debt: float, line_pct: float):
    """市值要跌多少,维持担保比例才掉到 line_pct。**和 margin_account 里那份逐字相同** ——
    两处都要用,而跨模块 import 会让这个文件反过来依赖它拆出来的那个文件。"""
    target_mv = debt * line_pct / 100 - cash
    if target_mv <= 0:
        return None
    return (target_mv / mv - 1) * 100


def load_views(codes, raw: Path, holdings: Path | None = None) -> dict:
    """对每只跑一遍完整管线。某只算不出来时**不静默跳过** ——
    跳过的话第 5 节会少一只,而少的那只恰恰是数据有问题的那只。

    ⚠ **`holdings` 要传。** 不传的话每只票拿到的是空持仓,
    净资产和「单票最大可承受亏损」都拿不到,**仓位上限整列算不出**,
    而超限是执行顺序的第一条规则 —— 结果是每只都显示「没超限」,
    顺序全按期望值排,而且看着完全正常。
    """
    import sections_position as sp
    out, failed = {}, []
    for c in codes:
        try:
            out[c] = position_view.build(c, raw, sp.load_position(holdings, c))
        except SystemExit as e:
            failed.append(f"{c}({e})")
    if failed:
        print(f"  ⚠ 这几只算不出来,第 5 节会说明:{'、'.join(failed)}")
    return out


def is_sell(view) -> bool:
    return bool(view) and view["建议"]["动作"].startswith(("减仓", "清仓"))


def over_limit(view) -> bool:
    return bool(view) and bool((view["建议"].get("决策") or {}).get("超限"))


def over_amount(view, market_value: float) -> float:
    """超过仓位上限的部分,单位是钱。**减到上限要卖的就是这个数。**

    ⚠ 它和「还清这只的融资负债要卖多少」是两个不同的约束,**取大的那个** ——
    2026-09-02 第一版只按还债算,中际旭创写「卖 291,877 就够(不用全卖)」,
    而它的上限要求至少卖 402,820。两个数印在同一份文档的两节里,差 11 万,
    读者按第 7 节做就还在超限,而第 5.5 节明明说了超限。
    """
    if not view:
        return 0.0
    d = (view["建议"].get("决策") or {})
    if not d.get("超限"):
        return 0.0
    cap = d.get("仓位上限") or {}
    if not cap.get("ok"):
        return 0.0
    return max(0.0, market_value - cap["上限金额"])


def ev_of(view):
    """这只票的期望值。算不出来返回 None —— **不要拿 0 顶替**,
    0 在排序里的意思是「不好不坏」,而算不出来的意思是「不知道」,两回事。"""
    if not view:
        return None
    return (view["建议"].get("决策") or {}).get("EV")


# 卖出的三笔费用。**都是估算,以券商实际扣的为准。**
# 为什么要给:报告里写「卖 402,820 元」,到账一定比这个数少,而八份文档
# 一个字都没提过佣金印花税 —— 读者只能自己再算一遍,或者干脆不知道差多少。
FEE_COMMISSION = 0.00025      # 佣金 万 2.5(双向;这里只算卖出这一笔)
FEE_STAMP = 0.001             # 印花税 千 1(**只在卖出时收**)
FEE_TRANSFER = 0.00001        # 过户费 万 0.1(沪深两市现在都收)
LOT = 100                     # A 股一手 = 100 股


def shares_for(pos: dict, amount: float) -> tuple[int, float]:
    """要卖 `amount` 元,实际该报多少股 → (股数, 按现价算的实际卖出金额)。

    **向上取整到整手**,理由:少卖一手就还在上限之上,而上限那条是硬约束;
    多卖一手只是多变现几百块。两个方向的代价不对称,所以往「够到」那边取。
    最后一手不足 100 股时(比如全卖 2,580 股)按实际持股数报,不再取整 ——
    零股可以整笔卖出,不能拆着卖。
    """
    px, held = pos.get("price"), pos.get("shares")
    if not px or not held or amount <= 0:
        return 0, 0.0
    need = int(-(-amount // px))                      # 向上取整到股
    if need >= held:
        return int(held), held * px
    lots = int(-(-need // LOT))                       # 再向上取整到手
    sh = min(int(held), lots * LOT)
    return sh, sh * px


def _fees(rows, L) -> None:
    """卖出费用估算。**标明是估算**,并且逐步给,不只给合计 ——
    只给合计的话读者没法判断哪一步值不值得拆成两次做。"""
    if not rows:
        return
    rate = FEE_COMMISSION + FEE_STAMP + FEE_TRANSFER
    L.append(f"**卖出费用(估算,以券商实际为准)** —— 佣金按万 2.5、"
             f"印花税卖出千 1、过户费万 0.1,合计约 **{rate * 1000:.2f}‰**:")
    L.append("")
    L.append("| 这一步卖 | 金额 | 佣金 | 印花税 | 过户费 | 合计 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    tot = 0.0
    for name, amt in rows:
        fee = amt * rate
        tot += fee
        L.append(f"| {name} | {money(amt)} | {money(amt * FEE_COMMISSION, 0)} "
                 f"| {money(amt * FEE_STAMP, 0)} | {money(amt * FEE_TRANSFER, 2)} "
                 f"| **{money(fee, 0)}** |")
    total_amt = sum(a for _, a in rows)
    L.append(f"| **合计** | {money(total_amt)} | | | | **{money(tot, 0)}** |")
    L.append("")
    L.append(f"> 上表的金额是**取整到手之后的实际卖出金额**,所以它比"
             f"「要卖多少」那一列的加总略多 —— 差的就是向上取整那部分"
             f"(少卖一手就还在上限之上,多卖一手只是多变现几百块,"
             f"两个方向的代价不对称)。")
    L.append("")
    L.append(f"> 这几步一共卖 {money(total_amt)},费用大约 **{money(tot, 0)} 元**"
             f"(占卖出金额 {tot / total_amt * 100:.3f}%)。"
             f"**量级上不改变任何结论**,但到账金额会比上表少这么多 —— "
             f"写出来是为了让你下单时心里有数,不用自己再算一遍。"
             f"佣金各家不同(有最低 5 元一笔的),印花税是法定的,"
             f"**实际以你的交割单为准**。")
    L.append("")


def sell_order(j, views):
    """排出执行顺序,返回 (该减的, 该留的) 两串。三条规则见文件头。

    ⚠ **第 6 节的方案和第 7 节的顺序必须是同一个东西。** 原来第 6 节把
    A/B/C/D 四个方案的股票代码写死,第 7 节另算一套顺序 —— 今天恰好一致,
    建议一变就分叉,而且分叉了不报错:两张表各自看着都合理,合起来自相矛盾。
    """
    debt_of = {d["code"]: d["当前负债总额"] for d in j["margin_debt"]}

    def rank(p):
        v = views.get(p["code"])
        if not v:                                   # 算不出建议的排最后,不动
            return (2, 2, 0.0, 1)
        ev = ev_of(v)
        return (0 if over_limit(v) else 1,          # ① 先动超限的
                0 if ev is not None else 1,         # 算不出 EV 的排在算得出的后面
                ev if ev is not None else 0.0,      # ② 期望值从差到好
                0 if p["code"] not in debt_of else 1)   # ③ 没融资负债的先卖

    ordered = sorted(j["positions"], key=rank)
    return ([p for p in ordered if is_sell(views.get(p["code"]))],
            [p for p in ordered if not is_sell(views.get(p["code"]))])


def caps_table(j, views, L) -> dict:
    """仓位上限表 —— 单只 + 行业合计。**这张表在执行顺序之前**,
    因为「谁超限」决定了先动谁。

    行业合计单独卡一条,是因为**同行业的票会一起跌**:三只光模块分开看
    各自不超限,合起来在 bear 情景下能让净资产掉三成 ——
    「分散到三只」并没有分散风险,只是把同一个赌注写成了三行。
    """
    import sections_position as sp
    equity = sp.net_equity(j)
    acct = {"净资产": equity,
            "单票最大可承受亏损_pct": j.get("_单票最大可承受亏损_pct_of_净资产"),
            "行业最大可承受亏损_pct": j.get("_行业最大可承受亏损_pct_of_净资产"),
            "单票硬上限_pct": j.get("_单票硬上限_pct_of_净资产"),
            "行业硬上限_pct": j.get("_单行业硬上限_pct_of_净资产")}
    rows = []
    for p in j["positions"]:
        v = views.get(p["code"])
        d = (v or {}).get("建议", {}).get("决策") or {}
        sc = d.get("三情景") or {}
        bear = (sc.get("情景") or {}).get("bear", {}).get("涨跌") if sc.get("ok") else None
        cap = d.get("仓位上限") or {}
        if not cap.get("ok") and not cap.get("why"):
            # 整个决策提前退出(行业弱匹配 / 没定尺 / 三情景算不出来)时根本没有
            # 「仓位上限」这个键。**原因要从决策那一层原样带过来** ——
            # 印一句「缺数据」等于把「我们不敢用这把尺」说成「数据没抓到」,
            # 两件事该做的完全不同。
            cap = {"ok": False, "why": d.get("why") or "这只票没给出决策,"
                                                       "见它自己那份报告第 11 节"}
        rows.append({"code": p["code"], "name": p["name"], "市值": p["market_value"],
                     "行业": (v or {}).get("建议", {}).get("行业"),
                     "bear跌幅": bear,
                     "上限": cap, "超限": d.get("超限"),
                     "EV": d.get("EV")})
    L.append("## 5.5 仓位上限:每只最多能占多少")
    L.append("")
    if not equity:
        L.append("`account.json` 里没有净资产,这一节算不了。")
        L.append("")
        return {"rows": rows, "行业": {}}
    L.append(f"上限不是拍一个「单票不超过 20%」。那种数和这只票**能跌多少**"
             f"毫无关系:同样 20% 仓位,一只 bear 跌 30% 的票让你亏 6%,"
             f"一只 bear 跌 70% 的票让你亏 14%,风险差一倍多。")
    L.append("")
    L.append("```")
    L.append(f"模型上限% = 可承受亏损 {acct['单票最大可承受亏损_pct']}% of 净资产"
             f" ÷ |bear 情景跌幅| × 确信度系数(0.5 + 0.5 × 得分÷本行业可得分)")
    L.append(f"硬线上限% = {acct['单票硬上限_pct']}% of 净资产"
             f"(**和估值模型无关的集中度线**)")
    L.append("单票上限%  = 两条取小的那条")
    L.append("```")
    L.append("")
    L.append(f"「可承受亏损 {acct['单票最大可承受亏损_pct']}%」和「硬线 "
             f"{acct['单票硬上限_pct']}%」都是 `account.json` 里的显式参数,"
             f"**你可以改,但改了要在同一个文件里写清楚为什么改** —— "
             f"它们直接决定下面每一行的结论。")
    L.append("")
    L.append("**为什么要有第二条硬线**:第一条从 bear 情景倒推,所以它会**跟着估值模型漂** —— "
             "哪天 PE 带变宽、bear 跌幅算成 −20%,上限就跳到 37.5%,"
             "而那时候你的杠杆和集中度一点没变。**风控结论不该随估值模型漂。**"
             "硬线丑但稳:模型出错的时候它仍然成立。")
    L.append("")
    L.append("| 股票 | 行业 | bear 跌幅 | 确信度 | 模型上限 | 硬线上限 "
             "| **实际上限** | 哪条起作用 | 上限金额 | 现在占 | 超出 |")
    L.append("|---|---|---:|---|---:|---:|---:|---|---:|---:|---:|")
    for r in rows:
        c = r["上限"]
        now = r["市值"] / equity * 100
        if c.get("ok"):
            over = max(0.0, r["市值"] - c["上限金额"])
            L.append(f"| {r['name']}({r['code']}) | {r['行业'] or '—'} "
                     f"| {r['bear跌幅']:+.1f}% "
                     f"| {c['档']} {c['分']}/{c['满分']}({c['系数']}) "
                     f"| {c['模型上限pct']:.1f}% "
                     + (f"| {c['硬上限pct']:.0f}% " if c.get("硬上限pct") is not None
                        else "| 没设 ")
                     + f"| **{c['上限pct']:.1f}%** | {c['起作用的']} "
                     f"| {money(c['上限金额'])} | **{now:.1f}%** | "
                     + (f"**{money(over)}**" if over > 0 else "—") + " |")
        else:
            L.append(f"| {r['name']}({r['code']}) | {r['行业'] or '—'} | — | — "
                     f"| 算不出 | — | 算不出 | — | — | {now:.1f}% | — |")
            L.append(f"| ↳ 为什么算不出 | | | | {c.get('why', '缺数据')} "
                     f"| | | | | | |")
    L.append("")
    sec = decision.sector_cap(
        [{"行业": r["行业"], "市值": r["市值"], "bear跌幅": r["bear跌幅"]} for r in rows],
        acct)
    if sec.get("ok"):
        L.append("### 5.5.1 同一行业合计的上限")
        L.append("")
        L.append(f"同行业的票会一起跌。三只光模块分开看各自不超限,"
                 f"合起来在 bear 情景下能让净资产掉三成 —— "
                 f"**「分散到三只」并没有分散风险,只是把同一个赌注写成了三行。**"
                 f"所以还要卡一条:同行业合计不超过"
                 f"「净资产的 {sec['可承受亏损pct']}% ÷ |该行业市值加权 bear 跌幅|」"
                 f"和「净资产的 {acct['行业硬上限_pct']}%(硬线)」两者的小值。")
        L.append("")
        L.append("| 行业 | 几只 | 合计市值 | 加权 bear 跌幅 | 模型上限 | 硬线上限 "
                 "| **实际上限** | 哪条起作用 | 上限金额 | 现在占 | 超出 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")
        for name, x in sorted(sec["按行业"].items(),
                              key=lambda kv: -(kv[1].get("市值") or 0)):
            if x.get("ok"):
                L.append(f"| **{name}** | {x['只数']} | {money(x['市值'])} "
                         f"| {x['加权bear跌幅']:+.1f}% | {x['模型上限pct']:.1f}% "
                         + (f"| {x['硬上限pct']:.0f}% " if x.get("硬上限pct") is not None
                            else "| 没设 ")
                         + f"| **{x['上限pct']:.1f}%** | {x['起作用的']} "
                         f"| {money(x['上限金额'])} | **{x['现仓位pct']:.1f}%** | "
                         + (f"**{money(x['超出金额'])}**" if x["超出金额"] > 0 else "—")
                         + " |")
            else:
                L.append(f"| {name} | — | {money(x.get('市值'))} | — | 算不出 | — "
                         f"| 算不出 | — | — | — | {x.get('why', '')} |")
        L.append("")
    # ⚠ 「可承受亏损 10%」是审核方定的**初值**,不是算出来的。
    #   只印一个 10% 的结论,读者没法判断这个参数有多要紧 ——
    #   所以把 10/15/20 三档一起算出来,让他看见换个参数结论变不变。
    L.append("### 5.5.2 换个可承受亏损,结论变不变")
    L.append("")
    L.append(f"上面那个「可承受亏损 {acct['单票最大可承受亏损_pct']}%」是**初值,"
             f"不是算出来的**。它直接决定谁超限、要卖多少,所以这里把三档"
             f"并排放出来 —— **看它变了之后结论会不会翻**,比只看一个数有用。")
    L.append("")
    L.append("| 股票 | 现在占净资产 | 上限 @10% | 上限 @15% | 上限 @20% "
             "| 超出 @10% | 超出 @15% | 超出 @20% |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    base_tol = acct["单票最大可承受亏损_pct"] or 10
    for r in rows:
        c = r["上限"]
        now = r["市值"] / equity * 100
        if not c.get("ok"):
            L.append(f"| {r['name']} | {now:.1f}% | 算不出 | 算不出 | 算不出 "
                     f"| — | — | — |")
            continue
        caps, overs = [], []
        for tol in (10, 15, 20):
            # ⚠ 只有**模型上限**和可承受亏损成正比;硬线不随它动(那正是硬线的意义)。
            #   拿最终上限直接按比例放会把硬线也放大,读者会以为调参数能救回来。
            capp = c["模型上限pct"] / base_tol * tol
            if c.get("硬上限pct") is not None:
                capp = min(capp, float(c["硬上限pct"]))
            caps.append(f"{capp:.1f}%")
            over = max(0.0, r["市值"] - equity * capp / 100)
            overs.append(f"**{money(over)}**" if over > 0 else "—")
        L.append(f"| {r['name']} | **{now:.1f}%** | " + " | ".join(caps)
                 + " | " + " | ".join(overs) + " |")
    if sec.get("ok"):
        base_stol = sec["可承受亏损pct"] or 20
        for name, x in sorted(sec["按行业"].items(),
                              key=lambda kv: -(kv[1].get("市值") or 0)):
            if not x.get("ok"):
                continue
            caps, overs = [], []
            for tol in (10, 15, 20):
                capp = x["模型上限pct"] / base_stol * (tol * 2)  # 行业档默认是单票的两倍
                if x.get("硬上限pct") is not None:
                    capp = min(capp, float(x["硬上限pct"]))
                caps.append(f"{capp:.1f}%")
                over = max(0.0, x["市值"] - equity * capp / 100)
                overs.append(f"**{money(over)}**" if over > 0 else "—")
            L.append(f"| *{name} 合计* | **{x['现仓位pct']:.1f}%** | "
                     + " | ".join(caps) + " | " + " | ".join(overs) + " |")
    L.append("")
    L.append("> 读法:**看哪一行在三档之间换了符号**。三档都超的,结论跟这个参数无关,"
             "调参数救不回来;只在 10% 那档超的,结论**是被这个参数定的** —— "
             "那就得先想清楚自己到底能承受多少,再看它该不该减。"
             "(行业那几行按「单票档 × 2」同比例放,和 5.5.1 用的是同一个比例关系。"
             "**只有模型那条随参数放,硬线不动** —— 那正是硬线存在的意义。)")
    L.append("")
    L.append("> ⚠ **上限的分母是净资产,不是总市值。** 总市值里有借来的钱:"
             f"这个账户总市值 {money(sum(p['market_value'] for p in j['positions']))}、"
             f"净资产 {money(equity)}。按总市值算「占 32%」听着还好,"
             f"按自己的钱算就不是那个数了 —— **亏的是自己的钱,不是券商的**。")
    L.append("")
    return {"rows": rows, "行业": sec.get("按行业", {})}


def why_this_one(p, views, all_ps) -> str:
    """这一只凭什么排在这个位置 —— **只说能从数据里判断出来的**。

    判断不出来就不写形容词。原来写的是「把三只**非核心**的都清掉」——
    「非核心」听着像个理由,其实数据里没有这个概念,是凑出来的。
    """
    tags = []
    if p["pnl_pct"] == max(x["pnl_pct"] for x in all_ps):
        tags.append("浮盈最高" if p["pnl_pct"] > 0 else "浮亏最小")
    v = views.get(p["code"])
    evs = [ev_of(views[x["code"]]) for x in all_ps if x["code"] in views]
    evs = [x for x in evs if x is not None]
    my_ev = ev_of(v)
    if my_ev is not None and evs and my_ev == min(evs):
        tags.append(f"期望值最差 {my_ev:+.1f}%")
    if over_limit(v):
        tags.append("超仓位上限")
    if p["market_value"] == min(x["market_value"] for x in all_ps):
        tags.append("仓位最小")
    return "、".join(tags)


def sec_options(j, views, L):
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    cash = a["可用现金"]
    debt = sum(d["当前负债总额"] for d in md)
    equity = a["总资产"] - debt
    lines = j.get("_担保比例线") or {"平仓线_pct": 130}
    by = {p["code"]: p for p in ps}

    def after(sell_codes, extra_cash=0.0):
        """卖掉这些票、拿回来的钱全部还债之后,账户变成什么样。

        **净资产不变** —— 卖出换现金再还债,只改变杠杆和担保比例,不改变你的钱。
        这一点很多人算错,以为减仓等于亏损落袋。落袋的是浮亏变实亏,
        但净资产这个数在卖出的那一刻是不变的。
        """
        got = sum(by[c]["market_value"] for c in sell_codes if c in by) + extra_cash
        repay = min(got, debt)
        return {"卖出": got, "还债": repay,
                "新负债": debt - repay,
                "新市值": mv - sum(by[c]["market_value"] for c in sell_codes if c in by),
                "新总资产": a["总资产"] - repay,
                "新现金": cash + got - repay}

    L.append("## 6 如果把这几只**整只卖掉**会怎样(后果演示)")
    L.append("")
    L.append("**下面不是建议,是算出来的后果。** 每一档都假设把那几只**全部卖光**,"
             "为的是看清「杠杆降到什么程度要付出多大代价」这条曲线。"
             "**实际该卖多少在第 7 节**,那里按规则算的是「减到仓位上限 / 还清负债」"
             "两条约束里大的那个,**通常不用全卖** —— 两节回答的不是同一个问题,"
             "数不一样是正常的,别把这里的 D 档当成执行量。")
    L.append("")
    L.append("每个数字你都可以自己验算:卖出换成现金还债,总资产和总负债同额减少,"
             f"**你的净资产一分不变**(始终是 {money(equity)} 元 = 总资产 − 总负债)。"
             f"变的只有杠杆和离强平的距离。")
    L.append("")

    # ⚠ 这几个方案以前把股票代码写死(`["688328", "002353", "300476"]`)。
    #   建议一变就和第 7 节的执行顺序分叉,而且不报错。现在从**同一个** sell_order
    #   取前缀:方案 B = 第 1 步,C = 前 2 步,D = 前 3 步 …… 一一对应。
    to_sell, _kept = sell_order(j, views)
    plans = [("A 什么都不动", [], "维持现状")]
    for i, p in enumerate(to_sell, 1):
        codes = [x["code"] for x in to_sell[:i]]
        names = " + ".join(x["name"] for x in to_sell[:i])
        tag = why_this_one(p, views, ps)
        plans.append((f"{chr(ord('A') + i)} 卖{names}",
                      codes,
                      f"在上一档基础上加卖{p['name']}" + (f"({tag})" if tag else "")
                      if i > 1 else
                      f"先卖{p['name']}" + (f"({tag})" if tag else "")))
    rate = (j.get("_融资利率_pct") or 6.0) / 100
    L.append(f"| 方案 | 卖出金额 | 还债后负债 | 维持担保比例 | 杠杆 | 离平仓线还要跌 | "
             f"年省利息(按{rate * 100:.0f}%) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, codes, _ in plans:
        r = after(codes)
        nd, nmv, nta = r["新负债"], r["新市值"], r["新总资产"]
        ratio = ratio_at(nta, nd) if nd else None
        lev = nmv / equity
        drop = drop_to_line(nmv, r["新现金"], nd, lines["平仓线_pct"]) if nd else None
        saved = r["还债"] * rate
        L.append(f"| {name} | {money(r['卖出'])} | {money(nd)} "
                 + (f"| {ratio:.0f}% " if ratio else "| 无负债 ")
                 + f"| {lev:.2f}x "
                 + (f"| **{abs(drop):.1f}%** " if drop else "| 不会被强平 ")
                 + f"| {money(saved)} |")
    L.append("")
    for name, codes, why in plans:
        r = after(codes)
        L.append(f"**{name}** —— {why}")
        if not codes:
            L.append(f"- 杠杆保持 {mv / equity:.2f}x,离平仓线 "
                     f"{abs(drop_to_line(mv, cash, debt, lines['平仓线_pct'])):.1f}%")
            L.append("- 利息继续走,负债和平仓线的距离每天缩短一点点")
            L.append("- **赌的是**:两只光模块在利息吃完之前先涨回来")
        else:
            names = "、".join(by[c]["name"] for c in codes if c in by)
            pnl = sum(by[c]["pnl"] for c in codes if c in by)
            L.append(f"- 卖出 {names},实现盈亏 {money(pnl, 2)} 元")
            L.append(f"- 负债从 {money(debt)} 降到 {money(r['新负债'])},"
                     f"离平仓线从 {abs(drop_to_line(mv, cash, debt, lines['平仓线_pct'])):.1f}% "
                     f"拉开到 "
                     + (f"{abs(drop_to_line(r['新市值'], r['新现金'], r['新负债'], lines['平仓线_pct'])):.1f}%"
                        if r["新负债"] else "不会被强平"))
            L.append(f"- **代价**:卖掉的那部分不再参与上涨。"
                     f"如果它们后面涨 30%,少赚 {money(r['卖出'] * 0.3)} 元")
        L.append("")
    L.append("> **哪个方案对,取决于一件我不知道的事:你能不能承受净资产再跌 30%。** "
             "这不在数据里 —— 它取决于这笔钱是什么钱、多久要用、以及你晚上睡不睡得着。"
             "数据能告诉你的只有上面那张表:每种做法之后,账户长什么样。")
    L.append("")
    L.append("⚠ **再说一遍这一节和第 7 节的关系。** 这一节每一档都是「整只卖光」;"
             "第 7 节按规则算的是「减到仓位上限」和「还清剩余负债」两条约束里大的那个,"
             "所以**同一只票在两节里的卖出金额会不一样**。"
             "要执行,按第 7 节;这一节只用来看「代价曲线长什么样」。")
    L.append("")


def plan_amounts(j, views, debt: float):
    """算出每只该卖多少 —— **两条约束分两遍走,不再取 max。**

    ## 2026-09-03 改:旧写法「卖出量 = max(减到上限, 还清剩余负债)」是错的

    它把两条本来分开的约束塞进同一个 max,而「还清剩余负债」这一项在**排在
    第一位的那只票**身上永远等于「剩余负债全额」—— 那个数通常比它自己的市值还大,
    于是排第一的票被全卖。实测:新易盛 EV −0.3%(中性带里),因为排第一,
    `max(超限 334,142, 剩余负债 614,575)` 把它整只清掉;
    而 EV −11.4% 的杰瑞排第三,只按判断减。**一只中性的票被清仓,
    一只判断该减的票反而减得少** —— 顺序决定了卖多少,这不是任何一条规则的本意。

    现在分三块算,每块回答一个不同的问题:

    | 块 | 问什么 | 量 |
    |---|---|---|
    | ① 减到上限 | 最坏那一档兑现时,亏掉的净资产超没超可承受线 | 超出上限的部分 |
    | ② 按判断 | 这只票自己的 EV 落在哪一档 | 清仓 = 全卖;减仓 = 减 `TRIM_FRACTION` |
    | ③ 追加还债 | ①② 卖完之后债还清了没有 | 没清才触发,按 EV 从差到好追加 |

    ①② 对同一只票**取大的那个**(它们说的是同一只票该减多少,不冲突);
    ③ 是账户层面的,只在前两块的钱不够还债时才动,**而且从 EV 最差的那只开始** ——
    追加卖的理由是「降杠杆」,那就该从最不值得留的那只身上出。

    返回 {code: {"量": 元, "为什么": 文字}}。
    """
    import decision
    by = {p["code"]: p for p in j["positions"]}
    plan = {}
    for code, p in by.items():
        v = views.get(code)
        if not v:
            continue
        act = v["建议"]["动作"]
        cap_amt = over_amount(v, p["market_value"])
        judge_amt, judge_why = 0.0, ""
        if act.startswith("清仓"):
            judge_amt, judge_why = p["market_value"], "按判断:清仓 = 全卖"
        elif act.startswith("减仓"):
            judge_amt = p["market_value"] * decision.TRIM_FRACTION
            judge_why = f"按判断:减仓 = 减 {decision.TRIM_FRACTION:.0%}"
        if cap_amt <= 0 and judge_amt <= 0:
            continue
        if cap_amt >= judge_amt:
            plan[code] = {"量": cap_amt, "为什么": "减到仓位上限"}
        else:
            plan[code] = {"量": judge_amt, "为什么": judge_why}
    # ③ 追加还债 —— 只在前两块不够时触发,按 EV 从差到好
    got = sum(x["量"] for x in plan.values())
    short = debt - got
    if short > 0:
        cand = [c for c in by
                if views.get(c) and is_sell(views[c])]
        cand.sort(key=lambda c: (ev_of(views[c]) if ev_of(views[c]) is not None
                                 else 0.0))
        for c in cand:
            if short <= 0:
                break
            room = by[c]["market_value"] - plan.get(c, {}).get("量", 0.0)
            if room <= 0:
                continue
            add = min(room, short)
            short -= add
            cur = plan.setdefault(c, {"量": 0.0, "为什么": ""})
            cur["量"] += add
            cur["为什么"] = ((cur["为什么"] + " + " if cur["为什么"] else "")
                             + "追加卖到还清负债")
    return plan


def sec_plan(j, views, L):
    """第 7 节:按规则实际该卖多少。量的算法见 `plan_amounts`。

    排序依据三条,**都不是「哪只亏得多」**:
      ① 先动超过仓位上限的(这一条不看期望值)
      ② 再按期望值从差到好
      ③ 同为减仓时,没有融资负债的先卖(卖有负债的那只,拿回的钱要先还它自己的债)
    """
    import decision
    a, ps, md = j["account"], j["positions"], j["margin_debt"]
    mv = sum(p["market_value"] for p in ps)
    debt_of = {d["code"]: d["当前负债总额"] for d in md}
    debt = sum(debt_of.values())
    equity = a["总资产"] - debt
    cash = a["可用现金"]
    lines = j.get("_担保比例线") or {"平仓线_pct": 130}

    to_sell, kept = sell_order(j, views)
    ordered = to_sell + kept
    plan = plan_amounts(j, views, debt)

    L.append("## 7 按规则实际该卖多少(执行量)")
    L.append("")
    L.append("**这一节和第 6 节回答的不是同一个问题。** 第 6 节每一档都假设「整只卖光」,"
             "是为了看清代价曲线;这一节按规则算每只**实际**该卖多少 —— "
             "所以同一只票在两节里的数会不一样,**要执行看这一节**。")
    L.append("")
    L.append("**卖多少,分三块算,每块回答一个不同的问题**"
             "(2026-09-03 改;旧写法把它们塞进一个 `max`,"
             "结果**排在第一位的那只票会被剩余负债全额吃掉** —— "
             "一只期望值在中性带里的票因为排序靠前被整只清掉,"
             "而期望值最差的那只反而减得少。顺序不该决定卖多少):")
    L.append("")
    L.append("| 块 | 问什么 | 卖多少 |")
    L.append("|---|---|---|")
    L.append("| ① **减到仓位上限** | 最坏那一档兑现时,亏掉的净资产超没超你设的可承受线 "
             "| 超出上限的那部分 |")
    L.append(f"| ② **按它自己的判断** | 这只票的期望值落在哪一档 "
             f"| 清仓 = 全卖;减仓 = 减 **{decision.TRIM_FRACTION:.0%}**"
             f"(和个股报告 11.8 节同一个数) |")
    L.append("| ③ **追加还债** | ①② 卖完之后,债还清了没有 "
             "| **没清才触发**,按期望值从差到好追加 |")
    L.append("")
    L.append("①② 对同一只票取大的那个 —— 它们说的是同一只票该减多少,不冲突;"
             "③ 是**账户层面**的,只在前两块的钱不够还债时才动,"
             "而且从期望值最差的那只开始:追加卖的理由是降杠杆,"
             "那就该从最不值得留的那只身上出。")
    L.append("")
    L.append("**先动哪只**,三条,**没有一条是「哪只亏得多」**:"
             "① 先动超过仓位上限的(不看期望值)· ② 再按期望值从差到好 · "
             "③ 同为减仓时没有融资负债的先卖(卖有负债的那只,"
             "拿回的钱要先还它自己的债,能腾出的现金反而少)。")
    L.append("")
    L.append("| 顺序 | 股票 | 建议 | 期望值 | 超限 | 市值 | 这只的融资负债 | 浮盈亏 "
             "| 按规则要卖 | 为什么是这个量 | **实际报单(整手)** | 卖完之后 |")
    L.append("|---|---|---|---:|:-:|---:|---:|---:|---:|---|---|---|")
    run_debt, run_mv, run_cash = debt, mv, cash
    steps = 0
    sold_rows, partial, keep_first = [], None, None
    for p in ordered:
        v = views.get(p["code"])
        if v:
            act = v["建议"]["动作"]
            ev = ev_of(v)
            sc = f"{ev:+.1f}%" if ev is not None else "—"
            ol = "**超**" if over_limit(v) else "—"
        else:
            act, sc, ol = "算不出", "—", "—"
        d = debt_of.get(p["code"])
        item = plan.get(p["code"])
        need, why_amt, lots, after = "—", "—", "—", "**不动**"
        if item and item["量"] > 0:
            steps += 1
            why_amt = item["为什么"]
            need = money(item["量"])
            # ⚠ **后面每一个数都用取整后的实际金额算。** 金额一列、股数一列
            #   各说各的,读者不知道担保比例是按哪个算的 —— 2026-09-02 那一版
            #   写着「卖 397,875」又写着「500 股≈429,650」,两个数差 3 万。
            sh, amt = shares_for(p, item["量"])
            if sh:
                full = "全卖" if sh >= p["shares"] else f"剩 {p['shares'] - sh:,} 股"
                lots = f"**{sh:,} 股**({sh // 100} 手)= {money(amt)},{full}"
                sold_rows.append((p["name"], amt))
                repay = min(amt, run_debt)
                run_mv -= amt
                run_debt -= repay
                run_cash += amt - repay
                if run_debt <= 0:
                    after = "**负债还清,杠杆消除**"
                    if run_cash - cash > 1:
                        after += f",累计现金 {money(run_cash - cash)}"
                else:
                    after = f"担保比例 {ratio_at(run_mv + run_cash, run_debt):.0f}%"
                    dd = drop_to_line(run_mv, run_cash, run_debt, lines["平仓线_pct"])
                    if dd:
                        after += f",离平仓线 {abs(dd):.0f}%"
                if sh < p["shares"] and partial is None:
                    partial = (steps, p["name"], why_amt, sh, amt)
        elif v:
            e = ev_of(v)
            if e is not None and (keep_first is None or e > keep_first[2]):
                keep_first = (p["name"], act, e, p["code"] in debt_of,
                              p["market_value"])
        L.append(f"| {steps if (item and item['量'] > 0) else '—'} "
                 f"| {p['name']}({p['code']}) "
                 f"| **{act}** | {sc} | {ol} | {money(p['market_value'])} "
                 + (f"| {money(d, 0)} " if d else "| 无 ")
                 + f"| {p['pnl_pct']:+.2f}% | {need} | {why_amt} | {lots} | {after} |")
    L.append("")
    L.append("> 「按规则要卖」是三块算出来的目标金额;「实际报单」是**向上取整到整手**"
             "之后的股数和它对应的金额。**卖完之后那一列、以及下面的合计,"
             "全部按实际报单的金额算** —— 两列各说各的话,读者不知道担保比例"
             "是按哪个数来的。向上取整的方向是刻意的:少卖一手就还在上限之上,"
             "多卖一手只是多变现几百块,两个方向的代价不对称。")
    L.append("")
    _fees(sold_rows, L)
    sold = mv - run_mv
    if run_debt <= 0:
        free_cash = run_cash - cash
        L.append(f"> 上表假设**每一步卖出的钱先拿去还债,还清之后留成现金**。"
                 f"走完这 {steps} 步共卖出 {money(sold)},"
                 f"负债从 {money(debt)} 降到 0,"
                 f"**杠杆消除**(不是变成某个更小的倍数 —— 没有负债就没有杠杆)。"
                 f"剩下持仓 {money(run_mv)}"
                 + (f",另有 {money(free_cash)} 现金" if free_cash > 1 else "")
                 + f",全部是自有资金。"
                 f"**你的净资产全程不变**({money(equity)} 元)—— "
                 f"卖出换现金再还债只改变杠杆,不改变你的钱。")
        if free_cash > 1:
            L.append("")
            L.append(f"> **那 {money(free_cash)} 现金不用急着买回去。** "
                     f"按第 11 节那套规则,加仓要同时满足三条:期望值 > +20%、"
                     f"确信度比例 ≥ 2/3、而且不超仓位上限 —— **五只里现在没有一只满足**。"
                     f"现金留着不是浪费:它是下一次「有票同时满足这三条」时的子弹,"
                     f"而在信用账户里它还顺带把可用保证金从负数拉回来。")
    else:
        new_lev = run_mv / equity if equity else None
        dd = drop_to_line(run_mv, run_cash, run_debt, lines["平仓线_pct"])
        keep_txt = "、".join(
            f"{p['name']}(建议「{views[p['code']]['建议']['动作']}」)"
            for p in kept if p["code"] in views) or "剩下几只"
        L.append(f"> 上表假设**每一步卖出的钱全部拿去还债**。走完这 {steps} 步共卖出 "
                 f"{money(sold)},负债从 {money(debt)} 降到 **{money(run_debt)}**,"
                 f"杠杆从 {mv / equity:.2f}x 降到 **{new_lev:.2f}x**"
                 + (f",离平仓线从 "
                    f"{abs(drop_to_line(mv, cash, debt, lines['平仓线_pct'])):.0f}% "
                    f"拉开到 **{abs(dd):.0f}%**" if dd else "")
                 + f"。**你的净资产全程不变**({money(equity)} 元)。")
        L.append("")
        L.append(f"> **债没有清零,还差 {money(run_debt)}。** 这套顺序到这里停,"
                 f"是因为剩下的 {keep_txt} —— 它们不在减仓名单里。"
                 f"继续降杠杆就得卖掉它们,那等于**违背它们自己的建议**,"
                 f"是另一个决定,依据也不同:"
                 f"卖它们的理由只能是「杠杆本身让你睡不着」,不是「这几家公司变差了」。"
                 f"这两条理由都成立,但要分开想。")
    L.append("")
    if partial:
        L.append(f"注意第 {partial[0]} 步:**不用把{partial[1]}全卖掉** —— "
                 f"卖 {partial[3]:,} 股({money(partial[4])})就够了,依据是"
                 f"「{partial[2]}」。再多卖的部分既不降杠杆也不降超限,"
                 f"只是把股票换成现金 —— 那是另一个决定。")
        L.append("")
    L.append("**不用一次做完。** 上表是顺序不是时间表:可以先做第 1 步看两周,"
             "再决定要不要往下走。**但顺序别倒过来**"
             + ((f" —— 先卖{keep_first[0]}没有依据:它不超限,期望值 "
                 f"{keep_first[2]:+.1f}% 落在中性带里(建议「{keep_first[1]}」),"
                 f"上面三条规则一条都没指向它。"
                 + ("" if not keep_first[3] else
                    "而且它背着自己的融资负债 —— 卖它拿回的钱要先还它自己的债,"
                    "能腾出来降杠杆的现金反而比卖没负债的那几只少。")
                 + (f"它市值 {money(keep_first[4])} 也是几只里最小的,"
                    f"先动它降杠杆的效率最低。" if keep_first[4] == min(
                        x["market_value"] for x in ps) else ""))
                if keep_first else
                "。表里每一只现在都是建议减的,所以按盈亏和负债结构排的这个顺序"
                "就是降杠杆效率最高的那个。"))
    L.append("")
