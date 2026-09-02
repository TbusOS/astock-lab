#!/usr/bin/env python3
"""check_flow_contract —— 卡住「这条判断流程」的硬规矩。

    python3 skills/forecast-from-facts/scripts/check_flow_contract.py [--raw <目录>]
    退出码 0 才算过

## 为什么这一层要有可执行检查,而不是写进文档提醒自己

**这里查的每一件事错了都是静默的**:报告照样生成、格式照样对、每个数都算得出来,
只是其中一条是错的,而读者(和我们自己)看不出来。
长会话里早期读过的规矩会被上下文稀释,执行时凭印象做,印象里没有那条就漏 ——
这不是「更认真点」能解决的,得让检查变成能跑的东西。

## 查什么

**静态**(不需要数据盘):

1. **`report.py` 在结论之前调行业层** —— 行业决定后面每一节用哪把尺。
   放到结论后面,报告读起来一样顺,但结论是用一把还没确定的尺量出来的。
2. **决策链不碰任何机构预测** —— `advice.py` / `decision.py` / `guidance.py` /
   `sector_rules.py` / `outside_view.py` 不许 import consensus,
   也不许读 `consensus/` `ratings/` `research/` 下的文件。
   ⚠ 2026-09-02 破坏测试发现:只查 `import` 和字面量 `"consensus/"` 拦不住
   `Path(raw) / "consensus" / code` 这种**分段拼路径**的写法 —— 两道检查全绿,
   而别人的预测已经进了决策链。所以现在**走 AST**:任何字符串常量
   (不管拼在哪)含那几个词都判红,而且再加一道运行期检查(见下)。
3. **journal 每条的 ev 和「三情景价位 × 概率」自洽**(差 ≤ 2 个百分点),
   而且**每条必须带齐 price / scenario_px / probs**。
   ⚠ 缺字段以前是 `continue` 跳过,「验过了」和「跳过了」外观完全一样。
4. **`SECTOR_RULES` 的键都在 `sectors.SECTORS` 里** —— 两张表分开放就必然会漂。
5. **概率算法**:起点三项和 = 1;四步之后仍然 = 1;
   **而且 bull ≤ base 且 bear ≤ base**(③b 那条被拿掉时,和仍然是 1,
   构造用例全过,报告还会打印一句「两端都不高于中枢,不用调」的假话)。
6. **`scenarios()` 按 `估值尺` 分派,不按 `nature`** —— AST 查一遍。
7. **报告模板里没有写死的五只横向数** —— 那种数过期时不报错。

**上数据盘**(给了 `--raw` 而且盘上真有数据时):

8. **五只 + 十只上游的 `单季资本开支` 覆盖率 ≥ 80%** ——
   `CAPEX_KEYS` 少一个字就整列静默变 None,两道检查以前全绿,
   而产业链位置会消失、EV 悄悄挪几个百分点,理由还写成「数据不够」。
9. **跑一遍五只的 `advice.build`,同时验四件事**:
   低 ≤ 中 ≤ 高;四步之后 bull ≤ base 且 bear ≤ base;
   PB 路径不出「清仓」(除非盈利侧条件成立);
   **运行期没有打开过 consensus / ratings / research / yahoo / ths 的文件**。
10. **账户文档含「五只 bear 同时兑现」那张表**,第 12 节列出的路径 glob 都非空。

## 一条元规矩:每个 check 自己包 try/except

2026-09-02 破坏测试:往 `decision.py` 顶部塞一个 `import consensus`,
`check_probability_rules()` 里的 `import decision` 直接崩掉,
**违规其实已经记进 FAILS 了,但打印在 main() 末尾,崩了就永远打不出来** ——
输出只有一段 traceback,一条 ❌ 都没有。所以现在**边查边打**,
而且每个 check 崩了算红并打印摘要。
"""
from __future__ import annotations

import argparse
import ast
import builtins
import glob
import json
import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "stock-analysis-workflow" / "scripts"))

FAILS, OKS, SKIPS = [], [], []


def chk(cond, msg):
    """记一条,**并且当场打印**。崩溃时也能看见已经查过的部分。"""
    (OKS if cond else FAILS).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg)


def skip(msg):
    """这一项**这次没法查**,如实记成「跳过」,不算过也不算不过。

    ⚠ **跳过和通过必须长得不一样。** 把「没数据所以没查」记成 ✅,
    等于在公开仓里每次都告诉自己「查过了,没问题」——
    那正是这套检查要防的那类假绿。
    """
    SKIPS.append(msg)
    print("  ⏭  " + msg)


def have_data(raw: Path, tag: str) -> bool:
    """这一轮有没有数据盘可查。**规则统一,只有两种结果**:

    · **数据目录不存在** → 跳过并说明(公开仓只有代码,没有 `data/raw`,
      那里能查的只有语法和静态项;数据项要在私有仓跑)
    · **数据目录存在但内容缺** → **判红**(那是真的数据缺口,不是环境差异)

    为什么要分开:两者外观一样(都是「查不到文件」),但该做的事完全相反 ——
    前者什么都不用做,后者要去跑取数脚本。混成一种处理,要么在公开仓天天报红
    (报红报到没人看),要么在私有仓把真缺口悄悄放过去。
    """
    if raw.is_dir():
        return True
    skip(f"{tag} 跳过:`{raw}` 不存在 —— **公开仓没有数据盘**,"
         f"这一项要在私有仓跑(`cd ~/astock-lab-private && "
         f"$VENV/bin/python /path/to/check_flow_contract.py --raw data/raw`)。"
         f"⚠ 数据目录**存在但内容缺**是另一回事,那会判红,不会走到这里。")
    return False


# 决策链上的文件 —— 这几个文件里的每一个数都必须能追到公司报表、SEC 申报原值,
# 或者公司自己在电话会上说的话。
DECISION_CHAIN = ["advice.py", "decision.py", "decision_render.py", "guidance.py",
                  "sector_rules.py", "outside_view.py", "journal_log.py"]
FORBIDDEN_IMPORTS = {"consensus"}
# 目录名。**分段拼路径也要抓**,所以这里不带斜杠,查的是字符串常量里的词。
FORBIDDEN_DIRS = ("consensus", "ratings", "research", "yahoo", "ths")

CODES = ["300308", "300502", "300476", "002353", "688328"]
UPSTREAM_CODES = ["601857", "600028", "600938", "000725", "000100",
                  "000050", "002387", "600584", "002156", "002185"]
CAPEX_MIN_COVER = 0.80


def check_order():
    """① report.py:行业层在结论节之前。"""
    src = (HERE / "report.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "build"), None)
    chk(fn is not None, "report.py 有 build()")
    if not fn:
        return
    body = ast.get_source_segment(src, fn) or ""
    has_sector = "sector_rules.render(" in body
    chk(has_sector, "build() 里调了行业层 `sector_rules.render(`")
    for later in ("sec_conclusion(", "advice.build(", "sec_valuation("):
        if has_sector and later in body:
            chk(body.index("sector_rules.render(") < body.index(later),
                f"行业层在 `{later}` **之前**被调用"
                f"(先判行业,才知道后面该用哪把尺)")


def _strip_comments_and_docstrings(src: str, tree) -> str:
    """把注释和 docstring 去掉,只留真正会执行的代码。

    为什么要剥:这几个文件的注释里**必须**能写「不许读 consensus/」这句话 ——
    那是在解释规矩,不是在违反它。不剥的话,写清楚规矩反而会让检查报错,
    于是下一个人的做法是把注释删掉。**检查不该逼人删注释。**
    """
    spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and ast.get_docstring(n) is not None:
            d = n.body[0]
            spans.append((d.lineno, d.end_lineno))
    lines = src.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if any(a <= i <= b for a, b in spans):
            continue
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def _docstring_nodes(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def check_no_consensus():
    """② 决策链不 import、不读机构预测。**AST 级** —— 分段拼路径也抓得到。"""
    for name in DECISION_CHAIN:
        f = HERE / name
        if not f.exists():
            chk(False, f"{name} 不存在")
            continue
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad_imp = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                bad_imp |= {a.name.split(".")[0] for a in n.names} & FORBIDDEN_IMPORTS
            elif isinstance(n, ast.ImportFrom) and n.module:
                if n.module.split(".")[0] in FORBIDDEN_IMPORTS:
                    bad_imp.add(n.module)
        chk(not bad_imp, f"{name} 没 import 机构预测模块"
                         + (f"(发现 {sorted(bad_imp)})" if bad_imp else ""))
        # ⚠ **走 AST 看每一个字符串常量**,不是在源码文本里找 `"consensus/"`。
        #   `Path(raw) / "consensus" / code` 这种写法源码里根本没有斜杠,
        #   2026-09-02 破坏测试就是这么绕过去的:两道检查全绿。
        docs = _docstring_nodes(tree)
        hits = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and id(n) not in docs:
                low = n.value.lower()
                for d in FORBIDDEN_DIRS:
                    if d in low:
                        hits.append(f"第 {n.lineno} 行的字符串含 `{d}`")
        chk(not hits, f"{name} 的字符串常量里没有 {'/'.join(FORBIDDEN_DIRS)}"
                      + (f" —— **发现**:{'; '.join(hits[:4])}" if hits else ""))


def check_journal(raw: Path):
    """③ journal:ev 与情景价×概率自洽;**字段必须带齐**;概率三项和 = 1。"""
    if not have_data(raw, "③"):
        return
    p = raw.parent / "journal.jsonl"
    if not p.exists():
        # 数据盘在,只是还没跑过报告 —— journal 是 `report.py` 的产物,不是取来的数据,
        # 所以这不算数据缺口,但也**不能记成通过**。
        skip(f"③ 跳过:`{p}` 还不存在 —— 数据盘在,只是这个仓还没跑过报告。"
             f"跑一次 `report.py` 之后这一项就有内容可查了。")
        return
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    # ③a 字段完整性。**缺字段判红,不再 continue** ——
    #    「验过了」和「跳过了」以前外观完全一样(破坏测试 6b:删掉 price 之后
    #    一条差 5 个百分点的错记录静静溜过去,全绿)。
    need = ("price", "scenario_px", "probs", "ev")
    incomplete, no_act = [], []
    for r in rows:
        if str(r.get("action", "")).startswith("不给动作"):
            # 「不给动作」的记录本来就没有价位和概率 —— 但它必须**明说自己是这一类**,
            # 而且要留下当时的价和原因,否则复核时连「那天它多少钱、卡在哪一步」
            # 都答不上来。**合法的空和缺字段的空,长得不能一样。**
            miss = [k for k in ("price", "no_action_why", "check_date")
                    if not r.get(k)]
            if miss:
                incomplete.append(f"#{r.get('id')} {r.get('code')} 不给动作,"
                                  f"但连 {'/'.join(miss)} 都没记")
            else:
                no_act.append(f"#{r.get('id')} {r.get('code')}")
            continue
        miss = [k for k in need
                if r.get(k) is None
                or (k in ("scenario_px", "probs")
                    and not all((r.get(k) or {}).get(x) is not None
                                for x in ("bear", "base", "bull")))]
        if miss:
            incomplete.append(f"#{r.get('id')} {r.get('code')} 缺 {'/'.join(miss)}")
    chk(not incomplete,
        f"journal {len(rows)} 条记录字段都带齐(price / scenario_px / probs / ev)"
        + (f" —— **字段不全、验不了的**:{'; '.join(incomplete)}。"
           f"记录不全就等于没记录:复核那天拿不出当时的价位和概率,"
           f"这条判断准不准永远说不清" if incomplete else "")
        + (f"。其中 {len(no_act)} 条是「不给动作」({'、'.join(no_act)}),"
           f"它们本来就没有价位和概率,但都记了当时的价和不给的原因"
           if no_act else ""))
    bad, checked = [], 0
    for r in rows:
        px, pr, ev, price = (r.get("scenario_px") or {}), (r.get("probs") or {}), \
            r.get("ev"), r.get("price")
        if ev is None or not price:
            continue
        if not all(px.get(k) is not None and pr.get(k) is not None
                   for k in ("bear", "base", "bull")):
            continue
        checked += 1
        calc = sum(pr[k] * (px[k] / price - 1) * 100 for k in ("bear", "base", "bull"))
        if abs(calc - ev) > 2.0:
            bad.append(f"#{r['id']} {r['code']} 写 {ev:+.1f}%、"
                       f"按情景价×概率算 {calc:+.1f}%(差 {abs(calc - ev):.1f}pp)")
    chk(not bad, f"journal {checked} 条的 ev 与三情景价位自洽"
                 + (f" —— **不自洽的**:{'; '.join(bad)}" if bad else ""))
    bad2 = []
    for r in rows:
        pr = r.get("probs") or {}
        if not all(pr.get(k) is not None for k in ("bear", "base", "bull")):
            continue
        tot = sum(pr[k] for k in ("bear", "base", "bull"))
        if abs(tot - 1.0) > 0.02:
            bad2.append(f"#{r['id']} {r['code']} 三项和 = {tot:.3f}")
    chk(not bad2, "journal 每条的三情景概率加起来等于 1"
                  + (f" —— **不等于 1 的**:{'; '.join(bad2)}" if bad2 else ""))


def check_sector_rules():
    """④ SECTOR_RULES 的键都在 sectors.SECTORS 里。"""
    import sector_rules
    import sectors
    unknown = set(sector_rules.SECTOR_RULES) - set(sectors.SECTORS)
    chk(not unknown,
        "SECTOR_RULES 的键全部在 sectors.SECTORS 里"
        + (f" —— **多出来的**:{sorted(unknown)}。"
           f"多出来的键**永远匹配不到**,那个行业会静默退回「未定义」,"
           f"所有尺全失效,而报告只会写一句「行业未定义」" if unknown else ""))
    missing = set(sectors.SECTORS) - set(sector_rules.SECTOR_RULES)
    chk(not missing,
        "sectors.SECTORS 的每个行业在 SECTOR_RULES 里都有一条"
        + (f" —— **缺的**:{sorted(missing)}。缺的那个会退回 UNDEFINED"
           f"(什么尺都不给),那是安全的,但应该显式写一条说明为什么没定尺"
           if missing else ""))
    need = ("估值尺", "估值尺说明", "展示尺", "基本面主指标", "基准率",
            "基准率不适用原因", "上游组", "领先指标")
    for name, spec in sector_rules.SECTOR_RULES.items():
        miss = [k for k in need if k not in spec]
        chk(not miss, f"行业「{name}」的规则字段完整"
                      + (f"(缺 {miss})" if miss else ""))
    chk(all(k in sector_rules.UNDEFINED for k in need),
        "UNDEFINED(未定义行业)的字段也完整")
    valid_lens = {None, "pe31", "pb", "ps31+pb"}
    bad = {n: s["估值尺"] for n, s in sector_rules.SECTOR_RULES.items()
           if s.get("估值尺") not in valid_lens}
    chk(not bad, "每个行业的估值尺都是已实现的那几把"
                 + (f" —— **不认识的**:{bad}" if bad else ""))


def check_probability_rules():
    """⑤ 概率算法:起点和 = 1;四步之后和 = 1、都在 [0,1]、**两端不高于中枢**。"""
    import decision
    tot0 = sum(decision.START_PROBS.values())
    chk(abs(tot0 - 1.0) < 1e-9, f"概率起点三项和 = {tot0}")
    cases = [
        ({"方向": []}, {"可用方法数": 3, "方法一致": True}, None),
        ({"方向": [{"方向": "↑"}] * 5}, {"可用方法数": 1, "方法一致": True}, 20),
        ({"方向": [{"方向": "↓"}] * 6}, {"可用方法数": 3, "方法一致": False}, None),
        ({"方向": [{"方向": "↑"}, {"方向": "↓"}]},
         {"可用方法数": 2, "方法一致": True}, 5),
        # 这一组专门顶 ③b:5 个 ↑ + 只有一个方法 → 不夹的话 bull 会超过 base
        ({"方向": [{"方向": "↑"}] * 4}, {"可用方法数": 1, "方法一致": True}, None),
    ]
    bad, tail_bad = [], []
    for lead, f, cap in cases:
        r = decision.probabilities(lead, f, cap, "构造用例")
        p = r["probs"]
        tot = sum(p.values())
        if abs(tot - 1.0) > 1e-9:
            bad.append(f"{lead}/{f}/cap={cap} → 和 {tot:.4f}")
        if any(v < -1e-9 or v > 1 + 1e-9 for v in p.values()):
            bad.append(f"{lead}/{f}/cap={cap} → 有概率跑出 [0,1]:{p}")
        if p["bull"] > p["base"] + 1e-9 or p["bear"] > p["base"] + 1e-9:
            tail_bad.append(f"{lead}/{f}/cap={cap} → "
                            f"bear {p['bear']:.2f} / base {p['base']:.2f} / "
                            f"bull {p['bull']:.2f}")
    chk(not bad, f"{len(cases)} 组构造用例走完四步之后,三项和仍然 = 1、都在 [0,1] 之内"
                 + (f" —— **坏的**:{bad}" if bad else ""))
    # ⑤b **③b「尾部不超过中枢」这一步真的生效。** 破坏测试:把整段拿掉之后
    #     三项和仍然 = 1,构造用例全过,而报告会打印「两端都不高于中枢,不用调」
    #     这句假话 —— 那时候胜宏的 bull 已经 45% > base 40%。
    chk(not tail_bad, "四步之后 bull ≤ base 且 bear ≤ base(在当前常量下)"
                      + (f" —— **两端翻过中枢的**:{tail_bad}" if tail_bad else ""))
    # ⚠ **上面那组用例顶不到 ③b。** 现在 LEAD_MAX_PP=10,② 最多把 bull 抬到 35%、
    #   ③ 再加 5 就是 40% —— 正好等于 base,永远不会超过。也就是说
    #   **把 ③b 整段删掉,上面那组用例照样全过**(2026-09-02 破坏测试实测)。
    #   一条今天顶不到的规则仍然要有检查:哪天有人把 LEAD_MAX_PP 调回 15,
    #   或者多加一个方法层的拨动,它立刻就会咬人,而那时候报告还会打印
    #   「两端都不高于中枢,不用调」这句假话。
    #   所以这里**把常量临时调大,专门去顶那条边界**,跑完还原。
    old_max = decision.LEAD_MAX_PP
    stress_bad = []
    try:
        decision.LEAD_MAX_PP = 30.0
        for lead, f, cap in cases + [
                ({"方向": [{"方向": "↑"}] * 6}, {"可用方法数": 1, "方法一致": True}, None),
                ({"方向": [{"方向": "↓"}] * 6}, {"可用方法数": 2, "方法一致": False}, None)]:
            p2 = decision.probabilities(lead, f, cap, "压力用例")["probs"]
            if p2["bull"] > p2["base"] + 1e-9 or p2["bear"] > p2["base"] + 1e-9:
                stress_bad.append(f"{lead}/{f} → bear {p2['bear']:.2f} / "
                                  f"base {p2['base']:.2f} / bull {p2['bull']:.2f}")
    finally:
        decision.LEAD_MAX_PP = old_max
    chk(not stress_bad,
        "把领先层上限临时调到 30 个百分点去顶边界,③b「尾部不超过中枢」仍然生效"
        + (f" —— **顶穿了**:{stress_bad}。这条规则一旦失效,报告还会照常打印"
           f"「两端都不高于中枢,不用调」这句假话" if stress_bad else ""))
    cuts = [c for c, _ in decision.EV_CUTS]
    chk(cuts == sorted(cuts), f"EV 阈值单调递增 {cuts}")


def check_action_rules():
    """⑤c EV → 动作那张表的两道额外门,**用构造出来的输入直接顶**。

    为什么不能只看当轮五只:今天没有一只 EV ≤ −20%,所以「清仓要有盈利侧证据」
    这条**根本没被走到** —— 把它整段拿掉,当轮跑一遍什么都不会变。
    2026-09-02 破坏测试实测:退出码 0。
    **一条今天走不到的规则仍然要有检查**,否则它坏了要等到下一次真出现清仓才发现,
    而那一次恰好是最不该出错的一次(清仓不可逆)。
    """
    import decision
    yes = {"成立": True, "命中": ["构造:领先层净方向 −2"], "看过": [], "拿不到": False}
    no = {"成立": False, "命中": [], "看过": ["构造:两条都不成立"], "拿不到": False}
    def cf(score, full):
        return {"分": score, "满分": full, "比例": score / full if full else 0.0}

    # ⚠ 「可加仓」那道门用的是**比例**不是分数。所以这里要同时顶两种行业:
    #   满分 3 的(AI算力链)和满分 1 的(半导体设备 / 油气设服)。
    #   门槛写成固定的 2 分时,满分 1 的行业拿满分也够不到 —— 那正是
    #   「把取数缺口变成看空理由」的同一个毛病,改了分母不改门槛等于没改。
    cases = [
        (-30.0, cf(3, 3), yes, "清仓", "EV 很负 + 盈利侧有证据 → 清仓"),
        (-30.0, cf(3, 3), no, "减仓", "EV 很负但盈利侧没证据 → **降级为减仓**"),
        (-12.0, cf(3, 3), no, "减仓", "EV 落在减仓档 → 减仓"),
        (0.0, cf(3, 3), no, "持有,不加仓", "EV 在中性带 → 持有,不加仓"),
        (12.0, cf(3, 3), no, "持有", "EV 落在持有档 → 持有"),
        (30.0, cf(2, 3), no, "可加仓", "EV 很正 + 比例 2/3 → 可加仓"),
        (30.0, cf(1, 3), no, "持有", "EV 很正但比例只有 1/3 → 退回持有"),
        (30.0, cf(1, 1), no, "可加仓",
         "**满分只有 1 的行业,拿满那一分就够** —— 门槛是比例不是分数"),
        (30.0, cf(0, 1), no, "持有", "满分 1 但一分没拿到 → 退回持有"),
    ]
    bad = []
    for ev, conf, pf, want, why in cases:
        got, _ = decision.act_from_ev(ev, conf, pf)
        if got != want:
            bad.append(f"{why}:实际给了「{got}」")
    chk(decision.ADD_MIN_CONF_RATIO > 0,
        f"「可加仓」的门槛是**比例**({decision.ADD_MIN_CONF_RATIO * 100:.0f}%)"
        f"不是固定分数 —— 分母按行业变的时候,固定分数会让登记项少的行业"
        f"结构上永远够不到")
    chk(not bad, f"EV → 动作那 {len(cases)} 组构造用例全部符合规则"
                 "(含「清仓要有盈利侧证据」和「可加仓看比例不看分数」两条)"
                 + (f" —— **不符的**:{'; '.join(bad)}" if bad else ""))


def _fake_view(act, mv, cap_amt, ev):
    """构造一只票的 view —— 只填 `plan_amounts` 会读的那几个字段。"""
    over = {"超出": cap_amt} if cap_amt > 0 else None
    return {"建议": {"动作": act,
                     "决策": {"ok": True, "EV": ev, "超限": over,
                              "仓位上限": {"ok": True, "上限金额": mv - cap_amt}}}}


def check_plan_rule():
    """⑤d 第 7 节的执行量:**排序不许决定卖多少**。

    ⚠ 旧写法是「卖出量 = max(减到上限, 还清剩余负债)」。「还清剩余负债」这一项
    在**排在第一位的那只票**身上永远等于「剩余负债全额」——那个数通常比它自己的
    市值还大,于是排第一的票被整只清掉。2026-09-02 实测:新易盛期望值 −0.3%
    (落在中性带里),因为排第一被全卖;而期望值 −11.4% 的杰瑞排第三,只减了一半。
    **一只中性的票被清仓,一只判断该减的票反而减得少** —— 这不是任何一条规则的本意,
    是两条约束被塞进同一个 max 之后,顺序悄悄变成了决定性的输入。

    这里用构造出来的账户直接顶那个形状:让排第一那只的市值**小于**剩余负债。
    """
    import account_plan as ap
    j = {"positions": [
        {"code": "AAA", "name": "甲", "market_value": 500000.0,
         "shares": 1000, "price": 500.0},
        {"code": "BBB", "name": "乙", "market_value": 100000.0,
         "shares": 1000, "price": 100.0}]}
    views = {"AAA": _fake_view("减仓(减到仓位上限)", 500000.0, 350000.0, -0.3),
             "BBB": _fake_view("减仓", 100000.0, 0.0, -11.0)}
    bad = []
    # ① 债不大时:甲只减到上限,不该被全卖;乙按判断减半
    plan = ap.plan_amounts(j, views, debt=300000.0)
    if abs(plan.get("AAA", {}).get("量", 0) - 350000.0) > 1:
        bad.append(f"债 30 万时甲该卖 350,000(减到上限),实际 "
                   f"{plan.get('AAA', {}).get('量')}")
    if abs(plan.get("BBB", {}).get("量", 0) - 50000.0) > 1:
        bad.append(f"债 30 万时乙该卖 50,000(减仓 = 减半),实际 "
                   f"{plan.get('BBB', {}).get('量')}")
    # ② 债略大时:缺口从**期望值最差**的那只(乙)出,排第一的甲不动
    plan2 = ap.plan_amounts(j, views, debt=430000.0)
    if abs(plan2.get("BBB", {}).get("量", 0) - 80000.0) > 1:
        bad.append(f"债 43 万时缺口 3 万该从期望值最差的乙出(50,000+30,000),实际 "
                   f"{plan2.get('BBB', {}).get('量')}")
    if abs(plan2.get("AAA", {}).get("量", 0) - 350000.0) > 1:
        bad.append(f"债 43 万时甲仍该停在上限 350,000,实际 "
                   f"{plan2.get('AAA', {}).get('量')}")
    # ③ 债更大时:乙先卖光,剩下的才轮到甲 —— 而且甲**仍然不是全卖**
    plan3 = ap.plan_amounts(j, views, debt=500000.0)
    if abs(plan3.get("BBB", {}).get("量", 0) - 100000.0) > 1:
        bad.append(f"债 50 万时乙该先被卖光 100,000,实际 "
                   f"{plan3.get('BBB', {}).get('量')}")
    if abs(plan3.get("AAA", {}).get("量", 0) - 400000.0) > 1:
        bad.append(f"债 50 万时甲该是 350,000 + 追加 50,000 = 400,000"
                   f"(**不是全卖 500,000**),实际 {plan3.get('AAA', {}).get('量')}")
    chk(not bad,
        "第 7 节的执行量按「减到上限 / 按判断 / 追加还债」三块分开算,"
        "**排在第一位的票不会被剩余负债全额吃掉**,追加从期望值最差的那只先出"
        + (f" —— **不对的**:{'; '.join(bad)}" if bad else ""))
    import decision
    chk(0 < decision.TRIM_FRACTION < 1,
        f"「减仓」减多少写死在一处(`decision.TRIM_FRACTION` = "
        f"{decision.TRIM_FRACTION:.0%})—— 个股报告 11.8 和账户第 7 节读同一个数,"
        f"两处各写一个今天恰好一样,改一处就分叉而且不报错")


def check_conf_denominator():
    """⑤e 确信度的分母**跟着行业走**,不是固定的 3。

    ⚠ 分母写死成 3 的时候,只登记 1 个领先指标、又写死「基准率不适用」的行业
    (半导体设备 / 油气设服)**结构上永远只能拿 1/3** —— 仓位上限被砍半、
    永远够不到「可加仓」。那是把我们自己的取数缺口变成了看空这只票的理由。
    这里拿两种行业的规则直接顶:满分该是 3 的和该是 1 的。
    """
    import decision
    f = {"可用方法数": 3, "方法一致": True,
         "methods": [{"name": n, "ok": True} for n in ("存货法", "同比法", "环比法")]}
    lead = {"方向": [{"方向": "↑"}] * 3}
    outside = {"ok": True}
    rich = {"领先指标": ["cloud_guidance", "taiwan_optics", "upstream_capex"],
            "基准率": True, "基准率不适用原因": ""}
    poor = {"领先指标": ["upstream_capex"], "基准率": False,
            "基准率不适用原因": "净利基数太小"}
    oil = {"领先指标": ["upstream_capex", "oil_price"], "基准率": False,
           "基准率不适用原因": "强周期"}
    bad = []
    for rules, want, why in ((rich, 3, "三项都拿得到的行业"),
                             (poor, 1, "只登记 1 个领先指标 + 基准率不适用"),
                             (oil, 1, "登记 2 个但油价按设计不给方向")):
        c = decision.confidence(f, lead, outside, False, rules)
        if c["满分"] != want:
            bad.append(f"{why}:满分该是 {want},实际 {c['满分']}")
        if c["满分"] and abs(c["系数"] - (0.5 + 0.5 * c["分"] / c["满分"])) > 1e-6:
            bad.append(f"{why}:系数不等于 0.5 + 0.5 × 得分÷满分")
    chk(not bad,
        "确信度的分母跟着行业走(三项都拿得到 → 3;只登记 1 个可检验项 → 1),"
        "系数 = 0.5 + 0.5 × 得分 ÷ 满分"
        + (f" —— **不对的**:{'; '.join(bad)}" if bad else ""))


def check_stress_drop(raw: Path):
    """⑩b 最坏情况表里,**没有 bear 的票不按「不跌」算**。

    按「不跌」算会让那张表系统性偏乐观,而它恰恰是用来回答「会不会被强平」的 ——
    一张偏乐观的最坏情况表比没有更糟:它给的是一个看着有依据的安心。
    """
    if not have_data(raw, "⑩b"):
        return
    import margin_account as ma
    codes = [c for c in CODES if list(raw.glob(f"quotes/{c}/*-qfq.json"))]
    if not codes:
        chk(False, f"`{raw}` 在,但 `quotes/<代码>/*-qfq.json` **一只都没有** —— "
                   f"没有日线就算不出最大回撤,最坏情况表只能退回「按不跌算」。"
                   f"重跑:`tools/fetch_all.py --codes {','.join(CODES)} --group quotes`")
        return
    bad = []
    for c in codes:
        drop, how = ma.stress_drop(None, raw, c)      # 假装这只算不出 bear
        if how != "回撤" or drop >= 0:
            bad.append(f"{c} → {how} {drop}")
    chk(not bad,
        f"{len(codes)} 只票在「算不出 bear」时都退到**近一年最大回撤**,"
        f"不是按「不跌」算"
        + (f" —— **退成不跌的**:{bad}" if bad else ""))
    d0, h0 = ma.stress_drop(-33.0, raw, codes[0])
    chk(h0 == "bear" and abs(d0 + 33.0) < 1e-9,
        "算得出 bear 的票仍然用 bear,不会被回撤顶掉")


def check_extern_notes():
    """⑩c 第 9.3 节**不许把 `filled.json` 里的 `notes` 原样印进报告**。

    ⚠ `notes` 是我们抽取那份 PDF 时留的**自己的工作笔记**:有的是字段说明
    (「单位:net_income 为 Rmb 百万」),有的是我们当时的想法
    (「我们此前用的是 TTM PE 的历史分位」—— 那还是一条已经改掉的旧做法)。
    原样印进报告,读者会以为**那是这份研报说的话**。
    研报自己引用的公司报表数住在 `report_facts`,那是研报内容,照印。

    这里用一份构造出来的研报直接顶:notes 里塞一句一眼能认出来的话,
    渲染完在输出里搜它。
    """
    import report_market as rm
    marker = "这是我们自己的工作笔记不该出现在报告里"
    j = {"source": {"publisher": "构造机构", "report_date": "2026-01-01",
                    "ticker": "000000.SZ"},
         "call": {"rating": "Buy", "target_price": 100.0, "currency": "CNY"},
         "valuation_anchor": {"method": "x", "multiple": 20.0, "base_year": "2027E",
                              "band": {"mid": 20.0}},
         "report_facts": ["它引用的公司存货 1 亿"],
         "key_risks": ["构造风险"],
         "notes": [marker, "单位:net_income 为 Rmb 百万"],
         "_dir": "构造目录"}
    L = []
    rm.sec_extern([j], -5.0, "减仓", {"close": 80.0}, L)
    body = "\n".join(L)
    chk(marker not in body,
        "第 9.3 节没有把 `filled.json` 的 `notes`(我们自己的工作笔记)印进报告"
        + ("" if marker not in body else
           " —— **印出来了**。那几条是字段说明和我们过时的想法,"
           "读者会以为是这份研报说的话"))
    chk("它引用的公司存货 1 亿" in body,
        "第 9.3 节照印 `report_facts`(研报自己引用的公司报表数)—— "
        "那是研报内容,能被我们自己的报表核")


def check_lens_dispatch():
    """⑥ `scenarios()` 按 `估值尺` 分派,不按 `nature`。

    ⚠ 按 nature 分派的毛病:`nature_of()` 对任何已定义但不在成长名单里的行业
    一律返回「周期」,于是消费 / 医药这种明写「本流程还没定尺」的行业
    会安安静静走完 PB 路径出三个价位 —— 报告第 0.5 节说没有尺,
    第 11 节给一个按 PB 算的动作,两句话打架而且不报错。
    """
    src = (HERE / "decision.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "scenarios"), None)
    chk(fn is not None, "decision.py 有 scenarios()")
    if not fn:
        return
    args = [a.arg for a in fn.args.args]
    chk("lens" in args and "nature" not in args,
        f"scenarios() 的入参是估值尺不是 nature(现在是 {args})")
    body = ast.get_source_segment(src, fn) or ""
    chk("weak" in args, "scenarios() 收 `weak`(弱匹配行业不给动作)")
    chk("LENS_PE" in body and "LENS_PB" in body,
        "scenarios() 用 LENS_PE / LENS_PB 这两个常量分派,没有把尺名散在函数体里")
    bsrc = ast.get_source_segment(src, next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "build")) or ""
    chk("scenarios(v, lens, weak)" in bsrc.replace(" ", "").replace(
        "scenarios(v,lens,weak)", "scenarios(v, lens, weak)")
        or "scenarios(v, lens, weak)" in bsrc,
        "build() 把 (lens, weak) 传给 scenarios()")


# 报告模板里不许出现的写死横向数。**这类数过期时不报错** ——
# 2026-09-02 那一版里,同一段落上一行算出 2.8、下一行印着写死的 0.8。
HARDCODED_PAT = re.compile(
    r"(中际旭创|新易盛|胜宏|杰瑞|深科达)\s*[0-9]+\.[0-9](?![%x])")
TEMPLATE_FILES = ["decision.py", "decision_render.py", "advice.py",
                  "report.py", "portfolio.py", "margin_account.py",
                  "account_plan.py", "position_view.py"]


def check_no_hardcoded_cross():
    """⑦ 报告模板里没有写死的五只横向数字。"""
    bad = []
    for name in TEMPLATE_FILES:
        f = HERE / name
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        code_only = _strip_comments_and_docstrings(src, ast.parse(src))
        for i, line in enumerate(code_only.splitlines(), 1):
            m = HARDCODED_PAT.search(line)
            if m:
                bad.append(f"{name}:{i} `{m.group(0)}`")
    chk(not bad,
        "报告模板里没有写死的「某只票 + 一个小数」这种横向数"
        + (f" —— **发现**:{'; '.join(bad)}。横向对照只能由 `portfolio.py` "
           f"当轮现算,写死的必然过期而且过期时不报错" if bad else ""))


def check_capex_coverage(raw: Path):
    """⑧ 五只 + 十只上游的 `单季资本开支` 覆盖率 ≥ 80%。

    ⚠ 这一条是补 2026-09-02 破坏测试的洞:把 `quarterly.CAPEX_KEYS` 改回
    少一个「所」字的旧写法之后,**两道检查全绿**,而十五只票的 `单季资本开支`
    全变 None —— 产业链位置消失、688328 的 EV 悄悄挪 3.4 个百分点,
    理由还写成「三大表都在,但凑不出可比季度」,把排查方向带到数据上。
    这个坑代码注释里已经写了两遍,还是没有可执行的检查。
    """
    if not have_data(raw, "⑧"):
        return
    import quarterly
    bad, checked = [], 0
    for code in CODES + UPSTREAM_CODES:
        try:
            qs = quarterly.build(raw, code, since="2019")["quarters"]
        except SystemExit:
            continue
        if not qs:
            continue
        checked += 1
        got = sum(1 for r in qs if r.get("单季资本开支") is not None)
        if got < len(qs) * CAPEX_MIN_COVER:
            bad.append(f"{code} {got}/{len(qs)}")
    if not checked:
        # ⚠ 数据目录在,却一只票的三大表都读不出来 —— **这是真的数据缺口,判红**。
        chk(False, f"`{raw}` 在,但 {len(CODES + UPSTREAM_CODES)} 只票"
                   f"(五只 + 十只上游)**一只的三大表都读不出来**。"
                   f"重跑:`tools/fetch_all.py --codes <代码> --group statements`")
        return
    chk(not bad,
        f"{checked} 只票的 `单季资本开支` 覆盖率都 ≥ {CAPEX_MIN_COVER:.0%}"
        + (f" —— **不够的**:{'、'.join(bad)}。"
           f"多半是 `quarterly.CAPEX_KEYS` 里的科目名和数据源对不上"
           f"(那个科目名少写一个字就整列静默变 None,表照样打得出来,"
           f"只是那一列全是「—」)" if bad else ""))


class _Watch:
    """运行期看着 `open` 和 `Path.read_text`,记下打开过哪些路径。

    为什么静态检查不够:字符串常量能查,`Path(raw) / dirname / code` 里的
    `dirname` 要是从别处传进来的变量,AST 就看不见了。
    **最后一道只能是「真跑一遍,看它到底碰了什么文件」。**
    """

    def __init__(self):
        self.paths = []
        self._open = builtins.open
        self._rt = Path.read_text

    def __enter__(self):
        w = self

        def open_(file, *a, **k):
            w.paths.append(str(file))
            return w._open(file, *a, **k)

        def read_text_(self_, *a, **k):
            w.paths.append(str(self_))
            return w._rt(self_, *a, **k)

        builtins.open = open_
        Path.read_text = read_text_
        return self

    def __exit__(self, *exc):
        builtins.open = self._open
        Path.read_text = self._rt
        return False


def check_live(raw: Path):
    """⑨ 真跑一遍五只的 `advice.build`,一次性验四件事 + 运行期不碰机构预测。"""
    if not have_data(raw, "⑨"):
        return
    import advice
    import guidance
    import sector_rules
    import sections_position as sp
    import valuation
    codes = [c for c in CODES if list(raw.glob(f"sectors/{c}/*.json"))]
    if not codes:
        chk(False, f"`{raw}` 在,但 `sectors/<代码>/` 下**一只票的行业缓存都没有** —— "
                   f"行业判断只读缓存不联网,没有它整条决策链都跑不起来。"
                   f"重跑:`tools/fetch_all.py --codes {','.join(CODES)} --group sectors`")
        return
    holdings = raw.parent.parent / "private" / "portfolio" / "account.json"
    hp = holdings if holdings.exists() else None
    band_bad, tail_bad, sell_bad, touched, cover_bad = [], [], [], [], []
    with _Watch() as w:
        for c in codes:
            v = valuation.build(c, raw, None)
            f = v["预测"]
            # ⚠ **一条带算不算数要看覆盖率,不只看「够不够 60 天」。**
            #   深科达 1332 个交易日里只有 559 天算得出 3+1 PE(42%),
            #   旧门槛照样过 —— 而那条带的分位数是由几段不连续的时期撑起来的。
            for lab, bd in (("3+1 PE 带", v.get("前瞻PE带") or {}),
                            ("3+1 PS 带", v.get("PS带") or {})):
                cov = bd.get("覆盖率pct")
                if bd.get("ok") and cov is not None and cov < valuation.COVERAGE_MIN_PCT:
                    cover_bad.append(f"{c} 的{lab}覆盖率只有 {cov:.0f}% 却判 ok")
            b = f.get("营收区间")
            if b and not (b["low"] <= b["mid"] <= b["high"]):
                band_bad.append(f"{c} {b['low'] / 1e8:.1f}/{b['mid'] / 1e8:.1f}"
                                f"/{b['high'] / 1e8:.1f}")
            a = advice.build(v, f, sp.load_position(hp, c))
            d = a["决策"]
            if not d.get("ok"):
                continue
            p = d["概率"]["probs"]
            if p["bull"] > p["base"] + 1e-9 or p["bear"] > p["base"] + 1e-9:
                tail_bad.append(f"{c} {p['bear']:.2f}/{p['base']:.2f}/{p['bull']:.2f}")
            if d["动作"].startswith("清仓") and not d["盈利侧证据"]["成立"]:
                sell_bad.append(f"{c}(路径 {d['三情景']['路径']})")
    for pth in w.paths:
        low = str(pth).lower()
        if any(f"/{x}/" in low or low.endswith(f"-{x}.json")
               for x in FORBIDDEN_DIRS):
            touched.append(pth)
    chk(valuation.COVERAGE_MIN_PCT >= 50.0,
        f"估值带的覆盖率门槛还在({valuation.COVERAGE_MIN_PCT:.0f}%,要 ≥50%)—— "
        f"低于一半的日子算不出来时,那条带的分位数不能当分位数读")
    chk(not cover_bad, f"{len(codes)} 只票的估值带没有「覆盖率不够却判 ok」的"
                       + (f" —— **有**:{cover_bad}" if cover_bad else ""))
    chk(not band_bad, f"{len(codes)} 只的营收区间满足 low ≤ mid ≤ high"
                      + (f" —— **中枢跑到区间外的**:{band_bad}" if band_bad else ""))
    chk(not tail_bad, f"{len(codes)} 只走完四步之后 bull ≤ base 且 bear ≤ base"
                      + (f" —— **翻过去的**:{tail_bad}" if tail_bad else ""))
    chk(not sell_bad,
        "没有一只在「盈利侧没有证据」的情况下被判清仓"
        + (f" —— **有**:{sell_bad}。清仓不可逆,而 EV 大半由估值倍数拉开,"
           f"估值回归是假设不是证据" if sell_bad else ""))
    chk(not touched,
        f"跑完 {len(codes)} 只的决策链,**运行期一次都没打开过** "
        f"{'/'.join(FORBIDDEN_DIRS)} 下的文件"
        + (f" —— **打开过**:{sorted(set(touched))[:4]}" if touched else ""))


def check_reports(raw: Path):
    """⑩ 账户文档含「五只 bear 同时兑现」表;第 12 节列的路径 glob 都非空。"""
    root = raw.parent.parent
    src = (HERE / "margin_account.py").read_text(encoding="utf-8")
    chk("同时兑现最坏那一档" in src,
        "账户文档里有「五只同时兑现最坏那一档」那一节"
        "(把每只的最坏跌幅接到维持担保比例上 —— 这个账户唯一致命的问题)")
    chk("max_drawdown_1y" in src,
        "算不出 bear 的票用**近一年最大回撤**代替,不按「不跌」算 —— "
        "当它不跌会让这张最坏情况表系统性偏乐观")
    # 上面两条是**静态检查**(读的是代码本身),没有数据盘也照跑。
    # 下面这几条要 glob 数据盘 —— 目录不在就跳过,不判红。
    if not have_data(raw, "⑩ 的数据源核对部分"):
        return
    sys.path.insert(0, str(HERE.parent.parent / "data-sources" / "scripts"))
    import source_catalog as catalog
    got, miss = catalog.present(catalog.rows(None, CODES[0]), root)
    chk(bool(got), f"数据源目录里至少有一条在盘上真有文件(现在 {len(got)} 条)"
                   + ("" if got else
                      f" —— `{raw}` 在,却一条源都 glob 不到文件。"
                      f"重跑:`tools/fetch_all.py --codes {CODES[0]} --group quotes`"))
    for _, name, _w, _u, _k, path, _c in got:
        hits = list(root.glob(catalog._glob_of(path)))
        chk(bool(hits), f"数据源「{name}」的落盘路径 `{path}` glob 到了 {len(hits)} 个文件")
    if miss:
        print(f"  · 本轮没有数据、已从表里剔除的源:{len(miss)} 条 "
              f"({'、'.join(x[1] for x in miss[:4])}…)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw",
                    help="原始数据目录;journal 从它的上一级找")
    a = ap.parse_args()
    raw = Path(a.raw)
    print("== 判断流程的合约检查 ==\n")
    steps = [
        ("① report.py 顺序", check_order, ()),
        ("② 决策链不碰机构预测(AST)", check_no_consensus, ()),
        ("③ journal 自洽 + 字段齐全", check_journal, (raw,)),
        ("④ 行业表不漂", check_sector_rules, ()),
        ("⑤ 概率算法", check_probability_rules, ()),
        ("⑤c EV → 动作的两道门", check_action_rules, ()),
        ("⑤d 第 7 节执行量:排序不决定卖多少", check_plan_rule, ()),
        ("⑤e 确信度分母跟着行业走", check_conf_denominator, ()),
        ("⑥ 按估值尺分派", check_lens_dispatch, ()),
        ("⑦ 没有写死的横向数", check_no_hardcoded_cross, ()),
        ("⑧ 资本开支覆盖率", check_capex_coverage, (raw,)),
        ("⑨ 跑一遍五只(区间 / 尾部 / 清仓门槛 / 运行期不碰预测)", check_live, (raw,)),
        ("⑩ 报告层", check_reports, (raw,)),
        ("⑩b 最坏情况表不按「不跌」算", check_stress_drop, (raw,)),
        ("⑩c 外部研报只印研报内容", check_extern_notes, ()),
    ]
    for label, fn, args in steps:
        print(f"── {label} ──")
        try:
            fn(*args)
        except Exception as e:                       # noqa: BLE001
            # ⚠ **每个 check 自己包住。** 2026-09-02 破坏测试:一个 import 崩掉
            #   让整轮输出只剩一段 traceback,已经记下的违规一条都没打出来。
            chk(False, f"{label} **自己崩了**:{e!r}")
            print("     " + "\n     ".join(
                traceback.format_exc().strip().splitlines()[-4:]))
        print()
    tail = (f",另有 {len(SKIPS)} 项跳过(没有数据盘,要在私有仓跑)"
            if SKIPS else "")
    if FAILS:
        print(f"有 {len(FAILS)} 项没落实{tail}。")
        print("**这里查的每件事错了都是静默的** —— 报告照样生成、格式照样对、"
              "每个数都算得出来,只是其中一条是错的。")
        return 1
    print(f"全部 {len(OKS)} 项通过{tail}。")
    if SKIPS:
        print("⏭ 跳过的不算通过 —— 它们要在有数据盘的那个仓里跑:")
        for m in SKIPS:
            print(f"   · {m.split(' ——')[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
