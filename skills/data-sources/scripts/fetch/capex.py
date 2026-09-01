#!/usr/bin/env python3
"""capex —— 从 SEC EDGAR 拉北美云厂的资本开支(Capex),看 A 股算力链的需求源头。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/capex.py                    # 四大云厂,最近 8 季
    $P ... --tickers MSFT,GOOGL,AMZN,META,ORCL,CRWV --quarters 12
    $P ... --md ~/capex.md

为什么要看这个(这是本脚本存在的理由):
    A 股光模块 / 服务器 / 液冷这条链,收入来自北美超大规模云厂的数据中心投资。
    股价、研报、龙虎榜都是**结果**;云厂的 Capex 是**原因**,而且领先 1-2 个季度。
    Capex 同比掉档 → 6 个月后 A 股这条链的订单和业绩才会体现。
    只看 A 股数据,等你从财报看出问题时已经晚了两个季度。

数据源:SEC EDGAR XBRL companyconcept API(官方,免费,无需注册)。
    标签 us-gaap:PaymentsToAcquirePropertyPlantAndEquipment
    (2026-08-27 实测 MSFT/GOOGL/AMZN/META/ORCL/NVDA/AVGO/CRWV 八家都用这个标签)

SEC 的两条硬要求(不满足会被 403):
    1. User-Agent 必须带联系方式。默认值见 DEFAULT_UA,
       **建议设环境变量 SEC_UA='你的名字 你的邮箱' 换成自己的**,这是 SEC 的规矩。
    2. 请求频率 < 10 次/秒。脚本每次请求间隔 0.15 秒,远低于上限。

季度值的取法(这里有三个坑,都实测踩过):
    1. **各家用的标签不一样**。MSFT/GOOGL/META 用 PaymentsToAcquirePropertyPlant-
       AndEquipment;AMZN 2025 年起改用 PaymentsToAcquireProductiveAssets
       (旧标签在 2025 年后完全没数据)。脚本按 TAG_CANDIDATES 逐个试,
       选**最近一期最新**的那个。
    2. **有的公司只报年初至今累计,不报单季**。谷歌就是:10-Q 里只有
       1/1→3/31、1/1→6/30、1/1→9/30。单季要用相邻累计值相减倒推。
    3. **364 天的记录不一定是财年**。亚马逊同时存着滚动 12 个月
       (start 2024-04-01 → end 2025-03-31),按天数判会误当全年。
       所以按 **start 分组**:同一 start 的一串记录才是累计序列,
       组内相邻相减得单季;首条自身 ≤100 天才认这组是财年累计。

两个取数引擎(--engine,可对比):
    raw        自写的 130 行,直接打 companyconcept API,零额外依赖(默认)
    edgartools 用 dgunning/edgartools 库取数,再走同一套单季折算
    both       两个都跑并逐季比对,数值不一致会标出来 —— 用来验证自写逻辑对不对

    **两个引擎共用同一套 _to_quarters 折算**,因为 edgartools 并不解决这个问题:
    它的 fiscal_period 标注会把谷歌 180 天累计记录(2025-01-01→2025-06-30,39.64B)
    标成 "Q2",而真实 Q2 是 39.64−17.20=22.44B,偏高 76%;
    改用 filter_by_period_type("quarterly") 虽然滤掉了累计记录,
    但谷歌就只剩 Q1 了(它只有 Q1 是真单季)。两条路都要靠差分补齐。
    (2026-08-27 实测)

代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

# SEC 要求 UA 带联系方式。用环境变量覆盖成你自己的,这是 SEC 明文规定。
DEFAULT_UA = "astock-lab-user your-email@example.com"
UA = os.environ.get("SEC_UA", DEFAULT_UA)

# 各家标签不统一,按顺序试,选最近一期最新的那个
TAG_CANDIDATES = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]
DEFAULT_TICKERS = ["MSFT", "GOOGL", "AMZN", "META"]

# 中文名,输出好读
CN = {"MSFT": "微软", "GOOGL": "谷歌", "AMZN": "亚马逊", "META": "Meta",
      "ORCL": "甲骨文", "NVDA": "英伟达", "AVGO": "博通", "CRWV": "CoreWeave",
      "AAPL": "苹果", "TSLA": "特斯拉"}

_OUT = []
_last_req = [0.0]


def say(s=""):
    print(s)
    _OUT.append(s)


def _get(url):
    """带 SEC 要求的 UA + 限速 + gzip 解压。"""
    gap = time.time() - _last_req[0]
    if gap < 0.15:
        time.sleep(0.15 - gap)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=30) as f:
        raw = f.read()
        if f.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    _last_req[0] = time.time()
    return json.loads(raw)


def load_ciks(tickers):
    t = _get("https://www.sec.gov/files/company_tickers.json")
    want = {x.upper() for x in tickers}
    out = {}
    for _, v in t.items():
        tk = v["ticker"].upper()
        if tk in want:
            out[tk] = (str(v["cik_str"]).zfill(10), v["title"])
    return out


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _to_quarters(records):
    """把 XBRL 的一堆 (start,end,val) 折成单季 {end: val}。

    做法:按 start 分组。同一 start 的记录按 end 排序 = 一条年初至今的累计序列,
    组内相邻相减就是单季。只认首条 ≤100 天的组(那才是从财年初起的累计),
    这样能排掉滚动 12 个月那类记录(start 不是财年初)。
    已经是单季(80~100 天)的记录直接采用,优先级最高。
    """
    direct, groups = {}, {}
    for s, e, v in records:
        n = _days(s, e)
        if 80 <= n <= 100:
            direct[e] = v
        groups.setdefault(s, []).append((e, v, n))

    derived = {}
    for s, items in groups.items():
        items.sort()
        if not items or items[0][2] > 100:
            continue          # 首条就超过一个季度 → 不是财年累计序列,跳过
        prev_e, prev_v = None, 0.0
        for e, v, n in items:
            if prev_e is None:
                derived[e] = v
            else:
                gap = _days(prev_e, e)
                if 80 <= gap <= 100 and v >= prev_v:
                    derived[e] = v - prev_v
            prev_e, prev_v = e, v
    derived.update(direct)     # 直报的单季覆盖倒推值
    return derived


def fetch_capex(cik):
    """逐个试标签,返回单季 dict + 用了哪个标签。"""
    best = None
    for tag in TAG_CANDIDATES:
        try:
            d = _get(f"https://data.sec.gov/api/xbrl/companyconcept/"
                     f"CIK{cik}/us-gaap/{tag}.json")
        except Exception:
            continue
        recs, seen = [], set()
        for x in d.get("units", {}).get("USD", []):
            s, e, v = x.get("start"), x.get("end"), x.get("val")
            if not (s and e and v is not None) or (s, e) in seen:
                continue
            seen.add((s, e))
            recs.append((s, e, v))
        q = _to_quarters(recs)
        if not q:
            continue
        latest = max(q)
        if best is None or latest > best["latest"]:
            best = {"q": q, "name": d.get("entityName", ""),
                    "tag": tag, "latest": latest}
    if best is None:
        raise RuntimeError("所有候选标签都没有可用数据")
    return best


def fetch_capex_edgartools(ticker):
    """用 edgartools 取同样的数据,再走同一套 _to_quarters 折算。

    注意:这里刻意**不用** edgartools 的 fiscal_period 标注,
    也不用 filter_by_period_type("quarterly") —— 原因见模块文档。
    只借它「一次拿到全部 concept、不用逐个标签试」这个便利。
    """
    from edgar import Company, set_identity
    set_identity(UA)
    c = Company(ticker)
    df = c.get_facts().to_dataframe()
    best = None
    for tag in TAG_CANDIDATES:
        h = df[df["concept"] == f"us-gaap:{tag}"]
        if not len(h):
            continue
        recs, seen = [], set()
        for _, x in h.iterrows():
            st, en, v = x.get("period_start"), x.get("period_end"), x.get("numeric_value")
            if st is None or en is None or v is None:
                continue
            st, en = str(st)[:10], str(en)[:10]
            if (st, en) in seen:
                continue
            seen.add((st, en))
            recs.append((st, en, float(v)))
        q = _to_quarters(recs)
        if not q:
            continue
        latest = max(q)
        if best is None or latest > best["latest"]:
            best = {"q": q, "name": c.name, "tag": tag, "latest": latest}
    if best is None:
        raise RuntimeError("edgartools:没有可用的 Capex 标签")
    return best


def compare_engines(raw, et, ticker):
    """逐季比对两个引擎,返回差异行列表。"""
    diffs = []
    keys = sorted(set(raw["q"]) | set(et["q"]))
    for k in keys:
        a, b = raw["q"].get(k), et["q"].get(k)
        if a is None or b is None:
            diffs.append((k, a, b, "只有一边有"))
        elif abs(a - b) > max(1e6, abs(a) * 0.005):   # 差 >0.5% 或 >100 万美元
            diffs.append((k, a, b, f"{(b/a-1)*100:+.2f}%"))
    return diffs


# 指引措辞:公司在 8-K 的 EX-99 新闻稿里给下一年 Capex 展望时用的句式
_GUIDE_PAT = None


def _guide_pat():
    global _GUIDE_PAT
    if _GUIDE_PAT is None:
        import re
        _GUIDE_PAT = re.compile(
            r"[^.]{0,220}\b(?:anticipate|expect|outlook|guidance|plan to invest|"
            r"we (?:will|intend))\b[^.]{0,120}\b(?:capital expenditure|capex|"
            r"capital spend)\w*[^.]{0,260}\."
            r"|[^.]{0,180}\b(?:capital expenditure|capex)\w*[^.]{0,80}"
            r"\b(?:to be in the range|will be|expected to)\b[^.]{0,220}\.",
            re.I)
    return _GUIDE_PAT


def fetch_guidance(ticker, look_back=6):
    """从最近几份 8-K 的 EX-99 新闻稿里抽 Capex 指引措辞。

    覆盖情况(2026-08-27 实测):META 的新闻稿里有明确年度指引;
    MSFT / AMZN / GOOGL 不在新闻稿给,而是在电话会口头给 —— 电话会纪要不在 SEC,
    所以这里拿不到。**拿不到不等于没有指引,只是不在这个渠道。**
    """
    from edgar import Company, set_identity
    set_identity(UA)
    fl = Company(ticker).get_filings(form="8-K").head(look_back)
    pat = _guide_pat()
    for i in range(len(fl)):
        f = fl[i]
        ex = [a for a in f.attachments
              if str(a.document_type).startswith("EX-99")]
        if not ex:
            continue
        try:
            txt = ex[0].text()
        except Exception:
            continue
        hits, seen = [], set()
        for m in pat.finditer(txt):
            h = " ".join(m.group(0).split())
            # 排除只是在定义自由现金流之类的说明性句子
            if len(h) < 60 or "we define" in h.lower():
                continue
            if h[:60] in seen:
                continue
            seen.add(h[:60])
            hits.append(h)
        if hits:
            return {"date": str(f.filing_date), "hits": hits[:4]}
    return None


def quarters_sorted(data, n):
    return sorted(data["q"].items())[-n:]


def yoy(data, end):
    """同比:找 365±20 天前的那一季。"""
    e = date.fromisoformat(end)
    for e2, v2 in data["q"].items():
        if 345 <= (e - date.fromisoformat(e2)).days <= 385:
            cur = data["q"][end]
            if v2:
                return (cur / v2 - 1) * 100
    return None


def main():
    p = argparse.ArgumentParser(
        prog="capex",
        description="从 SEC EDGAR 拉北美云厂 Capex —— A 股算力链的需求源头")
    p.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                   help=f"逗号分隔,默认 {','.join(DEFAULT_TICKERS)}")
    p.add_argument("--quarters", type=int, default=8, help="看最近几个季度(默认 8)")
    p.add_argument("--md", metavar="文件", help="存 markdown")
    p.add_argument("--guidance", action="store_true",
                   help="额外抓 8-K 新闻稿里的下期 Capex 指引(需 edgartools;"
                        "只有部分公司在新闻稿给,其余在电话会口头给,拿不到)")
    p.add_argument("--engine", choices=["raw", "edgartools", "both"], default="raw",
                   help="取数引擎:raw=自写(默认,零依赖) / edgartools=用库 / "
                        "both=两个都跑并比对")
    a = p.parse_args()

    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]

    say("# 北美云厂资本开支（Capex）")
    say()
    say(f"> 生成时间 {time.strftime('%Y-%m-%d %H:%M')}　·　"
        f"数据源 SEC EDGAR XBRL（官方，免费）")
    say("> 单季口径：直报的单季直接用；只报年初至今累计的（如谷歌），"
        "按 start 分组后相邻相减倒推。各家标签见下方各节。")
    say()
    if UA == DEFAULT_UA:
        say("> ⚠️ 正在用默认 User-Agent。SEC 要求声明联系方式，"
            "建议设 `export SEC_UA='你的名字 你的邮箱'` 换成自己的。")
        say()

    try:
        ciks = load_ciks(tickers)
    except Exception as e:
        print(f"取 CIK 失败：{type(e).__name__}: {e}", file=sys.stderr)
        if isinstance(e, urllib.error.HTTPError) and e.code == 403:
            print("403 通常是 User-Agent 不合规 —— SEC 要求带联系方式。"
                  "设 SEC_UA 环境变量。", file=sys.stderr)
        return 1

    allq = {}
    for tk in tickers:
        if tk not in ciks:
            say(f"（{tk}：在 SEC 代码表里没找到，跳过）")
            say()
            continue
        cik, name = ciks[tk]
        d = None
        try:
            if a.engine in ("raw", "both"):
                d = fetch_capex(cik)
            if a.engine in ("edgartools", "both"):
                et = fetch_capex_edgartools(tk)
                if a.engine == "edgartools":
                    d = et
                else:
                    diffs = compare_engines(d, et, tk)
                    if diffs:
                        say(f"> ⚠️ **{tk} 两引擎有 {len(diffs)} 处不一致**：")
                        say()
                        say("| 季度截止 | raw | edgartools | 差异 |")
                        say("|---|---|---|---|")
                        for k, x, y, note in diffs[-8:]:
                            xs = f"{x/1e9:.2f} B" if x is not None else "—"
                            ys = f"{y/1e9:.2f} B" if y is not None else "—"
                            say(f"| {k} | {xs} | {ys} | {note} |")
                        say()
                    else:
                        say(f"> ✅ {tk}：两引擎逐季完全一致"
                            f"（{len(d['q'])} 个季度）")
                        say()
        except Exception as e:
            say(f"（{tk}：取数失败 {type(e).__name__}: {str(e)[:60]}）")
            say()
            continue
        if d is None:
            continue
        allq[tk] = d
        qs = quarters_sorted(d, a.quarters)
        say(f"## {CN.get(tk, tk)}（{tk}）")
        say()
        say(f"{name}　·　CIK {cik}　·　标签 `{d['tag']}`")
        say()
        say("| 季度截止 | Capex | 同比 |")
        say("|---|---|---|")
        for e, v in qs:
            y = yoy(d, e)
            ys = f"**{y:+.1f}%**" if y is not None else "—"
            say(f"| {e} | {v/1e9:.2f} B | {ys} |")
        say()
        if len(qs) >= 2:
            trend = [yoy(d, e) for e, _ in qs[-4:]]
            trend = [t for t in trend if t is not None]
            if len(trend) >= 2:
                if trend[-1] > trend[0]:
                    say(f"> 同比增速从 {trend[0]:+.0f}% 抬到 {trend[-1]:+.0f}% —— **在加速**。")
                elif trend[-1] < trend[0]:
                    say(f"> 同比增速从 {trend[0]:+.0f}% 落到 {trend[-1]:+.0f}% —— **在减速**。")
                else:
                    say("> 同比增速大体走平。")
                say()

    # 合计
    if len(allq) >= 2:
        say("## 合计")
        say()
        # 合计的同比要拿到基期,所以多算 5 个季度,只显示最后 a.quarters 个
        all_ends = sorted({e for d in allq.values() for e in d["q"]})
        ends = all_ends[-(a.quarters + 5):]
        show_from = len(ends) - a.quarters
        say("| 季度截止 | " + " | ".join(CN.get(t, t) for t in allq) + " | 合计 | 同比 |")
        say("|---|" + "---|" * (len(allq) + 2))
        tot = {}
        for idx, e in enumerate(ends):
            vals, cells = [], []
            for tk, d in allq.items():
                # 各家财季末不同,取 ±45 天内最近的一季
                best, bd = None, 999
                for e2, v2 in d["q"].items():
                    gap = abs((date.fromisoformat(e) - date.fromisoformat(e2)).days)
                    if gap < bd and gap <= 45:
                        best, bd = v2, gap
                if best is not None:
                    vals.append(best)
                    cells.append(f"{best/1e9:.1f}")
                else:
                    cells.append("—")
            if len(vals) == len(allq):
                s = sum(vals)
                tot[e] = s
                prev = None
                for e2, v2 in tot.items():
                    g = (date.fromisoformat(e) - date.fromisoformat(e2)).days
                    if 345 <= g <= 385:
                        prev = v2
                ys = f"**{(s/prev-1)*100:+.1f}%**" if prev else "—"
                if idx >= show_from:      # 前 5 个季度只用来当同比基期,不显示
                    say(f"| {e} | " + " | ".join(cells)
                        + f" | **{s/1e9:.1f} B** | {ys} |")
        say()

    if a.guidance:
        say("## 下期指引（8-K 新闻稿原文）")
        say()
        say("这一层比上面的季度数据**再领先一个季度** —— 上面是已发生的开支，"
            "这里是公司自己说的下一步打算。")
        say()
        any_hit = False
        for tk in tickers:
            if tk not in ciks:
                continue
            try:
                g = fetch_guidance(tk)
            except Exception as e:
                say(f"- **{CN.get(tk, tk)}（{tk}）**：抓取失败 {type(e).__name__}")
                continue
            if g:
                any_hit = True
                say(f"- **{CN.get(tk, tk)}（{tk}）**　8-K {g['date']}")
                for h in g["hits"]:
                    say(f"  > {h}")
            else:
                say(f"- {CN.get(tk, tk)}（{tk}）：新闻稿里没有指引措辞 —— "
                    f"该公司在电话会口头给，纪要不在 SEC，此渠道拿不到")
            say()
        if not any_hit:
            say("（本轮没抓到任何指引。**拿不到不等于没有指引**，"
                "只是不在 8-K 新闻稿这个渠道。）")
            say()

    say("## 怎么用这组数字")
    say()
    say("- Capex 是 A 股光模块／服务器／液冷这条链的**需求源头**，"
        "领先 A 股业绩约 **1–2 个季度**。")
    say("- **同比增速掉档比绝对额下降更早预警**。绝对额还在涨但增速从 +60% 掉到 +20%，"
        "意味着半年后 A 股这条链的订单增速也会掉。")
    say("- 反过来，A 股在跌但 Capex 同比仍在加速 —— "
        "跌的多半是估值和情绪，不是需求（配合 `preport` 第 7 层海外同业一起看）。")
    say("- 财报有滞后：季度数据在财报发布后才更新，看的是**已发生**的投资。"
        "各家电话会给的**下季度指引**更领先，那要靠 WebSearch，本脚本不覆盖。")
    say()
    say("---")
    say()
    say("**数据源**：SEC EDGAR XBRL companyconcept API　·　"
        "标签见各节标注　·　官方免费无需注册。")
    say()

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
