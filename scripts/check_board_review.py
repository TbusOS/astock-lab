#!/usr/bin/env python3
"""check_board_review —— 预测看板「错了以后」两块的检查:原因概率(attribution.py)、重跑(replay.py)。

用法:
    python3 scripts/check_board_review.py        # 退出码 0 才算过

不联网、不碰真实数据:题目、输入包、存档目录都是临时目录里手造的合成数据。
重跑的编排用假的 runner 测;只有 check_replay_git 一块会在临时目录里建一个
一次性的 git 仓库,真的 git worktree add / remove 一遍(证明出错时也会删干净)。
写法和 check_board_scoring.py 一样:每块一个函数,登记进 CHECKS。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402  先于别的模块,和正式运行的顺序一样
import attribution as at  # noqa: E402
import replay as rp  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:500]))
    if not cond:
        FAILS += 1


def raises(fn, exc=Exception, text=""):
    """fn() 抛出 exc 且消息里有 text → True。"""
    try:
        fn()
    except exc as e:
        return text in str(e)
    return False


def item(status="ok", public_at="2026-11-02"):
    return {"value": 1, "status": status, "public_at": public_at, "source": "合成", "note": ""}


def q_rec(author, forecast, outcome, qtype="up"):
    """一条评分记录(题目 + 结算结果)。"""
    return {"id": f"r1/{author}/300308/{qtype}/n63", "run": "r1", "author": author, "code": "300308",
            "type": qtype, "opened": "2026-11-02", "params": {"n": 63}, "forecast": forecast,
            "resolution": {"outcome": outcome, "scorable_after": "2027-02-01"}}


def lines(path):
    return Path(path).read_bytes().splitlines()


# ── 原因概率 ────────────────────────────────────────────────────────────────
def check_constants():
    ok("七个原因,顺序固定", at.CAUSES == ["luck", "system", "ai", "data_gap", "data_error",
                                       "impl_error", "unforeseeable"], at.CAUSES)
    ok("每个原因都有中文名", set(at.CAUSE_LABELS) == set(at.CAUSES), at.CAUSE_LABELS)
    ok("中文名照计划", at.CAUSE_LABELS["data_gap"] == "数据没取全"
       and at.CAUSE_LABELS["unforeseeable"] == "事先没法知道的事"
       and at.CAUSE_LABELS["data_error"] == "数据内容或单位错", at.CAUSE_LABELS)
    ok("加分照计划:数据 30、系统 20、AI 30、运气 20",
       at.BONUS == {"data_gap": 30, "system": 20, "ai": 30, "luck": 20}, at.BONUS)


def check_suggest():
    inputs = {"capex": item("partial"), "prices.300308": item(), "market.regime": item()}
    used = ["capex", "prices.300308", "market.regime"]
    # 计划里的用例:用到的输入有一项 partial、系统层错了(比底线差),这道是系统层自己的题
    q = q_rec("system", {"pct": 80}, 0)
    p = at.suggest(q, {"baseline": 0.25, "system": 0.64}, inputs, used)
    ok("partial + 系统层错:data_gap 最大", max(p, key=p.get) == "data_gap", p)
    ok("加起来正好 100", sum(p.values()) == 100, p)
    ok("七个原因都在,顺序同 CAUSES", list(p) == at.CAUSES, list(p))
    ok("具体数:按 均分 + 30 / + 20 再按比例取整(最大余数)",
       p == {"luck": 10, "system": 23, "ai": 10, "data_gap": 30, "data_error": 9,
             "impl_error": 9, "unforeseeable": 9}, p)

    p0 = at.suggest(q_rec("system", {"pct": 80}, 0), {}, {}, [])
    ok("什么都没触发:均分(15 15 14 14 14 14 14)",
       list(p0.values()) == [15, 15, 14, 14, 14, 14, 14], p0)

    fc = {"system": {"pct": 40}, "claude": {"pct": 75}}
    p = at.suggest(q_rec("claude", {"pct": 75}, 0), {"system": 0.16, "claude": 0.5625}, {}, [],
                   forecasts=fc)
    ok("AI 推翻系统(差 35)且更差 → ai 最大", max(p, key=p.get) == "ai" and p["system"] < p["ai"], p)
    p = at.suggest(q_rec("claude", {"pct": 25}, 0), {"system": 0.16, "claude": 0.0625}, {}, [],
                   forecasts={"system": {"pct": 40}, "claude": {"pct": 25}})
    ok("AI 推翻系统但更好 → ai 不加分", p["ai"] == p["data_error"], p)
    p = at.suggest(q_rec("claude", {"pct": 54}, 0), {"system": 0.16, "claude": 0.2916}, {}, [],
                   forecasts={"system": {"pct": 40}, "claude": {"pct": 54}})
    ok("差 14(不到 15)不算推翻 → ai 不加分", p["ai"] == p["data_error"], p)
    p = at.suggest(q_rec("claude", {"pct": 55}, 0), {"system": 0.16, "claude": 0.3025}, {}, [],
                   forecasts={"system": {"pct": 40}, "claude": {"pct": 55}})
    ok("差正好 15 算推翻 → ai 加分", p["ai"] > p["data_error"], p)

    fc = {"system": {"pct": 75}, "codex": {"pct": 80}}
    p = at.suggest(q_rec("codex", {"pct": 80}, 0), {"baseline": 0.25, "system": 0.5625, "codex": 0.64},
                   {}, [], forecasts=fc)
    ok("AI 跟着系统(差 5)且系统比底线差 → system 最大", max(p, key=p.get) == "system", p)
    p = at.suggest(q_rec("codex", {"pct": 90}, 0), {"baseline": 0.25, "system": 0.5625, "codex": 0.81},
                   {}, [], forecasts={"system": {"pct": 75}, "codex": {"pct": 90}})
    ok("差正好 15 不算跟着:系统比底线差也不加 system,改加 ai",
       p["system"] < p["ai"] and p["system"] <= p["data_error"] + 1, p)
    p = at.suggest(q_rec("codex", {"pct": 80}, 0), {"baseline": 0.25, "system": 0.5625, "codex": 0.64},
                   {}, [])
    ok("没给系统层的答案,判断不了跟没跟 → system 不加分", p["system"] == p["data_error"] + 1
       or p["system"] == p["data_error"], p)
    p = at.suggest(q_rec("baseline", {"pct": 80}, 0), {"baseline": 0.64, "system": 0.81}, {}, [])
    ok("底线自己的题:系统比底线差也不怪系统", p["system"] <= p["data_error"] + 1, p)
    p = at.suggest(q_rec("system", {"pct": 80}, 0), {"baseline": 0.81, "system": 0.64}, {}, [])
    ok("系统比底线好 → system 不加分", p["system"] <= p["data_error"] + 1, p)
    p = at.suggest(q_rec("system", {"pct": 80}, 0), {"baseline": None, "system": 0.64}, {}, [])
    ok("某层分数是 None 当没有分数,不报错", sum(p.values()) == 100 and p["system"] == 15, p)

    p = at.suggest(q_rec("baseline", {"pct": 60}, 0), {}, {}, [])
    ok("实际发生那一项当时给了 40(≥ 30)→ luck 最大", max(p, key=p.get) == "luck"
       and p["luck"] > p["system"], p)
    cat = {"2026-11": 20, "2026-12": 50, "2027-01": 30}
    p = at.suggest(q_rec("system", cat, "2027-01", "bottom_month"), {}, {}, [])
    ok("分类题:实际那一类给了 30(正好 30)→ luck 加分", p["luck"] > p["system"], p)
    p = at.suggest(q_rec("system", cat, "2026-11", "bottom_month"), {}, {}, [])
    ok("分类题:实际那一类给了 20 → luck 不加分", p["luck"] == 15, p)
    p = at.suggest(q_rec("system", {"lo": 0.9, "hi": 1.1, "pct": 50}, 0, "interval"), {}, {}, [])
    ok("区间题:落在外面,当时给外面 50 → luck 加分", p["luck"] > p["system"], p)
    p = at.suggest(q_rec("system", {"pct": 80}, None), {}, {"capex": item("failed")}, ["capex"])
    ok("还没有结果(outcome None)照样给概率,加起来 100", sum(p.values()) == 100
       and max(p, key=p.get) == "data_gap", p)


def check_suggest_inputs():
    q = q_rec("system", {"pct": 80}, 0)
    for st in ("partial", "failed", "stale"):
        p = at.suggest(q, {}, {"capex": item(st)}, ["capex"])
        ok(f"{st} → data_gap 加分", max(p, key=p.get) == "data_gap", p)
    p = at.suggest(q, {}, {"capex": item("ok")}, ["capex"])
    ok("ok → data_gap 不加分", p["data_gap"] == 14, p)
    p = at.suggest(q, {}, {"capex": item("ok")}, ["capex", "valuation.300308"])
    ok("用到的项在输入包里根本没有 → 也算没取全", max(p, key=p.get) == "data_gap", p)
    p = at.suggest(q, {}, {"capex": item("partial"), "prices.300308": item()}, ["prices.300308"])
    ok("没用到的项 partial 不算", p["data_gap"] == 14, p)
    p = at.suggest(q, {}, {"items": {"capex": item("partial")}, "missing_required": []}, ["capex"])
    ok("输入包包了一层 {items: …} 也认", max(p, key=p.get) == "data_gap", p)
    p2 = at.suggest(q, {}, {"capex": item("partial"), "taiwan": item("stale")}, ["capex", "taiwan"])
    p1 = at.suggest(q, {}, {"capex": item("partial")}, ["capex"])
    ok("两项都不全也只加一次 30", p1 == p2, (p1, p2))

    rs = at.reasons(q, {"baseline": 0.25, "system": 0.64}, {"capex": item("partial")}, ["capex"])
    ok("reasons 列出触发的规则、加分和证据", [(r["cause"], r["points"]) for r in rs]
       == [("data_gap", 30), ("system", 20)] and "capex" in rs[0]["why"], rs)

    keys = at.system_keys("300308", {"peer.300308.300502": item(), "peer.300502.300308": item(),
                                     "peer.300394.300502": item(), "capex": item()})
    ok("系统层用到的键:日线、指数、五个信号、含本股的同行对",
       keys == ["prices.300308", "index.sz399006", "market.regime", "consensus.300308",
                "eps_revisions.300308", "capex", "valuation.300308",
                "peer.300308.300502", "peer.300502.300308"], keys)

    # 任何组合都加起来 100、七项都在、不会全空
    bad = []
    for gap, sys_worse, follow, worse, lucky in product((0, 1), repeat=5):
        sc = {"baseline": 0.3, "system": 0.5 if sys_worse else 0.1, "claude": 0.6 if worse else 0.05}
        fc = {"system": {"pct": 40}, "claude": {"pct": 45 if follow else 80}}
        qq = q_rec("claude", {"pct": 65 if lucky else 80}, 0)
        p = at.suggest(qq, sc, {"capex": item("partial" if gap else "ok")}, ["capex"], forecasts=fc)
        if sum(p.values()) != 100 or list(p) != at.CAUSES or min(p.values()) < 0:
            bad.append(p)
    ok("32 种组合都加起来 100、七项都在", not bad, bad[:2])


@contextmanager
def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="check-review-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_confirm():
    with tmpdir() as root:
        r1 = at.confirm("r1/system/300308/up/n63", {"data_gap": 60, "system": 30, "luck": 10},
                        "user", "补了数据结论翻转", ["capex 当时 partial"], root=root)
        f = root / "reviews.jsonl"
        first = lines(f)
        ok("第一次:一行", len(first) == 1, first)
        ok("没写的原因补 0,七项都在", r1["probs"] == {"luck": 10, "system": 30, "ai": 0, "data_gap": 60,
                                                   "data_error": 0, "impl_error": 0, "unforeseeable": 0},
           r1["probs"])
        ok("带编号、时间、确认人、证据", r1["id"] and r1["at"] and r1["who"] == "user"
           and r1["evidence"] == ["capex 当时 partial"], r1)
        r2 = at.confirm("r1/claude/300308/up/n63", {"ai": 100}, "user", "", [], root=root)
        now = lines(f)
        ok("第二次:文件多一行", len(now) == 2, now)
        ok("第一行一个字节都没变", now[0] == first[0], (first[0], now[0]))
        ok("两行编号不同", r1["id"] != r2["id"], (r1["id"], r2["id"]))
        ok("文件里的就是返回的", json.loads(now[1]) == r2, now[1])
        for name, fn in [("加起来不是 100", lambda: at.confirm("q", {"ai": 90}, "u", "", [], root=root)),
                         ("不认识的原因", lambda: at.confirm("q", {"bad": 100}, "u", "", [], root=root)),
                         ("不是整数", lambda: at.confirm("q", {"ai": 50.0, "luck": 50}, "u", "", [], root=root)),
                         ("负数", lambda: at.confirm("q", {"ai": 110, "luck": -10}, "u", "", [], root=root)),
                         ("没写确认人", lambda: at.confirm("q", {"ai": 100}, "", "", [], root=root)),
                         ("没写题号", lambda: at.confirm("", {"ai": 100}, "u", "", [], root=root)),
                         ("证据不是字符串列表", lambda: at.confirm("q", {"ai": 100}, "u", "", "x", root=root))]:
            ok(f"拒收:{name}", raises(fn, ValueError), name)
        ok("拒收时一行都没写", len(lines(f)) == 2, lines(f))
        ok("load_reviews 按写入顺序读回", [r["id"] for r in at.load_reviews(root)] == [r1["id"], r2["id"]])

    with tmpdir() as root:
        f = root / "reviews.jsonl"
        f.write_text('{"id": "old", "qid": "q0", "probs": {"luck": 100}}', encoding="utf-8")   # 末尾没换行
        at.confirm("q1", {"ai": 100}, "u", "", [], root=root)
        got = [json.loads(x) for x in lines(f)]
        ok("上一行末尾缺换行:先补换行再追加,旧行原样", [g["id"] for g in got][0] == "old"
           and got[1]["qid"] == "q1", got)
    ok("没有文件时 load_reviews 给空列表", at.load_reviews(Path(tempfile.gettempdir()) / "无此目录-x") == [])


def check_jsonl_lines():
    """U+2028 / U+2029 / U+0085 在 str.splitlines 眼里也是换行;写到一半的最后一行。"""
    note = "甲 乙\x85丙 丁"
    with tmpdir() as root:
        f = root / "reviews.jsonl"
        r = at.confirm("q1", {"ai": 100}, "user", note, ["证据 第二段"], root=root)
        at.confirm("q2", {"luck": 100}, "user", "第二条", [], root=root)
        ok("备注里有 U+2028 / U+0085 / U+2029:文件每行纯 ASCII、正好两行",
           f.read_bytes().isascii() and f.read_bytes().count(b"\n") == 2, f.read_bytes()[:200])
        back = at.load_reviews(root)
        ok("读回来两条,备注和证据原样", len(back) == 2 and back[0]["note"] == note
           and back[0]["evidence"] == r["evidence"] and back[1]["qid"] == "q2", back)
    with tmpdir() as root:
        f = root / "reviews.jsonl"
        raw = {"id": "h", "qid": "q0", "probs": {"luck": 100}, "note": "手写 的\x85一行"}
        f.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")   # 老文件里是原样字符
        ok("老文件里原样写着 U+2028 / U+0085:只按 \\n 切,读回一条", at.load_reviews(root) == [raw],
           at.load_reviews(root))
    with tmpdir() as root:
        f = root / "reviews.jsonl"
        at.confirm("q1", {"ai": 100}, "user", "", [], root=root)
        with open(f, "ab") as fh:
            fh.write(b'{"id": "half", "qid": "q2", "pro')                  # 写到一半崩了,没有换行
        before = f.read_bytes()
        err = io.StringIO()
        with redirect_stderr(err):
            back = at.load_reviews(root)
        ok("最后一行写到一半:跳过,其余照读,stderr 有警告", [b["qid"] for b in back] == ["q1"]
           and "没写完整" in err.getvalue(), (back, err.getvalue()))
        ok("最后一行写到一半:不再往后追加(抛错),文件一个字节不动",
           raises(lambda: at.confirm("q3", {"ai": 100}, "u", "", [], root=root), ValueError, "没写完整")
           and f.read_bytes() == before, f.read_bytes()[-80:])
    with tmpdir() as root:
        f = root / "reviews.jsonl"
        f.write_text('{"id": "a", "qid": "q0", "probs": {}}\n坏行\n{"id": "b", "qid": "q1", "probs": {}}\n',
                     encoding="utf-8")
        ok("中间某行坏了照样报错(带行号)", raises(lambda: at.load_reviews(root), ValueError, "第 2 行"))
        f.write_text('{"id": "a", "qid": "q0", "probs": {}}\n坏行\n', encoding="utf-8")
        ok("最后一行坏了但有换行(整行写完了还是坏的)→ 报错,不跳过",
           raises(lambda: at.load_reviews(root), ValueError, "第 2 行"))
    with tmpdir() as root:
        it = at.add_improvement("system", "f.py", "说明 第二行", root=root)
        ok("improvements.jsonl 同样纯 ASCII、读得回", (root / "improvements.jsonl").read_bytes().isascii()
           and at.load_improvements(root)[0]["desc"] == it["desc"], at.load_improvements(root))


def check_aggregate():
    revs = [{"qid": "a", "probs": {"data_gap": 60, "system": 40}},
            {"qid": "b", "probs": {"data_gap": 30, "luck": 70}},
            {"qid": "a", "probs": {"data_gap": 90, "system": 10}}]     # a 重跑后重新确认
    agg = at.aggregate(revs)
    ok("每道题只算最后一次确认:data_gap = 0.9 + 0.3", agg["data_gap"] == 1.2, agg)
    ok("system = 0.1,luck = 0.7", agg["system"] == 0.1 and agg["luck"] == 0.7, agg)
    ok("七项都在,没出现的是 0", list(agg) == at.CAUSES and agg["ai"] == 0.0, agg)
    ok("份量加起来 = 题数", abs(sum(agg.values()) - 2) < 1e-9, agg)
    ok("空列表:七项都是 0", at.aggregate([]) == dict.fromkeys(at.CAUSES, 0.0))


def check_improvements():
    with tmpdir() as root:
        f = root / "improvements.jsonl"
        it = at.add_improvement("system", "skills/x/decision.py", "熊市价按历史 PE 下沿算,给窄了", root=root)
        first = lines(f)
        ok("加一项:一行,带编号、时间,状态待改", len(first) == 1 and it["id"] and it["at"]
           and it["status"] == "todo", it)
        it2 = at.add_improvement("data_gap", "skills/y.py", "补研报", root=root)
        st = at.mark_improved(it["id"], "abc1234", root=root)
        now = lines(f)
        ok("标已改:追加一行,不改原来那行", len(now) == 3 and now[0] == first[0], now)
        ok("状态行指向那一项、带提交号和自己的编号", st["item"] == it["id"] and st["commit"] == "abc1234"
           and st["id"] not in (it["id"], it2["id"]), st)
        cur = at.load_improvements(root)
        ok("load_improvements 合出当前状态:两项,第一项已改", [c["id"] for c in cur] == [it["id"], it2["id"]]
           and cur[0]["status"] == "done" and cur[0]["commit"] == "abc1234"
           and cur[1]["status"] == "todo" and cur[1]["commit"] is None, cur)
        ok("状态的中文名", at.IMPROVEMENT_LABELS == {"todo": "待改", "done": "已改"})
        ok("拒收:不认识的原因", raises(lambda: at.add_improvement("bad", "f", "d", root=root), ValueError))
        ok("拒收:标一个不存在的编号", raises(lambda: at.mark_improved("nope", "abc", root=root), KeyError))
        ok("拒收:没写提交号", raises(lambda: at.mark_improved(it2["id"], "", root=root), ValueError))
        ok("拒收时一行都没写", len(lines(f)) == 3, lines(f))
    ok("没有文件时 load_improvements 给空列表", at.load_improvements(Path(tempfile.gettempdir()) / "无此目录-y") == [])


# ── 重跑 ────────────────────────────────────────────────────────────────────
def check_corrected():
    ok("计划里的用例:公开日晚于 as_of → 不合格",
       rp.check_corrected({"x": {"public_at": "2026-12-01"}}, "2026-11-03") == ["x"])
    items = {"late": {"public_at": "2026-11-04"}, "same": {"public_at": "2026-11-03"},
             "early": {"public_at": "2026-10-30"}, "none": {"public_at": None}, "nokey": {},
             "bad": {"public_at": "不知道"}, "dt": {"public_at": "2026-11-03T20:00:00+08:00"}}
    ok("缺公开日、写不成日期的都算没证明;同一天和更早的合格",
       rp.check_corrected(items, "2026-11-03") == ["bad", "late", "nokey", "none"],
       rp.check_corrected(items, "2026-11-03"))
    ok("as_of 写错直接报错", raises(lambda: rp.check_corrected({}, "11-03"), ValueError))


def layer(p63, p126, months):
    return {"horizons": {"63": {"p_up": p63}, "126": {"p_up": p126}}, "bottom": {"month_probs": months}}


def check_compare():
    a = {"baseline": {"300308": layer(50, 51, {"2026-11": 40, "2026-12": 60})},
         "system": {"300308": layer(45, 48, {"2026-11": 30, "2026-12": 70}),
                    "300502": layer(40, 40, {"2026-11": 100})}}
    b = {"baseline": {"300308": layer(50, 51, {"2026-11": 40, "2026-12": 60})},
         "system": {"300308": layer(55, 47, {"2026-11": 20, "2026-12": 70, "2027-01": 10})}}
    d = rp.compare(a, b)
    ok("p_up 差 = b − a", d["system"]["300308"]["p_up"] == {"63": 10, "126": -1}, d["system"]["300308"])
    ok("见底月份分布的差,只在一边有的月份按 0 算",
       d["system"]["300308"]["bottom_months"] == {"2026-11": -10, "2026-12": 0, "2027-01": 10},
       d["system"]["300308"])
    ok("没变就是 0", d["baseline"]["300308"]["p_up"] == {"63": 0, "126": 0}, d["baseline"])
    ok("只在一边有的股票照实标出来", d["system"]["300502"] == {"missing_in": "b"}, d["system"])


META = {"as_of": "2026-11-03", "start": "2026-11-04", "codes": ["300308"], "holidays": ["2027-01-01"],
        "horizons": [63, 126, 250], "bottom_window": 126, "buy_horizon": 250, "n_paths": 2000,
        "lookback": 120, "validated": {}}
ITEMS = {"capex": item(public_at=None), "prices.300308": item()}


def make_run(base, seed=7, inputs=None, head="abc123", untracked=(), **manifest):
    """一次存档:<base>/2026-11-03/153000/,inputs.json 是 {"items", "run_meta"} 形状。"""
    run = base / "2026-11-03" / "153000"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"seed": seed, "git_head": head, "clean": True,
                                                   "untracked": list(untracked), **manifest}), encoding="utf-8")
    pkg = {"items": inputs, "run_meta": META} if inputs is not None else {"items": ITEMS, "run_meta": META}
    (run / "inputs.json").write_text(json.dumps(pkg), encoding="utf-8")
    return run


STUB = {"baseline": {"300308": layer(50, 50, {"2026-11": 100})},
        "system": {"300308": layer(60, 55, {"2026-11": 100})}}


def write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def check_replay_orchestration():
    with tmpdir() as base:
        run = make_run(base)
        calls, seen = [], []

        def fake(repo, ref, inputs, seed, frozen_run):
            calls.append((repo, ref, Path(inputs), seed, frozen_run))
            seen.append(json.loads(Path(inputs).read_text(encoding="utf-8")))
            return "deadbeef" * 5, json.dumps(STUB)

        res = rp.replay(run, "HEAD", runner=fake)
        ok("runner 拿到:默认仓库、版本、原输入包、manifest 里的种子", calls[-1] == (
            rp.REPO, "HEAD", (run / "inputs.json").resolve(), 7, None), calls[-1])
        ok("读回 baseline 和 system", res["baseline"] == STUB["baseline"] and res["system"] == STUB["system"], res)
        ok("记下实际用的提交号、种子、没用修正输入", res["code"]["commit"] == "deadbeef" * 5
           and res["seed"] == 7 and res["corrected"] is False and res["changed"] == [], res)
        rp.replay(run, "abc", repo=base, runner=fake)
        ok("repo 可以换", calls[-1][0] == base and calls[-1][1] == "abc", calls[-1])
        res = rp.replay(run, rp.RUN_CODE, runner=fake)
        ok("版本写 run:用存档里的提交号,并把存档目录交给 runner 打补丁",
           calls[-1][1] == "abc123" and calls[-1][4] == run and res["code"]["frozen"] is True
           and res["code"]["untracked_skipped"] == [], (calls[-1], res["code"]))

        val_late = {**ITEMS, "valuation.300308": item(public_at="2026-11-04")}
        val_ok = {**ITEMS, "valuation.300308": item(public_at="2026-11-03")}
        corr = base / "corrected.json"
        n = len(calls)
        write(corr, val_late)
        ok("修正的项公开日晚于 as_of → 拒绝,不跑",
           raises(lambda: rp.replay(run, "HEAD", corrected=corr, runner=fake), ValueError, "valuation.300308")
           and len(calls) == n, len(calls))
        write(corr, {"items": {"prices.300308": item(), "valuation.300308": item(public_at="2026-11-03")}})
        ok("修正文件少了原来有的项(capex)→ 拒绝,不跑",
           raises(lambda: rp.replay(run, "HEAD", corrected=corr, runner=fake), ValueError, "capex")
           and len(calls) == n, len(calls))
        write(corr, {"items": val_ok, "run_meta": {**META, "start": "2026-11-05"}})
        ok("修正文件的 run_meta 和原来不一样 → 拒绝,不跑(只许改数据)",
           raises(lambda: rp.replay(run, "HEAD", corrected=corr, runner=fake), ValueError, "run_meta")
           and len(calls) == n, len(calls))
        write(corr, {"run_meta": META, **val_ok})
        ok("写了 run_meta 却没有 items → 拒绝", raises(lambda: rp.replay(run, "HEAD", corrected=corr, runner=fake),
                                                 ValueError, "items") and len(calls) == n, len(calls))

        write(corr, {"items": val_ok})
        res = rp.replay(run, "HEAD", corrected=corr, runner=fake)
        ok("修正文件只写 items:交给 recompute 的是 {修正后的项, 原来的 run_meta}",
           seen[-1] == {"items": val_ok, "run_meta": META}, seen[-1])
        ok("交给 runner 的是临时拼的文件,跑完删掉;结果里记的是修正文件本身",
           calls[-1][2] != corr.resolve() and not calls[-1][2].exists() and res["inputs"] == str(corr.resolve()),
           (calls[-1][2], res["inputs"]))
        ok("只查改过的项(capex 没公开日但没改,不拦)", res["changed"] == ["valuation.300308"]
           and res["corrected"] is True, res.get("changed"))
        write(corr, {"items": val_ok, "run_meta": META})
        rp.replay(run, "HEAD", corrected=corr, runner=fake)
        ok("run_meta 和原来一字不差 → 接受", seen[-1] == {"items": val_ok, "run_meta": META}, seen[-1])
        write(corr, val_ok)
        rp.replay(run, "HEAD", corrected=corr, runner=fake)
        ok("平铺的修正文件也拼上原来的 run_meta", seen[-1] == {"items": val_ok, "run_meta": META}, seen[-1])
        n = len(calls)
        ok("runner 报错时临时拼的文件照样删掉", raises(lambda: rp.replay(
            run, "HEAD", corrected=corr, runner=lambda *a: (calls.append(a), 1 / 0))) and not calls[-1][2].exists(),
           calls[-1:])
        del calls[n:]

        for name, out in [("不是 JSON", "oops"), ("缺 system", json.dumps({"baseline": {}})),
                          ("system 不是 dict", json.dumps({"baseline": {}, "system": []}))]:
            ok(f"recompute 输出{name} → 报错",
               raises(lambda o=out: rp.replay(run, "HEAD", runner=lambda *a: ("x", o)), ValueError))
        ok("版本号以 - 开头 → 拒绝(免得被 git 当成选项)",
           raises(lambda: rp.replay(run, "--all", runner=fake), ValueError))
        ok("修正文件不存在 → 报错", raises(lambda: rp.replay(run, "HEAD", corrected=base / "无.json", runner=fake),
                                          FileNotFoundError))
        (run / "inputs.json").rename(run / "inputs.bak")
        ok("存档没有 inputs.json → 报错", raises(lambda: rp.replay(run, "HEAD", runner=fake), FileNotFoundError))
        (run / "inputs.bak").rename(run / "inputs.json")
        (run / "manifest.json").unlink()
        ok("存档没有 manifest → 报错", raises(lambda: rp.replay(run, "HEAD", runner=fake), FileNotFoundError))

    with tmpdir() as base:
        calls = []

        def fake2(*a):
            calls.append(a)
            return "x", json.dumps(STUB)

        dirty = make_run(base / "a", clean=False)
        ok("版本写 run:存档时工作区不干净却没有 code.diff → 报错,不跑",
           raises(lambda: rp.replay(dirty, rp.RUN_CODE, runner=fake2), FileNotFoundError, "code.diff")
           and calls == [], calls)
        (dirty / "code.diff").write_text("")
        rp.replay(dirty, rp.RUN_CODE, runner=fake2)
        ok("有 code.diff 就照常跑", len(calls) == 1, calls)
        for i, bad in enumerate(["../evil.py", "/etc/evil.py", "skills/../../evil.py", ""]):
            r = make_run(base / f"b{i}", untracked=[bad])
            ok(f"untracked 路径 {bad!r} → 拒绝,不跑",
               raises(lambda r=r: rp.replay(r, rp.RUN_CODE, runner=fake2), ValueError) and len(calls) == 1, calls)
        skip = make_run(base / "c", untracked_skipped=[{"path": "skills/x/big.json", "reason": "超过 1 MB"}])
        res = rp.replay(skip, rp.RUN_CODE, runner=fake2)
        ok("存档时有文件没抄下来:照样跑,frozen 记 False,结果里列出来",
           res["code"]["frozen"] is False and res["code"]["untracked_skipped"][0]["path"] == "skills/x/big.json",
           res["code"])


FAKE_BOARD = r'''import argparse, json, os, sys, time
from pathlib import Path
ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd", required=True)
rc = sub.add_parser("recompute")
rc.add_argument("--inputs", required=True)
rc.add_argument("--seed", type=int, required=True)
a = ap.parse_args()
pkg = json.loads(Path(a.inputs).read_text())
items = pkg["items"]
if "boom" in items:
    sys.exit("故意失败")
if "sleep" in items:
    time.sleep(30)
here = Path(__file__).resolve().parent
extra = here / "extra.json"
print(json.dumps({"baseline": {"seed": a.seed, "keys": sorted(items), "meta": pkg.get("run_meta")},
                  "system": {"stock_lab": os.environ.get("STOCK_LAB"),
                             "extra": json.loads(extra.read_text()) if extra.is_file() else None,
                             "tag": "v2"}}))
'''

OLD_BOARD = r'''import argparse
ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("update")
ap.parse_args()
'''

BAD_DIFF = """diff --git a/README b/README
--- a/README
+++ b/README
@@ -1 +1 @@
-不是这一行
+y
"""

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}      # 不读本机全局配置(签名、钩子)


def _git(repo, *args):
    """一次性仓库专用:不签名、不读全局配置。"""
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True, env=GIT_ENV).stdout.strip()


def _worktrees(repo):
    return [x for x in _git(repo, "worktree", "list", "--porcelain").splitlines() if x.startswith("worktree ")]


def _throwaway_repo(base):
    """三个提交:c1 没有 board.py;c1b 的 board.py 不认 recompute;c2 是 FAKE_BOARD。
    另外造一份「存档时的未提交改动」(tag 改成 patched)和一个没进 git 的 extra.json。"""
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    c = {"repo": repo, "old": _git(repo, "rev-parse", "HEAD")}
    board = repo / rp.BOARD_REL
    board.parent.mkdir(parents=True)
    board.write_text(OLD_BOARD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1b")
    c["no_cmd"] = _git(repo, "rev-parse", "HEAD")
    board.write_text(FAKE_BOARD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c2")
    c["head"] = _git(repo, "rev-parse", "HEAD")
    board.write_text(FAKE_BOARD.replace('"tag": "v2"', '"tag": "patched"'))
    c["diff"] = _git(repo, "diff", "HEAD", "--binary") + "\n"
    _git(repo, "checkout", "--", str(board))
    c["rel"] = str(rp.BOARD_REL.parent / "extra.json")
    venv = base / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "python").chmod(0o755)
    c["tmps"] = base / "tmps"
    c["tmps"].mkdir()
    return c


def _clean(c):
    """worktree 只剩主工作区、临时目录空了。"""
    return len(_worktrees(c["repo"])) == 1 and list(c["tmps"].iterdir()) == []


def _git_happy(c, base):
    repo, head, rel = c["repo"], c["head"], c["rel"]
    run = make_run(base / "archive", seed=11, head=head, untracked=[rel], clean=False)
    (run / "code.diff").write_text(c["diff"])
    (run / "untracked" / rel).parent.mkdir(parents=True)
    (run / "untracked" / rel).write_text('{"x": 1}')
    res = rp.replay(run, "HEAD", repo=repo)
    ok("真跑:HEAD 的 board.py recompute 用了存档里的种子、读的是输入包(带 run_meta)",
       res["baseline"] == {"seed": 11, "keys": ["capex", "prices.300308"], "meta": META}, res)
    ok("子进程看不到 STOCK_LAB", res["system"]["stock_lab"] is None, res["system"])
    ok("记下 HEAD 解析成的提交号", res["code"]["commit"] == head, res["code"])
    ok("跑完 worktree 删掉了、临时目录删干净了", _clean(c), (_worktrees(repo), list(c["tmps"].iterdir())))

    res = rp.replay(run, rp.RUN_CODE, repo=repo)
    ok("版本写 run:提交号 + code.diff + 没进 git 的文件,原样还原当时的代码",
       res["system"]["tag"] == "patched" and res["system"]["extra"] == {"x": 1}
       and res["code"]["commit"] == head and res["code"]["frozen"] is True, res)
    ok("还原以后 worktree 照样删掉", _clean(c), (_worktrees(repo), list(c["tmps"].iterdir())))
    ok("仓库本身的工作区没被动过", _git(repo, "status", "--porcelain") == "", _git(repo, "status", "--porcelain"))

    corr = write(base / "corrected.json", {"items": {**ITEMS, "valuation.300308": item(public_at="2026-11-03")}})
    res = rp.replay(run, "HEAD", corrected=corr, repo=repo)
    ok("真跑修正输入:recompute 读到补上的项和原来的 run_meta",
       res["baseline"]["keys"] == ["capex", "prices.300308", "valuation.300308"]
       and res["baseline"]["meta"] == META, res["baseline"])
    ok("临时拼的输入文件也删干净了", _clean(c), list(c["tmps"].iterdir()))
    return run


def _git_failures(c, base, run):
    repo, head = c["repo"], c["head"]
    cases = [("旧版本没有 board.py", lambda: rp.replay(run, c["old"], repo=repo), RuntimeError, rp.NO_RECOMPUTE),
             ("有 board.py 但不认 recompute", lambda: rp.replay(run, c["no_cmd"], repo=repo), RuntimeError,
              rp.NO_RECOMPUTE)]
    boom = make_run(base / "archive2", seed=1, inputs={"boom": item()})
    cases.append(("recompute 自己失败(带它的输出)", lambda: rp.replay(boom, "HEAD", repo=repo), RuntimeError, "故意失败"))
    bad_diff = make_run(base / "archive3", head=head, clean=False)
    (bad_diff / "code.diff").write_text(BAD_DIFF)
    cases.append(("code.diff 打不上", lambda: rp.replay(bad_diff, rp.RUN_CODE, repo=repo), RuntimeError, "apply"))
    no_src = make_run(base / "archive4", head=head, untracked=[c["rel"]])
    cases.append(("manifest 列了没进 git 的文件,存档里却没有", lambda: rp.replay(no_src, rp.RUN_CODE, repo=repo),
                  FileNotFoundError, c["rel"]))
    slow = write(base / "slow.json", {"items": {"sleep": item()}, "run_meta": META})
    cases.append(("recompute 超时(timeout=1)", lambda: rp.run_recompute(
        repo, "HEAD", slow, 1, python=base / "venv" / "bin" / "python", timeout=1), RuntimeError, "超过 1 秒"))
    cases.append(("找不到的版本", lambda: rp.replay(run, "no-such-ref", repo=repo), RuntimeError, ""))
    for name, fn, exc, text in cases:
        ok(f"{name} → 报错", raises(fn, exc, text), name)
        ok(f"{name} 以后 worktree 和临时目录都删干净了", _clean(c), (_worktrees(repo), list(c["tmps"].iterdir())))

    other = base / "other-repo"          # 别的 git 仓库:不是要重跑的那个,只有「在不在工作区里」那条查得出
    (other / "sub").mkdir(parents=True)
    _git(other, "init", "-q")
    tempfile.tempdir = str(other / "sub")
    try:
        ok("临时目录落在别的 git 工作区里 → 报错", raises(lambda: rp.replay(run, "HEAD", repo=repo), RuntimeError,
                                                    "git 工作区"))
        ok("这样报错也没留下 worktree 和临时目录", len(_worktrees(repo)) == 1
           and list((other / "sub").iterdir()) == [], (_worktrees(repo), list((other / "sub").iterdir())))
    finally:
        tempfile.tempdir = str(c["tmps"])

    locked = base / "locked-wt"
    _git(repo, "worktree", "add", "--detach", str(locked), head)
    _git(repo, "worktree", "lock", str(locked))
    rp._remove_worktree(repo, locked)
    ok("锁住的 worktree 也删得掉(--force --force)", len(_worktrees(repo)) == 1 and not locked.exists(),
       _worktrees(repo))
    ok("找不到 venv 的 python → 报错写出要设 ASTOCK_VENV",
       raises(lambda: rp.venv_python({"ASTOCK_VENV": str(base / "无此 venv")}), RuntimeError, "ASTOCK_VENV"))


def check_replay_git():
    if shutil.which("git") is None:
        print("  ⏭  没有 git,跳过真跑 worktree 的检查")
        return
    with tmpdir() as base:
        c = _throwaway_repo(base)
        saved = (os.environ.get("ASTOCK_VENV"), tempfile.tempdir, fb_common.ORIG_ENV.get("STOCK_LAB"))
        os.environ["ASTOCK_VENV"] = str(base / "venv")
        tempfile.tempdir = str(c["tmps"])
        fb_common.ORIG_ENV["STOCK_LAB"] = str(base / "不该被读到")
        try:
            run = _git_happy(c, base)
            _git_failures(c, base, run)
        finally:
            if saved[0] is None:
                os.environ.pop("ASTOCK_VENV", None)
            else:
                os.environ["ASTOCK_VENV"] = saved[0]
            tempfile.tempdir = saved[1]
            if saved[2] is None:
                fb_common.ORIG_ENV.pop("STOCK_LAB", None)
            else:
                fb_common.ORIG_ENV["STOCK_LAB"] = saved[2]


CHECKS = [check_constants, check_suggest, check_suggest_inputs, check_confirm, check_jsonl_lines, check_aggregate,
          check_improvements, check_corrected, check_compare, check_replay_orchestration, check_replay_git]


def main():
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
