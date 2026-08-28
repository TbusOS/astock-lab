#!/usr/bin/env python3
"""consensus —— 券商一致预期与评级变动跟踪(领先指标)。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/consensus.py 300502
    $P ... 300502 --days 60          # 评级回看 60 天(默认 30)
    $P ... 300502 --md ~/c.md

为什么这是领先指标:
    财报是已发生的事。**券商一致预期是市场对未来的共识**,而
    **共识的变动方向**(上调 / 下调 / 维持)比绝对值有用得多 ——
    连续下调是明确预警,中报后集体维持「买入」是明确支撑。
    这一层缺失会直接算错期望值:2026-08-27 实测,用网页搜到的旧预测
    (2026E 178 亿 / 2027E 245 亿)算出 EV = −1.1%(边缘);
    换成 19 家机构的真实一致预期(196 亿 / 334 亿)后 EV = +11.5%(值得加仓)。
    **同一套框架,数据不全就得出相反结论。**

两个数据源(都实测可用,不需要 key):
    盈利预测   同花顺  ak.stock_profit_forecast_ths —— 机构数 / 最小 / 均值 / 最大
    评级变动   巨潮    ak.stock_rank_forecast_cninfo —— 评级 / 是否首次 / 评级变化 /
                                                       前一次评级 / 目标价上下限
    注意巨潮那个是**按日期查全市场**,要按天回看才能找到某只票,所以慢。

代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
from pathlib import Path  # noqa: E402

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402

_OUT = []


def say(s=""):
    print(s)
    _OUT.append(s)


# 评级文字 → 分值,用来算「一致度」与变动方向
RANK_SCORE = {
    "买入": 5, "强烈推荐": 5, "强推": 5, "推荐": 4, "增持": 4, "优于大市": 4,
    "谨慎推荐": 3, "中性": 3, "持有": 3, "同步大市": 3,
    "减持": 2, "回避": 1, "卖出": 1,
}


def sec_forecast(code):
    """盈利预测:每股收益 + 净利润。同花顺源。"""
    say("## 券商一致预期（同花顺）")
    say()
    got = {}
    for ind, label in (("预测年报净利润", "净利润（亿元）"),
                       ("预测年报每股收益", "每股收益（元）")):
        try:
            d = ak.stock_profit_forecast_ths(symbol=code, indicator=ind)
        except Exception as e:
            say(f"（{label} 取数失败：{type(e).__name__}）")
            say()
            continue
        if d is None or not len(d):
            continue
        say(f"### {label}")
        say()
        say("| 年度 | 预测机构数 | 最小 | **均值** | 最大 | 行业平均 | 分歧度 |")
        say("|---|---|---|---|---|---|---|")
        for _, r in d.iterrows():
            lo, mid, hi = r.get("最小值"), r.get("均值"), r.get("最大值")
            # 分歧度 =(最大−最小)÷ 均值,反映机构分歧有多大
            spread = ((hi - lo) / mid * 100) if (mid and pd.notna(mid) and mid) else None
            sp = f"{spread:.0f}%" if spread is not None else "—"
            say(f"| {r.get('年度')} | {r.get('预测机构数')} | {lo} | **{mid}** | {hi} "
                f"| {r.get('行业平均数')} | {sp} |")
        say()
        if ind == "预测年报净利润":
            got = {str(r.get("年度")): r.get("均值") for _, r in d.iterrows()}
            say("> **分歧度 >100% 说明机构之间对未来看法差异极大**，"
                "这时用「均值」做单点假设很危险，应该用最小值当 bear、均值当 base、"
                "最大值当 bull 去算概率加权期望值。")
            say()
    return got


def sec_ratings(code, days):
    """评级变动:按天回看巨潮的全市场评级表,挑出本股。"""
    say(f"## 机构评级变动（巨潮，回看 {days} 天）")
    say()
    rows = []
    today = dt.date.today()
    for back in range(days):
        day = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            r = ak.stock_rank_forecast_cninfo(date=day)
        except Exception:
            continue
        if r is None or not len(r) or "证券代码" not in r.columns:
            continue
        x = r[r["证券代码"].astype(str).str.zfill(6) == code]
        for _, y in x.iterrows():
            rows.append(y)
    if not rows:
        say(f"（回看 {days} 天没有找到 {code} 的评级记录 —— "
            f"可能是这段时间没有机构发研报，也可能巨潮当天数据缺失）")
        say()
        return
    df = pd.DataFrame(rows).drop_duplicates(subset=["研究机构简称", "发布日期"])
    df = df.sort_values("发布日期", ascending=False)
    say("| 发布日期 | 机构 | 评级 | 评级变化 | 前一次 | 目标价 |")
    say("|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        lo, hi = r.get("目标价格-下限"), r.get("目标价格-上限")
        if pd.notna(lo) and pd.notna(hi):
            tp = f"**{lo:.0f}**" if lo == hi else f"{lo:.0f}–{hi:.0f}"
        else:
            tp = "—"
        say(f"| {r.get('发布日期')} | {r.get('研究机构简称')} | **{r.get('投资评级')}** "
            f"| {r.get('评级变化')} | {r.get('前一次投资评级')} | {tp} |")
    say()

    # 变动方向统计 —— 这才是领先信号
    ups = (df["评级变化"].astype(str) == "调高").sum()
    downs = (df["评级变化"].astype(str) == "调低").sum()
    keeps = (df["评级变化"].astype(str) == "维持").sum()
    scores = [RANK_SCORE.get(str(v), None) for v in df["投资评级"]]
    scores = [s for s in scores if s is not None]
    say("### 变动方向（这才是领先信号）")
    say()
    say(f"- 共 **{len(df)}** 家机构发布　·　调高 **{ups}**　·　"
        f"维持 **{keeps}**　·　**调低 {downs}**")
    if scores:
        avg = sum(scores) / len(scores)
        tone = ("一致看多" if avg >= 4.5 else "偏多" if avg >= 4
                else "中性" if avg >= 3 else "偏空")
        say(f"- 平均评级分 **{avg:.2f} / 5**（{tone}）"
            f"　—— 5=买入 4=增持 3=中性 2=减持 1=卖出")
    say()
    if downs == 0 and len(df) >= 3:
        say("> ✅ **零下调**。多家机构在同一时段维持或调高，"
            "说明卖方对未来的看法没有恶化 —— 支撑 base/bull 情景的概率。")
    elif downs > ups:
        say(f"> 🔴 **下调 {downs} 家 > 调高 {ups} 家**。"
            "一致预期在恶化，这是明确预警 —— 应下调 bull 情景概率、上调 bear。")
    else:
        say("> 评级变动方向混杂，没有明确信号。")
    say()

    tps = [r for r in df["目标价格-上限"] if pd.notna(r)]
    if tps:
        say(f"**已披露的目标价**：{len(tps)} 家，"
            f"区间 {min(tps):.0f}–{max(tps):.0f}，均值 {sum(tps)/len(tps):.0f}")
        say()


def sec_ev_hint(code, profits):
    """把一致预期直接接到期望值框架上。"""
    if not profits:
        return
    say("## 接到期望值框架")
    say()
    say("拿上面的一致预期直接填三情景（PE 自己按历史分位定）：")
    say()
    say("```")
    say("bear  = 最小值预测 × 偏低 PE     ← 机构里最悲观的那个")
    say("base  = 均值预测   × 中枢 PE")
    say("bull  = 次年均值   × 中枢 PE     ← 增长兑现一年后")
    say("EV = Σ(概率 × 涨跌幅)　；EV ≤ 0 不新增下注，EV > 10% 且确信度高才加仓")
    say("```")
    say()
    yrs = sorted(profits)
    if len(yrs) >= 2:
        say(f"本股可用:{yrs[0]} 年均值 **{profits[yrs[0]]}** 亿　·　"
            f"{yrs[1]} 年均值 **{profits[yrs[1]]}** 亿")
        say()
    say("> ⚠️ **别用网页搜来的单一预测代替一致预期。**2026-08-27 实测 300502:"
        "搜到的旧数字(2026E 178 亿 / 2027E 245 亿)算出 EV = −1.1%(边缘),"
        "换成 19 家机构真实一致预期(196 亿 / 334 亿)后 EV = **+11.5%**(值得加仓)。"
        "同一套框架,数据不全就得出相反结论。")
    say()


def main():
    p = argparse.ArgumentParser(
        prog="consensus",
        description="券商一致预期与评级变动跟踪 —— 领先指标,不是后视镜")
    p.add_argument("code", nargs="?", help="股票代码,如 300502")
    p.add_argument("--days", type=int, default=30, help="评级回看天数(默认 30)")
    p.add_argument("--md", metavar="文件", help="存 markdown")
    a = p.parse_args()
    if not a.code:
        print(__doc__)
        return 1

    code = a.code.strip().zfill(6)
    say(f"# {code} 券商一致预期与评级")
    say()
    say(f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　·　"
        f"数据源 同花顺(盈利预测) + 巨潮(评级变动)")
    say()
    profits = sec_forecast(code)
    sec_ratings(code, a.days)
    sec_ev_hint(code, profits)

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
