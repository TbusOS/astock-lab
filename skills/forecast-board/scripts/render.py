#!/usr/bin/env python3
"""render —— 预测看板的三个页面:看板.html、成绩.html、看板_手机版.html(两页合成一个文件)。

它是什么:把 build_board_data 拼好的那份数排成静态 HTML。样式(templates/style.css)和数据
(<script id="board-data" type="application/json">)都内联进页面,不引用外部脚本、样式、字体,
离线能看,手机版一个文件 AirDrop 过去就能打开。页面只做排版,不再算数 —— 数都在 board_data.py。

页面内容:看板分顶栏、每只股票一块、关键日期、市场背景、成绩页链接五块;成绩页 11 块。几条硬规定:
  - 顶栏过期:判断作于距今超过 30 天、status.json 里有已触发的条件、持仓数据过期、关键数据取不到,
    任一成立就写「下面的结论已过期 / 已触发,需要重新判断」并列出原因。分两处判:
      · 生成页面时(stale_reasons,按 data["today"]):四条都判,写死在 HTML 里,不开脚本也看得到;
      · 打开页面时(页面里的小脚本 STALE_SCRIPT):判两样天数 —— 页面是静态文件,手机版离线看,
        生成那天没过期、40 天后再打开也要变过期。脚本拿内联数据的 as_of(判断作于哪天)、
        account.asof(持仓数据截至哪天)和北京时间的今天比,阈值和 Python 这边是同一个 FORECAST_STALE_DAYS、
        account.STALE_DAYS(生成时写进脚本);生成时已经写了的那条(data-why="days" / "holdings")不重复加。
  - 每块标身份:已公布 / 某机构 某日发布的预测 / 模型推算 / AI 判断;每项数据标去向:
    参与系统计算 / 只给 AI 参考 / 只展示。
  - 手里没有这只股票:「下次什么时候买」升到结论条下面第一块;持有时折叠在后面。
  - 概率一律说成「100 种走法里有几种」;「不涨」包括持平,不写成「跌」。
入口:render_board(data) / render_scores(data) / render_phone(data) / write_all(data, out_dir);
build_board_data 从 board_data 转出来,render.build_board_data 也能用。
检查:scripts/check_board_pages.py。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from html import escape
from pathlib import Path

import account
import render_judge
import svg
from board_data import build_board_data, key_text  # noqa: F401  build_board_data 从这里转出
from render_scores import scores_body

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
FORECAST_STALE_DAYS = 30    # 判断作于多少天以前算过期(持仓过期是另一个数 account.STALE_DAYS = 7)
HZ_TEXT = {"63": "3 个月", "126": "半年", "250": "一年"}
TONE = {"63": "strong", "126": "mid", "250": "faint"}
LAYER_TEXT = {"baseline": "统计底线", "system": "系统算法", "claude": "Claude", "codex": "codex"}
KIND_TEXT = {"sell": "卖出", "trim": "减仓", "hold": "持有", "wait": "等待", "buy_batches": "分批买入",
             "update_holdings": "先更新持仓"}
FILES = ("看板.html", "成绩.html", "看板_手机版.html")
# 预测是存档里冻结的,动作(卖多少、仓位上限)是每次画页面时按当时的持仓、收盘和代码现算的:两样不是同一份代码
ACTIONS_NOW = "「怎么做」按这天的持仓、收盘和当前代码现算(不是出预测时的代码)"
STALE_HEAD = "下面的结论已过期 / 已触发,需要重新判断"
DAYS_WHY = "判断作于 {as_of},已经过了 {days} 天(超过 {limit} 天要重新出预测)"
HOLD_WHY = ("持仓数据截至 {asof},已经过了 {days} 天(超过 {limit} 天算过期):下面的卖多少、买多少是按旧持仓算的,"
            "先更新借款看板,再跑 board.py update")
# 打开页面时再按天数判一次过期(判断作于哪天、持仓截至哪天)。中文用 json.dumps 写成 \uXXXX,
# 页面源码里不直接出现这几个字:没过期的页面就不含「下面的结论已过期」,检查才分得清。
STALE_SCRIPT = """(function () {
  var FORECAST_STALE_DAYS = %(limit)d;
  var HOLD_DAYS = %(hold)d;
  var HEAD = %(head)s, WHY = %(why)s, HOLD_WHY = %(hold_why)s;
  var el = document.getElementById('board-data'), top = document.querySelector('#board header.top');
  if (!el || !top) return;
  var d;
  try { d = JSON.parse(el.textContent); } catch (e) { return; }
  var now = new Date(Date.now() + 8 * 3600000);            // 北京时间的今天
  var today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  function age(s) {
    var p = String(s || '').split('-');
    if (p.length !== 3) return null;
    return Math.round((today - Date.UTC(+p[0], +p[1] - 1, +p[2])) / 86400000);
  }
  var lines = [], days = age(d.as_of), acc = d.account || {}, hdays = age(acc.asof);
  if (days !== null && days > FORECAST_STALE_DAYS)
    lines.push(['days', WHY.replace('{as_of}', d.as_of).replace('{days}', days).replace('{limit}', FORECAST_STALE_DAYS)]);
  if (hdays !== null && hdays > HOLD_DAYS)
    lines.push(['holdings', HOLD_WHY.replace('{asof}', acc.asof).replace('{days}', hdays).replace('{limit}', HOLD_DAYS)]);
  if (!lines.length) return;
  var box = document.getElementById('stale-banner');
  if (!box) {
    box = document.createElement('div');
    box.id = 'stale-banner'; box.className = 'banner'; box.setAttribute('role', 'alert');
    var h = document.createElement('p'); h.className = 'big'; h.textContent = HEAD;
    box.appendChild(h); box.appendChild(document.createElement('ul'));
    top.parentNode.insertBefore(box, top.nextSibling);
  }
  lines.forEach(function (x) {
    if (box.querySelector('[data-why="' + x[0] + '"]')) return;     // 生成页面时已经写了这一条
    var li = document.createElement('li');
    li.setAttribute('data-why', x[0]);
    li.textContent = x[1];
    box.querySelector('ul').appendChild(li);
  });
  top.classList.add('alert');
})();""" % {"limit": FORECAST_STALE_DAYS, "hold": account.STALE_DAYS, "head": json.dumps(STALE_HEAD),
            "why": json.dumps(DAYS_WHY), "hold_why": json.dumps(HOLD_WHY)}
TAB_SCRIPT = """(function () {
  var box = document.getElementById('stocks');
  var tabs = document.querySelectorAll('.tabs a');
  if (!box || !tabs.length) return;
  function show(code) {
    box.classList.add('one');
    box.querySelectorAll('article.stock').forEach(function (a) { a.classList.toggle('on', a.dataset.code === code); });
    tabs.forEach(function (t) { t.setAttribute('aria-current', t.dataset.code === code ? 'true' : 'false'); });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function (ev) { ev.preventDefault(); show(t.dataset.code); });
  });
  show(tabs[0].dataset.code);
})();"""


def _e(s) -> str:
    return escape("" if s is None else str(s), quote=True)


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def _fill(template: str, values: dict) -> str:
    """模板里的 {{名字}} 一次换完(和 ai_answer.build_prompt 一样):填进去的内容(AI 的理由、页面正文)
    里就算出现 {{DATA}} 这种字样,也不会被下一轮当成占位符再换一次。"""
    lack = [k for k in values if "{{" + k + "}}" not in template]
    if lack:
        raise ValueError(f"模板里没有占位符 {', '.join('{{' + k + '}}' for k in lack)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), template)


def data_script(data: dict) -> str:
    """数据内联成 JSON;< > & 换成 \\u 转义,正文里写了 </script> 也关不掉这个标签。"""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script id="board-data" type="application/json">{text}</script>'


def tag(kind: str, text: str | None = None) -> str:
    """身份标签:fact 已公布 · inst 机构的预测 · model 模型推算 · ai AI 判断 · acct 账户记录。"""
    default = {"fact": "已公布", "inst": "机构的预测", "model": "模型推算", "ai": "AI 判断", "acct": "账户记录"}
    return f'<span class="tag t-{kind}">{_e(text or default[kind])}</span>'


def use(label: str | None) -> str:
    return f'<span class="use">{_e(label)}</span>' if label else ""


def src(m: dict | None) -> str:
    if not m:
        return ""
    bits = [f"数据截至 {m.get('as_of')}" if m.get("as_of") else "", m.get("source") or ""]
    st = "" if m.get("status") == "ok" else f'<span class="warn">{_e(m.get("status_text"))}</span> {_e(m.get("note"))}'
    return f'<span class="src">{_e(" · ".join(b for b in bits if b))} {st}</span>'


def _ul(items: list[str]) -> str:
    """items 已经转义过(里面可能有 <b> 这类标签)。"""
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>" if items else ""


def _pct(x) -> str:
    return "没取到" if x is None else f"{x:+.1f}%"


def _money(x) -> str:
    return "?" if x is None else f"{x:,.0f}"


# ── 顶栏 ────────────────────────────────────────────────────────────────────
def stale_items(data: dict) -> list[tuple[str, str]]:
    """顶栏要变色的原因 [(种类, 一句话)];空 = 不过期。种类 days 是按天数判的那条(页面脚本认它,不重复加)。"""
    out = []
    try:
        days = (date.fromisoformat(data["today"]) - date.fromisoformat(data["as_of"])).days
    except (KeyError, TypeError, ValueError):
        days = None
    if days is None or days > FORECAST_STALE_DAYS:
        out.append(("days", DAYS_WHY.format(as_of=data.get("as_of"), days=days if days is not None else "?",
                                           limit=FORECAST_STALE_DAYS)))
    acc = data.get("account") or {}
    if acc.get("stale"):            # 打开页面时脚本按 data-why="holdings" 认这一条,不重复加
        out.append(("holdings", f"持仓数据截至 {acc.get('asof') or '不知道哪天'},已过期或读不出来"
                    + (f"({acc.get('error')})" if acc.get("error") else "") + ",先更新借款看板再算动作"))
    return out + [("other", r) for r in _other_reasons(data)]


def stale_reasons(data: dict) -> list[str]:
    """顶栏要变色的原因(只要句子)。"""
    return [r for _, r in stale_items(data)]


def _other_reasons(data: dict) -> list[str]:
    out = [str(n) for n in data.get("notices") or []]
    st = data.get("status") or {}
    if st.get("unreadable"):
        out.append("每日检查的结果 status.json 读不出来:触发了什么、到期几道、哪只没查成都不知道,先跑一次 board.py check")
    for t in st.get("triggered") or []:
        out.append(f"每日检查({str(st.get('checked_at') or '')[:16]})已触发:{t.get('code') or ''} {t.get('what')}"
                   + (f"({t.get('detail')})" if t.get("detail") else ""))
    for u in st.get("unchecked") or []:          # 这只这次没查成:卖出线、改主意条件、到期题都不知道
        head = "" if u.get("kind") == "no_bars" else "没查成:"     # 停牌那只不写成「没查成」,读着像网络出错
        out.append(f"每日检查({str(st.get('checked_at') or '')[:16]}){head}{u.get('code') or ''} {u.get('what')}"
                   + (f"({u.get('detail')})" if u.get("detail") else ""))
    if data.get("failed_required"):
        out.append("关键数据取不到:" + "、".join(key_text(k) for k in data["failed_required"]))
    return out


def _check_stamp(data: dict) -> str:
    """顶栏「每日检查于 …」:做在这次预测以前、不是页面生成那天做的,都在后面写出来。"""
    st = data.get("status")
    if not st:
        return "每日检查还没跑过"
    if st.get("unreadable"):
        return "每日检查的结果读不出来"
    at = str(st.get("checked_at") or "")
    if st.get("before_run"):
        tail = "(早于这次预测,出预测以后还没查过)"
    elif at[:10] and at[:10] < str(data.get("today") or ""):
        tail = "(不是今天查的)"
    else:
        tail = ""
    return f"每日检查于 {at[:16].replace('T', ' ') or '?'}{tail}"


def _topbar(data: dict, reasons: list[tuple[str, str]]) -> str:
    acc, mk = data.get("account") or {}, data.get("market") or {}
    made = str(data.get("made_at") or "")[11:16]
    # 默认只露三样:行情日期、判断时间、每日检查状态;大盘一句另起一行(不带标签);
    # 持仓截至、代码版本、存档编号、数据去向标签收进「数据与版本」折叠
    stamps = [f"行情更新于 {data.get('market_asof') or '?'}", f"判断作于 {data.get('as_of')}" + (f" {made}" if made else ""),
              _check_stamp(data)]
    rows = [r for r in (mk.get("regime"), mk.get("margin")) if r]
    plain = " · ".join(_e(r["text"]) for r in rows) or "这次没取到大盘数据"
    mrows = [f"{_e(r['text'])} {tag('fact')} {use(r['used_by'])}" for r in rows]
    code = data.get("code") or {}
    ver = f"代码版本 {code.get('head') or '?'}" + ("" if code.get("clean") in (True, None) else "(有未提交改动)")
    out = [f'<header class="top{" alert" if reasons else ""}"><h1>预测看板</h1>',
           '<p class="stamps">' + "".join(f"<span>{_e(s)}</span>" for s in stamps) + "</p>",
           f'<p class="mkt">大盘:{plain}</p>',
           '<details class="meta"><summary>数据与版本</summary>',
           f'<p>持仓数据截至 {_e(acc.get("asof") or "?")}</p>',
           f'<p class="mkt">{" · ".join(mrows) or "这次没取到大盘数据"}</p>',
           f'<p class="ver">{_e(ver)}(出预测时)· 存档 {_e(data.get("run"))} · 页面生成于 {_e(data.get("today"))}'
           f",{_e(ACTIONS_NOW)}</p></details></header>"]
    if reasons:
        lis = "".join(f'<li{f" data-why={chr(34)}{k}{chr(34)}" if k in ("days", "holdings") else ""}>{_e(r)}</li>'
                      for k, r in reasons)
        out.append(f'<div class="banner" id="stale-banner" role="alert"><p class="big">{_e(STALE_HEAD)}</p>'
                   f"<ul>{lis}</ul></div>")
    for a, errs in (data.get("layers_failed") or {}).items():
        out.append(f'<div class="notice"><b>{_e(LAYER_TEXT.get(a, a))} 这次作答失败</b>,这一层没有数(不拿默认值顶上):'
                   f'{_e(";".join(errs))}</div>')
    if data.get("seen_problem"):
        head = "以前的买卖记录读不出来" if data["seen_problem"] == "读不出来" else "没找到以前的买卖记录"
        out.append(f'<div class="notice">{head},「最晚哪天做」暂时从今天算。'
                   f'<details><summary>技术原因</summary>动作从哪天开始的记录(actions_seen.json){_e(data["seen_problem"])},'
                   "卖出、减仓的「最晚哪天做」这次从今天算</details></div>")
    for a in data.get("layers_not_asked") or []:
        out.append(f'<div class="notice"><b>{_e(LAYER_TEXT.get(a, a))} 这次没让它答</b>(存档里没有这份答卷,'
                   "比如出预测时加了 --no-codex),这一层没有数</div>")
    return "".join(out)


# ── 每只股票 ────────────────────────────────────────────────────────────────
def _dir_word(p) -> str:
    """p_up 是「期末高于起点」的种数,剩下的包括持平,所以不到一半不写成「跌」。"""
    if p is None:
        return "没有数"
    return "偏涨" if p > 50 else ("涨的不到一半" if p < 50 else "涨和不涨各一半")


def _unval(data: dict, layer: str) -> str:
    return "" if data["validated"].get(layer) else ' <span class="unval">尚未验证</span>'


def _due_text(a: dict, done: str = "做完") -> str:
    """截止日那半句:没过 →「最晚 <日期> 做完」;过了动作还在 →「已过最晚日期 <日期>,还没做」。"""
    if a.get("overdue"):
        return f"已过最晚日期 {a.get('deadline')},还没做"
    return f"最晚 {a.get('deadline')}{(' ' + done) if done else ''}"


def _action_text(s: dict) -> str:
    a = s.get("action") or {}
    k, sh, held = a.get("kind"), a.get("shares"), s.get("shares") or 0
    if k in ("sell", "trim"):
        left = held - (sh or 0)
        return (f"{KIND_TEXT[k]} {sh} 股(约 {_money(a.get('amount'))} 元)"
                + (f",剩 {left} 股继续持有" if left > 0 else ",卖完不剩") + f",{_due_text(a)}")
    if k == "hold":
        return f"继续持有 {held} 股,这次不卖"
    if k == "buy_batches":
        b = "、".join(f"{svg.fmt_price(x['price'])} 元买约 {_money(x['amount'])} 元" for x in a.get("batches") or [])
        return f"分批买入,共约 {_money(a.get('amount'))} 元:{b},{_due_text(a, '')}"
    if k == "wait":
        return "先等,这次不买"
    if k == "update_holdings":
        return "先更新持仓数据,再算卖多少"
    return "没算出动作"


def _change_if(s: dict, main: str | None) -> list[str]:
    rows = [f"如果{_e(c.get('if'))} → {_e(c.get('then'))}(已定规则)" for c in (s.get("action") or {}).get("change_if") or []]
    act = (s["layers"][main]["action"] or {}) if main else {}
    rows += [f"如果{_e(c.get('if'))} → {_e(c.get('then'))}({_e(LAYER_TEXT[main])})" for c in act.get("change_if") or []]
    return rows


# 结论条:第一屏要读出的 9 格按这个顺序排,每格一两行;长的(AI 的整段理由、
# 第二条以后的改主意条件)折叠进 <details>。下面 c~g 那几块照旧是完整版。
MISS_HEAD = {"gap": "还没有工具", "partial": "只取到一部分", "stale": "用了旧值", "failed": "取不到"}


def _more(summary: str, body: str) -> str:
    return f'<details class="more"><summary>{_e(summary)}</summary>{body}</details>'


def _buy_money_text(data: dict) -> str:
    """买入那块「能用多少钱」一句。free_cash_until 是算进去的最后一个月(含),所以写「到 X 为止」。"""
    acc = data.get("account") or {}
    fc = acc.get("free_cash")
    if fc is None:
        return "借款数据读不出来,算不了能用多少钱。"
    if fc <= 0:
        return (f"按现在的还款计划,到 {acc.get('free_cash_until')} 为止没有闲钱,买就等于动用还款的钱"
                f"(闲钱 {_money(fc)} 元)。买点预测照样给出,用来检验系统。")
    return f"不影响还款的闲钱约 {_money(fc)} 元(往后 6 个月的还款已经扣掉)。"


def _repay_need_text(acc: dict) -> str:
    """「X 要还 Y 元」:配了留够几个月(cover_months > 1)写那几个月的合计,否则照旧写这个月。"""
    n = acc.get("cover_months")
    if isinstance(n, int) and n > 1 and acc.get("cover_need") is not None:
        word = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}.get(n, str(n))
        return f"{acc.get('cover_from')}~{acc.get('cover_until')} {word}个月要还 {_money(acc.get('cover_need'))} 元"
    return f"{acc.get('need_month') or '这个月'} 要还 {_money(acc.get('month_need'))} 元"


def _why_short(s: dict, data: dict) -> str:
    """为什么:只写规则算出这个动作的那个原因(看跌 / 仓位超限 / 只因为要还款),带钱数。"""
    a, acc = s.get("action") or {}, data.get("account") or {}
    over = a.get("over_limit")
    if a.get("kind") == "trim" and over:
        both = f";还款也要卖 {a['repay_shares']} 股,取大的" if a.get("repay_shares") else ""
        return f"仓位超限:现在占净资产 {over['now_pct']:.0f}%,{over['label']}{both}"
    if a.get("kind") == "trim":
        # 「只」因为要还款:要确实算过仓位上限、没超才这么说;上限这次算不出来就不说「只」,并写出来
        lead = "只因为要还款" if a.get("limit_known") is True else "因为要还款"
        tail = ";仓位上限这次算不出来" if a.get("limit_known") is False else ""
        how = "按已定顺序" if "按卖出顺序" in str(a.get("why") or "") else "按已定比例"
        return (f"{lead}:{_repay_need_text(acc)},现金不够,{how}这只卖约 {_money(a.get('amount'))} 元{tail}")
    if a.get("kind") == "sell" and s.get("sell_line") is not None:
        return f"看跌:跌破卖出线 {svg.fmt_price(s['sell_line'])}(已定规则),全部卖出约 {_money(a.get('amount'))} 元"
    return str(a.get("why") or "没写").split(";")[0]


def _why_cell(s: dict, main: str | None, data: dict) -> str:
    L, a = s["layers"], s.get("action") or {}
    body = [f"<p>已定规则怎么算的:{_e(a.get('why') or '没写')}</p>"]
    if main and (L[main]["action"] or {}).get("why"):
        body.append(f"<p>{_e(LAYER_TEXT[main])} 的看法:{_e(L[main]['action']['why'])}</p>")
    return _e(_why_short(s, data)) + _more(f"{LAYER_TEXT[main]} 的完整理由" if main else "规则的完整算法", "".join(body))


def _change_cell(s: dict, main: str | None) -> str:
    rows = _change_if(s, main)
    if not rows:
        return "没写"
    return rows[0] + (_more(f"还有 {len(rows) - 1} 条", _ul(rows[1:])) if len(rows) > 1 else "")


def _horizons_cell(s: dict, data: dict, main: str | None) -> str:
    lay = main or "system"
    hz = s["layers"][lay]["horizons"]
    bits = [f"{HZ_TEXT[h]} <b>{hz[h]['p_up']}</b> 种{_unval(data, lay)}" for h in ("63", "126", "250")
            if isinstance((hz.get(h) or {}).get("p_up"), int)]
    return (" · ".join(bits) or "没有数") + f' <span class="small">(100 种走法里涨几种,{_e(LAYER_TEXT[lay])})</span>'


def _bottom_cell(s: dict, data: dict, main: str | None) -> str:
    lay = main or "system"
    b = s["layers"][lay]["bottom"] or {}
    mp, bins, out = b.get("month_probs") or {}, b.get("price_bins") or [], []
    if mp:
        k, v = max(mp.items(), key=lambda kv: kv[1])
        out.append(f"最低点最可能在 {'更晚' if k == 'later' else _e(month_text(k, data.get('as_of')))}(100 种里 {v} 种)")
    if bins:
        lo, hi, p = max(bins, key=lambda x: x[2])
        band = f"{svg.fmt_price(hi)} 元以下" if not lo else f"{svg.fmt_price(lo)}~{svg.fmt_price(hi)} 元"
        out.append(f"最低价最可能在 {band}(100 种里 {p} 种)")
    return (",".join(out) or "没有数") + _unval(data, lay)


def _missing_cell(s: dict) -> str:
    groups: dict[str, list[str]] = {}
    for m in s.get("missing") or []:
        head = MISS_HEAD.get(m.get("kind"), "")
        text = str(m.get("text") or "")
        groups.setdefault(head, []).append(text.split(":", 1)[-1].split("(")[0] if head else text)
    return _e(";".join(f"{h}:{'、'.join(v)}" if h else "、".join(v) for h, v in groups.items())
              or "这次该取的都取到了") + ' <span class="small">(详见下面「现在怎样」)</span>'


def _other_cell(s: dict, data: dict, main: str | None) -> str:
    L = s["layers"]
    other = next((a for a in ("claude", "codex") if a != main), None) if main else None
    if not other:
        return "两个 AI 都作答失败"
    if L[other]["failed"]:
        return f"{_e(LAYER_TEXT[other])} 这次{'没让它答' if L[other].get('not_asked') else '作答失败'},没有并排的结论"
    pm, ov = (L[main]["horizons"].get("63") or {}).get("p_up"), L[other]["horizons"].get("63") or {}
    okind, mkind = (L[other]["action"] or {}).get("kind"), (L[main]["action"] or {}).get("kind")
    out = [f"{_e(LAYER_TEXT[other])}:3 个月涨 {_e(ov.get('p_up'))} 种{_unval(data, other)} · "
           f"建议{_e(KIND_TEXT.get(okind, okind))} {tag('ai')}"]
    if mkind and okind and mkind != okind:
        out.append(f'<span class="diff">两个 AI 动作不同:{_e(LAYER_TEXT[main])} {_e(KIND_TEXT.get(mkind, mkind))},'
                   f'{_e(LAYER_TEXT[other])} {_e(KIND_TEXT.get(okind, okind))},看一眼两边的理由</span>')
    po = ov.get("p_up")
    if isinstance(pm, int) and isinstance(po, int) and abs(pm - po) >= 15:
        out.append(f'<span class="diff">两个 AI 涨的种数差 {abs(pm - po)} 种(15 种以上)</span>')
    return "<br>".join(out)


def _alerts_cell(s: dict) -> str:
    rows = [_e(a["text"]) for a in s.get("alerts") or []]
    return f'<span class="diff">{"<br>".join(rows)}</span>' if rows else "没有"


def _verdict(s: dict, data: dict) -> str:
    main, L = s.get("main"), s["layers"]
    view = L[main]["horizons"].get("63") if main else None
    if view:
        med = view["q"].get("50")
        chg = f",中间价 {svg.fmt_price(med)}(比判断那天的收盘 {(med / s['start_close'] - 1) * 100:+.1f}%)" \
            if med and s.get("start_close") else ""
        look = f"3 个月{_dir_word(view['p_up'])}:100 种走法里涨 {view['p_up']} 种{chg}"
        who = f"{LAYER_TEXT[main]} · 判断作于 {data['as_of']} {tag('ai')}"
    else:
        look, who = "两个 AI 都作答失败,这次没有 AI 的判断", tag("ai")
    cells = [("怎么看", f"{_e(look)}{_unval(data, main) if main else ''}", " big"),
             ("怎么做", _e(_action_text(s)), " big"),
             ("为什么", _why_cell(s, main, data), ""),
             ("什么时候改主意", _change_cell(s, main), ""),
             ("三个期限", _horizons_cell(s, data, main), ""),
             ("见底", _bottom_cell(s, data, main), ""),
             ("本次缺什么", _missing_cell(s), ""),
             ("另一个 AI", _other_cell(s, data, main), ""),
             ("要马上注意的", _alerts_cell(s), "")]
    out = [f'<section class="verdict"><div class="who">{who}</div><div class="four">']
    out += [f'<div class="k">{k}</div><div class="v{cls}">{v}</div>' for k, v, cls in cells]
    out.append("</div>")
    a = s.get("action") or {}
    if a.get("rule_conflict"):
        say = a["rule_conflict"].removeprefix("AI 建议:").split(" 理由:")[0]
        out.append(f'<p class="rulefix">AI 建议改规则:{_e(say)}(理由在「完整理由」里;动作仍按已定规则,改不改由你定)</p>')
    return "".join(out) + "</section>"


def _now(s: dict, data: dict) -> str:
    f, acc = s.get("facts") or {}, data.get("account") or {}
    first = (f"现价 <b>{_e(svg.fmt_price(s['close']) if s.get('close') else '?')}</b>({_e(s.get('close_date'))} 收盘)"
             f" · 近 1 个月 {_pct(f.get('m1'))} · 近 3 个月 {_pct(f.get('m3'))} · 离一年高点 {_pct(f.get('dd_1y'))}"
             f" {tag('fact')} {use(s['prices_item']['used_by'])}{src(s['prices_item'])}")
    if s.get("shares") is None:
        hold = f"持股不知道(持仓数据读不出来) {tag('acct')}"
    else:
        cost = f" · 成本 {svg.fmt_price(s['cost'])}" if s.get("cost") else ""
        hold = f"持股 {s['shares']} 股{cost} {tag('acct')} {use(acc.get('used_by'))}"
    rows = [first, hold + f'<span class="src">借款看板 · 截至 {_e(acc.get("asof"))}</span>']
    for r in s.get("leading") or []:
        kind = "fact" if r["tag"] == "已公布" else "inst"
        rows.append(f"<b>{_e(r['label'])}</b>:{_e(r['text'])} {tag(kind, r['tag'])} {use(r['used_by'])}{src(r)}")
    miss = "、".join(_e(m["text"]) for m in s.get("missing") or []) or "这次该取的都取到了"
    return (f'<section class="now"><h3>现在怎样</h3>{_ul(rows)}'
            f"<p><b>本次检查的数据缺什么:</b>{miss}</p></section>")


def _icons(s: dict, data: dict) -> str:
    main = s.get("main") or "system"
    figs = []
    for h in ("63", "126", "250"):
        v = s["layers"][main]["horizons"].get(h)
        if not v or not isinstance(v.get("p_up"), int):
            continue
        others = [f"{LAYER_TEXT[a]} {s['layers'][a]['horizons'][h]['p_up']}{_unval(data, a)}"
                  for a in ("baseline", "system", "claude", "codex")
                  if a != main and not s["layers"][a]["failed"] and h in s["layers"][a]["horizons"]]
        figs.append(f'<figure class="h{h}">{svg.icon_array(v["p_up"], HZ_TEXT[h], TONE[h])}'
                    f"<figcaption><b>{HZ_TEXT[h]}</b>:100 种走法里涨 {v['p_up']} 种{_unval(data, main)}"
                    f"<br><small>另外几层:{' · '.join(others) or '没有'}</small></figcaption></figure>")
    return f'<div class="icons">{"".join(figs)}</div>'


def _fan(s: dict) -> str:
    L = s["layers"]
    bands = L["baseline"]["horizons"]
    bands = {h: v["q"] for h, v in bands.items()}
    marks = {a: {h: v["q"] for h, v in L[a]["horizons"].items()} for a in (s.get("main"), "system", "claude", "codex")
             if a and not L[a]["failed"]}
    lines = {k: v for k, v in (("cost", s.get("cost")), ("sell_line", s.get("sell_line"))) if v}
    chart = svg.fan_chart([tuple(x) for x in s.get("history") or []], bands, marks, lines)
    sw = lambda col, txt: f'<span><i class="sw" style="background:{col}"></i>{_e(txt)}</span>'  # noqa: E731
    leg = [sw("rgba(106,155,204,.45)", "扇形:模型推算:只按波动幅度,不带方向(浅 10~90 分位,深 25~75 分位)")]
    leg += [sw(svg.LAYER_STYLE[a][0], f"{LAYER_TEXT[a]} 的分位(竖线 10~90、粗段 25~75、圆点中间价)") for a in marks]
    leg += [sw(svg.GOLD, "成本价(金色虚线)")] if "cost" in lines else []
    leg += [sw(svg.RED, "卖出线(红色虚线)")] if "sell_line" in lines else []
    return (f'<figure class="fanbox">{chart}<figcaption><div class="legend">{"".join(leg)}</div>'
            "阴影是每个时间点价格可能落在哪,不是说价格一路都在阴影里。三个时间点各层只打标记、不连线;"
            "3 个月最显眼,一年最淡。纵轴按涨跌比例画(涨一倍和跌一半一样长)。"
            f"{tag('model')}</figcaption></figure>")


def _layer_table(s: dict, data: dict) -> str:
    head = "<tr><th>层</th>" + "".join(f"<th>{HZ_TEXT[h]}</th>" for h in ("63", "126", "250")) + "</tr>"
    rows = []
    for a in ("baseline", "system", "claude", "codex"):
        v = s["layers"][a]
        if v["failed"]:
            rows.append(f"<tr><td>{LAYER_TEXT[a]}</td><td colspan=3>{'没让它答' if v.get('not_asked') else '作答失败'}</td></tr>")
            continue
        cells = "".join(f"<td>涨 {v['horizons'][h]['p_up']} 种 · 中间价 {svg.fmt_price(v['horizons'][h]['q']['50'])}</td>"
                        if h in v["horizons"] and v["horizons"][h]["q"].get("50") else "<td>没有</td>" for h in ("63", "126", "250"))
        who = tag("model") if a in ("baseline", "system") else tag("ai")
        rows.append(f"<tr><td>{LAYER_TEXT[a]} {who}{_unval(data, a)}</td>{cells}</tr>")
    return f'<table class="layers">{head}{"".join(rows)}</table>'


def _future(s: dict, data: dict) -> str:
    main = s.get("main")
    who = f"{LAYER_TEXT[main]} {tag('ai')}" if main else f"系统算法 {tag('model')}(两个 AI 都作答失败)"
    return (f'<section class="future"><h3>未来怎样</h3><p class="small">百格图是 {who} 的数;'
            f"每格是一种走法,绿 = 涨,灰 = 不涨(含持平)。</p>{_icons(s, data)}{_fan(s)}{_layer_table(s, data)}</section>")


def month_text(key: str, as_of) -> str:
    """类别键 2026-11 → 「11 月」;和判断那天不是同一年的写「2027 年 1 月」;认不出原样返回。"""
    try:
        y, m = (int(x) for x in str(key).split("-"))
    except ValueError:
        return str(key)
    return f"{m} 月" if str(as_of or "")[:4] == f"{y:04d}" else f"{y} 年 {m} 月"


def _top_month(probs: dict, as_of=None) -> str:
    if not probs:
        return "没有数"
    k, v = max(probs.items(), key=lambda kv: kv[1])
    return f"{'更晚或没出现' if k == 'later' else month_text(k, as_of)}({v} 种)"


def _bottom(s: dict, data: dict) -> str:
    L, main = s["layers"], s.get("main") or "system"
    mark = _unval(data, main)
    mp, bins = L[main]["bottom"].get("month_probs") or {}, L[main]["bottom"].get("price_bins") or []
    say = "、".join(f"{'更晚' if k == 'later' else month_text(k, data.get('as_of'))} {v} 种" for k, v in mp.items())
    others = " · ".join(f"{_e(LAYER_TEXT[a])} 最可能 {_e(_top_month(L[a]['bottom'].get('month_probs'), data.get('as_of')))}"
                        f"{_unval(data, a)}"
                        for a in ("baseline", "system", "claude", "codex") if a != main and not L[a]["failed"])
    sig = []
    for r in s.get("rebound") or []:
        cls, word = {True: ("lit", "亮了"), False: ("off", "没亮"), None: ("na", "没取到数据")}[r["lit"]]
        sig.append(f'<li class="{cls}"><b>{_e(r["name"])}</b>:{word}{(" — " + _e(r["text"])) if r["text"] else ""}</li>')
    sig_html = f'<ul class="signals">{"".join(sig)}</ul>' if sig else ""
    an = s.get("analogs")
    if an:
        g = an.get("given") or {}
        bk = (g.get("days_to_bottom") or {}).get("buckets") or {}
        analog = (f"<p>{_e(an.get('sample_note'))}</p><p>和现在一样的那几段,跌破后见底用时:"
                  + "、".join(f"{_e(k)} {v} 次" for k, v in bk.items()) + "</p>")
    else:
        analog = "<p>这次没算历史同类情形(股价跌破一年高点 50% 以后过去几次怎么走)。</p>"
    return ('<section class="bottom"><h3>什么时候见底、什么时候反弹</h3>'
            f"<h4>最低点在哪段时间 {tag('ai') if s.get('main') else tag('model')}</h4>"
            f"<p>100 种走法里,最低点出现在:{_e(say) or '没有数'}。{mark}</p>"
            f'<div class="rows">{svg.month_bars(mp) if mp else ""}</div><p class="small">其他层:{others}</p>'
            f"<h4>最低点在什么价位{mark}</h4>"
            f'<div class="rows">{svg.bin_bars(bins) if bins else "<p>没有数</p>"}</div>'
            "<h4>反弹开始的确认信号</h4><p class=\"small\">这些是发生了才看得到的事,不是预测。</p>"
            f'{sig_html}<p class="small">每个信号历史上出现时离低点多远,这一版还没算。'
            "「新一年开支计划」认句子里写的年份,财年(如 FY27)按数字当成 2027 年,不换算成日历年。</p>"
            f"<h4>历史同类情形</h4>{analog}</section>")


def _buy(s: dict, data: dict) -> str:
    L, main = s["layers"], s.get("main")
    view = L[main] if main else L["system"]
    mark = _unval(data, main or "system")
    zones = view["buy"].get("zones") or []
    zb = svg.hbars([(f"跌到 {svg.fmt_price(p)} 以下", pr) for p, pr in zones]) if zones else "<p>没有给买入价位。</p>"
    zs = "、".join(f"跌到 {svg.fmt_price(p)} 元以下的 {pr} 种" for p, pr in zones)
    mp = view["buy"].get("month_probs") or {}
    rec = (data["scores"]["report"].get("rule") or {})
    rule = (f"买点规则检验过 {rec['n']} 次,信号日买比当天买涨得多的 {rec['wins']} 次"
            if rec.get("n") else "买点规则还没有到期的检验(每次出预测记一道,拿 63 天后的结果比)")
    ent = []
    for r in s.get("entry") or []:
        word = {True: "现在满足", False: "现在不满足", None: "要人看"}[r["ok"]]
        ent.append(f"{_e(r['text'])}:<b>{word}</b> <small>({_e(r['who'])})</small>")
    odds = "".join(f"<tr><td>{svg.fmt_price(o['price'])}</td><td>"
                   + ("没有数" if o["p_above"] is None else f"{'≥' if o['bound'] == 'ge' else ('≤' if o['bound'] == 'le' else '')}"
                      f"{o['p_above']} 种") + f"</td><td>{_pct(o['median_pct'])}</td></tr>" for o in s.get("odds") or [])
    money = _buy_money_text(data)
    body = (f"<h4>买入价位 {tag('ai') if main else tag('model')}</h4>"
            f"<p>100 种走法里,未来 3 个月{_e(zs) or '没有数'}。{mark}</p>"
            f'<div class="rows">{zb}</div>'
            f"<h4>买入时间</h4><p>入场条件最可能在 {_e(_top_month(mp, data.get('as_of')))} 第一次满足。{mark}</p>"
            f'<div class="rows">{svg.month_bars(mp) if mp else ""}</div>'
            f"<h4>入场条件</h4>{_ul(ent)}<p class=\"small\">{_e(rule)}</p>"
            f"<h4>在这几个价位买的赔率 {tag('model')}{mark}</h4>"
            "<table><tr><th>买价</th><th>3 个月后收盘高于买价</th><th>中间价比买价</th></tr>" + odds + "</table>"
            f"<p class=\"small\">按 {_e(LAYER_TEXT.get(main, '系统算法'))} 第 63 个交易日的分位价粗算,"
            "假设在这个价买到;实际买入可能在这 3 个月中间,只当参考。</p>"
            f"<h4>能用多少钱买</h4><p>{_e(money)}</p>")
    if s.get("held"):
        return (f'<section class="buy"><details class="buyfold"><summary>下次什么时候买(手里有这只,先折叠)</summary>'
                f"{body}</details></section>")
    why = ("手里没有这只,这块排在前面。" if s.get("shares") == 0
           else "持仓数据读不出来,不知道手里有没有,先当没有、排在前面。")
    return f'<section class="buy"><h3>下次什么时候买</h3><p class="small">{why}</p>{body}</section>'


def _evidence(s: dict) -> str:
    def cols(ev):
        ev = ev or {}
        return '<div class="cols3">' + "".join(
            f"<div><h4>{t}</h4>{_ul([_e(x) for x in ev.get(k) or []]) or '<p>没写</p>'}</div>"
            for k, t in (("support", "支持"), ("against", "反对"), ("unverified", "还没验证"))) + "</div>"
    main = s.get("main")
    if not main:
        return f'<section class="evidence"><h3>依据</h3><p>两个 AI 都作答失败,没有依据可列。</p></section>'
    other = "codex" if main == "claude" else "claude"
    extra = (f"<details><summary>{LAYER_TEXT[other]} 的依据</summary>{cols(s['layers'][other]['evidence'])}</details>"
             if not s["layers"][other]["failed"] else "")
    return (f'<section class="evidence"><h3>依据 {tag("ai")}({LAYER_TEXT[main]})</h3>'
            f"<p class=\"small\">每条后面括号里是它来自输入包的哪一项。不做综合打分。</p>"
            f"{cols(s['layers'][main]['evidence'])}{extra}</section>")


def stock_html(s: dict, data: dict) -> str:
    pos = f"持有 {s['shares']} 股" if s.get("held") else ("手里没有" if s.get("shares") == 0 else "持仓不知道")
    parts = [f'<article class="stock" id="s-{_e(s["code"])}" data-code="{_e(s["code"])}">',
             f'<h2>{_e(s["name"])} <span class="code">{_e(s["code"])}</span> <span class="pos">{pos}</span></h2>',
             render_judge.first_screen(s, data),
             '<details class="detail"><summary>细节:各层预测、见底、买点、依据(原来的整块)</summary>',
             _verdict(s, data)]
    if not s.get("held"):
        parts.append(_buy(s, data))
    parts += [_now(s, data), _future(s, data), _bottom(s, data)]   # 要马上注意的已在结论条里
    if s.get("held"):
        parts.append(_buy(s, data))
    parts += [_evidence(s), "</details>", "</article>"]
    return "".join(parts)


# ── 关键日期、市场背景 ──────────────────────────────────────────────────────
def _dates(data: dict) -> str:
    d = data.get("dates") or {}
    ends = data.get("horizon_ends") or {}
    rows = [f"第 {h} 个交易日({HZ_TEXT.get(h, h)}那几道题)约 {_e(ends[h])} 到期" for h in sorted(ends, key=int)]
    rows += [f"{_e(k['date'])}:{_e(k['what'])} {tag('acct', '手工登记')}" for k in data.get("key_dates") or []]
    cal = [f"北京时间 {_e(r['when'])}:{_e(r['who'])} 发财报 <small>({_e(r['relation'])})</small>" for r in d.get("calendar") or []]
    sec = [f"{_e(r['date'])} {_e(r['who'])}" + ("<ul>" + "".join(f"<li>「{_e(x)}」</li>" for x in r["sentences"]) + "</ul>"
                                                if r.get("sentences") else ":没有摘到「下一年 + 资本开支」的句子")
           for r in d.get("sec") or []]
    ci, si = d.get("calendar_item"), d.get("sec_item")
    note = f'<p class="small">{_e(d["events_note"])}</p>' if d.get("events_note") else ""
    return ('<section class="dates"><h2>关键日期</h2>' + _ul(rows) + note
            + (f"<h3>未来几周的财报 {tag('fact')} {use(ci['used_by'])}</h3>{src(ci)}" + (_ul(cal) or "<p>这几周关注名单里没有公司发财报。</p>") if ci else "")
            + (f"<h3>云厂新交的 SEC 文件 {tag('fact')} {use(si['used_by'])}</h3>{src(si)}" + (_ul(sec) or "<p>近 30 天没有新文件。</p>") if si else "")
            + "</section>")


def _sectors(data: dict) -> str:
    s = data.get("sectors")
    if not s:
        return '<details class="bg"><summary>市场背景</summary><p>这次没取到板块 ETF。</p></details>'
    rows = s.get("rows") or []
    bars = svg.hbars([(f"{r['name']}", r["m3"]) for r in rows if r.get("m3") is not None], unit="%", signed=True)
    table = "".join(f"<tr><td>{_e(r['name'])} <small>{_e(r['code'])}</small></td><td>{_pct(r.get('m1'))}</td>"
                    f"<td>{_pct(r.get('m3'))}</td><td>{_pct(r.get('dd_1y'))}</td></tr>" for r in rows)
    return (f'<details class="bg"><summary>市场背景(板块 ETF 近 3 个月涨跌、离一年高点)</summary>'
            f"<p>{tag('fact')} {use(s.get('used_by'))}</p>{src(s)}<div class=\"rows\">{bars}</div>"
            f"<table><tr><th>板块</th><th>近 1 个月</th><th>近 3 个月</th><th>离一年高点</th></tr>{table}</table></details>")


def board_body(data: dict, scores_href: str = "成绩.html") -> str:
    reasons = stale_items(data)
    tabs = "".join(f'<a href="#s-{_e(s["code"])}" data-code="{_e(s["code"])}">{_e(s["name"])} {_e(s["code"])}'
                   f'<br><small>{"持有" if s.get("held") else "没有持仓"}</small></a>' for s in data.get("stocks") or [])
    return (_topbar(data, reasons) + render_judge.now_html(data) + f'<nav class="tabs" aria-label="切换股票">{tabs}</nav>'
            + '<div class="stocks" id="stocks">' + "".join(stock_html(s, data) for s in data.get("stocks") or [])
            + "</div>" + _dates(data) + _sectors(data)
            + f'<footer><a href="{_e(scores_href)}">看成绩:预测准不准、错在哪、改了以后有没有变准 →</a></footer>')


# ── 页面 ────────────────────────────────────────────────────────────────────
def _css() -> str:
    return _tpl("style.css")


def render_board(data: dict) -> str:
    return _fill(_tpl("board.html"), {"CSS": _css(), "BOARD": board_body(data), "DATA": data_script(data),
                                      "SCRIPT": STALE_SCRIPT + "\n" + TAB_SCRIPT + "\n" + render_judge.BAND_SCRIPT})


def render_scores(data: dict) -> str:
    return _fill(_tpl("scores.html"), {"CSS": _css(), "SCORES": scores_body(data), "DATA": data_script(data)})


def render_phone(data: dict) -> str:
    """看板和成绩合成一个文件,顶部两个页内跳转链接;两页之间的链接改成页内跳转。"""
    return _fill(_tpl("phone.html"), {"CSS": _css(), "BOARD": board_body(data, "#scores"),
                                      "SCORES": scores_body(data, "#board"), "DATA": data_script(data),
                                      "SCRIPT": STALE_SCRIPT + "\n" + TAB_SCRIPT + "\n" + render_judge.BAND_SCRIPT})


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_all(data: dict, out_dir: Path) -> list[Path]:
    """写 看板.html、成绩.html、看板_手机版.html 到 out_dir(先写临时文件再换名,不会留下写了一半的页面)。
    这三个页面每次更新都重写(它们是展示,不是存档;存档在 data/forecasts 里,只追加)。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pages = (render_board(data), render_scores(data), render_phone(data))
    paths = []
    for name, text in zip(FILES, pages):
        _write_atomic(out / name, text)
        paths.append(out / name)
    return paths
