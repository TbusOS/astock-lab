#!/usr/bin/env python3
"""外部视角 —— 历史上处在同样位置的公司,后来怎么样了。

    outside_view.py <code> [--raw data/raw]      (要 venv,依赖 pandas)

## 为什么要这一层

「这家公司净利同比 +91%」听起来很强,**但它没有参照系**。
人做判断时用的是「内部视角」:盯着这家公司自己的故事,
系统性高估它能持续多久 —— 因为故事里每一条都成立,而故事没告诉你
**历史上讲同样故事的一百家公司,四个季度后还剩几家**。

基准率就是那个数。它不回答「这家公司会怎样」,只回答
**「处在同样位置的一批公司,四个季度后有多大比例还保持着」**。

落到我们这套流程上,它只干一件事:**给 bull 情景的概率封顶**。
如果历史上只有 27% 的高增速公司四个季度后还保持 ≥50%,
那 bull 概率就不该给 35% —— 那等于说这只票的胜率是历史同类的 1.3 倍,
而我们并没有一条能支持这个说法的证据。

## 它什么时候不能用 —— 不能用的时候会给出一个看着正常的错数

基准率库是**全市场算法**:所有 A 股按净利同比分组。这有两个前提:
① 增长是同一类东西驱动的 ② 基数不至于小到让同比失去意义。
两个前提一破,算出来的比例照样是个漂亮的百分数,**但它回答的不是这只票的问题**:

- **强周期股**(油气设服):净利同比在拐点附近毫无预测力。周期底部的 +200%
  和成长股的 +200% 完全是两回事,把它们放进同一个样本组,
  算出来的「持续性」其实是在问「周期会不会连续两年在同一个位置」。
- **低基数股**(半导体设备):0.2 亿变 0.6 亿就是 +200%,和几十亿利润的公司
  同组,组内根本不是同一批生意。

所以**哪个行业能用基准率写在 `sector_rules.SECTOR_RULES`**,
不适用的行业这一节只写「不适用 + 原因」,不算 —— 而不是算完再打个折。

## 数据从哪来

`<--raw 的上一级>/perf_cache/*.parquet` —— 东财全市场季度业绩,每期约 4200~5600 家,
12 期已经在盘上。**这里只读缓存,不联网**:算基准率要跨 12 期两两比对,
现抓一次要好几分钟,而且抓失败就得不到结果。

依赖:pandas(在 venv 里)。**拿不到 pandas 时本节明说「跳过」,bull 不封顶** ——
不封顶要在概率表里标出来,否则读者会以为封过了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
sys.path.insert(0, str(_here.parent.parent / "stock-analysis-workflow" / "scripts"))


def cache_dir(raw: Path) -> Path:
    """基准率缓存在哪。**从 `--raw` 推,不靠 `_lab_root()` 猜。**

    `_lab_root()` 是往上找 `.astock-lab-root` 标记文件 —— 脚本在公开仓、
    数据在私有仓的时候,它会找到**公开仓**那个根,然后去一个不存在的目录
    找 parquet,找不到就转去联网抓。跑起来看着只是「有点慢」,
    实际上算的是刚抓的数据而不是盘上那 12 期。
    """
    return raw.parent / "perf_cache"


def load_cache(raw: Path):
    """读盘上的 12 期全市场业绩 → {报告期: DataFrame}。**不联网。**"""
    import pandas as pd
    d = cache_dir(raw)
    out = {}
    for f in sorted(d.glob("*.parquet")):
        try:
            out[f.stem] = pd.read_parquet(f)
        except Exception:
            continue
    return out


def latest_net_yoy(quarters) -> tuple[float | None, str]:
    """最近一个单季的归母净利同比。

    quarterly 里有营收同比,**没有净利同比** —— 净利同比要自己拿去年同期的
    单季净利去比。为什么不用累计数算:累计同比把半年、三季度混在一起,
    而基准率库按单季分组,两边不是同一件事。
    """
    ni = {r["period"]: r["单季归母净利"] for r in quarters
          if r.get("单季归母净利") is not None}
    if not ni:
        return None, "没有单季归母净利"
    last = max(ni)
    prev = f"{int(last[:4]) - 1}{last[4:]}"
    if prev not in ni:
        return None, f"{last} 的上年同期({prev})没有数"
    if ni[prev] <= 0:
        # 去年同期亏损时,同比是个没有含义的数(−3 亿 → +1 亿算 +133%?)
        return None, f"上年同期({prev})净利是 {ni[prev] / 1e8:.2f} 亿(非正),同比没有含义"
    return (ni[last] / ni[prev] - 1) * 100, last


def build(raw: Path, code: str, rules: dict, quarters) -> dict:
    """算这只票的基准率。返回 dict,**任何一步不成立都返回带 why 的失败**。"""
    if not rules.get("基准率"):
        return {"ok": False, "适用": False,
                "why": rules.get("基准率不适用原因") or "本行业不适用"}
    net_yoy, per = latest_net_yoy(quarters)
    gm = None
    for r in reversed(quarters):
        if r.get("单季毛利率") is not None:
            gm = r["单季毛利率"]
            break
    if net_yoy is None:
        return {"ok": False, "适用": True, "why": f"算不出最近单季净利同比:{per}"}
    try:
        import pandas  # noqa: F401
        import baserate
    except (ImportError, SystemExit) as e:
        return {"ok": False, "适用": True, "缺依赖": True,
                "why": f"这一步要 pandas(和 efinance),当前解释器里没有({e!r})。"
                       f"用 venv 重跑:`$VENV/bin/python tools/report.py …`"}
    data = load_cache(raw)
    if not data:
        return {"ok": False, "适用": True,
                "why": f"`{cache_dir(raw)}` 下没有 parquet 缓存。"
                       f"先跑 `$VENV/bin/python tools/baserate.py growth` 生成"}
    r = baserate.calibrate(net_yoy=net_yoy, gm=gm, after=4, data=data)
    if not r:
        return {"ok": False, "适用": True, "why": "calibrate 没返回结果"}
    return {"ok": True, "适用": True, "净利同比": net_yoy, "毛利率": gm,
            "期间": per, "结果": r,
            "样本期": (min(r["periods"]), max(r["periods"])),
            "期数": len(r["periods"])}


def bull_cap(o: dict):
    """bull 概率的上限 = 「4 季后净利同比仍 ≥50%」那条基准率,四舍五入到 5pp。

    ⚠ `baserate` 返回的 `rate` **已经是百分数**(它内部算的是 `kept / n * 100`),
    不要再乘 100。第一版就多乘了一次,印出「bull 概率封顶 2205%」——
    这种错好在会当场露馅;真正危险的是反过来少乘一次,那会印出「封顶 0.22%」,
    看着像个很严的约束,而它其实把 bull 直接压成 0。

    **为什么四舍五入到 5pp**:基准率本身是 12 期滚动平均,精度撑不起
    「27.3%」这种小数 —— 印出小数会让人以为它比实际更准。
    档到 5 个百分点,和概率表其它步骤的粒度一致。

    ⚠ **算不出这条线时不封顶**,并且必须在概率表里标出来。
    悄悄不封顶等于把「没校准」说成「校准过了,结果是不用降」。
    """
    if not o.get("ok"):
        return None, o.get("why", "基准率没算出来")
    g = (o["结果"].get("growth") or {}).get(50)
    if not g:
        gth = o["结果"].get("growth_cohort")
        return None, (f"没有「4 季后仍 ≥50%」这条 —— 这只票当前单季净利同比 "
                      f"{o['净利同比']:+.0f}%,分到 ≥{gth}% 那一组,"
                      f"而 50% 这条线**高于或等于**起始线,那问的就不是「能不能保持」"
                      f"而是「能不能更强」,是另一个问题")
    return round(g["rate"] / 5) * 5, ""


# ── 渲染:报告第 3.5 节 ───────────────────────────────────────────────
def render(o: dict, n: str, L: list) -> None:
    L.append(f"## {n} 外部视角:历史上同样位置的公司后来怎么样了")
    L.append("")
    L.append("上一节是**内部视角** —— 盯着这家公司自己的数据推。内部视角的毛病是"
             "系统性乐观:故事里每一条都成立,而故事不会告诉你"
             "**历史上讲同样故事的一百家公司,四个季度后还剩几家**。"
             "这一节就是那个数。")
    L.append("")
    L.append("> 它**不回答「这家公司会怎样」**,只回答「处在同样位置的一批公司,"
             "四个季度后有多大比例还保持着」。落到这份报告里它只干一件事:"
             "**给第 11 节 bull 情景的概率封顶。**")
    L.append("")
    if not o.get("适用", True):
        L.append(f"# → **本行业不适用,这一节不算**")
        L.append("")
        L.append(f"**原因**:{o['why']}")
        L.append("")
        L.append("**不是「拿不到数据」,是「算出来也答非所问」。** 基准率库是全市场算法,"
                 "按净利同比分组。这有两个前提:增长是同一类东西驱动的、"
                 "基数不至于小到让同比失去意义。前提一破,算出来照样是个漂亮的百分数,"
                 "**只是它回答的不是这只票的问题** —— 那比没有更糟,因为它看着像证据。")
        L.append("")
        L.append("所以第 11 节的 bull 概率**不做基准率封顶**,这一点会在概率表里标出来。")
        L.append("")
        return
    if not o.get("ok"):
        L.append(f"**这一节这次算不出来**:{o['why']}")
        L.append("")
        L.append("bull 概率因此**不封顶**,概率表里会标出来 —— "
                 "「没校准」和「校准过、结果是不用降」是两件事。")
        L.append("")
        return

    r = o["结果"]
    gth, mth = r.get("growth_cohort"), r.get("margin_cohort")
    L.append(f"这只票最近一个单季({o['期间']})归母净利同比 **{o['净利同比']:+.1f}%**"
             + (f",单季毛利率 **{o['毛利率']:.1f}%**" if o.get("毛利率") is not None else "")
             + f"。拿它去全市场历史里找同样位置的公司:")
    L.append("")
    L.append("| 问的问题 | 历史上的比例 | 样本 | 区间 |")
    L.append("|---|---:|---:|---|")
    for hold in (50, 30):
        g = (r.get("growth") or {}).get(hold)
        if not g:
            continue
        L.append(f"| 净利同比 ≥{gth}% 的公司,**4 个季度后仍 ≥{hold}%** "
                 f"| **{g['rate']:.0f}%** | 平均每期 {g['n_base_avg']:.0f} 家 "
                 f"× {g['n_spans']} 个跨期 | {g['lo']:.0f}% ~ {g['hi']:.0f}% |")
    m = r.get("margin")
    if m:
        L.append(f"| 毛利率 ≥{mth}% 的公司,**4 个季度后相对自身不掉 "
                 f"{r.get('margin_drop', 3)} 个百分点** "
                 f"| **{m['rate']:.0f}%** | 平均每期 {m['n_base_avg']:.0f} 家 "
                 f"× {m['n_spans']} 个跨期 | {m['lo']:.0f}% ~ {m['hi']:.0f}% |")
    L.append("")
    L.append(f"**样本期**:{o['样本期'][0]} ~ {o['样本期'][1]},共 {o['期数']} 个报告期,"
             f"数据源是东财全市场季度业绩(每期 4000~5600 家)。"
             f"「区间」那一列是各个跨期的最低值和最高值 —— "
             f"**它比平均值更该看**:区间越宽,说明这个比例本身很不稳定,"
             f"拿平均值当封顶线就不那么牢靠。")
    L.append("")
    L.append("**两条线为什么是 50% 和 30%**:它们和第 10 节的复核条件用的是同一个数 —— "
             "「继续持有的前提:净利同比保持 >50%」「减仓信号:跌破 30%」。"
             "判断条件和基准率用同一条线,这里的百分比才真的在回答那条判断条件,"
             "而不是回答一个相近但不同的问题。")
    L.append("")
    L.append("**毛利率那条走的是「相对自己」的算法**(不掉 3 个百分点),"
             "不是固定线。判断条件说的就是「相对自己不掉」,用固定线会让基准率"
             "去回答一个更容易的问题,算出一个偏高的比例。")
    L.append("")
    cap, why = bull_cap(o)
    if cap is not None:
        L.append(f"# → **bull 概率封顶 {cap}%**")
        L.append("")
        L.append(f"来自上表第一行:历史上同类公司四个季度后仍保持 ≥50% 的比例是 "
                 f"{r['growth'][50]['rate']:.1f}%,四舍五入到 5 个百分点 = {cap}%。"
                 f"第 11 节的概率如果算出 bull 高于这个数,超出的部分划给 base。")
        L.append("")
        L.append("**为什么档到 5 个百分点**:基准率本身是 12 期滚动平均,"
                 "精度撑不起「27.3%」这种小数 —— 印出小数会让人以为它比实际更准。")
    else:
        L.append(f"**这次不封顶**:{why}")
    L.append("")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    a = ap.parse_args()
    import quarterly
    import sector_rules
    for c in a.codes:
        raw = Path(a.raw)
        s = sector_rules.load(raw, c)
        qs = quarterly.build(raw, c, since="2022")["quarters"]
        o = build(raw, c, s["规则"], qs)
        L = []
        render(o, "3.5", L)
        print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
