#!/usr/bin/env python3
"""check_baserate_wiring —— 卡住「判据线必须等于基准率线」这条约束。

用法:
    python3 scripts/check_baserate_wiring.py       退出码 0 才算过

为什么需要这个脚本(不是写进文档就行):
    preport 第 9 层的判据是**写死的数字**:
        「净利同比保持 >50%」「净利同比跌破 30%」「毛利率不掉 3pp 以上」
    第 8 层的基准率则是**问 calibrate() 要的**。两边各写各的,只要有人
    改了其中一处 —— 比如把判据从 >50% 调成 >60% —— 报告就会出现
    「净利同比保持 >60%　—— 同类历史基准率 24%」这种句子:
    那个 24% 回答的还是 50% 的问题,**读者无从察觉**,因为两个数字
    印在同一行里看起来天经地义。

    这类错误编译器不管、跑起来不报错、输出看着完全正常 ——
    只能靠一道显式的闸。

    2026-08-27 接 baserate 进 preport 时已经真的踩过一次同类错误:
    毛利率判据是「相对自己掉 ≤3pp」,基准率却按固定线「仍 ≥42%」算,
    一家 48.4% 掉到 43% 的公司违反判据却算进分子 ——
    基准率因此虚高 24 个百分点(90% vs 真实 66%)。

设计:静态检查优先。
    绝大多数检查只读源码(AST + 正则),**不 import、不联网、不需要 venv**。
    刚 clone 下来、依赖一个没装,这道闸照样能告诉你接线对不对。
    只有最后那条「两个口径确有差异」需要真数据,拿不到就跳过并说明,
    不算失败 —— **把「环境没装好」报成「代码有问题」比不检查更糟**。
"""

import ast
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


# ── 工作台根目录定位 ────────────────────────────────────────────────────────
# ⚠ 这段在多个脚本里各有一份,**必须逐字一致**,由 scripts/check_gates.sh 比对。
#   为什么不做成共享模块:这几个脚本分属不同 skill,跨 skill import 要先解决
#   「怎么找到那个模块」—— 那是同一个问题,套娃解决不了。5 行代码复制若干份
#   加一道比对闸,比一个需要自己被找到的定位模块简单。
def _lab_root():
    """工作台根目录。顺序:$STOCK_LAB → 往上找 .astock-lab-root → 老默认路径。"""
    import os as _os
    e = _os.environ.get("STOCK_LAB")
    if e and Path(e).is_dir():
        return Path(e)
    for d in Path(__file__).resolve().parents:
        if (d / ".astock-lab-root").exists():
            return d
    for d in (Path.home() / "astock-lab-private", Path.home() / "astock-lab",
              Path.home() / "claude-tools" / "astock-lab-private",
              Path.home() / "claude-tools" / "astock-lab",
              Path.home() / "claude-tools" / "stock-lab"):
        if d.is_dir():
            return d
    return Path.cwd()


def _find(name):
    """按候选目录找脚本 —— 两个脚本分属不同 skill,软链会被 resolve 掉。"""
    cands = [_HERE,
             _HERE.parent.parent / "astock-quote" / "scripts",
             _lab_root() / "tools"]
    for d in cands:
        f = d / name
        if f.exists():
            return f
    sys.exit(f"❌ 找不到 {name}(找过:{[str(c) for c in cands]})")


FAILS, OKS, SKIPS = [], [], []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)


def _func(tree, src, name):
    """按名字取顶层函数的源码段。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


def _defaults(tree, name):
    """静态取函数的关键字默认值 —— 不 import,不需要依赖装好。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            args = n.args.args
            out = {}
            # 位置参数的默认值靠右对齐
            for a, d in zip(args[len(args) - len(n.args.defaults):],
                            n.args.defaults):
                try:
                    out[a.arg] = ast.literal_eval(d)
                except (ValueError, SyntaxError):
                    out[a.arg] = None
            for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults):
                if d is not None:
                    try:
                        out[a.arg] = ast.literal_eval(d)
                    except (ValueError, SyntaxError):
                        out[a.arg] = None
            return out
    return {}


def main():
    pr_path, br_path = _find("position_report.py"), _find("baserate.py")
    pr_src = pr_path.read_text(encoding="utf-8")
    br_src = br_path.read_text(encoding="utf-8")
    pr_tree, br_tree = ast.parse(pr_src), ast.parse(br_src)

    # ── 1 抽出 sec_verdict 里判据的字面阈值 ────────────────────────────
    vsrc = _func(pr_tree, pr_src, "sec_verdict")
    check(bool(vsrc), "position_report 里有 sec_verdict")
    if not vsrc:
        return report()

    hold_lit = re.search(r"净利同比保持 \*\*>(\d+)%\*\*", vsrc)
    cut_lit = re.search(r"净利同比\*\*跌破 (\d+)%\*\*", vsrc)
    drop_lit = re.search(r"毛利率不掉 \*\*(\d+)pp", vsrc)
    check(hold_lit is not None, "判据里找得到「净利同比保持 >N%」")
    check(cut_lit is not None, "判据里找得到「净利同比跌破 N%」")
    check(drop_lit is not None, "判据里找得到「毛利率不掉 Npp」")
    if not (hold_lit and cut_lit and drop_lit):
        return report()

    hold_n, cut_n, drop_n = (int(hold_lit.group(1)), int(cut_lit.group(1)),
                             int(drop_lit.group(1)))

    # ── 2 同一段里向基准率**要**的是哪几个数 ───────────────────────────
    asked_hold = re.search(r"_br_growth\(bases,\s*(\d+)\)", vsrc)
    asked_cut = re.search(r'get\("growth", \{\}\)\.get\((\d+)\)', vsrc)
    check(asked_hold is not None, "持有判据那行挂上了 _br_growth(...)")
    check(asked_cut is not None, "减仓判据那行取了 growth 基准率")
    if asked_hold:
        check(int(asked_hold.group(1)) == hold_n,
              f"持有线一致:判据 >{hold_n}% ↔ 基准率问 {asked_hold.group(1)}%")
    if asked_cut:
        check(int(asked_cut.group(1)) == cut_n,
              f"减仓线一致:判据 跌破 {cut_n}% ↔ 基准率问 {asked_cut.group(1)}%")

    # ── 3 calibrate 的默认值必须覆盖这几条线(静态读,不 import)──────────
    d = _defaults(br_tree, "calibrate")
    check(bool(d), "静态读到 calibrate 的默认参数")
    holds = set(d.get("growth_holds") or ())
    check(hold_n in holds, f"calibrate 默认 growth_holds 覆盖持有线 {hold_n}")
    check(cut_n in holds, f"calibrate 默认 growth_holds 覆盖减仓线 {cut_n}")
    check(d.get("margin_drop") == drop_n,
          f"calibrate 默认 margin_drop({d.get('margin_drop')}) == 判据的 {drop_n}pp")

    # ── 4 毛利率必须走**相对口径**,不能是固定线 ─────────────────────────
    csrc = _func(br_tree, br_src, "calibrate")
    check("max_drop=margin_drop" in csrc,
          "calibrate 的毛利率走相对口径(max_drop),不是固定线")
    check("margin_hold" not in csrc,
          "calibrate 不再产出固定线的 margin_hold(那是偏乐观的旧口径)")
    check("margin_drop" in pr_src and "margin_hold" not in pr_src,
          "position_report 读的是 margin_drop,没有残留 margin_hold")

    # ── 5 CLI 和报告必须共用同一份计算 ─────────────────────────────────
    check("calibrate(" in _func(br_tree, br_src, "cmd_calibrate"),
          "baserate calibrate 子命令走 calibrate(),没有另写一遍循环")

    # ── 6 两个口径确有实质差异(需要真数据;拿不到就跳过,不算失败)────────
    sys.path.insert(0, str(br_path.parent))
    try:
        import baserate as br
        data = br.load_all(12)
        if not data:
            SKIPS.append("口径实测:本地没有全市场业绩缓存")
        else:
            rel = br.rate(data, "销售毛利率", 45, None, 4, max_drop=3)
            fix = br.rate(data, "销售毛利率", 45, 42, 4)
            if rel and fix:
                check(fix["rate"] - rel["rate"] > 5,
                      f"两个口径确有实质差异(固定线 {fix['rate']:.0f}% > "
                      f"相对口径 {rel['rate']:.0f}%),相对口径没退化成固定线")
            else:
                SKIPS.append("口径实测:样本期不足")
    except SystemExit as e:
        SKIPS.append(f"口径实测:依赖没装好({e})")
    except Exception as e:
        SKIPS.append(f"口径实测:{type(e).__name__}: {e}")

    return report()


def report():
    for m in OKS:
        print(f"  ✅ {m}")
    for m in SKIPS:
        print(f"  ⏭  {m} —— 需要真数据,跳过,不算失败")
    for m in FAILS:
        print(f"  ❌ {m}")
    print()
    if FAILS:
        print(f"判据线与基准率线不一致:{len(FAILS)} 项不通过。")
        print("报告会印出「判据 >X%　—— 历史基准率 Y%」而 Y 回答的是别的问题，")
        print("读者无从察觉。改到一致再提交。")
        return 1
    tail = f"，{len(SKIPS)} 项跳过" if SKIPS else ""
    print(f"全部 {len(OKS)} 项通过{tail} —— 判据线与基准率线一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
