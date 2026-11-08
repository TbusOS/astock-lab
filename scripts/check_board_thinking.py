#!/usr/bin/env python3
"""check_board_thinking —— thinking_test(逐条考想法的打分程序)的离线检查。

查三件事:
1. 出题只在每月底、信号函数只拿到出题那天的日期(打分程序自己不偷看);
2. 已知答案:完全看对的信号方向对 100%、一直看涨的信号方向对 = 每次猜涨、随机信号约五成;
3. 先后关系(开支 → 业绩)的「之后」真是隔了 lag 天再看,不是同一天。
每一条都带反向自测:故意写错的版本必须被抓到。
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401
import history_test as ht  # noqa: E402
import thinking_test as tt  # noqa: E402

FAILS = 0


def ok(cond, what):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILS += 1


def _weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


CAL = _weekdays(date(2017, 1, 2), 2000)
_rng = random.Random(1)


def _walk(seed):
    r, p, out = random.Random(seed), 100.0, {}
    for d in CAL:
        p *= 1 + r.gauss(0.0005, 0.02)
        out[d] = p
    return out


PX = {"A": _walk(2), "B": _walk(3)}
ORIGINS = ht.month_end_indices(CAL, "2018-01-01")
POS = {d: i for i, d in enumerate(CAL)}


def check_forward_and_calls():
    print("== 出题、到期")
    ok(tt.forward_ret(PX["A"], CAL, 10, 63) == PX["A"][CAL[73]] / PX["A"][CAL[10]] - 1, "涨跌 = 第 t+h 天收盘 ÷ 第 t 天收盘 − 1")
    ok(tt.forward_ret(PX["A"], CAL, len(CAL) - 5, 63) is None, "还没到期 → None")
    gap = {k: v for k, v in PX["A"].items() if k != CAL[73]}
    ok(tt.forward_ret(gap, CAL, 10, 63) is None, "到期那天停牌 → None(不拿别的日子顶)")
    seen = []
    tt.questions(lambda c, d: seen.append(d) or 1, ["A"], CAL, PX, ORIGINS)
    ends = {CAL[t] for t in ORIGINS}
    ok(set(seen) == ends and all(d[:7] != CAL[POS[d] + 1][:7] for d in seen), "信号函数只在每月最后一个交易日被问到")
    qs = tt.questions(lambda c, d: None if c == "B" else 0, ["A", "B"], CAL, PX, ORIGINS)
    ok(all(q["code"] == "A" for q in qs), "信号给 None 的股票不出题")
    try:
        tt.questions(lambda c, d: 2, ["A"], CAL, PX, ORIGINS)
        ok(False, "信号不是 −1/0/+1 要报错")
    except ValueError:
        ok(True, "信号不是 −1/0/+1 直接报错")


def _future_sign(c, d, h=63):
    r = tt.forward_ret(PX[c], CAL, POS[d], h)
    return None if r is None else (1 if r > 0 else -1)


def check_known_answers():
    print("== 已知答案")
    perfect = tt.score(tt.questions(_future_sign, ["A", "B"], CAL, PX, ORIGINS), 63)
    ok(perfect["dir_hit"] == 1.0, f"偷看未来的「完美信号」方向对 100%(说明打分本身算得对):{perfect['dir_hit']}")
    bull = tt.score(tt.questions(lambda c, d: 1, ["A", "B"], CAL, PX, ORIGINS), 63)
    ok(bull["dir_hit"] == bull["always_up"] and bull["beats_always_up"] == 0, "一直看涨:方向对 = 每次猜涨,不比它强")
    rnd = random.Random(7)
    noise = tt.score(tt.questions(lambda c, d: rnd.choice((1, -1)), ["A", "B"], CAL, PX, ORIGINS), 63)
    ok(0.35 <= noise["dir_hit"] <= 0.65, f"随机信号方向对约五成:{noise['dir_hit']:.2f}")
    mute = tt.score(tt.questions(lambda c, d: 0, ["A"], CAL, PX, ORIGINS), 63)
    ok(mute["voted"] == 0 and mute["dir_hit"] is None, "一直不表态 → 没有方向成绩(不算成五成)")
    hand = [{"code": "A", "day": "2018-01-31", "s": 1, "ret": {63: 0.1}},
            {"code": "A", "day": "2018-02-28", "s": 1, "ret": {63: -0.1}},
            {"code": "B", "day": "2018-02-28", "s": -1, "ret": {63: -0.2}},
            {"code": "B", "day": "2018-03-30", "s": 0, "ret": {63: 0.3}},
            {"code": "B", "day": "2018-04-30", "s": -1, "ret": {63: None}}]
    s = tt.score(hand, 63)
    ok(s["n"] == 4 and s["voted"] == 3 and abs(s["dir_hit"] - 2 / 3) < 1e-12, "手算:到期 4 题、表态 3 题、对 2 题")
    ok(abs(s["always_up"] - 1 / 3) < 1e-12 and s["by_state"]["1"]["up"] == 0.5 and s["by_state"]["-1"]["up"] == 0.0,
       "手算:每次猜涨对 1/3;看涨时涨一半;看跌时没涨")
    ok(s["voted_months"] == 2 and s["by_state"]["-1"]["months"] == 1, "按月份数:表态的题落在 2 个月")
    rep = tt.report(hand + [{"code": "A", "day": "2023-05-31", "s": 1, "ret": {63: 0.2}}], (63,))
    ok(rep["early"]["63"]["n"] == 4 and rep["late"]["63"]["n"] == 1, "2023 年以前、以后分开算")


def check_lead():
    print("== 先后关系:隔 lag 天再看")
    asked = []

    def follow(c, d):
        asked.append(d)
        return 1

    res = tt.lead_pairs(lambda d: 1, follow, ["A"], CAL, ORIGINS, lags=(63,))
    want = {CAL[t + 63] for t in ORIGINS if t + 63 < len(CAL)}
    ok(set(asked) == want, "业绩信号是在出题日之后第 63 个交易日看的")
    ok(res["63"]["same_dir"] == 1.0 and res["63"]["n"] == len(want), "都同向 → 同向 100%")
    # 反向自测:同一天就看业绩的写法,问到的日期会对不上
    asked.clear()
    tt.lead_pairs(lambda d: 1, follow, ["A"], CAL, ORIGINS, lags=(0,))
    ok(set(asked) != want, "反向自测:隔 0 天的版本问到的是出题当天,上面那条检查抓得到")
    none = tt.lead_pairs(lambda d: 0, follow, ["A"], CAL, ORIGINS, lags=(63,))
    ok(none["63"]["n"] == 0 and none["63"]["same_dir"] is None, "开支不表态的月份不算")


def check_shift():
    print("== 错开月份检验:碰巧能有这么大的比例")
    perfect = tt.questions(_future_sign, ["A", "B"], CAL, PX, ORIGINS)
    sp = tt.shift_test(perfect, 63)
    ok(sp["spread"] == 1.0 and sp["chance"] is not None and sp["chance"] <= 0.05,
       f"完美信号:差 100 个百分点,错开后几乎配不出 → 碰巧比例 {sp['chance']}")
    rnd = random.Random(11)
    noise = tt.questions(lambda c, d: rnd.choice((1, -1)), ["A", "B"], CAL, PX, ORIGINS)
    sn = tt.shift_test(noise, 63)
    ok(sn["chance"] is not None and sn["chance"] >= 0.2, f"随机信号:碰巧比例不低 → {sn['chance']:.2f}")
    ok(sp["shifts"] > 40, f"错法够多:{sp['shifts']} 种")
    # 反向自测:不错开(k=0 也算进去)时,完美信号的碰巧比例会被抬高,上面「≤5%」就是检验这一点的
    months = sorted({q["day"] for q in perfect})
    ok(len(months) > 60, "出题月份足够多,错开检验才有意义")
    few = [q for q in noise if q["s"] == 1][:3] + [q for q in noise if q["s"] == -1][:3]
    ok(tt.shift_test(few, 63)["spread"] is None, "看涨、看跌各不到 5 题 → 不算差")
    # 结果那个月没有信号的股票,涨跌也要算进错开后的配对(否则会把亏损期这类月份悄悄丢掉)
    holes = [q for q in noise if not (q["code"] == "B" and q["day"][:4] == "2020")]
    pos = {d: i for i, d in enumerate(CAL)}
    ret_of = lambda c, d, h: tt.forward_ret(PX[c], CAL, pos[d], h)  # noqa: E731
    k = 6
    months = sorted({q["day"] for q in holes if q["ret"].get(63) is not None})
    sig = {(q["day"], q["code"]): q["s"] for q in holes}
    want = sum(1 for i, d in enumerate(months) for c in ("A", "B")
               if sig.get((months[(i + k) % len(months)], c)) in (1, -1) and ret_of(c, d, 63) is not None)
    full = tt.shift_test(holes, 63, ret_of, ["A", "B"])
    part = tt.shift_test(holes, 63)
    ok(full["pairs_per_shift"] > part["pairs_per_shift"],
       f"错开后配上的题:用全部结果 {full['pairs_per_shift']:.1f} 对 > 只用有信号月份的结果(旧写法){part['pairs_per_shift']:.1f} 对")
    ok(want > sum(1 for i, d in enumerate(months) for c in ("A", "B")
                  if sig.get((months[(i + k) % len(months)], c)) in (1, -1) and (d, c) in sig and sig[(d, c)] is not None),
       "B 在 2020 年没有信号的那些月,它的真实涨跌仍可以跟别的月的信号配对")


class FakeFetch:
    def __init__(self):
        self.calls = 0

    def index(self, start):
        self.calls += 1
        return {d: 1000.0 for d in CAL if d >= start}

    def calendar(self, start):
        return [d for d in CAL if d >= start]

    def hfq(self, code, start):
        return {d: v for d, v in PX[code].items() if d >= start}


def check_cache():
    print("== 价格缓存")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "px.json"
        ff = FakeFetch()
        cal, idx, px = tt.load_prices(["A", "B"], "2017-01-01", f, ff)
        cal2, idx2, px2 = tt.load_prices(["A"], "2017-01-01", f, ff)
        ok(ff.calls == 1 and px2["A"] == px["A"] and cal2 == cal, "第二次从缓存读,不再取数")
        got = json.loads(f.read_text())
        ok(set(got["px"]) == {"A", "B"}, "缓存里存了全部股票")
        ff2 = FakeFetch()
        tt.load_prices(["A", "B"], "2017-01-01", None, ff2)
        ok(ff2.calls == 1, "不给缓存文件就每次都取")


def main():
    check_forward_and_calls()
    check_known_answers()
    check_lead()
    check_shift()
    check_cache()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
