#!/usr/bin/env python3
"""领先层 —— 上游和同一条链上的人,现在怎么说、在花多少钱。

    guidance.py <code> [--raw data/raw]

## 这一层为什么必须有

我们前面三节推的全是**已经发生的事**:上季营收、上季存货、上季净利。
财报最快也要滞后一个多月,而且它只告诉你上一季是什么样。
**领先层回答的是「下一季大概率往哪走」** —— 靠三类东西:

| 类型 | 例子 | 领先多久 | 为什么可信 |
|---|---|---|---|
| 当事人自己的计划 | 云厂电话会上的 capex 指引 | 1~2 季 | 那是**掏钱的人自己说的下期预算**,不是旁观者的预测 |
| 同链上的月度数 | 台湾光通信月营收 | 1~2 月 | 同一条链上的公司,月频披露,比季报早两个月 |
| 上游已经花掉的钱 | 上游季度资本开支同比 | 1~2 季 | 已经付出去的现金,是行为不是说法 |

## ★ 云厂指引不是「别人的预测」

本仓有一条硬规矩:**不拿任何机构的盈利预测当输入**。云厂指引看着像预测,
但它和分析师预测是两回事 —— 分析师预测的是**别人**会怎么样,
云厂说的是**它自己**准备花多少钱,而且是在电话会上对着投资者说的,
说了要负责。这属于「公司自己说的」那一类,和财报同源,允许进概率修正。

## 每个指标都要给方向标记,规则写死在代码里

只给一堆数字等于没给判断。所以每个指标输出 ↑ / → / ↓,
**规则写在下面的常量里,并且原样印进报告** —— 读者能自己核对我们有没有照做。

⚠ **拿不到数据的指标不给方向。** 不是给 →(那等于说「持平」,是个判断),
是整条不计入,并在报告里写明为什么没有。缺数据是我们的工作缺口,
不是「上游没动静」。

依赖:无(只读 fetch_all 落下来的 json / txt)。
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import re
import statistics
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

import forecast

# ── 方向规则(写死,并且原样印进报告)────────────────────────────────
DIRECTION_RULES = {
    "cloud_guidance": "指引**上调**、或**区间收窄但上沿不动**(下沿被抬上来)= ↑;"
                      "**下调** = ↓;金额没变 = →;"
                      "**只有一版指引、没有可比的上一版** = →(不是「持平」,是「比不了」,"
                      "报告里会写明是哪一种)",
    "taiwan_optics": "当月营收同比的**中位数 > 30% 且当月环比中位 > 0** = ↑;"
                     "同比中位 < 0 = ↓;其余 = →",
    "upstream_capex": "上游**最近一个可比季度**的资本开支合计同比 > 10% = ↑;< 0 = ↓;"
                      "0~10% = →",
    "oil_price": "**不给方向。** 数据源只有一个现价字符串,没有历史序列 —— "
                 "没有位置就没有方向,给一个箭头等于编。只展示,不进概率。",
}

# 云厂 = forecast.UPSTREAM["cloud"] 的成员,**不在这里另写一份**。
# 另写一份的下场:改了上游注册表,这里还在按旧名单抽句,而且不报错。
CLOUD_TICKERS = forecast.UPSTREAM["cloud"]["members"]

# 抽指引句的正则。前两条抄自 `data-sources/scripts/fetch/capex.py:_guide_pat`
# (那边从 8-K 新闻稿抽,这边从电话会纪要抽,句式是一样的);
# 第三条是这边补的 —— 纪要开头的要点清单写成
# 「FY 2027 Capital Expenditure Outlook -- Estimated at $175 billion」,
# 触发词在 capex 后面而不是前面,前两条都抓不到,而那恰恰是 MSFT 唯一给出金额的地方。
GUIDE_PAT = re.compile(
    r"[^.]{0,220}\b(?:anticipate|expect|outlook|guidance|plan to invest|"
    r"we (?:will|intend))\b[^.]{0,120}\b(?:capital expenditure|capex|"
    r"capital spend)\w*[^.]{0,260}\."
    r"|[^.]{0,180}\b(?:capital expenditure|capex)\w*[^.]{0,80}"
    r"\b(?:to be in the range|will be|expected to)\b[^.]{0,220}\."
    r"|[^.]{0,200}\b(?:capital expenditure|capex|capital spend)\w*[^.]{0,80}"
    r"\b(?:outlook|estimated|estimate|guidance)\b[^.]{0,200}"
    r"\$\s?[\d.,]+\s*(?:billion|million|trillion)[^.]{0,140}\.",
    re.I)

AMOUNT_PAT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|trillion)", re.I)
PERIOD_PAT = re.compile(r"\b(?:FY\s*'?\s*(\d{2,4})|(?:calendar\s+)?(20\d{2}))\b", re.I)
# 「从 X 改成 Y」的句式:标记词之后的金额是**上一版**
REVISE_PAT = re.compile(
    r"\b(narrowed from|up from|raised from|increased from|lowered from|"
    r"reduced from|down from|(?:from our )?prior (?:outlook|guidance|range) of|"
    r"an? (?:increase|decrease|reduction) from (?:the |our )?previous|"
    r"(?:up|down) from (?:the |our )?previous|"
    r"compared (?:to|with) (?:our )?prior)\b", re.I)
GROW_PAT = re.compile(r"\b(grow year-over-year|grow year over year|increase\w*"
                      r"(?: sequentially)?|higher than|step up|ramp)\b", re.I)
SHRINK_PAT = re.compile(r"\b(decline\w*|decrease\w*|lower than|moderate\w*)\b", re.I)
MULT = {"trillion": 1e12, "billion": 1e9, "million": 1e6}


def newest(raw: Path, pat: str):
    fs = sorted(glob.glob(str(raw / pat)))
    return Path(fs[-1]) if fs else None


def yi_usd(v: float) -> str:
    """美元 → 「X 亿美元」。原句里写的是 billion,中文读者按亿看更顺。"""
    return f"{v / 1e8:,.0f} 亿美元"


def _amounts(text: str):
    return [float(m.group(1).replace(",", "")) * MULT[m.group(2).lower()]
            for m in AMOUNT_PAT.finditer(text)]


def _period(text: str) -> str | None:
    m = PERIOD_PAT.search(text)
    if not m:
        return None
    if m.group(1):
        y = m.group(1)
        return f"FY20{y}" if len(y) == 2 else f"FY{y}"
    return m.group(2)


def parse_guidance_sentence(sent: str) -> dict:
    """一句指引 → {原句, 新金额[], 旧金额[], 期间}。

    「旧金额」怎么认:句子里出现 `narrowed from` / `up from` / `prior outlook of`
    这类标记词时,**标记词之后的金额是上一版**,之前的是这一版。
    没有标记词就只有一版。
    """
    m = REVISE_PAT.search(sent)
    if m:
        new = _amounts(sent[:m.start()])
        old = _amounts(sent[m.end():])
    else:
        new, old = _amounts(sent), []
    return {"原句": sent, "新": new, "旧": old, "期间": _period(sent)}


def guidance_direction(items: list[dict]):
    """一家云厂的所有指引句 → (↑/→/↓, 一句话理由, 拍板的那一句)。

    **拍板那一句必须一起返回**:一家公司一份纪要里能抽到好几句带 capex 的话
    (开头的要点清单一句、CFO 正文里一句、问答里一句),方向是其中某一句定的。
    表里如果展示的是另一句,读者看到的原话和结论对不上,而两边看着都合理。

    规则见 DIRECTION_RULES。

    ⚠ **只有一版指引时给 →,不给 ↑**,哪怕公司说了「会同比增长」。
    「同比增长」和「比我上次说的多」是两个问题:capex 连年增长是这几年的常态,
    它不构成新信息;能改变概率的是**这一版和上一版比,方向变了没有**。
    把「会增长」当 ↑,等于每季度都白送一个 ↑。
    """
    if not items:
        return "", "没抽到指引句", None
    # 同一期间出现两句、后一句带 now/we now believe:后者是修订版
    same = [x for x in items if x["新"]]
    if not same:
        return "→", "抽到了指引句但里面没有金额,比不出方向", items[0]
    # ① 句内修订(narrowed from / up from …)
    for x in same:
        if x["旧"]:
            nl, nh = min(x["新"]), max(x["新"])
            ol, oh = min(x["旧"]), max(x["旧"])
            if nh > oh or (nh == oh and nl > ol):
                # 两种「往上」要分开说:区间收窄(上沿不动、下沿被抬)和整体上调
                # 不是一回事 —— 前者是「坏情况没那么坏了」,后者是「好情况更好了」。
                if nh == oh:
                    return "↑", (f"区间收窄且往上:上沿 {yi_usd(oh)} 不动、"
                                 f"下沿从 {yi_usd(ol)} 抬到 {yi_usd(nl)}"), x
                if nl == nh and ol == oh:
                    return "↑", f"金额上调:{yi_usd(oh)} → {yi_usd(nh)}", x
                return "↑", (f"区间上移:{yi_usd(ol)}~{yi_usd(oh)} → "
                             f"{yi_usd(nl)}~{yi_usd(nh)}"), x
            if nh < oh or (nh == oh and nl < ol):
                return "↓", (f"区间下移(上一版 {yi_usd(ol)}~{yi_usd(oh)} → "
                             f"这一版 {yi_usd(nl)}~{yi_usd(nh)})"), x
            return "→", "指引区间和上一版一样", x
    # ② 跨句修订:同一期间给了两个不同的金额,后出现的那句是新版
    by_period = {}
    for x in same:
        by_period.setdefault(x["期间"], []).append(x)
    for per, xs in by_period.items():
        if len(xs) >= 2:
            first, last = xs[0], xs[-1]
            a, b = max(first["新"]), max(last["新"])
            if b > a:
                return "↑", f"{per} 的指引从 {yi_usd(a)} 上调到 {yi_usd(b)}", last
            if b < a:
                return "↓", f"{per} 的指引从 {yi_usd(a)} 下调到 {yi_usd(b)}", last
            return "→", f"{per} 的指引没变", last
    # ③ 只有一版
    return "→", ("只有这一版指引,**没有可比的上一版**,比不出方向 —— "
                 "记 → 不是「持平」的意思,是「这次没有可比的东西」"), same[0]


def cloud_guidance(raw: Path) -> dict:
    """从电话会纪要抽云厂 capex 指引。**只抽 UPSTREAM['cloud'] 那四家。**

    ⚠ 纪要目录里还有别的公司(比如 Coherent)。那是**同业**不是上游 ——
    它自己的 capex 决定不了光模块的需求,拿它当领先指标是把同行的开支
    当成客户的开支。同业对照在报告第 8 节,两件事别混。
    """
    out = {"rows": [], "缺": [], "规则": DIRECTION_RULES["cloud_guidance"]}
    dirs = Path(raw / "transcripts")
    if not dirs.is_dir():
        out["缺"] = list(CLOUD_TICKERS)
        out["why"] = "`data/raw/transcripts/` 不存在,一份纪要都没抓"
        return out
    for tk in CLOUD_TICKERS:
        # ⚠ **取期次最新的那一份,不是目录里排第一的。** 目录名形如
        #   `alphabet-googl-q1-2026`,按字母序排 q1 在 q2 前面 ——
        #   一旦同时存在两个季度的纪要,`next(...)` 会静默拿到旧的那份,
        #   而旧指引和新指引很可能方向相反,报告不会有任何提示。
        cands = [x for x in dirs.iterdir()
                 if x.is_dir() and f"-{tk.lower()}-" in x.name]
        d = max(cands, key=_period_key, default=None)
        if d is None:
            out["缺"].append(f"{tk}(纪要目录里没有这家)")
            continue
        txts = sorted(d.glob("*.txt"))
        meta = {}
        for jf in sorted(d.glob("*.json")):
            for env in json.loads(jf.read_text(encoding="utf-8")).values():
                meta = {"url": env.get("url", ""), "抓于": env.get("fetched_at", "")[:10],
                        "ok": env.get("ok")}
        body = txts[-1].read_text(encoding="utf-8") if txts else ""
        if not body.strip():
            out["缺"].append(f"{tk}(纪要文件是空的,{meta.get('url', '')} 抓下来 0 字符)")
            continue
        body = html.unescape(body)
        items, seen = [], set()
        for m in GUIDE_PAT.finditer(body):
            h = " ".join(m.group(0).split()).strip()
            if len(h) < 60 or h in seen or "we define" in h.lower():
                continue
            seen.add(h)
            items.append(parse_guidance_sentence(h))
        if not items:
            out["缺"].append(f"{tk}(纪要抓到了 {len(body):,} 字符,但没有一句命中指引句式)")
            continue
        mark, why, deciding = guidance_direction(items)
        out["rows"].append({"ticker": tk, "会议": d.name, "方向": mark, "理由": why,
                            "句子": items, "拍板句": deciding or items[0],
                            "url": meta.get("url", ""),
                            "抓于": meta.get("抓于", "")})
    return out


def _fmt_period(name: str) -> str:
    m = re.search(r"-q([1-4])-(\d{4})$", name)
    return f"{m.group(2)}Q{m.group(1)}" if m else name


def _period_key(d: Path):
    """从纪要目录名里解出 (年, 季),用来挑最新的一份。解不出的排最后。"""
    m = re.search(r"-q([1-4])-(\d{4})$", d.name)
    return (int(m.group(2)), int(m.group(1))) if m else (0, 0)


def taiwan_optics(raw: Path) -> dict:
    """台湾光通信链的月营收。**月频**,比 A 股季报早两个月看到拐点。

    为什么看它:光模块这条链上,台湾厂商(智邦 / 光圣 / 联钧 / 众达…)和
    A 股厂商卖给同一批客户。台湾上市公司**每月 10 日前公告上月营收**,
    而 A 股一个季度才说一次话。同一条链上的月度数,是我们能拿到的最快的量。

    ⚠ **它不是这几只票自己的营收。** 它回答的是「这条链上个月忙不忙」,
    不回答「中际旭创上个月卖了多少」。份额变化在第 4 节看,不在这儿。
    """
    f = newest(raw, "industry/*-台湾光通信月营收.json")
    if not f:
        return {"ok": False, "why": "`data/raw/industry/` 下没有台湾光通信月营收文件。"
                                    "重跑:`tools/fetch_all.py --macro`(industry 组)"}
    for env in json.loads(f.read_text(encoding="utf-8")).values():
        if not env.get("ok") or not env.get("data"):
            continue
        rows = []
        for r in env["data"]:
            yoy = r.get("_同比%")
            try:
                mom = float(r.get("營業收入-上月比較增減(%)"))
            except (TypeError, ValueError):
                mom = None
            if yoy is None:
                continue
            rows.append({"公司": r.get("公司名稱"), "代號": r.get("公司代號"),
                         "为什么看它": (r.get("_我们为什么看它") or "").split("—— ")[-1],
                         "同比%": float(yoy), "环比%": mom,
                         "资料年月": r.get("資料年月")})
        if not rows:
            continue
        med_yoy = statistics.median(x["同比%"] for x in rows)
        moms = [x["环比%"] for x in rows if x["环比%"] is not None]
        med_mom = statistics.median(moms) if moms else None
        if med_yoy > 30 and med_mom is not None and med_mom > 0:
            mark, why = "↑", (f"同比中位 {med_yoy:.1f}% > 30%,"
                              f"而且当月环比中位 {med_mom:+.1f}% > 0(还在往上)")
        elif med_yoy < 0:
            mark, why = "↓", f"同比中位 {med_yoy:.1f}% < 0,这条链在缩"
        else:
            mark, why = "→", (f"同比中位 {med_yoy:.1f}%"
                              + (f"、环比中位 {med_mom:+.1f}%" if med_mom is not None else "")
                              + " —— 没到 ↑ 的两个条件都满足,也没到 ↓")
        return {"ok": True, "rows": rows, "同比中位": med_yoy, "环比中位": med_mom,
                "方向": mark, "理由": why, "期间": rows[0]["资料年月"],
                "n": len(rows), "url": env.get("url", ""),
                "source": env.get("source", ""), "抓于": env.get("fetched_at", "")[:10],
                "规则": DIRECTION_RULES["taiwan_optics"]}
    return {"ok": False, "why": f"{f.name} 里没有成功的信封"}


SINA_HF = re.compile(r'hq_str_(hf_\w+)="([^"]*)"')


def oil_price(raw: Path) -> dict:
    """油价现价。**只展示,不给方向,不进概率。**

    数据源是新浪外盘期货的一行字符串,里面只有当天那一个报价 ——
    **没有历史序列就没有位置**。「现在 92 美元」是高是低,取决于过去两年在什么区间,
    而那个区间我们没有。给一个 ↑/↓ 等于编一个判断出来。
    """
    f = newest(raw, "macro/*-油价.json")
    if not f:
        return {"ok": False, "why": "`data/raw/macro/` 下没有油价文件"}
    for env in json.loads(f.read_text(encoding="utf-8")).values():
        d = env.get("data")
        if not env.get("ok") or not isinstance(d, str):
            continue
        rows = []
        for key, body in SINA_HF.findall(d):
            parts = body.split(",")
            if len(parts) < 14:
                continue
            try:
                px = float(parts[0])
            except ValueError:
                continue
            rows.append({"代码": key, "名称": parts[13], "现价": px, "日期": parts[12]})
        if rows:
            return {"ok": True, "rows": rows, "url": env.get("url", ""),
                    "source": env.get("source", ""),
                    "抓于": env.get("fetched_at", "")[:10],
                    "规则": DIRECTION_RULES["oil_price"]}
    return {"ok": False, "why": f"{f.name} 里没有能解析出报价的信封"}


def upstream_capex(raw: Path, code: str) -> dict:
    """上游最近一个可比季度的资本开支合计同比。

    **这是「已经花出去的钱」**,和指引(计划)、月营收(同链的量)互相独立 ——
    三者出错的原因不一样,所以同向才算互相印证。

    ⚠ 只在**成员全都有数**的季度才算同比。少一家就少几百亿,
    同比会凭空多出几十个百分点,而且不报错。这条规矩在 `forecast.capex_yoy`
    里,这里不另写一份。
    """
    key = forecast.UPSTREAM_OF.get(code)
    if not key:
        return {"ok": False, "why": f"这只票({code})的上游还没登记,"
                                    f"见 `forecast.py` 的 `UPSTREAM_OF`"}
    g = forecast.UPSTREAM[key]
    if g["kind"] == "sec":
        cy, missing = forecast.capex_yoy(raw, key), []
    else:
        cy, missing = forecast.capex_yoy_ashare(raw, g["members"])
    if missing:
        return {"ok": False, "why": f"{g['名称']}里 {'、'.join(missing)} 的三大表没抓到 —— "
                                    f"**少一家就整组不算**,篮子少一家前后两年就不是"
                                    f"同一个篮子。重跑:`tools/fetch_all.py --codes "
                                    f"{','.join(missing)} --group statements`"}
    if not cy:
        return {"ok": False, "why": f"{g['名称']}凑不出任何「成员全都有数」的同比季度"}
    last = max(cy)
    v = cy[last]
    if v > 10:
        mark, why = "↑", f"最近可比季 {last} 同比 {v:+.1f}% > 10%,上游还在加大投入"
    elif v < 0:
        mark, why = "↓", f"最近可比季 {last} 同比 {v:+.1f}% < 0,上游在收缩"
    else:
        mark, why = "→", f"最近可比季 {last} 同比 {v:+.1f}%,在 0~10% 之间,基本持平"
    tail = sorted(cy)[-6:]
    return {"ok": True, "组": key, "名称": g["名称"], "成员": g["members"],
            "最近季": last, "最近同比": v, "方向": mark, "理由": why,
            "近几季": [(d, round(cy[d], 1)) for d in tail],
            "规则": DIRECTION_RULES["upstream_capex"]}


def cloud_merged(cg: dict) -> dict | None:
    """四家云厂的 capex 指引 → **一个**箭头。四家里 ↑ 多于 ↓ 记 ↑,反之 ↓,持平 →。

    ⚠ 2026-09-02 改。原来是**四家各一票**,合起来能挪 20 个百分点的概率
    (削到上限 15)。四家云厂的 capex 指引相关性接近 1 —— 同一轮 AI 投资周期、
    同一批供应商、同一份宏观预算。**当四票投,等于一件事投四次。**
    独立信号才能相加,不独立的信号相加只是把同一个赌注写成四行。

    它在不适用时会怎样:一家都没抽到指引句时返回 None(整条不计入,
    不当成「持平」——「缺数据」不等于「上游没动静」);
    四家打平(比如 2 ↑ 2 ↓)时给 →,并把两边的家数写在理由里,
    读者能看出这个 → 是「打平」而不是「都没动」。
    """
    rows = cg.get("rows") or []
    if not rows:
        return None
    ups = [r["ticker"] for r in rows if r["方向"] == "↑"]
    downs = [r["ticker"] for r in rows if r["方向"] == "↓"]
    flats = [r["ticker"] for r in rows if r["方向"] == "→"]
    mark = "↑" if len(ups) > len(downs) else "↓" if len(downs) > len(ups) else "→"
    detail = (f"{len(ups)} 家上调({'/'.join(ups) or '—'})、"
              f"{len(downs)} 家下调({'/'.join(downs) or '—'})、"
              f"{len(flats)} 家没变({'/'.join(flats) or '—'})")
    return {"来自": f"云厂 capex 指引({len(rows)} 家合成 1 票)",
            "方向": mark,
            "理由": (f"{detail} → 取 **{mark}**。"
                     f"**四家合成一票**:它们的 capex 指引相关性接近 1"
                     f"(同一轮投资周期、同一批供应商、同一份宏观预算),"
                     f"当四票投等于一件事投四次。"),
            "成员": [{"ticker": r["ticker"], "方向": r["方向"], "理由": r["理由"]}
                     for r in rows]}


# ── 汇总:给决策层用 ─────────────────────────────────────────────────
def build(raw: Path, code: str, which: list[str]) -> dict:
    """按行业要跑的那几项算,返回 {项目: 结果} + 一个 `方向` 列表给概率层。

    `which` 来自 `sector_rules.SECTOR_RULES[行业]["领先指标"]` ——
    **不同行业跑不同的指标**,油价对光模块没有含义,云厂指引对油服没有含义。
    """
    out = {"项目": {}, "方向": [], "跳过": []}
    fns = {"cloud_guidance": lambda: cloud_guidance(raw),
           "taiwan_optics": lambda: taiwan_optics(raw),
           "oil_price": lambda: oil_price(raw),
           "upstream_capex": lambda: upstream_capex(raw, code)}
    for k in which:
        if k not in fns:
            continue
        r = fns[k]()
        out["项目"][k] = r
        if k == "cloud_guidance":
            m = cloud_merged(r)
            r["合成"] = m
            if m:
                out["方向"].append(m)
            for miss in r.get("缺", []):
                out["跳过"].append(f"云厂指引 · {miss}")
        elif k == "oil_price":
            continue                      # 只展示,按设计不给方向
        elif r.get("ok"):
            label = {"taiwan_optics": "台湾光通信月营收",
                     "upstream_capex": r.get("名称", "上游资本开支")}[k]
            out["方向"].append({"来自": label, "方向": r["方向"], "理由": r["理由"]})
        else:
            out["跳过"].append(f"{k} · {r.get('why', '')}")
    ups = sum(1 for x in out["方向"] if x["方向"] == "↑")
    downs = sum(1 for x in out["方向"] if x["方向"] == "↓")
    out["↑数"], out["↓数"] = ups, downs
    out["净方向"] = ups - downs
    return out


# ── 渲染:报告第 4.5 节 ───────────────────────────────────────────────
def render(g: dict, n: str, L: list) -> None:
    L.append(f"## {n} 上游怎么说(领先层)")
    L.append("")
    L.append("前面几节推的全是**已经发生的事**。这一节回答的是**下一季大概率往哪走** —— "
             "靠三类东西:掏钱的人自己说的下期预算、同一条链上的月度数、"
             "上游已经花出去的现金。")
    L.append("")
    L.append("> **为什么云厂指引不算「别人的预测」**:这份报告不拿任何机构的盈利预测"
             "当输入。云厂指引看着像预测,但分析师预测的是**别人**会怎么样,"
             "云厂说的是**它自己**准备花多少钱 —— 在电话会上对着投资者说的,说了要负责。"
             "它和财报同源,属于「公司自己说的」那一类。")
    L.append("")
    if not g["项目"]:
        L.append("**这个行业没有登记领先指标**,这一节没有内容。"
                 "不是拿不到数据,是我们还没想清楚什么数据领先它 1~2 个季度。")
        L.append("")
        return

    cg = g["项目"].get("cloud_guidance")
    if cg is not None:
        L.append("### 云厂下期资本开支指引(当事人自己的计划)")
        L.append("")
        if cg["rows"]:
            L.append("| 云厂 | 方向 | 期间 | 这一版金额 | 上一版 | 原话 | 纪要 |")
            L.append("|---|:-:|---|---:|---:|---|---|")
            for r in cg["rows"]:
                best = r["拍板句"]
                new = " ~ ".join(yi_usd(x) for x in sorted(set(best["新"]))) or "—"
                old = " ~ ".join(yi_usd(x) for x in sorted(set(best["旧"]))) or "—"
                L.append(f"| **{r['ticker']}** | **{r['方向']}** | {best['期间'] or '—'} "
                         f"| {new} | {old} | {best['原句']} "
                         f"| [{r['会议']}]({r['url']})({r['抓于']}) |")
            pers = {_fmt_period(r["会议"]) for r in cg["rows"]}
            if len(pers) > 1:
                newest = max(pers)
                behind = [f"{r['ticker']}({_fmt_period(r['会议'])})"
                          for r in cg["rows"] if _fmt_period(r["会议"]) < newest]
                L.append("")
                L.append(f"> ⚠ **这几家的纪要不是同一个季度的**"
                         f"({'、'.join(sorted(pers))})。指引给的通常是**全年**数,"
                         f"所以不同季度的口吻仍可比;但**上调/下调是相对各自的上一版"
                         f"说的** —— 一家在 Q1 上调、另一家在 Q2 上调,"
                         f"不代表它们是同一时点的判断。"
                         + (f"落后的是 **{'、'.join(behind)}**,"
                            f"最新的那批是 {newest}。" if behind else "")
                         + "落后的那一家**不是我们漏抓了**:"
                         "`fetch_all.py --transcripts-latest <TICKER>` 会去它的"
                         "Motley Fool 个股页找最新一场,找不到就说明**那边还没发布**。"
                         "2026-09-02 实测 GOOGL 就是这一种 —— "
                         "站上最新仍是 2026-04-29 那场。")
            L.append("")
            for r in cg["rows"]:
                L.append(f"- **{r['ticker']} {r['方向']}** —— {r['理由']}")
            L.append("")
            m = cg.get("合成")
            if m:
                L.append(f"→ **这四家合起来只算一个箭头:{m['方向']}**。{m['理由']}")
                L.append("")
                L.append("> **为什么不是四票**:四家云厂的 capex 指引不是四条独立证据 —— "
                         "同一轮 AI 投资周期、同一批供应商、同一份宏观预算,"
                         "它们同向的概率本来就接近 1。当四票投,"
                         "等于把同一个赌注写成四行,然后说「四条证据都支持」。"
                         "**独立信号才能相加。**")
                L.append("")
        else:
            L.append("**四家云厂一句指引都没抽到。**")
            L.append("")
        for miss in cg.get("缺", []):
            L.append(f"- ⚠ **没有 {miss}** —— 这一家不计入方向统计。"
                     f"**缺数据是我们的工作缺口,不是「这家没动静」**,"
                     f"所以它不会被当成 →(持平)混进去。")
        if cg.get("缺"):
            L.append("")
        L.append(f"**方向规则(写死在代码里)**:{DIRECTION_RULES['cloud_guidance']}")
        L.append("")

    tw = g["项目"].get("taiwan_optics")
    if tw is not None:
        L.append("### 台湾光通信月营收(同一条链上的月度数)")
        L.append("")
        if tw.get("ok"):
            L.append(f"台湾上市公司**每月 10 日前公告上月营收**,而 A 股一个季度才说一次话。"
                     f"这几家和我们手上的光模块厂卖给同一批客户 —— "
                     f"**它回答的是「这条链上个月忙不忙」,不回答「中际旭创卖了多少」。**")
            L.append("")
            L.append(f"资料年月 **{tw['期间']}**(民国纪年,{tw['期间'][:3]} 年 = "
                     f"公元 {int(tw['期间'][:3]) + 1911} 年 {tw['期间'][3:]} 月),"
                     f"共 {tw['n']} 家。")
            L.append("")
            L.append("| 公司 | 代号 | 做什么 | 当月同比 | 环比上月 |")
            L.append("|---|---|---|---:|---:|")
            for r in sorted(tw["rows"], key=lambda x: -x["同比%"]):
                L.append(f"| {r['公司']} | {r['代號']} "
                         f"| {r['为什么看它']} | **{r['同比%']:+.1f}%** "
                         + (f"| {r['环比%']:+.1f}% |" if r["环比%"] is not None else "| — |"))
            L.append("")
            L.append(f"**{tw['n']} 家同比中位 {tw['同比中位']:+.1f}%**"
                     + (f",环比中位 {tw['环比中位']:+.1f}%" if tw["环比中位"] is not None else "")
                     + f" → **{tw['方向']}**({tw['理由']})。")
            L.append("")
            L.append(f"**方向规则(写死在代码里)**:{DIRECTION_RULES['taiwan_optics']}")
            L.append("")
            L.append(f"来源:{tw['source']} · [{tw['url']}]({tw['url']}) · 抓于 {tw['抓于']}")
        else:
            L.append(f"**拿不到**:{tw['why']}")
        L.append("")

    uc = g["项目"].get("upstream_capex")
    if uc is not None:
        L.append("### 上游已经花出去的钱(季度资本开支合计同比)")
        L.append("")
        if uc.get("ok"):
            L.append(f"上游是 **{uc['名称']}**(成员 {' / '.join(uc['成员'])})。"
                     f"这是**已经付出去的现金**,不是谁的说法。")
            L.append("")
            L.append("| 季度 | 合计资本开支同比 |")
            L.append("|---|---:|")
            for d, v in uc["近几季"]:
                L.append(f"| {d} | {v:+.1f}% |")
            L.append("")
            L.append(f"→ **{uc['方向']}**({uc['理由']})")
            L.append("")
            L.append(f"**方向规则(写死在代码里)**:{DIRECTION_RULES['upstream_capex']}")
            L.append("")
            L.append("> ⚠ **只在成员全都有数的季度才算同比。** 少一家就少几百亿,"
                     "同比会凭空多出几十个百分点,而且不报错 —— 序列看着完全正常,"
                     "只是每一格都偏。")
        else:
            L.append(f"**算不了**:{uc['why']}")
        L.append("")

    op = g["项目"].get("oil_price")
    if op is not None:
        L.append("### 油价(只展示,不进打分)")
        L.append("")
        if op.get("ok"):
            L.append("| 品种 | 现价(美元/桶) | 报价日 |")
            L.append("|---|---:|---|")
            for r in op["rows"]:
                L.append(f"| {r['名称']}({r['代码']}) | {r['现价']:.3f} | {r['日期']} |")
            L.append("")
            L.append(f"来源:{op['source']} · [{op['url']}]({op['url']}) · 抓于 {op['抓于']}")
            L.append("")
        else:
            L.append(f"**拿不到**:{op['why']}")
            L.append("")
        L.append(f"> {DIRECTION_RULES['oil_price']} "
                 f"这个数据源返回的是一行报价字符串,只有当天那一个数。"
                 f"要让油价能进判断,得先有一条日频历史序列 —— 那是取数层的事。")
        L.append("")

    L.append("### 这一节合起来是什么方向")
    L.append("")
    if g["方向"]:
        L.append("| 指标 | 方向 | 依据 |")
        L.append("|---|:-:|---|")
        for x in g["方向"]:
            L.append(f"| {x['来自']} | **{x['方向']}** | {x['理由']} |")
        L.append("")
        import decision
        L.append(f"**{len(g['方向'])} 个指标:{g['↑数']} 个 ↑、"
                 f"{sum(1 for x in g['方向'] if x['方向'] == '→')} 个 →、"
                 f"{g['↓数']} 个 ↓,净 {g['净方向']:+d}。**"
                 f"这个净方向会进第 11 节的概率修正:**每个 ↑ 把 "
                 f"{decision.LEAD_STEP_PP:.0f} 个百分点从 bear 挪到 bull,"
                 f"每个 ↓ 反过来,合计最多挪 {decision.LEAD_MAX_PP:.0f} 个百分点。**")
        L.append("")
        L.append("> ⚠ **这些箭头不按幅度加权,而且相互高度相关。** "
                 "AMZN 把 2000 亿上调到 2200 亿(+10%)算一个 ↑,"
                 "另一家中枢只抬了 1.9% 也算一个 ↑ —— 方向对了不等于幅度对了。"
                 "而且这几项指标背后是**同一批客户的同一笔开支**"
                 "(云厂的指引、同一批云厂已经花掉的钱、给这批客户供货的台湾厂商的月营收),"
                 "不是几条互相独立的证据。合计上限从 15 降到 "
                 f"{decision.LEAD_MAX_PP:.0f} 个百分点,就是为了这一点。")
    else:
        L.append("**一个方向标记都没有** —— 这一节的指标要么这个行业没登记,要么没抓到数据。"
                 "概率不做领先层修正。")
    L.append("")
    if g["跳过"]:
        L.append("**这几项没算进去**(缺数据是我们的工作缺口,不是「上游没动静」):")
        L.append("")
        for x in g["跳过"]:
            L.append(f"- {x}")
        L.append("")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--raw", default="data/raw")
    a = ap.parse_args()
    import sector_rules
    for c in a.codes:
        s = sector_rules.load(Path(a.raw), c)
        g = build(Path(a.raw), c, s["规则"]["领先指标"])
        L = []
        render(g, "4.5", L)
        print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
