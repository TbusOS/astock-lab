#!/usr/bin/env python3
"""行业规则表 —— 先问「这只票是什么生意」,再决定用哪把尺。

    sector_rules.py <code> [--raw data/raw]

## 这一层在回答什么

**同一套尺套所有行业会出错,而且错得看不出来。** 成长股看增速能不能持续,
周期股看它在周期的哪个位置;成长股的 PE 分位有意义,周期股的 PE 在盈利低谷
会给出「便宜」的假信号(分母小),在盈利高峰给出「贵」的假信号 ——
两次都是反的,而数字本身完全正常。

**没有这一层会怎样**:2026-08-28 实测,拿 AI 算力链那套跑油气设服票,
四个地方同时失效(云厂 capex 无关、PEG 算出 −15.30、净利增速基准率不适用、
只剩滞后指标),报告一句提示都没有。

## 为什么单独一张表,不写进 sectors.py

`sectors.SECTORS` 回答的是「这个行业该看什么领先指标、哪些指标在这里会骗人」——
那是**方法定义**。这张表回答的是「本仓这套预测/估值流程,在这个行业上
该用哪把估值尺、基准率算不算数、上游是谁」——那是**本流程的接线**。
两件事变化的速度不一样:换一把估值尺不需要改行业定义,新增一个行业也不必
立刻决定它用什么尺。所以分开放,用 `check_flow_contract.py` 卡住
「这张表的键必须都在 `sectors.SECTORS` 里」,防止两边漂开。

## 取数和判断分开

**行业判断只读 `data/raw/sectors/<code>/<日期>.json` 缓存,不联网。**
`sectors.classify()` 是纯函数,给同样的板块列表一定给同样的行业;
不稳的是「拿板块列表」那一步。2026-09-02 实测:同一个函数,库里调正常,
命令行直跑五只全判「未定义」——差别只在网络时序。
**判断建在会抖的调用上,行业就会随机漂,而漂了不报错。**
抓数据是 `fetch_all.py --group sectors` 的事。

依赖:无(读 fetch_all 落下来的 json + stock-analysis-workflow 的 sectors.py)。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
# sectors.py 住在 stock-analysis-workflow 那个 skill 下 —— 行业定义是**方法**,
# 属于那一套,不复制到这里。复制一份的下场见 check_sector_contract.py 的注释:
# 两边名字和切分全不一样,而且没人发现。
sys.path.insert(0, str(_here.parent.parent / "stock-analysis-workflow" / "scripts"))


# ── 行业 → 本流程该用哪把尺 ──────────────────────────────────────────
# 字段:
#   估值尺        进决策的那把尺:pe31 / pb / ps31+pb。三情景价位按它算
#   展示尺        算出来但**不打分**的尺,以及什么条件下明确标失效
#   基本面主指标  这个行业该盯的几件事(报告里逐条渲染,拿不到就写拿不到)
#   基准率        全市场净利增速基准率在这个行业适不适用 + 不适用的原因
#   上游组        forecast.UPSTREAM 里的组名;None = 按票登记(查 UPSTREAM_OF)
#   领先指标      guidance.py 里该跑哪几项
SECTOR_RULES = {
    "AI算力链": {
        "估值尺": "pe31",
        "估值尺说明": "自建 3+1 PE 带(市值 ÷「最近三季已披露 + 下一季」归母净利)。"
                      "这是成长股 —— 利润跟着需求走,而且没有大幅的产能周期把它压到 0,"
                      "所以利润这把尺是稳的。",
        "展示尺": [],
        "基本面主指标": [
            ("相对云厂 capex 的增速倍数", "上游把蛋糕做大时,这家分到的那块在变大还是变小"),
            ("云厂 capex 指引", "当事人自己的下期计划,领先出货 1~2 个季度"),
            ("台湾光通信月营收", "同一条链上的月度数,比季报早两个月看到拐点"),
        ],
        "基准率": True,
        "基准率不适用原因": "",
        "上游组": "cloud",
        "领先指标": ["cloud_guidance", "taiwan_optics", "upstream_capex"],
    },
    "半导体设备": {
        "估值尺": "ps31+pb",
        "估值尺说明": "3+1 PS 带(市值 ÷「最近三季已披露 + 下一季」营业总收入,"
                      "算法和 PE 带完全一样)+ PB 全历史分位。"
                      "**先看订单不看利润** —— 设备是项目制,一台机器从下单到确认收入"
                      "隔好几个季度,利润在这中间被一次性的验收、减值、股权激励费用推来推去;"
                      "营收和净资产比利润稳。",
        "展示尺": [("pe31", "3+1 净利率 < 5% 或 3+1 PE > 80 时标失效 —— "
                            "低基数上的 PE 是个噪声放大器:净利率 3% 时,"
                            "利润差一个点,PE 就差三分之一")],
        "基本面主指标": [
            ("合同负债同比", "已经收到的预收款 = 在手订单,领先营收 1~3 个季度"),
            ("存货同比", "为交付备的货,是已经花出去的钱,不是说法"),
            ("相对下游 capex 的增速倍数", "面板厂和封测厂在不在扩产能;算得出就看,算不出不猜"),
        ],
        "基准率": False,
        "基准率不适用原因": "净利基数太小 —— 0.2 亿变 0.6 亿就是 +200%,"
                            "全市场增速基准率把它和几十亿利润的公司放进同一个样本组,"
                            "算出来的「持续性」回答的不是这只票的问题",
        "上游组": None,            # 按票登记,查 forecast.UPSTREAM_OF
        "领先指标": ["upstream_capex"],
    },
    "油气设服": {
        "估值尺": "pb",
        "估值尺说明": "PB 全历史分位(日线 pbMRQ)。**周期股不能看 PE** —— "
                      "盈利低谷时利润接近 0,PE 飙到几百倍看着「很贵」,"
                      "而那正是该买的位置;盈利高峰反过来。两次都是反的,"
                      "数字却完全正常。PB 的分母是净资产,不随景气一年翻几倍。",
        "展示尺": [("pe31", "周期股的 PE 分位在拐点附近是反的,一律只展示不打分")],
        "基本面主指标": [
            ("相对三桶油 capex 的增速倍数", "客户的开支就是这门生意的总蛋糕"),
            ("合同负债 / 预付款同比", "在手订单和抢产能的钱,都是已经发生的行为"),
            ("油价现价", "只展示 —— 我们只有一个现价,没有历史序列,做不出位置判断"),
        ],
        "基准率": False,
        "基准率不适用原因": "强周期股的净利同比在拐点附近毫无预测力 —— "
                            "基准率库是全市场算法,拿它校准周期股会把周期位置误当成增长持续性",
        "上游组": "oil_cn",
        "领先指标": ["upstream_capex", "oil_price"],
    },
    "消费": {
        "估值尺": None,
        "估值尺说明": "**这个行业本流程还没定尺** —— 消费股的定价靠现金流质量和"
                      "渠道库存,我们两样都没有数据源。",
        "展示尺": [("pe31", "没有定尺之前,PE 分位只当背景,不打分")],
        "基本面主指标": [],
        "基准率": False,
        "基准率不适用原因": "本流程还没为这个行业定判断规则,基准率算出来也没有对应的判断条件",
        "上游组": None,
        "领先指标": [],
    },
    "医药": {
        "估值尺": None,
        "估值尺说明": "**这个行业本流程还没定尺** —— 集采和医保谈判会一次性重置"
                      "整个行业的估值中枢,历史分位在那之后不再可比。",
        "展示尺": [("pe31", "政策冲击会让历史分位失去参照意义,只当背景")],
        "基本面主指标": [],
        "基准率": False,
        "基准率不适用原因": "本流程还没为这个行业定判断规则",
        "上游组": None,
        "领先指标": [],
    },
}

# 未定义行业:**什么尺都不给**。
# 为什么不退回「默认用 PE 带」:那是最危险的做法 —— 它会产出一份看着完整的报告,
# 每个数都算得出来,而所有依赖行业假设的数都可能是反的,读者没有任何提示。
# 宁可明说「这只票我们还没想清楚它是什么生意」,让结论强度当场降下来。
UNDEFINED = {
    "估值尺": None,
    "估值尺说明": "**行业未定义,没有可用的估值尺。** 不知道是成长还是周期,"
                  "就不知道该看利润还是看净资产 —— 随便挑一把会给出一个看着正常的错数。",
    "展示尺": [("pe31", "行业未定义,所有分位只当背景")],
    "基本面主指标": [],
    "基准率": False,
    "基准率不适用原因": "行业未定义 —— 不知道这只票的增长是什么驱动的,"
                        "拿全市场基准率去校准它,回答的不是它的问题",
    "上游组": None,
    "领先指标": [],
}

# 性质 → 三情景用哪种倍数。**只有两种**,不搞第三种:
#   成长 → 用利润的尺(3+1 PE 带的 p25/p50/p75)
#   周期 → 用净资产的尺(PB 回到 p50 / 不变 / 到 p75)
GROWTH_SECTORS = {"AI算力链"}

# 宽泛提示词:命中它们**不代表判对了行业**。
# 「专用设备」是申万二级行业名,底下装着锂电设备、光伏设备、印刷机械、纺织机械……
# 一只票只靠这类词命中时,行业是**推出来的不是读出来的**,报告必须当场说明,
# 否则读者会以为它和「光通信模块 / 油气设服」这种精确命中是同一个可信度。
BROAD_HINTS = {"专用设备", "其他专用设备", "服务器", "算力"}


def weak_match(matched) -> bool:
    """这次命中是不是全靠宽泛提示词。**是的话,行业判断降级为「按主营推断」。**"""
    return bool(matched) and all(
        any(h in b for h in BROAD_HINTS) for b in matched)


def nature_of(name: str | None) -> str:
    """这只票按「成长」还是「周期」那套算三情景。

    ⚠ 这不是对公司的评价,是**选哪把尺**。半导体设备在 sectors.py 里写的是
    「成长(强周期性)」—— 生意确实在长,但它的利润被产能周期和项目确认节奏
    推来推去,拿利润的尺量它会在低谷说贵、在高峰说便宜。所以按周期那套算。
    """
    if name in GROWTH_SECTORS:
        return "成长"
    if name is None:
        return "未定义"
    return "周期"


def rules_for(name: str | None) -> dict:
    return SECTOR_RULES.get(name) or UNDEFINED


def newest(raw: Path, pat: str):
    fs = sorted(glob.glob(str(raw / pat)))
    return Path(fs[-1]) if fs else None


def load(raw: Path, code: str) -> dict:
    """读行业缓存。**文件不存在就说「没抓」,绝不现场联网补。**

    为什么不允许「文件没有就联网抓一次」:那样这一步的结果就又变成
    「取决于跑的时候网络通不通」——同一份数据跑两遍可能给出两个行业,
    而报告不会告诉你这次是哪一种。取数是取数,判断是判断。
    """
    f = newest(raw, f"sectors/{code}/*.json")
    if not f:
        return {"ok": False, "行业": None, "原因": "没抓 —— "
                f"`data/raw/sectors/{code}/` 下没有文件。"
                f"先跑 `tools/fetch_all.py --codes {code} --group sectors`",
                "boards": [], "命中板块": [], "次选": [],
                "规则": UNDEFINED, "性质": "未定义"}
    try:
        j = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        j = {}
    for env in j.values():
        d = env.get("data") or {}
        if not env.get("ok"):
            return {"ok": False, "行业": None,
                    "原因": f"抓过但失败了({env.get('error')})。重跑:"
                            f"`tools/fetch_all.py --codes {code} --group sectors`",
                    "boards": [], "命中板块": [], "次选": [],
                    "规则": UNDEFINED, "性质": "未定义", "文件": f.name}
        name = d.get("行业")
        return {"ok": True, "行业": name, "性质本表": d.get("性质"),
                "boards": d.get("boards") or [],
                "命中板块": d.get("命中板块") or [],
                "次选": d.get("次选") or [],
                "原因": "" if name else "板块列表拿到了,但没有一个命中已定义的行业",
                "规则": rules_for(name), "性质": nature_of(name),
                "文件": f.name, "url": env.get("url", ""),
                "抓取时间": env.get("fetched_at", "")}
    return {"ok": False, "行业": None, "原因": f"{f.name} 里没有可读的信封",
            "boards": [], "命中板块": [], "次选": [],
            "规则": UNDEFINED, "性质": "未定义"}


def spec_of(name: str | None) -> dict:
    """从 sectors.SECTORS 拿这个行业的领先指标表和失效指标 —— **不在这里复制一份**。"""
    import sectors
    return sectors.SECTORS.get(name) or sectors.DEFAULT


# ── 渲染:报告第 0.5 节 ───────────────────────────────────────────────
def render(s: dict, n: str, L: list) -> None:
    """第 0.5 节「这只票是什么生意」。

    放在第 0 节之后、结论之前,是因为**后面每一节用哪把尺都由它决定**。
    放在后面就成了背景介绍,而它其实是前置条件。
    """
    name, r = s["行业"], s["规则"]
    L.append(f"## {n} 这只票是什么生意(先判行业,再决定用哪把尺)")
    L.append("")
    if not name:
        L.append("# ⚠ **行业未定义 —— 这份报告的结论强度大幅下降**")
        L.append("")
        L.append(f"原因:{s['原因']}")
        L.append("")
        L.append("**为什么这件事这么严重**:同一套尺套所有行业会出错,而且错得看不出来。"
                 "成长股看增速能不能持续,周期股看它在周期的哪个位置;"
                 "成长股的 PE 分位有意义,周期股的 PE 在盈利低谷给「便宜」的假信号"
                 "(分母小)、在高峰给「贵」的假信号 —— **两次都是反的,而数字本身完全正常**。")
        L.append("")
        L.append("所以下面所有依赖行业假设的东西都标为失效:估值尺、基准率、"
                 "领先指标、三情景倍数。第 1 节不会给出动作。")
        L.append("")
        if s.get("boards"):
            L.append(f"这只票挂的板块:{' · '.join(s['boards'][:15])}"
                     + (f" …(共 {len(s['boards'])} 个)" if len(s["boards"]) > 15 else ""))
            L.append("")
        L.append("**要补上这一节,先回答三个问题,再把答案写进 "
                 "`stock-analysis-workflow/scripts/sectors.py` 的 `SECTORS` 表:**")
        L.append("")
        L.append("1. 这条链的**需求源头**是什么(谁掏钱买它的产品)")
        L.append("2. 什么数据**领先那个需求 1~2 个季度**,从哪取")
        L.append("3. 哪些常用指标在这个行业上会**给出假信号**")
        L.append("")
        return

    spec = spec_of(name)
    L.append(f"# → **{name}**　·　性质 **{spec.get('nature', '?')}**"
             f"　·　本流程按 **{s['性质']}股**那一套算")
    L.append("")
    L.append(f"- **命中板块**:{' / '.join('`' + x + '`' for x in s['命中板块']) or '—'}"
             f"(这只票一共挂 {len(s['boards'])} 个板块)")
    if s["次选"]:
        L.append("- **它还沾着别的行业**:"
                 + "、".join(f"**{x['行业']}**({x['分']} 分:"
                             + " / ".join('`' + b + '`' for b in (x.get('命中') or [])[:3])
                             + (",**宽泛提示词命中,不是这只票真的做这门生意**"
                                if weak_match(x.get('命中') or []) else "")
                             + ")" for x in s["次选"])
                 + "。多行业命中很常见 —— 一只票能挂几十个板块,里面大量是概念标签。"
                   "**主判只是起点**:如果次选才是它真正的主业,该换那一套的尺。")
    if weak_match(s["命中板块"]):
        L.append(f"- ⚠ **这是弱匹配,不是精确命中。** 命中的 "
                 f"{' / '.join('`' + x + '`' for x in s['命中板块'])} 是宽泛的行业分类名 —— "
                 f"「专用设备」底下装着锂电设备、光伏设备、印刷机械、纺织机械,"
                 f"和半导体设备完全不是一回事。**这只票归到「{name}」是按它的主营推断的**"
                 f"(显示模组设备 + 半导体封测设备),不是板块名直接读出来的。"
                 f"所以下面这套尺的适用性本身要打折看:如果它的主营其实更靠近别的行业,"
                 f"整节都要换。")
    L.append(f"- **行业怎么判出来的**:每个命中的板块 +1 分,**排在前 3 位的板块 +3 分**"
             f"(东财返回的前几个通常是行业分类,后面才是概念标签),取分最高的那个。"
             f"板块列表来自 {s.get('文件', '缓存')},"
             f"抓于 {s.get('抓取时间', '?')[:19]} —— **分析时只读这份缓存,不联网**。")
    L.append("")
    L.append("### 这个行业该用哪把尺")
    L.append("")
    L.append("| | 用什么 | 为什么 |")
    L.append("|---|---|---|")
    L.append(f"| **估值(进决策)** | {r['估值尺'] and {'pe31': '3+1 PE 带', 'pb': 'PB 全历史分位', 'ps31+pb': '3+1 PS 带 + PB 全历史分位'}[r['估值尺']] or '**没有可用的尺**'} "
             f"| {r['估值尺说明']} |")
    for k, why in r["展示尺"]:
        lab = {"pe31": "3+1 PE 带", "pb": "PB 分位", "ps31": "3+1 PS 带"}.get(k, k)
        L.append(f"| {lab}(**只展示不打分**) | 算出来放在第 5 节 | {why} |")
    L.append(f"| **基准率(外部视角)** | {'适用' if r['基准率'] else '**不适用**'} "
             f"| {r['基准率不适用原因'] or '成长股的增速持续性可以用全市场同类公司的历史来校准'} |")
    L.append("")
    if r["基本面主指标"]:
        L.append("### 这个行业该盯什么(第 2、4、4.5 节逐条给数)")
        L.append("")
        L.append("| 指标 | 为什么看它 |")
        L.append("|---|---|")
        for k, why in r["基本面主指标"]:
            L.append(f"| {k} | {why} |")
        L.append("")
    L.append("### 这个行业上会给假信号的指标")
    L.append("")
    inv = spec.get("invalid") or []
    if inv:
        for x in inv:
            L.append(f"- {x}")
    else:
        L.append("- 这个行业没有登记「会给假信号」的常用指标。"
                 "**这不等于没有** —— 只是我们还没被它坑过。")
    L.append("")
    L.append("### 这个行业的领先指标(定义在 `sectors.py`,这里只读不复制)")
    L.append("")
    L.append("| 领先指标 | 领先多久 | 怎么取 | 现在能不能取到 |")
    L.append("|---|---|---|---|")
    for a, b, c, dd in spec.get("leading", []):
        L.append(f"| {a} | {b} | {c} | {dd} |")
    L.append("")
    L.append(f"**判断要点**:{spec.get('criteria', '')}")
    L.append("")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    a = ap.parse_args()
    for c in a.codes:
        s = load(Path(a.raw), c)
        L = []
        render(s, "0.5", L)
        print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
