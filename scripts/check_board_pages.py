#!/usr/bin/env python3
"""check_board_pages —— 预测看板「图形和页面」(svg.py、board_data.py、render.py、templates/)的检查。

用法:
    python3 scripts/check_board_pages.py        # 退出码 0 才算过

不联网、不碰真实数据:
  - 页面用 scripts/fixtures/board/board_data.json(build_board_data 返回的那种 dict,合成数字);
  - build_board_data 在临时目录里现搭一个工作台(make_lab:一次存档、四份答卷、输入包、题目、
    结算记录、原因确认、要改的地方、决策日志、季度回测),跑完删掉。
fixture 怎么来的:make_lab(rich=True) + 注入合成持仓,跑 build_board_data 存下来
(python3 scripts/check_board_pages.py --write-fixture)。改了数据结构要重新生成,
check_build 会比对两边的键,对不上就不过。
写法和 check_board_engine.py 一样:每块一个函数,登记进 CHECKS。
"""
import json
import math
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FB = ROOT / "skills" / "forecast-board"
FIX = ROOT / "scripts" / "fixtures" / "board" / "board_data.json"
sys.path.insert(0, str(FB / "scripts"))

import fb_common  # noqa: E402,F401  先于 prices / baseline,和正式运行的顺序一样
import archive  # noqa: E402
import attribution  # noqa: E402
import baseline  # noqa: E402
import board_data  # noqa: E402
import questions  # noqa: E402
import system_forecast  # noqa: E402
import svg  # noqa: E402
import render  # noqa: E402

FAILS = 0
TODAY = date(2026, 11, 2)
AS_OF = "2026-10-30"
HOL = ["2027-01-01", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12"]
CODES = ("300502", "300308")          # 300502 排前面:它是空仓那只,页面上它的块在前
CLOSE = {"300308": 330.0, "300502": 150.0}
HFQ_K = {"300308": 1.8, "300502": 2.4}


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:400]))
    if not cond:
        FAILS += 1


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return str(e) or type(e).__name__
    return None


def load_fixture() -> dict:
    return json.loads(FIX.read_text(encoding="utf-8"))


# ── 合成工作台 ──────────────────────────────────────────────────────────────
def _days_back(end: date, n: int) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _series(n: int, last: float, period: float, amp: float, slope: float) -> list[float]:
    return [round(last * math.exp(amp * math.sin((i - n + 1) / period) + slope * (i - n + 1)), 2) for i in range(n)]


def _item(key, value, used_by, status="ok", note="", as_of=AS_OF, source="合成数据"):
    return {"key": key, "value": value, "source": source, "as_of": as_of, "public_at": as_of,
            "fetched_at": f"{AS_OF}T15:20:00+08:00", "status": status, "used_by": used_by, "note": note}


def _items(dates, qfq, idx):
    it = {f"prices.{c}": _item(f"prices.{c}", {"hfq": [[d, round(x * HFQ_K[c], 2)] for d, x in zip(dates, qfq[c])],
                                               "qfq": [[d, x] for d, x in zip(dates, qfq[c])]}, "system")
          for c in CODES}
    it["index.sz399006"] = _item("index.sz399006", {"day": [[d, x] for d, x in zip(dates, idx)]}, "system")
    it["market.regime"] = _item("market.regime", {"index": "sz399006", "close": idx[-1], "ma60": round(sum(idx[-60:]) / 60, 2),
                                                  "state": "below", "days": 12}, "system")
    it["market.margin"] = _item("market.margin", {"balance_yi": 24000.5, "chg20_pct": -1.2}, "ai")
    it["peer.300308.300502"] = _item("peer.300308.300502", {"gap10": 3.0, "gap20": 9.0}, "system")
    for c in CODES:
        it[f"consensus.{c}"] = _item(f"consensus.{c}", {"profit": {"2026": {"n": 12, "min": 80.0, "mean": 95.0, "max": 110.0},
                                                                   "2027": {"n": 10, "min": 120.0, "mean": 140.0, "max": 160.0}},
                                                        "ratings": None, "rating_failed_days": 0}, "system",
                                     source="合成:同花顺盈利预测")
        it[f"eps_revisions.{c}"] = _item(f"eps_revisions.{c}", {"up30": 1, "down30": 3, "fy": 2027,
                                                                "eps_basis_skipped30": 0, "unread30": 0}, "system")
        it[f"valuation.{c}"] = _item(f"valuation.{c}", {"pe_pctl": 35.0, "pb_pctl": None, "dyn_pe": 28.5}, "system",
                                     status="partial", note="缺 pb_pctl")
    it["capex"] = _item("capex", {"total": [{"q": "2026-06-30", "sum": 100.0, "yoy": 60.0, "cash_yoy": 55.0},
                                            {"q": "2026-09-30", "sum": 110.0, "yoy": 70.0, "cash_yoy": 66.0}],
                                  "guidance": ["MSFT 2026-10-29:合成指引,下一年资本开支继续增加"], "gaps": [], "failed": []},
                        "system", as_of="2026-09-30")
    it["taiwan"] = _item("taiwan", {"ym": "2026-09", "expected": 2, "got": 2, "companies": [
        {"code": "9001", "name": "合成光甲", "ym": "2026-09", "rev_k_twd": 1000.0, "rev_last_year_k_twd": 800.0,
         "yoy": 25.0, "why": "合成"},
        {"code": "9002", "name": "合成光乙", "ym": "2026-09", "rev_k_twd": 500.0, "rev_last_year_k_twd": 550.0,
         "yoy": -9.1, "why": "合成"}]}, "ai", as_of="2026-09")
    it["sectors"] = _item("sectors", {"159915": {"name": "合成创业板 ETF", "m1": -3.0, "m3": 5.5, "dd_1y": -12.0},
                                      "515880": {"name": "合成通信 ETF", "m1": 2.0, "m3": -4.0, "dd_1y": -20.0}}, "display")
    it["events.calendar"] = _item("events.calendar", [{"date_us": "2026-11-18", "symbol": "NVDA", "name": "合成英伟达",
                                                       "time": "time-after-hours", "beijing": "2026-11-19 05:00",
                                                       "relation": "下游:GPU"}], "ai")
    it["events.sec"] = _item("events.sec", [{"ticker": "AMZN", "date": "2026-10-31", "form": "10-Q", "doc": "a.htm",
                                             "url": "https://www.sec.gov/Archives/a.htm",
                                             "capex_sentences": ["合成摘句一:2027 年资本开支会明显增加。",
                                                                 "合成摘句二:下一年的设备采购会继续增加。"],
                                             "read": ["a.htm"]}],
                             "ai", status="partial", note="1 份正文没读到:META 8-K")
    it["account"] = _item("account", {"note": "合成"}, "ai")
    return it


def _ai_block(code, cats, p63, kind, why):
    c = CLOSE[code]
    n_m = len(cats["bottom_months"])
    bm = baseline.largest_remainder([3] + [2] * (n_m - 1))
    buy = baseline.largest_remainder([1] * len(cats["buy_months"]))
    return {"horizons": {h: {"p_up": p, "q": {"10": round(c * a, 2), "25": round(c * b, 2), "50": round(c * m, 2),
                                              "75": round(c * d, 2), "90": round(c * e, 2)}}
                         for h, p, (a, b, m, d, e) in (("63", p63, (0.78, 0.88, 0.97, 1.08, 1.2)),
                                                        ("126", p63 + 3, (0.7, 0.84, 0.99, 1.15, 1.32)),
                                                        ("250", p63 + 6, (0.6, 0.8, 1.02, 1.25, 1.5)))},
            "bottom": {"month_probs": dict(zip(cats["bottom_months"], bm)),
                       "price_bins": [[lo, hi, p] for (lo, hi), p in zip(cats["price_bins"], (2, 5, 10, 23, 35, 25))]},
            "buy": {"month_probs": dict(zip(cats["buy_months"], buy)),
                    "zones": [[round(c * 0.95, 2), 55], [round(c * 0.88, 2), 35], [round(c * 0.8, 2), 15]],
                    "conditions": ["合成:券商盈利预测近 30 天不再调低"]},
            "action": {"kind": kind, "shares": None, "amount": None, "deadline": "2026-11-06", "why": why,
                       "change_if": [{"if": f"合成:收盘跌破 {round(c * 0.9)}", "then": "卖出"}]},
            "evidence": {"support": ["合成:云厂资本开支同比 70%(capex)"], "against": ["合成:研报调低 3 篇(eps_revisions)"],
                         "unverified": ["合成:台湾月营收只看了两家(taiwan)"]}}


def _judgment(code, holding, rebuy, industry="watch"):
    """合成的「怎么看」(judgment.py 的格式):三件事的选项由参数给,其余固定。"""
    c = CLOSE[code]
    return {"fact": "合成:已知云厂资本开支同比 70%", "view": f"合成:据此判断 {code} 这轮跌主要是情绪",
            "holding": {"pick": holding, "answer": f"合成:{holding}", "when": "合成条件甲", "why": "合成理由甲"},
            "rebuy": {"pick": rebuy, "answer": f"合成:{rebuy}", "when": "合成条件乙", "why": "合成理由乙"},
            "industry": {"pick": industry, "answer": f"合成:{industry}", "when": "合成条件丙", "why": "合成理由丙"},
            "scenarios": [{"label": "A", "p": 30, "what": "合成情况甲", "low_when": "11 月", "low_price": [round(c * 0.95), round(c)],
                           "how": "合成估法"},
                          {"label": "B", "p": 45, "what": "合成情况乙", "low_when": "12 月~1 月", "low_price": round(c * 0.85),
                           "how": "合成估法"},
                          {"label": "C", "p": 25, "what": "合成情况丙", "low_when": "2 月~3 月", "low_price": round(c * 0.7),
                           "how": "合成估法"}],
            "change_if": "合成:财报指引超预期但股价两周内仍创新低,会削弱「情绪为主」的判断,要重新检查",
            "key_dates": [{"date": "2026-11-18", "what": "合成:英伟达财报", "why": "看是不是 A"},
                          {"date": "2027-01", "what": "合成:云厂开支计划", "why": "分出 B 还是 C"}],
            "facts": [{"kind": "需求", "text": "合成:云厂资本开支同比 70%", "source": "capex"},
                      {"kind": "公司", "text": "合成:研报调低 3 篇", "source": "eps_revisions"},
                      {"kind": "估值", "text": "合成:现价约 8 倍市盈率", "source": f"prices.{code}"}],
            "review": "2027-03-31"}


def _forecast_layers(dates, qfq, idx, items, start, cats):
    hol = set(HOL)
    base, sysl = {}, {}
    for c in CODES:
        hfq = [x * HFQ_K[c] for x in qfq[c]]
        b = baseline.baseline_forecast(hfq, start, hol, n_paths=2000, seed=7)
        base[c] = {**b, "buy": system_forecast.baseline_buy(hfq, idx, start, hol, seed=7, n_paths=2000)}
        sig = system_forecast.signals_from_inputs(items, c)
        sysl[c] = system_forecast.system_forecast(hfq, idx, start, hol, sig, {}, seed=7, n_paths=2000)
    acts = {"300502": (("wait", "合成:手里没有,先等"), ("buy_batches", "合成:分两批买")),
            "300308": (("hold", "合成:继续拿着"), ("trim", "合成:先减一点"))}
    p63 = {"300502": (40, 58), "300308": (52, 49)}
    # 「怎么看」:codex 在 300502 选买、在 300308 选减一部分,和 Claude 不同(页面要写出 codex 不同意)
    picks = {"claude": {"300502": ("none", "wait"), "300308": ("hold", "wait")},
             "codex": {"300502": ("none", "buy"), "300308": ("trim", "wait")}}
    claude = {c: {**_ai_block(c, cats, p63[c][0], *acts[c][0]), "judgment": _judgment(c, *picks["claude"][c])} for c in CODES}
    codex = {c: {**_ai_block(c, cats, p63[c][1], *acts[c][1]), "judgment": _judgment(c, *picks["codex"][c])} for c in CODES}
    return {"baseline": {"author": "baseline", "as_of": AS_OF, "stocks": base},
            "system": {"author": "system", "as_of": AS_OF, "stocks": sysl},
            "claude": {"author": "claude", "model": "合成模型", "as_of": AS_OF, "stocks": claude},
            "codex": {"author": "codex", "model": "合成模型", "as_of": AS_OF, "stocks": codex}}


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _old_run_outcomes(root: Path) -> list[dict]:
    """一次早一点的存档(07-31,两版,早的被取代)和它已结算的几道题。"""
    old = archive.new_run("2026-07-31", now=datetime(2026, 7, 31, 15, 0, 0), root=root)
    new = archive.new_run("2026-07-31", now=datetime(2026, 7, 31, 16, 0, 0), root=root)
    for r, head in ((old, "1111111aaaa"), (new, "2222222bbbb")):
        archive.write_json_once(r / "manifest.json", {"created_at": "2026-07-31T15:00:00+08:00", "git_head": head,
                                                      "clean": True, "seed": 7, "config": {}})
    archive.supersede(old, new, "合成:同一天改了一版")
    archive.save_answer(new, "claude", {"author": "claude", "as_of": "2026-07-31", "stocks": {"300308": {
        "action": {"kind": "hold", "why": "合成理由:订单还在涨"},
        "evidence": {"support": ["合成依据:云厂资本开支同比 60%"], "against": [], "unverified": []}}}})
    archive.save_answer(new, "baseline", {"author": "baseline", "as_of": "2026-07-31",
                                          "stocks": {"300308": {"start_price": 180.0}}})
    archive.save_answer(new, "system", {"author": "system", "as_of": "2026-07-31", "stocks": {"300308": {
        "tilt": -0.05, "signals": {"values": {"regime": -1, "peer": 0, "consensus": 0, "capex": 1, "valuation": 0}}}}})
    run = f"{new.parent.name}/{new.name}"
    recs = []
    for i, (author, c, pct, out) in enumerate([("baseline", "300308", 50, 1), ("system", "300308", 62, 0),
                                               ("claude", "300308", 70, 0), ("codex", "300308", 45, 0),
                                               ("baseline", "300502", 50, 0), ("claude", "300502", 30, 0)]):
        q = questions.make_question(run, author, c, "up", "2026-07-31", 100.0, {"n": 63}, {"pct": pct})
        res = {"outcome": out, "end_day": "2026-10-30", "scorable_after": "2026-10-30", "resolved_at": "2026-10-30T16:00:00+08:00",
               "value": 1.05 if out else 0.9, "note": "合成"}
        recs.append({**q, "resolution": res})
    iv = questions.make_question(run, "claude", "300308", "interval", "2026-07-31", 100.0, {"n": 63, "lo": "10", "hi": "90"},
                                 {"lo": 0.95, "hi": 1.2, "pct": 80})
    recs.append({**iv, "resolution": {"outcome": 0, "end_day": "2026-10-30", "scorable_after": "2026-10-30",
                                      "resolved_at": "x", "value": 0.9, "note": "合成"}})
    recs[2]["suggest"] = {"luck": 30, "system": 20, "ai": 10, "data_gap": 25, "data_error": 5, "impl_error": 5,
                          "unforeseeable": 5}
    recs[0]["resolution"].update(index_ratio=1.02, stock_ratio=1.05)       # 扣掉同期大盘:+3 个百分点
    chat = questions.make_question("chat/2026-07-31", "claude", "300502", "above_on", "2026-07-31", 100.0,
                                   {"ratio": 1.1, "day": "2026-10-30"}, {"pct": 35})
    chat.update(source="chat", note="合成:聊天里说的")
    recs.append({**chat, "resolution": {"outcome": 0, "end_day": "2026-10-30", "scorable_after": "2026-10-30",
                                        "resolved_at": "x", "value": 0.9, "note": "合成"}})
    old = questions.make_question("journal/2026-06-01", "claude", "300308", "above_on", "2026-06-01", 90.0,
                                  {"ratio": 0.9, "day": "2026-07-31"}, {"pct": 85})        # 换算进来的上线前旧记录
    old.update(source="journal", note="合成:由旧记录 #9 换算")
    recs.append({**old, "resolution": {"outcome": 1, "end_day": "2026-07-31", "scorable_after": "2026-07-31",
                                       "resolved_at": "x", "value": 1.1, "note": "合成"}})
    return recs


REPLAY_ROW = {"at": "2026-11-01T10:00:00+08:00", "run": "2026-07-31/160000", "code_ref": "abc1234",
              "commit": "abc1234" + "0" * 33, "frozen": False, "corrected": "修正.json", "changed": ["capex"],
              "compare": {"baseline": {"300308": {"p_up": {"63": 0, "126": 0, "250": 0}, "bottom_months": {}}},
                          "system": {"300308": {"p_up": {"63": 2, "126": 0, "250": -1},
                                                "bottom_months": {"2026-08": 5, "2026-09": -5}}}}}


def make_lab(tmp: Path, rich: bool = False, backtest: bool = False, status: dict | None = None,
             codex_failed: bool = False) -> tuple[Path, dict]:
    """在 tmp 下搭一个工作台,返回 (这次存档的目录, 配置)。"""
    root = tmp / "data" / "forecasts"
    dates = _days_back(date.fromisoformat(AS_OF), 500)
    qfq = {"300308": _series(500, CLOSE["300308"], 35.0, 0.2, 0.0008),
           "300502": _series(500, CLOSE["300502"], 28.0, 0.25, -0.0004)}
    idx = _series(500, 2000.0, 50.0, 0.1, 0.0)
    items = _items(dates, qfq, idx)
    start = date(2026, 11, 2)
    cats = baseline.categories(start, set(HOL))
    layers = _forecast_layers(dates, qfq, idx, items, start, cats)
    if codex_failed:
        layers["codex"] = {"author": "codex", "failed": True, "errors": ["第 1 次:合成错误", "第 2 次:合成错误"]}
    run = archive.new_run(AS_OF, now=datetime(2026, 10, 30, 15, 40, 0), root=root)
    rel = f"{run.parent.name}/{run.name}"
    p0 = {c: round(qfq[c][-1] * HFQ_K[c], 2) for c in CODES}
    meta = {"as_of": AS_OF, "start": start.isoformat(), "codes": list(CODES), "holidays": HOL, "horizons": [63, 126, 250],
            "bottom_window": 126, "buy_horizon": 250, "n_paths": 2000, "lookback": 120, "validated": {},
            "start_close": dict(CLOSE), "p0_hfq": p0}
    archive.write_json_once(run / "manifest.json", {"created_at": "2026-10-30T15:40:00+08:00", "git_head": "3333333cccc",
                                                    "clean": True, "seed": 7, "config": {}})
    archive.save_inputs(run, {"items": items, "run_meta": meta,
                              "missing_required": ["gap:海外同业股价", "gap:券商研报里的出货/产能"]})
    for author, ans in layers.items():
        archive.save_answer(run, author, ans)
    qmeta = {**meta, "run": rel, "categories": cats}
    qs = [q for a in layers.values() for q in questions.build_questions(a, qmeta)["questions"]]
    archive.write_json_once(run / "questions.json", {"questions": qs + questions.rule_questions(qmeta)})
    if status is not None:
        archive.write_json_once(root / "status.json", status)
    if rich:
        recs = _old_run_outcomes(root)
        _write_jsonl(root / "outcomes.jsonl", recs)
        waiting = questions.make_question("chat/2026-10-30", "codex", "300308", "touch", AS_OF, 594.0,
                                          {"ratio": 0.8, "n": 63}, {"pct": 30})      # 还没结算的聊天题
        _write_jsonl(root / "chat_questions.jsonl", [{k: v for k, v in r.items() if k != "resolution"} for r in recs[-2:]]
                     + [{**waiting, "source": "chat", "note": "合成"}])
        _write_jsonl(root / "replays.jsonl", [REPLAY_ROW])
        attribution.confirm(recs[1]["id"], {"luck": 40, "system": 30, "data_gap": 30}, "user", "合成说明",
                            ["合成证据"], root=root)
        a = attribution.add_improvement("system", "skills/forecast-board/scripts/system_forecast.py",
                                        "合成:信号阈值偏松", root=root)
        attribution.add_improvement("data_gap", "skills/data-sources/scripts/fetch/capex.py", "合成:少取一家", root=root)
        attribution.mark_improved(a["id"], "abc1234", root=root)
        _write_jsonl(tmp / "data" / "journal.jsonl", [
            {"id": 1, "logged_at": "2026-08-01T10:00:00", "code": "300308", "price": 300.0, "action": "hold",
             "probs": {"bear": 0.3, "base": 0.5, "bull": 0.2}, "thesis": "合成:看好下季度", "falsify": "合成:收盘跌破 280",
             "check_date": "2026-10-31", "deadline": None, "executed": None, "status": "open"},
            {"id": 2, "logged_at": "2026-08-02T10:00:00", "code": "300502", "price": 160.0, "action": "trim",
             "thesis": "合成:超仓先减", "check_date": "2026-12-31", "deadline": "2026-08-09",
             "executed": {"result": "done", "at": "2026-08-05T10:00:00", "note": "合成:减了 100 股"}, "status": "open"},
            {"id": 3, "logged_at": "2026-10-30T16:00:00", "code": "300308", "price": 330.0, "action": "watch",
             "thesis": f"预测存档 data/forecasts/{rel}", "check_date": "2027-02-03", "status": "open"}])
    if backtest:
        bt = tmp / "data" / "backtest"
        bt.mkdir(parents=True, exist_ok=True)
        (bt / "revenue-summary.json").write_text(json.dumps({"生成于": "2026-08-20", "逐季": [
            {"code": "300308", "name": "合成甲", "target": q, "real": r, "band": {"low": lo, "high": hi, "mid": m}}
            for q, r, lo, hi, m in (("2025Q4", 5.2e9, 4.0e9, 5.0e9, 4.5e9), ("2026Q1", 6.1e9, 5.5e9, 6.8e9, 6.0e9))]
            + [{"code": "300502", "name": "合成乙", "target": "2026Q1", "real": 3.0e9, "band": None}]},
            ensure_ascii=False), encoding="utf-8")
    config = {"stocks": [{"code": "300502", "name": "合成乙", "sector": "AI算力链"},
                         {"code": "300308", "name": "合成甲", "sector": "AI算力链"}],
              "peers": [["300308", "300502"]], "sell_lines": {"300308": 320.0},
              "sell_ratio": {"300308": 2, "300502": 1}, "holidays_2027": HOL}
    if rich:
        config["key_dates"] = [{"date": "2027-01 底~02 初", "what": "四家云厂发四季报,给下一年资本开支计划"},
                               {"date": "2027-01 底前", "what": "创业板公司发年度业绩预告"}]
    return run, config


SYN_STATE = {"asof": "2026-10-28", "holdings": {"300308": {"shares": 300, "cost": 400.0}}, "cash": 2000.0,
             "margin_debt": 0.0, "stale": False, "schedule": [["2026-11", 3000.0], ["2026-12", 3000.0]],
             "month_need": 3000.0, "need_month": "2026-11", "free_cash": -4000.0, "free_cash_until": "2027-05",
             "error": None, "notes": []}


def build_rich(tmp: Path) -> dict:
    run, cfg = make_lab(tmp, rich=True, backtest=True)
    return render.build_board_data(run, cfg, TODAY, state=SYN_STATE)


# ── 检查 ────────────────────────────────────────────────────────────────────
def check_svg():
    ia = svg.icon_array(40, "3 个月")
    cls = re.findall(r'<rect class="(c-up|c-no)"', ia)
    ok("百格图 100 格", len(cls) == 100, len(cls))
    ok("按行从左上开始先填「涨」", cls[:40] == ["c-up"] * 40 and cls[40:] == ["c-no"] * 60, cls[:45])
    ok("百格图带 title 和图上的数字", "<title>" in ia and "涨 40" in ia and "不涨 60" in ia, ia[:300])
    ok("百格图 101 / -1 / True 报错", all(raises(lambda v=v: svg.icon_array(v, "x"), ValueError) for v in (101, -1, True)))
    hist = [(f"2026-10-{d:02d}", 100.0 + d) for d in range(1, 11)]
    bands = {h: {"10": 80.0 * f, "25": 90.0 * f, "50": 110.0, "75": 120.0 / f, "90": 135.0 / f}
             for h, f in (("63", 1.0), ("126", 0.95), ("250", 0.9))}
    marks = {"system": {"63": {"10": 85, "25": 95, "50": 105, "75": 115, "90": 125}},
             "claude": {h: {"10": 82, "25": 92, "50": 104, "75": 118, "90": 130} for h in ("63", "126", "250")}}
    fan = svg.fan_chart(hist, bands, marks, {"cost": 95.0, "sell_line": 88.0})
    ok("扇形图:只有一条折线(已发生的),分位点不连线", fan.count("<polyline") == 1, fan.count("<polyline"))
    ok("扇形图:两层扇形 10~90、25~75", 'class="fan80"' in fan and 'class="fan50"' in fan)
    ok("扇形图:每层每个期限一个标记(1 + 3 = 4)", fan.count('class="mk ') == 4, fan.count('class="mk '))
    ok("扇形图:成本线、卖出线是虚线并写了数", "stroke-dasharray" in fan and "成本 95" in fan and "卖出线 88" in fan)
    ok("扇形图带 title", "<title>" in fan and "不带方向" in fan)
    mb = svg.month_bars({"2026-11": 30, "2026-12": 70})
    ok("按月条形:写了种数", "30 种" in mb and "70 种" in mb and "<title>" in mb and "11 月" in mb, mb[:200])
    lt = svg.month_bars({"2026-11": 40, "later": 60})
    ok("按月条形:later 有中文名", "later" not in re.sub(r"<title>.*?</title>", "", lt) and "60 种" in lt)
    bb = svg.bin_bars([[0.0, 100.0, 10], [100.0, 120.0, 90]])
    ok("价位条形:最低一段写「以下」", "100 以下" in bb and "100~120" in bb and "90 种" in bb, bb[:300])
    hb = svg.hbars([("甲", -3.5), ("乙", 2.0)], unit="%")
    ok("横条:正负都写", "-3.5%" in hb and "+2.0%" in hb and "<title>" in hb, hb[:300])
    rd = svg.range_dots([("2025Q4", 40.0, 50.0, 45.0, 52.0)], unit="亿")
    ok("季度区间图:空心横条、空心点、实心点、实际值", 'class="band"' in rd and 'class="mid"' in rd
       and 'class="real"' in rd and "52" in rd, rd[:300])
    ok("季度区间图:预测区间和中枢的数也写在图上", "预测 40~50" in rd and "中枢 45" in rd, rd)
    cc = svg.calib_chart({"claude": [[25, 0.3, 10], [75, 0.6, 12]]})
    ok("校准图带 title 和对角线", "<title>" in cc and 'class="diag"' in cc)
    ok("校准图:每个点写实际比例和题数", "30% · 10 题" in cc and "60% · 12 题" in cc, cc[-600:])


def check_board():
    data = load_fixture()
    html = render.render_board(data)
    ok("百格图那句", "100 种走法里涨 40 种" in html)
    ok("系统层标尚未验证", "尚未验证" in html)
    ok("两个 AI 动作不同要提示", "两个 AI 动作不同" in html)
    ok("空仓那只先显示买点", html.index("下次什么时候买") < html.index("未来怎样", html.index("300502")))
    ok("不引用外部脚本", "<script src=" not in html)
    old = render.render_board({**data, "as_of": "2026-09-20"})
    ok("超过 30 天显示过期", "下面的结论已过期" in old)
    # 下面是 brief 之外补的
    ok("默认数据(3 天前判断)不显示过期", "下面的结论已过期" not in html)
    blocks = {c: html[html.index(f'id="s-{c}"'):] for c in CODES}
    b502 = blocks["300502"]
    b308 = blocks["300308"][:blocks["300308"].index("</article>")]
    b502 = b502[:b502.index("</article>")]
    ok("空仓那只:块里买点在「未来怎样」前面", b502.index("下次什么时候买") < b502.index("未来怎样"))
    ok("持有那只:买点折叠在后面", b308.index("未来怎样") < b308.index("下次什么时候买")
       and re.search(r"<details[^>]*>\s*<summary>[^<]*下次什么时候买", b308) is not None)
    ok("不引用外部样式表和字体", "<link" not in html and "@import" not in html and "url(http" not in html)
    for tag in ("已公布", "发布的预测", "模型推算", "AI 判断"):
        ok(f"身份标签:{tag}", tag in html)
    for lab in ("参与系统计算", "只给 AI 参考", "只展示"):
        ok(f"数据去向:{lab}", lab in html)
    ok("扇形图说明两句", "模型推算:只按波动幅度,不带方向" in html
       and "阴影是每个时间点价格可能落在哪,不是说价格一路都在阴影里" in html)
    ok("「不涨」包括持平", "不涨(含持平)" in html)
    ok("本次检查的数据缺什么", "本次检查的数据缺什么" in html and "海外同业股价" in html)
    ok("AI 建议改规则另起一行", "AI 建议改规则" in html)
    ok("快到卖出线要马上注意", "离卖出线" in html and "要马上注意的" in html)
    ok("底部链到成绩页", 'href="成绩.html"' in html)
    for s in ("现在怎样", "什么时候见底、什么时候反弹", "依据", "关键日期", "市场背景", "顶栏" if False else "判断作于",
              "行情更新于", "持仓数据截至"):
        ok(f"有「{s}」", s in html)
    m = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.S)
    ok("数据内联成 JSON,读得回来", m is not None and json.loads(m.group(1))["as_of"] == data["as_of"])
    st = json.loads(json.dumps(data))
    st["status"] = {"checked_at": "2026-11-02T15:30:00+08:00", "triggered": [{"code": "300308", "what": "合成:跌破卖出线",
                                                                              "detail": "收盘 319"}], "due": []}
    ok("status.json 里有已触发 → 过期提示", "下面的结论已过期" in render.render_board(st)
       and "合成:跌破卖出线" in render.render_board(st))
    st2 = json.loads(json.dumps(data))
    st2["account"] = {**st2["account"], "stale": True}
    ok("持仓 stale → 过期提示", "下面的结论已过期" in render.render_board(st2))
    st3 = json.loads(json.dumps(data))
    st3["failed_required"] = ["capex"]
    ok("关键数据取不到 → 过期提示", "下面的结论已过期" in render.render_board(st3))
    ev = json.loads(json.dumps(data))
    ev["stocks"][0]["action"]["why"] = "<b>x</b></script>"
    eh = render.render_board(ev)
    ok("文字一律转义,JSON 里没有 </script>", "&lt;b&gt;x&lt;/b&gt;" in eh
       and eh.count("</script>") == eh.count("<script"), eh.count("</script>"))
    ok("页面里没有本机路径", not re.search(r"/Users/|/private/|/tmp/|/var/folders", html))
    # I1:每个由没验证过的层给出的概率旁边都标「尚未验证」
    verdict = b502[b502.index('<section class="verdict">'):]
    verdict = verdict[:verdict.index("</section>")]
    ok("结论条:主 AI 和另一个 AI 的概率旁边都标尚未验证", verdict.count("尚未验证") >= 2, verdict.count("尚未验证"))
    caps = re.findall(r"<figcaption>(.*?)</figcaption>", b502[b502.index('<div class="icons">'):b502.index('class="fanbox"')])
    ok("百格图下:主层和另外三层各标一次", len(caps) == 3 and all(c.count("尚未验证") == 4 for c in caps),
       [c.count("尚未验证") for c in caps])
    for name in ("什么时候见底", "下次什么时候买"):
        sec = b502[b502.index(name):]
        ok(f"「{name}」那块的概率标了尚未验证", "尚未验证" in sec[:sec.index("</section>")])
    good = json.loads(json.dumps(data))
    good["validated"] = dict.fromkeys(good["validated"], True)
    ok("各层都验证过 → 一处尚未验证都没有", "尚未验证" not in render.render_board(good))
    # I2:「不涨」不写成「跌」
    look = re.search(r'<div class="k">怎么看</div><div class="v big">(.*?)</div>', b502).group(1)
    ok("涨的不到一半时「怎么看」里没有「跌」", "跌" not in look and "涨的不到一半" in look, look)
    ok("全页没有「偏跌」", "偏跌" not in html)
    # I3:打开页面那天再按 as_of 判一次过期(页面是静态的,离线也要能变过期)
    ok("过期天数 Python 端是 30,名字写明是预测过期(不和持仓的 7 天混)", getattr(render, "FORECAST_STALE_DAYS", None) == 30
       and not hasattr(render, "STALE_DAYS"))
    ok("页面小脚本里用同一个 30 和 as_of、今天比", f"var FORECAST_STALE_DAYS = {getattr(render, 'FORECAST_STALE_DAYS', '?')};" in html
       and "d.as_of" in html and "Date.now()" in html and json.dumps(render.STALE_HEAD) in html)
    ok("生成时已过期:服务器端照样写出那条,带 data-why=days(脚本不重复加)",
       'data-why="days"' in old and 'id="stale-banner"' in old)
    # S2:SEC 文件里的相关句子全部列出
    ok("SEC 摘句全部列出", "合成摘句一" in html and "合成摘句二" in html)
    # S4:手工登记的关键日期
    ok("手工登记的关键日期", "手工登记" in html and "四家云厂发四季报" in html and "2027-01 底~02 初" in html)


def check_scores():
    data = load_fixture()
    sc = render.render_scores(data)
    ok("样本少只写一句话", "样本还少" in sc and 'class="calib"' not in sc)
    ok("不引用外部脚本", "<script src=" not in sc and "<link" not in sc)
    t = data["scores"]["totals"]
    ok("总账一行", f"共 {t['questions']} 道题" in sc and "journal 里另有旧记录" in sc, t)
    ok("概率误差前台叫法", "概率误差,越低越好" in sc and "区间得分" in sc)
    ok("错题有原因概率", "数据没取全" in sc and "正常的运气" in sc and "程序给的初值" in sc and "人确认" in sc)
    ok("要改的地方:待改 / 已改 + 提交号", "待改" in sc and "已改" in sc and "abc1234" in sc)
    ok("季度业绩比对图", 'class="real"' in sc and "2025Q4" in sc)
    ok("版本链列出被取代的旧版", "被取代" in sc)
    ok("已到期待复核的 journal 记录", "合成:收盘跌破 280" in sc)
    ok("执行回顾单列", "执行回顾" in sc and "合成:减了 100 股" in sc)
    ok("改了以后有没有变准:两栏", "旧题重跑" in sc and "改之后才出的新题" in sc)
    ok("两个 AI 比", "两个 AI 比" in sc)
    ok("底部链回看板", 'href="看板.html"' in sc)
    cards = re.findall(r'<div class="card[^"]*">.*?</div></div>', sc, re.S)
    ok("比对卡:上半写依据,有就列原文", any("依据" in c and "合成依据:云厂资本开支同比 60%" in c
                                         and "合成理由:订单还在涨" in c for c in cards))
    ok("比对卡:系统算法的依据写信号", any("系统算法" in c and "一年偏" in c for c in cards))
    ok("比对卡:没有依据照实写", any("统计底线" in c and "这一层没写依据" in c for c in cards))
    ok("比对卡:扣掉同期大盘 = 本股比值 − 创业板指比值", "扣掉同期大盘偏 +3.0%" in sc)
    ok("比对卡:聊天里加的题,依据写当时的说明", any("聊天里给的判断:合成:聊天里说的" in c for c in cards))
    ok("比对卡:journal 旧记录不进比对卡", not any("由旧记录 #9 换算" in c for c in cards))
    jsec = sc[sc.find("<h2>journal 旧记录原文比对</h2>"):]
    jsec = jsec[:jsec.find("</section>")]
    ok("journal 旧记录单独一栏:题目、换算说明、概率、结算结果", "由旧记录 #9 换算" in jsec and "100 种里 85 种" in jsec
       and "结果:2026-07-31:发生了" in jsec, jsec[:300])
    ver = sc[sc.find("<h3>改之后才出的新题</h3>"):]
    ok("聊天题按版本分组写「聊天里给的判断」,不写 unknown", "聊天里给的判断:" in ver[:ver.find("</section>")]
       and "unknown" not in ver[:ver.find("</section>")])
    old = sc[sc.find("<h3>旧题重跑</h3>"):sc.find("<h3>改之后才出的新题</h3>")]
    ok("旧题重跑:列出存下来的重跑结果(哪版代码、哪份输入、哪几个数变了)",
       "还没有存下来的重跑结果" not in old and "代码 abc1234" in old and "修正后的输入 修正.json" in old
       and "3 个月 +2" in old and "一年 -1" in old and "见底月份最多差 +5 种(2026-08)" in old, old)
    big = json.loads(json.dumps(data))
    big["scores"]["max_neff"] = 25
    big["scores"]["calib"] = {"claude": [[25, 0.3, 10], [75, 0.6, 12]], "baseline": [[45, 0.5, 20]]}
    bs = render.render_scores(big)
    ok("样本够了才画校准图", 'class="calib"' in bs and "样本还少" not in bs)
    nq = json.loads(json.dumps(data))
    nq["scores"]["quarters"] = None
    ok("没有季度回测写清楚", "还没跑季度回测" in render.render_scores(nq))


def check_phone_and_write():
    data = load_fixture()
    ph = render.render_phone(data)
    ok("手机版:顶部两个页内跳转", re.search(r'href="#board".*?href="#scores"', ph, re.S) is not None)
    ok("手机版:两页都在", 'id="board"' in ph and 'id="scores"' in ph and "下次什么时候买" in ph and "样本还少" in ph)
    ok("手机版:页内链接不指向别的文件", 'href="成绩.html"' not in ph and 'href="看板.html"' not in ph)
    ok("手机版:数据只内联一份", ph.count('id="board-data"') == 1)
    ok("手机版:不引用外部文件", "<script src=" not in ph and "<link" not in ph)
    tmp = Path(tempfile.mkdtemp(prefix="check-pages-"))
    try:
        paths = render.write_all(data, tmp / "out")
        names = sorted(p.name for p in paths)
        ok("write_all 写三个文件", names == sorted(["看板.html", "成绩.html", "看板_手机版.html"]), names)
        ok("写出来的就是渲染结果", (tmp / "out" / "看板.html").read_text(encoding="utf-8") == render.render_board(data))
        ok("目录里没有别的文件", sorted(p.name for p in (tmp / "out").iterdir()) == names)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _keys(d: dict) -> dict:
    return {"top": sorted(d), "stock": sorted(d["stocks"][0]), "scores": sorted(d["scores"]),
            "layer": sorted(d["stocks"][0]["layers"]["claude"])}


def check_build():
    tmp = Path(tempfile.mkdtemp(prefix="check-pages-"))
    try:
        run, cfg = make_lab(tmp / "tiny")
        d = render.build_board_data(run, cfg, TODAY)
        html = render.render_board(d)
        ok("build_board_data → render_board 能跑", "未来怎样" in html and "100 种走法里涨 40 种" in html)
        ok("没有借款看板 → 持仓过期,动作是先更新", d["account"]["stale"] and "下面的结论已过期" in html
           and all(s["action"]["kind"] == "update_holdings" for s in d["stocks"]), d["account"])
        ok("持仓读不出来:不说「手里没有」,也不把「先更新持仓」当成 AI 不同意规则",
           "手里没有这只" not in html and "持仓数据读不出来,不知道手里有没有" in html and "AI 建议改规则" not in html)
        sc = render.render_scores(d)
        ok("没有 data/backtest → 还没跑季度回测", "还没跑季度回测" in sc and d["scores"]["quarters"] is None)
        ok("没有结算记录 → 样本还少", "样本还少" in sc)
        ok("没有 replays.jsonl → 旧题重跑照实写还没有", "还没有存下来的重跑结果" in sc and d["scores"]["replays"] == [])
        ok("价位换成起点那天的价:底线 63 天中位数接近收盘",
           abs(d["stocks"][1]["layers"]["baseline"]["horizons"]["63"]["q"]["50"] / CLOSE["300308"] - 1) < 0.05,
           d["stocks"][1]["layers"]["baseline"]["horizons"]["63"]["q"])
        ok("题目数从 questions.json 数", d["scores"]["totals"]["questions"] > 0, d["scores"]["totals"])
        ok("配置里没有 key_dates 就不显示手工登记那几行", "手工登记" not in html and d["key_dates"] == [])
        ok("数据里没有临时目录路径", str(tmp) not in json.dumps(d, ensure_ascii=False))
        run2, cfg2 = make_lab(tmp / "st", status={"checked_at": "2026-11-02T15:30:00+08:00",
                                                  "triggered": [{"code": "300308", "what": "合成触发", "detail": "x"}],
                                                  "due": [], "unchecked": [{"code": "300502", "what": "收盘价取不到",
                                                                            "detail": "合成"}]}, codex_failed=True)
        d2 = render.build_board_data(run2, cfg2, TODAY, state=SYN_STATE)
        h2 = render.render_board(d2)
        ok("status.json 读进来", d2["status"]["triggered"][0]["what"] == "合成触发" and "合成触发" in h2)
        ok("status.json 的 unchecked(没查成的)列在顶栏", "没查成:300502 收盘价取不到(合成)" in h2)
        ok("codex 作答失败在首页提示", "codex 这次作答失败" in h2 and d2["layers_failed"].get("codex"), d2["layers_failed"])
        ok("一个 AI 失败时不说两个 AI 动作不同", "两个 AI 动作不同" not in h2)
        ml = board_data.missing_list({"taiwan": {"status": "failed", "note": "合成"},
                                             "capex": {"status": "failed"}}, ["capex", "gap:海外同业股价"])
        ok("缺数据:只有必需项取不到才标 required(顶栏只为它变色)",
           [m["key"] for m in ml if m.get("required")] == ["capex"] and any(m["key"] == "taiwan" for m in ml), ml)
        rich = build_rich(tmp / "rich")
        fix = load_fixture()
        ok("fixture 和 build_board_data 的键一样(结构没漂)", _keys(rich) == _keys(fix), (_keys(rich), _keys(fix)))
        ok("rich:结算记录进了评分", rich["scores"]["report"]["totals"]["scored"] == 8, rich["scores"]["report"]["totals"])
        qids = [q["id"] for q in json.loads((tmp / "rich" / "data" / "forecasts" / "2026-10-30" / "154000"
                                             / "questions.json").read_text(encoding="utf-8"))["questions"]]
        ok("rich:journal 题不进各层成绩", rich["scores"]["report"]["layers"]["claude"]["n"] == 4
           and [j["note"] for j in rich["scores"]["journal_questions"]] == ["合成:由旧记录 #9 换算"],
           (rich["scores"]["report"]["layers"]["claude"]["n"], rich["scores"]["journal_questions"]))
        ok("rich:还没结算的聊天题也算进总题数", rich["scores"]["totals"]["questions"] == len(set(qids)) + 10,
           (rich["scores"]["totals"], len(set(qids))))
        ok("rich:季度回测读进来,没区间的那行跳过", rich["scores"]["quarters"] is not None
           and len(rich["scores"]["quarters"]["rows"]) == 2, rich["scores"]["quarters"])
        ok("rich:旧记录只数非存档那几条", rich["scores"]["totals"]["journal_old"] == 2, rich["scores"]["totals"])
        ok("rich:两只股票都有动作", {s["code"] for s in rich["stocks"]} == set(CODES)
           and rich["stocks"][1]["action"]["kind"] == "trim", [s["action"]["kind"] for s in rich["stocks"]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


FIRST_SCREEN = ["怎么看", "怎么做", "为什么", "什么时候改主意", "三个期限", "见底", "本次缺什么", "另一个 AI", "要马上注意的"]


def _cell(verdict: str, label: str) -> str:
    """结论条里某一格的内容(到下一个 <div class="k"> 或结论条末尾);没有这一格 → 空。"""
    head = f'<div class="k">{label}</div>'
    if head not in verdict:
        return ""
    i = verdict.index(head) + len(head)
    j = verdict.find('<div class="k">', i)
    return verdict[i:j if j >= 0 else len(verdict)]


def check_first_screen():
    """第一屏:每只股票的结论条按固定顺序放下第一屏要读出的几样;AI 的长理由、多出来的改主意条件折叠在 <details> 里。
    合成答卷的理由只有一行,所以这里把 AI 理由换成很长的一段(真实答卷里这一段有五百多字)。"""
    data = load_fixture()
    for s in data["stocks"]:
        s["layers"]["claude"]["action"]["why"] = "合成长理由" + "很长" * 270
        s["layers"]["claude"]["action"]["change_if"] += [{"if": f"合成条件{i}", "then": "合成动作"} for i in range(3)]
    for page, html in (("看板", render.render_board(data)), ("手机版", render.render_phone(data))):
        for c in CODES:
            blk = html[html.index(f'id="s-{c}"'):]
            blk = blk[:blk.index("</article>")]
            v = blk[blk.index('<section class="verdict">'):]
            v = v[:v.index("</section>")]
            labels = re.findall(r'<div class="k">(.*?)</div>', v)
            ok(f"{page} {c}:结论条按顺序有这 9 格", labels == FIRST_SCREEN, labels)
            if labels != FIRST_SCREEN:
                continue
            i = v.index("合成长理由")
            ok(f"{page} {c}:AI 的长理由在「完整理由」的 <details> 里", v.rfind("<details", 0, i) > v.rfind("</details>", 0, i)
               and "Claude 的完整理由" in v[v.rfind("<details", 0, i):i])
            why = re.sub(r"<details.*?</details>", "", _cell(v, "为什么"), flags=re.S)
            ok(f"{page} {c}:「为什么」露在外面的只有一行(≤ 80 字)", len(re.sub(r"<[^>]+>", "", why)) <= 80, why)
            ch = _cell(v, "什么时候改主意")
            first = re.sub(r"<details.*?</details>", "", ch, flags=re.S)
            ok(f"{page} {c}:改主意露出第一条,其余在「还有 N 条」里", first.count("→") == 1
               and re.search(r"<details[^>]*><summary>还有 \d+ 条</summary>", ch) is not None, ch[:300])
            hz = _cell(v, "三个期限")
            ok(f"{page} {c}:三个期限都有涨的种数,每个标尚未验证", all(h in hz for h in ("3 个月", "半年", "一年"))
               and hz.count("尚未验证") == 3 and hz.count(" 种") >= 3, hz)
            ok(f"{page} {c}:见底写最可能的月份和价位段,各带 100 种里几种", _cell(v, "见底").count("100 种里") == 2,
               _cell(v, "见底"))
            ok(f"{page} {c}:本次缺什么", "海外同业股价" in _cell(v, "本次缺什么"), _cell(v, "本次缺什么"))
            ok(f"{page} {c}:另一个 AI 的结论和动作不同的提示", "codex" in _cell(v, "另一个 AI")
               and "两个 AI 动作不同" in _cell(v, "另一个 AI"), _cell(v, "另一个 AI"))
        b308 = html[html.index('id="s-300308"'):]
        ok(f"{page}:离卖出线的提醒在结论条里", "离卖出线" in _cell(b308[:b308.index("</section>")], "要马上注意的"))
    good = json.loads(json.dumps(data))
    good["validated"] = dict.fromkeys(good["validated"], True)
    g = render.render_board(good)
    ok("各层都验证过 → 三个期限那格不标尚未验证", "尚未验证" not in _cell(g[g.index('<section class="verdict">'):], "三个期限"))


def check_rebound_capex():
    """反弹信号「云厂给出新一年开支计划」:有目标年份(4 月起是明年,1~3 月是今年)及以后的开支数字才亮;
    只有口头说法(说会增加、没给数)不亮,写明「有口头说法、还没给数字」。公司名、句子都是合成的。"""
    def sig(guidance, sec, as_of):
        items = {}
        if guidance is not None:
            items["capex"] = {"status": "ok", "value": {"total": [], "guidance": guidance}}
        if sec is not None:
            items["events.sec"] = {"status": "ok", "value": sec}
        try:
            rows = board_data.rebound(items, "300308", as_of)
        except TypeError:                     # 老的 rebound 不收 as_of
            rows = board_data.rebound(items, "300308")
        return next(r for r in rows if r["name"] == "云厂给出新一年开支计划")

    def filing(who, day, text):
        return {"ticker": who, "date": day, "capex_sentences": [text]}
    this_year = ["AAA 2026-10-29:We continue to expect 2026 cash capital expenditures to be approximately $220 billion."]
    talk = [filing("AAA", "2026-10-30", "We expect cash capital expenditures to meaningfully increase in 2027, "
                                        "primarily driven by investments in technology infrastructure.")]
    fcf = [filing("BBB", "2026-10-28", "Following a period of peak capex in FY27-FY28, capital expenditures are expected "
                                       "to step-down in FY29, supporting positive free cash flow of at least $30 billion in FY29.")]
    num = [filing("CCC", "2027-01-29", "We expect 2027 capital expenditures to be in the range of $175 billion to $185 billion.")]
    r = sig(this_year, [], "2026-11-02")
    ok("只有今年(2026)的开支数字 → 不亮,写明还没有 2027 年的数", r["lit"] is False and "2027" in r["text"], r)
    r = sig(this_year, talk + fcf, "2026-11-02")
    ok("只说 2027 年会增加、FY29 的数是自由现金流 → 不亮,写「有口头说法、还没给数字」",
       r["lit"] is False and r["text"].startswith("有口头说法、还没给数字") and "AAA 2026-10-30" in r["text"], r)
    r = sig(this_year, talk + num, "2026-11-02")
    ok("有公司给了 2027 年的开支数字 → 亮,写是哪家哪天", r["lit"] is True and "CCC 2027-01-29" in r["text"], r)
    before = ["DDD 2027-02-01:We expect 2027 capital expenditures of approximately $200 billion."]
    ok("1~3 月目标年是今年:2027-03-31 看到 2027 年的数 → 亮", sig(before, None, "2027-03-31")["lit"] is True)
    ok("4 月起目标年是明年:2027-04-01 再看同一句 → 不亮", sig(before, None, "2027-04-01")["lit"] is False)
    ok("金额写在前面(「$135 to $145 billion of capital expenditures in 2027」)也认",
       sig(["EEE 2026-10-26:EEE expects $135 to $145 billion of capital expenditures in 2027."], None, "2026-11-02")["lit"] is True)
    ok("标签里的日期年份不算:「FFF 2027-01-29:… $30 billion in the quarter」不亮",
       sig(["FFF 2027-01-29:Capital expenditures were $30 billion in the quarter."], None, "2026-11-02")["lit"] is False)
    ok("两项都没取到 → 没取到数据(None)", sig(None, None, "2026-11-02")["lit"] is None)
    # 金额要和 ≥ 目标年的那个年份在同一个分句里连着
    both = ["AAA 2026-10-29:We expect cash capital expenditures to be approximately $220 billion in 2026 "
            "and to meaningfully increase in 2027."]
    r = sig(both, None, "2026-11-02")
    ok("「$220 billion in 2026 and to meaningfully increase in 2027」→ 不亮(金额连着的是 2026),算口头说法",
       r["lit"] is False and r["text"].startswith("有口头说法、还没给数字"), r)
    notes = [filing("GGG", "2026-10-30", "We will fund our investments, including capital expenditures, "
                                         "from the $15 billion of notes due 2031.")]
    ok("「capital expenditures, from the $15 billion of notes due 2031」→ 不亮(2031 是债的到期年)",
       sig([], notes, "2026-11-02")["lit"] is False, sig([], notes, "2026-11-02"))
    ok("「expects 2027 capital expenditures of about $90 billion」→ 亮",
       sig(None, [filing("HHH", "2027-01-28", "HHH expects 2027 capital expenditures of about $90 billion.")],
           "2026-11-02")["lit"] is True)
    ok("「$90 billion for 2027」写在后面也认 → 亮",
       sig(["III 2027-01-28:Capital expenditures are expected to be about $90 billion for 2027."], None,
           "2026-11-02")["lit"] is True)
    # 金额的年份 = 同一分句里离它最近的年份(按字符距离,债的到期年先去掉);分句里别的年份不算
    for tail in ("with a meaningful increase in 2027.", "rising further in 2027."):
        r = sig([f"JJJ 2026-10-29:We expect capital expenditures of approximately $220 billion in 2026 {tail}"],
                None, "2026-11-02")
        ok(f"「$220 billion in 2026 {tail}」→ 不亮(离金额最近的是 2026),算口头说法",
           r["lit"] is False and r["text"].startswith("有口头说法、还没给数字"), r)
    ok("「about $90 billion in calendar 2027」→ 亮(离金额最近的是 2027)",
       sig(["KKK 2027-01-28:We expect capital expenditures of about $90 billion in calendar 2027."], None,
           "2026-11-02")["lit"] is True)
    ok("「capital expenditures of about $90 billion funded by notes due 2031」→ 不亮(到期年去掉后分句里没有年份)",
       sig(["LLL 2026-10-29:Capital expenditures of about $90 billion will be funded by notes due 2031."], None,
           "2026-11-02")["lit"] is False)
    html = render.render_board(load_fixture())
    ok("页面写明财年不换算成日历年", "财年(如 FY27)按数字当成 2027 年,不换算成日历年" in html)


def _flat(s: str) -> bool:
    """没有括号套括号(半角、全角都算)。"""
    depth = 0
    for ch in s:
        depth += 1 if ch in "(（" else (-1 if ch in ")）" and depth else 0)
        if depth > 1:
            return False
    return True


def _first_screen(html: str, code: str) -> dict:
    v = html[html.index(f'id="s-{code}"'):]
    v = v[v.index('<section class="verdict">'):v.index("</section>", v.index('<section class="verdict">'))]
    plain = {k: re.sub(r"<[^>]+>", "", re.sub(r"<details.*?</details>", "", _cell(v, k), flags=re.S))
             for k in ("为什么", "本次缺什么")}
    return plain


def check_cover_texts():
    """2026-11-08:现金留够两个月(cover_*)、按顺序卖、闲钱写「到 X 为止」。直接喂 render 的小函数,数字是编的。"""
    acc = {"month_need": 5000.0, "need_month": "2026-12", "cover_months": 2, "cover_need": 10000.0,
           "cover_from": "2026-12", "cover_until": "2027-01", "free_cash": -1000.0, "free_cash_until": "2027-05"}
    s = {"action": {"kind": "trim", "amount": 6000.0, "limit_known": True,
                    "why": "…;按卖出顺序 300502→300308(前面的卖够了就不动后面的),这只分 4,100.00,…"}}
    w = render._why_short(s, {"account": acc})
    ok("结论条:两个月合计,写按已定顺序", "2026-12~2027-01 两个月要还 10,000 元" in w and "按已定顺序" in w, w)
    w1 = render._why_short({"action": {**s["action"], "why": "…;按卖出比例 2:1(300308:300502),…"}},
                           {"account": {k: v for k, v in acc.items() if not k.startswith("cover_")}})
    ok("反例:老数据(没有 cover_*)、按比例 → 照旧写这个月、按已定比例",
       "2026-12 要还 5,000 元" in w1 and "按已定比例" in w1, w1)
    m = render._buy_money_text({"account": acc})
    ok("闲钱:写「到 2027-05 为止没有闲钱」,不写「以前」", "到 2027-05 为止没有闲钱" in m and "以前" not in m, m)


def check_limit_wiring():
    """仓位上限接进页面:算不出来时写进「本次缺什么」和动作的「为什么」;算得出来时超限的照减,结论条写仓位超限。
    数字是编的:300308 持 300 股,收盘 330(市值 99,000);硬上限 25%(一看就是合成的)。"""
    tmp = Path(tempfile.mkdtemp(prefix="check-pages-"))
    try:
        run, cfg = make_lab(tmp / "lim")
        d = render.build_board_data(run, cfg, TODAY, state=SYN_STATE)
        s = next(x for x in d["stocks"] if x["code"] == "300308")
        ok("没有 account.json → 「本次缺什么」有一行仓位上限这次算不出来", any(m.get("kind") == "limit"
           and "仓位上限这次算不出来" in m["text"] for m in s["missing"]), s["missing"])
        ok("没有 account.json → 动作的「为什么」写这次算不出来", "仓位上限这次算不出来" in s["action"]["why"], s["action"])
        fs = _first_screen(render.render_board(d), "300308")
        ok("上限算不出来时,结论条那一行不写「只因为要还款」,写「仓位上限这次算不出来」",
           "只因为要还款" not in fs["为什么"] and "因为要还款" in fs["为什么"] and "仓位上限这次算不出来" in fs["为什么"], fs)
        acct = run.parent.parent.parent.parent / "private" / "portfolio" / "account.json"
        acct.parent.mkdir(parents=True, exist_ok=True)
        acct.write_text(json.dumps({"_单票最大可承受亏损_pct_of_净资产": 10, "_单票硬上限_pct_of_净资产": 25,
                                    "_硬上限_改动理由": "2026-01-15 初值,合成。"}, ensure_ascii=False), encoding="utf-8")
        st = {**SYN_STATE, "net_assets": 200000.0, "holdings": {"300308": {"shares": 300, "cost": 400.0, "price": 330.0}}}
        d = render.build_board_data(run, cfg, TODAY, state=st)
        s = next(x for x in d["stocks"] if x["code"] == "300308")
        ok("占净资产 49.5% > 25% → 减到上限:卖 200 股(超出 49,000 ÷ 330 = 148.5)", s["action"]["kind"] == "trim"
           and s["action"]["shares"] == 200 and (s["action"].get("over_limit") or {}).get("shares") == 200, s["action"])
        ok("「为什么」写仓位超限、上限从哪来、哪天定的", "仓位超限" in s["action"]["why"]
           and "仓位上限 25%(账户设置,2026-01-15 定)" in s["action"]["why"], s["action"]["why"])
        ok("最坏情景缺 → 「本次缺什么」写只用了账户设置的 25% 上限和原因", any(m.get("kind") == "limit"
           and "只用了账户设置的 25% 上限" in m["text"] and "按最坏情景倒推的上限这次算不出来" in m["text"]
           for m in s["missing"]), s["missing"])
        fs = _first_screen(render.render_board(d), "300308")
        ok("结论条「为什么」露出来的那一行写仓位超限", "仓位超限:现在占净资产 50%,仓位上限 25%(账户设置,2026-01-15 定)"
           in fs["为什么"], fs["为什么"])
        ok("结论条「本次缺什么」有上限那行", "只用了账户设置的 25% 上限" in fs["本次缺什么"], fs["本次缺什么"])
        ok("第一屏这两格平铺、不出现硬线 / bear / journal", all(_flat(t) and not any(
            w in t for w in ("硬线", "bear 倒推", "bear", "journal")) for t in fs.values()), fs)
        old = render.build_board_data(run, cfg, TODAY, state={**st, "stale": True})
        so = next(x for x in old["stocks"] if x["code"] == "300308")
        ok("持仓过期(动作是先更新持仓)→ 「本次缺什么」不加上限那几行",
           so["action"]["kind"] == "update_holdings" and not any(m.get("kind") == "limit" for m in so["missing"]),
           (so["action"]["kind"], so["missing"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CHECKS = [check_svg, check_board, check_first_screen, check_rebound_capex, check_cover_texts, check_limit_wiring, check_scores,
          check_phone_and_write, check_build]


def write_fixture():
    tmp = Path(tempfile.mkdtemp(prefix="check-pages-"))
    try:
        d = build_rich(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    FIX.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"写好了 {FIX.relative_to(ROOT)}")


def main():
    if "--write-fixture" in sys.argv[1:]:
        write_fixture()
        return 0
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
