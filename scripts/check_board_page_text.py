#!/usr/bin/env python3
"""check_board_page_text —— 预测看板页面上的字和数:从哪来、怎么换算、怎么写。

用法:
    python3 scripts/check_board_page_text.py        # 退出码 0 才算过

为什么单独一份:check_board_pages.py 已经 700 多行。这里查页面上一句话、一个数对不对:
AI 答卷里的价换不换算、一层没作答和没让它答怎么区分、数据进了哪里、模板替换、
最低点月份怎么写、动作按哪份代码算的、买点规则那道题怎么算分。

不联网、不碰真实数据:页面用 scripts/fixtures/board/board_data.json(合成数字),
要现算的用 check_board_pages.make_lab 在临时目录里搭工作台。
写法和 check_board_pages.py 一样:每块一个函数,登记进 CHECKS。
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402,F401  先于其他模块,和正式运行的顺序一样
import board_data  # noqa: E402
import board_io  # noqa: E402
import render  # noqa: E402
import check_board_pages as pages  # noqa: E402  合成工作台、fixture

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:500]))
    if not cond:
        FAILS += 1


def _try(fn):
    try:
        return fn()
    except Exception as e:          # 老代码签名不同、碰到没见过的写法可能直接报错,当成不过
        return f"出错:{type(e).__name__}: {e}"


# ── 答卷里的价怎么换算 ──────────────────────────────────────────────────────
def _view(ans: dict, author: str):
    """layer_view 要传作者;老版本只收三个参数(不分作者),那就按老的调,看它算出什么。"""
    try:
        return board_data.layer_view(ans, "300308", 330.0, author)
    except TypeError:
        return _try(lambda: board_data.layer_view(ans, "300308", 330.0))


def check_ai_start_price():
    """AI 的价本来就是起点那天的不复权价,不按 start_price 换算;只有统计底线、系统算法要换。"""
    block = {"horizons": {"63": {"p_up": 50, "q": {"10": 300.0, "25": 320.0, "50": 330.0, "75": 350.0, "90": 380.0}}},
             "start_price": 660.0}      # AI 照抄了输入包里那两层的后复权起点价(合成)
    ans = {"stocks": {"300308": block}}
    ai = _view(ans, "claude")
    q = (((ai if isinstance(ai, dict) else {}).get("horizons") or {}).get("63") or {}).get("q") or {}
    ok("AI 答卷多写了 start_price:价照原样(中间价 330,不减半)", q.get("50") == 330.0, ai)
    sys_view = _view(ans, "system")
    sq = (((sys_view if isinstance(sys_view, dict) else {}).get("horizons") or {}).get("63") or {}).get("q") or {}
    ok("系统算法的价照旧按 start_price 换算(660 → 330,中间价 165)", sq.get("50") == 165.0, sys_view)


# ── 没让它答 ≠ 作答失败 ─────────────────────────────────────────────────────
def check_not_asked():
    """forecast --no-codex 那次存档里没有 codex 答卷:页面写「没让它答」,不写「作答失败」。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-page-text-"))
    try:
        run, cfg = pages.make_lab(tmp)
        (run / "answers" / "codex.json").unlink()
        d = render.build_board_data(run, cfg, pages.TODAY, state=pages.SYN_STATE)
        html = render.render_board(d)
        ok("没有 codex 答卷:顶栏写「codex 这次没让它答」", "codex 这次没让它答" in html, html[:200])
        ok("没有 codex 答卷:哪儿都不写「codex 这次作答失败」", "codex 这次作答失败" not in html
           and "codex" not in (d.get("layers_failed") or {}), d.get("layers_failed"))
        ok("各层那张表:codex 那行写没让它答", "<td>codex</td><td colspan=3>没让它答</td>" in html, "")
        run2, cfg2 = pages.make_lab(tmp / "failed", codex_failed=True)
        h2 = render.render_board(render.build_board_data(run2, cfg2, pages.TODAY, state=pages.SYN_STATE))
        ok("真的作答失败:照旧写作答失败", "codex 这次作答失败" in h2 and "没让它答" not in h2, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 最晚哪天做 ──────────────────────────────────────────────────────────────
def _render(run, cfg, day, closes=None):
    """画一次页面,和真的一样画完把每只的动作和从哪天起记下来(老版本没有这一步就不记)。
    → (300308 的动作, 它「怎么做」那格, 页面)。"""
    d = render.build_board_data(run, cfg, day, state={**pages.SYN_STATE, "asof": day.isoformat()}, closes=closes)
    rec = getattr(board_io, "record_actions_seen", None)
    if rec:
        rec(run, d, day)
    html = render.render_board(d)
    s = next(x for x in d["stocks"] if x["code"] == "300308")
    return s["action"], pages._cell(html[html.index('id="s-300308"'):], "怎么做"), html


def check_deadline_overdue():
    """最晚日期从起点日(11-02)和这种动作第一次出现那天里较晚的那天数。合成:卖出线 320,起点收盘 330。
    起点就有的减仓,隔几天重画截止日不动,过了写「已过最晚日期」;11-10 才跌破卖出线的卖出有自己的两天。"""
    D = pages.date
    tmp = Path(tempfile.mkdtemp(prefix="check-page-text-"))
    try:
        run, cfg = pages.make_lab(tmp / "a")
        a1, c1, _ = _render(run, cfg, D(2026, 11, 2))
        a2, c2, _ = _render(run, cfg, D(2026, 11, 12))
        ok("11-02 画:减仓最晚 11-04", a1.get("deadline") == "2026-11-04" and "最晚 2026-11-04 做完" in c1, c1[:200])
        ok("11-12 再画还挂着:写「已过最晚日期 2026-11-04,还没做」", "已过最晚日期 2026-11-04,还没做" in c2, c2[:200])
        run2, cfg2 = pages.make_lab(tmp / "b")
        _render(run2, cfg2, D(2026, 11, 2))
        a3, c3, _ = _render(run2, cfg2, D(2026, 11, 10), {"300308": ["2026-11-10", 300.0]})
        ok("11-10 才跌破卖出线:写「最晚 2026-11-12 做完」,不写已过", a3.get("kind") == "sell"
           and "最晚 2026-11-12 做完" in c3 and "已过最晚日期" not in c3, c3[:200])
        a4, c4, _ = _render(run2, cfg2, D(2026, 11, 13), {"300308": ["2026-11-13", 300.0]})
        ok("三个交易日后还没卖:写「已过最晚日期 2026-11-12,还没做」", "已过最晚日期 2026-11-12,还没做" in c4, c4[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_seen_file():
    """记「从哪天起」的文件没有或读不出来:最晚日期从今天算,顶栏写一句;是别的存档的就从头记,不用写。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-page-text-"))
    try:
        run, cfg = pages.make_lab(tmp)
        f = run.parent.parent / "actions_seen.json"
        got = {}
        for name, body in (("没有", None), ("读不出来", "{坏的"),
                           ("别的存档", json.dumps({"run": "2026-01-02/150000", "stocks": {"300308": {"kind": "trim",
                                                                                              "since": "2026-01-05"}}}))):
            if body is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(body, encoding="utf-8")
            a, _, html = _render(run, cfg, pages.date(2026, 11, 5))
            f.unlink(missing_ok=True)
            got[name] = (a.get("deadline"), "actions_seen.json)" + name in html, "actions_seen.json)" in html)
        ok("没有记录:最晚日期从今天(11-05)算 → 11-09,顶栏写「没有」", got["没有"][:2] == ("2026-11-09", True), got["没有"])
        ok("记录读不出来:从今天算,顶栏写「读不出来」", got["读不出来"][:2] == ("2026-11-09", True), got["读不出来"])
        ok("记录是别的存档的:从今天算,顶栏不写", got["别的存档"] == ("2026-11-09", False, False), got["别的存档"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_events_note():
    """财报日历、SEC 新文件用的是存档里冻结的那份时(没刷新过、events.json 坏了、是别的存档的),
    「关键日期」那块写一句;刷新过的照用,不写。手改坏的值不让页面崩。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-page-text-"))
    try:
        run, cfg = pages.make_lab(tmp)
        f = run.parent.parent / "events.json"
        rel = f"{run.parent.name}/{run.name}"
        fresh = {"events.calendar": {"status": "ok", "as_of": "2026-11-04", "source": "合成", "used_by": "ai",
                                     "value": [{"symbol": "ZZZZ", "name": "合成新财报", "beijing": "2026-11-21 05:00"}]}}
        cases = [("没有", None, "还没刷新过"), ("坏的", "{坏", "读不出来"),
                 ("别的存档", json.dumps({"run": "2026-01-02/150000", "items": fresh}), "别的存档"),
                 ("手改坏的值", json.dumps({"run": rel, "items": {"events.calendar": {"status": "ok", "value": 7}}}), None),
                 ("这次存档的", json.dumps({"run": rel, "items": fresh}), None)]
        for name, body, want in cases:
            if body is not None:
                f.write_text(body, encoding="utf-8")
            try:
                html = render.render_board(render.build_board_data(run, cfg, pages.TODAY, state=pages.SYN_STATE))
            except Exception as e:      # 老代码碰到手改坏的值会崩
                html = f"出错:{type(e).__name__}"
            sec = html[html.find('<section class="dates">'):]
            sec = sec[:sec.find("</section>")]
            say = "用的是存档里冻结的那份" in sec
            ok(f"events.json {name}:" + (f"写一句(…{want})" if want else "不写冻结那句,页面照画"),
               (say and want in sec) if want else (not say and not html.startswith("出错")), (sec[-300:], html[:40]))
            f.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 持仓过期:顶栏和动作用同一个判断 ────────────────────────────────────────
def check_one_stale_rule():
    """state 是几天前算好的(没标过期),今天看已经过了 7 天:动作是「先更新持仓」,顶栏也要说过期。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-page-text-"))
    try:
        run, cfg = pages.make_lab(tmp)
        state = {**pages.SYN_STATE, "asof": "2026-10-20", "stale": False}     # 到 TODAY(11-02)已经 13 天
        d = render.build_board_data(run, cfg, pages.TODAY, state=state)
        kinds = {s["action"].get("kind") for s in d["stocks"]}
        ok("动作:先更新持仓", kinds == {"update_holdings"}, kinds)
        ok("顶栏的持仓状态也是过期(和动作同一个判断)", d["account"]["stale"] is True, d["account"])
        ok("页面顶栏写持仓过期", "持仓数据截至 2026-10-20,已过期" in pages_banner(render.render_board(d)), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pages_banner(html: str) -> str:
    i = html.find('id="stale-banner"')
    return html[i:html.find("</div>", i)] if i >= 0 else ""


# ── 页面上的几句话 ──────────────────────────────────────────────────────────
def _meta(item, key):
    try:
        return board_data._meta(item, key)
    except TypeError:               # 老版本只收一个参数
        return board_data._meta(item)


def check_used_by_default():
    """一项输入没记 used_by(老存档、手写的项):数据去向按键查,不一律写「只给 AI 参考」。"""
    got = [_meta({"key": "prices.300308", "status": "ok"}, None)["used_by"], _meta({"status": "ok"}, "sectors")["used_by"],
           _meta({"status": "ok", "used_by": "ai"}, "prices.300308")["used_by"]]
    ok("日线 → 参与系统计算;板块 ETF → 只展示;记了 used_by 的照记的", got == ["参与系统计算", "只展示", "只给 AI 参考"], got)


def check_fill_once():
    """模板一次换完:AI 写的理由里出现 {{DATA}} 这种字样,不会被当成占位符再换一次。"""
    ok("_fill:填进去的内容不再被替换", _try(lambda: render._fill("A={{A}};B={{B}}", {"A": "{{B}}", "B": "x"}))
       == "A={{B}};B=x", _try(lambda: render._fill("A={{A}};B={{B}}", {"A": "{{B}}", "B": "x"})))
    ok("_fill:占位符名字里有下划线、数字也认", _try(lambda: render._fill("{{A_1}}|{{B2}}", {"A_1": "x", "B2": "y"}))
       == "x|y", _try(lambda: render._fill("{{A_1}}|{{B2}}", {"A_1": "x", "B2": "y"})))
    d = pages.load_fixture()
    d["stocks"][0]["action"]["why"] = "合成:{{DATA}} {{SCRIPT}}"
    html = render.render_board(d)
    ok("页面:理由里写了 {{DATA}},内联数据照样只有一份", html.count('id="board-data"') == 1, html.count('id="board-data"'))


def check_entry_ma():
    """入场条件「本股收盘在均线上」用的均线和系统算法算买点的是同一条(system_forecast.BUY_MA),不写死 60。"""
    import system_forecast
    keep = system_forecast.BUY_MA
    series = [[f"d{i}", 100.0 if i < 100 else 90.0] for i in range(120)]      # 最后 20 根掉到 90
    empty = {"buy": {"conditions": []}}
    try:
        system_forecast.BUY_MA = 20
        f = board_data.facts(series)
        rows = board_data._entry({"system": empty, "claude": empty, "codex": empty}, f, {})
    finally:
        system_forecast.BUY_MA = keep
    ok("均线换成 20 日:默认的两条入场条件写 20 日线", [r["text"] for r in rows] == ["创业板指收盘在 20 日线上", "本股收盘在 20 日线上"],
       [r["text"] for r in rows])
    ok("均线换成 20 日:本股那条按 20 日均线判(收盘 90 = 均线,不在线上)", rows[1]["ok"] is False and f.get("ma") == 90.0,
       (rows[1], f))


def check_month_and_code_note():
    """见底月份写「11 月」(和判断那天不是同一年才写年份);顶栏写明「怎么做」按页面生成那天的代码现算。"""
    d = pages.load_fixture()
    html = render.render_board(d)
    cell = pages._cell(html, "见底")
    ok("见底那格写「x 月」,不写「2026-11」", "月(100 种里" in cell and not re.search(r"\d{4}-\d{2}(?!-)", cell), cell[:300])
    ok("月份文字:同一年只写月,跨年写年份", render.month_text("2026-11", "2026-10-30") == "11 月"
       and render.month_text("2027-01", "2026-10-30") == "2027 年 1 月"
       if hasattr(render, "month_text") else False, "")
    ok("顶栏写明「怎么做」按页面生成那天的持仓、收盘和当前代码现算", "按这天的持仓、收盘和当前代码现算" in html, "")


CHECKS = [check_ai_start_price, check_not_asked, check_deadline_overdue, check_seen_file, check_events_note,
          check_one_stale_rule, check_used_by_default, check_fill_once, check_entry_ma, check_month_and_code_note]


def main():
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
