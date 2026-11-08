#!/usr/bin/env python3
"""actions —— 账户层动作:这周每只卖多少、持有、还是等着买。两只股票共用一份计划。

它是什么:看板首页那一句「该干什么」。输入是 account.account_state 的结果(持股、可取现金、这个月要还的钱、
闲钱)、各只的收盘价、user 定过的规则(配置里的 sell_lines、sell_ratio、cash_months、sell_order)和 Claude 的答卷。

没有它会怎样:页面只摆概率,user 看完还是不知道「到底是继续持有还是卖掉等机会」。

为什么规则优先:卖出线、现金留够几个月、两只按比例或按顺序分摊,都是 user 点过头的。
卖出线可以不配:它防的是带融资时被强制平仓,融资还清后就没有要防的事了;这时用 cash_months
提前一两个月把还款的钱备好,不拖到还款日在当天的价格被迫卖;sell_order 让前景差的那只先卖。AI 的建议跟规则不一样时,
动作照规则写,AI 的意见另写在 rule_conflict 里(「AI 建议:… 理由:…」),改不改规则由 user 定。
持仓过期时动作是「先更新持仓」,不是规则算出来的,这时不比、rule_conflict 一律是 None。

规则(按顺序):
  1. 持仓数据过期或读不出来 → 每只 update_holdings,不拿旧股数算卖多少。过期不只看 state["stale"]:
     asof 缺了、早于今天超过 account.STALE_DAYS 天、或晚于今天(日期写错了),这里自己也判成过期
     (state 可能是几天前算的)。
  2. 手里有、收盘价跌破卖出线 → sell,全部持股。没配卖出线(sell_lines 空)就没有这一步。
  3. 其余手里有的:要卖的钱 = max(0, 要留的现金 − 可取现金 − 第 2 步全卖的钱),先取到分,不到 0.005 元算 0。
     要留的现金 = 从今天这个月起 cash_months 个月要还的钱(schedule 前几项);不配 cash_months 就是 1,
     即只看这个月(month_need)。排期是空的退回 month_need,排期不够几个月就按排到的算,两种都写进备注。
     分到各只:配了 sell_order(代码列表)就按顺序,前面那只卖到够或卖光,剩下的轮到下一只;
     没配就按 sell_ratio 比例分。股数向上取到 100 股整数倍、不超过持股。
     再看仓位上限(limits,position_limit.py 算):市值超过上限的,减到上限要卖的股数同样向上取到 100 股;
     两个股数取大的 → trim(超限这一条不看预测:超限优先);两个都是 0 → hold。
     上限算不出来时只按还款算,「为什么」里写明算不出来和原因(不悄悄跳过)。
     第 2 步全卖的钱先顶这个月的还款:够了就不用再从别的卖(有融资负债时,卖的钱会先拿去还融资,见账户备注)。
  4. 手里没有:闲钱 > 0 且 Claude 答卷给了 buy.zones → buy_batches(闲钱按 sell_ratio 分,每个价位一批,
     价高的先买);闲钱 ≤ 0 → wait,why 写「按还款计划到 <月份> 为止没有闲钱,买就等于动用还款的钱」
     (<月份> 是算进去的最后一个月,含)。
截止日(「最晚哪天做」):
  sell / trim  = 「起点日」和「since」里较晚那天之后的第 2 个交易日。
               起点日 = 这次存档的 start(as_of 之后第一个交易日);没传(老调用、检查)用今天。
               since  = 这只股票在这次存档里第一次出现这种动作的那个交易日:看板每次画页面都把每只的
                        动作种类和 since 记进 actions_seen.json(board_io.record_actions_seen),下次画时传回来
                        (参数 seen);存档和动作种类都没变就沿用,任何一样变了从今天重新算;没有记录也从今天算。
  buy_batches  = 买点那个窗口的最后一天:起点日算第 1 个的第 63 个交易日(和碰价题、决策日志复核日同一天)。
  其余不填。
为什么这么定:只从今天数,每日检查天天重画,「最晚哪天做」天天往后挪,永远到不了;只从起点日数,
存档几天以后才出现的卖出(那天才跌破卖出线)、减仓(下个月要还款、现金少了)一出来就是「已过」。
记下「第一次出现那天」,两头都对:动作一直挂着没做,截止日不动;新出现的动作有自己的两天。
过了截止日动作还在(还没卖)→ overdue = True,页面写「已过最晚日期 …,还没做」。
交易日去周末和配置里的 holidays / holidays_<年>(节假日是估的,只用来定截止日)。
检查:scripts/check_board_account.py。
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

import fb_common  # noqa: F401  先于 prices:它加载 market.py,一 import 就删代理变量
import account
import prices as price_mod

LOT = 100                   # A 股一手
CENT = 0.005                # 钱数小于这个当 0(取到分以后的「没有」),免得浮点误差多卖一手
SELL_DEADLINE_DAYS = 2      # 卖出类:今天之后第几个交易日以前做完
BUY_DAYS = 63               # 分批买的截止:和 ai_answer.ZONE_DAYS 一样(Claude 给的买点是这么多个交易日以内)
KIND_TEXT = {"sell": "卖出", "trim": "减仓", "hold": "持有", "wait": "等待", "buy_batches": "分批买入",
             "update_holdings": "先更新持仓"}
_HOLIDAY_KEY = re.compile(r"holidays(_[0-9]{4})?")


def config_holidays(cfg: dict) -> set[str]:
    """配置里所有节假日:键是 holidays 或 holidays_<年>(比如 holidays_2027)的列表合在一起。"""
    return {str(d) for k, v in (cfg or {}).items()
            if _HOLIDAY_KEY.fullmatch(str(k)) and isinstance(v, (list, tuple, set)) for d in v}


def deadline_after(today: date, n: int, holidays) -> str:
    """today 之后第 n 个交易日(today 本身不算)。"""
    return price_mod.future_trading_days(today + timedelta(days=1), n, set(holidays))[-1].isoformat()


def _num(x) -> str:
    """300.0 → '300',2.5 → '2.5'。"""
    return f"{float(x):g}"


def _price_ok(p) -> bool:
    return isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and p > 0


def _ym_after(t: date, months: int) -> str:
    y, m = divmod(t.month - 1 + months, 12)
    return f"{t.year + y:04d}-{m + 1:02d}"


def _act(kind: str, why: str, shares=None, amount=None, deadline=None, change_if=None, **extra) -> dict:
    return {"kind": kind, "shares": shares, "amount": amount, "deadline": deadline, "why": why,
            "change_if": list(change_if or []), **extra}


def _ai_stocks(ai) -> dict:
    """Claude 答卷 → {代码: 那只的答卷}。整份答卷({"stocks": …})或只给 stocks 那层都认。"""
    if not isinstance(ai, dict):
        return {}
    s = ai["stocks"] if isinstance(ai.get("stocks"), dict) else ai
    return {str(k): v for k, v in s.items() if isinstance(v, dict)}


def _conflict(kind: str, ai_stock) -> str | None:
    """AI 的 action.kind 和规则不同 → 「AI 建议:… 理由:…」;相同或没有答卷 → None。"""
    a = ai_stock.get("action") if isinstance(ai_stock, dict) else None
    if not isinstance(a, dict) or not a.get("kind") or a["kind"] == kind:
        return None
    txt = KIND_TEXT.get(a["kind"], str(a["kind"]))
    sh, amt = a.get("shares"), a.get("amount")
    if isinstance(sh, int) and not isinstance(sh, bool) and sh > 0:
        txt += f" {sh} 股"
    elif _price_ok(amt):
        txt += f" 约 {amt:,.0f} 元"
    return f"AI 建议:{txt} 理由:{str(a.get('why') or '没写').strip()}"


def _weights(codes: list[str], ratio: dict) -> dict[str, float]:
    """按 sell_ratio 归一;这几只都没配比例就平分。"""
    w = {c: max(0.0, float(ratio.get(c) or 0)) for c in codes}
    total = sum(w.values())
    if total <= 0:
        return {c: 1 / len(codes) for c in codes} if codes else {}
    return {c: v / total for c, v in w.items()}


def _margin_note(state: dict) -> list[str]:
    m = state.get("margin_debt")
    if not (isinstance(m, (int, float)) and m > 0):
        return []
    return [f"还有融资负债 {m:,.2f}:卖股票的钱可能先还融资,能取出来的比卖出额少,还掉它可取现金也会变,"
            f"以 App 里的可取现金为准"]


# ── 过期 ────────────────────────────────────────────────────────────────────
def _asof_age(state: dict, t: date) -> int | None:
    """最新一笔离今天几天;asof 缺了或不是 YYYY-MM-DD 返回 None。"""
    try:
        return (t - date.fromisoformat(state.get("asof"))).days
    except (TypeError, ValueError):
        return None


def is_stale(state: dict, t: date) -> bool:
    """state 标了过期,或 asof 缺了 / 早于 t 超过 account.STALE_DAYS 天 / 晚于 t(日期写错了,不当成最新)。
    不全信 state["stale"]:state 可能是几天前算好存下来的,那时没过期,今天已经过期了。"""
    age = _asof_age(state, t)
    return bool(state.get("stale")) or age is None or age > account.STALE_DAYS or age < 0


def _stale_why(state: dict, t: date) -> str:
    err, asof, age = state.get("error"), state.get("asof"), _asof_age(state, t)
    parts = [f"借款看板的数据读不出来({err})"] if err else []
    if age is None:
        if not err:
            parts.append("持仓数据没有日期(不知道截至哪天)")
    elif age < 0:
        if not err:                 # account_state 判出来的会写在 err 里,不重复说
            parts.append(f"持仓数据的日期 {asof} 晚于今天(写错了?)")
    elif not err or age > account.STALE_DAYS:
        parts.append(f"持仓数据过期(截至 {asof})")
    return ",".join(parts) + ",先更新再算"


def _stale_plan(state: dict, codes: list[str], t: date) -> tuple[dict, dict]:
    why = _stale_why(state, t)
    redo = [{"if": f"券商数据.js 记了新的一笔({account.STALE_DAYS} 天以内)、借款数据读得出来", "then": "重新算这周卖多少"}]
    acts = {c: _act("update_holdings", why, change_if=redo) for c in codes}
    acc = {"month_need": state.get("month_need"), "need_month": state.get("need_month"), "cash": state.get("cash"),
           "sell_need": None, "sell_total": None, "free_cash": state.get("free_cash"),
           "free_cash_until": state.get("free_cash_until"),
           "note": ";".join([why, *_margin_note(state), *(state.get("notes") or [])])}
    return acts, acc


# ── 手里有的 ────────────────────────────────────────────────────────────────
def _held_change_if(line, shares: int) -> list[dict]:
    out = [{"if": f"收盘跌破卖出线 {_num(line)}", "then": f"全部卖出({shares} 股)"}] if line is not None else []
    return out + [{"if": "持仓或借款看板的数变了", "then": "重新算这个月卖多少"}]


def _sell_all(shares: int, p: float, line, deadline: str) -> dict:
    return _act("sell", f"跌破卖出线 {_num(line)}(收盘 {p:.2f}),按已定规则全部卖出 {shares} 股",
                shares=shares, amount=round(shares * p, 2), deadline=deadline)


def _lots_up(amount: float, p: float, held: int) -> int:
    """要卖 amount 元:按收盘 p 向上取到 100 股整数倍,不超过持股。"""
    return min(held, math.ceil(amount / p / LOT - 1e-9) * LOT) if amount >= CENT else 0


def _over_limit(held: int, p: float, lim: dict | None) -> dict | None:
    """超过仓位上限 → {"shares", "now_pct", "limit_pct", "label", "note"}(减到上限至少卖几股);没超或算不出 → None。"""
    if not lim or not lim.get("ok"):
        return None
    sh = _lots_up(held * p - lim["limit_amount"], p, held)
    return {"shares": sh, "now_pct": round(lim["now_pct"], 1), "limit_pct": lim["limit_pct"], "label": lim["label"],
            "note": lim.get("note") or ""} if sh > 0 else None


def _limit_text(lim: dict | None, over: dict | None) -> str:
    """「为什么」里仓位那一段(平铺,不套括号)。lim 是 None(调用方没算上限)时不写。
    这段在页面上折叠在「完整理由」里,所以 journal 那条细节(detail)也写进来。"""
    if lim is None:
        return ""
    if not lim.get("ok"):
        return f"仓位上限这次算不出来:{lim.get('why')}"
    extra = "".join(f";{x}" for x in (lim.get("note"), lim.get("detail")) if x)
    if over:
        return (f"仓位超限:现在占净资产 {over['now_pct']:.1f}%,{over['label']},"
                f"减到上限至少卖 {over['shares']} 股,这一条不看预测{extra}")
    return f"没超仓位上限:现在占净资产 {lim['now_pct']:.1f}%,{lim['label']}{extra}"


def _trim(c: str, held: int, p: float, part: float, ctx: dict, lim: dict | None = None) -> dict:
    """还款分到的钱和超出仓位上限的部分各算一个股数(向上取到 100 股、不超过持股),取大的;0 股就 hold。
    动作里另记 over_limit、repay_shares、limit_known(True 算出来了 / False 这次算不出来 / None 没算),
    页面那一行「为什么」靠它们写,不去拆「为什么」的原句。"""
    line = ctx["lines"].get(c)
    repay = _lots_up(part, p, held)
    over = _over_limit(held, p, lim)
    shares = max(repay, over["shares"] if over else 0)
    pos = _limit_text(lim, over)
    extra = {"over_limit": over, "repay_shares": repay, "limit_known": None if lim is None else bool(lim.get("ok"))}
    if shares <= 0:
        why = (ctx["hold_why"] if ctx["need"] < CENT
               else f"{ctx['split_txt']},排在前面的已经卖够,这个月不从它卖" if ctx["ordered"]
               else "卖出比例里没有这只,这个月不从它卖")
        return _act("hold", why + (f";{pos}" if pos else ""), change_if=_held_change_if(line, held), **extra)
    repay_why = (f"{ctx['head']},还差 {ctx['need']:,.2f};{ctx['split_txt']},这只分 {part:,.2f},"
                 f"按收盘 {p:.2f} 向上取到 {repay} 股")
    if over and repay:
        why = f"{pos};还款也要这只卖:{repay_why}。取大的 {shares} 股"
    elif over:
        why = pos + (f";{ctx['hold_why']}" if ctx["need"] < CENT else "")
    else:
        why = repay_why + (f";{pos}" if pos else "")
    why += ",已是全部持股" if shares == held else ""
    return _act("trim", why, shares=shares, amount=round(shares * p, 2), deadline=ctx["deadline"],
                change_if=_held_change_if(line, held), **extra)


MONTH_WORD = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}


def _cash_months(rules: dict) -> int:
    n = rules.get("cash_months", 1)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"配置 cash_months 要是正整数(现金留够几个月的还款),现在是 {n!r}")
    return n


def _sell_order(rules: dict) -> list[str] | None:
    o = rules.get("sell_order")
    if o is None:
        return None
    if not isinstance(o, (list, tuple)) or not all(isinstance(c, str) and c for c in o):
        raise ValueError(f"配置 sell_order 要是代码列表(先卖的在前),现在是 {o!r}")
    return list(o)


def cover(state: dict, rules: dict, t: date) -> dict:
    """要留的现金:{"months", "need", "from", "until", "notes"}。months 是实际算进去的月数。
    cash_months = 1(或没配)→ 只看这个月,照旧用 month_need / need_month(这个月付完了就是下个月)。
    多于 1 → 从今天这个月起 schedule 前 cash_months 项相加;排期是空的退回 month_need,排期不够就按排到的算。"""
    want = _cash_months(rules)
    month = state.get("need_month") or f"{t:%Y-%m}"
    one = {"months": 1, "need": float(state["month_need"]), "from": month, "until": month, "notes": []}
    if want == 1:
        return one
    sched = [(str(m), float(a)) for m, a in (state.get("schedule") or [])][:want]
    if not sched:
        return {**one, "notes": [f"配置要留够 {want} 个月的还款,但借款排期是空的,只按这个月算"]}
    notes = [] if len(sched) == want else [f"配置要留够 {want} 个月的还款,借款排期只排到 {sched[-1][0]},按排到的算"]
    return {"months": len(sched), "need": round(sum(a for _, a in sched), 2), "from": sched[0][0],
            "until": sched[-1][0], "notes": notes}


def _cover_head(cv: dict, cash: float) -> str:
    n = cv["months"]
    if n == 1:
        return f"{cv['from']} 要还 {cv['need']:,.2f},可取现金 {cash:,.2f}"
    return (f"{cv['from']}~{cv['until']} {MONTH_WORD.get(n, str(n))}个月要还 {cv['need']:,.2f}"
            f"(现金要留够这么多),可取现金 {cash:,.2f}")


def _parts(eligible: list[str], need: float, rules: dict, px: dict, held: dict) -> dict[str, float]:
    """要卖的钱分到各只。配了 sell_order:按顺序,前面那只按向上取整后的股数卖到够或卖光,剩下的给下一只;
    没写进顺序的排在最后。没配:按 sell_ratio 比例。"""
    order = _sell_order(rules)
    if order is None:
        return {c: need * w for c, w in _weights(eligible, rules.get("sell_ratio") or {}).items()}
    seq = [c for c in order if c in eligible] + [c for c in eligible if c not in order]
    out, left = {}, need
    for c in seq:
        if left < CENT or not _price_ok(px.get(c)):
            out[c] = 0.0
            continue
        part = min(left, held[c] * px[c])
        out[c] = part
        left = round(left - _lots_up(part, px[c], held[c]) * px[c], 2)
    return out


def _held_ctx(state: dict, rules: dict, t: date, proceeds: float, eligible: list[str], deadline: str) -> dict:
    cv = cover(state, rules, t)
    cash, need_total = float(state["cash"]), cv["need"]
    month = cv["from"]
    head = _cover_head(cv, cash)
    if proceeds:
        head += f",跌破卖出线那只全卖约 {proceeds:,.2f} 先顶上"
    if round(need_total - cash, 2) < CENT:
        hold_why = f"{head},够了,这个月不用卖" if cv["months"] == 1 else f"{head},够了,现在不用卖"
    else:
        hold_why = f"{head},够这个月还款,这只这个月不用卖"
        if _margin_note(state):     # 全卖的钱可能先还融资,取不出这么多
            hold_why += "(有融资负债,全卖的钱可能先拿去还融资,够不够以 App 里的可取现金为准)"
    ratio, order = rules.get("sell_ratio") or {}, _sell_order(rules)
    if len(eligible) <= 1:
        split_txt = "只剩这一只能卖,全由它出"
    elif order is not None:
        seq = [c for c in order if c in eligible] + [c for c in eligible if c not in order]
        split_txt = f"按卖出顺序 {'→'.join(seq)}(前面的卖够了就不动后面的)"
    else:
        split_txt = f"按卖出比例 {':'.join(_num(ratio.get(c) or 0) for c in eligible)}({':'.join(eligible)})"
    need = round(max(0.0, need_total - cash - proceeds), 2)
    return {"head": head, "month": month, "hold_why": hold_why, "need": need if need >= CENT else 0.0,
            "split_txt": split_txt, "ordered": order is not None, "cover": cv,
            "deadline": deadline, "lines": rules.get("sell_lines") or {}}


# ── 手里没有的 ──────────────────────────────────────────────────────────────
def _zones(ai_stock) -> list[list]:
    buy = ai_stock.get("buy") if isinstance(ai_stock, dict) else None
    zones = buy.get("zones") if isinstance(buy, dict) else None
    good = [z for z in zones or [] if isinstance(z, (list, tuple)) and len(z) == 2 and _price_ok(z[0])]
    return sorted(good, key=lambda z: -z[0])


def _flat(fc, until: str, share: float, zones: list, base: date, hol) -> dict:
    head = "手里没有这只。"
    if fc is None:
        return _act("wait", head + "借款数据读不出来,算不了闲钱,先等")
    if fc <= 0:
        why = head + f"按还款计划到 {until} 为止没有闲钱,买就等于动用还款的钱(闲钱 {fc:,.2f})"
        if zones:
            why += ";Claude 给的买点照样列着,用来检验预测"
        return _act("wait", why, change_if=[{"if": f"可取现金多过往后 {account.FREE_CASH_MONTHS} 个月要还的钱",
                                              "then": "按买点分批买"}])
    if not zones:
        return _act("wait", head + f"有闲钱 {fc:,.2f},但 Claude 答卷里没有买入价位,先等")
    budget = round(fc * share, 2)
    per = round(budget / len(zones), 2)
    batches = [{"price": float(z[0]), "amount": per, "prob": z[1]} for z in zones]
    why = (head + f"不影响还款的闲钱 {fc:,.2f},这只分到 {budget:,.2f};按 Claude 给的 {len(zones)} 个价位"
           f"分 {len(zones)} 批,每批约 {per:,.2f},价高的先买")
    window_end = price_mod.future_trading_days(base, BUY_DAYS, set(hol))[-1].isoformat()   # base 算第 1 个
    return _act("buy_batches", why, amount=budget, deadline=window_end,
                change_if=[{"if": "持仓或借款看板的数变了", "then": "重新算能用多少钱"}], batches=batches)


# ── 合起来 ──────────────────────────────────────────────────────────────────
def _rule_plan(state: dict, px: dict, rules: dict, t: date, hol, ai_stocks: dict,
               limits: dict | None, base: date) -> tuple[dict, dict]:
    """t = 今天(还款按哪个月算);base = 截止日从哪天数(这次存档的起点日)。"""
    if state.get("month_need") is None or state.get("cash") is None:
        raise ValueError("持仓没标过期,却缺 month_need 或 cash")
    held = {c: int(((state.get("holdings") or {}).get(c) or {}).get("shares") or 0) for c in px}
    lines, sell_dl = rules.get("sell_lines") or {}, deadline_after(base, SELL_DEADLINE_DAYS, hol)
    acts = {c: _sell_all(held[c], px[c], lines[c], sell_dl) for c in px
            if held[c] > 0 and _price_ok(px[c]) and lines.get(c) is not None and px[c] < lines[c]}
    proceeds = round(sum(a["amount"] for a in acts.values()), 2)
    eligible = [c for c in px if c not in acts and held[c] > 0]
    ctx = _held_ctx(state, rules, t, proceeds, eligible, sell_dl)
    parts = _parts(eligible, ctx["need"], rules, px, held)
    for c in eligible:
        lim = None if limits is None else limits.get(c, {"ok": False, "why": "这只没算上限"})
        acts[c] = (_trim(c, held[c], px[c], parts[c], ctx, lim) if _price_ok(px[c])
                   else _act("hold", f"取不到 {c} 的收盘价,这次算不了卖多少"))
    flat = [c for c in px if c not in acts]
    fc = state["free_cash"] if "free_cash" in state else account.free_cash(state["cash"], state.get("schedule") or [])
    until = state.get("free_cash_until") or _ym_after(t, account.FREE_CASH_MONTHS - 1)
    shares_of_fc = _weights(flat, rules.get("sell_ratio") or {})
    for c in flat:
        acts[c] = _flat(fc, until, shares_of_fc[c], _zones(ai_stocks.get(c)), base, hol)
    return acts, _account_out(state, acts, ctx, proceeds, fc, until)


def _account_out(state: dict, acts: dict, ctx: dict, proceeds: float, fc, until: str) -> dict:
    cv = ctx["cover"]
    cash, need_total = float(state["cash"]), cv["need"]
    sell_need = round(max(0.0, need_total - cash), 2)
    sold = sum(a["amount"] for a in acts.values() if a["kind"] in ("sell", "trim"))
    notes = [_cover_head(cv, cash).replace("(现金要留够这么多)", "")
             + (f",要卖 {sell_need:,.2f}" if sell_need > 0 else ",够了,不用卖")] + cv["notes"]
    if proceeds:
        notes.append(f"跌破卖出线的全卖约 {proceeds:,.2f}" + (f",还差 {ctx['need']:,.2f} 由其余几只卖"
                                                           if ctx["need"] >= CENT else ",够这个月还款"))
    short = sell_need - sold
    if short >= CENT:
        notes.append(f"能卖的持股卖完还差 {short:,.2f}")
    notes += _margin_note(state) + list(state.get("notes") or [])
    return {"month_need": state["month_need"], "need_month": state.get("need_month"), "cash": cash,
            "sell_need": round(sell_need, 2), "sell_total": round(sold, 2), "free_cash": fc,
            "free_cash_until": until, "cover_months": cv["months"], "cover_need": cv["need"],
            "cover_from": cv["from"], "cover_until": cv["until"], "note": ";".join(notes)}


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        return None


def _since(prev, kind: str, day0: date) -> date:
    """这种动作从哪天起:上次记的种类一样就沿用上次的 since,否则(没记过、换了种类、记录坏了)是 day0。"""
    old = _day(prev.get("since")) if isinstance(prev, dict) and prev.get("kind") == kind else None
    return old or day0


def plan_actions(state: dict, prices: dict, rules: dict, ai: dict | None, today: str,
                 limits: dict | None = None, start: str | None = None, seen: dict | None = None) -> dict:
    """账户层动作。prices = {代码: 收盘价},决定给哪几只出动作;rules 一般直接传 load_config()
    (要 sell_lines、sell_ratio,节假日从 holidays / holidays_<年> 取);ai = Claude 答卷(可 None)。
    limits = {代码: position_limit.limit_for 的结果};看板一律传。None 只给不管上限的老调用和检查用,
    这时不比上限、「为什么」也不提上限。
    start = 这次存档的起点日 'YYYY-MM-DD';不给就用 today。
    seen  = {代码: {"kind", "since"}},上次画页面时记的(只给同一次存档的);截止日怎么用它见模块说明。

    返回 {"account": {"month_need", "sell_total", "free_cash", "note", …},
          "<代码>": {"kind", "shares", "amount", "deadline", "since", "overdue", "why", "change_if", "rule_conflict"}}
    since 是这种动作从哪天起(今天不是交易日就算下一个交易日),调用方原样记下来,下次传回 seen。
    规则见模块说明。不改传进来的任何东西。"""
    t = date.fromisoformat(today)
    base = date.fromisoformat(start) if start else t
    rules = rules or {}
    hol = config_holidays(rules)
    px = {str(c): p for c, p in (prices or {}).items()}
    ai_stocks = _ai_stocks(ai)
    stale = is_stale(state, t)
    if stale:
        acts, acc = _stale_plan(state, list(px), t)
    else:
        acts, acc = _rule_plan(state, px, rules, t, hol, ai_stocks, limits, base)
    out = {"account": acc}
    day0 = price_mod.future_trading_days(t, 1, set(hol))[0]       # 今天;不是交易日就算下一个交易日
    for c in px:
        a = acts[c]
        since = _since((seen or {}).get(c), a["kind"], day0)
        if a["kind"] in ("sell", "trim"):
            a = {**a, "deadline": deadline_after(max(base, since), SELL_DEADLINE_DAYS, hol)}
        dl = a.get("deadline")
        # 持仓过期时动作是「先更新持仓」,不是规则算出来的,AI 说什么都谈不上不同意规则
        out[c] = {**a, "since": since.isoformat(), "overdue": bool(dl) and dl < today[:10],
                  "rule_conflict": None if stale else _conflict(a["kind"], ai_stocks.get(c))}
    return out
