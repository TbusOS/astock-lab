#!/usr/bin/env python3
"""research —— 券商研报:列表 + 全文 PDF + 产业数据抽句 + 目标价分布。

用法:
    research 300502                    # 近一年研报列表 + 评级/目标价分布
    research 300502 --dig 3            # 再下载最新 3 篇 PDF，抽产业数据句子
    research 300502 --dig 3 --kw 1.6T,产能,出货   # 自定义关键词
    research 300502 --since 2026-06-01 --md r.md

为什么这个工具值得存在(2026-08-28 实测才发现):
    我们原来在 probe_sources 里写「产业高频数据不在任何免费公开源,
    券商产业链调研报告与 LightCounting 的付费数据库才有」。
    **前半句对,后半句错了一半** —— 精确的月度出货量确实要付费,
    但**券商研报 PDF 本身是免费的**:

        reportapi.eastmoney.com/report/list  列出个股研报
        pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf  直接下载,文本层完好非扫描件

    实测抓到的最新一篇标题就是「1.6T 光模块量价齐升」,正文含
    「1.6T 光模块 Q2 出货量较 Q1 明显增长,预计 Q3/Q4 起放量」
    以及产能、份额、毛利率的讨论。

    **拿不到的是精确数值,拿得到的是方向和幅度** ——
    而「变化率比水平值重要」本来就是这套方法的核心,方向往往够用。

⚠ 研报是**卖方观点不是事实**。它的用法是:
    ① 拿产业事实(出货、产能、价格方向)——这些通常有一手调研支撑
    ② 看**评级与目标价的变动方向**——比绝对值有用得多
    ③ **不要**把目标价当依据。见 SKILL.md §7 常见误判。

PDF 抽文本需要 pymupdf(pip install pymupdf);没装则只出列表,不影响主流程。
代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import re  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
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


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
LIST_URL = "https://reportapi.eastmoney.com/report/list"
PDF_URL = "https://pdf.dfcfw.com/pdf/H3_{}_1.pdf"
REFERER = "https://data.eastmoney.com/report/stock.jshtml"

# 默认抽句关键词:光模块/算力链的产业量价指标。--kw 可换。
DEFAULT_KW = ["1.6T", "800G", "400G", "出货", "产能", "份额", "价格", "涨价",
              "降价", "订单", "交付", "良率", "EML", "硅光", "CPO", "NPO",
              "毛利率", "供需", "紧缺", "扩产"]

# 评级文字 → 分值,用来看一致度与变动方向
RANK = {"买入": 5, "强烈推荐": 5, "强推": 5, "推荐": 4, "增持": 4, "优于大市": 4,
        "谨慎推荐": 3, "中性": 3, "持有": 3, "同步大市": 3,
        "减持": 2, "回避": 1, "卖出": 1}

_OUT = []


def say(s=""):
    print(s)
    _OUT.append(s)


def _get(url, params=None, referer=REFERER, timeout=25, binary=False):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer,
        "Accept": "application/json,text/html,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        raw = f.read()
    return raw if binary else raw.decode("utf-8", "replace")


def fetch_list(code, since, until, max_pages=5):
    """列出个股研报。

    ⚠ 这个接口对参数很挑:少给 beginTime/endTime/industry/rating 这几个
    就直接 400。别只传 code —— 2026-08-28 实测过,少一个都不行。
    """
    import json
    out, page, total = [], 1, 1
    while page <= min(total, max_pages):
        txt = _get(LIST_URL, {
            "industryCode": "*", "pageSize": "50", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": since, "endTime": until,
            "pageNo": str(page), "fields": "", "qType": "0",
            "orgCode": "", "code": code, "rcode": "",
            "p": str(page), "pageNum": str(page), "pageNumber": str(page)})
        j = json.loads(txt)
        total = int(j.get("TotalPage") or 1)
        for it in j.get("data") or []:
            lo, hi = it.get("indvAimPriceL"), it.get("indvAimPriceT")
            out.append({
                "date": (it.get("publishDate") or "")[:10],
                "org": it.get("orgSName") or it.get("orgName") or "",
                "title": (it.get("title") or "").strip(),
                "rating": it.get("emRatingName") or it.get("sRatingName") or "",
                "chg": it.get("ratingChange"),
                "tp_lo": _f(lo), "tp_hi": _f(hi),
                "researcher": it.get("researcher") or "",
                "info": it.get("infoCode") or "",
            })
        page += 1
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _f(v):
    try:
        x = float(v)
        return None if x <= 0 else x
    except (TypeError, ValueError):
        return None


def sec_list(rows, code, since):
    say(f"# {code} 券商研报")
    say()
    say(f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　·　"
        f"来源 东财 reportapi + pdf.dfcfw.com　·　{since} 至今")
    say()
    if not rows:
        say("（这段时间没有研报。把 `--since` 往前调，或确认代码对不对）")
        say()
        return
    say(f"## 一　共 {len(rows)} 篇")
    say()
    say("| 日期 | 机构 | 标题 | 评级 | 目标价 |")
    say("|---|---|---|---|---|")
    for r in rows:
        tp = "—"
        if r["tp_lo"] and r["tp_hi"]:
            tp = (f"**{r['tp_lo']:.0f}**" if abs(r["tp_lo"] - r["tp_hi"]) < 1e-6
                  else f"{r['tp_lo']:.0f}–{r['tp_hi']:.0f}")
        elif r["tp_hi"]:
            tp = f"**{r['tp_hi']:.0f}**"
        say(f"| {r['date']} | {r['org']} | {r['title'][:38]} | "
            f"**{r['rating']}** | {tp} |")
    say()

    # 评级与目标价分布 —— 变动方向比绝对值有用
    scores = [RANK[r["rating"]] for r in rows if r["rating"] in RANK]
    tps = [r["tp_hi"] for r in rows if r["tp_hi"]]
    say("### 卖方共识")
    say()
    if scores:
        avg = sum(scores) / len(scores)
        tone = ("一致看多" if avg >= 4.5 else "偏多" if avg >= 4
                else "中性" if avg >= 3 else "偏空")
        say(f"- 平均评级分 **{avg:.2f} / 5**（{tone}）　—— "
            f"5=买入 4=增持 3=中性 2=减持 1=卖出，{len(scores)} 篇有评级")
    if tps:
        tps.sort()
        mid = tps[len(tps) // 2]
        say(f"- 目标价 **{len(tps)}** 家给出，区间 **{min(tps):.0f}–{max(tps):.0f}**，"
            f"中位 **{mid:.0f}**，均值 {sum(tps)/len(tps):.0f}")
        say(f"- 分歧度 **{(max(tps)-min(tps))/mid*100:.0f}%**"
            f"（(最高−最低)÷中位）—— >50% 说明卖方之间看法差异极大")
    say()
    say("> ⚠️ **目标价只作参考，不当依据。**它建立在一串会被证否的假设上，"
        "而且卖方有天然的乐观偏向。**变动方向比绝对值有用** —— "
        "同一家机构连续下调，比十家给高目标价更有信息量。")
    say()


# 研报最后几页是固定的免责声明与评级定义,里面全是「价格」「投资评级」这类词,
# 会把真正的产业句子挤掉。命中这些标志的片段直接丢。
_BOILER = re.compile(
    r"免责|声明|评级说明|评级定义|本报告(采用|署名|版权|仅供)|不保证|"
    r"执业证书|分析师承诺|投资咨询|风险等级|适当性|未经授权|法律责任")


def extract(pdf_bytes, kws, ctx=70, limit=8):
    """从 PDF 抽含关键词的句子。没装 pymupdf 就返回 None。"""
    try:
        # ⚠ 先试 pymupdf:新版用 `import fitz` 会打一行弃用警告到 stderr,
        #   混在报告输出里很脏。老版本才只有 fitz。
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        txt = "".join(p.get_text() for p in doc)
    except Exception:
        return None
    # 扫描件没有文本层 —— 说清楚,别让人以为是没命中
    if len(txt) < 200:
        return {"scanned": True, "sents": [], "chars": len(txt)}
    txt = re.sub(r"[ \t]+", " ", txt)
    seen, sents = set(), []
    for kw in kws:
        for m in re.finditer(re.escape(kw), txt):
            a = max(0, m.start() - ctx)
            b = min(len(txt), m.end() + ctx)
            frag = " ".join(txt[a:b].split())
            # 掐到句子边界,避免半截话
            frag = re.sub(r"^[^。；]*?[。；]", "", frag, count=1) or frag
            key = frag[:40]
            if key in seen or len(frag) < 20 or _BOILER.search(frag):
                continue
            seen.add(key)
            sents.append((kw, frag[:180]))
            if len(sents) >= limit:
                return {"scanned": False, "sents": sents, "chars": len(txt)}
    return {"scanned": False, "sents": sents, "chars": len(txt)}


def sec_dig(rows, n, kws, save_dir):
    say(f"## 二　产业数据抽句（最新 {n} 篇全文）")
    say()
    say("研报正文里的**产业事实**（出货、产能、价格方向）通常有一手调研支撑，"
        "比结论部分可信。这里按关键词把相关句子抽出来 —— "
        "**拿方向和幅度，不要拿精确数值当实锤**。")
    say()
    got = 0
    for r in rows[:n]:
        if not r["info"]:
            continue
        url = PDF_URL.format(r["info"])
        say(f"### {r['date']}　{r['org']}　{r['title'][:40]}")
        say()
        try:
            blob = _get(url, referer="https://data.eastmoney.com/",
                        timeout=40, binary=True)
        except Exception as e:
            say(f"（下载失败：{type(e).__name__}）　{url}")
            say()
            continue
        if save_dir:
            try:
                save_dir.mkdir(parents=True, exist_ok=True)
                (save_dir / f"{r['date']}_{r['org']}_{r['info']}.pdf").write_bytes(blob)
            except OSError:
                pass
        res = extract(blob, kws)
        if res is None:
            say(f"（{len(blob)/1024:.0f} KB 已下载，但没装 pymupdf 抽不了文本：")
            say(f"`pip install pymupdf`）　[PDF]({url})")
            say()
            continue
        if res["scanned"]:
            say(f"（**扫描件，无文本层**，只有 {res['chars']} 字符 —— "
                f"抽不出来不是没命中）　[PDF]({url})")
            say()
            continue
        if not res["sents"]:
            say(f"（{res['chars']:,} 字符正文，但没命中关键词 "
                f"{'/'.join(kws[:5])}…）　[PDF]({url})")
            say()
            continue
        got += 1
        for kw, frag in res["sents"]:
            say(f"- **{kw}** — {frag}")
        say()
        say(f"　[PDF 原文]({url})　·　正文 {res['chars']:,} 字符")
        say()
    if got == 0:
        say("> 一篇都没抽到 —— 要么没装 pymupdf，要么这几篇都是扫描件，"
            "要么关键词不对（用 `--kw` 换成这个行业的词）。")
        say()


def main():
    p = argparse.ArgumentParser(
        prog="research",
        description="券商研报:列表 + 全文 PDF + 产业数据抽句 + 目标价分布")
    p.add_argument("code", nargs="?", help="股票代码,如 300502")
    p.add_argument("--since", metavar="日期", help="起始日期(默认一年前)")
    p.add_argument("--dig", type=int, default=0, metavar="N",
                   help="下载最新 N 篇 PDF 并抽产业数据句子(需 pymupdf)")
    p.add_argument("--kw", metavar="词1,词2", help="自定义抽句关键词")
    p.add_argument("--save", action="store_true",
                   help="把下载的 PDF 存到 data/research/<代码>/")
    p.add_argument("--md", metavar="文件", help="存 markdown")
    a = p.parse_args()
    if not a.code:
        print(__doc__)
        return 1

    code = a.code.strip().zfill(6)
    since = a.since or (dt.date.today() - dt.timedelta(days=365)).isoformat()
    until = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    kws = [x.strip() for x in a.kw.split(",")] if a.kw else DEFAULT_KW

    try:
        rows = fetch_list(code, since, until)
    except Exception as e:
        print(f"研报列表取数失败:{type(e).__name__}: {e}", file=sys.stderr)
        print("这个接口对参数很挑,少给 beginTime/industry/rating 会 400。",
              file=sys.stderr)
        return 2

    sec_list(rows, code, since)
    if a.dig and rows:
        save = (_lab_root() / "data" / "research" / code) if a.save else None
        sec_dig(rows, a.dig, kws, save)

    say("---")
    say()
    say("**数据来源**：研报列表 = 东财 `reportapi`　·　"
        "全文 = `pdf.dfcfw.com`（免费，文本层完好非扫描件）。")
    say("研报是**卖方观点不是事实**：拿它的产业事实（出货/产能/价格方向）"
        "与**评级变动方向**，别拿目标价当依据。")
    say()

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
