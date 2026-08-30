#!/usr/bin/env python3
"""sectors —— 赛道分派:不同赛道的领先指标、数据源、判据都不一样。

用法:
    python3 sectors.py                 # 看全部赛道
    python3 sectors.py 002353          # 按代码判它属于哪个赛道并给出该用什么
    python3 sectors.py --md s.md

为什么必须有这一层(2026-08-28 被四只票同时打脸):
    这套方法最早是围绕**光模块/AI 算力链**长出来的,领先指标就一个:
    北美云厂 Capex。然后拿它跑杰瑞股份(油气设服)——

      · 云厂 Capex 对它**完全无关**
      · PEG 算出 -15.30(净利负增长,这个指标在周期股上本来就没意义)
      · 第 7 层没有对照组,静默跳过
      · 剩下能用的只有滞后的财报与估值分位

    **结论不是「工具坏了」,是「用错了框架」。**成长股看增速持续性,
    周期股看的是**周期位置**;成长股 PEG 有意义,周期股 PEG 在盈利低谷时
    会给出「便宜」的假信号(分母小),在盈利高峰时给出「贵」的假信号。

    所以分析一只票之前,**先问它属于哪个赛道**,再决定:
    ① 领先指标是什么  ② 从哪取  ③ 判据模板怎么写  ④ 哪些常用指标在这里失效

⚠ 这张表是**方法定义**,不是数据。它告诉你该去找什么;
   实际能不能取到,以 `probe_sources` 为准。标 ❌ 的是还没做的工具缺口。
"""

import os
import socket

# akshare / efinance 内部请求不带 timeout，对端抖动时会**永久挂起**而不是报错
# （2026-08-30 实测：连接全在 CLOSE_WAIT，卡 10 分钟没动）。设一次全局默认值兜住下游库。
socket.setdefaulttimeout(float(os.environ.get("ASTOCK_SOCKET_TIMEOUT", "30")))

import argparse
import sys
from pathlib import Path

# 赛道定义。字段:
#   hints        板块关键词(efdata board 的返回里出现就算命中),窄词在前
#   nature       成长 / 周期 / 稳健 —— 决定用哪套判据
#   leading      领先指标:(名称, 领先多久, 怎么取, 现状)
#   peers        海外对照组
#   invalid      在这个赛道上会给出假信号的常用指标
#   criteria     判据模板要点
SECTORS = {
    "AI算力链": {
        "hints": ["CPO", "光通信", "光模块", "印制电路", "覆铜板", "PCB",
                  "服务器", "液冷", "算力", "云计算"],
        "nature": "成长",
        # 海外对照组。**preport 第 7 层直接读这里** —— 单一真相,
        # 不在 position_report 里另存一份 PEER_SETS(那样必漂,2026-08-28 已经漂过)。
        "peer_tickers": [("LITE", "Lumentum"), ("MRVL", "Marvell"),
                         ("COHR", "Coherent"), ("AVGO", "Broadcom"),
                         ("NVDA", "NVIDIA")],
        # 在这个赛道上会给出**假信号**的报告字段。preport 会按这个在正文里打标,
        # 而不是默默打印一个误导性的数字。key 见 position_report 的 INVALID_MARKS。
        "invalid_keys": [],
        "leading": [
            ("北美云厂 Capex 同比", "1-2 季", "capex", "✅"),
            ("云厂下季度 Capex 指引", "2-3 季", "capex --guidance（谷歌/Meta 可取）", "⚠ 微软/亚马逊拿不到"),
            ("海外同业股价", "数周", "preport 第 7 层", "✅"),
            ("券商研报里的出货/产能", "1-2 季", "research --dig", "✅"),
        ],
        "peers": "LITE MRVL COHR AVGO NVDA（光模块）· TTMI JBL CLS（PCB）",
        "invalid": [],
        "criteria": "增速持续性为主:净利同比守住阈值、毛利率不掉 3pp、"
                    "**云厂 Capex 同比不掉档**。基准率约束 bull 概率。",
    },
    "半导体设备": {
        "hints": ["半导体设备", "面板", "显示器件", "锂电专用设备", "专用设备"],
        "nature": "成长（强周期性）",
        "peer_tickers": [("AMAT", "应用材料"), ("LRCX", "泛林"),
                         ("KLAC", "科天"), ("ASML", "阿斯麦")],
        "invalid_keys": ["baserate_growth"],
        "leading": [
            ("下游晶圆厂资本开支", "2-4 季",
             "capex --tickers TSM,INTC,MU（SEC EDGAR，官方）", "✅"),
            ("设备商同业资本开支/景气", "2-4 季",
             "capex --tickers AMAT,LRCX,KLAC", "✅"),
            ("面板厂资本开支（做面板模组设备时看这个）", "2-4 季",
             "⚠ 京东方 000725 / TCL科技 000100 的现金流量表「购建固定资产支付的现金」，需手工取",
             "⚠"),
            ("在手订单 / 合同负债", "1-3 季", "⚠ 手工看财报「合同负债」科目", "⚠"),
            ("海外同业股价", "数周", "preport 第 7 层", "✅ AMAT LRCX KLAC ASML"),
        ],
        "peers": "AMAT LRCX KLAC ASML（前道设备）· 面板模组设备无美股好对标，看下游面板厂",
        "invalid": ["低基数下的净利同比（+198% 可能只是 0.2 亿 → 0.6 亿）"],
        "criteria": "**先看订单不看利润**。合同负债/存货的同比是真领先;"
                    "净利在小基数上波动极大,别当信号。估值用 PB 分位不用 PE。",
    },
    "油气设服": {
        "hints": ["油气设服", "油服", "油气资源", "页岩气", "海工装备"],
        "nature": "周期",
        "peer_tickers": [("SLB", "斯伦贝谢"), ("HAL", "哈里伯顿"),
                         ("BKR", "贝克休斯"), ("FTI", "TechnipFMC")],
        "invalid_keys": ["peg", "baserate_growth", "pe_percentile"],
        "leading": [
            ("国际油价（Brent/WTI）", "1-2 季", "commodity --group 油", "✅"),
            ("全球油气资本开支", "2-4 季",
             "capex --tickers SLB,HAL,BKR,XOM,CVX（SEC EDGAR，官方）", "✅"),
            ("北美钻机数（Baker Hughes rig count）", "1-2 季",
             "rigcount（官方站国内不可达，走转载源）", "✅"),
            ("海外油服同业股价", "数周", "preport 第 7 层", "✅ SLB HAL BKR FTI"),
        ],
        "peers": "SLB HAL BKR FTI",
        "invalid": [
            "**PEG** —— 盈利低谷时分母小给「便宜」假信号，高峰时给「贵」假信号",
            "净利同比 —— 周期股的同比在拐点附近毫无预测力",
            "净利增速的基准率 —— 基准率库是全市场口径，对强周期股不适用",
        ],
        "criteria": "**看周期位置不看增速**。PB 分位 + 油价位置 + 资本开支周期"
                    "三者交叉。周期股在 PE 最高时往往是盈利低谷（该买），"
                    "PE 最低时是盈利高峰（该卖）—— 与成长股相反。",
    },
    "消费": {
        "hints": ["白酒", "食品饮料", "调味", "乳制品", "家电", "纺织服装"],
        "nature": "稳健",
        "peer_tickers": [],
        "invalid_keys": ["peers"],
        "leading": [
            ("渠道库存与批价", "1-2 季", "❌ 无工具（草根调研/经销商反馈）", "❌"),
            ("预收款 / 合同负债", "1-2 季", "⚠ 手工看财报", "⚠"),
            ("社零同比", "同步", "⚠ akshare 宏观", "⚠"),
        ],
        "peers": "❌ 未配（消费品本土化强，海外对照意义有限）",
        "invalid": ["海外同业股价 —— 需求端在国内，海外同业无参考价值"],
        "criteria": "**现金流质量优先**:预收款/合同负债的同比、经营现金流"
                    "与净利的比值。毛利率稳定性比增速重要。",
    },
    "医药": {
        "hints": ["化学制药", "生物制品", "医疗器械", "CXO", "中药"],
        "nature": "成长（政策强相关）",
        "peer_tickers": [],
        "invalid_keys": ["pe_percentile"],
        "leading": [
            ("集采/医保谈判结果", "1-2 季", "❌ 无工具（政策公告）", "❌"),
            ("临床进度 / 获批", "多季", "⚠ efdata ann 能看到公告", "⚠"),
            ("海外 CXO 订单（若是 CXO）", "1-2 季", "⚠ 看 Lonza/Catalent 财报", "⚠"),
        ],
        "peers": "❌ 未配",
        "invalid": ["估值分位 —— 政策冲击会一次性重置整个行业的估值中枢"],
        "criteria": "**政策风险优先于财务**。先问下一轮集采覆不覆盖它的核心品种。",
    },
}

DEFAULT = {
    "nature": "未知",
    "hints": [],
    "peer_tickers": [],
    # 未定义赛道:**所有依赖赛道假设的指标都不可信**,全部打标。
    "invalid_keys": ["peg", "baserate_growth", "pe_percentile", "peers"],
    "leading": [("❌ 没有为这个赛道定义领先指标", "—", "—", "❌")],
    "peers": "❌ 未配",
    "invalid": [],
    "criteria": "⚠ **这个赛道还没定义方法。**别直接套 AI 算力链那套 —— "
                "先问「这条链的需求源头是什么、什么数据领先它 1-2 个季度」，"
                "然后把答案补进 sectors.py。",
}

# ── 库接口:给 preport 调 ─────────────────────────────────────────────────────
# preport 在跑任何一层之前先问这里「这只票是哪个赛道、哪些指标在这里失效」。
# **不这么做的后果**:2026-08-28 拿油服票跑成长股那套,报告里印出
# PEG −15.30 而没有任何提示 —— 读者会以为那是个有意义的数。


def boards_of(code):
    """取某只票的板块列表。取不到返回 []。"""
    try:
        import efinance as ef
        return [str(x) for x in ef.stock.get_belong_board(code)["板块名称"]]
    except Exception:
        try:
            import sys as _s
            _s.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                                  / "astock-quote" / "scripts"))
            import efdata
            import argparse as _ap
            df = efdata.ef.stock.get_belong_board(code)
            return [str(x) for x in df["板块名称"]]
        except Exception:
            return []


def for_code(code):
    """一步到位:给代码,返回 (赛道名 or None, spec, 命中板块, 次选列表, 板块原始列表)。

    spec 里 preport 会用到的两个字段:
      peer_tickers  海外对照组 —— **第 7 层直接用这个**
      invalid_keys  在这个赛道上会给假信号的报告字段 —— preport 按它打标
    """
    boards = boards_of(code)
    name, spec, matched, others = classify(boards)
    return name, spec, matched, others, boards


_OUT = []


def say(x=""):
    print(x)
    _OUT.append(x)


def classify(boards):
    """按板块名判赛道 —— 打分,不是先命中先用。

    ⚠ 两个坑,2026-08-28 拿杰瑞股份(油服)一次踩全:

    ① **不能把板块名拼成一个串再 `in` 匹配。**拼接会制造跨边界的假命中
       (「…先进制造风格」+「液冷服务器」拼起来能匹配出别的词)。逐个板块名匹配。

    ② **不能先命中先用。**一只票能挂 34 个板块,里面大量是**概念板块**:
       杰瑞股份主业是油服,却也挂着「液冷服务器」「动力电池回收」「核能核电」
       「锂电池概念」。只看第一个命中的,就会把油服票判成 AI 算力链。

    打分规则:每个命中的板块 +1;**排在前 3 位的板块 +3**
    (东财返回的前几个通常是行业分类,后面才是概念标签)。
    """
    scores = {}
    for name, spec in SECTORS.items():
        pts, hits = 0, []
        for i, b in enumerate(boards):
            for h in spec["hints"]:
                if h in b:
                    pts += 3 if i < 3 else 1
                    hits.append(b)
                    break            # 一个板块只算一次,别重复计分
        if pts:
            scores[name] = (pts, hits)
    if not scores:
        return None, DEFAULT, None, []
    ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])
    top = ranked[0]
    others = [(n, v[0], v[1]) for n, v in ranked[1:]]
    return top[0], SECTORS[top[0]], top[1][1], others


def show(name, spec, matched=None):
    title = name or "未定义赛道"
    say(f"## {title}")
    say()
    m = ""
    if matched:
        m = f"　·　命中板块 {' / '.join(f'`{x}`' for x in matched[:5])}"
    say(f"**性质**：{spec['nature']}{m}")
    say()
    say("| 领先指标 | 领先多久 | 怎么取 | 现状 |")
    say("|---|---|---|---|")
    for a, b, c, d in spec["leading"]:
        say(f"| {a} | {b} | {c} | {d} |")
    say()
    say(f"**海外对照组**：{spec['peers']}")
    say()
    if spec["invalid"]:
        say("**在这个赛道上会给出假信号的指标**（报告里出现要打折看）：")
        say()
        for x in spec["invalid"]:
            say(f"- {x}")
        say()
    say(f"**判据要点**：{spec['criteria']}")
    say()


def main():
    p = argparse.ArgumentParser(
        prog="sectors",
        description="赛道分派:不同赛道的领先指标 / 数据源 / 判据都不一样")
    p.add_argument("code", nargs="?", help="股票代码;不给就列出全部赛道")
    p.add_argument("--md", metavar="文件")
    a = p.parse_args()

    say("# 赛道分派")
    say()
    say("> **同一套判据套所有赛道会出错。**成长股看增速持续性，"
        "周期股看周期位置；成长股 PEG 有意义，周期股 PEG 在盈利低谷给"
        "「便宜」假信号、高峰给「贵」假信号。")
    say()

    if a.code:
        code = a.code.strip().zfill(6)
        boards = []
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent
                                   / "astock-quote" / "scripts"))
            import efdata
            import argparse as _ap
            df = efdata._board(_ap.Namespace(code=code, head=50, all=True)) \
                if hasattr(efdata, "_board") else None
            if df is None:
                import efinance as ef
                df = ef.stock.get_belong_board(code)
            boards = [str(x) for x in df["板块名称"]]
        except Exception as e:
            say(f"（取板块失败：{type(e).__name__}: {e}）")
            say()
        if boards:
            say(f"`{code}` 所属板块：{' · '.join(boards[:12])}")
            say()
        name, spec, matched, others = classify(boards)
        show(name, spec, matched)
        if others:
            # ⚠ 一票多赛道很常见(既是油服又挂液冷服务器概念)。把次选也说出来 ——
            #   主判可能是错的,读者要能一眼看到「它还沾着别的赛道」。
            say("**这只票还命中了其它赛道**（分数低于主判，但值得知道）：")
            say()
            for n, pts, hits in others:
                say(f"- **{n}**（{pts} 分）：{' / '.join(f'`{x}`' for x in hits[:4])}")
            say()
            say("> 多赛道命中时,**主判只是起点**。如果次选赛道才是它真正的主业,"
                "换用那一套的领先指标与判据 —— 概念板块会把行业判错。")
            say()
        if name is None:
            say("> **别把 AI 算力链那套直接套上来。**先回答三个问题，"
                "再把答案补进 `sectors.py` 的 SECTORS 表：")
            say(">")
            say("> 1. 这条链的**需求源头**是什么？（谁掏钱买它的产品）")
            say("> 2. 什么数据**领先那个需求 1-2 个季度**？从哪取？")
            say("> 3. 哪些常用指标在这个赛道上会**给出假信号**？")
            say()
    else:
        for name, spec in SECTORS.items():
            show(name, spec)
        say("---")
        say()
        say("**❌ 标记是工具缺口不是「拿不到」** —— 是还没做。"
            "补的时候先查 `probe_sources`，那里记着哪些源试过、哪些确认拿不到。")
        say()

    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(_OUT) + "\n", encoding="utf-8")
        print(f"\n已写入 {a.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
