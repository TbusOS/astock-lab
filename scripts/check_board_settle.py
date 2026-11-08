#!/usr/bin/env python3
"""check_board_settle —— board.py resolve 给已结算的题补记不复权收盘、结算记录的读法、取收盘价的两处提示。

用法:
    python3 scripts/check_board_settle.py        # 退出码 0 才算过

为什么单独一个文件:check_board_cli.py 已经 700 多行。这里借用它的临时工作台(Lab)和合成日线,
不联网、不碰真实数据;代码、价位、日期都是合成的(加的题用编的代码 CODE、编的开题日 OPENED,
日线照抄合成的 300502)。
写法和 check_board_scoring.py 一样:每块一个函数,登记进 CHECKS。

要点:
  - 存档只追加:补记收盘价是给同一题号再追加一整行,读的地方一律以最后一行为准,不能多算;
  - 起点和那天的不复权收盘要和后复权那两根是同一天才记(和 add-question 记 p0_close 一样);
  - 不复权日线取不到时,加题和结算各说各的后果。
"""
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import check_board_cli as cli  # noqa: E402  合成工作台和合成日线;它会先加载 fb_common
import board_scores  # noqa: E402
import board_settle  # noqa: E402

FAILS = 0
LATER = cli.LATER_NOW
CODE = "002222"          # 编的代码:加题、结算只按题目里的代码取日线,工作台配置不用改
OPENED = "2026-08-12"    # 编的开题日,早于第一次存档(2026-10-30),所以算换算进来的旧记录


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:500]))
    if not cond:
        FAILS += 1


def _prices(bad_close: bool = False) -> dict:
    """合成日线(cli.fake_prices + 之后的日线),另加一只 CODE:后复权和不复权都照抄 300502。
    bad_close=True 时 CODE 的不复权日线是坏的(取不到)。"""
    out = cli.fake_prices(cli.later_bars())
    for k in ("hfq", "close"):
        out[k][CODE] = list(out[k]["300502"])
    if bad_close:
        out["close"][CODE] = "坏的"
    return out


def _closes_of(prices_obj: dict, code: str) -> dict:
    return {d: c for d, c in prices_obj["close"][code]}


def _strip_closes(lab) -> None:
    """把结算记录和题目记录改回「补记功能出现以前」的样子:没有 p0_close、start_close、end_close。
    只在临时工作台里改,模拟真实存档里那种老记录。"""
    for name, drop in (("outcomes.jsonl", ("start_close", "end_close")), ("chat_questions.jsonl", ())):
        rows = cli.attribution._read_lines(lab.root / name)
        out = []
        for r in rows:
            r = {k: v for k, v in r.items() if k != "p0_close"}
            if isinstance(r.get("resolution"), dict):
                r["resolution"] = {k: v for k, v in r["resolution"].items() if k not in drop}
            out.append(r)
        (lab.root / name).write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in out), encoding="ascii")


def check_backfill():
    """已经结算、但记录里没有不复权收盘的题:再跑 resolve 给它追加一整行带 start_close / end_close 的;
    再跑一次不重复追加;读结算记录的地方都只认最后一行。"""
    tmp = cli._tmp()
    try:
        lab = cli.Lab(tmp)
        prices_obj = _prices()
        lab.fakes(prices_obj=prices_obj)
        rc, out, err = lab.run("add-question", "--code", CODE, "--type", "above_on", "--ratio", "0.9",
                               "--day", "2026-12-31", "--opened", OPENED, "--prob", "70", "--author", "claude",
                               "--note", "合成:旧记录换算")
        rc2, out2, err2 = lab.run("resolve", now=LATER)
        rows = lab.jsonl("outcomes.jsonl")
        ok("准备:加题、结算各一次,退出码 0,一行结算记录", rc == 0 and rc2 == 0 and len(rows) == 1,
           (rc, rc2, err[-200:], err2[-200:], len(rows)))
        if len(rows) != 1:
            return
        qid = rows[0]["id"]
        _strip_closes(lab)
        rc, out, err = lab.run("resolve", now=LATER)
        rows = lab.jsonl("outcomes.jsonl")
        close = _closes_of(prices_obj, CODE)
        last = rows[-1] if rows else {}
        res = last.get("resolution") or {}
        ok("resolve:老记录没有收盘价 → 同一题号追加一整行,带起点和那天的不复权收盘",
           rc == 0 and len(rows) == 2 and last.get("id") == qid and res.get("start_close") == close[OPENED]
           and res.get("end_close") == close[res.get("end_day", "")] and res.get("outcome") == rows[0]["resolution"]["outcome"]
           and res.get("closes_added_at"), (rc, len(rows), res, err[-300:]))
        ok("resolve:输出写了补记几道", "补记" in out and "1 道" in out, out[-300:])
        rc, out, err = lab.run("resolve", now=LATER)
        ok("resolve:再跑一次不再追加", rc == 0 and len(lab.jsonl("outcomes.jsonl")) == 2, (rc, err[-200:]))
        got = board_settle.resolved(lab.root)
        ok("读结算记录 board_settle.resolved:同一题号只给一条,是最后那行", len(got) == 1
           and got[0]["resolution"].get("end_close") == res.get("end_close"), [r["resolution"] for r in got])
        ok("还没结算的题 board_settle.pending 不把它算进去", qid not in {q["id"] for q in board_settle.pending(lab.root)})
        sc = board_scores.scores_data(lab.root, lab.root.parent, date(2027, 3, 1))
        jq = [j for j in sc["journal_questions"] if j["id"] == qid]
        ok("成绩页的数 board_scores.scores_data:已结算 1 道(不是 2),旧记录那栏只列一次",
           sc["totals"]["resolved"] == 1 and sc["totals"]["journal_settled"] == 1 and len(jq) == 1, (sc["totals"], len(jq)))
        start = close[OPENED]
        ok("旧记录那栏写价位线和收盘价(用补记的那行)", jq and f"收盘高于 {start * 0.9:,.2f} 元(起点 {start:,.2f} 的 90.0%"
           in jq[0]["said"] and f"收盘 {res.get('end_close', 0):,.2f} 元,后复权算是起点价的" in (jq[0]["actual"] or ""),
           jq[:1])
        rc, out, err = lab.run("confirm", "--qid", qid, "--probs", "luck=100", "--note", "合成", now=LATER)
        ok("confirm:补记过的题号照样认", rc == 0, (rc, err[-200:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_same_day():
    """不复权收盘要和后复权那两根同一天才记:起点那天缺不复权日线 → 两个都不记。"""
    q = {"id": "x", "code": CODE, "opened": OPENED}
    res = {"end_day": "2026-12-31", "outcome": 1}
    hfq = {"2026-08-11": 100.0, OPENED: 101.0, "2026-12-31": 120.0}
    same = {"2026-08-11": 50.0, OPENED: 50.5, "2026-12-31": 60.0}
    gap = {"2026-08-11": 50.0, "2026-12-31": 60.0}                   # 起点那天没有不复权日线
    good = board_settle._with_ratios(q, res, {}, hfq, same)
    bad = board_settle._with_ratios(q, res, {}, hfq, gap)
    ok("同一天:记 start_close、end_close", good.get("start_close") == 50.5 and good.get("end_close") == 60.0, good)
    ok("起点那天缺不复权日线:不拿前一天的顶上,两个都不记", "start_close" not in bad and "end_close" not in bad, bad)
    end_gap = {OPENED: 50.5, "2026-12-30": 59.0}
    bad2 = board_settle._with_ratios(q, res, {}, hfq, end_gap)
    ok("那天缺不复权日线:两个都不记", "start_close" not in bad2 and "end_close" not in bad2, bad2)


def check_close_warnings():
    """不复权日线取不到:加题说「这道题不记起点的不复权收盘」,结算说「结算记录不带收盘价」,两边都照常做完。"""
    tmp = cli._tmp()
    try:
        lab = cli.Lab(tmp)
        lab.fakes(prices_obj=_prices(bad_close=True))
        rc, out, err = lab.run("add-question", "--code", CODE, "--type", "above_on", "--ratio", "0.9",
                               "--day", "2026-12-31", "--opened", OPENED, "--prob", "70", "--author", "claude",
                               "--note", "合成")
        ok("add-question:取不到不复权日线 → 照常加题,提示这道题不记起点价", rc == 0 and "这道题不记起点" in err
           and "结算记录" not in err, (rc, err[-300:]))
        rc, out, err = lab.run("resolve", now=LATER)
        ok("resolve:取不到不复权日线 → 照常结算,提示结算记录不带收盘价", rc == 0 and "结算记录不带收盘价" in err
           and len(lab.jsonl("outcomes.jsonl")) == 1, (rc, err[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_confirm_who():
    """confirm --who 统一成小写;不是 user / claude / codex 的照样记下,但提示只有 user 算人确认
    (user 自己敲命令,手误不能悄悄变成「没确认」)。"""
    tmp = cli._tmp()
    try:
        lab = cli.Lab(tmp)
        lab.fakes(prices_obj=_prices())
        lab.run("add-question", "--code", CODE, "--type", "above_on", "--ratio", "1.5", "--day", "2026-12-31",
                "--opened", OPENED, "--prob", "70", "--author", "claude", "--note", "合成:会错的题")
        lab.run("resolve", now=LATER)
        qid = (lab.jsonl("outcomes.jsonl") or [{}])[-1].get("id", "")

        def confirm(who):
            return lab.run("confirm", "--qid", qid, "--probs", "luck=60,system=40", "--note", "合成", "--who", who,
                           now=LATER)

        def label():
            sc = board_scores.scores_data(lab.root, lab.root.parent, date(2027, 3, 1))
            return next((j["from"] for j in sc["journal_questions"] if j["id"] == qid), None)

        rc, out, err = confirm("User")
        ok("--who User → 记成 user,算人确认,不提示", rc == 0 and lab.jsonl("reviews.jsonl")[-1]["who"] == "user"
           and "只有 user" not in err and label() == "人确认", (rc, lab.jsonl("reviews.jsonl")[-1:], err[-200:], label()))
        rc, out, err = confirm("Claude")
        ok("--who Claude → 记成 claude,不提示", rc == 0 and lab.jsonl("reviews.jsonl")[-1]["who"] == "claude"
           and "只有 user" not in err, (rc, lab.jsonl("reviews.jsonl")[-1:], err[-200:]))
        rc, out, err = confirm("usr")
        ok("--who usr(手误)→ 照样记下,退出码 0,提示只有 user 算人确认", rc == 0
           and lab.jsonl("reviews.jsonl")[-1]["who"] == "usr" and "只有 user 算人确认" in err, (rc, err[-300:]))
        ok("手误那条在成绩页不算人确认:前面 user 那次照旧是人确认", label() == "人确认", label())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_suggest_why():
    """错题的原因初值连证据一起存(suggest_why:触发了的规则,一条一句),resolve 也把证据打出来。
    合成题:说 70 种会站上 1.5 倍、结果没站上 → 给实际那一项 30 种,「正常的运气」那条规则触发。"""
    tmp = cli._tmp()
    try:
        lab = cli.Lab(tmp)
        lab.fakes(prices_obj=_prices())
        lab.run("add-question", "--code", CODE, "--type", "above_on", "--ratio", "1.5", "--day", "2026-12-31",
                "--opened", OPENED, "--prob", "70", "--author", "claude", "--note", "合成:会错的题")
        rc, out, err = lab.run("resolve", now=LATER)
        row = (lab.jsonl("outcomes.jsonl") or [{}])[-1]
        why = row.get("suggest_why")
        ok("resolve:错题存了原因初值和证据(suggest_why,一条一句)", rc == 0 and isinstance(row.get("suggest"), dict)
           and isinstance(why, list) and any("本来就有这么大的机会" in w for w in why), (rc, row.get("suggest"), why))
        ok("resolve:证据和初值是同一次算的(按证据重算概率一样)",
           isinstance(why, list) and row.get("suggest") == cli.attribution.probs_from(
               [{"cause": "luck", "points": cli.attribution.BONUS["luck"], "why": w} for w in why])
           if hasattr(cli.attribution, "probs_from") else False, row.get("suggest"))
        ok("resolve:输出里每道错题下面写证据", "证据:" in out and "本来就有这么大的机会" in out, out[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_chat_last_row():
    """chat_questions.jsonl 里同一题号有两行(手改过、或以后补字段追加一整行):结算和成绩页都以最后一行为准。"""
    tmp = cli._tmp()
    try:
        root = tmp / "data" / "forecasts"
        root.mkdir(parents=True)
        q = {"id": f"journal/{OPENED}/claude/{CODE}/touch/r0.8-n63", "run": f"journal/{OPENED}", "author": "claude",
             "code": CODE, "type": "touch", "opened": OPENED, "p0_hfq": 100.0, "params": {"ratio": 0.8, "n": 63},
             "source": "journal"}
        rows = [{**q, "forecast": {"pct": 30}, "note": "合成:第一行"}, {**q, "forecast": {"pct": 40}, "note": "合成:后补的一行"}]
        (root / "chat_questions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                                   encoding="utf-8")
        got = board_settle.all_questions(root)
        ok("结算读题 all_questions:同一题号只给一条,是最后那行", [g.get("note") for g in got] == ["合成:后补的一行"],
           [g.get("note") for g in got])
        sc = board_scores.scores_data(root, root.parent, date(2026, 11, 3))
        jq = [j for j in sc["journal_questions"] if j["id"] == q["id"]]
        ok("成绩页读题:也是最后那行(两边一致)", [j["note"] for j in jq] == ["合成:后补的一行"], jq)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_replay_timeout():
    """重跑时 git 或重算子进程超时:退出码 1,写一行原因,不甩一整段报错。"""
    import argparse
    import os
    import subprocess
    import check_board_pages as pages
    tmp = cli._tmp()
    old = os.environ.get("STOCK_LAB")
    try:
        lab = cli.Lab(tmp)
        run, _ = pages.make_lab(lab.lab)
        os.environ["STOCK_LAB"] = str(lab.lab)

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(["git", "worktree", "add"], 60)
        try:
            rc = board_settle.cmd_replay(argparse.Namespace(run=f"{run.parent.name}/{run.name}", code="HEAD",
                                                            corrected=None), runner=slow)
        except subprocess.TimeoutExpired as e:
            rc = f"没接住:{e}"
        ok("replay:超时 → 退出码 1(不抛出),不写 replays.jsonl", rc == 1 and not lab.jsonl("replays.jsonl"), rc)
    finally:
        os.environ.pop("STOCK_LAB", None) if old is None else os.environ.__setitem__("STOCK_LAB", old)
        shutil.rmtree(tmp, ignore_errors=True)


CHECKS = [check_same_day, check_close_warnings, check_backfill, check_confirm_who, check_suggest_why,
          check_chat_last_row, check_replay_timeout]


def main():
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
