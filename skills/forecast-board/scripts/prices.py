#!/usr/bin/env python3
"""prices —— 预测看板用的交易日历和后复权价。

两个取数函数都调 stock-analysis-workflow 的 market.fetch_daily(腾讯日线):
    trading_calendar(start, end)  创业板指 sz399006 不复权日线的日期 = 交易日历
    hfq_closes(code, start)       个股 / ETF 后复权收盘 {日期: 价}

为什么交易日按指数日线数、不按个股日线数:个股会停牌,停牌那几天没有日线,
按个股数「第 63 个交易日」会往后错;指数每个交易日都有。
为什么结算用后复权:除权除息那天不复权价会跳一截,拿它比「涨没涨」会把分红送转当成下跌;
后复权把历史价格固定住,只往后累计,比较两天的比值不受以后再分红的影响。

盘中取数:腾讯日线在开盘时段会把当天的实时价当成「当天那根」返回。两个取数函数在北京时间
15:05 以前把当天那根去掉(LIVE_CUTOFF),免得拿盘中价当收盘结算、算均线;前几天的日线不动。
时间默认取 fb_common.now_cn(),检查脚本可以用 now= 传一个固定时间。

market.py 按本文件所在位置找(<仓库>/skills/stock-analysis-workflow/scripts/market.py),
不按工作台找 —— 工作台可能是内部仓,里面那份 market.py 是旧的(没有分段取数和后复权)。

其余三个是纯函数(不联网):
    nth_trading_day       从起点数第 n 个交易日,日历还没走到返回 None
    close_on_or_before    某天或之前最近一天的收盘(停牌顺延)
    future_trading_days   往后排 n 个交易日(去周末和配置里的节假日),给未来的日子标月份
"""
from __future__ import annotations

import importlib.util
import re
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # 先于下面加载 market.py:它一 import 就删掉代理变量,fb_common 要先记下原环境

CALENDAR_INDEX = "sz399006"     # 创业板指:交易日按它的不复权日线日期数
LIVE_CUTOFF = (15, 5)           # 北京时间这个点以前,当天那根日线是盘中实时价
_MARKET_PY = (Path(__file__).resolve().parents[2]
              / "stock-analysis-workflow" / "scripts" / "market.py")


def _load_market():
    """按文件路径加载本仓的 market.py,不走 sys.path —— 那里可能先放了别处的同名模块。"""
    spec = importlib.util.spec_from_file_location("fb_market", _MARKET_PY)
    if spec is None or spec.loader is None or not _MARKET_PY.is_file():
        raise ImportError(f"找不到 market.py:{_MARKET_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


market = _load_market()


def tencent_symbol(code: str) -> str:
    """6 位代码 → 腾讯代码。沪:6(主板、科创板)、9(B 股)、5(沪市 ETF);
    深:0(主板)、2(B 股)、3(创业板)、15 / 16(深市 ETF、LOF)。
    已经带 sh / sz 前缀的原样返回(转小写),指数就这样传,比如 sz399006。
    其他(北交所 4 / 8、债券 11 / 12 等)报 ValueError,不猜。"""
    c = (code or "").strip().lower()
    if re.fullmatch(r"(sh|sz)[0-9]{6}", c):
        return c
    if not re.fullmatch(r"[0-9]{6}", c):
        raise ValueError(f"代码要 6 位数字或 sh/sz 加 6 位数字,得到 {code!r}")
    if c[0] in "695":
        return "sh" + c
    if c[0] in "023" or c[:2] in ("15", "16"):
        return "sz" + c
    raise ValueError(f"认不出 {code!r} 是沪市还是深市(北交所、债券不支持)")


def _beijing(now: datetime | None) -> datetime:
    """现在的北京时间;now 带时区就换算成北京时间,不带时区当作已经是北京时间。"""
    if now is None:
        return fb_common.now_cn()
    return now.astimezone(fb_common.now_cn().tzinfo) if now.tzinfo is not None else now


def drop_live_bar(bars: list[tuple[str, float]], now: datetime | None = None) -> list[tuple[str, float]]:
    """北京时间 15:05 以前,去掉日期是今天的那根(盘中实时价,不是收盘);之后原样返回。
    只认今天:昨天及以前的日线不动。"""
    t = _beijing(now)
    if (t.hour, t.minute) >= LIVE_CUTOFF:
        return list(bars)
    today = t.date().isoformat()
    return [b for b in bars if b[0] != today]


def trading_calendar(start: str, end: str | None = None, now: datetime | None = None) -> list[str]:
    """[start, end] 之间的交易日('YYYY-MM-DD',升序)。end 不给 = 到今天(北京时间)。
    15:05 以前不含今天(见 drop_live_bar)。联网;取数失败的异常原样往上抛。"""
    t = _beijing(now)
    bars = market.fetch_daily(CALENDAR_INDEX, start, end or t.date().isoformat(), fq="")
    return sorted({d for d, _ in drop_live_bar(bars, t)})


def hfq_closes(code: str, start: str, now: datetime | None = None) -> dict[str, float]:
    """从 start 到今天(北京时间)的后复权收盘 {日期: 价},日期升序。15:05 以前不含今天。
    联网;腾讯缺后复权数据时 market.fetch_daily 抛 ValueError,这里不接 ——
    拿不复权价顶替,结算比较会把除权当成下跌。"""
    t = _beijing(now)
    bars = market.fetch_daily(tencent_symbol(code), start, t.date().isoformat(), fq="hfq")
    return dict(sorted(drop_live_bar(bars, t)))


def nth_trading_day(cal: list[str], start: str, n: int) -> str | None:
    """从 start 起数第 n 个交易日(第 0 个是起点)。cal 要升序,并且从 start 当天或更早取起
    (从更晚取起,中间漏掉的交易日这里看不出来,会数错)。

    起点不是交易日(周末、节假日)时,从它之前最近的交易日起数 ——
    跟 close_on_or_before 取起点价是同一个规则:周六开的题,起点价是周五收盘,
    第 1 个交易日是下周一。cal 从周六才开始取(里面没有周五)也一样算对。
    日历还没走到第 n 个返回 None。"""
    if n < 0:
        raise ValueError(f"n 不能是负数,得到 {n}")
    i = bisect_right(cal, start) - 1 + n
    if i < 0 or i >= len(cal):
        return None
    return cal[i]


def close_on_or_before(series: dict[str, float], day: str) -> tuple[str, float] | None:
    """day 当天或之前最近一天的 (日期, 收盘)。停牌那几天没有日线,就顺延用停牌前最后一天。
    day 之前一根都没有返回 None。series 不要求有序。"""
    last = max((d for d in series if d <= day), default=None)
    return None if last is None else (last, series[last])


def future_trading_days(start: date, n: int, holidays: set[str]) -> list[date]:
    """从 start(是交易日就算第一个)往后排 n 个交易日:去掉周六周日和 holidays
    ('YYYY-MM-DD' 字符串)。A 股周末调休上班的日子也不开市,所以周末一律去掉。
    节假日是配置里估的,结果只用来给未来的日子标月份,不用来结算。"""
    if n < 0:
        raise ValueError(f"n 不能是负数,得到 {n}")
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out
