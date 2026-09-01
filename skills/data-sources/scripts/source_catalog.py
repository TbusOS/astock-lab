#!/usr/bin/env python3
"""数据源目录 —— 每一条:分类 / 名称 / **链接** / 是事实还是预测 / 落在哪 / 怎么重跑。

    source_catalog.py [--markdown] [--group quotes,financials] [--code 300502]

为什么单独一份:
    2026-09-02 用户指出报告里「只写来源不给链接」。原因是链接散在两个地方 ——
    一部分记在抓取时的信封里(`url` 字段),一部分根本没记。
    结果是文档只能被相信,不能被点开核对。

    这份是**唯一权威表**。抓取代码和文档生成都从这里取,
    改链接只改一处。信封里的 url 和这里不一致时,以这里为准
    (信封记的是当次实际请求的地址,可能带参数;这里记的是人能打开的页面)。

★ 「是事实还是预测」这一列是这张表最重要的信息:
    事实类(公司报表、SEC 申报、交易所披露、价格)可以进模型;
    预测类(一致预期、评级、目标价)**只能放在报告最后一节作对照**。
    混用是这个项目最容易犯、也最难发现的错。
"""
from __future__ import annotations

import argparse

FACT, PRED = "事实", "别人的预测"

# (分类, 源名, 拿到什么, 链接, 事实/预测, 落盘路径, 重跑命令)
CATALOG = [
    # ── 价格 ──────────────────────────────────────────────────────────
    ("行情", "baostock query_history_k_data_plus", "日线(前复权)、PE-TTM、PB",
     "http://baostock.com/baostock/index.php/A股K线数据", FACT,
     "data/raw/quotes/<代码>/<日期>-qfq.json",
     "tools/fetch_all.py --codes <代码> --group quotes"),
    ("行情", "baostock query_adjust_factor", "复权因子(送转归一用)",
     "http://baostock.com/baostock/index.php/复权因子信息", FACT,
     "data/raw/quotes/<代码>/<日期>-adjfactor.json",
     "tools/fetch_all.py --codes <代码> --group quotes"),

    # ── 公司报表 ──────────────────────────────────────────────────────
    ("公司报表", "新浪 stock_financial_report_sina",
     "三大表**全历史**(利润表 83 列 / 资产负债表 147 列 / 现金流量表)",
     "https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/<代码>.phtml",
     FACT, "data/raw/financials/<代码>/<日期>-利润表.json 等",
     "tools/fetch_all.py --codes <代码> --group statements"),
    ("公司报表", "同花顺 stock_financial_abstract_ths",
     "按单季度的 25 项关键指标(和上面互相核对)",
     "https://basic.10jqka.com.cn/<代码>/finance.html", FACT,
     "data/raw/financials/<代码>/<日期>-单季指标.json",
     "tools/fetch_all.py --codes <代码> --group statements"),
    ("公司报表", "baostock query_profit_data", "季度盈利能力 + **总股本**",
     "http://baostock.com/baostock/index.php/季频盈利能力", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),
    ("公司报表", "baostock query_growth_data", "季度成长能力",
     "http://baostock.com/baostock/index.php/季频成长能力", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),
    ("公司报表", "baostock query_balance_data", "季度偿债能力",
     "http://baostock.com/baostock/index.php/季频偿债能力", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),
    ("公司报表", "baostock query_cash_flow_data", "季度现金流比率",
     "http://baostock.com/baostock/index.php/季频现金流量", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),
    ("公司报表", "baostock query_dupont_data", "杜邦拆解",
     "http://baostock.com/baostock/index.php/季频杜邦指数", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),
    ("公司报表", "baostock query_operation_data", "营运能力(周转率/周转天数)",
     "http://baostock.com/baostock/index.php/季频营运能力", FACT,
     "data/raw/financials/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group financials"),

    # ── 公司自己说的 ──────────────────────────────────────────────────
    ("公司自己说的", "baostock query_forecast_report", "业绩预告(公司自己发的区间)",
     "http://baostock.com/baostock/index.php/季频公司业绩预告", FACT,
     "data/raw/forecast/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group forecast"),
    ("公司自己说的", "baostock query_performance_express_report", "业绩快报",
     "http://baostock.com/baostock/index.php/季频公司业绩快报", FACT,
     "data/raw/forecast/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group forecast"),
    ("公司自己说的", "东财 np-anotice-stock", "交易所公告列表(含 PDF 链接)",
     "https://data.eastmoney.com/notices/stock/<代码>.html", FACT,
     "data/raw/announcements/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group announcements"),
    ("公司自己说的", "东财 RPT_ORG_SURVEYNEW", "机构调研记录(公司在会上回答了什么)",
     "https://data.eastmoney.com/jgdy/", FACT,
     "data/raw/surveys/<代码>/<日期>.json",
     "tools/fetch_all.py --codes <代码> --group surveys"),

    # ── 筹码 ──────────────────────────────────────────────────────────
    ("筹码", "东财 RPT_HOLDERNUMLATEST", "股东户数及上期对比",
     "https://data.eastmoney.com/gdhs/", FACT,
     "data/raw/chips/<代码>/<日期>-股东户数.json",
     "tools/fetch_all.py --codes <代码> --group chips"),
    ("筹码", "efinance get_top10_stock_holder_info", "前十大流通股东及变动",
     "https://data.eastmoney.com/gdfx/HoldingAnalyse.html", FACT,
     "data/raw/chips/<代码>/<日期>-前十大流通股东.json",
     "tools/fetch_all.py --codes <代码> --group chips"),
    ("筹码", "东财 RPT_DMSK_TS_STOCKNEW", "当日主力/超大单资金流",
     "https://data.eastmoney.com/zjlx/detail.html", FACT,
     "data/raw/chips/<代码>/<日期>-资金流.json",
     "tools/fetch_all.py --codes <代码> --group chips"),
    ("筹码", "东财 RPT_BLOCKTRADE_STA", "大宗交易(折溢价、成交额)",
     "https://data.eastmoney.com/dzjy/", FACT,
     "data/raw/chips/<代码>/<日期>-大宗交易.json",
     "tools/fetch_all.py --codes <代码> --group chips"),
    ("筹码", "akshare stock_restricted_release_detail_em", "限售解禁明细",
     "https://data.eastmoney.com/dxf/", FACT,
     "data/raw/chips/<代码>/<日期>-解禁.json",
     "tools/fetch_all.py --codes <代码> --group chips"),
    ("筹码", "akshare stock_margin_detail_szse", "深交所融资融券明细",
     "https://www.szse.cn/disclosure/margin/margin/", FACT,
     "data/raw/chips/<代码>/<日期>-融资融券.json",
     "tools/fetch_all.py --codes <代码> --group chips"),

    # ── 海外(事实)────────────────────────────────────────────────────
    ("海外·事实", "SEC XBRL companyconcept",
     "**云厂/油气公司季度资本开支**,10-Q/10-K 申报原值,全历史,免费无需 key",
     "https://data.sec.gov/api/xbrl/companyconcept/", FACT,
     "data/raw/overseas_facts/<日期>-capex-<分组>.json",
     "tools/sec_facts.py capex"),
    ("海外·事实", "yfinance quarterly_cashflow / quarterly_income_stmt",
     "海外同业季度营收、资本开支(10-Q 转录)",
     "https://finance.yahoo.com/quote/<美股代码>/cash-flow", FACT,
     "data/raw/overseas/<美股代码>/<日期>-季度财报.json",
     "tools/fetch_all.py --peers"),
    ("海外·事实", "Motley Fool 电话会纪要", "云厂高管**自己说的**指引原文,免费全文",
     "https://www.fool.com/earnings/call-transcripts/", FACT,
     "data/raw/transcripts/<公司-季度>/<日期>.json",
     "tools/fetch_all.py --transcripts <URL>"),

    # ── 行业量价 ──────────────────────────────────────────────────────
    ("行业量价", "台湾证交所/柜买 OpenAPI t187ap05",
     "台湾光通信 11-14 家**月营收**及同比,**滞后仅 1 个月,唯一领先指标**",
     "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", FACT,
     "data/raw/industry/<日期>-台湾光通信月营收.json", "tools/industry.py taiwan"),
    ("行业量价", "UN Comtrade", "中国光模块(HS 851762)出口金额/量/**均价**,滞后 21 个月",
     "https://comtradeplus.un.org/", FACT,
     "data/raw/industry/<日期>-光模块出口量价.json", "tools/industry.py comtrade"),
    ("行业量价", "LightCounting 官方 newsletter", "行业增速/市场规模,免费全文 43 篇",
     "https://www.lightcounting.com/newsletters", FACT,
     "data/raw/industry/<日期>-LightCounting-newsletter.json", "tools/industry.py lc"),
    ("行业量价", "券商研报正文抽取(pymupdf)",
     "研报里引用的 LightCounting / Yole / Omdia 第三方数据(二手,交叉验证用)",
     "https://data.eastmoney.com/report/", FACT,
     "data/raw/industry/<日期>-研报第三方引用.json", "tools/industry.py cite"),

    # ── 宏观 ──────────────────────────────────────────────────────────
    ("宏观", "CFETS 人民币汇率中间价", "汇率(影响财务费用里的汇兑)",
     "https://www.chinamoney.com.cn/chinese/bkccpr/", FACT,
     "data/raw/macro/<日期>-汇率中间价.json", "tools/fetch_all.py --macro"),
    ("宏观", "akshare bond_zh_us_rate", "中美国债收益率",
     "https://data.eastmoney.com/cjsj/zmgzsyl.html", FACT,
     "data/raw/macro/<日期>-中美国债.json", "tools/fetch_all.py --macro"),
    ("宏观", "新浪外盘期货", "原油(杰瑞股份的需求端)",
     "https://finance.sina.com.cn/futures/quotes/CL.shtml", FACT,
     "data/raw/macro/<日期>-油价.json", "tools/fetch_all.py --macro"),

    # ── 别人的预测(只作对照)──────────────────────────────────────────
    ("★ 别人的预测", "akshare stock_profit_forecast_ths", "卖方一致预期(分年度净利)",
     "https://basic.10jqka.com.cn/<代码>/worth.html", PRED,
     "data/raw/consensus/<代码>/<日期>-ths.json",
     "tools/fetch_all.py --codes <代码> --group consensus"),
    ("★ 别人的预测", "akshare stock_rank_forecast_cninfo", "券商评级与目标价",
     "http://www.cninfo.com.cn/", PRED,
     "data/raw/ratings/<代码>/<日期>-cninfo.json",
     "tools/fetch_all.py --codes <代码> --group ratings"),
    ("★ 别人的预测", "东财 reportapi + pdf.dfcfw.com", "研报列表与 PDF 原文",
     "https://data.eastmoney.com/report/", PRED,
     "data/raw/research/<代码>/<日期>-list.json", "tools/fetch_all.py --codes <代码> --group research"),
    ("★ 别人的预测", "yfinance analyst", "海外标的的目标价、评级变动、一致预期",
     "https://finance.yahoo.com/quote/<美股代码>/analysis", PRED,
     "data/raw/consensus/<美股代码>/<日期>-yahoo.json", "tools/fetch_all.py --peers"),

    # ── 人工投喂 ──────────────────────────────────────────────────────
    ("人工投喂", "外部研报 PDF", "投行研报(订阅制,没有自动渠道,靠人放进目录)",
     "—(人工)", PRED, "private/extern_research/<报告>/filled.json",
     "tools/ingest_report.py batch <放 pdf 的目录>"),
    ("人工投喂", "券商 APP 截图", "**持仓成本、股数、融资负债、维持担保比例**",
     "—(人工,原图存 长江证券我的持仓/)", FACT,
     "private/portfolio/account.json", "按券商界面手工更新"),
]

GROUPS = sorted({c[0] for c in CATALOG}, key=lambda x: (x.startswith("★"), x))


def rows(groups=None, code=None):
    out = []
    for g, name, what, url, kind, path, cmd in CATALOG:
        if groups and g not in groups:
            continue
        # 只替换 A 股占位符。海外源写的是 <美股代码>,不能拿 A 股代码去套 ——
        # 会生成 finance.yahoo.com/quote/300502 这种打不开的假地址。
        rep = (lambda s: s.replace("<代码>", code) if code else s)
        out.append((g, name, what, rep(url), kind, rep(path), rep(cmd)))
    return out


def markdown(groups=None, code=None, heading="数据来源:每条都能点开核对") -> str:
    """heading=None 时不出标题 —— 调用方自己已经写了小节标题的场景。"""
    L = ([f"## {heading}", ""] if heading else [])
    L.append("**「事实 / 别人的预测」这一列最重要。** 事实类(公司报表、SEC 申报、"
             "交易所披露、价格)可以进模型;预测类**只放在报告的市场对照那一节**,"
             "不进任何一步推导。混用是这类分析最容易犯、也最难发现的错。")
    L.append("")
    # 六列会把「拿到什么」挤成每行两个字,读不了。
    # 落盘路径和重跑命令在同一组里基本一样,提到组标题下面写一次,表里只留四列。
    by_group: dict[str, list] = {}
    for r in rows(groups, code):
        by_group.setdefault(r[0], []).append(r)

    for g, rs in by_group.items():
        L.append("")
        L.append(f"### {g}")
        L.append("")
        paths = sorted({r[5] for r in rs})
        cmds = sorted({r[6] for r in rs})
        L.append("落在哪:" + "、".join(f"`{x}`" for x in paths))
        L.append("")
        L.append("怎么重跑:" + "、".join(f"`{x}`" for x in cmds))
        L.append("")
        L.append("| 源 | 拿到什么 | 事实/预测 | 链接 |")
        L.append("|---|---|---|---|")
        for _, name, what, url, kind, _, _ in rs:
            # md2html 只认 [文字](链接),`<url>` 会被转义成字面量。
            # 链接文字用完整 URL —— 这份文档是拿去核对的,打印出来也要看得见地址。
            # 占位符没被替换掉时**不做成超链接** —— 点了打不开的链接比没有链接更糟,
            # 它让人以为核对过了。留成纯文本,占位符看得见,读者知道要自己换。
            link = (url if url.startswith("—") or "<" in url
                    else f"[{url}]({url})")
            k = "**事实**" if kind == FACT else "**预测**"
            L.append(f"| {name} | {what} | {k} | {link} |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--group", help="逗号分隔;不给就全部")
    ap.add_argument("--code")
    a = ap.parse_args()
    gs = [x.strip() for x in a.group.split(",")] if a.group else None
    if a.markdown:
        print(markdown(gs, a.code))
    else:
        for r in rows(gs, a.code):
            print(f"[{r[0]}] {r[1]}\n    {r[3]}\n    → {r[5]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
