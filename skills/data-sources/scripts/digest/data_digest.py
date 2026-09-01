#!/usr/bin/env python3
"""data_digest —— 把 fetch_all 抓下来的数据渲染成**给人读的**数据源明细文档。

    data_digest.py --code 300502 [--raw data/raw] [--out private/data-digest]
    data_digest.py --overseas                       # 海外上下游 + 宏观
    data_digest.py --code 300502 --pdf              # 顺带出 PDF

JSON 是给机器的,这份是给人的:一只股票当前手上**到底有哪些数据、来自哪个源、
抓于何时、内容是什么、还缺什么**,一页看完。可以直接发给别人。

设计上的两条硬规矩:

1. **抓失败的条目照样列出来,标红。** 静默丢掉失败项会让读的人以为这类数据
   根本不存在,而事实是「今天没抓到」。这两件事对决策的含义完全不同。
2. **每条都带 fetched_at。** 股东户数截止 6-30 和实时价并排放而不标日期,
   读的人会当成同一时点的事实 —— 2026-09-01 的报告就是这么错的。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

# 类别 → (中文名, 领先/同步/滞后, 一句话说明它回答什么问题)
GROUP_META = {
    "forecast":      ("业绩预告 / 快报", "领先", "公司自己在正式财报前 2-6 周给的净利区间。**0 条也是信息**:说明变动未超 ±50%"),
    "surveys":       ("机构调研", "领先", "谁来问、来了多少家、什么形式、什么时间。异常时点(周末夜间)和家数骤变是信号"),
    "announcements": ("公告", "领先", "中标 / 重大合同 / 框架协议 / 订单 —— 对订单驱动的公司比财报更早"),
    "consensus":     ("券商一致预期", "领先", "分年度净利/EPS 的区间与机构数。**两套源都抓**:同花顺只有境内,Yahoo 池里含外资行"),
    "ratings":       ("评级变动", "领先", "调高/调低的方向比目标价绝对值有用。含外资合资券商(野村东方国际/汇丰前海)"),
    "research":      ("券商研报", "领先", "带产业量价、订单、产能这些别处没有的句子;PDF 原件也存,抽错了能回去核"),
    "quotes":        ("行情与估值历史", "同步", "日线 + peTTM/pbMRQ —— 历史分位的唯一来源;复权因子保证目标价与日线可比"),
    "chips":         ("筹码与杠杆", "同步", "股东户数 / 资金流 / 解禁"),
    "financials":    ("财务三表 + 杜邦 + 偿债 + 营运", "滞后", "后视镜。用来验证领先指标有没有兑现,不用来预测"),
}
LEAD_MARK = {"领先": "🟢 领先", "同步": "🟡 同步", "滞后": "⚪ 滞后"}


def load(raw: Path, group: str, key: str, day: str | None) -> list[tuple[str, dict]]:
    """返回 [(文件名, 信封 dict)]。day=None 取该目录下最新的一天。"""
    d = raw / group / key
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"))
    if not files:
        return []
    if day:
        files = [f for f in files if f.name.startswith(day)] or files
    latest = max(f.name[:10] for f in files)
    out = []
    for f in files:
        if not f.name.startswith(latest):
            continue
        try:
            for src, e in json.loads(f.read_text(encoding="utf-8")).items():
                out.append((f.name, e))
        except Exception as ex:
            out.append((f.name, {"source": f.name, "ok": False, "error": repr(ex)}))
    return out


def mark_runs(envs: list[dict], gap_min: int = 10) -> None:
    """按 fetched_at 的时间空档把条目切成「轮次」,只有**最新一轮**不算旧记录。
    gap_min 分钟以上的空档视为换了一轮 —— 一轮抓取内部各源相差通常在几分钟内,
    而两次跑之间(改代码、重跑)一般隔得更久。"""
    import datetime as _dt
    ts = []
    for e in envs:
        try:
            ts.append(_dt.datetime.fromisoformat(e.get("fetched_at")))
        except Exception:
            ts.append(None)
    known = sorted(t for t in ts if t)
    if not known:
        for e in envs:
            e["_stale"] = False
        return
    cut = known[-1]                       # 从最新往回走,遇到大空档就断
    for a, b in zip(reversed(known[:-1]), reversed(known[1:])):
        if (b - a).total_seconds() > gap_min * 60:
            cut = b
            break
        cut = a
    for e, t in zip(envs, ts):
        e["_stale"] = bool(t and t < cut)


def brief(group: str, e: dict) -> str:
    """每条数据的一句话摘要 —— 按类别定制,不是通用的 len()。
    通用摘要('12 行')读的人什么也看不出来。"""
    d = e.get("data")
    if not e.get("ok"):
        return "—"
    try:
        if group == "forecast" and isinstance(d, list):
            if not d:
                return "无预告(净利变动未超 ±50%)"
            last = d[-1]
            return f"最近 {last[1]} · {last[3]} · {str(last[4])[:60]}"
        if group == "surveys" and isinstance(d, list) and d:
            x = d[0]
            return (f"最近 {str(x.get('RECEIVE_START_DATE',''))[:10]} · "
                    f"接待 {x.get('NUM')} 家 · {x.get('RECEIVE_WAY_EXPLAIN')} · "
                    f"{str(x.get('RECEIVE_TIME_EXPLAIN') or '')[:40]}")
        if group == "announcements" and isinstance(d, list) and d:
            return f"最近 {str(d[0].get('notice_date',''))[:10]} · {str(d[0].get('title',''))[:52]}"
        if group == "ratings" and isinstance(d, list):
            fo = [x for x in d if x.get("_是否外资系")]
            if not d:
                return "回看期内无评级记录"
            return (f"{len(d)} 条,其中外资系 {len(fo)} 条;最近 "
                    f"{d[-1].get('发布日期')} {d[-1].get('研究机构简称')} {d[-1].get('投资评级')}")
        if group == "consensus" and isinstance(d, dict):
            return (f"目标均 {d.get('target_mean')} (区间 {d.get('target_low')}~{d.get('target_high')}) · "
                    f"{d.get('n_analysts')} 家 · {d.get('recommendation')}")
        if group == "consensus" and isinstance(d, list) and d:
            return " / ".join(str(v)[:16] for v in list(d[0].values())[:4])
        if group == "research" and isinstance(d, list) and d:
            if "path" in d[0]:
                return f"已下载 PDF {len(d)} 份,最新 {d[0].get('date')} {str(d[0].get('org') or '')[:12]}"
            return f"{len(d)} 篇,最新 {str(d[0].get('publishDate',''))[:10]} {str(d[0].get('orgSName') or '')[:12]}"
        if group == "quotes" and isinstance(d, list) and d:
            return f"{len(d)} 根,末根 {d[-1][0]} 收 {d[-1][4]} peTTM {d[-1][7]}"
        if group == "financials" and isinstance(d, list) and d:
            return f"报告期 {d[0][2]} 披露 {d[0][1]} · 首字段 {d[0][3]}"
        if isinstance(d, list):
            return f"{len(d)} 行"
        if isinstance(d, dict):
            return "、".join(f"{k}={str(v)[:12]}" for k, v in list(d.items())[:3])
        return str(d)[:60]
    except Exception as ex:
        return f"(摘要失败 {type(ex).__name__})"


def stock_doc(code: str, raw: Path) -> str:
    L = [f"# {code} · 数据源明细", "",
         f"> 生成于 {date.today().isoformat()}　·　数据目录 `{raw}`　·　"
         f"每条都带**抓取时间**和**来源**,可逐条复核", ""]

    rows, missing = [], []
    for g, (cn, lead, why) in GROUP_META.items():
        items = load(raw, g, code, None)
        if not items:
            missing.append((cn, lead, why))
            continue
        # 识别「抓取轮次」:同一轮的条目时间挨在一起,两轮之间有明显空档。
        # ⚠ 不能拿「该类最大 fetched_at」当基准 —— 同一轮里各源本来就差几分钟,
        #   那样会把同轮的成功条目误标成旧记录(2026-09-02 踩过:
        #   23:59 成功的大宗交易被 00:00 的另一个源重试顶成了「旧记录」)。
        for fname, e in items:
            rows.append((g, cn, lead, dict(e), fname))

    mark_runs([r[3] for r in rows])
    cur = [r for r in rows if not r[3].get("_stale")]
    ok = sum(1 for r in cur if r[3].get("ok"))
    stale = len(rows) - len(cur)
    L += ["## 一、总览", "",
          f"最近一轮 **{len(cur)}** 条,成功 **{ok}** 条、失败 **{len(cur)-ok}** 条;"
          f"另有 **{len(missing)}** 类还没抓"
          + (f",以及 **{stale}** 条更早那轮的旧记录(标 🕗,不计入本轮)。" if stale else "。"), "",
          "| 类别 | 属性 | 源 | 抓取时间 | 行数 | 状态 |",
          "|---|---|---|---|---|---|"]
    for g, cn, lead, e, _ in rows:
        st = ("✅" if e.get("ok") else f"❌ {str(e.get('error') or '')[:34]}")
        if e.get("_stale"):
            st = "🕗 旧记录 · " + ("成功" if e.get("ok") else "失败")
        L.append(f"| {cn} | {LEAD_MARK[lead]} | `{e.get('source','')}` "
                 f"| {str(e.get('fetched_at') or '')[:16]} | {e.get('rows')} | {st} |")
    L.append("")

    # 按「领先 → 同步 → 滞后」组织,这是分析顺序,不是字母序
    by_lead = defaultdict(list)
    for g, cn, lead, e, fname in rows:
        by_lead[lead].append((g, cn, e))
    n = 2
    for lead in ("领先", "同步", "滞后"):
        if lead not in by_lead:
            continue
        L += [f"## {'一二三四五六'[n-1]}、{LEAD_MARK[lead]}指标", ""]
        n += 1
        seen = set()
        for g, cn, e in by_lead[lead]:
            if cn not in seen:
                seen.add(cn)
                L += [f"### {cn}", "", f"> {GROUP_META[g][2]}", ""]
            st = "🕗" if e.get("_stale") else ("✅" if e.get("ok") else "❌")
            L.append(f"- {st} `{e.get('source')}`　·　抓于 {str(e.get('fetched_at') or '')[:16]}"
                     f"　·　{brief(g, e)}")
            if not e.get("ok") and not e.get("_stale"):
                L.append(f"    - 失败原因:`{str(e.get('error'))[:120]}`")
            elif e.get("_stale"):
                L.append("    - 🕗 更早那轮留下的记录,当前代码已不再用这个源")
        L.append("")

    if missing:
        L += ["## 缺什么", "",
              "下面这些类别还没抓 —— **「没抓到」和「不存在」是两回事**,别当成后者。", "",
              "| 类别 | 属性 | 它回答什么问题 |", "|---|---|---|"]
        for cn, lead, why in missing:
            L.append(f"| {cn} | {LEAD_MARK[lead]} | {why} |")
        L.append("")

    L += ["---", "",
          "*本文档由 `tools/data_digest.py` 从 `data/raw/` 的落盘数据生成。*",
          "*数据源清单与获取方式见 `docs/DATA-SOURCES.md`。*"]
    return "\n".join(L)


def overseas_doc(raw: Path) -> str:
    L = ["# 海外上下游与宏观 · 数据源明细", "",
         "> 我们分析的是 A 股,但**需求端在海外**。云厂 capex 决定光模块,"
         "油服 capex 与钻机数决定油气设服,前道设备商决定半导体设备。"
         "下面这些是**领先指标,不是背景资料**。", ""]
    d = raw / "overseas"
    if d.is_dir():
        L += ["## 海外同业与下游客户", "",
              "| 代码 | 赛道分组 | 现价 | 目标价均 | 区间 | 分析师 | 建议 | 评级变动记录 | 抓取时间 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for tk in sorted(x.name for x in d.iterdir() if x.is_dir()):
            for _, e in load(raw, "overseas", tk, None):
                v = e.get("data") or {}
                ud = v.get("upgrades_downgrades")
                L.append(f"| **{tk}** | {e.get('params',{}).get('sector_group','')} "
                         f"| {v.get('price')} | {v.get('target_mean')} "
                         f"| {v.get('target_low')}~{v.get('target_high')} | {v.get('n_analysts')} "
                         f"| {v.get('recommendation')} | {0 if not ud else len(ud)} 条 "
                         f"| {str(e.get('fetched_at') or '')[:16]} |")
        L.append("")
    t = raw / "transcripts"
    if t.is_dir():
        L += ["## 电话会纪要(**下期 capex 指引只在这里**)", "",
              "SEC XBRL 只有已发生的 capex 总额;**下期指引、短周期/长周期拆分、"
              "公司自己的季度指引**只存在于电话会纪要。", "",
              "| 会议 | 正文字数 | 含 capex | 抓取时间 |", "|---|---|---|---|"]
        for k in sorted(x.name for x in t.iterdir() if x.is_dir()):
            for _, e in load(raw, "transcripts", k, None):
                v = e.get("data") or {}
                L.append(f"| {k} | {v.get('chars', 0):,} | "
                         f"{'✅' if v.get('has_capex') else '—'} | {str(e.get('fetched_at') or '')[:16]} |")
        L.append("")
    m = raw / "macro"
    if m.is_dir():
        L += ["## 宏观", "", "| 项 | 源 | 状态 | 摘要 | 抓取时间 |", "|---|---|---|---|---|"]
        for f in sorted(m.glob("*.json")):
            try:
                for src, e in json.loads(f.read_text(encoding="utf-8")).items():
                    nm = f.stem.split("-", 3)[-1] if "-" in f.stem else f.stem
                    L.append(f"| {nm} | `{src}` | {'✅' if e.get('ok') else '❌'} "
                             f"| {brief('macro', e)[:60]} | {str(e.get('fetched_at') or '')[:16]} |")
            except Exception:
                pass
        L.append("")
    L += ["---", "", "*由 `tools/data_digest.py --overseas` 生成。*"]
    return "\n".join(L)


def repo_root() -> Path:
    """向上找仓根,不用固定层数的 parent.parent —— 脚本挪目录时那种写法会静默错。
    2026-09-01 就踩了:脚本从 scripts/ 移到 scripts/digest/ 后路径少一层。"""
    d = Path(__file__).resolve()
    for _ in range(6):
        d = d.parent
        if (d / "skills").is_dir() and (d / "tools").is_dir():
            return d
    return Path(__file__).resolve().parents[3]


def to_pdf(md: Path, sub: str) -> None:
    root = repo_root()
    m2h = root / "skills" / "finance-pdf-report" / "scripts" / "md2html.py"
    h2p = root / "skills" / "finance-pdf-report" / "scripts" / "html2pdf.mjs"
    html = md.with_suffix(".html")
    subprocess.run([sys.executable, str(m2h), str(md), str(html), sub,
                    "--landscape", "--no-disclaimer"], check=True)
    r = subprocess.run(["node", str(h2p), str(html), str(md.with_suffix(".pdf")), sub])
    if r.returncode:
        print("  ⚠ PDF 没出来。playwright 装一次:"
              "\n     cd <有 node_modules 的目录> && npm i playwright && npx playwright install chromium"
              "\n     或设 PLAYWRIGHT_ROOT 指到已装的位置")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", action="append", default=[])
    ap.add_argument("--overseas", action="store_true")
    ap.add_argument("--raw", default="data/raw")
    # 默认落 private/ —— 明细本身只含公开行情,但**选了哪几只就暴露自选**,
    # 按本仓「个人的东西一律走 private/」的约定走。
    ap.add_argument("--out", default="private/data-digest")
    ap.add_argument("--pdf", action="store_true")
    a = ap.parse_args()
    if not a.code and not a.overseas:
        ap.error("至少给 --code 或 --overseas")

    raw, out = Path(a.raw), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for code in a.code:
        p = out / f"{code}-数据源明细.md"
        p.write_text(stock_doc(code, raw), encoding="utf-8")
        print(f"→ {p}")
        if a.pdf:
            to_pdf(p, f"{code} · 数据源明细 · {date.today().isoformat()}")
    if a.overseas:
        p = out / "海外上下游-数据源明细.md"
        p.write_text(overseas_doc(raw), encoding="utf-8")
        print(f"→ {p}")
        if a.pdf:
            to_pdf(p, f"海外上下游与宏观 · {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
