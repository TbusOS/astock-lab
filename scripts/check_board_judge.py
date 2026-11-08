#!/usr/bin/env python3
"""check_board_judge —— 看板第一屏(怎么看 / 价格可能晃多大 / 对照)的离线检查。

查四样:
A. 答卷「怎么看」的校验:合格的放行;概率合计 90、日期都在过去、事实没出处、不到 3 条事实、
   pick 不在选项里、价位写错,都要被拦下(每条反例都是一个故意写错的版本)。
B. 作答流程:这一块第一次不合格就重问;第二次还不合格,只丢这一块,答卷其余部分照用。
C. 页面数据:卖出线触发 / 没触发、手里没有、codex 选的一样 / 不一样、Claude 这块失败改用 codex、
   没有补考文件 / 有补考文件 —— 每种情况「怎么看」「对照」拼出来的东西对不对。
D. 页面:按规则写的那格盖住 AI 的答案、codex 不同意那行、没写出判断的提示、尚未验证跟着验证情况走、
   第一屏三块的顺序和原来整块收进「细节」、图上到期那天的价位按公式、手机上日期标签不重叠。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402,F401
import ai_answer as ai  # noqa: E402
import board_judge as bj  # noqa: E402
import check_board_ai as cba  # noqa: E402
import judgment  # noqa: E402
import render  # noqa: E402
import render_judge as rj  # noqa: E402

FAILS = 0
AS_OF = "2026-10-30"
KNOWN = {"prices.300308", "index.sz399006", "market.margin"} | set(judgment.EXTRA_SOURCES)
FIX = ROOT / "scripts" / "fixtures" / "board" / "board_data.json"


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:500]))
    if not cond:
        FAILS += 1


def _good(p0: float = 100.0) -> dict:
    return cba._ai_judgment(p0)


def _with(j: dict, **kw) -> dict:
    return {**json.loads(json.dumps(j)), **kw}


# ── A ───────────────────────────────────────────────────────────────────────
def check_validate():
    print("== A 答卷「怎么看」的校验")
    g = _good()
    ok("合格的放行", judgment.check("300308", g, AS_OF, KNOWN) == [], judgment.check("300308", g, AS_OF, KNOWN))
    bad = {
        "概率合计 90": _with(g, scenarios=[{**g["scenarios"][0], "p": 50}, g["scenarios"][1]]),
        "关键日期都在判断日以前": _with(g, key_dates=[{"date": "2026-10-01", "what": "过去的日子", "why": ""}]),
        "复核日在判断日以前": _with(g, review="2026-10-01"),
        "事实没出处": _with(g, facts=[*g["facts"][:2], {"kind": "需求", "text": "没出处的事实", "source": "我觉得"}]),
        "事实不到 3 条": _with(g, facts=g["facts"][:2]),
        "pick 不在选项里": _with(g, rebuy={"pick": "maybe", "answer": "看看", "why": "x"}),
        "价位写成 0": _with(g, scenarios=[{**g["scenarios"][0], "low_price": 0}, g["scenarios"][1]]),
        "价位下限大于上限": _with(g, scenarios=[{**g["scenarios"][0], "low_price": [120, 100]}, g["scenarios"][1]]),
        "只有一种情况": _with(g, scenarios=[{**g["scenarios"][0], "p": 100}]),
        "改判条件太短": _with(g, change_if="看看"),
        "没写已知事实": _with(g, fact=""),
        "三件事少了适用条件": _with(g, rebuy={"pick": "wait", "answer": "先等等", "why": "x"}),
    }
    want = {"概率合计 90": "加起来 90", "关键日期都在判断日以前": "key_dates", "复核日在判断日以前": "review",
            "事实没出处": "没写出处", "事实不到 3 条": "至少 3 条", "pick 不在选项里": "rebuy.pick",
            "价位写成 0": "low_price", "价位下限大于上限": "low_price", "只有一种情况": "2~4 种",
            "改判条件太短": "change_if", "没写已知事实": "fact 要写", "三件事少了适用条件": "rebuy.when"}
    for name, j in bad.items():
        es = judgment.check("300308", j, AS_OF, KNOWN)
        ok(f"反例:{name} → 被拦下,错误点出位置", any(want[name] in e for e in es) and all(e.startswith("300308.judgment") for e in es), es)
    ok("没有这一块 → 被拦下", judgment.check("300308", None, AS_OF, KNOWN) == ["300308.judgment 缺了"])
    ok("YYYY-MM 也算日期(按那个月 1 号)", judgment.check("300308", _with(g, key_dates=[{"date": "2026-12", "what": "x"}]),
                                                    AS_OF, KNOWN) == [])
    obj = {"stocks": {"300308": {"judgment": g, "x": 1}, "300502": {"judgment": bad["概率合计 90"], "x": 2}}}
    es = judgment.check_all(obj, ["300308", "300502"], AS_OF, KNOWN)
    m = judgment.mark_failed(obj, es)
    ok("只把有错的那只换成失败记录,别的不动,原对象不改", m["stocks"]["300308"]["judgment"] == g
       and m["stocks"]["300502"]["judgment"]["failed"] is True and m["stocks"]["300502"]["x"] == 2
       and "failed" not in obj["stocks"]["300502"]["judgment"], m["stocks"]["300502"])
    ok("页面能不能用:合格的能、失败记录不能、旧答卷没有这一块不能",
       judgment.usable(g) and not judgment.usable(m["stocks"]["300502"]["judgment"]) and not judgment.usable(None))


# ── B ───────────────────────────────────────────────────────────────────────
def check_flow():
    print("== B 作答流程:这一块不合格先重问,还不合格只丢这一块")
    pkg = cba._ai_package(ai)
    good = cba._ai_good()
    bad = json.loads(json.dumps(good))
    bad["stocks"]["300502"]["judgment"]["review"] = "2026-01-01"
    tmp = Path(tempfile.mkdtemp(prefix="check-judge-"))
    try:
        def runner_of(seq):
            calls = []

            def run(p, w):
                calls.append(p)
                return json.dumps(seq[min(len(calls), len(seq)) - 1])
            return run, calls
        r1, c1 = runner_of([bad, bad])
        got = ai.answer("claude", pkg, tmp / "2026-10-30" / "a1", runner=r1)
        ok("两次都只有这一块不合格 → 答卷照用,问了 2 次", "failed" not in got and len(c1) == 2, got.get("errors"))
        ok("…不合格的那只 judgment 换成失败记录,另一只和概率题原样", got["stocks"]["300502"]["judgment"].get("failed") is True
           and got["stocks"]["300308"]["judgment"] == good["stocks"]["300308"]["judgment"]
           and got["stocks"]["300502"]["horizons"] == good["stocks"]["300502"]["horizons"], got["stocks"]["300502"]["judgment"])
        ok("…重问时附上了这一块的错误", "300502.judgment.review" in c1[1], c1[1][-300:])
        meta = json.loads((tmp / "2026-10-30" / "a1" / "ai" / "claude" / "meta.json").read_text())
        ok("…meta 里记下这一块的错误", meta["failed"] is False and meta["attempts"][-1].get("judgment_errors"), meta["attempts"][-1])
        r2, c2 = runner_of([bad, good])
        got2 = ai.answer("claude", pkg, tmp / "2026-10-30" / "a2", runner=r2)
        ok("第一次这一块不合格、第二次改对 → 两只的这一块都能用", len(c2) == 2
           and all(judgment.usable(got2["stocks"][c]["judgment"]) for c in ("300308", "300502")))
        r3, c3 = runner_of([good])
        ai.answer("claude", pkg, tmp / "2026-10-30" / "a3", runner=r3)
        ok("第一次就合格 → 只问一次", len(c3) == 1)
        worse = json.loads(json.dumps(bad))
        worse["stocks"]["300502"]["bottom"]["month_probs"]["2026-11"] += 1
        r4, c4 = runner_of([worse, worse])
        got4 = ai.answer("claude", pkg, tmp / "2026-10-30" / "a4", runner=r4)
        ok("反例:概率题也错了 → 整份答卷仍算失败(这一块的宽松只管它自己)", got4.get("failed") is True, got4)
        ok("提示词里有「怎么看」的说明和骨架", "judgment:「怎么看」" in ai.build_prompt(pkg) and '"scenarios"' in ai.build_prompt(pkg))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── C ───────────────────────────────────────────────────────────────────────
def _answers(claude_j, codex_j, code="300308"):
    return {"claude": {"stocks": {code: {"judgment": claude_j}}}, "codex": {"stocks": {code: {"judgment": codex_j}}},
            "baseline": {"stocks": {code: {"vol_annual": 0.8}}}}


def check_data():
    print("== C 页面数据")
    g = _good()
    same = bj.judge_block("300308", _answers(g, g), shares=100, close=100.0, line=80.0, today=AS_OF, verified={})
    ok("Claude 为主;codex 选的一样 → 不写不同意", same["ok"] and same["author"] == "claude"
       and all(c["other"] is None for c in same["three"]), same["three"])
    diff = bj.judge_block("300308", _answers(g, _with(g, rebuy={"pick": "buy", "answer": "现在买", "why": "x"})),
                          shares=100, close=100.0, line=80.0, today=AS_OF, verified={})
    rb = next(c for c in diff["three"] if c["key"] == "rebuy")
    ok("codex 选的不一样 → 那一格写 codex 的答案和选项", rb["other"] == {"author": "codex", "answer": "现在买", "pick_text": "买"}, rb)
    words = bj.judge_block("300308", _answers(g, _with(g, rebuy={"pick": "wait", "answer": "换个说法也是等", "why": "x"})),
                           shares=100, close=100.0, line=80.0, today=AS_OF, verified={})
    ok("反例:只是措辞不同、选项一样 → 不算不同意", all(c["other"] is None for c in words["three"]))
    hit = bj.judge_block("300308", _answers(g, g), shares=100, close=75.0, line=80.0, today=AS_OF, verified={})
    h = hit["three"][0]
    ok("卖出线已触发 → 「手上的」由程序按规则写,不用 AI 的「拿着」", h["by"] == "rule" and h["answer"] == "卖(待执行)"
       and "已跌破" in h["why"] and h["pick"] == "sell", h)
    near = bj.judge_block("300308", _answers(g, g), shares=100, close=82.0, line=80.0, today=AS_OF, verified={})
    ok("离卖出线 5% 以内 → 用 AI 的答案,适用条件后面加一句离线多远(露在外面)", near["three"][0]["by"] == "ai"
       and "只差 2.5%" in near["three"][0]["when"], near["three"][0])
    far = bj.judge_block("300308", _answers(g, g), shares=100, close=120.0, line=80.0, today=AS_OF, verified={})
    ok("离卖出线远 → 原样用 AI 的", far["three"][0]["by"] == "ai" and "卖出线" not in far["three"][0]["why"])
    none = bj.judge_block("300308", _answers(g, g), shares=0, close=120.0, line=None, today=AS_OF, verified={})
    ok("手里没有 → 「手上的」写手里没有这只", none["three"][0]["answer"] == "手里没有这只" and none["three"][0]["by"] == "rule")
    unk = bj.holding_rule(None, 100.0, 80.0)
    ok("持仓读不出来 → 程序写读不出来(不说手里没有)", unk["answer"] == "持仓读不出来")
    fb = bj.judge_block("300308", _answers({"failed": True, "errors": ["合成错误"]}, g), shares=100, close=120.0, line=None,
                        today=AS_OF, verified={})
    ok("Claude 这一块失败 → 用 codex 的,不和自己比", fb["ok"] and fb["author"] == "codex"
       and all(c["other"] is None for c in fb["three"]), fb.get("author"))
    both = bj.judge_block("300308", _answers({"failed": True, "errors": ["甲"]}, None), shares=100, close=75.0, line=80.0,
                          today=AS_OF, verified={})
    ok("两边都没写出来 → ok=False、带错误,卖出线那格照样由程序写", not both["ok"] and both["errors"] == ["甲"]
       and both["rule"]["answer"] == "卖(待执行)", both)
    ok("下一个关键日期 = 今天及以后的第一个", same["next"]["date"] == "2026-11-18")
    ok("几种情况的价位写成「约 x~y」或「约 x」", same["scenarios"][0]["low_text"] == "约 90~100" and same["scenarios"][1]["low_text"] == "约 70")
    series = [["d", 100.0 + i] for i in range(80)] + [["d", 60.0]]
    rn = bj.ref_now("300308", {"system": {"stocks": {"300308": {"signals": {"weights": {"valuation": 0.025, "capex": 0.0}}}}}},
                    {"system": {"horizons": {"63": {"p_up": 52}}}}, series, {"close": 2400.0, "ma60": 3200.0})
    ok("对照现状:系统 3 个月涨的种数、还起作用的信号只列权重大于 0 的", rn["system_p63"] == 52 and rn["system_active"] == ["估值"], rn)
    ok("存档里用到、现在权重已是 0 的信号单独列出(存档早于改权重)", rn["system_stopped"] == [], rn)
    old = bj.ref_now("300308", {"system": {"stocks": {"300308": {"signals": {"weights": {"capex": 0.05, "valuation": 0.025}}}}}},
                     {}, series, {"close": 2400.0, "ma60": 3200.0})
    ok("…旧存档用了云服务公司投资(现在是 0)→ 列进已停用", old["system_stopped"] == ["大型云服务公司投资"], old)
    fresh = bj.ref_now("300308", {}, {}, series[:-1], {"close": 2400.0, "ma60": 3200.0}, now=["e", 60.0])
    ok("有更新的收盘 → 接在后面算(和「手上的」那格同一个收盘)", fresh["dd_1y"] == rn["dd_1y"], (fresh["dd_1y"], rn["dd_1y"]))
    ok("对照现状:离 60 日线、创业板指离线、离一年高点、跌超一半", rn["index_ma_gap"] == -25.0 and rn["stock_ma_gap"] < 0
       and rn["t60i_ok"] is False and rn["dd_1y"] == -66.5 and rn["dip_hit"] is True, rn)   # 60 ÷ 179 − 1
    tmp = Path(tempfile.mkdtemp(prefix="check-judge-"))
    try:
        ok("没有补考文件 → 对照不给过去检验那几句", bj.ref_past(tmp) == {})
        d = tmp / "history" / "2026-11-03"
        d.mkdir(parents=True)
        port = lambda c, m: {"portfolio": {"cagr": c, "mdd": m}}  # noqa: E731
        (d / "rules.json").write_text(json.dumps({"meta": {"main": ["a"] * 10, "control": ["b"] * 19}, "groups": {
            "main": {"summary": {"H": {"early": port(0.03, 0.54), "late": port(0.88, 0.56)},
                                 "T60I": {"early": port(0.03, 0.29), "late": port(0.41, 0.25)}},
                     "dip50": {"63": {"dip": {"up": 0.8, "median_further": -0.07}}}},
            "control": {"dip50": {"63": {"dip": {"up": 0.57, "median_further": -0.07}}}}}}), encoding="utf-8")
        cell = lambda sp, ch: {"shift": {"spread": sp, "chance": ch}}  # noqa: E731
        (d / "thinking.json").write_text(json.dumps({"ideas": {
            "反的": {"main": {"early": {"63": cell(-0.17, 0.04)}}},
            "有用": {"main": {"late": {"63": cell(0.36, 0.03)}}},
            "碰巧": {"main": {"early": {"63": cell(0.24, 0.06)}}},
            "碰巧(线上口径)": {"main": {"early": {"63": cell(0.5, 0.0)}}}}}), encoding="utf-8")
        (d / "history.json").write_text(json.dumps({"summary": {"baseline": {"63": {"cover80": 0.81, "by_period": {
            "late": {"cover80": 0.76}}}}}}), encoding="utf-8")
        rp = bj.ref_past(tmp)
        ok("有补考文件 → 规则的过去成绩、跌超一半那句、覆盖率都读出来", "29%" in rp.get("t60i", "") and "80 次涨" in rp.get("dip50", "")
           and rp.get("cover", {}).get("63") == ("81%", "76%") and rp.get("from") == "2026-11-03", rp)
        ok("想法计数:错开检验 ≤5% 为正算有用、为负算反了;0.06 算碰巧;「线上口径」那条不重复数",
           "3 条想法里,1 条跟碰巧分不开、1 条是反的、1 条只在部分样本里有一点用" in rp.get("ideas", ""), rp.get("ideas"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── D ───────────────────────────────────────────────────────────────────────
def _overlaps(labels, rows, fs):
    boxes = []
    for (x, t, a), r in zip(labels, rows):
        w = rj._tw(t, fs)
        x0 = x if a == "start" else (x - w if a == "end" else x - w / 2)
        boxes.append((r, x0, x0 + w))
    return any(r1 == r2 and not (b1 + 6 < a2 or a1 > b2 + 6) for i, (r1, a1, b1) in enumerate(boxes)
               for (r2, a2, b2) in boxes[i + 1:])


def check_page():
    print("== D 页面")
    data = json.loads(FIX.read_text(encoding="utf-8"))
    html = render.render_board(data)
    b308 = html[html.index('id="s-300308"'):]
    b308 = b308[:b308.index("</article>")]
    order = [b308.find(w) for w in ('class="jcard judge"', 'class="jcard band', 'class="jcard ref"', '<details class="detail">',
                                    '<section class="verdict">')]
    ok("每只股票:怎么看 → 价格可能晃多大 → 对照 → 细节(原来的结论条收在细节里)", all(i >= 0 for i in order)
       and order == sorted(order), order)
    ok("「现在」在股票切换之前,整个账户一份", html.index('class="jcard now0"') < html.index('class="tabs"')
       and html.count('class="jcard now0"') == 1)
    ok("codex 选得不一样 → 写出 codex 不同意", "codex 不同意:合成:trim(选的是「减一部分」)" in b308, b308[:200])
    ok("尚未验证:这一层没验证过 → 标", "按当时的事实判断 · 尚未验证" in b308)
    good = json.loads(json.dumps(data))
    good["validated"] = dict.fromkeys(good["validated"], True)
    ok("反例:这一层验证过 → 不标尚未验证", "按当时的事实判断 · 尚未验证" not in render.render_board(good))
    hit = json.loads(json.dumps(data))
    s = next(x for x in hit["stocks"] if x["code"] == "300308")
    s["judge"]["three"][0].update(bj.holding_rule(100, 1.0, 320.0), by="rule")
    hh = render.render_board(hit)
    ok("按规则写的那格:显示规则的答案、标「按规则」,不显示 AI 的「拿着」", "卖(待执行)" in hh and 'class="by">按规则' in hh and "按规则卖" not in hh
       and "合成:hold" not in hh[hh.index('id="s-300308"'):hh.index("什么情况要重新检查", hh.index('id="s-300308"'))])
    fail = json.loads(json.dumps(data))
    for x in fail["stocks"]:
        x["judge"] = {"ok": False, "author": None, "errors": ["合成:没写出来"], "rule": None}
    fh = render.render_board(fail)
    ok("没写出判断 → 写「这一层这次没写出判断」和原因", "这一层这次没写出判断" in fh and "合成:没写出来" in fh)
    p = data["stocks"][1]["band"]
    for h in ("63", "250"):
        z = rj.Z["90"]
        want = p["s0"] * 2.718281828459045 ** (p["sigma"] * int(h) ** 0.5 * z)
        ok(f"{h} 天:图右边「约一成高于」的价按公式", f"约一成高于 {rj._f0(want)}" in rj.band_svg(p, int(h), None, False, "t"),
           rj._f0(want))
    fs = 12
    xs = [(40.0, "2026-05", "start"), (180.0, "起点 10-30", "middle"), (262.0, "到期约 27-01-29", "end")]
    rows = rj.x_label_rows(xs, fs)
    ok("手机宽度上日期标签分行后不重叠", not _overlaps(xs, rows, fs), rows)
    ok("反例:都放第一行就会重叠(上面那条检查抓得到)", _overlaps(xs, [0, 0, 0], fs))
    ok("宽、窄两份图都在,只显示一份由样式管", b308.count('<div class="wide">') == 3 and b308.count('<div class="narrow">') == 3)
    ok("页面上没有「journal」「云厂开支」「补考」这类词(第一屏)", not re.search(r"journal|补考",
       b308[:b308.index('<details class="detail">')]), None)


def _visible(html: str) -> str:
    """去掉所有 <details>…</details>(折叠里的不算露在外面),再去掉标签。"""
    while "<details" in html:
        i = html.rfind("<details")
        j = html.find("</details>", i)
        if j < 0:
            break
        html = html[:i] + html[j + len("</details>"):]
    return re.sub(r"<[^>]+>", "", html)


def check_review8():
    print("== E codex 第四轮 8 条")
    data = json.loads(FIX.read_text(encoding="utf-8"))
    acc = {**data["account"], "cash": 5000.0, "month_need": 3000.0, "need_month": "2026-11", "free_cash": -128000.0,
           "free_cash_until": "2027-05", "note": "2026-11 要还 3,000.00,可取现金 5,000.00,够了,不用卖;跌破卖出线的全卖约 48,020.00,够这个月还款;"
                                                "借款看板里还有没录进来的借款(合成)",
           "reminders": ["借款看板里还有没录进来的借款(合成)", "合成:银行卡里还有现金没记"]}
    now = rj.now_html({**data, "account": acc})
    vis = _visible(now)
    ok("1 现金够 → 写「还款现金够,不用靠卖股票筹钱」,露在外面的不出现「不用卖」", "还款现金够,不用靠卖股票筹钱" in vis
       and "够了,不用卖" not in vis and "够了,不用卖" in now, vis)
    short = _visible(rj.now_html({**data, "account": {**acc, "cash": 1000.0}}))
    ok("1 反例:现金不够 → 写还差多少、要靠卖股票筹钱,卖不卖看每只股票那一行", "还差 0.2 万,要靠卖股票筹钱" in short
       and "还款现金够" not in short, short)
    two = {**acc, "cover_months": 2, "cover_need": 5100.0, "cover_from": "2026-11", "cover_until": "2026-12",
           "cash": 6000.0}
    v2 = _visible(rj.now_html({**data, "account": two}))
    ok("1b 配了留够两个月 → 写两个月合计和够不够,不只写这个月", "2026-11~2026-12 两个月要还 0.5 万,可取现金 0.6 万" in v2
       and "还款现金够" in v2 and "2026-11 要还 0.3 万" not in v2, v2)
    v3 = _visible(rj.now_html({**data, "account": {**two, "cash": 3000.0}}))
    ok("1b 反例:两个月不够 → 写还差多少", "还差 0.2 万" in v3 and "还款现金够" not in v3, v3)
    ok("1c 只留一个月(老数据没有 cover_months)→ 照旧写这个月", "2026-11 要还 0.3 万" in vis, vis)
    ok("2 露在外面的:持仓、本月够不够、后续缺口(负数那句)", "持仓:" in vis and "后续还款:到 2027-05 为止要还的钱,现金还差 12.8 万" in vis, vis)
    ok("2 漏录借款的提醒露在外面,别的明细(全卖约多少、银行卡)收进「展开还款明细」",
       "账目还没补齐:借款看板里还有没录进来的借款(合成)" in vis and "48,020" not in vis and "银行卡" not in vis
       and "展开还款明细" in now and "48,020" in now and "银行卡" in now, vis)
    html = render.render_board(data)
    b = html[html.index('id="s-300308"'):]
    b = b[:b.index('<details class="detail">')]
    rows = re.findall(r'<div class="r3">(.*?)</div>', b, re.S)
    ok("3 三件事是三行,每行露出问题 · 结论 · 适用条件,完整理由在折叠里", len(rows) == 3 and all(
        'class="q"' in r and 'class="ans"' in r and 'class="when"' in r and '<details class="why">' in r for r in rows)
       and "合成理由甲" not in _visible(b) and "合成理由甲" in b, [r[:120] for r in rows])
    top = html[html.index('<header class="top'):html.index("</header>")]
    tv = _visible(top)
    ok("4 顶栏露在外面的只有行情日期、判断时间、每日检查和大盘一句", all(w in tv for w in ("行情更新于", "判断作于", "每日检查"))
       and not any(w in tv for w in ("代码版本", "持仓数据截至", "参与系统计算", "只给 AI 参考", "存档 ")), tv)
    ok("4 代码版本、存档、数据去向收进「数据与版本」", "数据与版本" in top and all(w in top for w in ("代码版本", "持仓数据截至")))
    import board_data
    mr = board_data.market_rows({"market.regime": {"status": "ok", "value": {"close": 2400.0, "ma60": 3200.0, "state": "below",
                                                                              "days": 76}}})
    ok("4 大盘那句写「已连续 76 个交易日低于 60 日均线」", "已连续 76 个交易日低于 60 日均线" in mr["regime"]["text"], mr["regime"]["text"])
    seen = render.render_board({**data, "seen_problem": "没有"})
    note = seen[seen.index('<div class="notice">没找到'):]
    note = note[:note.index("</div>")]
    ok("5 黄色提醒:白话一句露在外面,文件名在「技术原因」里", "没找到以前的买卖记录,「最晚哪天做」暂时从今天算" in _visible(note)
       and "actions_seen.json" not in _visible(note) and "actions_seen.json)没有" in note, note)
    p = data["stocks"][1]["band"]
    narrow = rj.band_svg(p, 63, 320.0, True, "t")
    sizes = [float(x) for x in re.findall(r'font-size="([\d.]+)"', narrow)]
    ok("6 手机图里的字都 ≥ 14(屏幕上约 13px)", sizes and min(sizes) >= 14, sorted(set(sizes)))
    xt = [m for m in re.findall(r">([^<>]+)</text>", narrow) if m.startswith(("20", "起点", "到期"))]
    ok("6 横轴日期只放三个:开头、起点、到期", len(xt) == 3 and not any("+3 个月" in m or "+半年" in m for m in narrow.split("</text>")), xt)
    fig = b[b.index('<figure class="bh"'):b.index("</figure>")]
    ok("6 图下固定一句「这是到期那天的收盘范围,不是这段时间的最低价」,图例和算法在折叠里", rj.BAND_NOTE in _visible(fig)
       and "怎么看这张图、怎么算的" in b and "每天各自估计的八成范围" not in _visible(b), _visible(fig)[-200:])
    ok("7 情况条:百分比旁标「主观」,写「估计低点可能在」,不写「最低收盘在」", 'class="subj">主观' in b
       and "估计低点可能在" in b and "最低收盘在" not in _visible(b))
    ok("7 改判条件的标签写「什么情况要重新检查这个判断」", "什么情况要重新检查这个判断" in b and "什么情况改判" not in b)
    pr = ai.build_prompt(cba._ai_package(ai))
    ok("7 提示词要求改判条件写「会削弱……要重新检查……」,不写证明口吻", "会削弱" in pr and "要重新检查" in pr and "这就说明" in pr)
    fv = _visible(b)
    ok("8 第一屏露在外面的没有「改判」「中间五成」「×」算式;估价算式在「价位怎么估的」里",
       not any(w in fv for w in ("改判", "中间五成", "×")) and "价位怎么估的" in b, [w for w in ("改判", "中间五成", "×") if w in fv])
    ok("8 反例:把算式写进「what」会被这条检查抓到", "×" in _visible(rj._scen([{"label": "A", "p": 50, "what": "×7 估的",
                                                                         "low_when": "x", "low_text": "约 1", "how": ""}])))
    ok("8 提示词提醒 AI:写「提高情况 C 的可能」、不写「中间五成」、算式只进 how", "提高情况 C 的可能" in pr and "中间五成" in pr
       and "只写在 scenarios 的 how 里" in pr)


def main():
    for fn in (check_validate, check_flow, check_data, check_page, check_review8):
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
