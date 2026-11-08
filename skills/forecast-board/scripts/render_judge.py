#!/usr/bin/env python3
"""render_judge —— 看板第一屏:现在、怎么看、价格可能晃多大、对照。数都在 board_judge.py 里算好。

它是什么:第一屏要回答三件事 —— 手上的该不该卖、要不要买回、这个行业还值不值得待。
「现在」放在股票切换之前(整个账户一份);每只股票那块里依次是:怎么看 → 价格可能晃多大 → 对照(折叠),
原来那一整块(结论条、各层预测、见底、买点、依据)原样放进下面的「细节」折叠里,内容不删。
为什么这样排:用户看页面是要做决定;各层的概率表只说「100 种走法里涨几种」,回答不了「该怎么做、凭什么」。
  - 「现在」只露三项:持仓、这个月还款的现金够不够、后续还款缺口;借款来历、逐笔金额、计算过程收进
    「展开还款明细」。漏录借款这种「账目没补齐」的提醒照样露在外面(它会让后面的数全偏)。
    卖不卖只在每只股票「手上的」那一行说,「现在」只说还款要不要靠卖股票筹钱,两处不打架。
  - 「怎么看」是 AI 按当时事实写的判断,标「尚未验证」(这一层验证过才不标)。三件事各一行:
    问题 · 结论 + 一句适用条件,完整理由展开再看。几种情况的百分比旁标「主观」,低点写「估计可能在」,
    估价算式收进「价位怎么估的」;改判条件写成「什么情况要重新检查这个判断」,不写成证明口吻。
  - 「价格可能晃多大」只画历史波动参考;图下固定一句「这是到期那天的收盘范围,不是这段时间的最低价」,
    图例和算法收进折叠。
  - 「对照」默认折叠:简单规则和系统算法现在各说什么、它们过去检验得怎样 —— 是让判断至少比它们说得更有道理,
    不是又一个买卖信号。过去检验的次数旁边写明「不代表这次的胜率」。
  - 图在服务器端画好(不开脚本也看得到),宽(560)、窄(360)两份按屏幕宽度显示一份:SVG 跟着容器缩放,
    宽图在手机上字会缩到六成,所以手机另画一份(check_objective 要求图里的字 ≥ 9px)。
    字写大(窄图 14、宽图 15),日期刻度只留三个(开头、起点、到期),按估出来的字宽分行放,手机上不重叠。
检查:scripts/check_board_judge.py。
"""
from __future__ import annotations

import math
from html import escape

import board_judge
import svg

HZ = (("63", "3 个月"), ("126", "半年"), ("250", "一年"))
Z = {"10": -1.2815516, "25": -0.6744898, "75": 0.6744898, "90": 1.2815516}
SCEN_COLOR = ("#788c5d", "#6a9bcc", "#d97757", "#c9913f")
BAND_NOTE = "这是到期那天的收盘范围,不是这段时间的最低价。"
NARROW = {"W": 360, "H": 300, "L": 44, "R": 122, "T": 24, "B": 52, "fs": 14}
WIDE = {"W": 560, "H": 330, "L": 52, "R": 136, "T": 24, "B": 52, "fs": 15}
BAND_SCRIPT = """(function () {
  document.querySelectorAll('.bandbox').forEach(function (box) {
    var bs = box.querySelectorAll('button[data-h]');
    bs.forEach(function (b) {
      b.addEventListener('click', function () {
        bs.forEach(function (x) { x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
        box.querySelectorAll('figure.bh').forEach(function (f) { f.hidden = f.dataset.h !== b.dataset.h; });
      });
    });
  });
})();"""


def _e(s) -> str:
    return escape("" if s is None else str(s), quote=True)


def _wan(x) -> str:
    return "?" if x is None else f"{x / 1e4:.1f} 万"


def _f0(v: float) -> str:
    return svg.fmt_price(v)


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ── 现在 ────────────────────────────────────────────────────────────────────
def _repay_line(acc: dict) -> str:
    """配了现金留够几个月(cover_months > 1)就写那几个月的合计;老数据没有 cover_* 照旧写这个月。"""
    need, cash = acc.get("month_need"), acc.get("cash")
    who = f"{acc.get('need_month') or '这个月'} "
    n = acc.get("cover_months")
    if isinstance(n, int) and n > 1 and _isnum(acc.get("cover_need")):
        need = acc["cover_need"]
        word = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}.get(n, str(n))
        who = f"{acc.get('cover_from')}~{acc.get('cover_until')} {word}个月"
    if not (_isnum(need) and _isnum(cash)):
        return ""
    head = f"{_e(who)}要还 {_wan(need)},可取现金 {_wan(cash)}:"
    if cash >= need:
        return f"<p>{head}还款现金够,不用靠卖股票筹钱。</p>"
    return f"<p>{head}还差 {_wan(need - cash)},要靠卖股票筹钱(卖不卖、卖多少看每只股票「手上的」那一行)。</p>"


def _later_line(acc: dict) -> str:
    fc, until = acc.get("free_cash"), _e(acc.get("free_cash_until") or "?")
    if not _isnum(fc):
        return ""
    if fc >= 0:
        return f"<p>后续还款:留出到 {until} 要还的钱以后,还能动的现金 {_wan(fc)}。</p>"
    return f'<p>后续还款:到 {until} 为止要还的钱,现金还差 <b>{_wan(-fc)}</b>(看板里记成 {_wan(fc)},负数就是缺口)。</p>'


def now_html(data: dict) -> str:
    """整个账户一份。露三项:持仓、这个月还款够不够、后续还款缺口;漏录借款的提醒也露;其余在「展开还款明细」。"""
    stocks, acc = data.get("stocks") or [], data.get("account") or {}
    if any(s.get("shares") is None for s in stocks):
        hold = "持仓数据读不出来,先更新借款看板"
    else:
        mv = sum(s["shares"] * s["close"] for s in stocks if s.get("close"))
        hold = "持仓:" + "、".join(f"{_e(s['name'])} {s['shares']} 股" for s in stocks) + f"(按收盘约 {_wan(mv)})"
    rem = acc.get("reminders") or []
    gaps = [r for r in rem if "没录" in r]
    rows = [f'<p class="big">{hold}</p>', _repay_line(acc), _later_line(acc)]
    rows += [f'<p class="gap">账目还没补齐:{_e(r)}</p>' for r in gaps]
    detail = [x for x in str(acc.get("note") or "").split(";") if x.strip() and x not in gaps]
    detail += [r for r in rem if r not in gaps and r not in detail]
    if detail:
        rows.append('<details class="acct"><summary>展开还款明细</summary><ul>'
                    + "".join(f"<li>{_e(x)}</li>" for x in detail) + "</ul></details>")
    return f'<div class="jcard now0"><h2>现在</h2>{"".join(rows)}</div>'


# ── 怎么看 ──────────────────────────────────────────────────────────────────
def _row(c: dict) -> str:
    """三件事的一行:问题 · 结论 + 一句适用条件;codex 不同意露在外面,完整理由折叠。"""
    by = '<span class="by">按规则</span>' if c.get("by") == "rule" else ""
    other = ""
    if c.get("other"):
        o = c["other"]
        other = f'<p class="other">{_e(o["author"])} 不同意:{_e(o["answer"])}(选的是「{_e(o["pick_text"])}」)</p>'
    when = f'<span class="when"> · {_e(c["when"])}</span>' if c.get("when") else ""
    why = f'<details class="why"><summary>完整理由</summary><p>{_e(c["why"])}</p></details>' if c.get("why") else ""
    return (f'<div class="r3"><p class="r3h"><span class="q">{_e(c["q"])}{by}</span>'
            f'<b class="ans">{_e(c["answer"])}</b>{when}</p>{other}{why}</div>')


def _scen(rows: list[dict]) -> str:
    out = []
    for i, x in enumerate(rows):
        p = x["p"] if isinstance(x.get("p"), int) else 0
        how = f'<details class="how"><summary>价位怎么估的</summary><p>{_e(x["how"])}</p></details>' if x.get("how") else ""
        out.append(f'<div class="scen"><div class="sh"><b>{_e(x["label"])} · {p}%</b><span class="subj">主观</span>'
                   f'<span>{_e(x["what"])}</span></div>'
                   f'<div class="track" aria-hidden="true"><div style="width:{p}%;background:{SCEN_COLOR[i % 4]}"></div></div>'
                   f'<p class="when">估计低点可能在 <b>{_e(x["low_when"])}</b>,<b>{_e(x["low_text"])}</b></p>{how}</div>')
    return "".join(out)


def _entry_html(s: dict) -> str:
    rows = s.get("entry") or []
    if not rows:
        return ""
    lit = sum(1 for r in rows if r.get("ok") is True)
    known = sum(1 for r in rows if r.get("ok") is not None)
    items = "".join(f'<li class="{"lit" if r.get("ok") else ("off" if r.get("ok") is False else "na")}">'
                    f'{_e(r["text"])} <small>({_e(r["who"])})</small></li>' for r in rows)
    return (f"<details><summary>买回条件:能自动判的 {known} 项里满足 {lit} 项</summary>"
            f'<p class="small">要同时满足才算到买点;满足几项不是买入成功的概率。AI 写的条件要人看,程序不判。</p>'
            f'<ul class="signals">{items}</ul></details>')


def _fail_html(j: dict) -> str:
    why = ";".join(j.get("errors") or []) or "答卷里没有这一块(旧答卷,或这次两个 AI 都没写出来)"
    rule = j.get("rule")
    cell = f'<div class="rows3">{_row({"q": "手上的", "by": "rule", **rule})}</div>' if rule else ""
    return f'<p>这一层这次没写出判断:<small>{_e(why)}</small></p>{cell}'


def judge_html(s: dict, data: dict) -> str:
    j = s.get("judge") or {}
    if not j.get("ok"):
        return f'<div class="jcard judge"><h2>怎么看</h2>{_fail_html(j)}{_entry_html(s)}</div>'
    unv = "" if (data.get("validated") or {}).get(j["author"]) else " · 尚未验证"   # 和别处一样,按画页面时的验证情况
    who = "Claude" if j["author"] == "claude" else j["author"]
    nxt = j.get("next")
    nxt_html = (f'<p>下一个能看出是哪种的日子:<b>{_e(nxt["date"])}</b> {_e(nxt["what"])}'
                + (f"({_e(nxt['why'])})" if nxt.get("why") else "") + "</p>") if nxt else ""
    dates = "".join(f"<li><b>{_e(r['date'])}</b> {_e(r['what'])}" + (f" <small>· {_e(r['why'])}</small>" if r["why"] else "")
                    + "</li>" for r in j["key_dates"])
    facts = "".join(f'<div class="fact"><span>{_e(f["kind"])}</span><div>{_e(f["text"])}<small>出处:{_e(f["source"])}</small>'
                    "</div></div>" for f in j["facts"])
    ideas = (data.get("ref_past") or {}).get("ideas")
    status = (f"这条判断由 {_e(who)} 在 {_e(data.get('as_of'))} 按当时输入包里的事实写的(它不联网,新闻要先写进查证笔记);"
              f"{_e(j['review'])} 到期核对:按最低收盘价落在哪档、跟上面的百分比对照打分。"
              + (f"提醒:{_e(ideas)},所以这类判断准不准,要靠以后一条条核对。" if ideas else ""))
    return (f'<div class="jcard judge"><h2>怎么看<span class="jtag">按当时的事实判断{unv}</span></h2>'
            f'<p>{_e(j["fact"])}</p><p class="big">{_e(j["view"])}</p>'
            f'<div class="rows3">{"".join(_row(c) for c in j["three"])}</div>'
            f'<p class="change">什么情况要重新检查这个判断:{_e(j["change_if"])}</p>'
            f'<h3>接下来几种情况 <span class="small">到 {_e(j["review"])} 前,这段时间的低点估计在哪</span></h3>'
            '<p class="small">百分比是按当时的事实做的主观估计,不是拿过去数据算出来的概率;几档价位都不是跌不破的底线。</p>'
            f'{_scen(j["scenarios"])}{nxt_html}'
            f'<details><summary>全部关键日期</summary><ol class="dates">{dates}</ol></details>'
            f'<details><summary>依据:当时看到的事实(带出处)</summary>{facts}</details>'
            f'{_entry_html(s)}<p class="small">{status}</p></div>')


# ── 价格可能晃多大 ──────────────────────────────────────────────────────────
def _ticks(lo: float, hi: float, n: int = 3) -> list[float]:
    """纵轴刻度:大约 n 段,步长取 1 / 2 / 5 × 10 的整数次方。"""
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    v, out = math.ceil(lo / step) * step, []
    while v <= hi:
        out.append(v)
        v += step
    return out


def _tw(t: str, fs: float) -> float:
    return sum(fs if ord(ch) > 0x2E80 else fs * 0.6 for ch in t)


def x_label_rows(labels: list[tuple[float, str, str]], fs: float, gap: float = 6, rows: int = 3) -> list[int]:
    """日期标签 [(x, 字, 靠哪边:start / middle / end)] → 每个放第几行(0 起):
    和这一行已有的标签留不出 gap 就往下挪,最多 rows 行。"""
    used = [[] for _ in range(rows)]
    out = []
    for x, t, a in labels:
        w = _tw(t, fs)
        x0 = x if a == "start" else (x - w if a == "end" else x - w / 2)
        x1 = x0 + w
        r = next((i for i, rw in enumerate(used) if all(x1 + gap < u0 or x0 > u1 + gap for u0, u1 in rw)), rows - 1)
        used[r].append((x0, x1))
        out.append(r)
    return out


def x_labels(p: dict, h: int, k0: int, X, small: bool, first_day: str | None) -> list[tuple[float, str, str]]:
    """横轴只放三个:开头那天的年月、起点、到期日(中间的 +3 个月、+半年不放,手机上太挤)。"""
    end = p["ends"].get(str(h)) or ""
    out = [(X(k0), first_day[:7], "start")] if first_day else []
    out.append((X(0), f"起点 {p['as_of'][5:]}", "middle"))
    out.append((X(h), (f"到期约 {end[2:]}" if small else f"到期约 {end}") if end else "到期", "end"))
    return out


def band_svg(p: dict, h: int, line, small: bool, title: str) -> str:
    g = NARROW if small else WIDE
    W, H, L, R, T, B, fs = g["W"], g["H"], g["L"], g["R"], g["T"], g["B"], g["fs"]
    past = p["history"][-((h if h < 250 else 126) + 1):]
    n = len(past) - 1
    k0 = -n if n > 0 else -20
    q = lambda k, z: p["s0"] * math.exp(p["sigma"] * math.sqrt(k) * z)  # noqa: E731
    ys = [c for _, c in past] + [q(h, Z["10"]), q(h, Z["90"]), p["s0"]] + ([float(line)] if line else [])
    lo, hi = min(ys) * 0.93, max(ys) * 1.05
    X = lambda k: L + (k - k0) / (h - k0) * (W - L - R)  # noqa: E731
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)  # noqa: E731
    ks = range(h + 1)
    poly = lambda za, zb: " ".join([f"{X(k):.1f},{Y(q(k, za)):.1f}" for k in ks]  # noqa: E731
                                   + [f"{X(k):.1f},{Y(q(k, zb)):.1f}" for k in reversed(ks)])
    body = []
    for v in _ticks(lo, hi):
        body.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#ece9df"/>')
        body.append(svg._txt(L - 6, Y(v) + fs * 0.35, _f0(v), fs, svg.MUTED, "end"))
    body.append(f'<polygon points="{poly(Z["90"], Z["10"])}" fill="#b5cde4" opacity=".75"/>')
    body.append(f'<polygon points="{poly(Z["75"], Z["25"])}" fill="#3f6f9e" opacity=".5"/>')
    body.append(f'<line x1="{X(0):.1f}" x2="{X(h):.1f}" y1="{Y(p["s0"]):.1f}" y2="{Y(p["s0"]):.1f}" '
                'stroke="#3f6f9e" stroke-dasharray="5 4" stroke-width="1.5"/>')
    if line:
        body.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(line):.1f}" y2="{Y(line):.1f}" stroke="{svg.RED}" '
                    'stroke-dasharray="6 4" stroke-width="1.5"/>')
        body.append(svg._txt(L + 6, Y(line) - 5, f"卖出线 {_f0(line)}", fs, svg.RED, halo=True))
    if n > 0:
        pts = " ".join(f"{X(i - n):.1f},{Y(c):.1f}" for i, (_, c) in enumerate(past))
        body.append(f'<polyline points="{pts}" fill="none" stroke="{svg.INK}" stroke-width="2"/>')
    body.append(f'<line x1="{X(0):.1f}" x2="{X(0):.1f}" y1="{T - 8}" y2="{H - B}" stroke="{svg.INK}" stroke-dasharray="2 3"/>')
    body.append(svg._txt(X(0) - 6, T - 10, "判断前真实收盘", fs, svg.INK, "end"))
    body.append(svg._txt(X(0) + 6, T - 10, "估计的范围", fs, svg.T_BLUE))
    xl = x_labels(p, h, k0, X, small, past[0][0] if n > 0 else None)
    for (x, t, a), r in zip(xl, x_label_rows(xl, fs)):
        body.append(svg._txt(x, H - B + fs + 4 + r * (fs + 3), t, fs, svg.MUTED, a))
    lab = sorted([(q(h, Z["90"]), "约一成高于 "), (p["s0"], "中间 "), (q(h, Z["10"]), "约一成低于 ")], key=lambda x: -x[0])
    last = -1e9
    for v, t in lab:
        y = max(Y(v) + fs * 0.35, last + fs + 2)
        last = y
        body.append(svg._txt(W - R + 8, y, t + _f0(v), fs, svg.T_BLUE))
    return svg._svg(W, H, title, body, "band")


def _band_facts(s: dict, h: str, hz: str, data: dict) -> str:
    p = s["band"]
    q = lambda z: board_judge.band_q(p, int(h), z)  # noqa: E731
    pu = ((s["layers"].get("baseline") or {}).get("horizons") or {}).get(h, {}).get("p_up")
    rows = [f"到期那天的收盘:约一成模拟结果低于 <b>{_f0(q(Z['10']))}</b>、约一成高于 <b>{_f0(q(Z['90']))}</b>;"
            f"一半的模拟结果落在 {_f0(q(Z['25']))}~{_f0(q(Z['75']))}。途中可能比 {_f0(q(Z['10']))} 更低"]
    if isinstance(pu, int):
        rows.append(f"比起点 {_f0(p['s0'])} 涨的走法:100 种里 <b>{pu}</b> 种(只看波动,本来就不判断方向)")
    cov = ((data.get("ref_past") or {}).get("cover") or {}).get(h)
    if cov:
        rows.append(f"以前靠不靠谱:拿过去数据检验,{hz}的八成范围实际接住 {cov[0]};2023 年以来 {cov[1]}")
    return "<ul>" + "".join(f"<li>{x}</li>" for x in rows) + "</ul>"


def _band_how(p: dict) -> str:
    return ('<details class="how"><summary>怎么看这张图、怎么算的</summary>'
            '<p class="legend"><span><i class="sw" style="background:#141413;height:3px"></i>判断前真实收盘</span>'
            '<span><i class="sw" style="background:#b5cde4"></i>浅色 + 深色:每天各自估计的八成范围(不是说八成走法全程待在里面)</span>'
            '<span><i class="sw" style="background:#3f6f9e"></i>深色:一半的模拟结果落在这里</span></p>'
            '<p class="small">只假设波动跟最近 120 个交易日一样,不判断涨跌方向,回答的是「会晃多大」。'
            "上面几种情况是按业务情况估的低点,这里是某一天的收盘价,两者不是一回事,都不是价格下限。"
            f"以 {_e(p['as_of'])} 收盘 {_f0(p['s0'])} 为起点;3 个月按 63 个交易日算。</p></details>")


def band_html(s: dict, data: dict) -> str:
    p = s.get("band")
    if not p:
        return ('<div class="jcard band"><h2>价格可能晃多大</h2>'
                "<p>这次没有历史波动参考的数(底线那一层没算出来),画不了图。</p></div>")
    btn = "".join(f'<button type="button" data-h="{h}" aria-pressed="{"true" if h == "63" else "false"}">{t}</button>'
                  for h, t in HZ)
    figs = []
    for h, hz in HZ:
        end = p["ends"].get(h) or "?"
        title = f"{s['name']}:{hz}后那天(约 {end})的收盘价可能在哪"
        alt = title + ",只按波动算,不判断方向"
        figs.append(f'<figure class="bh" data-h="{h}"{"" if h == "63" else " hidden"}><h3>{_e(title)}</h3>'
                    f'<div class="wide">{band_svg(p, int(h), s.get("sell_line"), False, alt)}</div>'
                    f'<div class="narrow">{band_svg(p, int(h), s.get("sell_line"), True, alt)}</div>'
                    f'<p class="bandnote">{BAND_NOTE}</p>{_band_facts(s, h, hz, data)}</figure>')
    return (f'<div class="jcard band bandbox"><h2>价格可能晃多大</h2><p class="hzbtn">{btn}</p>'
            '<p class="small">只按最近的波动估计价格会晃多大,不判断涨跌方向。</p>'
            f"{''.join(figs)}{_band_how(p)}</div>")


# ── 对照 ────────────────────────────────────────────────────────────────────
def _side(x) -> str:
    return "上" if x > 0 else "下"


def ref_html(s: dict, data: dict) -> str:
    r, past, name = s.get("ref") or {}, data.get("ref_past") or {}, s["name"]
    rows = []
    p63 = r.get("system_p63")
    act = "、".join(r.get("system_active") or []) or "没有"
    if isinstance(p63, int):
        cmp = "跟只看波动的 50 种几乎一样" if abs(p63 - 50) <= 5 else f"比只看波动的 50 种{'多' if p63 > 50 else '少'} {abs(p63 - 50)} 种"
        stop = "、".join(r.get("system_stopped") or [])
        how = (f"这次用到{_e(act)};其中{_e(stop)}拿过去数据检验时没显出用处,已停用,下次出预测不再算进去"
               if stop else f"现在只用{_e(act)},其余几项拿过去数据检验时没显出用处、已停用")
        rows.append(f"<b>系统算法</b>({how}):3 个月涨的走法 100 种里 <b>{p63}</b> 种,{cmp},不能当买卖信号。")
    sg, ig = r.get("stock_ma_gap"), r.get("index_ma_gap")
    if sg is not None and ig is not None:
        now = "成立,规则说拿着" if r.get("t60i_ok") else "不成立,规则说不拿"
        rows.append(f"<b>规则「本股和创业板指都在 60 日线上才拿着」</b>:现在{now}({_e(name)}在 60 日线{_side(sg)} "
                    f"{abs(sg):.0f}%,创业板指在线{_side(ig)} {abs(ig):.0f}%)。"
                    + (f"({_e(past['t60i'])})" if past.get("t60i") else ""))
    dd = r.get("dd_1y")
    if dd is not None:
        hit = "已经触发" if r.get("dip_hit") else "没触发"
        rows.append(f"<b>规则「从一年高点跌超一半就买」</b>:{hit}({_e(name)}比一年高点低 {abs(dd):.0f}%)。"
                    + (f"({_e(past['dip50'])};按我们的原则只当参考,不当这次的概率)" if past.get("dip50") else ""))
    if not rows:
        return ""
    src = f'<p class="small">过去检验的结果来自 {_e(past["from"])} 那次拿过去数据做的检验。</p>' if past.get("from") else ""
    return ('<div class="jcard ref"><h2>对照:简单规则和系统算法现在怎么说</h2>'
            "<p>这些方法过去的检验,不足以支持这次的买卖判断;放在这里,是让上面的判断至少要比它们说得更有道理。</p>"
            f"<details><summary>展开看它们现在各说什么、过去检验的结果</summary><ul>"
            + "".join(f"<li>{x}</li>" for x in rows) + f"</ul>{src}</details></div>")


def first_screen(s: dict, data: dict) -> str:
    """一只股票的第一屏三块(「现在」在外面,整个账户一份)。"""
    return judge_html(s, data) + band_html(s, data) + ref_html(s, data)
