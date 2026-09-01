#!/usr/bin/env python3
"""commodity —— 大宗商品价格（周期股的领先指标）。

**为什么要这个**:周期股的判据和成长股完全不同。成长股看增速持续性,周期股看**周期位置**,
而周期位置的锚是商品价格,不是 PE 分位 —— 油价在低位时油服公司的 PE 会因为盈利低谷而显得很高,
把它读成「估值贵」是典型的假信号。`sectors` 已经把周期股的 PE 分位/PEG/基准率标为失效,
这个工具补上真正该看的那个。

覆盖:
  油   WTI(hf_CL) / 布伦特(hf_OIL)      —— 油气设服、炼化
  金属 铜(hf_CAD) / 铝(hf_ALI)          —— 有色、电缆
  贵金属 黄金(hf_GC) / 白银(hf_SI)

数据源:新浪外盘期货 hq.sinajs.cn(免费、无 key、需 Referer)。
字段实测(2026-08-30):[0]最新价 [2]买价 [3]卖价 [4]最高 [5]最低 [6]时间
                     [7]昨结算 [8]今开 [12]日期 [13]品种名

用法:
    commodity              # 油价(默认)
    commodity --group 金属
    commodity --symbols CL,OIL,GC
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import urllib.request

SINA = "https://hq.sinajs.cn/list={codes}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

GROUPS = {
    "油":   [("CL", "WTI 原油", "美元/桶"), ("OIL", "布伦特原油", "美元/桶")],
    "金属": [("CAD", "LME 铜", "美元/吨"), ("ALI", "LME 铝", "美元/吨")],
    "贵金属": [("GC", "黄金", "美元/盎司"), ("SI", "白银", "美元/盎司")],
}
# 哪个赛道该看哪一组 —— 与 sectors.py 的赛道名对齐
SECTOR_GROUP = {"油气设服": "油", "有色": "金属", "黄金": "贵金属"}


def fetch(symbols: list[str]) -> dict[str, dict]:
    codes = ",".join(f"hf_{s}" for s in symbols)
    req = urllib.request.Request(
        SINA.format(codes=codes),
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("gbk", errors="replace")

    out = {}
    for line in text.split("\n"):
        if '"' not in line or "hf_" not in line:
            continue
        sym = line.split("hf_")[1].split('"')[0].rstrip("=").strip()
        f = line.split('"')[1].split(",")
        if len(f) < 14:
            continue
        try:
            last, prev = float(f[0]), float(f[7])
        except ValueError:
            continue
        out[sym] = {
            "last": last, "prev_settle": prev,
            "chg_pct": (last - prev) / prev * 100 if prev else None,
            "high": _f(f[4]), "low": _f(f[5]), "open": _f(f[8]),
            "time": f[6], "date": f[12], "name": f[13],
        }
    return out


def _f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def render(quotes: dict, spec: list[tuple[str, str, str]]) -> str:
    L = [
        "# 大宗商品价格（周期股的领先指标）",
        "",
        f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　·　数据源 新浪外盘期货（免费，无需 key）",
        "",
        "| 品种 | 最新 | 涨跌 | 日内高/低 | 昨结算 | 报价时间 |",
        "|---|---|---|---|---|---|",
    ]
    missing = []
    for sym, cn, unit in spec:
        q = quotes.get(sym)
        if not q:
            missing.append(f"{cn}({sym})")
            continue
        chg = q["chg_pct"]
        arrow = "" if chg is None else ("▲" if chg > 0 else ("▼" if chg < 0 else "－"))
        L.append(
            f"| {cn} | **{q['last']:,.2f}** {unit} | {arrow} {chg:+.2f}% | "
            f"{q['high']:,.2f} / {q['low']:,.2f} | {q['prev_settle']:,.2f} | "
            f"{q['date']} {q['time']} |"
        )
    if missing:
        L += ["", f"> 取不到：{'、'.join(missing)}（新浪该代码可能已变更）"]
    L += [
        "",
        "## 怎么用",
        "",
        "**周期股不看 PE 分位，看周期位置。** 商品价格在低位时，周期股的盈利处于低谷，"
        "PE 会被小分母顶得很高 —— 这时读成「估值贵」是**假信号**；反过来，商品价格在高位、"
        "盈利在峰值时 PE 会显得很低，读成「便宜」同样是假信号。`sectors` 已经把周期赛道的 "
        "`peg` / `pe_percentile` / `baserate_growth` 标为失效，就是这个原因。",
        "",
        "**看两件事**：① 商品价格在自身历史区间的什么位置；"
        "② 下游的资本开支在扩还是在收（`capex --tickers` 可以取任意美股公司，"
        "油服看 `SLB,HAL,BKR,XOM,CVX`）。价格决定下游赚不赚钱，资本开支决定下游买不买设备 —— "
        "**后者才是设备商的订单来源，而且领先 2–4 个季度。**",
        "",
        "---",
        "",
        "**数据来源**：`hq.sinajs.cn` 外盘期货（需 `Referer: finance.sina.com.cn`，否则 403）。"
        "这是**连续合约报价**不是现货价，用于看方向与区间位置，别当结算价用。",
    ]
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="commodity", description="大宗商品价格（周期股领先指标）")
    p.add_argument("--group", default="油", choices=sorted(GROUPS), help="品种组，默认 油")
    p.add_argument("--symbols", help="直接指定代码，如 CL,OIL,GC（覆盖 --group）")
    p.add_argument("--md", help="同时写入 markdown 文件")
    a = p.parse_args(argv)

    if a.symbols:
        spec = [(s.strip().upper(), s.strip().upper(), "") for s in a.symbols.split(",") if s.strip()]
    else:
        spec = GROUPS[a.group]
    try:
        quotes = fetch([s for s, _, _ in spec])
    except Exception as e:
        print(f"取报价失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not quotes:
        print("新浪返回空 —— 代码可能变了，或缺 Referer 被 403", file=sys.stderr)
        return 1

    text = render(quotes, spec)
    print(text)
    if a.md:
        from pathlib import Path
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
