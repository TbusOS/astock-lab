#!/usr/bin/env python3
"""check_preport —— preport 两个不联网就能查的毛病。

用法:python3 scripts/check_preport.py [--module <position_report.py>]   # 反向验证用旧版
      (--file 是 --module 的旧名字,两个都认)
      [--deep-report <deep_report.py>]                                   # 读快照的那一头

③ 快照不再覆盖(2026-11-02,做预测看板前):
   原来快照叫 `<日期>.json`,同一天重跑就把当天那份盖掉 —— 早上那次看到的数没了。
   改成 `<日期>_<时分>.json`,同一分钟再跑加 `_2`。读的两头(preport 找上一份、
   deep_report 按日期找)都要同时认新旧两种名字。

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
import json
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parent / "skills" / "astock-quote" / "scripts" / "position_report.py"
DEFAULT_DR = HERE.parent / "skills" / "finance-pdf-report" / "scripts" / "deep_report.py"


def load(path, name="pr_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
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


def _put(d, name):
    (d / name).write_text(json.dumps({"tag": name}), encoding="utf-8")


def check_snapshots(mod, ok):
    """快照:同一天写两次留两份;找上一份时新旧两种文件名都认,只看日期在今天之前的。"""
    need = [n for n in ("snapshot_dir", "write_snapshot", "load_prev_snapshot") if not hasattr(mod, n)]
    if need:
        ok("有快照读写函数", False, f"缺 {need}")
        return
    real_dir, real_time = mod.snapshot_dir, mod.time
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        mod.snapshot_dir = lambda code: root / code
        try:
            a = root / "AAA"
            a.mkdir()
            for n in ("2026-08-14.json", "2026-11-02_0930.json", "2026-11-02_1500.json"):
                _put(a, n)
            got = mod.load_prev_snapshot("AAA", "2026-11-02")
            ok("今天之前最近的一份:新旧名字混放时取到 2026-08-14.json",
               (got or {}).get("tag") == "2026-08-14.json", got)
            for n in ("2026-10-15_0930.json", "2026-10-15_1500.json"):
                _put(a, n)
            got = mod.load_prev_snapshot("AAA", "2026-11-02")
            ok("同一天有几份时取最晚那份(2026-10-15_1500)",
               (got or {}).get("tag") == "2026-10-15_1500.json", got)

            # 钟固定在同一分钟,连写两次 —— 第二次不许盖掉第一次
            fixed = time.strptime("2026-11-02 09:30:15", "%Y-%m-%d %H:%M:%S")
            mod.time = types.SimpleNamespace(strftime=lambda fmt, t=None: time.strftime(fmt, fixed))
            f1 = mod.write_snapshot("BBB", "测试", None)
            f2 = mod.write_snapshot("BBB", "测试", None)
            files = sorted(p.name for p in (root / "BBB").glob("*.json"))
            ok("同一天(同一分钟)连写两次 → 2 个文件", len(files) == 2, files)
            ok("文件名是 <日期>_<时分>", bool(files) and files[0] == "2026-11-02_0930.json", files)
            ok("write_snapshot 返回两个不同的文件", f1 is not None and f1 != f2, (f1, f2))
            got = mod.load_prev_snapshot("BBB", "2026-11-03")
            ok("第二天找上一份 → 取到同一分钟里后写的那份",
               f2 is not None and (got or {}).get("ts") == "2026-11-02 09:30"
               and json.loads(Path(f2).read_text(encoding="utf-8")) == got, got)
        finally:
            mod.snapshot_dir, mod.time = real_dir, real_time


def check_deep_report_snapshots(dr, ok):
    """deep_report 按日期找快照:`--date 2026-11-02` 要能找到 `2026-11-02_HHMM.json`。"""
    if not hasattr(dr, "load_snapshot"):
        ok("deep_report 有 load_snapshot", False, "没有这个函数")
        return
    real = dr.SNAPDIR
    with tempfile.TemporaryDirectory() as d:
        dr.SNAPDIR = Path(d)
        try:
            a = Path(d) / "AAA"
            a.mkdir()
            for n in ("2026-08-14.json", "2026-11-02_0930.json", "2026-11-02_1500.json"):
                _put(a, n)

            def tag(*args):
                try:
                    snap, f = dr.load_snapshot("AAA", *args)
                except SystemExit as e:
                    return f"SystemExit: {e}"
                return (snap or {}).get("tag")

            ok("deep_report 不给日期 → 最新一份", tag() == "2026-11-02_1500.json", tag())
            ok("deep_report --date 2026-11-02 → 那天最晚的一份",
               tag("2026-11-02") == "2026-11-02_1500.json", tag("2026-11-02"))
            ok("deep_report --date 2026-08-14 → 老名字照样认",
               tag("2026-08-14") == "2026-08-14.json", tag("2026-08-14"))
            ok("deep_report --date 写完整文件名 → 就是那一份",
               tag("2026-11-02_0930") == "2026-11-02_0930.json", tag("2026-11-02_0930"))
            ok("deep_report 那天没有快照 → 报错并列出已有的",
               str(tag("2026-10-01")).startswith("SystemExit") and "2026-08-14" in str(tag("2026-10-01")),
               tag("2026-10-01"))
        finally:
            dr.SNAPDIR = real


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
    ap.add_argument("--module", "--file", dest="file", type=Path, default=DEFAULT,
                    help="被测的 position_report.py(反向验证时指向旧版)")
    ap.add_argument("--deep-report", dest="deep_report", type=Path, default=DEFAULT_DR,
                    help="被测的 deep_report.py(反向验证时指向旧版)")
    a = ap.parse_args()
    mod = load(a.file)
    fails = 0

    def ok(name, cond, got=""):
        nonlocal fails
        fails += not cond
        print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到 {got}"))

    print(f"== preport 不联网检查:{a.file}")
    check_annualize(mod, ok)
    check_snapshots(mod, ok)          # 要在 check_two_holdings 之前:那边会把快照函数换成替身
    check_two_holdings(mod, ok)
    print(f"== deep_report 读快照:{a.deep_report}")
    check_deep_report_snapshots(load(a.deep_report, "dr_under_test"), ok)
    print("✅ 全过" if not fails else f"❌ {fails} 项不过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
