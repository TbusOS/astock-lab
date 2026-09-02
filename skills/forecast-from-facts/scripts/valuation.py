#!/usr/bin/env python3
"""自建 3+1 PE 带 + 目标价 —— 全部用事后真实数据算,不含任何人的预测。

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

同一个错第二天又犯了一次,换了个样子:2026-09-02 第一版自建带,
历史那条带的分母是**季度末之后四季**的实际净利(纯前瞻),
现价那一格的分母却是**最近三季已披露 + 预测一季**。两边差三个季度 ——
对增速 100%+ 的票,现价 PE 被系统性抬高,三只 AI 票的估值分因此错了一档
(中际旭创现价算出 37.3x,而按同样算法做出来的带 p50 是 35.2x,不是 22.7x)。
**乘数和利润必须算同一段时间**,是这两次错的共同教训。

━━ 这里怎么算:「3+1 PE」━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    3+1 PE = 市值 ÷(最近三季已披露归母净利 + 下一季归母净利)

这个名字就是它的定义。历史带里那个「1」是**事后实际**,现价那一格是
**我们自己的预测** —— 两边唯一的差别就是最后那一季是实际还是预测,
前三季两边都是已经披露的实际值。

**为什么不做成纯前瞻(四季全在未来)**:那样带和现价就不是同一个东西了。
现价那一格永远只能是「3 季实际 + 1 季预测」——我们没有能力预测四个季度。
带若用纯前瞻,分母整整往后挪了三个季度;票增长越快,同一个价格算出的 PE
差得越远,而两个数摆在一起看着都很正常。**先让两边算同一段时间,再谈分位高低。**

对历史上第 i 个季度(报告日 t_i),分母取 ni[i-2 : i+2] ——
i-2 / i-1 / i 这三季在 t_i 当天已经披露,i+1 是它的下一季。
落在 t_i 到 t_{i+1} 之间的每一个交易日 t 都用这个分母:

    市值(t)    = 前复权收盘价(t) × 当前总股本
    3+1 PE(t)  = 市值(t) ÷ (那三季已披露实际 + 下一季实际)

两点让它成为纯事实:
  · 用**前复权**价乘当前股本,等于把历史价格换算到今天的股本上 ——
    送转不再制造假的价格跳空,这和「拿未复权目标价比未复权日线」是同一类坑
  · 分母四季全是**事后真实发生**的净利,不是当时任何人的预测。
    所以这条带回答的是:「市场当年为这家公司这四季的真实利润,实际付了多少倍」

代价:**最近一季算不出来** —— 它的「下一季」还没发生,分母缺一块。
不拿预测去填,填了这条带就不再是事实。
(改这个算法之前,代价是最近四季算不出来;现在只丢一季,带反而更长。)

━━ 目标价怎么出 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    目标价 = (我们预测的 3+1 净利 ÷ 总股本) × 我们选的 3+1 PE

倍数从上面那条带里选,选哪一档由**我们自己预测的增长速度**决定 ——
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


def daily_pb(raw: Path, code: str):
    """前复权日线里的 `pbMRQ` → [(date, pb)]。

    **为什么周期股和设备股要用 PB 不用 PE**:PE 的分母是利润,而周期股在盈利
    低谷时利润接近 0 —— 除以一个很小的数,PE 会飙到几百倍,看着「很贵」,
    实际上那正是周期底部该买的位置;盈利高峰时反过来给出「便宜」的假信号。
    PB 的分母是净资产,它不随景气一年翻几倍,所以在周期股上是**稳定的尺**。

    ⚠ **它不适用于轻资产公司**:净资产里没有品牌、没有研发积累时,PB 衡量的
    只是账面家当。所以这把尺只挂给「有产能、有存货、靠资本开支赚钱」的行业
    (油气设服 / 半导体设备),不往光模块和消费股上套。
    """
    for env in envs(newest(raw, f"quotes/{code}/*-qfq.json")).values():
        if env.get("ok") and env.get("data"):
            out = []
            for row in env["data"]:
                r = dict(zip(K_FIELDS, row))
                try:
                    pb = float(r["pbMRQ"])
                except (TypeError, ValueError):
                    continue
                if pb > 0:
                    out.append((r["date"], pb))
            return sorted(out)
    return []


def pb_band(raw: Path, code: str) -> dict:
    """PB 的全历史分位。**这里没有任何预测** —— 每天的 PB 都是当天的事实。

    和 3+1 PE 带的区别:PE 带要拼「三季实际 + 一季」的分母,最近一季算不出来;
    PB 的分母是最近一期的净资产,券商每天算好挂在日线上,**一天都不丢**。
    """
    pts = daily_pb(raw, code)
    if len(pts) < 250:
        return {"ok": False, "why": f"带 pbMRQ 的交易日只有 {len(pts)} 天(要 250 天以上),"
                                    f"算不出有意义的分位"}
    v = sorted(x[1] for x in pts)

    def q(x):
        return v[min(len(v) - 1, int(len(v) * x))]

    cur = pts[-1][1]
    rank = sum(1 for x in v if x <= cur) / len(v) * 100
    return {"ok": True, "n": len(v), "从": pts[0][0], "到": pts[-1][0],
            "现值": cur, "现值分位": rank,
            "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
            "均值": statistics.mean(v), "最低": v[0], "最高": v[-1]}


def forward_ps_band(raw: Path, code: str, share: float, since="2019") -> dict:
    """3+1 PS 带 —— 和 3+1 PE 带**同一套算法**,只把分母从归母净利换成营业总收入。

    **为什么设备股要看 PS**:半导体设备和面板设备是项目制,一台机器从下单到确认
    收入要好几个季度,利润在这中间会因为一次性的验收、减值、股权激励费用摆来摆去。
    营收比利润稳,而且它更贴近「这门生意做了多大」。

    ⚠ **PS 不管赚不赚钱**:两家营收一样、一家净利率 20% 一家 2%,PS 完全相同。
    所以它只在「利润这把尺已经因为低基数失效」的时候顶上,不单独用来下结论 ——
    要和 PB 一起看。
    """
    qs = quarterly.build(raw, code, since=since)["quarters"]
    rv = [(p["period"], p["报告日"], p["单季营收"]) for p in qs
          if p.get("单季营收") is not None]
    if len(rv) < 8:
        return {"ok": False, "why": f"单季营收只有 {len(rv)} 期,算不出 3+1 PS 带(至少要 8 期)"}
    fwd = {}
    for i in range(2, len(rv) - 1):
        fwd[rv[i][1]] = sum(x[2] for x in rv[i - 2:i + 2])
    all_ends = sorted(x[1] for x in rv)
    pts, dropped = [], 0
    for d, close in daily(raw, code):
        key = d.replace("-", "")
        cur = None
        for e in all_ends:
            if key >= e:
                cur = e
        if cur is None or cur not in fwd or not fwd[cur] or fwd[cur] <= 0:
            dropped += 1
            continue
        pts.append({"date": d, "ps": close * share / fwd[cur]})
    if len(pts) < 60:
        return {"ok": False, "why": f"能算出 3+1 PS 的交易日只有 {len(pts)} 天,样本不够"}
    v = sorted(p["ps"] for p in pts)

    def q(x):
        return v[min(len(v) - 1, int(len(v) * x))]

    return {"ok": True, "n": len(v), "从": pts[0]["date"], "到": pts[-1]["date"],
            "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
            "均值": statistics.mean(v), "最低": v[0], "最高": v[-1],
            "算不了的交易日": dropped}


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
    """逐日算 3+1 PE,给出分布。

    3+1 PE = 市值 ÷(最近三季已披露归母净利 + 下一季归母净利)。
    对季度 i,分母 = ni[i-2 : i+2] —— i-2/i-1/i 三季在 i 的报告日当天已经披露,
    i+1 是它的下一季。**这里的 i+1 是事后实际**;现价那一格的 i+1 是我们的预测。
    两边唯一的差别就在最后那一季,前三季两边都是已披露的实际值。

    ⚠ 为什么不用「季度末之后四季」(纯前瞻):那样带的分母整整比现价那一格
    往后挪三个季度。对增速 100%+ 的票,同一个价格算出来的 PE 能差近一倍,
    而两个数摆在一起看着都很正常 —— 2026-09-02 第一版就是这么错的。
    """
    qs = quarterly.build(raw, code, since=since)["quarters"]
    ni = [(p["period"], p["报告日"], p["单季归母净利"]) for p in qs
          if p.get("单季归母净利") is not None]
    if len(ni) < 8:
        return {"ok": False, "why": f"单季净利只有 {len(ni)} 期,算不出 3+1 PE 带(至少要 8 期)"}

    # 3+1 净利:以每个季度为锚,取它自己和前两季(都已披露)+ 它的下一季
    fwd = {}
    for i in range(2, len(ni) - 1):
        s = sum(x[2] for x in ni[i - 2:i + 2])
        fwd[ni[i][1]] = s                        # key = 该季度末日期 YYYYMMDD
    if not fwd:
        return {"ok": False, "why": "凑不出任何「3+1」窗口(至少要 4 期连续单季净利)"}

    all_ends = sorted(x[1] for x in ni)          # 所有季度末,含算不出窗口的
    pts, dropped = [], 0
    for d, close in daily(raw, code):
        key = d.replace("-", "")
        # ⚠ 必须取**紧挨着 t 之前的那个季度末**,而且它要有完整的 3+1 窗口。
        #   2026-09-02 实测踩到:如果只在「有窗口的季度」里找最后一个,
        #   最近的每一天都会退回到某个旧季度的窗口 —— 那个分母对当天的价格
        #   根本不是同一段时间。算出来的数完全合理,没有任何提示,但它是错的。
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
        return {"ok": False, "why": f"能算出 3+1 PE 的交易日只有 {len(pts)} 天,样本不够"}

    v = sorted(p["fwd_pe"] for p in pts)

    def q(x):
        return v[min(len(v) - 1, int(len(v) * x))]

    return {"ok": True, "n": len(v), "从": pts[0]["date"], "到": pts[-1]["date"],
            "p10": q(.10), "p25": q(.25), "p50": q(.50), "p75": q(.75), "p90": q(.90),
            "均值": statistics.mean(v), "最低": v[0], "最高": v[-1],
            "最近一个可算日": pts[-1]["date"], "最近值": pts[-1]["fwd_pe"],
            "算不了的交易日": dropped,
            "为什么最近算不了": "最近一个季度的「下一季」实际净利还没发生,分母缺一块。"
                              "**不拿预测去填** —— 填了这条带就不再是事实。"
                              "现价那一格用预测补上,是因为它本来就是我们的预测;"
                              "带里补上就等于拿预测当历史。"}


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

    # 现价那一格的 3+1 净利 = 已披露最近三季 + 我们预测的那一季。
    # dict 的 key 沿用旧名(下游读它的地方多),**用户看得到的文字一律叫「3+1 净利」**。
    hist = [r["单季归母净利"] for r in qs if r.get("单季归母净利") is not None]
    p = f.get("归母净利区间")
    our_fwd = None
    if p and len(hist) >= 3:
        base = sum(hist[-3:])
        our_fwd = {"low": base + p["low"], "mid": base + p["mid"], "high": base + p["high"],
                   "说明": f"已披露最近三季实际({'、'.join(r['period'] for r in qs[-3:])})"
                           f" + 我们预测的 {f['预测期']}"}
    # 3+1 营收:和 3+1 净利同一段时间,给 PS 带的现价那一格用
    hrev = [r["单季营收"] for r in qs if r.get("单季营收") is not None]
    rb = f.get("营收区间")
    our_rev = None
    if rb and len(hrev) >= 3:
        base_r = sum(hrev[-3:])
        our_rev = {"low": base_r + rb["low"], "mid": base_r + rb["mid"],
                   "high": base_r + rb["high"]}
    return {"code": code, "总股本": share, "股本截止日": share_date,
            # ⚠ **把 raw 一起带出去。** 下游的行业 / 领先层 / 基准率都要读盘,
            #   而它们只拿得到这个 dict。不带的话,调用方要么各自再传一遍 raw
            #   (五个调用点,漏一个就静默用默认路径),要么去猜工作台在哪。
            "raw": str(raw),
            "现价": spot, "前瞻PE带": band, "预测": f,
            # 另外两把尺。**不是每只都用** —— 用哪把由行业决定,见 sector_rules.py。
            # 这里一律算出来,是因为「算出来但标明不进决策」和「没算」是两件事:
            # 前者读者能看到那把尺长什么样,后者只能猜。
            "PS带": forward_ps_band(raw, code, share),
            "PB带": pb_band(raw, code),
            "我们预测的未来四季营收": our_rev,
            "我们预测的未来四季净利": our_fwd, "生成日": date.today().isoformat()}


def pick_multiple(band: dict, growth_pct: float | None) -> dict:
    """⚠ **2026-09-02 退役,报告里已经没有调用点。** 留着是为了留住这段教训。

    它按「我们预测的增长」挑倍数档(≥60% 取带的上半段,25%~60% 取中段,
    <25% 取下半段)。**这个做法把对增长的看法算了两遍** —— 增长已经进了
    第 11.2 节的概率(领先层那一步),再拿它挑倍数就是同一件事算两次,
    而两次都看着合理。和 2026-09-01 那次「TTM PE 分位乘前瞻利润」是同一类错。

    现在三情景固定用带的 p25 / p50 / p75,对增长的看法**只进概率**。

    下面是它原来的说明,保留:

    选一个 3+1 PE。**理由必须写出来**,不能是拍的。

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
            "理由": f"我们预测的 3+1 净利相对再往前四季同比 {growth_pct:.0f}% —— {why}"}


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
            print(f"   自建 3+1 PE 带({b['从']} ~ {b['到']},{b['n']} 个交易日):")
            print(f"     p10={b['p10']:.1f}  p25={b['p25']:.1f}  p50={b['p50']:.1f}  "
                  f"p75={b['p75']:.1f}  p90={b['p90']:.1f}   均值={b['均值']:.1f}")
            print(f"     最近可算日 {b['最近一个可算日']} 时为 {b['最近值']:.1f}x")
        else:
            print(f"   3+1 PE 带算不出来:{b['why']}")
        o = v["我们预测的未来四季净利"]
        if o:
            print(f"   我们预测的 3+1 归母净利:{o['low']/1e8:.1f} ~ {o['high']/1e8:.1f} 亿")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
