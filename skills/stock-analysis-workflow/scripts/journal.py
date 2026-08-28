#!/usr/bin/env python3
"""journal —— 分析决策的记录与复核，让这套方法真的越用越准。

用法:
    P=$VENV/bin/python
    J=$LAB/tools/journal.py

    # ① 每次给结论时记一笔（含当时的概率假设与可证伪判据）
    $P $J log 300502 --price 409.10 --cost 400.00 \
        --action hold --ev 11.5 \
        --bear 0.25 --base 0.50 --bull 0.25 \
        --thesis "市场为『AI Capex 见顶』定价，但 SEC 数据显示云厂 Capex 同比 +87% 加速" \
        --falsify "下季度云厂 Capex 同比 <20% → 预期差消失，清仓" \
        --check 2026-10-31

    # ② 到期回来复核（下个季报）
    $P $J review                       # 列出所有到期未复核的
    $P $J close <id> --outcome right --note "Capex 同比仍 +80%，判据未触发"

    # ③ 看自己的记录与偏差
    $P $J stats                        # 命中率、概率校准、系统性偏差

为什么要有这个（这是本方法最大的漏洞）:
    现在的流程是单向的 —— 分析 → 给建议 → 结束。**没有任何机制记录
    「当时判断了什么、后来对不对、错在哪」。不复核就不会进步。**

    参考 EvolveR（arXiv:2510.16079，见 ai-doc/self-improving-agents/evolver.md）
    的三阶段循环，落到投资分析上:
      1. 在线交互 = 每次分析产生一条轨迹（结论 + 概率 + 判据）
      2. 离线自蒸馏 = 到期复核，把「对了/错了」压缩成**抽象原则**
         （不是存「300502 那次判断对了」，而是存
          「我在有明确领先指标支撑时倾向低估 bull 概率」）
      3. 策略进化 = 下次分析前先读这些原则，修正自己的先验

    EvolveR 的关键发现:3B 规模下自蒸馏的原则**优于更大教师模型的指导** ——
    自己的经验比外部知识更管用，前提是你真的记录并复核。

    某自研脚手架 的设计里有这一层（principles.jsonl + 成功/使用计数），
    但**至今 0 字节，一次没跑过**。设计不等于实现。这个脚本是最小可用实现。

存储:JSONL，一行一条，人和 AI 都能直接读。
    $LAB/data/journal.jsonl        决策记录
    $LAB/data/principles.jsonl     蒸馏出的原则
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
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
              Path.home() / "claude-tools" / "astock-lab"):
        if d.is_dir():
            return d
    return Path.cwd()


LAB = _lab_root()
JOURNAL = LAB / "data" / "journal.jsonl"
PRINCIPLES = LAB / "data" / "principles.jsonl"


_OUT = []


def say(x=""):
    print(x)
    _OUT.append(x)


def _load_md2html():
    """md2html 在 skill finance-pdf-report 里 —— 跨 skill,按候选目录找。

    ⚠ 同 position_report 的 _load_sibling:__file__ 会把软链解成真实路径,
    sys.path.insert(本目录) 找不到别的 skill 的模块。
    另外必须接住 SystemExit —— 被 import 的模块缺依赖时会 sys.exit(),
    那是 BaseException 不是 Exception,`except Exception` 拦不住,
    会把整个 journal 一起带走。
    """
    here = Path(__file__).resolve().parent
    for d in (here.parent.parent / "finance-pdf-report" / "scripts",
              LAB / "tools"):
        if (d / "md2html.py").exists():
            sys.path.insert(0, str(d))
            try:
                import md2html
                return md2html
            except SystemExit:
                continue
            except Exception:
                continue
    return None


def _load(p):
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _append(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _save_all(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_log(a):
    rows = _load(JOURNAL)
    rid = max([r.get("id", 0) for r in rows], default=0) + 1
    probs = {"bear": a.bear, "base": a.base, "bull": a.bull}
    tot = sum(v for v in probs.values() if v is not None)
    if tot and abs(tot - 1.0) > 0.02:
        print(f"⚠ 三情景概率之和 = {tot:.2f}，不是 1.00 —— 确认是不是写错了",
              file=sys.stderr)
    rec = {
        "id": rid,
        "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "code": a.code,
        "price": a.price,
        "cost": a.cost,
        "action": a.action,
        "ev": a.ev,
        "probs": probs,
        "thesis": a.thesis,          # 当时的预期差判断
        "falsify": a.falsify,        # 可证伪判据 —— 复核时就核这条
        "check_date": a.check,       # 什么时候回来核
        "status": "open",
    }
    _append(JOURNAL, rec)
    print(f"已记录 #{rid}　{a.code}　{a.action}　EV {a.ev}%　复核日 {a.check}")
    if not a.falsify:
        print("⚠ 没写可证伪判据 —— 那这条记录复核时无从核起，"
              "补一条:journal edit 或重记", file=sys.stderr)


def cmd_review(a):
    rows = _load(JOURNAL)
    today = dt.date.today().isoformat()
    due = [r for r in rows
           if r.get("status") == "open"
           and (a.all or (r.get("check_date") or "9999") <= today)]
    if not due:
        n_open = sum(1 for r in rows if r.get("status") == "open")
        print(f"没有到期待复核的记录（共 {n_open} 条未结）。"
              f"想看全部未结:--all")
        return
    print(f"待复核 {len(due)} 条：\n")
    for r in due:
        print(f"── #{r['id']}　{r['code']}　记于 {r['logged_at'][:10]}　"
              f"复核日 {r.get('check_date')}")
        print(f"   当时:价 {r.get('price')}　动作 **{r.get('action')}**　"
              f"EV {r.get('ev')}%　概率 "
              f"熊{r['probs'].get('bear')}/中{r['probs'].get('base')}/牛{r['probs'].get('bull')}")
        if r.get("thesis"):
            print(f"   判断:{r['thesis']}")
        if r.get("falsify"):
            print(f"   **判据:{r['falsify']}**　← 现在就核这条")
        print()
    print("核完用:journal close <id> --outcome right|wrong|partial --note \"...\"")


def cmd_close(a):
    rows = _load(JOURNAL)
    hit = [r for r in rows if r.get("id") == a.id]
    if not hit:
        sys.exit(f"没有 #{a.id} 这条记录")
    r = hit[0]
    if r.get("status") != "open":
        print(f"⚠ #{a.id} 已经是 {r['status']} 状态，本次覆盖")
    r["status"] = "closed"
    r["outcome"] = a.outcome
    r["closed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    r["review_note"] = a.note
    r["actual_price"] = a.actual_price
    if a.actual_price and r.get("price"):
        r["actual_return_pct"] = round((a.actual_price / r["price"] - 1) * 100, 1)
    _save_all(JOURNAL, rows)
    print(f"#{a.id} 已结:{a.outcome}")
    if r.get("actual_return_pct") is not None:
        print(f"  实际涨跌 {r['actual_return_pct']:+.1f}%　"
              f"（当时 EV 预期 {r.get('ev')}%）")
    if a.principle:
        p = {
            "distilled_at": dt.datetime.now().isoformat(timespec="seconds"),
            "from_journal_id": a.id,
            "principle": a.principle,
            "used": 0,
            "hit": 0,
        }
        _append(PRINCIPLES, p)
        print(f"  已蒸馏出原则:{a.principle}")
    else:
        print("  提示:加 --principle \"...\" 把这次的教训蒸馏成**抽象原则**，"
              "不是「这次对了」，而是「我在什么情况下会系统性判断错」")


def cmd_stats(a):
    """自我进化知识库 —— 终端看文本,--html 出给人看的视图。

    ★ 「一个给 AI 看,一个给人看」:
      给 AI 的是 `data/principles.jsonl` 与快照里的 reasoning 段(结构化,
      下一轮分析直接读回去);给人的是这个视图 —— 人要看的是**知识库怎么长出来的**:
      哪条原则从哪次教训里来、当时的判断是什么、后来对不对、概率估偏了多少。
      同一份数据两种呈现,不是两份数据。
    """
    rows = _load(JOURNAL)
    prin = _load(PRINCIPLES)
    closed = [r for r in rows if r.get("status") == "closed"]
    opens = [r for r in rows if r.get("status") != "closed"]
    today = dt.date.today()

    say("# 自我进化知识库")
    say()
    say(f"> {dt.datetime.now():%Y-%m-%d %H:%M}　·　"
        f"决策记录 `data/journal.jsonl`　·　原则库 `data/principles.jsonl`")
    say()

    # ── 概览 ────────────────────────────────────────────────────────
    say("| 项 | 值 |")
    say("|---|---|")
    say(f"| 决策记录 | {len(rows)} 条 |")
    say(f"| 已复核 | {len(closed)} 条 |")
    say(f"| 待复核 | {len(opens)} 条 |")
    say(f"| 已蒸馏原则 | **{len(prin)}** 条 |")
    if closed:
        rt = sum(1 for r in closed if r.get("outcome") == "right")
        say(f"| 命中率 | **{rt/len(closed)*100:.0f}%**（{rt}/{len(closed)} 判对） |")
    say()

    # ── 原则库:知识库本体 ───────────────────────────────────────────
    say("## 一　原则库")
    say()
    if prin:
        say("**下次分析前先读一遍。**这是 EvolveR 三阶段里的「策略进化」，"
            "也是这套方法唯一会自己变好的地方。")
        say()
        say("| # | 原则 | 来自 | 蒸馏于 | 用过 | 命中 |")
        say("|---|---|---|---|---|---|")
        for i, p in enumerate(prin, 1):
            used, hit = p.get("used", 0), p.get("hit", 0)
            rate = f"{hit/used*100:.0f}%" if used else "—"
            say(f"| {i} | **{p.get('principle', '')}** | "
                f"#{p.get('from_journal_id')} | "
                f"{str(p.get('distilled_at', ''))[:10]} | {used} | {hit}（{rate}） |")
        say()
    else:
        say("**还是空的。**原则库要靠复核长出来 —— 每次 `journal close` 时用")
        say("`--principle \"...\"` 记下抽象教训。")
        say()
        say("> 存**「我在有领先指标支撑时倾向低估 bull 概率」**这种，")
        say("> 不要存「300502 那次判断对了」。前者换只票还能用，后者不能。")
        say()
        if opens:
            nxt = min((r.get("check_date") or "9999") for r in opens)
            say(f"最近一次能蒸馏的机会:**{nxt}**（{len(opens)} 条待复核里最早的复核日）。")
            say()

    # ── 待复核 ──────────────────────────────────────────────────────
    say("## 二　待复核")
    say()
    if not opens:
        say("（没有未结记录）")
        say()
    else:
        say("| # | 代码 | 记于 | 当时价 | 动作 | EV | 复核日 | 还有 |")
        say("|---|---|---|---|---|---|---|---|")
        for r in opens:
            cd = r.get("check_date") or "未定"
            left = "—"
            try:
                y, m, d = (int(x) for x in cd.split("-"))
                n = (dt.date(y, m, d) - today).days
                left = f"**{n} 天**" if n >= 0 else f"**已过期 {-n} 天**"
            except (ValueError, AttributeError):
                pass
            ev = r.get("ev")
            ev_s = "—" if ev is None else f"{ev:+.1f}%"
            say(f"| {r['id']} | {r['code']} | {str(r.get('logged_at',''))[:10]} | "
                f"{r.get('price')} | {r.get('action')} | {ev_s} | {cd} | {left} |")
        say()

    # ── 决策时间线:知识库怎么长出来的 ────────────────────────────────
    say("## 三　决策时间线")
    say()
    if not rows:
        say("（还没有记录。`journal log <代码> --price ... --action ... "
            "--falsify \"...\" --check <日期>`）")
        say()
    else:
        for r in sorted(rows, key=lambda x: str(x.get("logged_at", "")), reverse=True):
            st = r.get("status", "open")
            oc = r.get("outcome")
            badge = {"right": "✅ 判对", "partial": "◐ 部分对",
                     "wrong": "❌ 判错"}.get(oc, "○ 待复核")
            say(f"### #{r['id']}　{r['code']}　{badge}")
            say()
            say(f"{str(r.get('logged_at',''))[:10]} 记录　·　当时价 "
                f"**{r.get('price')}**　·　动作 **{r.get('action')}**"
                + (f"　·　成本 {r['cost']}" if r.get("cost") else ""))
            say()
            if r.get("thesis"):
                say(f"- **当时的预期差判断**：{r['thesis']}")
            if r.get("probs"):
                p = r["probs"]
                say(f"- **当时的概率**：bear {p.get('bear')} / base {p.get('base')} "
                    f"/ bull {p.get('bull')}"
                    + (f"，EV **{r['ev']:+.1f}%**" if r.get("ev") is not None else ""))
            if r.get("falsify"):
                say(f"- **可证伪判据**：{r['falsify']}")
            if st == "closed":
                if r.get("actual_return_pct") is not None:
                    gap = r["actual_return_pct"] - (r.get("ev") or 0)
                    say(f"- **实际**：{r['actual_return_pct']:+.1f}%"
                        f"（当时 EV {r.get('ev')}%，偏差 **{gap:+.1f}pp**）")
                if r.get("review_note"):
                    say(f"- **复核说明**：{r['review_note']}")
            say()

    # ── 概率校准 ────────────────────────────────────────────────────
    withret = [r for r in closed if r.get("actual_return_pct") is not None]
    say("## 四　概率校准")
    say()
    if not withret:
        say("还没有带实际涨跌的已结记录。**至少要 3-5 次复核才能看出系统性偏差** ——")
        say("这一节是整套方法里唯一能证明「我到底准不准」的地方，急不来。")
        say()
    else:
        say("| # | 代码 | 动作 | 当时 EV | 实际涨跌 | 偏差 |")
        say("|---|---|---|---|---|---|")
        gaps = []
        for r in withret:
            gap = r["actual_return_pct"] - (r.get("ev") or 0)
            gaps.append(gap)
            say(f"| {r['id']} | {r['code']} | {r.get('action')} | "
                f"{r.get('ev')}% | {r['actual_return_pct']:+.1f}% | {gap:+.1f}pp |")
        say()
        avg = sum(gaps) / len(gaps)
        if avg < -5:
            say(f"> 🔴 **平均比预期低 {-avg:.1f} 个百分点 —— 系统性过度乐观。**"
                f"下次把 bull 概率调低、bear 调高。")
        elif avg > 5:
            say(f"> 平均比预期高 {avg:.1f} 个百分点 —— 偏保守，可以适度提高 bull 概率。")
        else:
            say(f"> 平均偏差 {avg:+.1f}pp，概率估计基本校准。")
        say()

    say("---")
    say()
    say("**这份视图是给人看的。**给 AI 看的是同一份数据的结构化形态："
        "`data/principles.jsonl` 与每份快照里的 `reasoning` 段 —— "
        "下一轮 `preport` 会把它们读回去，印在第 0 节后面。")
    say()

    if getattr(a, "md", None):
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    if getattr(a, "html", None):
        m = _load_md2html()
        if m is None:
            print("\n⚠ 找不到 md2html.py（在 skill finance-pdf-report 里），跳过 HTML",
                  file=sys.stderr)
        else:
            Path(a.html).parent.mkdir(parents=True, exist_ok=True)
            Path(a.html).write_text(
                m.render("\n".join(_OUT), title="自我进化知识库",
                         subtitle=f"生成于 {dt.datetime.now():%Y-%m-%d %H:%M}"),
                encoding="utf-8")
            print(f"已写入 {a.html}")


def main():
    p = argparse.ArgumentParser(
        prog="journal",
        description="分析决策的记录与复核 —— 让方法真的越用越准（EvolveR 三阶段）")
    sub = p.add_subparsers(dest="cmd")

    lg = sub.add_parser("log", help="记录一次分析决策")
    lg.add_argument("code")
    lg.add_argument("--price", type=float, required=True, help="当时价格")
    lg.add_argument("--cost", type=float, help="持仓成本")
    lg.add_argument("--action", required=True,
                    choices=["buy", "add", "hold", "trim", "sell", "watch"])
    lg.add_argument("--ev", type=float, help="概率加权期望收益 %%")
    lg.add_argument("--bear", type=float, help="熊市情景概率 如 0.25")
    lg.add_argument("--base", type=float, help="中性情景概率")
    lg.add_argument("--bull", type=float, help="牛市情景概率")
    lg.add_argument("--thesis", help="当时的预期差判断（市场错在哪、我凭什么这么认为）")
    lg.add_argument("--falsify", help="**可证伪判据** —— 什么情况下这个判断算错")
    lg.add_argument("--check", help="复核日期 YYYY-MM-DD，通常是下个季报")

    rv = sub.add_parser("review", help="列出到期待复核的记录")
    rv.add_argument("--all", action="store_true", help="列出全部未结，不只到期的")

    cl = sub.add_parser("close", help="复核并结掉一条记录")
    cl.add_argument("id", type=int)
    cl.add_argument("--outcome", required=True,
                    choices=["right", "partial", "wrong"])
    cl.add_argument("--note", help="复核说明：判据触发了吗、错在哪")
    cl.add_argument("--actual-price", type=float, dest="actual_price",
                    help="复核时的价格，用来算实际涨跌与 EV 的偏差")
    cl.add_argument("--principle", help="**蒸馏出的抽象原则**（不是「这次对了」）")

    st = sub.add_parser("stats", help="自我进化知识库:原则、时间线、概率校准")
    st.add_argument("--md", metavar="文件", help="存 markdown")
    st.add_argument("--html", metavar="文件",
                   help="出给人看的 HTML 视图(浏览器可看,也能打成 PDF)")

    a = p.parse_args()
    if not a.cmd:
        print(__doc__)
        return 1
    return {"log": cmd_log, "review": cmd_review,
            "close": cmd_close, "stats": cmd_stats}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
