#!/usr/bin/env python3
"""自建前瞻 PE 带 + 目标价 —— 全部用事后真实数据算,不含任何人的预测。

    valuation.py <code> [--raw data/raw] [--target 2026Q3]

━━ 理论:PE 到底是什么 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PE = 市值 ÷ 净利 = **市场愿意为一块钱利润付多少倍**。它由三件事决定:
  ① 增长速度 —— 利润明年会变多少
  ② 增长的确定性 —— 这个增长有多大概率兑现
  ③ 资本回报 —— 赚这份利润要占用多少资本
增长减速时 PE 必然下移,这是数学不是情绪:同样一块钱当期利润,
背后的未来现金流少了,值的钱就少。

━━ 为什么不能用 TTM PE 的历史分位 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-09-01 我们自己就栽在这儿:拿**过去十二个月** PE 的历史分位,
去乘**明年**的预测利润。分子是明年的利润,乘数却是用今年利润算出来的 ——
同一段增长被算了两遍,单只票的隐含空间能差 45 个百分点。
数字看着全都合理,方向却系统性偏乐观。

━━ 这里怎么算 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
对历史上每一个交易日 t:

    市值(t)     = 前复权收盘价(t) × 当前总股本
    前瞻PE(t)   = 市值(t) ÷ Σ(t 之后四个季度的**实际**归母净利)

两点让它成为纯事实:
  · 用**前复权**价乘当前股本,等于把历史价格换算到今天的股本上 ——
    送转不再制造假的价格跳空,这和「拿未复权目标价比未复权日线」是同一类坑
  · 分母是**事后真实发生**的净利,不是当时任何人的预测。
    所以这条带回答的是:「市场当年为这家公司未来一年的真实利润,实际付了多少倍」

代价:最近四个季度算不出来(未来的净利还没发生)。这是诚实的代价,
不能拿预测去填 —— 填了这条带就不再是事实。

━━ 目标价怎么出 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    目标价 = (我们预测的未来四季净利 ÷ 总股本) × 我们选的前瞻 PE

前瞻 PE 从上面那条带里选,选哪一档由**我们自己预测的增长速度**决定 ——
增长比历史中位快就往上沿靠,慢就往下沿靠。理由写在报告里,可以被质疑。

依赖:无。
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
from datetime import date
import sys
from pathlib import Path

# tools/ 下是软链,__file__ 指向真实目录,但 sys.path[0] 是软链所在目录 ——
# 于是 import quarterly 找不到。**把脚本真实所在目录加进 sys.path**,
# 这样 `tools/report.py` 和 `skills/.../report.py` 两条路径都能直接跑,
# 不需要调用方设 PYTHONPATH(别人 clone 下来第一件事就会卡在这)。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quarterly

K_FIELDS = ["date", "open", "high", "low", "close", "volume",
            "amount", "peTTM", "pbMRQ", "psTTM", "turn"]
PROFIT_FIELDS = ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
                 "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"]
QEND = {"Q1": "-03-31", "Q2": "-06-30", "Q3": "-09-30", "Q4": "-12-31"}


def newest(raw: Path, pat: str):
    fs = sorted(glob.glob(str(raw / pat)))
    return Path(fs[-1]) if fs else None


def envs(p):
    if not p or not Path(p).exists():
        return {}
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def daily(raw: Path, code: str):
    """前复权日线 → [(date, close)]。"""
    for env in envs(newest(raw, f"quotes/{code}/*-qfq.json")).values():
        if env.get("ok") and env.get("data"):
            out = []
            for row in env["data"]:
                r = dict(zip(K_FIELDS, row))
                try:
                    c = float(r["close"])
                except (TypeError, ValueError):
                    continue
                if c > 0:
                    out.append((r["date"], c))
            return sorted(out)
    return []


def total_share(raw: Path, code: str):
    for env in envs(newest(raw, f"financials/{code}/*[0-9].json")).values():
        if "query_profit_data" in env.get("source", "") and env.get("data"):
            r = dict(zip(PROFIT_FIELDS, env["data"][0]))
            try:
                return float(r["totalShare"]), r["statDate"]
            except (KeyError, TypeError, ValueError):
                pass
    return None, None


def forward_pe_band(raw: Path, code: str, share: float, since="2019") -> dict:
    """逐日算前瞻 PE,给出分布。"""
    qs = quarterly.build(raw, code, since=since)["quarters"]
    ni = [(p["period"], p["报告日"], p["单季归母净利"]) for p in qs
          if p.get("单季归母净利") is not None]
    if len(ni) < 8:
        return {"ok": False, "why": f"单季净利只有 {len(ni)} 期,算不出前瞻带(至少要 8 期)"}

    # 未来四季净利:以每个季度末为起点,往后加四季
    fwd = {}
    for i in range(len(ni) - 4):
        s = sum(x[2] for x in ni[i + 1:i + 5])
        fwd[ni[i][1]] = s                        # key = 该季度末日期 YYYYMMDD
    if not fwd:
        return {"ok": False, "why": "凑不出任何「未来四季」窗口"}

    all_ends = sorted(x[1] for x in ni)          # 所有季度末,含没有未来窗口的
    pts, dropped = [], 0
    for d, close in daily(raw, code):
        key = d.replace("-", "")
        # ⚠ 必须取**紧挨着 t 之前的那个季度末**,而且它要有完整的未来四季。
        #   2026-09-02 实测踩到:如果只在「有未来窗口的季度」里找最后一个,
        #   最近一年多的每一天都会退回到 2025Q2 的窗口 —— 而那四季**已经披露完了**,
        #   对 2026 年的价格来说是回头看不是前瞻。算出来 42.8x,数字完全合理,
        #   没有任何提示,但它根本不是前瞻 PE。
        cur = None
        for e in all_ends:
            if key >= e:
                cur = e
        if cur is None or cur not in fwd or not fwd[cur] or fwd[cur] <= 0:
            dropped += 1
            continue
        pts.append({"date": d, "close": close, "fwd_ni": fwd[cur],
                    "fwd_pe": close * share / fwd[cur]})
    if len(pts) < 60:
        return {"ok": False, "why": f"能算出前瞻 PE 的交易日只有 {len(pts)} 天,样本不够"}

    v = sorted(p["fwd_pe"] for p in pts)

    def q(x):
        return v[min(len(v) - 1, int(len(v) * x))]

    return {"ok": True, "n": len(v), "从": pts[0]["date"], "到": pts[-1]["date"],
            "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
            "均值": statistics.mean(v), "最低": v[0], "最高": v[-1],
            "最近一个可算日": pts[-1]["date"], "最近值": pts[-1]["fwd_pe"],
            "算不了的交易日": dropped,
            "为什么最近算不了": "最近四个季度的实际净利还没发生,分母不存在。"
                              "**不拿预测去填** —— 填了这条带就不再是事实。"}


def build(code: str, raw: Path, target: str | None = None) -> dict:
    import forecast
    share, share_date = total_share(raw, code)
    if not share:
        raise SystemExit(f"{code}: 拿不到总股本。先跑 fetch_all --group financials")
    band = forward_pe_band(raw, code, share)
    f = forecast.build(code, raw, target)
    qs = quarterly.build(raw, code, since="2023")["quarters"]
    px = daily(raw, code)
    spot = {"date": px[-1][0], "close": px[-1][1]} if px else None

    # 我们预测的未来四季净利 = 已披露最近三季 + 我们预测的那一季
    hist = [r["单季归母净利"] for r in qs if r.get("单季归母净利") is not None]
    p = f.get("归母净利区间")
    our_fwd = None
    if p and len(hist) >= 3:
        base = sum(hist[-3:])
        our_fwd = {"low": base + p["low"], "mid": base + p["mid"], "high": base + p["high"],
                   "说明": f"已披露最近三季实际({'、'.join(r['period'] for r in qs[-3:])})"
                           f" + 我们预测的 {f['预测期']}"}
    return {"code": code, "总股本": share, "股本截止日": share_date,
            "现价": spot, "前瞻PE带": band, "预测": f,
            "我们预测的未来四季净利": our_fwd, "生成日": date.today().isoformat()}


def pick_multiple(band: dict, growth_pct: float | None) -> dict:
    """选一个前瞻 PE。**理由必须写出来**,不能是拍的。

    第一性原理:PE 跟增长走。所以拿我们预测的增长和这只票历史增长比 ——
    快就往带的上沿靠,慢就往下沿靠。用的是这只票**自己**的历史带,
    不是同业对比,因为同业的商业模式、资本结构、确定性都不一样。
    """
    if not band.get("ok"):
        return {"ok": False, "why": band.get("why")}
    if growth_pct is None:
        return {"ok": True, "低": band["p25"], "中": band["p50"], "高": band["p75"],
                "理由": "没算出增长,直接用历史带的 p25 / p50 / p75"}
    if growth_pct >= 60:
        lo, mid, hi, why = band["p50"], band["p75"], band["p90"], "增长 ≥60%,取带的上半段"
    elif growth_pct >= 25:
        lo, mid, hi, why = band["p25"], band["p50"], band["p75"], "增长 25%~60%,取带的中段"
    else:
        lo, mid, hi, why = band["p10"], band["p25"], band["p50"], "增长 <25%,取带的下半段"
    return {"ok": True, "低": lo, "中": mid, "高": hi,
            "理由": f"我们预测的未来四季净利同比 {growth_pct:.0f}% —— {why}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--target")
    a = ap.parse_args()
    for c in a.codes:
        v = build(c, Path(a.raw), a.target)
        b = v["前瞻PE带"]
        print(f"── {c}  总股本 {v['总股本']/1e8:.2f} 亿股({v['股本截止日']})")
        if v["现价"]:
            print(f"   现价 {v['现价']['close']:.2f}({v['现价']['date']})"
                  f"  市值 {v['现价']['close']*v['总股本']/1e8:,.0f} 亿")
        if b.get("ok"):
            print(f"   自建前瞻 PE 带({b['从']} ~ {b['到']},{b['n']} 个交易日):")
            print(f"     p10={b['p10']:.1f}  p25={b['p25']:.1f}  p50={b['p50']:.1f}  "
                  f"p75={b['p75']:.1f}  p90={b['p90']:.1f}   均值={b['均值']:.1f}")
            print(f"     最近可算日 {b['最近一个可算日']} 时为 {b['最近值']:.1f}x")
        else:
            print(f"   前瞻带算不出来:{b['why']}")
        o = v["我们预测的未来四季净利"]
        if o:
            print(f"   我们预测的未来四季归母净利:{o['low']/1e8:.1f} ~ {o['high']/1e8:.1f} 亿")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
