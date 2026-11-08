#!/usr/bin/env python3
"""thinking_test —— 逐条考「想法」:站在过去每个月底,这条想法用当时能看到的数据说涨还是跌,后来对不对。

它是什么:预测的思路(stock-analysis-workflow/references/prediction-thinking.md)把我们的判断拆成
几条能检验的想法 —— 大盘弱要避险、业绩加速股价跟着涨、券商上调股价跟着涨、海外同行先动、
估值太贵涨得少。每条想法在每个出题日给一个 +1(看涨)/ −1(看跌)/ 0(不表态),
这里拿后来的真实涨跌给它打分。
没有它会怎样:只能凭印象说「这套想法挺准」;想法错了也不知道错在哪一环。
为什么这样设计:
- 打分和取数分开。各条想法「当时能看到什么」由各自的历史表模块负责(只用公开日在出题日以前的数),
  这里只管统一打分,几条想法才能用同一把尺子比。
- 不把考出来的比例当成以后的概率 —— 它只回答「这条想法当年灵不灵」。
- 跟抛硬币、跟「每次都猜涨」比:这批股票这些年大涨,每次猜涨也能对六成多,想法要比它强才算有用。
- 主池(今天还活着的光模块股)和对照组(同行业其他股票)分开报,2018–22 和 2023 起分开报。
- 一条想法对所有股票给同一个值时(大盘、云厂开支、海外同行),真正独立的次数接近出题月份数,
  报告里写出月份数,不拿题数冒充独立证据。

用法(在本仓 skills/forecast-board/scripts 下):
    STOCK_LAB=<内部仓> python3 thinking_test.py --out <结果 json>
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

import fb_common  # noqa: F401  先于 prices(market.py 会删代理变量)
import history_test as ht

HORIZONS = (63, 126, 250)
SPLIT = "2023-01-01"
STATES = (1, 0, -1)
MIN_SIDE = 5                      # 看涨、看跌各至少这么多题才算差


# ── 一道题 ──────────────────────────────────────────────────────────────────
def forward_ret(series: dict[str, float], cal: list[str], t: int, h: int) -> float | None:
    """出题日 cal[t] 收盘到 cal[t+h] 收盘的涨跌;任一天没有收盘(停牌)或还没到期 → None。"""
    if t + h >= len(cal):
        return None
    a, b = series.get(cal[t]), series.get(cal[t + h])
    if a is None or b is None:
        return None
    return b / a - 1


def questions(signal, codes, cal: list[str], prices: dict, origins: list[int], horizons=HORIZONS) -> list[dict]:
    """signal(code, day) → −1/0/+1/None。每个出题日、每只股票一道题;signal 给 None 的不出题。"""
    out = []
    for t in origins:
        day = cal[t]
        for c in codes:
            s = signal(c, day)
            if s is None:
                continue
            if s not in STATES:
                raise ValueError(f"信号只能是 −1/0/+1,{c} {day} 得到 {s!r}")
            rets = {h: forward_ret(prices.get(c) or {}, cal, t, h) for h in horizons}
            out.append({"code": c, "day": day, "s": s, "ret": rets})
    return out


# ── 打分 ────────────────────────────────────────────────────────────────────
def _rate(xs) -> float | None:
    return sum(xs) / len(xs) if xs else None


def score(qs: list[dict], h: int) -> dict:
    """一条想法在一个期限上的成绩。只算已到期的题。
    dir_hit:表了态(±1)的题里方向对的比例;always_up:同一批题每次猜涨能对多少。"""
    done = [q for q in qs if q["ret"].get(h) is not None]
    by = {}
    for s in STATES:
        rs = [q["ret"][h] for q in done if q["s"] == s]
        by[str(s)] = {"n": len(rs), "up": _rate([r > 0 for r in rs]),
                      "median": median(rs) if rs else None,
                      "months": len({q["day"][:7] for q in done if q["s"] == s})}
    voted = [q for q in done if q["s"] != 0]
    hit = _rate([(q["s"] > 0) == (q["ret"][h] > 0) for q in voted])
    up = _rate([q["ret"][h] > 0 for q in voted])
    spread = (by["1"]["up"] - by["-1"]["up"]) if by["1"]["up"] is not None and by["-1"]["up"] is not None else None
    return {"n": len(done), "voted": len(voted), "voted_months": len({q["day"][:7] for q in voted}),
            "by_state": by, "dir_hit": hit, "always_up": up,
            "beats_always_up": None if hit is None or up is None else hit - up,
            "up_spread": spread}


def _spread(pairs) -> float | None:
    up = [r > 0 for s, r in pairs if s == 1]
    dn = [r > 0 for s, r in pairs if s == -1]
    if len(up) < MIN_SIDE or len(dn) < MIN_SIDE:
        return None
    return sum(up) / len(up) - sum(dn) / len(dn)


def shift_test(qs: list[dict], h: int, ret_of=None, codes=None) -> dict:
    """错开月份检验:把每只股票的信号整体往后挪 k 个出题月(循环)再跟真实涨跌配对。
    挪过以后信号和之后的涨跌已经没有关系,但信号自己的连续性(一连几个月同向)、
    同一个月各股票之间的关系都保留了。挪出来的差有多少次 ≥ 真实的差,就是「碰巧能有这么大」的比例。
    至少挪 h/21+1 个月,免得挪完的到期区间和原来重叠。
    ret_of(code, day, h):结果那个月这只股票没有信号(比如亏损期没有估值分位)也照样有涨跌,
    要用它取;不给就只用 qs 里有的(只给检查用)。"""
    months = sorted({q["day"] for q in qs if q["ret"].get(h) is not None})
    sig = {(q["day"], q["code"]): q["s"] for q in qs}
    codes = sorted(codes or {q["code"] for q in qs})
    if ret_of is None:
        got = {(q["day"], q["code"]): q["ret"].get(h) for q in qs}
        ret_of = lambda c, d, _h: got.get((d, c))  # noqa: E731
    ret = {(d, c): ret_of(c, d, h) for d in months for c in codes}
    m = len(months)

    def pairs(k):
        out = []
        for i, d in enumerate(months):
            ds = months[(i + k) % m]
            for c in codes:
                s, r = sig.get((ds, c)), ret.get((d, c))
                if s in (1, -1) and r is not None:
                    out.append((s, r))
        return out
    obs = _spread(pairs(0)) if m else None
    gap = h // 21 + 1
    ks = range(gap, m - gap + 1)
    shifted = [x for x in (_spread(pairs(k)) for k in ks) if x is not None]
    p = None if obs is None or not shifted else sum(abs(x) >= abs(obs) for x in shifted) / len(shifted)
    per = sum(len(pairs(k)) for k in ks) / len(ks) if len(ks) else 0
    return {"spread": obs, "chance": p, "shifts": len(shifted), "pairs_per_shift": per}


def report(qs: list[dict], horizons=HORIZONS, ret_of=None, codes=None) -> dict:
    parts = {"all": qs, "early": [q for q in qs if q["day"] < SPLIT], "late": [q for q in qs if q["day"] >= SPLIT]}
    return {p: {str(h): {**score(xs, h), "shift": shift_test(xs, h, ret_of, codes)} for h in horizons}
            for p, xs in parts.items()}


# ── 想法 2a:开支加速 → 之后业绩加速(不看股价)────────────────────────────────
def lead_pairs(lead, follow, codes, cal: list[str], origins: list[int], lags=(63, 126)) -> dict:
    """lead(day) → 板块信号;follow(code, day) → 这只股票当时能看到的业绩加速信号。
    出题日 cal[t] 看 lead,lag 个交易日以后看 follow:lead 表态时,follow 同向的比例,
    对照 follow 本身表态时 +1 的比例(不看 lead 也能猜对多少)。"""
    out = {}
    for lag in lags:
        pairs = []
        for t in origins:
            if t + lag >= len(cal):
                continue
            a = lead(cal[t])
            if a in (None, 0):
                continue
            for c in codes:
                b = follow(c, cal[t + lag])
                if b in (None, 0):
                    continue
                pairs.append((cal[t], a, b))
        same = _rate([a == b for _, a, b in pairs])
        base = _rate([b > 0 for _, _, b in pairs])
        out[str(lag)] = {"n": len(pairs), "months": len({d[:7] for d, _, _ in pairs}), "same_dir": same,
                         "follow_up_rate": base}
    return out


# ── 取价格(带缓存,29 只取一次要好几分钟)──────────────────────────────────────
def load_prices(codes, data_start: str, cache: Path | None, fetch=None) -> tuple[list[str], dict, dict]:
    """返回 (日历, 创业板指, {代码: 后复权收盘})。cache 文件存在且截至日相同就直接用。"""
    if cache and cache.exists():
        got = json.loads(cache.read_text(encoding="utf-8"))
        if set(codes) <= set(got["px"]):
            return got["cal"], got["index"], {c: got["px"][c] for c in codes}
    fetch = fetch or ht.NetFetch()
    index_px = fetch.index(data_start)
    cal = [d for d in fetch.calendar(data_start) if d in index_px]
    px = {c: fetch.hfq(c, data_start) for c in codes}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"cal": cal, "index": index_px, "px": px}), encoding="utf-8")
    return cal, index_px, px


def run(ideas: dict, groups: dict[str, list[str]], cal: list[str], prices: dict, begin: str = "2018-01-01",
        horizons_of=None, lead_tests=None) -> dict:
    """ideas: {名字: signal(code, day)};groups: {"main": [...], "control": [...]}。"""
    origins = ht.month_end_indices(cal, begin)
    pos = {d: i for i, d in enumerate(cal)}

    def ret_of(code, day, h):
        return forward_ret(prices.get(code) or {}, cal, pos[day], h)
    res = {}
    for name, sig in ideas.items():
        hz = (horizons_of or {}).get(name, HORIZONS)
        res[name] = {g: report(questions(sig, codes, cal, prices, origins, hz), hz, ret_of, codes)
                     for g, codes in groups.items()}
    leads = {}
    for name, (lead, follow) in (lead_tests or {}).items():
        leads[name] = {g: lead_pairs(lead, follow, codes, cal, origins) for g, codes in groups.items()}
    return {"meta": {"made_at": datetime.now().isoformat(timespec="seconds"), "as_of": cal[-1], "begin": begin,
                     "split": SPLIT, "origins": len(origins), "groups": groups, "ideas": list(ideas),
                     "note": "每条想法只用当时能看到的数据出题;成绩只说明这条想法当年灵不灵,不当以后的概率"},
            "ideas": res, "leads": leads}


def _p(x):
    return "  —" if x is None else f"{100 * x:3.0f}%"


def print_report(res: dict) -> None:
    for name, by_group in res["ideas"].items():
        print(f"\n== 想法:{name}")
        for g, parts in by_group.items():
            for p in ("early", "late"):
                for h, s in parts[p].items():
                    b = s["by_state"]
                    sh = s["shift"]
                    print(f"  {g:<7} {p:<5} {h:>3}天  表态 {s['voted']:4d} 题/{s['voted_months']:3d} 月  "
                          f"方向对 {_p(s['dir_hit'])}  每次猜涨 {_p(s['always_up'])}  "
                          f"看涨时涨 {_p(b['1']['up'])}({b['1']['n']})  看跌时涨 {_p(b['-1']['up'])}({b['-1']['n']})  "
                          f"碰巧能这么大 {_p(sh['chance'])}")
    for name, by_group in res["leads"].items():
        print(f"\n== 先后关系:{name}")
        for g, lags in by_group.items():
            for lag, s in lags.items():
                print(f"  {g:<7} 隔 {lag} 天  {s['n']} 对/{s['months']} 月  同向 {_p(s['same_dir'])}  "
                      f"(业绩本身加速的比例 {_p(s['follow_up_rate'])})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逐条考想法:当时的信号 vs 后来的涨跌")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rules", required=True, help="rules.json(取主池、对照组名单)")
    ap.add_argument("--signals", required=True, help="各条想法历史表所在目录")
    ap.add_argument("--cache", help="价格缓存文件")
    a = ap.parse_args(argv)
    meta = json.loads(Path(a.rules).read_text(encoding="utf-8"))["meta"]
    groups = {"main": meta["main"], "control": meta["control"]}
    cal, index_px, prices = load_prices(groups["main"] + groups["control"], "2015-01-01",
                                        Path(a.cache) if a.cache else None)
    import thinking_ideas  # 各条想法怎么接到历史表上,单独一个文件
    ideas, horizons_of, leads = thinking_ideas.build(Path(a.signals), cal, index_px)
    res = run(ideas, groups, cal, prices, horizons_of=horizons_of, lead_tests=leads)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print_report(res)
    print(f"\n写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
