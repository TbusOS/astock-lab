#!/usr/bin/env python3
"""外部研报汇总 —— 把多家机构的 filled.json 并排放成一份能核对的 markdown。

    research_summary.py [DIR] [--out summary.md] [--as-of YYYY-MM-DD]
                        [--holdings 300502.SZ:新易盛 300308.SZ:中际旭创 …]

为什么要并排:
    一家投行的目标价单看没有意义 —— 它是一组假设的输出。
    有用的是**分歧**:同一年同一家公司,A 给 EPS 22.99、B 给 18.4,
    差的那 25% 一定落在某个可以查的假设上(出货量?毛利率?价格?)。
    并排是把"该去查哪一条假设"变得肉眼可见的唯一办法。

为什么估值锚单开一节:
    2026-09-01 我们自己就错在这里 —— 拿过去十二个月 PE 的历史分位去乘未来的利润,
    而卖方用的是**前瞻 PE 的历史带**。同一只票 EV 差 45 个百分点。
    所以「机构用什么倍数、乘在哪一年的利润上」比目标价本身更值钱。

单位不一致时**拒绝并排**,不做自动换算:
    "Rmb mn" 和 "Rmb bn" 我能换,但 "Rmb mn (adj.)" 和 "Rmb mn" 我换不了 ——
    猜错的代价是一张看起来正常、数字全错 1000 倍的表,而且没人会发现。

依赖:无(只用标准库)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

# 数据源目录住在 data-sources 那个 skill 下,两个 skill 的 scripts 都进 sys.path
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent.parent / "data-sources" / "scripts"))

import source_catalog as catalog

DEFAULT_DIR = "private/extern_research"


# ── 读取 ──────────────────────────────────────────────────────────────
def load(root: Path) -> list[dict]:
    """读全部 filled.json。把目录名一起带上 —— 汇总里每个数字都要能指回一个目录。"""
    out = []
    for d in sorted(x for x in root.glob("*") if x.is_dir()):
        fp = d / "filled.json"
        if not fp.exists():
            continue
        try:
            j = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  ⚠ 跳过 {d.name}:filled.json 解析失败 {e}")
            continue
        j["_dir"] = d.name
        j["_path"] = str(d)
        out.append(j)
    return out


# 汇总要用到的最少字段。**没有这几样的不进表** ——
# 2026-09-02 踩到:批量抽取之后还没填的空壳照样被渲染,
# 出来一整页破折号,而且看起来像「这几家机构什么都没给」,
# 实际是「我们还没填」。两回事,不能长一个样。
MIN = [("source", "publisher"), ("source", "report_date"), ("call", "rating")]


def is_filled(j: dict) -> bool:
    return all(str((j.get(a) or {}).get(b) or "").strip() for a, b in MIN)


def bare(ticker: str) -> str:
    """300502.SZ / 300502.SZ / sz300502 → 300502。用于跨写法匹配。"""
    m = re.search(r"\d{6}", str(ticker or ""))
    return m.group(0) if m else str(ticker or "").strip()


# ── 数值格式 ──────────────────────────────────────────────────────────
def num(x, nd: int = 2) -> str:
    """格式化一个数。**只在有小数点时才去尾零** ——
    直接 rstrip("0") 会把整数的零一起啃掉:num(100) 曾返回 "1",
    而表里 100 和 1 都是合理数字,没有任何一处会报错。2026-09-02 实测踩到。"""
    if x is None or isinstance(x, bool):
        return "—"
    if not isinstance(x, (int, float)):
        return str(x)
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    t = f"{x:.{nd}f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def pct(x, nd: int = 1) -> str:
    return "—" if x is None else f"{x:.{nd}f}%"


def days_ago(date_str: str, as_of: dt.date) -> str:
    try:
        d = dt.date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return "—"
    n = (as_of - d).days
    return f"{n} 天前" if n >= 0 else f"{-n} 天后(日期有误?)"


# ── 各节 ──────────────────────────────────────────────────────────────
def sec_overview(reports: list[dict], as_of: dt.date, L: list[str]) -> None:
    tickers = sorted({bare(r.get("source", {}).get("ticker")) for r in reports})
    pubs = sorted({r.get("source", {}).get("publisher") or "?" for r in reports})
    dates = sorted(d for d in (r.get("source", {}).get("report_date") for r in reports) if d)

    L.append("## 1 这份汇总里有什么")
    L.append("")
    L.append(f"- **{len(reports)} 份**外部研报,来自 **{len(pubs)} 家**机构:{'、'.join(pubs)}")
    L.append(f"- 覆盖 **{len(tickers)} 只**标的:{'、'.join(tickers)}")
    if dates:
        L.append(f"- 报告日跨度:{dates[0]} ~ {dates[-1]}(最新一份距今 {days_ago(dates[-1], as_of)})")
    L.append("")
    if len(reports) == 1:
        L.append("> ⚠ **只有 1 份研报,本汇总无法给出机构间分歧。**"
                 "单家机构的目标价是一组假设的输出,没有第二家做对照时,"
                 "它只能当作「一种可能」,不能当作「市场共识」。"
                 "下面第 4 节的分歧表因此是空的 —— 那是数据缺,不是没有分歧。")
        L.append("")


def sec_calls(reports: list[dict], as_of: dt.date, L: list[str]) -> None:
    L.append("## 2 各家的结论并排")
    L.append("")
    L.append("| 机构 | 报告日 | 时效 | 标的 | 评级 | 目标价 | 报告日股价 | 隐含空间 | 期限 |")
    L.append("|---|---|---|---|---|---:|---:|---:|---|")
    for r in reports:
        s, c = r.get("source", {}), r.get("call", {})
        tp, px = c.get("target_price"), c.get("price_at_report")
        up = c.get("upside_pct")
        if up is None and isinstance(tp, (int, float)) and isinstance(px, (int, float)) and px:
            up = (tp / px - 1) * 100
        cur = c.get("currency") or ""
        tp_cell = f"{cur} {num(tp)}" if tp is not None else (
            c.get("target_price_note") or "—")
        L.append(f"| {s.get('publisher') or '—'} | {s.get('report_date') or '—'} "
                 f"| {days_ago(s.get('report_date'), as_of)} | {bare(s.get('ticker'))} "
                 f"| {c.get('rating') or '—'} | {tp_cell} | {cur} {num(px)} "
                 f"| {pct(up)} | {c.get('horizon_months') or '—'} 个月 |")
    L.append("")
    L.append("> 「隐含空间」是**报告日**那天的空间,不是今天的。"
             "股价一动它就过期 —— 要看今天的空间,用目标价除以今天收盘价自己算。")
    L.append("")


def sec_anchor(reports: list[dict], L: list[str]) -> None:
    L.append("## 3 估值锚对照 ★ 这一节比目标价重要")
    L.append("")
    L.append("目标价 = **倍数 × 某一年的每股利润**。两个输入都可能错,"
             "而**倍数配错利润**的错法最隐蔽:数字看起来完全正常,方向却系统性偏乐观。")
    L.append("")
    L.append("| 机构 | 估值方法 | 目标倍数 | 乘在哪一年 | 倍数依据 | 历史带 −1σ / 中枢 / +1σ | 带的出处 |")
    L.append("|---|---|---:|---|---|---|---|")
    for r in reports:
        s, va = r.get("source", {}), r.get("valuation_anchor", {})
        b = va.get("band") or {}
        band = " / ".join(num(b.get(k)) for k in ("low", "mid", "high"))
        pg = va.get("band_source_page")
        L.append(f"| {s.get('publisher') or '—'} | {va.get('method') or '—'} "
                 f"| {num(va.get('multiple'))}x | {va.get('base_year') or '—'} "
                 f"| {(va.get('multiple_basis') or '—').replace('|', '/')} "
                 f"| {band} | {('第 ' + str(pg) + ' 页') if pg else '未填'} |")
    L.append("")
    L.append("**怎么用这一节**:把「目标倍数」和「乘在哪一年」抄下来,"
             "去对我们自己三情景里用的乘数 —— **两者必须算的是同一段时间**:"
             "用未来那一年的利润,就要配未来那一年的倍数;拿过去十二个月的倍数"
             "去乘未来的利润,同一段增长会被算两遍,三情景的价位全部作废。")
    L.append("")


def implied_shares(r: dict) -> float | None:
    """从 净利 ÷ EPS 反推股本(单位随净利的单位走,这里只用来比大小)。

    为什么需要它:**EPS 跨送转不可比**。同一只票同一年,一份研报写 EPS 13.86、
    另一份写 27.18,差近 2 倍 —— 那不是分歧,是中间做了 1 送 1,股本翻倍。
    直接并排会造出一个不存在的分歧,而且看起来完全像真的。
    净利和营收不受股本影响,那两张表才是真能比的。
    """
    vals = [r_["net_income"] / r_["eps"]
            for r_ in r.get("forecast", [])
            if isinstance(r_.get("net_income"), (int, float))
            and isinstance(r_.get("eps"), (int, float)) and r_.get("eps")]
    return sum(vals) / len(vals) if vals else None


def shares_comparable(rs: list[dict], tol: float = 0.05) -> bool:
    """各家隐含股本是否落在同一档(默认 5% 容差)。差太多就是股本口不同,EPS 不能并排。"""
    v = [x for x in (implied_shares(r) for r in rs) if x]
    return len(v) < 2 or (max(v) - min(v)) / min(v) <= tol


def _unit(r: dict, key: str) -> str:
    return (r.get("units") or {}).get(key) or "未声明"


def auto_nd(vals: list[float]) -> int:
    """按这一列的量级定小数位。**写死位数会在换单位时毁掉精度** ——
    净利写 Rmb mn 是五位数(0 位刚好),写 Rmb bn 就是个位数(0 位把 9.53 舍成 10)。
    位数跟着数据走,同一张表才既不啰嗦也不丢信息。"""
    m = max((abs(v) for v in vals), default=0)
    return 0 if m >= 1000 else (1 if m >= 100 else 2)


def _fc_table(group: list[dict], key: str, label: str, L: list[str],
              comparable: bool = True) -> None:
    """一张指标表:行=年份,列=机构。单位不一致就分开出表,不做换算。

    comparable=False 时照样把各家的数列出来(那是原始信息,该看),
    但**不算极差** —— 算了就等于宣称"这两个数在比同一件事",而它们不是。
    """
    by_unit: dict[str, list[dict]] = {}
    for r in group:
        by_unit.setdefault(_unit(r, key), []).append(r)

    if len(by_unit) > 1:
        L.append(f"**{label}** —— ⚠ 各家单位不一致({'、'.join(by_unit)}),"
                 f"**不并排**,分开列。要比大小请先自己换算,并确认两边算的是同一件事。")
        L.append("")

    for unit, rs in by_unit.items():
        years = sorted({str(row.get("year", "")).strip()
                        for r in rs for row in r.get("forecast", []) if row.get("year")})
        if not years:
            continue
        pubs = [r.get("source", {}).get("publisher") or "?" for r in rs]
        head = f"**{label}**(单位:{unit})" if len(by_unit) == 1 else f"单位:{unit}"
        L.append(head)
        L.append("")
        # 先把整张表的数收齐,才能定小数位 —— 位数是**列级**属性,不是单元格级
        grid = {(y, i): next((row.get(key) for row in r.get("forecast", [])
                              if str(row.get("year", "")).strip() == y), None)
                for y in years for i, r in enumerate(rs)}
        nd = auto_nd([v for v in grid.values()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)])

        spread_col = len(rs) > 1 and comparable
        L.append("| 年份 | " + " | ".join(pubs) + (" | 极差 | 极差÷最低 |" if spread_col else " |"))
        L.append("|---|" + "---:|" * len(pubs) + ("---:|---:|" if spread_col else ""))
        for y in years:
            vals, cells = [], []
            for i, r in enumerate(rs):
                v = grid[(y, i)]
                cells.append(num(v, nd))
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(v)
            tail = ""
            if spread_col:
                if len(vals) >= 2 and min(vals):
                    spread = max(vals) - min(vals)
                    tail = f" {num(spread, nd)} | {spread / abs(min(vals)) * 100:.1f}% |"
                else:
                    tail = " — | — |"
            L.append(f"| {y} | " + " | ".join(cells) + " |" + tail)
        L.append("")


def sec_forecast(reports: list[dict], L: list[str]) -> None:
    L.append("## 4 分年度预测:分歧落在哪一年")
    L.append("")
    groups: dict[str, list[dict]] = {}
    for r in reports:
        groups.setdefault(bare(r.get("source", {}).get("ticker")), []).append(r)

    for tk, group in sorted(groups.items()):
        L.append(f"### {tk}")
        L.append("")
        if len(group) == 1:
            L.append(f"只有 **{group[0].get('source', {}).get('publisher')}** 一家覆盖,"
                     "没有第二家可比 —— 下表是这一家的预测原样,**不构成共识**。")
            L.append("")
        cmp_eps = shares_comparable(group)
        if len(group) > 1 and not cmp_eps:
            sh = [(r.get("source", {}).get("publisher") or "?", implied_shares(r))
                  for r in group]
            L.append("> ⚠ **各家的 EPS 不能直接比。** 用「净利 ÷ EPS」反推出来的股本对不上:"
                     + "、".join(f"{n} ≈ {num(v)}" for n, v in sh if v)
                     + "。中间做过送转或增发,同一年的每股利润被摊薄到不同的股本上 —— "
                       "并排看着像 2 倍分歧,其实是股本变了。**下面 EPS 表照列但不算极差**;"
                       "要比预测请看归母净利和营业收入那两张,它们不受股本影响。")
            L.append("")
        _fc_table(group, "eps", "每股收益 EPS", L, comparable=cmp_eps)
        _fc_table(group, "net_income", "归母净利", L)
        _fc_table(group, "revenue", "营业收入", L)
        if len(group) > 1:
            L.append("> 极差大的那一年就是要去查的地方 —— 差异一定落在某条可查的假设上"
                     "(出货量 / 单价 / 毛利率 / 费用率),不会凭空产生。")
            L.append("")


def sec_detail(reports: list[dict], as_of: dt.date, L: list[str]) -> None:
    L.append("## 5 逐份详情")
    L.append("")
    for r in reports:
        s, c = r.get("source", {}), r.get("call", {})
        L.append(f"### {s.get('publisher') or '?'} · {s.get('report_date') or '?'} "
                 f"· {bare(s.get('ticker'))}")
        L.append("")
        an = "、".join(s.get("analysts") or []) or "—"
        L.append(f"- **分析师**:{an}")
        L.append(f"- **结论**:{c.get('rating') or '—'},目标价 {c.get('currency') or ''} "
                 f"{num(c.get('target_price'))}({c.get('horizon_months') or '?'} 个月),"
                 f"{days_ago(s.get('report_date'), as_of)}")
        rev = r.get("revisions") or {}
        if rev.get("note"):
            L.append(f"- **本次修订**:{rev['note']}")
        if rev.get("by_year"):
            L.append("- **各年调整幅度**:" + "、".join(
                f"{k} {v:+}%" if isinstance(v, (int, float)) else f"{k} {v}"
                for k, v in rev["by_year"].items()))
        if r.get("company_guidance"):
            L.append(f"- **公司自己的指引**:{r['company_guidance']}")
        for k in r.get("key_risks") or []:
            L.append(f"- **风险**:{k}")
        for n in r.get("notes") or []:
            L.append(f"- {n}")
        # provenance 里常含研报原标题(别人写的),里面可能有我们自己禁用的词。
        # 用行内代码包起来 —— 检查会跳过行内代码,而原文一个字不改。
        prov = s.get("provenance") or "⚠ 未填 —— 外部研报必须记来源"
        L.append(f"- **来源**:`{prov}`" if s.get("provenance") else f"- **来源**:{prov}")
        cd = s.get("conflict_disclosure")
        L.append(f"- **利益冲突**:{cd or '⚠ 未填'}")
        if cd:
            L.append("  - 投行与标的有业务关系时,评级和目标价不是中立的第三方意见。"
                     "这不代表数据是假的,但**结论要打折看,预测表可以照用**。")
        L.append(f"- **原始材料**:`{r['_path']}/`(text.txt · tables.json · pages/*.png)")
        L.append("")


def sec_gap(reports: list[dict], holdings: dict[str, str], L: list[str]) -> None:
    if not holdings:
        return
    have = {bare(r.get("source", {}).get("ticker")) for r in reports}
    L.append("## 6 覆盖缺口:我们持有但一份外部研报都没有的")
    L.append("")
    L.append("| 标的 | 名称 | 外部研报 |")
    L.append("|---|---|---|")
    miss = 0
    for tk, name in sorted(holdings.items()):
        n = sum(1 for r in reports if bare(r.get("source", {}).get("ticker")) == bare(tk))
        if not n:
            miss += 1
        L.append(f"| {bare(tk)} | {name} | {'✓ ' + str(n) + ' 份' if n else '**无**'} |")
    L.append("")
    if miss:
        L.append(f"**{miss} 只没有任何外部研报。** 这几只的盈利预测目前只有我们自己一套,"
                 "没有第二套预测可以交叉验证 —— 结论的不确定性比有覆盖的那几只**高一档**,"
                 "写进报告时要标出来。")
        L.append("")
        L.append("补法:拿到 PDF 后丢进目录再跑一遍 —— "
                 "`tools/ingest_report.py batch <放 pdf 的目录>`。")
    else:
        L.append("每只都有外部研报覆盖。")
    L.append("")


def sec_how(root: Path, L: list[str]) -> None:
    L.append("## 7 这些数怎么来的 / 去哪核对 / 怎么重跑")
    L.append("")
    L.append("**怎么来的** —— 三步,机器和人的分工是固定的:")
    L.append("")
    L.append("| 步 | 谁做 | 做什么 | 命令 |")
    L.append("|---|---|---|---|")
    L.append("| 1 抽取 | 脚本 | 文本按坐标重建版面、抽表格候选、**整页渲成 PNG** | "
             "`tools/ingest_report.py batch <pdf 目录>` |")
    L.append("| 2 填数 | 人/模型 | 逐页看 PNG,把数填进 `filled.json` | 看 `pages/*.png` |")
    L.append("| 3 校验 | 脚本 | 9 道算术检查,抓转录错 | "
             "`tools/ingest_report.py validate <目录>/filled.json` |")
    L.append("")
    L.append("**第 2 步为什么不能交给正则**:各家投行版式完全不同,"
             "正则在没见过的版式上不是报错,而是**抽出错的数** —— 抽错了不会有任何提示,比抽不到糟得多。")
    L.append("")
    L.append("**第 1 步为什么一定要渲染每一页**:2026-09-01 实测,高盛那份研报"
             "最关键的三个数(前瞻 PE 带 17x / 28x / 40x)**只存在于一张图里**,"
             "文本层一个都没有。只跑文本提取会得到一份看起来完整、其实缺了估值锚的输入。")
    L.append("")
    L.append("**去哪核对** —— 表里每个数都能指回一个具体位置:")
    L.append("")
    L.append(f"- 结构化值:`{root}/<报告目录>/filled.json`")
    L.append(f"- 原文:同目录 `text.txt`(按坐标重建版面,可直接和 PDF 对行)")
    L.append(f"- 图里的数:同目录 `pages/pNN.png`,页码见第 3 节「带的出处」列")
    L.append(f"- 原始 PDF 指纹:同目录 `meta.json` 的 `sha256` —— 换了文件这个值就变")
    L.append("")
    L.append("**怎么重跑**:")
    L.append("")
    L.append("```bash")
    L.append("cd ~/astock-lab-private                        # 真实数据在私有仓")
    L.append("tools/ingest_report.py batch <放 pdf 的目录>    # 抽取(已抽过且内容没变的自动跳过)")
    L.append("tools/ingest_report.py pending                 # 看还差哪几份没填")
    L.append("tools/ingest_report.py validate private/extern_research/*/filled.json")
    L.append("tools/research_summary.py --out private/reports/外部研报汇总.md")
    L.append("```")
    L.append("")
    L.append(catalog.markdown(["人工投喂", "★ 别人的预测"], None,
                              heading=None).lstrip("\n"))
    L.append("**研报本身从哪来** —— 说实话:海外投行研报是订阅制,"
             "我们**没有**自动获取渠道(GitHub 上没有可用工具;智通/格隆汇/华盛通这类"
             "聚合站全是 JS 渲染且只覆盖港股)。目前靠人工投喂 PDF。"
             "研报里的**信息**有免费替代来源(云厂电话会纪要、SEC 8-K/10-Q、"
             "台湾光通信月营收、LightCounting 免费 newsletter),那是另一件事,"
             "见 `docs/DATA-SOURCES.md`。")
    L.append("")


# ── 主流程 ────────────────────────────────────────────────────────────
def build(root: Path, as_of: dt.date, holdings: dict[str, str]) -> str:
    reports = load(root)
    if not reports:
        raise SystemExit(
            f"{root} 下没有任何 filled.json。\n"
            f"先跑:tools/ingest_report.py batch <放 pdf 的目录>")
    pending = [r for r in reports if not is_filled(r)]
    reports = [r for r in reports if is_filled(r)]
    if not reports:
        raise SystemExit(
            f"{root} 下有 {len(pending)} 份抽取过但**还没填**的研报,一份填好的都没有。\n"
            f"先看 pages/*.png 填 filled.json,再跑:\n"
            f"  tools/ingest_report.py pending {root}")
    reports.sort(key=lambda r: (bare(r.get("source", {}).get("ticker")),
                                r.get("source", {}).get("report_date") or ""))

    L: list[str] = []
    L.append("# 外部研报汇总")
    L.append("")
    L.append(f"生成于 {as_of.isoformat()} · 共 {len(reports)} 份 · 源目录 `{root}`")
    L.append("")
    sec_overview(reports, as_of, L)
    if pending:
        L.append(f"> ⚠ 另有 **{len(pending)} 份已抽取但还没填**的研报,"
                 f"**没有进入下面任何一张表** —— 空壳渲染出来是一排破折号,"
                 f"看着像「这几家什么都没给」,实际是「我们还没填」,两回事。")
        L.append("")
        L.append("| 目录 | 页数 | 待填 |")
        L.append("|---|---:|---|")
        for r in pending:
            meta = {}
            mf = Path(r["_path"]) / "meta.json"
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            L.append(f"| `{r['_dir']}` | {meta.get('pages', '?')} "
                     f"| 看 `{r['_path']}/pages/*.png` 填 `filled.json` |")
        L.append("")
        L.append(f"跑 `tools/ingest_report.py pending {root}` 看每份还缺哪些字段。")
        L.append("")
    sec_calls(reports, as_of, L)
    sec_anchor(reports, L)
    sec_forecast(reports, L)
    sec_detail(reports, as_of, L)
    sec_gap(reports, holdings, L)
    sec_how(root, L)
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", default=DEFAULT_DIR)
    ap.add_argument("--out", help="输出 markdown 路径;不给就打到 stdout")
    ap.add_argument("--as-of", help="以哪天为准算时效,默认今天")
    ap.add_argument("--holdings", nargs="*", default=[],
                    help="持仓,形如 300502.SZ:新易盛 —— 给了才出「覆盖缺口」一节")
    a = ap.parse_args()

    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today()
    holdings = {}
    for item in a.holdings:
        tk, _, name = item.partition(":")
        holdings[tk.strip()] = name.strip() or tk.strip()

    md = build(Path(a.dir), as_of, holdings)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
