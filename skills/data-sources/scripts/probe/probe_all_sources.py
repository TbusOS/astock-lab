#!/usr/bin/env python3
"""数据源全量探测 —— 逐条真调一次,输出可复核的状态表。

为什么要有这个脚本:
  2026-09-01 发现我们 SKILL.md 与报告里记的「拿不到」有三条是错的
  (Motley Fool transcript / roic.ai / stockanalysis.com 实测全是 200)。
  凭记忆维护数据源清单必然过期,所以清单必须由**每次真调**生成。

用法:
  $VENV/bin/python tools/probe_all_sources.py            # 全量,输出人类可读表
  $VENV/bin/python tools/probe_all_sources.py --json out.json
  $VENV/bin/python tools/probe_all_sources.py --group 领先指标

退出码:0 = 必需源全通;1 = 有必需源不通。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field, asdict

# ── 代理:只在本进程去掉,不动 shell 全局(Claude Code 还要用)────────────
# 注意 a-stock-ai 实测的坑:Clash **TUN 模式**在 L3 路由层截流,env 变量是应用层,
# 这段对 TUN 无效。本机能用是因为 Clash 规则里对国内财经域名走了 DIRECT。
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

socket.setdefaulttimeout(30)          # 兜底:akshare / efinance 内部不带超时

import requests  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")
SEC_UA = os.environ.get("SEC_UA", "")   # SEC 要求 UA 里带联系邮箱,否则 403

TIMEOUT = 25


@dataclass
class Probe:
    group: str
    name: str
    lead: str                 # 领先 / 同步 / 滞后 / 元数据
    what: str                 # 拿到什么
    how: str                  # 怎么拿(工具或接口)
    url: str                  # 可点开复核的链接
    required: bool = False
    status: str = ""
    detail: str = ""
    ms: int = 0


def http(p: Probe, url=None, headers=None, params=None, ok=None, _try=0):
    t0 = time.time()
    try:
        r = requests.get(url or p.url, headers=headers or {"User-Agent": UA},
                         params=params, timeout=TIMEOUT)
        p.ms = int((time.time() - t0) * 1000)
        note = ""
        if ok:
            try:
                note = ok(r)
            except Exception as e:                       # 解析失败也是失败
                p.status, p.detail = "PARSE", f"{type(e).__name__}: {e}"[:110]
                return
        p.status = "OK" if r.status_code == 200 else str(r.status_code)
        p.detail = note or f"{len(r.text):,}B"
    except Exception as e:
        # 网络类抖动(SSL/连接/代理)重试一次 —— 这几个源在本机时通时不通,
        # 一次失败就记 FAIL 会让清单看起来比实际差。协议类错误不重试。
        if _try == 0 and any(k in type(e).__name__ for k in ("SSL", "Connection", "Proxy", "Timeout")):
            time.sleep(1.5)
            return http(p, url, headers, params, ok, _try=1)
        p.ms = int((time.time() - t0) * 1000)
        p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]


def call(p: Probe, fn):
    t0 = time.time()
    try:
        p.detail = str(fn())[:110]
        p.status = "OK"
    except Exception as e:
        p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]
    p.ms = int((time.time() - t0) * 1000)


# ══════════════════════════════════════════════════════════════════════
def build() -> list[tuple[Probe, callable]]:
    """(探针, 执行函数) 列表。执行函数接收探针本身。"""
    P: list[tuple[Probe, callable]] = []

    def add(pr, fn):
        P.append((pr, fn))

    # ── 1. 行情与技术位 ────────────────────────────────────────────────
    add(Probe("1 行情与技术位", "新浪实时快照", "同步", "五档/最新价/成交量",
              "tools/astock.py", "https://hq.sinajs.cn/list=sz300502", required=True),
        lambda p: http(p, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"},
                       ok=lambda r: "有报价" if "var hq_str" in r.text and len(r.text) > 40 else "空"))

    add(Probe("1 行情与技术位", "腾讯实时/逐笔", "同步", "快照 + 逐笔明细",
              "tools/astock.py", "http://qt.gtimg.cn/q=sz300502", required=True),
        lambda p: http(p, ok=lambda r: "有报价" if "~" in r.text and len(r.text) > 40 else "空"))

    add(Probe("1 行情与技术位", "腾讯日线(前复权)", "滞后", "OHLCV 历史",
              "tools/astock.py", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz300502,day,,,60,qfq"),
        lambda p: http(p, ok=lambda r: f"{len(r.json()['data']['sz300502']['qfqday'])} 根"))

    add(Probe("1 行情与技术位", "baostock 日线+估值", "滞后",
              "OHLCV + **peTTM/pbMRQ/psTTM**(历史分位的唯一来源)",
              "baostock query_history_k_data_plus", "http://baostock.com/baostock/index.php/A股K线数据", required=True),
        _bs_kline)

    add(Probe("1 行情与技术位", "新浪美股日线", "领先", "海外同业股价(市场先反应)",
              "akshare stock_us_daily", "https://finance.sina.com.cn/stock/usstock/"),
        lambda p: _ak(p, "stock_us_daily", symbol="AMAT"))

    # ── 2. A 股财务(baostock 主干)────────────────────────────────────
    for fn_name, cn, lead, why in [
        ("query_profit_data",   "盈利能力", "滞后", "ROE/净利率/毛利率/EPS"),
        ("query_growth_data",   "成长能力", "滞后", "营收/净利/总资产同比"),
        ("query_balance_data",  "偿债能力", "滞后", "流动比/速动比/**资产负债率**/权益乘数/总负债同比"),
        ("query_cash_flow_data", "现金流", "滞后", "经营现金流/营收、每股现金流"),
        ("query_dupont_data",   "杜邦分解", "滞后", "ROE = 净利率 × 周转率 × 权益乘数"),
        ("query_operation_data", "营运能力", "滞后", "应收周转率/存货周转率"),
    ]:
        add(Probe("2 A股财务(baostock)", f"{cn} {fn_name}", lead, why,
                  f"baostock {fn_name}", "http://baostock.com/baostock/index.php/季频财务数据"),
            _mk_bs_fin(fn_name))

    add(Probe("2 A股财务(baostock)", "业绩预告 query_forecast_report", "**领先**",
              "净利预告区间,比正式财报早 2-6 周。无预告本身也是信息(变动在 ±50% 内)",
              "baostock query_forecast_report", "http://baostock.com/baostock/index.php/季频盈利能力"),
        _bs_forecast)

    add(Probe("2 A股财务(baostock)", "业绩快报 query_performance_express_report", "**领先**",
              "正式财报前的营收/净利快报", "baostock query_performance_express_report",
              "http://baostock.com/baostock/index.php/业绩快报"),
        _bs_express)

    # ⚠ baostock **没有** query_st_stocks / query_suspended_stocks /
    #   query_terminated_stocks —— 2026-09-01 实测 AttributeError。
    #   参考仓 a-stock-ai 的 spec 11 声称"逐个确认存在",是错的。
    #   ST / 停牌真正的拿法是下面两条。
    add(Probe("2 A股财务(baostock)", "全市场状态 query_all_stock", "元数据",
              "**tradeStatus** 1=正常 0=停牌;配 query_stock_basic 的名称含 ST 判风险警示。"
              "只能当验证标签,不能当打分输入(事后贴标 = 马后炮 + 回测前视)",
              "baostock query_all_stock", "http://baostock.com/baostock/index.php/证券代码查询"),
        _bs_all_stock)

    add(Probe("2 A股财务(baostock)", "复权因子 query_adjust_factor", "元数据",
              "回测可复现的前提 —— 目标价/成本价与日线必须在同一复权基准上。"
              "2026-09-01 踩过:拿未复权目标价比未复权日线,基准混了(送转系数 1.40)",
              "baostock query_adjust_factor", "http://baostock.com/baostock/index.php/复权因子"),
        _bs_adjust)

    add(Probe("2 A股财务(baostock)", "行业分类 query_stock_industry", "元数据",
              "行业中性化 / 同业对照的分组依据", "baostock query_stock_industry",
              "http://baostock.com/baostock/index.php/行业分类"),
        _bs_industry)

    add(Probe("2 A股财务(baostock)", "交易日历 query_trade_dates", "元数据",
              "所有定时任务的前置闸:非交易日直接跳过", "baostock query_trade_dates",
              "http://baostock.com/baostock/index.php/交易日查询"),
        _bs_dates)

    # ── 3. 财务与筹码(东财)────────────────────────────────────────────
    add(Probe("3 财务与筹码(东财)", "datacenter-web 主机", "—", "报表类数据总入口",
              "requests", "http://datacenter-web.eastmoney.com/api/data/v1/get", required=True),
        lambda p: http(p, params={"reportName": "RPT_ORG_SURVEYNEW", "columns": "ALL",
                                  "pageNumber": 1, "pageSize": 1, "sortColumns": "NOTICE_DATE",
                                  "sortTypes": -1, "source": "WEB", "client": "WEB"},
                       headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
                       ok=lambda r: "有数据" if r.json().get("result") else "空"))

    add(Probe("3 财务与筹码(东财)", "机构调研 RPT_ORG_SURVEYNEW", "**领先**",
              "接待机构数 / 会议形式 / 时间 / 接待人。异常时点(周末夜间)+ 家数骤变是信号",
              "datacenter-web reportName=RPT_ORG_SURVEYNEW",
              "https://data.eastmoney.com/jgdy/"),
        _em_survey)

    add(Probe("3 财务与筹码(东财)", "股东户数", "滞后", "户数 / 户均持股 / 环比",
              "efinance get_latest_holder_number", "https://data.eastmoney.com/gdhs/"),
        lambda p: _ef(p, "get_latest_holder_number"))

    add(Probe("3 财务与筹码(东财)", "全市场季度业绩(基准率底料)", "滞后",
              "算「起始 ≥N% 增速四季后仍 ≥50%」的外部视角基准率",
              "efinance get_all_company_performance", "https://data.eastmoney.com/bbsj/"),
        lambda p: _ef(p, "get_all_company_performance"))

    add(Probe("3 财务与筹码(东财)", "现金流量表(A股 capex)", "滞后",
              "购建固定资产支付的现金 —— **面板厂(京东方/TCL)capex 就靠这个**",
              "akshare stock_cash_flow_sheet_by_report_em",
              "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?code=SZ000725"),
        lambda p: _ak(p, "stock_cash_flow_sheet_by_report_em", symbol="SZ000725"))

    # ── 4. 融资 / 解禁 / 港资 ─────────────────────────────────────────
    add(Probe("4 杠杆与解禁", "融资融券(深)", "同步", "融资余额 / 当日买入",
              "akshare stock_margin_detail_szse", "https://www.szse.cn/disclosure/margin/margin/"),
        lambda p: _ak(p, "stock_margin_detail_szse", date="20260828"))

    add(Probe("4 杠杆与解禁", "限售解禁", "**领先**", "未来解禁日期与市值",
              "akshare stock_restricted_release_detail_em", "https://data.eastmoney.com/dxf/"),
        lambda p: _ak(p, "stock_restricted_release_detail_em",
                      start_date="20260901", end_date="20261231"))

    # ── 5. 一致预期与评级 ─────────────────────────────────────────────
    add(Probe("5 一致预期与评级", "同花顺盈利预测", "**领先**",
              "分年度净利/EPS 的最小/均值/最大 + 机构数 + 分歧度 —— 三情景的输入",
              "tools/consensus.py", "https://basic.10jqka.com.cn/300502/worth.html"),
        lambda p: _ak(p, "stock_profit_forecast_ths", symbol="300502", indicator="预测年报每股收益"))

    add(Probe("5 一致预期与评级", "巨潮评级变动", "**领先**",
              "评级调高/调低方向。**只覆盖境内券商**,外资行的下调看不到",
              "tools/consensus.py", "http://www.cninfo.com.cn/new/commonUrl?url=data/gg-rating"),
        lambda p: _ak(p, "stock_rank_forecast_cninfo", date="20260828"))

    add(Probe("5 一致预期与评级", "东财研报全文", "同步",
              "研报 PDF,可抽产业量价句子", "tools/research.py --dig",
              "https://data.eastmoney.com/report/"),
        lambda p: http(p, url="https://reportapi.eastmoney.com/report/list",
                       params={"industryCode": "*", "pageSize": 5, "beginTime": "2026-08-01",
                               "endTime": "2026-09-01", "pageNo": 1, "qType": 0},
                       ok=lambda r: f"{len(r.json().get('data', []))} 篇"))

    # ── 6. 领先指标:海外需求 ─────────────────────────────────────────
    add(Probe("6 领先指标·海外需求", "SEC EDGAR XBRL", "**领先**",
              "云厂/油服**已发生**的季度 capex(官方口径)。UA 必须含联系邮箱否则 403",
              "tools/capex.py", "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/PaymentsToAcquirePropertyPlantAndEquipment.json",
              required=True),
        lambda p: http(p, headers={"User-Agent": SEC_UA or UA},
                       ok=lambda r: f"{len(r.json().get('units', {}).get('USD', []))} 期"))

    add(Probe("6 领先指标·海外需求", "Motley Fool 电话会纪要", "**领先**",
              "**下期 capex 指引 + 短周期/长周期拆分**(SEC 只有总额,拆分只在纪要里)。"
              "URL 规律 /earnings/call-transcripts/YYYY/MM/DD/<公司>-<代码>-qN-YYYY-earnings-call-transcript/",
              "requests + 正则取 article-body",
              "https://www.fool.com/earnings/call-transcripts/2026/08/07/microsoft-msft-q4-2026-earnings-call-transcript/"),
        lambda p: http(p, ok=_fool_ok))

    add(Probe("6 领先指标·海外需求", "stockanalysis.com 前瞻一致预期", "**领先**",
              "美股分年度营收/EPS 一致预期(免费无 key)",
              "requests + 表格解析", "https://stockanalysis.com/stocks/msft/forecast/"),
        lambda p: http(p, ok=lambda r: "有预测表" if "Fiscal Year" in r.text else "无表"))

    add(Probe("6 领先指标·海外需求", "stockanalysis.com 历史财务", "滞后",
              "美股 20 个季度三表(比 SEC XBRL 好解析)",
              "requests", "https://stockanalysis.com/stocks/msft/financials/cash-flow-statement/?p=quarterly"),
        lambda p: http(p, ok=lambda r: "有 capex 行" if "Capital Expenditures" in r.text else "无"))

    add(Probe("6 领先指标·海外需求", "roic.ai", "滞后", "美股三表(备源)",
              "requests", "https://roic.ai/financials/MSFT"),
        lambda p: http(p, ok=lambda r: f"{len(r.text):,}B"))

    # ── 7. 领先指标:大宗与油服 ───────────────────────────────────────
    add(Probe("7 领先指标·大宗", "新浪外盘期货 油价", "**领先**", "WTI / 布伦特",
              "tools/commodity.py", "https://hq.sinajs.cn/list=hf_CL,hf_OIL"),
        lambda p: http(p, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"},
                       ok=lambda r: "有报价" if "hq_str" in r.text and len(r.text) > 40 else "空"))

    add(Probe("7 领先指标·大宗", "北美钻机数(Baker Hughes 转载)", "**领先**",
              "作业量的既成事实 —— 油价和 capex 只是意愿,钻机数是已发生",
              "tools/rigcount.py", "https://api.oilpriceapi.com/v1/prices/latest"),
        lambda p: http(p, url="https://oilprice.com/rig-count", ok=lambda r: f"{len(r.text):,}B"))

    add(Probe("7 领先指标·大宗", "人民币中间价(CFETS)", "同步",
              "出口占比高的公司的汇兑影响。**pageSize ≥ 100 返回 403 是分页超限不是被封**",
              "skills/astock-quote/scripts/fx.py",
              "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"),
        lambda p: http(p, url="https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew",
                       params={"startDate": "2026-08-01", "endDate": "2026-09-01",
                               "currency": "USD/CNY", "pageNum": 1, "pageSize": 20},
                       headers={"User-Agent": UA, "Referer": "https://www.chinamoney.com.cn/"},
                       ok=lambda r: f"{len(r.json().get('records', []))} 条"))

    # ── 3b. 事件面(目前管道里完全没有的一块)────────────────────────────
    add(Probe("3b 事件面(缺口)", "上市公司公告", "**领先**",
              "中标 / 重大合同 / 框架协议 / 订单 —— 对订单驱动的公司(杰瑞、深科达)"
              "比财报更直接。**PIT 纪律:只在公告日之后生效**",
              "东财 np-anotice-stock", "https://np-anotice-stock.eastmoney.com/api/security/ann"),
        lambda p: http(p, params={"page_size": 5, "page_index": 1, "ann_type": "A",
                                  "client_source": "web", "stock_list": "300502"},
                       headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
                       ok=lambda r: f"{len(r.json().get('data', {}).get('list', []))} 条"))

    add(Probe("3b 事件面(缺口)", "个股资金流", "同步", "主力/大单/中单/小单净流入",
              "akshare stock_individual_fund_flow",
              "https://data.eastmoney.com/zjlx/300502.html"),
        lambda p: _ak(p, "stock_individual_fund_flow", stock="300502", market="sz"))

    add(Probe("3b 事件面(缺口)", "龙虎榜", "同步", "游资/机构席位买卖",
              "akshare stock_lhb_detail_em", "https://data.eastmoney.com/stock/lhb.html"),
        lambda p: _ak(p, "stock_lhb_detail_em", start_date="20260801", end_date="20260901"))

    add(Probe("6 领先指标·海外需求", "海外同业电话会纪要", "**领先**",
              "光模块链同业(COHR/LITE/FN/CRDO/MRVL/AVGO/ANET/CIEN)的需求与产能口径。"
              "相干 Q4FY26 原话:能见度到 **calendar 2028**,长约到 end of decade",
              "requests + 正则(同 Motley Fool)",
              "https://www.fool.com/earnings/call-transcripts/2026/08/19/coherent-cohr-q4-2026-earnings-call-transcript/"),
        lambda p: http(p, ok=_fool_ok))

    # ── 8. 宏观 ───────────────────────────────────────────────────────
    add(Probe("8 宏观", "中美国债收益率", "领先", "贴现率 —— 高估值成长股的分母",
              "akshare bond_zh_us_rate", "https://data.eastmoney.com/cjsj/zmgzsyl.html"),
        lambda p: _ak(p, "bond_zh_us_rate", start_date="20260801"))

    add(Probe("8 宏观", "全球指数", "同步", "外围市场温度",
              "akshare index_global_spot_em", "https://quote.eastmoney.com/center/gridlist.html#global_asia"),
        lambda p: _ak(p, "index_global_spot_em"))

    return P


# ── baostock 相关(单独 login 一次)──────────────────────────────────
_BS = {"ok": False}


def _bs_login():
    if _BS.get("done"):
        return _BS["ok"]
    _BS["done"] = True
    try:
        import baostock as bs
        lg = bs.login()
        _BS["ok"] = lg.error_code == "0"
        _BS["bs"] = bs
    except Exception:
        _BS["ok"] = False
    return _BS["ok"]


def _bs_rows(rs):
    out = []
    while rs.error_code == "0" and rs.next():
        out.append(rs.get_row_data())
    return out


def _bs_kline(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败(或未安装)"
    else:
        bs = _BS["bs"]
        rs = bs.query_history_k_data_plus(
            "sz.300502", "date,close,peTTM,pbMRQ", start_date="2026-08-01",
            end_date="2026-09-01", frequency="d", adjustflag="2")
        r = _bs_rows(rs)
        p.status, p.detail = ("OK", f"{len(r)} 根,末 peTTM={r[-1][2] if r else '—'}") if r else ("EMPTY", "空")
    p.ms = int((time.time() - t0) * 1000)


def _mk_bs_fin(fn_name):
    def f(p: Probe):
        t0 = time.time()
        if not _bs_login():
            p.status, p.detail = "FAIL", "baostock login 失败"
        else:
            try:
                fn = getattr(_BS["bs"], fn_name)
                r = _bs_rows(fn(code="sz.300502", year=2026, quarter=2))
                p.status, p.detail = ("OK", f"{len(r)} 行 {r[0][:4] if r else ''}") if r else ("EMPTY", "空")
            except Exception as e:
                p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]
        p.ms = int((time.time() - t0) * 1000)
    return f


def _bs_forecast(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_forecast_report(
            "sz.300502", start_date="2025-01-01", end_date="2026-09-01"))
        p.status, p.detail = ("OK", f"{len(r)} 条,最近 {r[-1][1] if r else ''} {r[-1][3] if r else ''}") \
            if r else ("EMPTY", "该股无预告")
    p.ms = int((time.time() - t0) * 1000)


def _bs_express(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_performance_express_report(
            "sz.300308", start_date="2025-01-01", end_date="2026-09-01"))
        p.status, p.detail = ("OK", f"{len(r)} 条") if r else ("EMPTY", "该股无快报")
    p.ms = int((time.time() - t0) * 1000)


def _mk_bs_simple(fn_name):
    def f(p: Probe):
        t0 = time.time()
        if not _bs_login():
            p.status, p.detail = "FAIL", "baostock login 失败"
        else:
            try:
                r = _bs_rows(getattr(_BS["bs"], fn_name)())
                p.status, p.detail = "OK", f"{len(r)} 只"
            except Exception as e:
                p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]
        p.ms = int((time.time() - t0) * 1000)
    return f


def _bs_all_stock(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_all_stock(day="2026-09-01"))
        halt = sum(1 for x in r if x[1] == "0")
        p.status, p.detail = ("OK", f"{len(r)} 只,其中停牌 {halt}") if r else ("EMPTY", "空")
    p.ms = int((time.time() - t0) * 1000)


def _bs_adjust(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_adjust_factor(
            code="sz.300502", start_date="2023-10-01", end_date="2026-09-01"))
        p.status, p.detail = ("OK", f"{len(r)} 次除权除息") if r else ("EMPTY", "期间无除权")
    p.ms = int((time.time() - t0) * 1000)


def _bs_industry(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_stock_industry(code="sz.300502"))
        p.status, p.detail = ("OK", f"{r[0][3] if r else ''}") if r else ("EMPTY", "空")
    p.ms = int((time.time() - t0) * 1000)


def _bs_dates(p: Probe):
    t0 = time.time()
    if not _bs_login():
        p.status, p.detail = "FAIL", "baostock login 失败"
    else:
        r = _bs_rows(_BS["bs"].query_trade_dates(
            start_date="2026-08-01", end_date="2026-09-01"))
        n = sum(1 for x in r if x[1] == "1")
        p.status, p.detail = ("OK", f"{len(r)} 天,其中交易日 {n}") if r else ("EMPTY", "空")
    p.ms = int((time.time() - t0) * 1000)


# ── akshare / efinance 包装 ────────────────────────────────────────────
def _ak(p: Probe, fn_name, _retried=False, **kw):
    t0 = time.time()
    try:
        import akshare as ak
        df = getattr(ak, fn_name)(**kw)
        p.status, p.detail = ("OK", f"{len(df)} 行 × {len(df.columns)} 列") if len(df) else ("EMPTY", "空表")
    except Exception as e:
        if not _retried and any(k in type(e).__name__ for k in ("SSL", "Connection", "Proxy", "Timeout", "JSONDecode")):
            time.sleep(1.5)
            return _ak(p, fn_name, _retried=True, **kw)
        p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]
    p.ms = int((time.time() - t0) * 1000)


def _ef(p: Probe, fn_name, arg="300502"):
    t0 = time.time()
    try:
        import efinance as ef
        df = getattr(ef.stock, fn_name)(arg) if fn_name != "get_all_company_performance" \
            else ef.stock.get_all_company_performance("2026-06-30")
        p.status, p.detail = ("OK", f"{len(df)} 行") if df is not None and len(df) else ("EMPTY", "空")
    except ImportError:
        p.status, p.detail = "MISS", "efinance 未安装"
    except Exception as e:
        p.status, p.detail = "FAIL", f"{type(e).__name__}: {e}"[:110]
    p.ms = int((time.time() - t0) * 1000)


def _em_survey(p: Probe):
    http(p, url="http://datacenter-web.eastmoney.com/api/data/v1/get",
         params={"reportName": "RPT_ORG_SURVEYNEW", "columns": "ALL", "pageNumber": 1,
                 "pageSize": 3, "sortColumns": "NOTICE_DATE", "sortTypes": -1,
                 "filter": '(SECURITY_CODE="300502")', "source": "WEB", "client": "WEB"},
         headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
         ok=lambda r: (lambda d: f"共 {d['result']['count']} 次,最近接待 "
                                 f"{d['result']['data'][0]['NUM']} 家")(r.json()))


def _fool_ok(r):
    import re
    m = re.search(r'class="[^"]*article-body[^"]*"(.*?)(?:</article>|id="disclosure")',
                  r.text, re.S)
    if not m:
        return "无正文(可能改版或被墙)"
    b = re.sub(r"<[^>]+>", " ", m.group(1))
    b = re.sub(r"\s+", " ", b)
    return f"正文 {len(b):,} 字符" + (" · 含 capex" if "apital expenditure" in b or "CapEx" in b else "")


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结果写到这个文件")
    ap.add_argument("--markdown", help="把清单表写成 Markdown(docs/DATA-SOURCES.md 的表就是它生成的)")
    ap.add_argument("--group", help="只跑某一组(子串匹配)")
    a = ap.parse_args()

    probes = sorted(build(), key=lambda t: t[0].group)
    if a.group:
        probes = [(p, f) for p, f in probes if a.group in p.group]

    results: list[Probe] = []
    cur = None
    for p, fn in probes:
        if p.group != cur:
            cur = p.group
            print(f"\n{'─' * 88}\n{cur}\n{'─' * 88}")
        fn(p)
        results.append(p)
        mark = {"OK": "✓", "EMPTY": "○", "MISS": "◇"}.get(p.status, "✗")
        req = " [必需]" if p.required else ""
        print(f"  {mark} {p.name:<40} {p.status:<6} {p.ms:>6}ms  {p.detail[:60]}{req}")

    if _BS.get("ok"):
        _BS["bs"].logout()

    ok = sum(1 for r in results if r.status == "OK")
    bad_req = [r for r in results if r.required and r.status != "OK"]
    print(f"\n{'═' * 88}")
    print(f"合计 {len(results)} 条:通 {ok}、其它 {len(results) - ok}")
    if bad_req:
        print(f"必需源不通 {len(bad_req)} 条:" + "、".join(r.name for r in bad_req))
    else:
        print("必需源全通。")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
        print(f"→ {a.json}")

    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(to_markdown(results))
        print(f"→ {a.markdown}")

    return 1 if bad_req else 0


MARK = {"OK": "✅ 通", "EMPTY": "⚪ 空", "MISS": "◇ 缺包", "PARSE": "⚠ 解析失败"}


def to_markdown(rs: list[Probe]) -> str:
    """生成清单表。**这张表由每次真调生成,不是手写的** —— 手写的清单必然过期,
    2026-09-01 就发现三条记成「拿不到」的其实一直是通的。"""
    out = []
    cur = None
    for r in rs:
        if r.group != cur:
            cur = r.group
            out.append(f"\n### {cur}\n")
            # 5 列不是 6 列:**数据源名字本身就是链接**,不单列一列。
            # 2026-09-01 踩过:6 列在 A4 竖版放不下,「属性」「链接」被压成一字一行。
            out.append("| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |")
            out.append("|---|---|---|---|---|")
        st = MARK.get(r.status, f"❌ {r.status}")
        req = " ⭐" if r.required else ""
        # ⚠ 这里只能出**纯 markdown**,不能夹 <br>/<sub> —— md2html.py 的 _inline
        #   先 escape 再放标签(防内容破坏结构),夹 HTML 会渲染成字面量。
        #   2026-09-01 踩过:41 行全变成可见的 "&lt;br&gt;&lt;sub&gt;"。
        detail = r.detail[:56].replace("|", "/").replace("\n", " ")
        out.append(f"| [**{r.name}**]({r.url}){req} | {r.lead} | {r.what} | `{r.how}` "
                   f"| {st} · {detail} |")
    ok = sum(1 for r in rs if r.status == "OK")
    out.append(f"\n> ⭐ = 必需源(缺了做不了基础分析)。本次实测 {len(rs)} 条,通 {ok} 条。\n")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
