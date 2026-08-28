#!/usr/bin/env python3
"""check_sector_contract —— 卡住「先判赛道再跑策略」这条规范。

用法:python3 scripts/check_sector_contract.py    退出码 0 才算过

## 这条规范是什么

**拿到一只票,先确定它属于哪个赛道、该抓什么数据源、跑哪套判据,再跑策略。**
不这么做的后果不是「少了点信息」,是**跑了错的策略,预测结果自然不对**。

2026-08-28 实测:拿 AI 算力链那套跑油气设服票,四个地方同时失效 ——
云厂 Capex 无关、PEG 算出 -15.30、净利增速基准率不适用、只剩滞后指标。
而报告一句提示都没有,照样把 PEG -15.30 印出来。

## 这道闸查五件事

1. **赛道定义单一真相** —— 只能有 sectors.py 一份。
   position_report 里曾经另有 PEER_SETS + BOARD_HINTS,实测已经漂了:
   一边写「光模块/半导体/云算力/PCB/油服」,另一边写
   「AI算力链/半导体设备/油气设服/消费/医药」,名字和切分全不同。

2. **每个赛道字段完整** —— 缺 leading / invalid_keys / peer_tickers 的赛道
   等于没定义,会静默退化成默认那套。

3. **invalid_keys 的每个 key 都能被 preport 认识** —— 写了却没人处理
   等于没写,报告照样印误导性的数。

4. **preport 真的先判赛道** —— sec_sector 必须在 run_one 里、
   且在取数各层之前被调用。

5. **失效指标真的被打标** —— PEG / PE 分位 / 基准率三处必须挂 _mark()。
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECT = ROOT / "skills" / "stock-analysis-workflow" / "scripts" / "sectors.py"
PREP = ROOT / "skills" / "astock-quote" / "scripts" / "position_report.py"

FAILS, OKS = [], []


def chk(cond, msg):
    (OKS if cond else FAILS).append(msg)


def main():
    if not SECT.exists() or not PREP.exists():
        print(f"❌ 找不到 sectors.py 或 position_report.py")
        return 2
    ssrc, psrc = SECT.read_text(encoding="utf-8"), PREP.read_text(encoding="utf-8")

    # ── 1 单一真相:position_report 不许再定义赛道 ────────────────────
    for bad in ("PEER_SETS = {", "BOARD_HINTS = ["):
        chk(bad not in psrc,
            f"position_report 里没有第二份赛道定义（`{bad.strip(' ={[')}`）")

    # ── 2 每个赛道字段完整 ──────────────────────────────────────────
    sys.path.insert(0, str(SECT.parent))
    try:
        import sectors
    except SystemExit as e:
        print(f"❌ import sectors 失败:{e}")
        return 1
    need = ("hints", "nature", "leading", "peers", "invalid", "criteria",
            "peer_tickers", "invalid_keys")
    for name, spec in sectors.SECTORS.items():
        miss = [k for k in need if k not in spec]
        chk(not miss, f"赛道「{name}」字段完整" + (f"（缺 {miss}）" if miss else ""))
    chk(all(k in sectors.DEFAULT for k in ("invalid_keys", "peer_tickers")),
        "DEFAULT（未定义赛道）也有 invalid_keys / peer_tickers")
    # 未定义赛道必须把所有可疑指标都标掉 —— 不知道是什么赛道时,什么都不该信
    chk(len(sectors.DEFAULT["invalid_keys"]) >= 3,
        f"DEFAULT 把 ≥3 个指标标为失效"
        f"（现 {len(sectors.DEFAULT['invalid_keys'])} 个）—— "
        f"不知道是什么赛道时，依赖赛道假设的指标都不该信")

    # ── 3 invalid_keys 都能被 preport 认识 ──────────────────────────
    m = re.search(r"INVALID_MARKS = \{(.*?)\n\}", psrc, re.S)
    chk(m is not None, "position_report 有 INVALID_MARKS 表")
    known = set(re.findall(r'"(\w+)":', m.group(1))) if m else set()
    used = set()
    for spec in list(sectors.SECTORS.values()) + [sectors.DEFAULT]:
        used |= set(spec.get("invalid_keys") or [])
    unknown = used - known
    chk(not unknown,
        f"invalid_keys 全部能被 preport 认识"
        + (f"（{sorted(unknown)} 没有对应的 INVALID_MARKS 条目 —— "
           f"写了却没人处理，报告照样印误导性的数）" if unknown else ""))

    # ── 4 preport 真的先判赛道 ──────────────────────────────────────
    tree = ast.parse(psrc)
    ro = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run_one"), None)
    chk(ro is not None, "position_report 有 run_one")
    if ro:
        body = ast.get_source_segment(psrc, ro) or ""
        chk("sec_sector(code)" in body, "run_one 调了 sec_sector")
        # 顺序:sec_sector 必须在这几层之前
        for later in ("sec_valuation", "sec_baserate", "sec_peers"):
            if "sec_sector(code)" in body and later in body:
                chk(body.index("sec_sector(code)") < body.index(later),
                    f"sec_sector 在 {later} **之前**（先判赛道再跑那一层）")

    # ── 5 三处失效指标真的挂了 _mark ────────────────────────────────
    for fn, key in (("sec_valuation", "peg"),
                    ("sec_valuation", "pe_percentile"),
                    ("sec_baserate", "baserate_growth")):
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == fn), None)
        seg = ast.get_source_segment(psrc, node) if node else ""
        chk(f'_mark(inv, "{key}")' in (seg or ""),
            f"{fn} 里对 `{key}` 挂了 _mark()")

    for m_ in OKS:
        print(f"  ✅ {m_}")
    for m_ in FAILS:
        print(f"  ❌ {m_}")
    print()
    if FAILS:
        print(f"「先判赛道再跑策略」这条规范有 {len(FAILS)} 项没落实。")
        print("后果不是少点信息，是**跑了错的策略** —— "
              "报告会把周期股的 PEG -15 印成一个看起来有意义的数。")
        return 1
    print(f"全部 {len(OKS)} 项通过 —— 赛道规范落实到位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
