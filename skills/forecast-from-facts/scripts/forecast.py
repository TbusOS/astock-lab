#!/usr/bin/env python3
"""从事实推下一季 —— 多个互相独立的估计法,每个自己判断适不适用。

    forecast.py <code> [--raw data/raw] [--target 2026Q3] [--log predictions/]

为什么要好几个方法而不是一个:
    单一方法在**它不适用的票上不会报错,只会给出一个看着正常的错数**。
    存货法在中际旭创上历史离散度 20.6%(能用),在杰瑞股份上 53.8%(不能用)——
    杰瑞 Q4 是交付旺季,期末存货和下季营收的关系被季节性彻底压过去。
    如果只写一个方法,杰瑞那个数照样会打印出来,而且没有任何提示。

    所以每个方法**先自检适用性,不适用就退出并说明理由**,
    最后只用通过自检的那几个取并集。剩几个方法本身就是信息:
    只剩一个 = 这只票的下季营收我们其实没什么把握。

三个方法互相独立在哪:
    存货法看**供给侧已经花出去的钱**;同比法看**需求侧的增长趋势**;
    环比法看**这门生意本身的季节形状**。三者共用的只有营收这一个观测量,
    出错的原因互不相同 —— 所以三个都指向同一区间才算真的有把握。

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


def _qidx(period: str) -> int:
    """把 `2025Q3` 变成一个能做减法的序号,用来量两个季度之间隔多远。

    为什么需要它:判「四个点连不连续」不能比年份 —— 2025Q4 和 2026Q1 年份不同
    但只隔一个季度,2025Q1 和 2025Q4 年份相同却隔三个。年份和距离是两回事。
    """
    return int(period[:4]) * 4 + Q_ORDER.index(period[4:])


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
    ratios = [r["下季营收÷期末存货"] for r in qs if r["下季营收÷期末存货"]][-4:]
    if inv is None:
        return {"name": "存货法", "ok": False, "why": f"{last} 没有期末存货数据"}
    if len(ratios) < 3:
        return {"name": "存货法", "ok": False, "why": f"历史比值只有 {len(ratios)} 个样本,不够"}
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


# 每个方法的说明。**报告里必须把这三段打出来** ——
# 只写「三个方法里 1 个适用」而不说是哪三个、每个怎么算,读者没法判断这个「1」够不够。
METHOD_DOC = {
    "存货法": {
        "一句话": "拿上季末的存货,乘以这只票历史上「下一季营收 ÷ 上季末存货」的比值区间。",
        "为什么成立": "制造业要出货就得先有货。期末存货是下一季能交付的物质基础,"
                      "而且这笔钱**已经花出去了** —— 是行为不是说法,"
                      "管理层在电话会上怎么讲都改不了这个数。",
        "算式": "下季营收 = 上季末存货 × 历史比值[最低, 最高]",
        "什么时候不能用": "这个比值本身不稳的时候。脚本算近四季比值的离散度"
                          "(极差 ÷ 中位),超过 30% 就判定不适用 —— "
                          "多半是季节性交付把关系压过去了(比如 Q4 是交付旺季),"
                          "或者扩产爬坡改变了备货节奏。",
    },
    "同比法": {
        "一句话": "拿去年同一个季度的营收,乘以最近三个季度的同比增速区间。",
        "为什么成立": "同比自动消掉季节性 —— Q3 比 Q3,不受「Q4 是旺季」影响。"
                      "对季节性强的生意,这是唯一不用先建季节模型就能用的方法。",
        "算式": "下季营收 = 去年同期营收 × (1 + 近三季同比[最低, 最高])",
        "什么时候不能用": "增速本身在快速拐弯的时候。脚本的判断是:"
                          "近三季同比的跨度超过 100 个百分点就不适用 —— "
                          "那说明增速在剧烈变化,拿过去的增速外推没有意义。",
    },
    "环比法": {
        "一句话": "拿上一季营收,乘以这只票历史上**同一个季度转换**的环比倍数。",
        "为什么成立": "很多生意的季节形状是稳定的(Q1 春节淡、Q4 交付旺)。"
                      "只取同一个转换(历年的 Q2→Q3),不混用不同转换 —— "
                      "混用等于把刚消掉的季节性又搅回去。",
        "算式": "下季营收 = 上季营收 × 历年同一转换的环比[最低, 最高]",
        "什么时候不能用": "季节形状不稳定的时候。脚本判断:历年同一转换的环比"
                          "离散度超过 45% 就不适用(环比天然比存货法散,所以阈值放宽)。"
                          "样本少于 2 个也不用 —— 两个点不构成规律。",
    },
}


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
        lo, hi = max(m["low"] for m in good), min(m["high"] for m in good)
        if lo <= hi:
            # 交集:**所有能用的方法都同意**的那一段。比并集窄得多,而且更有信息 ——
            # 并集只说"至少一个方法这么认为",交集说"没有一个方法反对"。
            band = {"low": lo, "high": hi,
                    "mid": statistics.median([m["mid"] for m in good])}
            band_note = "交集(所有可用方法都同意的区间)"
        else:
            # 不重叠本身就是结论:我们的方法互相矛盾,这时候给并集并且明说
            band = {"low": min(m["low"] for m in good), "high": max(m["high"] for m in good),
                    "mid": statistics.median([m["mid"] for m in good])}
            band_note = "并集 —— ⚠ **几个方法的区间不重叠**,它们互相矛盾"
            consistent = False
    mg = margins(qs)
    profit = None
    if band and mg["净利率"]:
        profit = {"low": band["low"] * mg["净利率"]["低"] / 100,
                  "high": band["high"] * mg["净利率"]["高"] / 100,
                  "mid": band["mid"] * mg["净利率"]["中位"] / 100}
    cp, up_note = _chain(code, qs, raw)
    return {"code": code, "最新已披露": latest, "预测期": target,
            "methods": methods, "可用方法数": len(good),
            # 「方法一致」= 可用方法之间到底有没有互相印证。
            # ≤1 个方法时记 True:一个方法无从和谁矛盾,它把握低是由「可用方法数」
            # 那一维体现的,不该再被「矛盾」罚第二次。
            "方法一致": consistent,
            "边界判定数": sum(1 for m in methods if m.get("边界")),
            "营收区间": band, "区间取法": band_note,
            "产业链位置": cp,
            "上游说明": up_note,
            "利润率": mg, "归母净利区间": profit,
            # PB 路径要用它算「这一年净资产会长多少」—— 那条路径假设净资产不变,
            # 这个数就是那个假设漏掉了多少。
            "最近归母净资产": qs[-1].get("期末归母净资产"),
            "生成日": date.today().isoformat(),
            "来源": d["来源"]}


def render(f: dict) -> str:
    def y(v, nd=1):
        return "—" if v is None else f"{v / 1e8:,.{nd}f}"

    L = [f"### {f['code']} · 我们自己对 {f['预测期']} 的预测", ""]
    L.append(f"最新已披露:**{f['最新已披露']}**。以下全部由公司自己报表里的数推出,"
             f"**没有引用任何机构的预测**。")
    L.append("")
    L.append("**先说清楚是哪三个方法。** 三者共用的只有营收这一个观测量,"
             "**出错的原因互不相同** —— 存货法看供给侧已经花出去的钱,"
             "同比法看需求侧的增长趋势,环比法看这门生意本身的季节形状。"
             "所以三个都指向同一区间才算真的有把握;只剩一个的时候,"
             "那个区间没有任何东西可以印证它。")
    L.append("")
    for name, doc in METHOD_DOC.items():
        m = next((x for x in f["methods"] if x["name"] == name), None)
        mark = "✓ 本次适用" if (m and m.get("ok")) else "✗ 本次不适用"
        L.append(f"**{name}**　{mark}")
        L.append("")
        L.append(f"- **是什么**:{doc['一句话']}")
        L.append(f"- **为什么这么算**:{doc['为什么成立']}")
        L.append(f"- **算式**:`{doc['算式']}`")
        L.append(f"- **什么时候不能用**:{doc['什么时候不能用']}")
        if m:
            L.append(f"- **这只票的实际情况**:"
                     + (m.get("依据", "") if m.get("ok") else m.get("why", "")))
        L.append("")
    L.append("三个方法在这只票上的结果:")
    L.append("")
    L.append("| 方法 | 能不能用 | 结果 | 依据 / 不能用的原因 |")
    L.append("|---|---|---:|---|")
    for m in f["methods"]:
        if m.get("ok"):
            L.append(f"| {m['name']} | ✓ | {y(m['low'])} ~ {y(m['high'])} 亿 "
                     f"| {m['依据']} |")
        else:
            L.append(f"| {m['name']} | ✗ | — | {m['why']} |")
    L.append("")

    n = f["可用方法数"]
    if not n:
        L.append("> **三个方法一个都不适用。** 这只票的下季营收我们没有可靠的推法 —— "
                 "如实写出来,而不是硬给一个数。要补的是更细的数据"
                 "(分产品/分客户收入、订单、产能),现有财报颗粒不够。")
        return "\n".join(L)

    edge_n = f.get("边界判定数") or 0
    if edge_n:
        L.append(f"> ⚠ **上面有 {edge_n} 个判定踩在阈值边上**(离阈值不到它的 10%)。"
                 f"阈值两边是两个相反的结论,而差距只有几个点 —— "
                 f"下个季度重跑时它很可能翻面。翻面时该问的是"
                 f"「公司变了没有」,多半答案是没变,只是噪声过了线。")
        L.append("")

    b = f["营收区间"]
    L.append(f"**{f['预测期']} 单季营收:{y(b['low'])} ~ {y(b['high'])} 亿"
             f"(中枢 {y(b['mid'])} 亿)**,{n} 个方法取{f['区间取法']}。")
    if n >= 2 and not f.get("方法一致", True):
        L.append("")
        L.append(f"> ⚠ **{n} 个方法都通过了自检,但它们给的区间不重叠 —— 互相矛盾。**"
                 f"这不构成互相印证,反而比只有一个方法更糟:"
                 f"至少有一个方法在这只票上是错的,而我们不知道是哪一个。"
                 f"上面给的是并集,它只说「至少一个方法这么认为」。")
    if n == 1:
        L.append("")
        L.append("> ⚠ **只有 1 个方法适用**,没有第二个来交叉验证。"
                 "这个区间的可信度比有 2~3 个方法时低一档,用的时候要打折。")
    L.append("")

    mg = f["利润率"]
    if mg["毛利率"]:
        g = mg["毛利率"]
        L.append(f"- **毛利率**:近四季 {g['低']:.1f}%~{g['高']:.1f}%,中位 {g['中位']:.1f}%")
    if mg["三费率"]:
        e = mg["三费率"]
        L.append(f"- **三费率**:近四季 {e['低']:.1f}%~{e['高']:.1f}%")
    if mg["财务费用波动"] is not None:
        fin = mg["财务费用"]
        L.append(f"- **财务费用**:近四季 {y(fin['低'], 2)} ~ {y(fin['高'], 2)} 亿,"
                 f"波动 {y(mg['财务费用波动'], 2)} 亿。**这一项主要是汇兑,和生意好坏无关**,"
                 f"但能吃掉几个点的净利率 —— 它是净利预测里最大的不确定来源。")
    p = f["归母净利区间"]
    if p:
        L.append("")
        L.append(f"**{f['预测期']} 单季归母净利:{y(p['low'])} ~ {y(p['high'])} 亿"
                 f"(中枢 {y(p['mid'])} 亿)**,= 营收区间 × 近四季净利率区间。")
    cp = f.get("产业链位置")
    if cp:
        up = cp["上游"]
        L.append("")
        L.append(f"**在产业链里的位置:{cp['判断']}**"
                 f"(相对{up}的增速倍数,{cp['期间']} 从 "
                 f"{cp['起点']:.2f}x 变到 {cp['终点']:.2f}x)")
        if not cp.get("可打分", True):
            L.append("")
            L.append(f"> ⚠ **这四个可比点不连续,所以这一节只展示,不参与打分。**"
                     f"它们是 {'/'.join(cp['四点'])},横跨 {cp['跨越季度数']} 个季度"
                     f"(连续的话应该是 4 个)。中间被跳掉的季度里发生过什么完全看不见,"
                     f"首尾一比却会得出一个像模像样的趋势。"
                     f"跳掉的原因有两种:上游同比低于 10%(除以接近 0 的数会让倍数爆掉),"
                     f"或公司营收同比 ≤0(倍数是负数,没有含义)。"
                     f"本轮共跳过 {cp['跳过']} 个季度。")
        L.append("")
        L.append(f"| 季度 | {up}同比 | 公司营收同比 | 倍数 |")
        L.append("|---|---:|---:|---:|")
        for x in cp["points"][-6:]:
            L.append(f"| {x['period']} | {x['上游同比']:.1f}% "
                     f"| {x['公司营收同比']:.1f}% | {x['倍数']:.2f}x |")
        L.append("")
        kind = (UPSTREAM.get(UPSTREAM_OF.get(f["code"], ""), {}) or {}).get("kind")
        src = ("上游数据来自各家 10-Q/10-K 的申报原值(SEC XBRL)。" if kind == "sec" else
               "上游数据来自各家自己的现金流量表(「购建固定资产、无形资产和其他"
               "长期资产所支付的现金」),和公司营收出自同一套报表。")
        L.append(f"> {up}是这条链的总量,公司增速相对它的倍数就是分到的份额在怎么变。"
                 "**这个倍数本身不稳(历史离散度 70% 以上),不能拿来推营收** —— "
                 "但它的方向是硬信息。" + src)
    elif f.get("上游说明"):
        L.append("")
        L.append(f"**在产业链里的位置:算不了。** {f['上游说明']}")
        L.append("")
        L.append("> **「算不了」不等于「这只票没有上游」。** 上面那句话说的是"
                 "具体卡在哪一步 —— 是还没登记客户群、是数据没抓到、"
                 "还是数据齐全但可比季度不够。三种要做的事完全不同。")

    L.append("")
    L.append("**这份预测怎么被推翻**:")
    for m in f["methods"]:
        if m.get("ok"):
            L.append(f"- {m['name']}:{m['失效条件']}")
    L.append(f"- 任何一条:{f['预测期']} 季报出来后实际值落在区间外 —— "
             f"那就是这套方法在这只票上不成立,要改的是方法不是解释")
    return "\n".join(L)


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
