#!/usr/bin/env python3
"""五只并排 —— 单看一只看不出相对贵贱,并排才能排序。

    portfolio.py 300502:新易盛 300308:中际旭创 … [--raw data/raw] [--out 组合.md]

为什么要并排:
    「现价对应前瞻 PE 36.3x」单看是个中性的数。放到这只票**自己**的历史带里
    (p50=19.8x)才知道它贵;再和另外四只的「相对自己历史的贵贱」比,
    才知道该先减哪一只。绝对 PE 跨行业比是没有意义的 ——
    油服和光模块的合理倍数本来就不在一个量级。

排序用什么:
    现价前瞻 PE ÷ 这只票自己历史前瞻 PE 的 p50。
    这个比值消掉了行业差异,回答的是「相对它自己,现在贵了几倍」。

依赖:无。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quarterly
import valuation


def yi(v, nd=1):
    return "—" if v is None else f"{v / 1e8:,.{nd}f}"


def one(code: str, name: str, raw: Path):
    v = valuation.build(code, raw, None)
    f = v["预测"]
    d = quarterly.build(raw, code, since="2023")
    hist = [r["单季归母净利"] for r in d["quarters"] if r.get("单季归母净利") is not None]
    our = v["我们预测的未来四季净利"]
    b, spot = v["前瞻PE带"], v["现价"]
    if not (our and spot):
        return None
    cap = spot["close"] * v["总股本"]
    pe = cap / our["mid"] if our["mid"] else None
    p50 = b["p50"] if b.get("ok") else None
    prev4 = sum(hist[-7:-3]) if len(hist) >= 7 else None
    return {
        "code": code, "name": name, "价": spot["close"], "日": spot["date"],
        "市值": cap, "预测净利": our, "前瞻PE": pe, "带": b,
        "相对自己历史": pe / p50 if pe and p50 else None,
        "净利同比": (our["mid"] / prev4 - 1) * 100 if prev4 else None,
        "方法数": f["可用方法数"], "预测期": f["预测期"],
        "产业链": f.get("产业链位置"), "上游说明": f.get("上游说明"),
        "预测原文": f,
        "单季": d["quarters"],
    }


def build(pairs, raw: Path) -> str:
    rows = [r for r in (one(c, n, raw) for c, n in pairs) if r]
    rows.sort(key=lambda r: -(r["相对自己历史"] or 0))

    L = ["# 五只持仓 · 并排对照", ""]
    L.append(f"生成于 {date.today().isoformat()} · 收盘价截至 "
             f"{max(r['日'] for r in rows)}")
    L.append("")
    L.append("> 全部结论建在公司自己披露的报表和 SEC 申报原值上,"
             "**不引用任何机构的盈利预测**。每只票的完整推导见各自那份报告。")
    L.append("")

    L.append("## 1 按「相对自己历史贵多少」排序")
    L.append("")
    L.append("绝对 PE 跨行业比没有意义 —— 油服和光模块的合理倍数本来就不在一个量级。"
             "所以这里用**现价前瞻 PE ÷ 这只票自己历史前瞻 PE 的 p50**,"
             "消掉行业差异,回答「相对它自己,现在贵了几倍」。")
    L.append("")
    L.append("| 排 | 代码 | 名称 | 现价 | 市值(亿) | 我们预测的未来四季净利(亿) "
             "| 现价前瞻PE | 自己历史p50 | **相对自己** | 预测把握 |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for i, r in enumerate(rows, 1):
        b = r["带"]
        rel = r["相对自己历史"]
        L.append(f"| {i} | {r['code']} | {r['name']} | {r['价']:.2f} | {yi(r['市值'], 0)} "
                 f"| {yi(r['预测净利']['low'])} ~ {yi(r['预测净利']['high'])} "
                 f"| {r['前瞻PE']:.1f}x | "
                 + (f"{b['p50']:.1f}x" if b.get("ok") else "—") + " | "
                 + (f"**{rel:.2f}x**" if rel else "—") + " | "
                 + (f"{r['方法数']}/3 个方法" if r["方法数"] else "**0/3,推不出**") + " |")
    L.append("")
    L.append("「预测把握」= 三个方法里有几个通过了自己的适用性检查。"
             "只有 1 个的,区间没有第二个来交叉验证,要打折看。")
    L.append("")

    L.append("## 2 每只在产业链里的位置")
    L.append("")
    L.append("| 代码 | 名称 | 上游是谁 | 期间 | 增速倍数变化 | 判断 |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        cp = r["产业链"]
        if cp:
            L.append(f"| {r['code']} | {r['name']} | {cp['上游']} | {cp['期间']} "
                     f"| {cp['起点']:.2f}x → {cp['终点']:.2f}x | {cp['判断']} |")
        else:
            L.append(f"| {r['code']} | {r['name']} | **没有对应数据源** | — | — "
                     f"| 算不了 —— 拿别的行业的开支当分母,算得出数字但没有含义 |")
    L.append("")
    L.append("> 倍数 >1 = 增速快于上游总开支(在得份额);倍数在降 = 这个优势在收窄。"
             "**水平和趋势是两件事**,可以同时成立。"
             "这个倍数本身不稳(历史离散度 70% 以上),**不能拿来推营收**,只看方向。")
    L.append("")

    L.append("## 3 最近两个季度的实际经营(全部是公司自己报的数)")
    L.append("")
    L.append("| 代码 | 名称 | 上季营收(亿) | 同比 | 毛利率 | 期末存货(亿) | 预付款(亿) | 财务费用(亿) |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        q = r["单季"][-1]
        L.append(f"| {r['code']} | {r['name']} | {yi(q['单季营收'])} "
                 + (f"| {q['单季营收同比']:.1f}% " if q['单季营收同比'] is not None else "| — ")
                 + (f"| {q['单季毛利率']:.1f}% " if q['单季毛利率'] is not None else "| — ")
                 + f"| {yi(q['期末存货'])} | {yi(q['期末预付款'], 2)} "
                 + f"| {yi(q['单季财务费用'], 2)} |")
    L.append("")
    L.append("> **预付款**值得单独看:它是抢上游产能、提前锁料的证据,"
             "是已经花出去的钱,不是任何人的说法。"
             "**财务费用**主要是汇兑,和生意好坏无关,但能吃掉几个点的净利率。")
    L.append("")

    L.append("## 4 这批预测已经存档,等季报验")
    L.append("")
    L.append("| 代码 | 名称 | 预测期 | 单季营收(亿) | 单季归母净利(亿) | 用了哪些方法 |")
    L.append("|---|---|---|---:|---:|---|")
    for r in rows:
        f_ = r["预测原文"]
        rev, ni = f_.get("营收区间"), f_.get("归母净利区间")
        ok = "、".join(m["name"] for m in f_["methods"] if m.get("ok")) or "**没有方法适用**"
        L.append(f"| {r['code']} | {r['name']} | {r['预测期']} | "
                 + (f"{yi(rev['low'])} ~ {yi(rev['high'])} " if rev else "— ")
                 + (f"| {yi(ni['low'])} ~ {yi(ni['high'])} " if ni else "| — ")
                 + f"| {ok} |")
    L.append("")
    L.append("存档文件:`private/predictions/<代码>-" + rows[0]["预测期"] + ".json`,"
             "里面记着每个方法当时给的区间、依据和失效条件。")
    L.append("")
    L.append("**存档后不覆盖** —— 预测存档后改它就等于没预测过。"
             "季报出来后对比实际值、记录误差。几个季度之后就知道哪条推法靠谱、"
             "哪条是自己臆想的。没有这个记录,预测准不准永远说不清。")
    L.append("")

    L.append("## 5 这份对照不做的事")
    L.append("")
    L.append("- **不给买卖建议。** 它只把公开事实推到一个可以被证伪的结论上。")
    L.append("- **不引用机构的盈利预测作为输入。** 各只报告的第 6 节有市场怎么看,"
             "那一节的数据没有进入前五节任何一步。")
    L.append("- **不用新闻和市场情绪。** 没有一个数来自新闻。")
    L.append("- **不做仓位建议。** 排序回答的是「相对它自己现在贵不贵」,"
             "不是「该买多少」—— 后者取决于你的资金、期限和承受能力,不在数据里。")
    L.append("")

    L.append("## 6 数据来源")
    L.append("")
    L.append("| 这份对照里的 | 来源 | 是事实还是预测 | 落在哪 | 怎么重跑 |")
    L.append("|---|---|---|---|---|")
    L.append("| 单季营收 / 毛利率 / 存货 / 预付款 / 财务费用 | 新浪转录的公司三大表"
             "(利润表 83 列、资产负债表 147 列) | **事实**(公司自己申报) "
             "| `data/raw/financials/<代码>/*-利润表.json` 等 "
             "| `tools/fetch_all.py --codes <代码> --group statements` |")
    L.append("| 收盘价 / 前复权序列 | baostock 日线 | **事实** "
             "| `data/raw/quotes/<代码>/*-qfq.json` "
             "| `tools/fetch_all.py --codes <代码> --group quotes` |")
    L.append("| 总股本 | baostock `query_profit_data` | **事实** "
             "| `data/raw/financials/<代码>/` "
             "| `tools/fetch_all.py --codes <代码> --group financials` |")
    L.append("| 北美四大云厂季度资本开支 | **SEC XBRL** `companyconcept`,10-Q/10-K 申报原值 "
             "| **事实** | `data/raw/overseas_facts/*-capex-cloud.json` "
             "| `tools/sec_facts.py capex --group cloud` |")
    L.append("| 五家国际油气公司季度资本开支 | 同上 | **事实** "
             "| `data/raw/overseas_facts/*-capex-oilgas.json` "
             "| `tools/sec_facts.py capex --group oilgas` |")
    L.append("| 一致预期 / 评级(**只在各只报告的第 6 节**,没进这份对照) "
             "| 同花顺、巨潮 | **别人的预测** | `data/raw/consensus/`、`data/raw/ratings/` "
             "| `tools/fetch_all.py --codes <代码> --group consensus,ratings` |")
    L.append("")
    L.append("整份对照重跑:")
    L.append("")
    L.append("```bash")
    L.append("cd ~/astock-lab-private")
    L.append("tools/portfolio.py " + " ".join(f"{r['code']}:{r['name']}" for r in rows)
             + " \\")
    L.append("    --out private/reports/<日期>/00-五只并排对照.md")
    L.append("```")
    L.append("")
    L.append("**已知缺口**:深科达的上游(面板厂、封测厂的资本开支)没有对应数据源 —— "
             "京东方、TCL 华星、长电这些公司不在 SEC 申报,要另找渠道。"
             "在补上之前,那只票的产业链位置这一节留空,不用别的行业的开支凑数。")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="+", help="形如 300502:新易盛")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--out")
    a = ap.parse_args()
    pairs = []
    for x in a.pairs:
        c, _, n = x.partition(":")
        pairs.append((c.strip(), n.strip() or c.strip()))
    md = build(pairs, Path(a.raw))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"→ {a.out}  ({len(md.splitlines())} 行)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
