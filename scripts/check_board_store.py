#!/usr/bin/env python3
"""check_board_store —— 预测看板「输入包」和「事件」两块的检查(不联网)。

用法:
    python3 scripts/check_board_store.py        # 退出码 0 才算过

查什么:
  - inputs.collect:每项都带齐来源、时间、取数结果、用途;失败有旧值沿用(stale),没有记 failed,不抛出
  - 两个解析器:券商一致预期的 markdown、云厂资本开支的 --json
  - 每个取数器拿到数以后怎么整理:用假的取数函数 / 假的 mini 代替真实网络,
    查「向谁要了什么」和「拿回来算成了什么」;值的形状要能被 system_forecast 直接读
  - missing_required:行业必需项缺了要列出来;行业判不出来也要列出来
  - events:纳斯达克财报日历换北京时间、SEC 文件里摘「年份 + 资本开支」的句子、
    SEC 扫描整段脚本送到 mini 上跑

结构:每个模块一个函数,登记进 CHECKS(和 check_board_engine.py 一样)。
两个 AI 作答(ai_answer)那块在 check_board_ai.py(这个文件超过 800 行时拆出去的)。
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FB_SCRIPTS = ROOT / "skills" / "forecast-board" / "scripts"
FIX = ROOT / "scripts" / "fixtures" / "board"
sys.path.insert(0, str(FB_SCRIPTS))

import fb_common  # noqa: E402  先于 inputs / prices,和正式运行的顺序一样
import inputs  # noqa: E402
import system_forecast as sf  # noqa: E402

events = inputs.events
FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:500]))
    if not cond:
        FAILS += 1


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    return None


@contextmanager
def patched(obj, **kw):
    """临时换掉模块或 dict 里的几项,退出时原样还原。"""
    is_dict = isinstance(obj, dict)
    old = {k: (obj.get(k, _MISSING) if is_dict else getattr(obj, k, _MISSING)) for k in kw}
    for k, v in kw.items():
        if is_dict:
            obj[k] = v
        else:
            setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                obj.pop(k, None) if is_dict else delattr(obj, k)
            elif is_dict:
                obj[k] = v
            else:
                setattr(obj, k, v)


_MISSING = object()


def _proc(stdout="", code=0, stderr=""):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _bdays(start: str, end: str, skip_years=()):
    """start~end 的周一到周五('YYYY-MM-DD'),跳过 skip_years 整年。"""
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if d.weekday() < 5 and d.year not in skip_years:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ── fb_common.ORIG_ENV ──────────────────────────────────────────────────────
def check_orig_env():
    print("\n[fb_common.ORIG_ENV]")
    env = {**os.environ, "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1"}
    code = ("import os, inputs, fb_common, prices;"
            "print(fb_common.ORIG_ENV.get('https_proxy'), os.environ.get('https_proxy'))")
    p = subprocess.run([sys.executable, "-c", code], cwd=str(FB_SCRIPTS), env=env,
                       capture_output=True, text=True, timeout=60)
    ok("import inputs 之后:ORIG_ENV 还留着代理,os.environ 里的已被 market 删掉",
       p.returncode == 0 and p.stdout.split() == ["http://127.0.0.1:1", "None"], (p.stdout, p.stderr[-300:]))


# ── inputs.collect ──────────────────────────────────────────────────────────
def check_collect():
    print("\n[inputs.collect]")

    def boom(cfg):
        raise TimeoutError("mini 不通")
    prev = {"capex": {"key": "capex", "value": {"x": 1}, "as_of": "2026-11-02", "status": "ok",
                      "source": "旧来源", "public_at": None}}
    got = inputs.collect({}, prev, {"capex": boom, "taiwan": boom})
    ok("失败有旧值 → stale 沿用旧值", got["capex"]["status"] == "stale" and got["capex"]["value"] == {"x": 1})
    ok("stale 沿用旧值的 as_of,note 写明这次为什么失败",
       got["capex"]["as_of"] == "2026-11-02" and "mini 不通" in got["capex"]["note"], got["capex"])
    ok("失败没旧值 → failed", got["taiwan"]["status"] == "failed" and got["taiwan"]["value"] is None, got["taiwan"])
    ok("prev 不被改动", prev["capex"]["status"] == "ok" and "note" not in prev["capex"], prev)
    ok("每项字段齐全(9 个键)", all(set(v) == set(inputs.ITEM_KEYS) for v in got.values()), got)

    good = inputs.collect({}, None, {"market.margin": lambda cfg: {"value": {"balance_yi": 1.0, "chg20_pct": -2.0},
                                                                   "source": "假源", "as_of": "2026-10-30"}})
    m = good["market.margin"]
    ok("取到 → status 默认 ok、key 填上、fetched_at 是北京时间", m["status"] == "ok" and m["key"] == "market.margin"
       and m["fetched_at"].endswith("+08:00") and m["note"] == "", m)
    ok("used_by 按键查表:market.margin 只给 AI", m["used_by"] == "ai", m)
    ok("used_by:prices.* 参与系统计算、sectors 只展示",
       inputs.used_by_for("prices.300308") == "system" and inputs.used_by_for("sectors") == "display"
       and inputs.used_by_for("peer.300308.300502") == "system" and inputs.used_by_for("events.sec") == "ai")
    part = inputs.collect({}, None, {"capex": lambda cfg: {"value": {}, "status": "partial", "note": "缺一家"}})
    ok("取数器自己报 partial 原样保留", part["capex"]["status"] == "partial" and part["capex"]["note"] == "缺一家")
    bad = inputs.collect({}, None, {"capex": lambda cfg: {"value": 1, "status": "好"}})
    ok("取数器给了不认识的 status → 当失败(不抛出)", bad["capex"]["status"] == "failed", bad["capex"])
    chain = inputs.collect({}, got, {"capex": boom})
    ok("上次就是 stale、这次又失败 → 继续沿用最早那份值和 as_of",
       chain["capex"]["status"] == "stale" and chain["capex"]["as_of"] == "2026-11-02", chain["capex"])
    again = inputs.collect({}, got, {"taiwan": boom})
    ok("上次 failed(没有值)→ 这次还是 failed", again["taiwan"]["status"] == "failed")
    old_part = {"capex": {"key": "capex", "value": {"x": 1}, "as_of": "2026-11-02", "status": "partial",
                          "note": "合成:缺一家云厂", "source": "旧来源"}}
    sp = inputs.collect({}, old_part, {"capex": boom})["capex"]
    ok("旧值当时只取到一部分 → 沿用时 note 照样写明不全和当时的说明", sp["status"] == "stale"
       and "只取到一部分" in sp["note"] and "合成:缺一家云厂" in sp["note"] and "mini 不通" in sp["note"], sp)
    sp2 = inputs.collect({}, {"capex": sp}, {"capex": boom})["capex"]
    ok("连着两次沿用:不全那句还在,只出现一次", sp2["note"].count("合成:缺一家云厂") == 1
       and "只取到一部分" in sp2["note"], sp2["note"])

    cfg = {"stocks": [{"code": "300308"}, {"code": "300502"}], "peers": [["300308", "300502"]]}
    ex = inputs.expand(cfg)
    want = {"prices.300308", "prices.300502", "consensus.300502", "eps_revisions.300308",
            "valuation.300502", "peer.300308.300502", "index.sz399006", "market.regime", "market.margin",
            "capex", "taiwan", "sectors", "events.calendar", "events.sec", "account"}
    ok("expand 按配置展开 <code>、<a>.<b>,共 4×2 + 1 + 9 = 18 项",
       want <= set(ex) and len(ex) == 18 and not any("<" in k for k in ex), sorted(ex))
    ok("account 取数器只给 AI 参考(细节在 check_board_account.py)",
       inputs.DEFAULT_COLLECTORS.get("account") is getattr(inputs, "c_account", 0) and inputs.used_by_for("account") == "ai")


# ── 两个解析器 ──────────────────────────────────────────────────────────────
def check_parsers():
    print("\n[parse_consensus_md / parse_capex_json]")
    c = inputs.parse_consensus_md((FIX / "consensus_300308.md").read_text(encoding="utf-8"))
    ok("一致预期 2027 均值", c["profit"]["2027"]["mean"] == 670.89, c)
    ok("一致预期 2026:机构数、最小、最大", c["profit"]["2026"] == {"n": 25, "min": 280.72, "mean": 339.68,
                                                         "max": 368.96}, c["profit"].get("2026"))
    ok("不把每股收益那张表混进来(2026 均值是 339.68 不是 29.42)", len(c["profit"]) == 3, c["profit"])
    ok("评级调低 0 条", c["ratings"]["down"] == 0)
    ok("评级:调高 0、维持 5、共 9 家", c["ratings"] == {"up": 0, "keep": 5, "down": 0, "n": 9}, c["ratings"])
    no_rate = inputs.parse_consensus_md("### 净利润（亿元）\n\n| 2026 | 3 | 1 | **2** | 3 | 1 | 9% |\n"
                                        "## 机构评级变动\n（回看 30 天没有找到 300308 的评级记录）\n")
    ok("没有评级记录 → ratings 记 None(不当成 0 家调低)", no_rate["ratings"] is None
       and no_rate["profit"]["2026"]["mean"] == 2.0, no_rate)
    fail = inputs.parse_consensus_md("（净利润（亿元） 取数失败：ConnectionError）")
    ok("净利润取数失败 → profit 是空的", fail["profit"] == {}, fail)

    obj = json.loads((FIX / "capex.json").read_text(encoding="utf-8"))
    x = inputs.parse_capex_json(obj)
    ok("合计最新一季:含租赁合计、同比、现金同比",
       x["total"][-1] == {"q": "2026-09-30", "sum": 208.08, "yoy": 94.5, "cash_yoy": 107.6}, x["total"][-1:])
    ok("季度从旧到新、8 季", [r["q"] for r in x["total"]] == sorted(r["q"] for r in x["total"])
       and len(x["total"]) == 8, [r["q"] for r in x["total"]])
    ok("缺口季的同比照 JSON 原样是 None", [r["yoy"] for r in x["total"] if r["q"] == "2026-03-31"] == [None])
    ok("指引两句,写明是哪家哪天", len(x["guidance"]) == 2 and x["guidance"][0].startswith("AMZN 2026-10-29")
       and "$220 billion" in x["guidance"][0], x["guidance"])
    ok("gaps / failed 原样带出", x["gaps"] == [{"ticker": "META", "q": "2025-03-31", "what": "lease"}]
       and x["failed"] == [], (x["gaps"], x["failed"]))
    shuffled = {**obj, "quarters": list(reversed(obj["quarters"]))}
    ok("JSON 里季度顺序乱了也按日期排", inputs.parse_capex_json(shuffled)["total"][-1]["q"] == "2026-09-30")


# ── 取数器:日线、大盘、同行 ────────────────────────────────────────────────
def check_price_collectors():
    print("\n[prices / index / market / peer]")
    calls = []

    def fake(sym, start, fq):
        calls.append((sym, fq))
        days = _bdays("2018-01-01", "2021-06-30", skip_years=(2019,))
        return [(d, 10.0 + i * (2 if fq == "hfq" else 1)) for i, d in enumerate(days)]
    got = inputs.collect({}, None, {"prices.300308": partial(inputs.c_prices, code="300308", fetch=fake)})
    p = got["prices.300308"]
    ok("缺 2019 年整年 → partial,note 写出 2019", p["status"] == "partial" and "2019" in p["note"], p["note"])
    ok("第一年、最后一年不足 200 根不算缺(2021 只有半年)", "2021" not in p["note"] and "2018" not in p["note"],
       p["note"])
    ok("向腾讯要的是 sz300308 的后复权和前复权", sorted(calls) == [("sz300308", "hfq"), ("sz300308", "qfq")], calls)
    ok("值是 {hfq, qfq},每根 [日期, 收盘],as_of 是最后一天",
       set(p["value"]) == {"hfq", "qfq"} and p["value"]["hfq"][0] == ["2018-01-01", 10.0]
       and p["as_of"] == "2021-06-30" and p["value"]["qfq"][1] == ["2018-01-02", 11.0], (p["as_of"], p["value"]["hfq"][:2]))
    full = inputs.c_prices({}, "300308", fetch=lambda s, st, fq: [(d, 1.0) for d in _bdays("2024-11-04", "2026-10-30")])
    ok("两年日线每年都够 → ok", full.get("status", "ok") == "ok", full.get("note"))
    # 数据源按参数缓存:两年那次请求的后复权停在前一天,换个起点(短窗口)就有最后一天
    days = _bdays("2024-11-04", "2026-11-02")

    def cached(tail_ok, sym, start, fq):
        rows = [(d, 100.0 + i if fq == "hfq" else 10.0 + i) for i, d in enumerate(days)]
        if fq == "hfq" and start == "2024-11-02":
            return rows[:-1]
        return [(d, c + (0 if tail_ok else 0.5)) for d, c in rows if d >= start]
    lag = inputs.c_prices({}, "300308", fetch=partial(cached, True), today=date(2026, 11, 2))
    ok("后复权少最后一天 → 换短窗口重取接上:两种复权最后一天相同、状态 ok、note 写明",
       lag["value"]["hfq"][-1] == ["2026-11-02", 100.0 + len(days) - 1] and lag["value"]["qfq"][-1][0] == "2026-11-02"
       and lag["status"] == "ok" and "换" in lag["note"], (lag["value"]["hfq"][-2:], lag["status"], lag["note"]))
    bad = inputs.c_prices({}, "300308", fetch=partial(cached, False), today=date(2026, 11, 2))
    ok("短窗口重取的数和原来那段重叠的日子对不上 → 不接,照旧 partial「日期对不上」",
       bad["value"]["hfq"][-1][0] == "2026-10-30" and bad["status"] == "partial" and "日期对不上" in bad["note"],
       (bad["value"]["hfq"][-1], bad["note"]))

    def boom(sym, start, fq):                 # 两年那次照常(后复权少最后一天),重取那次网络出错
        if start != "2024-11-02":
            raise OSError("合成:重取超时")
        return cached(True, sym, start, fq)
    try:
        err = inputs.c_prices({}, "300308", fetch=boom, today=date(2026, 11, 2))
    except Exception as e:                    # 老代码在这里整项抛出,好的那次也丢了
        err = {"value": {"hfq": [["", 0]]}, "status": "failed", "note": repr(e)}
    ok("重取出错 → 留住两年那次取到的,partial,写明重取失败的原因和日期对不上",
       err["value"]["hfq"][-1][0] == "2026-10-30" and err["status"] == "partial" and "重取失败" in err["note"]
       and "合成:重取超时" in err["note"] and "日期对不上" in err["note"], err.get("note"))
    import board_forecast
    ok("这一项照旧让 forecast 不出预测", any("prices.300308" in x and "对不上" in x
                                      for x in board_forecast.price_problems({"prices.300308": err}, ["300308"])))
    # 两种复权都停在前一天、创业板指已有当天(2026-11-03 真跑时漏过去的那种)
    def _it(rows):
        return {"status": "ok", "value": {"hfq": rows, "qfq": rows}}
    lag = {"prices.300308": _it([["2026-10-30", 1.0], ["2026-11-02", 1.0]]),
           "index.sz399006": {"status": "ok", "value": {"day": [["2026-11-02", 2.0], ["2026-11-03", 2.0]]}}}
    probs = board_forecast.price_problems(lag, ["300308"])
    ok("股票日线停在 11-02、创业板指已到 11-03 → 不出预测,写明两个日期",
       any("prices.300308" in x and "2026-11-02" in x and "2026-11-03" in x for x in probs), probs)
    even = {**lag, "prices.300308": _it([["2026-11-02", 1.0], ["2026-11-03", 1.0]])}
    ok("反向:股票和创业板指都到 11-03 → 这一条不报", board_forecast.price_problems(even, ["300308"]) == [],
       board_forecast.price_problems(even, ["300308"]))
    stuck = inputs.c_prices({}, "300308", fetch=lambda s, st, fq: cached(True, s, "2024-11-02", fq),
                            today=date(2026, 11, 2))
    ok("短窗口也停在前一天 → 写「还是停在 2026-10-30」,不写「接上 0 根」",
       "还是停在 2026-10-30" in stuck["note"] and "接上 0 根" not in stuck["note"] and stuck["status"] == "partial",
       stuck["note"])
    ok("thin_years 只看中间的年份", inputs.thin_years([["2017-12-29", 1], ["2019-01-02", 1]]) == [(2018, 0)])

    idx = inputs.c_index({}, fetch=lambda s, st, fq: [(d, 2000.0) for d in _bdays("2024-11-04", "2026-10-30")])
    ok("index.sz399006:不复权日线 {day: [[日期, 收盘]]}", set(idx["value"]) == {"day"}
       and idx["value"]["day"][-1] == ["2026-10-30", 2000.0], idx["value"]["day"][-1:])

    dates = _bdays("2026-01-01", "2026-06-30")
    up = [100.0 + i for i in range(len(dates))]
    r = inputs.regime_value(dates, up, "sz399006")
    ok("一路涨:在 60 日线上,天数 = 均线算出来以后的全部天数",
       r["state"] == "above" and r["days"] == len(dates) - 59 and r["index"] == "sz399006"
       and r["close"] == up[-1] and r["ma60"] == round(sum(up[-60:]) / 60, 2), r)
    down = up[:-3] + [up[-4] - 40, up[-4] - 41, up[-4] - 42]
    r2 = inputs.regime_value(dates, down, "sz399006")
    ok("最后 3 天跌破 → below,days = 3", r2["state"] == "below" and r2["days"] == 3, r2)

    a_days = _bdays("2026-09-01", "2026-10-30")
    a = [(d, 100.0) for d in a_days[:-10]] + [(d, 100.0 + 1.0 * (i + 1)) for i, d in enumerate(a_days[-10:])]
    b = [(d, 50.0) for d in a_days if d != a_days[-15]]          # B 少一天,要按日期配对
    g = inputs.peer_value(a, b)
    ok("同行差:(A 10 日涨幅 − B 10 日涨幅) × 100,按两只都有的日期", g == {"gap10": 10.0, "gap20": 10.0}, g)
    # 跟 system_forecast 走前检验同一种算法
    pa = dict(a)
    t, t10 = a_days[-1], a_days[-11]
    ok("和 walk_forward_stats 的写法一致", abs(g["gap10"] - 100 * (pa[t] / pa[t10] - 50 / 50)) < 1e-9)

    margin = inputs.margin_value([("2026-09-%02d" % (i + 1), 20000.0 + i * 10) for i in range(25)])
    ok("融资余额:亿元、近 20 个交易日变化 %", margin == {"balance_yi": 20240.0,
                                                 "chg20_pct": round((20240 / 20040 - 1) * 100, 2)}, margin)


# ── 取数器:mini 上跑的几项 ──────────────────────────────────────────────────
def check_mini_collectors():
    print("\n[capex / consensus / valuation / events.sec(假的 mini)]")
    fixture = (FIX / "capex.json").read_text(encoding="utf-8")
    seen = {}

    def run_capex(script, args, timeout=None):
        seen.update(script=script, args=args)
        return _proc("已写入 x\n" + fixture.replace("\n", " ") + "\n")
    got = inputs.collect({}, None, {"capex": partial(inputs.c_capex, run=run_capex)})["capex"]
    ok("送到 mini 的是本仓的 capex.py(带 --json 那版),参数 --quarters 8 --guidance --json",
       "def build_json" in seen["script"] and seen["args"] == ["--quarters", "8", "--guidance", "--json"], seen.get("args"))
    ok("JSON 里有缺口 → partial,note 写出哪家哪季", got["status"] == "partial" and "META" in got["note"]
       and "2025-03-31" in got["note"], got["note"])
    ok("capex 值是 parse_capex_json 的结果,as_of 是最新一季", got["value"]["total"][-1]["q"] == "2026-09-30"
       and got["as_of"] == "2026-09-30", got["as_of"])
    clean = json.loads(fixture)
    clean["gaps"] = []
    ok_item = inputs.c_capex({}, run=lambda s, a, timeout=None: _proc(json.dumps(clean)))
    ok("没有缺口、退出码 0 → ok", ok_item.get("status", "ok") == "ok", ok_item.get("note"))
    two = {**clean, "failed": [{"ticker": "GOOGL", "why": "HTTPError"}]}
    p2 = inputs.c_capex({}, run=lambda s, a, timeout=None: _proc(json.dumps(two), code=2))
    ok("退出码 2(有公司没取到)→ partial,note 写出 GOOGL", p2["status"] == "partial" and "GOOGL" in p2["note"], p2)
    f1 = inputs.collect({}, None, {"capex": partial(inputs.c_capex, run=lambda s, a, timeout=None:
                                                     _proc("", code=1, stderr="取 CIK 失败"))})["capex"]
    ok("退出码 1(一家都没取到)→ failed", f1["status"] == "failed" and "取 CIK 失败" in f1["note"], f1)

    md = (FIX / "consensus_300308.md").read_text(encoding="utf-8")
    cseen = {}

    def run_cons(script, args, timeout=None):
        cseen.update(script=script, args=args)
        return _proc(md)
    c = inputs.c_consensus({}, "300308", run=run_cons)
    ok("一致预期:送本仓 consensus.py,参数 <代码> --days 30", "def sec_ratings" in cseen["script"]
       and cseen["args"] == ["300308", "--days", "30"], cseen.get("args"))
    ok("一致预期:值、as_of 取生成时间那天", c["value"]["profit"]["2027"]["mean"] == 670.89
       and c["as_of"] == "2026-11-02" and c.get("status", "ok") == "ok", (c["as_of"], c.get("status")))
    no_profit = inputs.collect({}, None, {"consensus.300308": partial(
        inputs.c_consensus, code="300308", run=lambda s, a, timeout=None: _proc("# 300308\n（净利润（亿元） 取数失败：X）"))})
    ok("一致预期读不到净利润 → failed", no_profit["consensus.300308"]["status"] == "failed")
    ok("没有「取数失败」那行 → rating_failed_days 记 0", c["value"].get("rating_failed_days") == 0, c["value"])
    md_fail = md.replace("### 变动方向", "巨潮评级:3 天取数失败(这几天的评级没看到,家数可能偏少)\n\n### 变动方向")
    cf = inputs.c_consensus({}, "300308", run=lambda s, a, timeout=None: _proc(md_fail))
    ok("巨潮有 3 天取数失败 → partial,note 写明天数;评级照样读出来",
       cf["status"] == "partial" and "3 天" in cf["note"] and cf["value"]["rating_failed_days"] == 3
       and cf["value"]["ratings"]["n"] == 9, (cf["status"], cf["note"], cf["value"].get("rating_failed_days")))

    # 估值:真正送到 mini 的那段函数(holdings_check.valuation_percentile)在假 baostock 上跑,看分位是 0~100
    src = inputs.function_source(inputs.HOLDINGS_CHECK, "valuation_percentile")
    rows = [[f"2026-01-{i:02d}", str(10.0 + i), str(1.0 + i / 100)] for i in range(1, 29)]
    fake_bs = types.SimpleNamespace(
        login=lambda: None, logout=lambda: None,
        query_history_k_data_plus=lambda *a, **k: _FakeRS(rows))
    ns = {}
    with patched(sys.modules, baostock=fake_bs):
        exec(compile(src, "valuation_percentile", "exec"), ns)
        v = ns["valuation_percentile"]("300308")
    pe_pctl = v["pe"][1]
    ok("valuation_percentile 的分位是 0~100(最新值是最高时约 96,不是 0.96)",
       90 < pe_pctl <= 100, v)
    ok("valuation_percentile 带回最后一天(as_of 用)", v.get("end") == "2026-01-28", v)
    val = inputs.valuation_value({"pe": v["pe"], "pb": v["pb"], "start": v["start"], "end": v.get("end"), "dyn_pe": 21.97})
    ok("valuation 值 {pe_pctl, pb_pctl, dyn_pe}", set(val) == {"pe_pctl", "pb_pctl", "dyn_pe"}
       and val["pe_pctl"] == round(pe_pctl, 1) and val["dyn_pe"] == 21.97, val)
    ok("分位超出 0~100 → ValueError", raises(lambda: inputs.valuation_value({"pe": [30, 150.0, 1, 2]}), ValueError)
       is not None)
    vseen = {}

    def run_val(script, args, timeout=None):
        vseen["script"] = script
        return _proc(json.dumps({"pe": [25.1, 96.4, 10.0, 90.0], "pb": None, "start": "2020-01-02",
                                 "end": "2026-10-30", "dyn_pe": None}))
    vi = inputs.c_valuation({}, "300308", run=run_val)
    ok("估值:脚本里就是 holdings_check 那段函数", "def valuation_percentile" in vseen.get("script", "")
       and "300308" in vseen.get("script", ""))
    ok("估值:缺 PB 和动态 PE → partial,as_of 是 baostock 最后一天",
       vi["status"] == "partial" and vi["as_of"] == "2026-10-30" and vi["value"]["pe_pctl"] == 96.4, vi)

    out = {"filings": [{"ticker": "AMZN", "date": "2026-10-30", "form": "10-Q", "doc": "a.htm", "url": "u",
                        "capex_sentences": ["We expect cash capital expenditures to meaningfully increase in 2027."]}],
           "failed": [{"ticker": "NVDA", "why": "HTTPError 503"}]}
    s = inputs.c_sec({"watch_sec": {"AMZN": 1018724, "NVDA": 1045810}},
                     run=lambda script, args, timeout=None: _proc(json.dumps(out)), today=date(2026, 11, 2))
    ok("SEC:有公司没取到 → partial,值是文件清单", s["status"] == "partial" and "NVDA" in s["note"]
       and s["value"][0]["form"] == "10-Q", s)
    unread = {"filings": [{"ticker": "AMZN", "date": "2026-10-30", "form": "10-Q", "doc": "a.htm", "url": "u",
                           "capex_sentences": [], "read": [], "note": "a.htm 没取到 HTTPError"},
                          {"ticker": "AMZN", "date": "2026-10-29", "form": "8-K", "doc": "k.htm", "url": "u",
                           "capex_sentences": [], "read": ["k.htm", "ex99.htm"]}], "failed": []}
    su = inputs.c_sec({"watch_sec": {"AMZN": 1018724}},
                      run=lambda script, args, timeout=None: _proc(json.dumps(unread)), today=date(2026, 11, 2))
    ok("SEC:公司都取到了,但有一份正文没读到 → partial,note 写「1 份正文没读到:AMZN 10-Q」",
       su["status"] == "partial" and "1 份正文没读到" in su["note"] and "AMZN 10-Q" in su["note"], su)
    sa = inputs.c_sec({"watch_sec": {"AMZN": 1018724}}, today=date(2026, 11, 2),
                      run=lambda script, args, timeout=None: _proc(json.dumps({**unread, "filings": unread["filings"][1:]})))
    ok("SEC:正文都读到了 → ok", sa["status"] == "ok", sa)


class _FakeRS:
    """假的 baostock 查询结果:error_code / next() / get_row_data()。"""

    def __init__(self, rows):
        self.error_code, self._rows, self._i = "0", rows, -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return ["%s" % x for x in self._rows[self._i]]


# ── consensus.py:巨潮按天查,取数失败的天数要报出来 ────────────────────────────
def check_consensus_tool():
    print("\n[consensus.py 巨潮评级:取数失败和当天没数据分开数]")
    spec = importlib.util.spec_from_file_location("fb_consensus_under_test", inputs.FETCH_DIR / "consensus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def flaky(date):
        calls.append(date)
        if len(calls) <= 2:
            raise ConnectionError("巨潮超时")
        return None                                   # 这天没有任何评级
    del mod._OUT[:]
    with patched(mod, ak=types.SimpleNamespace(stock_rank_forecast_cninfo=flaky)), redirect_stdout(io.StringIO()):
        mod.sec_ratings("300308", 5)
    text = "\n".join(mod._OUT)
    ok("5 天里 2 天抛异常 → 打出「巨潮评级:2 天取数失败」", "巨潮评级:2 天取数失败" in text and len(calls) == 5, text)
    parsed = inputs.parse_consensus_md(text)
    ok("parse_consensus_md 读出失败天数,没有评级记录时 ratings 仍是 None",
       parsed.get("rating_failed_days") == 2 and parsed["ratings"] is None, parsed)
    del mod._OUT[:]
    with patched(mod, ak=types.SimpleNamespace(stock_rank_forecast_cninfo=lambda date: None)), \
            redirect_stdout(io.StringIO()):
        mod.sec_ratings("300308", 3)
    ok("一天都没失败(只是没数据)→ 不打那行", "取数失败" not in "\n".join(mod._OUT), mod._OUT)


# ── 取数器:研报、台湾、板块、财报日历 ────────────────────────────────────────
def check_other_collectors():
    print("\n[eps_revisions / taiwan / sectors / events.calendar]")
    er = inputs.eps_revisions_module()

    def row(org, d, fy, eps, profit):
        return {"org": org, "date": d, "fy": fy, "eps": eps, "profit": profit, "info": f"{org}{d}"}
    rows = [row("甲", "2026-09-20", 2026, [10.0, 20.0, 30.0], [100.0, 200.0, 300.0]),
            row("甲", "2026-10-25", 2026, [9.0, 15.0, 30.0], [90.0, 150.0, 300.0]),     # 净利润比:2027 调低
            row("乙", "2026-09-20", 2026, [10.0, 20.0, 30.0], [None, None, None]),
            row("乙", "2026-10-28", 2026, [8.0, 14.0, 30.0], [None, None, None]),       # 只能比每股收益:2027 调低
            row("丙", "2026-08-01", 2026, [10.0, 20.0, 30.0], [100.0, 200.0, 300.0]),
            row("丙", "2026-08-20", 2026, [10.0, 25.0, 30.0], [100.0, 250.0, 300.0])]   # 30 天以前,不算
    v = inputs.eps_value(rows, "2026-11-02")
    s = er.summarize(rows, 2027, "2026-11-02")
    ok("只数按净利润比的上下调:2027 年近 30 天调低 1(summarize 把每股收益那条也算进去是 2)",
       v["down30"] == 1 and v["up30"] == 0 and s["down30"] == 2, (v, s["down30"]))
    ok("默认看明年(2027),并写出跳过了几条按每股收益比的", v["fy"] == 2027 and v["eps_basis_skipped30"] == 1, v)
    unread = rows + [row("丁", "2026-10-30", None, [5.0, 6.0, 7.0], [None, None, None]),    # 近 30 天,PDF 没读出来
                     row("戊", "2026-06-01", None, [5.0, 6.0, 7.0], [None, None, None]),    # 更早的
                     row("己", "2026-10-31", None, [None, None, None], [None, None, None])]  # 没有盈利预测,不算
    ei = inputs.eps_item(unread, "2026-11-02")
    ok("研报 PDF 没读出年份:近 30 天 1 篇 → partial,note 写明;更早的 1 篇也提一句",
       ei["status"] == "partial" and "近 30 天有 1 篇" in ei["note"] and "更早的另有 1 篇" in ei["note"]
       and ei["value"]["unread30"] == 1 and ei["value"]["down30"] == 1, ei)
    ok("研报都读出来了 → ok", inputs.eps_item(rows, "2026-11-02")["status"] == "ok")
    empty = inputs.eps_item([], "2026-11-02")
    ok("近一年一篇研报都没有 → note 写明(值是 0 / 0)", "没有研报" in empty["note"]
       and empty["value"]["up30"] == 0 and empty["as_of"] == "2026-11-02", empty)

    tw = [{"公司代號": "3081", "公司名稱": "聯亞", "資料年月": "11509", "出表日期": "1151017",
           "營業收入-當月營收": "1000", "營業收入-去年當月營收": "500", "_同比%": 100.0, "_我们为什么看它": "EML 芯片"},
          {"公司代號": "2345", "公司名稱": "智邦", "資料年月": "11509", "出表日期": "1151017",
           "營業收入-當月營收": "40398562", "營業收入-去年當月營收": "24318092", "_同比%": 66.1,
           "_我们为什么看它": "交换机"}]
    t = inputs.taiwan_value(tw, expected={"3081": "", "2345": "", "4979": ""})
    ok("台湾月营收:民国年月换成公元、出表日期当公开时间",
       t["value"]["ym"] == "2026-09" and t["as_of"] == "2026-09" and t["public_at"] == "2026-10-17", t)
    ok("台湾月营收:每家营收(千元)和同比", t["value"]["companies"][0] == {
        "code": "3081", "name": "聯亞", "ym": "2026-09", "rev_k_twd": 1000.0, "rev_last_year_k_twd": 500.0,
        "yoy": 100.0, "why": "EML 芯片"}, t["value"]["companies"][0])
    ok("台湾月营收:少了一家 → partial,note 写出代号", t["status"] == "partial" and "4979" in t["note"], t["note"])

    series = [(d, 100.0) for d in _bdays("2025-09-01", "2026-08-31")] + \
             [(d, 80.0) for d in _bdays("2026-09-01", "2026-10-30")]      # 后面 44 根是 80
    e = inputs.etf_value("通信", series)
    ok("板块 ETF:近 1 / 3 个月(21 / 63 个交易日)涨跌、离一年高点",
       e == {"name": "通信", "m1": 0.0, "m3": round((80 / 100 - 1) * 100, 2), "dd_1y": -20.0}, e)
    sec_cfg = {"sector_etfs": {"515880": "通信", "512480": "半导体"}}

    def fake_etf(sym, start, fq):
        if sym == "sh512480":
            raise TimeoutError("超时")
        return series
    se = inputs.c_sectors(sec_cfg, fetch=fake_etf)
    ok("板块:一只没取到 → partial,其余照样给", se["status"] == "partial" and "512480" in se["note"]
       and se["value"]["515880"]["name"] == "通信", se)
    asked = []

    def no_qfq(sym, start, fq):
        asked.append((sym, fq))
        if sym == "sz159819" and fq == "qfq":       # 腾讯对没分过红的 ETF 只回 day,market 拒绝顶替
            raise ValueError("sz159819 没有 qfq 日线(缺 qfqday),不用不复权数据顶替")
        return series
    nq_se = inputs.c_sectors({"sector_etfs": {"159819": "人工智能", "515880": "通信"}}, fetch=no_qfq)
    ok("板块:ETF 没有前复权 → 改用不复权,note 写明是哪只;数据是全的,status 仍 ok",
       nq_se["status"] == "ok" and "159819" in nq_se["value"] and "不复权" in nq_se["note"]
       and "159819" in nq_se["note"] and ("sz159819", "") in asked, (nq_se["status"], nq_se["note"], asked))

    nq = json.loads((FIX / "nasdaq_2026-11-02.json").read_text(encoding="utf-8"))
    tried = []

    def fake_nq(day, proxy=None):
        tried.append((day, proxy))
        if proxy is None:
            raise OSError("直连被拒")
        return nq if day == "2026-11-02" else {"data": {"rows": None}}
    with patched(fb_common.ORIG_ENV, https_proxy="http://127.0.0.1:7"):
        cal = inputs.c_calendar({"watch_us": ["FN"]}, fetch=fake_nq, today=date(2026, 11, 2), days=3)
    ok("财报日历:直连失败 → 用启动时的代理重试,note 写明", cal["value"] == [
        {"date_us": "2026-11-02", "symbol": "FN", "name": "Fabrinet", "time": "time-after-hours",
         "beijing": "2026-11-03 早上", "relation": events.RELATION["FN"]}]
       and "代理" in cal["note"] and ("2026-11-04", "http://127.0.0.1:7") in tried, (cal, tried))
    days_asked = []
    inputs.c_calendar({}, fetch=lambda day, proxy=None: days_asked.append(day) or {"data": {"rows": None}},
                      today=date(2026, 11, 2))
    ok("财报日历默认看 21 个自然日(配置没写 calendar_days 时)", len(days_asked) == 21
       and days_asked[-1] == "2026-11-22", days_asked[-1:])
    days_asked.clear()
    inputs.c_calendar({"calendar_days": 5}, fetch=lambda day, proxy=None: days_asked.append(day) or {},
                      today=date(2026, 11, 2))
    ok("配置写了 calendar_days 就按配置", len(days_asked) == 5, days_asked)


# ── missing_required ────────────────────────────────────────────────────────
def check_missing_required():
    print("\n[missing_required]")
    cfg = {"stocks": [{"code": "300308", "sector": "AI算力链"}]}
    base = {"prices.300308": {"status": "ok"}, "index.sz399006": {"status": "ok"}}
    gaps = ["gap:海外同业股价", "gap:券商研报里的出货/产能"]
    got = inputs.missing_required({**base, "capex": {"status": "failed"}}, cfg)
    ok("AI算力链:capex 取数失败 → 列出 capex;这个行业还没有工具的两个指标列成 gap:<指标>",
       got == ["capex", *gaps], got)
    ok("gap: 后面的名字都能在 TOOL_GAPS 里查到原因", all(g[4:] in inputs.TOOL_GAPS for g in gaps))
    ok("capex 是 stale / partial 不算缺(页面另列「用了旧值、不全」),工具缺口照列",
       inputs.missing_required({**base, "capex": {"status": "stale"}}, cfg) == gaps
       and inputs.missing_required({**base, "capex": {"status": "partial"}}, cfg) == gaps)
    ok("日线没取 → 列出 prices.300308",
       inputs.missing_required({"index.sz399006": {"status": "ok"}, "capex": {"status": "ok"}}, cfg)
       == ["prices.300308", *gaps])
    unknown = inputs.missing_required(base, {"stocks": [{"code": "300308"}]})
    ok("配置里没写行业 → 列出 sector.300308(不联网猜,也不当成没有要求)", unknown == ["sector.300308"], unknown)
    typo = inputs.missing_required(base, {"stocks": [{"code": "300308", "sector": "AI 算力"}]})
    ok("写了行业表里没有的名字 → 同样列 sector.300308", typo == ["sector.300308"], typo)
    other = inputs.missing_required(base, {"stocks": [{"code": "300308", "sector": "消费"}]})
    ok("消费:领先指标全是工具缺口 → 三个 gap:", other == ["gap:渠道库存与批价", "gap:预收款 / 合同负债",
                                                    "gap:社零同比"], other)


# ── events ──────────────────────────────────────────────────────────────────
def check_events():
    print("\n[events]")
    nq = json.loads((FIX / "nasdaq_2026-11-02.json").read_text(encoding="utf-8"))
    cal = events.nasdaq_calendar(["2026-11-02"], {"FN"}, fetch=lambda d: nq)
    ok("盘后换北京次日早上", cal[0]["beijing"] == "2026-11-03 早上", cal)
    every = {r["symbol"]: r["beijing"] for r in events.nasdaq_calendar(["2026-11-02"], set(), fetch=lambda d: nq)}
    ok("盘前 → 当天晚上;没给时间 → 时间未定;watch 为空 = 全列",
       every.get("MAR") == "2026-11-02 晚上" and every.get("PKX") == "时间未定" and len(every) == 4, every)
    ok("周末(rows 为 null)→ 空", events.nasdaq_calendar(["2026-11-07"], {"FN"},
                                                    fetch=lambda d: {"data": {"rows": None}}) == [])
    err = {"data": None, "message": "Bad Request", "status": {"rCode": 400}}
    ok("接口回错误码 → 抛出(不当成那天没有财报)",
       raises(lambda: events.nasdaq_calendar(["2026-11-02"], {"FN"}, fetch=lambda d: err), RuntimeError) is not None)
    ok("跨月:10-31 盘后 → 11-01 早上", events.beijing_time("2026-10-31", "time-after-hours") == "2026-11-01 早上")
    ok("next_days:从起点数 N 个自然日", events.next_days(3, date(2026, 11, 2)) == ["2026-11-02", "2026-11-03",
                                                                                "2026-11-04"])

    s = events.extract_capex_sentences((FIX / "sec_amzn_10q.txt").read_text(encoding="utf-8"))
    ok("摘到亚马逊 2027 那句", any("meaningfully increase in 2027" in t for t in s), s)
    neg = events.extract_capex_sentences(
        "We enter into supplier financing arrangements for technology infrastructure assets from Q1 2027 "
        "through Q3 2028. Capital expenditures were high. We expect capex to grow next year.")
    ok("没有资本开支字样的 2027 句、没有年份的资本开支句都不摘;next year + capex 要摘",
       neg == ["We expect capex to grow next year."], neg)
    orcl = ("It also outlined its expectations for strong profitability. Following a period of peak capex in "
            "FY27-FY28, capital expenditures are expected to step-down in FY29, supporting positive free cash flow.")
    ok("财年写成 FY27 也算年份(甲骨文 2026-10-28 投资者日那句)",
       events.extract_capex_sentences(orcl) == [orcl.split(". ", 1)[1]], events.extract_capex_sentences(orcl))
    past = " ".join(f"Depreciation on property and equipment was ${i} billion in 2025." for i in range(1, 11))
    ranked = events.extract_capex_sentences(past + " We expect capital expenditures to rise in 2027.")
    ok("往后看的句子(expect / anticipate …)排前面,再截到 8 句 —— 不被前面的旧数字挤掉",
       len(ranked) == 8 and ranked[0] == "We expect capital expenditures to rise in 2027.", ranked[:2])
    plant = " ".join(f"Capital expenditures for property, plant and equipment were ${i} billion in 2025."
                     for i in range(1, 11))
    pr = events.extract_capex_sentences(plant + " Capital expenditures are planned to rise in 2027.")
    ok("「plant」不算往后看(property, plant and equipment);planned 算",
       pr[0] == "Capital expenditures are planned to rise in 2027.", pr[:2])

    sub = {"filings": {"recent": {
        "filingDate": ["2026-10-30", "2026-10-29", "2026-10-28", "2026-09-01"],
        "form": ["10-Q", "4", "8-K", "10-Q"],
        "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003", "0001-26-000004"],
        "primaryDocument": ["q.htm", "f4.xml", "k.htm", "old.htm"]}}}
    pages = {
        "https://data.sec.gov/submissions/CIK0001018724.json": json.dumps(sub).encode(),
        "https://www.sec.gov/Archives/edgar/data/1018724/000126000001/q.htm":
            b"<p>We expect cash capital expenditures to meaningfully increase in 2027.</p>",
        "https://www.sec.gov/Archives/edgar/data/1018724/000126000003/k.htm": b"<p>Item 2.02</p>",
        "https://www.sec.gov/Archives/edgar/data/1018724/000126000003/index.json":
            json.dumps({"directory": {"item": [{"name": "k.htm"}, {"name": "ex99-1.htm"}]}}).encode(),
        "https://www.sec.gov/Archives/edgar/data/1018724/000126000003/ex99-1.htm":
            b"<div>Capital expenditures in fiscal 2027 will peak.</div>",
    }

    def fake_get(url):
        if url not in pages:
            raise OSError(f"没有这页 {url}")
        return pages[url]
    res = events.scan_here({"AMZN": 1018724, "META": 1326801}, "2026-10-15", get=fake_get)
    forms = [(f["form"], f["doc"]) for f in res["filings"]]
    ok("只列 since 以后的、要看的文件类型(去掉 Form 4、9 月那份)", forms == [("10-Q", "q.htm"), ("8-K", "k.htm")], forms)
    ok("10-Q 正文摘句", res["filings"][0]["capex_sentences"] == [
        "We expect cash capital expenditures to meaningfully increase in 2027."], res["filings"][0])
    ok("8-K 读附件 99.1 摘句", res["filings"][1]["capex_sentences"] == ["Capital expenditures in fiscal 2027 will peak."]
       and "ex99-1.htm" in res["filings"][1]["read"], res["filings"][1])
    ok("取不到文件列表的公司进 failed", [x["ticker"] for x in res["failed"]] == ["META"], res["failed"])

    seen = {}

    def fake_remote(script, args):
        seen.update(script=script, args=args)
        return "噪音\n" + json.dumps(res) + "\n"
    got = events.sec_new_filings({"AMZN": 1018724}, "2026-10-15", run_remote=fake_remote)
    ok("sec_new_filings:把 events.py 整个送到 mini,参数 sec-here --since … --ciks …",
       "def scan_here" in seen["script"] and seen["args"] == ["sec-here", "--since", "2026-10-15",
                                                             "--ciks", "AMZN=1018724"], seen.get("args"))
    ok("sec_new_filings 返回文件清单", got == res["filings"], got)

    argv = []

    def fake_run(cmd, **kw):
        argv.append((cmd, kw))
        return _proc("{}")
    with patched(events, subprocess=types.SimpleNamespace(run=fake_run)):
        events.ssh_python("print(1)", ["sec-here", "--since", "2026-10-15"], host="h", timeout=900)
    remote = argv[0][0][-1] if argv else ""
    ok("送到 mini 的脚本先设闹钟:signal.alarm(870),比本机超时 900 早 30 秒自己退出",
       "signal.alarm(870)" in remote and remote.endswith("sec-here --since 2026-10-15")
       and argv[0][1].get("timeout") == 900 and argv[0][1].get("input") == "print(1)", argv)


# ── 值的形状:system_forecast 能直接读 ──────────────────────────────────────
def check_shapes_for_system():
    print("\n[输入包 → system_forecast.signals_from_inputs]")
    dates = _bdays("2026-01-01", "2026-06-30")
    closes = [100.0 + i for i in range(len(dates))]
    a = [(d, 100.0) for d in dates[:-10]] + [(d, 120.0) for d in dates[-10:]]
    b = [(d, 100.0) for d in dates]
    cap = inputs.parse_capex_json(json.loads((FIX / "capex.json").read_text(encoding="utf-8")))
    items = {"market.regime": {"status": "ok", "value": inputs.regime_value(dates, closes, "sz399006")},
             "peer.300308.300502": {"status": "ok", "value": inputs.peer_value(a, b)},
             "eps_revisions.300308": {"status": "ok", "value": {"up30": 0, "down30": 1, "fy": 2027}},
             "capex": {"status": "partial", "value": cap},
             "valuation.300308": {"status": "ok", "value": inputs.valuation_value(
                 {"pe": [25.1, 85.0, 10.0, 90.0], "pb": [6.9, 50.0, 1, 9], "dyn_pe": 22.0})}}
    sig = sf.signals_from_inputs(items, "300308")
    ok("五个信号都读得到(一个都不缺)", sig["missing"] == [], sig)
    ok("各信号方向:大盘 +1、同行 +1(A 多涨 20 点)、券商 −1、资本开支 +1(79.7 → 94.5)、估值 −1(分位 85)",
       sig["values"] == {"regime": 1, "peer": 1, "consensus": -1, "capex": 1, "valuation": -1}
       and sig["sources"]["consensus"] == "eps_revisions", sig)
    sig_b = sf.signals_from_inputs(items, "300502")
    ok("同一对同行,对 B 取反", sig_b["values"]["peer"] == -1, sig_b)


def check_archive():
    import tempfile
    import subprocess as sp
    from datetime import datetime
    import archive
    print("archive")
    t = Path(tempfile.mkdtemp()); now = datetime(2026, 11, 3, 9, 30, 0)
    r1 = archive.new_run("2026-11-03", now, t)
    ok("同一秒重复建目录要报错", raises(lambda: archive.new_run("2026-11-03", now, t), FileExistsError) is not None)
    r2 = archive.new_run("2026-11-03", now + timedelta(minutes=5), t)
    archive.write_json_once(r2 / "manifest.json", {})
    ok("最新一次", archive.latest_run(t) == r2)
    ok("before 只看更早的日期", archive.latest_run(t, before="2026-11-03") is None
       and archive.latest_run(t, before="2026-11-04") == r2)
    ok("list_runs 按时间排", archive.list_runs(t, complete_only=False) == [r1, r2]
       and archive.list_runs(t) == [r2])
    archive.write_json_once(r1 / "a.json", {"x": 1})
    ok("不许覆盖", raises(lambda: archive.write_json_once(r1 / "a.json", {"x": 2}), FileExistsError) is not None
       and json.load(open(r1 / "a.json")) == {"x": 1})
    ok("写完不留临时文件", [p.name for p in r1.iterdir()] == ["a.json"], list(r1.iterdir()))
    archive.supersede(r1, r2, "同日重跑")
    ok("旧版只多一个指向文件", (r1 / "superseded.json").exists() and json.load(open(r1 / "a.json")) == {"x": 1})
    ok("supersede 不能写第二次", raises(lambda: archive.supersede(r1, r2, "x"), FileExistsError) is not None)
    archive.save_answer(r2, "baseline", {"p": 1}); archive.save_inputs(r2, [{"k": 1}])
    ok("答案和输入落盘", json.load(open(r2 / "answers" / "baseline.json")) == {"p": 1}
       and json.load(open(r2 / "inputs.json")) == [{"k": 1}])
    ok("作者名带路径要拒", raises(lambda: archive.save_answer(r2, "../x", {}), ValueError) is not None)
    # manifest:用一个临时 git 仓
    g = Path(tempfile.mkdtemp())
    def git(*a):
        return sp.run(["git", "-C", str(g), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                      capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q"); (g / "f.txt").write_text("a\n"); git("add", "."); git("commit", "-qm", "c")
    m = archive.write_manifest(r1, {"sell": 1}, 7, g)
    ok("manifest 记提交号", m["git_head"] == git("rev-parse", "HEAD") and m["clean"] is True, m)
    ok("干净时 code.diff 为空", (r1 / "code.diff").read_text() == "")
    r3 = archive.new_run("2026-11-03", now + timedelta(minutes=9), t)
    (g / "f.txt").write_text("b\n")
    m2 = archive.write_manifest(r3, {"sell": 1}, 7, g)
    ok("有改动时 clean=False 且 diff 带改动", m2["clean"] is False and "+b" in (r3 / "code.diff").read_text())
    ok("manifest 存了配置和种子", json.load(open(r3 / "manifest.json"))["config"] == {"sell": 1}
       and json.load(open(r3 / "manifest.json"))["seed"] == 7)
    ok("manifest 不能写第二次", raises(lambda: archive.write_manifest(r3, {}, 7, g), FileExistsError) is not None)
    # link_journal:临时工作台,空日志
    lab = Path(tempfile.mkdtemp()); (lab / "data").mkdir(); (lab / "data" / "journal.jsonl").write_text("")
    run = archive.new_run("2026-11-03", now + timedelta(minutes=20), lab / "data" / "forecasts")
    ids = archive.link_journal(run, ["300308", "300502"], {"300308": 100.0, "300502": 200.0},
                               ["2027-01-01"], lab=lab, start=date(2026, 11, 3))
    rows = [json.loads(x) for x in (lab / "data" / "journal.jsonl").read_text().splitlines() if x.strip()]
    ok("决策日志多了 2 行", len(rows) == 2 and ids == [r["id"] for r in rows], (ids, rows))
    ok("thesis 带存档相对路径", all("data/forecasts/2026-11-03/" in r["thesis"] for r in rows), rows)
    import prices as _p
    exp = _p.future_trading_days(date(2026, 11, 3), 63, {"2027-01-01"})[-1].isoformat()
    ok("复核日 = 从起点日(算第 1 个)数的第 63 个交易日", all(r["check_date"] == exp for r in rows),
       (exp, rows[0]["check_date"]))
    n0 = len((lab / "data" / "journal.jsonl").read_text().splitlines())
    e = raises(lambda: archive.link_journal(run, ["300308", "600000"], {"300308": 1.0}, [], lab=lab,
                                            start=date(2026, 11, 3)), KeyError)
    ok("缺起点价要报错,且一条都不写", e is not None and "600000" in str(e)
       and len((lab / "data" / "journal.jsonl").read_text().splitlines()) == n0, e)
    # 第二次调用失败的假 journal.py
    fake = Path(tempfile.mkdtemp()) / "journal.py"
    cnt = fake.parent / "n"
    fake.write_text("import sys,pathlib\nc=pathlib.Path(%r)\nn=int(c.read_text()) if c.exists() else 0\n"
                    "c.write_text(str(n+1))\nif n==1:\n    print('boom',file=sys.stderr); sys.exit(1)\n"
                    "print('已记录 #%%d' %% (n+10))\n" % str(cnt))
    e = raises(lambda: archive.link_journal(run, ["a", "b", "c"], {"a": 1, "b": 1, "c": 1}, [], lab=lab,
                                            start=date(2026, 11, 3), journal=fake), archive.JournalLinkError)
    ok("第 2 条失败:异常带已写编号", e is not None and e.written_ids == [10] and "[10]" in str(e), e)
    ok("journal.py 退出码非 0 要抛错", e is not None and "boom" in str(e))
    # 新增:as_of 校验、跨日期排序、没写完的不算、git 失败、untracked
    for bad in ("../x", "2026-11-3", ""):
        ok(f"as_of {bad!r} 要拒", raises(lambda b=bad: archive.new_run(b, now, t), ValueError) is not None)
    t2 = Path(tempfile.mkdtemp())
    ra = archive.new_run("2026-11-03", now, t2); rb = archive.new_run("2026-11-10", now, t2)
    for r in (ra, rb):
        archive.write_json_once(r / "manifest.json", {})
    rc = archive.new_run("2026-11-10", now + timedelta(hours=1), t2)   # 没 manifest,没写完
    ok("跨日期:最新是 11-10 那次(不是字符串碰巧靠后的)", archive.latest_run(t2) == rb, archive.latest_run(t2))
    ok("没写完的目录默认跳过", rc not in archive.list_runs(t2) and rc in archive.list_runs(t2, complete_only=False))
    archive.supersede(rb, rc, "x")
    ok("被 supersede 的仍可选中", archive.latest_run(t2) == rb)
    nogit = Path(tempfile.mkdtemp())
    ok("不是 git 仓要抛错", raises(lambda: archive.write_manifest(archive.new_run("2026-11-04", now, t2), {}, 1, nogit), RuntimeError) is not None)
    g2 = Path(tempfile.mkdtemp())
    def git2(*a):
        return sp.run(["git", "-C", str(g2), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                      capture_output=True, text=True, check=True).stdout.strip()
    git2("init", "-q"); (g2 / "scripts").mkdir(); (g2 / "scripts" / "a.py").write_text("1\n")
    git2("add", "."); git2("commit", "-qm", "c")
    (g2 / "scripts" / "new.py").write_text("print('new')\n")
    (g2 / "scripts" / "big.py").write_text("x" * (1024 * 1024 + 1))
    (g2 / "scripts" / "data.bin").write_bytes(b"\0")
    (g2 / ".superpowers").mkdir(); (g2 / ".superpowers" / "n.md").write_text("n")
    r5 = archive.new_run("2026-11-05", now, t2)
    m5 = archive.write_manifest(r5, {}, 1, g2)
    ok("未跟踪 .py 被抄进 untracked/", m5["untracked"] == ["scripts/new.py"]
       and (r5 / "untracked" / "scripts" / "new.py").read_text() == "print('new')\n", m5)
    ok("太大和扩展名不对的记在 skipped", {x["path"] for x in m5["untracked_skipped"]} == {"scripts/big.py", "scripts/data.bin"}, m5)
    ok("其他目录忽略", not (r5 / "untracked" / ".superpowers").exists())
    ok("有抄进来的未跟踪文件 → clean=False", m5["clean"] is False)


CHECKS = [check_orig_env, check_collect, check_parsers, check_price_collectors, check_mini_collectors,
          check_consensus_tool,
          check_other_collectors, check_missing_required, check_events, check_shapes_for_system, check_archive]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
