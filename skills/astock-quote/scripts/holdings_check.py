#!/usr/bin/env python3
"""持仓业绩体检 —— 拉最新季报做同比对比,按"拿住/减仓信号"给判断。

需求背景:长线持有光模块这类景气成长股,不能靠成本价决定去留,要靠业绩验证。
本脚本每次运行拉最新一期财报,和去年同期比:净利/营收同比、毛利率变化、PE(TTM),
再按预设阈值给"拿住信号还在不在"的判断。

用法:
    P=$VENV/bin/python
    $P holdings_check.py 300502 300308                 # 只体检
    $P holdings_check.py 300502:400.00 300308:1100.00 # 带成本,附浮亏

数据源:财务=新浪/雪球(akshare stock_financial_abstract);PE/市值=腾讯;价=新浪。
行情/估值不用东财源(push2 类会被限流)。代理只对本进程关闭,不动 shell 全局。

判据(拿住信号):
  净利同比 >50% = 强(继续拿) · 0~50% = 减速警惕 · <0 = 业绩破位(卖出信号)
  毛利率同比 持平/上升 = OK · 下滑 >3 个百分点 = 警惕
消息面(1.6T放量/订单/大客户长约/解禁/砍单)需联网,脚本末尾给提示,用 WebSearch 补。
"""
import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import requests  # noqa: E402


def to_sina_symbol(code: str) -> str:
    code = code.strip().lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if code[0] == "6":
        return "sh" + code
    if code[0] in ("0", "3"):
        return "sz" + code
    return "bj" + code


def fetch_price(code6: str):
    """新浪快照:返回 (name, price, prev)。"""
    sym = to_sina_symbol(code6)
    r = requests.get(f"https://hq.sinajs.cn/list={sym}",
                     headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
    r.encoding = "gbk"
    f = r.text.split('"')[1].split(",")
    return f[0], float(f[3]), float(f[2])


def fetch_pe_cap(code6: str):
    """腾讯:返回 (pe_ttm, 总市值亿)。字段 39=PE(TTM),45=总市值(亿)。"""
    sym = to_sina_symbol(code6)
    r = requests.get(f"https://qt.gtimg.cn/q={sym}",
                     headers={"Referer": "https://gu.qq.com"}, timeout=15)
    r.encoding = "gbk"
    f = r.text.split("~")
    try:
        pe = float(f[39]) if f[39] not in ("", "-") else None
    except (IndexError, ValueError):
        pe = None
    try:
        cap = float(f[45]) if f[45] not in ("", "-") else None
    except (IndexError, ValueError):
        cap = None
    return pe, cap


def valuation_percentile(code6: str, start="2020-01-01"):
    """baostock 估值历史:返回当前 PE(TTM)/PB 及其历史分位。北交所不支持返回 None。"""
    if code6[0] in ("4", "8") or code6[:2] == "92":
        return None
    mkt = "sh." if code6[0] == "6" else "sz."
    import baostock as bs
    import pandas as pd
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            mkt + code6, "date,peTTM,pbMRQ",
            start_date=start, frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "peTTM", "pbMRQ"])
    pe = pd.to_numeric(df["peTTM"], errors="coerce")
    pb = pd.to_numeric(df["pbMRQ"], errors="coerce")
    pe = pe[pe > 0]
    pb = pb[pb > 0]
    out = {}
    if len(pe):
        out["pe"] = (pe.iloc[-1], (pe < pe.iloc[-1]).mean() * 100, pe.min(), pe.max())
    if len(pb):
        out["pb"] = (pb.iloc[-1], (pb < pb.iloc[-1]).mean() * 100, pb.min(), pb.max())
    out["start"] = df["date"].iloc[0]
    return out


def _row(df, name):
    """从 stock_financial_abstract 取某指标行(常用指标组),返回 {日期:值}。"""
    sub = df[df["指标"] == name]
    if sub.empty:
        return {}
    row = sub.iloc[0]
    out = {}
    for col in df.columns:
        if col in ("选项", "指标"):
            continue
        v = row[col]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:  # 非 NaN
            out[str(col)] = fv
    return out


def year_ago(date_str):
    """20260630 -> 20250630。"""
    return str(int(date_str[:4]) - 1) + date_str[4:]


def check_one(code6: str, cost=None):
    import akshare as ak
    name, price, prev = fetch_price(code6)
    pe, cap = fetch_pe_cap(code6)
    df = ak.stock_financial_abstract(symbol=code6)

    profit = _row(df, "归母净利润")
    rev = _row(df, "营业总收入")
    gm = _row(df, "毛利率")

    dates = sorted(profit.keys(), reverse=True)
    latest = dates[0] if dates else None
    ya = year_ago(latest) if latest else None

    print("=" * 58)
    hdr = f"{name} ({code6})   现价 {price:.2f}"
    if cost is not None:
        pnl = (price - cost) / cost * 100
        hdr += f"   成本 {cost:.3f}   浮盈亏 {pnl:+.1f}%"
    print(hdr)
    if pe or cap:
        print(f"  PE(TTM): {pe if pe else '?'}    总市值(亿): {cap if cap else '?'}")

    # 估值分位(baostock,本机可用;不依赖东财)
    try:
        v = valuation_percentile(code6)
        if v:
            if "pe" in v:
                c, pct, lo, hi = v["pe"]
                tag = "低估" if pct < 30 else ("偏高" if pct > 70 else "中性")
                print(f"  PE 分位({v['start'][:4]}至今): {pct:.0f}% [{tag}]  区间 {lo:.0f}~{hi:.0f}")
            if "pb" in v:
                c, pct, lo, hi = v["pb"]
                tag = "低估" if pct < 30 else ("偏高" if pct > 70 else "中性")
                print(f"  PB 分位({v['start'][:4]}至今): {pct:.0f}% [{tag}]  区间 {lo:.1f}~{hi:.1f}")
    except Exception as e:
        print(f"  (估值分位取数失败: {repr(e)[:80]})")

    if not latest:
        print("  !! 拉不到财务数据")
        return

    def yoy(d):
        if latest in d and ya in d and d[ya] != 0:
            return (d[latest] - d[ya]) / abs(d[ya]) * 100
        return None

    p_now, r_now = profit.get(latest), rev.get(latest)
    p_yoy, r_yoy = yoy(profit), yoy(rev)
    gm_now = gm.get(latest)
    gm_ya = gm.get(ya)

    print(f"  最新报告期: {latest}  (与 {ya} 同比)")
    if r_now is not None:
        s = f"  营业总收入: {r_now/1e8:.2f} 亿"
        if r_yoy is not None:
            s += f"  同比 {r_yoy:+.1f}%"
        print(s)
    if p_now is not None:
        s = f"  归母净利润: {p_now/1e8:.2f} 亿"
        if p_yoy is not None:
            s += f"  同比 {p_yoy:+.1f}%"
        print(s)
    if gm_now is not None:
        s = f"  毛利率: {gm_now:.1f}%"
        if gm_ya is not None:
            s += f"  (去年同期 {gm_ya:.1f}%, {gm_now-gm_ya:+.1f}pp)"
        print(s)

    # 判据
    print("  --- 拿住信号 ---")
    verdict = []
    if p_yoy is not None:
        if p_yoy > 50:
            verdict.append(f"✅ 净利同比 {p_yoy:+.0f}% >50%,高增在,继续拿")
        elif p_yoy >= 0:
            verdict.append(f"⚠️ 净利同比 {p_yoy:+.0f}% 掉到个位/低增,减速警惕")
        else:
            verdict.append(f"🔴 净利同比 {p_yoy:+.0f}% 转负,业绩破位=卖出信号")
    if gm_now is not None and gm_ya is not None:
        d = gm_now - gm_ya
        if d >= -0.5:
            verdict.append(f"✅ 毛利率 {gm_now:.0f}% 维持({d:+.1f}pp)")
        elif d > -3:
            verdict.append(f"⚠️ 毛利率小幅下滑 {d:+.1f}pp")
        else:
            verdict.append(f"🔴 毛利率下滑 {d:+.1f}pp,盈利质量转弱")
    for v in verdict:
        print("   " + v)
    if not verdict:
        print("   (数据不足,无法判断)")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    for a in args:
        if ":" in a:
            code, cost = a.split(":", 1)
            cost = float(cost)
        else:
            code, cost = a, None
        try:
            check_one(code.strip(), cost)
        except Exception as e:
            print(f"[{a}] 失败: {repr(e)[:160]}")
        print()
    print("─" * 58)
    print("消息面(1.6T放量/订单排期/大客户长约/解禁/砍单/研报目标价)需联网,")
    print("脚本不含联网搜索;让 Claude 用 WebSearch 查最新,或看 App 的新闻页。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
