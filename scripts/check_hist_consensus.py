#!/usr/bin/env python3
"""check_hist_consensus —— 券商信号历史表(hist_consensus.py)的离线检查。不联网。

查四件事:
1. 不偷看:某天之后发布的研报,改掉它、删掉它,那天的信号都不变;
   反向自测:一个不按发布日过滤、把全部研报交给线上函数的版本,结果会变 —— 这道检查抓得到。
2. 同一家券商前后两篇比归母净利润、不比每股收益:送转后每股收益变小、净利润没改 → 不算调低。
3. 解析:假 PDF 文本经 eps_revisions.resolve(live 口径)和 read_pdf_only(pdf 口径)读出年份和净利润;
   读不出的 fy 是 None、不进统计;live 口径不用 pdf 口径的研报。
4. 取列表:一段取满 250 篇会对半再取,重复的研报只留一篇;回看窗口 365 天。
研报和 PDF 文本都是合成的,数字随便取,只为测规则。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "forecast-board" / "scripts"))

import fb_common  # noqa: E402,F401  先于 prices(market.py 会删代理变量)
import hist_consensus as hc  # noqa: E402
import inputs  # noqa: E402
import system_forecast as sf  # noqa: E402

FAILS = 0
ER = inputs.eps_revisions_module()


def ok(cond, what):
    global FAILS
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILS += 1


def pdf_txt(first_year: int, profits, eps, actual: int = 1) -> str:
    """一行写完的预测表:表头 first_year 起,前 actual 列是实际(A),后面是预测(E)。"""
    ys = [f"{first_year + k}{'A' if k < actual else 'E'}" for k in range(len(profits))]
    return "\n".join([" ".join(ys), "归母净利润（亿元） " + " ".join(f"{p:.2f}" for p in profits),
                      "每股收益（元） " + " ".join(f"{e:.2f}" for e in eps)])


def rec(known, org, fy, profit, eps=None, src="api", info=None):
    return {"known": known, "org": org, "info": info or f"{org}-{known}", "title": "", "rating": "",
            "rating_change": None, "eps": list(eps if eps is not None else [p / 10 for p in profit]),
            "fy": fy, "profit": list(profit), "src": src}


def table(recs, code="X"):
    return {"meta": {}, "keys": {code: sorted(recs, key=lambda x: (x["known"], x["info"]))}}


# 甲 06-10 调高明年(2027)净利润;乙 06-25 调低 —— 06-20 那天还看不到乙那篇
BASE = [rec("2026-05-01", "甲", 2026, [80, 100, 130]), rec("2026-06-10", "甲", 2026, [80, 110, 140]),
        rec("2026-05-05", "乙", 2026, [80, 100, 130]), rec("2026-06-25", "乙", 2026, [80, 90, 120])]


def unfiltered_signal(tab, code, day):
    """反面:不按发布日过滤,一年里全部研报都交给线上函数。"""
    rows = [{"date": x["known"], "org": x["org"], "info": x["info"], "eps": x["eps"], "fy": x["fy"],
             "profit": x["profit"]} for x in tab["keys"][code] if x["src"] == "api"]
    v = inputs.eps_value(rows, day)
    return sf._net_sign(v["up30"], v["down30"])


def check_no_peek():
    print("== 不偷看:某天以后发布的研报不影响那天的信号")
    t = table(BASE)
    ok(hc.signal_at(t, "X", "2026-06-20") == 1, "06-20:只看得到甲 06-10 的调高 → +1")
    ok(hc.signal_at(t, "X", "2026-06-30") == 0, "06-30:甲调高、乙调低各一家 → 0")
    later = [dict(x, profit=[p * 3 for p in x["profit"]], eps=[e * 3 for e in x["eps"]])
             if x["known"] > "2026-06-20" else x for x in BASE]
    ok(hc.signal_at(table(later), "X", "2026-06-20") == 1, "06-20 以后的研报数字改成 3 倍 → 06-20 的信号不变")
    ok(hc.signal_at(table([x for x in BASE if x["known"] <= "2026-06-20"]), "X", "2026-06-20") == 1,
       "删掉 06-20 以后的研报 → 不变")
    ok(unfiltered_signal(t, "X", "2026-06-20") == 0,
       "反向自测:不按发布日过滤的版本,06-20 就把乙 06-25 的调低数进去(天数是负的),得 0 → 抓得到")
    ok(hc.value_at(t, "X", "2026-06-20")["n_reports"] == 3, "06-20 能看到的研报 3 篇(乙 06-25 那篇不在)")


def check_profit_not_eps():
    print("== 比归母净利润,不比每股收益")
    split = [rec("2026-05-01", "丙", 2026, [80, 193.5, 260], eps=[8.0, 19.47, 26.2]),
             rec("2026-06-01", "丙", 2026, [80, 193.2, 260], eps=[5.7, 13.86, 18.6])]
    t = table(split)
    v = hc.value_at(t, "X", "2026-06-15")
    ok(hc.signal_at(t, "X", "2026-06-15") == 0 and v["down30"] == 0,
       f"送转后每股收益 19.47 → 13.86、净利润 193.5 → 193.2 → 持平,不算调低:{v}")
    naive = (split[1]["eps"][1] - split[0]["eps"][1]) / split[0]["eps"][1] * 100
    ok(naive < -20, f"对照:要是比每股收益,会算成调低 {naive:.1f}% —— 这条检查有意义")
    no_profit = [dict(split[0]), dict(split[1], profit=[None, None, None])]
    v2 = hc.value_at(table(no_profit), "X", "2026-06-15")
    ok(v2["down30"] == 0 and v2["eps_basis_skipped30"] == 1,
       f"一篇读不出净利润 → 只能比每股收益的那次不算进上下调,另记 1 条:{v2}")


def check_parse():
    print("== 解析:假 PDF 文本读年份和净利润")
    texts = {"A1": pdf_txt(2025, [50, 100, 150, 200], [5.0, 10.0, 15.0, 20.0]),
             "A2": "这篇没有预测表,只有正文。",
             "P1": pdf_txt(2017, [5, 8, 12, 15], [0.5, 0.8, 1.2, 1.5])}
    rows = [{"date": "2026-03-01", "org": "甲", "info": "A1", "eps": [10.0, 15.0, 20.0]},
            {"date": "2026-03-02", "org": "乙", "info": "A2", "eps": [10.0, 15.0, 20.0]},
            {"date": "2018-04-01", "org": "丙", "info": "P1", "eps": [None, None, None]}]
    fetch = lambda info: texts[info].encode()  # noqa: E731
    text_of = lambda b: b.decode()  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        live = ER.resolve(rows, Path(tmp), fetch, text_of)
        a1 = next(r for r in live if r["info"] == "A1")
        ok(a1["fy"] == 2026 and a1["profit"] == [100.0, 150.0, 200.0],
           f"live:列表本年 10.0 在 PDF 表里是 2026 那列,净利润 100/150/200:{a1['fy']} {a1['profit']}")
        a2 = next(r for r in live if r["info"] == "A2")
        ok(a2["fy"] is None, "live:PDF 里没有预测表 → fy 是 None(不猜)")
        po = hc.resolve_pdf_only(rows, Path(tmp) / hc.PDFONLY_NAME, fetch, text_of, ER)
        ok(len(po) == 1 and po[0]["fy"] == 2018 and po[0]["profit"] == [8.0, 12.0, 15.0],
           f"pdf 口径:列表没预测数的那篇,按表头读 2018~2020 的净利润:{po}")
        ok(hc.read_pdf_only("正文,没有表", 2018, ER)["fy"] is None, "pdf 口径:读不出 → fy 是 None")
    recs = hc.build_records([r for r in live if r["eps"][0]], po)
    t = table(recs)
    ok([x["src"] for x in t["keys"]["X"]] == ["pdf", "api", "api"], "合表按发布日从旧到新,标出口径")
    pdf_up = [rec("2018-04-01", "丁", 2018, [8, 12, 15], eps=[], src="pdf"),
              rec("2018-05-10", "丁", 2018, [8, 13, 16], eps=[], src="pdf")]
    tp = table(pdf_up)
    ok(hc.signal_at(tp, "X", "2018-05-20") == 0 and hc.signal_at(tp, "X", "2018-05-20", mode="pdf") == 1,
       "pdf 口径的调高:live 口径不用(得 0),pdf 口径用(得 +1)")
    ok(hc.signal_at(tp, "Y", "2018-05-20") is None, "表里没有这只股票 → None,不是 0")
    mixed = [rec("2018-04-01", "己", 2018, [None, None, None], eps=[0.5, 0.8, 1.2]),
             rec("2018-05-10", "己", 2018, [8, 13, 16], eps=[], src="pdf")]
    vm = hc.value_at(table(mixed), "X", "2018-05-20", mode="pdf")
    ok(vm["up30"] == 0 and vm["down30"] == 0,
       f"pdf 口径混用:前一篇只有每股收益、后一篇净利润占位 → 比不了净利润,不算调高调低:{vm}")


def check_lists_and_window():
    print("== 取列表、回看窗口")
    calls = []

    def fake(code, a, b):
        calls.append((a, b))
        if (a, b) == ("2020-01-01", "2020-12-31"):
            return [{"info": f"I{k}", "date": "2020-03-01"} for k in range(250)]
        return [{"info": "I1", "date": "2020-03-01"}, {"info": f"J{a}", "date": a}]
    got = hc.fetch_code("X", "2020-12-31", fake, sleep=0, first_year=2020)
    ok(len(calls) == 3 and calls[1][0] == "2020-01-01" and calls[2][1] == "2020-12-31",
       f"一段取满 250 篇 → 对半再取两段:{calls}")
    ok(len(got) == 3 and len({r["info"] for r in got}) == 3, "重复的研报只留一篇")
    old = [rec("2025-05-01", "戊", 2025, [60, 100, 130]), rec("2026-06-01", "戊", 2026, [100, 130, 160])]
    v = hc.value_at(table(old), "X", "2026-06-15")
    ok(v["n_reports"] == 1 and hc.signal_at(table(old), "X", "2026-06-15") == 0,
       "一年以前那篇不拿来比(和线上一样只看近 365 天)")
    ok(hc.signal_at(table(old), "X", "2026-04-15") == 0 and hc.value_at(table(old), "X", "2026-04-15")["n_reports"] == 1,
       "2026-04-15 只看得到 2025-05-01 那篇")


def main():
    check_no_peek()
    check_profit_not_eps()
    check_parse()
    check_lists_and_window()
    print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项没过'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
