#!/usr/bin/env python3
"""决策记录 —— 每出一份报告,往 `data/journal.jsonl` 写一条。

## 为什么必须写

**不记录就永远说不清自己准不准。** 三个月后回头看,人只会记得对的那几次;
错的那几次会被解释成「当时情况特殊」。而这套方法的价值恰恰建立在
「概率给得准不准」上 —— 概率是可以被统计检验的:
记 20 次 bull 25%,如果实际发生了 10 次,那不是运气好,是概率给低了。

所以记的不只是结论,还有**当时的三情景价位、概率、期望值、可证伪条件**。
只记「2026-09-02 建议减仓」这一句,复核时无从核起 ——
减仓对不对取决于后来跌了没有,而「跌了没有」要跟当时说的哪个价位比?

## 记录结构照抄 `stock-analysis-workflow/scripts/journal.py` 的 `cmd_log`

字段:id / logged_at / code / price / cost / action / ev / probs / scenario_px /
thesis / falsify / check_date / status。**不另造一套** ——
那边的 `journal review` / `journal close` 读的就是这个结构,
另造一套等于这些记录复核工具认不出来。

## 同一天同一票只留一条

重跑报告是常事(改了一行文案就重跑)。每跑一次追加一条,journal 里
同一天同一只票会有五条一模一样的记录,**而复核工具会把它们当成五次独立判断**——
统计准确率时同一个判断被数了五遍。所以写之前先删掉当天同代码的旧条。

⚠ **只删当天的。** 昨天那条是昨天的判断,今天改主意了不代表昨天没判断过 ——
删掉历史记录等于把自己的错误抹掉,那正是这个文件要防的事。

## 路径从 `--raw` 推,不猜

`journal.py` 用 `_lab_root()` 往上找 `.astock-lab-root` 标记文件。
脚本在公开仓、数据在私有仓时,它会找到**公开仓**那个根,
于是写进公开仓的 `data/journal.jsonl` —— 那个文件没人读,而且是公开的。
这里一律用 `--raw 的上一级 / journal.jsonl`,和数据待在一起。

依赖:无。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def journal_path(raw: Path) -> Path:
    """`data/raw` → `data/journal.jsonl`。**和数据同仓,不靠标记文件猜。**"""
    return raw.parent / "journal.jsonl"


def load(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 坏行保留在文件里、跳过不解析 —— 直接丢掉等于悄悄删记录
            continue
    return out


def save(p: Path, rows: list) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def make_thesis(a: dict, f: dict, cash: dict) -> str:
    """当时的判断,一句话。**五块拼起来,每块都是这次算出来的数**,不是形容词。

    为什么固定这五块:行业决定用哪把尺、领先方向决定概率往哪偏、
    基准率决定 bull 能给多高、方法数决定分布有多散、现金转化率决定
    利润是不是真的收到了。复核时逐块对,哪一块变了一目了然。
    """
    d = a["决策"]
    lead = a["领先"]
    parts = [f"行业 {a.get('行业') or '未定义'}(按{d.get('性质')}股那套算)"]
    if lead.get("方向"):
        ups = sum(1 for x in lead["方向"] if x["方向"] == "↑")
        downs = sum(1 for x in lead["方向"] if x["方向"] == "↓")
        parts.append(f"领先指标 {ups}↑/{downs}↓(净 {lead['净方向']:+d}):"
                     + "、".join(f"{x['来自']}{x['方向']}" for x in lead["方向"]))
    else:
        parts.append("领先指标一个方向都没拿到,概率不做领先层修正")
    o = a["基准率"]
    if o.get("ok"):
        cap = d["概率"]["封顶"]
        parts.append(f"基准率 bull 封顶 {cap}%"
                     + ("(已生效)" if d["概率"]["封顶生效"] else "(未触及)"))
    else:
        parts.append(f"基准率不适用/算不出({(o.get('why') or '')[:36]})→ bull 不封顶")
    n = f.get("可用方法数", 0)
    parts.append(f"{n}/3 个预测方法适用"
                 + ("、区间重叠" if f.get("方法一致", True) else "、但区间不重叠(互相矛盾)"))
    parts = [x for x in parts if x]
    if cash.get("ok") and cash.get("现金转化率") is not None:
        parts.append(f"现金转化率 {cash['现金转化率']:.0f}%")
    else:
        parts.append("现金转化率算不出")
    return ";".join(parts) + "。"


def make_falsify(a: dict, f: dict, rv, p, gm_min) -> str:
    """可证伪条件 —— 复核日就核这几条。**每条都要带一个当时的数**。

    「毛利率不能掉太多」是废话;「不低于 46.9%(近四季最低)」才能被核对。
    写不出数字的条目就不写 —— 那说明我们其实没有依据。
    """
    d = a["决策"]
    out = []
    if rv:
        out.append(f"①{f['预测期']} 单季营收落在 {rv['low'] / 1e8:.1f}~"
                   f"{rv['high'] / 1e8:.1f} 亿之外")
    if p:
        out.append(f"②{f['预测期']} 单季归母净利落在 {p['low'] / 1e8:.1f}~"
                   f"{p['high'] / 1e8:.1f} 亿之外")
    if gm_min is not None:
        out.append(f"③单季毛利率跌破 {gm_min:.1f}%(近四季最低)")
    if d.get("ok"):
        sc = d["三情景"]["情景"]
        out.append(f"④价格跌破 bear 价位 {sc['bear']['价']:,.2f} 元"
                   f"({sc['bear']['涨跌']:+.1f}%)")
        lead = a["领先"]
        if lead.get("方向"):
            out.append("⑤领先指标转向:" + "、".join(
                f"{x['来自']}从 {x['方向']} 翻面" for x in lead["方向"]))
        # PB 路径的票,结论建在「估值回归」上,而那条路径的**前提**是
        # 「市场没有看见我们看不见的东西」。这个前提是可以证伪的,要写下来。
        if d["三情景"].get("路径") == "PB":
            out.append("⑥合同负债同比或新签订单公告显著上升(报表之外出现新的量)"
                       " —— 那说明「没有我们看不见的新信息」这个前提错了,"
                       "估值高于历史极值可能有它的道理,这条结论要重估")
    return " ".join(out) or "(这次没写出任何带数字的条件 —— 说明依据不足)"


def write(raw: Path, code: str, a: dict, f: dict, position: dict,
          cash: dict, gm_min, today: str | None = None) -> dict:
    """写一条。返回写进去的那条记录(报告第 10 节要把它印出来)。

    ⚠ **EV 用存进去的那三个价位重算一遍**,不直接抄报告里那个数。
    两边如果算法有一丝不同,`check_report_contract.py` 的 EV 自洽检查会报 ——
    而那道检查存在的意义就是让「两边算法不一致」当场暴露,不是让我们绕过它。
    """
    d = a["决策"]
    p = journal_path(raw)
    rows = load(p)
    day = today or dt.date.today().isoformat()
    # 同一天同一票只留一条:先删当天的旧条,**只删当天的**
    kept = [r for r in rows
            if not (r.get("code") == code
                    and str(r.get("logged_at", ""))[:10] == day)]
    dropped = len(rows) - len(kept)
    rid = max([r.get("id", 0) for r in rows], default=0) + 1

    # ⚠ **不给动作的记录也要有价。** 那条记录记的是「这一天我们看了它,
    #   而且决定不给动作」—— 没有当时的价,下次回来连「那天它多少钱」都答不上来。
    spot = d["三情景"]["现价"] if d.get("ok") else d.get("现价")
    probs, px, ev = None, None, None
    if d.get("ok"):
        probs = {k: round(v, 4) for k, v in d["概率"]["probs"].items()}
        sc = d["三情景"]["情景"]
        px = {k: round(sc[k]["价"], 2) for k in ("bear", "base", "bull")}
        for k in ("bear", "base", "bull"):
            px[f"{k}_chg"] = round((px[k] / spot - 1) * 100, 1)
        ev = round(sum(probs[k] * (px[k] / spot - 1) * 100
                       for k in ("bear", "base", "bull")), 1)

    rec = {
        "id": rid,
        "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "code": code,
        "price": spot,
        "cost": position.get("cost"),
        "action": a["动作"],
        "ev": ev,
        "probs": probs,
        "scenario_px": px,
        "thesis": make_thesis(a, f, cash),
        "falsify": make_falsify(a, f, f.get("营收区间"), f.get("归母净利区间"), gm_min),
        "check_date": d.get("复核日"),
        # 「这次为什么没给动作」。**空字符串和「不给动作」是两回事** ——
        # 前者是正常给了动作,后者要在复核时知道当初卡在哪一步。
        "no_action_why": "" if d.get("ok") else (d.get("why") or "未说明"),
        "status": "open",
    }
    save(p, kept + [rec])
    rec["_覆盖了当天旧条数"] = dropped
    rec["_文件"] = str(p)
    return rec


def render(rec: dict, n: str, L: list) -> None:
    """报告第 10 节里那一小块:这次记了什么。"""
    L.append(f"### {n} 这次的判断已经记进决策日志")
    L.append("")
    L.append(f"写进 `{rec['_文件']}` 第 **#{rec['id']}** 条"
             + (f"(覆盖了当天同一只票的 {rec['_覆盖了当天旧条数']} 条旧记录 —— "
                f"**同一天同一票只留一条**,否则同一个判断会被统计成好几次)"
                if rec["_覆盖了当天旧条数"] else "")
             + "。")
    L.append("")
    L.append("| 字段 | 值 |")
    L.append("|---|---|")
    L.append(f"| 记录时间 | {rec['logged_at']} |")
    L.append(f"| 当时价格 | {rec['price']} |")
    L.append(f"| 动作 | **{rec['action']}** |")
    L.append(f"| 期望值 | **{rec['ev']}%** |")
    if rec["probs"]:
        L.append(f"| 概率 | bear {rec['probs']['bear'] * 100:.0f}% / "
                 f"base {rec['probs']['base'] * 100:.0f}% / "
                 f"bull {rec['probs']['bull'] * 100:.0f}% |")
    if rec["scenario_px"]:
        s = rec["scenario_px"]
        L.append(f"| 三情景价位 | bear {s['bear']} / base {s['base']} / "
                 f"bull {s['bull']} |")
    L.append(f"| 复核日 | **{rec['check_date']}** |")
    L.append(f"| 当时的判断 | {rec['thesis']} |")
    L.append(f"| 可证伪条件 | {rec['falsify']} |")
    L.append("")
    L.append("**为什么要存三情景价位和概率,而不只是存结论**:"
             "「2026-09-02 建议减仓」这一句复核时无从核起 —— "
             "减仓对不对取决于后来跌了没有,而「跌了没有」要跟当时说的哪个价位比?"
             "更重要的是**概率可以被统计检验**:记 20 次 bull 25%,"
             "如果实际发生了 10 次,那不是运气好,是概率给低了。"
             "这件事只有攒够记录才看得出来。")
    L.append("")
    L.append("复核用:")
    L.append("")
    L.append("```bash")
    L.append("tools/journal.py review              # 列出到期待复核的")
    L.append(f"tools/journal.py close {rec['id']} --outcome right|wrong|partial \\")
    L.append('    --note "实际值 …,当时的判断错在 …"')
    L.append("```")
    L.append("")
