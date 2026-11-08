"""thinking_ideas —— 把每条「想法」接到它的历史表上,交给 thinking_test 统一打分。

它是什么:一条想法 = 一个 signal(code, day) → −1/0/+1/None 的函数。
「当时能看到什么」由各历史表模块的 signal_at 负责(只用公开日在 day 以前的记录),
这里只做接线:哪条想法用哪张表、看哪几个期限。
为什么单独一个文件:打分程序(thinking_test)不该知道每张表长什么样;
加一条想法只改这里,打分和检查不动。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import history_test as ht

# 想法名 → (历史表文件名, 模块名, 股票代码 → 表里的键, 看哪几个期限;None = 默认 63/126/250)。
# 模块要有 signal_at(table, key, day)。
TABLES = {
    "2 开支加速股价涨": ("capex.json", "hist_capex", lambda code: "sector", None),
    "4 海外同行先动": ("overseas.json", "hist_overseas", lambda code: "sector", (10, 63, 126)),
    "5 估值太贵涨得少": ("valuation.json", "hist_valuation", lambda code: code, None),
}


def regime_signal(cal: list[str], index_px: dict[str, float]):
    """想法 1「大盘弱时要先避险」:创业板指收盘在含当天的 60 日均线上 +1、下 −1(同 history_test)。"""
    pos = {d: i for i, d in enumerate(cal)}
    closes = [index_px[d] for d in cal]

    def sig(code: str, day: str):
        t = pos.get(day)
        if t is None or t + 1 < 60:
            return None
        return ht.regime_value(closes, t)
    return sig


def _load(signals_dir: Path, fname: str):
    f = signals_dir / fname
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def table_signal(table: dict, module: str, key_of=lambda code: code):
    mod = importlib.import_module(module)

    def sig(code: str, day: str):
        return mod.signal_at(table, key_of(code), day)
    return sig


# 中际 2017 年重组并入苏州旭创,2017Q3~2018Q2 营收同比失真(+2400%~+9500%),
# 2018Q3 的「比上一季加速多少」又拿失真的 2018Q2 当基数 —— 这五期的业绩信号不算。
DISTORTED = {"300308": {"2017Q3", "2017Q4", "2018Q1", "2018Q2", "2018Q3"}}


def earnings_signal(table: dict):
    import hist_earnings as he

    def sig(code: str, day: str):
        rec = he.value_at(table, code, day)
        if rec is not None and rec.get("q") in DISTORTED.get(code, ()):
            return None
        return he.signal_at(table, code, day)
    return sig


def build(signals_dir: Path, cal: list[str], index_px: dict[str, float]):
    """返回 (ideas, horizons_of, lead_tests)。历史表还没生成的想法跳过,并打印出来。"""
    ideas = {"1 大盘弱要避险": regime_signal(cal, index_px)}
    horizons_of, leads = {}, {}
    for name, (fname, module, key_of, hz) in TABLES.items():
        table = _load(signals_dir, fname)
        if table is None:
            print(f"跳过「{name}」:{signals_dir / fname} 还没有")
            continue
        ideas[name] = table_signal(table, module, key_of)
        if hz:
            horizons_of[name] = hz
    cons = _load(signals_dir, "consensus.json")
    if cons is not None:
        import hist_consensus as hk
        # pdf:按 PDF 表头读年份,2018 起都有;live:照线上原样,东财列表 2024 起才带预测数,之前全是 0
        ideas["3 券商上调股价涨(读PDF)"] = lambda code, day: hk.signal_at(cons, code, day, mode="pdf")
        ideas["3 券商上调股价涨(线上口径)"] = lambda code, day: hk.signal_at(cons, code, day, mode="live")
    else:
        print(f"跳过「3 券商上调股价涨」:{signals_dir / 'consensus.json'} 还没有")
    earn = _load(signals_dir, "earnings.json")
    if earn is None:
        print(f"跳过「2b 业绩加速股价涨」和「2a 开支加速→业绩加速」:{signals_dir / 'earnings.json'} 还没有")
        return ideas, horizons_of, leads
    ideas["2b 业绩加速股价涨"] = earnings_signal(earn)
    capex = _load(signals_dir, "capex.json")
    if capex is not None:
        import hist_capex as hc
        leads["2a 开支加速→之后业绩加速"] = (lambda day: hc.signal_at(capex, "sector", day), earnings_signal(earn))
    return ideas, horizons_of, leads
