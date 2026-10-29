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
    $P $J review                       # 列出所有到期未复核的 + 过了期限还没执行的动作
    $P $J close <id> --outcome right --note "Capex 同比仍 +80%，判据未触发" \
        --actual-price 520 --bench-return -9.5      # 同期大盘涨跌，用来算超额

    # ③ 看自己的记录与偏差
    $P $J stats                        # 命中率、按动作分组、概率校准、系统性偏差

    # ④ 记录本身的维护
    $P $J exec <id> --done --note "已按计划减到上限"    # 动作做了没有（--skip 要写理由）
    $P $J supersede <旧id> --by <新id>                   # 当晚改了几版，旧版不算命中率
    $P $J edit <id> --check 2027-01-31                   # 改复核日 / 判据 / 期限，留痕

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


ACTIONS = ["buy", "add", "hold", "trim", "sell", "watch"]
# 要「去做」的动作才有执行期限。2026-09-02「减到仓位上限」没执行，之后 6 周两只又跌了三成多 ——
# 当时记录里只有判断，没有「做了没有」，复核时连这件事都看不出来。
MUST_DO = {"buy", "add", "trim", "sell"}
DEADLINE_DAYS = 7
# forecast-from-facts 的 journal_log 写的是中文动作（decision.py 的 EV_CUTS）。
# 按前缀归类，顺序有讲究:「持有,不加仓」含「加仓」，必须先认「持有」。
_ZH_ACTIONS = (("清仓", "sell"), ("减仓", "trim"), ("持有", "hold"),
               ("可加仓", "add"), ("加仓", "add"), ("买", "buy"), ("不给动作", "watch"))


def norm_action(s):
    """把动作统一成六类,统计时才能分组。认不出的归 other，不硬猜。"""
    s = str(s or "")
    if s in ACTIONS:
        return s
    for prefix, act in _ZH_ACTIONS:
        if s.startswith(prefix):
            return act
    return "other"


def _find(rows, rid):
    hit = [r for r in rows if r.get("id") == rid]
    if not hit:
        sys.exit(f"没有 #{rid} 这条记录")
    return hit[0]


def _overdue(rows, today):
    """过了执行期限还没执行的动作。只看写了 deadline 的记录 —— 老记录没有这一栏，不追溯。"""
    return [r for r in rows
            if r.get("status") != "superseded"
            and norm_action(r.get("action")) in MUST_DO
            and r.get("deadline") and r["deadline"] < today
            and not r.get("executed")]


def cmd_log(a):
    rows = _load(JOURNAL)
    rid = max([r.get("id", 0) for r in rows], default=0) + 1
    probs = {"bear": a.bear, "base": a.base, "bull": a.bull}
    tot = sum(v for v in probs.values() if v is not None)
    if tot and abs(tot - 1.0) > 0.02:
        print(f"⚠ 三情景概率之和 = {tot:.2f}，不是 1.00 —— 确认是不是写错了",
              file=sys.stderr)
    # 概率和三个价位是复核时「算准不准」的唯一依据。2026-10-16 以后 14 条判断都没带，
    # 到复核日只能说对错，说不出偏了多少。所以除了 watch，缺了就拒绝，
    # 真给不出（比如纯规则信号）要用 --no-probs 写明理由 —— 不许一声不吭地缺。
    need = [f"--{k}" for k in ("bear", "base", "bull") if probs[k] is None] + \
           [f"--{k}-px" for k, v in (("bear", a.bear_px), ("base", a.base_px),
                                     ("bull", a.bull_px)) if v is None]
    if need and a.action != "watch" and not a.no_probs:
        sys.exit(f"❌ 缺 {' '.join(need)}。复核时没法算这条判断偏了多少。\n"
                 f"   补上，或用 --no-probs \"理由\" 写明为什么给不了。")

    # 三情景的**价位**。为什么必须单独存而不能留在 thesis 散文里
    # （2026-08-28 做深度报告时才发现的坑）:
    #   赔率图、下行空间、「跌到多少才是加仓区」全都要拿这三个数去算。
    #   埋在 thesis 里就只能靠正则抠 —— 换个人写法不同就抠不出来，
    #   而抠不出来时图会**静默少画一张**，报告看着还是完整的。
    #   结构化字段抠不到就是 None，缺了会明说。
    px = {"bear": a.bear_px, "base": a.base_px, "bull": a.bull_px}
    if any(v is not None for v in px.values()) and a.price:
        for k in ("bear", "base", "bull"):
            if px[k] is not None:
                px[f"{k}_chg"] = round((px[k] / a.price - 1) * 100, 1)
    # EV 自洽性检查:三情景价位齐全时，用价位 × 概率重算一遍 EV 与你写的对不上就报。
    # 不是拦截（你可能有别的算法），是让口径不一致当场暴露而不是留到复核时。
    if a.ev is not None and a.price and all(px.get(k) is not None
                                            for k in ("bear", "base", "bull")) \
            and all(probs.get(k) is not None for k in ("bear", "base", "bull")):
        calc = sum(probs[k] * (px[k] / a.price - 1) * 100
                   for k in ("bear", "base", "bull"))
        if abs(calc - a.ev) > 2.0:
            print(f"⚠ EV 对不上:你写 {a.ev:+.1f}%，按三情景价位×概率算是 "
                  f"{calc:+.1f}%（差 {abs(calc - a.ev):.1f}pp）", file=sys.stderr)
    ev = a.ev
    if ev is None and a.price and all(px.get(k) is not None for k in ("bear", "base", "bull")) \
            and all(probs.get(k) is not None for k in ("bear", "base", "bull")):
        ev = round(sum(probs[k] * (px[k] / a.price - 1) * 100
                       for k in ("bear", "base", "bull")), 1)
        print(f"EV 没写，按三情景价位×概率算出 {ev:+.1f}%")
    deadline = a.deadline
    if deadline is None and a.action in MUST_DO:
        deadline = (dt.date.today() + dt.timedelta(days=DEADLINE_DAYS)).isoformat()
        print(f"执行期限没写，默认 {DEADLINE_DAYS} 天后:{deadline}（做完用 journal exec {rid} --done）")
    rec = {
        "id": rid,
        "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "code": a.code,
        "price": a.price,
        "cost": a.cost,
        "action": a.action,
        "ev": ev,
        "probs": probs,
        "scenario_px": px,           # 三情景**价位**（画赔率图、算下行空间靠它）
        "no_probs_why": a.no_probs,  # 没给概率的理由（给了就是 None）
        "thesis": a.thesis,          # 当时的预期差判断
        "falsify": a.falsify,        # 可证伪判据 —— 复核时就核这条
        "check_date": a.check,       # 什么时候回来核
        "deadline": deadline,        # 动作的执行期限（watch / hold 没有）
        "executed": None,            # journal exec 填:做了 / 没做及理由
        "status": "open",
    }
    _append(JOURNAL, rec)
    print(f"已记录 #{rid}　{a.code}　{a.action}　EV {ev}%　复核日 {a.check}")
    if not a.falsify:
        print("⚠ 没写可证伪判据 —— 那这条记录复核时无从核起，"
              f"补一条:journal edit {rid} --falsify \"...\"", file=sys.stderr)


def cmd_review(a):
    rows = _load(JOURNAL)
    today = dt.date.today().isoformat()
    late = _overdue(rows, today)
    if late:
        print(f"⚠ 过了执行期限还没执行的动作 {len(late)} 条（做了用 journal exec <id> --done，"
              f"没做用 --skip --note 写理由）：")
        for r in late:
            print(f"   #{r['id']}　{r['code']}　{r.get('action')}　期限 {r['deadline']}　没执行")
        print()
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
        p = r.get("probs") or {}     # 「不给动作」的记录 probs 是 None，不是空字典
        print(f"── #{r['id']}　{r['code']}　记于 {r['logged_at'][:10]}　"
              f"复核日 {r.get('check_date')}")
        print(f"   当时:价 {r.get('price')}　动作 **{r.get('action')}**"
              f"（{norm_action(r.get('action'))}）　EV {r.get('ev')}%　概率 "
              f"熊{p.get('bear')}/中{p.get('base')}/牛{p.get('bull')}")
        if r.get("thesis"):
            print(f"   判断:{r['thesis']}")
        if r.get("falsify"):
            print(f"   **判据:{r['falsify']}**　← 现在就核这条")
        print()
    print("核完用:journal close <id> --outcome right|wrong|partial --note \"...\" "
          "--actual-price <价> --bench-return <同期大盘涨跌%>")


def cmd_exec(a):
    if a.skip and not a.note:
        sys.exit("❌ --skip 要写 --note 理由:没做的原因复核时最有用")
    rows = _load(JOURNAL)
    r = _find(rows, a.id)
    r["executed"] = {"result": "done" if a.done else "skipped",
                     "at": dt.datetime.now().isoformat(timespec="seconds"),
                     "note": a.note}
    _save_all(JOURNAL, rows)
    print(f"#{a.id} 执行情况:{r['executed']['result']}　{a.note or ''}")


def cmd_supersede(a):
    """同一天改了几版，旧版标「被取代」:不进复核、不算命中率，但留在时间线里。"""
    rows = _load(JOURNAL)
    old, new = _find(rows, a.id), _find(rows, a.by)
    if old.get("status") != "open":
        sys.exit(f"❌ #{a.id} 是 {old.get('status')} 状态，只有未结的才能标被取代")
    if old.get("code") != new.get("code"):
        print(f"⚠ #{a.id}({old.get('code')}) 和 #{a.by}({new.get('code')}) 不是同一只票",
              file=sys.stderr)
    old.update(status="superseded", superseded_by=a.by,
               superseded_at=dt.datetime.now().isoformat(timespec="seconds"),
               superseded_note=a.note)
    _save_all(JOURNAL, rows)
    print(f"#{a.id} 已标为被 #{a.by} 取代")


def cmd_edit(a):
    rows = _load(JOURNAL)
    r = _find(rows, a.id)
    changes = {k: v for k, v in (("check_date", a.check), ("falsify", a.falsify),
                                 ("deadline", a.deadline), ("no_probs_why", a.no_probs))
               if v is not None}
    if not changes:
        sys.exit("没有要改的:--check / --falsify / --deadline / --no-probs 至少给一个")
    now = dt.datetime.now().isoformat(timespec="seconds")
    for k, v in changes.items():
        r.setdefault("edits", []).append({"at": now, "field": k, "old": r.get(k), "new": v})
        r[k] = v
    _save_all(JOURNAL, rows)
    print(f"#{a.id} 已改:{', '.join(changes)}（旧值存在 edits 里）")


def cmd_close(a):
    rows = _load(JOURNAL)
    r = _find(rows, a.id)
    if r.get("status") != "open":
        print(f"⚠ #{a.id} 已经是 {r['status']} 状态，本次覆盖")
    r["status"] = "closed"
    r["outcome"] = a.outcome
    r["closed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    r["review_note"] = a.note
    r["actual_price"] = a.actual_price
    if a.actual_price and r.get("price"):
        r["actual_return_pct"] = round((a.actual_price / r["price"] - 1) * 100, 1)
    # 同期大盘:2026-10-16 第一次核对时发现，两只票 −23%~−25% 里约 19 个点是大盘。
    # 不扣大盘就分不清「判断错了」还是「整个市场在跌」。
    if a.bench_return is not None:
        r["bench_return_pct"] = a.bench_return
        if r.get("actual_return_pct") is not None:
            r["excess_pct"] = round(r["actual_return_pct"] - a.bench_return, 1)
    _save_all(JOURNAL, rows)
    print(f"#{a.id} 已结:{a.outcome}")
    if r.get("actual_return_pct") is not None:
        print(f"  实际涨跌 {r['actual_return_pct']:+.1f}%　"
              f"（当时 EV 预期 {r.get('ev')}%）"
              + (f"　扣掉大盘 {r['excess_pct']:+.1f}%" if r.get("excess_pct") is not None else ""))
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
    opens = [r for r in rows if r.get("status") == "open"]
    superseded = [r for r in rows if r.get("status") == "superseded"]
    today = dt.date.today()
    late = _overdue(rows, today.isoformat())

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
    say(f"| 被取代(中间版本，不算命中率) | {len(superseded)} 条 |")
    say(f"| 过了期限没执行的动作 | {'**' + str(len(late)) + '** 条' if late else '0 条'} |")
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
            if st == "superseded":
                badge = f"↷ 被 #{r.get('superseded_by')} 取代"
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
        # 按动作分组:2026-10-16 第一次核对是手工分的 ——「持有」6 条平均超额 −5.0，
        # 「减仓」8 条 −11.3，说明系统挑出了更弱的票。这个对比以后每次都该自动出。
        say("### 按动作分组")
        say()
        say("| 动作 | 条数 | 平均涨跌 | 平均超额(扣大盘) |")
        say("|---|---|---|---|")
        groups = {}
        for r in withret:
            groups.setdefault(norm_action(r.get("action")), []).append(r)
        for act, rs in sorted(groups.items()):
            avg_r = sum(x["actual_return_pct"] for x in rs) / len(rs)
            ex = [x["excess_pct"] for x in rs if x.get("excess_pct") is not None]
            ex_s = f"{sum(ex) / len(ex):+.1f}%（{len(ex)} 条有大盘数）" if ex else "—"
            say(f"| {act} | {len(rs)} | {avg_r:+.1f}% | {ex_s} |")
        say()
        say("### 期望值 vs 实际")
        say()
        say("| # | 代码 | 动作 | 当时 EV | 实际涨跌 | 偏差 |")
        say("|---|---|---|---|---|---|")
        gaps = []
        for r in withret:
            ev = r.get("ev")
            gap = None if ev is None else r["actual_return_pct"] - ev
            if gap is not None:      # 没有 EV 的记录不能当 EV=0 去算偏差
                gaps.append(gap)
            say(f"| {r['id']} | {r['code']} | {r.get('action')} | "
                f"{'—' if ev is None else f'{ev}%'} | {r['actual_return_pct']:+.1f}% | "
                f"{'—' if gap is None else f'{gap:+.1f}pp'} |")
        say()
        avg = sum(gaps) / len(gaps) if gaps else 0.0
        if not gaps:
            say("> 已结记录里没有带 EV 的，偏差算不了。")
        elif avg < -5:
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
    lg.add_argument("--bear-px", type=float, dest="bear_px",
                    help="熊市情景**价位**（元）—— 深度报告画赔率图靠它")
    lg.add_argument("--base-px", type=float, dest="base_px", help="中性情景价位")
    lg.add_argument("--bull-px", type=float, dest="bull_px", help="牛市情景价位")
    lg.add_argument("--thesis", help="当时的预期差判断（市场错在哪、我凭什么这么认为）")
    lg.add_argument("--falsify", help="**可证伪判据** —— 什么情况下这个判断算错")
    lg.add_argument("--check", help="复核日期 YYYY-MM-DD，通常是下个季报")
    lg.add_argument("--deadline", help=f"执行期限 YYYY-MM-DD;buy/add/trim/sell 不写默认 {DEADLINE_DAYS} 天")
    lg.add_argument("--no-probs", dest="no_probs", metavar="理由",
                    help="给不了概率和三个价位时写明理由(否则非 watch 动作缺了就拒绝)")

    rv = sub.add_parser("review", help="列出到期待复核的记录 + 过了期限没执行的动作")
    rv.add_argument("--all", action="store_true", help="列出全部未结，不只到期的")

    cl = sub.add_parser("close", help="复核并结掉一条记录")
    cl.add_argument("id", type=int)
    cl.add_argument("--outcome", required=True,
                    choices=["right", "partial", "wrong"])
    cl.add_argument("--note", help="复核说明：判据触发了吗、错在哪")
    cl.add_argument("--actual-price", type=float, dest="actual_price",
                    help="复核时的价格，用来算实际涨跌与 EV 的偏差")
    cl.add_argument("--bench-return", type=float, dest="bench_return",
                    help="同期大盘涨跌 %%（如创业板指），用来算扣掉大盘后的超额")
    cl.add_argument("--principle", help="**蒸馏出的抽象原则**（不是「这次对了」）")

    ex = sub.add_parser("exec", help="记下动作做了没有")
    ex.add_argument("id", type=int)
    g = ex.add_mutually_exclusive_group(required=True)
    g.add_argument("--done", action="store_true", help="做了")
    g.add_argument("--skip", action="store_true", help="没做(必须 --note 写理由)")
    ex.add_argument("--note", help="做了什么 / 为什么没做")

    sp = sub.add_parser("supersede", help="把同一天的旧版判断标为被新版取代")
    sp.add_argument("id", type=int, help="旧版编号")
    sp.add_argument("--by", type=int, required=True, help="新版编号")
    sp.add_argument("--note", help="为什么改")

    ed = sub.add_parser("edit", help="改复核日 / 判据 / 执行期限(旧值留在 edits 里)")
    ed.add_argument("id", type=int)
    ed.add_argument("--check", help="新的复核日")
    ed.add_argument("--falsify", help="新的可证伪判据")
    ed.add_argument("--deadline", help="新的执行期限")
    ed.add_argument("--no-probs", dest="no_probs", metavar="理由",
                    help="给当时没带概率的旧记录补上理由(不是事后补概率 —— 那等于看着结果改答案)")

    st = sub.add_parser("stats", help="自我进化知识库:原则、时间线、概率校准")
    st.add_argument("--md", metavar="文件", help="存 markdown")
    st.add_argument("--html", metavar="文件",
                   help="出给人看的 HTML 视图(浏览器可看,也能打成 PDF)")

    a = p.parse_args()
    if not a.cmd:
        print(__doc__)
        return 1
    return {"log": cmd_log, "review": cmd_review, "close": cmd_close,
            "stats": cmd_stats, "exec": cmd_exec, "supersede": cmd_supersede,
            "edit": cmd_edit}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
