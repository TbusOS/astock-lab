#!/usr/bin/env python3
"""check_preport —— preport 两个不联网就能查的毛病。

用法:python3 scripts/check_preport.py [--file <position_report.py>]   # 反向验证用旧版

① 动态 PE 按报告期折全年(2026-10-29 晚发现):
   原来不看报告期，一律「最新累计净利 × 2」当全年。三季报出来以后，它把 9 个月的累计
   当半年翻倍，全年利润多算三分之一 —— 中际旭创印出 15.4 倍，实际约 23 倍;
   新易盛印 14.0 倍，实际约 21 倍。数字看着正常，没有任何报错。

② 一次跑两只时，各自的小节留在各自的报告里(同一晚发现):
   run_one 用 `_OUT.index("## 1　持仓状况")` 找插入点，那总是找到**第一只**的标题 ——
   第二只的「0a 行业」「0 与上次对比」全插进了第一只的报告。以前每次只跑一只，看不出来;
   为了比较同行背离一次跑两只，才暴露。这里用替身函数顶掉所有取数，只查排版顺序。
"""
import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parent / "skills" / "astock-quote" / "scripts" / "position_report.py"


def load(path):
    spec = importlib.util.spec_from_file_location("pr_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    return mod


def check_annualize(mod, ok):
    fn = getattr(mod, "annualize_net", None)
    if fn is None:
        ok("有 annualize_net(报告期 → 全年净利)", False, "没有这个函数")
        return
    cases = [
        ("三季报 222.38 亿 → 保守全年 296.5", 222.38, "2026-09-30", 0, 296.5),
        ("半年报 136.51 亿 → 保守全年 273.0(原来的 ×2 在这里是对的)", 136.51, "2026-06-30", 0, 273.0),
        ("半年报加速一档 = 2.3 倍(保持原来的算法)", 100.0, "2026-06-30", 1, 230.0),
        ("一季报 57.35 亿 → 保守全年 229.4", 57.35, "2026-03-31", 0, 229.4),
        ("年报就是全年实际", 107.97, "2025-12-31", 0, 107.97),
    ]
    for name, net, period, idx, want in cases:
        rows = fn(net, period)
        got = rows[idx][1] if len(rows) > idx else None
        ok(name, got is not None and abs(got - want) < 0.05, got)
    ok("报告期不明 → 不出表，不猜", fn(100.0, "") == [], fn(100.0, ""))


def check_two_holdings(mod, ok):
    """顶掉所有取数，跑两只，看每只的 0a / 0 是不是落在自己的标题和「## 1」之间。"""
    say = mod.say

    def position(code, cost):
        say(f"# 报告 {code}")
        say("## 1　持仓状况")
        return code, 1.0

    stubs = {
        "load_prev_snapshot": lambda code, today: None,
        "load_open_journal": lambda code: None,
        "sec_sector": lambda code: (say(f"## 0a　行业 {code}"), ("x", (), None))[1],
        "sec_position": position,
        "sec_fundamental": lambda code: {},
        "sec_valuation": lambda *a, **k: None,
        "sec_chips": lambda *a, **k: None,
        "sec_risk": lambda *a, **k: None,
        "sec_technical": lambda *a, **k: None,
        "sec_peers": lambda *a, **k: None,
        "pick_peers": lambda *a, **k: [],
        "sec_baserate": lambda *a, **k: None,
        "sec_verdict": lambda *a, **k: None,
        "sec_diff": lambda prev, code: say(f"## 0　与上次对比 {code}"),
        "sec_reasoning": lambda prev, jr, code: None,
        "write_snapshot": lambda *a, **k: None,
    }
    for k, v in stubs.items():
        setattr(mod, k, v)
    args = argparse.Namespace(no_peers=True, no_baserate=True, br_periods=12, no_diff=False,
                              peers=None)
    mod._OUT.clear()
    for code in ("AAA", "BBB"):
        mod.run_one(code, None, args)
    out = [l for l in mod._OUT if l.startswith("#")]
    want = ["# 报告 AAA", "## 0a　行业 AAA", "## 0　与上次对比 AAA", "## 1　持仓状况",
            "# 报告 BBB", "## 0a　行业 BBB", "## 0　与上次对比 BBB", "## 1　持仓状况"]
    ok("两只一起跑:各自的 0a / 0 留在各自报告里", out == want, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=DEFAULT)
    a = ap.parse_args()
    mod = load(a.file)
    fails = 0

    def ok(name, cond, got=""):
        nonlocal fails
        fails += not cond
        print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到 {got}"))

    print(f"== preport 不联网检查:{a.file}")
    check_annualize(mod, ok)
    check_two_holdings(mod, ok)
    print("✅ 全过" if not fails else f"❌ {fails} 项不过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
