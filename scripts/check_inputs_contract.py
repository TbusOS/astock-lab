#!/usr/bin/env python3
"""check_inputs_contract —— 行业表里登记的每个领先指标,输入包要么有取数器,要么登记成工具缺口。

用法:
    python3 scripts/check_inputs_contract.py                    # 查本仓的 inputs.py,退出码 0 才算过
    python3 scripts/check_inputs_contract.py --inputs <副本>    # 查另一份 inputs.py(反向验证用)

为什么要有:sectors.py 说「这个行业该看哪几个领先指标」,inputs.py 决定「看板实际取了哪些数」。
两边各改各的,就会出现行业表里新加了一个指标、看板从来没取,页面上也不提 —— 读的人以为都看过了。
所以每个指标必须二选一:
  ① 在 inputs.LEAD_INDICATORS 里映射到 DEFAULT_COLLECTORS 的某个键(真有取数器);
  ② 在 inputs.TOOL_GAPS 里写明「为什么还没取」。
两边都没有、两边都有、映射到一个不存在的取数器,都算不过。

行业表按文件路径加载(skills/stock-analysis-workflow/scripts/sectors.py),只读里面的 SECTORS;
DEFAULT(没定义的行业)那一行写的是「没有为这个行业定义领先指标」,不是指标,不查。
"""
import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FB_SCRIPTS = ROOT / "skills" / "forecast-board" / "scripts"
SECTORS_PY = ROOT / "skills" / "stock-analysis-workflow" / "scripts" / "sectors.py"
INPUTS_PY = FB_SCRIPTS / "inputs.py"


def load(name: str, path: Path):
    """按文件路径加载模块;inputs 的副本也要能 import 到同目录的 fb_common / prices。"""
    sys.path.insert(0, str(FB_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None or not path.is_file():
        raise SystemExit(f"找不到 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def problems(sector_table: dict, inputs) -> list[str]:
    """不合格的地方,一条一句。空 = 合格。"""
    out = []
    lead, gaps, collectors = inputs.LEAD_INDICATORS, inputs.TOOL_GAPS, inputs.DEFAULT_COLLECTORS
    for key in sorted(set(lead.values()) - set(collectors)):
        names = [n for n, k in lead.items() if k == key]
        out.append(f"LEAD_INDICATORS 把 {names} 映射到 {key!r},但 DEFAULT_COLLECTORS 里没有这个取数器")
    for sector, spec in sector_table.items():
        for row in spec.get("leading", []):
            name = row[0]
            if name in lead and name in gaps:
                out.append(f"{sector}「{name}」既有取数器又登记成工具缺口,只能二选一")
            elif name not in lead and name not in gaps:
                out.append(f"{sector}「{name}」既没有取数器,也没在 TOOL_GAPS 里写原因")
            elif name in gaps and not str(gaps[name]).strip():
                out.append(f"{sector}「{name}」登记成工具缺口,但原因是空的")
    return out


def stale(sector_table: dict, inputs) -> list[str]:
    """映射表、缺口表里有、行业表里已经没有的名字(行业表改名了?)。只提醒,不算不过。"""
    listed = {row[0] for spec in sector_table.values() for row in spec.get("leading", [])}
    return sorted((set(inputs.LEAD_INDICATORS) | set(inputs.TOOL_GAPS)) - listed)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="行业领先指标 ↔ 输入包取数器")
    ap.add_argument("--inputs", type=Path, default=INPUTS_PY, help="要查的 inputs.py(默认本仓那份)")
    a = ap.parse_args(argv)
    sectors = load("fb_contract_sectors", SECTORS_PY)
    inputs = load("fb_contract_inputs", a.inputs.resolve())

    n = sum(len(s.get("leading", [])) for s in sectors.SECTORS.values())
    print(f"行业表 {len(sectors.SECTORS)} 个行业、{n} 个领先指标;查的是 {a.inputs}")
    for sector, spec in sectors.SECTORS.items():
        for row in spec.get("leading", []):
            name = row[0]
            if name in inputs.LEAD_INDICATORS:
                print(f"  {sector}「{name}」→ 取数器 {inputs.LEAD_INDICATORS[name]}")
            elif name in inputs.TOOL_GAPS:
                print(f"  {sector}「{name}」→ 工具缺口:{inputs.TOOL_GAPS[name]}")
    for name in stale(sectors.SECTORS, inputs):
        print(f"  ⚠ 「{name}」在 inputs 里登记了,行业表里已经没有(改名了?可以删掉)")
    bad = problems(sectors.SECTORS, inputs)
    print()
    for p in bad:
        print(f"  ❌ {p}")
    print("✅ 每个领先指标都有取数器或登记了缺口" if not bad else f"❌ {len(bad)} 处不合格")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
