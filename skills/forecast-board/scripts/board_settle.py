#!/usr/bin/env python3
"""board_settle —— board.py 的 resolve / check / add-question / confirm / replay。

resolve       所有还没结算的题(各次存档的 questions.json + 聊天里加的题)按真实收盘结算,
              一题一行追加到 <存档根>/outcomes.jsonl;错题另带 suggest(attribution 给的原因初值,
              等人用 confirm 确认)和 suggest_why(触发了哪几条规则,一条一句,成绩页当证据列出来、
              resolve 也打出来 —— 没有证据,人确认时只能猜)。每条结果另记同一段时间创业板指的收盘之比 index_ratio、
              本股后复权之比 stock_ratio,成绩页拿两者之差写「扣掉同期大盘」(questions.py 不动);
              取得到本股不复权日线时再记开题日和那天的不复权收盘 start_close、end_close,
              成绩页写「收盘 x 元」用(取不到就不记,不耽误结算;和后复权那两根不是同一天也不记)。
              已经结算、但记录里没有这两个价的题(这个功能以前结的),resolve 每次顺手补:
              同一题号追加一整行(原来的字段不变,resolution 里加两个价和 closes_added_at)。
              所以 outcomes.jsonl 里同一题号可能有几行,读的地方一律以最后一行为准(resolved、
              board_scores.scores_data 都是;replay、attribution 不读这个文件)。
check         每日检查:刷新收盘价,查卖出线、最新一次存档里两个 AI 写的改主意条件、能结算的题,
              写 <存档根>/status.json,重画页面让顶栏标出来;--notify 时有触发、到期或没查成就弹系统通知。
              一只股票取不到不连累别的:取到的照常比,取不到的写进 status.json 新加的 unchecked
              [{"code","what","detail"}](页面顶栏列出来),退出码 1;到期题那段取不到只影响 due。
              近 20 天没有日线(长期停牌)、到期题结算不了,也写进 unchecked —— 定时任务没人看 stderr。
add-question  聊天里给的判断(碰价 / 某天高于 / 到某个倍数)按 questions.make_question 写成题,
              追加到 <存档根>/chat_questions.jsonl。同一个题号已经有了就报错。另记开题日的不复权收盘
              p0_close,成绩页把比值写成价位线用(取不到就不记)。按开题日分两种来源:
                journal  开题日早于第一次 board 存档(或者还没有任何存档)= 换算进来的上线前 journal 旧记录,
                         只做原文比对:不进各层成绩、不进两个 AI 比,成绩页单独一栏
                chat     之后聊天里给的判断:照常算进作者那一层,版本那栏写「聊天」
confirm       人确认一道错题的原因概率(attribution.confirm,追加到 reviews.jsonl)。题号要在结算记录里。
replay        用指定版本的代码重算一次存档的两层(replay.replay),和存档里的两层比(replay.compare),
              一次一行追加到 <存档根>/replays.jsonl,成绩页「旧题重跑」读它。

几条规矩:
  - 本股日线、交易日历都只取到今天,15:05 以前去掉当天那根(board_io.Market)。
  - 买点那天同一只股票、同一个开题日只算一次(questions.buy_signal_day),再传给各题。
  - 结算过的题不再算:结果已经定了(碰到当天就结的题,计分日照样是窗口走完那天,见 questions.py)。
  - 改主意条件是 AI 写的一句话,这里只认「一句话里只有一个本股收盘价位」的(收盘跌破 297 / 站上 360);
    说的是均线、指数、利润、两个价位的,一律交给人看,检查只报有几条没法自动查。
    价位按存档那天的不复权价写的;中间除过权,比较会差一点。
"""
from __future__ import annotations

import math
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import fb_common
import actions
import archive
import attribution
import board_io
import board_scores
import inputs
import prices
import questions
import replay as replay_mod
import scoring

CHAT = "chat_questions.jsonl"
KNOWN_WHO = ("user", "claude", "codex")   # confirm --who:user 是人确认;claude / codex 是 AI 的提议
OUTCOMES = board_scores.OUTCOMES
REPLAYS = board_scores.REPLAYS
STATUS = "status.json"
HIST_DAYS = 150             # 开题日往前多取多少个自然日:买点题要 60 根算均线
AI_LAYERS = ("claude", "codex")
LAYER_TEXT = {"claude": "Claude", "codex": "codex"}
TITLE = "预测看板"
_CODE = re.compile(r"\b\d{6}\b")


# ── 题目 ────────────────────────────────────────────────────────────────────
def _run_questions(run: Path) -> list[dict]:
    q = board_io.read_json(run / "questions.json")
    qs = q.get("questions") if isinstance(q, dict) else q
    return [x for x in qs or [] if isinstance(x, dict) and x.get("id")]


def all_questions(root: Path) -> list[dict]:
    """各次存档的题 + 聊天里加的题,同一题号以最后一行为准(顺序按第一次出现)。
    和 outcomes.jsonl、成绩页读 chat_questions.jsonl 一个规矩:只追加的文件,后写的那行是最新的。"""
    out: dict[str, dict] = {}
    for q in [q for run in archive.list_runs(root) for q in _run_questions(run)] + attribution._read_lines(root / CHAT):
        if q.get("id"):
            out[q["id"]] = q
    return list(out.values())


def resolved(root: Path) -> list[dict]:
    """结算记录,同一题号只给一条:最后那行(补记收盘价会给结过的题再追加一整行)。顺序按题号第一次出现。"""
    last: dict[str, dict] = {}
    for r in attribution._read_lines(root / OUTCOMES):
        if r.get("id") and isinstance(r.get("resolution"), dict):
            last[r["id"]] = r
    return list(last.values())


def pending(root: Path) -> list[dict]:
    done = {r["id"] for r in resolved(root)}
    return [q for q in all_questions(root) if q["id"] not in done]


# ── 结算 ────────────────────────────────────────────────────────────────────
def _with_ratios(q: dict, res: dict, idx: dict, hfq: dict, close: dict | None = None) -> dict:
    """结果里加同一段时间(开题日 → end_day)创业板指收盘之比、本股后复权之比;
    给了本股不复权日线 close 时再加两天的不复权收盘 start_close、end_close。"""
    extra = {}
    for name, series in (("index_ratio", idx), ("stock_ratio", hfq)):
        a, b = prices.close_on_or_before(series, q["opened"]), prices.close_on_or_before(series, res["end_day"])
        if a and b and a[1] > 0:
            extra[name] = round(b[1] / a[1], questions.RATIO_DIGITS)
    return {**res, **extra, **close_pair(q, res["end_day"], hfq, close)}


def close_pair(q: dict, end_day: str, hfq: dict, close: dict | None) -> dict:
    """{"start_close", "end_close"}:开题日和 end_day 的不复权收盘。两天都要和后复权那两根是同一天
    (停牌顺延时两种日线顺延到同一天),有一天对不上就都不记,免得拿前一天的价顶上。"""
    if not close:
        return {}
    pairs = [(prices.close_on_or_before(hfq, d), prices.close_on_or_before(close, d)) for d in (q["opened"], end_day)]
    if not all(h and c and h[0] == c[0] for h, c in pairs):
        return {}
    return {"start_close": pairs[0][1][1], "end_close": pairs[1][1][1]}


CLOSE_MISS = {"add": "这道题不记起点的不复权收盘,成绩页对它只写百分比",
              "resolve": "这次的结算记录不带收盘价,下次 resolve 会再补"}


def _closes(market: board_io.Market, code: str, start: str, caller: str) -> dict:
    """本股不复权收盘 {日期: 价};取不到给空 dict —— 只给页面写价位用,不该挡住加题和结算。
    caller = "add" / "resolve",提示里写各自的后果。"""
    try:
        return dict(market.close(code, start))
    except Exception as e:      # 网络、接口……
        print(f"⚠ {code} 不复权日线取不到,{CLOSE_MISS[caller]}:{type(e).__name__}: {str(e)[:120]}",
              file=sys.stderr)
        return {}


PRICE_TYPES = ("up", "interval", "touch", "above_on", "multiple_by")


def backfill_closes(root: Path, market: board_io.Market, t) -> tuple[int, list[str]]:
    """已结算、记录里没有不复权收盘的价位类题:补 start_close / end_close,同一题号追加一整行。
    → (补了几道, 提示)。取不到、对不上同一天的这次不补,下次再试;一行都不改、不删。"""
    todo = [r for r in resolved(root) if r.get("type") in PRICE_TYPES and r["resolution"].get("end_day")
            and not (r["resolution"].get("start_close") and r["resolution"].get("end_close"))]
    if not todo:
        return 0, []
    start = (min(date.fromisoformat(str(r["opened"])[:10]) for r in todo) - timedelta(days=HIST_DAYS)).isoformat()
    n, notes = 0, []
    for c in sorted({r["code"] for r in todo}):
        try:
            hfq = market.hfq(c, start)
        except Exception as e:      # 只误这一只
            notes.append(f"{c} 后复权日线取不到,它结过的题这次没补收盘价:{type(e).__name__}")
            continue
        close = _closes(market, c, start, "resolve")
        for r in (x for x in todo if x["code"] == c):
            pair = close_pair(r, r["resolution"]["end_day"], hfq, close)
            if not pair:
                notes.append(f"{r['id']}:不复权收盘和后复权对不上同一天,这次没补")
                continue
            res = {**r["resolution"], **pair, "closes_added_at": t.isoformat(timespec="seconds")}
            attribution._append(root / OUTCOMES, {**r, "resolution": res})
            n += 1
    return n, notes


def settle(qs: list[dict], market: board_io.Market,
           hol: set[str]) -> tuple[list[tuple[dict, dict]], list[str], list[dict]]:
    """→ ([(题目, 结算结果)], [结算不了的题和原因], [后复权日线取不到的股票 board_io.failure(...)])。
    还没到期的不在结果里。一只股票的后复权日线取不到:只有它的题这次不结(下次再来),其他股票照结。
    交易日历(创业板指日线)取不到时哪道题都数不了日子,异常往上抛。"""
    if not qs:
        return [], [], []
    start = (min(date.fromisoformat(str(q["opened"])[:10]) for q in qs) - timedelta(days=HIST_DAYS)).isoformat()
    idx = dict(market.index(start))
    cal = sorted(idx)
    hfq, failed, close = {}, [], {}
    for c in sorted({q["code"] for q in qs}):
        try:
            hfq[c] = market.hfq(c, start)
        except Exception as e:      # 网络、接口……只误这一只
            failed.append(board_io.failure(c, "后复权日线取不到", e))
            continue
        close[c] = _closes(market, c, start, "resolve")
    out, errors, signal = [], [], {}
    for q in qs:
        if q["code"] not in hfq:
            continue
        try:
            sd = None
            if q["type"] in ("buy_signal_month", "buy_vs_now"):
                key = (q["code"], q["opened"], int(q["params"].get("horizon", questions.BUY_HORIZON)))
                if key not in signal:
                    signal[key] = questions.buy_signal_day(cal, q["opened"], hfq[q["code"]], idx, horizon=key[2])
                sd = signal[key]
                if sd is None:
                    continue
            res = questions.resolve(q, cal, hfq[q["code"]], index_closes=idx, signal_day=sd, holidays=hol)
        except ValueError as e:
            errors.append(f"{q['id']}:{e}")
            continue
        if res is not None:
            out.append((q, _with_ratios(q, res, idx, hfq[q["code"]], close.get(q["code"]))))
    return out, errors, failed


def layer_score(r: dict) -> float | None:
    """这一层在这道题上的分数(越低越好):是非题 Brier、分类题多类 Brier、区间题区间得分。"""
    f, res = r.get("forecast"), r.get("resolution") or {}
    o = res.get("outcome")
    if not isinstance(f, dict) or o is None:
        return None
    if r.get("type") == "interval":
        x = res.get("value")
        if not (isinstance(x, (int, float)) and x > 0 and f.get("lo", 0) > 0 and f.get("hi", 0) > 0):
            return None
        alpha = 1 - (int(r["params"]["hi"]) - int(r["params"]["lo"])) / 100
        return scoring.interval_score(math.log(f["lo"]), math.log(f["hi"]), math.log(x), alpha)
    if "pct" in f:
        return scoring.brier(f["pct"] / 100, int(o)) if o in (0, 1) else None
    return scoring.brier_multi({k: v / 100 for k, v in f.items()}, o)


def used_keys(r: dict, items: dict) -> list[str]:
    """这道题用到的输入键:系统层按 attribution.system_keys,长日线取不到时(输入包里有 history.long)
    再加上它 —— 信号检验因此一律算没检验过;统计底线只用本股和指数日线;
    AI 看的是整份输入包 —— 取和这只股票有关的(键里有它的代码、或不属于哪只股票的),
    去掉只展示的、持仓(持仓不影响涨跌判断)和长日线(没给 AI)。"""
    code = r["code"]
    if r["author"] == "system":
        return attribution.system_keys(code, items) + ([inputs.HISTORY_KEY] if inputs.HISTORY_KEY in items else [])
    if r["author"] == "baseline":
        return [f"prices.{code}", f"index.{prices.CALENDAR_INDEX}"]
    return [k for k in sorted(items) if k not in ("account", inputs.HISTORY_KEY) and inputs.used_by_for(k) != "display"
            and (code in k.split(".") or not _CODE.search(k))]


def with_suggest(new: list[dict], old: list[dict], root: Path) -> list[dict]:
    """错题加 suggest(原因初值)和 suggest_why(触发了的规则,一条一句;一条都没触发是空列表 = 七项均分)。
    同一道题各层的答案和分数一起看(forecasts / scored_layers)。"""
    groups: dict[tuple, list[dict]] = {}
    for r in old + new:
        groups.setdefault(scoring.question_key(r), []).append(r)
    cache: dict[str, dict] = {}
    out = []
    for r in new:
        if r.get("author") == questions.RULE_AUTHOR or not board_scores.is_wrong(r):
            out.append(r)
            continue
        same = groups[scoring.question_key(r)]
        scores = {x["author"]: s for x in same if (s := layer_score(x)) is not None}
        if r["run"] not in cache:
            cache[r["run"]] = attribution.items_of(board_io.read_json(root / r["run"] / "inputs.json") or {})
        items = cache[r["run"]]
        fired = attribution.reasons(r, scores, items, used_keys(r, items),
                                    forecasts={x["author"]: x.get("forecast") for x in same})
        out.append({**r, "suggest": attribution.probs_from(fired), "suggest_why": [f["why"] for f in fired]})
    return out


def cmd_resolve(args) -> int:
    t = board_io.now()
    hol = actions.config_holidays(fb_common.load_config())
    root = fb_common.archive_root()
    market = board_io.Market(t)
    if pending(root):
        rc = _resolve_pending(root, market, hol)
    else:
        print("没有要结算的题。")
        rc = 0
    n, notes = backfill_closes(root, market, t)
    if n:
        print(f"给 {n} 道以前结算的题补记了不复权收盘(同一题号追加一整行,以最后一行为准)")
    for x in notes:
        print(f"⚠ {x}", file=sys.stderr)
    return rc


def _resolve_pending(root: Path, market: board_io.Market, hol: set[str]) -> int:
    qs = pending(root)
    try:
        done, errors, failed = settle(qs, market, hol)
    except Exception as e:          # 多半是交易日历取不到(一道都数不了日子),下次再来;不猜原因,照实打出来
        print(f"这次没结算:{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    blocked = {f["code"]: sum(1 for q in qs if q["code"] == f["code"]) for f in failed}
    recs = with_suggest([{**q, "resolution": res} for q, res in done], resolved(root), root)
    for rec in recs:
        attribution._append(root / OUTCOMES, rec)
    outs = [r["resolution"]["outcome"] for r in recs]
    print(f"结算了 {len(recs)} 道(发生 {sum(1 for o in outs if o == 1)}、没发生 {sum(1 for o in outs if o == 0)}、"
          f"分类题 {sum(1 for o in outs if isinstance(o, str))}、不计分 {outs.count(None)});"
          f"还没到期 {len(qs) - len(recs) - len(errors) - sum(blocked.values())} 道")
    sug = [r for r in recs if "suggest" in r]
    if sug:
        print(f"错题 {len(sug)} 道已给原因初值,看过证据后用 board.py confirm 确认:")
        for r in sug[:20]:
            top = sorted(r["suggest"].items(), key=lambda kv: -kv[1])[:3]
            print(f"  {r['id']}  " + "、".join(f"{attribution.CAUSE_LABELS[c]} {v}" for c, v in top))
            for why in board_scores.suggest_evidence(r):
                print(f"      证据:{why}")
    for f in failed:
        print(f"⚠ {board_io.failure_text(f)}:它的 {blocked[f['code']]} 道题这次没结,下次再来", file=sys.stderr)
    for e in errors:
        print(f"⚠ 结算不了:{e}", file=sys.stderr)
    return 1 if errors or failed else 0


# ── 每日检查 ────────────────────────────────────────────────────────────────
_NUM = r"(\d+(?:\.\d+)?)"
_NOT_PRICE = r"(?!\d|\.\d|\s*(?:日|个|天|周|月|年|倍|%|％|亿|万|成|次|只|家|篇|根|点|手|股))"
_SUBJECT = r"(?:收盘|股价|价格)[^,,;;。]*?"
_DOWN = re.compile(_SUBJECT + r"(?:跌破|低于|跌到|跌至|跌回|小于|<|＜)\s*" + _NUM + _NOT_PRICE)
_UP = re.compile(_SUBJECT + r"(?:站上|站回|高于|突破|涨到|涨至|涨回|大于|>|＞)\s*" + _NUM + _NOT_PRICE)
_OTHER = re.compile(r"指|大盘|板块|ETF|均线")
# 要看不止一天的收盘(连续两天、周线、3 个交易日……):每日检查只拿最后一个收盘比,查不了,交给人看。
# 不然一天跌破就报「已触发」、弹通知,和条件说的不是一回事。
_MULTI_DAY = re.compile(r"连续|周线|周收盘|两天|[二三四五六七八九十\d]+\s*(?:天|个交易日|周)")


def price_condition(text) -> tuple[str, float] | None:
    """改主意条件里的本股收盘价位 → ("down" | "up", 价位);认不出、一句话里不止一个、
    或者要看不止一天的收盘(连续几天、周线)→ None(交给人看)。"""
    s = str(text or "")
    if _OTHER.search(s) or _MULTI_DAY.search(s):
        return None
    downs, ups = _DOWN.findall(s), _UP.findall(s)
    if len(downs) + len(ups) != 1:
        return None
    return ("down", float(downs[0])) if downs else ("up", float(ups[0]))


def sell_line_hits(config: dict, closes: dict) -> list[dict]:
    out = []
    for code, line in sorted((config.get("sell_lines") or {}).items()):
        c = closes.get(str(code))
        if c and isinstance(line, (int, float)) and c[1] < line:
            out.append({"code": str(code), "what": f"收盘跌破卖出线 {line:g}", "kind": "sell_line",
                        "detail": f"{c[0]} 收盘 {c[1]:g},按已定规则全部卖出"})
    return out


def change_if_hits(run: Path, closes: dict) -> tuple[list[dict], int, int]:
    """最新一次存档里两个 AI 的改主意条件 → (触发了的, 没法自动查的条数, 这只没取到收盘、没查的条数)。
    后两种分开数:「说的不是本股收盘价位」要人看条件本身;「没取到收盘」是这次没查成,那只已经记进 unchecked。"""
    hits, manual, unfetched = [], 0, 0
    for author in AI_LAYERS:
        ans = board_io.read_json(run / "answers" / f"{author}.json") or {}
        for code, block in sorted((ans.get("stocks") or {}).items()):
            act = block.get("action") if isinstance(block, dict) else None
            for cond in (act or {}).get("change_if") or []:
                pc = price_condition(cond.get("if")) if isinstance(cond, dict) else None
                c = closes.get(str(code))
                if pc is None:
                    manual += 1
                    continue
                if c is None:
                    unfetched += 1
                    continue
                if (c[1] < pc[1]) if pc[0] == "down" else (c[1] > pc[1]):
                    hits.append({"code": str(code), "what": f"{LAYER_TEXT[author]} 的改主意条件:{cond.get('if')}",
                                 "kind": "change_if", "run": board_io.run_rel(run),
                                 "detail": f"{c[0]} 收盘 {c[1]:g};条件成立时改成:{cond.get('then')}"})
    return hits, manual, unfetched


def note_text(triggered: list[dict], due: list[str], unchecked: list[dict]) -> str:
    """通知正文:触发了什么、几道题到期、哪几只没查成。"""
    parts = [f"{t['code']} {t['what']}" for t in triggered[:3]]
    if len(triggered) > 3:
        parts.append(f"另有 {len(triggered) - 3} 条")
    if due:
        parts.append(f"{len(due)} 道题到期,跑 board.py resolve")
    # 停牌那只单独写:「没查成」读着像网络出错,其实是一根日线都没有
    parts += [f"{u.get('code')} {u.get('what')},卖出线没查" for u in unchecked if u.get("kind") == board_io.NO_BARS]
    rest = [u for u in unchecked if u.get("kind") != board_io.NO_BARS]
    if rest:
        who = "、".join(dict.fromkeys(u.get("code") or "到期题" for u in rest))
        parts.append(f"没查成:{who}(看 status.json)")
    return ";".join(parts)


def osa_script(msg: str, title: str = TITLE) -> str:
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return f"display notification {q(msg)} with title {q(title)}"


def notify(msg: str) -> str | None:
    """弹系统通知;弹不出来(osascript 起不来、超时、退出码不是 0)→ 返回原因并打到 stderr,弹出来了 → None。
    退出码要看:没给通知权限时 osascript 照样能跑完,只是退出码不是 0,不看就以为通知过了。"""
    try:
        p = subprocess.run(["osascript", "-e", osa_script(msg)], capture_output=True, text=True, timeout=15,
                           check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        why = f"{type(e).__name__}: {e}"
    else:
        if p.returncode == 0:
            return None
        why = f"osascript 退出码 {p.returncode}:{(p.stderr or p.stdout or '').strip()[:200]}"
    print(f"⚠ 系统通知没弹出来:{why}", file=sys.stderr)
    return why


def _code_in(text: str) -> str:
    """结算不了的那句话「题号:原因」里的股票代码:题号是 <run>/<author>/<code>/<type>/<参数>,
    倒数第三段就是代码(run 里的时分秒也是 6 位数字,不能拿正则在整句里找)。认不出给空串。"""
    parts = str(text).split(":", 1)[0].split("/")
    return parts[-3] if len(parts) >= 5 and re.fullmatch(r"\d{6}", parts[-3]) else ""


def cmd_check(args) -> int:
    t = board_io.now()
    config = fb_common.load_config()
    hol, root = actions.config_holidays(config), fb_common.archive_root()
    market = board_io.Market(t)
    codes = [str(s["code"]) for s in config.get("stocks") or []]
    closes, unchecked, gaps = board_io.latest_closes(market, codes, t.date())
    # 近 20 天没有日线(长期停牌)的也算没查成:不写进 status.json 的话,页面上它和「查过了、没事」一模一样
    unchecked += [{**g, "what": f"{g['what']},卖出线和改主意条件没查"} for g in gaps]
    triggered = sell_line_hits(config, closes)          # 取到收盘的照常比,取不到的记进 unchecked
    run = archive.latest_run(root)
    manual = unfetched = 0
    if run is not None:
        hits, manual, unfetched = change_if_hits(run, closes)
        triggered += hits
    due, qerrors = [], []
    try:
        todo = pending(root)
    except Exception as e:          # 题目文件读不出来(比如 chat_questions.jsonl 中间有一行坏了)
        todo = None
        unchecked.append(board_io.failure("", "到期题没查成(题目文件读不出来)", e))
    if todo is not None:
        try:
            done, qerrors, failed = settle(todo, market, hol)
            due = [q["id"] for q, _ in done]
            unchecked += [{**f, "what": f"到期题没查成({f['what']})"} for f in failed]
        except Exception as e:      # 多半是交易日历取不到,也可能是别的:不猜原因,detail 里是真的那句
            unchecked.append(board_io.failure("", "到期题没查成", e))
    # 结算不了的题也写进 unchecked:每日检查没人看着跑,只打在 stderr 等于没说
    unchecked += [{"code": _code_in(e), "what": "到期题结算不了", "detail": e} for e in qerrors]
    # checked_at、triggered、due 三个字段照旧;unchecked 是这次没查成的(哪只、哪一项、为什么),页面顶栏会列出来
    status = {"checked_at": t.isoformat(timespec="seconds"), "triggered": triggered, "due": due,
              "unchecked": unchecked}
    board_io.write_json_atomic(root / STATUS, status)
    for c, (d, px) in sorted(closes.items()):
        print(f"{c} 收盘 {px:g}({d})")
    for x in triggered:
        print(f"已触发:{x['code']} {x['what']}({x['detail']})")
    print(f"到期能结算的题 {len(due)} 道" + (",跑 board.py resolve" if due else ""))
    if manual:
        print(f"另有 {manual} 条改主意条件说的不是本股收盘价位(或者要看不止一天的收盘),要人看")
    if unfetched:
        print(f"另有 {unfetched} 条改主意条件的股票这次没取到收盘,没查(见下面「没查成」)")
    for u in unchecked:
        print(f"❌ 没查成:{board_io.failure_text(u)}", file=sys.stderr)
    rc = 1 if unchecked else 0
    if run is not None:
        try:
            board_io.write_pages(run, config, t.date(), closes=closes)
        except Exception as e:      # status.json 已写好;页面没画成要说出来
            print(f"页面没画成:{type(e).__name__}: {e}", file=sys.stderr)
            rc = 1
    if args.notify and (triggered or due or unchecked):
        why = notify(note_text(triggered, due, unchecked))
        if why:                     # 记进 status.json:定时任务没人看 stderr
            board_io.write_json_atomic(root / STATUS, {**status, "notify_failed": why})
            rc = 1
    return rc


# ── 聊天里给的判断 ──────────────────────────────────────────────────────────
def _day(s: str | None, what: str) -> str | None:
    if s is None:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError as e:
        raise ValueError(f"{what} 要是 YYYY-MM-DD,得到 {s!r}") from e


def question_params(args) -> dict:
    """命令行参数 → 题目 params;不合题型的组合报 ValueError。"""
    wrong = "--ratio" if args.type == "multiple_by" and args.ratio is not None else \
        "--k" if args.type != "multiple_by" and args.k is not None else None
    if wrong:
        raise ValueError(f"{args.type} 不用 {wrong}(multiple_by 写 --k,touch / above_on 写 --ratio)")
    ratio = args.k if args.type == "multiple_by" else args.ratio
    if ratio is None or not ratio > 0:
        raise ValueError(f"{args.type} 要写 {'--k(倍数)' if args.type == 'multiple_by' else '--ratio(价位 ÷ 开题日收盘)'},"
                         "要大于 0")
    key = "k" if args.type == "multiple_by" else "ratio"
    if args.type == "above_on":
        if args.n is not None or args.end is not None or args.dir is not None:
            raise ValueError("above_on 只写 --day,不写 --n / --end / --dir")
        return {key: ratio, "day": _day(args.day, "--day") or _need("above_on 要写 --day")}
    if args.day is not None:
        raise ValueError(f"{args.type} 用 --n 或 --end 定期限,不写 --day")
    if (args.n is None) == (args.end is None):
        raise ValueError(f"{args.type} 要写 --n(往后几个交易日)或 --end(截止日期),只写一个")
    params = {key: ratio, **({"n": int(args.n)} if args.n is not None else {"end": _day(args.end, "--end")})}
    if args.dir is not None:
        if args.type != "touch":
            raise ValueError("--dir 只给 touch 用")
        params["dir"] = args.dir
    return params


def _need(msg: str):
    raise ValueError(msg)


def cmd_add_question(args) -> int:
    t = board_io.now()
    root = fb_common.archive_root()
    try:
        if not 0 <= args.prob <= 100:
            raise ValueError(f"--prob 要是 0~100 的整数,得到 {args.prob}")
        if not (args.note or "").strip():
            raise ValueError("--note 要写这道题从哪来(哪次聊天、怎么换算的)")
        params = question_params(args)
        market = board_io.Market(t)
        opened = _day(args.opened, "--opened")
        if opened is not None and opened > t.date().isoformat():
            raise ValueError(f"开题日 {opened} 还没到")
        if opened is None:
            cal = market.index((t.date() - timedelta(days=board_io.CLOSE_DAYS)).isoformat())
            opened = cal[-1][0] if cal else _need("近 20 天的交易日历是空的,用 --opened 写开题日")
        if any(params.get(k) and params[k] <= opened for k in ("day", "end")):
            raise ValueError(f"日子要晚于开题日 {opened}")
        start = (date.fromisoformat(opened) - timedelta(days=board_io.CLOSE_DAYS)).isoformat()
        p0 = prices.close_on_or_before(market.hfq(args.code, start), opened)
        if p0 is None:
            raise ValueError(f"{args.code} 在 {opened} 及以前 {board_io.CLOSE_DAYS} 天里没有后复权收盘")
        source = question_source(root, opened)
        c0 = prices.close_on_or_before(_closes(market, args.code, start, "add"), opened)
        q = questions.make_question(f"{source}/{opened}", args.author, args.code, args.type, opened, p0[1], params,
                                    questions.binary_forecast(args.prob, "--prob"))
    except ValueError as e:
        print(f"没加:{e}", file=sys.stderr)
        return 1
    have = {x["id"] for x in all_questions(root)} | {r["id"] for r in resolved(root)}
    if q["id"] in have:
        print(f"没加:题号已经有了 {q['id']}(同一个人、同一天、同样的题只记一次)", file=sys.stderr)
        return 1
    rec = {**q, "source": source, "note": args.note.strip(), "added_at": t.isoformat(timespec="seconds"),
           **({"p0_close": c0[1]} if c0 and c0[0] == p0[0] else {})}
    attribution._append(root / CHAT, rec)
    print(f"加了一道题:{q['id']}\n  {board_scores.said(rec)}(开题日 {opened},到期后 board.py resolve 结算)")
    print("  来源:" + ("上线前 journal 旧记录(只做原文比对,不进各层成绩,成绩页单独一栏)" if source == "journal"
                     else "聊天里给的判断(算进作者那一层)"))
    return 0


def question_source(root: Path, opened: str) -> str:
    """开题日早于第一次 board 存档(或者还没有任何存档)→ "journal",否则 "chat"。"""
    runs = archive.list_runs(root)
    return "journal" if not runs or opened < runs[0].parent.name else "chat"


def parse_probs(text: str) -> dict[str, int]:
    out = {}
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, sep, v = part.partition("=")
        if not sep:
            raise ValueError(f"要写成 原因=整数,用逗号隔开,得到 {part!r}")
        try:
            out[k.strip()] = int(v)
        except ValueError as e:
            raise ValueError(f"{k.strip()} 的概率要是整数,得到 {v!r}") from e
    return out


def cmd_confirm(args) -> int:
    root = fb_common.archive_root()
    if args.qid not in {r["id"] for r in resolved(root)}:
        print(f"没写:题号 {args.qid} 不在结算记录里(先 board.py resolve,题号照 resolve 打出来的抄)", file=sys.stderr)
        return 1
    who = str(args.who or "").strip().lower()      # User / USER 都记成 user
    try:
        rec = attribution.confirm(args.qid, parse_probs(args.probs), who, args.note or "", list(args.evidence or []),
                                  root=root)
    except ValueError as e:
        print(f"没写:{e}", file=sys.stderr)
        return 1
    print(f"记下了:{args.qid}  " + "、".join(f"{attribution.CAUSE_LABELS[c]} {v}" for c, v in rec["probs"].items() if v))
    if who not in KNOWN_WHO:
        print(f"⚠ 确认人写的是 {who!r}:只有 user 算人确认。这条照样记下了,成绩页只当提议显示、不进按原因汇总;"
              "要算你自己确认的,再跑一次同样的命令,不写 --who", file=sys.stderr)
    return 0


# ── 重跑 ────────────────────────────────────────────────────────────────────
def cmd_replay(args, runner=None) -> int:
    """runner 只给检查用(换成假的,不建 worktree);命令行走 replay.run_recompute。"""
    root = fb_common.archive_root()
    try:
        run = board_io.find_run(args.run)
        corrected = Path(args.corrected) if args.corrected else None
        res = replay_mod.replay(run, args.code, corrected=corrected, runner=runner)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:   # 超时:git、重算子进程卡住
        print(f"重跑没成:{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    orig = {n: (board_io.read_json(run / "answers" / f"{n}.json") or {}).get("stocks") or {} for n in replay_mod.LAYERS}
    rec = {"at": board_io.now().isoformat(timespec="seconds"), "run": board_io.run_rel(run), "code_ref": args.code,
           "commit": res["code"]["commit"], "frozen": res["code"]["frozen"],
           "corrected": corrected.name if corrected else None, "changed": res["changed"],
           "compare": replay_mod.compare(orig, res)}
    attribution._append(root / REPLAYS, rec)
    print(f"重跑 {rec['run']}:代码 {args.code}({str(rec['commit'])[:7]})"
          + (f" + 修正后的输入 {rec['corrected']}(改了 {'、'.join(rec['changed']) or '没改'})" if corrected else " + 原输入"))
    for line in board_scores.replay_summary(rec["compare"]):
        print(f"  {line}")
    return 0
