#!/usr/bin/env python3
"""check_review_scoring —— 复核季度预测时，「落在区间里」之外还要给中枢误差打分。

用法:python3 scripts/check_review_scoring.py [--file <backtest.py>]   # 反向验证用旧版

为什么有这道检查(2026-10-30):
  9/2 那 5 份三季度预测，按「落在区间里」只中 2 只，看着很差;可 5 只的营收中枢误差
  全部小于走前回测的历史中位数 12.7%,杰瑞、深科达只偏 4%~6%,是因为只有 1~2 个方法
  能用、区间太窄才被判「没中」。反过来，区间宽(中位 45%)时落在里面也说明不了什么。
  只看一个指标，两头都会看错。

  这条改进原本的想法是「把区间收窄」,被 forecast.py 里 9/2 的走前回测否了
  (并集命中 61% vs 交集 35%,宽度 <10% 的 17 个只中 1 个)。所以区间不动，改评分。
"""
import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parent / "skills" / "forecast-from-facts" / "scripts" / "backtest.py"


def load(path: Path):
    sys.path.insert(0, str(path.parent))        # backtest import 同目录的 quarterly
    spec = importlib.util.spec_from_file_location("bt_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    print(f"== 季度预测评分:{a.file}")
    g = getattr(mod, "grade", None)
    if g is None:
        ok("有 grade(落在区间? 偏中枢, 历史中位)", False, "没有这个函数")
    else:
        cases = [((True, 3.0, 12.7), "准"), ((True, -20.0, 12.7), "区间宽"),
                 ((False, 5.6, 12.7), "区间太窄"), ((False, -20.0, 12.7), "错"),
                 ((None, None, 12.7), "—")]
        for args, want in cases:
            got = g(*args)
            ok(f"grade{args} 含「{want}」", want in got, got)
        got = g(True, 3.0, None)
        ok("没有走前回测参照时不硬判准不准", "✅ 准" not in got and "参照" in got, got)

    # build 端到端:顶掉取数，看表头、评价、汇总句
    pred = {"code": "000001", "预测期": "2026Q3", "可用方法数": 1, "_文件": "x.json",
            "营收区间": {"low": 100e8, "high": 105e8, "mid": 102e8},
            "归母净利区间": {"low": 10e8, "high": 12e8, "mid": 11e8}}
    mod.load_predictions = lambda d: [pred]
    mod.load_journal = lambda p: []
    mod.actual_quarter = lambda raw, code, period: {"营收": 108e8, "归母净利": 11.5e8}
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d) / "raw"
        raw.mkdir()
        (Path(d) / "backtest").mkdir()
        (Path(d) / "backtest" / "revenue-summary.json").write_text(
            json.dumps({"并集": {"中位绝对偏差": 12.7}}), encoding="utf-8")
        md = mod.build(raw, Path(d), today="2026-10-30")
    ok("表里有「区间宽」一列", "区间宽" in md, md[:400])
    ok("108 不在 100~105 里、但只偏中枢 +5.9% → 判「中枢准、区间太窄」", "区间太窄" in md, md[:800])
    ok("汇总句写出偏中枢不超过历史中位的只数和参照值",
       "偏中枢不超过历史中位" in md and "12.7%" in md, md[:900])

    print("✅ 全过" if not fails else f"❌ {fails} 项不过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
