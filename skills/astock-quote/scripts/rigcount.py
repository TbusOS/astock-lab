#!/usr/bin/env python3
"""rigcount —— 北美钻机数（油服赛道的领先指标）。

**为什么要这个**:钻机数是油服设备需求最直接的先行量 —— 钻机在钻,才需要压裂设备、
钻杆、井口装置。它领先油服公司的订单 1-2 个季度,比油价更贴近设备商的收入。
Baker Hughes 每周五公布,免费。

**取数路径有个坑**:官方站 rigcount.bakerhughes.com **从中国大陆不可达**
(2026-08-31 实测:直连、走代理、真浏览器全部 000),而且页面本身是 JS 渲染的。
所以这里走**转载源**,并在输出里如实标注「原始发布方 = Baker Hughes,本工具读的是转载」。
两个源互为备份,主源挂了自动降级:
  1. oilpriceapi.com/data/rig-count   —— 最全(总数/油气拆分/盆地/州),但需走代理
  2. fueldataportal.com/data/us-rig-count —— 只有总数,国内直连可达

用法:
    rigcount
    rigcount --md ~/rig.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PRIMARY = "https://www.oilpriceapi.com/data/rig-count"
FALLBACK = "https://fueldataportal.com/data/us-rig-count"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def _fetch(url: str) -> str:
    """跟随系统代理 —— oilpriceapi 是海外站,mac 上直连会 403。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler())
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with op.open(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def _text(raw: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def parse_primary(raw: str) -> dict:
    t = _text(raw)
    out: dict = {"source": "oilpriceapi.com（转载 Baker Hughes）", "url": PRIMARY}

    m = re.search(r"(\d{3,4})\s+Total Active Rigs\s+(\S+(?:\s+\S+)?)\s+WoW", t)
    if not m:
        raise ValueError("没找到「Total Active Rigs」——页面结构变了")
    out["total"] = int(m.group(1))
    out["wow_text"] = m.group(2).strip()

    d = re.search(rf"As of ({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})", t)
    if d:
        out["as_of"] = f"{d.group(3)}-{_mnum(d.group(1)):02d}-{int(d.group(2)):02d}"

    o = re.search(r"Oil-directed rigs total\s+(\d{2,4})\s*\((\d{1,3})%\).*?"
                  r"(\d{2,4})\s+gas-directed", t)
    if o:
        out["oil"], out["oil_pct"], out["gas"] = int(o.group(1)), int(o.group(2)), int(o.group(3))

    # 盆地表在纯文本里是「名称 总数 油 气 周变化 占比%」连排(油/气常为 "-"),
    # 不是 markdown 表格 —— 用 HTML 抽标签会拿到一堆空格,所以在 _text() 结果上抽。
    # 从表头「% of Total」之后开始截 —— 否则表头末尾的 "Total" 会粘到第一行盆地名上
    seg = t[t.find("Basin Rig Count Breakdown"):]
    hdr = seg.find("% of Total")
    if hdr >= 0:
        seg = seg[hdr + len("% of Total"):]
    out["basins"] = [
        {"name": name.strip(), "rigs": int(rigs), "wow": wow, "pct": pct}
        for name, rigs, wow, pct in
        re.findall(r"([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?) (\d{1,4}) [-\d]+ [-\d]+ ([+-]?\d+) ([\d.]+)%", seg[:1200])
    ][:8]
    return out


def parse_fallback(raw: str) -> dict:
    t = _text(raw)
    m = re.search(r"U\.?S\.?\s*Rig Count\s+(\d{3,4})", t) or re.search(r"Total U\.?S\.? rigs\s*\|?\s*(\d{3,4})", t)
    if not m:
        raise ValueError("备用源也没解析出总数")
    d = re.search(rf"({MONTHS[:20]}\w*)\s+(\d{{1,2}}),\s*(\d{{4}})", t)
    return {"source": "fueldataportal.com（转载 Baker Hughes）", "url": FALLBACK,
            "total": int(m.group(1)), "wow_text": "—",
            "as_of": f"{d.group(3)}-{_mnum(d.group(1)):02d}-{int(d.group(2)):02d}" if d else None,
            "basins": []}


def _mnum(name: str) -> int:
    return MONTHS.split("|").index(name.capitalize()) + 1


def collect() -> tuple[dict, list[str]]:
    """主源失败就降级,并把降级原因带出来 —— 静默降级会让人以为数据本来就这么少。"""
    notes = []
    for fn, url, label in ((parse_primary, PRIMARY, "主源 oilpriceapi"),
                           (parse_fallback, FALLBACK, "备用源 fueldataportal")):
        try:
            return fn(_fetch(url)), notes
        except Exception as e:
            notes.append(f"{label} 失败：{type(e).__name__}: {str(e)[:70]}")
    raise RuntimeError("两个源都取不到：" + "；".join(notes))


def render(d: dict, notes: list[str]) -> str:
    L = [
        "# 北美钻机数（油服赛道领先指标）",
        "",
        f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　·　"
        f"原始发布方 **Baker Hughes**（每周五）　·　本工具读的是转载源",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 美国活跃钻机总数 | **{d['total']:,}** |",
        f"| 周环比 | {d.get('wow_text', '—')} |",
        f"| 数据截止 | {d.get('as_of') or '—'} |",
    ]
    if d.get("oil"):
        L += [f"| 油井钻机 | {d['oil']:,}（{d.get('oil_pct','?')}%） |",
              f"| 气井钻机 | {d.get('gas', 0):,} |"]
    L.append("")

    if d.get("basins"):
        L += ["## 主要盆地", "", "| 盆地 | 钻机数 | 周变化 | 占比 |", "|---|---|---|---|"]
        L += [f"| {b['name']} | {b['rigs']:,} | {b['wow']} | {b['pct']}% |" for b in d["basins"]]
        L.append("")

    if notes:
        L += ["> ⚠ 取数时发生过降级：" + "；".join(notes), ""]

    L += [
        "## 怎么用",
        "",
        "钻机数是油服设备需求**最直接的先行量** —— 钻机在钻，才需要压裂设备、钻杆、井口装置。"
        "它比油价更贴近设备商的收入：油价高但资本开支不扩，钻机数不会涨，设备订单也不会来。",
        "",
        "**三个量一起看**：油价（`commodity --group 油`）决定下游赚不赚钱 → "
        "油气资本开支（`capex --tickers SLB,HAL,BKR,XOM,CVX`）决定下游愿不愿意花钱 → "
        "**钻机数是花钱之后真正落地的作业量**。三者背离时，以钻机数为准 —— 它是已发生的事实，"
        "另外两个还只是意愿。",
        "",
        "---",
        "",
        f"**数据来源**：原始发布方 Baker Hughes（每周五公布，免费）；本工具读的是 `{d['source']}` 的转载。",
        "",
        "⚠️ **官方站 `rigcount.bakerhughes.com` 从中国大陆不可达**"
        "（2026-08-31 实测：直连、走规则代理、headless 浏览器全部 000），且页面是 JS 渲染的。"
        "所以这里走转载源 —— **数字是二手的**，做关键判断前建议对一下另一个源。",
    ]
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rigcount", description="北美钻机数（油服领先指标）")
    p.add_argument("--md", help="同时写入 markdown 文件")
    a = p.parse_args(argv)
    try:
        d, notes = collect()
    except Exception as e:
        print(f"取钻机数失败：{e}", file=sys.stderr)
        return 1
    text = render(d, notes)
    print(text)
    if a.md:
        from pathlib import Path
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
