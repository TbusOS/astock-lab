#!/usr/bin/env python3
"""check_board_scoring —— 预测看板「评分」(skills/forecast-board/scripts/scoring.py)的检查。

用法:
    python3 scripts/check_board_scoring.py        # 退出码 0 才算过

不联网、不碰真实数据,题目和结算结果都是手造的合成数字。
成绩页那几块(谁确认的原因概率)在临时目录里搭一个只有结算记录和原因记录的存档根,跑完删掉。
写法和 check_board_engine.py 一样:每块一个函数,登记进 CHECKS。
"""
import json
import math
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401  先于 attribution / board_scores,和正式运行的顺序一样
import attribution  # noqa: E402
import board_scores  # noqa: E402
import render_scores  # noqa: E402
import scoring  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:400]))
    if not cond:
        FAILS += 1


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) < tol


def rec(author, qtype, forecast, outcome, *, opened="2026-11-03", params=None, code="300308",
        value=None, after="2027-02-01", run="r1"):
    """一条评分记录 = 题目 + 结算结果("resolution")。"""
    return {"id": f"{run}/{author}/{code}/{qtype}", "run": run, "author": author, "code": code, "type": qtype,
            "opened": opened, "p0_hfq": 100.0, "params": params or {"n": 63}, "forecast": forecast,
            "resolution": {"outcome": outcome, "end_day": "2027-02-01", "resolved_at": "x",
                           "value": value, "note": "", "scorable_after": after}}


def check_basics():
    ok("Brier", close(scoring.brier(0.7, 1), 0.09))
    ok("多类 Brier", close(scoring.brier_multi({"a": 0.5, "b": 0.5}, "a"), 0.5))
    ok("落在区间里只算宽度", close(scoring.interval_score(-0.1, 0.1, 0.0, 0.2), 0.2))
    ok("落在外面加罚", close(scoring.interval_score(-0.1, 0.1, 0.2, 0.2), 1.2))
    ok("落在下面也加罚", close(scoring.interval_score(-0.1, 0.1, -0.2, 0.2), 1.2))
    ok("覆盖比例", close(scoring.coverage([True, True, False, True]), 0.75))
    ok("覆盖比例:空列表给 None", scoring.coverage([]) is None)
    ok("常量", scoring.MIN_NEFF_FOR_CHART == 20)
    qs = [{"opened": "2026-11-03", "type": "up", "params": {"n": 63}, "code": c} for c in ("300308", "300502")]
    ok("两只同周同题只算 1 个独立样本", scoring.n_eff(qs) == 1)
    ok("不同周算 2 个", scoring.n_eff(qs + [{**qs[0], "opened": "2026-11-10"}]) == 2)
    ok("同周同题型不同期限算 2 个", scoring.n_eff(qs + [{**qs[0], "params": {"n": 126}}]) == 2)
    ok("同周同期限不同题型算 2 个", scoring.n_eff(qs + [{**qs[0], "type": "interval"}]) == 2)
    ok("ISO 年周跨年:2026-12-31 与 2027-01-01 同一周", scoring.n_eff(
        [{**qs[0], "opened": "2026-12-31"}, {**qs[0], "opened": "2027-01-01"}]) == 1)


def check_report():
    iv = {"lo": 0.9, "hi": 1.1, "pct": 50}
    iv_params = {"n": 63, "lo": "25", "hi": "75"}
    iv80 = {"lo": 0.8, "hi": 1.2, "pct": 80}
    iv80_params = {"n": 63, "lo": "10", "hi": "90"}
    recs = [
        # baseline:up 两条(p=70 实际涨 → 命中;p=50 → 半个命中),一条 interval 区间内
        rec("baseline", "up", {"pct": 70}, 1),
        rec("baseline", "up", {"pct": 50}, 0, code="300502"),
        rec("baseline", "interval", iv, 1, params=iv_params, value=1.0),
        # system:up p=20 实际 1 → 没中;interval 落在外面;分类题
        rec("system", "up", {"pct": 20}, 1),
        rec("system", "interval", iv80, 0, params=iv80_params, value=1.5),
        rec("system", "bottom_month", {"2026-11": 60, "2026-12": 40}, "2026-11", params={"window": 63}),
        # rule:不进 Brier
        rec("rule", "buy_vs_now", None, 1, params={"n": 63}, value=0.1),
        rec("rule", "buy_vs_now", None, 0, params={"n": 63}, value=-0.1, code="300502"),
        # 没买点:不计分
        rec("rule", "buy_vs_now", None, None, params={"n": 63}, code="300999"),
        # 还没到可评分日:pending
        rec("baseline", "up", {"pct": 90}, 1, after="2027-06-01", code="300777"),
    ]
    rep = scoring.layer_report(recs, today="2027-03-01")
    b, s = rep["layers"]["baseline"], rep["layers"]["system"]
    ok("没到期的只算 pending,不进分数", rep["pending"] == 1 and b["n"] == 3, (rep["pending"], b))
    ok("rule 单独列,不进层", "rule" not in rep["layers"] and rep["rule"] == {"n": 2, "wins": 1, "win_rate": 0.5},
       rep.get("rule"))
    ok("outcome None 记 unscored", rep["unscored"] == 1, rep["unscored"])
    ok("baseline 平均 Brier = (0.09 + 0.25 + 0 在 up 外不算)/2", close(b["brier"], (0.09 + 0.25) / 2), b["brier"])
    ok("baseline 方向命中率 (1 + 0.5)/2 = 0.75(50 算半个)", close(b["direction_hit"], 0.75), b["direction_hit"])
    ok("system 方向命中率 0(p=20 但涨了)", close(s["direction_hit"], 0.0), s["direction_hit"])
    w = math.log(1.1) - math.log(0.9)
    ok("baseline 区间得分 = 对数宽度(落在里面)", close(b["interval_score"], w), b["interval_score"])
    w80 = math.log(1.2) - math.log(0.8)
    pen = 2 / 0.2 * (math.log(1.5) - math.log(1.2))
    ok("system 区间得分:alpha=0.2、罚对数超出量", close(s["interval_score"], w80 + pen), s["interval_score"])
    ok("覆盖比例:baseline 1,system 0", close(b["coverage"], 1.0) and close(s["coverage"], 0.0), (b, s))
    ok("分档覆盖:50% 档 / 80% 档", close(b["coverage_by_level"]["50"], 1.0)
       and close(s["coverage_by_level"]["80"], 0.0), (b, s))
    ok("分类题 Brier 单列", close(s["brier_multi"], 0.16 + 0.16), s["brier_multi"])
    ok("system 的二分类 Brier 只含 up", close(s["brier"], 0.64), s["brier"])
    ok("n_eff:baseline 同周 up(两只)+ interval = 2", b["n_eff"] == 2, b["n_eff"])
    d = rep["diff_vs_baseline"]["system"]
    ok("system 减 baseline 只比共同的那 1 题(300308 的 up):0.64 − 0.09、-1 − 0",
       d["n_matched"] == 1 and close(d["brier"], 0.64 - 0.09) and close(d["direction_hit"], -1.0)
       and "interval_score" not in d, d)
    ok("baseline 自己不出差值", "baseline" not in rep["diff_vs_baseline"])
    ok("没有的层不出现(codex 没数据)", "codex" not in rep["layers"])
    rep2 = scoring.layer_report(recs, today="2027-07-01")
    ok("today 往后推,pending 变 0、数据进分数", rep2["pending"] == 0 and rep2["layers"]["baseline"]["n"] == 4, rep2["pending"])
    miss = rec("baseline", "up", {"pct": 70}, 1)
    del miss["resolution"]["scorable_after"]
    ok("缺 scorable_after 算 pending,不进分数", scoring.layer_report([miss], today="2099-01-01")["pending"] == 1)
    ok("没结算的(无 resolution)算 pending", scoring.layer_report(
        [{**recs[0], "resolution": None}], today="2027-03-01")["pending"] == 1)


def check_versions():
    recs = [rec("system", "up", {"pct": 80}, 1, run="r1"), rec("baseline", "up", {"pct": 50}, 1, run="r1"),
            rec("system", "up", {"pct": 80}, 0, run="r2", opened="2026-11-10"),
            rec("system", "up", {"pct": 80}, 1, run="r3", opened="2026-11-17")]
    rep = scoring.layer_report(recs, today="2027-03-01", versions={"r1": "abc1234", "r2": "def5678", "r3": "abc1234"})
    bv = rep["by_version"]
    ok("按提交号分组", set(bv) == {"abc1234", "def5678"}, list(bv))
    ok("abc1234 system 两条 Brier 平均 0.04", close(bv["abc1234"]["layers"]["system"]["brier"], 0.04), bv["abc1234"])
    ok("def5678 system Brier 0.64", close(bv["def5678"]["layers"]["system"]["brier"], 0.64))
    ok("版本里 baseline 差值也有", "system" in bv["abc1234"]["layers"] and "baseline" in bv["abc1234"]["layers"])
    dv = bv["abc1234"]["diff_vs_baseline"]["system"]
    ok("版本内差值也只比共同题:r1 有 baseline 的 up 一题,system 0.04 − baseline 0.25",
       dv["n_matched"] == 1 and close(dv["brier"], 0.04 - 0.25), dv)
    ok("没给 versions 时不出 by_version(空 dict)", scoring.layer_report(recs, today="2027-03-01")["by_version"] == {})
    rep3 = scoring.layer_report(recs, today="2027-03-01", versions={"r1": "abc1234"})
    ok("manifest 里查不到的 run 归 unknown", "unknown" in rep3["by_version"], list(rep3["by_version"]))


def check_question_key_runs():
    """同一天出两版预测(开题日、参数都一样)时,两版的题不能混成一道。合成:A 版只有 Claude 答(90 种),
    B 版 Claude、codex 都答 10 种、统计底线 50 种,结果都没发生。两个 AI 比、对统计底线的差值都只该比 B 版。"""
    a = [rec("claude", "up", {"pct": 90}, 0, run="2026-11-03/150000")]
    b = [rec(x, "up", {"pct": p}, 0, run="2026-11-03/160000") for x, p in (("claude", 10), ("codex", 10), ("baseline", 50))]
    cmp = board_scores._ai_compare(a + b, "2027-03-01")
    ok("两个 AI 比:只比 B 版两边都答了的 1 道,两边 Brier 都是 0.01", cmp["n"] == 1
       and close(cmp["claude"]["brier"], 0.01) and close(cmp["codex"]["brier"], 0.01), cmp)
    d = scoring.layer_report(a + b, today="2027-03-01")["diff_vs_baseline"].get("claude") or {}
    ok("对统计底线的差值:只比 B 版 1 道,0.01 − 0.25", d.get("n_matched") == 1 and close(d.get("brier"), 0.01 - 0.25), d)
    src = ROOT / "skills" / "forecast-board" / "scripts"
    defs = [f"{p.name}:{m.group(1)}" for p in sorted(src.glob("*.py"))
            for m in re.finditer(r"^def (\w*(?:qkey|question_key)\w*)\(", p.read_text(encoding="utf-8"), re.M)]
    ok("「同一道题」的键只有一份:scoring.question_key(结算、成绩页都用它)", defs == ["scoring.py:question_key"], defs)


def check_skipped_and_totals():
    iv = {"lo": 0.9, "hi": 1.1, "pct": 50}
    ip = {"n": 63, "lo": "25", "hi": "75"}
    recs = [rec("baseline", "up", {"pct": 70}, 1),
            rec("baseline", "interval", iv, 1, params=ip, value=None),
            rec("baseline", "interval", iv, 1, params=ip, value=-0.2, code="300502"),
            rec("system", "up", None, 1),
            rec("system", "up", {"pct": 60}, 1, after="2099-01-01", code="300502"),
            rec("rule", "buy_vs_now", None, 1),
            rec("rule", "buy_vs_now", None, None, code="300502")]
    rep = scoring.layer_report(recs, today="2027-03-01")
    t = rep["totals"]
    ok("跳过的不进 n / n_eff(baseline 只剩 1 条 up)", rep["layers"]["baseline"]["n"] == 1
       and rep["layers"]["baseline"]["n_eff"] == 1 and "system" not in rep["layers"], rep["layers"])
    ok("跳过 3 条并带原因", t["skipped"] == 3 and len(t["skipped_reasons"]) == 3, t)
    ok("n_total = scored + pending + unscored + rule + skipped",
       t["n_total"] == len(recs) == t["scored"] + t["pending"] + t["unscored"] + t["rule"] + t["skipped"], t)
    ok("各项数:scored 1 / pending 1 / unscored 1 / rule 1", (t["scored"], t["pending"], t["unscored"], t["rule"])
       == (1, 1, 1, 1), t)
    ok("按题型 Brier", close(rep["layers"]["baseline"]["brier_by_type"]["up"], 0.09), rep["layers"]["baseline"])


# ── 成绩页:原因概率是谁给的、旧记录、写法 ──────────────────────────────────
# 合成数据:代码、概率、比值、日期都是编的,不照抄真实存档里的任何一道题。
SUG = {"luck": 20, "system": 25, "ai": 10, "data_gap": 25, "data_error": 10, "impl_error": 5, "unforeseeable": 5}
END = "2026-08-28"                      # 合成题的结算日
COPIED = "演示记录(概率照抄程序初值),待 user 确认"
DEMO = "Claude 提议,演示,待 user 确认"


def _res(outcome, value=0.5, **extra) -> dict:
    return {"outcome": outcome, "end_day": END, "scorable_after": END, "resolved_at": "x",
            "value": value, "note": "合成", **extra}


def _q(run, author, code, pct, outcome, source, ratio=0.55, value=0.5, **res_extra) -> dict:
    q = {"id": f"{run}/{author}/{code}/above_on/r{ratio}", "run": run, "author": author, "code": code,
         "type": "above_on", "opened": run.split("/")[1], "p0_hfq": 100.0,
         "params": {"ratio": ratio, "day": END}, "forecast": {"pct": pct}, "source": source,
         "note": "合成:旧记录换算" if source == "journal" else "合成:聊天里说的"}
    if outcome is None:
        return q
    return {**q, "resolution": _res(outcome, value, **res_extra), **({"suggest": SUG} if outcome == 0 else {})}


def _write_lab(root: Path, qs: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    done = [q for q in qs.values() if "resolution" in q]
    (root / "outcomes.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in done),
                                         encoding="utf-8")
    (root / "chat_questions.jsonl").write_text(
        "".join(json.dumps({k: v for k, v in q.items() if k not in ("resolution", "suggest")}, ensure_ascii=False)
                + "\n" for q in qs.values()), encoding="utf-8")
    return {k: q["id"] for k, q in qs.items()}


def _scores_lab(tmp: Path, chat: bool = True) -> tuple[Path, dict]:
    """临时存档根:换算进来的旧记录两道(一错一对)、还没到期的一道,聊天题一道(错;chat=False 时没有)。"""
    root = tmp / "data" / "forecasts"
    qs = {"jw": _q("journal/2026-05-11", "claude", "688111", 65, 0, "journal"),
          "jr": _q("journal/2026-05-11", "claude", "002222", 72, 1, "journal", ratio=0.7, value=0.9),
          "jp": _q("journal/2026-05-12", "claude", "002222", 58, None, "journal", ratio=0.8)}
    if chat:
        qs["cw"] = _q("chat/2026-06-15", "claude", "688111", 66, 0, "chat", ratio=1.15)
    return root, _write_lab(root, qs)


def _scores(root: Path) -> tuple[dict, str]:
    sc = board_scores.scores_data(root, root.parent, date(2026, 11, 3))
    return sc, render_scores.scores_body({"scores": sc, "today": "2026-11-03", "run": None})


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows or []}


def _section(html: str, title: str) -> str:
    sec = html[html.find(f"<h2>{title}</h2>"):]
    return sec[:sec.find("</section>")]


def _max_depth(s: str) -> int:
    d = m = 0
    for ch in s:
        d += (ch == "(") - (ch == ")")
        m = max(m, d)
    return m


def _review_days(root) -> dict:
    """{(题号, 谁): 最后一条记录的日期}:页面上写的是记录那天,检查拿记录里的日子比,不写死哪一天。"""
    return {(r["qid"], r["who"]): r["at"][:10] for r in attribution.load_reviews(root)}


def check_review_who():
    """reviews.jsonl 里 who 不是 user 的只算提议:页面不写「人确认」,也不进「按原因汇总(人确认过的)」。
    概率照抄程序初值的演示记录写「演示记录(概率照抄程序初值)」;来源那一行平铺,不套两层括号。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root, ids = _scores_lab(tmp)
        attribution.confirm(ids["jw"], SUG, "claude", "演示,不算成绩:合成", ["合成证据"], root=root)
        attribution.confirm(ids["cw"], {"system": 70, "luck": 30}, "claude", "演示,不算成绩:合成", ["合成证据"],
                            root=root)
        sc, html = _scores(root)
        j, w, c = _by_id(sc["journal_questions"]), _by_id(sc["wrong"]), _by_id(sc["cards"])
        ok("演示记录、概率和程序初值一样:写「演示记录(概率照抄程序初值),待 user 确认」", j[ids["jw"]]["from"] == COPIED,
           j[ids["jw"]].get("from"))
        ok("演示记录、概率改过:错在哪里、比对卡写「Claude 提议,演示,待 user 确认」",
           w.get(ids["cw"], {}).get("from") == DEMO and c.get(ids["cw"], {}).get("from") == DEMO,
           (w.get(ids["cw"], {}).get("from"), c.get(ids["cw"], {}).get("from")))
        ok("按原因汇总只算 user 确认的:现在一项都没有", all(v == 0 for _, v in sc["causes"]), sc["causes"])
        labels = re.findall(r"原因概率[^:<]*:", html)
        at = _review_days(root)          # 记录写下那天的日期,不写死:检查哪天跑都一样
        ok("页面:来源一行平铺「原因概率 · … · 日期:」,不套两层括号,不写人确认",
           f"原因概率 · {COPIED} · {at[ids['jw'], 'claude']}:" in html
           and f"原因概率 · {DEMO} · {at[ids['cw'], 'claude']}:" in html
           and labels and max(_max_depth(x) for x in labels) <= 1 and "人确认 ·" not in html
           and "还没有人确认过的原因" in html, labels)
        attribution.confirm(ids["cw"], {"system": 60, "luck": 40}, "user", "合成:人看过", [], root=root)
        attribution.confirm(ids["cw"], {"luck": 100}, "claude", "合成:后来的提议", [], root=root)
        attribution.confirm(ids["jw"], {"luck": 100}, "codex", "合成:codex 看过", [], root=root)
        sc2, html2 = _scores(root)
        w2, j2 = _by_id(sc2["wrong"]), _by_id(sc2["journal_questions"])
        cw = w2.get(ids["cw"], {})
        ok("user 确认过的:写人确认,用 user 那次的概率(后来 claude 的提议不盖掉)", cw.get("from") == "人确认"
           and dict(cw.get("causes") or []).get("系统算法") == 60, cw)
        agg = dict(sc2["causes"])
        ok("按原因汇总只算 user 的:系统算法 0.6、正常的运气 0.4", agg.get("系统算法") == 0.6
           and agg.get("正常的运气") == 0.4 and sum(agg.values()) == 1.0, agg)
        ok("不是 user、不是演示:写「codex 提议,待 user 确认」", j2[ids["jw"]]["from"] == "codex 提议,待 user 确认",
           j2[ids["jw"]].get("from"))
        at = _review_days(root)
        ok("页面:user 确认的写「原因概率 · 人确认 · user 日期:」,codex 的写提议",
           f"原因概率 · 人确认 · user {at[ids['cw'], 'user']}:" in html2
           and f"原因概率 · codex 提议,待 user 确认 · {at[ids['jw'], 'codex']}:" in html2,
           re.findall(r"原因概率[^:<]*:", html2))
        attribution.confirm(ids["jw"], {"luck": 30, "system": 70}, "codex", "合成:概率照抄程序初值", [], root=root)
        j3 = _by_id(_scores(root)[0]["journal_questions"])
        ok("note 写了照抄程序初值、又不是演示:写「codex 记录(概率照抄程序初值),待 user 确认」",
           j3[ids["jw"]]["from"] == "codex 记录(概率照抄程序初值),待 user 确认", j3[ids["jw"]].get("from"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_suggest_evidence():
    """还没人确认的错题显示「程序给的初值,待人确认」时,要列出初值的依据(结算时存的 suggest_why):
    错在哪里、比对卡、journal 那栏都列;一条规则都没触发写均分;以前结的题没存依据照实写没存。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root = tmp / "data" / "forecasts"
        why = "合成:用到的输入不全:capex partial"
        qs = {"cw": {**_q("chat/2026-06-15", "claude", "688111", 66, 0, "chat", ratio=1.15), "suggest_why": [why]},
              "cz": {**_q("chat/2026-06-15", "codex", "688111", 60, 0, "chat", ratio=1.15), "suggest_why": []},
              "jw": _q("journal/2026-05-11", "claude", "688111", 65, 0, "journal")}      # 以前结的:没有 suggest_why
        ids = _write_lab(root, qs)
        sc, html = _scores(root)
        w, c, j = _by_id(sc["wrong"]), _by_id(sc["cards"]), _by_id(sc["journal_questions"])
        ok("错在哪里:程序初值的证据列出来", w.get(ids["cw"], {}).get("evidence") == [why], w.get(ids["cw"]))
        ok("比对卡:程序初值的证据列出来", c.get(ids["cw"], {}).get("evidence") == [why], c.get(ids["cw"]))
        ok("一条规则都没触发:写七个原因按均分", c.get(ids["cz"], {}).get("evidence") == [board_scores.NO_RULE_TEXT]
           if hasattr(board_scores, "NO_RULE_TEXT") else False, c.get(ids["cz"]))
        ok("以前结的题没存依据:照实写没存", j.get(ids["jw"], {}).get("evidence") == [board_scores.NO_WHY_TEXT]
           if hasattr(board_scores, "NO_WHY_TEXT") else False, j.get(ids["jw"]))
        ok("页面:错在哪里、比对卡、journal 那栏都写出证据", why in _section(html, "错在哪里")
           and why in _section(html, "比对卡") and "没存程序给初值的依据" in _section(html, "journal 旧记录原文比对"),
           _section(html, "错在哪里")[:400])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_rule_note():
    """「按买点买 vs 当天买」那道题按买点规则的胜率算,不是哪一层的概率:成绩页要写明(没到期时也写)。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root = tmp / "data" / "forecasts"
        root.mkdir(parents=True)
        sec = _section(_scores(root)[1], "三层各自的成绩")
        ok("成绩页写明这道题按买点规则的胜率算、不进各层成绩", "按买点规则的胜率算" in sec
           and "当天就买" in sec and "各层的成绩里没有它" in sec, sec[-500:])
        ok("成绩页写明这道题每只股票每次出预测各一道", "每只股票每次出预测各一道" in sec, sec[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_journal_score():
    """换算进来的 journal 旧记录照是非题评分:结算了的写概率误差(Brier),没结算的不写。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root, ids = _scores_lab(tmp)
        sc, html = _scores(root)
        j = _by_id(sc["journal_questions"])
        ok("说 72 种、发生了 → 概率误差 (0.72 − 1)² = 0.0784", close(j[ids["jr"]].get("brier"), 0.0784), j[ids["jr"]])
        ok("说 65 种、没发生 → (0.65 − 0)² = 0.4225", close(j[ids["jw"]].get("brier"), 0.4225), j[ids["jw"]])
        ok("还没结算的不写概率误差", j[ids["jp"]].get("brier") is None and j[ids["jp"]]["actual"] is None, j[ids["jp"]])
        sec = _section(html, "journal 旧记录原文比对")
        ok("页面 journal 那栏写出概率误差", "概率误差 0.0784" in sec and "概率误差 0.4225" in sec
           and sec.count("概率误差") == 2, sec[:600])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_journal_totals():
    """总账的「已到期」「已结算」算上换算进来的旧记录,并写明含几道;错在哪里、比对卡没有上线后的题时,
    照实写旧记录结了几道、错了几道在哪一栏,不写成「一道都没有」。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root, _ = _scores_lab(tmp)
        sc, html = _scores(root)
        t = sc["totals"]
        ok("总账:共 4 道、已到期 3 道(两道旧记录 + 一道聊天题)、已结算 3 道", (t["questions"], t["due"], t["resolved"])
           == (4, 3, 3), t)
        ok("总账那一行写明三个数各含几道换算进来的旧记录",
           "共 4 道题(含换算进来的旧记录 3 道)、已到期 3 道(含换算进来的旧记录 2 道)、已结算 3 道(含换算进来的旧记录 2 道)"
           in _section(html, "总账"), _section(html, "总账"))
        root2, _ = _scores_lab(tmp / "only-journal", chat=False)
        sc, html = _scores(root2)
        ok("只有旧记录:已到期 2、已结算 2", (sc["totals"]["due"], sc["totals"]["resolved"]) == (2, 2), sc["totals"])
        wrong, cards = _section(html, "错在哪里"), _section(html, "比对卡")
        ok("错在哪里:写旧记录错了 1 道、在 journal 那栏", "旧记录里错了 1 道" in wrong
           and "journal 旧记录原文比对" in wrong, wrong)
        ok("比对卡:写旧记录结算了 2 道、在 journal 那栏", "旧记录结算了 2 道" in cards
           and "journal 旧记录原文比对" in cards, cards)
        root3 = tmp / "empty" / "data" / "forecasts"
        root3.mkdir(parents=True)
        _, h3 = _scores(root3)
        ok("什么都没有时照旧写还没有,总账不写「含旧记录」", "还没有到期的错题。" in _section(h3, "错在哪里")
           and "旧记录" not in _section(h3, "错在哪里") and "还没有到期结算的题。" in _section(h3, "比对卡")
           and "共 0 道题、已到期 0 道、已结算 0 道;" in _section(h3, "总账"), _section(h3, "总账"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _said(q) -> str:
    try:
        return board_scores.said(q)
    except Exception as e:          # 老代码碰到没见过的写法可能直接报错,当成不过
        return f"出错:{type(e).__name__}: {e}"


def check_wording():
    """题目说法和实际结果写价位线、起点价、跌或涨多少,不写「起点的 -45.0%」这种。价位都是编的(50 元、40 元)。
    没记收盘价时,比值要写明是后复权算的(它不是两天收盘价之比)。"""
    q = _q("journal/2026-05-11", "claude", "688111", 65, 0, "journal")            # 比值 0.55
    s1, s0 = _said({**q, "p0_close": 50.0}), _said(q)
    ok("有起点价:写价位线、起点价和跌多少", f"{END} 收盘高于 27.50 元(起点 50.00 的 55.0%,也就是跌 45.0%)" in s1, s1)
    ok("没有起点价:写起点价的百分之几和跌多少", f"{END} 收盘高于起点价的 55.0%(也就是跌 45.0%)" in s0, s0)
    up = _said({**q, "params": {"ratio": 1.25, "day": END}, "p0_close": 40.0})
    ok("线在起点之上:写涨多少", "收盘高于 50.00 元(起点 40.00 的 125.0%,也就是涨 25.0%)" in up, up)
    tc = _said({**q, "type": "touch", "params": {"ratio": 0.8, "n": 63, "dir": "down"}, "p0_close": 50.0})
    ok("碰价题同样写法", "63 个交易日内收盘往下跌到 40.00 元(起点 50.00 的 80.0%,也就是跌 20.0%)" in tc, tc)
    iv = _said({**q, "type": "interval", "params": {"n": 63, "lo": "10", "hi": "90"},
                "forecast": {"lo": 0.9, "hi": 1.2, "pct": 80}, "p0_close": 50.0})
    ok("区间题写两个价位", "45.00~60.00 元(起点 50.00 的 90.0%~120.0%)" in iv, iv)
    a0 = board_scores.actual({**q, "resolution": _res(0, 0.45)})
    ok("实际:没记收盘价时写明是后复权算的比", f"{END}:没发生,收盘后复权算是起点价的 45.0%(跌 55.0%)" in a0, a0)
    a1 = board_scores.actual({**q, "resolution": _res(1, 1.1, end_close=55.0)})
    ok("实际:记了收盘价时写收盘价", f"{END}:发生了,收盘 55.00 元,后复权算是起点价的 110.0%(涨 10.0%)" in a1, a1)
    tmp = Path(tempfile.mkdtemp(prefix="check-scoring-"))
    try:
        root = tmp / "data" / "forecasts"
        run = root / "2026-07-31" / "150000"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"git_head": "abc1234"}), encoding="utf-8")
        (run / "inputs.json").write_text(json.dumps({"run_meta": {"start_close": {"002222": 40.0}},
                                                     "missing_required": []}), encoding="utf-8")
        iv_q = {**_q("2026-07-31/150000", "claude", "002222", 80, 0, "x"), "type": "interval",
                "opened": "2026-07-31", "params": {"n": 63, "lo": "10", "hi": "90"},
                "forecast": {"lo": 0.9, "hi": 1.2, "pct": 80}}
        iv_q.pop("source")
        # 换算进来的题:题目记录里没有起点价,结算记录里有(补记的 start_close / end_close)
        jq = _q("journal/2026-05-11", "claude", "688111", 65, 0, "journal", value=0.48,
                start_close=50.0, end_close=24.4)
        ids = _write_lab(root, {"iv": iv_q, "jq": jq})
        rows = [json.loads(x) for x in (root / "chat_questions.jsonl").read_text(encoding="utf-8").splitlines()]
        ok("合成数据:题目记录里确实没有起点价", all("p0_close" not in r for r in rows), rows)
        sc, html = _scores(root)
        card = _by_id(sc["cards"]).get(ids["iv"], {}).get("said") or ""
        ok("存档里的题:起点价取这次存档的 start_close", "36.00~48.00 元(起点 40.00 的 90.0%~120.0%)" in card, card)
        jsec = _section(html, "journal 旧记录原文比对")
        ok("journal 那栏:题目说法用结算记录里的起点价写价位线",
           "收盘高于 27.50 元(起点 50.00 的 55.0%,也就是跌 45.0%)" in jsec, jsec[:500])
        ok("journal 那栏:结果写收盘价", "收盘 24.40 元,后复权算是起点价的 48.0%(跌 52.0%)" in jsec, jsec[:700])
        ok("页面哪儿都没有「起点的 -」「起点的 +」", re.search(r"起点的 [-+]", html) is None,
           re.findall(r".{20}起点的 [-+].{10}", html)[:3])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_basis_zero_weight():
    print("== 比对卡依据:权重 0 的信号标「不计」")
    ans = {"stocks": {"300308": {"tilt": 0.075, "signals": {
        "values": {"regime": -1, "consensus": 1}, "weights": {"regime": 0.0, "consensus": 0.05}}}}}
    line = board_scores.basis(ans, "300308")[-1]
    ok("大盘权重 0:写成「regime -1(权重 0,不计)」,券商照常", "regime -1(权重 0,不计)" in line
       and "consensus +1、" not in line and "consensus +1" in line and "consensus +1(" not in line, line)
    old = {"stocks": {"300308": {"tilt": 0.075, "signals": {"values": {"regime": -1}}}}}
    ok("旧存档没有 weights:不加标注(不猜当时的权重)",
       "不计" not in board_scores.basis(old, "300308")[-1], board_scores.basis(old, "300308"))


CHECKS = [check_basics, check_report, check_versions, check_question_key_runs, check_skipped_and_totals,
          check_review_who, check_suggest_evidence, check_rule_note, check_journal_score,
          check_journal_totals, check_wording, check_basis_zero_weight]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
