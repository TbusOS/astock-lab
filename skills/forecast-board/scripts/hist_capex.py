#!/usr/bin/env python3
"""hist_capex —— 还原「当时能看到的」北美四大云厂资本开支,给历史补考用。

它是什么:微软、谷歌、亚马逊、Meta 每季资本开支(现金购建 + 融资租赁)的四家合计,
按「哪天公开」排好。每条记录 = 某天新披露了一份文件以后,当时能算出的最近一季合计、
合计同比、上一季合计同比。
没有它会怎样:补考要回答「站在 2019 年某天,云厂开支在加速还是减速」。今天从 SEC
拉下来的数已经被后来的 10-K 改过,季度也比当时多;直接拿来用,等于提前知道了答案。
为什么这么设计:
- SEC companyconcept 每条数都带 filed(哪天交的文件)。某季的数从**第一次披露它的
  文件**起才算知道;后来的文件改了数,从改的那份起替换(同一 start/end 取 filed 最晚、
  但不晚于当天的那条)。
- known = filed 的**第二天**。filed 是美国东部时间的日期,那天 A 股早已收盘;
  当天收盘出题用不到它。
- 折单季、选标签、四家对齐都直接调 capex.py 的函数(_to_quarters / combined_rows /
  TAG_CANDIDATES / LEASE_TAGS),和线上同一套算法,不另写一份。
- 线上信号只看含融资租赁的合计同比。早年微软、Meta 还没有融资租赁这个标签,含租赁的
  同比算不出来;这时两季都改用现金购建的同比算加速度(两季口径必须一样,混用会把口径
  切换当成加速)。记录里 basis 写明用的哪个。
什么场景用:hist 补考按月底出题时,signal_at(table, "sector", 出题日) 给云厂开支那一票。
机制:build_table 把四家所有 filed 日期排一遍,每个日期按「filed ≤ 那天」重建四家单季,
跑 combined_rows,最近一季有变化才记一条。

用法:
    STOCK_LAB=<内部仓> python3 hist_capex.py --fetch --raw <原始数据.json>      在 mini 上取 SEC
    python3 hist_capex.py --raw <原始数据.json> --out <表.json>                本机重建表
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # noqa: F401  先于 inputs(取数脚本会删代理变量)
import inputs
import system_forecast as sf



def _load_capex():
    """按路径加载 data-sources 里的 capex.py(inputs._module 的表里没有它,这里不去改 inputs.py)。"""
    import importlib.util
    path = inputs.FETCH_DIR / "capex.py"
    spec = importlib.util.spec_from_file_location("fb_capex", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise ImportError(f"找不到 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


capex = _load_capex()

TICKERS = ("MSFT", "GOOGL", "AMZN", "META")
# 补考多取一个标签:亚马逊 2017-07 ~ 2018-07 交的文件用的是它(线上 capex.py 不需要,那几年早过去了)
EXTRA_TAGS = ("PaymentsForProceedsFromProductiveAssets",)
START = "2015-01-01"            # 早于这天交的文件不用(要的是 2017 起的季度做 2018 起的同比基期)
MAX_AGE_DAYS = 200              # 最近一条比出题日早这么多天还没更新 → 当成缺数,不给信号
KEY = "sector"

RULE = ("线上 _sig_capex:最近两季四家合计同比之差,≥ CAPEX_GAP 个百分点 +1、≤ −CAPEX_GAP −1、中间 0。"
        "补考改动:① 现金购建合并三个标签(capex.py 的两个 + PaymentsForProceedsFromProductiveAssets),"
        "同一期取当天以前最晚披露的那个数 —— 亚马逊 2017-2018 年换过两次标签,还改过口径(2017Q2 先报 25.01 亿、"
        "一年后的对比数改成 31.13 亿),只选一个标签会断档。② 含融资租赁的同比两季都有时用它(basis=total);否则两季都用现金购建的同比"
        "(basis=cash,早年微软 / Meta 没有融资租赁标签);两种都凑不齐不给信号。"
        f"最近一条比出题日早 {MAX_AGE_DAYS} 天以上不给信号。8-K 里的开支指引还原不了,不在信号里。")
KNOWN_RULE = "known = 披露这一季的 SEC 文件 filed 日 + 1 天(filed 是美东日期,当天 A 股已收盘)"


# ── 原始数据 → 每家「某天为止」的单季 ─────────────────────────────────────────
def facts(concept: dict) -> list[dict]:
    """companyconcept JSON → [{start, end, val, filed, form, accn}],缺字段的丢掉。"""
    out = []
    for x in (concept or {}).get("units", {}).get("USD", []):
        s, e, v, f = x.get("start"), x.get("end"), x.get("val"), x.get("filed")
        if s and e and f and v is not None:
            out.append({"start": s, "end": e, "val": float(v), "filed": f,
                        "form": x.get("form"), "accn": x.get("accn")})
    return out


def vintage(fs: list[dict], asof: str) -> list[tuple[str, str, float]]:
    """filed ≤ asof 的数;同一 (start, end) 取 filed 最晚的那条(后来改过的数替换旧的)。"""
    best = {}
    for x in fs:
        if x["filed"] > asof:
            continue
        k = (x["start"], x["end"])
        if k not in best or x["filed"] > best[k]["filed"]:
            best[k] = x
    return [(s, e, best[(s, e)]["val"]) for s, e in sorted(best)]


def cash_tags() -> tuple[str, ...]:
    return tuple(capex.TAG_CANDIDATES) + EXTRA_TAGS


def company_at(raw_tk: dict, ticker: str, asof: str) -> dict | None:
    """一家公司在 asof 那天能看到的单季现金购建 + 融资租赁。
    现金购建:几个候选标签的数合在一起,同一 (start, end) 取 filed 最晚的(见 RULE ①)。
    这家这天还一个数都没有 → None。融资租赁标签那天还没有数 → lease 空、lease_tag 照填,
    combined_rows 会把这些季度记成缺口(含租赁的同比置空)。"""
    fs = [x for tag in cash_tags() for x in facts(raw_tk.get(tag))]
    q = capex._to_quarters(vintage(fs, asof))
    if not q:
        return None
    ltag = capex.LEASE_TAGS.get(ticker)
    lease = capex._to_quarters(vintage(facts(raw_tk.get(ltag)), asof)) if ltag else {}
    return {"q": q, "latest": max(q), "lease": lease, "lease_tag": ltag}


def _cal_q(end: str) -> str:
    d = date.fromisoformat(end)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _accel(last: dict, prev: dict) -> tuple[float | None, str | None]:
    """两季同比之差和口径:优先含租赁的合计,凑不齐用现金购建,两季口径一样。"""
    for key, basis in (("yoy_total", "total"), ("yoy_cash", "cash")):
        if last.get(key) is not None and prev.get(key) is not None:
            return last[key] - prev[key], basis
    return None, None


def _record(f: str, rows: list[dict], trigger: list[dict]) -> dict:
    last, prev = rows[-1], (rows[-2] if len(rows) > 1 else {})
    acc, basis = _accel(last, prev) if prev else (None, None)
    r1 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {"known": (date.fromisoformat(f) + timedelta(days=1)).isoformat(), "filed": f,
            "q": last["q"], "cal_q": _cal_q(last["q"]),
            "sum_total_b": capex._b(last["sum_total"]), "sum_cash_b": capex._b(last["sum_cash"]),
            "yoy_total": r1(last["yoy_total"]), "yoy_cash": r1(last["yoy_cash"]),
            "prev_q": prev.get("q"), "prev_yoy_total": r1(prev.get("yoy_total")),
            "prev_yoy_cash": r1(prev.get("yoy_cash")), "accel": r1(acc), "basis": basis,
            "lease_gaps": list(last["gaps"]),
            "by": {tk: capex._b(v["total"]) for tk, v in last["by"].items()},
            "trigger": trigger}


def build_table(raw: dict, start: str = START) -> dict:
    """raw = {"concepts": {ticker: {tag: companyconcept JSON}}} → 历史表。"""
    con = raw["concepts"]
    filed = {}
    for tk in TICKERS:
        for tag, js in (con.get(tk) or {}).items():
            for x in facts(js):
                if x["filed"] >= start:
                    filed.setdefault(x["filed"], set()).add((tk, x["form"], x["accn"]))
    recs, last_sig = [], None
    for f in sorted(filed):
        allq = {}
        for tk in TICKERS:
            c = company_at(con.get(tk) or {}, tk, f)
            if c is None:
                break
            allq[tk] = c
        if len(allq) != len(TICKERS):
            continue
        rows = capex.combined_rows(allq, 400)
        if not rows:
            continue
        rec = _record(f, rows, [{"ticker": t, "form": fm, "accn": a} for t, fm, a in sorted(filed[f])])
        sig = (rec["q"], rec["sum_total_b"], rec["yoy_total"], rec["yoy_cash"], rec["accel"])
        if sig != last_sig:
            recs.append(rec)
            last_sig = sig
    gaps = sorted({f"{r['cal_q']} {tk} 融资租赁缺" for r in recs for tk in r["lease_gaps"]})
    return {"meta": {"name": "capex", "built_at": datetime.now().isoformat(timespec="seconds"),
                     "source": "SEC EDGAR XBRL companyconcept(" + "、".join(TICKERS) + ");标签同 capex.py",
                     "rule": RULE, "known_rule": KNOWN_RULE, "gaps": gaps,
                     "notes": "sum_* 单位十亿美元;yoy_* 百分数;accel 百分点;trigger = 那天交的、带来新数的文件"},
            "keys": {KEY: recs}}


# ── 按日查 ───────────────────────────────────────────────────────────────────
def value_at(table: dict, key: str, day: str) -> dict | None:
    """known ≤ day 的最后一条;没有返回 None。"""
    recs = table["keys"].get(key) or []
    i = bisect.bisect_right([r["known"] for r in recs], day)
    return recs[i - 1] if i else None


def signal_at(table: dict, key: str, day: str) -> int | None:
    r = value_at(table, key, day)
    if r is None or r.get("accel") is None:
        return None
    if (date.fromisoformat(day) - date.fromisoformat(r["known"])).days > MAX_AGE_DAYS:
        return None
    return sf._band(r["accel"], -sf.CAPEX_GAP, sf.CAPEX_GAP)


# ── 取数(在 mini 上跑) ───────────────────────────────────────────────────────
def remote_script() -> str:
    """送到 mini 的取数脚本:四家的现金购建两个候选标签 + 融资租赁标签,打一行 JSON。"""
    tags = {tk: list(cash_tags()) + ([capex.LEASE_TAGS[tk]] if tk in capex.LEASE_TAGS else [])
            for tk in TICKERS}
    return f'''
import gzip, json, time, urllib.request
UA = {capex.UA!r}
TAGS = {json.dumps(tags)}
import urllib.error
def get(url, tries=6):
    for i in range(tries):
        time.sleep(0.3 + 2 ** i - 1)          # 0.3、1.3、3.3 … 秒,SEC 断连接时退一退再来
        try:
            req = urllib.request.Request(url, headers={{"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}})
            with urllib.request.urlopen(req, timeout=60) as f:
                raw = f.read()
                if f.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404 or i == tries - 1:      # 404 = 这家没用这个标签,重试也没用
                raise
        except Exception:
            if i == tries - 1:
                raise
t = get("https://www.sec.gov/files/company_tickers.json")
ciks = {{v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in t.values() if v["ticker"].upper() in TAGS}}
out = {{"ciks": ciks, "concepts": {{}}, "errors": []}}
for tk, tags in TAGS.items():
    out["concepts"][tk] = {{}}
    for tag in tags:
        try:
            out["concepts"][tk][tag] = get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{{ciks[tk]}}/us-gaap/{{tag}}.json")
        except Exception as e:
            out["errors"].append([tk, tag, type(e).__name__ + ": " + str(e)[:120]])
print(json.dumps(out))
'''


def fetch_raw(host: str = "mini") -> dict:
    p = inputs.events.ssh_python(remote_script(), [], host=host, env=fb_common.ORIG_ENV, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"mini 取 SEC 失败,退出码 {p.returncode}:{(p.stderr or '')[-300:]}")
    return inputs.events.last_json(p.stdout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="还原当时能看到的云厂资本开支")
    ap.add_argument("--raw", required=True, help="原始数据 JSON(--fetch 时写到这里,否则从这里读)")
    ap.add_argument("--fetch", action="store_true", help="先在 mini 上取 SEC 原始数据")
    ap.add_argument("--host", default="mini")
    ap.add_argument("--out", help="历史表写到哪")
    a = ap.parse_args(argv)
    if a.fetch:
        raw = fetch_raw(a.host)
        bad = [e for e in raw.get("errors") or [] if "404" not in e[2]]
        if bad:
            print(f"有标签没取到(不是 404):{bad}", file=sys.stderr)
        Path(a.raw).parent.mkdir(parents=True, exist_ok=True)
        Path(a.raw).write_text(json.dumps(raw), encoding="utf-8")
        print(f"原始数据写到 {a.raw};出错 {raw.get('errors')}")
    raw = json.loads(Path(a.raw).read_text(encoding="utf-8"))
    if a.out:
        t = build_table(raw)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
        recs = t["keys"][KEY]
        if not recs:
            print("一条都没算出来(四家里有没取到数的?看原始数据里的 errors)", file=sys.stderr)
            return 1
        print(f"{len(recs)} 条,known {recs[0]['known']} ~ {recs[-1]['known']},写到 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
