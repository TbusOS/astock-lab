#!/usr/bin/env python3
"""check_board_ai —— 预测看板「两个 AI 作答」(skills/forecast-board/scripts/ai_answer.py)的检查(不联网)。

用法:
    python3 scripts/check_board_ai.py        # 退出码 0 才算过

原来在 check_board_store.py 里,那个文件超过 800 行时原样拆出来,用例一条没改。
不调真的 claude / codex:runner 换成假的,subprocess.run 换成假的,查命令行、提示词、校验、重问、存档。
临时换掉模块里几项的小工具(patched、_MISSING)用 check_board_store.py 里的那份,不另抄。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402  先于 inputs / ai_answer,和正式运行的顺序一样
from check_board_store import _MISSING, patched  # noqa: E402

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


# ── ai_answer:两个 AI 作答 ─────────────────────────────────────────────────
AI_CODES = ["300308", "300502"]
AI_CATS = {"bottom_months": ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05"],
           "buy_months": ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05",
                          "2027-06", "2027-07", "2027-08", "2027-09", "2027-10", "2027-11", "later"],
           "price_bins": [[0.0, 0.5], [0.5, 0.6], [0.6, 0.7], [0.7, 0.8], [0.8, 0.9], [0.9, 1.0]]}


def _ai_stock(p0: float) -> dict:
    """一只股票的合格答卷(合成数字):三个期限、三组分类各自加起来 100。"""
    hz = {h: {"p_up": p, "q": {"10": round(p0 * a, 2), "25": round(p0 * b, 2), "50": round(p0 * c, 2),
                               "75": round(p0 * d, 2), "90": round(p0 * e, 2)}}
          for h, p, (a, b, c, d, e) in (("63", 45, (0.7, 0.85, 0.98, 1.12, 1.3)),
                                        ("126", 48, (0.6, 0.8, 1.0, 1.2, 1.45)),
                                        ("250", 52, (0.5, 0.75, 1.05, 1.4, 1.8)))}
    return {"horizons": hz,
            "bottom": {"month_probs": dict(zip(AI_CATS["bottom_months"], [30, 25, 15, 10, 8, 7, 5])),
                       "price_bins": [[lo, hi, p] for (lo, hi), p in
                                      zip(AI_CATS["price_bins"], [5, 10, 20, 30, 25, 10])]},
            "buy": {"month_probs": dict(zip(AI_CATS["buy_months"], [5, 10, 10, 10, 10, 10, 10, 5, 5, 5, 5, 5, 5, 5])),
                    "zones": [[round(p0 * 0.9, 2), 60], [round(p0 * 0.8, 2), 35], [round(p0 * 0.7, 2), 15]],
                    "conditions": ["创业板指收盘回到 60 日线上"]},
            "action": {"kind": "hold", "shares": None, "amount": None, "deadline": "2026-11-30",
                       "why": "合成样例", "change_if": [{"if": "收盘跌破 0.8 倍起点价", "then": "trim"}]},
            "evidence": {"support": ["样例 A"], "against": ["样例 B"], "unverified": []},
            "judgment": _ai_judgment(p0)}


def _ai_judgment(p0: float) -> dict:
    """合格的「怎么看」(合成):出处用 _ai_package 输入包里有的键,日期在 as_of 2026-10-30 之后。"""
    return {"fact": "已知:样例事实", "view": "据此判断:样例判断",
            "holding": {"pick": "hold", "answer": "拿着", "when": "没跌破卖出线时", "why": "样例"},
            "rebuy": {"pick": "wait", "answer": "先等等", "when": "等下个季报", "why": "样例"},
            "industry": {"pick": "watch", "answer": "值得跟踪", "when": "需求没变坏时", "why": "样例"},
            "scenarios": [{"label": "A", "p": 60, "what": "样例甲", "low_when": "11 月", "low_price": [p0 * 0.9, p0],
                           "how": "样例"},
                          {"label": "B", "p": 40, "what": "样例乙", "low_when": "12 月", "low_price": p0 * 0.7, "how": "样例"}],
            "change_if": "收盘跌破 0.7 倍起点价会削弱这个判断,要重新检查需求",
            "key_dates": [{"date": "2026-11-18", "what": "样例日子", "why": "样例"}],
            "facts": [{"kind": "资金", "text": "融资余额 20 日 -2%", "source": "market.margin"},
                      {"kind": "情绪", "text": "指数样例", "source": "index.sz399006"},
                      {"kind": "估值", "text": "日线样例", "source": "prices.300308"}],
            "review": "2027-01-29"}


def _ai_good() -> dict:
    return {"stocks": {"300308": _ai_stock(100.0), "300502": _ai_stock(50.0)}}


def _ai_package(ai, **kw) -> dict:
    days = [[f"2026-{m:02d}-{d:02d}", 100.0 + i] for i, (m, d) in
            enumerate((m, d) for m in range(1, 11) for d in range(1, 29))]          # 280 根,超过 250
    layer = {"layer": "baseline", "start_price": 200.0,       # 底线用后复权价:起点 200,答题用的起点 100
             "horizons": {"63": {"p_up": 50, "q": {"10": 160.0, "25": 180.0, "50": 200.0, "75": 220.0,
                                                   "90": 250.0}}},
             "bottom": {"window": 126, "month_probs": {"2026-11": 100}, "price_bins": [], "price_q": {}}}
    args = dict(as_of="2026-10-30", start="2026-11-02", codes=AI_CODES,
                start_close={"300308": 100.0, "300502": 50.0}, categories=AI_CATS,
                inputs={"prices.300308": {"key": "prices.300308", "status": "ok",
                                          "value": {"hfq": days, "qfq": days}},
                        "index.sz399006": {"key": "index.sz399006", "status": "ok", "value": {"day": days}},
                        "market.margin": {"key": "market.margin", "status": "ok",
                                          "value": {"balance_yi": 1.0, "chg20_pct": -2.0}}},
                missing_required=["gap:海外同业股价"],
                baseline={c: layer for c in AI_CODES},
                system={c: {**layer, "layer": "system", "unvalidated": ["regime"]} for c in AI_CODES},
                account=None, rules={"sell_lines": {"300308": 1.0}}, research_notes=None)
    return ai.build_package(**{**args, **kw})


def _put(obj: dict, value, *path) -> dict:
    """深拷贝后把 path 指向的那一项换成 value;value 是 _MISSING 就删掉那一项。不改原对象。"""
    out = json.loads(json.dumps(obj))
    node = out
    for k in path[:-1]:
        node = node[k]
    if value is _MISSING:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return out


def check_ai_answer():
    import tempfile
    import ai_answer as ai
    print("\n[ai_answer]")
    good, codes = _ai_good(), AI_CODES
    ok("合格答卷 → 没有错", ai.validate_answer(good, codes) == [], ai.validate_answer(good, codes))
    ok("合格答卷 + 分类对得上 → 没有错", ai.validate_answer(good, codes, categories=AI_CATS) == [],
       ai.validate_answer(good, codes, categories=AI_CATS))

    def errs(obj, cats=None):
        return ai.validate_answer(obj, codes, categories=cats)

    def hit(es, *words):
        return bool(es) and any(all(w in e for w in words) for e in es)

    e = errs(_put(good, _MISSING, "stocks", "300308", "horizons", "250"))
    ok("删掉 250 天 → 报错并点出股票和期限", hit(e, "300308", "250"), e)
    e = errs(_put(good, 29, "stocks", "300502", "bottom", "month_probs", "2026-11"))
    ok("见底月份加起来 99 → 报错并点出位置和合计", hit(e, "300502", "bottom.month_probs", "99"), e)
    e = errs(_put(good, 75.0, "stocks", "300308", "horizons", "126", "q", "50"))     # 25 分位是 80
    ok("分位 50 比 25 小 → 报错并点出期限和分位", hit(e, "300308", "126", "50"), e)
    e = errs(_put(good, 101, "stocks", "300308", "horizons", "63", "p_up"))
    ok("p_up = 101 → 报错并点出 p_up", hit(e, "300308", "63", "p_up", "101"), e)
    e = errs(_put(good, 45.0, "stocks", "300308", "horizons", "63", "p_up"))
    ok("p_up 给小数 45.0 → 报错(要整数)", hit(e, "p_up"), e)
    e = errs(_put(good, True, "stocks", "300308", "horizons", "63", "p_up"))
    ok("p_up 给 true → 报错(布尔不算整数)", hit(e, "p_up"), e)
    e = errs(_put(good, None, "stocks", "300308", "horizons", "63", "q", "10"))
    ok("分位留空(null)→ 报错", hit(e, "300308", "63", "10"), e)
    e = errs(_put(good, "sell_all", "stocks", "300308", "action", "kind"))
    ok("action.kind 不在枚举里 → 报错", hit(e, "300308", "action.kind"), e)
    e = errs(_put(good, "11月底", "stocks", "300308", "action", "deadline"))
    ok("deadline 不是 YYYY-MM-DD → 报错", hit(e, "deadline"), e)
    e = errs(_put(good, _MISSING, "stocks", "300502"))
    ok("少一只股票 → 报错", hit(e, "300502"), e)
    e = errs(_put(good, _ai_stock(10.0), "stocks", "688000"))
    ok("多出一只没问的股票 → 报错", hit(e, "688000"), e)
    e = errs(_put(good, _MISSING, "stocks", "300308", "buy", "month_probs", "later"))
    ok("买点月份没有 later → 报错", hit(e, "later"), e)
    e = errs(_put(good, [[95.0, 20], [90.0, 60]], "stocks", "300308", "buy", "zones"))
    ok("买点价位:价越低、跌到的概率反而越高 → 报错(前后矛盾)", hit(e, "300308", "zones"), e)
    e = errs(_put(good, [], "stocks", "300308", "action", "change_if"))
    ok("没写改主意条件 → 报错", hit(e, "change_if"), e)
    ok("不是 dict / 没有 stocks → 报错不抛出", errs([]) != [] and errs({"x": 1}) != [])

    # 分类必须和输入包一字不差
    e = errs(_put(good, _MISSING, "stocks", "300308", "bottom", "month_probs", "2027-05"), AI_CATS)
    ok("有分类时:见底月份缺一个 → 报错点出缺的月份", hit(e, "300308", "2027-05"), e)
    months = good["stocks"]["300308"]["bottom"]["month_probs"]
    shifted = _put(good, {**{m: v for m, v in months.items() if m != "2027-05"}, "2027-06": 5},
                   "stocks", "300308", "bottom", "month_probs")
    ok("没给分类时同一份只查加和 → 不报月份", errs(shifted) == [], errs(shifted))
    e = errs(shifted, AI_CATS)
    ok("有分类时:多出 2027-06 → 报错", hit(e, "2027-06"), e)
    bins = good["stocks"]["300308"]["bottom"]["price_bins"]
    e = errs(_put(good, [[lo * 100, hi * 100, p] for lo, hi, p in bins], "stocks", "300308", "bottom", "price_bins"),
             AI_CATS)
    ok("有分类时:价格分段写成绝对价(不是比值)→ 报错", hit(e, "300308", "price_bins"), e)
    e = errs(_put(good, [[0.0, 0.5, 50], [0.5, 1.0, 50]], "stocks", "300308", "bottom", "price_bins"), AI_CATS)
    ok("有分类时:价格分段数不对 → 报错", hit(e, "price_bins"), e)
    e = errs(_put(good, _MISSING, "stocks", "300502", "buy", "month_probs", "2027-11"), AI_CATS)
    ok("有分类时:买点月份缺一个 → 报错", hit(e, "300502", "2027-11"), e)

    # parse_answer
    body = json.dumps(good, ensure_ascii=False, indent=1)
    ok("认 ```json 代码块,前面有客套话", ai.parse_answer(f"好的,结果如下:\n```json\n{body}\n```\n") == good)
    ok("没有代码块:取第一个完整 JSON 对象,前后有字", ai.parse_answer(f"答卷 {body} 以上") == good)
    ok("前面有个不是 JSON 的花括号 → 跳过它", ai.parse_answer("格式见 {下面} 这样:" + body) == good)
    ok("两个对象 → 取第一个", ai.parse_answer('{"a": 1} {"b": 2}') == {"a": 1})
    ok("根本没有 JSON → ValueError", raises(lambda: ai.parse_answer("不是 JSON"), ValueError) is not None)
    ok("顶层是数组不算 → ValueError", raises(lambda: ai.parse_answer("[1, 2]"), ValueError) is not None)

    # build_package
    pkg = _ai_package(ai)
    hfq = pkg["inputs"]["prices.300308"]["value"]["hfq"]
    ok("输入包:日线只留最近 250 个收盘(最后一根不丢)", len(hfq) == 250 and hfq[-1][0] == "2026-10-28"
       and len(pkg["inputs"]["index.sz399006"]["value"]["day"]) == 250, (len(hfq), hfq[-1]))
    ok("输入包:不是日线的值原样保留",
       pkg["inputs"]["market.margin"]["value"] == {"balance_yi": 1.0, "chg20_pct": -2.0})
    ok("输入包字段齐全", set(pkg) >= {"as_of", "start", "codes", "start_close", "categories", "inputs",
                                     "missing_required", "baseline", "system", "account", "rules",
                                     "research_notes"}, sorted(pkg))
    src = {"prices.300308": {"key": "prices.300308", "status": "ok",
                             "value": {"hfq": [[f"d{i:03d}", 1.0] for i in range(300)]}}}
    _ai_package(ai, inputs=src)
    ok("build_package 不改传进来的 inputs", len(src["prices.300308"]["value"]["hfq"]) == 300)
    ok("缺某只股票的起点价 → 报错",
       raises(lambda: _ai_package(ai, start_close={"300308": 100.0}), ValueError) is not None)
    ok("缺某只股票的底线层 → 报错",
       raises(lambda: _ai_package(ai, baseline={"300308": {}}), ValueError) is not None)
    # 隐私:给 AI 的只有持股数、还款数、可取现金和后续缺口;成本、截图价、净资产、备注都不给
    full = {"asof": "2026-01-10", "stale": False, "error": None, "cash": 7777.77, "net_assets": 88888.88,
            "margin_debt": 4321.0, "month_need": 1234.0, "need_month": "2026-02",
            "schedule": [["2026-02", 1234.0], ["2026-03", 1234.0]], "free_cash": -999.99, "free_cash_until": "2026-07",
            "notes": ["合成:银行卡里还有 6666.66 元"],
            "holdings": {"300308": {"shares": 300, "cost": 5555.55, "price": 4444.44}}}
    acc = _ai_package(ai, account=full)["account"]
    ok("给 AI 的 account 只有这几个键:asof、holdings、margin_debt、month_need、need_month、schedule、cash、free_cash、free_cash_until",
       sorted(acc or {}) == sorted(["asof", "holdings", "margin_debt", "month_need", "need_month", "schedule",
                                    "cash", "free_cash", "free_cash_until"]), sorted(acc or {}))
    ok("可取现金和后续缺口照原值给", (acc or {}).get("cash") == 7777.77 and (acc or {}).get("free_cash") == -999.99
       and (acc or {}).get("free_cash_until") == "2026-07", acc)
    ok("持仓每只只给股数", (acc or {}).get("holdings") == {"300308": {"shares": 300}}, (acc or {}).get("holdings"))
    leak = ai.build_prompt(_ai_package(ai, account=full))
    ok("提示词里没有成本、截图价、净资产、备注里的数",
       not any(x in leak for x in ("5555.55", "4444.44", "88888.88", "6666.66")))
    ok("反向:可取现金和缺口确实进了提示词(上一条不是因为整段 account 都没写进去才过的)",
       "7777.77" in leak and "999.99" in leak)
    ok("提示词讲清这个月不够和后面几个月缺口的区别", "cash < month_need" in leak and "free_cash < 0" in leak)
    ok("没有持仓数据 → account 照旧是 null", _ai_package(ai, account=None)["account"] is None)

    # build_prompt
    prompt = ai.build_prompt(pkg)
    ok("提示词:同一份输入包两次生成一字不差", prompt == ai.build_prompt(_ai_package(ai)))
    ok("提示词:模板占位符都填上了", re.search(r"\{\{[A-Z_]+\}\}", prompt) is None)
    ok("提示词:要求文字用大白话、不用自己造的简称(答卷原文会直接上页面)",
       "大白话" in prompt and "自己造的简称" in prompt)
    ok("提示词:两只股票、每个见底月份、later 都在", all(c in prompt for c in codes)
       and all(m in prompt for m in AI_CATS["bottom_months"]) and '"later"' in prompt)
    for need in ("每个数都必须给", "0~100 的整数", "加起来正好 100", "只输出一个 JSON 对象", "建议改规则",
                 "不带方向", "尚未验证"):
        ok(f"提示词写明「{need}」", need in prompt)
    ok("提示词:比值分段旁边写出绝对价(0.7 × 起点 100 = 70.00)", "70.00" in prompt)
    ok("提示词:参考层的价按 start_close 换算(底线起点 200 的 q90=250 → 125.00)", "125.00" in prompt)
    ok("提示词:附输入包全文(含 missing_required、规则)", "gap:海外同业股价" in prompt and "sell_lines" in prompt)
    ok("提示词:讲清规则里每个键的意思(cash_months 留够几个月、sell_order 卖的先后)",
       "cash_months 可取现金要留够" in prompt and "sell_order 卖的先后" in prompt)
    ok("提示词:free_cash_until 写明含这个月、算到那个月为止", "到 free_cash_until 那个月(含这个月)" in prompt)
    import board_forecast as bf  # noqa: E402  只为看 RULE_KEYS
    ok("交给 AI 的规则键里有 cash_months、sell_order(现金留够几个月、卖的先后)",
       {"cash_months", "sell_order", "sell_lines", "sell_ratio"} <= set(bf.RULE_KEYS), bf.RULE_KEYS)
    m = re.search(r"<input_package>\n(.*?)\n</input_package>", prompt, re.S)
    ok("提示词里的输入包是合法 JSON,读回来和输入包一模一样", m is not None and json.loads(m.group(1)) == pkg)

    # run_claude / run_codex:换掉 subprocess.run,查命令行、标准输入、工作目录、环境
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv[0] == "codex":
            Path(argv[argv.index("-o") + 1]).write_text('{"stocks": {}}', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "x", "OpenAI Codex\n--------\nmodel: gpt-fake\n"
                                                              "reasoning effort: xhigh\n--------\n")
        return subprocess.CompletedProcess(argv, 0, '{"stocks": {}}', "")
    wd = Path(tempfile.mkdtemp())
    with patched(ai.subprocess, run=fake_run):
        out_c = ai.run_claude("提示词正文", wd)
        out_x = ai.run_codex("提示词正文", wd)
    argv_c, kw_c = calls[0]
    ok("claude:命令行带齐隔离参数", argv_c[:2] == ["claude", "-p"] and all(
        f in argv_c for f in ("--model", "claude-opus-5-5", "--strict-mcp-config", "--no-session-persistence"))
       and argv_c[argv_c.index("--tools") + 1] == "" and argv_c[argv_c.index("--setting-sources") + 1] == ""
       and argv_c[argv_c.index("--output-format") + 1] == "text", argv_c)
    ok("claude:提示词走标准输入、工作目录是临时目录、环境用 ORIG_ENV、有超时",
       kw_c.get("input") == "提示词正文" and Path(kw_c.get("cwd")) == wd and kw_c.get("env") is fb_common.ORIG_ENV
       and kw_c.get("timeout") == ai.AI_TIMEOUT, {k: v for k, v in kw_c.items() if k != "env"})
    ok("claude:返回原始输出,带模型标识和退出码", out_c == '{"stocks": {}}' and out_c.model == "claude-opus-5-5"
       and out_c.exit_code == 0, (out_c, getattr(out_c, "model", None)))
    argv_x, kw_x = calls[1]
    ok("codex:命令行(只读、不查 git、关网络搜索、-C 临时目录、-o 写 out.md、- 读标准输入)",
       argv_x[:2] == ["codex", "exec"] and "--skip-git-repo-check" in argv_x
       and argv_x[argv_x.index("-s") + 1] == "read-only" and argv_x[argv_x.index("-C") + 1] == str(wd)
       and 'web_search="disabled"' in argv_x and argv_x[argv_x.index("-o") + 1] == str(wd / "out.md")
       and argv_x[-1] == "-", argv_x)
    ok("codex:关掉联网的插件和本机工具(apps、MCP 服务、hooks、命令行、AGENTS.md)",
       all(f in argv_x for f in ("apps", "hooks", "shell_tool", "code_mode_host", "mcp_servers={}",
                                 "project_doc_max_bytes=0")), argv_x)
    ok("codex:提示词走标准输入、环境用 ORIG_ENV、有超时", kw_x.get("input") == "提示词正文"
       and kw_x.get("env") is fb_common.ORIG_ENV and kw_x.get("timeout") == ai.AI_TIMEOUT)
    ok("codex:读 out.md,模型标识取日志头的 model: 行", out_x == '{"stocks": {}}' and out_x.model == "gpt-fake")
    ok("思考深度:codex 取日志头的 reasoning effort: 行,claude 记 default(两边条件不一样要看得到)",
       getattr(out_x, "effort", None) == "xhigh" and getattr(out_c, "effort", None) == "default",
       (getattr(out_x, "effort", None), getattr(out_c, "effort", None)))

    def fail_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, "", "boom")

    def slow_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    def web_run(argv, **kw):
        Path(argv[argv.index("-o") + 1]).write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "",
                                           "model: gpt-fake\nmcp: codex_apps/search_service.web_run started\n")
    with patched(ai.subprocess, run=fail_run):
        e1 = raises(lambda: ai.run_claude("p", wd), ai.RunFailed)
    ok("退出码不是 0 → RunFailed,带退出码", e1 is not None and e1.exit_code == 3 and "boom" in str(e1), e1)
    with patched(ai.subprocess, run=slow_run):
        e2 = raises(lambda: ai.run_codex("p", wd), ai.RunFailed)
    ok("超时 → RunFailed,写明超时", e2 is not None and "超时" in str(e2), e2)
    with patched(ai.subprocess, run=web_run):
        e3 = raises(lambda: ai.run_codex("p", wd), ai.RunFailed)
    ok("codex 日志里出现联网搜索 → RunFailed(两边输入不一样了)", e3 is not None and "联网" in str(e3), e3)

    # answer
    root = Path(tempfile.mkdtemp())
    run = root / "2026-10-30" / "093000"
    run.mkdir(parents=True)
    seen = []

    def junk(p, w):
        seen.append((p, sorted(x.name for x in Path(w).iterdir()), Path(w)))
        return "不是 JSON"
    res = ai.answer("codex", pkg, run, runner=junk)
    ok("两次都不是 JSON → failed,runner 调了 2 次", res.get("failed") is True and len(seen) == 2, res)
    ok("失败答卷只有作者、失败标记、错误,不填任何默认数",
       res["author"] == "codex" and res["errors"] and "stocks" not in res, res)
    ok("失败也存进 answers/", json.loads((run / "answers" / "codex.json").read_text())["failed"] is True)
    ok("工作目录里只有输入包,用完删掉", seen[0][1] == ["package.json"] and not seen[0][2].exists(), seen[0][1:])
    ok("第一次收到的就是 build_prompt 的原文", seen[0][0] == prompt)
    ok("第二次 = 原提示词 + 错误清单", seen[1][0].startswith(prompt) and "没通过检查" in seen[1][0])
    ok("提示词只存一份 ai/prompt.md", (run / "ai" / "prompt.md").read_text(encoding="utf-8") == prompt)
    meta = json.loads((run / "ai" / "codex" / "meta.json").read_text())
    ok("ai/codex/meta.json:提示词哈希、两次尝试的起止时间、错误", meta["prompt_sha256"] == ai.sha256(prompt)
       and len(meta["attempts"]) == 2 and meta["failed"] is True
       and all(a["started_at"] and a["ended_at"] and a["errors"] for a in meta["attempts"]), meta)
    ok("每次原始输出都存了", (run / "ai" / "codex" / "attempt1.out.md").read_text(encoding="utf-8") == "不是 JSON"
       and (run / "ai" / "codex" / "attempt2.out.md").exists())

    tries = []

    def fix_on_second(p, w):
        tries.append(p)
        bad = _put(good, 29, "stocks", "300502", "bottom", "month_probs", "2026-11")
        obj = bad if len(tries) == 1 else good
        out = ai.RunOutput("```json\n" + json.dumps(obj) + "\n```", model="claude-test", exit_code=0,
                           log=f"第 {len(tries)} 次的日志")
        out.effort = "default"
        return out
    got = ai.answer("claude", pkg, run, runner=fix_on_second)
    ok("第一次加和 99、第二次改对 → 成功,调了 2 次", "failed" not in got and len(tries) == 2, got.get("errors"))
    ok("重问时把具体错误附上", "300502" in tries[1] and "99" in tries[1], tries[1][-400:])
    ok("两个 AI 收到的第一份提示词一字不差", tries[0] == seen[0][0])
    ok("成功答卷:作者、模型、as_of 由程序填,stocks 是 AI 的",
       got["author"] == "claude" and got["model"] == "claude-test" and got["as_of"] == "2026-10-30"
       and got["stocks"] == good["stocks"], {k: got.get(k) for k in ("author", "model", "as_of")})
    ok("成功答卷存进 answers/claude.json", json.loads((run / "answers" / "claude.json").read_text()) == got)
    m2 = json.loads((run / "ai" / "claude" / "meta.json").read_text())
    ok("meta 记模型标识、退出码", m2["model"] == "claude-test" and m2["attempts"][0]["exit_code"] == 0, m2)
    ok("meta 记思考深度(effort)", m2.get("effort") == "default" and m2["attempts"][-1].get("effort") == "default", m2)
    ok("每次的命令行日志存成 attemptN.log(查有没有联网用)",
       (run / "ai" / "claude" / "attempt2.log").read_text(encoding="utf-8") == "第 2 次的日志"
       and m2["attempts"][1]["log_file"] == "attempt2.log" and meta["attempts"][0]["log_file"] is None, m2)
    ok("重问的提示词另存 attempt2.prompt.md", (run / "ai" / "claude" / "attempt2.prompt.md").read_text(
        encoding="utf-8") == tries[1])
    ok("同一作者在同一次存档里再答一次 → 拒绝(不覆盖)",
       raises(lambda: ai.answer("claude", pkg, run, runner=fix_on_second), FileExistsError) is not None)
    other = _ai_package(ai, research_notes="多了一段")
    ok("第二个 AI 拿到的输入包不一样 → 拒绝(提示词必须同一份)",
       raises(lambda: ai.answer("x2", other, run, runner=junk), ValueError) is not None)

    def boom(p, w):
        raise ai.RunFailed("claude 超时(900 秒)", exit_code=None)
    run2 = root / "2026-10-30" / "093100"
    run2.mkdir()
    r3 = ai.answer("claude", pkg, run2, runner=boom)
    ok("runner 两次都失败(超时)→ failed,错误写明超时",
       r3.get("failed") is True and "超时" in " ".join(r3["errors"]), r3)
    ok("不认识的作者又没给 runner → 报错", raises(lambda: ai.answer("gemini", pkg, run2), ValueError) is not None)


CHECKS = [check_ai_answer]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
