#!/usr/bin/env python3
"""probe_sources —— 数据源可达性总探针(含已知拿不到的,附原因与替代)。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/probe_sources.py
    $P ... --md ~/probe.md       # 存 markdown
    $P ... --only blocked        # 只看已知拿不到的那些及其原因

为什么要有这个:
    「哪些数据能拿到」这件事**会变**(接口改版、监管改披露规则、封装库烂尾),
    而每次都靠人重新搜一遍太慢。这个脚本把**已经搜过、试过、验证过的结论**
    固化成可重跑的代码 —— 换机器、隔几个月回来,跑一次就知道现状。

    尤其重要的是**已知拿不到的那些**:写清楚为什么拿不到、试过什么、
    替代方案是什么。否则下次又会从头搜一遍,或者误以为是接口坏了去修。

代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SEC_UA = os.environ.get("SEC_UA", "astock-lab-user your-email@example.com")

_OUT = []


def say(s=""):
    print(s)
    _OUT.append(s)


def http(url, ua=UA, timeout=12, referer=None):
    hdr = {"User-Agent": ua}
    # 新浪行情接口强制校验 Referer,不带会 403 —— 这不是不可达
    if referer is None and "sinajs" in url:
        referer = "https://finance.sina.com.cn"
    if referer:
        hdr["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


# ── 能用的源 ────────────────────────────────────────────────────────────────
WORKING = [
    ("行情快照 / 五档 / 日线", "新浪 hq.sinajs.cn",
     "https://hq.sinajs.cn/list=sz300502", UA, "astock"),
    ("逐笔成交", "腾讯 web.sqt.gtimg.cn",
     "https://web.sqt.gtimg.cn/q=sz300502", UA, "astock"),
    ("龙虎榜 / 股东户数 / 全市场业绩 / 可转债", "东财 datacenter-web",
     "https://datacenter-web.eastmoney.com/api/data/v1/get"
     "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&pageSize=1&columns=ALL",
     UA, "efdata"),
    ("机构调研", "东财 RPT_ORG_SURVEYNEW（akshare 封装已坏，走直连）",
     "https://datacenter-web.eastmoney.com/api/data/v1/get"
     "?reportName=RPT_ORG_SURVEYNEW&pageSize=1&columns=ALL", UA,
     "efdata survey"),
    ("北向成交额与占比", "东财 RPT_MUTUAL_TOP10DEAL（MUTUAL_TYPE 003=深股通）",
     "https://datacenter-web.eastmoney.com/api/data/v1/get"
     "?reportName=RPT_MUTUAL_TOP10DEAL&pageSize=1&columns=ALL"
     "&filter=%28MUTUAL_TYPE%3D%22003%22%29", UA, "手工 / 见 SKILL §6"),
    ("前十大流通股东", "东财 emh5", "https://emh5.eastmoney.com", UA, "preport"),
    ("基金净值 / 持仓", "东财 fundmobapi",
     "https://fundmobapi.eastmoney.com", UA, "efdata fund*"),
    ("北美云厂 Capex（领先指标）", "SEC EDGAR XBRL（UA 必须带联系方式）",
     "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/"
     "us-gaap/PaymentsToAcquirePropertyPlantAndEquipment.json", SEC_UA, "capex"),
    ("云厂下期 Capex 指引（最领先）", "SEC 8-K 的 EX-99.1 新闻稿",
     "https://www.sec.gov/files/company_tickers.json", SEC_UA,
     "capex --guidance"),
    ("美股日线（海外同业）", "新浪美股（akshare stock_us_daily）",
     "https://stock.finance.sina.com.cn", UA, "preport 第 7 层"),
    ("美股实时报价", "Nasdaq API",
     "https://api.nasdaq.com/api/quote/NVDA/info?assetclass=stocks", UA, "手工"),
    ("美股财务速览", "stockanalysis.com",
     "https://stockanalysis.com/stocks/nvda/financials/", UA, "手工"),
]

# ── 明确拿不到的源(附原因、试过什么、替代)───────────────────────────────
BLOCKED = [
    {
        "what": "北向资金**净买方向**（个股级）",
        "why": "监管 2024-05-13 停日内实时、**2024-08-19 停日终数据改按季公布**。"
               "不是接口坏了，是不再披露。",
        "tried": "akshare 全部 hsgt 接口（`individual_em` 数据停在 2024-08-16）· "
                 "adata（无北向模块）· 东财 RPT_MUTUAL_TOP10DEAL 的 "
                 "`NET_BUY_AMT` 字段为 None · 港交所 CCASS（那是港股持股，"
                 "A 股北向在中登）",
        "alt": "**深股通/沪股通前十大成交活跃股**仍每日公布 —— 拿得到"
               "**成交额与占该股总成交比例（参与度）**，拿不到方向。"
               "另有季度持股与季报十大流通股东里的「香港中央结算」（滞后最长 5 个月）。",
        "url": "https://datacenter-web.eastmoney.com/api/data/v1/get"
               "?reportName=RPT_MUTUAL_TOP10DEAL&pageSize=1&columns=ALL",
        "ua": UA,
    },
    {
        "what": "微软 / 亚马逊的 **Capex 指引**",
        "why": "这两家在**电话会口头给**指引，不写进 8-K 新闻稿；"
               "电话会纪要不属于 SEC 披露文件。",
        "tried": "SEC 8-K 的 EX-99.1 全文正则扫（谷歌、Meta 命中，这两家零命中）",
        "alt": "谷歌、Meta 的指引拿得到（`capex --guidance`）。"
               "微软/亚马逊要靠 transcript：Motley Fool / Seeking Alpha 页面可达但"
               "是 JS 渲染的 Next.js payload，需要浏览器渲染才能取；"
               "EarningsCall / Alpha Vantage 有 API 但要 key。",
        "url": "https://www.fool.com/earnings-call-transcripts/",
        "ua": UA,
    },
    {
        "what": "**产业高频数据**（1.6T 月度出货量 / 800G 报价 / 光芯片供需）",
        "why": "不在任何免费公开源。券商产业链调研报告与 LightCounting 等"
               "第三方咨询机构的付费数据库才有。",
        "tried": "akshare（只有宏观进出口总额，无 HS 编码细分）· "
                 "海关总署 stats.customs.gov.cn（**412，WAF 挡爬虫**）· "
                 "国贸通 gtradedata.com（页面可达但**数据在付费报告里**）",
        "alt": "**海关月度出口是个可行方向但目前取不到**：HS 编码 "
               "**85177060 = 光通信设备的激光收发模块**，月度出口额公开发布，"
               "财经媒体会转载具体数字。可靠的自动化路径需要突破海关网站的 WAF，"
               "或找已经整理好的第三方免费源。**目前只能靠 WebSearch 读新闻里的数字。**",
        "url": "http://stats.customs.gov.cn/",
        "ua": UA,
    },
]

# ── 曾经试过但不用的(避免重复踩)─────────────────────────────────────────
REJECTED = [
    ("Yahoo Finance / yfinance", "本机 403（直连）/ 429（走代理）",
     "美股日线改用 akshare `stock_us_daily`（新浪源）"),
    ("stooq", "JS 挑战页，脚本取不到", "同上"),
    ("finviz", "403 挡爬虫", "—"),
    ("financialmodelingprep", "401，付费", "—"),
    ("东财 push2 / push2his 的行情类接口", "本机取不到（按接口路径分，非协议非主机）",
     "行情用新浪 + 腾讯"),
    ("mootdx（通达信）", "TCP 7709 通但取数返回空，库烂尾", "K 线用新浪 / 腾讯"),
]


def main():
    p = argparse.ArgumentParser(
        prog="probe_sources",
        description="数据源可达性总探针（含已知拿不到的及其原因与替代）")
    p.add_argument("--only", choices=["working", "blocked", "rejected"],
                   help="只看某一类")
    p.add_argument("--md", metavar="文件", help="存 markdown")
    a = p.parse_args()

    say("# 数据源可达性探针")
    say()
    say(f"> {time.strftime('%Y-%m-%d %H:%M')}　·　"
        f"本机实测，不是抄文档")
    say()

    fails = 0
    if a.only in (None, "working"):
        say("## 一　能用的源")
        say()
        say("| 数据 | 来源 | 状态 | 工具 |")
        say("|---|---|---|---|")
        for what, src, url, ua, tool in WORKING:
            code = http(url, ua=ua)
            # 404/403 说明主机**可达**（只是路径不对或需额外 header），
            # 对「这个源还在不在」这个判断够了。只有 None（超时/连接失败）才算断。
            ok = code is not None
            mark = ("✅ 可达" if code == 200
                    else f"✅ 主机可达（{code}）" if ok else "❌ 不可达")
            if not ok:
                fails += 1
            say(f"| {what} | {src} | {mark} | `{tool}` |")
        say()

    if a.only in (None, "blocked"):
        say("## 二　明确拿不到的（附原因、试过什么、替代）")
        say()
        say("**这一节是本脚本最重要的部分。**下次别再从头搜一遍，"
            "也别误以为是接口坏了去修。")
        say()
        for b in BLOCKED:
            code = http(b["url"], ua=b["ua"])
            say(f"### {b['what']}")
            say()
            say(f"- **为什么拿不到**：{b['why']}")
            say(f"- **试过**：{b['tried']}")
            say(f"- **替代**：{b['alt']}")
            say(f"- 相关端点当前状态：`{b['url'][:70]}` → "
                f"**{code if code else '不可达'}**")
            say()

    if a.only in (None, "rejected"):
        say("## 三　试过但不用的（避免重复踩）")
        say()
        say("| 源 | 为什么不用 | 改用什么 |")
        say("|---|---|---|")
        for src, why, alt in REJECTED:
            say(f"| {src} | {why} | {alt} |")
        say()

    say("---")
    say()
    say("**维护约定**：每次发现新的可用源或确认某个源不可用，"
        "**改这个脚本的 WORKING / BLOCKED / REJECTED 三个表**，"
        "不要只写进文档 —— 文档不会被执行，代码会。")
    say()

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")

    if fails:
        print(f"\n⚠ {fails} 个「能用的源」这次不可达 —— 检查网络，"
              f"或该源真的变了（那就改脚本）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
