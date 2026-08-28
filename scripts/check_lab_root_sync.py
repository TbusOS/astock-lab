#!/usr/bin/env python3
"""check_lab_root_sync —— _lab_root() 的多份副本必须逐字一致。

用法:python3 scripts/check_lab_root_sync.py   (退出码 0 才算过)

为什么需要:
    _lab_root() 在若干个脚本里各有一份(它们分属不同 skill,跨 skill import
    要先解决「怎么找到那个模块」—— 那正是同一个问题,套娃解决不了)。
    复制若干份是刻意的取舍,但复制就会漂:改了 baserate 里那份、忘了
    position_report 里那份,两个脚本对「工作台在哪」给出不同答案,
    表现是「有的工具找得到数据、有的找不到」,而且不报错。
    所以复制的代价必须由一道闸来兜。
"""
import ast
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def extract(path):
    """取出 _lab_root 的源码段;没有就返回 None。"""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_lab_root":
            return ast.get_source_segment(src, n)
    return None


def main():
    found = {}
    for f in sorted((ROOT / "skills").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        seg = extract(f)
        if seg:
            found[f.relative_to(ROOT)] = hashlib.md5(
                seg.encode("utf-8")).hexdigest()[:12]

    if not found:
        print("❌ 一份 _lab_root() 都没找到 —— 是不是被删了?")
        return 1

    groups = {}
    for f, h in found.items():
        groups.setdefault(h, []).append(f)

    for h, fs in groups.items():
        for f in fs:
            print(f"  {h}  {f}")

    print()
    if len(groups) == 1:
        print(f"✅ {len(found)} 份 _lab_root() 逐字一致。")
        return 0
    print(f"❌ {len(found)} 份 _lab_root() 分成了 {len(groups)} 种,不一致。")
    print("   它们必须完全相同 —— 否则不同工具会对「工作台在哪」给出不同答案,")
    print("   而且不报错,只表现为「有的工具找得到数据、有的找不到」。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
