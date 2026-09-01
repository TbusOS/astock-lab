#!/usr/bin/env python3
"""industry —— 行业量价的三条免费渠道,按**时效**排。

    industry.py taiwan                      台湾光通信月营收(滞后 ~1 月)★ 唯一真领先
    industry.py comtrade [--hs 851762]      UN Comtrade 出口量价(滞后 ~21 月)
    industry.py cite <研报PDF目录>            从券商研报里抽第三方机构引用(LightCounting 等)
    industry.py all --out data/raw

为什么要这三条:LightCounting 是光模块行业量价的事实标准,订阅制拿不到。
下面是能拿到的替代,**各自的时效差别极大,不能混着用**:

| 渠道 | 拿到什么 | 滞后 | 能当领先指标吗 |
|---|---|---|---|
| 台湾光通信月营收 | 14 家台企当月营收与同比 | **~1 个月** | **能** |
| 券商研报里的第三方引用 | LightCounting/Yole/Omdia 的预测与市场规模 | 随研报 | 能(但是二手) |
| UN Comtrade | 中国出口 HS 851762 金额/数量/**均价** | **~21 个月** | **不能**,只作历史校准 |

2026-09-02 实测:Comtrade 数据前沿停在 2024-12,逐月扫过 2025-2026 全部 24 个月一条没有。
所以它只能回答「过去几年单价怎么走的」,回答不了「这个季度在放量吗」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import date, datetime
from pathlib import Path

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
socket.setdefaulttimeout(40)

import requests  # noqa: E402

def get(url, **kw):
    """网络类抖动重试两次 —— 这几个源在本机时通时不通,一次失败就少一年数据/少七家公司,
    而清单看起来像「源没了」。2026-09-02 实测:台湾上市接口超时导致只回来 7/14 家。"""
    import time as _t
    last = None
    for i in range(3):
        try:
            return requests.get(url, **kw)
        except Exception as e:
            last = e
            if not any(k in type(e).__name__ for k in ("SSL", "Connection", "Proxy", "Timeout", "Read")):
                raise
            _t.sleep(1.5 * (i + 1))
    raise last


UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126 Safari/537.36"}
TODAY = date.today().isoformat()

# 台湾光通信链。**改这张表 = 改我们的行业景气口径**,所以每家写清是做什么的。
TW_OPTICAL = {
    "3081": "聯亞 —— 磊晶片/EML 芯片",
    "4979": "華星光 —— 光收发模块",
    "3163": "波若威 —— 光被动元件",
    "3234": "光環 —— 光收发模块/BOSA",
    "4908": "前鼎 —— 光通讯零组件",
    "6442": "光聖 —— 光纤连接/模块",
    "3363": "上詮 —— 光被动/封装",
    "3450": "聯鈞 —— 光电半导体",
    "4977": "眾達-KY —— 光收发模块",
    "8111": "立碁 —— 光电",
    "2345": "智邦 —— 交换机(下游,验证需求)",
    "3596": "智易 —— 网通设备",
}
TW_APIS = [
    ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", "上市"),
    ("https://openapi.twse.com.tw/v1/opendata/t187ap05_P", "公发"),
    ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", "上柜"),
]


def env(source, url="", params=None, ok=True, data=None, err=None, rows=None, note=""):
    return {"source": source, "url": url,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "params": {**(params or {}), **({"note": note} if note else {})},
            "ok": ok, "rows": rows if rows is not None else (len(data) if hasattr(data, "__len__") else None),
            "error": err, "data": data}


def write(out: Path, name: str, payload: dict):
    d = out / "industry"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{TODAY}-{name}.json"
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old[payload["source"]] = payload
    p.write_text(json.dumps(old, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p


# ── ① 台湾光通信月营收 ────────────────────────────────────────────────
def fetch_taiwan(out: Path):
    """台湾上市柜公司**法定每月 10 日前**公告月营收 —— 滞后只有约一个月,
    是我们能拿到的**唯一真正领先**的光模块行业量价代理。
    这些台企做的是光模块的芯片/BOSA/被动件,它们的营收领先于模块厂出货。"""
    rows, srcs = [], []
    for u, mk in TW_APIS:
        try:
            for x in get(u, headers=UA, timeout=60).json():
                rows.append({**x, "_市场": mk})
            srcs.append(u)
        except Exception as e:
            print(f"  ⚠ {mk} 接口失败 {type(e).__name__}: {str(e)[:60]}")
    if not rows:
        write(out, "台湾光通信月营收", env("台湾证交所/柜买 OpenAPI", ok=False, err="全部接口失败"))
        return []
    hit = [x for x in rows if str(x.get("公司代號")) in TW_OPTICAL]
    for x in hit:
        x["_我们为什么看它"] = TW_OPTICAL[str(x.get("公司代號"))]
        try:      # 同比自己算 —— 源里的百分比列各接口口径不一,别直接信
            cur, last = float(x.get("營業收入-當月營收") or 0), float(x.get("營業收入-去年當月營收") or 0)
            x["_同比%"] = round((cur / last - 1) * 100, 1) if last else None
        except Exception:
            x["_同比%"] = None
    ym = sorted({str(x.get("資料年月")) for x in hit if x.get("資料年月")})
    write(out, "台湾光通信月营收",
          env("台湾证交所/柜买 OpenAPI t187ap05", url=srcs[0] if srcs else "",
              params={"资料年月": ym, "扫描家数": len(rows), "命中": len(hit),
                      "接口": srcs},
              note="民国年月:11507 = 2026-07。法定每月 10 日前公告,滞后约 1 个月 —— "
                   "三条行业渠道里唯一能当领先指标的。_同比% 是自己算的,"
                   "源里的百分比列各接口口径不一",
              data=hit))
    return hit


# ── ② UN Comtrade ────────────────────────────────────────────────────
def fetch_comtrade(out: Path, hs="851762", years=("2020", "2021", "2022", "2023", "2024")):
    """⚠ HS 码必须是 **851762**。851770 在中国出口口径下**返回 0 条** ——
    2026-09-02 实测。851762 = 光通信设备(含光模块),是行业量价的最近似公开口径。
    注意它也含交换机/路由器,是**粗代理**不是纯光模块。"""
    B = "https://comtradeapi.un.org/public/v1/preview/C"
    got, frontier = [], None
    for y in years:
        try:
            d = get(f"{B}/A/HS", headers=UA, timeout=40, params={
                "reporterCode": "156", "period": y, "cmdCode": hs,
                "flowCode": "X", "partnerCode": "0"}).json().get("data") or []
            for x in d:
                v, q = x.get("primaryValue") or 0, x.get("qty") or 0
                got.append({"期间": y, "频率": "年", "金额USD": v, "数量": q,
                            "均价USD": round(v / q, 2) if q else None,
                            "净重kg": x.get("netWgt")})
        except Exception as e:
            print(f"  ⚠ {y} 失败 {type(e).__name__}")
    for y in (2024, 2025, 2026):        # 找月度前沿
        for m in range(1, 13):
            p = f"{y}{m:02d}"
            try:
                d = get(f"{B}/M/HS", headers=UA, timeout=40, params={
                    "reporterCode": "156", "period": p, "cmdCode": hs,
                    "flowCode": "X", "partnerCode": "0"}).json().get("data") or []
            except Exception:
                continue
            for x in d:
                v, q = x.get("primaryValue") or 0, x.get("qty") or 0
                got.append({"期间": p, "频率": "月", "金额USD": v, "数量": q,
                            "均价USD": round(v / q, 2) if q else None,
                            "净重kg": x.get("netWgt")})
                frontier = p
    lag = None
    if frontier:
        lag = (date.today().year - int(frontier[:4])) * 12 + date.today().month - int(frontier[4:])
    write(out, "光模块出口量价",
          env("UN Comtrade preview", url=f"{B}/A/HS?cmdCode={hs}&reporterCode=156",
              params={"HS": hs, "月度前沿": frontier, "滞后月数": lag},
              note=f"⚠ 滞后 {lag} 个月 —— **不能当领先指标**,只作历史均价校准。"
                   f"HS 851770 在此口径下返回 0 条,必须用 851762。"
                   f"该码含交换机/路由器,是粗代理不是纯光模块",
              data=got))
    return got, frontier, lag


# ── ③ 研报里的第三方机构引用 ──────────────────────────────────────────
AGENCIES = ["LightCounting", "Light Counting", "Yole", "Omdia", "Dell'Oro", "DellOro",
            "TrendForce", "集邦", "IDC", "Gartner", "Counterpoint", "群智", "CINNO"]


def extract_citations(pdf_dir: Path, out: Path):
    """从已下载的券商研报里抽**第三方机构的引用**。

    为什么值得做:LightCounting 是光模块行业量价的事实标准,订阅制拿不到原始报告,
    但**中国券商研报大量引用它的数字**。2026-09-02 实测:7 份新易盛研报里有 2 份
    直接引用 LightCounting 的市场规模与增速预测。这是它的数字进入公开域的通道。

    ⚠ 这是**二手引用**:券商可能引错、引旧、只引对自己论点有利的那半句。
    所以每条都保留**原文上下文和出处 PDF**,让人能回去核。
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf       # noqa: N813
        except ImportError:
            sys.exit("缺 pymupdf:$VENV/bin/pip install pymupdf")
    hits = []
    files = sorted(pdf_dir.rglob("*.pdf"))
    for f in files:
        try:
            doc = pymupdf.open(f)
            txt = "".join(p.get_text() for p in doc)
            doc.close()
        except Exception as e:
            hits.append({"文件": str(f), "错误": f"{type(e).__name__}: {e}"})
            continue
        for ag in AGENCIES:
            for m in re.finditer(re.escape(ag), txt):
                ctx = re.sub(r"\s+", " ", txt[max(0, m.start() - 220): m.start() + 320]).strip()
                hits.append({"机构": ag, "出处PDF": str(f), "原文上下文": ctx,
                             "含数字": bool(re.search(r"\d", ctx))})
                break                    # 每份 PDF 每个机构取一处,避免重复
    write(out, "研报第三方引用",
          env("券商研报正文抽取(pymupdf)", url="https://data.eastmoney.com/report/",
              params={"扫描PDF数": len(files), "机构清单": AGENCIES},
              note="**二手引用**:券商可能引错/引旧/只引有利的半句。每条都带原文上下文和"
                   "出处 PDF,必须能回去核。LightCounting 原始报告是订阅制,这是它的数字"
                   "进入公开域的通道",
              data=hits))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["taiwan", "comtrade", "cite", "all"])
    ap.add_argument("pdf_dir", nargs="?", default="data/raw/research")
    ap.add_argument("--hs", default="851762")
    ap.add_argument("--out", default="data/raw")
    a = ap.parse_args()
    out = Path(a.out)

    if a.cmd in ("taiwan", "all"):
        print("① 台湾光通信月营收(滞后 ~1 月,**唯一真领先**)")
        hit = fetch_taiwan(out)
        for x in sorted(hit, key=lambda z: -(z.get("_同比%") or -999))[:14]:
            print(f"   {x.get('公司代號')} {str(x.get('公司名稱'))[:8]:<9}"
                  f"当月 {str(x.get('營業收入-當月營收')):>12}  同比 "
                  f"{('%+.1f%%' % x['_同比%']) if x.get('_同比%') is not None else '—':>9}"
                  f"   {x.get('_我们为什么看它','')}")
    if a.cmd in ("comtrade", "all"):
        print(f"\n② UN Comtrade 出口量价(HS {a.hs})")
        got, fr, lag = fetch_comtrade(out, a.hs)
        for x in [g for g in got if g["频率"] == "年"]:
            print(f"   {x['期间']}  ${x['金额USD']:>16,.0f}  量 {x['数量']:>16,.0f}  "
                  f"均价 ${x['均价USD']}")
        print(f"   月度前沿 {fr},滞后 {lag} 个月 —— ⚠ 不能当领先指标")
    if a.cmd in ("cite", "all"):
        print(f"\n③ 研报里的第三方机构引用({a.pdf_dir})")
        hits = extract_citations(Path(a.pdf_dir), out)
        for h in hits:
            if h.get("机构"):
                print(f"   [{h['机构']}] {os.path.basename(h['出处PDF'])}")
                print(f"      {h['原文上下文'][:190]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
