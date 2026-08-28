#!/usr/bin/env python3
"""baserate —— 基准率:用全市场历史数据校准「这种增速能持续多久」。

用法:
    P=$VENV/bin/python
    B=$LAB/tools/baserate.py

    # 高增速的持续性:同比 >90% 的公司,4 个季度后还保持 >50% 的比例
    $P $B growth --threshold 90 --hold 50 --after 4

    # 毛利率高位的持续性
    $P $B margin --threshold 45 --drop 5 --after 4

    # 直接给某只票做校准(自动读它当前的增速,再查同类历史)
    $P $B calibrate 300502

    $P $B growth --md ~/br.md          # 存 markdown

为什么必须有这一层(Mauboussin 的核心之一):
    「净利同比 +91%」听起来很强,但**没有参照系**。人做判断时用的是
    「内部视角」—— 盯着这家公司的故事,系统性高估它能持续多久。
    **基准率是外部视角**:历史上处在同样位置的公司,后来怎么样了。

    Mauboussin 的研究显示,**高估值倍数往往先于低于平均的回报**,
    正是因为投资者的预期系统性偏乐观。基准率就是用来对冲这种偏乐观的。

    落到期望值框架上:基准率**直接决定 bull 情景的概率**。
    如果历史上只有 20% 的高增速公司能在四个季度后保持,
    那 bull 概率就不该给 35%,该给 20% 附近。

    参见 skill §1.5 前瞻性思维 · docs/06-投资思维框架对比.md。

数据源:东财全市场季度业绩(efinance get_all_company_performance),
    每期约 4200-5600 家。拉 N 期做时间序列,零成本零 key。
    首次跑会比较慢(每期约 2-3 秒 + 缓存写盘),之后走缓存。

缓存:$LAB/data/perf_cache/<报告期>.parquet
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

# ── 工作台根目录定位 ────────────────────────────────────────────────────────
# ⚠ 这段在多个脚本里各有一份,**必须逐字一致**,由 scripts/check_gates.sh 比对。
#   为什么不做成共享模块:这几个脚本分属不同 skill,跨 skill import 要先解决
#   「怎么找到那个模块」—— 那是同一个问题,套娃解决不了。5 行代码复制若干份
#   加一道比对闸,比一个需要自己被找到的定位模块简单。
def _lab_root():
    """工作台根目录。顺序:$STOCK_LAB → 往上找 .astock-lab-root → 老默认路径。"""
    import os as _os
    e = _os.environ.get("STOCK_LAB")
    if e and Path(e).is_dir():
        return Path(e)
    for d in Path(__file__).resolve().parents:
        if (d / ".astock-lab-root").exists():
            return d
    for d in (Path.home() / "astock-lab-private", Path.home() / "astock-lab",
              Path.home() / "claude-tools" / "astock-lab-private",
              Path.home() / "claude-tools" / "astock-lab",
              Path.home() / "claude-tools" / "stock-lab"):
        if d.is_dir():
            return d
    return Path.cwd()


LAB = _lab_root()
CACHE = LAB / "data" / "perf_cache"

_EF_REPO = LAB / "repos" / "efinance"
try:
    import efinance as ef
except ImportError:
    if _EF_REPO.is_dir():
        sys.path.insert(0, str(_EF_REPO))
    try:
        import efinance as ef
    except ImportError:
        sys.exit("需要 efinance:pip install efinance 或 clone 到 repos/efinance")

import pandas as pd  # noqa: E402

_OUT = []


def say(s=""):
    print(s)
    _OUT.append(s)


def load_period(date, quiet=False):
    """取某个报告期的全市场业绩,带本地缓存。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{date}.parquet"
    if f.exists():
        return pd.read_parquet(f)
    if not quiet:
        print(f"  拉取 {date} …", end="", flush=True)
    t0 = time.time()
    d = ef.stock.get_all_company_performance(date=date)
    keep = [c for c in ["股票代码", "股票简称", "净利润", "净利润同比增长",
                        "营业收入", "营业收入同比增长", "销售毛利率",
                        "净资产收益率"] if c in d.columns]
    d = d[keep].copy()
    d["股票代码"] = d["股票代码"].astype(str).str.zfill(6)
    try:
        d.to_parquet(f)
    except Exception:
        d.to_csv(f.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    if not quiet:
        print(f" {len(d)} 行 {time.time()-t0:.1f}s")
    return d


def periods(n):
    """最近 n 个报告期(倒序:最新在前)。

    取不到报告期列表时回落到缓存目录里已有的期 —— 基准率算的是历史统计,
    少最新一期不改变结论;但整个功能因为一次网络抖动就不可用是不能接受的,
    尤其它现在被 preport 当成报告的一节在调。
    """
    try:
        d = ef.stock.get_all_report_dates()
        got = [str(x)[:10] for x in d["报告日期"].head(n)]
        if got:
            return got
    except Exception:
        pass
    cached = sorted((f.stem for f in CACHE.glob("*.parquet")), reverse=True)
    if not cached:
        raise RuntimeError("报告期列表取不到，且本地缓存为空 —— 先联网跑一次 baserate")
    return cached[:n]


def _cohort(base_df, later_df, col, threshold, hold, higher_is_better=True):
    """base 期里满足条件的一组公司,在 later 期还满足较宽条件的比例。"""
    if higher_is_better:
        cohort = base_df[base_df[col] >= threshold]
    else:
        cohort = base_df[base_df[col] <= threshold]
    if not len(cohort):
        return None
    m = cohort.merge(later_df[["股票代码", col]], on="股票代码",
                     suffixes=("_base", "_later"))
    if not len(m):
        return None
    later_col = f"{col}_later"
    if higher_is_better:
        kept = (m[later_col] >= hold).sum()
    else:
        kept = (m[later_col] <= hold).sum()
    return {"n_base": len(cohort), "n_matched": len(m),
            "n_kept": int(kept), "rate": kept / len(m) * 100}


def _cohort_drop(base_df, later_df, col, threshold, max_drop):
    """起始 ≥ threshold 的公司里,N 期后该指标**相对自身起始值**下滑
    不超过 max_drop 个百分点的比例。

    ⚠ 为什么不能用固定线代替(2026-08-27 接进 preport 时发现):
    报告里的判据是「毛利率不掉 3pp 以上」,这是**相对自己**的线。
    若把它翻译成固定线(「起始 ≥45% 的公司,后来仍 ≥42%」),
    一家起始 48.4% 的公司掉到 43% —— 违反了判据(该 ≥45.4%),
    却仍算进固定线的分子。基准率于是系统性偏乐观,报告里那句
    「历史基准率 90%」在回答一个更容易的问题。
    实测这个口径差把 300502 的毛利率基准率从 90% 拉到真实水平。
    """
    cohort = base_df[base_df[col] >= threshold]
    if not len(cohort):
        return None
    m = cohort.merge(later_df[["股票代码", col]], on="股票代码",
                     suffixes=("_base", "_later"))
    if not len(m):
        return None
    kept = ((m[f"{col}_later"] - m[f"{col}_base"]) >= -max_drop).sum()
    return {"n_base": len(cohort), "n_matched": len(m),
            "n_kept": int(kept), "rate": kept / len(m) * 100}


# ── 库接口:给别的脚本调(preport 走这一层,不经过 say() 打印)─────────────
def load_all(n=12, quiet=True):
    """一次把 n 期全市场业绩装进内存(带盘缓存),返回 {报告期: DataFrame}。

    为什么单独有这个:算一个基准率要跨多期两两比对,算三个基准率如果各自
    重新 load 一遍就是三倍 IO 和三倍网络。调用方 load 一次,下面的 rate()
    复用同一份内存数据。
    """
    out = {}
    for p in periods(n):
        try:
            out[p] = load_period(p, quiet=quiet)
        except Exception:
            pass
    return out


def rate(data, col, threshold, hold, after, higher_is_better=True,
         max_drop=None):
    """纯计算:滚过所有可用的「起始期 → after 期后」配对,返回平均保持率。

    hold      固定线口径:N 期后该指标仍 ≥ hold。适合「净利同比保持 >50%」
              这种**判据本身就是绝对线**的情形。
    max_drop  相对口径:N 期后相对自身起始值下滑不超过 max_drop 个百分点。
              适合「毛利率不掉 3pp 以上」这种**判据是相对自己**的情形。
              给了 max_drop 就走相对口径,hold 被忽略。

    两个口径不能混用 —— 用错会让基准率回答一个和判据不同的问题,见
    _cohort_drop 的注释。不打印、不联网(data 由 load_all 提供)。
    样本期不足返回 None。返回 {"rate","n_spans","lo","hi","n_base_avg"}。
    """
    ordered = sorted(data)
    vals, bases = [], []
    for i, base in enumerate(ordered):
        j = i + after
        if j >= len(ordered):
            break
        later = ordered[j]
        if col not in data[base].columns or col not in data[later].columns:
            continue
        if max_drop is not None:
            r = _cohort_drop(data[base], data[later], col, threshold, max_drop)
        else:
            r = _cohort(data[base], data[later], col, threshold, hold,
                        higher_is_better)
        if r:
            vals.append(r["rate"])
            bases.append(r["n_base"])
    if not vals:
        return None
    return {"rate": sum(vals) / len(vals), "n_spans": len(vals),
            "lo": min(vals), "hi": max(vals),
            "n_base_avg": sum(bases) / len(bases)}


def calibrate(net_yoy=None, gm=None, periods_n=12, after=4,
              growth_holds=(50, 30), margin_drop=3, data=None):
    """给定「当前净利同比 / 毛利率」,算出对应的一组基准率。

    这是 preport 接进来的入口 —— 它在第 2 层已经拿到这两个数,不必再查一遍。

    growth_holds 里每个值对应报告里的**一条判据**,不是随手取的:
        50 = 「继续持有的前提:净利同比保持 >50%」
        30 = 「减仓信号:净利同比跌破 30%」
    判据线和基准率线用同一个数,报告里那句「历史基准率 X%」才真的在回答
    那条判据,而不是回答一个相近但不同的问题。

    毛利率走**相对口径**(margin_drop):判据说的「不掉 3pp 以上」是相对
    自己的线,不是固定线。用固定线会让基准率回答一个更容易的问题,见
    _cohort_drop 的注释。

    返回 {"growth_cohort", "growth": {hold: rate_dict}, "margin_cohort",
          "margin_drop", "margin": rate_dict, "periods", "after"}
    """
    if data is None:
        data = load_all(periods_n)
    if not data:
        return None
    out = {"periods": sorted(data), "after": after, "growth": {}, "margin": None}
    if net_yoy is not None and net_yoy == net_yoy:
        # 档位向下取整到 10 的倍数 —— +91% 和 +95% 不该拆成两个样本量都不够的组
        gth = max(20, int(net_yoy // 10 * 10))
        out["growth_cohort"] = gth
        for h in growth_holds:
            if h >= gth:      # 保持线高于起始线时,问的是「还能更强吗」,不是持续性
                continue
            r = rate(data, "净利润同比增长", gth, h, after)
            if r:
                out["growth"][h] = r
    if gm is not None and gm == gm:
        mth = int(gm // 5 * 5)
        out["margin_cohort"] = mth
        out["margin_drop"] = margin_drop
        # 相对口径:与报告里「毛利率不掉 3pp 以上」这条判据问的是同一件事
        out["margin"] = rate(data, "销售毛利率", mth, None, after,
                             max_drop=margin_drop)
    return out


def cmd_growth(a):
    """高增速的持续性。"""
    ps = periods(a.periods)
    say(f"# 基准率:净利同比 ≥ {a.threshold}% 的公司，"
        f"{a.after} 个季度后还保持 ≥ {a.hold}% 的比例")
    say()
    say(f"> 数据源:东财全市场季度业绩　·　报告期 {ps[-1]} ~ {ps[0]}　·　"
        f"共 {len(ps)} 期")
    say()
    print("拉取数据（首次较慢，之后走缓存）：")
    data = {}
    for p in ps:
        try:
            data[p] = load_period(p)
        except Exception as e:
            print(f"  {p} 取数失败:{type(e).__name__}")
    print()

    col = "净利润同比增长"
    rows = []
    ordered = sorted(data)                      # 时间正序
    for i, base in enumerate(ordered):
        j = i + a.after
        if j >= len(ordered):
            break
        later = ordered[j]
        if col not in data[base] or col not in data[later]:
            continue
        r = _cohort(data[base], data[later], col, a.threshold, a.hold)
        if r:
            rows.append((base, later, r))

    if not rows:
        say("（可用报告期不足以做这个跨度的统计，把 --periods 调大或 --after 调小）")
        return

    say(f"| 起始期 | {a.after} 期后 | 起始满足数 | 可比对 | 仍保持 | **保持率** |")
    say("|---|---|---|---|---|---|")
    rates = []
    for base, later, r in rows:
        rates.append(r["rate"])
        say(f"| {base} | {later} | {r['n_base']} | {r['n_matched']} | "
            f"{r['n_kept']} | **{r['rate']:.1f}%** |")
    say()
    avg = sum(rates) / len(rates)
    say(f"## 基准率 ≈ **{avg:.0f}%**"
        f"（{len(rates)} 个样本期，区间 {min(rates):.0f}% ~ {max(rates):.0f}%）")
    say()
    say(f"> **怎么用**：如果你手上的票现在净利同比 ≥ {a.threshold}%，"
        f"那么「{a.after} 个季度后仍保持 ≥ {a.hold}%」这件事的**外部视角概率约 {avg:.0f}%**。")
    say(f"> 这直接约束 bull 情景的概率 —— **别给到远高于 {avg:.0f}% 的水平**，"
        f"除非你有这家公司特有的、能推翻基准率的证据（如已锁定的长约订单）。")
    say()
    if avg < 35:
        say(f"> 🔴 **{avg:.0f}% 是个低数字。**高增速的均值回归比直觉强得多 —— "
            f"这正是「高估值往往先于低于平均的回报」的机制。")
    say()


def cmd_margin(a):
    """高毛利率的持续性。"""
    ps = periods(a.periods)
    # 「掉不超过 N pp」是**相对自身**的,不是固定线 —— 见 _cohort_drop 的注释
    say(f"# 基准率:毛利率 ≥ {a.threshold}% 的公司，"
        f"{a.after} 个季度后相对自身下滑不超过 {a.drop}pp 的比例")
    say()
    print("拉取数据：")
    data = {}
    for p in ps:
        try:
            data[p] = load_period(p)
        except Exception as e:
            print(f"  {p} 失败:{type(e).__name__}")
    print()
    col = "销售毛利率"
    ordered = sorted(data)
    rows = []
    for i, base in enumerate(ordered):
        j = i + a.after
        if j >= len(ordered):
            break
        r = _cohort_drop(data[base], data[ordered[j]], col, a.threshold, a.drop)
        if r:
            rows.append((base, ordered[j], r))
    if not rows:
        say("（报告期不足）")
        return
    say(f"| 起始期 | {a.after} 期后 | 起始满足数 | 可比对 | 仍保持 | **保持率** |")
    say("|---|---|---|---|---|---|")
    rates = []
    for base, later, r in rows:
        rates.append(r["rate"])
        say(f"| {base} | {later} | {r['n_base']} | {r['n_matched']} | "
            f"{r['n_kept']} | **{r['rate']:.1f}%** |")
    say()
    avg = sum(rates) / len(rates)
    say(f"## 基准率 ≈ **{avg:.0f}%**（{len(rates)} 个样本期）")
    say()
    say(f"> 毛利率的粘性通常远高于增速 —— 如果这个数远低于净利增速的基准率，"
        f"说明该行业价格竞争激烈。")
    say()


def cmd_calibrate(a):
    """给某只票做校准:读它当前的增速与毛利率,查对应的基准率。

    ⚠ 这里**必须**走 calibrate(),不要在这里另写一遍循环 ——
    preport 第 8 层调的就是 calibrate()。两处各写一遍的结果是:
    改了一处忘了另一处,同一只票 CLI 和报告给出两个不同的基准率,
    而且谁都不知道该信哪个。
    """
    ps = periods(a.periods)
    latest = load_period(ps[0], quiet=True)
    row = latest[latest["股票代码"] == a.code.zfill(6)]
    if not len(row):
        sys.exit(f"最新报告期 {ps[0]} 里没有 {a.code}")
    r = row.iloc[0]
    g = r.get("净利润同比增长")
    m = r.get("销售毛利率")
    say(f"# {r.get('股票简称')}（{a.code}）基准率校准")
    say()
    say(f"> 最新报告期 {ps[0]}　·　净利同比 **{g:+.1f}%**　·　"
        f"毛利率 **{m:.1f}%**")
    say()
    say("## 外部视角:历史上处在同样位置的公司，后来怎么样了")
    say()

    print("拉取全市场历史（首次较慢）：")
    data = load_all(a.periods, quiet=False)
    print()

    say("| 问题 | 同类公司 | 基准率 | 样本期 | 区间 |")
    say("|---|---|---|---|---|")
    for after in (2, 4):
        c = calibrate(net_yoy=g, gm=m, after=after, data=data,
                      margin_drop=a.drop)
        if not c:
            continue
        gth = c.get("growth_cohort")
        for hold, x in sorted(c.get("growth", {}).items(), reverse=True):
            say(f"| {after} 个季度后净利同比仍 ≥ {hold}% | "
                f"起始 ≥ {gth}% 的约 {x['n_base_avg']:.0f} 家 | "
                f"**{x['rate']:.0f}%** | {x['n_spans']} 段 | "
                f"{x['lo']:.0f}% ~ {x['hi']:.0f}% |")
        mm = c.get("margin")
        if mm and after == 4:
            say(f"| {after} 个季度后毛利率相对自身下滑 ≤ {c['margin_drop']}pp | "
                f"起始 ≥ {c['margin_cohort']}% 的约 {mm['n_base_avg']:.0f} 家 | "
                f"**{mm['rate']:.0f}%** | {mm['n_spans']} 段 | "
                f"{mm['lo']:.0f}% ~ {mm['hi']:.0f}% |")
    say()
    say(f"数据源：东财全市场季度业绩，报告期 {sorted(data)[0]} ~ "
        f"{sorted(data)[-1]}（{len(data)} 期）。")
    say()
    say("## 怎么接到期望值框架")
    say()
    say("上面这些百分比就是 **bull 情景概率的外部锚**。")
    say("如果你打算给 bull 35% 而基准率只有 20%，"
        "**必须说出这家公司凭什么超出基准率** —— "
        "已锁定的长约、独家产能、客户结构，而不是「行业景气」这种谁都能说的话。")
    say()
    say("> ⚠️ 基准率是**下限约束**不是预测。它告诉你「凭什么」这个问题该多难回答，"
        "不告诉你这一家会怎样。")
    say()
    say("> 同样这几个数会自动出现在 `preport <代码>:<成本>` 的第 8 层，"
        "并逐条标注在第 9 层的判据后面 —— 那才是它们真正该被读到的地方。")
    say()


def main():
    p = argparse.ArgumentParser(
        prog="baserate",
        description="基准率:用全市场历史校准「这种增速/毛利能持续多久」")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("growth", help="高增速的持续性")
    g.add_argument("--threshold", type=float, default=90, help="起始净利同比阈值 %%")
    g.add_argument("--hold", type=float, default=50, help="N 期后仍需保持的阈值 %%")
    g.add_argument("--after", type=int, default=4, help="看几个报告期之后")
    g.add_argument("--periods", type=int, default=16, help="回看几期(默认 16≈4 年)")
    g.add_argument("--md", metavar="文件")

    m = sub.add_parser("margin", help="高毛利率的持续性")
    m.add_argument("--threshold", type=float, default=45, help="起始毛利率 %%")
    m.add_argument("--drop", type=float, default=5, help="允许下滑 pp")
    m.add_argument("--after", type=int, default=4)
    m.add_argument("--periods", type=int, default=16)
    m.add_argument("--md", metavar="文件")

    c = sub.add_parser("calibrate", help="给某只票做校准")
    c.add_argument("code")
    c.add_argument("--drop", type=float, default=3,
                   help="毛利率允许下滑 pp(默认 3,与 preport 判据一致)")
    c.add_argument("--periods", type=int, default=16)
    c.add_argument("--md", metavar="文件")

    a = p.parse_args()
    if not a.cmd:
        print(__doc__)
        return 1
    {"growth": cmd_growth, "margin": cmd_margin, "calibrate": cmd_calibrate}[a.cmd](a)
    if getattr(a, "md", None):
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
