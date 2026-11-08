#!/usr/bin/env python3
"""svg —— 看板页面上的图:百格图、价格扇形图、按月 / 按价位的条形、横条、季度区间、校准图。

它是什么:全是内联 SVG 字符串,不引用外部脚本或字体,离线能看、能 AirDrop 到手机。
每张图带 <title>(读屏、悬停时的说明),数字直接写在图上 —— 不靠颜色或悬停才知道是多少。

为什么字号跟着 viewBox 宽度走:手机上图会跟着容器缩小。design-review 的 check_objective 要求
390 宽的屏幕上图里的字不小于 9px;一张 viewBox 640 宽的图在手机上大约缩到 330px(一半),
字号写 12 就只剩 6px。所以宽图字写大(_fs),窄图(横条类统一 360 宽)字写正常大小。

为什么价格图的纵轴按比例画(对数刻度):一年后的分位比三个月的宽得多,直线刻度会把近期挤成一条线,
「远期压过近期」正是设计里列的做砸的样子;按比例画,涨一倍和跌一半在图上一样长。

配色:底 #faf9f5、墨字 #141413;图形用 蓝 #6a9bcc / 绿 #788c5d / 橙 #d97757 / 金 #c9913f,
写字用深一档(#3f6f9e #5b6e45 #a14f2e #8a5f1c),浅色字在米白底上对比度不够。
"""
from __future__ import annotations

import math
from html import escape

INK, MUTED, GRID, BG = "#141413", "#5e5d59", "#e8e6dc", "#faf9f5"
BLUE, GREEN, ORANGE, GOLD, RED = "#6a9bcc", "#788c5d", "#d97757", "#c9913f", "#a14238"
T_BLUE, T_GREEN, T_ORANGE, T_GOLD = "#3f6f9e", "#5b6e45", "#a14f2e", "#8a5f1c"
# 各层在价格图上的标记颜色(图形色, 写字色, 中文名)
LAYER_STYLE = {"baseline": (BLUE, T_BLUE, "统计底线"), "system": (T_BLUE, T_BLUE, "系统算法"),
               "claude": (ORANGE, T_ORANGE, "Claude"), "codex": (GREEN, T_GREEN, "codex")}
LINE_STYLE = {"cost": (GOLD, T_GOLD, "成本"), "sell_line": (RED, RED, "卖出线")}
HORIZON_TEXT = {"63": "+3 月", "126": "+6 月", "250": "+1 年"}
TONES = {"strong": (GREEN, "#e3e0d5"), "mid": ("#93a57b", "#e8e6dc"), "faint": ("#b3c09f", "#efede6")}
ROW_W = 360          # 横条类图的 viewBox 宽


def _e(s) -> str:
    return escape(str(s), quote=True)


def _fs(width: float) -> float:
    """宽 width 的图用多大的字:手机上大约缩到 320px(一半)时还有 10px 左右。"""
    return round(max(14.0, width / 31), 1)


def fmt_price(x: float) -> str:
    """价格怎么写:100 以上不带小数,10~100 一位,10 以下两位。"""
    x = float(x)
    if abs(x) >= 100:
        return f"{x:.0f}"
    s = f"{x:.1f}" if abs(x) >= 10 else f"{x:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _text_w(s: str, fs: float) -> float:
    """估一段字的宽度:汉字一个字号宽,其余大约半个多。"""
    return sum(fs if ord(ch) > 0x2E80 else fs * 0.58 for ch in str(s))


def _svg(w: float, h: float, title: str, body: list[str], cls: str) -> str:
    t = _e(title)
    return (f'<svg class="chart {cls}" viewBox="0 0 {w:g} {h:g}" role="img" aria-label="{t}" '
            f'xmlns="http://www.w3.org/2000/svg"><title>{t}</title>' + "".join(body) + "</svg>")


def _txt(x, y, s, fs, fill=INK, anchor="start", weight=None, cls=None, halo=False) -> str:
    """halo:字周围描一圈底色,压在扇形、线上面时也看得清。"""
    extra = (f' font-weight="{weight}"' if weight else "") + (f' class="{cls}"' if cls else "")
    if halo:
        extra += f' stroke="{BG}" stroke-width="{fs * 0.28:.1f}" stroke-linejoin="round" paint-order="stroke"'
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs:g}" fill="{fill}" text-anchor="{anchor}"{extra}>'
            f"{_e(s)}</text>")


# ── 百格图 ──────────────────────────────────────────────────────────────────
def icon_array(n_up: int, label: str, tone: str = "strong") -> str:
    """10×10 方格,按行从左上开始先填「涨」的 n_up 格,其余是「不涨」(含持平)。
    tone:strong / mid / faint,一年那张用 faint(设计:3 个月最显眼,一年最淡)。"""
    if isinstance(n_up, bool) or not isinstance(n_up, int) or not 0 <= n_up <= 100:
        raise ValueError(f"n_up 要是 0~100 的整数,得到 {n_up!r}")
    up, no = TONES.get(tone, TONES["strong"])
    cell, gap = 18, 2
    body = []
    for i in range(100):
        r, c = divmod(i, 10)
        kind, fill = ("c-up", up) if i < n_up else ("c-no", no)
        body.append(f'<rect class="{kind}" x="{c * (cell + gap)}" y="{r * (cell + gap)}" width="{cell}" '
                    f'height="{cell}" rx="3" fill="{fill}"/>')
    body.append(_txt(0, 226, f"涨 {n_up}", 22, T_GREEN, weight="700"))
    body.append(_txt(198, 226, f"不涨 {100 - n_up}", 21, MUTED, anchor="end"))
    title = f"{label}:100 种走法里涨 {n_up} 种,不涨(含持平){100 - n_up} 种"
    return _svg(198, 236, title, body, "icons")


# ── 价格扇形图 ──────────────────────────────────────────────────────────────
def _nice(x: float) -> float:
    """取两位有效数字。"""
    if x <= 0:
        return x
    k = 10 ** (math.floor(math.log10(x)) - 1)
    return round(x / k) * k


def _log_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    raw = [_nice(lo * (hi / lo) ** (i / (n - 1))) for i in range(n)]
    out = []
    for v in raw:
        if lo <= v <= hi and (not out or v / out[-1] > 1.08):
            out.append(v)
    return out


def _qvals(q: dict) -> list[float]:
    return [float(q[k]) for k in ("10", "25", "50", "75", "90") if isinstance(q.get(k), (int, float)) and q[k] > 0]


def fan_chart(history: list[tuple[str, float]], bands: dict[str, dict], markers: dict[str, dict],
              lines: dict[str, float], width: int = 640, height: int = 320) -> str:
    """价格图。横轴:过去(history 的根数)→ 现在 → 各期限(交易日);纵轴按比例画。
    history  已发生的收盘 [(日期, 价)],画成实线
    bands    统计底线 {期限: {"10","25","50","75","90"}},画成往右张开的扇形(10~90 浅,25~75 深)
    markers  {层: {期限: 五个分位}},只在期限那一点打标记(竖线 10~90、粗段 25~75、圆点 50),不连线;
             第一层是主角,它的中间价写在图上。3 个月最显眼,一年最淡
    lines    {"cost": 成本价, "sell_line": 卖出线},金色 / 红色虚线
    价格要和 history 同一种价(起点那天的不复权价)。"""
    fs = _fs(width)
    hs = sorted((h for h in bands if _qvals(bands[h])), key=int)
    x_end = max([int(h) for h in hs] + [int(h) for m in markers.values() for h in m] + [1])
    hist = [(d, float(p)) for d, p in history if p and p > 0]
    p0 = hist[-1][1] if hist else (float(bands[hs[0]]["50"]) if hs else 1.0)
    x_start = -(len(hist) - 1) if len(hist) > 1 else -20
    vals = [p for _, p in hist] + [p0] + [v for h in hs for v in _qvals(bands[h])]
    vals += [v for m in markers.values() for q in m.values() for v in _qvals(q)]
    vals += [float(v) for v in lines.values() if v and v > 0]
    lo, hi = min(vals) * 0.94, max(vals) * 1.06
    # 右边留出最后一个期限那组标记的一半宽(每层错开 0.85 个字号)
    right = fs * (0.85 * max(1, len(markers)) / 2 + 0.6)
    left, top, bottom = fs * 3.0, fs * 0.9, fs * 2.0
    pw, ph = width - left - right, height - top - bottom

    def sx(t):
        return left + (t - x_start) / (x_end - x_start) * pw

    def sy(p):
        return top + (math.log(hi) - math.log(p)) / (math.log(hi) - math.log(lo)) * ph

    body = []
    for v in _log_ticks(lo, hi):
        y = sy(v)
        body.append(f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{width - right:.1f}" y2="{y:.1f}" stroke="{GRID}"/>')
        body.append(_txt(left - fs * 0.3, y + fs * 0.35, fmt_price(v), fs, MUTED, "end"))
    if hs:
        for cls, a, b, op in (("fan80", "10", "90", 0.2), ("fan50", "25", "75", 0.42)):
            up = [(0, p0)] + [(int(h), float(bands[h][b])) for h in hs]
            dn = [(int(h), float(bands[h][a])) for h in reversed(hs)] + [(0, p0)]
            pts = " ".join(f"{sx(t):.1f},{sy(p):.1f}" for t, p in up + dn)
            body.append(f'<polygon class="{cls}" points="{pts}" fill="{BLUE}" fill-opacity="{op}" stroke="none"/>')
    x0 = sx(0)
    body.append(f'<line x1="{x0:.1f}" y1="{top:.1f}" x2="{x0:.1f}" y2="{top + ph:.1f}" stroke="{MUTED}" '
                f'stroke-dasharray="3 4"/>')
    for key, v in lines.items():
        if not v or v <= 0:
            continue
        col, tcol, name = LINE_STYLE.get(key, (MUTED, MUTED, key))
        y = sy(float(v))
        body.append(f'<line class="ln" x1="{left:.1f}" y1="{y:.1f}" x2="{width - right:.1f}" y2="{y:.1f}" '
                    f'stroke="{col}" stroke-width="2" stroke-dasharray="7 5"/>')
        body.append(_txt(left + fs * 0.3, y - fs * 0.3, f"{name} {fmt_price(v)}", fs, tcol, halo=True))
    if len(hist) > 1:
        pts = " ".join(f"{sx(i - len(hist) + 1):.1f},{sy(p):.1f}" for i, (_, p) in enumerate(hist))
        body.append(f'<polyline class="hist" points="{pts}" fill="none" stroke="{INK}" stroke-width="2"/>')
    body.append(f'<circle cx="{x0:.1f}" cy="{sy(p0):.1f}" r="{fs * 0.25:.1f}" fill="{INK}"/>')
    body += _markers(markers, sx, sy, fs, x_end)
    ticks = [(x_start, f"{max(1, round(-x_start / 21))} 个月前", "start"), (0, "现在", "middle")]
    ticks += [(int(h), HORIZON_TEXT.get(str(h), f"+{h} 天"), "end" if int(h) == x_end else "middle")
              for h in sorted({h for h in hs} | {h for m in markers.values() for h in m}, key=int)]
    for t, s, anc in ticks:
        body.append(_txt(sx(t), height - fs * 0.6, s, fs, MUTED, anc))
    title = ("价格图:实线是已发生的收盘;扇形是模型推算,只按波动幅度,不带方向(浅色 10~90 分位、深色 25~75 分位);"
             "各层的分位只在三个时间点打标记,不连线;虚线是成本价和卖出线")
    return _svg(width, height, title, body, "fan")


def _markers(markers: dict, sx, sy, fs: float, x_end: int) -> list[str]:
    names = list(markers)
    out = []
    for k, name in enumerate(names):
        col, tcol, _ = LAYER_STYLE.get(name, (MUTED, MUTED, name))
        dx = (k - (len(names) - 1) / 2) * fs * 0.85
        for h, q in sorted(markers[name].items(), key=lambda kv: int(kv[0])):
            if len(_qvals(q)) < 5:
                continue
            op = {"63": 1.0, "126": 0.72, "250": 0.48}.get(str(h), 0.8)
            x = sx(int(h)) + dx
            w = fs * 0.5
            out.append(f'<g class="mk {_e(name)}" opacity="{op}">'
                       f'<line x1="{x:.1f}" y1="{sy(q["10"]):.1f}" x2="{x:.1f}" y2="{sy(q["90"]):.1f}" '
                       f'stroke="{col}" stroke-width="2"/>'
                       f'<rect x="{x - w / 2:.1f}" y="{sy(q["75"]):.1f}" width="{w:.1f}" '
                       f'height="{max(1.0, sy(q["25"]) - sy(q["75"])):.1f}" fill="{col}"/>'
                       f'<circle cx="{x:.1f}" cy="{sy(q["50"]):.1f}" r="{fs * 0.24:.1f}" fill="{BG}" '
                       f'stroke="{INK}" stroke-width="1.5"/></g>')
            if k == 0:      # 主角的中间价写在这组标记的外侧:最后一个期限写在左边,其余写在右边
                last = int(h) == x_end
                edge = fs * (0.85 * len(names) / 2 + 0.35)
                tx = sx(int(h)) - edge if last else sx(int(h)) + edge
                out.append(_txt(tx, sy(q["50"]) + fs * 0.32, fmt_price(q["50"]), fs, tcol,
                                "end" if last else "start", weight="600", halo=True))
    return out


# ── 横条类 ──────────────────────────────────────────────────────────────────
def _rows(rows: list[tuple[str, float, str]], title: str, cls: str, colors=None, signed: bool = False) -> str:
    """一行一条:左边名字、中间横条、右边数。rows = [(名字, 数值, 写在右边的字)]。"""
    fs, rh = 14.0, 26.0
    lw = min(ROW_W * 0.45, max([_text_w(r[0], fs) for r in rows] + [fs * 2]) + 8)
    vw = max([_text_w(r[2], fs) for r in rows] + [fs * 2]) + 6
    bx, bw = lw, ROW_W - lw - vw
    vals = [float(r[1]) for r in rows]
    lo, hi = (min(0.0, min(vals, default=0)), max(0.0, max(vals, default=0))) if signed else (0.0, max(vals + [1e-9]))
    span = (hi - lo) or 1.0
    zero = bx + (0 - lo) / span * bw
    body = []
    for i, (name, v, label) in enumerate(rows):
        y = i * rh
        col = (colors[i] if colors else GREEN) if v >= 0 or not signed else ORANGE
        x1, x2 = sorted((zero, bx + (float(v) - lo) / span * bw))
        body.append(_txt(lw - 6, y + rh * 0.66, name, fs, INK, "end"))
        body.append(f'<rect x="{x1:.1f}" y="{y + 5:.1f}" width="{max(1.5, x2 - x1):.1f}" height="{rh - 10:.1f}" '
                    f'rx="2" fill="{col}"/>')
        body.append(_txt(ROW_W - 2, y + rh * 0.66, label, fs, INK, "end"))
    if signed and lo < 0:
        body.append(f'<line x1="{zero:.1f}" y1="0" x2="{zero:.1f}" y2="{len(rows) * rh:.1f}" stroke="{MUTED}"/>')
    return _svg(ROW_W, max(rh, len(rows) * rh), title, body, cls)


def _month_name(key: str, prev_year: str | None) -> str:
    if key == "later":
        return "更晚或没出现"
    y, m = key.split("-")
    return f"{y} 年 {int(m)} 月" if y != prev_year else f"{int(m)} 月"


def month_bars(probs: dict[str, int]) -> str:
    """按月分格的条形:{"YYYY-MM": 种数, …, "later": 种数}。年份变了的那行写年份。"""
    rows, prev = [], None
    for k, v in probs.items():
        rows.append((_month_name(k, prev), float(v), f"{v} 种"))
        prev = k[:4] if k != "later" else prev
    return _rows(rows, "按月分:100 种走法里各月几种", "months")


def bin_bars(bins: list[list]) -> str:
    """价位分段的条形:[[下限价, 上限价, 种数], …](下限 0 的那段写「X 以下」)。价高的段在上面。"""
    rows = []
    for lo, hi, p in sorted(bins, key=lambda b: -float(b[1])):
        name = f"{fmt_price(hi)} 以下" if float(lo) <= 0 else f"{fmt_price(lo)}~{fmt_price(hi)}"
        rows.append((name, float(p), f"{p} 种"))
    return _rows(rows, "最低价落在哪一段:100 种走法里各段几种", "bins")


def hbars(items: list[tuple[str, float]], unit: str = " 种", signed: bool | None = None) -> str:
    """普通横条:[(名字, 数)],数后面跟 unit。signed(默认:有负数就是)时带正负号、负数往左画。"""
    signed = any(float(v) < 0 for _, v in items) if signed is None else signed
    def label(v):
        return f"{float(v):+.1f}{unit}" if signed else f"{float(v):g}{unit}"
    return _rows([(n, float(v), label(v)) for n, v in items], "横条:" + "、".join(n for n, _ in items[:6]),
                 "hbars", signed=signed)


# ── 季度区间 ────────────────────────────────────────────────────────────────
def range_dots(rows: list[tuple[str, float, float, float, float]], unit: str = "亿") -> str:
    """每季一行:空心横条 = 预测区间,空心点 = 中枢,实心点 = 实际值。rows = [(季度, 下限, 上限, 中枢, 实际)]。
    每行下面一行小字写出四个数(预测区间、中枢、实际),不用对着刻度猜。"""
    fs, rh = 14.0, 50.0
    lw, vw = fs * 4.6, fs * 5.6
    vals = [float(v) for r in rows for v in r[1:]]
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    bw = ROW_W - lw - vw

    def sx(v):
        return lw + (float(v) - lo) / (hi - lo) * bw

    body = []
    for i, (name, a, b, m, real) in enumerate(rows):
        y = i * rh + 16
        body.append(_txt(lw - 6, y + fs * 0.35, name, fs, INK, "end"))
        body.append(f'<rect class="band" x="{sx(a):.1f}" y="{y - 7:.1f}" width="{max(2.0, sx(b) - sx(a)):.1f}" '
                    f'height="14" rx="3" fill="none" stroke="{T_BLUE}" stroke-width="1.6"/>')
        body.append(f'<circle class="mid" cx="{sx(m):.1f}" cy="{y:.1f}" r="5" fill="{BG}" stroke="{INK}" '
                    f'stroke-width="1.6"/>')
        body.append(f'<circle class="real" cx="{sx(real):.1f}" cy="{y:.1f}" r="5.5" fill="{T_ORANGE}"/>')
        body.append(_txt(ROW_W - 2, y + fs * 0.35, f"实际 {fmt_price(real)}{unit}", fs, INK, "end"))
        body.append(_txt(lw, y + 26, f"预测 {fmt_price(a)}~{fmt_price(b)} · 中枢 {fmt_price(m)} · "
                                     f"实际 {fmt_price(real)}{unit}", 13, MUTED))
    title = "季度业绩比对:空心横条是预测区间,空心点是中枢,实心点是实际值"
    return _svg(ROW_W, max(rh, len(rows) * rh), title, body, "quarters")


# ── 校准图 ──────────────────────────────────────────────────────────────────
def calib_chart(series: dict[str, list[list]]) -> str:
    """概率说得准不准:横轴是当时说的概率(0~100),纵轴是实际发生的比例;落在对角线上 = 说多少就是多少。
    series = {层: [[说的概率中点, 实际比例 0~1, 题数], …]}。每个点旁边写「实际比例 · 题数」。"""
    size, fs, m = 360.0, 14.0, 44.0
    pw = size - m - 12

    def sx(p):
        return m + float(p) / 100 * pw

    def sy(f):
        return 12 + (1 - float(f)) * pw

    body = [f'<rect x="{m}" y="12" width="{pw}" height="{pw}" fill="none" stroke="{GRID}"/>',
            f'<line class="diag" x1="{sx(0):.1f}" y1="{sy(0):.1f}" x2="{sx(100):.1f}" y2="{sy(1):.1f}" '
            f'stroke="{MUTED}" stroke-dasharray="4 4"/>']
    for v in (0, 50, 100):
        body.append(_txt(sx(v), size - 14, f"{v}", fs, MUTED, "middle"))
        body.append(_txt(m - 6, sy(v / 100) + 5, f"{v}%", fs, MUTED, "end"))
    for name, pts in series.items():
        col, tcol, label = LAYER_STYLE.get(name, (MUTED, MUTED, name))
        for p, f, n in pts:
            body.append(f'<circle cx="{sx(p):.1f}" cy="{sy(f):.1f}" r="{3 + min(6, math.sqrt(n)):.1f}" '
                        f'fill="{col}" fill-opacity="0.75"/>')
            right = float(p) < 60          # 点靠左写在右边,靠右写在左边,别出界
            body.append(_txt(sx(p) + (12 if right else -12), sy(f) + 4, f"{float(f) * 100:.0f}% · {n} 题", 12.5,
                             tcol, "start" if right else "end", halo=True))
    return _svg(size, size, "校准图:横轴是当时说的概率,纵轴是实际发生的比例,虚线是说多少就是多少", body, "calibsvg")
