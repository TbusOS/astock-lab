#!/usr/bin/env python3
"""check_journal —— journal 的行为检查。在临时目录里跑真的命令行,不碰真实日志。

用法:
    python3 scripts/check_journal.py                       # 查本仓的 journal.py
    python3 scripts/check_journal.py --journal <旧版路径>    # 反向验证:旧版必须不过

为什么有这道检查(2026-10-29 晚复核时一次发现的四个问题):
  1. `review --all` 读到 probs 为 None 的记录就崩 —— forecast 的 journal_log 在
     「不给动作」时本来就写 None(#41),所以这是代码的错不是数据的错。
  2. 10/16 以后 14 条判断没有概率和价位,到复核时没法算准不准;log 当时一声不吭。
  3. 9/2「减到仓位上限」没执行,之后 6 周两只又跌了三成多 —— 记录里没有「做了没有」。
  4. 一晚上改三版的中间记录(#59~#63)跟正式记录混在一起算命中率。
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_JOURNAL = HERE.parent / "skills" / "stock-analysis-workflow" / "scripts" / "journal.py"
FULL = ["--bear", "0.3", "--base", "0.5", "--bull", "0.2",
        "--bear-px", "80", "--base-px", "100", "--bull-px", "150"]


class Runner:
    def __init__(self, journal: Path, lab: Path):
        self.journal, self.lab = journal, lab
        self.fails = 0

    def run(self, *args):
        env = dict(os.environ, STOCK_LAB=str(self.lab))
        p = subprocess.run([sys.executable, str(self.journal), *args],
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr

    def rows(self):
        f = self.lab / "data" / "journal.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    def check(self, name, ok, detail=""):
        print(f"  {'✅' if ok else '❌'} {name}" + ("" if ok else f"\n       {detail[:300]}"))
        self.fails += 0 if ok else 1


def norm_cases(journal: Path, t: Runner):
    spec = importlib.util.spec_from_file_location("journal_under_test", journal)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    fn = getattr(mod, "norm_action", None)
    if fn is None:
        t.check("有 norm_action(把中文动作归成六类)", False, "没有这个函数")
        return
    cases = {"减仓(减到仓位上限)": "trim", "持有,不加仓": "hold", "清仓": "sell",
             "可加仓": "add", "不给动作(行业未定义)": "watch", "trim": "trim", "": "other"}
    bad = {k: (fn(k), v) for k, v in cases.items() if fn(k) != v}
    t.check("中文动作归类:「持有,不加仓」是 hold 不是 add", not bad, str(bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        lab = Path(d)
        (lab / "data").mkdir()
        t = Runner(a.journal, lab)
        print(f"== journal 行为检查:{a.journal}")

        rc, out = t.run("log", "000001", "--price", "100", "--action", "hold",
                        "--falsify", "x", "--check", "2027-01-01")
        t.check("hold 不带概率和价位 → 拒绝", rc != 0, out)
        rc, out = t.run("log", "000001", "--price", "100", "--action", "hold",
                        "--no-probs", "规则信号,不是概率判断", "--falsify", "x", "--check", "2027-01-01")
        t.check("带 --no-probs 理由 → 放行", rc == 0, out)

        rc, out = t.run("log", "000001", "--price", "100", "--action", "trim", *FULL,
                        "--falsify", "x", "--check", "2027-01-01")
        r = t.rows()[-1] if rc == 0 else {}
        t.check("概率和价位齐全 → EV 自动算(0.3×-20 + 0.5×0 + 0.2×50 = +4.0)",
                rc == 0 and r.get("ev") == 4.0, out)
        week = (dt.date.today() + dt.timedelta(days=7)).isoformat()
        t.check("trim 没给期限 → 默认 7 天后", r.get("deadline") == week, str(r.get("deadline")))

        rc, out = t.run("log", "000001", "--price", "100", "--action", "watch",
                        "--falsify", "x", "--check", "2027-01-01")
        t.check("watch 不要求概率", rc == 0, out)

        # 模拟 forecast journal_log 写的「不给动作」记录(probs/scenario_px 为 None,中文动作)
        f = lab / "data" / "journal.jsonl"
        legacy = {"id": 90, "logged_at": "2026-09-02T10:00:00", "code": "000002", "price": 80.6,
                  "action": "不给动作(行业是弱匹配推断的)", "ev": None, "probs": None,
                  "scenario_px": None, "falsify": "x", "check_date": "2026-10-31", "status": "open"}
        past = {"id": 91, "logged_at": "2026-09-02T10:00:00", "code": "000003", "price": 50,
                "action": "减仓(减到仓位上限)", "ev": -0.3, "probs": {"bear": 0.2, "base": 0.55, "bull": 0.25},
                "falsify": "x", "check_date": "2026-10-31", "deadline": "2026-09-09",
                "executed": None, "status": "open"}
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy, ensure_ascii=False) + "\n")
            fh.write(json.dumps(past, ensure_ascii=False) + "\n")
        rc, out = t.run("review", "--all")
        t.check("review --all 遇到 probs=None 不崩", rc == 0, out)
        t.check("review 列出过了期限还没执行的动作(#91)", "#91" in out and "没执行" in out, out)

        rc, out = t.run("exec", "91", "--done", "--note", "9/5 卖了")
        rc2, out2 = t.run("review", "--all")
        t.check("exec --done 以后不再报没执行", rc == 0 and "没执行" not in out2, out + out2)
        rc, out = t.run("exec", "91", "--skip")
        t.check("exec --skip 必须写理由", rc != 0, out)

        rc, out = t.run("supersede", "1", "--by", "2")
        rc2, out2 = t.run("review", "--all")
        t.check("supersede 以后 review 不再列它", rc == 0 and "#1　" not in out2, out + out2)
        rc, out = t.run("supersede", "999", "--by", "2")
        t.check("supersede 不存在的编号 → 拒绝", rc != 0, out)

        rc, out = t.run("edit", "2", "--check", "2027-02-02")
        t.check("edit 改复核日并留痕", rc == 0 and t.rows()[1].get("check_date") == "2027-02-02"
                and t.rows()[1].get("edits"), out)
        rc, out = t.run("edit", "90", "--no-probs", "当时按规则信号做的,没估概率")
        r90 = [x for x in t.rows() if x["id"] == 90][0]
        t.check("edit --no-probs 给旧记录补理由", rc == 0 and r90.get("no_probs_why")
                and r90.get("probs") is None, out)

        rc, out = t.run("close", "2", "--outcome", "wrong", "--actual-price", "70",
                        "--bench-return", "-20")
        r2 = [x for x in t.rows() if x["id"] == 2][0]
        t.check("close 带大盘涨跌 → 存超额(-30 − -20 = -10)", r2.get("excess_pct") == -10.0, out)
        rc, out = t.run("stats")
        t.check("stats 按动作分组并显示超额", rc == 0 and "按动作分组" in out and "-10.0" in out, out)
        t.check("stats 概览里有被取代条数", "被取代" in out, out)

        norm_cases(a.journal, t)

        print()
        if t.fails:
            print(f"❌ {t.fails} 项不过")
            return 1
        print("✅ 全过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
