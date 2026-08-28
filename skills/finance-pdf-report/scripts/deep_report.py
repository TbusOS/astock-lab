#!/usr/bin/env python3
"""deep_report —— 把快照 + journal 渲染成**给人看的深度分析报告**。

    $P $LAB/tools/deep_report.py 300308 --out r.html

和 `preport` 的分工（2026-08-28 被问「为什么只有一只票有投资分析报告」才补的这一层）:

    preport      → 数据层。0a~9 十节表格，每个数都有出处，机器生成，可复核。
                   它诚实，但**它不替你下判断** —— 判断留在对话里，没进文件。
    deep_report  → 结论层。结论前置、三档操作、证据分正反、赔率图、数据来源。
                   人真正会读的是这一份。

为什么必须是两个而不是把 preport 改大:
  ① 两者的**输入不同**。preport 输入是「代码 + 成本」，跑一次网络取数;
     deep_report 输入是「已经存好的快照 + 已经记好的 journal」，**不联网也能重出**。
     混在一起就变成「想重排一下版式也得把所有接口再打一遍」。
  ② 两者的**可信度来源不同**。preport 的每个数来自接口，错了是接口的事;
     deep_report 的每句判断来自 journal 里当时写下的 thesis，错了是**判断**的事。
     混在一起，读者分不清哪句是数据、哪句是观点。

★ 本工具**不发明任何判断**。结论、三情景、可证伪判据全部来自 journal 那条记录 ——
  也就是当时真写下来、将来要被复核的那份。它只做两件事:把结构化的数渲染成图表，
  把已有的判断排进版式。所以 `journal log` 没记的票，这里出不了报告，这是有意的。

数据来源（都在本机，不联网）:
    data/snapshots/<代码>/<日期>.json   ← preport 每跑一次自动落的证据层
    data/journal.jsonl                  ← journal log 记的决策层
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import html as _html  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
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
SNAPDIR = LAB / "data" / "snapshots"

E = _html.escape


def ET(t):
    """转义 + 把 journal 里手写的 **粗体** 渲成 <b>。

    为什么需要:thesis 和 falsify 是我在 `journal log` 时手打的中文，
    里面本来就用 `**…**` 标重点。直接 escape 会把星号原样印出来，
    读者看到的是 `**净利同比 -3.6%**` 这种噪声 —— 重点反而被削弱。
    只放行这一种标记，不引入 markdown 解析器（那会带来注入面）。"""
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _html.escape(t or ""))


# ══════════════════════════════════════════════════════════════════════════
# 读数据
# ══════════════════════════════════════════════════════════════════════════
def load_snapshot(code, date=None):
    d = SNAPDIR / code
    if not d.is_dir():
        return None, None
    files = sorted(d.glob("*.json"))
    if not files:
        return None, None
    if date:
        want = d / f"{date}.json"
        if not want.exists():
            have = ", ".join(f.stem for f in files)
            raise SystemExit(f"没有 {code} 在 {date} 的快照。已有:{have}")
        f = want
    else:
        f = files[-1]
    return json.loads(f.read_text(encoding="utf-8")), f


def load_journal(code):
    """取这只票**最近一条**记录。优先未结的（open）——
    已结的那条讲的是过去某次判断，拿它当「现在的结论」会误导。"""
    if not JOURNAL.exists():
        return None
    rows = [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    mine = [r for r in rows if r.get("code") == code]
    if not mine:
        return None
    op = [r for r in mine if r.get("status") == "open"]
    return (op or mine)[-1]


# ══════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════
def g(d, *path, default=None):
    """安全取嵌套值。快照里任何一层都可能因为当时取数失败而缺席。"""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur or cur[k] is None:
            return default
        cur = cur[k]
    return cur


def num(v, nd=2, suffix="", plus=False):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f):
        return "—"
    s = f"{f:+.{nd}f}" if plus else f"{f:,.{nd}f}"
    return s + suffix


def cls_of(v, good_is_up=True):
    if v is None:
        return "ink"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "ink"
    if abs(f) < 1e-9:
        return "ink"
    up = f > 0
    return "pos" if up == good_is_up else "neg"


# ══════════════════════════════════════════════════════════════════════════
# SVG 图
#
# 为什么手写 SVG 而不用 matplotlib:
#   ① 报告要能单文件带走 —— 内联 SVG 是文本，matplotlib 出的是位图或另一个文件
#   ② 打印成 PDF 时矢量不糊
#   ③ 少一个重量级依赖。这几张图的形态是固定的，不需要绘图库的通用能力
# ══════════════════════════════════════════════════════════════════════════
SVG_OPEN = ('<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;'
            'background:#fff;border:1px solid #e8e6dc;border-radius:8px;">')

C_INK, C_ACC, C_ACCD = "#141413", "#d97757", "#c2613f"
C_GOOD, C_GOODD, C_MUT = "#788c5d", "#5d7045", "#6b6a5f"
C_WARN, C_LINE, C_GREY = "#c9913f", "#d8d4c8", "#a8a496"


def svg_price_axis(marks, w=780, h=215):
    """价格坐标图 —— 本报告的招牌图。

    横轴是价格，把「你在哪、目标在哪、支撑压力在哪」放到同一根轴上。
    **等距映射**（不是等比），并在图注里写明，免得读者按视觉距离估涨跌幅。

    marks: [{px, label, sub, color, weight, dash}] —— px 是价格。
    """
    marks = [m for m in marks if m.get("px") is not None]
    if len(marks) < 2:
        return ""
    lo = min(m["px"] for m in marks)
    hi = max(m["px"] for m in marks)
    if hi - lo < 1e-9:
        return ""
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    x0, x1, base = 62, w - 30, h - 62

    def X(p):
        return x0 + (p - lo) / (hi - lo) * (x1 - x0)

    # 标签防重叠。**两行交替不够用** —— 2026-08-28 渲染出来一看，
    # 现价/成本/MA20/MA60/MA120 五个数挤在一起时两行照样叠成一团。
    # 改成贪心分行:每个标签放进「最靠下的、放得下的那一行」，行不够就再开一行。
    # 图会随之长高，这是对的 —— 挤成一团的图等于没画。
    marks = sorted(marks, key=lambda m: m["px"])

    # ⚠ 分行宽度必须按**最长的那个标签**算，不能按数字算。
    #   第一版按数字宽度分行 —— 数字确实不叠了，但下面的
    #   「MA120 / 牛市情景 / 你的成本 / MA20 / MA60」照样糊成一团，
    #   因为中文标签比数字宽一倍。渲染出来才看见。
    def _halfw(m):
        wnum = len(m["txt"]) * (3.6 if m.get("weight", 1) >= 2 else 3.0)
        wlab = len(m["label"]) * 5.2          # 中文标签，一个字约 9.2px
        wsub = len(m.get("sub") or "") * 3.0
        return max(wnum, wlab, wsub) + 5

    rows, rowx = [], []          # rowx[i] = 第 i 行已占到的最右 x
    for m in marks:
        x, hw = X(m["px"]), _halfw(m)
        for i, rx in enumerate(rowx):
            if x - hw > rx:
                rows.append(i)
                rowx[i] = x + hw
                break
        else:
            rows.append(len(rowx))
            rowx.append(x + hw)
    nrow = len(rowx)
    # 上下都要按行错开:上面放数字，下面放标签+副标题，各占 nrow 层
    h = max(h, 46 + nrow * 22 + 20 + nrow * 24 + 26)
    base = 46 + nrow * 22 + 14
    out = [SVG_OPEN.format(w=w, h=h),
           f'<text x="16" y="22" font-size="11" font-weight="700" fill="{C_ACC}"'
           f' letter-spacing="1">价格坐标：你在哪，目标在哪</text>',
           f'<line x1="{x0}" y1="{base}" x2="{x1}" y2="{base}" '
           f'stroke="{C_LINE}" stroke-width="1"/>']
    for m, ri in zip(marks, rows):
        x = X(m["px"])
        top = 40 + ri * 22
        boty = base + 15 + ri * 24
        col = m.get("color", C_MUT)
        wt = m.get("weight", 1.0)
        dash = ' stroke-dasharray="3 3"' if m.get("dash") else ""
        # 竖线起点要躲开本行的数字。重点标的字号 12，比普通的 9.8 高，
        # 用同一个偏移会被线穿过（300308 的「1,157.90」就被穿了）。
        out.append(f'<g><line x1="{x:.0f}" y1="{top + (11 if wt >= 2 else 8)}" '
                   f'x2="{x:.0f}" y2="{base}" '
                   f'stroke="{col}" stroke-width="{wt}"{dash}/>')
        if wt >= 2:
            out.append(f'<circle cx="{x:.0f}" cy="{base}" r="4.5" fill="{col}"/>')
        fs = 12 if wt >= 2 else 9.8
        fw = 700 if wt >= 2 else 600
        out.append(f'<text x="{x:.0f}" y="{top}" text-anchor="middle" '
                   f'font-size="{fs}" font-weight="{fw}" fill="{col}">'
                   f'{E(m["txt"])}</text>')
        if ri:   # 被挤到下面几行的标签，画引线连回轴上那个点，
                 # 否则读者不知道「你的成本」对应的是哪根竖线
            out.append(f'<line x1="{x:.0f}" y1="{base + 3}" x2="{x:.0f}" '
                       f'y2="{boty - 8}" stroke="{col}" stroke-width="0.6" '
                       f'opacity="0.45"/>')
        out.append(f'<text x="{x:.0f}" y="{boty}" text-anchor="middle" '
                   f'font-size="9.2" font-weight="{600 if wt >= 2 else 400}" '
                   f'fill="{col}">{E(m["label"])}</text>')
        if m.get("sub"):
            out.append(f'<text x="{x:.0f}" y="{boty + 11}" text-anchor="middle" '
                       f'font-size="8.6" fill="{C_MUT}">{E(m["sub"])}</text>')
        out.append("</g>")
    out.append(f'<text x="{x1}" y="{h - 8}" text-anchor="end" font-size="8.6" '
               f'fill="{C_MUT}">横轴为价格（元），等距映射 —— '
               f'视觉距离不等于涨跌幅</text>')
    out.append("</svg>")
    return "\n".join(out)


def svg_scenarios(price, px, probs, ev, w=780, h=225, tol=None):
    """三情景赔率图。左侧是下行，右侧是上行，现价是那根竖线。

    为什么要画:EV 是一个数，看不出「上行靠什么、下行有多深」。
    你能不能拿得住，取决于 bear 那根有多长，不取决于 EV 正不正。
    """
    keys = [("bear", "熊", C_ACCD), ("base", "中", C_MUT), ("bull", "牛", C_GOODD)]
    have = [(k, nm, c) for k, nm, c in keys if px.get(k) is not None]
    if not have or not price:
        return ""
    chgs = [px[k] / price - 1 for k, _, _ in have]
    lo, hi = min(min(chgs), -0.05), max(max(chgs), 0.05)
    span = max(abs(lo), abs(hi)) * 1.18
    x0, x1 = 96, w - 118
    zero = x0 + (x1 - x0) * (abs(-span) / (2 * span))

    def X(c):
        return x0 + (x1 - x0) * ((c + span) / (2 * span))

    out = [SVG_OPEN.format(w=w, h=h),
           f'<text x="16" y="22" font-size="11" font-weight="700" fill="{C_ACC}" '
           f'letter-spacing="1">三情景：上行靠什么，下行有多深</text>']
    y = 48
    for (k, nm, c), ch in zip(have, chgs):
        xe = X(ch)
        left = min(zero, xe)
        out.append(f'<rect x="{left:.0f}" y="{y}" width="{abs(xe - zero):.0f}" '
                   f'height="26" fill="{c}" opacity="0.30" rx="3"/>')
        out.append(f'<text x="{x0 - 10}" y="{y + 18}" text-anchor="end" '
                   f'font-size="10.5" font-weight="600" fill="{c}">{nm}　'
                   f'{probs.get(k, 0) * 100:.0f}%</text>')
        tx = xe + (7 if ch >= 0 else -7)
        anc = "start" if ch >= 0 else "end"
        out.append(f'<text x="{tx:.0f}" y="{y + 17}" text-anchor="{anc}" '
                   f'font-size="10.5" font-weight="700" fill="{c}">'
                   f'{px[k]:,.0f}　{ch * 100:+.1f}%</text>')
        y += 36
    # 回撤容忍线。为什么值得单独画一条:
    #   EV 是正的、图看着也好看，但**能不能拿住取决于熊市那根越不越线**，
    #   而不是 EV 正不正。688328 就是这个形状 —— EV +14.6%（绿的），
    #   熊市 −49.2%（越线 19pp）。不画线，这件事就得读者自己心算。
    if tol:
        tx = X(-abs(tol) / 100)
        if tx > x0 - 4:
            out.append(f'<line x1="{tx:.0f}" y1="34" x2="{tx:.0f}" y2="{y - 2}" '
                       f'stroke="{C_WARN}" stroke-width="1.6" stroke-dasharray="5 3"/>')
            out.append(f'<text x="{tx:.0f}" y="28" text-anchor="middle" '
                       f'font-size="9.4" font-weight="700" fill="{C_WARN}">'
                       f'你的容忍线 −{abs(tol):.0f}%</text>')
            bc = px.get("bear")
            if bc is not None and (bc / price - 1) * 100 < -abs(tol):
                over = abs((bc / price - 1) * 100) - abs(tol)
                out.append(f'<text x="{x0 - 10}" y="{y + 12}" text-anchor="end" '
                           f'font-size="9.6" font-weight="700" fill="{C_ACCD}">'
                           f'熊市越线 {over:.0f}pp</text>')
    out.append(f'<line x1="{zero:.0f}" y1="38" x2="{zero:.0f}" y2="{y - 4}" '
               f'stroke="{C_INK}" stroke-width="2"/>')
    out.append(f'<text x="{zero:.0f}" y="{y + 12}" text-anchor="middle" '
               f'font-size="9.5" font-weight="600" fill="{C_INK}">'
               f'现价 {price:,.2f}</text>')
    if ev is not None:
        evc = C_GOODD if ev > 0 else C_ACCD
        out.append(f'<text x="{w - 20}" y="30" text-anchor="end" font-size="10.5" '
                   f'fill="{C_MUT}">概率加权期望值</text>')
        out.append(f'<text x="{w - 20}" y="{y + 12}" text-anchor="end" '
                   f'font-size="17" font-weight="700" fill="{evc}">'
                   f'EV {ev:+.1f}%</text>')
    out.append("</svg>")
    return "\n".join(out)


def svg_bars(title, rows, w=780, h=None, unit="%"):
    """通用横条对比图（海外同业、基准率这类两三个数的对照）。
    rows: [(标签, 数值, 颜色, 说明)]"""
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return ""
    h = h or (52 + 34 * len(rows))
    x0, x1 = 132, w - 132
    vals = [r[1] for r in rows]
    span = max(abs(min(vals + [0])), abs(max(vals + [0]))) * 1.2 or 1
    zero = x0 + (x1 - x0) * 0.5 if min(vals) < 0 else x0

    def X(v):
        if min(vals) < 0:
            return zero + (x1 - x0) * 0.5 * (v / span)
        return x0 + (x1 - x0) * (v / span)

    out = [SVG_OPEN.format(w=w, h=h),
           f'<text x="16" y="22" font-size="11" font-weight="700" fill="{C_ACC}" '
           f'letter-spacing="1">{E(title)}</text>']
    y = 40
    for label, v, col, note in rows:
        xe = X(v)
        left = min(zero, xe)
        out.append(f'<rect x="{left:.0f}" y="{y}" width="{max(abs(xe - zero), 1):.0f}" '
                   f'height="22" fill="{col}" opacity="0.32" rx="3"/>')
        out.append(f'<text x="{x0 - 10}" y="{y + 16}" text-anchor="end" '
                   f'font-size="10" font-weight="600" fill="{C_INK}">{E(label)}</text>')
        tx = xe + (7 if v >= 0 else -7)
        anc = "start" if v >= 0 else "end"
        out.append(f'<text x="{tx:.0f}" y="{y + 16}" text-anchor="{anc}" '
                   f'font-size="10.5" font-weight="700" fill="{col}">'
                   f'{v:+.1f}{unit}</text>')
        if note:
            out.append(f'<text x="{w - 16}" y="{y + 16}" text-anchor="end" '
                       f'font-size="8.8" fill="{C_MUT}">{E(note)}</text>')
        y += 34
    if min(vals) < 0:
        out.append(f'<line x1="{zero:.0f}" y1="34" x2="{zero:.0f}" y2="{y - 6}" '
                   f'stroke="{C_LINE}" stroke-width="1"/>')
    out.append("</svg>")
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════
# 证据:从快照的数自动生成，每条 = 一个数 + 一句判读
#
# ★ 判读句是**规则**不是模型输出 —— 阈值写死在这里，可以被质疑、被改。
#   哪条规则给出的判断，报告里就标哪条规则的阈值，读者能自己复核。
# ══════════════════════════════════════════════════════════════════════════
def build_evidence(f, sector_invalid):
    """返回 (正面, 反面, 中性) 三组证据。每条 = dict(t=标题, v=数值串, why=判读)"""
    pro, con, neu = [], [], []
    inv = set(sector_invalid or [])

    def add(bucket, t, v, why, rule=""):
        bucket.append({"t": t, "v": v, "why": why, "rule": rule})

    # ① 海外同业背离 —— 需求端在海外，只看 A 股会漏
    gap = g(f, "peers", "gap_pp")
    if gap is not None:
        pa = g(f, "peers", "peer_1m_avg")
        sa = g(f, "peers", "self_1m")
        v = f"海外同业近 1 月 {pa:+.1f}%，本股 {sa:+.1f}%，背离 {gap:+.1f}pp"
        if gap >= 15:
            add(pro, "海外同业正向背离", v,
                "同一条需求链，海外在涨而本股在跌 —— 需求端没恶化，跌的是情绪。"
                "这是可下注的那种背离。", "背离 ≥ +15pp 记正面")
        elif gap <= -15:
            add(con, "海外同业反向背离", v,
                "本股跑赢海外同业一大截，而需求源头在海外 —— "
                "<b>可能已经透支</b>，不是利好。", "背离 ≤ −15pp 记反面")
        else:
            add(neu, "与海外同业同步", v,
                "走势同步，没有可利用的背离 —— 这一条既不支持也不反对，"
                "但它意味着<b>你赚不到「市场看错了」的钱</b>。", "|背离| < 15pp 记中性")

    # ② 盈利的水平与加速度 —— 看二阶导不看水平值
    ny, nq = g(f, "fundamental", "net_yoy"), g(f, "fundamental", "net_qoq")
    if ny is not None:
        v = f"净利同比 {ny:+.1f}%" + (f"，环比 {nq:+.1f}%" if nq is not None else "")
        if ny > 30 and (nq or 0) > 0:
            add(pro, "盈利在加速", v,
                "同比高且环比还在正增长 —— 增速的二阶导没转负，"
                "顶部信号还没出现。", "同比 >30% 且环比 >0")
        elif ny < 0:
            add(con, "盈利在退", v,
                "同比负增长。<b>这条压过所有估值讨论</b> —— "
                "便宜的前提是盈利不塌。", "同比 < 0")
        else:
            add(neu, "盈利增速平淡", v, "谈不上加速也没退。", "")

    gm = g(f, "fundamental", "gm")
    if gm is not None:
        add(neu, "毛利率", f"{gm:.1f}%",
            "毛利率的粘性高于增速（基准率 66% vs 24%），所以"
            "<b>「毛利率还没动」不能当成「增速能撑住」的证据</b> —— "
            "它本来就是更慢的那个指标。", "")

    # ③ 估值:PEG / 分位。周期股这两个会给反向假信号，按赛道禁用
    peg = g(f, "valuation", "peg")
    if peg is not None and "peg" not in inv:
        if peg < 1:
            add(pro, "PEG < 1", f"PEG {peg:.2f}",
                "增速能撑住当前估值。", "PEG < 1")
        else:
            add(con, "PEG > 1", f"PEG {peg:.2f}",
                "按当前增速，估值偏贵。", "PEG ≥ 1")
    elif peg is not None:
        add(neu, "PEG（本赛道不适用）", f"PEG {peg:.2f}",
            "⚠️ 周期股的 PEG 在盈利低谷给「便宜」假信号、高峰给「贵」假信号。"
            "<b>这个数在这里不能用</b>，列出来只是让你知道别去引用它。", "赛道判定为不适用")

    pep, pbp = g(f, "valuation", "pe_pctl"), g(f, "valuation", "pb_pctl")
    if pep is not None and "pe_percentile" in inv:
        add(neu, "PE/PB 分位（本赛道不适用）",
            f"PE 分位 {pep:.0f}%" + (f"，PB 分位 {pbp:.0f}%" if pbp is not None else ""),
            "⚠️ 周期股在 PE 最高时往往是<b>盈利低谷</b>（该买）、PE 最低时是盈利高峰"
            "（该卖），与成长股相反。<b>但要注意例外</b>：高分位同时伴随"
            "<b>盈利同比转负</b>时，「低谷」那套解释不成立 —— 那是估值高而业绩在退，"
            "两件坏事叠在一起。这条要人来判，工具不替你判。", "赛道判定为不适用")
    elif pep is not None:
        v = f"PE 分位 {pep:.0f}%" + (f"，PB 分位 {pbp:.0f}%" if pbp is not None else "")
        if max(pep, pbp or 0) >= 85:
            add(con, "估值在历史高位", v,
                "分位 ≥85% 意味着历史上绝大多数时候都比现在便宜 —— "
                "安全边际很薄。", "分位 ≥ 85%")
        elif pep <= 50:
            add(pro, "估值不贵", v, "PE 分位在历史中位以下。", "PE 分位 ≤ 50%")
        else:
            add(neu, "估值中性偏上", v, "", "")

    # ④ 筹码:股东户数（滞后但影响反弹阻力）、融资余额、北向参与度
    hc = g(f, "chips", "holders_chg_pct")
    if hc is not None:
        asof = g(f, "chips", "holders_asof", default="")
        v = f"股东户数 {hc:+.1f}%（截至 {asof}）"
        if hc > 20:
            add(con, "筹码在分散", v,
                "户数大增 = 散户接盘、筹码分散，<b>反弹到套牢密集区会遇阻</b>。"
                "这个数滞后约两个月，但它影响的是未来的阻力位。", "户数增幅 > +20%")
        elif hc < -10:
            add(pro, "筹码在集中", v, "户数减少 = 筹码向少数人集中。", "户数增幅 < −10%")

    nb = g(f, "chips", "nb_pct_total")
    if nb is not None:
        add(neu, "北向持股占比", f"{nb:.2f}%（{g(f, 'chips', 'nb_asof', default='')}）",
            "⚠️ 2024-08 起北向<b>净买额不再披露</b>，只剩持股占比与成交活跃度。"
            "拿得到「参与度」，拿不到「方向」—— 涨是买跌是卖分不出来。", "")

    mb = g(f, "chips", "margin_bal")
    if mb is not None:
        add(neu, "融资余额", f"{mb:.1f} 亿",
            "杠杆盘。它不预示方向，但<b>下跌时会自我强化</b>（平仓 → 更跌 → 更平仓）。", "")

    # ⑤ 解禁
    uc, uv = g(f, "risk", "unlock_count"), g(f, "risk", "unlock_value")
    if uc is not None:
        if uc == 0:
            add(pro, "近期无限售解禁", "未来窗口内 0 笔",
                "少一个明确的抛压来源。", "解禁笔数 = 0")
        else:
            add(con, "有限售解禁", f"{uc} 笔，约 {uv:.1f} 亿"
                + (f"，最近一笔 {g(f, 'risk', 'unlock_first')}"
                   if g(f, "risk", "unlock_first") else ""),
                "解禁是<b>日期确定</b>的抛压，不像情绪那样会自己消失。", "解禁笔数 > 0")

    # ⑥ 基准率 —— 外部视角，压住过度乐观
    h50 = g(f, "baserate", "hold_50")
    if h50 is not None and "baserate_growth" not in inv:
        coh = g(f, "baserate", "cohort")
        add(con if h50 < 40 else neu, "基准率（外部视角）",
            f"同类公司四季度后仍保持 ≥50% 增速的只有 {h50:.0f}%"
            + (f"（起始 ≥{coh}% 的样本）" if coh else ""),
            "这不是预测，是<b>约束</b>:你要给 bull 情景高于这个数的概率，"
            "必须说出这家公司凭什么超出基准率 —— "
            "已锁定的长约、独家产能、客户结构，而不是「行业景气」。",
            "基准率 < 40% 记反面")

    return pro, con, neu


# ══════════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════════
CSS = """
  @page { size: A4; margin: 14mm 12mm; }
  *{box-sizing:border-box}
  body, p, li, td, th { font-family:"Noto Sans SC","Source Han Sans SC",
       "PingFang SC","Microsoft YaHei",sans-serif; }
  body { background:#faf9f5; color:#141413; font-size:11.5px; line-height:1.65;
         margin:0; }
  .wrap { max-width:186mm; margin:0 auto; padding:16px 4mm 28px; }

  .cover { border-bottom:3px solid #d97757; padding-bottom:14px; margin-bottom:18px; }
  .cover h1 { font-size:27px; font-weight:600; margin:0 0 6px; line-height:1.25; }
  .meta { font-size:9.6px; color:#6b6a5f; }
  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:14px; }
  .kpi { background:#fff; border:1px solid #e8e6dc; border-radius:8px; padding:10px 12px; }
  .kpi__n { font-size:21px; font-weight:600; line-height:1.1; }
  .kpi__l { font-size:10px; color:#6b6a5f; margin-top:3px; }
  .neg { color:#c2613f; } .pos { color:#5d7045; } .ink { color:#141413; }

  .verdict { background:#f7e4dc; border-left:4px solid #d97757; border-radius:8px;
             padding:14px 18px; margin:16px 0; }
  .verdict h2 { margin:0 0 6px; font-size:17px; border:0; padding:0; }
  .verdict p { margin:0 0 6px; font-size:12.5px; line-height:1.7; }
  .verdict p:last-child { margin-bottom:0; }

  h2 { font-size:18px; font-weight:600; margin:24px 0 10px; padding-bottom:5px;
       border-bottom:1px solid #e8e6dc; }
  h2 .num { color:#d97757; margin-right:7px; }
  h3 { font-size:13px; font-weight:600; margin:15px 0 6px; }

  table { width:100%; border-collapse:collapse; background:#fff; font-size:10.5px;
          border:1px solid #e8e6dc; border-radius:6px; overflow:hidden; margin:8px 0 12px; }
  th { background:#f0ede3; text-align:left; padding:6px 9px; font-weight:600;
       font-size:10px; border-bottom:1px solid #d8d4c8; }
  td { padding:6px 9px; border-bottom:1px solid #f0ede3; vertical-align:top; }
  tr:last-child td { border-bottom:0; }
  td.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }

  .tag { display:inline-block; font-size:9px; font-weight:700; padding:2px 7px;
         border-radius:9px; white-space:nowrap; }
  .t-buy  { background:rgba(120,140,93,.2);  color:#4e6343; }
  .t-hold { background:rgba(201,145,63,.2);  color:#8a5a2a; }
  .t-sell { background:rgba(194,97,63,.18);  color:#c2613f; }
  .t-flat { background:rgba(107,106,95,.15); color:#4a4a42; }

  figure { margin:10px 0 14px; }
  figcaption { font-size:9.5px; color:#6b6a5f; text-align:center; margin-top:5px;
               line-height:1.55; }
  .note { background:#fff; border:1px solid #e8e6dc; border-left:3px solid #6b6a5f;
          border-radius:6px; padding:9px 13px; font-size:10.5px; margin:9px 0; }
  .note--warn { border-left-color:#c9913f; background:#f0e4d8; }
  .note--good { border-left-color:#788c5d; background:#e5e9dd; }
  .note--bad  { border-left-color:#d97757; background:#f7e4dc; }
  .ev { font-size:10.2px; color:#6b6a5f; font-style:normal; }
  .avoid { page-break-inside: avoid; }
  footer { margin-top:24px; padding-top:10px; border-top:1px solid #e8e6dc;
           font-size:9.4px; color:#6b6a5f; line-height:1.7; }
"""

ACTION_CN = {"hold": ("持有", "t-hold"), "add": ("加仓", "t-buy"),
             "trim": ("减仓", "t-sell"), "sell": ("清仓", "t-sell"),
             "watch": ("观察", "t-flat"), "buy": ("买入", "t-buy")}


def fig(svg, caption):
    if not svg:
        return ""
    return (f'<figure class="avoid">{svg}'
            f'<figcaption>{caption}</figcaption></figure>')


def render(snap, jr, snap_path, tol=None):
    f = snap.get("facts", {})
    code, name = snap.get("code", "?"), snap.get("name", "")
    cost = snap.get("cost")
    price = g(f, "position", "price")
    pnl = g(f, "position", "pnl_pct")
    sector = g(snap, "sector", "name", default="未判定")
    inv = g(snap, "sector", "invalid_keys", default=[])
    px = dict((jr or {}).get("scenario_px", {}) or {})
    probs = (jr or {}).get("probs", {}) or {}

    # ⚠ 分母必须只有一个（2026-08-28 自己刚踩的坑，当天上午才把同样的问题
    #    写进 memory）。journal 里存的 bear_chg 是**决策当日**价算出来的，
    #    而报告要展示的是**快照当日**的处境 —— 两个数并排出现却不同源，
    #    读者看不出差别，但差 3%~5% 足以让「跌 28%」变成「跌 24%」。
    #    做法:报告内一律用快照价重算，决策价单独在抬头写明。
    jp = (jr or {}).get("price")
    if price:
        for k in ("bear", "base", "bull"):
            if px.get(k) is not None:
                px[f"{k}_chg"] = (px[k] / price - 1) * 100
    drift = ((price / jp - 1) * 100) if (jp and price) else None
    ev = (jr or {}).get("ev")
    act = (jr or {}).get("action", "")
    act_cn, act_cls = ACTION_CN.get(act, (act or "—", "t-flat"))
    need = ((cost / price - 1) * 100) if (cost and price) else None

    P = []
    A = P.append

    # ── 封面 ────────────────────────────────────────────────────────────
    A('<div class="wrap"><div class="cover">')
    A(f'<h1>{E(name)}（{E(code)}）投资分析报告</h1>')
    A(f'<div class="meta">快照 {E(snap.get("date", ""))}　·　赛道 {E(sector)}'
      f'　·　数据取自 {E(snap.get("ts", ""))}'
      f'　·　结论来自 journal #{(jr or {}).get("id", "—")}'
      f'（{E((jr or {}).get("logged_at", "")[:16])}）'
      f'　·　<b>本报告是数据分析，不是投资建议</b></div>')
    if drift is not None and abs(drift) >= 0.05:
        A(f'<div class="meta" style="margin-top:6px">决策当日价 {jp:,.2f}，'
          f'快照当日价 {price:,.2f}（{drift:+.1f}%）。'
          f'<b>下文所有涨跌幅一律按快照价 {price:,.2f} 算</b>，'
          f'不是按决策价 —— 一份报告里只能有一个分母。</div>')
    A('<div class="kpis">')
    A(f'<div class="kpi"><div class="kpi__n ink">{num(price)}</div>'
      f'<div class="kpi__l">现价（元）</div></div>')
    A(f'<div class="kpi"><div class="kpi__n ink">{num(cost, 3)}</div>'
      f'<div class="kpi__l">你的成本</div></div>')
    A(f'<div class="kpi"><div class="kpi__n {cls_of(pnl)}">{num(pnl, 1, "%", True)}'
      f'</div><div class="kpi__l">浮盈亏</div></div>')
    lbl = "回本需涨" if (need or 0) > 0 else "已高于成本"
    A(f'<div class="kpi"><div class="kpi__n {cls_of(need, good_is_up=False)}">'
      f'{num(need, 1, "%", True)}</div><div class="kpi__l">{lbl}</div></div>')
    A('</div></div>')

    # ── 结论 ────────────────────────────────────────────────────────────
    if jr:
        headline = _headline(act, act_cn, px, price, jr)
        A(f'<div class="verdict"><h2>结论：{E(headline)}</h2>')
        pr = "／".join(f"{n} {probs.get(k, 0) * 100:.0f}%"
                       for k, n in (("bear", "熊"), ("base", "中"), ("bull", "牛"))
                       if probs.get(k) is not None)
        if ev is not None:
            A(f'<p>概率加权期望值 <b>EV = {ev:+.1f}%</b>'
              + (f'（{pr}）' if pr else "") + '。</p>')
        if jr.get("thesis"):
            A(f'<p>{ET(jr["thesis"])}</p>')
        A('</div>')
    else:
        A('<div class="note note--warn"><b>这份报告没有结论段。</b>'
          f'{E(code)} 在 journal 里没有记录 —— 本工具不自己发明判断，'
          '结论必须来自当时真写下来、将来要被复核的那条记录。'
          '先跑 <code>journal log</code> 再重出。</div>')

    # ── 价格坐标图 ──────────────────────────────────────────────────────
    marks = []
    if px.get("bear") is not None:
        marks.append({"px": px["bear"], "txt": f'{px["bear"]:,.0f}', "label": "熊市情景",
                      "sub": f'{px.get("bear_chg", 0):+.0f}%', "color": C_ACCD,
                      "weight": 1.4, "dash": True})
    if price:
        marks.append({"px": price, "txt": f"{price:,.2f}", "label": "现价",
                      "color": C_INK, "weight": 2.2})
    if px.get("base") is not None:
        marks.append({"px": px["base"], "txt": f'{px["base"]:,.0f}', "label": "中性目标",
                      "sub": f'{px.get("base_chg", 0):+.0f}%', "color": C_MUT,
                      "weight": 1.4, "dash": True})
    if cost:
        marks.append({"px": cost, "txt": f"{cost:,.2f}", "label": "你的成本",
                      "color": C_ACC, "weight": 2.2})
    for n in (20, 60, 120):
        mv = g(f, "technical", f"ma{n}")
        if mv:
            marks.append({"px": mv, "txt": f"{mv:,.0f}", "label": f"MA{n}",
                          "color": C_GREY, "weight": 1})
    if px.get("bull") is not None:
        marks.append({"px": px["bull"], "txt": f'{px["bull"]:,.0f}', "label": "牛市情景",
                      "sub": f'{px.get("bull_chg", 0):+.0f}%', "color": C_GOODD,
                      "weight": 1.4, "dash": True})
    hi = g(f, "technical", "high")
    if hi:
        marks.append({"px": hi, "txt": f"{hi:,.0f}", "label": "阶段高点",
                      "color": C_GREY, "weight": 1, "dash": True})

    cap = "把成本、现价、均线、三情景目标放到同一根价格轴上。"
    dd = g(f, "technical", "drawdown_pct")
    if dd is not None:
        cap += f"距阶段高点 {dd:+.1f}%。"
    if cost and price and cost > price:
        cap += f"成本在现价上方 {(cost / price - 1) * 100:.1f}%，回本要穿过中间这些均线。"
    A(fig(svg_price_axis(marks), cap))

    # ── 1 三档操作 ──────────────────────────────────────────────────────
    A('<h2><span class="num">1</span>三档操作</h2>')
    A("<table><tr><th>档位</th><th>价位</th><th>怎么做 · 为什么</th></tr>")
    A(f'<tr><td><span class="tag {act_cls}">{E(act_cn)}</span></td>'
      f'<td class="n">{num(price)} 附近</td><td>{_now_what(act, jr, f, price)}</td></tr>')
    if px.get("bear") is not None:
        anchor = px["bear"]
        A(f'<tr><td><span class="tag t-buy">补仓区</span></td>'
          f'<td class="n">&lt; {anchor:,.0f}</td>'
          f'<td>熊市情景价位 —— <b>那是「我错了」的价</b>，'
          f'在它以下买才是为已经打过折的假设付钱。'
          f'分 3 批，每批不超过原仓位 1/3。'
          f'{"⚠️ 注意这里离现价还有 %.0f%%，短期不会到。" % abs(px.get("bear_chg", 0)) if abs(px.get("bear_chg", 0)) > 25 else ""}'
          f'</td></tr>')
    if jr and jr.get("falsify"):
        A(f'<tr><td><span class="tag t-sell">减 / 清仓</span></td>'
          f'<td class="n">看判据不看价</td>'
          f'<td><b>可证伪判据（复核日 {E(jr.get("check_date", "—"))}）</b><br>'
          f'{ET(jr["falsify"])}</td></tr>')
    A("</table>")

    A(fig(svg_scenarios(price, px, probs, ev, tol=tol),
          "左侧是下行、右侧是上行，黑线是现价。"
          "<b>你能不能拿得住取决于熊市那根有多长，不取决于 EV 正不正。</b>"))

    # ── 2/3 证据 ────────────────────────────────────────────────────────
    pro, con, neu = build_evidence(f, inv)

    # ⚠ 证据的正反是**相对这只票**说的，不是相对结论说的。
    #   把「看多证据」标成「支持这个判断」，在结论是减仓时就完全读反了
    #   —— 2026-08-28 渲染 002353 时当场看见:结论「减仓」，
    #   下面却写着「支持这个判断的 2 条证据:海外同业正向背离」。
    #   改法:标题只说正反，并且**让驱动结论的那一组排在前面**。
    bearish = act in ("trim", "sell")
    first = ("反面", con, "3") if bearish else ("正面", pro, "2")
    second = ("正面", pro, "2") if bearish else ("反面", con, "3")
    lead = ("这几条是减仓的理由" if bearish else "这几条支持继续持有")
    for idx, (nm, rows_, _n) in enumerate(((first[0], first[1], first[2]),
                                           (second[0], second[1], second[2]))):
        num_ = 2 + idx
        tail = f"　<span class=\"ev\">{lead}</span>" if idx == 0 else ""
        A(f'<h2><span class="num">{num_}</span>{nm}证据（{len(rows_)} 条）{tail}</h2>')
        if rows_:
            A(_ev_table(rows_))
        elif nm == "正面":
            A('<div class="note note--warn">一条正面证据都没有。'
              '这不是取数失败，是<b>确实没有</b> —— '
              '拿不出证据支持的持有是惯性，不是判断。</div>')
        else:
            A('<div class="note note--good">当前数据里没有明确的反面项。'
              '但这只说明<b>已取到的指标</b>没报警，见第 6 节还缺什么。</div>')

    gap = g(f, "peers", "gap_pp")
    if gap is not None:
        A(fig(svg_bars("海外同业对照（近 1 月）", [
            ("海外同业均值", g(f, "peers", "peer_1m_avg"), C_MUT, ""),
            (f"{name}", g(f, "peers", "self_1m"),
             C_GOODD if gap >= 15 else (C_ACCD if gap <= -15 else C_MUT),
             f"背离 {gap:+.1f}pp"),
        ]), "需求端在海外时，只看 A 股会漏掉背离。"
            "背离 &gt;15pp 通常说明跌的是情绪不是需求。"))

    if neu:
        A(f'<h2><span class="num">4</span>中性项与口径提醒</h2>')
        A(_ev_table(neu))

    # ── 5 基本面与估值 ──────────────────────────────────────────────────
    A('<h2><span class="num">5</span>基本面与估值（滞后指标，只用来验证）</h2>')
    A("<table><tr><th>指标</th><th>值</th><th>说明</th></tr>")
    for label, val, note in [
        ("营收", num(g(f, "fundamental", "rev"), 2, " 亿"),
         f'同比 {num(g(f, "fundamental", "rev_yoy"), 1, "%", True)}'
         f'　环比 {num(g(f, "fundamental", "rev_qoq"), 1, "%", True)}'),
        ("净利", num(g(f, "fundamental", "net"), 2, " 亿"),
         f'同比 {num(g(f, "fundamental", "net_yoy"), 1, "%", True)}'
         f'　环比 {num(g(f, "fundamental", "net_qoq"), 1, "%", True)}'),
        ("毛利率", num(g(f, "fundamental", "gm"), 1, "%"), "毛利率比增速更慢掉"),
        ("ROE", num(g(f, "fundamental", "roe"), 2, "%"), ""),
        ("PE(TTM)", num(g(f, "valuation", "pe_ttm")),
         f'历史分位 {num(g(f, "valuation", "pe_pctl"), 0, "%")}'),
        ("动态 PE", num(g(f, "valuation", "dyn_pe")), "按当期年化"),
        ("PB 分位", num(g(f, "valuation", "pb_pctl"), 0, "%"), ""),
        ("总市值", num(g(f, "valuation", "cap"), 0, " 亿"), ""),
        ("报告期", g(f, "fundamental", "period", default="—"), ""),
    ]:
        A(f'<tr><td>{E(label)}</td><td class="n">{val}</td><td>{note}</td></tr>')
    A("</table>")

    # ── 6 数据完备性 ────────────────────────────────────────────────────
    A('<h2><span class="num">6</span>数据完备性：缺什么，影响多大</h2>')
    A(_completeness(f, inv, sector))

    # ── 7 数据来源 ──────────────────────────────────────────────────────
    A('<h2><span class="num">7</span>数据来源（逐条可复核）</h2>')
    A("<table><tr><th>本报告里的</th><th>来自</th><th>怎么自己复核</th></tr>")
    for what, src, how in [
        ("现价 · 均线 · 阶段高点", "新浪 hq.sinajs.cn（日线）",
         f"<code>astock {code} --daily</code>"),
        ("营收 / 净利 / 毛利率 / ROE", "东财 datacenter-web 全市场业绩表",
         f"<code>efdata perf</code>"),
        ("PE/PB 历史分位", "baostock", f"<code>hcheck {code}:&lt;成本&gt;</code>"),
        ("股东户数 · 融资余额 · 北向占比", "东财 datacenter-web",
         "<code>efdata holdernum</code> / <code>efdata northbound</code>"),
        ("海外同业涨跌", "akshare stock_us_daily（新浪美股源）",
         f"<code>preport {code}:&lt;成本&gt;</code> 第 7 层"),
        ("基准率", "东财全市场 12 期业绩，本地算",
         f"<code>baserate calibrate {code}</code>"),
        ("三情景价位 · EV · 可证伪判据 · thesis",
         f"<b>journal #{(jr or {}).get('id', '—')}</b>（不是数据，是当时的判断）",
         "<code>journal review</code>"),
        ("本报告的全部数字", f"<code>{E(str(snap_path))}</code>",
         "快照是 preport 跑出来的，不改可重出"),
    ]:
        A(f"<tr><td>{what}</td><td>{src}</td><td>{how}</td></tr>")
    A("</table>")

    A('<footer>')
    A(f'本报告由 <code>deep_report.py</code> 从快照 <code>{E(snap.get("date", ""))}</code>'
      f' 与 journal #{(jr or {}).get("id", "—")} 渲染，'
      f'生成于 {dt.datetime.now():%Y-%m-%d %H:%M}。<br>'
      '<b>所有判断来自 journal 里当时写下的记录，本工具不发明判断。</b>'
      '每一条结论都挂着可证伪判据 —— 到复核日拿实际数字去核，'
      '错了就把教训蒸馏成原则（<code>journal close --principle</code>）。<br>'
      '基准率是历史统计不是预测；一致预期是卖方观点不是事实；'
      '任何目标价都建立在一串会被证否的假设上。<b>决策与后果都是你自己的。</b>')
    A('</footer></div>')
    return "\n".join(P)


def _headline(act, act_cn, px, price, jr):
    """结论那一句。**只用已有的数拼**，不添加新判断。"""
    if act in ("trim", "sell"):
        bc = px.get("bear_chg")
        if bc is not None and bc < -30:
            return f"{act_cn}；下行 {bc:.0f}% 超出你的 30% 容忍线"
        ev = jr.get("ev")
        if ev is not None and ev < 0:
            return f"{act_cn}；期望值为负（EV {ev:+.1f}%）"
        return f"{act_cn}"
    if act in ("add", "buy"):
        return f"{act_cn}"
    b = px.get("bear")
    if b is not None and price:
        return f"{act_cn}；跌到 {b:,.0f} 以下才是加仓区"
    return f"{act_cn}"


def _now_what(act, jr, f, price):
    if act == "hold":
        return ("不动。判据一条没触发，而<b>赔率不够加仓</b> —— "
                "不加不是看空，是「值得持有」和「值得加仓」是两个门槛。")
    if act == "trim":
        return ("<b>减仓，不是持有。</b>减仓的理由可以与 EV 无关 —— "
                "EV 为正但下行超出你能承受的幅度时，正确动作仍是减。")
    if act == "sell":
        return "清仓。判据已触发。"
    if act in ("add", "buy"):
        return "按计划分批买入。"
    return "见 journal 记录。"


def _ev_table(rows):
    out = ["<table><tr><th style='width:20%'>证据</th>"
           "<th style='width:26%'>数</th><th>判读</th></tr>"]
    for r in rows:
        rule = (f'<br><span class="ev">判读规则：{E(r["rule"])}</span>'
                if r.get("rule") else "")
        out.append(f'<tr><td><b>{E(r["t"])}</b></td><td>{E(r["v"])}</td>'
                   f'<td>{r["why"]}{rule}</td></tr>')
    out.append("</table>")
    return "\n".join(out)


def _completeness(f, inv, sector):
    """诚实说缺什么。**静默跳过会让报告显得比实际完整** —— 这是刻意反过来做。"""
    inv = set(inv or [])
    # (名字, 取到了吗, 说明, 被哪个 invalid_key 判为不适用)
    checks = [
        ("海外同业对照", g(f, "peers", "gap_pp") is not None,
         "需求端在海外时这是主要的领先信号，缺了只能看后视镜", "peers"),
        ("基准率（外部视角）", g(f, "baserate", "hold_50") is not None,
         "缺了就没有参照系，容易系统性高估「好状态还能持续多久」", "baserate_growth"),
        ("股东户数", g(f, "chips", "holders") is not None, "影响反弹阻力", None),
        ("北向持股", g(f, "chips", "nb_pct_total") is not None, "参与度，不含方向", None),
        ("融资余额", g(f, "chips", "margin_bal") is not None, "下跌自我强化的燃料", None),
        ("解禁", g(f, "risk", "unlock_count") is not None, "日期确定的抛压", None),
        ("估值历史分位", g(f, "valuation", "pe_pctl") is not None,
         "贵不贵的坐标", "pe_percentile"),
    ]
    out = ['<table><tr><th style="width:22%">数据层</th><th style="width:13%">状态</th>'
           '<th>缺了影响什么</th></tr>']
    真缺 = []
    for nm, ok, why, key in checks:
        if key and key in inv:
            # ⚠ 「不适用」不是「缺」。混成同一个符号，读者会去补一个
            #   本来就不该用的指标 —— 补回来只会让结论更错。
            st, note = "⊘ 不适用", "赛道判定：取到也不能用，见上方说明"
        elif ok:
            st, note = "✅ 有", why
        else:
            st, note = "❌ 缺", why
            真缺.append(nm)
        out.append(f"<tr><td>{E(nm)}</td><td>{st}</td><td>{E(note)}</td></tr>")
    out.append("</table>")
    if inv:
        cn = {"peg": "PEG", "baserate_growth": "增速基准率",
              "pe_percentile": "PE 历史分位", "peers": "海外同业对照"}
        names = "、".join(cn.get(k, k) for k in inv)
        out.append(f'<div class="note note--warn"><b>赛道「{E(sector)}」上有 '
                   f'{len(inv)} 个指标不适用：{E(names)}。</b>'
                   '它们不是取不到，是<b>取到了也不能用</b> —— '
                   '套错框架不会报错，会输出一个看着有意义的数。'
                   '本报告已把它们排除在正反证据之外。</div>')
    if 真缺:
        out.append(f'<div class="note note--warn">真正<b>缺</b>了 {len(真缺)} 层：'
                   f'{E("、".join(真缺))}。'
                   '<b>缺的层数直接决定这份结论的强度</b> —— '
                   '不是「大致能看」，是那几个维度上你没有证据。'
                   '（标 ⊘ 的不算缺，那是不该用。）</div>')
    return "\n".join(out)


def build_html(snap, jr, snap_path, tol=None):
    title = f'{snap.get("name", "")}（{snap.get("code", "")}）投资分析报告'
    return ("<!doctype html>\n<html lang=\"zh-CN\">\n<meta charset=\"utf-8\">\n"
            f"<title>{E(title)}</title>\n<style>{CSS}</style>\n"
            + render(snap, jr, snap_path, tol) + "\n</html>\n")


def main():
    ap = argparse.ArgumentParser(
        description="把快照 + journal 渲染成给人看的深度分析报告（不联网）")
    ap.add_argument("code", help="股票代码，如 300308")
    ap.add_argument("--date", help="用哪一天的快照（默认最新）")
    ap.add_argument("--out", help="输出 HTML 路径（默认打到 stdout）")
    ap.add_argument("--tolerance", type=float, metavar="PCT",
                    default=(float(os.environ["ASTOCK_DD_TOLERANCE"])
                             if os.environ.get("ASTOCK_DD_TOLERANCE") else None),
                    help="你能承受的回撤上限（%%，如 30）。给了就在三情景图上画一条线，"
                         "并标出熊市情景越线多少。也可用环境变量 ASTOCK_DD_TOLERANCE。"
                         "**不给就不画** —— 这个数因人而异，没有合理默认值")
    a = ap.parse_args()

    snap, path = load_snapshot(a.code, a.date)
    if snap is None:
        raise SystemExit(
            f"没有 {a.code} 的快照。深度报告是**重排已有证据**，不自己取数 ——\n"
            f"  先跑:preport {a.code}:<你的成本>\n"
            f"  快照会落在 {SNAPDIR / a.code}/")
    jr = load_journal(a.code)
    html = build_html(snap, jr, path.relative_to(LAB) if path else "",
                      tol=a.tolerance)

    if a.out:
        Path(a.out).write_text(html, encoding="utf-8")
        n_fig = html.count("<figure")
        print(f"已生成 {a.out}　({len(html):,} 字节，{n_fig} 张图，"
              f"快照 {snap.get('date')}，journal #{(jr or {}).get('id', '无')})")
        if not jr:
            print("⚠ 这只票没有 journal 记录 —— 报告里没有结论段。"
                  "本工具不发明判断，先 journal log 再重出。", file=sys.stderr)
    else:
        sys.stdout.write(html)


if __name__ == "__main__":
    main()
