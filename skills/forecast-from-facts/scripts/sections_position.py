#!/usr/bin/env python3
"""报告里和**持仓**有关的几节:持仓状况 / 筹码与杠杆 / 技术位 / 海外同业 / 操作判断。

和 report.py 分开的理由:那边是「这家公司值多少钱」,这边是「你手上这笔怎么办」。
两件事的数据来源、失效方式、可信度都不一样,混在一个文件里改一个会碰到另一个。

⚠ **持仓成本和价格必须是同一种价格。** 成本是未复权的买入价、日线是前复权的话,
跨过送转就完全对不上 —— 差出来的百分比看着像盈亏,其实是股本变化。
holdings.json 里存的成本已经和前复权日线是同一种价格了,改之前先确认这一点。
"""
from __future__ import annotations

import glob
import json
import statistics
from pathlib import Path

K_FIELDS = ["date", "open", "high", "low", "close", "volume",
            "amount", "peTTM", "pbMRQ", "psTTM", "turn"]


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


def pick(raw: Path, pat: str, key_part: str = ""):
    """取某个文件里第一个成功的信封。key_part 给了就只认来源名含它的那条。"""
    for k, v in envs(newest(raw, pat)).items():
        if v.get("ok") and v.get("data") and (not key_part or key_part in k):
            return v
    return None


def yi(v, nd=1):
    return "—" if v is None else f"{v / 1e8:,.{nd}f}"


def load_position(fp: Path | None, code: str) -> dict:
    """读这只票的持仓。两种文件都认:

    · `account.json`(信用账户全量:股数 / 市值 / 融资负债 / 担保比例)—— **优先**
    · `holdings.json`(只有成本)—— 退而求其次

    优先用 account.json 的理由:只有成本的话,报告只能说「亏了 19%」,
    说不出「这只占你 32% 仓位、而且背着 26.5 万融资负债」——
    后者才是决定该怎么办的信息。
    """
    if not fp or not fp.exists():
        return {}
    try:
        j = json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # account.json
    if "positions" in j:
        pos = next((x for x in j["positions"] if x.get("code") == code), None)
        if not pos:
            return {}
        total_mv = sum(x.get("market_value") or 0 for x in j["positions"])
        debt = next((d["当前负债总额"] for d in (j.get("margin_debt") or [])
                     if d.get("code") == code), None)
        return {"cost": pos.get("cost"), "shares": pos.get("shares"),
                "market_value": pos.get("market_value"), "pnl": pos.get("pnl"),
                "weight": (pos["market_value"] / total_mv * 100
                           if total_mv and pos.get("market_value") else None),
                "margin_debt": debt,
                "account_type": j.get("_账户类型"),
                "维持担保比例": (j.get("account") or {}).get("维持担保比例_pct")}
    # holdings.json
    h = (j.get("holdings") or {}).get(code) or {}
    return {"cost": h.get("cost"), "shares": h.get("shares")}


def load_cost(fp: Path | None, code: str):
    return load_position(fp, code).get("cost")


def daily(raw: Path, code: str):
    v = pick(raw, f"quotes/{code}/*-qfq.json")
    if not v:
        return []
    out = []
    for row in v["data"]:
        r = dict(zip(K_FIELDS, row))
        try:
            out.append({"date": r["date"], "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"])})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["date"])


# ── 0 持仓状况 ────────────────────────────────────────────────────────
def sec_position(raw: Path, code: str, pos: dict, spot, L) -> None:
    L.append("## 0 你手上这笔")
    L.append("")
    cost = pos.get("cost")
    if cost is None:
        L.append(f"持仓文件里没有 {code} 的成本 —— 补上之后这一节会显示浮盈亏、"
                 f"仓位占比和融资负债。**没有持仓信息就没法谈该怎么办**,"
                 f"下面的分析只回答「这家公司值多少钱」,不回答「你该拿还是该减」。")
        L.append("")
        return
    px = spot["close"]
    pl = (px / cost - 1) * 100
    L.append("| 项 | 值 |")
    L.append("|---|---:|")
    if pos.get("shares"):
        L.append(f"| 持股 | {pos['shares']:,} 股 |")
    L.append(f"| 成本 | {cost:,.3f} |")
    L.append(f"| 现价({spot['date']}) | {px:,.2f} |")
    if pos.get("market_value"):
        L.append(f"| 市值 | {pos['market_value']:,.0f} |")
    L.append(f"| **浮盈亏** | **{pl:+.1f}%**"
             + (f"({pos['pnl']:+,.0f} 元)" if pos.get("pnl") else "") + " |")
    if pl < 0:
        L.append(f"| 回本需涨 | **{(cost / px - 1) * 100:+.1f}%** |")
    if pos.get("weight"):
        L.append(f"| 占组合 | **{pos['weight']:.1f}%** |")
    if pos.get("margin_debt"):
        L.append(f"| **融资负债** | **{pos['margin_debt']:,.2f}** "
                 f"(占该股市值 {pos['margin_debt'] / pos['market_value'] * 100:.0f}%) |")
    L.append("")
    if pos.get("margin_debt"):
        L.append(f"> 🔴 **这只是用融资买的。** 亏损被杠杆放大,而且有一条"
                 f"**你不能选择**的线 —— 维持担保比例到了就强制平仓,"
                 f"卖在什么价不由你决定。账户整体现在是 "
                 f"{pos.get('维持担保比例', '?')}%。"
                 f"**账户层面的分析在同目录《融资融券账户 · 操盘文档》**,"
                 f"那份比这份更该先看:公司再好,扛不到那天也没用。")
        L.append("")
    elif pos.get("account_type"):
        L.append(f"> 这只**没有融资负债**,是纯自有资金持有。"
                 f"账户整体是{pos['account_type']},维持担保比例 "
                 f"{pos.get('维持担保比例', '?')}% —— "
                 f"意味着这只在需要降杠杆时是可以先动的那类。")
        L.append("")
    L.append("> 成本和现价用的是**同一个价格基准**(前复权)。"
             "如果拿未复权的买入价去比前复权的现价,跨过送转会差出一大截 —— "
             "那个差是股本变化,不是盈亏。")
    L.append("")


# ── 成本在历史里的位置 ────────────────────────────────────────────────
def cost_percentile(px: list, cost) -> str | None:
    if not px or cost is None:
        return None
    above = [d for d in px if d["close"] > cost]
    n = len(px)
    if not above:
        return (f"过去 {n} 个交易日里,收盘价**没有一天**高于你的成本 {cost:,.3f} —— "
                f"这个成本在整段历史之上,不是买贵一点的问题。")
    first, last = above[0]["date"], above[-1]["date"]
    return (f"过去 {n} 个交易日里,收盘价高于你的成本 {cost:,.3f} 的有 "
            f"**{len(above)} 天({len(above) / n * 100:.0f}%)**,"
            f"首次 {first},最后一次 {last}。"
            + ("**成本落在一个很窄的高位价格带上** —— 说明买点接近阶段顶部,"
               "不是公司选错,是价格当时不好。"
               if len(above) / n < 0.15 else ""))


# ── 筹码与杠杆 ────────────────────────────────────────────────────────
def sec_chips(raw: Path, code: str, n: int, L) -> None:
    L.append(f"## {n} 筹码与杠杆")
    L.append("")
    wrote = False

    v = pick(raw, f"chips/{code}/*-股东户数.json", "HOLDERNUMLATEST")
    if v:
        r = v["data"][0]
        cur, pre = r.get("HOLDER_NUM"), r.get("PRE_HOLDER_NUM")
        ratio = r.get("HOLDER_NUM_RATIO")
        end = str(r.get("END_DATE") or "")[:10]
        L.append(f"**股东户数** {cur:,} 户(截至 {end})")
        L.append("")
        L.append("| 项 | 值 |")
        L.append("|---|---:|")
        L.append(f"| 上期 | {pre:,} |")
        L.append(f"| 增减 | **{cur - pre:+,} 户** |")
        if ratio is not None:
            L.append(f"| 变化 | **{ratio:+.1f}%** |")
        L.append("")
        if ratio is not None and ratio > 20:
            L.append(f"> 🔴 **户数增加 {ratio:.0f}% = 筹码分散。** "
                     "同样的股份从少数账户散到大量新账户,"
                     "之后反弹会遇到更重的解套抛压。"
                     "这一条要和股价位置一起看:高位分散是派发,低位分散是恐慌交换。")
            L.append("")
        elif ratio is not None and ratio < -10:
            L.append(f"> 户数减少 {abs(ratio):.0f}% = 筹码集中,通常是好事,"
                     "但要确认不是因为股价跌到没人愿意接。")
            L.append("")
        wrote = True

    v = pick(raw, f"chips/{code}/*-资金流.json", "RPT_DMSK_TS")
    if v:
        r = v["data"][0]
        d = str(r.get("TRADE_DATE") or "")[:10]
        sup = (r.get("SUPERDEAL_INFLOW") or 0) - (r.get("SUPERDEAL_OUTFLOW") or 0)
        L.append(f"**资金流({d})**:超大单净额 **{yi(sup, 2)} 亿**"
                 f",收盘 {r.get('CLOSE_PRICE')}")
        L.append("")
        L.append("> 单日资金流噪声很大,**一天的数字说明不了什么** —— "
                 "它只在连续多日同向、且和股价方向背离时才值得看。")
        L.append("")
        wrote = True

    v = pick(raw, f"chips/{code}/*-前十大流通股东.json")
    if v:
        rows = v["data"]
        dt = rows[0].get("更新日期", "")
        L.append(f"**前十大流通股东**({dt})")
        L.append("")
        L.append("| 股东 | 持股比例 | 变动 | 变动率 |")
        L.append("|---|---:|---:|---:|")
        for r in rows[:10]:
            L.append(f"| {r.get('股东名称')} | {r.get('持股比例')} "
                     f"| {r.get('增减')} | {r.get('变动率')} |")
        L.append("")
        L.append(f"> 这是**上一个披露日**({dt})的快照,不是现在。"
                 "季报之间发生的变动看不到 —— 前十大股东是季度披露,不是实时数据。")
        L.append("")
        wrote = True

    v = pick(raw, f"chips/{code}/*-融资融券.json")
    if v and v["data"]:
        L.append(f"**融资融券**:{len(v['data'])} 条记录,见 "
                 f"`data/raw/chips/{code}/`")
        L.append("")
        wrote = True
    else:
        L.append("**融资融券**:本轮没抓到(深交所接口按日提供,当日数据可能还没发布)。")
        L.append("")

    if not wrote:
        L.append("筹码数据本轮一条都没抓到。重跑:"
                 f"`tools/fetch_all.py --codes {code} --group chips`")
        L.append("")


# ── 限售解禁 + 技术位 ─────────────────────────────────────────────────
def sec_technical(raw: Path, code: str, cost, spot, n: int, L) -> None:
    L.append(f"## {n} 限售解禁与价格位置")
    L.append("")
    v = pick(raw, f"chips/{code}/*-解禁.json")
    if v and v["data"]:
        L.append(f"**未来有 {len(v['data'])} 笔限售解禁**,明细见 "
                 f"`data/raw/chips/{code}/`。解禁本身不必然砸盘,"
                 "但它是一个**时间确定**的供给增量,值得标在日历上。")
    else:
        L.append("**未来一段时间无限售解禁**(接口返回 0 条)。")
    L.append("")

    px = daily(raw, code)
    if not px:
        return
    closes = [d["close"] for d in px]
    cur = closes[-1]

    def ma(k):
        return statistics.mean(closes[-k:]) if len(closes) >= k else None

    L.append("**均线与区间**(前复权日线,最后一根 " + px[-1]["date"] + ")")
    L.append("")
    L.append("| 项 | 值 | 现价相对位置 |")
    L.append("|---|---:|---|")
    L.append(f"| 现价 | {cur:,.2f} | — |")
    for k in (20, 60, 120):
        m = ma(k)
        if m:
            L.append(f"| MA{k} | {m:,.1f} | 现价在其**{'上方' if cur > m else '下方'}** |")
    hi = max(px, key=lambda d: d["high"])
    lo = min(px[-250:] if len(px) >= 250 else px, key=lambda d: d["low"])
    L.append(f"| 区间最高 | {hi['high']:,.2f}({hi['date']}) "
             f"| 距最高 **{(cur / hi['high'] - 1) * 100:+.1f}%** |")
    L.append(f"| 近一年最低 | {lo['low']:,.2f}({lo['date']}) "
             f"| 距最低 **{(cur / lo['low'] - 1) * 100:+.1f}%** |")
    L.append("")
    cp = cost_percentile(px, cost)
    if cp:
        L.append("**你的成本在历史里的位置**:" + cp)
        L.append("")
    L.append("> 均线和区间是**价格自己的统计**,不含任何基本面信息。"
             "它回答的是「现在的价格相对过去在哪」,不回答「该不该买」——"
             "后者要看前面几节的事实和估值。")
    L.append("")


# ── 海外同业 ──────────────────────────────────────────────────────────
def sec_peers(raw: Path, group: str | None, n: int, L) -> None:
    """用**季度财报事实**做同业对照,不用分析师目标价。

    没有对应同业组时**也要出这一节并说明原因** —— 直接跳过会让编号断掉,
    读者只会以为报告缺页,不会知道是「这只票我们没有同业数据」。
    """
    if not group:
        L.append(f"## {n} 海外同业对照")
        L.append("")
        L.append("**没有可比的海外同业。** 我们目前只维护了两组:AI 算力链同业"
                 "(COHR / LITE / FN / CRDO / MRVL / AVGO / ANET / CIEN)和油气设服"
                 "(SLB / HAL / BKR / XOM / CVX)。这只票不属于其中任何一组,"
                 "拿不相干的公司做对照只会得出没有含义的数字。")
        L.append("")
        return
    files = sorted((raw / "overseas").glob("*/*-季度财报.json"))
    if not files:
        L.append(f"## {n} 海外同业对照")
        L.append("")
        L.append("本轮没抓到同业季度财报。重跑:"
                 "`tools/fetch_all.py --peers`")
        L.append("")
        return
    L.append(f"## {n} 海外同业对照(用它们自己的财报,不用分析师目标价)")
    L.append("")
    L.append("这条链的需求端在海外。只看 A 股会漏掉「海外同行在扩张而 A 股在收缩」"
             "这类背离 —— 那通常说明问题出在公司自己,不是行业。")
    L.append("")
    L.append("| 标的 | 最近一季营收(亿美元) | 上年同期 | 同比 | 资本开支(亿美元) |")
    L.append("|---|---:|---:|---:|---:|")
    got = 0
    for f in files:
        tk = f.parent.name
        for v in envs(f).values():
            if not v.get("ok") or v.get("params", {}).get("sector_group") != group:
                continue
            ic = (v["data"].get("季度利润表") or {}).get("Total Revenue") or {}
            cf = (v["data"].get("季度现金流") or {}).get("Capital Expenditure") or {}
            if not ic:
                continue
            ds = sorted(ic)
            last = ds[-1]
            yoy_key = next((d for d in ds if d[:4] == str(int(last[:4]) - 1)
                            and d[5:7] == last[5:7]), None)
            rev, prev = ic[last], ic.get(yoy_key) if yoy_key else None
            cap = abs(cf.get(last)) if cf.get(last) else None
            L.append(f"| {tk} | {rev / 1e8:,.0f}({last}) "
                     + (f"| {prev / 1e8:,.0f} | **{(rev / prev - 1) * 100:+.1f}%** "
                        if prev else "| — | — ")
                     + (f"| {cap / 1e8:,.1f} |" if cap else "| — |"))
            got += 1
    L.append("")
    if not got:
        L.append("这一组本轮一条都没抓到。重跑:`tools/fetch_all.py --peers`")
        L.append("")
        return
    L.append("> 资本开支这一列对元器件厂参考价值有限(它们是重研发轻资产),"
             "看营收同比就够了。全部来自各家 10-Q/10-K 的申报数字(yfinance 转录),"
             "**不是分析师预测**。同比对不上的是因为那家公司这一季的上年同期"
             "在 yfinance 的季度表里没有(它只保留最近几期)。")
    L.append("")


# ── 操作判断 ──────────────────────────────────────────────────────────
def sec_action(v, f, d, cost, spot, pick_mult, n: int, L) -> None:
    """继续持有的前提 / 减仓信号 / 补仓参考。

    **每一条都必须是这只票自己算出来的数字,不是通用规则。**
    「毛利率不能掉太多」是废话;「不低于 46.9%(近四季最低)」才能被核对。
    写不出数字的条目就不要写 —— 那说明我们其实没有依据。
    """
    qs = d["quarters"]
    L.append(f"## {n} 操作判断")
    L.append("")
    L.append("**下面是从前面几节的数字机械推出来的条件,不是投资建议。"
             "最终决策和后果都是你的。**")
    L.append("")

    gm = [r["单季毛利率"] for r in qs if r["单季毛利率"] is not None][-4:]
    rev = [r["单季营收"] for r in qs if r.get("单季营收")][-4:]
    inv = [r["期末存货"] for r in qs if r.get("期末存货")][-2:]
    pre = [r["期末预付款"] for r in qs if r.get("期末预付款") is not None][-2:]
    p = f.get("归母净利区间")
    rv = f.get("营收区间")
    tgt = f["预测期"]

    L.append(f"### 继续持有的前提({tgt} 季报出来后逐条核)")
    L.append("")
    if rv:
        L.append(f"- **单季营收落在 {yi(rv['low'])} ~ {yi(rv['high'])} 亿之内** —— "
                 f"这是我们第 3 节的预测区间。落在区间外说明推法错了,要先改方法再谈估值")
    if p:
        L.append(f"- **单季归母净利落在 {yi(p['low'])} ~ {yi(p['high'])} 亿之内**")
    if gm:
        L.append(f"- **毛利率不低于 {min(gm):.1f}%**(近四季最低)—— "
                 f"跌破说明要么在打价格战,要么产品结构变差")
    if len(inv) == 2 and inv[0]:
        L.append(f"- **期末存货不明显掉头**(上季 {yi(inv[-2])} → 本季 {yi(inv[-1])} 亿)—— "
                 f"存货是下一两季出货的物质基础,它先转向,营收随后")
    if len(pre) == 2 and pre[-1] and pre[-1] > 1e8:
        L.append(f"- **预付款维持在 {yi(pre[-1], 2)} 亿附近或更高** —— "
                 f"预付款是抢上游产能的钱,大幅回落说明公司自己觉得后面不需要那么多料")
    cp = f.get("产业链位置")
    if cp:
        L.append(f"- **相对{cp['上游']}的增速倍数不继续下滑**"
                 f"(当前 {cp['终点']:.2f}x)—— 掉到 0.85x 以下就是明确在丢份额")
    L.append("")

    L.append("### 减仓信号(任一触发就重新评估)")
    L.append("")
    if rv:
        L.append(f"- **单季营收跌破 {yi(rv['low'])} 亿** —— 低于我们最谨慎的推法")
    if gm and len(gm) >= 2:
        L.append(f"- **毛利率跌破 {min(gm) - 3:.1f}%**(近四季最低再掉 3 个百分点)")
    b = v["前瞻PE带"]
    our = v["我们预测的未来四季净利"]
    if b.get("ok") and our and spot:
        cap = spot["close"] * v["总股本"]
        pe = cap / our["mid"] if our["mid"] else None
        if pe:
            L.append(f"- **现价前瞻 PE 已经是 {pe:.1f}x,而自己历史 p90 是 {b['p90']:.1f}x** —— "
                     + ("**已经在 p90 之上**。这时候增长只要减速,估值和利润会一起往下,"
                        "两头挤。" if pe > b["p90"] else
                        f"再涨 {(b['p90'] / pe - 1) * 100:.0f}% 就到 p90。")
                     + "判断依据是**增长有没有减速**,不是价格本身")
    if cp and cp["终点"] < 1.0:
        L.append(f"- **增速已经慢于上游开支({cp['终点']:.2f}x < 1)** —— "
                 f"行业在扩,这家公司没跟上,份额在被别人拿走")
    L.append("- **股东户数继续大增,同时股价创新高** —— 还在派发,别追")
    L.append("")

    L.append("### 关于补仓")
    L.append("")
    if cost is None:
        L.append("没有成本数据,这一段给不出参考。补进 "
                 "`private/portfolio/holdings.json` 之后会有。")
    elif pick_mult and pick_mult.get("ok") and our and v.get("总股本"):
        low_tp = our["low"] * pick_mult["低"] / v["总股本"]
        L.append(f"- 我们**最谨慎那一档**的目标价是 **{low_tp:,.0f} 元**"
                 f"(净利下沿 × 倍数下沿)。现价 {spot['close']:,.2f} "
                 + ("**已经低于它**,从估值角度是有安全边际的"
                    if spot["close"] < low_tp else
                    f"比它高 {(spot['close'] / low_tp - 1) * 100:.0f}%,"
                    f"现在补仓等于赌乐观档兑现"))
        L.append(f"- 你的成本 {cost:,.3f}。在当前价补仓,"
                 f"平均成本会被拉到 {(cost + spot['close']) / 2:,.2f}(等额补一次)")
        L.append("- **先看第 7 节「你的成本在历史里的位置」。** 如果成本落在高位窄带,"
                 "在当前价补仓会把平均成本拖到一个更难受的地方;"
                 "更该等的是前一个明确低点附近,而且分批不要一次打满")
    L.append("")
    L.append("> 这一节的每个数字都能在前面几节找到出处。"
             "**它不预测股价** —— 它只把「什么情况下我们的判断就错了」写成可以核对的条件。")
    L.append("")
