#!/usr/bin/env python3
"""board_forecast —— board.py 的 inputs / forecast / update 三个子命令。

inputs --check [代码…]   只取数、列数据清单(每项状态、截至哪天、来源、缺什么),不出预测、不写存档。
                         行业必需项取不到、或这只股票没配行业 → 退出码 1;日线用了旧值、前后复权对不上
                         (forecast 会因此不出预测)也算,退出码 1,不说「必需项都取到了」;
                         「行业要看但还没有取数工具」(gap:…)照列,但不算这次取数失败。
forecast                 取数 → 统计底线、系统算法 → 两个 AI → 存档 → 出题 → 决策日志 → 画页面。
update                   刷新收盘价、财报日历和 SEC 新文件、持仓 → 用最新一次存档重画页面,不改预测。
                         刷新的财报日历和 SEC 新文件另存 <存档根>/events.json(整份重写的状态文件,不是存档),
                         每日检查重画页面时接着用 —— 不然一重画就退回存档那天的日历,几周后「未来几周的财报」全是过去的。

forecast 的几条规矩:
  - 交易日 09:15~15:05 不出预测:这段时间日线最后一根是盘中实时价,大盘 60 日线、同行涨跌差也是拿它算的。
  - 本股日线、创业板指日线这次没取到(取不到或沿用旧值)→ 不出预测:拿旧日线算出来的起点价是错的。
  - as_of = 创业板指日线最后一天(最后一个收盘);start = 它之后的第一个交易日。
    分类、两层模拟、出题都用这一个 start。
  - 起点价(start_close)= 前复权最后一根 = 当天的实际收盘;p0_hfq = 后复权最后一根(结算时拿期末的后复权价除以它)。
  - 哪些信号检验过:取 history_since(默认 2010-01-01)以来的长日线做走前检验(输入包里只有近两年,
    样本太少,检验永远过不了);长日线取不到就退回输入包那两年算一份成绩留底,但信号一律算没检验过
    (validated 全是 False),并写明。结果冻结进 run_meta.validated,重算(recompute)只读它,不再检验。
  - 历史同类情形(analogs.json)也用这份长日线算;取不到就不写,页面照实写「这次没算」。
  - 长日线取不到:stderr 打一行原因,输入包里另加一项 history.long(status=failed、used_by=system、
    note 写原因),页面「本次缺什么」照这一项列出来 —— 不然页面只说「这次没算」,看不出是取数失败。
  - 存档里最后写 manifest.json:没写它的目录 archive.list_runs 当「跑到一半崩了」,不会被拿去当最新一次。
  - 同一天(同一个 as_of)之前的存档标成被取代(superseded.json),旧版照样到期核对。
  - 每只股票在决策日志里记一条,复核日 = 这次 3 个月题的到期日;记下的编号写进存档的 journal.json。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import fb_common
import account  # noqa: F401  (inputs 里 c_account 用,先加载好)
import actions
import ai_answer
import analogs
import archive
import attribution
import baseline
import board_data
import board_io
import board_layers
import inputs
import prices
import questions
import system_forecast

# 期限、见底窗口、买点期限是定死的:出题(questions)、AI 提示词和答卷校验(ai_answer)都按这几个常数写。
# 配置里写了别的值,两层模拟按配置算、出题按常数拆,对不上 → 整层没出题。所以配置里只准不写或写成一样的。
FIXED = {"horizons": [int(h) for h in ai_answer.HORIZONS], "bottom_window": ai_answer.BOTTOM_WINDOW,
         "buy_horizon": system_forecast.BUY_HORIZON}
DEFAULTS = {**FIXED, "n_paths": 20000, "lookback": 120}       # 后两项配置里可以改(检查用少一点的路径数)
HISTORY_SINCE = "2010-01-01"
HISTORY_KEY = inputs.HISTORY_KEY
NOTES_KEY = "notes"
EVENT_KEYS = ("events.calendar", "events.sec")
OPEN_AT = (9, 15)                       # 集合竞价开始;这以前日线里没有今天那根
AI_LAYERS = ("claude", "codex")
RULE_KEYS = ("sell_lines", "sell_ratio", "cash_months", "sell_order", "monthly_cash_need_fallback")
JOURNAL_FILE = "journal.json"           # 这次在决策日志里记的编号 {"ids": [...]};日志那边写「预测存档 <目录>」,两边互相指


def _codes(config: dict) -> list[str]:
    return [str(s["code"]) for s in config.get("stocks") or []]


def _prev_items() -> dict | None:
    run = archive.latest_run()
    raw = board_io.read_json(run / "inputs.json") if run else None
    return attribution.items_of(raw) if isinstance(raw, dict) else None


# ── inputs --check ──────────────────────────────────────────────────────────
def config_for(config: dict, codes: list[str]) -> dict:
    """只查这几只:配置里有的照用(带行业),没有的只写代码;同行对只留两边都在的。"""
    if not codes:
        return config
    known = {str(s["code"]): s for s in config.get("stocks") or []}
    peers = [p for p in config.get("peers") or [] if str(p[0]) in codes and str(p[1]) in codes]
    return {**config, "stocks": [known.get(c, {"code": c, "name": c}) for c in codes], "peers": peers}


def print_inputs(items: dict, missing: list[str], codes=()) -> int:
    """数据清单 + 缺什么;返回退出码(真缺、或 forecast 会因日线不出预测 → 1,只缺还没工具的 → 0)。
    codes = 这次查的股票:日线这几项用 price_problems 再判一次,和 forecast 拒绝的条件一模一样。"""
    print(f"数据清单(共 {len(items)} 项):")
    for k in sorted(items):
        it = items[k] if isinstance(items[k], dict) else {}
        st = it.get("status") or "failed"
        print(f"  [{board_data.STATUS_TEXT.get(st, st)}] {board_data.key_text(k)}({k})"
              f" 截至 {it.get('as_of') or '—'} · {board_data.USED_BY_TEXT.get(it.get('used_by'), '只给 AI 参考')}"
              f" · {it.get('source') or '没有来源'}")
        if it.get("note"):
            print(f"      {it['note']}")
    failed = [k for k in missing if not k.startswith(("gap:", "sector."))]
    sector = [k[len("sector."):] for k in missing if k.startswith("sector.")]
    gaps = [k[len("gap:"):] for k in missing if k.startswith("gap:")]
    print("\n缺什么:")
    for k in failed:
        print(f"  取不到(必需):{board_data.key_text(k)}({k})")
    for c in sector:
        print(f"  没配行业:{c}(不知道该看哪些领先指标,不能说什么都不缺)")
    for g in gaps:
        print(f"  还没有取数工具(不算这次取数失败):{g} —— {inputs.TOOL_GAPS.get(g, '')}")
    if not missing:
        print("  什么都不缺")
    refuse = [p for p in price_problems(items, list(codes)) if p.split("(")[0] not in failed]
    for p in refuse:
        print(f"  日线不能拿来出预测(forecast 会拒绝):{p}")
    bad = failed + [f"sector.{c}" for c in sector] + refuse
    print(f"\n{'有 ' + str(len(bad)) + ' 项必需的数据取不到、用了旧值或判断不了' if bad else '必需项都取到了'}")
    return 1 if bad else 0


def cmd_inputs(args) -> int:
    config = config_for(fb_common.load_config(), [str(c) for c in args.check or []])
    items = inputs.collect(config, _prev_items(), collectors=board_io.collectors(config), progress=board_io.progress)
    return print_inputs(items, inputs.missing_required(items, config), _codes(config))


def fixed_conflicts(config: dict) -> list[str]:
    """配置里写了、又和定死的值不一样的那几项 → [「键 = 值(只能是 …)」];都没问题给空列表。"""
    return [f"{k} = {config[k]!r}(只能是 {v!r})" for k, v in FIXED.items() if k in config and config[k] != v]


# ── forecast:输入包 ────────────────────────────────────────────────────────
def in_trading_hours(t, hol: set[str]) -> bool:
    """交易日 09:15 到 15:05 之间:日线最后一根是盘中实时价。节假日是配置里估的。"""
    return (t.weekday() < 5 and t.date().isoformat() not in hol
            and OPEN_AT <= (t.hour, t.minute) < prices.LIVE_CUTOFF)


def trim_live(items: dict, t) -> dict:
    """日线(prices.* 的 hfq / qfq、创业板指的 day)去掉盘中那根(prices.drop_live_bar),返回新字典。
    出预测已经拒绝盘中跑;这里防的是节假日表估错(当天其实开市)。"""
    out = {}
    for k, it in items.items():
        v = it.get("value") if isinstance(it, dict) else None
        if isinstance(v, dict) and (k.startswith("prices.") or k == board_layers.INDEX_KEY):
            new = {f: ([list(b) for b in prices.drop_live_bar([tuple(x) for x in rows], t)]
                       if isinstance(rows, list) else rows) for f, rows in v.items()}
            if new != v:
                last = max((r[-1][0] for r in new.values() if isinstance(r, list) and r), default=it.get("as_of"))
                note = ";".join(x for x in (it.get("note"), "去掉了今天盘中那根") if x)
                it = {**it, "value": new, "as_of": last, "note": note}
        out[k] = it
    return out


def notes_item(path: Path, t) -> dict:
    """查证笔记 → 输入包的一项(只给 AI 参考)。来源只写文件名,不写本机路径。"""
    day = t.date().isoformat()
    return {"key": NOTES_KEY, "value": path.read_text(encoding="utf-8"), "source": f"查证笔记 {path.name}",
            "as_of": day, "public_at": day, "fetched_at": t.isoformat(timespec="seconds"), "status": "ok",
            "used_by": "ai", "note": ""}


def price_problems(items: dict, codes: list[str]) -> list[str]:
    """日线哪几项不能拿来出预测:没取到 / 沿用旧值;或者后复权和前复权最后一天不一样。
    后一种是数据源缓存没更新(2026-11-02 实测腾讯:同一只股票前复权有当天、后复权停在上一个交易日)。
    起点价取前复权最后一根、结算基准取后复权最后一根,两天不一样,结算时就多算或少算了一天的涨跌。
    还有一种:两种复权都停在前一天,而创业板指已经有当天(2026-11-03 实测腾讯:中际旭创收盘两个多小时后
    两种日线都还没有当天这根,这一项照样是 ok)。起点就成了前一天的收盘,当天的涨跌整个被漏掉 ——
    所以每只股票日线的最后一天还要跟创业板指的最后一天一样;停牌也会落到这里,要人看过再跑。"""
    keys = [f"prices.{c}" for c in codes] + [board_layers.INDEX_KEY]
    out = [f"{k}({(items.get(k) or {}).get('status') or '没有这项'})" for k in keys
           if (items.get(k) or {}).get("status") not in ("ok", "partial")]
    idx = board_layers.bars(items, board_layers.INDEX_KEY, "day")
    last_idx = max(idx) if idx else None
    for c in codes:
        hfq, qfq = board_layers.bars(items, f"prices.{c}", "hfq"), board_layers.bars(items, f"prices.{c}", "qfq")
        if hfq and qfq and max(hfq) != max(qfq):
            out.append(f"prices.{c}(后复权最后一天 {max(hfq)}、前复权最后一天 {max(qfq)},对不上,等数据源更新再跑)")
        elif last_idx and hfq and qfq and max(hfq) < last_idx:
            out.append(f"prices.{c}(日线最后一天 {max(hfq)},创业板指已经到 {last_idx}:"
                       f"数据源还没更新这只股票,或者它停牌了,等更新再跑;停牌要人看过再定)")
    return out


# ── forecast:长日线、检验、历史同类情形 ─────────────────────────────────────
def long_history(market, codes: list[str], since: str, as_of: str) -> tuple[dict | None, str]:
    try:
        idx = [(d, c) for d, c in market.index(since) if d <= as_of]
        stocks = {c: {d: p for d, p in market.hfq(c, since).items() if d <= as_of} for c in codes}
    except Exception as e:          # 网络、接口……取不到就退回输入包那两年,写明
        return None, f"{since} 以来的长日线取不到({type(e).__name__}: {str(e)[:120]})"
    if not idx:
        return None, f"{since} 以来的创业板指日线是空的"
    return {"index": idx, "stocks": stocks}, ""


def validation(items: dict, codes: list[str], config: dict, hist: dict | None, note: str) -> tuple[dict, dict]:
    """→ (validated {信号: bool}, 记录 {"source", "stats" 或 "error"})。
    hist 是 None(长日线取不到):照样拿输入包近两年算一份成绩留在记录里,但 validated 一律 False ——
    两年样本太少,算出来「检验过」也不能信,和记录里写的「一律算没检验过」一致。"""
    if hist is None:
        idx = sorted(board_layers.bars(items, board_layers.INDEX_KEY, "day").items())
        stocks = {c: board_layers.bars(items, f"prices.{c}", "hfq") for c in codes}
        src = f"{note};只用输入包里近两年的日线检验,样本太少,一律算没检验过"
    else:
        idx, stocks = hist["index"], hist["stocks"]
        src = f"{idx[0][0]} 到 {idx[-1][0]} 的日线"
    dates, closes = [d for d, _ in idx], [c for _, c in idx]
    series = {c: [stocks[c].get(d) for d in dates] for c in codes}
    pairs = [(str(a), str(b)) for a, b in config.get("peers") or []
             if str(a) in codes and str(b) in codes and str(a) != str(b)]
    try:
        stats = system_forecast.walk_forward_stats(dates, closes, series, pairs=pairs)
        validated = system_forecast.validate_price_signals(dates, closes, series, pairs=pairs)
    except ValueError as e:
        return dict.fromkeys(system_forecast.SIGNAL_WEIGHTS, False), {"source": src, "error": str(e)}
    if hist is None:
        validated = dict.fromkeys(validated, False)
    return validated, {"source": src, "stats": stats}


def history_failed_item(note: str, since: str, t) -> dict:
    """长日线取不到 → 输入包里的一项(status=failed),页面「本次缺什么」照它列出来。"""
    return {"key": HISTORY_KEY, "value": None, "source": f"{since} 以来的后复权日线和创业板指日线", "as_of": None,
            "public_at": None, "fetched_at": t.isoformat(timespec="seconds"), "status": "failed",
            "used_by": inputs.used_by_for(HISTORY_KEY),
            "note": f"{note};信号一律算没检验过,历史同类情形这次没算"}


def _current_episode(ds: list[str], px: list[float], eps: list[dict]) -> tuple[int, float]:
    """现在这次跌破:(跌破后过了几个交易日, 到现在最深离一年高点跌了多少);不在一段里 → (0, 0.0)。"""
    e = eps[-1] if eps and not eps[-1]["complete"] else None
    if e is None:
        return 0, 0.0
    t = ds.index(e["date"])
    high = max(px[max(0, t - 249):t + 1])
    return len(ds) - 1 - t, min(max(1 - min(px[t:]) / high, 0.0), 0.9999)


def analogs_block(hist: dict) -> dict:
    """{代码: summarize_episodes(几只合起来的段, 这只现在的状态)}。"""
    pooled, now = [], {}
    for code, s in hist["stocks"].items():
        ds = sorted(s)
        px = [s[d] for d in ds]
        eps = analogs.drawdown_episodes(ds, px)
        pooled += [{**e, "code": code} for e in eps]
        now[code] = _current_episode(ds, px, eps)
    return {code: analogs.summarize_episodes(pooled, *now[code]) for code in hist["stocks"]}


# ── forecast:拼起来 ────────────────────────────────────────────────────────
def build_meta(items: dict, codes: list[str], config: dict, hol: set[str], validated: dict, vrec: dict) -> dict:
    as_of = max(board_layers.bars(items, board_layers.INDEX_KEY, "day"))
    start = prices.future_trading_days(date.fromisoformat(as_of) + timedelta(days=1), 1, hol)[0]
    sc, p0 = {}, {}
    for c in codes:
        qfq, hfq = board_layers.bars(items, f"prices.{c}", "qfq"), board_layers.bars(items, f"prices.{c}", "hfq")
        sc[c], p0[c] = qfq[max(d for d in qfq if d <= as_of)], hfq[max(d for d in hfq if d <= as_of)]
    meta = {k: config.get(k, v) for k, v in DEFAULTS.items()}
    return {**meta, "as_of": as_of, "start": start.isoformat(), "codes": codes, "holidays": sorted(hol),
            "validated": validated, "validation": vrec, "start_close": sc, "p0_hfq": p0}


def ai_package(items: dict, meta: dict, cats: dict, missing: list, layers: dict, config: dict) -> dict:
    """两个 AI 共用的输入包。查证笔记、持仓走 build_package 自己的两个参数,不在 inputs 里重复一遍。
    长日线那一项(只在取不到时有)不给 AI:它只管系统算法的信号检验,AI 用不上。"""
    acc = items.get("account") or {}
    notes = items.get(NOTES_KEY) or {}
    return ai_answer.build_package(
        meta["as_of"], meta["start"], meta["codes"], meta["start_close"], cats,
        {k: v for k, v in items.items() if k not in (NOTES_KEY, "account", HISTORY_KEY)}, missing,
        layers["baseline"], layers["system"],
        account=acc.get("value") if acc.get("status") in ("ok", "partial") else None,
        rules={k: config[k] for k in RULE_KEYS if k in config}, research_notes=notes.get("value"),
        bottom_window=meta["bottom_window"], buy_horizon=meta["buy_horizon"])


def make_questions(answers: dict, qmeta: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """各层答卷 → (题目, 没出题的买入价位, 整层没出题的 [{"layer", "why"}])。
    某层拆不出题(答卷过了校验但题号撞了之类)不让整次存档失败,但要单独报出来:
    每一层都必须给概率,一层一道题都没有就等于这一层这次没预测。作答失败的层另有失败记录,不算在这里。"""
    qs, skipped, empty = [], [], []
    for name, ans in answers.items():
        try:
            got = questions.build_questions(ans, qmeta)
        except ValueError as e:
            empty.append({"layer": name, "why": str(e)})
            continue
        qs += got["questions"]
        skipped += got["skipped"]
    return qs + questions.rule_questions(qmeta), skipped, empty


def supersede_same_day(run: Path) -> list[Path]:
    out = []
    for old in archive.list_runs():
        if old != run and old.parent.name == run.parent.name and not (old / "superseded.json").exists():
            archive.supersede(old, run, "同一天又出了一版预测,首页改用新版;旧版照样到期核对")
            out.append(old)
    return out


def _answer_line(name: str, ans: dict | None) -> str:
    if ans is None:
        return f"  {name}:这次没让它答"
    if ans.get("failed"):
        return f"  {name}:作答失败 —— " + ";".join(str(e) for e in ans.get("errors") or [])[:400]
    return f"  {name}:合格(模型 {ans.get('model')})"


def cmd_forecast(args) -> int:
    t = board_io.now()
    today = t.date()
    config = fb_common.load_config()
    hol = actions.config_holidays(config)
    if in_trading_hours(t, hol):
        print("现在是交易日盘中(09:15~15:05),日线最后一根是实时价,不能当收盘出预测;15:05 以后再跑。", file=sys.stderr)
        return 2
    bad = fixed_conflicts(config)
    if bad:
        print("配置 board.json 里这几项是定死的(出题、AI 提示词按固定值写,改了两边对不上),不出预测:"
              + "、".join(bad) + "。删掉这几项,或者改回固定值。", file=sys.stderr)
        return 2
    codes = _codes(config)
    notes = Path(args.notes) if args.notes else None
    if not codes or (notes is not None and not notes.is_file()):
        print("配置里没有股票" if not codes else f"查证笔记 {notes} 不在", file=sys.stderr)
        return 2
    items = inputs.collect(config, _prev_items(), collectors=board_io.collectors(config), progress=board_io.progress)
    items = trim_live(items, t)
    if notes is not None:
        items[NOTES_KEY] = notes_item(notes, t)
    bad = price_problems(items, codes)
    if bad:
        print(f"日线这次没取到或对不上,不出预测(拿旧日线算的起点价是错的):{'、'.join(bad)}", file=sys.stderr)
        return 1
    missing = inputs.missing_required(items, config)
    as_of = max(board_layers.bars(items, board_layers.INDEX_KEY, "day"))
    board_io.progress("取长日线,检验信号、算历史同类情形 …")
    since = str(config.get("history_since") or HISTORY_SINCE)
    hist, hnote = long_history(board_io.Market(t), codes, since, as_of)
    if hist is None:
        print(f"⚠ {hnote}:信号一律算没检验过,历史同类情形这次没算", file=sys.stderr)
    validated, vrec = validation(items, codes, config, hist, hnote)
    meta = build_meta(items, codes, config, hol, validated, vrec)
    seed = args.seed if args.seed is not None else int(config.get("seed") or as_of.replace("-", ""))
    board_io.progress("算统计底线和系统算法 …")
    layers = board_layers.compute_layers(items, meta, seed)
    cats = baseline.categories(date.fromisoformat(meta["start"]), hol, meta["bottom_window"], meta["buy_horizon"])

    if hist is None:
        items = {**items, HISTORY_KEY: history_failed_item(hnote, since, t)}
    run = archive.new_run(as_of)
    archive.save_inputs(run, {"items": items, "run_meta": meta, "missing_required": missing})
    answers = {}
    for name in ("baseline", "system"):
        answers[name] = {"author": name, "as_of": as_of, "stocks": layers[name]}
        archive.save_answer(run, name, answers[name])
    pkg = ai_package(items, meta, cats, missing, layers, config)
    for author in AI_LAYERS if not args.no_codex else AI_LAYERS[:1]:
        board_io.progress(f"{author} 作答 …")
        answers[author] = ai_answer.answer(author, pkg, run, runner=board_io.ai_runner(author))
    if hist is not None:
        archive.write_json_once(run / "analogs.json", analogs_block(hist))
    qs, skipped, empty = make_questions(answers, {**meta, "run": board_io.run_rel(run), "categories": cats})
    archive.write_json_once(run / "questions.json", {"questions": qs, "skipped": skipped,
                                                     "layers_without_questions": empty})
    archive.write_manifest(run, config, seed)            # 最后写:有了它这次存档才算完整
    return _finish(run, config, today, codes, hol, meta, answers, (qs, skipped, empty))


def _finish(run, config, today, codes, hol, meta, answers, made) -> int:
    qs, skipped, empty = made
    rc = 0
    print(f"存档:{run}")
    print(f"  as_of {meta['as_of']},起点 {meta['start']};统计底线、系统算法已算")
    for a in AI_LAYERS:
        print(_answer_line(a, answers.get(a)))
        rc = rc or int(bool((answers.get(a) or {}).get("failed")))
    print(f"  题目 {len(qs)} 道;没出题的买入价位 {len(skipped)} 处(见 questions.json 的 skipped)")
    for e in empty:
        print(f"❌ {e['layer']} 整层没出题(这一层这次等于没给概率,答卷照存):{e['why']}", file=sys.stderr)
        rc = 1
    for old in supersede_same_day(run):
        print(f"  同一天的旧版 {board_io.run_rel(old)} 标成被取代")
    try:
        ids = archive.link_journal(run, codes, meta["start_close"], hol, start=date.fromisoformat(meta["start"]))
        archive.write_json_once(run / JOURNAL_FILE, {"ids": ids})
        print("决策日志:" + " ".join(f"#{i}" for i in ids))
    except (archive.JournalLinkError, KeyError) as e:
        if isinstance(e, archive.JournalLinkError):       # 写了一半:写进去的那几条也要能从存档找回来
            archive.write_json_once(run / JOURNAL_FILE, {"ids": e.written_ids, "error": str(e)})
        print(f"决策日志没写全:{e}", file=sys.stderr)
        rc = 1
    try:
        for p in board_io.write_pages(run, config, today):
            print(f"页面:{p}")
    except Exception as e:          # 存档已经写好,页面没画成要说清楚,不能当没事
        print(f"页面没画成:{type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    return rc


# ── update ──────────────────────────────────────────────────────────────────
def cmd_update(args) -> int:
    t = board_io.now()
    config = fb_common.load_config()
    run = archive.latest_run()
    if run is None:
        print("还没有存档:先跑 board.py forecast", file=sys.stderr)
        return 1
    closes, errors, notes = board_io.latest_closes(board_io.Market(t), _codes(config), t.date())
    prev = attribution.items_of(board_io.read_json(run / "inputs.json") or {})
    events = inputs.collect(config, prev, collectors=board_io.collectors(config, keys=EVENT_KEYS),
                            progress=board_io.progress)
    # 取不到的那只页面上用存档那天的收盘算动作:顶栏要写出来,不能只打在 stderr
    notices = [f"这次刷新没取到:{board_io.failure_text(e)},页面和动作用的是存档那天的收盘" for e in errors]
    notices += [f"{n['code']} {n['what']},页面和动作用的是存档那天的收盘" for n in notes]       # 停牌,不是取数失败
    for line in notices:
        print(f"⚠ {line}", file=sys.stderr)
    for k, it in sorted(events.items()):
        print(f"{board_data.key_text(k)}:{board_data.STATUS_TEXT.get(it['status'], it['status'])}")
    if events:
        board_io.write_json_atomic(fb_common.archive_root() / board_data.EVENTS_FILE,
                                   {"saved_at": t.isoformat(timespec="seconds"), "run": board_io.run_rel(run),
                                    "items": events})
    for c, (d, px) in sorted(closes.items()):
        print(f"{c} 收盘 {px:g}({d})")
    for p in board_io.write_pages(run, config, t.date(), closes=closes, events=events, notices=notices):
        print(f"页面:{p}")
    print(f"预测没改,用的还是 {board_io.run_rel(run)} 那次存档")
    return 1 if errors else 0
