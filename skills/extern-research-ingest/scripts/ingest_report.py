#!/usr/bin/env python3
"""extern-research-ingest —— 把外部研报(PDF)变成可核对的结构化输入。

    extract   <report.pdf> [--out DIR] [--dpi 150]   机械抽取 + 逐页渲染 + 出待填模板
    batch     <目录|多个 pdf> [--out DIR] [--force]  一次抽一批,末尾打印待填清单
    pending   [DIR]                                  只看待填清单(哪几份还缺哪些字段)
    validate  <filled.json>                          交叉校验填好的结构

为什么不写正则直接抽:
    各家投行版式完全不同(高盛/大摩/瑞银的表头、单位、年度列都不一样),
    正则在没见过的版式上**不是报错而是抽出错的数**——抽错了不报错,比抽不到糟得多。
    所以分工是:**脚本做机械的部分,模型看着渲染页填数,validate 用算术拦住转录错误。**

为什么必须渲染每一页:
    2026-09-01 踩过:高盛那份新易盛研报,最关键的三个数
    (12 个月前瞻 PE 的 +1σ 40x / 均值 28x / −1σ 17x)**只存在于 Exhibit 4 那张图里**,
    文本层里一个都没有。只跑文本提取会得到一份看起来完整、其实缺了估值锚的输入。
    见 sky-skills 的 tech-pdf-reader:「图能看见才算读到」。

依赖:pymupdf(唯一必需)。一个包同时给:文本、坐标、表格候选、整页渲染、内嵌图。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf                              # PyMuPDF ≥ 1.24
except ImportError:                             # 老版本包名
    try:
        import fitz as pymupdf                  # noqa: N813
    except ImportError:
        sys.exit("缺 pymupdf。装一次:\n  $VENV/bin/pip install pymupdf")


DEFAULT_OUT = "private/extern_research"


# ── 版面文本重建 ──────────────────────────────────────────────────────
# PyMuPDF 的 get_text("text") 不保列对齐,而研报的核心就是数字表。
# 用词的坐标自己重建版面 —— 等价于 pdftotext -layout,但不依赖 poppler,
# 这样别人 clone 仓库只装一个 pip 包就能用。
def layout_text(page, char_w: float = 4.8) -> str:
    words = page.get_text("words")              # (x0,y0,x1,y1,word,block,line,word_no)
    if not words:
        return ""
    rows: dict[int, list] = {}
    for x0, y0, x1, y1, w, *_ in words:
        key = round(y0 / 3)                     # 3pt 容差归行
        rows.setdefault(key, []).append((x0, w))
    out = []
    for key in sorted(rows):
        line = ""
        for x0, w in sorted(rows[key]):
            col = int(x0 / char_w)
            if col > len(line):
                line += " " * (col - len(line))
            elif line and not line.endswith(" "):
                line += " "
            line += w
        out.append(line.rstrip())
    return "\n".join(out)


# ── 待填模板 ──────────────────────────────────────────────────────────
# 字段是按「三情景要什么」倒推的,不是照抄研报目录。
TEMPLATE = {
    "_填写说明": "先看 pages/*.png 每一页,再填。图里的数(估值带、分位线)文本层没有,必须看图。",
    "source": {
        "publisher": "",                 # 高盛 / 摩根士丹利 / …
        "report_date": "",               # YYYY-MM-DD
        "analysts": [],
        "ticker": "",
        "language": "en",
        "provenance": "",                # 哪来的。转手流出的要写清楚,含水印文字
        "conflict_disclosure": "",       # 「未来 3 个月拟寻求投行业务」这类,必填
    },
    # 国内券商多数**不给目标价**(只给评级 + 盈利预测表),这是行业惯例不是漏填。
    # 所以留一个说明位:填了它就等于"确认过,这份确实没有",待填清单不再追。
    "call": {"rating": "", "target_price": None, "target_price_note": "", "currency": "CNY",
             "price_at_report": None, "upside_pct": None, "horizon_months": 12},
    # ★ 最值钱的一节:研报用什么锚定估值。我们自己就错在这里 ——
    #   用了过去十二个月 PE 的历史分位去乘未来的利润,而卖方用的是**前瞻 PE 的历史带**。
    "valuation_anchor": {
        "method": "",                    # 如 "near-term P/E on 2027E"
        "multiple": None,                # 如 27.8
        "multiple_basis": "",            # 如 "历史 12M forward P/E 均值 28x since 2018"
        "base_year": "",                 # 如 "2027E"
        "band": {"low": None, "mid": None, "high": None},   # −1σ / 均值 / +1σ,常只在图里
        "band_source_page": None,        # 这三个数在第几页的哪张图 —— 必填,便于复核
    },
    # ★ 单位必填。跨机构比净利时,一家写百万一家写亿就会差 100 倍且**不报错** ——
    #   数字都"看起来合理",不会有任何提示。EPS 是每股数,唯一天然可比的那列。
    "units": {"revenue": "Rmb mn", "net_income": "Rmb mn", "eps": "Rmb"},
    "forecast": [],                      # [{year, revenue, net_income, eps, gross_margin, net_margin, is_estimate}]
    "quarterly": [],                     # [{period, revenue, net_income, eps, is_estimate}]
    "revisions": {"note": "", "by_year": {}},   # 本次上调/下调幅度
    "company_guidance": "",              # 公司自己给的指引(研报常引用),我们没有别的渠道
    "key_risks": [],
    "notes": [],
}


def _digest(pdf: Path) -> str:
    return hashlib.sha256(pdf.read_bytes()).hexdigest()


def already_done(pdf: Path, out: Path) -> Path | None:
    """抽过且**内容没变**就返回目录,否则 None。

    比对 sha256 而不是 mtime:批量重跑时 mtime 常因 cp/rsync 变化,内容却一样;
    反过来内容改了 mtime 不变的情况也有(编辑器保留时间戳)。只有摘要说了算。
    另外还要求 pages/ 里的 PNG 张数对得上 —— 上次渲到一半被打断的目录必须重来。
    """
    d = out / re.sub(r"[^\w.-]+", "_", pdf.stem)[:60]
    mf = d / "meta.json"
    if not mf.exists():
        return None
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if m.get("sha256") != _digest(pdf):
        return None
    if len(list((d / "pages").glob("p*.png"))) != m.get("pages"):
        return None
    return d


def extract(pdf: Path, out: Path, dpi: int) -> int:
    doc = pymupdf.open(pdf)
    slug = re.sub(r"[^\w.-]+", "_", pdf.stem)[:60]
    d = out / slug
    (d / "pages").mkdir(parents=True, exist_ok=True)

    raw = pdf.read_bytes()
    meta = {
        "file": str(pdf), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "pages": doc.page_count,
        "pdf_metadata": {k: v for k, v in doc.metadata.items() if v},
        "encrypted": bool(doc.metadata.get("encryption")),
    }

    texts, tables, watermarks = [], [], set()
    for i, page in enumerate(doc, 1):
        t = layout_text(page)
        texts.append(f"\n{'=' * 78}\n[page {i}]\n{'=' * 78}\n{t}")
        # 水印/来源痕迹:转手流出的研报常带渠道号,这属于 provenance,必须记下来
        for m in re.finditer(r"(wechat|微信|WeChat)[:：]?\s*([\w.@-]{3,40})", t, re.I):
            watermarks.add(m.group(0).strip())
        try:
            found = page.find_tables()
            for k, tb in enumerate(found.tables):
                tables.append({"page": i, "index": k, "rows": tb.extract()})
        except Exception as e:                   # 表格检测失败不该让整个抽取失败
            tables.append({"page": i, "error": f"{type(e).__name__}: {e}"})
        page.get_pixmap(dpi=dpi).save(d / "pages" / f"p{i:02d}.png")

    meta["watermarks"] = sorted(watermarks)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "text.txt").write_text("\n".join(texts), encoding="utf-8")
    (d / "tables.json").write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")

    tpl = d / "filled.json"
    if not tpl.exists():                         # 已填过就不覆盖
        seed = json.loads(json.dumps(TEMPLATE))
        seed["source"]["provenance"] = (
            "PDF 元数据 title=" + str(meta["pdf_metadata"].get("title", ""))[:80]
            + ("；水印 " + "；".join(meta["watermarks"]) if meta["watermarks"] else ""))
        tpl.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"→ {d}")
    print(f"   meta.json      {doc.page_count} 页"
          + ("  ⚠ 加密(仅所有者口令,可读)" if meta["encrypted"] else ""))
    print(f"   text.txt       {sum(len(t) for t in texts):,} 字符(按坐标重建版面)")
    print(f"   tables.json    {len([t for t in tables if 'rows' in t])} 张表候选")
    print(f"   pages/         {doc.page_count} 张 PNG @ {dpi}dpi")
    if meta["watermarks"]:
        print(f"   ⚠ 检出水印/渠道痕迹:{'、'.join(meta['watermarks'])} —— 记进 provenance")
    else:
        # 2026-09-01 实测:高盛那份的水印「wechat: Hillwood2024」是旋转文字/图片,
        # **不在文本层**,正则一个都抓不到。所以「没报」≠「没有」——必须看渲染页。
        print("   · 文本层没检出水印。**这不等于没有** —— 水印常是图片或旋转文字,"
              "看 pages/*.png 页边再填 provenance")
    print(f"   filled.json    待填模板")
    print()
    print("下一步(**顺序不能反**):")
    print(f"  1. 用 Read 逐页看 {d}/pages/*.png —— 估值带、分位线这类数常常只在图里")
    print(f"  2. 对着图和 text.txt 填 {d}/filled.json")
    print(f"  3. 跑 validate,让算术拦住转录错误")
    return 0


# ── 批量入库 + 待填清单 ────────────────────────────────────────────────
# 一份研报里真正要人看的只有几张图,但机械部分(渲染、抽表、算摘要)完全可以成批做。
# 分开的理由:抽取是纯机械且可重跑的,填数是人/模型看图做的且**不可重跑**
# (填过的 filled.json 永远不覆盖)。所以 batch 只推进机械那一半,
# 末尾把"还差谁"打出来 —— 不打这张清单,批量抽完就没人知道该接着填哪份。

# 字段是按「汇总表要哪几列」倒推的。缺硬项,那份研报在汇总里就是个空格。
# 元组表示**多选一**:目标价和"确认过没有目标价"填任一个都算数 ——
# 国内券商多数不给目标价,把它当漏填会让这几份永远挂在待填清单上,清单一旦
# 有永远消不掉的项,人就不看它了。
REQUIRED = [
    ("source.publisher",          "机构"),
    ("source.report_date",        "报告日"),
    ("source.ticker",             "标的"),
    ("source.provenance",         "来源"),
    ("call.rating",               "评级"),
    (("call.target_price", "call.target_price_note"), "目标价(或注明本报告不给)"),
    ("call.price_at_report",      "报告日价"),
    ("valuation_anchor.method",   "估值方法"),
    ("valuation_anchor.multiple", "倍数"),
    ("valuation_anchor.base_year", "基准年"),
    ("units.net_income",          "净利单位"),
    ("forecast",                  "分年度预测"),
]

# 可补项:没有也能用,但有了价值大得多。分开列,免得和"真缺"混在一起。
NICE = [
    ("valuation_anchor.band.mid", "估值带中枢(历史前瞻 PE 带,通常只在图里)"),
    ("source.conflict_disclosure", "利益冲突披露"),
    ("quarterly",                 "分季度预测"),
]


def dig(obj, path: str):
    """按点号取值。中途遇到非 dict 就返回 None,不抛 —— 半填的 json 是常态。"""
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _filled(j: dict, path) -> bool:
    paths = path if isinstance(path, tuple) else (path,)
    return any(not _empty(dig(j, x)) for x in paths)


def missing_fields(j: dict, table=REQUIRED) -> list[str]:
    return [label for path, label in table if not _filled(j, path)]


def pending(root: Path) -> int:
    """打印待填清单。**没有待填也要打印一行**,否则看不出是"都填完了"还是"脚本没跑"。"""
    dirs = sorted(d for d in root.glob("*") if d.is_dir() and (d / "meta.json").exists())
    if not dirs:
        print(f"{root} 下没有已抽取的研报。先跑:ingest_report.py batch <放 pdf 的目录>")
        return 0

    rows, done = [], 0
    for d in dirs:
        fp = d / "filled.json"
        if not fp.exists():
            rows.append((d.name, "—", ["整份未填(filled.json 不存在)"], []))
            continue
        try:
            j = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rows.append((d.name, "—", [f"filled.json 不是合法 JSON:{e}"], []))
            continue
        miss, nice = missing_fields(j), missing_fields(j, NICE)
        who = (dig(j, "source.publisher") or "?") + " " + (dig(j, "source.report_date") or "")
        if miss:
            rows.append((d.name, who.strip(), miss, nice))
        else:
            done += 1
            if nice:
                rows.append((d.name, who.strip(), [], nice))

    hard = [r for r in rows if r[2]]
    print(f"待填清单 —— 已抽取 {len(dirs)} 份,硬项填全 {done} 份,还差 {len(hard)} 份")
    if not rows:
        print("  ✓ 全部填完。下一步:validate 再出汇总")
        return 0
    for name, who, miss, nice in rows:
        print(f"\n  ▸ {name}  [{who}]")
        if miss:
            print(f"    缺 {len(miss)} 项:{'、'.join(miss)}")
            print(f"    填法:先 Read {root / name}/pages/*.png 逐页看,"
                  f"再改 {root / name}/filled.json")
        if nice:
            print(f"    另可补:{'、'.join(nice)}")
    return 0


def batch(paths: list[str], out: Path, dpi: int, force: bool) -> int:
    pdfs: list[Path] = []
    for x in paths:
        pth = Path(x)
        if pth.is_dir():
            pdfs += sorted(q for q in pth.rglob("*.pdf") if not q.name.startswith("."))
        elif pth.exists():
            pdfs.append(pth)
        else:
            print(f"  ⚠ 跳过,找不到:{x}")
    if not pdfs:
        print("没找到任何 PDF。")
        return 1

    print(f"共 {len(pdfs)} 份 PDF → {out}\n")
    ok = skipped = failed = 0
    for i, pdf in enumerate(pdfs, 1):
        hit = None if force else already_done(pdf, out)
        if hit:
            print(f"[{i}/{len(pdfs)}] {pdf.name}  · 已抽过且内容未变,跳过({hit.name})")
            skipped += 1
            continue
        print(f"[{i}/{len(pdfs)}] {pdf.name}")
        try:
            extract(pdf, out, dpi)
            ok += 1
        except Exception as e:                   # 一份坏 PDF 不该让整批停下
            print(f"   ✗ 抽取失败:{type(e).__name__}: {e}\n")
            failed += 1

    print(f"抽取完成:新抽 {ok} · 跳过 {skipped} · 失败 {failed}\n")
    pending(out)
    return 1 if failed else 0


# ── 交叉校验 ──────────────────────────────────────────────────────────────
def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(fp: Path) -> int:
    j = json.loads(fp.read_text(encoding="utf-8"))
    errs, warns = [], []

    src, call, va = j.get("source", {}), j.get("call", {}), j.get("valuation_anchor", {})
    for k in ("publisher", "report_date", "ticker"):
        if not src.get(k):
            errs.append(f"source.{k} 空")
    if not src.get("provenance"):
        errs.append("source.provenance 空 —— 外部研报**必须**记来源,转手流出的尤其")
    if not src.get("conflict_disclosure"):
        warns.append("source.conflict_disclosure 空 —— 投行与标的的业务关系会影响结论,该记")
    if src.get("report_date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", src["report_date"]):
        errs.append(f"source.report_date 格式应为 YYYY-MM-DD,得到 {src['report_date']!r}")

    # 上涨空间自洽:目标价 / 报告当日价 − 1
    tp, px, up = call.get("target_price"), call.get("price_at_report"), call.get("upside_pct")
    if _num(tp) and _num(px) and px:
        calc = (tp / px - 1) * 100
        if _num(up) and abs(calc - up) > 1.5:
            errs.append(f"call.upside_pct={up} 与 目标价/现价−1={calc:.1f}% 对不上(差 {abs(calc-up):.1f}pp)")
        elif not _num(up):
            warns.append(f"call.upside_pct 未填,可由目标价推出 {calc:.1f}%")

    # ★ 估值锚自洽:目标价 ÷ 基准年 EPS ≈ 目标倍数。这条最能抓转录错误。
    mult, by = va.get("multiple"), (va.get("base_year") or "").strip()
    if _num(tp) and _num(mult) and by:
        eps = next((r.get("eps") for r in j.get("forecast", [])
                    if str(r.get("year", "")).strip() == by), None)
        if _num(eps) and eps:
            implied = tp / eps
            if abs(implied - mult) / mult > 0.05:
                errs.append(f"目标价 {tp} ÷ {by} EPS {eps} = {implied:.1f}x,"
                            f"但 valuation_anchor.multiple 填的是 {mult}x —— 两者必须一致")
        else:
            warns.append(f"forecast 里没有 {by} 的 eps,无法核对估值锚")
    if not va.get("method"):
        errs.append("valuation_anchor.method 空 —— **这是这份研报最值钱的一节**"
                    "(卖方用前瞻 PE 带,我们曾错用 TTM PE 分位)")
    band = va.get("band") or {}
    if any(_num(band.get(k)) for k in ("low", "mid", "high")) and not va.get("band_source_page"):
        errs.append("填了估值带却没填 band_source_page —— 这类数通常只在图里,必须能复核到页")

    # 预测表内部自洽:净利 ÷ EPS 应给出稳定的股本
    shares = []
    for r in j.get("forecast", []):
        ni, eps = r.get("net_income"), r.get("eps")
        if _num(ni) and _num(eps) and eps:
            shares.append((r.get("year"), ni / eps))
    if len(shares) >= 2:
        vals = [s for _, s in shares]
        if (max(vals) - min(vals)) / min(vals) > 0.15:
            errs.append("forecast 各年 净利÷EPS 推出的股本差异 >15%,"
                        f"多半是单位或转录错:{[(y, round(s, 3)) for y, s in shares]}")
    if not j.get("forecast"):
        errs.append("forecast 为空 —— 没有分年度预测就接不进三情景")
    else:
        # 单位缺失是**跨机构汇总**里最阴的一种错:一家 Rmb mn、一家 Rmb bn,
        # 表格并排放出来数字全在合理区间,没有任何一处会报错。
        for k in ("revenue", "net_income", "eps"):
            if not (j.get("units") or {}).get(k):
                errs.append(f"units.{k} 空 —— 有 forecast 就必须声明单位,"
                            f"否则汇总时会拿百万比亿")

    tag = fp.name
    for e in errs:
        print(f"  ✗ {tag}: {e}")
    for w in warns:
        print(f"  ⚠ {tag}: {w}")
    if not errs and not warns:
        print(f"  ✓ {tag}: 通过")
    elif not errs:
        print(f"  ✓ {tag}: 通过({len(warns)} 条提醒)")
    return 1 if errs else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="机械抽取 + 逐页渲染 + 出待填模板")
    e.add_argument("pdf")
    e.add_argument("--out", default=DEFAULT_OUT)
    e.add_argument("--dpi", type=int, default=150)
    b = sub.add_parser("batch", help="一次抽一批 PDF,末尾打印待填清单")
    b.add_argument("paths", nargs="+", help="目录(递归找 *.pdf)或若干 pdf 路径")
    b.add_argument("--out", default=DEFAULT_OUT)
    b.add_argument("--dpi", type=int, default=150)
    b.add_argument("--force", action="store_true", help="已抽过的也重抽(重渲染整页)")
    g = sub.add_parser("pending", help="只看待填清单")
    g.add_argument("dir", nargs="?", default=DEFAULT_OUT)
    v = sub.add_parser("validate", help="交叉校验填好的 filled.json")
    v.add_argument("json", nargs="+")
    a = ap.parse_args()

    if a.cmd == "extract":
        p = Path(a.pdf)
        if not p.exists():
            sys.exit(f"找不到 {p}")
        return extract(p, Path(a.out), a.dpi)
    if a.cmd == "batch":
        return batch(a.paths, Path(a.out), a.dpi, a.force)
    if a.cmd == "pending":
        return pending(Path(a.dir))
    return max(validate(Path(x)) for x in a.json)


if __name__ == "__main__":
    sys.exit(main())
