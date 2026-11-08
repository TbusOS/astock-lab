#!/usr/bin/env python3
"""check_board_account —— 预测看板「账户与动作」(account.py、actions.py、inputs 的 account 取数器)的检查。

用法:
    python3 scripts/check_board_account.py        # 退出码 0 才算过

不联网、不碰真实数据:借款看板的两个文件用 scripts/fixtures/board/broker.js、debt.js(合成数字),
其余情形在临时目录里现写。要本机有 node(读 .js 文件靠它)。
写法和 check_board_engine.py 一样:每块一个函数,登记进 CHECKS。
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FB_SCRIPTS = ROOT / "skills" / "forecast-board" / "scripts"
FIX = ROOT / "scripts" / "fixtures" / "board"
sys.path.insert(0, str(FB_SCRIPTS))

import fb_common  # noqa: E402,F401  先于 inputs / prices,和正式运行的顺序一样
import account  # noqa: E402
import actions  # noqa: E402
import inputs  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:400]))
    if not cond:
        FAILS += 1


def raises(fn, exc=Exception):
    """fn() 抛出 exc 就返回异常文字,没抛返回 None。"""
    try:
        fn()
    except exc as e:
        return str(e)
    return None


@contextmanager
def dashboard(broker: str | None = "fixture", debt: str | None = "fixture"):
    """临时的借款看板目录。"fixture" = 拷合成文件,None = 不放这个文件,其他字符串 = 文件内容。"""
    with tempfile.TemporaryDirectory() as d:
        for name, src, content in (("券商数据.js", "broker.js", broker), ("债务数据.js", "debt.js", debt)):
            if content == "fixture":
                shutil.copy(FIX / src, Path(d) / name)
            elif content is not None:
                (Path(d) / name).write_text(content, encoding="utf-8")
        yield {"dashboard_dir": d, "monthly_cash_need_fallback": 9999}


# ── 读 .js 文件 ─────────────────────────────────────────────────────────────
def check_read_js_seed():
    print("\n[read_js_seed / latest_snapshot]")
    b = account.read_js_seed(FIX / "broker.js", "BROKER_SEED")
    ok("读出券商数据(键不带引号、有尾逗号也行)", b.get("version") == 3 and len(b.get("snapshots", [])) == 2, b)
    d = account.read_js_seed(FIX / "debt.js", "DEBT_SEED")
    ok("读出债务数据,四笔", len(d.get("items", [])) == 4, d)

    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        bad = t / "坏的.js"
        bad.write_text("window.BROKER_SEED = {snapshots: [\n", encoding="utf-8")
        e = raises(lambda: account.read_js_seed(bad, "BROKER_SEED"), account.SeedError)
        ok("语法错 → SeedError,写出文件路径和错误", e is not None and str(bad) in e and "SyntaxError" in e, e)
        e = raises(lambda: account.read_js_seed(FIX / "debt.js", "BROKER_SEED"), account.SeedError)
        ok("文件里没有这个变量 → SeedError,写出变量名和路径",
           e is not None and "BROKER_SEED" in e and "debt.js" in e, e)
        e = raises(lambda: account.read_js_seed(t / "没有.js", "X"), account.SeedError)
        ok("文件不存在 → SeedError,写出路径", e is not None and "没有.js" in e, e)
        e = raises(lambda: account.read_js_seed(FIX / "debt.js", "DEBT_SEED", node=str(t / "不存在的node")),
                   account.SeedError)
        ok("node 起不来 → SeedError,写出路径", e is not None and "debt.js" in e, e)
        loop = t / "死循环.js"
        loop.write_text("window.X = (function(){ while(true){} })();\n", encoding="utf-8")
        e = raises(lambda: account.read_js_seed(loop, "X", timeout=1), account.SeedError)
        ok("跑不完 → 超时 SeedError,写出路径", e is not None and "死循环.js" in e, e)
        sneaky = t / "要文件.js"
        sneaky.write_text("window.X = {v: require('fs').readdirSync('.')};\n", encoding="utf-8")
        e = raises(lambda: account.read_js_seed(sneaky, "X"), account.SeedError)
        ok("文件里不能直接用 require(防误用,不是安全隔离)", e is not None and "require" in e, e)
        dash = t / "-p.js"
        dash.write_text("window.X = {a: 1};\n", encoding="utf-8")
        cwd = os.getcwd()
        try:
            os.chdir(t)
            got = account.read_js_seed(Path("-p.js"), "X")
        except account.SeedError as err:
            got = str(err)
        finally:
            os.chdir(cwd)
        ok("文件名以 - 开头也当文件,不当 node 的选项(参数前有 --)", got == {"a": 1}, got)
        num = t / "数.js"
        num.write_text("window.X = 3;\n", encoding="utf-8")
        e = raises(lambda: account.read_js_seed(num, "X"), account.SeedError)
        ok("变量不是对象 → SeedError", e is not None and "数.js" in e, e)

    snap = account.latest_snapshot(b)
    ok("最新一笔按日期取,不按位置(新的一笔写在前面)", snap["date"] == "2026-10-28", snap)
    ok("没有 snapshots → ValueError", raises(lambda: account.latest_snapshot({"snapshots": []}), ValueError) is not None)


# ── 每月要还的钱 ────────────────────────────────────────────────────────────
def _inst(**kw):
    return {"name": "分期", "category": "installment", "periods": 3, "monthly": 1000.0,
            "startDate": "2026-11-15", "autoPaid": True, "paidPeriods": 0, "cleared": False, **kw}


def _sched(items, start=date(2026, 11, 1), months=4):
    return account.monthly_schedule({"items": items}, start, months)


def _try_sched(items):
    try:
        return _sched(items)
    except ValueError as e:
        return f"ValueError: {e}"


def check_monthly_schedule():
    print("\n[monthly_schedule]")
    s = account.monthly_schedule(account.read_js_seed(FIX / "debt.js", "DEBT_SEED"), date(2026, 11, 1), 4)
    ok("合成月供", s == [("2026-11", 1300.0), ("2026-12", 1300.0), ("2027-01", 6300.0), ("2027-02", 0.0)], s)
    s = account.monthly_schedule(account.read_js_seed(FIX / "debt.js", "DEBT_SEED"), date(2026, 11, 16), 2)
    ok("本月只算起点以后的(11-15 那笔已经过了)", s == [("2026-11", 0.0), ("2026-12", 1300.0)], s)
    ok("起点当天要付的算进去", _sched([_inst()], start=date(2026, 11, 15), months=1) == [("2026-11", 1000.0)])
    gone = [{"name": "合成:已还清", "category": "installment", "cleared": True},          # 缺 periods 等字段
            {"name": "合成:已还清的整笔", "category": "lump", "cleared": True}]              # 缺 principal、dueDate
    s = _try_sched(gone + [_inst()])
    ok("还清的借款缺字段不报错(总看板先跳过它),其余照算", s == [("2026-11", 1000.0), ("2026-12", 1000.0),
                                                       ("2027-01", 1000.0), ("2027-02", 0.0)], s)

    ok("installment:从首次还款日起每月一笔,还 periods 期",
       _sched([_inst()]) == [("2026-11", 1000.0), ("2026-12", 1000.0), ("2027-01", 1000.0), ("2027-02", 0.0)])
    ok("installment:还款日按首次还款日那天,月底不够就取月底(1-31 起 → 2 月 28、3 月 31)",
       _sched([_inst(startDate="2026-01-31", periods=12, monthly=100.0)], start=date(2026, 2, 1), months=2)
       == [("2026-02", 100.0), ("2026-03", 100.0)])
    ok("installment:cleared 的不算", _sched([_inst(cleared=True)]) == [(m, 0.0) for m in
                                                                       ("2026-11", "2026-12", "2027-01", "2027-02")])
    ok("installment:不自动扣款、paidPeriods 已满 → 当还清(和总看板一样)",
       sum(a for _, a in _sched([_inst(autoPaid=False, paidPeriods=3)])) == 0.0)
    ok("installment:自动扣款、最后一期就在起点当天 → 总看板当已还清,不算",
       _sched([_inst(periods=1, startDate="2026-11-01")], months=1) == [("2026-11", 0.0)])
    ok("installment:同一天但不是最后一期 → 照算",
       _sched([_inst(periods=2, startDate="2026-11-01")], months=2) == [("2026-11", 1000.0), ("2026-12", 1000.0)])

    it = {"name": "先息后本", "category": "interest", "principal": 5000, "monthInterest": 200.0,
          "interestDay": 20, "startDate": "2026-08-20", "dueDate": "2027-01-15", "cleared": False}
    ok("interest:还息日在到期日之后的那个月只还本金(和总看板一样)",
       _sched([it]) == [("2026-11", 200.0), ("2026-12", 200.0), ("2027-01", 5000.0), ("2027-02", 0.0)], _sched([it]))
    ok("interest:首次还息日在起点以后,从首次还息日起",
       _sched([{**it, "startDate": "2026-12-20"}])[:2] == [("2026-11", 0.0), ("2026-12", 200.0)])
    ok("interest:cleared 的不算", sum(a for _, a in _sched([{**it, "cleared": True}])) == 0.0)

    lump = {"name": "一次性", "category": "lump", "principal": 800, "dueDate": "2026-12-09", "cleared": False}
    ok("lump:应还日那个月还本金", _sched([lump]) == [("2026-11", 0.0), ("2026-12", 800.0), ("2027-01", 0.0),
                                                      ("2027-02", 0.0)])
    ok("lump:过了应还日还没还 → 算在起点那个月(和总看板一样)",
       _sched([{**lump, "dueDate": "2026-10-01"}])[0] == ("2026-11", 800.0))
    ok("lump:cleared 的不算", sum(a for _, a in _sched([{**lump, "cleared": True}])) == 0.0)
    ok("pending:待到账的不用还", sum(a for _, a in _sched([{"name": "额度", "category": "pending",
                                                           "principal": 3000}])) == 0.0)

    e = raises(lambda: _sched([{"name": "样例卡", "category": "credit_card"}]), ValueError)
    ok("没写规则的类别 → ValueError,写出类别名", e is not None and "credit_card" in e, e)
    e = raises(lambda: _sched([{"name": "样例卡", "category": "credit_card", "cleared": True}]), ValueError)
    ok("没写规则的类别,标了 cleared 也报(文件格式变了要让人知道)", e is not None and "credit_card" in e, e)
    e = raises(lambda: _sched([{"name": "缺字段", "category": "installment", "periods": 3, "monthly": 1000}]),
               ValueError)
    ok("缺字段 → ValueError,写出哪一笔、缺什么", e is not None and "缺字段" in e and "startDate" in e, e)
    ok("months=0 → 空列表", _sched([_inst()], months=0) == [])
    ok("没有 items → ValueError", raises(lambda: account.monthly_schedule({}, date(2026, 11, 1), 2),
                                        ValueError) is not None)


def check_free_cash():
    print("\n[free_cash / month_need_of]")
    sch = [(f"m{i}", 1000.0) for i in range(8)]
    ok("闲钱 = 可取现金 − 往后 6 个月要还的", account.free_cash(5000.0, sch) == -1000.0)
    ok("months 可调", account.free_cash(5000.0, sch, months=3) == 2000.0)
    ok("这个月还有要付的 → 取这个月", account.month_need_of([("2026-11", 500.0), ("2026-12", 9.0)])
       == ("2026-11", 500.0))
    ok("这个月已经付完 → 看下个月(和总看板「要动多少」一样)",
       account.month_need_of([("2026-11", 0.0), ("2026-12", 1300.0)]) == ("2026-12", 1300.0))
    ok("空 → (None, None)", account.month_need_of([]) == (None, None))


# ── account_state ───────────────────────────────────────────────────────────
def check_account_state():
    print("\n[account_state]")
    with dashboard() as cfg:
        st = account.account_state(cfg, date(2026, 11, 2))
        ok("持股、成本和截图价(截图价给仓位上限重算净资产用)",
           st["holdings"] == {"300308": {"shares": 300, "cost": 400.0, "price": 300.0},
                              "300502": {"shares": 400, "cost": 250.0, "price": 150.0}}, st["holdings"])
        ok("截至最新一笔", st["asof"] == "2026-10-28" and st["stale"] is False and st["error"] is None, st)
        ok("现金用可取现金(和总看板「要动多少」一样),融资负债照抄",
           st["cash"] == 1800.0 and st["margin_debt"] == 20000.0, st)
        ok("schedule 从今天这个月起排 12 个月", len(st["schedule"]) == 12 and st["schedule"][0] == ("2026-11", 1300.0)
           and st["schedule"][2] == ("2027-01", 6300.0), st["schedule"])
        ok("这个月要还的钱", st["month_need"] == 1300.0 and st["need_month"] == "2026-11", st)
        ok("闲钱 = 可取现金 − 往后 6 个月(1800 − 8900)", st["free_cash"] == -7100.0
           and st["free_cash"] == account.free_cash(st["cash"], st["schedule"]), st["free_cash"])
        ok("闲钱算到哪个月为止(含这个月,往后共 6 个月:2026-11~2027-04)", st["free_cash_until"] == "2027-04",
           st.get("free_cash_until"))
        ok("刚好 7 天不算过期", account.account_state(cfg, date(2026, 11, 4))["stale"] is False)
        old = account.account_state(cfg, date(2026, 11, 5))
        ok("超过 7 天算过期", old["stale"] is True and old["error"] is None, old)
        late = account.account_state(cfg, date(2026, 11, 16))
        ok("这个月付完了,要还的钱看下个月", late["need_month"] == "2026-12" and late["month_need"] == 1300.0, late)
        ok("todo 空、银行卡没钱 → 没有备注", st["notes"] == [], st["notes"])

    todo_debt = (FIX / "debt.js").read_text(encoding="utf-8").replace(
        "cashInBank: 0,", "cashInBank: 500,").replace("todo: [],", "todo: [\"样例:另一笔还没录\"],")
    with dashboard(debt=todo_debt) as cfg:
        st = account.account_state(cfg, date(2026, 11, 2))
        ok("有没录进来的借款 → 备注写出来(要还的钱会少算)",
           any("样例:另一笔还没录" in n and "少算" in n for n in st["notes"]) and st["stale"] is False, st["notes"])
        ok("银行卡有现金 → 备注写出来,不并进可取现金",
           any("500.00" in n for n in st["notes"]) and st["cash"] == 1800.0, st["notes"])

    bad_debt = "window.DEBT_SEED = {items: [{name:\"样例卡\", category:\"credit_card\", principal:1}]};\n"
    with dashboard(debt=bad_debt) as cfg:
        st = account.account_state(cfg, date(2026, 11, 2))
        ok("借款数据有没写规则的类别 → 过期,error 写出类别名", st["stale"] is True and "credit_card" in (st["error"] or ""),
           st)
        ok("要还的钱改用配置里的 monthly_cash_need_fallback,并注明", st["month_need"] == 9999.0
           and any("9999" in n for n in st["notes"]), st)
        ok("持仓照样读出来(只是借款坏了)", st["holdings"]["300308"]["shares"] == 300 and st["free_cash"] is None, st)
    with dashboard(broker=None) as cfg:
        st = account.account_state(cfg, date(2026, 11, 2))
        ok("券商数据文件不在 → 过期,error 写出路径,不留旧数",
           st["stale"] is True and "券商数据.js" in (st["error"] or "") and st["holdings"] == {}
           and st["cash"] is None and st["asof"] is None, st)
    with dashboard(broker="window.BROKER_SEED = {snapshots: [{date:\"2026-10-28\",\n") as cfg:
        st = account.account_state(cfg, date(2026, 11, 2))
        ok("node 解析失败 → 过期,error 写出路径", st["stale"] is True and "券商数据.js" in (st["error"] or ""), st)
    st = account.account_state({}, date(2026, 11, 2))
    ok("配置里没有 dashboard_dir → 过期,不抛出", st["stale"] is True and "dashboard_dir" in (st["error"] or ""), st)
    with dashboard() as cfg:
        fut = account.account_state(cfg, date(2026, 10, 20))     # 最新一笔 10-28,比「今天」还晚 8 天
        ok("最新一笔的日期晚于今天(写错了)→ 算过期,error 写明,不当成最新", fut["stale"] is True
           and "晚于今天" in (fut["error"] or ""), (fut["stale"], fut["error"]))


# ── plan_actions ────────────────────────────────────────────────────────────
ST = {"stale": False, "holdings": {"300308": {"shares": 400}, "300502": {"shares": 900}},
      "cash": 1500.0, "month_need": 30000, "schedule": [], "asof": "2026-10-28"}
RULES = {"sell_lines": {"300308": 300, "300502": 100}, "sell_ratio": {"300308": 3, "300502": 1}}
P = {"300308": 320.0, "300502": 150.0}


def check_plan_rules():
    print("\n[plan_actions:规则]")
    a = actions.plan_actions(ST, P, RULES, None, "2026-11-02")
    ok("按 3:1 分摊后取整到 100 股", a["300308"]["shares"] == 100 and a["300502"]["shares"] == 100, a)
    ok("每月卖出记成减仓,截止 today 之后第 2 个交易日",
       a["300308"]["kind"] == "trim" and a["300308"]["deadline"] == "2026-11-04", a["300308"])
    ok("账户:要还、卖出合计、闲钱", a["account"]["month_need"] == 30000 and a["account"]["sell_total"] == 47000.0
       and a["account"]["free_cash"] == 1500.0, a["account"])
    st2 = {**ST, "cash": 0.0, "month_need": 60000}
    low = {**RULES, "sell_lines": {"300308": 100, "300502": 50}}     # 下面几道价格低,卖出线跟着放低,只看分摊
    a2 = actions.plan_actions(st2, {"300308": 200.0, "300502": 100.0}, low, None, "2026-11-02")
    ok("3:1 真的按比例(1:1 会是 200 / 300)", (a2["300308"]["shares"], a2["300502"]["shares"]) == (300, 200), a2)
    a3 = actions.plan_actions({**st2, "month_need": 60100}, {"300308": 200.0, "300502": 100.0}, low, None,
                              "2026-11-02")
    ok("向上取到 100 股整数倍(225.4 股 → 300、150.3 股 → 200)",
       (a3["300308"]["shares"], a3["300502"]["shares"]) == (300, 200), a3)
    cap = actions.plan_actions({**st2, "holdings": {"300308": {"shares": 150}, "300502": {"shares": 900}}},
                               {"300308": 200.0, "300502": 100.0}, low, None, "2026-11-02")
    ok("不超过持股,不够的写进账户备注", cap["300308"]["shares"] == 150 and "还差" in cap["account"]["note"], cap)

    b = actions.plan_actions(ST, {"300308": 290.0, "300502": 150.0}, RULES, None, "2026-11-02")
    ok("跌破 300 全卖", b["300308"]["kind"] == "sell" and b["300308"]["shares"] == 400, b["300308"])
    ok("why 写跌破哪条线", b["300308"]["why"].startswith("跌破卖出线 300"), b["300308"]["why"])
    ok("另一只没跌破 → 不是 sell(全卖的钱够这个月还)", b["300502"]["kind"] == "hold", b["300502"])
    ok("账户备注写全卖的钱够了", "够这个月还款" in b["account"]["note"], b["account"]["note"])
    ok("没有融资负债 → 持有那只不提融资", "融资" not in b["300502"]["why"], b["300502"]["why"])
    bm = actions.plan_actions({**ST, "margin_debt": 20000.0}, {"300308": 290.0, "300502": 150.0}, RULES, None,
                              "2026-11-02")
    ok("有融资负债 → 持有那只的理由提醒全卖的钱可能先还融资",
       bm["300502"]["kind"] == "hold" and "先拿去还融资" in bm["300502"]["why"], bm["300502"]["why"])
    b2 = actions.plan_actions(ST, {"300308": 320.0, "300502": 90.0}, RULES, None, "2026-11-02")
    ok("只有跌破的那只 sell", b2["300502"]["kind"] == "sell" and b2["300502"]["shares"] == 900
       and b2["300308"]["kind"] != "sell", b2)
    small = {**ST, "holdings": {"300308": {"shares": 400}, "300502": {"shares": 100}}}
    b3 = actions.plan_actions(small, {"300308": 320.0, "300502": 90.0}, RULES, None, "2026-11-02")
    ok("全卖的钱不够 → 剩下的由没跌破的那只卖(9000 不够 28500,中间差 19500 → 100 股)",
       b3["300308"]["kind"] == "trim" and b3["300308"]["shares"] == 100, b3["300308"])

    tiny = actions.plan_actions({**ST, "cash": 1500.0, "month_need": 1500 + 0.004}, P, RULES, None, "2026-11-02")
    ok("差额不到半分钱 → 不卖(不会因为浮点误差多卖一手)",
       tiny["300308"]["kind"] == "hold" and tiny["300502"]["kind"] == "hold"
       and tiny["account"]["sell_total"] == 0.0 and "还差" not in tiny["account"]["note"], tiny)
    rich = actions.plan_actions({**ST, "cash": 40000.0}, P, RULES, None, "2026-11-02")
    ok("可取现金够这个月 → 都持有,卖出合计 0", rich["300308"]["kind"] == "hold" and rich["300502"]["kind"] == "hold"
       and rich["account"]["sell_total"] == 0.0 and "不用卖" in rich["300308"]["why"], rich)
    ok("持有不填截止日", rich["300308"]["deadline"] is None)
    m = actions.plan_actions({**ST, "margin_debt": 20000.0}, P, RULES, None, "2026-11-02")
    ok("有融资负债写进账户备注", "还有融资负债 20,000.00" in m["account"]["note"], m["account"]["note"])
    ok("没有融资负债不提", "融资负债" not in a["account"]["note"], a["account"]["note"])


# ── 现金留够几个月(cash_months)、卖出顺序(sell_order)────────────────────────
# 数字都是编的(整数好心算),和真实账户无关
SCHED_NOV = [("2026-11", 100.0), ("2026-12", 5000.0), ("2027-01", 5000.0), ("2027-02", 5000.0)]
SCHED_DEC = SCHED_NOV[1:] + [("2027-03", 5000.0)]
ST2 = {"stale": False, "holdings": {"300308": {"shares": 300}, "300502": {"shares": 400}}, "cash": 6000.0,
       "month_need": 100.0, "need_month": "2026-11", "schedule": SCHED_NOV, "asof": "2026-11-08"}
R2 = {"sell_lines": {}, "sell_ratio": {"300308": 2, "300502": 1}, "cash_months": 2, "sell_order": ["300502", "300308"]}
P2 = {"300308": 40.0, "300502": 20.0}


def check_plan_cover_and_order():
    print("\n[plan_actions:现金留够两个月、按顺序卖]")
    a = actions.plan_actions(ST2, P2, R2, None, "2026-11-09")
    ok("11 月:现金 6,000 够 11、12 两个月(5,100)→ 都持有,卖出合计 0",
       a["300308"]["kind"] == "hold" and a["300502"]["kind"] == "hold" and a["account"]["sell_total"] == 0.0, a)
    ok("账户记下留够几个月、合计多少、从哪月到哪月", a["account"].get("cover_months") == 2
       and a["account"].get("cover_need") == 5100.0 and a["account"].get("cover_from") == "2026-11"
       and a["account"].get("cover_until") == "2026-12", a["account"])
    ok("账户备注写两个月合计,不只写这个月", "2026-11~2026-12 两个月要还 5,100.00" in a["account"]["note"],
       a["account"]["note"])
    ok("没有卖出线 → 不会因为价格低就全卖", all(a[c]["kind"] != "sell" for c in P2), a)

    dec = {**ST2, "cash": 5900.0, "month_need": 5000.0, "need_month": "2026-12", "schedule": SCHED_DEC,
           "asof": "2026-12-01"}
    b = actions.plan_actions(dec, P2, R2, None, "2026-12-01")
    ok("12-01:12 月 + 1 月 10,000,现金 5,900,差 4,100 → 排第一的 300502 先卖,205 股向上取到 300 股",
       b["300502"]["kind"] == "trim" and b["300502"]["shares"] == 300, b["300502"])
    ok("300502 卖的钱够了 → 300308 这个月不卖,理由写按卖出顺序", b["300308"]["kind"] == "hold"
       and "卖出顺序" in b["300308"]["why"], b["300308"])
    ok("300502 的理由写两个月合计和卖出顺序", "两个月要还 10,000.00" in b["300502"]["why"]
       and "按卖出顺序" in b["300502"]["why"], b["300502"]["why"])
    ok("反例:按 2:1 比例会两只都卖,这里 300308 是 0", b["account"]["sell_total"] == 6000.0,
       b["account"])

    big = actions.plan_actions({**dec, "cash": 0.0}, P2, R2, None, "2026-12-01")
    ok("差 10,000 超过 300502 全部 8,000 → 300502 卖光,剩下 2,000 由 300308 出,50 股向上取到 100 股",
       big["300502"]["shares"] == 400 and big["300308"]["kind"] == "trim" and big["300308"]["shares"] == 100, big)

    gone = actions.plan_actions({**dec, "holdings": {"300308": {"shares": 300}, "300502": {"shares": 0}}},
                                P2, R2, None, "2026-12-01")
    ok("排在前面的那只已经没有 → 轮到下一只(300308 出 4,100,102.5 股向上取到 200 股)",
       gone["300308"]["kind"] == "trim" and gone["300308"]["shares"] == 200, gone["300308"])

    one = actions.plan_actions(dec, P2, {k: v for k, v in R2.items() if k != "cash_months"}, None, "2026-12-01")
    ok("不写 cash_months → 和以前一样只看这个月(5,000 < 5,900,不用卖)",
       one["300308"]["kind"] == "hold" and one["300502"]["kind"] == "hold" and one["account"].get("cover_months") == 1,
       one)
    ratio = actions.plan_actions({**dec, "cash": 0.0}, P2, {k: v for k, v in R2.items() if k != "sell_order"}, None,
                                 "2026-12-01")
    ok("不写 sell_order → 和以前一样按 2:1 比例分(两只都卖)",
       ratio["300308"]["kind"] == "trim" and ratio["300502"]["kind"] == "trim", ratio)
    nos = actions.plan_actions({**dec, "schedule": []}, P2, R2, None, "2026-12-01")
    ok("借款排期是空的 → 退回只看这个月,备注写明", nos["account"].get("cover_months") == 1
       and "只按这个月算" in nos["account"]["note"], nos["account"])
    short = actions.plan_actions({**dec, "schedule": SCHED_DEC[:1]}, P2, R2, None, "2026-12-01")
    ok("排期只排到 1 个月 → 按能排到的算,备注写明不够两个月", short["account"].get("cover_months") == 1
       and "只排到 2026-12" in short["account"]["note"], short["account"])
    ok("配置写错(cash_months 不是正整数)→ 报错,不悄悄按 1 算",
       raises(lambda: actions.plan_actions(ST2, P2, {**R2, "cash_months": 0}, None, "2026-11-09"), ValueError))
    ok("配置写错(sell_order 不是代码列表)→ 报错",
       raises(lambda: actions.plan_actions(ST2, P2, {**R2, "sell_order": "300502"}, None, "2026-11-09"), ValueError))


def check_plan_stale_and_flat():
    print("\n[plan_actions:过期、空仓]")
    c = actions.plan_actions({**ST, "stale": True}, P, RULES, None, "2026-11-02")
    ok("持仓过期先更新", c["300308"]["kind"] == "update_holdings" and c["300502"]["kind"] == "update_holdings", c)
    ok("why 写截至哪天", c["300308"]["why"] == "持仓数据过期(截至 2026-10-28),先更新再算", c["300308"]["why"])
    ok("过期不算卖多少", c["300308"]["shares"] is None and c["account"]["sell_total"] is None, c)
    old = actions.plan_actions({**ST, "asof": "2026-10-25"}, P, RULES, None, "2026-11-02")
    ok("state 没标过期,但 asof 早了 8 天 → 也先更新",
       all(old[c]["kind"] == "update_holdings" for c in P) and "截至 2026-10-25" in old["300308"]["why"], old)
    ok("asof 早 7 天 → 不算过期", actions.plan_actions({**ST, "asof": "2026-10-26"}, P, RULES, None,
                                                    "2026-11-02")["300308"]["kind"] == "trim")
    nod = actions.plan_actions({**ST, "asof": None}, P, RULES, None, "2026-11-02")
    ok("state 没标过期,但没有 asof → 也先更新", all(nod[c]["kind"] == "update_holdings" for c in P)
       and "没有日期" in nod["300308"]["why"], nod["300308"]["why"])
    ok("is_stale 只看 state 和日期", actions.is_stale({"asof": "2026-10-28"}, date(2026, 11, 2)) is False
       and actions.is_stale({"asof": "坏的"}, date(2026, 11, 2)) is True)
    ok("asof 晚于今天(写错了)→ 算过期,why 写明", actions.is_stale({"asof": "2026-11-10"}, date(2026, 11, 2)) is True
       and "晚于今天" in actions.plan_actions({**ST, "asof": "2026-11-10"}, P, RULES, None, "2026-11-02")["300308"]["why"])
    keep = account.STALE_DAYS
    try:
        account.STALE_DAYS = 5
        redo = actions.plan_actions({**ST, "stale": True}, P, RULES, None, "2026-11-02")["300308"]["change_if"]
    finally:
        account.STALE_DAYS = keep
    ok("「先更新持仓」的改主意条件里写的天数跟着 account.STALE_DAYS(不写死 7)",
       any("5 天以内" in c["if"] for c in redo), redo)
    e = actions.plan_actions({**ST, "stale": True, "asof": None, "error": "读不出 x/券商数据.js:SyntaxError: …"},
                             P, RULES, None, "2026-11-02")
    ok("读不出来 → why 写原因", "读不出来" in e["300308"]["why"] and "SyntaxError" in e["300308"]["why"],
       e["300308"]["why"])

    flat = {**ST, "holdings": {"300308": {"shares": 0}, "300502": {"shares": 0}}, "cash": 0.0, "free_cash": 0.0}
    d = actions.plan_actions(flat, P, RULES, None, "2026-11-02")
    ok("空仓没闲钱写清楚", "没有闲钱" in d["300308"]["why"], d["300308"])
    ok("写到哪个月为止(含今天这个月,共 6 个月)", "到 2027-04 为止没有闲钱,买就等于动用还款的钱" in d["300308"]["why"],
       d["300308"]["why"])
    ok("反例:不再写「2027-05 以前」(那样读者会以为 5 月也算进去了)", "2027-05" not in d["300308"]["why"],
       d["300308"]["why"])
    ok("空仓没闲钱 → 等待", d["300308"]["kind"] == "wait" and d["account"]["sell_total"] == 0.0, d)

    ai = {"stocks": {"300308": {"buy": {"zones": [[220.0, 40], [250.0, 60], [200.0, 25]]},
                                "action": {"kind": "buy_batches", "why": "分三批"}},
                     "300502": {"buy": {"zones": [[120.0, 50]]}, "action": {"kind": "wait", "why": "再等等"}}}}
    f = actions.plan_actions({**flat, "free_cash": 9000.0}, P, RULES, ai, "2026-11-02")
    ok("有闲钱、Claude 给了买点 → 分批买", f["300308"]["kind"] == "buy_batches", f["300308"])
    ok("闲钱按 3:1 分,每个价位一批、价高的先买", f["300308"]["amount"] == 6750.0
       and [x["price"] for x in f["300308"]["batches"]] == [250.0, 220.0, 200.0]
       and all(x["amount"] == 2250.0 for x in f["300308"]["batches"]), f["300308"])
    ok("分批买的截止日 = 买点那 63 个交易日的最后一天(今天算第 1 个)", f["300308"]["deadline"] == "2027-01-27",
       f["300308"]["deadline"])
    import ai_answer
    ok("63 和答卷里买点的期限是同一个数", actions.BUY_DAYS == ai_answer.ZONE_DAYS, (actions.BUY_DAYS, ai_answer.ZONE_DAYS))
    ok("AI 也说分批买 → 没有冲突", f["300308"]["rule_conflict"] is None, f["300308"])
    ok("AI 说等待、规则说分批买 → 写 AI 建议,动作仍按规则", f["300502"]["kind"] == "buy_batches"
       and f["300502"]["rule_conflict"] == "AI 建议:等待 理由:再等等", f["300502"])
    g = actions.plan_actions({**flat, "free_cash": 9000.0}, P, RULES, None, "2026-11-02")
    ok("有闲钱但没有买点 → 等待,写原因", g["300308"]["kind"] == "wait" and "买入价位" in g["300308"]["why"], g)


def check_plan_ai_and_deadline():
    print("\n[plan_actions:AI 不同意、截止日]")
    ai = {"stocks": {"300308": {"action": {"kind": "hold", "shares": None, "why": "等财报"}},
                     "300502": {"action": {"kind": "trim", "shares": 200, "why": "随便"}}}}
    a = actions.plan_actions(ST, P, RULES, ai, "2026-11-02")
    ok("AI 不同意 → 动作照规则", a["300308"]["kind"] == "trim" and a["300308"]["shares"] == 100, a["300308"])
    ok("rule_conflict 写 AI 建议和理由", a["300308"]["rule_conflict"] == "AI 建议:持有 理由:等财报",
       a["300308"]["rule_conflict"])
    ok("动作种类一样(股数不同)不算冲突", a["300502"]["rule_conflict"] is None, a["300502"])
    bare = actions.plan_actions(ST, P, RULES, ai["stocks"], "2026-11-02")
    ok("答卷不带 stocks 外层也认", bare["300308"]["rule_conflict"] == "AI 建议:持有 理由:等财报")
    sell = actions.plan_actions(ST, P, RULES, {"300308": {"action": {"kind": "sell", "shares": 400, "why": "跌"}}},
                                "2026-11-02")
    ok("AI 建议带股数", sell["300308"]["rule_conflict"] == "AI 建议:卖出 400 股 理由:跌", sell["300308"])
    ok("没有答卷 → 没有冲突", actions.plan_actions(ST, P, RULES, None, "2026-11-02")["300308"]["rule_conflict"] is None)
    # 持仓过期时动作是「先更新持仓」,那不是规则,AI 说持有 / 减仓谈不上不同意规则
    st_old = actions.plan_actions({**ST, "stale": True}, P, RULES, ai, "2026-11-02")
    ok("持仓过期 → 先更新,不拿 AI 的动作去比(没有 rule_conflict)",
       all(st_old[c]["kind"] == "update_holdings" and st_old[c]["rule_conflict"] is None for c in P), st_old)
    aged = actions.plan_actions({**ST, "asof": "2026-10-01"}, P, RULES, ai, "2026-11-02")
    ok("asof 太旧(state 没标过期)也一样不比", all(aged[c]["rule_conflict"] is None for c in P), aged)

    fri = actions.plan_actions({**ST, "asof": "2026-11-05"}, P, RULES, None, "2026-11-06")
    ok("周五 → 截止下周二", fri["300308"]["deadline"] == "2026-11-10", fri["300308"]["deadline"])
    hol = {**RULES, "holidays_2027": ["2027-01-01"]}
    dec = {**ST, "asof": "2026-12-29"}
    ny = actions.plan_actions(dec, P, hol, None, "2026-12-30")
    ok("跳过配置里的节假日(12-31、元旦放假 → 1-4)", ny["300308"]["deadline"] == "2027-01-04", ny["300308"]["deadline"])
    ok("没配节假日就不跳", actions.plan_actions(dec, P, RULES, None, "2026-12-30")["300308"]["deadline"] == "2027-01-01")
    ok("config_holidays 收 holidays 和 holidays_<年>", actions.config_holidays(
        {"holidays": ["2026-10-01"], "holidays_2027": ["2027-01-01"], "holidays_note": "x"})
       == {"2026-10-01", "2027-01-01"})


def _plan_seen(state, today, start, seen=None, px=None):
    """截止日从起点日和「这种动作第一次出现那天」数;老版本不收 seen / start,就按老的调,看它算出什么。"""
    px = px or P
    try:
        return actions.plan_actions(state, px, RULES, None, today, start=start, seen=seen)
    except TypeError:
        try:
            return actions.plan_actions(state, px, RULES, None, today, start=start)
        except TypeError:
            return actions.plan_actions(state, px, RULES, None, today)


def _carry(plan) -> dict:
    """这次画页面记下的(和 board_io.record_actions_seen 写的一样):{代码: {"kind", "since"}}。"""
    return {c: {"kind": plan[c]["kind"], "since": plan[c].get("since")} for c in P}


def _days_plan(start, steps):
    """steps = [(今天, state 改动, 价格改动)]:一天一天画,每天把上次记的传回去。返回每天的 plan。"""
    out, seen = [], None
    for today, upd, px in steps:
        plan = _plan_seen({**ST, "asof": today, **upd}, today, start, seen, {**P, **px})
        out.append(plan)
        seen = _carry(plan)
    return out


def check_deadline_anchor():
    """「最晚哪天做」= 起点日和这种动作第一次出现那天里较晚那天之后的第 2 个交易日。
    一直挂着的动作截止日不动、过了算没做;存档几天以后才出现的卖出、减仓有自己的两天。"""
    print("\n[plan_actions:截止日从起点日、第一次出现那天里较晚的那天数]")
    start, rich = "2026-11-02", {"cash": 40000.0}           # 现金够还这个月 → 持有
    d = _days_plan(start, [("2026-11-02", {}, {}), ("2026-11-03", {}, {}), ("2026-11-09", {}, {})])
    ok("起点那天就有的减仓:隔一天、隔一周重画,截止日都是 11-04",
       [x["300308"]["deadline"] for x in d] == ["2026-11-04"] * 3, [x["300308"]["deadline"] for x in d])
    ok("还没到截止日 overdue 是 False;过了还挂着 overdue 是 True",
       d[1]["300308"].get("overdue") is False and d[2]["300308"].get("overdue") is True,
       [x["300308"].get("overdue") for x in d])
    b = _days_plan(start, [("2026-11-02", {}, {}), ("2026-11-10", {}, {"300308": 290.0}),
                           ("2026-11-13", {}, {"300308": 290.0})])
    ok("11-10 才跌破卖出线:那天是卖出,最晚 11-12,不算过期", b[1]["300308"]["kind"] == "sell"
       and b[1]["300308"]["deadline"] == "2026-11-12" and b[1]["300308"].get("overdue") is False, b[1]["300308"])
    ok("跌破以后三个交易日还没卖:截止日还是 11-12,算过期", b[2]["300308"]["deadline"] == "2026-11-12"
       and b[2]["300308"].get("overdue") is True, b[2]["300308"])
    a = _days_plan(start, [("2026-11-02", rich, {}), ("2026-11-20", rich, {}),
                           ("2026-12-01", {"need_month": "2026-12"}, {})])
    ok("下个月的还款:11 月持有,12-01 才要减仓 → 最晚 12-03,不算过期",
       a[0]["300308"]["kind"] == "hold" and a[2]["300308"]["kind"] == "trim"
       and a[2]["300308"]["deadline"] == "2026-12-03" and a[2]["300308"].get("overdue") is False, a[2]["300308"])
    c = _days_plan(start, [("2026-11-02", rich, {}), ("2026-11-06", {}, {})])
    ok("同一个月里现金少了:11-06 才出现的减仓 → 最晚 11-10,不算过期", c[0]["300308"]["kind"] == "hold"
       and c[1]["300308"]["deadline"] == "2026-11-10" and c[1]["300308"].get("overdue") is False, c[1]["300308"])
    sat = _plan_seen({**ST, "asof": "2026-11-07"}, "2026-11-07", start)
    ok("第一次出现那天是周六:从下一个交易日(11-09)起算,最晚 11-11", sat["300308"].get("since") == "2026-11-09"
       and sat["300308"]["deadline"] == "2026-11-11", sat["300308"])
    flat = {**ST, "asof": "2026-11-12", "holdings": {}, "free_cash": 9000.0}
    ai = {"stocks": {"300308": {"buy": {"zones": [[300.0, 40]]}}}}
    try:
        buy = actions.plan_actions(flat, P, RULES, ai, "2026-11-12", start=start)
    except TypeError:
        buy = actions.plan_actions(flat, P, RULES, ai, "2026-11-12")
    ok("分批买的最晚日期 = 买点窗口最后一天:起点 11-02 算第 1 个的第 63 个交易日 2027-01-27",
       buy["300308"]["deadline"] == "2027-01-27", buy["300308"]["deadline"])


# ── inputs 的 account 取数器 ────────────────────────────────────────────────
def check_inputs_account():
    print("\n[inputs:account 取数器]")
    c_account = getattr(inputs, "c_account", None)
    ok("登记进 DEFAULT_COLLECTORS", c_account is not None and inputs.DEFAULT_COLLECTORS.get("account") is c_account)
    if c_account is None:
        return
    ok("用途:只给 AI 参考", inputs.used_by_for("account") == "ai")
    with dashboard() as cfg:
        got = inputs.c_account(cfg, today=date(2026, 11, 2))
        ok("新鲜 → ok,值就是 account_state", got["status"] == "ok" and got["used_by"] == "ai"
           and got["value"]["holdings"]["300308"]["shares"] == 300 and got["as_of"] == "2026-10-28", got)
        old = inputs.c_account(cfg, today=date(2026, 11, 20))
        ok("过期 → partial,note 写原因", old["status"] == "partial" and "2026-10-28" in old["note"]
           and "7 天" in old["note"], old)
        items = inputs.collect(cfg, None, {"account": partial(inputs.c_account, today=date(2026, 11, 2))})
        ok("collect 里成一项,used_by=ai", items["account"]["status"] == "ok" and items["account"]["used_by"] == "ai",
           items["account"])
    with dashboard(broker=None) as cfg:
        bad = inputs.c_account(cfg, today=date(2026, 11, 2))
        ok("读不出来 → partial,note 写出错原因", bad["status"] == "partial" and "券商数据.js" in bad["note"], bad)


# ── 仓位上限(超限优先:不论预测多好,先减到上限)─────────────────────────
# 数字全是编的:净资产 200,000;硬上限 25%(一看就是合成的,不是账户真实设置);
# 300308 持 400 股 @320 = 128,000(64%);300502 持 900 股 @150 = 135,000(67.5%)。
LIM_ST = {**ST, "cash": 50000.0, "month_need": 1000}            # 现金够还款:这个月不用为还款卖
FIRST_SCREEN_BANNED = ("硬线", "bear 倒推", "bear", "journal")   # 第一屏不写这几个词


def _nested(s: str) -> bool:
    """括号里还有括号(要平铺)。半角、全角都算。"""
    depth = 0
    for ch in s:
        if ch in "(（":
            depth += 1
            if depth > 1:
                return True
        elif ch in ")）":
            depth = max(0, depth - 1)
    return False


def _lim(mv: float, equity: float = 200000.0, pct: float = 25.0, note: str = "", detail: str = "") -> dict:
    return {"ok": True, "limit_pct": pct, "now_pct": mv / equity * 100, "limit_amount": equity * pct / 100,
            "equity": equity, "label": f"仓位上限 {pct:g}%(账户设置,2026-01-15 定)", "note": note, "detail": detail}


LIMS = {"300308": _lim(128000), "300502": _lim(135000)}


def _plan(state, px, rules, ai, today, limits):
    try:
        return actions.plan_actions(state, px, rules, ai, today, limits=limits)
    except TypeError:                         # 老的 plan_actions 不收 limits
        return actions.plan_actions(state, px, rules, ai, today)


def check_plan_limits():
    print("\n[plan_actions:仓位上限]")
    bull = {"stocks": {c: {"action": {"kind": "hold", "shares": None, "why": "合成:看涨,100 种里涨 80 种"}} for c in P}}
    a = _plan(LIM_ST, P, RULES, bull, "2026-11-02", LIMS)
    ok("① 超限 + AI 看涨 + 现金够还款 → 照样减仓到上限:300308 卖 300 股(超出 78,000 ÷ 320 = 243.75 → 300)",
       a["300308"]["kind"] == "trim" and a["300308"]["shares"] == 300, a["300308"])
    ok("① 「为什么」写仓位超限、现在占比和上限", all(x in a["300308"]["why"] for x in ("仓位超限", "64.0%", "仓位上限 25%")),
       a["300308"]["why"])
    ok("① 300502 卖 600 股(超出 85,000 ÷ 150 = 566.7)", a["300502"]["kind"] == "trim" and a["300502"]["shares"] == 600,
       a["300502"])
    ok("① 动作里记下超限的股数、还款的股数、上限算没算出来", (a["300308"].get("over_limit") or {}).get("shares") == 300
       and a["300308"].get("repay_shares") == 0 and a["300308"].get("limit_known") is True, a["300308"])
    big = {"300308": _lim(128000, equity=480000.0), "300502": _lim(135000, equity=480000.0)}   # 26.7% / 28.1%:只超一点
    st2 = {**ST, "cash": 0.0, "month_need": 60000}
    low = {**RULES, "sell_lines": {"300308": 100, "300502": 50}}
    b = _plan(st2, P, low, None, "2026-11-02", big)
    ok("② 还款要卖的多过超限:300308 还款 45,000 → 200 股,超限 8,000 → 100 股,取大的 200",
       b["300308"]["shares"] == 200 and (b["300308"].get("over_limit") or {}).get("shares") == 100
       and b["300308"].get("repay_shares") == 200, b["300308"])
    ok("② 两样都写进「为什么」", "仓位超限" in b["300308"]["why"] and "还款" in b["300308"]["why"]
       and "取大的" in b["300308"]["why"], b["300308"]["why"])
    c = _plan({**ST, "cash": 0.0, "month_need": 8000}, P, low, None, "2026-11-02", LIMS)
    ok("③ 超限要卖的多过还款:300308 还款 6,000 → 100 股,超限 → 300 股,取大的 300",
       c["300308"]["shares"] == 300 and c["300308"].get("repay_shares") == 100, c["300308"])
    d = _plan(LIM_ST, {"300308": 290.0, "300502": 150.0}, RULES, None, "2026-11-02", LIMS)
    ok("④ 跌破卖出线 → 照旧全部卖出(卖出线优先,不按上限只卖一部分)",
       d["300308"]["kind"] == "sell" and d["300308"]["shares"] == 400, d["300308"])
    nolim = {c: {"ok": False, "why": "合成:借款看板没有净资产"} for c in P}
    e = _plan(st2, P, low, None, "2026-11-02", nolim)
    ok("⑤ 上限算不出来 → 只按还款卖(200 / 100 股),「为什么」写明这次算不出来和原因,动作记 limit_known=False",
       (e["300308"]["shares"], e["300502"]["shares"]) == (200, 100) and e["300308"].get("limit_known") is False
       and "仓位上限这次算不出来" in e["300308"]["why"] and "没有净资产" in e["300308"]["why"], e["300308"])
    f = _plan(LIM_ST, P, RULES, None, "2026-11-02", nolim)
    ok("⑤ 上限算不出来、也不用还款 → 持有,「为什么」照样写这次算不出来",
       f["300308"]["kind"] == "hold" and "仓位上限这次算不出来" in f["300308"]["why"], f["300308"])
    fine = {c: _lim(v, equity=1000000.0) for c, v in (("300308", 128000), ("300502", 135000))}
    g = _plan(st2, P, low, None, "2026-11-02", fine)
    ok("没超限 → 股数只按还款,「为什么」写没超上限", g["300308"]["shares"] == 200
       and "没超仓位上限" in g["300308"]["why"] and g["300308"].get("over_limit") is None, g["300308"])
    det = {c: _lim(v, note="只用了账户设置的 25% 上限;按最坏情景倒推的上限这次算不出来:合成原因",
                   detail="journal 最近一次的最坏情景价是 #17,2026-01-03 的 160,已经高于现价 150")
           for c, v in (("300308", 128000), ("300502", 135000))}
    h = _plan(LIM_ST, P, RULES, None, "2026-11-02", det)
    ok("折叠起来的完整理由里有 journal 那条细节,而且不套括号", "#17" in h["300308"]["why"]
       and not _nested(h["300308"]["why"]), h["300308"]["why"])


def check_position_limit():
    print("\n[position_limit:上限从哪来]")
    try:
        import position_limit as pl
    except ImportError as e:
        ok("有 position_limit 模块", False, str(e))
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "account.json"
        p.write_text(json.dumps({"_单票最大可承受亏损_pct_of_净资产": 10, "_单票硬上限_pct_of_净资产": 25,
                                 "_硬上限_改动理由": "2026-01-15 初值 25% / 50%,合成。"}, ensure_ascii=False), encoding="utf-8")
        cfg = pl.load_account_limits(p)
        ok("读 account.json 的可承受亏损、硬上限,定的日子取自它自己的改动理由",
           (cfg["tol"], cfg["hard"], cfg["set_on"], cfg["error"]) == (10, 25, "2026-01-15", None), cfg)
        p.write_text(json.dumps({"_单票硬上限_pct_of_净资产": 25}), encoding="utf-8")
        ok("没写日子 → set_on 为空(不编日子)", pl.load_account_limits(p)["set_on"] is None)
        ok("文件不在 → error", bool(pl.load_account_limits(Path(d) / "nope.json")["error"]))
    cfg = {"tol": 10, "hard": 25, "set_on": "2026-01-15", "error": None}
    h = pl.limit_for(128000.0, 200000.0, cfg, "合成:没有这次的最坏情景价")
    ok("⑥ 最坏情景缺 → 只用账户设置的 25% 上限,写明按最坏情景倒推的算不出来和原因",
       h["ok"] and h["limit_pct"] == 25 and h["limit_amount"] == 50000
       and h["label"] == "仓位上限 25%(账户设置,2026-01-15 定)"
       and h["note"] == "只用了账户设置的 25% 上限;按最坏情景倒推的上限这次算不出来:合成:没有这次的最坏情景价", h)
    # 页面上出现过的那种套两层括号的写法,检查要认得出来
    ok("括号检查认得出上一版真实页面的写法", _nested("仓位上限只用了硬线:bear 倒推的上限算不出来(看板没有这次的三情景"
                                         "(bear 价和确信度);journal 最近一次的 bear(#17,2026-01-03)是 160,没有确信度)"))
    last = pl.last_bear([{"id": 17, "logged_at": "2026-01-03T10:00", "code": "300502", "scenario_px": {"bear": 160.0}}],
                        "300502")
    real = pl.limit_for(135000.0, 200000.0, cfg, pl.bear_why(last, 150.0), detail=pl.bear_detail(last, 150.0))
    row = pl.missing_rows({"300502": real})[0]["text"]
    ok("真实那种情况(journal 里的最坏情景价已高于现价):「本次缺什么」那行平铺,不出现硬线 / bear / journal",
       not _nested(row) and not any(w in row for w in FIRST_SCREEN_BANNED) and "#17" not in row, row)
    ok("journal 那条细节留给折叠的完整理由(detail),不进「本次缺什么」", "#17" in real["detail"]
       and "已经高于现价 150" in real["detail"] and not _nested(real["detail"]) and not _nested(real["note"]), real)
    sc = {"ok": True, "情景": {"bear": {"涨跌": -50.0}}}
    conf = {"系数": 0.6, "档": "合成", "分": 3, "满分": 5}
    full = pl.limit_for(128000.0, 200000.0, cfg, "", sc=sc, conf=conf)
    ok("position_cap 算得出 → 用它的上限(10 ÷ 50 × 0.6 = 12%,比 25% 小)", full["ok"]
       and abs(full["limit_pct"] - 12) < 1e-9 and not full["note"] and "按最坏情景倒推" in full["label"], full)
    ok("账户里没写日子 → 只写「账户设置」", pl.limit_for(1.0, 10.0, {**cfg, "set_on": None}, "x")["label"]
       == "仓位上限 25%(账户设置)")
    ok("没有净资产 → 算不出来", not pl.limit_for(128000.0, None, cfg, "x")["ok"])
    r = pl.limit_for(128000.0, 200000.0, {**cfg, "hard": None}, "合成:没有最坏情景价")
    ok("没有硬上限、最坏情景也缺 → 算不出来,原因平铺写到账户设置", not r["ok"] and "单票硬上限" in r["why"]
       and not _nested(r["why"]), r)
    st = {"net_assets": 133000.0,
          "holdings": {"300308": {"shares": 300, "price": 300.0}, "300502": {"shares": 400, "price": 150.0}}}
    ok("净资产按现在的收盘重算:133,000 + 300 × (320 − 300) + 400 × (140 − 150) = 135,000",
       pl.net_assets_now(st, {"300308": 320.0, "300502": 140.0}) == (135000.0, ""))
    ok("看板没有净资产 → None", pl.net_assets_now({**st, "net_assets": None}, {"300308": 320.0})[0] is None)
    other = {**st, "holdings": {**st["holdings"], "688001": {"shares": 100, "price": 50.0}}}
    v, note = pl.net_assets_now(other, {"300308": 320.0, "300502": 140.0})
    ok("借款看板里有一只看板没有收盘价 → 只重算有价的,它按那一笔的价算,写进说明",
       v == 135000.0 and "688001" in note, (v, note))
    lims = pl.limits(other, {"300308": 320.0, "300502": 140.0}, cfg, [])
    rows = pl.missing_rows(lims)
    ok("这条说明进「本次缺什么」(账户那一行,不带股票代码的键)", any("688001" in x["text"] and x["key"] == "limit.account"
                                                     for x in rows), rows)
    rows = [{"id": 5, "logged_at": "2026-01-03T10:00", "code": "300308", "scenario_px": {"bear": 250.0}},
            {"id": 9, "logged_at": "2026-02-03T10:00", "code": "300308", "scenario_px": {"bear": None}},
            {"id": 7, "logged_at": "2026-01-20T10:00", "code": "300502", "scenario_px": {"bear": 160.0}}]
    ok("journal 里这只最近一条带最坏情景价的", pl.last_bear(rows, "300308") == {"id": 5, "date": "2026-01-03", "bear": 250.0})
    ok("最坏情景价高于现价时,细节里写出来", "已经高于现价" in pl.bear_detail(pl.last_bear(rows, "300502"), 150.0))
    with dashboard() as dcfg:
        stt = account.account_state(dcfg, date(2026, 11, 2))
        ok("account_state 带上看板的净资产和每只的截图价", stt.get("net_assets") == 133000.0
           and stt["holdings"]["300308"].get("price") == 300.0, (stt.get("net_assets"), stt["holdings"]))


CHECKS = [check_read_js_seed, check_monthly_schedule, check_free_cash, check_account_state,
          check_plan_rules, check_plan_cover_and_order, check_plan_stale_and_flat, check_plan_ai_and_deadline, check_deadline_anchor,
          check_plan_limits,
          check_position_limit, check_inputs_account]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
