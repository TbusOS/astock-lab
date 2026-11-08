#!/usr/bin/env python3
"""position_limit —— 每只持仓的仓位上限(占净资产的百分比),给 actions.py 用。

它是什么:「仓位上限」是账户层动作的输入之一,而且「超限优先:不论预测多好,先减到上限」
(forecast-from-facts 的 decision.py 原话)。这个文件只负责把上限算出来、写清从哪来;
「超了要卖多少」在 actions.py。

没有它会怎样:看板只按还款卖。一只票占了净资产的一倍半,只要这个月还款的钱够,页面照样写
「持有」—— 而 user 自己定的规则是「超过上限先减到上限,这一条不看预测」。

上限的算法只有一份:forecast-from-facts/scripts/decision.position_cap
(上限% = 单票可承受亏损% ÷ |bear 情景跌幅%| × 确信度系数,再和单票硬上限取小的)。这里不另写一份。
看板不跑那条三情景的管线,手里没有这次的 bear 跌幅和确信度,position_cap 在这里多半算不出来;
算不出来时只用 account.json 里的单票硬上限(position_cap 读的就是这个键)。
为什么硬线可以单独用:完整规则的上限 = min(bear 倒推, 硬线) ≤ 硬线,所以只用硬线卖的
不会比 user 自己的规则多;它只是少卖了 bear 倒推那一截,页面上写明「只用了硬线」。
**现在正式运行时 bear 倒推那条路还没接上**:board_data 调 limits() 不传三情景和确信度,
所以页面上的上限都是账户设置的那个;limit_for 收 sc / conf,是给以后接上三情景时用的(检查里用合成数测过)。
页面第一屏上的字不写「硬线」「bear 倒推」「journal」,写「账户设置的 N% 上限」「按最坏情景倒推的上限」。

输入:
  <工作台>/private/portfolio/account.json   _单票最大可承受亏损_pct_of_净资产、_单票硬上限_pct_of_净资产、
                                            _硬上限_改动理由(定的日子从这句里取,没有就不写日子)
  借款看板最新一笔的净资产(account.account_state 的 net_assets),按现在的收盘重算
  <工作台>/data/journal.jsonl              这只最近一条带 bear 价的记录,只用来写「为什么算不出来」
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_FFF = Path(__file__).resolve().parents[2] / "forecast-from-facts" / "scripts"
if str(_FFF) not in sys.path:
    sys.path.insert(0, str(_FFF))
import decision  # noqa: E402  上限的算法在这里,只有这一份

ACCOUNT_REL = Path("private") / "portfolio" / "account.json"
TOL_KEY = "_单票最大可承受亏损_pct_of_净资产"
HARD_KEY = "_单票硬上限_pct_of_净资产"
HARD_WHY_KEY = "_硬上限_改动理由"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_account_limits(path: Path) -> dict:
    """account.json → {"tol", "hard", "set_on", "error"}。读不了时 error 写原因,其余为 None。"""
    try:
        j = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"tol": None, "hard": None, "set_on": None, "error": f"内部仓里没有 {ACCOUNT_REL}"}
    except (OSError, ValueError) as e:
        return {"tol": None, "hard": None, "set_on": None, "error": f"{ACCOUNT_REL} 读不了({type(e).__name__})"}
    m = _DATE.search(str(j.get(HARD_WHY_KEY) or ""))
    return {"tol": j.get(TOL_KEY), "hard": j.get(HARD_KEY), "set_on": m.group(0) if m else None, "error": None}


def net_assets_now(state: dict, px: dict) -> tuple[float | None, str]:
    """借款看板那一笔的净资产,按现在的收盘重算 → (净资产, 说明)。
    净资产 + Σ 股数 × (现在收盘 − 那一笔的截图价),只重算看板有收盘价、看板那一笔有截图价的几只;
    其余的(例:借款看板里还有一只不在看板的股票清单里)按那一笔的价算,说明里写出是哪几只。
    看板那一笔没有净资产 → (None, 原因)。"""
    base = (state or {}).get("net_assets")
    if not isinstance(base, (int, float)):
        return None, "借款看板那一笔没有净资产"
    out, kept = float(base), []
    for c, h in sorted(((state or {}).get("holdings") or {}).items()):
        shares, then = h.get("shares") or 0, h.get("price")
        if not shares:
            continue
        if isinstance(then, (int, float)) and isinstance(px.get(c), (int, float)):
            out += shares * (float(px[c]) - float(then))
        else:
            kept.append(c)
    note = f"净资产里 {'、'.join(kept)} 没有现在的收盘价或截图价,按借款看板那一笔的价算" if kept else ""
    return round(out, 2), note


def last_bear(rows: list[dict], code: str) -> dict | None:
    """journal 里这只最近一条带最坏情景价(scenario_px.bear)的记录 → {"id", "date", "bear"}。"""
    got = [r for r in rows or [] if str(r.get("code")) == code
           and isinstance((r.get("scenario_px") or {}).get("bear"), (int, float))]
    if not got:
        return None
    r = max(got, key=lambda x: str(x.get("logged_at") or ""))
    return {"id": r.get("id"), "date": str(r.get("logged_at") or "")[:10], "bear": float(r["scenario_px"]["bear"])}


def bear_why(last: dict | None, close: float | None) -> str:
    """按最坏情景倒推的上限为什么算不出来:一句平铺的话,第一屏「本次缺什么」用(不写 journal 编号)。"""
    return "看板没有这次的最坏情景价和确信度"


def bear_detail(last: dict | None, close: float | None) -> str:
    """细节:journal 里最近一次的最坏情景价是哪条、哪天、多少,是否已高于现价。只放进折叠的完整理由。"""
    if not last:
        return "journal 里也没有这只的最坏情景价"
    said = f"journal 最近一次的最坏情景价是 #{last['id']},{last['date']} 的 {last['bear']:g}"
    if isinstance(close, (int, float)) and last["bear"] >= close:
        return f"{said},已经高于现价 {close:g}"
    return f"{said},没有确信度"


def _label(pct: float, cfg: dict, basis: str) -> str:
    if basis == "bear 倒推":                  # decision.position_cap 的「起作用的」
        return f"仓位上限 {pct:.1f}%(按最坏情景倒推)"
    when = f",{cfg['set_on']} 定" if cfg.get("set_on") else ""
    return f"仓位上限 {pct:g}%(账户设置{when})"


def limit_for(mv: float, equity: float | None, cfg: dict, why_no_bear: str, sc: dict | None = None,
              conf: dict | None = None, detail: str = "") -> dict:
    """一只的上限。ok → {"limit_pct", "now_pct", "limit_amount", "equity", "label", "note", "detail"};
    算不出来 → {"ok": False, "why"}。
    sc / conf 是 decision.py 的三情景和确信度,有就交给 position_cap。**正式运行时还没有接上**:
    看板不跑那条管线,board_data 调这里时 sc / conf 都不传,所以总是走「只用账户设置的上限」那条路;
    检查里传了合成的 sc / conf,确认算得出来时用的是 position_cap 的上限。
    note 是给「本次缺什么」和「为什么」的一句平铺的话;detail 是只放进折叠理由里的细节。"""
    if cfg.get("error"):
        return {"ok": False, "why": cfg["error"]}
    if not isinstance(equity, (int, float)) or equity <= 0:
        return {"ok": False, "why": "借款看板没有净资产或不是正数,算不了占比"}
    acct = {"单票最大可承受亏损_pct": cfg.get("tol"), "单票硬上限_pct": cfg.get("hard"), "净资产": equity}
    full = decision.position_cap(sc or {"ok": False, "why": why_no_bear}, conf or {}, acct)
    # 没给三情景时 position_cap 只是把原因包一层(「三情景算不出来(…)」),直接写原因,不套括号
    reason = why_no_bear if sc is None and cfg.get("tol") is not None else full.get("why")
    if full.get("ok"):
        pct, basis, note = float(full["上限pct"]), full.get("起作用的") or "", ""
    elif cfg.get("hard") is None:
        return {"ok": False, "why": f"按最坏情景倒推的上限这次算不出来:{reason};account.json 里也没有单票硬上限 {HARD_KEY}"}
    else:
        pct, basis = float(cfg["hard"]), "硬线"
        note = f"只用了账户设置的 {pct:g}% 上限;按最坏情景倒推的上限这次算不出来:{reason}"
    return {"ok": True, "limit_pct": pct, "now_pct": mv / equity * 100, "limit_amount": equity * pct / 100,
            "equity": equity, "label": _label(pct, cfg, basis), "note": note, "detail": detail if note else ""}


def limits(state: dict, px: dict, cfg: dict, journal_rows: list[dict]) -> dict:
    """{代码: limit_for 的结果},只算手里有的那几只(手里没有的不存在超限)。
    按最坏情景倒推的那条路还没接上(sc / conf 不传,见 limit_for),所以这里给的都是账户设置的上限。
    净资产只重算了一部分时,说明放在 {"account": …} 这一项里,missing_rows 写成账户那一行。"""
    equity, enote = net_assets_now(state, px)
    out = {}
    for c, h in ((state or {}).get("holdings") or {}).items():
        shares, p = h.get("shares") or 0, px.get(c)
        if shares and isinstance(p, (int, float)):
            last = last_bear(journal_rows, c)
            out[c] = limit_for(shares * p, equity, cfg, bear_why(last, p), detail=bear_detail(last, p))
    if enote and equity is not None:
        out["account"] = {"ok": True, "equity_note": enote}
    return out


def missing_rows(lims: dict) -> list[dict]:
    """「本次缺什么」里的行:上限这次算不出来,或者只用了账户设置的上限;净资产只重算了一部分。
    key 带代码的页面分到那一只,limit.account 每只都显示。都是一句平铺的话,不写 journal 细节。"""
    out = []
    for c, lim in sorted(lims.items()):
        if c == "account":
            out.append({"key": "limit.account", "kind": "limit", "text": lim["equity_note"]})
        elif not lim.get("ok"):
            out.append({"key": f"limit.{c}", "kind": "limit", "text": f"仓位上限这次算不出来:{lim.get('why')}"})
        elif lim.get("note"):
            out.append({"key": f"limit.{c}", "kind": "limit", "text": lim["note"]})
    return out
