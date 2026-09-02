#!/usr/bin/env python3
"""从事实推下一季 —— 多个互相独立的估计法,每个自己判断适不适用。

    forecast.py <code> [--raw data/raw] [--target 2026Q3] [--log predictions/]

为什么要好几个方法而不是一个:
    单一方法在**它不适用的票上不会报错,只会给出一个看着正常的错数**。
    存货法在中际旭创上历史离散度 20.6%(能用),在杰瑞股份上 53.8%(不能用)——
    杰瑞 Q4 是交付旺季,期末存货和下季营收的关系被季节性彻底压过去。
    如果只写一个方法,杰瑞那个数照样会打印出来,而且没有任何提示。

    所以每个方法**先自检适用性,不适用就退出并说明理由**,
    最后把通过自检的那几个**取并集**。剩几个方法本身就是信息:
    只剩一个 = 这只票的下季营收我们其实没什么把握。

为什么是并集不是交集(2026-09-02 改,依据是走前回测):
    五只 × 十个季度的走前回测里,原来那套「先取交集、交集为空才退并集」命中
    16/46 = 35%;一律取并集命中 28/46 = 61%。原因是三条方法给的区间是
    **几个历史比值的最大最小值围出来的线段**,不是概率区间 ——
    两条线段相交得到的是「两边都同意的那一小段」,和「真实值落在里面的概率」
    没有关系。宽度不到 10% 的 17 个交集只命中 1 个(6%)。
    **窄不等于有把握,只等于更容易错。** 并集的代价是宽(中位宽度 45%),
    信息量确实少,但它不骗人。

三个方法互相独立在哪:
    存货法看**供给侧已经花出去的钱**;同比法看**需求侧的增长趋势**;
    环比法看**这门生意本身的季节形状**。三者共用的只有营收这一个观测量,
    出错的原因互不相同。⚠ 但这个独立性**只在平稳期成立** ——
    三条都是外推,增速拐头那一季会一起慢半拍,而且是同方向错。

依赖:无。读 quarterly.py 的输出。
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
import sys
from pathlib import Path

# tools/ 下是软链,__file__ 指向真实目录,但 sys.path[0] 是软链所在目录 ——
# 于是 import quarterly 找不到。**把脚本真实所在目录加进 sys.path**,
# 这样 `tools/report.py` 和 `skills/.../report.py` 两条路径都能直接跑,
# 不需要调用方设 PYTHONPATH(别人 clone 下来第一件事就会卡在这)。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quarterly

DISPERSION_MAX = 30.0        # 历史比值离散度超过这个数,该方法判定不适用
Q_ORDER = ["Q1", "Q2", "Q3", "Q4"]


def next_period(p: str) -> str:
    y, q = int(p[:4]), p[4:]
    i = Q_ORDER.index(q)
    return f"{y}{Q_ORDER[i + 1]}" if i < 3 else f"{y + 1}Q1"


# 季度序号和连续性判断住在 quarterly.py —— 三处共用(产业链四个点、
# 3+1 PE/PS 带的四季窗口、存货法的近四季比值)。各写一份必然漂,而漂了不报错。
_qidx = quarterly.qidx


def prev_period(p: str) -> str:
    y, q = int(p[:4]), p[4:]
    i = Q_ORDER.index(q)
    return f"{y}{Q_ORDER[i - 1]}" if i > 0 else f"{y - 1}Q4"


def spread(vals) -> float:
    """离散度 = 极差 ÷ 中位。用中位不用均值 —— 一个离群值就能把均值拖走。"""
    m = statistics.median(vals)
    return (max(vals) - min(vals)) / abs(m) * 100 if m else 999.0


def series(qs, key):
    return {r["period"]: r[key] for r in qs if r.get(key) is not None}


def edge_note(val: float, limit: float, unit: str) -> str | None:
    """判定值离阈值还有多远。近到阈值的 10% 以内就标出来。**只加文案,不动阈值。**

    为什么要标:阈值两边是两个相反的结论(这个方法「能用」还是「不能用」),
    可是 28% 和 32% 之间只差 4 个点,下一季就可能反过来。不标的话,
    报告里的「✓ 本次适用」看起来像一个稳定的事实,其实是踩在线上 ——
    下季度重跑时它突然翻面,人会以为是公司变了,其实只是噪声过了线。

    为什么不干脆把阈值改成软的(比如按离散度连续打折):那会让「能不能用」
    变成一个可以调的旋钮,而这套东西的价值恰恰在于**判断规则固定、五只同一把尺**。
    宁可保留硬阈值,再把「这次踩在线上」如实说出来。
    """
    gap = abs(val - limit)
    if gap > abs(limit) * 0.10:
        return None
    return (f"(判定值 {val:.1f}{unit},离阈值 {limit:g}{unit} 只差 {gap:.1f},"
            f"**属于边界值,下一季很可能翻转**)")


# ── 方法 A:存货法(供给侧)────────────────────────────────────────────
def m_inventory(qs, target):
    """期末存货 × 历史「下季营收÷期末存货」区间。

    第一性原理:制造业的营收要有货可交。期末存货是下一季出货的物质基础,
    而且这笔钱**已经花出去了** —— 是行为不是说法,管理层怎么讲都改不了这个数。
    失效条件:周转天数变了(扩产爬坡、备货策略变)或季节性交付。
    """
    last = prev_period(target)
    inv = next((r["期末存货"] for r in qs if r["period"] == last), None)
    have = [r for r in qs if r["下季营收÷期末存货"]][-4:]
    ratios = [r["下季营收÷期末存货"] for r in have]
    if inv is None:
        return {"name": "存货法", "ok": False, "why": f"{last} 没有期末存货数据"}
    if len(ratios) < 3:
        return {"name": "存货法", "ok": False, "why": f"历史比值只有 {len(ratios)} 个样本,不够"}
    # ⚠ 上面是 `[-4:]`,取的是「最后四个**算得出来**的比值」,不是「最近四个季度」。
    #   中间缺一季时它会悄悄往回够,四个点横跨五个季度 —— 离散度照样算得出来,
    #   报告一个字都不会提。所以这里先判连续,不连续就退出并说明缺在哪。
    gap = quarterly.gap_in([r["period"] for r in have])
    if gap:
        return {"name": "存货法", "ok": False,
                "why": f"近四季比值取不到连续的四个季度:{gap} —— "
                       f"隔着缺口的四个比值算出来的离散度不代表「近四季」,"
                       f"拿它判适用性等于用一个跨了五季的窗口冒充四季",
                "细节": {"取到的季度": [r["period"] for r in have]}}
    d = spread(ratios)
    edge = edge_note(d, DISPERSION_MAX, "%")
    if d > DISPERSION_MAX:
        return {"name": "存货法", "ok": False, "边界": bool(edge),
                "why": f"近四季比值离散度 {d:.1f}% > {DISPERSION_MAX:.0f}%,"
                       f"这条关系在这只票上被别的因素压过去了(多半是季节性交付)"
                       + (edge or ""),
                "细节": {"比值": [round(x, 3) for x in ratios]}}
    return {"name": "存货法", "ok": True, "边界": bool(edge),
            "low": inv * min(ratios), "high": inv * max(ratios),
            "mid": inv * statistics.median(ratios),
            "依据": f"{last} 期末存货 {inv/1e8:.1f} 亿 × 近四季比值 "
                    f"{min(ratios):.2f}~{max(ratios):.2f}x(离散度 {d:.1f}%)"
                    + (edge or ""),
            "失效条件": "周转天数变化、扩产爬坡改变备货节奏、交付集中到某一季"}


# ── 方法 B:同比法(需求侧)────────────────────────────────────────────
def m_yoy(qs, target):
    """去年同期 × 近期同比增速区间。

    第一性原理:同比自动消掉季节性 —— 拿 Q3 比 Q3,不受"Q4 是旺季"影响。
    失效条件:增速本身在快速拐弯时,用过去几个季度的增速外推会系统性偏。
    """
    rev = series(qs, "单季营收")
    yoy = series(qs, "单季营收同比")
    base = rev.get(f"{int(target[:4]) - 1}{target[4:]}")
    recent = [v for k, v in sorted(yoy.items())][-3:]
    if base is None:
        return {"name": "同比法", "ok": False, "why": f"缺去年同期({int(target[:4])-1}{target[4:]})营收"}
    if len(recent) < 3:
        return {"name": "同比法", "ok": False, "why": "同比样本不足 3 个"}
    lo, hi = min(recent), max(recent)
    edge = edge_note(hi - lo, 100.0, " 个百分点")
    if hi - lo > 100:
        return {"name": "同比法", "ok": False, "边界": bool(edge),
                "why": f"近三季同比在 {lo:.0f}%~{hi:.0f}% 之间摆动,跨度超过 100 个百分点,"
                       f"外推没有意义" + (edge or ""),
                "细节": {"近三季同比": [round(x, 1) for x in recent]}}
    return {"name": "同比法", "ok": True, "边界": bool(edge),
            "low": base * (1 + lo / 100), "high": base * (1 + hi / 100),
            "mid": base * (1 + statistics.median(recent) / 100),
            "依据": f"去年同期 {base/1e8:.1f} 亿 × 近三季同比区间 {lo:.1f}%~{hi:.1f}%"
                    + (edge or ""),
            "失效条件": "增速正在拐弯(加速或失速),用过去的增速外推会系统性偏"}


# ── 方法 C:环比法(季节形状)──────────────────────────────────────────
def m_qoq(qs, target):
    """上季营收 × 历史上「同一个季度转换」的环比。

    第一性原理:很多生意的季节形状是稳定的(Q4 交付旺季、Q1 春节淡季)。
    只取**同一个季度转换**(如历年的 Q2→Q3),不混用不同转换 ——
    混用等于把季节性重新搅回去。
    失效条件:业务结构变了(新产品线、新客户改变了交付节奏)。
    """
    rev = series(qs, "单季营收")
    last = prev_period(target)
    if last not in rev:
        return {"name": "环比法", "ok": False, "why": f"缺上季({last})营收"}
    hist = []
    for p in sorted(rev):
        if p[4:] == target[4:]:                      # 同一个季度
            pp = prev_period(p)
            if pp in rev and rev[pp]:
                hist.append(rev[p] / rev[pp])
    if len(hist) < 2:
        return {"name": "环比法", "ok": False,
                "why": f"历史上「→{target[4:]}」的环比只有 {len(hist)} 个样本,不够"}
    d = spread(hist)
    limit = DISPERSION_MAX * 1.5                     # 环比天然比存货法散,放宽到 45%
    edge = edge_note(d, limit, "%")
    if d > limit:
        return {"name": "环比法", "ok": False, "边界": bool(edge),
                "why": f"历年「→{target[4:]}」环比离散度 {d:.1f}%,季节形状不稳定"
                       + (edge or ""),
                "细节": {"历年环比": [round(x, 3) for x in hist]}}
    return {"name": "环比法", "ok": True, "边界": bool(edge),
            "low": rev[last] * min(hist), "high": rev[last] * max(hist),
            "mid": rev[last] * statistics.median(hist),
            "依据": f"{last} 营收 {rev[last]/1e8:.1f} 亿 × 历年「→{target[4:]}」环比 "
                    f"{min(hist):.2f}~{max(hist):.2f}x({len(hist)} 个样本)"
                    + (edge or ""),
            "失效条件": "业务结构变化改变了交付节奏"}


# ── 产业链位置:公司增速 ÷ 上游开支增速 ────────────────────────────────
# ★ 这**不是预测方法**。2026-09-02 实测,这个倍数在三只票上离散度 70%~120%,
#   拿它推下季营收会给出一个看着正常的错数。
#   但它的**趋势**是硬信息:倍数持续往上 = 在产业链里得份额,往下 = 丢份额。
#   第一性原理:上游的资本开支是这条链的总蛋糕(云厂之于光模块、三桶油之于油服设备、
#   面板厂与封测厂之于它们的设备商),公司增速相对它的比值就是分到的份额变化。
CAP_Q = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}

# ★ 每条链拿谁的资本开支当分母。**拿错分母比不算更糟**:杰瑞股份(油服设备)
#   对着云厂资本开支算出来的倍数是 0.04x —— 数字有,含义没有,还看起来像个结论。
#
# `kind` 决定数据从哪来,两条路径的成员写法不同:
#   · sec    —— 美股 ticker,数据来自 SEC XBRL 申报原值(overseas_facts/*.json)
#   · ashare —— A 股代码,数据来自各家自己的现金流量表,走 quarterly.py 还原单季
#
# ⚠ 2026-09-02 修正的两处分母:
#   ① 杰瑞股份原来挂 SEC 的 XOM/CVX/SLB/HAL/BKR。SLB/HAL/BKR 是油服公司,
#     **是杰瑞的同行不是它的客户**;而且三家 capex 合计一季约 10 亿美元,
#     那个分母其实 85% 是 XOM+CVX 两家。杰瑞真正的客户是三桶油和中东 NOC,
#     所以换成三桶油,数据也改从 A 股报表取。海外那五家仍留在第 8 节做**同业**对照 ——
#     同业和上游是两件事,不能互相顶替。
#   ② 深科达原来没登记,报告里写成「没有数据源」。它的下游(显示模组设备卖给
#     面板厂、半导体封测设备卖给封测厂)**全都是 A 股**,三大表走同一条接口就能拿。
UPSTREAM = {
    "cloud": {
        "kind": "sec",
        "members": ["MSFT", "AMZN", "GOOGL", "META"],
        "名称": "北美四大云厂资本开支",
    },
    "oil_cn": {
        "kind": "ashare",
        # 中国石油 / 中国石化 / 中国海油 —— 杰瑞的直接客户群
        "members": ["601857", "600028", "600938"],
        "名称": "三桶油资本开支",
    },
    "panel_osat": {
        "kind": "ashare",
        # 京东方 / TCL 科技 / 深天马 / 维信诺 · 长电科技 / 通富微电 / 华天科技
        # ⚠ 这七家是**按深科达主营(显示模组设备 + 半导体封测设备)推断的客户群**,
        #   不是从年报客户名单来的。它衡量的是「这两个下游行业在不在扩产能」,
        #   不等于深科达的实际订单来源。
        "members": ["000725", "000100", "000050", "002387",
                    "600584", "002156", "002185"],
        "名称": "国内面板与封测厂资本开支(7 家)",
    },
}

UPSTREAM_OF = {
    "300502": "cloud",       # 新易盛     光模块 → 云厂建数据中心
    "300308": "cloud",       # 中际旭创   光模块 → 同上
    "300476": "cloud",       # 胜宏科技   AI 服务器 PCB → 同上
    "002353": "oil_cn",      # 杰瑞股份   油服设备 → 三桶油的资本开支
    "688328": "panel_osat",  # 深科达     面板/封测设备 → 面板厂与封测厂的资本开支
}


def capex_yoy(raw: Path, group: str) -> dict:
    """某条链上游季度资本开支合计的同比。**只在成员全都有数的季度才算** ——
    少一家就少几百亿美元,同比会凭空多出几十个百分点,而且不报错。"""
    files = sorted((raw / "overseas_facts").glob(f"*-capex-{group}.json"))
    if not files:
        return {}
    env = json.loads(files[-1].read_text(encoding="utf-8"))
    if not env.get("ok"):
        return {}
    tot, who = {}, {}
    for tk, v in env["data"].items():
        for d, x in v["quarters"].items():
            tot[d] = tot.get(d, 0.0) + x
            who.setdefault(d, set()).add(tk)
    full = set(env["data"])
    out = {}
    for d in tot:
        y = f"{int(d[:4]) - 1}{d[4:]}"
        if y in tot and who.get(d) == full and who.get(y) == full and tot[y]:
            out[d] = (tot[d] / tot[y] - 1) * 100
    return out


def capex_yoy_ashare(raw: Path, codes: list[str]):
    """A 股上游的季度资本开支合计同比 → ({日历季度末: 同比%}, 缺了哪几只)。

    钱从哪来:各家自己现金流量表里的「购建固定资产、无形资产和其他长期资产
    所支付的现金」,quarterly.py 已经把累计数还原成单季(它的 `单季资本开支`
    字段;那个字段名 2026-09-02 之前少写了一个「所」,一直静默取不到值)。和云厂那条一样,是**已经花出去的钱**,不是谁的说法。

    和 capex_yoy 同一条规矩:**只在成员全都有数的季度才算同比**。
    少一家就少几十亿,同比会凭空多出几十个百分点,而且不报错 ——
    序列看着完全正常,只是每一格都偏。

    某只缺三大表时 quarterly.build 会 SystemExit(它是给命令行用的退出方式)。
    这里抓住它,**整组返回空**并记下缺哪几只 —— 不用剩下的凑合算,
    因为篮子少一家,前后两年就不是同一个篮子了。
    """
    tot, who, missing = {}, {}, []
    for c in codes:
        try:
            qs = quarterly.build(raw, c, since="2019")["quarters"]
        except SystemExit:
            missing.append(c)
            continue
        for r in qs:
            v = r.get("单季资本开支")
            if v is None:
                continue
            k = f"{r['period'][:4]}-{CAP_Q[r['period'][4:]]}"
            tot[k] = tot.get(k, 0.0) + v
            who.setdefault(k, set()).add(c)
    if missing:
        return {}, missing
    full = set(codes)
    out = {}
    for d in tot:
        y = f"{int(d[:4]) - 1}{d[4:]}"
        if y in tot and who.get(d) == full and who.get(y) == full and tot[y]:
            out[d] = (tot[d] / tot[y] - 1) * 100
    return out, []


def chain_position(qs, capyoy: dict, up_name: str) -> dict:
    """公司营收同比 ÷ 上游开支同比,按季排。看趋势不看绝对值。

    **返回值总是 dict**,算不出来时带 ok=False 和两个计数(可比点几个、
    跳过几个)。以前算不出来直接返回 None,调用方只能写「没有对应的数据源」——
    而实际原因可能是数据齐全但这条链增速温和,两者该做的事完全不同。
    """
    if not capyoy:
        return {"ok": False, "可比点": 0, "跳过": 0, "上游": up_name}
    pts, skipped = [], 0
    for r in qs:
        k = f"{r['period'][:4]}-{CAP_Q[r['period'][4:]]}"
        cy, ry = capyoy.get(k), r.get("单季营收同比")
        if cy is None or ry is None:
            continue
        # 上游同比为负或接近 0 时比值会爆掉(除以小数),这几季直接跳过
        if cy < 10:
            skipped += 1
            continue
        # ⚠ 公司营收同比 ≤0 时倍数是负数,**没有含义**:「增速是上游开支的 −2.85 倍」
        #   不是一句能读的话,更不能进趋势判断(它会把首尾比值的方向整个翻过来)。
        #   深科达 2022Q3 营收同比 −64.6%,原来就在表里挂着一行 −2.85x。
        if ry <= 0:
            skipped += 1
            continue
        pts.append({"period": r["period"], "上游同比": round(cy, 1),
                    "公司营收同比": round(ry, 1), "倍数": round(ry / cy, 2)})
    if len(pts) < 4:
        return {"ok": False, "可比点": len(pts), "跳过": skipped, "上游": up_name}
    first, last = pts[-4]["倍数"], pts[-1]["倍数"]
    # ⚠ **水平和趋势是两件事,不能混成一句话。**
    #   倍数 > 1 = 增速快于上游开支 = 还在得份额;倍数在下降 = 这个优势在收窄。
    #   两者可以同时成立(新易盛 2026Q2 就是:1.11x 仍 >1,但从 2.34x 一路降下来)。
    #   写成"在丢份额"是错的 —— 它仍然比行业快,只是快得没以前多。
    level = ("增速明显快于上游开支(在得份额)" if last > 1.15 else
             "增速明显慢于上游开支(在丢份额)" if last < 0.85 else
             "增速和上游开支基本同步")
    trend = ("优势在扩大" if last > first * 1.15 else
             "优势在收窄" if last < first * 0.85 else "大致平稳")
    # ⚠ 这四个点**不一定是连续的四个季度**。上游增速低于 10%、或公司营收同比 ≤0
    #   的季度被跳过了,所以增速温和的链上序列是稀疏的。
    #
    #   2026-09-02 修正:原来判「稀不稀疏」用的是**首尾年份不同**,那是错的 ——
    #   2025Q3 → 2026Q2 是**连续四季**却因为跨了年被标成「跨了不止一年」,
    #   三只 AI 票的报告和账户文档一直在打这个假警告;反过来,
    #   同一年里跳着取的四个点(2025Q1/Q3 这种)又会被判成「不稀疏」。
    #   正确的判法是**按季度序号比**:四个点横跨的季度数是不是正好 4。
    span = f"{pts[-4]['period']} → {pts[-1]['period']}"
    idx = [_qidx(p["period"]) for p in pts[-4:]]
    span_q = idx[-1] - idx[0] + 1                    # 这四个点实际横跨几个季度
    consecutive = span_q == 4
    # **不连续就不打分,但表照样展示。** 隔着好几个季度的四个点连不成趋势 ——
    # 中间发生过什么完全看不见,首尾一比却会得出一个像模像样的「优势在收窄」。
    # 展示是为了让人看见数据长什么样,打分要的是能支撑判断的证据,两件事。
    base = f"{level},{trend}"
    return {"ok": True, "points": pts, "起点": first, "终点": last, "期间": span,
            "稀疏": not consecutive, "可打分": consecutive, "跨越季度数": span_q,
            "四点": [p["period"] for p in pts[-4:]],
            "上游": up_name, "跳过": skipped,
            "水平": level, "趋势": trend,
            "判断": base if consecutive else f"{base}(⚠ 可比点不连续,只展示不打分)"}


# ── 利润:营收定了之后的第二步 ─────────────────────────────────────────
def margins(qs, n=4):
    """近 n 季的毛利率 / 三费率 / 净利率区间。

    为什么用区间不用点估计:这三项本来就在波动,给一个点等于假装知道得比实际多。
    **财务费用单列** —— 它主要是汇兑,和经营好坏无关,却能吃掉几个点的净利率。
    """
    gm = [r["单季毛利率"] for r in qs if r["单季毛利率"] is not None][-n:]
    ex = [r["三费率"] for r in qs if r["三费率"] is not None][-n:]
    fin = [r["单季财务费用"] for r in qs if r["单季财务费用"] is not None][-n:]
    npm = [r["单季归母净利"] / r["单季营收"] * 100
           for r in qs if r.get("单季归母净利") and r.get("单季营收")][-n:]
    def rng(v):
        return None if not v else {"低": min(v), "高": max(v), "中位": statistics.median(v)}
    return {"毛利率": rng(gm), "三费率": rng(ex), "净利率": rng(npm),
            "财务费用": rng(fin),
            "财务费用波动": (max(fin) - min(fin)) if len(fin) >= 2 else None}


def _chain(code: str, qs, raw: Path):
    """返回 (产业链位置 or None, 上游说明)。

    ⚠ **「算不了」有三种,补救的动作完全不同**,所以说明必须分开写:
      ① 没登记上游       —— 要先弄清这只票的客户是谁,再跑取数也没用
      ② 登记了但数据缺   —— 跑一遍取数脚本就有,是我们的问题
      ③ 数据齐但可比点少 —— 这条链本身增速温和,补数据也补不出趋势
    以前三种都写成一句「没有对应的数据源」。深科达就栽在这上面:
    报告写它「没有数据源」,而它的下游其实全在 A 股,一直拿得到。
    """
    key = UPSTREAM_OF.get(code)
    if not key:
        return None, (f"这只票({code})的上游我们还没登记。要先弄清它的客户是谁 —— "
                      f"拿别的行业的开支当分母,算得出数字但没有含义。")
    g = UPSTREAM[key]
    name = g["名称"]
    if g["kind"] == "sec":
        capyoy, missing = capex_yoy(raw, key), []
        if not capyoy:
            return None, (f"上游是{name},但那份 SEC 季度资本开支没抓到,"
                          f"或者成员不齐凑不出同比。重跑:"
                          f"`tools/sec_facts.py capex --group {key}`。")
    else:
        capyoy, missing = capex_yoy_ashare(raw, g["members"])
        if missing:
            return None, (f"上游是{name},但其中 {'、'.join(missing)} 的三大表没抓到。"
                          f"**少一家就整组不算** —— 篮子少一家,前后两年就不是同一个"
                          f"篮子,同比会凭空多出几十个百分点而且不报错。重跑:"
                          f"`tools/fetch_all.py --codes {','.join(missing)} "
                          f"--group statements`。")
        if not capyoy:
            return None, (f"上游是{name},三大表都在,但凑不出任何"
                          f"「{len(g['members'])} 家全都有数」的同比季度。")
    cp = chain_position(qs, capyoy, name)
    if cp.get("ok"):
        return cp, name
    return None, (f"上游是{name},数据也拿到了,但**能比的季度只有 "
                  f"{cp['可比点']} 个**(要 4 个才看得出趋势),"
                  f"另有 {cp['跳过']} 个季度被跳过 —— 上游同比低于 10% 时,"
                  f"除以一个接近 0 的数会让倍数爆掉。"
                  f"**这不是数据没抓到,是这条链本身增速温和** —— 再去取数也补不出来。")


def load_backtest(raw: Path) -> dict | None:
    """读走前回测的汇总(`data/backtest/revenue-summary.json`)。

    **报告里那几个「历史上这套方法准不准」的数字全部从这里读,一个都不写死。**
    写死的下场:换一批票、多攒几个季度之后,那几个百分比还在讲上一版的故事,
    而且不报错。文件不在就返回 None,报告写「未回测」——
    「没跑过」和「跑过、结果是这样」是两件事。

    生成:`tools/backtest.py --walkforward --raw data/raw`
    """
    p = raw.parent / "backtest" / "revenue-summary.json"
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return j if j.get("并集") else None


def _mid_yoy(qs, target: str, band: dict | None, profit: dict | None):
    """预测中枢相对**去年同一个季度**的同比 → (百分数, 用的是哪个口径, 基数期)。

    先用归母净利(那是盈利侧真正关心的量),去年同期净利 ≤0 时同比没有含义
    (−1 亿 → +1 亿算 +200%?),这时候退到营收。两个都拿不到就返回 None ——
    **不拿 0 顶替**:0 的意思是「持平」,拿不到的意思是「不知道」,两回事。
    """
    prev = f"{int(target[:4]) - 1}{target[4:]}"
    row = next((r for r in qs if r["period"] == prev), None)
    if not row:
        return None, "", prev
    base_ni = row.get("单季归母净利")
    if profit and profit.get("mid") is not None and base_ni and base_ni > 0:
        return (profit["mid"] / base_ni - 1) * 100, "归母净利", prev
    base_rev = row.get("单季营收")
    if band and band.get("mid") is not None and base_rev and base_rev > 0:
        return (band["mid"] / base_rev - 1) * 100, "营收", prev
    return None, "", prev


def build(code: str, raw: Path, target: str | None) -> dict:
    d = quarterly.build(raw, code, since="2022")
    qs = d["quarters"]
    if not qs:
        raise SystemExit(f"{code}: 没有单季数据")
    latest = qs[-1]["period"]
    target = target or next_period(latest)

    methods = [m_inventory(qs, target), m_yoy(qs, target), m_qoq(qs, target)]
    good = [m for m in methods if m.get("ok")]
    band, band_note, consistent = None, "", True
    if good:
        # ── 2026-09-02 改:交集 → 并集。**这一条是被数据否决的,不是拍的。**
        #   46 个走前回测的季度里,生产做法(先交集、空了退并集)命中 16 个(35%),
        #   一律取并集命中 28 个(61%)。三条方法各自的区间是「几个历史比值的
        #   最大值和最小值」围出来的线段,不是概率区间 —— 两条线段相交得到的是
        #   「两边都同意的那一小段」,和「真实值有多大概率落在里面」没有关系。
        #   宽度不到 10% 的 17 个交集只命中 1 个(6%)。
        #   代价说清楚:并集宽(中位 45%),信息量确实少,但它不骗人。
        lo = min(m["low"] for m in good)
        hi = max(m["high"] for m in good)
        overlap = max(m["low"] for m in good) <= min(m["high"] for m in good)
        # 中枢 = 各可用方法中枢的中位数。**并集下它天然落在区间内**
        #   (每个方法的 mid 都在自己的 low~high 里,而并集罩住所有方法),
        #   所以不用再夹一次;真夹到边界说明上游算错了,那要当场报出来而不是抹平。
        mid = statistics.median([m["mid"] for m in good])
        band = {"low": lo, "high": hi, "mid": mid}
        band_note = "并集(至少一个方法这么认为)"
        consistent = overlap
    mg = margins(qs)
    profit = None
    if band and mg["净利率"]:
        profit = {"low": band["low"] * mg["净利率"]["低"] / 100,
                  "high": band["high"] * mg["净利率"]["高"] / 100,
                  "mid": band["mid"] * mg["净利率"]["中位"] / 100}
    cp, up_note = _chain(code, qs, raw)
    yoy, yoy_basis, yoy_base_period = _mid_yoy(qs, target, band, profit)
    return {"code": code, "最新已披露": latest, "预测期": target,
            # 中枢相对去年同期的同比。**清仓那道盈利侧的门要用它** ——
            # 「我们自己推的下一季比去年同期还少」是一条能被季报证伪的坏消息,
            # 而「估值该回到历史某个分位」不是。
            "中枢同比pct": yoy, "中枢同比口径": yoy_basis,
            "中枢同比基数期": yoy_base_period,
            "methods": methods, "可用方法数": len(good),
            # 「方法一致」= 可用方法之间到底有没有互相印证。
            # ≤1 个方法时记 True:一个方法无从和谁矛盾,它把握低是由「可用方法数」
            # 那一维体现的,不该再被「矛盾」罚第二次。
            "方法一致": consistent,
            "边界判定数": sum(1 for m in methods if m.get("边界")),
            "营收区间": band, "区间取法": band_note,
            # 区间有多宽(高÷低−1)。回测里 <10% 宽度的区间几乎必错,
            # 所以这个数要印出来,而不是只印区间本身。
            "区间宽度pct": ((band["high"] / band["low"] - 1) * 100
                            if band and band["low"] > 0 else None),
            "回测": load_backtest(raw),
            "产业链位置": cp,
            "上游说明": up_note,
            "利润率": mg, "归母净利区间": profit,
            # PB 路径要用它算「这一年净资产会长多少」—— 那条路径假设净资产不变,
            # 这个数就是那个假设漏掉了多少。
            "最近归母净资产": qs[-1].get("期末归母净资产"),
            "生成日": date.today().isoformat(),
            "来源": d["来源"]}


def render(f: dict) -> str:
    """第 3 节的正文。**实现搬到 `forecast_render.py`** —— 这个文件本来快到 800 行,
    再往里加就成了「算和写混在一起」的大文件。调用方一个字都不用改。"""
    import forecast_render
    return forecast_render.render(f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--target", help="预测哪一季,如 2026Q3;默认最新已披露的下一季")
    ap.add_argument("--log", help="把预测存进这个目录(存档后不许改)")
    a = ap.parse_args()

    for c in a.codes:
        f = build(c, Path(a.raw), a.target)
        print(render(f))
        print()
        if a.log:
            d = Path(a.log)
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{c}-{f['预测期']}.json"
            if p.exists():
                print(f"  · {p} 已存在,**不覆盖** —— 预测存档后改它就等于没预测过")
            else:
                p.write_text(json.dumps(f, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  → 存档 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
