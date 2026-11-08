#!/usr/bin/env python3
"""hist_consensus —— 券商盈利预测上调 / 下调的「当时能看到的」历史表。

它是什么:把 2017 年以来每篇研报(发布日、券商、盈利预测、PDF 里读出的年份和归母净利润)
排成一张按发布日排序的表。给定过去任何一天 day,只拿那天及以前发布的研报,
交给线上同一套函数(inputs.eps_value → system_forecast._net_sign)算出那天的券商信号。

没有它会怎样:系统算法的券商信号只有今天的值,历史补考里只能记 0,
「券商上调盈利预测 → 股价跟着涨」这条想法就没法拿当时的信息去考。

为什么这样设计:
- 只按发布日过滤,不按研报里写的日期或预测的年份:发布日是市场第一次看到它的日子。
  线上 eps_revisions.summarize 数「近 30 天」时,发布日在「今天」之后的研报天数是负数,
  照样会被数进去 —— 所以过滤必须在交给它之前做(rows_at),检查脚本专门测这一点。
- 回看窗口和线上一样是 365 天(inputs.c_eps_revisions 只取近一年的研报),
  同一家券商一年以前的那篇不拿来比。
- 两种口径:
  live(默认):和线上完全一样,只用东财列表里带每股收益预测的研报,
    年份靠 eps_revisions.eps_year 拿列表里的数去 PDF 表里对出来。
    2021 年及以前的研报列表里大多没有预测数,这几年 live 口径下信号基本都是 0。
  pdf:另外把列表里没有预测数的研报也读进来,年份直接按 PDF 预测表的表头认
    (eps_revisions.profit_year,读不出就不用,不猜)。和线上不同,结果单独标出。
- PDF 原文件不放进内部仓的 data/(那里进 git),只缓存在本机缓存目录;
  读出的年份和净利润:live 口径并入线上用的 data/research/<代码>/eps_profit.json,
  pdf 口径另存 eps_profit_pdfonly.json,不影响线上。

什么场景用:thinking_test 逐条考想法时,在每个月底出题日调 signal_at;
往系统算法的历史补考里接券商信号时也用它。

用法(STOCK_LAB 指向内部仓;ASTOCK_CACHE 指向本机缓存目录,默认 ~/.cache/astock-lab):
    python3 hist_consensus.py lists --codes 300308,300502          按年取研报列表
    python3 hist_consensus.py pdfs  --codes 300308 [--mode live|pdf] 下载并读 PDF
    python3 hist_consensus.py build --codes 300308,300502 --out <consensus.json>
    python3 hist_consensus.py coverage --table <consensus.json>
检查:scripts/check_hist_consensus.py(不联网)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # noqa: F401  先于 prices(market.py 会删代理变量)
import inputs
import system_forecast as sf

LOOKBACK_DAYS = 365          # 跟 inputs.c_eps_revisions 一样只看近一年的研报
PAGE_CAP = 250               # research.fetch_list 默认最多 5 页 × 50 篇
FIRST_YEAR = 2017            # 2018 年初的出题日要往回看一年
PDF_YEARS = 3                # pdf 口径每篇读几年(发布那年起)
SLEEP = 1.0                  # 两次请求之间至少隔这么久(秒)
RETRIES = 3                  # 网络出错最多试几次
CACHE_NAME = "eps_profit.json"
PDFONLY_NAME = "eps_profit_pdfonly.json"
TABLE_FIELDS = ("known", "org", "info", "title", "rating", "rating_change", "eps", "fy", "profit", "src")


def cache_root() -> Path:
    return Path(os.environ.get("ASTOCK_CACHE") or Path.home() / ".cache" / "astock-lab")


def research_module():
    return inputs._module("research")


# ── 取列表 ──────────────────────────────────────────────────────────────────
def _segments(year: int, until: str) -> list[tuple[str, str]]:
    end = min(f"{year}-12-31", until)
    return [(f"{year}-01-01", end)] if f"{year}-01-01" <= end else []


def _halves(a: str, b: str) -> list[tuple[str, str]]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    mid = d0 + (d1 - d0) / 2
    return [(a, mid.isoformat()), ((mid + timedelta(days=1)).isoformat(), b)]


def _retry(fn, sleep: float, tries: int = RETRIES):
    """网络偶尔断开(SSL 连接被对方关掉、超时),隔一会儿再试;试满 tries 次还不行就抛出去。"""
    for k in range(tries):
        try:
            return fn()
        except (OSError, ValueError):
            if k == tries - 1:
                raise
            time.sleep(max(sleep, 1.0) * 5 * (k + 1))


def fetch_code(code: str, until: str, get_list, sleep: float = SLEEP, first_year: int = FIRST_YEAR) -> list[dict]:
    """一只股票 first_year 年起到 until 的全部研报(按 info 去重,新的在前)。
    一段取满 PAGE_CAP 篇说明可能被截断,对半再取,直到不满为止。"""
    todo = [s for y in range(first_year, int(until[:4]) + 1) for s in _segments(y, until)]
    got: dict[str, dict] = {}
    while todo:
        a, b = todo.pop(0)
        rows = _retry(lambda: get_list(code, a, b), sleep)
        time.sleep(sleep)
        if len(rows) >= PAGE_CAP and a != b:
            todo = _halves(a, b) + todo
            continue
        for r in rows:
            got.setdefault(r["info"], r)
    return sorted(got.values(), key=lambda r: (r["date"], r["info"]), reverse=True)


def list_path(code: str) -> Path:
    return cache_root() / "research-list" / f"{code}.json"


def load_list(code: str) -> list[dict]:
    return json.loads(list_path(code).read_text(encoding="utf-8"))


# ── 读 PDF ──────────────────────────────────────────────────────────────────
def pdf_path(info: str) -> Path:
    return cache_root() / "research-pdf" / f"{info}.pdf"


def make_fetch_pdf(research, sleep: float = SLEEP):
    """下载一篇 PDF:先看本机缓存,没有再下(下完隔 sleep 秒),存进缓存目录。"""
    def fetch(info: str) -> bytes:
        p = pdf_path(info)
        if p.is_file() and p.stat().st_size > 0:
            return p.read_bytes()
        b = research._get(research.PDF_URL.format(info), referer="https://data.eastmoney.com/",
                          timeout=40, binary=True)
        time.sleep(sleep)
        if not b.startswith(b"%PDF"):
            raise ValueError(f"{info} 下载回来的不是 PDF")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
        return b
    return fetch


def pdf_text(b: bytes) -> str:
    import pymupdf
    return "\n".join(pg.get_text() for pg in pymupdf.open(stream=b, filetype="pdf"))


def research_dir(code: str) -> Path:
    return research_module()._lab_root() / "data" / "research" / code


def read_pdf_only(text: str, year: int, er) -> dict:
    """pdf 口径:列表里没有预测数的研报,按 PDF 预测表表头读 year、year+1、year+2 的归母净利润。
    一年都读不出 → fy 记 None(不用这篇)。不拿列表数核股本,所以读错的风险比 live 口径大。"""
    prof = []
    for k in range(PDF_YEARS):
        try:
            prof.append(er.profit_year(text, year + k))
        except Exception:
            prof.append(None)
    return {"fy": year if any(p is not None for p in prof) else None, "profit": prof}


def resolve_pdf_only(rows: list[dict], cache_file: Path, fetch_pdf, text_of, er) -> list[dict]:
    """列表里没有预测数的研报 → {**r, fy, profit}(pdf 口径)。读过的存 cache_file,下载失败的不存。"""
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out = []
    for r in rows:
        if (r.get("eps") or [None])[0]:
            continue
        got = cache.get(r["info"])
        if not isinstance(got, dict):
            try:
                got = read_pdf_only(text_of(fetch_pdf(r["info"])), int(r["date"][:4]), er)
                cache = {**cache, r["info"]: got}
            except Exception:
                got = {"fy": None, "profit": []}
        out.append({**r, "fy": got.get("fy"), "profit": list(got.get("profit") or [])})
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


# ── 合表 ────────────────────────────────────────────────────────────────────
def to_record(r: dict, src: str) -> dict:
    return {"known": r["date"], "org": r.get("org", ""), "info": r["info"], "title": r.get("title", ""),
            "rating": r.get("rating", ""), "rating_change": r.get("chg"), "eps": list(r.get("eps") or []),
            "fy": r.get("fy"), "profit": list(r.get("profit") or []), "src": src}


def build_records(live_rows: list[dict], pdf_rows: list[dict]) -> list[dict]:
    """live 口径的(列表带预测数)和 pdf 口径的(没带)合成一张表,按发布日从旧到新。"""
    recs = [to_record(r, "api") for r in live_rows] + [to_record(r, "pdf") for r in pdf_rows]
    seen, out = set(), []
    for x in sorted(recs, key=lambda x: (x["known"], x["info"])):
        if x["info"] not in seen:
            seen.add(x["info"])
            out.append(x)
    return out


# ── 查某一天 ────────────────────────────────────────────────────────────────
def rows_at(table: dict, code: str, day: str, mode: str = "live", lookback: int = LOOKBACK_DAYS) -> list[dict] | None:
    """day 那天收盘后能看到的研报(发布日在 day−lookback 到 day 之间),换成线上 rows 的格式。
    这张表没有这只股票 → None。
    pdf 口径的研报没有每股收益,summarize 又只认每股收益那格不为空的点,所以把净利润放进
    eps 那格占位;比较时两篇都有净利润,classify_change 只比净利润,占位的数不参与。"""
    recs = (table.get("keys") or {}).get(code)
    if recs is None:
        return None
    lo = (date.fromisoformat(day) - timedelta(days=lookback)).isoformat()
    out = []
    for x in recs:
        if not lo <= x["known"] <= day:
            continue
        if x["src"] == "api":
            out.append({"date": x["known"], "org": x["org"], "info": x["info"], "eps": x["eps"],
                        "fy": x["fy"], "profit": x["profit"]})
        elif mode == "pdf" and x["fy"] is not None:
            out.append({"date": x["known"], "org": x["org"], "info": x["info"], "eps": list(x["profit"]),
                        "fy": x["fy"], "profit": x["profit"]})
    return out


def value_at(table: dict, code: str, day: str, mode: str = "live") -> dict | None:
    """那天的 {"up30", "down30", "fy", "eps_basis_skipped30", "n_reports", "n_usable"};没有这只股票 → None。"""
    rows = rows_at(table, code, day, mode)
    if rows is None:
        return None
    v = inputs.eps_value(rows, day)
    return {**v, "n_reports": len(rows), "n_usable": sum(1 for r in rows if r["fy"] is not None)}


def signal_at(table: dict, code: str, day: str, mode: str = "live") -> int | None:
    """那天的券商信号 −1 / 0 / +1:近 30 天按净利润比调高的家次多 → +1,调低多 → −1(同线上 _consensus)。"""
    v = value_at(table, code, day, mode)
    return None if v is None else sf._net_sign(v["up30"], v["down30"])


# ── 覆盖率 ──────────────────────────────────────────────────────────────────
def coverage(table: dict) -> dict:
    """每只股票按年:研报篇数、列表带预测数的、live 读出年份的、读出净利润的、pdf 口径读出的。"""
    out = {}
    for code, recs in (table.get("keys") or {}).items():
        by = {}
        for x in recs:
            y = x["known"][:4]
            c = by.setdefault(y, {"n": 0, "api": 0, "api_fy": 0, "api_profit": 0, "pdf": 0, "pdf_fy": 0})
            c["n"] += 1
            if x["src"] == "api":
                c["api"] += 1
                c["api_fy"] += x["fy"] is not None
                c["api_profit"] += any(p is not None for p in x["profit"])
            else:
                c["pdf"] += 1
                c["pdf_fy"] += x["fy"] is not None
        out[code] = {"years": by, "first": recs[0]["known"] if recs else None,
                     "last": recs[-1]["known"] if recs else None}
    return out


# ── 命令行 ──────────────────────────────────────────────────────────────────
def cmd_lists(codes, until):
    research = research_module()
    for c in codes:
        p = list_path(c)
        if p.is_file():
            print(f"{c} 列表已有,跳过(删掉 {p.name} 重取)")
            continue
        rows = fetch_code(c, until, research.fetch_list)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"{c} {len(rows)} 篇,带预测数 {sum(1 for r in rows if (r['eps'] or [None])[0])}", flush=True)


def cmd_pdfs(codes, mode):
    research, er = research_module(), inputs.eps_revisions_module()
    fetch = make_fetch_pdf(research)
    for c in codes:
        rows = load_list(c)
        got = er.resolve(rows, research_dir(c), fetch, pdf_text)          # live:写线上同一个缓存
        n_api = sum(1 for r in got if (r.get("eps") or [None])[0])
        msg = f"{c} live:带预测数 {n_api} 篇,读出年份 {sum(1 for r in got if r.get('fy') is not None)}"
        if mode == "pdf":
            po = resolve_pdf_only(rows, research_dir(c) / PDFONLY_NAME, fetch, pdf_text, er)
            msg += f";pdf 口径 {len(po)} 篇,读出 {sum(1 for r in po if r['fy'] is not None)}"
        print(msg, flush=True)


def _cached(f: Path) -> set[str]:
    try:
        return set(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def cmd_build(codes, out, mode_note):
    research, er = research_module(), inputs.eps_revisions_module()
    keys, gaps = {}, []
    no_fetch = lambda info: (_ for _ in ()).throw(RuntimeError("build 不下载"))  # noqa: E731
    for c in codes:
        try:
            rows = load_list(c)
        except OSError:
            gaps.append(f"{c}:没有研报列表")
            continue
        api_have, pdf_file = _cached(research_dir(c) / CACHE_NAME), research_dir(c) / PDFONLY_NAME
        pdf_have = _cached(pdf_file)
        lost = sum(1 for r in rows if ((r.get("eps") or [None])[0] and r["info"] not in api_have)
                   or (not (r.get("eps") or [None])[0] and pdf_file.is_file() and r["info"] not in pdf_have))
        if lost:
            gaps.append(f"{c}:{lost} 篇 PDF 没下到,fy 记 None、不进统计")
        if not pdf_file.is_file():
            gaps.append(f"{c}:没跑 pdf 口径,列表没带预测数的研报不在表里")
        live = er.resolve(rows, research_dir(c), no_fetch, pdf_text)
        live = [r for r in live if (r.get("eps") or [None])[0]]
        f = research_dir(c) / PDFONLY_NAME
        pdf = resolve_pdf_only(rows, f, no_fetch, pdf_text, er) if f.is_file() else []
        keys[c] = build_records(live, pdf)
    meta = {"name": "consensus", "built_at": datetime.now().isoformat(timespec="seconds"),
            "source": "东财 reportapi 研报列表 + pdf.dfcfw.com 研报 PDF 的盈利预测表(eps_revisions.py)",
            "rule": "近 30 天同一家券商对明年归母净利润调高的家次 vs 调低的家次 → +1 / 0 / −1(inputs.eps_value + system_forecast._net_sign)",
            "known_rule": f"known = 研报发布日;某天只用 known 在那天往前 {LOOKBACK_DAYS} 天到那天之间的研报",
            "notes": ["src=api:列表带每股收益预测,年份按 eps_year 对出(live 口径,和线上一样)",
                      "src=pdf:列表没带预测数,年份按 PDF 表头认、只读净利润(pdf 口径,线上没有这一步)",
                      "PDF 下载失败或读不出的 fy 是 None,不进上下调统计", mode_note],
            "gaps": gaps}
    table = {"meta": meta, "keys": keys}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    print(f"写到 {out}:{len(keys)} 只,{sum(len(v) for v in keys.values())} 篇")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="券商盈利预测上调 / 下调的历史表")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lists", "pdfs", "build"):
        s = sub.add_parser(name)
        s.add_argument("--codes", required=True, help="逗号分隔的 6 位代码")
        s.add_argument("--until", default=date.today().isoformat())
        if name == "pdfs":
            s.add_argument("--mode", choices=("live", "pdf"), default="live")
        if name == "build":
            s.add_argument("--out", required=True)
            s.add_argument("--note", default="")
    s = sub.add_parser("coverage")
    s.add_argument("--table", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "coverage":
        print(json.dumps(coverage(json.loads(Path(a.table).read_text(encoding="utf-8"))), ensure_ascii=False, indent=1))
        return 0
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    if a.cmd == "lists":
        cmd_lists(codes, a.until)
    elif a.cmd == "pdfs":
        cmd_pdfs(codes, a.mode)
    else:
        cmd_build(codes, a.out, a.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
