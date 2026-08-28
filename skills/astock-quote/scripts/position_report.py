#!/usr/bin/env python3
"""position_report —— 持仓决策报告:一条命令跑完八层数据,输出可直接读的结论。

用法:
    P=$VENV/bin/python
    $P $LAB/tools/position_report.py 300502:400.00
    $P ... 300502:400.00 300308:1100.00 --md ~/report.md
    $P ... 300502:400.00 --peers LITE,MRVL,COHR      # 自定义海外对照
    $P ... 300502:400.00 --no-peers                  # 跳过海外(省 20 秒)

与另外三个脚本的关系:
    astock   问一次答一次的行情快照        —— 盘中看盘用
    hcheck   持仓业绩体检(基本面+估值分位) —— 本脚本包含它的全部内容
    efdata   东财报表数据的通用取数口       —— 本脚本调它取筹码数据
    本脚本   把上面几层拼成一份**决策报告**  —— 季报后跑一次

八层数据(每层失败都降级不中断):
    1 持仓状况   成本/现价/浮亏/回本需涨幅            新浪
    2 基本面     营收净利同比+环比、毛利率、ROE、EPS   akshare + efinance
    3 估值       PE/PB 历史分位 + 按业绩推的动态 PE    baostock + 腾讯
    4 筹码杠杆   股东户数变化、融资余额占比、十大股东  efinance + akshare
    5 风险       未来 10 个月解禁                     akshare
    6 技术位     均线、距高点回撤、成本的历史分位      akshare 前复权
    7 海外同业   光模块上游链近 5 日/1 月/3 月涨跌     akshare 美股(新浪源)
    8 基准率     同类公司历史上保持住的比例(外部视角)  baserate(全市场业绩)

第 7 层是本脚本存在的主要理由:A 股光模块的需求端在海外,
只看 A 股会漏掉「海外同行在涨而 A 股在跌」这类背离信号。

第 8 层解决另一个问题:前 7 层全是**这一家**的数据 —— 那是内部视角,
人在内部视角下会系统性高估「这种好状态还能持续多久」。第 8 层用全市场
历史给每条判据配一个基准率:历史上处在同样位置的公司,四个季度后有多少
还保持着。**判据线和基准率线用同一个数**,那句「历史基准率 24%」才真的
在回答那条判据。用 --no-baserate 跳过(省约 2 秒,冷缓存时约 30 秒)。

代理:只在本进程 os.environ.pop 掉代理变量,不动 shell 全局。
"""

import os
import sys

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import argparse  # noqa: E402
import time  # noqa: E402
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


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

try:
    import holdings_check as hc  # 复用它的取数函数,不重复实现
except ImportError:
    sys.exit(f"找不到同目录的 holdings_check.py({_HERE})")



def _load_sibling(module, skill):
    """从**另一个 skill** 里 import 一个模块(baserate / md2html)。

    ⚠ 为什么不能只靠 sys.path.insert(_HERE):要找的模块属于别的 skill,
    本脚本属于 astock-quote。两边都被软链进 <仓>/tools/,但
    `Path(__file__).resolve()` 会把软链解成**真实路径**,_HERE 因此指向
    astock-quote/scripts —— 那里没有那个模块。
    所以按候选目录逐个试:先试仓内的兄弟 skill,再试 tools/ 汇集目录。

    ⚠ 必须显式接住 SystemExit:被 import 的模块可能在缺依赖时
    `sys.exit("需要 xxx…")`,而 SystemExit 继承自 BaseException,
    **不是 Exception 的子类** —— `except Exception` 接不住它,
    于是那个模块的退出会**把整个 preport 一起带走**,连 --help 都看不了。
    preport 对这些可选依赖本来都有降级逻辑,不该因此死掉。
    """
    cands = [_HERE.parent.parent / skill / "scripts"]
    lab = os.environ.get("STOCK_LAB")
    if lab:
        cands.append(Path(lab) / "tools")
    cands.append(_lab_root() / "tools")
    for d in cands:
        if (d / f"{module}.py").exists():
            sys.path.insert(0, str(d))
            try:
                return __import__(module)
            except SystemExit:
                continue
            except Exception:
                continue
    return None


try:
    import efdata as _ef   # 同一个 skill,直接 import;缺依赖时降级不中断
except Exception:
    _ef = None

br = _load_sibling("baserate", "stock-analysis-workflow")
md2html = _load_sibling("md2html", "finance-pdf-report")

_EF_REPO = _lab_root() / "repos" / "efinance"
try:
    import efinance as ef
except ImportError:
    if _EF_REPO.is_dir():
        sys.path.insert(0, str(_EF_REPO))
    try:
        import efinance as ef
    except ImportError:
        ef = None

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402

# 行业 → 海外可比标的。A 股这些赛道的需求端在海外,只看 A 股会漏信号。
PEER_SETS = {
    "光模块": [("LITE", "Lumentum"), ("MRVL", "Marvell"), ("COHR", "Coherent"),
             ("AVGO", "Broadcom"), ("NVDA", "NVIDIA")],
    "半导体": [("NVDA", "NVIDIA"), ("AMD", "AMD"), ("INTC", "Intel"),
             ("TSM", "台积电"), ("AMAT", "应用材料")],
    "云算力": [("MSFT", "微软"), ("GOOGL", "谷歌"), ("META", "Meta"),
             ("AMZN", "亚马逊")],
    # PCB / 覆铜板 —— AI 服务器链的一环,需求端同样在海外云厂
    "PCB": [("TTMI", "TTM Technologies"), ("JBL", "Jabil"),
            ("CLS", "Celestica"), ("AVGO", "Broadcom"), ("NVDA", "NVIDIA")],
    # 油服装备 —— 需求端是全球油气资本开支,跟云厂 Capex 完全无关
    "油服": [("SLB", "斯伦贝谢"), ("HAL", "哈里伯顿"), ("BKR", "贝克休斯"),
            ("FTI", "TechnipFMC")],
}
# 板块关键词 → peer set,用 efdata board 的返回自动判
# ⚠ 顺序有意义:先匹配到的先用。窄的行业词放前面,宽的放后面 ——
#   胜宏科技同时属于「印制电路板」和「电子」,要匹配到 PCB 不是别的。
BOARD_HINTS = [
    ("CPO", "光模块"), ("光通信", "光模块"), ("光模块", "光模块"),
    ("印制电路", "PCB"), ("覆铜板", "PCB"), ("PCB", "PCB"),
    ("油气设服", "油服"), ("油服", "油服"), ("油气资源", "油服"),
    ("半导体", "半导体"), ("芯片", "半导体"),
    ("云计算", "云算力"), ("算力", "云算力"),
]

_OUT = []

# ── 结构化快照 ──────────────────────────────────────────────────────────────
# 报告的 markdown 是给人读的,**不适合做历史对比**:两份 markdown 做 diff
# 得到的是排版噪音。真正要比的是「当时看到的数字」与「当时下的判据」。
# 所以每跑一次同时落一份 JSON:journal 记**决策**,snapshot 记**证据**,
# --diff 显示**什么变了、判据离触发还有多远**。三者合起来自我进化才闭得上。
_SNAP = {"facts": {}, "criteria": {}}


def snap(section, **kv):
    """记一层结构化事实。NaN 归一成 None —— 「这次没取到」本身是信息。"""
    _SNAP["facts"].setdefault(section, {}).update(
        {k: (None if v is None or v != v else v) for k, v in kv.items()})


def say(s=""):
    print(s)
    _OUT.append(s)


def pct_chg(df, n, col="close"):
    if df is None or len(df) <= n:
        return None
    return (df.iloc[-1][col] / df.iloc[-1 - n][col] - 1) * 100


# ── 1 持仓状况 ──────────────────────────────────────────────────────────────
def sec_position(code, cost):
    name, price, prev = hc.fetch_price(code)
    say(f"# {name}（{code}）持仓决策报告")
    say()
    say(f"> 生成时间 {time.strftime('%Y-%m-%d %H:%M')}　·　"
        f"数据均为本次实时拉取，非缓存")
    say()
    say("## 1　持仓状况")
    say()
    say("| 项 | 值 |")
    say("|---|---|")
    say(f"| 现价 | {price:.2f} |")
    if cost is not None:
        pnl = (price - cost) / cost * 100
        need = (cost / price - 1) * 100
        say(f"| 成本 | {cost:.3f} |")
        say(f"| 浮盈亏 | **{pnl:+.1f}%** |")
        if pnl < 0:
            say(f"| 回本需涨 | **{need:+.1f}%** |")
    say()
    snap("position", price=price, cost=cost,
         pnl_pct=None if cost is None else (price - cost) / cost * 100)
    return name, price


# ── 2 基本面 ────────────────────────────────────────────────────────────────
def sec_fundamental(code):
    say("## 2　基本面")
    say()
    got = False
    if ef is not None:
        try:
            p = ef.stock.get_all_company_performance()
            r = p[p["股票代码"] == code]
            if len(r):
                r = r.iloc[0]
                say("| 指标 | 数值 | 同比 | 环比 |")
                say("|---|---|---|---|")
                say(f"| 营业收入 | {r['营业收入']/1e8:.2f} 亿 | "
                    f"**{r['营业收入同比增长']:+.1f}%** | {r['营业收入季度环比']:+.1f}% |")
                say(f"| 净利润 | {r['净利润']/1e8:.2f} 亿 | "
                    f"**{r['净利润同比增长']:+.1f}%** | {r['净利润季度环比']:+.1f}% |")
                say(f"| 销售毛利率 | {r['销售毛利率']:.1f}% | — | — |")
                say(f"| 净资产收益率 | {r['净资产收益率']:.1f}% | — | — |")
                say(f"| 每股收益 | {r['每股收益']} 元 | — | — |")
                say(f"| 每股经营现金流 | {r['每股经营现金流量']:.2f} 元 | — | — |")
                say()
                say(f"报告期公告日 {str(r['公告日期'])[:10]}")
                say()
                got = True
                snap("fundamental",
                     rev=float(r["营业收入"]) / 1e8, rev_yoy=float(r["营业收入同比增长"]),
                     rev_qoq=float(r["营业收入季度环比"]),
                     net=float(r["净利润"]) / 1e8, net_yoy=float(r["净利润同比增长"]),
                     net_qoq=float(r["净利润季度环比"]),
                     gm=float(r["销售毛利率"]), roe=float(r["净资产收益率"]),
                     eps=float(r["每股收益"]), period=str(r["公告日期"])[:10])
                return {"net": r["净利润"], "net_yoy": r["净利润同比增长"],
                        "gm": r["销售毛利率"], "eps": r["每股收益"]}
        except Exception as e:
            say(f"（efinance 全市场业绩表取数失败：{type(e).__name__}）")
            say()
    if not got:
        say("（efinance 不可用，回退到 akshare 财务摘要）")
        say()
    return {}


# ── 3 估值 ──────────────────────────────────────────────────────────────────
def sec_valuation(code, price, fund):
    say("## 3　估值")
    say()
    pe, cap = hc.fetch_pe_cap(code)
    rows = []
    if pe:
        rows.append(("PE(TTM)", f"{pe}"))
    if cap:
        rows.append(("总市值", f"{cap} 亿"))
    try:
        v = hc.valuation_percentile(code)
        if v:
            for key, label in (("pe", "PE"), ("pb", "PB")):
                if key in v:
                    cur, pctl, lo, hi = v[key]
                    tag = "低估" if pctl < 30 else ("偏高" if pctl > 70 else "中性")
                    rows.append((f"{label} 历史分位（{v['start'][:4]} 至今）",
                                 f"**{pctl:.0f}%　{tag}**（区间 {lo:.0f}~{hi:.0f}）"))
                    snap("valuation", **{f"{key}_pctl": pctl})
    except Exception as e:
        rows.append(("历史分位", f"取数失败 {type(e).__name__}"))
    say("| 项 | 值 |")
    say("|---|---|")
    for k, val in rows:
        say(f"| {k} | {val} |")
    say()

    # 用半年报净利推全年动态 PE。半年报净利 × 2 是保守口径(不假设 H2 加速)。
    if cap and fund.get("net"):
        try:
            capv = float(str(cap).replace(",", ""))
            h1 = fund["net"] / 1e8
            say("按半年报推的动态 PE（市值 ÷ 全年净利）：")
            say()
            say("| 全年净利假设 | 对应净利 | 动态 PE |")
            say("|---|---|---|")
            for mult, desc in ((2.0, "H2 与 H1 持平（保守）"),
                               (2.3, "H2 比 H1 增 30%（延续环比加速）")):
                fy = h1 * mult
                say(f"| {desc} | {fy:.0f} 亿 | **{capv/fy:.1f}x** |")
            say()
            if fund.get("net_yoy"):
                peg = (capv / (h1 * 2.0)) / fund["net_yoy"]
                snap("valuation", pe_ttm=float(pe) if pe else None, cap=capv,
                     dyn_pe=capv / (h1 * 2.0), peg=peg)
                say(f"保守口径 PEG ≈ **{peg:.2f}**"
                    f"（动态 PE ÷ 净利增速 {fund['net_yoy']:.0f}%）"
                    f"　—— < 1 通常视为增速能撑住估值。")
                say()
        except Exception:
            pass


# ── 4 筹码与杠杆 ────────────────────────────────────────────────────────────
def sec_chips(code, price):
    say("## 4　筹码与杠杆")
    say()
    hit = False
    if ef is not None:
        try:
            d = ef.stock.get_latest_holder_number()
            r = d[d["股票代码"] == code]
            if len(r):
                r = r.iloc[0]
                # ⚠ efinance 这两列的**标签是反的**(2026-08-27 实测 300502:
                #   股东人数=257067、股东人数增减=65.33、较上期变化百分比=101578)
                #   —— "股东人数增减" 装的是百分比,"较上期变化百分比" 装的是户数。
                #   下面按**实际值**取,不按列名取。看到列名觉得写反了想"修正"的,
                #   先跑 `efdata holdernum --csv x.csv` 看原始数再动。
                chg = r["较上期变化百分比"]        # 实为:增减户数
                prev_n = r["股东人数"] - chg if pd.notna(chg) else None
                pctchg = r["股东人数增减"]         # 实为:变化百分比
                say(f"**股东户数** {r['股东人数']:,.0f} 户"
                    f"（截止 {str(r['股东户数统计截止日'])[:10]}）")
                say()
                say("| 项 | 值 |")
                say("|---|---|")
                if prev_n:
                    say(f"| 上期户数 | {prev_n:,.0f} |")
                    say(f"| 增减 | **{chg:+,.0f} 户** |")
                say(f"| 变化幅度 | **{pctchg:+.1f}%** |")
                say(f"| 户均持股 | {r['户均持股数量']:,.0f} 股 |")
                say(f"| 户均市值 | {r['户均持股市值']/1e4:,.1f} 万 |")
                say()
                snap("chips", holders=float(r["股东人数"]),
                     holders_chg_pct=None if pd.isna(pctchg) else float(pctchg),
                     holders_asof=str(r["股东户数统计截止日"])[:10])
                if pd.notna(pctchg):
                    if pctchg > 20:
                        say(f"> 🔴 **户数大增 {pctchg:+.0f}%＝筹码分散**。"
                            f"典型的高位派发特征：股价高位时筹码从少数人散到大量新账户，"
                            f"之后反弹会遇到更重的解套抛压。")
                    elif pctchg < -10:
                        say(f"> ✅ **户数减少 {pctchg:.0f}%＝筹码集中**，"
                            f"通常出现在主力吸筹阶段。")
                    else:
                        say(f"> 户数变化 {pctchg:+.0f}%，筹码结构无显著变化。")
                    say()
                hit = True
        except Exception as e:
            say(f"（股东户数取数失败：{type(e).__name__}）")
            say()

    # 融资余额:反映杠杆资金参与度。占流通市值越高,下跌时越容易踩踏
    try:
        mkt = "sh" if code.startswith("6") else "sz"
        fn = ak.stock_margin_detail_szse if mkt == "sz" else ak.stock_margin_detail_sse
        for back in range(0, 7):  # 往前找最近有数据的交易日
            day = (pd.Timestamp.now() - pd.Timedelta(days=back)).strftime("%Y%m%d")
            try:
                m = fn(date=day)
            except Exception:
                continue
            key = "证券代码" if "证券代码" in m.columns else "标的证券代码"
            r = m[m[key].astype(str).str.zfill(6) == code]
            if len(r):
                r = r.iloc[0]
                bal = float(r.get("融资余额", 0))
                say(f"**融资融券**（{day[:4]}-{day[4:6]}-{day[6:]}）")
                say()
                say("| 项 | 值 |")
                say("|---|---|")
                snap("chips", margin_bal=bal / 1e8)
                say(f"| 融资余额 | {bal/1e8:,.1f} 亿 |")
                if "融资买入额" in r:
                    say(f"| 当日融资买入 | {float(r['融资买入额'])/1e8:,.1f} 亿 |")
                if "融券余额" in r:
                    say(f"| 融券余额 | {float(r['融券余额'])/1e8:,.2f} 亿 |")
                say()
                hit = True
                break
    except Exception as e:
        say(f"（融资融券取数失败：{type(e).__name__}）")
        say()

    if ef is not None:
        try:
            t = ef.stock.get_top10_stock_holder_info(code, top=1)
            if len(t):
                say(f"**前十大流通股东**（{str(t.iloc[0]['更新日期'])[:10]}）")
                say()
                say("| 股东 | 持股比例 | 变动 |")
                say("|---|---|---|")
                for _, x in t.head(10).iterrows():
                    say(f"| {x['股东名称']} | {x['持股比例']} | {x['增减']} {x['变动率']} |")
                say()
                hit = True
        except Exception:
            pass
    # ── 港资(香港中央结算)多期持股 ──────────────────────────────────
    # 比十大股东那张单期表长得多,能看出**趋势**而不只是一个截面。
    if _ef is not None:
        # ⚠ 别用 `class _A: code = code` —— 类体里同名赋值会自我遮蔽
        #   (右侧查的是还没赋值的类作用域名),直接 NameError。
        _a = argparse.Namespace(code=code, head=8, all=False, daily=False)
        try:
            h = _ef._hksc(_a)
            if len(h):
                say(f"**港资持股（香港中央结算，近 {len(h)} 期）**")
                say()
                say("| 报告期 | 持股万股 | 占总股本% | 变动万股 | 方向 |")
                say("|---|---|---|---|---|")
                for _, x in h.tail(8).iterrows():
                    chg = x["变动万股"]
                    chg_s = "—" if pd.isna(chg) else f"{chg:+,.0f}"
                    say(f"| {x['报告期']} | {x['持股万股']:,.0f} | "
                        f"{x['占总股本%']} | {chg_s} | {x['方向']} |")
                say()
                last = h.iloc[-1]
                snap("chips", hksc_shares_wan=float(last["持股万股"]),
                     hksc_pct=float(last["占总股本%"]) if pd.notna(last["占总股本%"]) else None,
                     hksc_period=str(last["报告期"]))
                # ★ 这条判断很容易看反,单独写出来
                if len(h) >= 2:
                    prev = h.iloc[-2]
                    up_shares = last["持股万股"] > prev["持股万股"]
                    dn_pct = (pd.notna(last["占总股本%"]) and pd.notna(prev["占总股本%"])
                              and last["占总股本%"] < prev["占总股本%"])
                    if up_shares and dn_pct:
                        say(f"> ⚠️ **持股数增加但占总股本比例下降**"
                            f"（{prev['占总股本%']}% → {last['占总股本%']}%）"
                            f"—— 这是**股本摊薄**（增发 / 转股 / 激励授予），"
                            f"**不是港资减仓**。判方向要看股数不看比例，只看比例会看反。")
                        say()
                hit = True
        except Exception as e:
            say(f"（港资持股取数失败：{type(e).__name__}: {e}）")
            say()

    # ── 北向个股持仓 ────────────────────────────────────────────────
    if _ef is not None:
        _b = argparse.Namespace(code=code, head=4, all=False, daily=False)
        try:
            nb = _ef._northbound(_b)
            if len(nb):
                x = nb.iloc[-1]
                say(f"**北向个股持仓**（官方口径，{x['日期']}）："
                    f"**{x['持股万股']:,.0f} 万股**，占总股本 {x['占总股本%']}%，"
                    f"占流通 {x['占流通%']}%")
                say()
                snap("chips", nb_shares_wan=float(x["持股万股"]),
                     nb_pct_total=float(x["占总股本%"]) if pd.notna(x["占总股本%"]) else None,
                     nb_asof=str(x["日期"]))
                say("> ⚠️ 这是**持仓**不是**净买入**。2024-08-19 起交易所不再公布"
                    "单票日度北向净买入 —— 那是监管改了披露规则，不是接口坏了，"
                    "没有免费替代。停更前的日度序列仍可查：`efdata northbound <代码> --daily`。")
                say()
                hit = True
        except Exception as e:
            say(f"（北向持仓取数失败：{type(e).__name__}: {e}）")
            say()

    if not hit:
        say("（本层全部取数失败）")
        say()


# ── 5 风险:解禁 ─────────────────────────────────────────────────────────────
def sec_risk(code, price):
    say("## 5　风险：限售解禁")
    say()
    try:
        start = pd.Timestamp.now().strftime("%Y%m%d")
        end = (pd.Timestamp.now() + pd.Timedelta(days=300)).strftime("%Y%m%d")
        d = ak.stock_restricted_release_detail_em(start_date=start, end_date=end)
        r = d[d["股票代码"].astype(str).str.zfill(6) == code]
        if len(r):
            say("| 解禁时间 | 类型 | 解禁数量 | 解禁市值 |")
            say("|---|---|---|---|")
            for _, x in r.iterrows():
                mv = x.get("实际解禁市值", x.get("解禁市值", 0))
                say(f"| {str(x['解禁时间'])[:10]} | {x['限售股类型']} | "
                    f"{float(x['解禁数量'])/1e4:,.0f} 万股 | {float(mv)/1e8:,.1f} 亿 |")
            say()
            snap("risk", unlock_count=len(r),
                 unlock_value=float(sum(float(x.get("实际解禁市值",
                     x.get("解禁市值", 0)) or 0) for _, x in r.iterrows())) / 1e8,
                 unlock_first=str(r.iloc[0]["解禁时间"])[:10])
            say("> ⚠️ 解禁前后常有抛压，把解禁日标进日历。")
        else:
            snap("risk", unlock_count=0, unlock_value=0.0, unlock_first=None)
            say("未来约 10 个月**无限售解禁**。")
        say()
    except Exception as e:
        say(f"（解禁取数失败：{type(e).__name__}）")
        say()


# ── 6 技术位 ────────────────────────────────────────────────────────────────
def sec_technical(code, price, cost):
    say("## 6　技术位")
    say()
    try:
        sym = ("sh" if code.startswith("6") else
               "bj" if code[0] in "48" else "sz") + code
        d = ak.stock_zh_a_daily(symbol=sym, adjust="qfq")
        d["date"] = pd.to_datetime(d["date"])
        w = d[d["date"] >= (pd.Timestamp.now() - pd.Timedelta(days=550))].copy()
        for n in (20, 60, 120):
            w[f"ma{n}"] = w["close"].rolling(n).mean()
        last = w.iloc[-1]
        hi = w["high"].max()
        hi_date = w.loc[w["high"].idxmax(), "date"].date()

        asof = str(w.iloc[-1]["date"])[:10]
        say(f"均线与区间高点基于**前复权日线，最后一根 {asof}**；"
            f"现价是实时报价，两者基准日可能差一天。")
        say()
        say("| 项 | 值 |")
        say("|---|---|")
        say(f"| 现价（实时） | {price:.2f} |")
        say(f"| 收盘（{asof}） | {w.iloc[-1]['close']:.2f} |")
        for n in (20, 60, 120):
            v = last[f"ma{n}"]
            if pd.notna(v):
                rel = "上方" if price > v else "**下方**"
                say(f"| MA{n} | {v:.1f}　现价在其{rel} |")
        say(f"| 区间最高 | {hi:.2f}（{hi_date}） |")
        say(f"| 距最高回撤 | **{(price/hi-1)*100:.1f}%** |")
        snap("technical", **{f"ma{n}": (None if pd.isna(last[f"ma{n}"])
                                        else float(last[f"ma{n}"])) for n in (20, 60, 120)},
             high=float(hi), drawdown_pct=(price / hi - 1) * 100)
        say()

        if cost is not None:
            above = (w["close"] > cost).sum()
            say(f"**你的成本在历史中的位置**：过去 {len(w)} 个交易日里，"
                f"收盘价高于 {cost:.3f} 的只有 **{above} 天（{above/len(w)*100:.0f}%）**。")
            up = w[w["close"] >= cost]
            if len(up):
                say(f"首次上穿 {up['date'].iloc[0].date()}，"
                    f"最后一次站上 {up['date'].iloc[-1].date()}。")
            if above / len(w) < 0.2:
                say()
                say("> 成本落在一个很窄的高位价格带上 —— 说明买点接近阶段顶部，"
                    "不是公司选错，是价格当时不好。")
            say()

        say("**月度走势（前复权）**")
        say()
        w["ym"] = w["date"].dt.to_period("M")
        m = w.groupby("ym").agg(开=("open", "first"), 高=("high", "max"),
                                低=("low", "min"), 收=("close", "last"))
        say("| 月份 | 开 | 高 | 低 | 收 |")
        say("|---|---|---|---|---|")
        for ym, x in m.tail(8).iterrows():
            say(f"| {ym} | {x['开']:.0f} | {x['高']:.0f} | {x['低']:.0f} | {x['收']:.0f} |")
        say()
        return w
    except Exception as e:
        say(f"（技术位取数失败：{type(e).__name__}）")
        say()
        return None


# ── 7 海外同业 ──────────────────────────────────────────────────────────────
def sec_peers(code, peers, self_df):
    say("## 7　海外同业对照")
    say()
    if not peers:
        # ⚠ 静默跳过是错的:读者看到章节从 6 跳到 8,不知道是漏了还是不适用。
        #   **缺一层证据必须说出来**,否则报告看起来比实际更完整。
        say("**这只票没有匹配到海外对照组** —— 它所属的板块不在 `PEER_SETS` 里。")
        say()
        say("可能是两种情况，含义完全不同：")
        say()
        say("1. **这条链的需求端本来就不在海外**（内需驱动的行业）—— "
            "这一层不适用，不是缺失")
        say("2. **有海外同业但工具还没配** —— 那是工具的缺口，"
            "用 `--peers TK1,TK2` 手工指定，并把这个行业补进 `PEER_SETS`")
        say()
        say("> **别把「没这一层」当成「这一层没问题」。**领先指标少一个，"
            "后面的判据就少一层交叉验证，结论的确信度要相应下调。")
        say()
        return
    say("A 股这条赛道的需求端在海外。只看 A 股会漏掉「海外同行在涨而 A 股在跌」"
        "这类背离 —— 那通常说明跌的是情绪不是需求。")
    say()
    say("| 标的 | 近 5 日 | 近 1 月 | 近 3 月 | 最新 |")
    say("|---|---|---|---|---|")
    rows = []
    for tk, cn in peers:
        try:
            d = ak.stock_us_daily(symbol=tk)
            c5, c22, c66 = pct_chg(d, 5), pct_chg(d, 22), pct_chg(d, 66)
            rows.append((cn, c22))
            say(f"| {cn}（{tk}） | {c5:+.1f}% | **{c22:+.1f}%** | {c66:+.1f}% | "
                f"{d.iloc[-1]['close']:.2f} |")
        except Exception:
            say(f"| {cn}（{tk}） | — | — | — | 取数失败 |")
        time.sleep(0.3)
    if self_df is not None and len(self_df) > 66:
        s5, s22, s66 = (pct_chg(self_df, 5), pct_chg(self_df, 22),
                        pct_chg(self_df, 66))
        asof = str(self_df.iloc[-1]["date"])[:10]
        say(f"| **本股** | {s5:+.1f}% | **{s22:+.1f}%** | {s66:+.1f}% | "
            f"{self_df.iloc[-1]['close']:.2f} |")
        say()
        # ⚠ 这里的收盘价是**日线最后一根**,第 1 层的现价是**实时报价**,
        #   两者基准日可能差一天。同一份报告里出现两个不同的「现价」而不说明,
        #   读者会以为数据错了。实测:002353 日线 08-27 收 136.24、
        #   实时 08-28 报 130.70(当天跌 4%)。
        say(f"> 本股与海外同业的收盘价都取**日线最后一根（{asof}）**；"
            f"第 1 层的现价是**实时报价**，两者基准日可能差一天。")
        say()
        good = [c for _, c in rows if c is not None]
        if good and s22 is not None:
            avg = sum(good) / len(good)
            gap = avg - s22
            snap("peers", peer_1m_avg=avg, self_1m=s22, gap_pp=gap)
            if gap > 15:
                say(f"> 🔺 **背离 {gap:.0f} 个百分点**：海外同业近 1 月均值 {avg:+.1f}%，"
                    f"本股 {s22:+.1f}%。需求端没恶化而股价在跌，"
                    f"更像资金面／情绪面问题，不是基本面破位。")
            elif gap < -15:
                say(f"> 🔻 **本股跑赢海外同业 {-gap:.0f} 个百分点**。"
                    f"要留意是否已透支，海外没跟上说明产业景气未必同步。")
            else:
                say(f"> 海外同业近 1 月均值 {avg:+.1f}%，本股 {s22:+.1f}%，走势同步。")
    say()


def pick_peers(code, explicit):
    if explicit:
        return [(t.strip().upper(), t.strip().upper()) for t in explicit.split(",")]
    if ef is None:
        return PEER_SETS["光模块"]
    try:
        b = ef.stock.get_belong_board(code)
        names = "".join(b["板块名称"].astype(str).tolist())
        for kw, setname in BOARD_HINTS:
            if kw in names:
                return PEER_SETS[setname]
    except Exception:
        pass
    return []


# ── 8 基准率校准 ────────────────────────────────────────────────────────────
def sec_baserate(fund, periods_n):
    """外部视角:历史上处在同样位置的公司,四个季度后有多少还保持着。

    返回 calibrate() 的结果字典,交给第 9 层把数标进每条判据;失败返回 None。
    """
    yoy, gm = fund.get("net_yoy"), fund.get("gm")
    if yoy is None and gm is None:
        return None
    say("## 8　基准率校准（外部视角）")
    say()
    if br is None:
        say("（找不到 baserate.py —— 它在 skill `stock-analysis-workflow` 的 "
            "scripts/ 下，或软链在 <仓>/tools/）")
        say()
        return None
    say("前面七层全是**这一家**的数据。人只看一家公司时会系统性高估"
        "「这种好状态还能持续多久」—— 因为脑子里装的是这家公司的故事，"
        "没有参照系。这一节换成外部视角：**全市场历史上处在同样位置的公司，"
        "四个季度后有多少还保持着。**")
    say()
    try:
        c = br.calibrate(net_yoy=yoy, gm=gm, periods_n=periods_n)
    except Exception as e:
        say(f"（基准率取数失败：{type(e).__name__}: {e}）")
        say()
        return None
    if not c:
        say("（全市场历史数据为空）")
        say()
        return None

    say("| 问题 | 同类公司 | 基准率 | 样本期 | 区间 |")
    say("|---|---|---|---|---|")
    gth = c.get("growth_cohort")
    for hold, r in sorted(c.get("growth", {}).items(), reverse=True):
        say(f"| {c['after']} 个季度后净利同比仍 ≥ {hold}% | "
            f"起始 ≥ {gth}% 的约 {r['n_base_avg']:.0f} 家 | "
            f"**{r['rate']:.0f}%** | {r['n_spans']} 段 | "
            f"{r['lo']:.0f}% ~ {r['hi']:.0f}% |")
    m = c.get("margin")
    if m:
        # 毛利率走**相对口径**(下滑不超过 N pp),因为报告里那条判据就是相对
        # 自己说的。用固定线会让基准率回答一个更容易的问题 —— 实测口径差
        # 把 300502 的毛利率基准率从 90% 虚高到真实的 66%。
        say(f"| {c['after']} 个季度后毛利率相对自身下滑 ≤ {c['margin_drop']}pp | "
            f"起始 ≥ {c['margin_cohort']}% 的约 {m['n_base_avg']:.0f} 家 | "
            f"**{m['rate']:.0f}%** | {m['n_spans']} 段 | "
            f"{m['lo']:.0f}% ~ {m['hi']:.0f}% |")
    say()
    snap("baserate", cohort=c.get("growth_cohort"),
         **{f"hold_{h}": r["rate"] for h, r in c.get("growth", {}).items()},
         margin_cohort=c.get("margin_cohort"), margin_drop=c.get("margin_drop"),
         margin_rate=(c.get("margin") or {}).get("rate"))
    say(f"数据源：东财全市场季度业绩，报告期 {c['periods'][0]} ~ {c['periods'][-1]}"
        f"（{len(c['periods'])} 期）。")
    say()

    # 把基准率直接翻译成对期望值框架的约束 —— 这是这一层唯一的用途
    top = max(c.get("growth", {}).items(), key=lambda kv: kv[0], default=None)
    if top:
        hold, r = top
        say(f"> **怎么用**：bull 情景的概率**不该显著高于 {r['rate']:.0f}%**，"
            f"除非你能说出这家公司凭什么超出基准率 —— 已锁定的长约、"
            f"独家产能、客户结构这类**具体且可证伪**的理由，"
            f"不是「行业景气」这种谁都能说的话。")
        say()
        if r["rate"] < 35:
            say(f"> 🔴 **{r['rate']:.0f}% 是个低数字。**高增速的均值回归比直觉强得多。"
                f"这不是说这家公司会掉，是说**「它不会掉」这个假设的举证责任很重**。")
            say()
    if m and c.get("growth"):
        gmax = max(r["rate"] for r in c["growth"].values())
        if m["rate"] - gmax > 30:
            say(f"> 毛利率的粘性（{m['rate']:.0f}%）远高于增速（{gmax:.0f}%）—— "
                f"这是常态：**利润增速先掉，毛利率后掉**。"
                f"所以毛利率还没动不能当成「增速能撑住」的证据，"
                f"它本来就是更慢的那个指标。")
            say()
    return c


def _br_growth(bases, hold):
    """取「N 期后净利同比仍 ≥ hold%」的基准率,拼成判据后面那句标注。"""
    if not bases:
        return ""
    r = bases.get("growth", {}).get(hold)
    if not r:
        return ""
    return (f"　—— 同类历史基准率 **{r['rate']:.0f}%**"
            f"（起始 ≥ {bases['growth_cohort']}% 的公司里）")


def sec_verdict(fund, cost, price, bases=None):
    say("## 9　判据小结")
    say()
    say("**下面是机械判据，不是投资建议。最终决策和后果都是你的。**")
    say()
    say("### 继续持有的前提（下个季报逐条核）")
    say()
    yoy = fund.get("net_yoy")
    gm = fund.get("gm")
    if yoy is not None:
        say(f"- 净利同比保持 **>50%**（当前 {yoy:+.0f}%）{_br_growth(bases, 50)}")
    if gm is not None:
        tag = ""
        if bases and bases.get("margin"):
            tag = (f"　—— 同类历史基准率 **{bases['margin']['rate']:.0f}%**"
                   f"（起始 ≥ {bases['margin_cohort']}% 的公司里，"
                   f"下滑 ≤ {bases['margin_drop']}pp 的比例）")
        say(f"- 毛利率不掉 **3pp 以上**（当前 {gm:.1f}%，即不低于 {gm-3:.1f}%）{tag}")
    say("- 营收环比不转负")
    say()
    say("### 减仓信号（任一触发就重新评估）")
    say()
    if yoy is not None:
        tag = ""
        r30 = (bases or {}).get("growth", {}).get(30)
        if r30:
            # 这里要**反过来说**:基准率算的是「保持住」的比例,
            # 而减仓信号问的是「触发」的概率 —— 两者互补。
            tag = (f"　—— 历史上同类公司 {bases['after']} 个季度后"
                   f"**有 {100-r30['rate']:.0f}% 会跌破这条线**")
        say(f"- 净利同比**跌破 30%** —— 增速掉档，成长逻辑动摇{tag}")
    if gm is not None:
        say(f"- 毛利率跌破 **{gm-5:.1f}%**（掉 5pp）—— 价格战开打")
    say("- PE 历史分位回到 **80% 以上** —— 估值透支，该兑现")
    say("- 股东户数继续大增**同时**股价创新高 —— 还在派发，别追")
    say("- 海外同业转跌而本股独强 —— 需求端出问题的领先信号")
    say()
    if cost is not None and price < cost:
        say("### 关于补仓")
        say()
        say("看第 6 节「成本在历史中的位置」。若成本落在高位窄带，"
            "**在当前价补仓会把平均成本拖在一个更难受的位置**；"
            "更合理的参考是前一个明确低点附近，且分批不要一次打满。")
        say()


# ── 快照落盘与历史对比 ──────────────────────────────────────────────────────
SNAP_DIR_NAME = "snapshots"

# 要对比的字段:(路径, 显示名, 单位, 变多是好事吗)
# ⚠ 最后一列不能省 —— 「股东户数 +65%」是坏事,「净利同比 +19pp」是好事,
#   光看符号会把坏消息标成绿的。
DIFF_FIELDS = [
    ("position.price",           "现价",         "",   True),
    ("fundamental.rev_yoy",      "营收同比",     "%",  True),
    ("fundamental.net_yoy",      "净利同比",     "%",  True),
    ("fundamental.net_qoq",      "净利环比",     "%",  True),
    ("fundamental.gm",           "毛利率",       "%",  True),
    ("fundamental.roe",          "ROE",          "%",  True),
    ("valuation.pe_pctl",        "PE 历史分位",  "%",  False),
    ("valuation.peg",            "PEG",          "",   False),
    ("chips.holders",            "股东户数",     "户", False),
    ("chips.margin_bal",         "融资余额",     "亿", None),
    ("technical.drawdown_pct",   "距高点回撤",   "%",  True),
    ("peers.gap_pp",             "与海外同业背离", "pp", None),
    ("baserate.hold_50",         "基准率(保持50%)", "%", True),
    ("risk.unlock_value",        "未来解禁市值", "亿", False),
]


def _fmt(v, unit):
    """按单位定精度 —— 「股东户数 155,489.0 户」和「PEG 0.6」都是错的显示。
    户数是整数;PEG / 价格这类无单位的小数值要两位,一位会把 0.55→0.42
    显示成「▼0.1」,看不出实际变化。"""
    if v is None:
        return "—"
    if unit == "户":
        return f"{v:,.0f}户"
    if unit == "":
        return f"{v:,.2f}"
    return f"{v:,.1f}{unit}"


def _dig(d, path):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def load_open_journal(code):
    """读这只票**未结**的决策记录 —— 里面有当时的 thesis 与 falsify。

    ★ 为什么快照必须带上这个:只记数字的话,复核时能看到「净利同比掉了
      19pp」,却看不到「我上次凭什么认为这不要紧」。**没有当时的推理,
      就无法判断是推理错了还是世界变了** —— 而这两者的改进方向完全相反:
      推理错了要改方法,世界变了只要更新事实。自我进化改的是方法,
      所以必须能把这两种情况分开。
    """
    import json
    f = _lab_root() / "data" / "journal.jsonl"
    if not f.exists():
        return None
    rows = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("code") == code and r.get("status") == "open":
            rows.append(r)
    return rows[-1] if rows else None


def sec_reasoning(prev, jr, code):
    """第 0b 节:上次是怎么想的,现在还成立吗。

    这一节是**给 AI 读的**多于给人读的 —— 下一轮分析时它会被读回来,
    作为「上次的推理」的输入。人读第 0 节的数字变化就够了。
    """
    if not jr:
        return
    say("### 上次是怎么想的")
    say()
    say(f"决策 **#{jr['id']}**（{jr.get('logged_at', '')[:10]} 记录，"
        f"复核日 {jr.get('check_date', '未定')}，当时价 {jr.get('price')}）"
        f"　·　动作 **{jr.get('action')}**")
    say()
    if jr.get("thesis"):
        say(f"**当时的预期差判断**：{jr['thesis']}")
        say()
    if jr.get("probs"):
        p = jr["probs"]
        say(f"**当时的概率假设**：bear {p.get('bear')} / base {p.get('base')} / "
            f"bull {p.get('bull')}"
            + (f"　·　EV **{jr['ev']:+.1f}%**" if jr.get("ev") is not None else ""))
        say()
    if jr.get("falsify"):
        say(f"**当时写下的可证伪判据**：{jr['falsify']}")
        say()
    say("> **这一节是给下一轮分析读的。**只有数字变化说明不了问题 —— "
        "净利同比掉 19pp，可能是**上次的推理错了**（该改方法），"
        "也可能是**世界变了而推理本身没问题**（只要更新事实）。"
        "两者的改进方向相反，分不开就没法进化。")
    say()
    say("**下一步该做的**：拿上面的可证伪判据，逐条对第 0 节的「这次」列。"
        "触发了 → `journal close` 并蒸馏原则；没触发但在逼近 → 记一笔新的。")
    say()


def snapshot_dir(code):
    return _lab_root() / "data" / SNAP_DIR_NAME / code


def write_snapshot(code, name, cost, jr=None):
    """落一份结构化快照。同一天重跑覆盖当天那份。

    ★ 快照里**同时存事实和当时的推理**。只存事实的话,下次回来只能看到
      「数变了」,看不到「我上次凭什么这么判断」—— 那样没法分辨
      「推理错了」和「世界变了」,而自我进化要改的恰恰是前者。
    """
    import json
    _SNAP.update({"code": code, "name": name, "cost": cost,
                  "date": time.strftime("%Y-%m-%d"),
                  "ts": time.strftime("%Y-%m-%d %H:%M")})
    if jr:
        _SNAP["reasoning"] = {
            "journal_id": jr.get("id"),
            "logged_at": jr.get("logged_at"),
            "action": jr.get("action"),
            "thesis": jr.get("thesis"),
            "falsify": jr.get("falsify"),
            "probs": jr.get("probs"),
            "ev": jr.get("ev"),
            "check_date": jr.get("check_date"),
        }
    d = snapshot_dir(code)
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{_SNAP['date']}.json"
        f.write_text(json.dumps(_SNAP, ensure_ascii=False, indent=2), encoding="utf-8")
        return f
    except OSError:
        return None


def load_prev_snapshot(code, today):
    """取**今天之前**最近的一份。同一天重跑不该跟自己比。"""
    import json
    d = snapshot_dir(code)
    if not d.is_dir():
        return None
    fs = sorted(f for f in d.glob("*.json") if f.stem < today)
    if not fs:
        return None
    try:
        return json.loads(fs[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sec_diff(prev, code):
    """跟上一份快照逐项对比。这是自我进化真正缺的那一环。"""
    if not prev:
        say("## 0　与上次对比")
        say()
        say(f"（`data/{SNAP_DIR_NAME}/{code}/` 下还没有更早的快照 —— "
            f"这是第一次跑，下次再来就有对比了）")
        say()
        return
    say("## 0　与上次对比")
    say()
    say(f"上一份快照：**{prev.get('date')}**（{prev.get('ts', '')}）　·　"
        f"相隔 {_days_between(prev.get('date'), time.strftime('%Y-%m-%d'))} 天")
    say()
    say("| 指标 | 上次 | 这次 | 变化 |")
    say("|---|---|---|---|")
    moved = []
    for path, label, unit, higher_good in DIFF_FIELDS:
        a, b = _dig(prev.get("facts", {}), path), _dig(_SNAP["facts"], path)
        if a is None and b is None:
            continue
        fa, fb = _fmt(a, unit), _fmt(b, unit)
        if a is None or b is None:
            say(f"| {label} | {fa} | {fb} | — |")
            continue
        d = b - a
        if abs(d) < 1e-9:
            say(f"| {label} | {fa} | {fb} | 持平 |")
            continue
        arrow = "▲" if d > 0 else "▼"
        # unit 是 % 或 pp 时,差值本身就是百分点,不要再写成「%」
        du = "pp" if unit in ("%", "pp") else unit
        say(f"| {label} | {fa} | {fb} | **{arrow} {_fmt(abs(d), du)}** |")
        if higher_good is not None:
            moved.append((label, d, higher_good, du))
    say()
    good = [m for m in moved if (m[1] > 0) == m[2]]
    bad = [m for m in moved if (m[1] > 0) != m[2]]
    def _brief(items):
        return "、".join(f"{l} {'+' if d > 0 else '−'}{_fmt(abs(d), u)}"
                        for l, d, _, u in items[:5])
    if bad:
        say(f"> 🔴 **变差 {len(bad)} 项**：{_brief(bad)}")
        say()
    if good:
        say(f"> ✅ 变好 {len(good)} 项：{_brief(good)}")
        say()
    say("> **怎么用这一节**：不是看涨跌，是看**你上次的判据现在离触发有多远**。"
        "第 9 层每条判据后面都有阈值，拿这里的「这次」去对。"
        "判据接近触发而你没动作，那就是该记进 `journal` 的东西。")
    say()
    # 上一份快照里存的推理 —— 优先用快照里的,那是**当时**的版本;
    # journal 里的可能已经被后来的记录覆盖。
    if prev.get("reasoning"):
        r = prev["reasoning"]
        say("### 上次快照里存的推理")
        say()
        if r.get("thesis"):
            say(f"- **当时的判断**：{r['thesis']}")
        if r.get("falsify"):
            say(f"- **当时的可证伪判据**：{r['falsify']}")
        if r.get("probs"):
            p = r["probs"]
            say(f"- **当时的概率**：bear {p.get('bear')} / base {p.get('base')} / "
                f"bull {p.get('bull')}"
                + (f"，EV {r['ev']:+.1f}%" if r.get("ev") is not None else ""))
        say()


def _days_between(a, b):
    from datetime import date
    try:
        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db = (int(x) for x in b.split("-"))
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except (ValueError, AttributeError):
        return "?"


def run_one(code, cost, args):
    prev = load_prev_snapshot(code, time.strftime("%Y-%m-%d"))
    jr = load_open_journal(code)
    name, price = sec_position(code, cost)
    fund = sec_fundamental(code)
    sec_valuation(code, price, fund)
    sec_chips(code, price)
    sec_risk(code, price)
    tech = sec_technical(code, price, cost)
    if not args.no_peers:
        sec_peers(code, pick_peers(code, args.peers), tech)
    bases = None if args.no_baserate else sec_baserate(fund, args.br_periods)
    sec_verdict(fund, cost, price, bases)
    # 对比放在**判据小结之后**产出、但插到报告最前面(第 0 节):
    # 采集要等所有层跑完,而读者要先看到「跟上次比什么变了」。
    if not args.no_diff:
        head = _OUT.index("## 1　持仓状况") if "## 1　持仓状况" in _OUT else len(_OUT)
        tail = _OUT[head:]
        del _OUT[head:]
        sec_diff(prev, code)
        sec_reasoning(prev, jr, code)
        _OUT.extend(tail)
    f = write_snapshot(code, name, cost, jr)
    if f:
        print(f"\n快照 → {f}")
    say("---")
    say()
    say("**数据来源**：行情=新浪　·　估值分位=baostock　·　"
        "财务与筹码=东财（efinance）　·　解禁与融资融券=akshare　·　"
        "海外=新浪美股　·　基准率=东财全市场季度业绩（baserate）。"
        "消息面（订单／砍单／研报目标价）与券商一致预期本脚本不覆盖，"
        "前者让 Claude 用 WebSearch 补，后者跑 `consensus <代码>`。")
    say()


def main():
    p = argparse.ArgumentParser(
        prog="position_report",
        description="持仓决策报告:八层数据一次跑完,输出 markdown。")
    p.add_argument("holdings", nargs="*", metavar="代码:成本",
                   help="如 300502:400.00;不带成本只出分析不算盈亏")
    p.add_argument("--md", metavar="文件", help="把报告存成 markdown")
    p.add_argument("--peers", metavar="TK1,TK2", help="自定义海外对照标的")
    p.add_argument("--no-peers", action="store_true", help="跳过海外对照(省约 20 秒)")
    p.add_argument("--no-baserate", action="store_true",
                   help="跳过第 8 层基准率(热缓存约 2 秒,冷缓存约 30 秒)")
    p.add_argument("--br-periods", type=int, default=12, metavar="N",
                   help="基准率回看几个报告期(默认 12≈3 年)")
    p.add_argument("--html", metavar="文件",
                   help="同时出一份带版式的 HTML(浏览器可看,也能打印成 PDF)")
    p.add_argument("--no-diff", action="store_true",
                   help="跳过第 0 节「与上次对比」")
    a = p.parse_args()

    if not a.holdings:
        print(__doc__)
        return 1

    for h in a.holdings:
        if ":" in h:
            code, c = h.split(":", 1)
            cost = float(c)
        else:
            code, cost = h, None
        try:
            run_one(code.strip(), cost, a)
        except Exception as e:
            say(f"[{h}] 整体失败:{type(e).__name__}: {e}")
            say()

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    if a.html:
        if md2html is None:
            print("\n⚠ 找不到 md2html.py(在 skill finance-pdf-report 里)，跳过 HTML",
                  file=sys.stderr)
        else:
            md = "\n".join(_OUT)
            title = next((l.lstrip("# ").strip() for l in _OUT if l.startswith("# ")),
                         "持仓决策报告")
            Path(a.html).parent.mkdir(parents=True, exist_ok=True)
            Path(a.html).write_text(
                md2html.render(md, title=title,
                               subtitle=f"生成于 {time.strftime('%Y-%m-%d %H:%M')}"),
                encoding="utf-8")
            print(f"已写入 {a.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
