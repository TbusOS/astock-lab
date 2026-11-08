#!/usr/bin/env python3
"""render_scores —— 成绩页(给预测系统纠错用)的正文,按 11 块排(总账、各层成绩、错在哪里……执行回顾)。

它是什么:render.render_scores / render_phone 调这里拿正文,外面套 templates/scores.html。
单独一个文件只是为了 render.py 不太长;数都在 board_scores.py 里算好,这里只排版。
几条规矩:
  - 独立样本不到 MIN_NEFF_FOR_CHART(20)时只写一句「样本还少……」,不画任何比较图(校准图)。
  - 概率误差(Brier)前台叫「概率误差,越低越好」;区间得分用一句话解释;覆盖比例只作参考。
  - 「旧题重跑」和「改之后才出的新题」分两栏;第一批题没到期时照实写空着的原因。
检查:scripts/check_board_pages.py。
"""
from __future__ import annotations

from html import escape

import svg

LAYERS = ("baseline", "system", "claude", "codex")
LAYER_TEXT = {"baseline": "统计底线", "system": "系统算法", "claude": "Claude", "codex": "codex"}
# 「按买点买 vs 当天买」这类题没有哪一层给概率:它检验的是买点规则本身,只数胜负(questions.rule_questions)
RULE_NOTE = ("「按预测的买点买,3 个月后比当天就买多赚还是少赚」这道题不是哪一层给的概率,"
             "按买点规则的胜率算(每只股票每次出预测各一道,63 个交易日后比),所以上面各层的成绩里没有它。")


def _e(s) -> str:
    return escape("" if s is None else str(s), quote=True)


def _ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>" if items else ""


def _f(x, nd=3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _p(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _diff(x) -> str:
    return "—" if x is None else f"{x:+.3f}"


# ── 2 三层各自的成绩 ────────────────────────────────────────────────────────
def _layers(sc: dict, data: dict) -> str:
    rep = sc.get("report") or {}
    lay, diff = rep.get("layers") or {}, rep.get("diff_vs_baseline") or {}
    def cell(a, fn):
        return f"<td>{fn(lay.get(a) or {}, diff.get(a) or {})}</td>"
    rows = [("已打分题数(相当于独立样本)", lambda v, d: f"{v.get('n', 0)}({v.get('n_eff', 0)})"),
            ("方向猜对的比例", lambda v, d: _p(v.get("direction_hit"))),
            ("概率误差,越低越好", lambda v, d: _f(v.get("brier"))),
            ("分类题概率误差", lambda v, d: _f(v.get("brier_multi"))),
            ("区间得分,越低越好", lambda v, d: _f(v.get("interval_score"))),
            ("25~75 区间包住实际(应约 50%)", lambda v, d: _p((v.get("coverage_by_level") or {}).get("50"))),
            ("10~90 区间包住实际(应约 80%)", lambda v, d: _p((v.get("coverage_by_level") or {}).get("80"))),
            ("概率误差比统计底线(负数 = 更好)", lambda v, d: _diff(d.get("brier")))]
    head = "<tr><th>指标</th>" + "".join(f"<th>{LAYER_TEXT[a]}</th>" for a in LAYERS) + "</tr>"
    body = "".join(f"<tr><td>{_e(name)}</td>" + "".join(cell(a, fn) for a in LAYERS) + "</tr>" for name, fn in rows)
    n, need = sc.get("max_neff") or 0, sc.get("min_neff_chart") or 20
    if n < need:
        due = (data.get("horizon_ends") or {}).get("63")
        lead = (f'<p class="big">样本还少:已到期能打分的题相当于约 {n} 个独立样本,要 {need} 个以上才画图比较谁更准。'
                f"下面的数只说明现状。</p><p class=\"small\">两只同行股票、重叠的期限、同一周出的题一起涨跌,不算各自独立。"
                + (f"这次存档的 3 个月题约 {_e(due)} 到期。" if due else "") + "</p>")
        calib = ""
    else:
        lead = f"<p>已到期能打分的题相当于约 {n} 个独立样本。</p>"
        calib = (f'<figure class="calib">{svg.calib_chart(sc.get("calib") or {})}<figcaption>'
                 "落在虚线上 = 说多少种就真有多少种;在虚线下面 = 说得太乐观。</figcaption></figure>")
    rule = (rep.get("rule") or {})
    rule_line = (f"买点规则(不算概率,只数胜负):{rule['n']} 次里信号日买更好 {rule['wins']} 次。"
                 if rule.get("n") else "买点规则还没有到期的检验。") + RULE_NOTE
    return ("<section><h2>三层各自的成绩</h2>" + lead
            + f'<table class="layers">{head}{body}</table>' + calib
            + "<p class=\"small\">概率误差:说 100 种里 70 种会涨、结果涨了,误差是 (0.7−1)² = 0.09,越低越好。"
            "区间得分:区间越宽扣得越多,实际落在区间外面还按离多远加扣 —— 报太宽、报太窄都吃亏。"
            "覆盖比例只作参考:只看它会鼓励把区间报宽。系统算法和 AI 都要跟统计底线比。</p>"
            f"<p>{_e(rule_line)}</p></section>")


# ── 3 错在哪里 ──────────────────────────────────────────────────────────────
def _cause_bars(causes: list) -> str:
    return f'<div class="rows">{svg.hbars([(c, v) for c, v in causes], unit="%")}</div>' if causes else ""


def _source(x: dict) -> str:
    """「原因概率 · <谁给的> · <谁 哪天>:」平铺写,不套括号。"""
    return " · ".join(_e(v) for v in ("原因概率", x.get("from"), x.get("who")) if v) + ":"


def _wrong(sc: dict) -> str:
    agg = [(c, v) for c, v in sc.get("causes") or [] if v]
    total = ("按原因汇总(人确认过的):" + "、".join(f"{_e(c)} 累计约 {v:.1f} 道题的份量" for c, v in agg)
             if agg else "还没有人确认过的原因。")
    cards = []
    for w in sc.get("wrong") or []:
        src = _source(w) if w.get("from") else "原因概率:还没给(board.py resolve 会给程序算的初值,再由人确认)"
        ev = _ul([_e(x) for x in w.get("evidence") or []])
        cards.append(f'<div class="card wrong"><p><b>{_e(w["author"])} · {_e(w["code"])}</b> {_e(w["said"])}</p>'
                     f"<p>实际:{_e(w['actual'])}</p><p>{src}</p>{_cause_bars(w.get('causes'))}{ev}</div>")
    return ("<section><h2>错在哪里</h2><p>每道错题,每个可能的原因各给一个概率,加起来 100;"
            "不写「判断不了」。</p>" + f"<p>{total}</p>"
            + ("".join(cards) or _none_yet(sc, "还没有到期的错题", "journal_wrong", "里错了")) + "</section>")


def _none_yet(sc: dict, text: str, key: str, verb: str) -> str:
    """错在哪里、比对卡没有上线后的题时:换算进来的旧记录有几道照实写,并指到它们那一栏。"""
    n = (sc.get("totals") or {}).get(key) or 0
    if not n:
        return f"<p>{text}。</p>"
    return (f"<p>{text}(上线后出的题)。换算进来的 journal 旧记录{verb} {n} 道,只做原文比对,"
            "题目、结果、概率误差、原因概率列在下面「journal 旧记录原文比对」那一栏。</p>")


# ── 4~7 ─────────────────────────────────────────────────────────────────────
def _chain(sc: dict) -> str:
    rows = sc.get("chain") or []
    if not rows:
        body = "<p>还没有能沿计算链(营收 → 利润率 → 净利润 → 估值倍数 → 股价 → 动作)逐步核对的题:要等季报出来、题目到期。</p>"
    else:
        body = "".join(f"<p>{_e(r)}</p>" for r in rows)
    return f"<section><h2>沿计算链逐步看</h2>{body}</section>"


def _improve(sc: dict) -> str:
    rows = "".join(f"<tr><td>{_e(i['cause'])}</td><td><code>{_e(i['file'])}</code></td><td>{_e(i['desc'])}</td>"
                   f"<td>{_e(i['status'])}{(' ' + _e(i['commit'])) if i.get('commit') else ''}</td></tr>"
                   for i in sc.get("improvements") or [])
    table = ("<table><tr><th>原因</th><th>改哪个文件</th><th>说明</th><th>状态</th></tr>" + rows + "</table>"
             if rows else "<p>还没有要改的地方。</p>")
    return ("<section><h2>要改的地方</h2>" + table
            + "<p class=\"small\">旧题重跑的结果、改之后新题的成绩见下一节。</p></section>")


def _versions(sc: dict, data: dict) -> str:
    bv = (sc.get("report") or {}).get("by_version") or {}
    lines = []
    for head, v in bv.items():
        parts = [f"{LAYER_TEXT.get(a, a)} 概率误差 {_f(x.get('brier'))}({x.get('n')} 题)"
                 for a, x in (v.get("layers") or {}).items()]
        label = "聊天里给的判断" if head == "聊天" else f"代码版本 <code>{_e(head)}</code>"
        lines.append(f"{label}:" + _e(" · ".join(parts)))
    due = (data.get("horizon_ends") or {}).get("63")
    new = _ul(lines) or (f"<p>还没有到期的新题。这次存档的 3 个月题约 {_e(due)} 到期,在那之前这一栏是空的。</p>")
    return ("<section><h2>改了以后有没有变准</h2><p class=\"small\">同一批题、同一期限、同一种评分下比较版本。</p>"
            '<div class="cols2"><div><h3>旧题重跑</h3><p>说明改动影响了什么(和存档里当时算的两层比,'
            "差 = 重跑 − 原来)。</p>" + _replays(sc) + "</div>"
            f"<div><h3>改之后才出的新题</h3><p>说明改动是不是真的有用。</p>{new}</div></div></section>")


def _replays(sc: dict) -> str:
    rows = []
    for r in sc.get("replays") or []:
        code = ("存档当时那份代码" if r.get("code_ref") == "run" else f"代码 {r.get('code_ref')}"
                + (f"({r['commit']})" if r.get("commit") and r["commit"] != r.get("code_ref") else ""))
        inp = (f"修正后的输入 {r['corrected']}(改了 {'、'.join(r.get('changed') or []) or '没改'})"
               if r.get("corrected") else "原输入")
        rows.append(f"{_e(r.get('at'))} 重跑 {_e(r.get('run'))}:{_e(code)} + {_e(inp)} —— "
                    + ";".join(_e(x) for x in r.get("lines") or []))
    return _ul(rows) or "<p>还没有存下来的重跑结果(board.py replay 跑完会记在这里)。</p>"


def _ai(sc: dict) -> str:
    a = sc.get("ai_compare") or {}
    n = a.get("n") or 0
    if not n:
        body = "<p>还没有两个 AI 都答了、又已到期的题。</p>"
    else:
        c, x = a.get("claude") or {}, a.get("codex") or {}
        cb, xb = c.get("brier"), x.get("brier")
        lead = ""
        if cb is not None and xb is not None and cb != xb:
            lead = f"目前 {'Claude' if cb < xb else 'codex'} 误差低(样本少,只说现状)。"
        body = (f"<p>共同已结算 {n} 道题(同一批题、同一份输入包、冻结版本):Claude 概率误差 {_f(cb)}、"
                f"codex {_f(xb)}。{lead}</p>")
    return f"<section><h2>两个 AI 比</h2>{body}</section>"


# ── 8 比对卡 ────────────────────────────────────────────────────────────────
def _cards(sc: dict) -> str:
    out = []
    for c in sc.get("cards") or []:
        miss = "、".join(_e(m) for m in c.get("missing") or []) or "没记下缺什么"
        exc = f";扣掉同期大盘偏 {c['excess']:+.1%}" if isinstance(c.get("excess"), (int, float)) else ";同期大盘没有记录"
        causes = (f"<p>{_source(c)}</p>{_cause_bars(c['causes'])}{_ul([_e(x) for x in c.get('evidence') or []])}"
                  if c.get("causes") else "")
        out.append(f'<div class="card{" wrong" if c.get("wrong") else ""}">'
                   f'<div class="half"><p><b>当时怎么说</b>({_e(c["opened"])} · {_e(c["author"])} · {_e(c["code"])})</p>'
                   f"<p>{_e(c['type'])}:{_e(c['said'])}</p>"
                   f"<p class=\"small\"><b>依据</b>:{';'.join(_e(x) for x in c.get('basis') or ['这一层没写依据'])}</p>"
                   f"<p class=\"small\">当时缺的数据:{miss}</p></div>"
                   f'<div class="half"><p><b>实际怎样</b>:{_e(c["actual"])}{_e(exc)}</p>'
                   f"<p class=\"small\">{_e(c['note'])}</p>{causes}</div></div>")
    return ("<section><h2>比对卡</h2>" + (f'<div class="grid-cards">{"".join(out)}</div>' if out
                                          else _none_yet(sc, "还没有到期结算的题", "journal_settled", "结算了"))
            + "</section>")


# ── 9~11 ────────────────────────────────────────────────────────────────────
def _quarters(sc: dict) -> str:
    q = sc.get("quarters")
    if not q or not q.get("rows"):
        return "<section><h2>季度业绩比对</h2><p>还没跑季度回测(backtest.py --walkforward)。</p></section>"
    figs = []
    for code in dict.fromkeys(r["code"] for r in q["rows"]):
        rows = [r for r in q["rows"] if r["code"] == code]
        chart = svg.range_dots([(r["target"], r["low"], r["high"], r["mid"], r["real"]) for r in rows])
        figs.append(f"<h3>{_e(rows[0]['name'])} {_e(code)}</h3><div class=\"rows\">{chart}</div>")
    return ("<section><h2>季度业绩比对</h2><p class=\"small\">每季一行:空心横条 = 预测区间,空心点 = 中枢,"
            f"实心点 = 实际营收(亿元)。来源:季度营收走前回测,生成于 {_e(q.get('made'))}。</p>"
            + "".join(figs) + "</section>")


def _runs(sc: dict) -> str:
    rows = []
    for r in sc.get("runs") or []:
        bits = [f"{r['run']}", f"代码 {r['head'] or '?'}" + ("" if r.get("clean") in (True, None) else "(有未提交改动)")]
        if not r.get("complete"):
            bits.append("没跑完(没有 manifest)")
        if r.get("superseded_by"):
            bits.append(f"被取代,新版是 {r['superseded_by']}(旧版照样到期核对)")
        if r.get("failed"):
            bits.append("作答失败:" + "、".join(LAYER_TEXT.get(a, a) for a in r["failed"]))
        if r.get("no_answer"):
            bits.append("没有答卷:" + "、".join(LAYER_TEXT.get(a, a) for a in r["no_answer"]))
        rows.append(_e(" · ".join(bits)))
    return "<section><h2>版本链</h2>" + (_ul(rows) or "<p>还没有存档。</p>") + "</section>"


def _journal_questions(sc: dict) -> str:
    rows = []
    for j in sc.get("journal_questions") or []:
        res = _e(j["actual"]) if j.get("actual") else "还没到期"
        if j.get("brier") is not None:
            res += f";概率误差 {j['brier']:.4f}(越低越好)"
        cause = ""
        if j.get("causes") and j.get("wrong"):
            cause = (f"<br>{_source(j)}" + "、".join(f"{_e(c)} {v}" for c, v in j["causes"] if v)
                     + "".join(f"<br>证据:{_e(x)}" for x in j.get("evidence") or []))
        rows.append(f"{_e(j['code'])} · {_e(j['author'])} · 开题 {_e(j['opened'])}:{_e(j['said'])}"
                    f"<br>换算说明:{_e(j['note'])}<br>结果:{res}{cause}")
    return ("<section><h2>journal 旧记录原文比对</h2><p class=\"small\">上线前 journal 里的判断,按原文换算成题"
            "(board.py add-question,开题日早于第一次存档)。只做原文比对:不进各层成绩、不进两个 AI 比。</p>"
            + (_ul(rows) or "<p>还没有换算进来的旧记录。</p>") + "</section>")


def _journal(sc: dict) -> str:
    due = []
    for j in sc.get("journal_due") or []:
        old = "(被取代的旧版,不算命中率)" if j.get("superseded") else ""
        due.append(f"#{_e(j['id'])} {_e(j['code'])} 记于 {_e(j['logged'])},复核日 {_e(j['check_date'])},当时价 "
                   f"{_e(j['price'])},动作 {_e(j['action'])}{old}<br>判断:{_e(j['thesis'])}<br>什么情况算错:{_e(j['falsify'])}")
    ex = []
    for x in sc.get("execution") or []:
        word = {"done": "做了", "skipped": "没做"}.get(x.get("done"), "没记录做没做")
        ex.append(f"#{_e(x['id'])} {_e(x['code'])} {_e(x['action'])},期限 {_e(x.get('deadline') or '没写')}:{word}"
                  + (f" — {_e(x['note'])}" if x.get("note") else ""))
    return ("<section><h2>已到期待复核的旧记录</h2><p class=\"small\">上线前 journal 里的判断只做原文比对:"
            "有明确价格线和日期的照题型评分,其余照原文列出。核完用 journal close。</p>"
            + (_ul(due) or "<p>没有到期待复核的旧记录。</p>")
            + "</section><section><h2>执行回顾</h2><p class=\"small\">单列,不影响预测得分:动作做没做、做了多少。</p>"
            + (_ul(ex) or "<p>还没有要执行的动作记录。</p>") + "</section>")


def scores_body(data: dict, board_href: str = "看板.html") -> str:
    sc = data.get("scores") or {}
    t = sc.get("totals") or {}
    head = (f'<header class="top"><h1>预测成绩</h1><p>给预测系统纠错用:哪一层准、错在哪、改了以后有没有变准。</p>'
            f'<p class="ver">页面生成于 {_e(data.get("today"))} · 最新存档 {_e(data.get("run"))}</p></header>')
    def inc(n) -> str:
        return f"(含换算进来的旧记录 {n} 道)" if n else ""
    total = (f"<section><h2>总账</h2><p class=\"big\">共 {t.get('questions', 0)} 道题{inc(t.get('journal_total'))}、"
             f"已到期 {t.get('due', 0)} 道{inc(t.get('journal_due_n'))}、"
             f"已结算 {t.get('resolved', 0)} 道{inc(t.get('journal_settled'))};"
             f"journal 里另有旧记录 {t.get('journal_old', 0)} 条(只做原文比对),"
             f"其中 {t.get('journal_due', 0)} 条到期待复核。</p></section>")
    return (head + total + _layers(sc, data) + _wrong(sc) + _chain(sc) + _improve(sc) + _versions(sc, data)
            + _ai(sc) + _cards(sc) + _quarters(sc) + _runs(sc) + _journal_questions(sc) + _journal(sc)
            + f'<footer><a href="{_e(board_href)}">← 回看板</a></footer>')
