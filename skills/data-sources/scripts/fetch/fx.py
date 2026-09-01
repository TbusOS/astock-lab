#!/usr/bin/env python3
"""fx —— 人民币汇率与汇兑影响估算。

**为什么要这个工具**:出口占比高的公司,汇率是直接进利润表的变量,不是背景噪音。
新易盛 1H26 境外销售 204.60 亿,占营收约 98%;研报里明确写了「1Q 物料与汇兑承压」
「剔除汇兑影响,公司期间费用率保持低位」。汇率动 1%,对这类公司就是两亿量级的事。

数据源(都免费、都不要 key):
  - **中间价历史**:中国外汇交易中心 chinamoney.com.cn —— **官方口径**,日频序列
  - **即期报价**:新浪 fx_susdcny —— 实时,用来看中间价之后有没有继续走

用法:
    fx                          # USD/CNY 近 90 天中间价 + 即期
    fx --days 180
    fx --overseas-share 98      # 给出境外收入占比,估算汇兑对营收的影响
    fx --currency EUR/CNY
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request

CCPR = ("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
        "?startDate={start}&endDate={end}&currency={cur}&pageNum=1&pageSize={size}")
SINA = "https://hq.sinajs.cn/list=fx_s{pair}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


# 中国货币网只认「像浏览器」的请求头：光带 User-Agent 会 403，
# 必须同时给 Referer + Accept + Accept-Language（2026-08-29 实测）。
def _get(url: str, referer: str | None = None, encoding: str = "utf-8") -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(encoding, errors="replace")


# ⚠️ pageSize ≥ 100 会返回 **403**（2026-08-29 实测:90 通、100 起全挂）。
# 这个 403 极具误导性 —— 看着像被封了 IP 或缺鉴权,实际只是分页参数超限。
# 所以这里硬性封顶 90 并翻页,别把 days 直接当 pageSize 传。
PAGE_MAX = 90


def _page(currency: str, start: str, end: str, page: int) -> tuple[list, int]:
    url = CCPR.format(start=start, end=end, cur=currency, size=PAGE_MAX)
    url = url.replace("pageNum=1", f"pageNum={page}")
    payload = json.loads(_get(url, "https://www.chinamoney.com.cn/chinese/bkccpr/"))
    code = (payload.get("head") or {}).get("rep_code")
    if code != "200":
        raise RuntimeError(f"外汇交易中心返回 rep_code={code}")
    total = int((payload.get("data") or {}).get("total") or 0)
    return (payload.get("records") or []), total


def midprice_series(currency: str, days: int) -> list[tuple[str, float]]:
    """中间价日频序列,新→旧。官方口径,人民银行授权外汇交易中心发布。

    超过 PAGE_MAX 天自动翻页 —— 单页要 100 条以上会被接口以 403 拒绝。
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    s_iso, e_iso = start.isoformat(), end.isoformat()

    records, total = _page(currency, s_iso, e_iso, 1)
    pages = -(-total // PAGE_MAX) if total else 1
    for pg in range(2, pages + 1):
        try:
            more, _ = _page(currency, s_iso, e_iso, pg)
        except Exception:
            break                          # 后续页失败不算致命,用已拿到的
        if not more:
            break
        records.extend(more)

    out = []
    for rec in records:
        vals = rec.get("values") or []
        if not vals or vals[0] in ("", None):
            continue                       # 节假日无报价,跳过而不是填 0
        try:
            out.append((rec["date"], float(vals[0])))
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def spot(pair: str = "usdcny") -> dict | None:
    """新浪即期。中间价是每日 9:15 公布的官方价,即期反映之后的实际走向。"""
    try:
        text = _get(SINA.format(pair=pair), "https://finance.sina.com.cn", "gbk")
    except Exception:
        return None
    if '"' not in text:
        return None
    f = text.split('"')[1].split(",")
    if len(f) < 9:
        return None
    try:
        return {"time": f[0], "open": float(f[1]), "high": float(f[2]),
                "last": float(f[3]), "low": float(f[5]), "prev_close": float(f[7])}
    except ValueError:
        return None


def render(series: list[tuple[str, float]], sp: dict | None,
           currency: str, overseas_share: float | None) -> str:
    if len(series) < 2:
        return "取不到足够的中间价数据。"
    newest_d, newest = series[0]
    oldest_d, oldest = series[-1]
    chg = (newest - oldest) / oldest * 100
    # USD/CNY 下跌 = 人民币升值
    direction = "**人民币升值**" if chg < 0 else "**人民币贬值**"
    vals = [v for _, v in series]

    L = [
        f"# {currency} 汇率与汇兑影响",
        "",
        f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　·　"
        f"中间价 = 中国外汇交易中心（官方）　·　即期 = 新浪",
        "",
        "## 中间价",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 最新（{newest_d}） | **{newest:.4f}** |",
        f"| 区间起点（{oldest_d}） | {oldest:.4f} |",
        f"| 区间变动 | **{chg:+.2f}%** —— {direction} |",
        f"| 区间高 / 低 | {max(vals):.4f} / {min(vals):.4f} |",
        f"| 有效交易日 | {len(series)} 天 |",
        "",
    ]
    if sp:
        L += [
            "## 即期（中间价之后的实际走向）",
            "",
            "| 项 | 值 |",
            "|---|---|",
            f"| 最新 | {sp['last']:.4f}（{sp['time']}） |",
            f"| 日内高 / 低 | {sp['high']:.4f} / {sp['low']:.4f} |",
            f"| 昨收 | {sp['prev_close']:.4f} |",
            "",
        ]
    else:
        L += ["> 即期报价取不到（新浪源），只用中间价。", ""]

    if overseas_share is not None:
        # 方向别搞反:USD/CNY 下跌 = 人民币升值 = 同样的美元收入换回**更少**人民币。
        # 所以出口商的人民币收入与 USD/CNY 同向,不是反向。
        # 验算:6.9236→6.7811(-2.06%),100 美元由 692.36 元变 678.11 元,收入 -2.06%。
        eff = chg * overseas_share / 100
        L += [
            "## 对出口型标的的影响估算",
            "",
            f"境外收入占比 **{overseas_share:.0f}%**，区间汇率变动 {chg:+.2f}%：",
            "",
            f"> 折算成人民币的收入受影响约 **{eff:+.2f}%**"
            f"（{'升值压收入' if chg < 0 else '贬值抬收入'}）"
            f"（仅汇率折算这一项，不含套保、不含定价调整、不含成本端的进口物料对冲）。",
            "",
            "⚠️ 这是**粗估不是测算**。真实影响还取决于：结算币种与账期（远期结汇会把影响推后）、"
            "是否做外汇套保、以美元计价的进口物料能对冲多少、以及公司会不会把汇率变化转嫁到售价。"
            "**要拿准数就去财报里找「汇兑损益」科目和管理层讨论段，别用这个估算下结论。**",
            "",
        ]

    L += [
        "---",
        "",
        "**怎么用**：汇率对出口占比高的公司是直接进利润表的变量。"
        "人民币升值会同时压**收入折算**和**毛利率**——研报里「剔除汇兑影响后毛利率如何」"
        "这类表述就是在剥离这一层。看财报时把汇兑损益单独拎出来，"
        "别把汇率造成的波动当成经营层面的变化。",
        "",
        "**数据来源**：中间价 = `chinamoney.com.cn`（中国外汇交易中心，人民银行授权发布，官方口径）"
        "　·　即期 = `hq.sinajs.cn`（需 Referer）。两者都免费、无需 key。",
    ]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fx", description="人民币汇率与汇兑影响估算")
    p.add_argument("--currency", default="USD/CNY", help="货币对，默认 USD/CNY")
    p.add_argument("--days", type=int, default=90, help="回看天数，默认 90")
    p.add_argument("--overseas-share", type=float, default=None,
                   help="境外收入占比（%%），给了就估算汇兑对营收的影响")
    p.add_argument("--md", help="同时写入 markdown 文件")
    a = p.parse_args(argv)

    try:
        series = midprice_series(a.currency, a.days)
    except Exception as e:
        print(f"取中间价失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    pair = a.currency.replace("/", "").lower()
    text = render(series, spot(pair), a.currency, a.overseas_share)
    print(text)
    if a.md:
        from pathlib import Path
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
