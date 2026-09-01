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
    ("**北美钻机数**（油服赛道领先指标）",
     "oilpriceapi.com 转载 Baker Hughes　⚠️ 官方站 rigcount.bakerhughes.com "
     "从中国大陆不可达（直连/代理/headless 浏览器全 000），只能走转载源，数字是二手的",
     "https://www.oilpriceapi.com/data/rig-count", UA, "rigcount"),
    ("**人民币汇率中间价**（出口占比高的标的必看）",
     "中国外汇交易中心 chinamoney.com.cn（人民银行授权发布，官方口径）"
     "　⚠️ pageSize ≥ 100 会返回 403 —— 是分页超限不是被封，单页封顶 90 并翻页",
     "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
     "?startDate=2026-08-20&endDate=2026-08-29&currency=USD/CNY&pageNum=1&pageSize=10",
     UA, "fx"),
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
    # ── 以下 6 条 2026-08-28 实测新增(来自一份外部实现的接口清单,逐条验过)──
    ("**券商研报全文 PDF + 产业量价抽句**",
     "东财 reportapi + pdf.dfcfw.com（文本层完好，非扫描件）",
     "https://reportapi.eastmoney.com/report/list"
     "?industryCode=*&pageSize=5&industry=*&rating=*&ratingChange=*"
     "&beginTime=2026-01-01&endTime=2027-01-01&pageNo=1&qType=0&code=300502",
     UA, "research --dig"),
    ("公司公告(含投资者关系记录)", "东财 np-anotice-stock",
     "https://np-anotice-stock.eastmoney.com/api/security/ann"
     "?sr=-1&page_size=5&page_index=1&ann_type=A&client_source=web&stock_list=300502",
     UA, "efdata ann"),
    ("香港中央结算多期持股序列", "东财 RPT_F10_EH_HOLDERS（比十大股东单期长得多）",
     "https://datacenter.eastmoney.com/securities/api/data/v1/get"
     "?reportName=RPT_F10_EH_HOLDERS&pageSize=1&columns=ALL", UA, "hksc"),
    ("北向个股持仓·季度", "东财 RPT_MUTUAL_HOLDSTOCKNORTH_STA",
     "https://datacenter-web.eastmoney.com/api/data/v1/get"
     "?reportName=RPT_MUTUAL_HOLDSTOCKNORTH_STA&pageSize=1&columns=ALL", UA, "hksc"),
    ("北向个股持仓·日度(停更前)", "东财 RPT_MUTUAL_HOLDSTOCKNDATE_STA",
     "https://datacenter-web.eastmoney.com/api/data/v1/get"
     "?reportName=RPT_MUTUAL_HOLDSTOCKNDATE_STA&pageSize=1&columns=ALL", UA, "hksc"),
    ("F10 流通股东(带流通口径比例)", "东财 emweb PC_HSF10",
     "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax"
     "?code=SZ300502", UA, "hksc"),
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
               "微软/亚马逊要靠 transcript，而 transcript 源本机都不通:"
               "**roic.ai 403 · Motley Fool 是付费墙**（2026-08-28 首测记为 404；2026-08-29 在 mac mini 上用带 headless Chromium 的抓取器复测，拿到的具体 transcript 页正文只有 163 字的 “Premium Investing Services” 墙 —— **不是 JS 渲染问题，是收费**，换抓取器没用）（"
               "一份外部实现走的就是这两条路，它自己产出的 best_guidance "
               "五家全是空的）;Seeking Alpha 是 JS 渲染的 Next.js payload;"
               "EarningsCall / Alpha Vantage 有 API 但要 key。"
               "**这一条目前没有免费自动化路径，只能 WebSearch 读转述。**",
        "url": "https://www.fool.com/earnings-call-transcripts/",
        "ua": UA,
    },
    {
        "what": "**产业高频数据的精确值**（1.6T 月度出货量 / 800G 现货报价 / 光芯片供需）",
        "why": "**精确的月度数字**确实只有 LightCounting 等第三方咨询机构的付费库有。"
               "但下面那条『替代』2026-08-28 被推翻了一半 —— 见下。",
        "tried": "akshare（只有宏观进出口总额，无 HS 编码细分）· "
                 "海关总署 stats.customs.gov.cn（**412，WAF 挡爬虫；2026-08-29 用 headless Chromium 的 stealth 模式复测仍 412，真浏览器也过不去**）· "
                 "国贸通 gtradedata.com（页面可达但**数据在付费报告里**）· "
                 "LightCounting 官网 newsletter 页（抓下来 0 条可用条目）",
        "alt": "★ **券商研报 PDF 免费可下，而且里面有真数字，不只是定性描述。**"
               "工具 `research <代码> --dig N`。2026-08-28 实测抽到的:"
               "销量 1119 万只、**产能扩至 2836 万只/年（同比 +86.58%）**、"
               "境外均价约 2103 元/只、"
               "「1.6T 光模块 Q2 出货量较 Q1 明显增长，预计 Q3/Q4 起放量」、"
               "「产业链原材料整体较为紧张」。"
               "路径:东财 `reportapi` 列研报 → `pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf` "
               "直接下载，**文本层完好不是扫描件**。"
               "**仍拿不到的**:LightCounting 那种全行业月度出货与现货报价序列 —— "
               "研报给的是**这一家**的量价，不是全行业的。"
               "此外海关 HS 编码 **85177060（光通信激光收发模块）** 月度出口额公开发布，"
               "但海关站有 WAF，目前只能靠 WebSearch 读新闻转载的数字。"
               "⚠ 研报是**卖方观点**:拿它的产业事实与评级变动方向，别拿目标价当依据。",
        "url": "https://reportapi.eastmoney.com/report/list"
               "?industryCode=*&pageSize=1&industry=*&rating=*&ratingChange=*"
               "&beginTime=2026-01-01&endTime=2027-01-01&pageNo=1&qType=0&code=300502",
        "ua": UA,
    },
]

# ── 曾经试过但不用的(避免重复踩)─────────────────────────────────────────
# 依赖缺失导致的「静默降级」——不是网络问题，但表现得像数据源没了。
# 2026-08-29 在 mac mini 全新装机时两条都踩到：
# (import 名, pip 名, 影响什么, 缺了会怎样)
# ⚠ import 名 ≠ pip 名：edgartools 装完 import 的是 edgar，pymupdf 装完 import 的是 fitz。
#   用 pip 名去 find_spec 会永远报「缺失」——这条注释是踩过才写的。
DEPS = [
    ("fitz", "pymupdf", "research --dig 的产业抽句",
     "缺了照样出研报列表，只是「产业数据抽句」整节变成一行安装提示 —— "
     "最容易被当成「这批研报没有量价数据」，其实是没装库"),
    ("edgar", "edgartools", "capex / capex --guidance",
     "缺了直接执行失败，领先指标整块没有"),
]

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

    import importlib.util
    missing = [(mod, pkg, what, note) for mod, pkg, what, note in DEPS
               if importlib.util.find_spec(mod) is None]
    if missing:
        say("## 四　本机缺的 Python 依赖（会让工具静默降级）")
        say()
        say("这一节和上面三节不同：**不是网络问题，但表现得像数据源没了**。")
        say()
        say("| 包 | 影响什么 | 缺了会怎样 |")
        say("|---|---|---|")
        for _mod, pkg, what, note in missing:
            say(f"| `{pkg}` | {what} | {note} |")
        say()
        say("修复：`.venv/bin/pip install " + " ".join(pkg for _, pkg, _, _ in missing) + "`")
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
