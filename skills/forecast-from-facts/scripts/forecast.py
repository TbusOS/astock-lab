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
    if d > DISPERSION_MAX:
        return {"name": "存货法", "ok": False,
                "why": f"近四季比值离散度 {d:.1f}% > {DISPERSION_MAX:.0f}%,"
                       f"这条关系在这只票上被别的因素压过去了(多半是季节性交付)",
                "细节": {"比值": [round(x, 3) for x in ratios]}}
    return {"name": "存货法", "ok": True,
            "low": inv * min(ratios), "high": inv * max(ratios),
            "mid": inv * statistics.median(ratios),
            "依据": f"{last} 期末存货 {inv/1e8:.1f} 亿 × 近四季比值 "
                    f"{min(ratios):.2f}~{max(ratios):.2f}x(离散度 {d:.1f}%)",
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
    if hi - lo > 100:
        return {"name": "同比法", "ok": False,
                "why": f"近三季同比在 {lo:.0f}%~{hi:.0f}% 之间摆动,跨度超过 100 个百分点,"
                       f"外推没有意义",
                "细节": {"近三季同比": [round(x, 1) for x in recent]}}
    return {"name": "同比法", "ok": True,
            "low": base * (1 + lo / 100), "high": base * (1 + hi / 100),
            "mid": base * (1 + statistics.median(recent) / 100),
            "依据": f"去年同期 {base/1e8:.1f} 亿 × 近三季同比区间 {lo:.1f}%~{hi:.1f}%",
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
    if d > DISPERSION_MAX * 1.5:                     # 环比天然比存货法散,放宽到 45%
        return {"name": "环比法", "ok": False,
                "why": f"历年「→{target[4:]}」环比离散度 {d:.1f}%,季节形状不稳定",
                "细节": {"历年环比": [round(x, 3) for x in hist]}}
    return {"name": "环比法", "ok": True,
            "low": rev[last] * min(hist), "high": rev[last] * max(hist),
            "mid": rev[last] * statistics.median(hist),
            "依据": f"{last} 营收 {rev[last]/1e8:.1f} 亿 × 历年「→{target[4:]}」环比 "
                    f"{min(hist):.2f}~{max(hist):.2f}x({len(hist)} 个样本)",
            "失效条件": "业务结构变化改变了交付节奏"}


# ── 产业链位置:公司增速 ÷ 上游开支增速 ────────────────────────────────
# ★ 这**不是预测方法**。2026-09-02 实测,这个倍数在三只票上离散度 70%~120%,
#   拿它推下季营收会给出一个看着正常的错数。
#   但它的**趋势**是硬信息:倍数持续往上 = 在产业链里得份额,往下 = 丢份额。
#   第一性原理:云厂开支是这条链的总蛋糕,公司增速相对它的比值就是分到的份额变化。
CAP_Q = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}

# ★ 每只票的上游是谁。**拿错分母比不算更糟**:杰瑞股份(油服设备)对着云厂
#   资本开支算出来的倍数是 0.04x —— 数字有,含义没有,而且看起来像个结论。
#   没登记的票不做这一节,并在报告里说明「这只票的上游我们还没有对应的数据源」。
UPSTREAM_OF = {
    "300502": "cloud",     # 新易盛     光模块 → 云厂建数据中心
    "300308": "cloud",     # 中际旭创   光模块 → 同上
    "300476": "cloud",     # 胜宏科技   AI 服务器 PCB → 同上
    "002353": "oilgas",    # 杰瑞股份   油服设备 → 油气公司资本开支
    # 688328 深科达:面板/半导体封装设备,上游是面板厂和封测厂的资本开支。
    #   京东方 / TCL 华星 / 长电 这些没有 SEC 申报,得另找数据源 —— 现在没有,所以不做。
}
UPSTREAM_NAME = {"cloud": "北美四大云厂资本开支", "oilgas": "五家国际油气公司资本开支"}


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


def chain_position(qs, capyoy: dict, up_name: str) -> dict | None:
    """公司营收同比 ÷ 上游开支同比,按季排。看趋势不看绝对值。"""
    if not capyoy:
        return None
    pts = []
    for r in qs:
        k = f"{r['period'][:4]}-{CAP_Q[r['period'][4:]]}"
        cy, ry = capyoy.get(k), r.get("单季营收同比")
        # 上游同比为负或接近 0 时比值会爆掉(除以小数),这几季直接跳过
        if cy is None or ry is None or cy < 10:
            continue
        pts.append({"period": r["period"], "上游同比": round(cy, 1),
                    "公司营收同比": round(ry, 1), "倍数": round(ry / cy, 2)})
    if len(pts) < 4:
        return None
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
    # ⚠ 这几个点**不一定是连续的四个季度**。上游增速低于 10% 的季度被跳过了
    #   (除以接近 0 的数会让倍数爆掉),所以油气这类增速温和的链上,序列是稀疏的。
    #   杰瑞股份实测:最后四个可比点跨了 2023Q4~2025Q4 整整两年。
    #   写成「近四季」会让人以为是最近一年的变化 —— 必须把真实期间打出来。
    span = f"{pts[-4]['period']} → {pts[-1]['period']}"
    sparse = len(pts) >= 4 and pts[-1]["period"][:4] != pts[-4]["period"][:4]
    return {"points": pts, "起点": first, "终点": last, "期间": span,
            "稀疏": sparse, "上游": up_name,
            "水平": level, "趋势": trend, "判断": f"{level},{trend}"}


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
    g = UPSTREAM_OF.get(code)
    if not g:
        return None
    return chain_position(qs, capex_yoy(raw, g), UPSTREAM_NAME[g])


def build(code: str, raw: Path, target: str | None) -> dict:
    d = quarterly.build(raw, code, since="2022")
    qs = d["quarters"]
    if not qs:
        raise SystemExit(f"{code}: 没有单季数据")
    latest = qs[-1]["period"]
    target = target or next_period(latest)

    methods = [m_inventory(qs, target), m_yoy(qs, target), m_qoq(qs, target)]
    good = [m for m in methods if m.get("ok")]
    band, band_note = None, ""
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
    mg = margins(qs)
    profit = None
    if band and mg["净利率"]:
        profit = {"low": band["low"] * mg["净利率"]["低"] / 100,
                  "high": band["high"] * mg["净利率"]["高"] / 100,
                  "mid": band["mid"] * mg["净利率"]["中位"] / 100}
    return {"code": code, "最新已披露": latest, "预测期": target,
            "methods": methods, "可用方法数": len(good),
            "营收区间": band, "区间取法": band_note,
            "产业链位置": _chain(code, qs, raw),
            "上游说明": (UPSTREAM_NAME.get(UPSTREAM_OF.get(code), "")
                        or f"这只票({code})的上游我们还没有对应的数据源,"
                           f"这一节不做 —— 拿云厂资本开支去衡量一家上游不是云厂的公司,"
                           f"算得出数字但没有含义"),
            "利润率": mg, "归母净利区间": profit,
            "生成日": date.today().isoformat(),
            "来源": d["来源"]}


def render(f: dict) -> str:
    def y(v, nd=1):
        return "—" if v is None else f"{v / 1e8:,.{nd}f}"

    L = [f"### {f['code']} · 我们自己对 {f['预测期']} 的预测", ""]
    L.append(f"最新已披露:**{f['最新已披露']}**。以下全部由公司自己报表里的数推出,"
             f"**没有引用任何机构的预测**。")
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

    b = f["营收区间"]
    L.append(f"**{f['预测期']} 单季营收:{y(b['low'])} ~ {y(b['high'])} 亿"
             f"(中枢 {y(b['mid'])} 亿)**,{n} 个方法取{f['区间取法']}。")
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
        if cp["稀疏"]:
            L.append("")
            L.append(f"> ⚠ 这四个可比点**跨了不止一年**({cp['期间']})。"
                     f"上游增速低于 10% 的季度被跳过了 —— 除以接近 0 的数会让倍数爆掉。"
                     f"所以这条趋势反映的是更长时间的变化,不是最近一年的。")
        L.append("")
        L.append(f"| 季度 | {up}同比 | 公司营收同比 | 倍数 |")
        L.append("|---|---:|---:|---:|")
        for x in cp["points"][-6:]:
            L.append(f"| {x['period']} | {x['上游同比']:.1f}% "
                     f"| {x['公司营收同比']:.1f}% | {x['倍数']:.2f}x |")
        L.append("")
        L.append(f"> {up}是这条链的总量,公司增速相对它的倍数就是分到的份额在怎么变。"
                 "**这个倍数本身不稳(历史离散度 70% 以上),不能拿来推营收** —— "
                 "但它的方向是硬信息。上游数据来自各家 10-Q/10-K 的申报原值(SEC XBRL)。")
    elif f.get("上游说明"):
        L.append("")
        L.append(f"**在产业链里的位置:算不了。** {f['上游说明']}")

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
