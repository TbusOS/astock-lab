#!/usr/bin/env python3
"""data_digest —— 把抓下来的**数据本身**渲染成给人核对的文档(MD + PDF)。

    data_digest.py --code 300502 [--raw data/raw] [--out private/data-digest] [--pdf]
    data_digest.py --overseas                       # 海外上下游 + 宏观

`data/raw/` 里的 JSON 是给程序读的。这份是给**人**读的,目的只有一个:
**让人能核对数字对不对、缺没缺**。所以两条硬要求:

1. **渲染真实数值,不是元数据。** 上一版只写了「源 / 抓取时间 / 行数」,
   读的人什么也核不了。要的是预告原文、一致预期表、财务数字、调研记录、公告清单。
2. **每一节都给出获取链接和取数命令。** 数字有疑问就点链接看官方页面 ——
   没有这两样,文档只能被相信,不能被检验。

baostock 返回的是**没有字段名的裸数组**,所以下面有字段映射表(中文名 + 单位)。
新抓的数据字段名存在信封的 `params.fields` 里,老数据回退到 FALLBACK。
"""
from __future__ import annotations

import argparse
import bisect
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# ── baostock 字段映射:英文名 → (中文名, 单位/说明)──────────────────────
# 顺序以 rs.fields 为准,含义按 baostock 官方文档。**别改中文名去「优化」**,
# 改了就和源对不上,核对时反而更费劲。
F = {
    "code": ("代码", ""), "pubDate": ("披露日", ""), "statDate": ("报告期", ""),
    "roeAvg": ("净资产收益率(平均)", "小数,×100 为 %"), "npMargin": ("销售净利率", "小数"),
    "gpMargin": ("销售毛利率", "小数"), "netProfit": ("净利润", "元"),
    "epsTTM": ("每股收益 TTM", "元"), "MBRevenue": ("主营收入", "元"),
    "totalShare": ("总股本", "股"), "liqaShare": ("流通股本", "股"),
    "YOYEquity": ("净资产同比", "小数"), "YOYAsset": ("总资产同比", "小数"),
    "YOYNI": ("净利润同比", "小数"), "YOYEPSBasic": ("基本每股收益同比", "小数"),
    "YOYPNI": ("归母净利润同比", "小数"),
    "currentRatio": ("流动比率", "倍"), "quickRatio": ("速动比率", "倍"),
    "cashRatio": ("现金比率", "倍"), "YOYLiability": ("总负债同比", "小数"),
    "liabilityToAsset": ("资产负债率", "小数"), "assetToEquity": ("权益乘数", "倍"),
    "CAToAsset": ("流动资产/总资产", "小数"), "NCAToAsset": ("非流动资产/总资产", "小数"),
    "tangibleAssetToAsset": ("有形资产/总资产", "小数"),
    "ebitToInterest": ("已获利息倍数", "倍"), "CFOToOR": ("经营现金流/营业收入", "小数"),
    "CFOToNP": ("**经营现金流/净利润**", "小数 —— 利润有没有变成钱"),
    "CFOToGr": ("经营现金流/营业总收入", "小数"),
    "dupontROE": ("ROE", "小数"), "dupontAssetStoEquity": ("权益乘数", "倍"),
    "dupontAssetTurn": ("总资产周转率", "次"), "dupontPnitoni": ("归母净利/净利润", "小数"),
    "dupontNitogr": ("净利率(净利/营收)", "小数"), "dupontTaxBurden": ("税负(净利/利润总额)", "小数"),
    "dupontIntburden": ("息负(利润总额/EBIT)", "小数"), "dupontEbittogr": ("EBIT/营收", "小数"),
    "NRTurnRatio": ("应收账款周转率", "次"), "NRTurnDays": ("应收账款周转天数", "天"),
    "INVTurnRatio": ("存货周转率", "次"), "INVTurnDays": ("存货周转天数", "天"),
    "CATurnRatio": ("流动资产周转率", "次"), "AssetTurnRatio": ("总资产周转率", "次"),
    "profitForcastExpPubDate": ("预告公布日", ""), "profitForcastExpStatDate": ("预告报告期", ""),
    "profitForcastType": ("预告类型", ""), "profitForcastAbstract": ("预告摘要", ""),
    "profitForcastChgPctUp": ("增幅上限", "%"), "profitForcastChgPctDwn": ("增幅下限", "%"),
    "dividOperateDate": ("除权除息日", ""), "foreAdjustFactor": ("前复权因子", ""),
    "backAdjustFactor": ("后复权因子", ""), "adjustFactor": ("复权因子", ""),
    "date": ("日期", ""), "open": ("开", "元"), "high": ("高", "元"), "low": ("低", "元"),
    "close": ("收", "元"), "volume": ("成交量", "股"), "amount": ("成交额", "元"),
    "peTTM": ("**PE(TTM)**", "倍"), "pbMRQ": ("**PB(MRQ)**", "倍"),
    "psTTM": ("PS(TTM)", "倍"), "turn": ("换手率", "%"),
}
FALLBACK = {
    "query_profit_data": ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin",
                          "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
    "query_growth_data": ["code", "pubDate", "statDate", "YOYEquity", "YOYAsset", "YOYNI",
                          "YOYEPSBasic", "YOYPNI"],
    "query_balance_data": ["code", "pubDate", "statDate", "currentRatio", "quickRatio",
                           "cashRatio", "YOYLiability", "liabilityToAsset", "assetToEquity"],
    "query_cash_flow_data": ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset",
                             "tangibleAssetToAsset", "ebitToInterest", "CFOToOR",
                             "CFOToNP", "CFOToGr"],
    "query_dupont_data": ["code", "pubDate", "statDate", "dupontROE", "dupontAssetStoEquity",
                          "dupontAssetTurn", "dupontPnitoni", "dupontNitogr",
                          "dupontTaxBurden", "dupontIntburden", "dupontEbittogr"],
    "query_operation_data": ["code", "pubDate", "statDate", "NRTurnRatio", "NRTurnDays",
                             "INVTurnRatio", "INVTurnDays", "CATurnRatio", "AssetTurnRatio"],
    "query_forecast_report": ["code", "profitForcastExpPubDate", "profitForcastExpStatDate",
                              "profitForcastType", "profitForcastAbstract",
                              "profitForcastChgPctUp", "profitForcastChgPctDwn"],
    "query_adjust_factor": ["code", "dividOperateDate", "foreAdjustFactor",
                            "backAdjustFactor", "adjustFactor"],
    "query_history_k_data_plus": ["date", "open", "high", "low", "close", "volume",
                                  "amount", "peTTM", "pbMRQ", "psTTM", "turn"],
}

# 每类的「怎么拿 + 去哪核对 + 怎么重跑」。**这三列是文档能被检验的全部依据。**
HOW = {
    "forecast": ("baostock `query_forecast_report` / `query_performance_express_report`",
                 "http://baostock.com/baostock/index.php/季频盈利能力",
                 "tools/fetch_all.py --codes {code} --group forecast"),
    "surveys": ("东财 datacenter-web `RPT_ORG_SURVEYNEW`",
                "https://data.eastmoney.com/jgdy/",
                "tools/fetch_all.py --codes {code} --group surveys"),
    "announcements": ("东财 `np-anotice-stock`",
                      "https://data.eastmoney.com/notices/stock/{code}.html",
                      "tools/fetch_all.py --codes {code} --group announcements"),
    "consensus": ("同花顺 `stock_profit_forecast_ths` + Yahoo `yfinance`",
                  "https://basic.10jqka.com.cn/{code}/worth.html",
                  "tools/fetch_all.py --codes {code} --group consensus"),
    "ratings": ("巨潮 `stock_rank_forecast_cninfo`(回看 90 天)",
                "http://www.cninfo.com.cn/new/commonUrl?url=data/gg-rating",
                "tools/fetch_all.py --codes {code} --group ratings"),
    "research": ("东财 `reportapi/report/list` + `pdf.dfcfw.com` 下原件",
                 "https://data.eastmoney.com/report/{code}.html",
                 "tools/fetch_all.py --codes {code} --group research"),
    "quotes": ("baostock `query_history_k_data_plus`(前复权)+ `query_adjust_factor`",
               "http://baostock.com/baostock/index.php/A股K线数据",
               "tools/fetch_all.py --codes {code} --group quotes"),
    "chips": ("东财 `RPT_HOLDERNUMLATEST` / `RPT_DMSK_TS_STOCKNEW` / `RPT_BLOCKTRADE_STA` + akshare",
              "https://data.eastmoney.com/gdhs/detail/{code}.html",
              "tools/fetch_all.py --codes {code} --group chips"),
    "financials": ("baostock 六张季频表(盈利/成长/偿债/现金流/杜邦/营运)",
                   "http://baostock.com/baostock/index.php/季频财务数据",
                   "tools/fetch_all.py --codes {code} --group financials"),
}
LEAD = {"forecast": "领先", "surveys": "领先", "announcements": "领先", "consensus": "领先",
        "ratings": "领先", "research": "领先", "quotes": "同步", "chips": "同步",
        "financials": "滞后"}
CN = {"forecast": "业绩预告 / 快报", "surveys": "机构调研", "announcements": "公告",
      "consensus": "券商一致预期", "ratings": "评级变动", "research": "券商研报",
      "quotes": "行情与估值历史", "chips": "筹码与杠杆", "financials": "财务六表"}
MARK = {"领先": "🟢 领先", "同步": "🟡 同步", "滞后": "⚪ 滞后"}


# ── 读盘 ──────────────────────────────────────────────────────────────
def load(raw: Path, group: str, key: str) -> list[dict]:
    d = raw / group / key if key else raw / group
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"))
    if not files:
        return []
    latest = max(f.name[:10] for f in files)
    out = []
    for f in files:
        if not f.name.startswith(latest):
            continue
        try:
            for src, e in json.loads(f.read_text(encoding="utf-8")).items():
                e = dict(e)
                e["_src"] = src
                out.append(e)
        except Exception as ex:
            out.append({"_src": f.name, "ok": False, "error": repr(ex)})
    return out


def mark_runs(envs: list[dict], gap_min: int = 10) -> None:
    """按 fetched_at 的时间空档切「轮次」,只有最新一轮不算旧记录。
    不能拿「最大 fetched_at」当基准 —— 同一轮内各源本来就差几分钟。"""
    ts = []
    for e in envs:
        try:
            ts.append(datetime.fromisoformat(e.get("fetched_at")))
        except Exception:
            ts.append(None)
    known = sorted(t for t in ts if t)
    if not known:
        for e in envs:
            e["_stale"] = False
        return
    cut = known[-1]
    for a, b in zip(reversed(known[:-1]), reversed(known[1:])):
        if (b - a).total_seconds() > gap_min * 60:
            cut = b
            break
        cut = a
    for e, t in zip(envs, ts):
        e["_stale"] = bool(t and t < cut)


def fields_of(e: dict) -> list[str]:
    f = (e.get("params") or {}).get("fields")
    if f:
        return f
    for k, v in FALLBACK.items():
        if k in str(e.get("_src", "")):
            return v
    return []


def esc(v) -> str:
    return str(v).replace("|", "/").replace("\n", " ") if v is not None else "—"


def num(v, dg=4):
    try:
        f = float(v)
        if abs(f) >= 1e6:
            return f"{f:,.0f}"
        return f"{f:,.{dg}f}".rstrip("0").rstrip(".")
    except Exception:
        return esc(v)


def kv_table(fields, row) -> list[str]:
    """一条记录竖着排:字段中文名 | 值 | 单位。横排列太多,A4 上会挤成一团。"""
    L = ["| 字段 | 值 | 单位 / 说明 |", "|---|---|---|"]
    for name, val in zip(fields, row):
        cn, unit = F.get(name, (name, ""))
        L.append(f"| {cn} `{name}` | {num(val)} | {unit} |")
    return L


def rows_table(fields, rows, cols, limit=12) -> list[str]:
    idx = [fields.index(c) for c in cols if c in fields]
    head = [F.get(fields[i], (fields[i], ""))[0] for i in idx]
    L = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows[:limit]:
        L.append("| " + " | ".join(num(r[i]) if i < len(r) else "—" for i in idx) + " |")
    return L


def head_block(group, code, envs) -> list[str]:
    how, url, cmd = HOW[group]
    L = [f"### {CN[group]}　{MARK[LEAD[group]]}", "",
         f"- **怎么拿**:{how}",
         f"- **去哪核对**:{url.format(code=code)}"]
    for u in sorted({e.get("url") for e in envs if e.get("url")}):
        if u and u != url.format(code=code):
            L.append(f"- **接口**:{u}")
    L += [f"- **重跑**:`$VENV/bin/python {cmd.format(code=code)}`",
          "- **抓于**:" + "、".join(sorted({str(e.get('fetched_at'))[:16] for e in envs})), ""]
    return L


# ── 各类正文 ──────────────────────────────────────────────────────────
def sec_forecast(code, envs):
    L = head_block("forecast", code, envs)
    for e in envs:
        d, f = e.get("data") or [], fields_of(e)
        if "forecast_report" in e["_src"]:
            L += [f"**业绩预告** —— 共 {len(d)} 条。"
                  "**0 条也是信息**:说明净利变动没超 ±50%,按规则无需预告。", ""]
            if d:
                L += ["| 公布日 | 报告期 | 类型 | 增幅下限 % | 增幅上限 % | 摘要 |",
                      "|---|---|---|---|---|---|"]
                for r in d[-6:]:
                    L.append(f"| {esc(r[1])[:10]} | {esc(r[2])[:10]} | **{esc(r[3])}** "
                             f"| {num(r[6], 2)} | {num(r[5], 2)} | {esc(r[4])[:110]} |")
            L.append("")
        else:
            L += [f"**业绩快报** —— 共 {len(d)} 条。", ""]
            if d and f:
                L += kv_table(f, d[-1]) + [""]
    return L


def sec_surveys(code, envs):
    L = head_block("surveys", code, envs)
    for e in envs:
        d = e.get("data") or []
        L += [f"历史累计 **{e.get('rows')}** 条记录(每场每家一行,`接待家数` 是该场总数),"
              "下面是最近几条。", "",
              "| 调研日 | 公告日 | 接待家数 | 方式 | 时间说明 | 接待人 | 来访机构 |",
              "|---|---|---|---|---|---|---|"]
        for r in d[:8]:
            L.append(f"| {str(r.get('RECEIVE_START_DATE') or '')[:10]} "
                     f"| {str(r.get('NOTICE_DATE') or '')[:10]} | **{r.get('NUM')}** "
                     f"| {esc(r.get('RECEIVE_WAY_EXPLAIN'))} "
                     f"| {esc(r.get('RECEIVE_TIME_EXPLAIN'))[:42]} "
                     f"| {esc(r.get('RECEPTIONIST'))[:32]} "
                     f"| {esc(r.get('RECEIVE_OBJECT'))[:16]} |")
        L += ["", "> **怎么读**:看的不是内容(纪要正文在巨潮的公告 PDF 里,还没接),"
              "而是**家数骤变**和**异常时点** —— 周末或夜间开会、董秘加财务总监一起上,"
              "通常意味着有需要向机构解释的事。", ""]
    return L


def sec_announcements(code, envs):
    L = head_block("announcements", code, envs)
    for e in envs:
        d = e.get("data") or []
        L += [f"最近 **{len(d)}** 条,下列 15 条,**每条可点开原文 PDF**。", "",
              "| 日期 | 标题 | 原文 |", "|---|---|---|"]
        for r in d[:15]:
            art = r.get("art_code") or ""
            L.append(f"| {str(r.get('notice_date') or '')[:10]} "
                     f"| {esc(r.get('title'))[:60]} "
                     f"| [PDF](https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf) |")
        L += ["", "> **缺口**:现在只取到标题。**中标 / 重大合同 / 框架协议 / 订单** "
              "这类关键词的正文识别还没做 —— 对订单驱动的公司,这比财报更领先。", ""]
    return L


def sec_consensus(code, envs):
    L = head_block("consensus", code, envs)
    L += ["> **两套源都抓,因为不是同一个池子。** 同花顺只覆盖境内券商;"
          "Yahoo 的池里含外资行(实测新易盛 2026E 营收下沿正是高盛研报里的数)。"
          "**两套对不上时,分歧本身就是信号。**", ""]
    for e in envs:
        d = e.get("data")
        if not d:
            continue
        if "ths" in e["_src"]:
            ind = esc((e.get("params") or {}).get("indicator"))
            L += [f"**同花顺** —— `{ind}`", ""]
            if isinstance(d, list) and d:
                cols = list(d[0].keys())
                L += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                for r in d:
                    L.append("| " + " | ".join(esc(r.get(c)) for c in cols) + " |")
            L.append("")
        else:
            L += ["**Yahoo(池含外资行)**", "",
                  f"- 目标价:均 **{num(d.get('target_mean'), 2)}** ｜ 低 {num(d.get('target_low'), 2)} "
                  f"｜ 高 {num(d.get('target_high'), 2)} ｜ 分析师 **{d.get('n_analysts')}** 家 "
                  f"｜ 建议 `{d.get('recommendation')}`", ""]
            for key, cn in (("earnings_estimate", "每股收益预估"), ("revenue_estimate", "营收预估")):
                t = d.get(key)
                if t:
                    cols = [c for c in t[0].keys() if c != "currency"]
                    L += [f"*{cn}*　`0q`=本季　`+1q`=下季　`0y`=本年　`+1y`=次年", "",
                          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                    for r in t:
                        L.append("| " + " | ".join(num(r.get(c), 2) for c in cols) + " |")
                    L.append("")
    return L


def sec_ratings(code, envs):
    L = head_block("ratings", code, envs)
    L += ["> **回看 90 天不是 30 天。** 2026-07-20 高盛把新易盛目标价 841→633(−25%),"
          "落在 30 天窗口外 —— 上一版报告因此写成「零下调」。"
          "`外资系` 一列标出合资券商(野村东方国际 / 汇丰前海等),纯 A 股唯一的外资口径。", ""]
    for e in envs:
        d = e.get("data") or []
        if not d:
            L += ["回看期内**无评级记录**。", ""]
            continue
        L += ["| 发布日 | 机构 | 评级 | 评级变化 | 前一次 | 目标价下限 | 上限 | 外资系 |",
              "|---|---|---|---|---|---|---|---|"]
        for r in d[-15:]:
            L.append(f"| {esc(r.get('发布日期'))[:10]} | {esc(r.get('研究机构简称'))} "
                     f"| **{esc(r.get('投资评级'))}** | {esc(r.get('评级变化'))} "
                     f"| {esc(r.get('前一次投资评级'))} | {num(r.get('目标价格-下限'), 2)} "
                     f"| {num(r.get('目标价格-上限'), 2)} "
                     f"| {'**是**' if r.get('_是否外资系') else '否'} |")
        L.append("")
    return L


def sec_research(code, envs):
    L = head_block("research", code, envs)
    for e in envs:
        d = e.get("data") or []
        if "reportapi" in e["_src"]:
            L += [f"**研报列表** —— {len(d)} 篇", "",
                  "| 发布日 | 机构 | 分析师 | 标题 | 评级 |", "|---|---|---|---|---|"]
            for r in d[:12]:
                L.append(f"| {str(r.get('publishDate') or '')[:10]} | {esc(r.get('orgSName'))} "
                         f"| {esc(r.get('researcher'))[:16]} | {esc(r.get('title'))[:50]} "
                         f"| {esc(r.get('emRatingName'))} |")
            L.append("")
        else:
            L += [f"**已下载原件** —— {len(d)} 份 PDF。"
                  "存原件不只存抽出的文字 —— 抽错了能回去核。", "",
                  "| 日期 | 机构 | 标题 | 本地路径 | 大小 |", "|---|---|---|---|---|"]
            for r in d[:12]:
                kb = f"{(r.get('bytes') or 0)/1024:.0f} KB" if r.get("bytes") else "(已缓存)"
                L.append(f"| {esc(r.get('date'))} | {esc(r.get('org'))} "
                         f"| {esc(r.get('title'))[:42]} | `{esc(r.get('path'))}` | {kb} |")
            L.append("")
    return L


def sec_quotes(code, envs):
    L = head_block("quotes", code, envs)
    for e in envs:
        d, f = e.get("data") or [], fields_of(e)
        if "adjust_factor" in e["_src"]:
            L += [f"**复权因子** —— {len(d)} 次除权除息。"
                  "**目标价 / 成本价与日线必须换算到同一基准再比** —— 2026-09-01 踩过:"
                  "拿未复权目标价比未复权日线,中间隔着一次送转(系数 1.40),涨幅全算错。", ""]
            if d and f:
                L += rows_table(f, d, ["dividOperateDate", "foreAdjustFactor",
                                       "backAdjustFactor"], 10) + [""]
        else:
            rng = f"{d[0][0]} ~ {d[-1][0]}" if d else "?"
            L += [f"**日线(前复权)** —— {len(d)} 根,{rng}。"
                  "含 `peTTM` / `pbMRQ`,是历史估值分位的**唯一来源**。", "",
                  "最近 10 个交易日:", ""]
            if d and f:
                L += rows_table(f, d[-10:], ["date", "close", "volume", "peTTM",
                                             "pbMRQ", "turn"], 10)
                try:
                    i = f.index("peTTM")
                    pe = sorted(float(r[i]) for r in d if r[i] not in ("", None))
                    cur = float(d[-1][i])
                    pct = bisect.bisect_left(pe, cur) / len(pe) * 100
                    L += ["", f"> **PE(TTM) 历史分位**:当前 {cur:.2f}x,在 {len(pe)} 个有效交易日里"
                          f"排 **{pct:.1f}%**;p10={pe[len(pe)//10]:.1f} p25={pe[len(pe)//4]:.1f} "
                          f"p50={pe[len(pe)//2]:.1f} p75={pe[len(pe)*3//4]:.1f}。", "",
                          "> ⚠ **这是 TTM 口径。拿它去乘「前瞻」利润是错的。** 卖方用的是"
                          "前瞻 PE 的历史带(高盛给新易盛的是 −1σ 17x / 均值 28x / +1σ 40x),"
                          "两者差 60%。"]
                except Exception:
                    pass
            L.append("")
    return L


CHIP_CN = {"HOLDER_NUM": "股东户数(户)", "PRE_HOLDER_NUM": "上期户数(户)",
           "HOLDER_NUM_CHANGE": "户数变化(户)", "HOLDER_NUM_RATIO": "变化率(%)",
           "END_DATE": "**数据截止日**", "PRE_END_DATE": "上期截止日",
           "AVG_MARKET_CAP": "户均市值(元)", "AVG_HOLD_NUM": "户均持股(股)"}


def sec_chips(code, envs):
    L = head_block("chips", code, envs)
    for e in envs:
        if not e.get("ok"):
            continue
        d, src = e.get("data"), e["_src"]
        if "HOLDERNUMLATEST" in src and d:
            r = d[0]
            L += ["**股东户数**", "", "| 项 | 值 |", "|---|---|"]
            for k, cn in CHIP_CN.items():
                if k in r:
                    v = str(r.get(k))[:10] if "DATE" in k else num(r.get(k), 2)
                    L.append(f"| {cn} | {v} |")
            L += ["", "> ⚠ **`END_DATE` 是数据截止日,和实时价不是同一时点。** "
                  "上一版报告把 6-30 的户数和当天价并排放且没标日期 —— "
                  "读的人会当成同时点的事实。", ""]
        elif "DMSK_TS_STOCKNEW" in src and d:
            r = d[0]
            L += [f"**资金流** —— 交易日 {str(r.get('TRADE_DATE') or '')[:10]}", "",
                  "| 项 | 值(元) |", "|---|---|"]
            for k in ("SUPERDEAL_INFLOW", "SUPERDEAL_OUTFLOW", "PRIME_INFLOW",
                      "PRIME_OUTFLOW", "CLOSE_PRICE"):
                if k in r:
                    L.append(f"| `{k}` | {num(r.get(k), 0)} |")
            L.append("")
        elif "BLOCKTRADE" in src and d:
            L += [f"**大宗交易** —— 历史 {e.get('rows')} 笔,最近 8 笔", "",
                  "| 交易日 | 笔数 | 成交量 | 成交额 | 均价 | 收盘价 | 折溢价 |",
                  "|---|---|---|---|---|---|---|"]
            for r in d[:8]:
                ap, cp = r.get("AVERAGE_PRICE"), r.get("CLOSE_PRICE")
                try:
                    prem = f"{(float(ap)/float(cp)-1)*100:+.1f}%"
                except Exception:
                    prem = "—"
                L.append(f"| {str(r.get('TRADE_DATE') or '')[:10]} | {num(r.get('DEAL_NUM'), 0)} "
                         f"| {num(r.get('VOLUME'), 0)} | {num(r.get('DEAL_AMT'), 0)} "
                         f"| {num(ap, 2)} | {num(cp, 2)} | {prem} |")
            L.append("")
        elif isinstance(d, list) and d and isinstance(d[0], dict):
            nm = {"restricted": "限售解禁", "margin": "融资融券", "top10": "前十大流通股东"}
            title = next((v for k, v in nm.items() if k in src), src)
            cols = list(d[0].keys())[:8]
            L += [f"**{title}** —— {len(d)} 条", "",
                  "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for r in d[:8]:
                L.append("| " + " | ".join(esc(r.get(c))[:20] for c in cols) + " |")
            L.append("")
        elif isinstance(d, list) and not d:
            L += [f"`{src}` —— **0 条**(该股当前无此项)", ""]
    return L


def sec_financials(code, envs):
    L = head_block("financials", code, envs)
    L += ["> 这一节是**后视镜**,用来验证前面的领先指标有没有兑现,不用来预测。"
          "字段后面反引号里是 baostock 的原始英文名,便于对着官方文档核。", ""]
    cn = {"profit": "盈利能力", "growth": "成长能力", "balance": "偿债能力",
          "cash_flow": "现金流质量", "dupont": "杜邦分解", "operation": "营运能力"}
    for key in ("profit", "growth", "balance", "cash_flow", "dupont", "operation"):
        for e in envs:
            if key not in e["_src"] or not e.get("ok"):
                continue
            d, f = e.get("data") or [], fields_of(e)
            if not d or not f:
                continue
            L += [f"**{cn[key]}** `{e['_src']}`", ""] + kv_table(f, d[0]) + [""]
    return L


SECTIONS = [("forecast", sec_forecast), ("surveys", sec_surveys),
            ("announcements", sec_announcements), ("consensus", sec_consensus),
            ("ratings", sec_ratings), ("research", sec_research),
            ("quotes", sec_quotes), ("chips", sec_chips), ("financials", sec_financials)]


def stock_doc(code: str, raw: Path) -> str:
    all_envs, by_group, missing = [], {}, []
    for g, _ in SECTIONS:
        items = load(raw, g, code)
        if not items:
            missing.append(g)
            continue
        by_group[g] = items
        all_envs += items
    mark_runs(all_envs)

    cur = [e for e in all_envs if not e.get("_stale")]
    ok = sum(1 for e in cur if e.get("ok"))
    L = [f"# {code} · 数据源数据文档", "",
         f"> 生成于 {date.today().isoformat()}　·　数据目录 `{raw}`", "",
         "**这份文档是拿来核对的。** 每一节都写清三件事:数据**怎么拿的**、"
         "**去哪能核对**、**怎么重跑**。数字有疑问就点链接看官方页面。", "",
         "## 零、完备性一览", "",
         f"最近一轮 **{len(cur)}** 条数据,成功 **{ok}** 条、失败 **{len(cur)-ok}** 条"
         + (f";另有 **{len(missing)}** 类还没抓" if missing else "") + "。", "",
         "| 类别 | 属性 | 条数 | 状态 | 数据日 | 去哪核对 |", "|---|---|---|---|---|---|"]
    for g, _ in SECTIONS:
        if g in missing:
            L.append(f"| {CN[g]} | {MARK[LEAD[g]]} | — | ⚠ **还没抓** | — "
                     f"| {HOW[g][1].format(code=code)} |")
            continue
        es = [e for e in by_group[g] if not e.get("_stale")]
        bad = [e for e in es if not e.get("ok")]
        n = sum(e.get("rows") or 0 for e in es if e.get("ok"))
        when = "、".join(sorted({str(e.get("fetched_at"))[:10] for e in es}))
        st = "✅" if not bad else f"❌ {len(bad)} 个源失败"
        L.append(f"| {CN[g]} | {MARK[LEAD[g]]} | {n} | {st} | {when} "
                 f"| {HOW[g][1].format(code=code)} |")
    L.append("")

    n = 1
    for lead in ("领先", "同步", "滞后"):
        gs = [(g, fn) for g, fn in SECTIONS if LEAD[g] == lead and g in by_group]
        if not gs:
            continue
        L += [f"## {'一二三四五'[n-1]}、{MARK[lead]}指标", ""]
        n += 1
        for g, fn in gs:
            es = [e for e in by_group[g] if not e.get("_stale")]
            if not es:
                continue
            try:
                L += fn(code, es)
            except Exception as ex:
                L += [f"### {CN[g]}", "", f"⚠ 渲染失败:`{type(ex).__name__}: {ex}`", ""]
            for e in es:
                if not e.get("ok"):
                    L += [f"> ❌ `{e['_src']}` 抓取失败:`{str(e.get('error'))[:150]}`", ""]

    if missing:
        L += ["## 还没抓的类别", "",
              "**「没抓到」和「不存在」是两回事**,别当成后者。", "",
              "| 类别 | 属性 | 怎么补 |", "|---|---|---|"]
        for g in missing:
            L.append(f"| {CN[g]} | {MARK[LEAD[g]]} "
                     f"| `$VENV/bin/python {HOW[g][2].format(code=code)}` |")
        L.append("")

    L += ["---", "",
          "*由 `tools/data_digest.py` 从 `data/raw/` 的落盘数据生成。*",
          "*数据源总清单(42 条,含确认拿不到的)见 `docs/DATA-SOURCES.md`。*"]
    return "\n".join(L)


def overseas_doc(raw: Path) -> str:
    L = ["# 海外上下游与宏观 · 数据源数据文档", "",
         "> 我们分析的是 A 股,但**需求端在海外**。云厂 capex 决定光模块,"
         "油服 capex 与钻机数决定油气设服,前道设备商决定半导体设备。"
         "下面这些是**领先指标,不是背景资料**。", "",
         "- **怎么拿**:`yfinance` —— 目标价 / 分析师预估 / 评级变动",
         "- **去哪核对**:`https://finance.yahoo.com/quote/<代码>/analysis`",
         "- **重跑**:`$VENV/bin/python tools/fetch_all.py --peers`", ""]
    d = raw / "overseas"
    if d.is_dir():
        L += ["## 一、海外同业与下游客户", "",
              "| 代码 | 赛道分组 | 现价 | 目标价均 | 低~高 | 分析师 | 建议 | 评级变动 | 抓于 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for tk in sorted(x.name for x in d.iterdir() if x.is_dir()):
            for e in load(raw, "overseas", tk):
                v = e.get("data") or {}
                ud = v.get("upgrades_downgrades")
                L.append(f"| **{tk}** | {(e.get('params') or {}).get('sector_group', '')} "
                         f"| {num(v.get('price'), 2)} | **{num(v.get('target_mean'), 2)}** "
                         f"| {num(v.get('target_low'), 2)}~{num(v.get('target_high'), 2)} "
                         f"| {v.get('n_analysts')} | {v.get('recommendation')} "
                         f"| {0 if not ud else len(ud)} 条 | {str(e.get('fetched_at'))[:10]} |")
        L += ["", "> **怎么读**:目标价的绝对值信息量很低(卖方长期追价);"
              "**有信息量的是调整方向** —— 见下表。", "",
              "## 二、最近的评级与目标价调整", "",
              "| 代码 | 日期 | 机构 | 动作 | 新目标价 | 前值 | 评级 |",
              "|---|---|---|---|---|---|---|"]
        recent = []
        for tk in sorted(x.name for x in d.iterdir() if x.is_dir()):
            for e in load(raw, "overseas", tk):
                for r in ((e.get("data") or {}).get("upgrades_downgrades") or [])[:4]:
                    recent.append((str(r.get("GradeDate"))[:10], tk, r))
        # 只按前两项排 —— 三元组的第三项是 dict,日期和代码都相同时
        # Python 会拿两个 dict 比大小,直接 TypeError。
        for dt_, tk, r in sorted(recent, key=lambda x: (x[0], x[1]), reverse=True)[:26]:
            L.append(f"| **{tk}** | {dt_} | {esc(r.get('Firm'))} "
                     f"| {esc(r.get('priceTargetAction'))} | {num(r.get('currentPriceTarget'), 2)} "
                     f"| {num(r.get('priorPriceTarget'), 2)} | {esc(r.get('ToGrade'))} |")
        L.append("")
    t = raw / "transcripts"
    if t.is_dir():
        L += ["## 三、电话会纪要 —— **下期 capex 指引只在这里**", "",
              "- **怎么拿**:Motley Fool,URL 规律 "
              "`/earnings/call-transcripts/YYYY/MM/DD/<公司>-<代码>-qN-YYYY-earnings-call-transcript/`",
              "- **为什么必须要**:SEC XBRL 只有**已发生**的 capex 总额;"
              "**下期指引、短周期/长周期拆分、公司自己的季度指引**只存在于纪要正文。", "",
              "| 会议 | 正文字数 | 含 capex | 本地路径 | 抓于 |", "|---|---|---|---|---|"]
        for k in sorted(x.name for x in t.iterdir() if x.is_dir()):
            for e in load(raw, "transcripts", k):
                v = e.get("data") or {}
                if e.get("ok"):
                    L.append(f"| {k} | {v.get('chars', 0):,} | "
                             f"{'✅' if v.get('has_capex') else '—'} | `{esc(v.get('path'))}` "
                             f"| {str(e.get('fetched_at'))[:10]} |")
                else:
                    L.append(f"| {k} | — | — | ❌ **抓取失败**,URL 可能不对 "
                             f"| {str(e.get('fetched_at'))[:10]} |")
        L.append("")
    m = raw / "macro"
    if m.is_dir():
        L += ["## 四、宏观", "",
              "- 油价 `新浪外盘期货 hf_CL/hf_OIL` ｜ 中美国债 `akshare bond_zh_us_rate` "
              "｜ 汇率中间价 `CFETS CcprHisNew` ｜ 全球指数 `akshare index_global_spot_em`",
              "- **重跑**:`$VENV/bin/python tools/fetch_all.py --macro`", "",
              "| 项 | 源 | 状态 | 条数 | 抓于 |", "|---|---|---|---|---|"]
        for f in sorted(m.glob("*.json")):
            try:
                for src, e in json.loads(f.read_text(encoding="utf-8")).items():
                    nm = f.stem.split("-", 3)[-1] if "-" in f.stem else f.stem
                    L.append(f"| {nm} | `{src}` | {'✅' if e.get('ok') else '❌'} "
                             f"| {e.get('rows')} | {str(e.get('fetched_at'))[:10]} |")
            except Exception:
                pass
        L.append("")
    L += ["---", "", "*由 `tools/data_digest.py --overseas` 生成。*"]
    return "\n".join(L)


def repo_root() -> Path:
    """向上找仓根,不用固定层数的 parent —— 脚本挪目录时那种写法会静默错。"""
    d = Path(__file__).resolve()
    for _ in range(6):
        d = d.parent
        if (d / "skills").is_dir() and (d / "tools").is_dir():
            return d
    return Path(__file__).resolve().parents[3]


def to_pdf(md: Path, sub: str) -> None:
    root = repo_root()
    m2h = root / "skills" / "finance-pdf-report" / "scripts" / "md2html.py"
    h2p = root / "skills" / "finance-pdf-report" / "scripts" / "html2pdf.mjs"
    html = md.with_suffix(".html")
    subprocess.run([sys.executable, str(m2h), str(md), str(html), sub,
                    "--landscape", "--no-disclaimer"], check=True)
    if subprocess.run(["node", str(h2p), str(html), str(md.with_suffix(".pdf")), sub]).returncode:
        print("  ⚠ PDF 没出来。装一次:npm i playwright && npx playwright install chromium"
              ",或设 PLAYWRIGHT_ROOT 指到已装的位置")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", action="append", default=[])
    ap.add_argument("--overseas", action="store_true")
    ap.add_argument("--raw", default="data/raw")
    # 默认落 private/ —— 明细只含公开行情,但**选了哪几只本身就暴露自选**。
    ap.add_argument("--out", default="private/data-digest")
    ap.add_argument("--pdf", action="store_true")
    a = ap.parse_args()
    if not a.code and not a.overseas:
        ap.error("至少给 --code 或 --overseas")

    raw, out = Path(a.raw), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for code in a.code:
        p = out / f"{code}-数据源数据文档.md"
        p.write_text(stock_doc(code, raw), encoding="utf-8")
        print(f"→ {p}  ({p.stat().st_size/1024:.0f} KB)")
        if a.pdf:
            to_pdf(p, f"{code} · 数据源数据文档 · {date.today().isoformat()}")
    if a.overseas:
        p = out / "海外上下游-数据源数据文档.md"
        p.write_text(overseas_doc(raw), encoding="utf-8")
        print(f"→ {p}  ({p.stat().st_size/1024:.0f} KB)")
        if a.pdf:
            to_pdf(p, f"海外上下游与宏观 · {date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
