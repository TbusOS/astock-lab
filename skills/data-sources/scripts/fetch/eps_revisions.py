"""研报盈利预测:每篇的 EPS 对应哪一年(读 PDF 表头)、同一张表里那几年的归母净利润、
每家券商相对自己上一次是调高还是调低(比归母净利润，读不出才比每股收益)。

为什么年份要读 PDF(2026-10-30 实测):
    东财研报接口有 predictThisYearEps / NextYear / NextTwoYear,字面是「本年 / 次年 / 后年」,
    但「本年」**不是研报发布那年**。拿 300308、300502 一年内 50 篇研报的 PDF 预测表核:
    2025-09~12 发的 13 篇、2026 年发的 37 篇,「本年」全是 2026 年那一列。
    看起来是按查询那年标的 —— 这是推断，所以不靠它:每篇以 PDF 表头为准，读不出的不用，不猜。
    照字面当发布年用，会把「换了年份」算成「上调 40%」,一个看着正常的假信号。

为什么比归母净利润、不比每股收益(2026-11-02 改):
    每股收益 = 净利润 ÷ 股本。公司送转股以后股本变大，每股收益跟着变小，券商的看法却没变。
    新易盛开源证券两篇:2026 年每股收益 19.47 → 13.86,按每股收益算是「调低 28.8%」;
    同一张表里的归母净利润 193.5 → 193.2 亿，没改(两篇算出来的股本 9.94 亿 → 13.94 亿)。
    所以两篇都读出净利润就比净利润;有一篇读不出才退回比每股收益，并注明「可能受送转影响」。
    净利润和每股收益在同一张 PDF 表里，年份按同一个表头认。

为什么看「相对自己上一次」:
    各家的算法和假设差得多(国金对中际 2026 年 EPS 给 36,别家 18~30),拿不同券商的数比，
    比出来的是各家算法的差别;同一家前后两次比，才是这家改了主意。
    方向(调高 / 调低的家数)是领先信号，水平(中位数)只作参照。

检查:scripts/check_eps_revisions.py(不联网)。
"""

import json
import re
import statistics
import datetime as dt
from pathlib import Path

_YEAR = re.compile(r"^(?:FY)?(20\d{2}|\d{2})[AEＥ]$|^(20[1-3]\d)$")
# 每篇存「年份 + 那三年的归母净利润」。旧的 eps_years.json 只存了年份，不再读(要补读净利润)。
_CACHE = "eps_profit.json"
FLAT_PCT = 1.0           # 前后差不到 1% 算没改(比净利润、比每股收益用同一条线)
WINDOW_DAYS = 180        # 超过半年没更新的券商，不算进「最新」
SHARE_TOL = 0.03         # 同一篇各年「净利润 ÷ 每股收益」(就是股本)差超过 3%,说明净利润读错了
SPLIT_NOTE = "可能受送转影响"

# 净利润那一行的行名。「归属母公司股东净利润」(西南、民生)没有「于」,「归母净利」(太平洋)没有「润」
_PROFIT = re.compile(r"^(?:归母净利润?|归属于?母公司.{0,8}净利润)")
# 以上面开头、但不是净利润本身的行:增长率、同比、正文里的句子(「同比增长 76.8%」)
_NOT_LEVEL = re.compile(r"增长|增速|同比|yoy|%|率", re.I)
# 只写了单位的格子:「百万元」「（百万元）」「人民币百万元」「(RMB」「mn)」。
# 行名被折到下一行时，下一行就是这么半截;不能放宽成「带个元字」—— 下一行的「EPS(摊薄/元)」也带元，
# 放宽了会在净利润那格空着时把每股收益那行当成净利润。
_UNIT_ONLY = r"^[(（]?(?:人民币|RMB)?(?:百万|千万|亿|万|千|mn)?元?(?:人民币)?[)）]?$"
_LABEL_TAIL = re.compile(_UNIT_ONLY, re.I)
# 表头前面写单位的格子:上面那种，加上「单位：百万元」「利润表(百万元)」
_UNIT_CELL = re.compile(r"单位|表[(（]|" + _UNIT_ONLY, re.I)


def _num(t):
    try:
        return float(str(t).replace(",", "").replace("，", ""))
    except ValueError:
        return None


def _year(t):
    m = _YEAR.match(t)
    if not m:
        return None
    y = m.group(1) or m.group(2)
    y = int(y) if len(y) == 4 else 2000 + int(y)
    return y if 2015 <= y <= 2035 else None


def _split(line):
    return [t for t in re.split(r"[\s|]+", line.strip()) if t]


def _tokens(text):
    return [t for line in text.splitlines() for t in _split(line)]


def _close(a, b):
    return b is not None and abs(a - b) <= max(0.006, abs(a) * 0.002)


def eps_year(text, this, nxt):
    """接口「本年」EPS 在 PDF 预测表里是哪一年那列。读不出返回 None。

    认法:找到等于 this 的数，且它后面紧跟的数等于 nxt(证明是 EPS 那一行，
    不是别处碰巧相等);数它在这一行排第几;往前找最近一串 ≥3 个连续年份的表头，
    列数相同就按位置对，表头多出前几列就从右往左对。
    """
    tk = _tokens(text)
    for i, t in enumerate(tk):
        if not _close(this, _num(t)):
            continue
        j = i + 1
        while j < len(tk) and _num(tk[j]) is None:
            j += 1
        if j >= len(tk) or (nxt and not _close(nxt, _num(tk[j]))):
            continue
        k, s = 0, i - 1
        while s >= 0 and _num(tk[s]) is not None:
            k += 1
            s -= 1
        n, e = k + 1, i + 1
        while e < len(tk) and _num(tk[e]) is not None:
            n += 1
            e += 1
        y = _header_year(tk, s, k, n)
        if y is not None:
            return y
    return None


def _header_at(tk, s):
    """从第 s 格往前找最近一串 ≥3 个连续年份的表头，返回 (年份列表, 表头第一格的位置);找不到 None。

    数据里碰巧有个像年份的数(群益的附表，所得税那行有个 2020):不带 A/E、凑不成一串的纯数字，
    跳过接着往前找。带 A/E 却凑不成一串(如「盈利预测调整」表三段年份重复),认不出就不认。
    """
    h = s
    while h >= 0:
        if _year(tk[h]) is None:
            h -= 1
            continue
        end = h
        while h >= 0 and _year(tk[h]) is not None:
            h -= 1
        run = tk[h + 1:end + 1]
        hdr = [_year(x) for x in run]
        if len(hdr) >= 3 and all(b - a == 1 for a, b in zip(hdr, hdr[1:])):
            return hdr, h + 1
        if not all(x.isdigit() for x in run):
            return None
    return None


def _header_year(tk, s, k, n):
    """一行共 n 个数，第 k 个对应表头哪一年。列数相同按位置对，表头多出前几列就从右往左对。"""
    found = _header_at(tk, s)
    if found is None:
        return None
    hdr = found[0]
    if len(hdr) == n:
        return hdr[k]
    if len(hdr) > n:
        return hdr[len(hdr) - n + k]
    return None


def _unit(s):
    """单位文字 → 换成亿元要除以几。认不出返回 None。"""
    if re.search(r"百万|\bmn\b|million", s, re.I):
        return 100.0
    if "千万" in s:
        return 10.0
    if "亿" in s:
        return 1.0
    if "千元" in s:
        return 1e5
    if "万" in s:
        return 1e4
    return None


def _profit_row(tk, p, line_end):
    """净利润行名从第 p 格开始(行名那行到 line_end 为止)。返回这一行数字的 (起, 止) 位置。"""
    j, tail = p + 1, 0
    while j < len(tk) and _num(tk[j]) is None:
        if j >= line_end:              # 过了行名那一行，只许跳两格「（百万元）」这种半截
            if tail == 2 or not _LABEL_TAIL.search(tk[j]):
                break
            tail += 1
        j += 1
    e = j
    while e < len(tk) and _num(tk[e]) is not None:
        e += 1
    return j, e


def profit_year(text, year):
    """研报 PDF 文本里 year 那一年的归母净利润，换成亿元。读不出返回 None,不猜。

    认法:行首是「归母净利润」或「归属(于)母公司…净利润」、且不是增长率 / 正文句子那种行;
    取它后面连着的一串数，按和每股收益同样的办法找表头、对年份。
    单位先看行名(「(百万元)」),没写就看表头前面几格(「单位：百万元」「利润表(百万元)」);
    都没写就不用 —— 差 100 倍的数比没有更糟。一篇里有几张表都写了净利润的，用第一张认得出的。
    """
    lines = text.splitlines()
    tk, start = [], []
    for line in lines:
        start.append(len(tk))
        tk += _split(line)
    start.append(len(tk))
    for i, line in enumerate(lines):
        s = line.strip()
        if not _PROFIT.match(s) or _NOT_LEVEL.search(s):
            continue
        j, e = _profit_row(tk, start[i], start[i + 1])
        found = _header_at(tk, j - 1) if e > j else None
        if found is None or len(found[0]) < e - j:
            continue
        hdr, hdr_at = found
        first = hdr[len(hdr) - (e - j)]        # 这一行第一个数是哪年(表头多出的列在左边)
        if not 0 <= year - first < e - j:
            continue
        div = _unit(" ".join(tk[start[i]:j])) or _unit(
            " ".join(t for t in tk[max(0, hdr_at - 12):hdr_at] if _UNIT_CELL.search(t)))
        if div is None:
            continue
        return _num(tk[j + year - first]) / div
    return None


def _same_shares(eps, profit):
    """同一篇各年「归母净利润 ÷ 每股收益」就是股本，应该差不多。差太多说明净利润那行读错了。"""
    r = [p / e for p, e in zip(profit, eps) if p and e]
    if any(x <= 0 for x in r):
        return False
    return len(r) < 2 or max(r) / min(r) - 1 <= SHARE_TOL


def _read_pdf(eps, info, fetch_pdf, pdf_text):
    """下载一篇、读年份和三年的净利润。

    下载或抽字抛异常 → 返回 None:这是一时的网络 / 文件问题,调用方不写缓存,下次再试。
    读到了字但认不出年份 → {"fy": None, ...}:换个时间读也一样,写缓存,不再重复下载。
    (2026-11-02 前两种都写进缓存,一次超时就让那篇研报永远不算数。)"""
    try:
        text = pdf_text(fetch_pdf(info))
    except Exception:
        return None
    try:
        fy = eps_year(text, eps[0], eps[1] if len(eps) > 1 else None)
    except Exception:
        return {"fy": None, "profit": []}
    try:                               # 净利润读出错只丢净利润，年份照用(退回比每股收益)
        profit = [profit_year(text, fy + k) for k in range(len(eps))] if fy else []
    except Exception:
        profit = []
    if not _same_shares(eps, profit):
        profit = []
    return {"fy": fy, "profit": profit}


def resolve(rows, cache_dir: Path, fetch_pdf, pdf_text):
    """给每篇有 EPS 的研报填 fy(「本年」那格是哪一年)和 profit(fy、fy+1、fy+2 的归母净利润，亿元)。

    返回新的 rows,不改传进来的。读过的存在 cache_dir/eps_profit.json;下载 / 抽字失败的那篇
    这次 fy 记 None、不写缓存(下次重试)。fy 是 None 的研报不进上下调统计,调用方要自己数出来报。
    fetch_pdf(info) → bytes;pdf_text(bytes) → str。
    """
    f = Path(cache_dir) / _CACHE
    try:
        cache = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out = []
    for r in rows:
        eps = r.get("eps") or [None]
        if not eps[0]:
            out.append({**r, "fy": None, "profit": [None] * len(eps)})
            continue
        got = cache.get(r["info"])
        if not isinstance(got, dict):
            got = _read_pdf(eps, r["info"], fetch_pdf, pdf_text)
            if got is None:                        # 一时取不到:这次不算,也不记进缓存
                got = {"fy": None, "profit": []}
            else:
                cache = {**cache, r["info"]: got}
        profit = list(got.get("profit") or [])
        out.append({**r, "fy": got.get("fy"),
                    "profit": (profit + [None] * len(eps))[:len(eps)]})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


def classify_change(old, new):
    """同一家券商前后两篇对同一年的预测：调高、调低还是没改。

    old / new = {"eps": 每股收益, "profit": 归母净利润(亿元)},读不出的是 None。
    两边都有净利润就比净利润;否则比每股收益，并注明可能受送转影响。差不到 1% 算持平。
    分母取绝对值:亏损从 -10 亿收窄到 -5 亿是调高 50%,不是「-50%」。
    """
    po, pn = old.get("profit"), new.get("profit")
    if po and pn is not None:
        basis, a, b, note = "net_profit", po, pn, ""
    else:
        a, b = old.get("eps"), new.get("eps")
        if not a or b is None:
            raise ValueError(f"两篇既没都读出净利润，也没都给每股收益，比不了:{old} → {new}")
        basis, note = "eps", SPLIT_NOTE
    pct = (b - a) / abs(a) * 100
    label = "持平" if abs(pct) < FLAT_PCT else "调高" if pct > 0 else "调低"
    return {"basis": basis, "pct": pct, "label": label, "note": note}


def _series(rows):
    """{(券商, 年份): [{"date", "eps", "profit"}]},按日期排好。一篇研报的三格分别落到 fy、fy+1、fy+2。"""
    out = {}
    for r in rows:
        if r.get("fy") is None:
            continue
        profit = list(r.get("profit") or [])
        for k, v in enumerate(r.get("eps") or []):
            if v:
                p = profit[k] if k < len(profit) else None
                out.setdefault((r["org"], r["fy"] + k), []).append(
                    {"date": r["date"], "eps": v, "profit": p})
    return {key: sorted(v, key=lambda x: x["date"]) for key, v in out.items()}


def summarize(rows, fy, today, window=WINDOW_DAYS):
    """某一年的盈利预测：各家最新值、中位数、近 30 / 90 天调高调低几家、每次调整明细。

    latest = {券商: {"date", "eps", "profit"}}(半年内最新一篇);
    median = 每股收益中位数(⚠ 送转前后的数混在一起，只作参照);
    median_profit = 归母净利润中位数(只算读出净利润的那几家);
    revisions = [{"date", "org", "old", "new", "basis", "pct", "label", "note"}],新的在前。
    """
    t = dt.date.fromisoformat(today)
    age = lambda d: (t - dt.date.fromisoformat(d)).days
    latest, revisions = {}, []
    for (org, y), pts in _series(rows).items():
        if y != fy:
            continue
        if age(pts[-1]["date"]) <= window:
            latest[org] = pts[-1]
        revisions += [{"date": b["date"], "org": org, "old": a, "new": b, **classify_change(a, b)}
                      for a, b in zip(pts, pts[1:])]
    revisions.sort(key=lambda r: (r["date"], r["org"]), reverse=True)
    cnt = lambda days, label: sum(1 for r in revisions
                                  if age(r["date"]) <= days and r["label"] == label)
    eps = [p["eps"] for p in latest.values()]
    prof = [p["profit"] for p in latest.values() if p["profit"] is not None]
    return {"fy": fy, "latest": latest,
            "median": statistics.median(eps) if eps else None,
            "median_profit": statistics.median(prof) if prof else None,
            "up30": cnt(30, "调高"), "down30": cnt(30, "调低"), "up90": cnt(90, "调高"),
            "down90": cnt(90, "调低"), "flat90": cnt(90, "持平"), "revisions": revisions}


def _latest_lines(s):
    lat = s["latest"]
    if not lat:
        return ["- 半年内没有新的预测"]
    out = []
    withp = sorted((x for x in lat.items() if x[1]["profit"] is not None),
                   key=lambda x: x[1]["profit"])
    only_eps = sorted((x for x in lat.items() if x[1]["profit"] is None), key=lambda x: x[1]["eps"])
    if withp:
        out.append(f"- 半年内各家最新的归母净利润预测，中位数 **{s['median_profit']:.1f} 亿元**"
                   f"({len(withp)} 家:" + "、".join(f"{o} {p['profit']:.1f}" for o, p in withp) + ")")
    if only_eps:
        out.append(f"- 读不出净利润、只有每股收益的 {len(only_eps)} 家:"
                   + "、".join(f"{o} {p['eps']:.2f} 元" for o, p in only_eps)
                   + "(每股收益受送转影响，不放进上面的中位数)")
    return out


def _revision_row(r):
    if r["basis"] == "net_profit":
        what, v0, v1 = "归母净利润(亿元)", f"{r['old']['profit']:.1f}", f"{r['new']['profit']:.1f}"
    else:
        what, v0, v1 = "每股收益(元)", f"{r['old']['eps']:.2f}", f"{r['new']['eps']:.2f}"
    tail = f"{r['label']} {r['pct']:+.1f}%" + (f"({r['note']})" if r["note"] else "")
    return f"| {r['date']} | {r['org']} | {what} | {v0} | {v1} | {tail} |"


def lines(rows, today, years):
    """研报报告里那一节。"""
    have = [r for r in rows if (r.get("eps") or [None])[0]]
    got = [r for r in have if r.get("fy") is not None]
    prof = [r for r in got if any(p is not None for p in r.get("profit") or [])]
    L = ["## 二　盈利预测怎么变(各家券商相对自己上一次)", "",
         f"带盈利预测的研报 {len(have)} 篇，从 PDF 预测表读出年份的 {len(got)} 篇"
         + (f",读不出的 {len(have) - len(got)} 篇不用(不猜年份)" if len(have) > len(got) else "")
         + f";读出年份的里面，读出归母净利润的 {len(prof)} 篇。", ""]
    for fy in years:
        s = summarize(rows, fy, today)
        if not s["latest"] and not s["revisions"]:
            continue
        L += [f"### {fy} 年", "",
              f"- 近 30 天:调高 **{s['up30']}** 家次、调低 **{s['down30']}** 家次;"
              f"近 90 天:调高 {s['up90']}、调低 {s['down90']}、没改 {s['flat90']}",
              *_latest_lines(s), ""]
        if s["revisions"]:
            L += ["| 日期 | 券商 | 比的是 | 上次 | 这次 | 变化 |", "|---|---|---|---|---|---|",
                  *[_revision_row(r) for r in s["revisions"][:10]], ""]
    L += ["> 看方向不看水平:各家的算法和假设差得多,不同券商的数不能直接比;同一家调高还是调低才是信号。"
          "比的是归母净利润:送转股以后股本变大、每股收益变小，赚多少钱的看法却没变;"
          "两篇里有一篇读不出净利润，才比每股收益并标「可能受送转影响」。"
          "年份以研报 PDF 预测表的表头为准(东财接口的「本年」不是发布年,2026-10-30 核过)。", ""]
    return L
