#!/usr/bin/env python3
"""check_board_notices —— 预测看板:没取到、没查成、过期的东西要写到页面和 status.json 上。

用法:
    python3 scripts/check_board_notices.py        # 退出码 0 才算过

为什么单独一份:每日检查是定时任务,没人看着跑;update、forecast 也常常只看页面。
取数失败只打在 stderr 上,页面上「没取到的那只」和「查过了、没事的那只」一模一样。
这里查的就是这类:哪样东西没取到,页面或 status.json 上要写出来。

不联网、不碰真实数据:工作台搭在临时目录里(check_board_cli.Lab),存档用 check_board_pages.make_lab
现搭(合成行情、合成答卷),行情和取数器用 BOARD_FAKE_* 换成假的。
写法和 check_board_cli.py 一样:每块一个函数,登记进 CHECKS。
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402,F401  先于其他模块,和正式运行的顺序一样
import check_board_cli as cli  # noqa: E402  临时工作台、合成行情、合成答卷
import check_board_pages as pages  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:600]))
    if not cond:
        FAILS += 1


def _page(lab, name: str = "看板.html") -> str:
    p = lab.out / name
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _banner(html: str) -> str:
    i = html.find('id="stale-banner"')
    return html[i:html.find("</div>", i)] if i >= 0 else ""


def _lab_with_run(tmp: Path):
    """临时工作台 + 一次已经跑完的存档(make_lab 现搭,不跑 forecast)。"""
    lab = cli.Lab(tmp)
    run, _ = pages.make_lab(lab.lab)
    return lab, run


# ── update:收盘价取不到 ─────────────────────────────────────────────────────
def check_update_notices():
    """update 时一只的收盘取不到、另一只近 20 天没有日线:页面顶栏写出来,不只打在 stderr。"""
    tmp = cli._tmp()
    try:
        lab, _ = _lab_with_run(tmp)
        lab.fakes(prices_obj={"index": [], "hfq": {}, "close": {"300308": "坏的", "300502": []}}, inputs_obj={})
        rc, out, err = lab.run("update")
        ban = _banner(_page(lab))
        ok("update:收盘价取不到 → 退出码 1,stderr 写明", rc == 1 and "300308 收盘价取不到" in err, (rc, err[-400:]))
        ok("update:页面顶栏写出哪只没取到、页面用的是存档那天的收盘",
           "300308 收盘价取不到" in ban and "页面和动作用的是存档那天的收盘" in ban, ban[:600])
        ok("update:近 20 天没有日线的那只也写到顶栏", "300502 近 20 天没有日线" in ban, ban[:600])
        ok("update:停牌那只写「停牌?」,不写成「这次刷新没取到」", "300502 近 20 天没有日线(停牌?)" in ban
           and "没取到:300502" not in ban, ban[:600])
        phone = _banner(_page(lab, "看板_手机版.html"))
        ok("update:手机版顶栏一样写", "300308 收盘价取不到" in phone, phone[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── forecast:长日线取不到 ───────────────────────────────────────────────────
def check_forecast_history_failed():
    """长日线(检验信号、算历史同类情形用)取不到:stderr 写原因;输入包里记一项 history.long(failed、
    used_by=system),页面「本次缺什么」列出来;信号一律算没检验过。"""
    tmp = cli._tmp()
    try:
        lab = cli._forecast_lab(tmp)
        bad = cli.fake_prices()
        bad["hfq"]["300308"] = "坏的"          # 长日线要取后复权,这只取数抛错
        lab.fakes(prices_obj=bad)
        rc, out, err = lab.run("forecast")
        runs = lab.runs()
        ok("forecast:长日线取不到照样出预测(退回近两年),退出码 0", rc == 0 and len(runs) == 1, (rc, err[-600:]))
        ok("forecast:stderr 写明长日线取不到", "长日线取不到" in err, err[-600:])
        if not runs:
            return
        raw = json.loads((runs[0] / "inputs.json").read_text(encoding="utf-8"))
        it = (raw.get("items") or {}).get("history.long") or {}
        ok("forecast:输入包里记一项 history.long(failed、参与系统计算、note 写原因)",
           it.get("status") == "failed" and it.get("used_by") == "system" and "长日线取不到" in str(it.get("note")), it)
        val = (raw.get("run_meta") or {}).get("validated") or {}
        ok("forecast:信号一律算没检验过(validated 全是 False)", val and not any(val.values()), val)
        page = _page(lab)
        cell = pages._cell(page, "本次缺什么")
        ok("forecast:页面「本次缺什么」列出长日线", "长日线" in cell, cell[:400])
        ok("forecast:页面「现在怎样」写出取不到的原因", "以来的长日线取不到" in page, "")
        prompt = (runs[0] / "ai" / "prompt.md").read_text(encoding="utf-8")
        ok("forecast:长日线那一项不给 AI(只管系统算法的信号检验)", "history.long" not in prompt, "")
        import board_settle
        items = raw.get("items") or {}
        keys = {a: board_settle.used_keys({"author": a, "code": "300308"}, items) for a in ("system", "claude")}
        ok("错题原因:长日线取不到算系统算法的输入缺口,不算 AI 的", "history.long" in keys["system"]
           and "history.long" not in keys["claude"], keys)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── check:停牌、结算不了 ────────────────────────────────────────────────────
BAD_Q = {"id": "chat/2026-10-30/codex/300308/xyz/r1", "run": "chat/2026-10-30", "author": "codex", "code": "300308",
         "type": "xyz", "opened": "2026-10-30", "p0_hfq": 100.0, "params": {"ratio": 1.0}, "forecast": {"pct": 30},
         "source": "chat", "note": "合成:题型写错的题"}


def check_daily_unchecked():
    """每日检查:近 20 天没有日线的那只、结算不了的题都进 status.json 的 unchecked(页面顶栏列出来)。"""
    tmp = cli._tmp()
    try:
        lab, run = _lab_with_run(tmp)
        (lab.root / "chat_questions.jsonl").write_text(json.dumps(BAD_Q, ensure_ascii=False) + "\n", encoding="utf-8")
        fp = cli.fake_prices()
        fp["close"]["300502"] = []
        lab.fakes(prices_obj=fp)
        rc, out, err = lab.run("check", "--no-notify")
        un = lab.status().get("unchecked") or []
        ok("check:近 20 天没有日线 → 写进 unchecked(卖出线、改主意条件没查)",
           any(u.get("code") == "300502" and "没有日线" in u.get("what", "") and "卖出线" in u.get("what", "")
               for u in un), un)
        ok("check:结算不了的题 → 写进 unchecked(代码、原因里有题号)",
           any(u.get("code") == "300308" and "结算不了" in u.get("what", "") and BAD_Q["id"] in str(u.get("detail"))
               for u in un), un)
        ok("check:有没查成的 → 退出码 1", rc == 1, (rc, err[-300:]))
        ban = _banner(_page(lab))
        ok("check:页面顶栏列出这两条", "300502 近 20 天没有日线" in ban and "结算不了" in ban,
           ban[:600])
        ok("check:停牌那只顶栏写「停牌?」,不写「没查成」", "300502 近 20 天没有日线(停牌?)" in ban
           and "没查成:300502" not in ban, ban[:600])
        import board_settle
        msg = board_settle.note_text([], [], [u for u in un if u.get("code") == "300502"])
        ok("check:停牌那只的通知写「停牌?」、卖出线没查,不写「没查成」", "300502 近 20 天没有日线(停牌?)" in msg
           and "卖出线没查" in msg and "没查成" not in msg, msg)
        seen = json.loads((lab.root / "actions_seen.json").read_text(encoding="utf-8")) \
            if (lab.root / "actions_seen.json").is_file() else {}
        ok("check:画完页面把每只的动作和从哪天起记进 actions_seen.json(这次存档的)",
           seen.get("run") == f"{run.parent.name}/{run.name}" and set(seen.get("stocks") or {}) == {"300308", "300502"}
           and all(v.get("kind") and v.get("since") for v in seen["stocks"].values()), seen)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_daily_bad_questions_file():
    """题目文件中间有一行坏了:到期题那段没查成,写成「题目文件读不出来」,不写成「交易日历取不到」。"""
    tmp = cli._tmp()
    try:
        lab, _ = _lab_with_run(tmp)
        good = json.dumps(BAD_Q, ensure_ascii=False)
        (lab.root / "chat_questions.jsonl").write_text(good + "\n{坏的一行\n" + good + "\n", encoding="utf-8")
        lab.fakes(prices_obj=cli.fake_prices())
        rc, out, err = lab.run("check", "--no-notify")
        un = [u for u in lab.status().get("unchecked") or [] if not u.get("code")]
        ok("check:题目文件坏了 → 到期题没查成,写明是题目文件读不出来,原因里有第几行",
           len(un) == 1 and "题目文件读不出来" in un[0].get("what", "") and "第 2 行" in str(un[0].get("detail")),
           un)
        ok("check:不写成交易日历取不到", all("交易日历" not in u.get("what", "") for u in un), un)
        ok("check:别的照常查(状态文件照写,退出码 1)", rc == 1 and lab.status().get("checked_at"), (rc, err[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── status.json 新不新、读不读得出来 ────────────────────────────────────────
OLD_STATUS = {"checked_at": "2026-10-29T15:30:00+08:00", "due": [], "unchecked": [],      # 存档是 10-30 15:40 建的
              "triggered": [{"code": "300308", "what": "Claude 的改主意条件:合成:收盘跌破 297", "detail": "x",
                             "kind": "change_if"},
                            {"code": "300308", "what": "codex 的改主意条件:合成:收盘站上 400", "detail": "老格式没有 kind"},
                            {"code": "300308", "what": "收盘跌破卖出线 320", "detail": "y", "kind": "sell_line"}]}


def check_status_freshness():
    """每日检查做在这次存档以前:旧答卷的「改主意条件已触发」不再顶在新预测上,顶栏写明检查早于这次预测;
    status.json 读不出来:顶栏写出来,不当成「什么都没触发」。"""
    import render
    tmp = Path(cli._tmp())
    try:
        run, cfg = pages.make_lab(tmp / "old", status=OLD_STATUS)
        d = render.build_board_data(run, cfg, pages.TODAY, state=pages.SYN_STATE)
        html = render.render_board(d)
        whats = [t.get("what") for t in (d.get("status") or {}).get("triggered") or []]
        ok("检查早于这次存档:改主意条件那两条(含老格式)去掉,卖出线那条留着", whats == ["收盘跌破卖出线 320"], whats)
        ok("页面上不再有旧答卷的「已触发」(顶栏、要马上注意的都没有)", "的改主意条件:" not in html
           and "收盘站上 400" not in html, "")
        ok("顶栏写每日检查是哪天、早于这次预测", "每日检查于 2026-10-29 15:30(早于这次预测" in html, "")
        run2, cfg2 = pages.make_lab(tmp / "bad")
        (run2.parent.parent / "status.json").write_text("{坏的", encoding="utf-8")
        h2 = render.render_board(render.build_board_data(run2, cfg2, pages.TODAY, state=pages.SYN_STATE))
        ok("status.json 读不出来:顶栏横幅写出来", "status.json 读不出来" in _banner(h2), _banner(h2)[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── inputs --check、改主意条件、通知 ────────────────────────────────────────
def check_inputs_check_stale():
    """inputs --check:必需的日线用了旧值(forecast 会因此不出预测)→ 不说「必需项都取到了」,退出码 1。"""
    import contextlib
    import io
    import board_forecast
    items = cli.fake_items()
    items["prices.300308"] = {**items["prices.300308"], "status": "stale", "note": "合成:这次取数失败,沿用旧值"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = board_forecast.print_inputs(items, [], ["300502", "300308"])
        except TypeError:           # 老版本不收代码
            rc = board_forecast.print_inputs(items, [])
    out = buf.getvalue()
    ok("日线用了旧值 → 退出码 1,不说「必需项都取到了」,写明 forecast 会拒绝", rc == 1 and "必需项都取到了" not in out
       and "prices.300308" in out and "forecast 会拒绝" in out, (rc, out[-300:]))


def check_change_if_unfetched():
    """一只的收盘没取到:它的改主意条件数成「没取到收盘、没查」,不混进「说的不是本股收盘价位」。"""
    tmp = cli._tmp()
    try:
        lab, _ = _lab_with_run(tmp)
        fp = cli.fake_prices()
        fp["close"]["300308"] = "坏的"
        lab.fakes(prices_obj=fp)
        rc, out, err = lab.run("check", "--no-notify")
        ok("check:没取到收盘的那只,改主意条件单独数「没取到收盘,没查」", "2 条改主意条件的股票这次没取到收盘" in out
           and "说的不是本股收盘价位" not in out, out[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_notify_failed():
    """系统通知没弹出来(osascript 退出码不是 0):stderr 写原因,status.json 记 notify_failed,退出码 1。
    用临时目录里的假 osascript(PATH 放最前面),不会真弹通知。"""
    import os
    tmp = cli._tmp()
    try:
        lab, _ = _lab_with_run(tmp)
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "osascript").write_text("#!/bin/sh\necho '合成:不允许发通知' >&2\nexit 1\n", encoding="utf-8")
        (fake / "osascript").chmod(0o755)
        path = f"{fake}{os.pathsep}{lab.env.get('PATH', '')}"
        if cli.shutil.which("osascript", path=path) != str(fake / "osascript"):
            ok("假 osascript 排在 PATH 最前面(不然会真弹通知,不跑)", False, cli.shutil.which("osascript", path=path))
            return
        lab.env["PATH"] = path
        lab.fakes(prices_obj={"index": [], "hfq": {}, "close": {"300308": [["2026-11-02", 300.0]],
                                                               "300502": [["2026-11-02", 150.0]]}})
        rc, out, err = lab.run("check", "--notify")       # 300308 收盘 300 跌破卖出线 320,会发通知
        st = lab.status()
        ok("通知弹不出来:stderr 写原因,status.json 记 notify_failed,退出码 1", rc == 1 and "通知没弹出来" in err
           and "不允许发通知" in str(st.get("notify_failed")), (rc, st.get("notify_failed"), err[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 打开页面时再判一次过期 ──────────────────────────────────────────────────
# 页面里的小脚本在 node 里跑:给一个只够它用的假 document(顶栏、横幅、列表),Date.now 换成「打开那天」。
_DOM = r"""
const [data, whys, nowMs, script] = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function E(tag) { return {tag, kids: [], attrs: {}, textContent: '', className: '', id: '', parentNode: null,
  classList: {list: [], add(c) { this.list.push(c); }},
  setAttribute(k, v) { this.attrs[k] = v; },
  appendChild(c) { this.kids.push(c); c.parentNode = this; return c; },
  insertBefore(c) { return this.appendChild(c); },
  all() { const out = []; (function walk(n) { for (const k of n.kids) { out.push(k); walk(k); } })(this); return out; },
  querySelector(sel) {
    const m = sel.match(/^\[data-why="(\w+)"\]$/);
    return this.all().find(n => m ? n.attrs['data-why'] === m[1] : n.tag === sel) || null; }}; }
const body = E('body'), top = E('header');
body.appendChild(top);
if (whys) {
  const box = body.appendChild(E('div')); box.id = 'stale-banner';
  const ul = box.appendChild(E('ul'));
  for (const w of whys) { const li = ul.appendChild(E('li')); li.setAttribute('data-why', w); li.textContent = '生成时写的'; }
}
global.document = {createElement: E, querySelector: () => top,
  getElementById: id => id === 'board-data' ? {textContent: JSON.stringify(data)}
                                            : body.all().find(n => n.id === id) || null};
Date.now = () => nowMs;
eval(script);
process.stdout.write(JSON.stringify({alert: top.classList.list.includes('alert'),
  lines: body.all().filter(n => n.attrs['data-why']).map(n => [n.attrs['data-why'], n.textContent])}));
"""


def _open_page(data: dict, whys, opened: str) -> dict:
    """在 node 里跑页面的过期小脚本:data = 内联数据,whys = 生成时横幅里已有的 data-why(None = 没有横幅),
    opened = 打开页面那天(北京时间中午)。→ {"alert", "lines": [[data-why, 文字]]}。"""
    import render
    from datetime import datetime, timezone
    now = datetime.fromisoformat(f"{opened}T04:00:00").replace(tzinfo=timezone.utc).timestamp() * 1000
    p = cli.subprocess.run(["node", "-e", _DOM], input=json.dumps([data, whys, now, render.STALE_SCRIPT]),
                           capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout) if p.returncode == 0 else {"error": p.stderr[-300:]}


def check_open_time_holdings():
    """手机版离线看:生成那天持仓还没过期,隔几天打开要变成「持仓数据过期」;生成时已经写了的不重复加。"""
    data = {"as_of": "2026-10-30", "account": {"asof": "2026-10-28"}}
    fresh = _open_page(data, None, "2026-11-02")
    ok("打开那天持仓才 5 天:不加横幅", fresh.get("lines") == [] and not fresh.get("alert"), fresh)
    late = _open_page(data, None, "2026-11-07")
    hold = [t for w, t in late.get("lines") or [] if w == "holdings"]
    ok("打开那天持仓 10 天(超过 7 天):横幅加一条持仓过期,写截至哪天、过了几天", late.get("alert")
       and len(hold) == 1 and "2026-10-28" in hold[0] and "10 天" in hold[0] and "旧持仓" in hold[0], late)
    ok("这时判断才 8 天:不加「判断过期」那条", [w for w, _ in late.get("lines") or []] == ["holdings"], late)
    both = _open_page(data, None, "2026-12-15")
    ok("两样都过期:两条都加", sorted(w for w, _ in both.get("lines") or []) == ["days", "holdings"], both)
    again = _open_page(data, ["holdings"], "2026-11-07")
    ok("生成时已经写了持仓那条:不重复加", [w for w, _ in again.get("lines") or []] == ["holdings"], again)
    import render
    page = render.render_board({**pages.load_fixture(), "account": {**pages.load_fixture()["account"], "stale": True}})
    ok("生成时持仓已过期:那条带 data-why=holdings(脚本认得出)", 'data-why="holdings"' in page, "")


CHECKS = [check_update_notices, check_forecast_history_failed, check_daily_unchecked, check_daily_bad_questions_file,
          check_status_freshness, check_inputs_check_stale, check_change_if_unfetched, check_notify_failed,
          check_open_time_holdings]


def main():
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
