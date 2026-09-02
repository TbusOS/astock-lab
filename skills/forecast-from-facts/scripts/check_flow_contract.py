#!/usr/bin/env python3
"""check_flow_contract —— 卡住「合并后这条判断流程」的五条硬规矩。

    python3 skills/forecast-from-facts/scripts/check_flow_contract.py [--raw <目录>]
    退出码 0 才算过

## 为什么这一层要有可执行检查,而不是写进文档提醒自己

**这五件事错了都是静默的**:报告照样生成、格式照样对、每个数都算得出来,
只是其中一条是错的,而读者(和我们自己)看不出来。
长会话里早期读过的规矩会被上下文稀释,执行时凭印象做,印象里没有那条就漏 ——
这不是「更认真点」能解决的,得让检查变成能跑的东西。

## 查五件事

1. **`report.py` 在结论之前调行业层** —— 行业决定后面每一节用哪把尺。
   放到结论后面,报告读起来一样顺,但结论是用一把还没确定的尺量出来的。
2. **决策链不碰任何机构预测** —— `advice.py` / `decision.py` / `guidance.py` /
   `sector_rules.py` / `outside_view.py` 不许 import consensus,
   也不许读 `consensus/` `ratings/` `research/` 下的文件。
   一致预期、评级、目标价只能出现在报告第 9 节作事后对照。
3. **journal 每条的 ev 和「三情景价位 × 概率」自洽**(差 ≤ 2 个百分点)——
   两边算法一有出入,复核时就核不了:你不知道该信哪个数。
4. **`SECTOR_RULES` 的键都在 `sectors.SECTORS` 里** —— 两张表分开放,
   就必然会漂。漂了的表现是:某个行业在 sectors 里改了名,
   这边还挂着旧名,于是那个行业**静默退回未定义**,所有尺全失效,
   而报告只会在第 0.5 节写一句「行业未定义」,没人会去查为什么。
5. **概率三项和 = 1** —— 四步修正里任何一步算错,和就不是 1,
   而期望值照样算得出来(只是权重不对)。这是唯一能当场发现它的办法。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "stock-analysis-workflow" / "scripts"))

FAILS, OKS = [], []


def chk(cond, msg):
    (OKS if cond else FAILS).append(msg)


# 决策链上的文件 —— 这几个文件里的每一个数都必须能追到公司报表、SEC 申报原值,
# 或者公司自己在电话会上说的话。
DECISION_CHAIN = ["advice.py", "decision.py", "guidance.py",
                  "sector_rules.py", "outside_view.py", "journal_log.py"]
FORBIDDEN_IMPORTS = {"consensus"}
FORBIDDEN_PATHS = ("consensus/", "ratings/", "research/")


def check_order():
    """① report.py:行业层在结论节之前。"""
    src = (HERE / "report.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "build"), None)
    chk(fn is not None, "report.py 有 build()")
    if not fn:
        return
    body = ast.get_source_segment(src, fn) or ""
    has_sector = "sector_rules.render(" in body
    chk(has_sector, "build() 里调了行业层 `sector_rules.render(`")
    for later in ("sec_conclusion(", "advice.build(", "sec_valuation("):
        if has_sector and later in body:
            chk(body.index("sector_rules.render(") < body.index(later),
                f"行业层在 `{later}` **之前**被调用"
                f"(先判行业,才知道后面该用哪把尺)")


def check_no_consensus():
    """② 决策链不 import、不读机构预测。"""
    for name in DECISION_CHAIN:
        f = HERE / name
        if not f.exists():
            chk(False, f"{name} 不存在")
            continue
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad_imp = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                bad_imp |= {a.name.split(".")[0] for a in n.names} & FORBIDDEN_IMPORTS
            elif isinstance(n, ast.ImportFrom) and n.module:
                if n.module.split(".")[0] in FORBIDDEN_IMPORTS:
                    bad_imp.add(n.module)
        chk(not bad_imp, f"{name} 没 import 机构预测模块"
                         + (f"(发现 {sorted(bad_imp)})" if bad_imp else ""))
        # 字符串里出现 `consensus/` 之类的路径 = 在读那类文件。
        # 注释里提到不算 —— 先把注释和文档字符串剥掉再找。
        code_only = _strip_comments_and_docstrings(src, tree)
        hits = [p for p in FORBIDDEN_PATHS if p in code_only]
        chk(not hits, f"{name} 的代码里没有读 {'/'.join(FORBIDDEN_PATHS)} 的路径"
                      + (f"(发现 {hits})" if hits else ""))


def _strip_comments_and_docstrings(src: str, tree) -> str:
    """把注释和 docstring 去掉,只留真正会执行的代码。

    为什么要剥:这几个文件的注释里**必须**能写「不许读 consensus/」这句话 ——
    那是在解释规矩,不是在违反它。不剥的话,写清楚规矩反而会让检查报错,
    于是下一个人的做法是把注释删掉。**检查不该逼人删注释。**
    """
    spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and ast.get_docstring(n) is not None:
            d = n.body[0]
            spans.append((d.lineno, d.end_lineno))
    lines = src.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if any(a <= i <= b for a, b in spans):
            continue
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def check_journal(raw: Path):
    """③ journal 每条 ev 与 scenario_px × probs 自洽(差 ≤ 2pp)。"""
    p = raw.parent / "journal.jsonl"
    if not p.exists():
        chk(True, f"没有 {p}(新装的仓属正常),③ 跳过")
        return
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    bad, checked = [], 0
    for r in rows:
        px, pr, ev, price = (r.get("scenario_px") or {}), (r.get("probs") or {}), \
            r.get("ev"), r.get("price")
        if ev is None or not price:
            continue
        if not all(px.get(k) is not None and pr.get(k) is not None
                   for k in ("bear", "base", "bull")):
            continue
        checked += 1
        calc = sum(pr[k] * (px[k] / price - 1) * 100 for k in ("bear", "base", "bull"))
        if abs(calc - ev) > 2.0:
            bad.append(f"#{r['id']} {r['code']} 写 {ev:+.1f}%、"
                       f"按情景价×概率算 {calc:+.1f}%(差 {abs(calc - ev):.1f}pp)")
    chk(not bad, f"journal {checked} 条的 ev 与三情景价位自洽"
                 + (f" —— **不自洽的**:{'; '.join(bad)}" if bad else ""))
    # ⑤ 概率三项和 = 1
    bad2 = []
    for r in rows:
        pr = r.get("probs") or {}
        if not all(pr.get(k) is not None for k in ("bear", "base", "bull")):
            continue
        tot = sum(pr[k] for k in ("bear", "base", "bull"))
        if abs(tot - 1.0) > 0.02:
            bad2.append(f"#{r['id']} {r['code']} 三项和 = {tot:.3f}")
    chk(not bad2, "journal 每条的三情景概率加起来等于 1"
                  + (f" —— **不等于 1 的**:{'; '.join(bad2)}" if bad2 else ""))


def check_sector_rules():
    """④ SECTOR_RULES 的键都在 sectors.SECTORS 里。"""
    try:
        import sector_rules
        import sectors
    except Exception as e:                       # noqa: BLE001
        chk(False, f"import sector_rules / sectors 失败:{e!r}")
        return
    unknown = set(sector_rules.SECTOR_RULES) - set(sectors.SECTORS)
    chk(not unknown,
        "SECTOR_RULES 的键全部在 sectors.SECTORS 里"
        + (f" —— **多出来的**:{sorted(unknown)}。"
           f"多出来的键**永远匹配不到**,那个行业会静默退回「未定义」,"
           f"所有尺全失效,而报告只会写一句「行业未定义」" if unknown else ""))
    missing = set(sectors.SECTORS) - set(sector_rules.SECTOR_RULES)
    chk(not missing,
        "sectors.SECTORS 的每个行业在 SECTOR_RULES 里都有一条"
        + (f" —— **缺的**:{sorted(missing)}。缺的那个会退回 UNDEFINED"
           f"(什么尺都不给),那是安全的,但应该显式写一条说明为什么没定尺"
           if missing else ""))
    need = ("估值尺", "估值尺说明", "展示尺", "基本面主指标", "基准率",
            "基准率不适用原因", "上游组", "领先指标")
    for name, spec in sector_rules.SECTOR_RULES.items():
        miss = [k for k in need if k not in spec]
        chk(not miss, f"行业「{name}」的规则字段完整"
                      + (f"(缺 {miss})" if miss else ""))
    chk(all(k in sector_rules.UNDEFINED for k in need),
        "UNDEFINED(未定义行业)的字段也完整")
    valid_lens = {None, "pe31", "pb", "ps31+pb"}
    bad = {n: s["估值尺"] for n, s in sector_rules.SECTOR_RULES.items()
           if s.get("估值尺") not in valid_lens}
    chk(not bad, "每个行业的估值尺都是已实现的那几把"
                 + (f" —— **不认识的**:{bad}" if bad else ""))


def check_probability_rules():
    """⑤ 概率算法本身:起点三项和 = 1,四步之后仍然 = 1。

    起点是常量,四步是算术 —— 两处都可能被改坏,而改坏之后期望值照样算得出来,
    只是权重不对。这里拿几组构造出来的输入跑一遍,比只查盘上那几条记录更硬:
    **盘上的记录只覆盖今天恰好出现的情形。**
    """
    import decision
    tot0 = sum(decision.START_PROBS.values())
    chk(abs(tot0 - 1.0) < 1e-9, f"概率起点三项和 = {tot0}")
    cases = [
        ({"方向": []}, {"可用方法数": 3, "方法一致": True}, None),
        ({"方向": [{"方向": "↑"}] * 5}, {"可用方法数": 1, "方法一致": True}, 20),
        ({"方向": [{"方向": "↓"}] * 6}, {"可用方法数": 3, "方法一致": False}, None),
        ({"方向": [{"方向": "↑"}, {"方向": "↓"}]},
         {"可用方法数": 2, "方法一致": True}, 5),
    ]
    bad = []
    for lead, f, cap in cases:
        r = decision.probabilities(lead, f, cap, "构造用例")
        tot = sum(r["probs"].values())
        if abs(tot - 1.0) > 1e-9:
            bad.append(f"{lead}/{f}/cap={cap} → 和 {tot:.4f}")
        if any(v < -1e-9 or v > 1 + 1e-9 for v in r["probs"].values()):
            bad.append(f"{lead}/{f}/cap={cap} → 有概率跑出 [0,1]:{r['probs']}")
    chk(not bad, f"{len(cases)} 组构造用例走完四步之后,三项和仍然 = 1、都在 [0,1] 之内"
                 + (f" —— **坏的**:{bad}" if bad else ""))
    # 期望值阈值必须单调,否则 act_from_ev 会永远命中第一档
    cuts = [c for c, _ in decision.EV_CUTS]
    chk(cuts == sorted(cuts), f"EV 阈值单调递增 {cuts}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw",
                    help="原始数据目录;journal 从它的上一级找")
    a = ap.parse_args()
    print("== 合并后判断流程的合约检查 ==\n")
    check_order()
    check_no_consensus()
    check_journal(Path(a.raw))
    check_sector_rules()
    check_probability_rules()
    for m in OKS:
        print(f"  ✅ {m}")
    for m in FAILS:
        print(f"  ❌ {m}")
    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项没落实。")
        print("**这五件事错了都是静默的** —— 报告照样生成、格式照样对、"
              "每个数都算得出来,只是其中一条是错的。")
        return 1
    print(f"全部 {len(OKS)} 项通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
