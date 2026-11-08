#!/usr/bin/env python3
"""questions —— 把每一层的答卷拆成一道道能到期核对的题,到期后按真实收盘结算。

它是什么:答卷(统计底线、系统算法、两个 AI 用同一种结构)里每个数都是一句预测,
比如「第 63 个交易日收盘高于起点的概率 45」「最低收盘落在 12 月的概率 30」。
这里把它们一句一句拆成题目存下来,到期后拿后复权收盘判对错,交给评分。
没有它会怎样:答卷只是一堆数,没法逐句说「这句对了、那句错了」,
也就没法按层比谁准、改了算法以后有没有变准。

固定下来的规矩:
  - 日子:opened = as_of(预测用到的最后一个收盘那天),是第 0 个交易日;
    第 n 个交易日 = prices.nth_trading_day(cal, opened, n)。cal 是创业板指不复权日线的日期,
    要从 opened 当天或更早取起(买点题要更早:往前够 60 根算均线)。
  - 价位一律存成比值 = 答卷里的价 ÷ 起点价。按层定除数:统计底线、系统算法用后复权价模拟,
    除以它那一块自己的 start_price;AI 的价和 run_meta 的 start_close(as_of 那天的不复权收盘)同一种,
    一律除以 start_close —— AI 答卷里就算抄进了 start_price 也不用。
    结算比的是后复权比值 hfq[那天] ÷ hfq[as_of]:除权那天不复权价会跳一截,后复权不会。
    比值先取 9 位小数再跟阈值、分段比(2.32 ÷ 2.9 算出来是 0.7999999999999999)。
  - 见底:as_of 收盘(比值 1.0)加上第 1~126 个交易日的收盘里最低的那个。as_of 本身最低时
    算第一个预测月(第 1 个交易日所在的月),一样低时取最早那天 —— 和 baseline.bottom_block 一样。
  - 买点:第 1~250 个交易日里第一次「创业板指收盘 > 它的 60 日线,并且本股后复权收盘 > 本股 60 日线」
    (均线含当天,收盘等于均线不算),和 system_forecast.buy_block 模拟的是同一件事。本股停牌那天不算。
  - 「信号日买 vs as_of 买」(buy_vs_now)检验的是买点规则本身,不是哪一层的预测:
    每次每只股票一道,作者 "rule",没有概率(rule_questions)。
  - 本股日线要走到期末那天或更晚才结算:日线没取到可能只是数据慢了,不能当停牌。
    真停牌的股票复牌后有了新日线再结,结果和当时就结一样(都是用停牌前最后一个收盘);
    日历走过期末 max_wait_days(默认 40)个交易日还没有新日线,就不等了,按停牌结算。
    期末那天本股没有日线,用之前最后一个收盘,note 写「停牌顺延,用 <日期> 收盘」;
    看一段时间的题(见底、碰价、买点),停牌那几天跳过。
  - 结算和计分分开:结果不会再变了就结算(碰价碰到、买点出现当天就结,note 写「提前结算」);
    但每条结果都带 scorable_after = 这道题窗口走完那天,评分只算窗口走完的。
    不然猜中的结得早、没猜中的要等到期末,中途的平均分会偏好看。

题目结构见 make_question,结算结果见 resolve。
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

import fb_common  # 先于 prices:prices 会加载 market.py,它一 import 就删掉代理变量
import prices
from ai_answer import BOTTOM_WINDOW, HORIZONS, ZONE_DAYS
from baseline import LATER
from system_forecast import BUY_HORIZON, BUY_MA

TYPES = ("up", "interval", "bottom_month", "bottom_price_bin", "buy_signal_month", "buy_vs_now",
         "touch", "above_on", "multiple_by")
REF_LAYERS = ("baseline", "system")          # 这两层的价是后复权价,除以它们自己的 start_price
RULE_AUTHOR = "rule"                          # buy_vs_now 的作者:检验买点规则,不属于哪一层
INTERVALS = (("25", "75"), ("10", "90"))     # 每个期限出两道区间题:中间 50% 和中间 80%
HOLD_DAYS = 63                               # buy_vs_now:买进以后拿多少个交易日
RATIO_DIGITS = 6                             # 存下来的比值:6 位小数(价差 0.01 在百元价上是 1e-4,够分开)
CMP_DIGITS = 9                               # 结算时算出的比值先取 9 位小数再比
MAX_WAIT_DAYS = 40                           # 本股日线迟迟不来:日历走过期末这么多个交易日(约两个月)就按停牌结
_RATIO_PARAMS = ("ratio", "k")


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_prob(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 100


def _ratio(price, base: float, where: str) -> float:
    if not (_is_num(price) and price > 0):
        raise ValueError(f"{where} 要是大于 0 的价,得到 {price!r}")
    return round(price / base, RATIO_DIGITS)


def _num_key(x) -> str:
    """题号里的比值:和存下来的数同一个值(6 位小数),去掉末尾的 0。0.9 → '0.9',2.0 → '2'。"""
    return f"{round(float(x), RATIO_DIGITS):.{RATIO_DIGITS}f}".rstrip("0").rstrip(".")


def bin_key(lo, hi) -> str:
    """最低价分段的类别名:'0.6-0.7'。各层都从同一份 categories 生成,所以名字一致。"""
    return f"{float(lo)!r}-{float(hi)!r}"


def binary_forecast(pct, where: str = "") -> dict:
    """是非题的 forecast:{"pct": 0~100 的整数}。评分时自己除以 100。"""
    if not _is_prob(pct):
        raise ValueError(f"{where + ' ' if where else ''}概率要是 0~100 的整数,得到 {pct!r}")
    return {"pct": pct}


def _touch_dir(params: dict) -> str:
    """碰价的方向。没写时按比值定:≥ 1 往上碰(收盘 ≥ 阈值),< 1 往下碰(收盘 ≤ 阈值)。"""
    d = params.get("dir")
    if d is None:
        return "up" if float(params["ratio"]) >= 1.0 else "down"
    if d not in ("up", "down"):
        raise ValueError(f"碰价方向要是 up / down,得到 {d!r}")
    return d


def _span_key(params: dict) -> str:
    if "n" in params:
        return f"n{int(params['n'])}"
    if "end" in params:
        return f"e{params['end']}"
    raise ValueError(f"要 n(往后几个交易日)或 end(截止日期),得到 {params!r}")


def params_key(qtype: str, params: dict) -> str:
    """题号最后一段:同一组参数永远得到同一串。"""
    p = params
    keys = {"up": lambda: f"n{int(p['n'])}",
            "buy_vs_now": lambda: f"n{int(p['n'])}",
            "interval": lambda: f"n{int(p['n'])}-q{p['lo']}-q{p['hi']}",
            "bottom_month": lambda: f"w{int(p['window'])}",
            "bottom_price_bin": lambda: f"w{int(p['window'])}",
            "buy_signal_month": lambda: f"h{int(p['horizon'])}",
            "touch": lambda: f"{_touch_dir(p)}-r{_num_key(p['ratio'])}-{_span_key(p)}",
            "above_on": lambda: f"r{_num_key(p['ratio'])}-d{p['day']}",
            "multiple_by": lambda: f"k{_num_key(p['k'])}-{_span_key(p)}"}
    if qtype not in keys:
        raise ValueError(f"不认识的题型 {qtype!r},只有 {TYPES}")
    return keys[qtype]()


def make_question(run: str, author: str, code: str, qtype: str, opened: str, p0_hfq: float,
                  params: dict, forecast: dict | None) -> dict:
    """一道题:{"id": "<run>/<author>/<code>/<type>/<参数>", "run", "author", "code", "type",
    "opened"(= as_of), "p0_hfq"(as_of 那天的后复权收盘), "params", "forecast"}。
    forecast:是非题 {"pct": 0~100}(区间题另有 lo / hi 两个比值);分类题 {类别: 整数};
    buy_vs_now 是 None(检验规则,没有概率)。params 里的 ratio、k 存成 6 位小数,和题号里写的同一个值。
    聊天里给的判断(touch / above_on / multiple_by)也用它建,题号规则就只有这一份。"""
    if not (_is_num(p0_hfq) and p0_hfq > 0):
        raise ValueError(f"p0_hfq 要是大于 0 的数,得到 {p0_hfq!r}")
    bad = [k for k in _RATIO_PARAMS if k in params and not (_is_num(params[k]) and params[k] > 0)]
    if bad:
        raise ValueError(f"{qtype} 的 {bad} 要是大于 0 的比值,得到 {params!r}")
    params = {k: round(float(v), RATIO_DIGITS) if k in _RATIO_PARAMS else v for k, v in params.items()}
    if qtype == "touch":
        params["dir"] = _touch_dir(params)
    qid = "/".join([run, author, code, qtype, params_key(qtype, params)])
    return {"id": qid, "run": run, "author": author, "code": code, "type": qtype, "opened": opened,
            "p0_hfq": float(p0_hfq), "params": params, "forecast": None if forecast is None else dict(forecast)}


# ── 答卷 → 题目 ─────────────────────────────────────────────────────────────
def _categorical(where: str, probs, keys: list) -> dict:
    if not isinstance(probs, dict) or set(probs) != set(keys):
        got = sorted(probs) if isinstance(probs, dict) else probs
        raise ValueError(f"{where} 的类别要和 categories 一字不差:要 {keys},得到 {got}")
    if not all(_is_prob(v) for v in probs.values()) or sum(probs.values()) != 100:
        raise ValueError(f"{where} 每项要是 0~100 的整数、加起来 100,得到 {probs}")
    return {k: probs[k] for k in keys}


def _bins_forecast(where: str, rows, cat_bins: list) -> dict:
    same = (isinstance(rows, list) and len(rows) == len(cat_bins)
            and all(isinstance(r, list) and len(r) == 3 and _is_num(r[0]) and _is_num(r[1])
                    and abs(r[0] - lo) < 1e-9 and abs(r[1] - hi) < 1e-9 for r, (lo, hi) in zip(rows, cat_bins)))
    if not same:
        raise ValueError(f"{where} 要按 categories 的分段 {cat_bins} 逐段写 [下限, 上限, 概率],得到 {rows!r}")
    return _categorical(where, {bin_key(lo, hi): r[2] for r, (lo, hi) in zip(rows, cat_bins)},
                        [bin_key(lo, hi) for lo, hi in cat_bins])


def _horizon_questions(mk, where: str, hz, base: float) -> list[dict]:
    if not isinstance(hz, dict) or sorted(hz, key=int) != list(HORIZONS):
        raise ValueError(f"{where}.horizons 要正好是 {list(HORIZONS)}")
    out = []
    for h in HORIZONS:
        b, n = hz[h], int(h)
        out.append(mk("up", {"n": n}, binary_forecast(b.get("p_up"), f"{where}.horizons.{h}.p_up")))
        q = b.get("q") or {}
        for lo, hi in INTERVALS:
            r_lo = _ratio(q.get(lo), base, f"{where}.horizons.{h}.q.{lo}")
            r_hi = _ratio(q.get(hi), base, f"{where}.horizons.{h}.q.{hi}")
            if not r_lo < r_hi:
                raise ValueError(f"{where}.horizons.{h}:{lo} 分位要小于 {hi} 分位")
            out.append(mk("interval", {"n": n, "lo": lo, "hi": hi},
                          {"lo": r_lo, "hi": r_hi, **binary_forecast(int(hi) - int(lo))}))
    return out


def _zone_questions(mk, where: str, zones, base: float) -> tuple[list[dict], list[dict]]:
    """买入价位 → 往下碰价题(63 个交易日内收盘 ≤ 这个价)。价位不低于起点价的不出题、记进 skipped:
    那不是「等跌下来再买」的价,而且起点附近的价几天内碰到的机会很大,当题会白送分。"""
    out, skipped = [], []
    for i, z in enumerate(zones or []):
        w = f"{where}.buy.zones[{i}]"
        if not (isinstance(z, list) and len(z) == 2):
            raise ValueError(f"{w} 要是 [价格, 概率],得到 {z!r}")
        fc, r = binary_forecast(z[1], w), _ratio(z[0], base, w)
        if r >= 1.0:
            skipped.append({"where": w, "price": z[0], "ratio": r, "pct": z[1],
                            "why": "买入价位不低于起点价,不是等跌下来再买的价,不出题"})
            continue
        out.append(mk("touch", {"ratio": r, "dir": "down", "n": ZONE_DAYS}, fc))
    return out, skipped


def _stock_questions(mk, where: str, block: dict, base: float, cats: dict) -> tuple[list[dict], list[dict]]:
    out = _horizon_questions(mk, where, block.get("horizons"), base)
    bottom, buy = block.get("bottom"), block.get("buy")
    if not isinstance(bottom, dict) or not isinstance(buy, dict):
        raise ValueError(f"{where} 缺 bottom 或 buy(统计底线要先拼上 system_forecast.baseline_buy 那块)")
    window = int(bottom.get("window", BOTTOM_WINDOW))
    out.append(mk("bottom_month", {"window": window},
                  _categorical(f"{where}.bottom.month_probs", bottom.get("month_probs"), cats["bottom_months"])))
    out.append(mk("bottom_price_bin", {"window": window, "bins": [list(b) for b in cats["price_bins"]]},
                  _bins_forecast(f"{where}.bottom.price_bins", bottom.get("price_bins"), cats["price_bins"])))
    out.append(mk("buy_signal_month", {"horizon": BUY_HORIZON},
                  _categorical(f"{where}.buy.month_probs", buy.get("month_probs"), cats["buy_months"])))
    zq, skipped = _zone_questions(mk, where, buy.get("zones"), base)
    return out + zq, skipped


def _base_price(author: str, code: str, block: dict, run_meta: dict) -> float:
    """价位的除数:统计底线、系统算法用它那一块自己的 start_price(后复权),
    其他作者(AI)一律用 run_meta 的 start_close,答卷里有没有 start_price 都不看。"""
    if author in REF_LAYERS:
        base, what = block.get("start_price"), f"{author}.{code}.start_price"
    else:
        base, what = (run_meta.get("start_close") or {}).get(code), f"run_meta.start_close.{code}"
    if not (_is_num(base) and base > 0):
        raise ValueError(f"{what} 缺了或不是正数:{base!r}")
    return float(base)


def build_questions(answer: dict, run_meta: dict) -> dict:
    """一层的答卷 → {"questions": [题目], "skipped": [没出题的买入价位及原因]}。
    run_meta = {"run": 存档相对路径, "as_of": "YYYY-MM-DD", "start_close": {代码: 不复权收盘},
                "p0_hfq": {代码: as_of 的后复权收盘}, "categories": baseline.categories(...) 的结果}。
    每只股票:3 个期限各 1 道涨跌 + 2 道区间;见底月份、见底分段、买点月份各 1 道;
    每个低于起点价的买入价位 1 道往下碰价(63 个交易日内)。题目按代码排序,同一份答卷永远拆出同样的题。
    作答失败的答卷({"failed": True})没有题;其他不合格(类别对不上、缺块、缺起点价)报 ValueError。"""
    if answer.get("failed"):
        return {"questions": [], "skipped": []}
    author, stocks = answer.get("author"), answer.get("stocks")
    if not isinstance(author, str) or not author or not isinstance(stocks, dict) or not stocks:
        raise ValueError('答卷要是 {"author": 名字, "stocks": {代码: …}}')
    if author == RULE_AUTHOR:
        raise ValueError(f"作者名 {RULE_AUTHOR!r} 留给 buy_vs_now(检验买点规则),答卷不能用")
    run, as_of, cats = run_meta["run"], run_meta["as_of"], run_meta["categories"]
    if answer.get("as_of", as_of) != as_of:
        raise ValueError(f"答卷的 as_of {answer['as_of']} 和 run_meta 的 {as_of} 不一样")
    out, skipped = [], []
    for code in sorted(stocks):
        block = stocks[code]
        if not isinstance(block, dict):
            raise ValueError(f"{author}.{code} 要是对象")
        p0 = (run_meta.get("p0_hfq") or {}).get(code)
        if not (_is_num(p0) and p0 > 0):
            raise ValueError(f"run_meta.p0_hfq.{code} 缺了或不是正数:{p0!r}")

        def mk(qtype, params, forecast, code=code, p0=p0):
            return make_question(run, author, code, qtype, as_of, p0, params, forecast)
        qs, sk = _stock_questions(mk, f"{author}.{code}", block, _base_price(author, code, block, run_meta), cats)
        out += qs
        skipped += sk
    ids = [q["id"] for q in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"题号重复(多半是两个买入价位太近):{dup}")
    return {"questions": out, "skipped": skipped}


def questions_from_answer(answer: dict, run_meta: dict) -> list[dict]:
    """build_questions 的题目那部分。要知道哪些买入价位没出题,用 build_questions。"""
    return build_questions(answer, run_meta)["questions"]


def rule_questions(run_meta: dict) -> list[dict]:
    """「信号日买 vs as_of 买」:每只股票一道,作者 "rule",forecast 为 None。
    它检验的是买点规则本身(各层的买点是同一件事),所以不跟着哪一层出、也没有概率;
    结果 1 = 信号日买、拿 63 天比 as_of 买、拿 63 天涨得多。"""
    run, as_of = run_meta["run"], run_meta["as_of"]
    return [make_question(run, RULE_AUTHOR, code, "buy_vs_now", as_of, p0,
                          {"n": HOLD_DAYS, "horizon": BUY_HORIZON}, None)
            for code, p0 in sorted((run_meta.get("p0_hfq") or {}).items())]


# ── 结算:公用 ───────────────────────────────────────────────────────────────
def _qid(q: dict) -> str:
    return q.get("id", q.get("type", "?"))


def _rv(close: float, p0: float) -> float:
    return round(close / p0, CMP_DIGITS)


def _result(outcome, end_day: str, value, notes: list[str], scorable_after: str) -> dict:
    return {"outcome": outcome, "end_day": end_day, "scorable_after": scorable_after,
            "resolved_at": fb_common.now_cn().isoformat(timespec="seconds"),
            "value": None if value is None else round(float(value), RATIO_DIGITS),
            "note": ";".join(n for n in notes if n)}


def _p0(q: dict, hfq: dict) -> tuple[float, list[str]]:
    """起点后复权价:优先用这次取到的 hfq[opened](和期末价同一次取数,比值最可靠),
    没取到才用存档的 p0_hfq。两个差 0.1% 以上、或用了存档的,都写进 note。"""
    got, stored = prices.close_on_or_before(hfq, q["opened"]), q.get("p0_hfq")
    if got is None:
        if not (_is_num(stored) and stored > 0):
            raise ValueError(f"{_qid(q)}:{q['opened']} 及以前没有本股收盘,题里也没有 p0_hfq")
        return float(stored), [f"这次没取到 {q['opened']} 及以前的本股收盘,起点用存档的 p0_hfq {stored}"]
    if _is_num(stored) and stored > 0 and abs(got[1] / stored - 1) > 1e-3:
        return got[1], [f"起点后复权价和存档时不同(存档 {stored},这次取到 {got[1]}),按这次取到的算"]
    return got[1], []


def _wait(cal: list[str], hfq: dict, day: str, max_wait: int) -> str | None:
    """本股日线走到 day 了没有。走到了(有 day 或更晚的日线)→ "";
    没走到、日历在 day 之后还没走满 max_wait 个交易日 → None(再等等,可能只是数据慢了);
    没走到但已经等满 → 一句说明(按停牌结算)。"""
    last = max(hfq, default=None)
    if last is not None and last >= day:
        return ""
    waited = len(cal) - bisect_right(cal, day)
    if waited < max_wait:
        return None
    return f"本股日线只到 {last},{day} 之后又过了 {waited} 个交易日还没有新日线,按停牌结算"


def _close(q: dict, hfq: dict, day: str) -> tuple[float, str]:
    """day 的后复权收盘;本股那天没有日线 → 之前最后一个收盘 + 「停牌顺延」。"""
    if day in hfq:
        return hfq[day], ""
    got = prices.close_on_or_before(hfq, day)
    if got is None:
        raise ValueError(f"{_qid(q)}:{day} 及以前没有本股收盘")
    return got[1], f"停牌顺延,用 {got[0]} 收盘"


def _after(cal: list[str], opened: str) -> int:
    """opened 在 cal 里的位置(不是交易日就取之前最近的交易日),跟 nth_trading_day 同一个规则。"""
    return bisect_right(cal, opened) - 1


def _nth_est(cal: list[str], start: str, n: int, holidays) -> tuple[str, str]:
    """第 n 个交易日;日历还没走到,就按 prices.future_trading_days(去周末和 holidays)往后估,并写明是估的。
    只用在 scorable_after 上(题已经提前结了,窗口末尾还没到)。"""
    d = prices.nth_trading_day(cal, start, n)
    if d is not None:
        return d, ""
    extra = bisect_right(cal, start) - 1 + n - (len(cal) - 1)
    nxt = date.fromisoformat(cal[-1]) + timedelta(days=1)
    est = prices.future_trading_days(nxt, extra, set(holidays))[-1].isoformat()
    return est, f"计分日 {est} 是按节假日表估的(日历还没走到)"


def _not_in_forecast(q: dict, outcome) -> str:
    fc = q.get("forecast")
    if isinstance(fc, dict) and fc and outcome not in fc:
        return f"结果 {outcome} 不在这道题预测的类别里(节假日估得和实际不一样)"
    return ""


# ── 结算:各题型 ─────────────────────────────────────────────────────────────
def _res_end(q, cal, hfq, opt, check):
    """期末看一个收盘的题(up、interval)。"""
    end = prices.nth_trading_day(cal, q["opened"], int(q["params"]["n"]))
    if end is None:
        return None
    wait = _wait(cal, hfq, end, opt["max_wait"])
    if wait is None:
        return None
    p0, notes = _p0(q, hfq)
    c, note = _close(q, hfq, end)
    r = _rv(c, p0)
    return _result(int(check(r)), end, r, notes + [wait, note], end)


def _res_up(q, cal, hfq, opt):
    return _res_end(q, cal, hfq, opt, lambda r: r > 1.0)       # 持平算不涨


def _res_interval(q, cal, hfq, opt):
    lo, hi = q["forecast"]["lo"], q["forecast"]["hi"]
    return _res_end(q, cal, hfq, opt, lambda r: lo <= r <= hi)


def _bottom(q, cal, hfq, opt):
    """(最低比值, 所在月, 窗口最后一天, notes);日历没走到窗口末尾、或本股日线还没走到,返回 None。"""
    w = int(q["params"]["window"])
    end = prices.nth_trading_day(cal, q["opened"], w)
    if end is None:
        return None
    wait = _wait(cal, hfq, end, opt["max_wait"])
    if wait is None:
        return None
    p0, notes = _p0(q, hfq)
    i0 = _after(cal, q["opened"])
    days = cal[i0 + 1:i0 + w + 1]
    best, best_day = 1.0, None          # as_of 本身:比值 1.0
    gaps = 0
    for d in days:
        if d not in hfq:
            gaps += 1
            continue
        r = _rv(hfq[d], p0)
        if r < best:                    # 严格小于:一样低时留早的那天(含 as_of)
            best, best_day = r, d
    month = (best_day or days[0])[:7]
    notes += [wait, f"最低收盘在 {best_day},比值 {best:.4f}" if best_day
              else f"as_of 收盘就是最低,算第一个月 {month}"]
    if gaps:
        notes.append(f"窗口里 {gaps} 个交易日本股没有日线(停牌),按有收盘的日子算")
    return best, month, end, notes


def _res_bottom_month(q, cal, hfq, opt):
    got = _bottom(q, cal, hfq, opt)
    if got is None:
        return None
    best, month, end, notes = got
    return _result(month, end, best, notes + [_not_in_forecast(q, month)], end)


def _res_bottom_bin(q, cal, hfq, opt):
    got = _bottom(q, cal, hfq, opt)
    if got is None:
        return None
    best, _, end, notes = got
    bins = q["params"]["bins"]
    # 前几段含下限不含上限,最后一段两头都含:数一数有几个「下一段的下限」≤ 比值
    key = bin_key(*bins[sum(1 for lo, _ in bins[1:] if best >= lo)])
    return _result(key, end, best, notes + [_not_in_forecast(q, key)], end)


def _above_ma(series: dict, dates: list[str], day: str, ma: int, what: str) -> bool:
    j = bisect_right(dates, day)
    if j < ma:
        raise ValueError(f"{what}算 {day} 的 {ma} 日线要 {ma} 根收盘,只有 {j} 根(历史要从更早取)")
    return series[day] > sum(series[d] for d in dates[j - ma:j]) / ma


def buy_signal_day(cal: list[str], opened: str, hfq: dict, index_closes: dict, horizon: int = BUY_HORIZON,
                   ma: int = BUY_MA, max_wait_days: int = MAX_WAIT_DAYS) -> str | None:
    """第 1~horizon 个交易日里,买点条件第一次成立的那天('YYYY-MM-DD');
    horizon 天都没成立返回 "later";还不知道(日历没走到,或本股日线还没取到那天)返回 None。
    条件:创业板指收盘 > 它的 ma 日线,并且本股后复权收盘 > 本股 ma 日线(各自最近 ma 根、含当天)。
    本股停牌那天跳过;日线迟迟不来、日历又走过 max_wait_days 个交易日,也当停牌跳过。
    算均线的历史不够、指数缺某个交易日,报 ValueError —— 不悄悄当成「不在线上」,
    那样会把数据没取够错判成「没有买点」。
    同一只股票各层的买点是同一件事,调用方可以算一次,再用 resolve(signal_day=...) 传给各题。"""
    if not index_closes:
        raise ValueError("买点题要 index_closes(创业板指不复权收盘 {日期: 价}),或直接给 signal_day")
    i0 = _after(cal, opened)
    ix_dates, st_dates = sorted(index_closes), sorted(hfq)
    last = st_dates[-1] if st_dates else ""
    for k in range(1, horizon + 1):
        i = i0 + k
        if i >= len(cal):
            return None
        d = cal[i]
        if d not in hfq:
            if d > last and len(cal) - 1 - i < max_wait_days:
                return None             # 本股日线还没取到这天,可能只是数据慢了
            continue
        if d not in index_closes:
            raise ValueError(f"创业板指在 {d} 没有收盘,和交易日历对不上")
        ix_up = _above_ma(index_closes, ix_dates, d, ma, "创业板指")
        st_up = _above_ma(hfq, st_dates, d, ma, "本股")
        if ix_up and st_up:
            return d
    return LATER


def _bad_signal(q, day, why):
    raise ValueError(f"{_qid(q)}:signal_day {day} {why}")


def _signal(q, cal, hfq, opt):
    """(买点那天 | "later" | None, horizon)。"""
    h, signal_day = int(q["params"].get("horizon", BUY_HORIZON)), opt["signal_day"]
    if signal_day is None:
        return buy_signal_day(cal, q["opened"], hfq, opt["index_closes"], h, max_wait_days=opt["max_wait"]), h
    if signal_day == LATER:
        return LATER, h
    if signal_day not in cal:
        if cal and signal_day > cal[-1]:
            return None, h          # 日历还没走到那天
        _bad_signal(q, signal_day, "不是交易日")
    k = bisect_left(cal, signal_day) - _after(cal, q["opened"])
    if not 1 <= k <= h:
        _bad_signal(q, signal_day, f"是第 {k} 个交易日,不在 1~{h} 里(超过 {h} 天要传 later)")
    return signal_day, h


def _gap_note(q, cal, hfq, n) -> str:
    i0 = _after(cal, q["opened"])
    gaps = sum(1 for d in cal[i0 + 1:i0 + n + 1] if d not in hfq)
    return f"期间 {gaps} 个交易日本股没有日线(停牌)" if gaps else ""


def _res_buy_month(q, cal, hfq, opt):
    s, h = _signal(q, cal, hfq, opt)
    if s is None:
        return None
    if s == LATER:
        end = prices.nth_trading_day(cal, q["opened"], h)
        if end is None:
            return None
        notes = [f"{h} 天内没出现买点信号", _gap_note(q, cal, hfq, h), _not_in_forecast(q, LATER)]
        return _result(LATER, end, None, notes, end)
    k = bisect_left(cal, s) - _after(cal, q["opened"])
    sa, est = _nth_est(cal, q["opened"], h, opt["holidays"])
    early = ",提前结算" if s != sa else ""
    return _result(s[:7], s, k, [f"买点信号在 {s}(第 {k} 个交易日){early}", _not_in_forecast(q, s[:7]), est], sa)


def _res_buy_vs_now(q, cal, hfq, opt):
    """信号那天买、拿 n 天的涨幅 > as_of 买、拿 n 天的涨幅 → 1。没有信号:outcome None(结了但不计分)。"""
    s, h = _signal(q, cal, hfq, opt)
    if s is None:
        return None
    if s == LATER:
        end = prices.nth_trading_day(cal, q["opened"], h)
        return None if end is None else _result(None, end, None, [f"{h} 天内没出现买点信号,这道题不计分"], end)
    n = int(q["params"]["n"])
    e_sig = prices.nth_trading_day(cal, s, n)
    if e_sig is None:
        return None
    wait = _wait(cal, hfq, e_sig, opt["max_wait"])
    if wait is None:
        return None
    e_now = prices.nth_trading_day(cal, q["opened"], n)
    p0, notes = _p0(q, hfq)
    c_now, n1 = _close(q, hfq, e_now)
    c_s, n2 = _close(q, hfq, s)
    c_e, n3 = _close(q, hfq, e_sig)
    r_now, r_sig = _rv(c_now, p0) - 1, _rv(c_e, c_s) - 1
    msg = f"信号日 {s} 买、拿 {n} 天涨 {r_sig * 100:.2f}%;as_of 买、拿 {n} 天涨 {r_now * 100:.2f}%"
    return _result(int(r_sig > r_now), e_sig, r_sig - r_now, notes + [wait, n1, n2, n3, msg], e_sig)


def _window(q, cal):
    """(窗口里的交易日, 窗口最后一天或 None, 窗口是否已走完, 计分日)。params 写 n 或 end。
    写 n 时计分日是第 n 个交易日:窗口走完就是最后一天,没走完返回 None(要用时再估)。"""
    p, opened = q["params"], q["opened"]
    i0 = _after(cal, opened)
    if "n" in p:
        n = int(p["n"])
        days = cal[i0 + 1:i0 + n + 1]
        done = len(days) == n
        return days, (days[-1] if done else None), done, (days[-1] if done else None)
    end = p["end"]
    if end <= opened:
        raise ValueError(f"{_qid(q)}:截止日 {end} 不晚于开题日 {opened}")
    days = [d for d in cal[i0 + 1:] if d <= end]
    done = bool(cal) and cal[-1] >= end
    if done and not days:
        raise ValueError(f"{_qid(q)}:{opened} 到 {end} 之间没有交易日")
    return days, (days[-1] if days else None), done, end


def _reach(q, cal, hfq, opt, ratio: float, up: bool):
    """期间有没有一天收盘比值 ≥ ratio(up)或 ≤ ratio(往下)。碰到当天就结算,计分日仍是窗口末尾。"""
    days, last, done, sa = _window(q, cal)
    p0, notes = _p0(q, hfq)
    extreme, gaps = None, 0
    for d in days:
        if d not in hfq:
            gaps += 1
            continue
        r = _rv(hfq[d], p0)
        extreme = r if extreme is None else (max(extreme, r) if up else min(extreme, r))
        if (r >= ratio) if up else (r <= ratio):
            early, est = ("" if d == last else ",提前结算"), ""
            if sa is None:      # 写 n 的窗口还没走完:计分日按节假日表往后估
                sa, est = _nth_est(cal, q["opened"], int(q["params"]["n"]), opt["holidays"])
            return _result(1, d, r, notes + [f"{d} 收盘比值 {r:.4f} 碰到 {_num_key(ratio)}{early}", est], sa)
    if not done:
        return None
    wait = _wait(cal, hfq, last, opt["max_wait"])
    if wait is None:
        return None
    if gaps:
        notes.append(f"期间 {gaps} 个交易日本股没有日线(停牌)")
    best = "期间一直没有收盘" if extreme is None else f"期间{'最高' if up else '最低'}比值 {extreme:.4f}"
    return _result(0, last, extreme, notes + [wait, f"没碰到 {_num_key(ratio)},{best}"], sa)


def _res_touch(q, cal, hfq, opt):
    return _reach(q, cal, hfq, opt, float(q["params"]["ratio"]), _touch_dir(q["params"]) == "up")


def _res_multiple(q, cal, hfq, opt):
    return _reach(q, cal, hfq, opt, float(q["params"]["k"]), True)


def _res_above_on(q, cal, hfq, opt):
    """day 那天收盘比值严格高于 ratio → 1。day 不开市就看它之前最近的交易日。"""
    day, opened = q["params"]["day"], q["opened"]
    if day <= opened:
        raise ValueError(f"{_qid(q)}:日子 {day} 不晚于开题日 {opened}")
    if not cal or cal[-1] < day:
        return None
    end = cal[bisect_right(cal, day) - 1]
    if end <= opened:
        raise ValueError(f"{_qid(q)}:{opened} 到 {day} 之间没有交易日")
    wait = _wait(cal, hfq, end, opt["max_wait"])
    if wait is None:
        return None
    p0, notes = _p0(q, hfq)
    c, note = _close(q, hfq, end)
    r = _rv(c, p0)
    shift = f"{day} 不开市,看 {end} 收盘" if end != day else ""
    return _result(int(r > float(q["params"]["ratio"])), end, r, notes + [wait, shift, note], day)


_RESOLVERS = {"up": _res_up, "interval": _res_interval, "bottom_month": _res_bottom_month,
              "bottom_price_bin": _res_bottom_bin, "buy_signal_month": _res_buy_month,
              "buy_vs_now": _res_buy_vs_now, "touch": _res_touch, "above_on": _res_above_on,
              "multiple_by": _res_multiple}


def resolve(q: dict, cal: list[str], hfq: dict[str, float], *, index_closes: dict | None = None,
            signal_day: str | None = None, holidays=frozenset(), max_wait_days: int = MAX_WAIT_DAYS) -> dict | None:
    """结算一道题。cal = 创业板指交易日(升序,从 opened 或更早取起);hfq = 本股后复权收盘 {日期: 价};
    index_closes = 创业板指不复权收盘 {日期: 价},买点两类题要(或者直接给 signal_day:
    买点那天,或 "later" 表示 250 天内没出现);holidays = 配置里的节假日,只用来估还没到的计分日。
    结果还可能变、或本股日线还没走到期末 → None;定了 → {
      "outcome": 0 | 1 | "<类别>" | None,  None 只有一种:buy_vs_now 在期限内没出现买点,结了但不计分
      "end_day": 看的是哪一天(提前结算的是碰到 / 出现的那天),
      "scorable_after": 窗口走完那天,评分只算这天以后的(提前结算的也一样),
      "resolved_at": 结算时间(北京), "value": 比值或差值(没有可写的数时 None), "note": 说明}。
    cal、hfq 用 prices.trading_calendar / hfq_closes 取:15:05 以前它们会去掉当天盘中那根。"""
    fn = _RESOLVERS.get(q.get("type"))
    if fn is None:
        raise ValueError(f"不认识的题型 {q.get('type')!r},只有 {TYPES}")
    opt = {"index_closes": index_closes, "signal_day": signal_day, "holidays": holidays,
           "max_wait": max_wait_days}
    return fn(q, cal, hfq, opt)
