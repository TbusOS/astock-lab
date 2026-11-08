#!/usr/bin/env python3
"""check_journal —— journal 的行为检查。在临时目录里跑真的命令行,不碰真实日志。

用法:
    python3 scripts/check_journal.py                       # 查本仓的 journal.py 和 journal_log.py
    python3 scripts/check_journal.py --journal <旧版路径>    # 反向验证:旧版必须不过
    python3 scripts/check_journal.py --journal-log <旧版 journal_log.py>

为什么有这道检查(2026-10-29 晚复核时一次发现的四个问题):
  1. `review --all` 读到 probs 为 None 的记录就崩 —— forecast 的 journal_log 在
     「不给动作」时本来就写 None(#41),所以这是代码的错不是数据的错。
  2. 10/16 以后 14 条判断没有概率和价位,到复核时没法算准不准;log 当时一声不吭。
  3. 9/2「减到仓位上限」没执行,之后 6 周两只又跌了三成多 —— 记录里没有「做了没有」。
  4. 一晚上改三版的中间记录(#59~#63)跟正式记录混在一起算命中率。

2026-11-02 加的两条(做预测看板前,先让旧记录不再被删):
  5. forecast 的 journal_log 同一天重出报告时把当天旧条**删掉** —— 旧版当时也是一次
     判断,删了就再也核不到。改成标「被取代」、指向新条。
  6. 被取代的旧版不进复核 —— 它到了复核日照样该对一遍,只是不算命中率。
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
DEFAULT_JOURNAL_LOG = HERE.parent / "skills" / "forecast-from-facts" / "scripts" / "journal_log.py"
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


def _no_action_stub():
    """journal_log.write 要的最小输入:一次「不给动作」的决策(决策.ok = False)。"""
    a = {"决策": {"ok": False, "现价": 10.0, "why": "检查用的桩数据", "复核日": "2027-01-01"},
         "领先": {}, "基准率": {}, "动作": "不给动作(检查用)"}
    return a, {}, {}, {}, None          # a, f, position, cash, gm_min


def journal_log_cases(path: Path, t: Runner):
    """同一天同一只票出两次报告:旧条留在文件里,标「被取代」并指向新条。"""
    spec = importlib.util.spec_from_file_location("journal_log_under_test", path)
    jl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jl)
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d) / "data" / "raw"
        raw.mkdir(parents=True)
        f = raw.parent / "journal.jsonl"

        def rows():
            return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

        a, fc, pos, cash, gm = _no_action_stub()
        jl.write(raw, "000009", a, fc, pos, cash, gm)
        rec2 = jl.write(raw, "000009", a, fc, pos, cash, gm)
        rs = rows()
        t.check("同一天出两次报告 → 文件里 2 行(旧条不删)", len(rs) == 2, str(rs)[:300])
        if len(rs) == 2:
            old, new = rs
            t.check("第 1 行标「被取代」、指向第 2 行",
                    old.get("status") == "superseded" and old.get("superseded_by") == new.get("id"),
                    str(old))
            t.check("被取代的理由写「同日重出报告」",
                    old.get("superseded_note") == "同日重出报告", str(old.get("superseded_note")))
            t.check("第 2 行是未结", new.get("status") == "open", str(new.get("status")))
        L = []
        jl.render(rec2, "10.1", L)
        t.check("报告里说清旧条是「被取代」不是被覆盖", "被取代" in "\n".join(L), "\n".join(L)[:300])

        # 再出第三次:只取代当天还开着的那条;昨天的、别的票的都不动
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": 50, "logged_at": f"{yday}T10:00:00", "code": "000009",
                                 "price": 9.0, "status": "open"}) + "\n")
            fh.write(json.dumps({"id": 51, "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
                                 "code": "000010", "price": 5.0, "status": "open"}) + "\n")
        rec3 = jl.write(raw, "000009", a, fc, pos, cash, gm)
        by = {r["id"]: r for r in rows()}
        t.check("第三次:上一版改指向第三版,最早那版仍指向第二版",
                by.get(2, {}).get("superseded_by") == rec3["id"]
                and by.get(1, {}).get("superseded_by") == 2, str(by)[:300])
        t.check("昨天的记录、别的票的记录不动",
                by.get(50, {}).get("status") == "open" and by.get(51, {}).get("status") == "open",
                str([by.get(50), by.get(51)]))

        # 预测看板记的那条(thesis 以「预测存档」开头)同一天同一只票:报告不取代它
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": 52, "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
                                 "code": "000009", "price": 9.5, "action": "watch", "status": "open",
                                 "thesis": "预测存档 data/forecasts/2026-11-03/150000"}, ensure_ascii=False) + "\n")
        jl.write(raw, "000009", a, fc, pos, cash, gm)
        by = {r["id"]: r for r in rows()}
        t.check("同一天预测看板记的那条(预测存档)不被报告取代", by.get(52, {}).get("status") == "open"
                and "superseded_by" not in by.get(52, {}), str(by.get(52)))


def superseded_review_cases(t: Runner):
    """到期的被取代旧版:review 单独列一段;结掉以后命中率不变。"""
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    old = {"id": 95, "logged_at": "2026-08-19T10:00:00", "code": "000003", "price": 50,
           "action": "减仓(减到仓位上限)", "ev": -0.3,
           "probs": {"bear": 0.2, "base": 0.55, "bull": 0.25},
           "falsify": "x", "check_date": yday, "status": "superseded",
           "superseded_by": 2, "superseded_note": "同日重出报告"}
    with (t.lab / "data" / "journal.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(old, ensure_ascii=False) + "\n")
    title = "被取代的旧版(照样到期核对)"
    rc, out = t.run("review")
    t.check("review 单独列出到期的被取代旧版(#95)",
            rc == 0 and title in out and "#95" in out and out.index(title) < out.index("#95"), out)
    t.check("被取代的旧版不混进「待复核」那一段", out.count("#95") == 1, out)

    _, before = t.run("stats")
    rc, out = t.run("close", "95", "--outcome", "right", "--actual-price", "60")
    r95 = [x for x in t.rows() if x["id"] == 95][0]
    t.check("结掉被取代的旧版:记下结果,但仍标「被取代」",
            rc == 0 and r95.get("status") == "superseded" and r95.get("outcome") == "right", out)
    _, after = t.run("stats")
    # 概览表里「命中率」和「被取代」两行(时间线里也会出现「不算命中率」字样,不算)
    head = ("| 命中率", "| 被取代")
    hit = [ln for ln in before.splitlines() if ln.startswith(head)]
    hit2 = [ln for ln in after.splitlines() if ln.startswith(head)]
    t.check("结掉被取代的旧版以后命中率不变", hit and hit == hit2, f"{hit} → {hit2}")
    rc, out = t.run("review")
    t.check("结掉以后 review 不再列它", "#95" not in out, out)
    rc, out = t.run("close", "95", "--outcome", "wrong", "--actual-price", "40")
    t.check("被取代的旧版再结一次:先说已经核过、本次覆盖(不悄悄盖掉)", rc == 0 and "已经核过" in out
            and "right" in out, out)


def review_wording_cases(journal: Path):
    """只有被取代的旧版到期、未结的都没到期:review 不能先说「没有到期待复核」再列出到期的旧版。"""
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    with tempfile.TemporaryDirectory() as d:
        lab = Path(d)
        (lab / "data").mkdir()
        t = Runner(journal, lab)
        rows = [{"id": 96, "logged_at": "2026-08-19T10:00:00", "code": "000004", "price": 20, "action": "hold",
                 "probs": None, "ev": None, "falsify": "x", "check_date": yday, "status": "superseded",
                 "superseded_by": 97}, {"id": 97, "logged_at": "2026-08-19T15:00:00", "code": "000004", "price": 20,
                                       "action": "hold", "falsify": "x", "check_date": "2099-01-01", "status": "open"}]
        (lab / "data" / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        rc, out = t.run("review")
        t.check("只有被取代的旧版到期:不说「没有到期待复核」,照列 #96", rc == 0 and "没有到期待复核" not in out
                and "#96" in out, out)
        return t.fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    ap.add_argument("--journal-log", dest="journal_log", type=Path, default=DEFAULT_JOURNAL_LOG,
                    help="forecast-from-facts 的 journal_log.py(反向验证时指向旧版)")
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
        t.check("没给概率的记录:不打「EV None%」「熊None」,写没给概率", "EV None" not in out and "熊None" not in out
                and "没给概率" in out, out)
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

        superseded_review_cases(t)
        t.fails += review_wording_cases(a.journal)
        norm_cases(a.journal, t)
        print(f"== journal_log 同日重出报告:{a.journal_log}")
        journal_log_cases(a.journal_log, t)

        print()
        if t.fails:
            print(f"❌ {t.fails} 项不过")
            return 1
        print("✅ 全过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
