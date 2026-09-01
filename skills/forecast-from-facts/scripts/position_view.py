#!/usr/bin/env python3
"""逐只视图 —— 账户文档第 5 节那几段话,全部从同一条管线现算。

为什么单独拆一个文件:
    `margin_account.py` 原来把五只的建议、分数、「现价前瞻 PE 37.3x」这类数字
    **写死在代码里**。写死的问题不是当时写错了 —— 当时是对的 ——
    而是它**不跟着数据变**:估值改一次算法、季报一出,那几行还在讲上一版的故事,
    看起来完全正常,不报错、不缺行,只是每个数都是旧的。
    2026-09-02 把 PE 改成 3+1 算法,那张表当场全部过期,而没有任何东西会提示。

    所以这里**不存任何数字**,只存「怎么把数字变成一句话」的规则,
    每句话都按当前数据分支:得份额 / 同步 / 丢份额;p90 之上 / p75~p90 / …;
    方法互相印证 / 互相矛盾 / 只有一个。数据变了,话跟着变。

⚠ 这个模块只负责**措辞**,不负责判断。分数和动作全部来自 `advice.py`,
    和五份个股报告第 11 节是同一套规则、同一把尺 —— 两处必须一致,
    所以两处读同一个函数,而不是各写一遍。

依赖:无(读 valuation / forecast / advice 的输出)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import advice
import valuation


def build(code: str, raw: Path) -> dict:
    """跑完整条管线,把账户文档要用的东西都算出来。

    和个股报告走的是同一串调用(valuation.build → 里面带 forecast → advice.build),
    所以两份文档不可能给出不同的建议 —— 它们读的是同一次计算的结果。
    """
    v = valuation.build(code, raw, None)
    f = v["预测"]
    a = advice.build(v, f, {})
    band, spot, our = v["前瞻PE带"], v["现价"], v["我们预测的未来四季净利"]
    pe = None
    if spot and our and our.get("mid"):
        pe = spot["close"] * v["总股本"] / our["mid"]
    return {"code": code, "建议": a, "带": band, "pe": pe,
            "预测": f, "估值": v, "产业链": f.get("产业链位置")}


def band_slot(pe, band: dict) -> str | None:
    """现价的 3+1 PE 落在自己历史带的哪一格。**按数据分支,不写死。**

    写死一句「在 p75~p90 之间」的代价:下次重跑价格变了、带也变了,
    那句话就成了错的,而且是静默错 —— 报告照样生成,读者照样相信。
    """
    if pe is None or not band.get("ok"):
        return None
    if pe > band["p90"]:
        return f"**已经越过自己历史 p90**(p90={band['p90']:.1f}x)"
    if pe > band["p75"]:
        return f"在自己历史 p75~p90 之间(p75={band['p75']:.1f}x、p90={band['p90']:.1f}x)"
    if pe > band["p50"]:
        return f"在自己历史 p50~p75 之间(p50={band['p50']:.1f}x、p75={band['p75']:.1f}x)"
    if pe > band["p25"]:
        return f"在自己历史 p25~p50 之间(p25={band['p25']:.1f}x、p50={band['p50']:.1f}x)"
    return f"**低于自己历史 p25**(p25={band['p25']:.1f}x)"


def headline(view: dict) -> str:
    """一句话概括:先说份额,再说估值。两件事都可能缺,缺了就不说那半句。"""
    cp = view["产业链"]
    a = view["建议"]
    parts = []
    if cp:
        parts.append(cp["判断"])
    else:
        parts.append("相对上游的份额变化算不出来")
    slot = band_slot(view["pe"], view["带"])
    if slot:
        parts.append("估值" + slot.replace("**", ""))
    return "**" + ",".join(parts) + "**"


def detail(view: dict) -> list[str]:
    """展开成几句话。每一句都对应打分表里的一维,顺序和那张表一致。"""
    out = []
    cp, f, band, pe = view["产业链"], view["预测"], view["带"], view["pe"]

    # ① 基本面:相对上游的份额
    if cp:
        out.append(f"相对{cp['上游']}的增速倍数,{cp['期间']} 从 "
                   f"{cp['起点']:.2f}x 变到 {cp['终点']:.2f}x。")
        if cp.get("稀疏"):
            out.append("⚠ 这几个可比点跨了不止一年 —— 上游同比低于 10% 的季度被跳过了"
                       "(除以接近 0 的数会让倍数爆掉),所以它反映的是更长时间的变化。")
    else:
        out.append(f"产业链位置算不了:{f.get('上游说明', '')}")

    # ② 确定性:几个方法适用,以及它们同不同意
    n = f["可用方法数"]
    ok = "、".join(m["name"] for m in f["methods"] if m.get("ok"))
    if n == 0:
        out.append("三个预测方法**一个都不适用** —— 下季营收我们没有可靠的推法。")
    elif n == 1:
        bad = "、".join(m["name"] for m in f["methods"] if not m.get("ok"))
        out.append(f"三个预测方法里**只有 1 个适用**({ok};{bad}没通过自检),"
                   f"区间没有第二个来交叉验证。")
    elif not f.get("方法一致", True):
        out.append(f"三个预测方法里 **{n} 个适用**({ok}),但它们给的区间**不重叠、"
                   f"互相矛盾** —— 这不是互相印证,是至少有一个方法在这只票上是错的,"
                   f"而我们不知道是哪一个。")
    else:
        out.append(f"三个预测方法里 **{n} 个适用**({ok}),区间重叠、互相印证。")
    if f.get("边界判定数"):
        out.append(f"其中 {f['边界判定数']} 个方法的适用性判定**踩在阈值边上**"
                   f"(离阈值不到它的 10%),下季度重跑很可能翻面。")

    # ③ 估值:现价在自己历史带的哪一格
    slot = band_slot(pe, band)
    if pe is not None and band.get("ok") and slot:
        out.append(f"现价 3+1 PE {pe:.1f}x,自己历史 p50 是 {band['p50']:.1f}x —— {slot}。")
    elif pe is not None:
        out.append(f"现价 3+1 PE {pe:.1f}x,但自己的历史带算不出来({band.get('why', '')}),"
                   f"没有参照系。")
    return out


def order_key(view: dict, has_debt: bool, pnl_pct: float):
    """执行顺序的排序键。**依据两条,都不是「哪只亏得多」**:
      ① 建议是减仓/清仓的先动 —— 卖出的理由必须是判断变了,不是账面亏了多少
      ② 同为减仓时,**没有融资负债的先卖** —— 卖有负债的那只,拿回的钱要先还
         它自己的债,能腾出的现金反而少;卖没负债的,全额都能拿去还债
    """
    act = view["建议"]["动作"]
    return (0 if act.startswith(("清仓", "减仓")) else 1,
            0 if not has_debt else 1,
            -pnl_pct)
