#!/usr/bin/env python3
"""efdata —— 东财数据速查(基于 efinance),补 astock.py 拿不到的那些。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/efdata.py <子命令> [参数] [--csv 文件]
    $P $LAB/tools/efdata.py --check      # 探测哪些接口当前可用
    $P $LAB/tools/efdata.py --list       # 列出全部子命令

分工(重要,别拿本脚本当行情工具):
- 行情 / K线 / 五档 / 逐笔 → 用 astock.py(新浪 + 腾讯,本机稳定)
- 东财独有的报表数据(龙虎榜 / 股东户数 / 全市场业绩 / 可转债 / 基金)→ 用本脚本

本机可达性(2026-08-27 实测 36 个 efinance 接口,26 个通):
- 稳定:datacenter-web / datacenter / emh5 / fundmobapi / hsmarketwg / fundztapi
- 取不到:push2 / push2his 上的行情类接口(实时报价 / K线 / 逐笔 / 资金流 / 基本信息)
- 协议不是判据:datacenter-web 走的就是明文 http 却一直稳定,push2his 走 https 照样不通;
  同一台 push2 上 get_belong_board 能通而 get_latest_quote 不通 —— 按接口路径分的。
  标 [不稳] 的子命令失败属预期,脚本会提示替代方案,不要重试硬刚。

代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局(Claude Code 还要用)。
"""

import os
import sys

# ── 必须在 import efinance / requests 之前去代理 ────────────────────────────
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

# ── 全局 socket 超时(必须在 import akshare/efinance 之前设)──────────────
# akshare / efinance 内部的请求普遍不带 timeout。对端瞬时抖动时会**永久挂起**
# 而不是报错(2026-08-30 实测:连接全在 CLOSE_WAIT,卡 10 分钟没动)。
# requests/urllib3 未显式指定 timeout 时会退回 socket 全局默认值,所以这里设一次
# 就能兜住所有下游库。**挂起比报错更糟** —— 报错能被重试,挂起会静默卡死流水线。
import socket  # noqa: E402

socket.setdefaulttimeout(float(os.environ.get("ASTOCK_SOCKET_TIMEOUT", "30")))

import argparse  # noqa: E402
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


# efinance 优先用已安装版本;没装则回落到 <仓>/repos 下的 clone
try:
    import efinance as ef
except ImportError:
    _repo = _lab_root() / "repos" / "efinance"
    if not _repo.is_dir():
        sys.exit(f"找不到 efinance。装它:pip install efinance\n"
                 f"或把仓库 clone 到 {_repo}")
    sys.path.insert(0, str(_repo))
    try:
        import efinance as ef
    except ImportError as e:
        sys.exit(f"从 {_repo} 导入 efinance 失败:{e}\n"
                 f"缺依赖就装:pip install retry multitasking rich jsonpath")

import pandas as pd  # noqa: E402

# ── 稳定档(本机实测可用)────────────────────────────────────────────────────
STABLE = {
    "lhb": (
        "龙虎榜明细(可指定日期区间)",
        "datacenter-web",
        lambda a: ef.stock.get_daily_billboard(
            start_date=a.start, end_date=a.end),
    ),
    "holders": (
        "前十大流通股东(需 <代码>;--periods N 取最近 N 个报告期,默认 1)",
        "emh5",
        lambda a: ef.stock.get_top10_stock_holder_info(
            _need(a, "code"), top=a.periods),
    ),
    "holdernum": (
        "全市场股东户数变化(可指定报告期)",
        "datacenter-web",
        lambda a: ef.stock.get_latest_holder_number(date=a.date),
    ),
    "perf": (
        "全市场某季度业绩(营收/净利/毛利率/ROE 等 14 列)",
        "datacenter-web",
        lambda a: ef.stock.get_all_company_performance(date=a.date),
    ),
    "dates": (
        "全市场可用报告期列表",
        "datacenter",
        lambda a: ef.stock.get_all_report_dates(),
    ),
    "board": (
        "某只股票所属板块(需 <代码>)",
        "push2",
        lambda a: ef.stock.get_belong_board(_need(a, "code")),
    ),
    "members": (
        "指数成分股与权重(需 <指数代码>,如 000300)",
        "fundztapi",
        lambda a: ef.stock.get_members(_need(a, "code")),
    ),
    "ipo": (
        "企业 IPO 审核状态全表",
        "datacenter-web",
        lambda a: ef.stock.get_latest_ipo_info(),
    ),
    "snapshot": (
        "实时行情快照 37 字段(需 <代码>;含五档,与 astock --l5 重叠)",
        "hsmarketwg",
        lambda a: ef.stock.get_quote_snapshot(_need(a, "code")),
    ),
    "cb": (
        "可转债基本信息(不带代码=全表 1000+ 只;带代码=单只)",
        "datacenter-web",
        lambda a: (ef.bond.get_base_info(a.code) if a.code
                   else ef.bond.get_all_base_info()),
    ),
    "fundnav": (
        "基金历史净值(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_quote_history(_need(a, "code")),
    ),
    "fundpos": (
        "基金股票持仓明细(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_invest_position(_need(a, "code")),
    ),
    "fundind": (
        "基金行业分布(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_industry_distribution(_need(a, "code")),
    ),
    "fundmgr": (
        "基金经理与规模(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_fund_manager(_need(a, "code")),
    ),
    "fundpct": (
        "基金股债现金配置比例(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_types_percentage(_need(a, "code")),
    ),
    "fundchg": (
        "基金阶段涨幅与同类排名(需 <基金代码>)",
        "fundmobapi",
        lambda a: ef.fund.get_period_change(_need(a, "code")),
    ),
    "survey": (
        "机构调研记录(需 <代码>;akshare 封装已坏,本命令走东财直连)",
        "datacenter-web",
        lambda a: _survey(a),   # lambda 延迟求值:STABLE 字典在 _survey 定义之前构建
    ),
    "hksc": (
        "香港中央结算多期持股序列(需 <代码>;比十大股东单期长得多)",
        "datacenter(securities)",
        lambda a: _hksc(a),
    ),
    "northbound": (
        "北向个股持仓(需 <代码>;--daily 看停更前的日度序列)",
        "datacenter-web",
        lambda a: _northbound(a),
    ),
    "ann": (
        "公司公告(需 <代码>;含投资者关系记录、业绩预告)",
        "np-anotice-stock",
        lambda a: _ann(a),
    ),
    "fundcodes": (
        "某类型基金代码全表(--type gp股票型/hh混合型/zq债券型/zs指数型/etf/qdii)",
        "fund.eastmoney",
        lambda a: ef.fund.get_fund_codes(ft=a.type),
    ),
}

# ── 不稳档(push2 / push2his,本机常失败,给替代方案)─────────────────────────
UNSTABLE = {
    "kline": (
        "K 线历史",
        "push2his",
        "改用  astock <代码> --daily  (新浪源,本机稳定)",
        lambda a: ef.stock.get_quote_history(_need(a, "code"), klt=a.klt),
    ),
    "tick": (
        "当日逐笔成交",
        "push2",
        "改用  astock <代码> --tick  (腾讯源,本机稳定)",
        lambda a: ef.stock.get_deal_detail(_need(a, "code"), max_count=a.top),
    ),
    "bill": (
        "历史每日主力资金流",
        "push2his",
        "本机暂无替代;需要就在另一台网络不受限的机器 上跑",
        lambda a: ef.stock.get_history_bill(_need(a, "code")),
    ),
    "billtoday": (
        "当日分钟级资金流",
        "push2",
        "本机暂无替代;需要就在另一台网络不受限的机器 上跑",
        lambda a: ef.stock.get_today_bill(_need(a, "code")),
    ),
    "info": (
        "股票基本信息(市盈率/市净率/所处行业等)",
        "push2",
        "估值分位改用  hcheck <代码>:<成本价>  (baostock 源)",
        lambda a: ef.stock.get_base_info(_need(a, "code")),
    ),
    "spot": (
        "全市场实时行情(5000+ 行)",
        "push2",
        "单只用  astock <代码>;全市场本机暂无替代",
        lambda a: ef.stock.get_realtime_quotes(a.fs),
    ),
}


def _em_datacenter(report_name, filters="", page_size=50, sort_col="", desc=True):
    """直接打东财 datacenter-web,绕过 efinance/akshare 的封装。

    ⚠ 为什么需要这个:有些数据 akshare 的封装已经坏了(接口变更后没跟上),
    但**底层东财接口是通的**。典型例子:机构调研 —— akshare 的
    `stock_jgdy_detail_em` 在本机报 TypeError,而直接打 RPT_ORG_SURVEYNEW 正常返回。
    (2026-08-27 实测)

    reportName 速查:
      RPT_ORG_SURVEYNEW      机构调研(按个股 filter SECURITY_CODE)
      RPT_MUTUAL_TOP10DEAL   沪深股通十大活跃股(MUTUAL_TYPE:001沪 003深 002港)
      RPT_DAILYBILLBOARD_DETAILSNEW  龙虎榜
    """
    import json
    import urllib.parse
    import urllib.request
    q = {"reportName": report_name, "columns": "ALL", "pageSize": str(page_size)}
    if sort_col:
        q["sortColumns"] = sort_col
        q["sortTypes"] = "-1" if desc else "1"
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           + urllib.parse.urlencode(q))
    if filters:
        url += "&filter=" + urllib.parse.quote(filters, safe="")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
        "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=25) as f:
        d = json.loads(f.read())
    r = d.get("result")
    if not r or not r.get("data"):
        raise RuntimeError(f"东财返回空:{d.get('message')}")
    return pd.DataFrame(r["data"])


def _em_get(url, params, referer="https://data.eastmoney.com/", timeout=25):
    """通用 JSON GET —— 有几个东财接口不在 datacenter-web 上,参数形态也不同。"""
    import json
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120",
                 "Referer": referer,
                 "Accept": "application/json,text/html,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read())


def _hksc(a):
    """香港中央结算(港资)多期持股序列。

    ⚠ 这个接口在 **datacenter.eastmoney.com/securities/**,不是常用的
    datacenter-web,参数也多了 source=HSF10 & client=PC,别套 _em_datacenter。

    ★ 判方向要用**股数**不是比例:持股数增加但占总股本比例下降,
      那是**股本摊薄**(增发/转股),不是港资减仓。只看比例会看反。
    """
    import pandas as pd
    code = _need(a, "code")
    j = _em_get("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                {"reportName": "RPT_F10_EH_HOLDERS", "columns": "ALL",
                 "filter": f'(SECURITY_CODE="{code}")(HOLDER_NAME="香港中央结算有限公司")',
                 "pageNumber": "1", "pageSize": str(200 if a.all else max(a.head, 8)),
                 "sortTypes": "-1", "sortColumns": "END_DATE",
                 "source": "HSF10", "client": "PC"},
                referer="https://emweb.securities.eastmoney.com/")
    rows = ((j.get("result") or {}).get("data")) or []
    if not rows:
        raise RuntimeError(f"{code} 没有香港中央结算持股记录(可能不是陆股通标的)")
    out = []
    for r in rows:
        sh, chg = r.get("HOLD_NUM"), r.get("HOLD_NUM_CHANGE")
        try:
            sh = float(sh)
        except (TypeError, ValueError):
            continue
        try:
            chg = float(chg)
            direction = "增持" if chg > 0 else ("减持" if chg < 0 else "不变")
        except (TypeError, ValueError):
            chg, direction = None, ("新进" if "新进" in str(r.get("HOLD_NUM_CHANGE") or "")
                                    else "未知")
        out.append({"报告期": str(r.get("END_DATE") or "")[:10],
                    "持股万股": round(sh / 1e4, 2),
                    "占总股本%": r.get("HOLD_NUM_RATIO"),
                    "变动万股": None if chg is None else round(chg / 1e4, 2),
                    "变动比例%": r.get("CHANGE_RATIO"),
                    "方向": direction})
    df = pd.DataFrame(out).sort_values("报告期")
    return df


def _northbound(a):
    """北向个股持仓。季度表仍在更新;日度表停在监管停更前(约 2024-08-16)。

    ⚠ 这里给的是**持仓**不是**净买入**。2024-08-19 起交易所不再公布单票
      日度北向净买入 —— 那是监管改了披露规则,不是接口坏了,没有免费替代。
      持仓的相邻期差可以推方向,但那是季度粒度。
    """
    import pandas as pd
    code = _need(a, "code")
    if a.daily:
        df = _em_datacenter(
            "RPT_MUTUAL_HOLDSTOCKNDATE_STA",
            filters=f'(SECURITY_CODE="{code}")(INTERVAL_TYPE="1")',
            page_size=200 if a.all else max(a.head, 10),
            sort_col="TRADE_DATE")
        cols = {"TRADE_DATE": "日期", "HOLD_SHARES": "持股数",
                "HOLD_MARKET_CAP": "持股市值", "A_SHARES_RATIO": "占总股本%",
                "FREE_SHARES_RATIO": "占流通%", "ADD_SHARES_REPAIR": "增减股数"}
    else:
        df = _em_datacenter(
            "RPT_MUTUAL_HOLDSTOCKNORTH_STA",
            filters=f'(SECURITY_CODE="{code}")',
            page_size=200 if a.all else max(a.head, 10),
            sort_col="TRADE_DATE")
        cols = {"TRADE_DATE": "日期", "HOLD_SHARES": "持股数",
                "HOLD_MARKET_CAP": "持股市值",
                "A_SHARES_RATIO": "占总股本%", "FREE_SHARES_RATIO": "占流通%"}
    keep = [c for c in cols if c in df.columns]
    out = df[keep].rename(columns=cols)
    if "日期" in out.columns:
        out["日期"] = out["日期"].astype(str).str[:10]
    for c in ("持股数", "持股市值", "增减股数"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "持股数" in out.columns:
        out["持股万股"] = (out["持股数"] / 1e4).round(2)
        out = out.drop(columns=["持股数"])
    return out.sort_values("日期") if "日期" in out.columns else out


def _ann(a):
    """公司公告。投资者关系记录、业绩预告、重大合同都在这。

    ⚠ 这个接口在 **np-anotice-stock.eastmoney.com**,参数是下划线风格
      (page_size / stock_list),跟 datacenter-web 完全两套,别套。
    """
    import pandas as pd
    code = _need(a, "code")
    j = _em_get("https://np-anotice-stock.eastmoney.com/api/security/ann",
                {"sr": "-1", "page_size": str(100 if a.all else max(a.head, 20)),
                 "page_index": "1", "ann_type": "A", "client_source": "web",
                 "stock_list": code, "f_node": "0", "s_node": "0"})
    d = j.get("data") or {}
    rows = d.get("list") if isinstance(d, dict) else d
    if not rows:
        raise RuntimeError(f"{code} 没取到公告")
    out = []
    for r in rows:
        art = r.get("art_code") or r.get("info_code") or ""
        out.append({
            "日期": (r.get("notice_date") or "")[:10],
            "标题": r.get("notice_title") or r.get("title") or "",
            "链接": (f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                     if art else ""),
        })
    return pd.DataFrame(out)


def _survey(a):
    """机构调研 —— akshare 封装已坏,走直连。"""
    code = _need(a, "code")
    df = _em_datacenter("RPT_ORG_SURVEYNEW",
                        filters=f'(SECURITY_CODE="{code}")',
                        page_size=a.head if not a.all else 200,
                        sort_col="NOTICE_DATE")
    cols = {"NOTICE_DATE": "公告日", "RECEIVE_START_DATE": "调研日",
            "RECEIVE_OBJECT": "接待对象", "RECEIVE_PLACE": "地点",
            "RECEIVE_WAY_EXPLAIN": "方式", "INVESTIGATORS": "参与人员",
            "SECURITY_NAME_ABBR": "简称"}
    keep = [c for c in cols if c in df.columns]
    out = df[keep].rename(columns=cols)
    for c in ("公告日", "调研日"):
        if c in out.columns:
            out[c] = out[c].astype(str).str[:10]
    return out


def _need(a, field):
    v = getattr(a, field, None)
    if not v:
        sys.exit(f"子命令 `{a.cmd}` 需要 <{field}> 参数。看用法:efdata --list")
    return v


def _show(obj, args):
    """打印结果;--csv 则另存。Series 竖排,DataFrame 表格。"""
    if obj is None:
        print("(无数据)")
        return
    if isinstance(obj, pd.Series):
        df = obj.to_frame(name="值")
        df.index.name = "字段"
    elif isinstance(obj, pd.DataFrame):
        df = obj
    else:
        print(obj)
        return

    total = len(df)
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.csv, index=isinstance(obj, pd.Series),
                  encoding="utf-8-sig")
        print(f"已写入 {args.csv}  ({total} 行 × {len(df.columns)} 列)")
        return

    view = df if args.all else df.head(args.head)
    with pd.option_context("display.max_columns", None,
                           "display.width", 200,
                           "display.max_colwidth", 28):
        print(view.to_string())
    if not args.all and total > len(view):
        print(f"\n… 共 {total} 行,只显示前 {len(view)} 行。"
              f"全部用 --all,存文件用 --csv out.csv")


def cmd_list():
    print("efdata —— 东财数据速查(补 astock.py 拿不到的报表类数据)\n")
    print("【稳定档】本机实测可用")
    for k, (desc, host, _) in STABLE.items():
        print(f"  {k:<11} {desc}")
        print(f"  {'':<11} └ 主机 {host}")
    print("\n【不稳档】push2 / push2his,本机常失败,失败属预期")
    for k, (desc, host, alt, _) in UNSTABLE.items():
        print(f"  {k:<11} {desc}")
        print(f"  {'':<11} └ 主机 {host} · 替代:{alt}")
    print("\n通用参数:--head N(默认 15)  --all  --csv 文件  --check")
    print("行情 / K线 / 五档 / 逐笔请用 astock,不要用本脚本。")


def cmd_check():
    """探测各主机当前是否可用,每类打一个代表性接口。"""
    probes = [
        ("datacenter-web", "龙虎榜",   lambda: ef.stock.get_daily_billboard()),
        ("datacenter",     "报告期",   lambda: ef.stock.get_all_report_dates()),
        ("emh5",           "前十股东", lambda: ef.stock.get_top10_stock_holder_info("300308", top=1)),
        ("hsmarketwg",     "实时快照", lambda: ef.stock.get_quote_snapshot("300308")),
        ("fundmobapi",     "基金净值", lambda: ef.fund.get_quote_history("161725")),
        ("fundztapi",      "指数成分", lambda: ef.stock.get_members("000300")),
        ("push2",          "所属板块", lambda: ef.stock.get_belong_board("300308")),
        ("push2his",       "K线",      lambda: ef.stock.get_quote_history("300308", klt=101)),
    ]
    print("== efdata 数据源探测(去代理直连)==")
    bad = []
    for host, what, fn in probes:
        try:
            r = fn()
            n = len(r) if hasattr(r, "__len__") else 1
            print(f"  [OK  ] {host:<16} {what:<9} {n} 行")
        except Exception as e:
            print(f"  [FAIL] {host:<16} {what:<9} {type(e).__name__}")
            bad.append(host)
    print()
    if set(bad) <= {"push2", "push2his"}:
        print("结论:稳定档全通。push2 / push2his 失败属本机预期,"
              "行情用 astock(新浪+腾讯)。")
        return 0
    print(f"结论:有稳定档主机失败({', '.join(sorted(set(bad) - {'push2', 'push2his'}))})"
          f" —— 检查网络/代理,或换 另一台机器 跑。")
    return 1


def main():
    p = argparse.ArgumentParser(
        prog="efdata", add_help=True,
        description="东财数据速查(efinance 封装)。行情用 astock,报表用本脚本。")
    p.add_argument("cmd", nargs="?", help="子命令,见 --list")
    p.add_argument("code", nargs="?", help="股票 / 基金 / 债券 / 指数代码")
    p.add_argument("--list", action="store_true", help="列出全部子命令")
    p.add_argument("--check", action="store_true", help="探测各数据源当前可用性")
    p.add_argument("--head", type=int, default=15, help="显示前 N 行(默认 15)")
    p.add_argument("--daily", action="store_true",
                   help="northbound:看停更前的日度持仓序列(默认看季度)")
    p.add_argument("--all", action="store_true", help="显示全部行")
    p.add_argument("--csv", metavar="文件", help="结果存 CSV(utf-8-sig,Excel 可直接开)")
    p.add_argument("--start", help="lhb:开始日期 如 2026-08-01")
    p.add_argument("--end", help="lhb:结束日期")
    p.add_argument("--date", help="perf / holdernum:报告期 如 2026-06-30")
    p.add_argument("--top", type=int, default=10, help="tick:取最近 N 笔成交")
    p.add_argument("--periods", type=int, default=1,
                   help="holders:取最近 N 个报告期(efinance 的 top 是报告期数,不是股东数)")
    p.add_argument("--klt", type=int, default=101, help="kline:周期 101日 102周 103月")
    p.add_argument("--type", default="gp", help="fundcodes:基金类型 gp/hh/zq/zs/etf/qdii")
    p.add_argument("--fs", default="沪深A股", help="spot:市场范围")
    a = p.parse_args()

    # --check 必须先判:它可以不带子命令,否则会被下面的 `not a.cmd` 截走
    if a.check or a.cmd == "check":
        return cmd_check()
    if a.list or not a.cmd:
        cmd_list()
        return 0

    if a.cmd in STABLE:
        desc, host, fn = STABLE[a.cmd]
        try:
            _show(fn(a), a)
            return 0
        except SystemExit:
            raise
        except Exception as e:
            print(f"取数失败({host}):{type(e).__name__}: {e}", file=sys.stderr)
            print("这属于稳定档,失败不正常 —— 跑 efdata --check 看是不是网络问题。",
                  file=sys.stderr)
            return 1

    if a.cmd in UNSTABLE:
        desc, host, alt, fn = UNSTABLE[a.cmd]
        try:
            _show(fn(a), a)
            return 0
        except SystemExit:
            raise
        except Exception as e:
            print(f"取数失败({host}):{type(e).__name__}", file=sys.stderr)
            print(f"这条在本机失败属预期(push2 系接口取不到)。", file=sys.stderr)
            print(f"替代方案:{alt}", file=sys.stderr)
            return 2

    print(f"未知子命令 `{a.cmd}`。看全部:efdata --list", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
