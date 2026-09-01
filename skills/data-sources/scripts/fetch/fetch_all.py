#!/usr/bin/env python3
"""fetch_all —— 把所有数据源抓下来,按类别落盘。

    fetch_all.py --codes 300502,300308,300476,688328,002353
    fetch_all.py --codes 300502 --group financials       # 只抓一类
    fetch_all.py --peers                                  # 只抓海外上下游
    fetch_all.py --list                                   # 列出所有类别与条目

落盘结构(`--out` 默认 `data/raw`):

    data/raw/
    ├── quotes/        <code>/<date>.json    行情与技术位
    ├── financials/    <code>/<date>.json          比率快照(baostock,单期)
    │                  <code>/<date>-利润表.json 等  **全历史三大表 + 单季指标**
    ├── forecast/      <code>/<date>.json    业绩预告 / 快报  ← 领先
    ├── consensus/     <code>/<date>.json    一致预期(同花顺 + Yahoo 两套)
    ├── ratings/       <code>/<date>.json    评级变动(巨潮,**含外资合资券商**)
    ├── surveys/       <code>/<date>.json    机构调研  ← 领先
    ├── announcements/ <code>/<date>.json    公告      ← 领先
    ├── chips/         <code>/<date>.json    股东户数 / 融资 / 解禁 / 龙虎榜
    ├── overseas/      <ticker>/<date>.json  海外上下游:目标价/预估/评级变动
    ├── macro/         <date>.json           油价 / 钻机数 / 汇率 / 中美国债
    └── meta/          <date>.json           本次抓取的健康汇总

每个文件都是**信封格式**,不是裸数据:

    {"source": "baostock query_forecast_report", "url": "...",
     "fetched_at": "2026-09-01T23:40:12", "params": {...},
     "ok": true, "rows": 4, "data": [...]}

这样时效、出处、成败是免费得到的 —— 不用另建 data_freshness 表,
也不会出现「文件在但不知道是哪天哪个源抓的」。

为什么海外也要抓:我们分析的是 A 股,但**需求端在海外**。云厂 capex 决定
光模块,油服 capex 与钻机数决定油气设服,前道设备商决定半导体设备。
海外同业的分析师预估和评级变动是这些 A 股的领先指标,不是背景资料。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path

# 代理:只在本进程去掉。⚠ Clash **TUN 模式**在 L3 路由层截流,这段对 TUN 无效 ——
# 那种情况要在 Clash 规则层对国内财经域名放 DIRECT。见 docs/DATA-SOURCES.md §六。
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

# akshare / efinance 内部很多调用不带 timeout,进程级兜底是唯一可靠的做法,
# 否则会挂死在 CLOSE_WAIT 上(2026-08-31 踩过,preport 永久卡住)。
socket.setdefaulttimeout(45)

TODAY = date.today().isoformat()
RUN_START = datetime.now().isoformat(timespec="seconds")   # 区分本轮与同日更早的残留

# ── 海外上下游:按赛道分组 ────────────────────────────────────────────
# 这不是「参考」,是领先指标。改这张表 = 改我们的领先指标口径。
PEERS = {
    "AI算力链-云厂": ["MSFT", "AMZN", "GOOGL", "META"],      # capex 决定光模块需求
    "AI算力链-同业": ["COHR", "LITE", "FN", "CRDO", "MRVL", "AVGO", "ANET", "CIEN"],
    "半导体设备":     ["AMAT", "LRCX", "KLAC", "ASML"],
    "油气设服":       ["SLB", "HAL", "BKR", "XOM", "CVX"],
}


def env(source, url="", params=None, ok=True, data=None, err=None, rows=None):
    return {"source": source, "url": url, "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "params": params or {}, "ok": ok,
            "rows": rows if rows is not None else (len(data) if isinstance(data, (list, dict)) else None),
            "error": err, "data": data}


def write(out: Path, group: str, key: str, name: str, payload: dict):
    d = out / group / key if key else out / group
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{TODAY}.json" if not name else d / f"{TODAY}-{name}.json"
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old[payload["source"]] = payload
    p.write_text(json.dumps(old, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p


def df_json(df):
    """DataFrame → 可序列化。列名保留中文,别改 —— 改了以后对不上源。"""
    if df is None:
        return None
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


# ══════════════════════════════════════════════════════════════════════
_BS = {}


def bs():
    if "m" not in _BS:
        import baostock as b
        b.login()
        _BS["m"] = b
    return _BS["m"]


def bs_rows(rs):
    """返回 (行, 字段名)。**字段名必须一起存** —— baostock 返回的是裸数组,
    只存数组的话文档里会渲染成 ['sz.300502','2026-08-25','0.356666'] 这种没法核对的东西。"""
    out = []
    while rs.error_code == "0" and rs.next():
        out.append(rs.get_row_data())
    return out, list(getattr(rs, "fields", []) or [])


def bs_code(code: str) -> str:
    return ("sh." if code[0] in "6" else "sz.") + code


def fetch_financials(code, out):
    b = bs()
    c, y, q = bs_code(code), date.today().year, (date.today().month - 1) // 3 or 4
    got = []
    for fn, cn in [("query_profit_data", "盈利"), ("query_growth_data", "成长"),
                   ("query_balance_data", "偿债"), ("query_cash_flow_data", "现金流"),
                   ("query_dupont_data", "杜邦"), ("query_operation_data", "营运")]:
        try:
            rows, flds = bs_rows(getattr(b, fn)(code=c, year=y, quarter=q))
            if not rows:                                   # 当季未披露就回退一季
                rows, flds = bs_rows(getattr(b, fn)(code=c, year=y, quarter=max(1, q - 1)))
            got.append(write(out, "financials", code, "",
                             env(f"baostock {fn}", params={"code": c, "year": y,
                                                          "quarter": q, "fields": flds},
                                 data=rows, ok=bool(rows))))
        except Exception as e:
            write(out, "financials", code, "", env(f"baostock {fn}", ok=False, err=repr(e)))
    return got


# ── 全历史三大表:自己做预测的地基 ────────────────────────────────────
# baostock 那六个接口给的是**比率**(毛利率 / ROE / 周转率)且只有一期。
# 想自己推营收、看存货和预付款的领先关系,必须要绝对值 + 全历史。
# 三条源都是公司自己报表的转录,不含任何人的观点。
def fetch_statements(code, out):
    import akshare as ak
    got, sina = [], "sz" + code if code[0] in "03" else "sh" + code
    for sym in ("利润表", "资产负债表", "现金流量表"):
        try:
            df = retry(lambda: ak.stock_financial_report_sina(stock=sina, symbol=sym))
            got.append(write(out, "financials", code, sym,
                             env("新浪 stock_financial_report_sina",
                                 url=f"https://vip.stock.finance.sina.com.cn/corp/go.php/"
                                     f"vFD_FinanceSummary/stockid/{code}.phtml",
                                 params={"stock": sina, "symbol": sym},
                                 data=df_json(df), ok=df is not None and not df.empty)))
        except Exception as e:
            write(out, "financials", code, sym,
                  env("新浪 stock_financial_report_sina", ok=False, err=repr(e)))
    # 同花顺按单季度:省掉自己做累计相减,可以和上面互相核对
    try:
        df = retry(lambda: ak.stock_financial_abstract_ths(symbol=code, indicator="按单季度"))
        got.append(write(out, "financials", code, "单季指标",
                         env("同花顺 stock_financial_abstract_ths",
                             url=f"https://basic.10jqka.com.cn/{code}/finance.html",
                             params={"symbol": code, "indicator": "按单季度"},
                             data=df_json(df), ok=df is not None and not df.empty)))
    except Exception as e:
        write(out, "financials", code, "单季指标",
              env("同花顺 stock_financial_abstract_ths", ok=False, err=repr(e)))
    return got


def fetch_forecast(code, out):
    b, c = bs(), bs_code(code)
    for fn in ("query_forecast_report", "query_performance_express_report"):
        try:
            rows, flds = bs_rows(getattr(b, fn)(c, start_date="2024-01-01", end_date=TODAY))
            write(out, "forecast", code, "",
                  env(f"baostock {fn}", data=rows, ok=True,
                      params={"code": c, "fields": flds,
                              "note": "0 条也是信息:净利变动未超 ±50% 才无需预告"}))
        except Exception as e:
            write(out, "forecast", code, "", env(f"baostock {fn}", ok=False, err=repr(e)))


def fetch_quotes(code, out):
    b, c = bs(), bs_code(code)
    try:
        rows, flds = bs_rows(b.query_history_k_data_plus(
            c, "date,open,high,low,close,volume,amount,peTTM,pbMRQ,psTTM,turn",
            start_date="2020-01-01", end_date=TODAY, frequency="d", adjustflag="2"))
        write(out, "quotes", code, "qfq",
              env("baostock query_history_k_data_plus", data=rows,
                  params={"adjustflag": "2 前复权", "fields": flds,
                          "note": "含 peTTM/pbMRQ —— 历史分位的唯一来源"}))
    except Exception as e:
        write(out, "quotes", code, "qfq", env("baostock kline", ok=False, err=repr(e)))
    try:                                                   # 复权因子:回测可复现的前提
        rows, flds = bs_rows(b.query_adjust_factor(code=c, start_date="2020-01-01", end_date=TODAY))
        write(out, "quotes", code, "adjfactor",
              env("baostock query_adjust_factor", data=rows,
                  params={"fields": flds,
                          "note": "目标价/成本价与日线必须换算到同一复权基准再比"}))
    except Exception as e:
        write(out, "quotes", code, "adjfactor", env("baostock query_adjust_factor", ok=False, err=repr(e)))


def fetch_consensus(code, out):
    """两套一致预期都抓 —— 它们**不是同一个池子**。
    Yahoo 的池里含外资行(实测新易盛 2026E 营收下沿 544.3 亿正是高盛那份的数),
    同花顺只有境内。两套对不上时,分歧本身就是信号。"""
    import akshare as ak
    for ind in ("预测年报每股收益", "预测年报净利润"):
        try:
            write(out, "consensus", code, "ths",
                  env("akshare stock_profit_forecast_ths", url="https://basic.10jqka.com.cn/",
                      params={"symbol": code, "indicator": ind},
                      data=df_json(ak.stock_profit_forecast_ths(symbol=code, indicator=ind))))
        except Exception as e:
            write(out, "consensus", code, "ths",
                  env(f"akshare stock_profit_forecast_ths[{ind}]", ok=False, err=repr(e)))
    try:
        import yfinance as yf
        t = yf.Ticker(f"{code}.{'SS' if code[0] == '6' else 'SZ'}")
        i = t.info
        write(out, "consensus", code, "yahoo",
              env("yfinance analyst", url="https://finance.yahoo.com/",
                  params={"note": "池中含外资行,与同花顺不是同一套"},
                  data={"target_mean": i.get("targetMeanPrice"), "target_high": i.get("targetHighPrice"),
                        "target_low": i.get("targetLowPrice"), "n_analysts": i.get("numberOfAnalystOpinions"),
                        "recommendation": i.get("recommendationKey"),
                        "earnings_estimate": df_json(t.earnings_estimate.reset_index())
                        if t.earnings_estimate is not None else None,
                        "revenue_estimate": df_json(t.revenue_estimate.reset_index())
                        if t.revenue_estimate is not None else None}))
    except Exception as e:
        write(out, "consensus", code, "yahoo", env("yfinance analyst", ok=False, err=repr(e)))


# 外资在华合资券商 —— 它们的研报**进巨潮评级库**,是纯 A 股唯一的外资口径
FOREIGN_JV = ["高盛", "摩根士丹利", "摩根大通", "瑞银", "汇丰", "野村", "星展", "大和",
              "瑞信", "花旗", "美银", "巴克莱", "法巴", "德意志", "麦格理", "里昂", "杰富瑞"]


def fetch_ratings(code, out, days=90):
    """回看 90 天不是 30 天 —— 2026-09-01 踩过:高盛 07-20 把新易盛目标价
    841→633(−25%),落在 30 天窗口外,我们的报告写成「零下调」。"""
    import akshare as ak
    import datetime as dt
    hits = []
    d0 = date.today() - dt.timedelta(days=days)
    for i in range(days + 1):
        day = d0 + dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            df = ak.stock_rank_forecast_cninfo(date=day.strftime("%Y%m%d"))
        except Exception:
            continue
        if df is None or not len(df):
            continue
        sub = df[df["证券代码"].astype(str).str.zfill(6) == code]
        for r in df_json(sub) or []:
            r["_是否外资系"] = any(f in str(r.get("研究机构简称", "")) for f in FOREIGN_JV)
            hits.append(r)
    write(out, "ratings", code, "cninfo",
          env("akshare stock_rank_forecast_cninfo", url="http://www.cninfo.com.cn/",
              params={"lookback_days": days, "note": "_是否外资系 标出合资券商(野村东方国际/汇丰前海等)"},
              data=hits))


def fetch_surveys(code, out):
    import requests
    p = {"reportName": "RPT_ORG_SURVEYNEW", "columns": "ALL", "pageNumber": 1, "pageSize": 50,
         "sortColumns": "NOTICE_DATE", "sortTypes": -1,
         "filter": f'(SECURITY_CODE="{code}")', "source": "WEB", "client": "WEB"}
    try:
        r = requests.get("http://datacenter-web.eastmoney.com/api/data/v1/get", params=p,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://data.eastmoney.com/"}, timeout=30)
        d = r.json().get("result") or {}
        write(out, "surveys", code, "",
              env("东财 RPT_ORG_SURVEYNEW", url="https://data.eastmoney.com/jgdy/", params=p,
                  data=d.get("data"), rows=d.get("count")))
    except Exception as e:
        write(out, "surveys", code, "", env("东财 RPT_ORG_SURVEYNEW", ok=False, err=repr(e)))


def fetch_announcements(code, out):
    import requests
    p = {"page_size": 50, "page_index": 1, "ann_type": "A", "client_source": "web",
         "stock_list": code}
    try:
        r = requests.get("https://np-anotice-stock.eastmoney.com/api/security/ann", params=p,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://data.eastmoney.com/"}, timeout=30)
        write(out, "announcements", code, "",
              env("东财 np-anotice-stock", params=p,
                  data=(r.json().get("data") or {}).get("list")))
    except Exception as e:
        write(out, "announcements", code, "", env("东财 np-anotice-stock", ok=False, err=repr(e)))


def _em(out, group, code, name, report, flt=None, size=60, note="", extra=None):
    """东财 datacenter-web 的统一取法。**这台主机本机稳定**,而 push2/push2his 时通时不通 ——
    能走 datacenter-web 的就别走 push2。同一个 eastmoney.com 下两种行为。"""
    import requests
    p = {"reportName": report, "columns": "ALL", "pageNumber": 1, "pageSize": size,
         "source": "WEB", "client": "WEB"}
    if flt:
        p["filter"] = flt
    if extra:
        p.update(extra)
    try:
        r = requests.get("http://datacenter-web.eastmoney.com/api/data/v1/get", params=p,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://data.eastmoney.com/"}, timeout=30)
        d = (r.json() or {}).get("result") or {}
        write(out, group, code, name,
              env(f"东财 {report}", url="https://data.eastmoney.com/", params={**p, "note": note},
                  data=d.get("data"), rows=d.get("count"), ok=bool(d.get("data"))))
    except Exception as e:
        write(out, group, code, name, env(f"东财 {report}", ok=False, err=repr(e)))


def retry(fn, n=2, wait=1.5):
    """网络类抖动重试。SSL/连接/代理这些在本机时通时不通,一次失败就记 FAIL
    会让清单看起来比实际差(probe_all_sources 里已经有同样的处理)。"""
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e
            if not any(k in type(e).__name__ for k in ("SSL", "Connection", "Proxy", "Timeout")):
                raise
            if i < n - 1:
                time.sleep(wait)
    raise last


def fetch_chips(code, out):
    """筹码与杠杆。**优先走 datacenter-web** —— 2026-09-01 实测:
    efinance 的 get_latest_holder_number 参数是**日期不是代码**(传代码报
    「time data '300502' does not match format」),而 akshare 的个股资金流走
    push2his,这台主机本机长期不稳。两条都换成 datacenter-web 的 reportName。"""
    fl = f'(SECURITY_CODE="{code}")'
    _em(out, "chips", code, "股东户数", "RPT_HOLDERNUMLATEST", fl, 8,
        "含 HOLDER_NUM/PRE_HOLDER_NUM/HOLDER_NUM_RATIO/END_DATE —— **END_DATE 是数据截止日,"
        "和实时价不是同一时点,报告里必须标出来**")
    _em(out, "chips", code, "资金流", "RPT_DMSK_TS_STOCKNEW", fl, 60,
        "SUPERDEAL/PRIME 各档净流入。替代 akshare stock_individual_fund_flow(走 push2his 不稳)")
    _em(out, "chips", code, "大宗交易", "RPT_BLOCKTRADE_STA", fl, 60,
        "折溢价与成交额 —— 大额换手的价格与对手方")

    import akshare as ak
    try:                                   # 解禁:领先,未来的供给冲击
        df = ak.stock_restricted_release_detail_em(
            start_date=TODAY.replace("-", ""), end_date=f"{date.today().year + 1}1231")
        col = next((c for c in df.columns if "代码" in c), None)
        if col is not None:
            df = df[df[col].astype(str).str.zfill(6) == code]
        write(out, "chips", code, "解禁",
              env("akshare stock_restricted_release_detail_em", data=df_json(df)))
    except Exception as e:
        write(out, "chips", code, "解禁",
              env("akshare stock_restricted_release_detail_em", ok=False, err=repr(e)))
    try:                                   # 融资融券
        fn = "stock_margin_detail_sse" if code[0] == "6" else "stock_margin_detail_szse"
        df = retry(lambda: getattr(ak, fn)(date=(date.today()).strftime("%Y%m%d")))
        col = next((c for c in df.columns if "代码" in c or "证券代码" in c), None)
        if col is not None:
            df = df[df[col].astype(str).str.zfill(6) == code]
        write(out, "chips", code, "融资融券", env(f"akshare {fn}", data=df_json(df)))
    except Exception as e:
        write(out, "chips", code, "融资融券", env("akshare margin_detail", ok=False, err=repr(e)))
    try:                                   # 前十大流通股东
        import efinance as ef
        write(out, "chips", code, "前十大流通股东",
              env("efinance get_top10_stock_holder_info",
                  data=df_json(retry(lambda: ef.stock.get_top10_stock_holder_info(code, top=4)))))
    except Exception as e:
        write(out, "chips", code, "前十大流通股东",
              env("efinance get_top10_stock_holder_info", ok=False, err=repr(e)))


def fetch_overseas_facts(out, only=None):
    """海外上下游的**事实**:季度利润表 + 季度现金流量表(10-Q 转录)。

    和 fetch_overseas 分开的理由:那个抓的是 yfinance analyst,
    也就是**分析师预测** —— 只能当作"市场怎么想"的记录,不能进我们的模型。
    这个抓的是公司报表里的实际数字,尤其是**资本开支**:
    云厂季度 capex 是光模块需求的直接来源,而且是已经花掉的钱,不是谁的观点。

    实测 2026Q2:MSFT 358 亿、GOOGL 449 亿、META 301 亿美元,
    三家合计同比 +98%;同期新易盛营收同比 +96.9%。这条对应关系
    就是从这里算出来的,不需要引用任何研报。
    """
    import yfinance as yf
    WANT_CF = ["Capital Expenditure", "Free Cash Flow", "Operating Cash Flow"]
    WANT_IS = ["Total Revenue", "Gross Profit", "Operating Income", "Net Income"]
    for grp, tickers in PEERS.items():
        if only and only != grp:
            continue
        for tk in tickers:
            try:
                t = yf.Ticker(tk)
                cf, ic = t.quarterly_cashflow, t.quarterly_income_stmt
                def pick(df, want):
                    if df is None or df.empty:
                        return None
                    return {str(r): {str(c)[:10]: (None if v != v else float(v))
                                     for c, v in zip(df.columns, df.loc[r].values)}
                            for r in want if r in df.index}
                data = {"季度现金流": pick(cf, WANT_CF), "季度利润表": pick(ic, WANT_IS)}
                n = len((data["季度现金流"] or {}).get("Capital Expenditure", {}))
                write(out, "overseas", tk, "季度财报",
                      env("yfinance quarterly_cashflow + quarterly_income_stmt(10-Q 转录)",
                          url=f"https://finance.yahoo.com/quote/{tk}/cash-flow",
                          params={"sector_group": grp}, data=data,
                          ok=bool(data["季度现金流"] or data["季度利润表"]), rows=n))
                print(f"    · {grp:<14} {tk:<6} 季度资本开支 {n} 期")
            except Exception as e:
                write(out, "overseas", tk, "季度财报",
                      env("yfinance quarterly 财报", ok=False, err=repr(e)))
                print(f"    · {grp:<14} {tk:<6} 失败 {type(e).__name__}")
            time.sleep(0.4)


def fetch_overseas(out, only=None):
    """海外上下游的**市场预期**:目标价、评级变动、一致预期。

    ⚠ 这一整组是**别人的预测**,只作为"市场怎么想"的记录,
    **不进我们自己的模型**。事实那一半在 fetch_overseas_facts。
    """
    import yfinance as yf
    for grp, tickers in PEERS.items():
        if only and only != grp:
            continue
        for tk in tickers:
            try:
                t = yf.Ticker(tk)
                i = t.info
                ud = t.upgrades_downgrades
                write(out, "overseas", tk, "",
                      env("yfinance analyst", url=f"https://finance.yahoo.com/quote/{tk}",
                          params={"sector_group": grp},
                          data={"price": i.get("currentPrice"),
                                "target_mean": i.get("targetMeanPrice"),
                                "target_high": i.get("targetHighPrice"),
                                "target_low": i.get("targetLowPrice"),
                                "n_analysts": i.get("numberOfAnalystOpinions"),
                                "recommendation": i.get("recommendationKey"),
                                "earnings_estimate": df_json(t.earnings_estimate.reset_index())
                                if t.earnings_estimate is not None else None,
                                "revenue_estimate": df_json(t.revenue_estimate.reset_index())
                                if t.revenue_estimate is not None else None,
                                "upgrades_downgrades": df_json(ud.reset_index().head(40))
                                if ud is not None and len(ud) else None}))
                print(f"    · {grp:<14} {tk:<6} 目标均={i.get('targetMeanPrice')} "
                      f"评级变动={0 if ud is None else len(ud)} 条")
            except Exception as e:
                write(out, "overseas", tk, "", env("yfinance analyst", ok=False, err=repr(e)))
                print(f"    · {grp:<14} {tk:<6} 失败 {type(e).__name__}")
            time.sleep(0.4)                                # Yahoo 会 429


def fetch_macro(out):
    import akshare as ak
    import requests
    ua = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
    try:
        r = requests.get("https://hq.sinajs.cn/list=hf_CL,hf_OIL", headers=ua, timeout=25)
        write(out, "macro", "", "油价", env("新浪外盘期货", url="https://hq.sinajs.cn/list=hf_CL,hf_OIL",
                                           data=r.text.strip()))
    except Exception as e:
        write(out, "macro", "", "油价", env("新浪外盘期货", ok=False, err=repr(e)))
    for fn, cn, kw in [("bond_zh_us_rate", "中美国债", {"start_date": "20260101"}),
                       ("index_global_spot_em", "全球指数", {})]:
        try:
            write(out, "macro", "", cn, env(f"akshare {fn}", params=kw, data=df_json(getattr(ak, fn)(**kw))))
        except Exception as e:
            write(out, "macro", "", cn, env(f"akshare {fn}", ok=False, err=repr(e)))
    try:
        r = requests.get("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew",
                         params={"startDate": "2026-01-01", "endDate": TODAY,
                                 "currency": "USD/CNY", "pageNum": 1, "pageSize": 90},
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "https://www.chinamoney.com.cn/"}, timeout=25)
        write(out, "macro", "", "汇率中间价",
              env("CFETS CcprHisNew", params={"note": "pageSize ≥ 100 的 403 是分页超限不是被封"},
                  data=r.json().get("records")))
    except Exception as e:
        write(out, "macro", "", "汇率中间价", env("CFETS CcprHisNew", ok=False, err=repr(e)))


def fetch_research(code, out, months=6, max_pdf=12):
    """券商研报:**列表 + PDF 正文都落盘**。
    研报是数据源不是参考资料 —— 它带着产业量价、订单、产能这些别处没有的句子,
    而且卖方的估值锚(用什么倍数、基于哪一年)是我们校准自己口径的对照物。
    PDF 存原件不只存抽出的文字:抽错了还能回去核,只存文字就核不回去了。"""
    import requests, re as _re, datetime as _dt
    d = out / "research" / code
    (d / "pdf").mkdir(parents=True, exist_ok=True)
    beg = (date.today() - _dt.timedelta(days=30 * months)).isoformat()
    ua = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/report/"}
    try:
        r = requests.get("https://reportapi.eastmoney.com/report/list", timeout=30, headers=ua,
                         params={"industryCode": "*", "pageSize": 60, "beginTime": beg,
                                 "endTime": TODAY, "pageNo": 1, "qType": 0, "code": code})
        items = r.json().get("data") or []
    except Exception as e:
        write(out, "research", code, "list", env("东财 reportapi", ok=False, err=repr(e)))
        return
    write(out, "research", code, "list",
          env("东财 reportapi/report/list", url="https://data.eastmoney.com/report/",
              params={"code": code, "months": months}, data=items))

    got = []
    for it in items[:max_pdf]:
        inf = it.get("infoCode")
        if not inf:
            continue
        fp = d / "pdf" / f"{it.get('publishDate','')[:10]}-{inf}.pdf"
        if fp.exists():
            got.append({"infoCode": inf, "path": str(fp), "cached": True})
            continue
        try:
            pr = requests.get(f"https://pdf.dfcfw.com/pdf/H3_{inf}_1.pdf", timeout=40, headers=ua)
            if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                fp.write_bytes(pr.content)
                got.append({"infoCode": inf, "title": it.get("title"),
                            "org": it.get("orgSName"), "date": it.get("publishDate", "")[:10],
                            "path": str(fp), "bytes": len(pr.content)})
        except Exception:
            pass
        time.sleep(0.3)
    write(out, "research", code, "pdf-index",
          env("pdf.dfcfw.com", url="https://pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf",
              params={"note": "原件存 research/<code>/pdf/,抽错了能回去核"}, data=got))


# 海外电话会纪要:URL 规律固定,站内搜索是 JS 的,所以按「代码 + 季度」构造后直取。
# 拿不到就如实记 ok=false —— 不猜、不用旧的顶替。
def fetch_transcripts(out, urls: list[str]):
    """下游/同业的电话会纪要正文。**下期 capex 指引和短周期/长周期拆分只在这里**,
    SEC XBRL 只有已发生的总额。"""
    import requests, re as _re
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
    for u in urls:
        m = _re.search(r"/([a-z0-9-]+)-earnings", u)
        key = (m.group(1) if m else u.rstrip("/").rsplit("/", 1)[-1])[:60]
        try:
            r = requests.get(u, headers=ua, timeout=30)
            mm = _re.search(r'class="[^"]*article-body[^"]*"(.*?)(?:</article>|id="disclosure")',
                            r.text, _re.S)
            body = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", mm.group(1))) if mm else ""
            d = out / "transcripts" / key
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{TODAY}.txt").write_text(body, encoding="utf-8")
            write(out, "transcripts", key, "",
                  env("Motley Fool transcript", url=u,
                      params={"note": "capex 指引 / 短周期占比 / 公司自己的下期指引"},
                      ok=bool(body), rows=len(body),
                      data={"chars": len(body), "has_capex": "apital expenditure" in body,
                            "path": str(d / f"{TODAY}.txt")}))
            print(f"    · {key:<44} {len(body):>7,} 字符"
                  + ("  含 capex" if "apital expenditure" in body else ""))
        except Exception as e:
            write(out, "transcripts", key, "", env("Motley Fool transcript", url=u,
                                                   ok=False, err=repr(e)))
            print(f"    · {key:<44} 失败 {type(e).__name__}")


GROUPS = {"quotes": fetch_quotes, "research": fetch_research, "financials": fetch_financials,
          "statements": fetch_statements, "forecast": fetch_forecast,
          "consensus": fetch_consensus, "ratings": fetch_ratings, "surveys": fetch_surveys,
          "announcements": fetch_announcements, "chips": fetch_chips}


def health(out: Path):
    """健康汇总:扫本次落盘的信封,统计每类的成败。
    ok=false 的条目**留在盘上**不删 —— 「抓失败了」本身要能被看见,
    静默丢弃会让下游以为这类数据根本不存在。"""
    rows = []
    for p in sorted(out.rglob(f"{TODAY}*.json")):
        if p.parent.name == "meta":
            continue
        try:
            for src, e in json.loads(p.read_text(encoding="utf-8")).items():
                rows.append({"group": p.relative_to(out).parts[0],
                             "key": p.parent.name, "source": src,
                             "ok": e.get("ok"), "rows": e.get("rows"),
                             "fetched_at": e.get("fetched_at"),
                             "stale": (e.get("fetched_at") or "") < RUN_START,
                             "error": (e.get("error") or "")[:90]})
        except Exception:
            pass
    (out / "meta").mkdir(parents=True, exist_ok=True)
    (out / "meta" / f"{TODAY}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    cur = [r for r in rows if not r["stale"]]
    ok = sum(1 for r in cur if r["ok"])
    stale_bad = [r for r in rows if r["stale"] and not r["ok"]]
    print(f"\n健康汇总 → {out/'meta'/(TODAY+'.json')}")
    print(f"  本轮 {len(cur)} 条:成功 {ok}、失败 {len(cur)-ok}")
    for r in cur:
        if not r["ok"]:
            print(f"    ✗ {r['group']}/{r['key']} · {r['source']} — {r['error']}")
    if stale_bad:
        # 同日更早那轮留下的失败条目。**不删** —— 失败要能被看见;
        # 但也不能算进本轮,否则改好了代码,健康看起来还是坏的。
        print(f"  另有 {len(stale_bad)} 条是**同日更早那轮**的失败残留(可能已被新代码取代):")
        for r in stale_bad:
            print(f"    · {r['group']}/{r['key']} · {r['source']}")
    return cur


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes", default="")
    ap.add_argument("--group", help="只抓一类:" + " / ".join(GROUPS))
    ap.add_argument("--peers", action="store_true", help="抓海外上下游")
    ap.add_argument("--macro", action="store_true", help="抓宏观")
    ap.add_argument("--transcripts", nargs="*", metavar="URL",
                    help="抓海外电话会纪要(给 Motley Fool 的 URL)")
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        print("A 股类别:", " ".join(GROUPS))
        print("海外上下游分组:")
        for g, t in PEERS.items():
            print(f"  {g:<16} {' '.join(t)}")
        return 0

    out = Path(a.out)
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    if not codes and not a.peers and not a.macro and a.transcripts is None:
        ap.error("至少给 --codes / --peers / --macro / --transcripts 之一")

    for code in codes:
        print(f"\n[{code}]")
        for name, fn in GROUPS.items():
            if a.group and a.group != name:
                continue
            t0 = time.time()
            try:
                fn(code, out)
                print(f"  ✓ {name:<14} {int((time.time()-t0)*1000):>6}ms")
            except Exception:
                print(f"  ✗ {name:<14} {traceback.format_exc().strip().splitlines()[-1][:80]}")

    if a.peers:
        print("\n[海外上下游]")
        fetch_overseas_facts(out)
        fetch_overseas(out)
    if a.macro:
        print("\n[宏观]")
        fetch_macro(out)
    if a.transcripts:
        print("\n[海外电话会纪要]")
        fetch_transcripts(out, a.transcripts)

    if _BS.get("m"):
        _BS["m"].logout()
    rows = health(out)
    return 1 if any(not r["ok"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
