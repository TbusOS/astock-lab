#!/usr/bin/env python3
"""单季还原 + 领先量 —— 只用公司自己报表里的事实,不含任何人的预测。

    quarterly.py <code> [--raw data/raw] [--since 2023] [--json out.json]

为什么要自己还原单季:
    A 股三大表报的是**累计数**(一季报=Q1,半年报=H1,三季报=Q1-3,年报=全年)。
    直接看累计数会把季节性和拐点全抹平 —— 2026H1 营收 209 亿看不出
    Q2 单季 125.7 亿比 Q1 的 83.4 亿跳了 51%。拐点只在单季数据里。

为什么盯存货和预付款:
    这两项是**钱已经花出去了**,是行为不是说法。对制造业,
    期末存货是下一两个季度出货的物质基础;预付款是抢上游产能的证据。
    两者都领先营收,而且是财报里的硬数,不需要相信任何人。

    ⚠ 这条关系**不是普适规律**,是每家公司自己的历史关系,必须逐只算出来看。
    周转天数一变它就失效 —— 所以脚本输出的是**历史比值的分布**,
    不是一个拍出来的系数。用的时候看分布宽不宽,宽就说明这条链在这只票上不可靠。

依赖:无(只读 fetch_all 落下来的 json)。
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# 利润表:要绝对值,不要比率
PL = ["营业总收入", "营业收入", "营业成本", "销售费用", "管理费用", "研发费用",
      "财务费用", "营业利润", "利润总额", "净利润", "归属于母公司所有者的净利润"]
# 资产负债表:时点数,不做差分
# 归母净资产:PB 路径的分母基数。**多种写法都认** —— 和资本开支那个坑同一类,
# 少写一个字就静默取不到,而 PB 路径会因此说不出「净资产一年长多少」。
EQUITY_KEYS = ("归属于母公司股东权益合计", "归属于母公司所有者权益合计",
               "归属于母公司股东的权益合计", "所有者权益(或股东权益)合计")
BS = ["存货", "预付款项", "合同负债", "应收票据及应收账款", "在建工程",
      "固定资产", "货币资金", "应付票据及应付账款", *EQUITY_KEYS]
# ⚠ 同一个科目在不同数据源上**叫法会差一个字**。新浪那条接口给的是
#   「购建固定资产、无形资产和其他长期资产**所**支付的现金」,少写这个「所」
#   就一个都取不到 —— 而且是静默的 None,不报错、不缺行,表照样打得出来,
#   只是那一列全是「—」。2026-09-02 实测:五只票 + 十只上游,`单季资本开支`
#   一直是 None,没人发现,因为在此之前没有任何一节用到它。
#   所以两种写法都列上,取到哪个算哪个。
CAPEX_KEYS = ("购建固定资产、无形资产和其他长期资产所支付的现金",
              "购建固定资产、无形资产和其他长期资产支付的现金")
CF = ["经营活动产生的现金流量净额", *CAPEX_KEYS]

QEND = {"0331": "Q1", "0630": "Q2", "0930": "Q3", "1231": "Q4"}
PREV = {"0630": "0331", "0930": "0630", "1231": "0930"}

Q_ORDER = ["Q1", "Q2", "Q3", "Q4"]


def qidx(period: str) -> int:
    """把 `2025Q3` 变成一个能做减法的序号,用来量两个季度之间隔多远。

    为什么需要它:判「几个点连不连续」不能比年份 —— 2025Q4 和 2026Q1 年份不同
    但只隔一个季度,2025Q1 和 2025Q4 年份相同却隔三个。年份和距离是两回事。

    **住在这里而不是 forecast.py**:三处要用它 —— 产业链位置判四个点连不连续、
    3+1 PE/PS 带取四季窗口、存货法取近四季比值。各写一份必然漂,
    而漂了不报错:窗口悄悄跨了五个季度,数字照样算得出来。
    """
    return int(period[:4]) * 4 + Q_ORDER.index(period[4:])


def gap_in(periods) -> str | None:
    """这一串季度是不是首尾相连。连续返回 None,有洞返回一句「缺了哪几季」。

    ⚠ **为什么必须查**:下游都是按**下标**取窗口(`xs[i-2:i+2]`、`ratios[-4:]`),
    而下标是「第几个有数的季度」,不是「第几个季度」。中间缺一季,
    四个下标就横跨五个季度 —— 分母多算了一整季,PE 因此偏低,
    而报告不会有任何提示。2026-09-02 审核用模拟数据复现过三次。
    """
    if len(periods) < 2:
        return None
    idx = [qidx(p) for p in periods]
    if idx[-1] - idx[0] + 1 == len(idx):
        return None
    have = set(idx)
    miss = [i for i in range(idx[0], idx[-1] + 1) if i not in have]
    names = [f"{i // 4}{Q_ORDER[i % 4]}" for i in miss]
    return (f"{periods[0]}~{periods[-1]} 之间缺 {'、'.join(names)},"
            f"这 {len(periods)} 个点实际横跨 {idx[-1] - idx[0] + 1} 个季度,窗口跨季")


def num(v):
    """报表里空值有好几种写法(''、'--'、None、'nan'),统一成 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def first_of(d: dict, keys):
    """按顺序取第一个不是 None 的字段。给「同一科目多种写法」用。"""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def load(raw: Path, code: str, name: str):
    """取最新一次抓到的那份。同名文件按日期排,取最后一个。"""
    files = sorted((raw / "financials" / code).glob(f"*-{name}.json"))
    for f in reversed(files):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for env in j.values():
            if env.get("ok") and env.get("data"):
                return env["data"], f.name, env.get("url", "")
    return None, None, ""


def by_period(rows, keys):
    """报告日 → {字段: 值}。报告日形如 20260630。"""
    out = {}
    for r in rows or []:
        d = str(r.get("报告日") or r.get("报告期") or "").strip()[:8]
        if len(d) != 8 or d[4:8] not in QEND:
            continue
        out[d] = {k: num(r.get(k)) for k in keys if k in r}
    return out


def to_single_quarter(cum: dict, keys) -> dict:
    """累计 → 单季。Q1 直接用;其余减去上一期累计。

    上一期缺失时**返回 None 而不是拿累计数顶替** —— 顶替出来的数看着正常
    (量级对、正负对),但它是半年数混在季度序列里,会把后面所有比值算歪。
    """
    out = {}
    for d in sorted(cum):
        q = d[4:8]
        if q == "0331":
            out[d] = dict(cum[d])
            continue
        pk = d[:4] + PREV[q]
        if pk not in cum:
            out[d] = {k: None for k in keys}
            continue
        out[d] = {k: (cum[d].get(k) - cum[pk].get(k))
                  if cum[d].get(k) is not None and cum[pk].get(k) is not None else None
                  for k in keys}
    return out


def build(raw: Path, code: str, since: str = "2023") -> dict:
    pl_rows, pl_file, pl_url = load(raw, code, "利润表")
    bs_rows, bs_file, bs_url = load(raw, code, "资产负债表")
    cf_rows, cf_file, cf_url = load(raw, code, "现金流量表")
    if not pl_rows or not bs_rows:
        raise SystemExit(f"{code}: 缺利润表或资产负债表。先跑\n"
                         f"  tools/fetch_all.py --codes {code} --group statements")

    pl_c, bs_p = by_period(pl_rows, PL), by_period(bs_rows, BS)
    cf_c = by_period(cf_rows, CF) if cf_rows else {}
    pl_q = to_single_quarter(pl_c, PL)
    cf_q = to_single_quarter(cf_c, CF) if cf_c else {}

    periods = sorted(d for d in pl_q if d >= f"{since}0101")
    rec = []
    for i, d in enumerate(periods):
        p, b = pl_q[d], bs_p.get(d, {})
        rev = p.get("营业总收入") or p.get("营业收入")
        cost = p.get("营业成本")
        exp = sum(x for x in (p.get("销售费用"), p.get("管理费用"),
                              p.get("研发费用")) if x is not None) or None
        nxt = None
        if i + 1 < len(periods):
            n = pl_q[periods[i + 1]]
            nxt = n.get("营业总收入") or n.get("营业收入")
        inv = b.get("存货")
        yoy_key = f"{int(d[:4]) - 1}{d[4:]}"
        rev_yoy = pl_q.get(yoy_key, {}).get("营业总收入") or pl_q.get(yoy_key, {}).get("营业收入")
        rec.append({
            "period": f"{d[:4]}{QEND[d[4:8]]}",
            "报告日": d,
            "单季营收": rev,
            "单季营收同比": (rev / rev_yoy - 1) * 100 if rev and rev_yoy else None,
            "单季毛利率": (1 - cost / rev) * 100 if rev and cost is not None else None,
            "三费率": exp / rev * 100 if rev and exp else None,
            "单季财务费用": p.get("财务费用"),
            "单季归母净利": p.get("归属于母公司所有者的净利润"),
            "期末存货": inv,
            "期末预付款": b.get("预付款项"),
            "期末合同负债": b.get("合同负债"),
            "期末应收": b.get("应收票据及应收账款"),
            "期末在建工程": b.get("在建工程"),
            "期末固定资产": b.get("固定资产"),
            "期末归母净资产": first_of(b, EQUITY_KEYS),
            "单季经营现金流": cf_q.get(d, {}).get("经营活动产生的现金流量净额"),
            "单季资本开支": first_of(cf_q.get(d, {}), CAPEX_KEYS),
            "下季营收÷期末存货": nxt / inv if nxt and inv else None,
        })

    ratios = [r["下季营收÷期末存货"] for r in rec if r["下季营收÷期末存货"]]
    recent = ratios[-4:] if len(ratios) >= 4 else ratios
    stats = None
    if len(recent) >= 2:
        stats = {
            "近四季比值": [round(x, 3) for x in recent],
            "低": round(min(recent), 3), "高": round(max(recent), 3),
            "中位": round(statistics.median(recent), 3),
            "离散度": round((max(recent) - min(recent)) / statistics.median(recent) * 100, 1),
            "全历史样本数": len(ratios),
        }
    return {
        "code": code,
        "since": since,
        "来源": {"利润表": {"文件": pl_file, "url": pl_url},
                 "资产负债表": {"文件": bs_file, "url": bs_url},
                 "现金流量表": {"文件": cf_file, "url": cf_url}},
        "quarters": rec,
        "存货领先关系": stats,
    }


def render(d: dict) -> str:
    """打成表。金额一律折算成亿元 —— 报表原值是元,读起来全是零。"""
    L = [f"### {d['code']} 单季实际(全部来自公司自己的报表)", ""]
    L.append("| 报告期 | 单季营收 | 同比 | 毛利率 | 三费率 | 财务费用 | 归母净利 | "
             "期末存货 | 预付款 | 合同负债 | 下季营收÷存货 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    def y(v, nd=1):
        return "—" if v is None else f"{v / 1e8:,.{nd}f}"

    def ratio(v):
        return "—" if v is None else f"{v:.2f}x"

    def p(v, nd=1):
        return "—" if v is None else f"{v:.{nd}f}%"

    for r in d["quarters"]:
        L.append(f"| {r['period']} | {y(r['单季营收'])} | {p(r['单季营收同比'])} "
                 f"| {p(r['单季毛利率'])} | {p(r['三费率'])} | {y(r['单季财务费用'], 2)} "
                 f"| {y(r['单季归母净利'])} | {y(r['期末存货'])} | {y(r['期末预付款'], 2)} "
                 f"| {y(r['期末合同负债'], 2)} "
                 f"| {ratio(r['下季营收÷期末存货'])} |")
    L.append("")
    L.append("金额单位:亿元。")
    s = d.get("存货领先关系")
    if s:
        L.append("")
        L.append(f"**存货→下季营收**:近四季比值 {s['近四季比值']},"
                 f"中位 {s['中位']}x,区间 {s['低']}~{s['高']}x,"
                 f"离散度 {s['离散度']}%(全历史 {s['全历史样本数']} 个样本)。")
        if s["离散度"] > 30:
            L.append("")
            L.append("> ⚠ 离散度超过 30%,这条关系在这只票上**不稳**,"
                     "不能拿来推下季营收。要么周转天数在变,要么业务结构在变。")
    return "\n".join(L)


def _yoy(cur, prev):
    """同比。上年同期缺失、为 0 或为负时返回 None ——
    **负基数的同比是个没有含义的数**(−1 亿 → +1 亿算 +200%?),
    印出来会被当成一个很强的增长。"""
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur / prev - 1) * 100


def cash_and_orders(d: dict) -> dict:
    """第 2.1 节要的几个数。**这些 `build()` 早就算了,只是一直没渲染。**

    ⚠ 2026-09-02 之前 `单季资本开支` 一直是 None —— 现金流量表里那个科目
    少写了一个「所」字,取不到值,而且是静默的:不报错、不缺行,
    表照样打得出来,只是那一列全是「—」。没人发现,因为在此之前没有一节用到它。
    **一个从来没被读过的字段,等于没有这个字段。**
    """
    qs = d["quarters"]
    if not qs:
        return {"ok": False, "why": "没有单季数据"}
    last = qs[-1]
    ocf4 = [r["单季经营现金流"] for r in qs if r.get("单季经营现金流") is not None][-4:]
    ni4 = [r["单季归母净利"] for r in qs if r.get("单季归母净利") is not None][-4:]
    conv = None
    if len(ocf4) == 4 and len(ni4) == 4 and sum(ni4) > 0:
        conv = sum(ocf4) / sum(ni4) * 100
    prev = {r["period"]: r for r in qs}
    y = f"{int(last['period'][:4]) - 1}{last['period'][4:]}"
    yr = prev.get(y, {})
    return {"ok": True, "期间": last["period"], "上年同期": y if yr else None,
            "经营现金流": last.get("单季经营现金流"),
            "资本开支": last.get("单季资本开支"),
            "现金转化率": conv,
            "现金转化率样本": len(ocf4),
            "合同负债": last.get("期末合同负债"),
            "合同负债同比": _yoy(last.get("期末合同负债"), yr.get("期末合同负债")),
            "预付款": last.get("期末预付款"),
            "预付款同比": _yoy(last.get("期末预付款"), yr.get("期末预付款")),
            "存货": last.get("期末存货"),
            "存货同比": _yoy(last.get("期末存货"), yr.get("期末存货")),
            "应收": last.get("期末应收"),
            "应收同比": _yoy(last.get("期末应收"), yr.get("期末应收")),
            "营收同比": last.get("单季营收同比")}


def render_cash_orders(c: dict, n: str, L: list) -> None:
    """第 2.1 节。**每一项配一句「为什么看它」** ——
    一张只有数字的表,读者只能选择相信;写清楚为什么看,他才能判断这个数够不够。"""
    L.append(f"### {n} 现金与订单")
    L.append("")
    if not c.get("ok"):
        L.append(f"算不出来:{c['why']}")
        L.append("")
        return
    L.append(f"上面那张表是**利润**,这一节是**现金和订单**。"
             f"两者经常打架 —— 利润是按会计规则算出来的,现金是真的收到没有,"
             f"订单是下一两季有没有活干。数字都是 {c['期间']} 单季或期末,"
             f"同比对的是 {c['上年同期'] or '上年同期(缺)'}。")
    L.append("")

    def y(v, nd=2):
        return "—" if v is None else f"{v / 1e8:,.{nd}f} 亿"

    def pc(v):
        return "—" if v is None else f"{v:+.1f}%"

    L.append("| 项目 | 值 | 同比 | 为什么看它 |")
    L.append("|---|---:|---:|---|")
    L.append(f"| 单季经营现金流 | {y(c['经营现金流'])} | — "
             f"| 利润是算出来的,现金是收到的。两者长期背离,多半是账面利润先于收款确认 |")
    conv_txt = "—" if c["现金转化率"] is None else f"{c['现金转化率']:.0f}%"
    L.append(f"| **现金转化率** | {conv_txt} "
             f"| — | 近四季经营现金流合计 ÷ 近四季归母净利合计。"
             f"**低于 100% 说明赚的是账面利润**,钱还在客户那儿;"
             f"持续低于 100% 要去看应收 |")
    L.append(f"| 单季资本开支 | {y(c['资本开支'])} | — "
             f"| 为了未来产能已经花出去的钱。它先于营收 —— "
             f"扩产的钱花了,货才有地方出 |")
    L.append(f"| 期末合同负债 | {y(c['合同负债'])} | {pc(c['合同负债同比'])} "
             f"| **已经收到的预收款 = 在手订单**。它领先营收 1~3 个季度,"
             f"而且是客户真的打了钱,不是意向 |")
    L.append(f"| 期末预付款 | {y(c['预付款'])} | {pc(c['预付款同比'])} "
             f"| 付给上游抢产能的钱。公司自己觉得后面要备料才会付 —— "
             f"是行为不是说法 |")
    L.append(f"| 期末存货 | {y(c['存货'], 1)} | {pc(c['存货同比'])} "
             f"| 下一两季出货的物质基础。⚠ 它有两面:备货是好事,卖不掉也是它 |")
    L.append(f"| 期末应收 | {y(c['应收'], 1)} | {pc(c['应收同比'])} "
             f"| 单独看没有意义,**要和营收同比比**(下一行) |")
    L.append(f"| 单季营收同比 | — | {pc(c['营收同比'])} "
             f"| **应收同比明显快于营收同比 = 靠放宽账期换来的增长**,"
             f"那种增长下一季会以坏账或收款放缓的形式还回来 |")
    L.append("")
    if c["应收同比"] is not None and c["营收同比"] is not None:
        gap = c["应收同比"] - c["营收同比"]
        L.append(f"**应收同比 {c['应收同比']:+.1f}% vs 营收同比 "
                 f"{c['营收同比']:+.1f}%,差 {gap:+.1f} 个百分点** —— "
                 + ("应收涨得明显比营收快,**这部分增长是赊出去的**,要盯下一季能不能收回来。"
                    if gap > 15 else
                    "应收涨得明显比营收慢,收款质量在改善。" if gap < -15 else
                    "两者大致同步,没有靠放宽账期换增长的迹象。"))
        L.append("")
    if c["现金转化率"] is not None:
        L.append(f"**现金转化率 {c['现金转化率']:.0f}%**"
                 f"(近 {c['现金转化率样本']} 个季度合计)—— "
                 + ("赚到的钱比账面利润还多,通常是预收款在增加或折旧大。"
                    if c["现金转化率"] >= 100 else
                    "**每赚 1 块钱账面利润,只收回 "
                    f"{c['现金转化率'] / 100:.2f} 块现金**。"
                    "差额压在应收和存货里 —— 那不是造假,是这门生意的账期结构,"
                    "但它决定了增长要不要不断补运营资金。"))
        L.append("")
    else:
        L.append("**现金转化率算不出来** —— 近四季的经营现金流或归母净利有缺,"
                 "或者四季净利合计不是正数(负基数下这个比率没有含义)。")
        L.append("")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--since", default="2023")
    ap.add_argument("--json", help="把结构化结果写到这个文件")
    a = ap.parse_args()

    all_out = {}
    for c in a.codes:
        d = build(Path(a.raw), c, a.since)
        all_out[c] = d
        print(render(d))
        print()
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(all_out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"→ {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
