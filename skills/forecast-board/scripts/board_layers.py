#!/usr/bin/env python3
"""board_layers —— 统计底线、系统算法两层怎么从一份输入包算出来;board.py recompute 也在这里。

它是什么:出预测(board.py forecast)和重算(board.py recompute,replay.py 在别的代码版本里调它)
走同一个函数 compute_layers。
没有它会怎样:两处各算各的,「旧算法 + 原输入」就复现不了存档里的两层 ——
重跑看到的差别,分不清是改了代码,还是两处本来就算得不一样。

输入包自带全部材料(连 run_meta 一起),不读配置、不联网:
    {"items": {键: 一项}, "run_meta": {"as_of", "start", "codes", "holidays", "horizons", "bottom_window",
                                       "buy_horizon", "n_paths", "lookback", "validated", …}}
每只股票用「本股后复权收盘」和「创业板指不复权收盘」按日期取交集后的两条序列(as_of 当天及以前):
    统计底线 = baseline_forecast,另拼上 baseline_buy(信号全 0、同一个种子跑系统算法的买点那块);
    系统算法 = system_forecast,信号按输入包算,哪些信号检验过用 run_meta 里冻结的 validated。
recompute 的 stdout 只有一段 JSON {"baseline": {代码: …}, "system": {代码: …}},进度和错误都走 stderr。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import fb_common  # noqa: F401  先于 baseline:baseline 会加载 prices → market.py,它一 import 就删掉代理变量
import baseline
import prices
import system_forecast

INDEX_KEY = f"index.{prices.CALENDAR_INDEX}"
NEED_META = ("as_of", "start", "codes", "holidays", "horizons", "bottom_window", "buy_horizon", "n_paths",
             "lookback", "validated")


def bars(items: dict, key: str, field: str) -> dict[str, float]:
    """输入包里某项日线 {日期: 收盘};没有这项、取数失败、没有这个字段 → 空。"""
    it = items.get(key)
    if not isinstance(it, dict) or it.get("status") == "failed" or not isinstance(it.get("value"), dict):
        return {}
    return {str(d): float(c) for d, c in it["value"].get(field) or []}


def paired_closes(items: dict, code: str, as_of: str) -> tuple[list[float], list[float]]:
    """(本股后复权收盘, 创业板指收盘),按日期取交集、as_of 当天及以前,第 i 个是同一天。"""
    stock, idx = bars(items, f"prices.{code}", "hfq"), bars(items, INDEX_KEY, "day")
    days = sorted(d for d in set(stock) & set(idx) if d <= as_of)
    if not days:
        raise ValueError(f"{code} 的后复权日线和创业板指日线没有共同的日子(有一样没取到?),两层都算不了")
    return [stock[d] for d in days], [idx[d] for d in days]


def compute_layers(items: dict, meta: dict, seed: int) -> dict:
    """→ {"baseline": {代码: 统计底线(带 buy)}, "system": {代码: 系统算法}}。同样的输入和种子,结果逐位相同。"""
    lack = [k for k in NEED_META if k not in (meta or {})]
    if lack:
        raise ValueError(f"run_meta 缺 {lack}:输入包要自带这些(起点日、股票、节假日、期限……),重算不读配置")
    start, hol = date.fromisoformat(str(meta["start"])), {str(d) for d in meta["holidays"]}
    kw = {"horizons": tuple(int(h) for h in meta["horizons"]), "n_paths": int(meta["n_paths"]),
          "lookback": int(meta["lookback"]), "bottom_window": int(meta["bottom_window"])}
    bh = int(meta["buy_horizon"])
    out = {"baseline": {}, "system": {}}
    for code in [str(c) for c in meta["codes"]]:
        closes, idx = paired_closes(items, code, str(meta["as_of"]))
        b = baseline.baseline_forecast(closes, start, hol, seed=seed, **kw)
        b["buy"] = system_forecast.baseline_buy(closes, idx, start, hol, seed=seed, buy_horizon=bh, **kw)
        out["baseline"][code] = b
        sig = system_forecast.signals_from_inputs(items, code)
        out["system"][code] = system_forecast.system_forecast(closes, idx, start, hol, sig, meta["validated"],
                                                              seed=seed, buy_horizon=bh, **kw)
    return out


def cmd_recompute(args) -> int:
    """board.py recompute --inputs <文件> --seed <n>:只读那个文件,stdout 只写那段 JSON。"""
    try:
        obj = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"读不了输入包 {args.inputs}:{e}", file=sys.stderr)
        return 2
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), dict) or not isinstance(obj.get("run_meta"), dict):
        print('输入包要是 {"items": …, "run_meta": …}:老的平铺输入包没有 run_meta(起点日、节假日、期限),'
              "重算不了", file=sys.stderr)
        return 2
    try:
        layers = compute_layers(obj["items"], obj["run_meta"], int(args.seed))
    except ValueError as e:
        print(f"重算失败:{e}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(layers, ensure_ascii=False, sort_keys=True))
    sys.stdout.flush()
    return 0
