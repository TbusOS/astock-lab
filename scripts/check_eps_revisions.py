#!/usr/bin/env python3
"""check_eps_revisions —— 研报盈利预测:年份读得对不对、净利润读得对不对、上下调算得对不对。不联网。

用法:
    python3 scripts/check_eps_revisions.py                    # 查本仓的 eps_revisions.py
    python3 scripts/check_eps_revisions.py --file <旧版路径>    # 反向验证:旧版必须不过

为什么有这道检查:
  1.(2026-10-30)东财研报接口的「本年」EPS 不是研报发布那年。2025-09~12 发的研报,
     「本年」从 PDF 预测表核出来全是 2026 年那列。照字面当发布年用，会把「换了年份」
     算成「上调 40%」—— 一个看着正常的假信号。所以年份一律从 PDF 表头读，读不出就不用。
  2.(2026-11-02)比每股收益会被送转股骗:新易盛开源证券两篇,2026 年每股收益
     19.47 → 13.86,看着是「调低 28.8%」;同一张表里的归母净利润 193.5 → 193.2 亿，没改
     (股本变大了)。所以改成比归母净利润，读不出才比每股收益并注明。
这里用几种真实研报的表格版式(从 PDF 抽出的文字)当样本，数字是合成的。
"""
import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT = HERE.parent / "skills" / "data-sources" / "scripts" / "fetch" / "eps_revisions.py"

# 山西证券 2025-09-17(中文表，两位小数)
CN = """财务数据与估值：
会计年度
2023A
2024A
2025E
2026E
2027E
营业收入(百万元)
10,718
23,862
35,463
62,109
85,296
EPS(摊薄/元)
1.96
4.65
8.43
16.30
22.40
ROE(%)"""
# 国金证券那种版式(表头年份不带 A,三位小数)。数字换成了个位数:公开仓检查 1 把
# 「两位以上整数 + 三位小数」一律当真实成交价拦下，而这里只要测版式，不在乎数是多少。
GJ = """项目
2023
2024
2025E
2026E
2027E
摊薄每股收益(元)
2.707
4.613
8.790
9.418
9.952
每股经营性现金流净额"""
# 招银国际(英文表头)
EN = """FY23A FY24A FY25E FY26E FY27E
Net profit (RMB mn) 2,173.5 5,171.5 10,789.2 23,249.2 33,410.1
EPS (Reported) (RMB) 2.00 4.72 9.80 21.12 30.36
P/E (x) 236.5 100.2 48.3 22.4 15.6"""
# 16.30 碰巧出现在别处，后面不是 22.40 —— 不能认
FAKE = """2023A 2024A 2025E 2026E 2027E
毛利率(%) 33.0 16.30 39.4 41.5 41.0"""
# 表头只有 2 个年份(不够一串),认不出
SHORT = """2026E 2027E
EPS 16.30 22.40"""


def np_text(profit_2026, eps_2026, with_profit=True):
    """开源证券那种首页「财务摘要」版式，一格一行，表头 2025 2026 2027。
    前面放两句正文、一行「归母净利润增长率」—— 都以「归母净利润」开头，不能当成净利润那行。"""
    cells = ["归母净利润27.80 亿元，同比增长76.80%。",
             "归母净利润分别为 193.5、351.4、536.5 亿元",
             "财务摘要和估值指标", "指标", "2025", "2026", "2027",
             "营业收入(百万元)", "24,842", "52,020", "93,301",
             "归母净利润增长率(%)", "235.9", "103.0", "82.0"]
    if with_profit:
        cells += ["归母净利润(百万元)", "9,532", profit_2026, "35,210"]
    cells += ["EPS(摊薄/元)", "9.59", eps_2026, "35.42", "P/E(倍)", "60.3", "29.7", "16.3"]
    return "\n".join(cells)


# 一行写完的版式，单位写成亿元
ONE_LINE_YI = """2025A 2026E 2027E
营业收入（亿元） 248.42 520.20 933.01
归母净利润（亿元） 95.32 193.50 352.10
每股收益（元） 9.59 19.47 35.42"""
# 单位写在表头前面(上海证券那种「单位：百万元」)
UNIT_BEFORE_HEADER = "\n".join(["◼ 数据预测与估值", "单位：百万元", "2025A", "2026E", "2027E",
                                "营业收入", "24842", "52020", "93301",
                                "归母净利润", "9532", "19350", "35210"])
# 行名和表头都没写单位 —— 差 100 倍的数比没有更糟，不用
NO_UNIT = """2025A 2026E 2027E
归母净利润 9532 19350 35210"""
# 群益那种附表:表名后面跟单位;数据里碰巧有个 2020(所得税),不能因此把整张表认丢
QUNYI = "\n".join(["附一：合并损益表", "百万元", "2024", "2025", "2026E", "2027E", "2028E",
                   "利润总额", "6052", "13600", "41409", "77205", "126551",
                   "所得税", "681", "2020", "5797", "10809", "17717",
                   "归属于母公司所有者的净利润", "5171", "10797", "33475", "62412", "102304"])
# 西南 / 民生那种「归属母公司股东净利润」(没有「于」字)、太平洋那种「归母净利」(没有「润」字)
XINAN = """2025A 2026E 2027E
归属母公司股东净利润（百万元） 9532 19350 35210"""
TAIPING = """2025A 2026E 2027E
归母净利（百万元） 9532 19350 35210"""
# 招银那种「盈利预测调整」表:表头三段 FY26E~FY28E 重复(更新 / 过往 / 差别),认不出就不认
ADJUST = "\n".join(["更新预测", "过往预测", "差别 (%)", "人民币百万元",
                    "FY26E", "FY27E", "FY28E", "FY26E", "FY27E", "FY28E", "FY26E", "FY27E", "FY28E",
                    "归母净利润", "36,007", "59,809", "82,086", "28,041", "42,888", "66,065",
                    "28.4%", "39.5%", "24.3%"])
# 净利润那几格空着，下一行是「EPS(摊薄/元)」—— 带个「元」字，但不是行名的半截，不能把每股收益当净利润
BLANK_PROFIT = "\n".join(["2025A", "2026E", "2027E", "归母净利润(百万元)",
                          "EPS(摊薄/元)", "9.59", "19.47", "35.42"])
# 只有增长率那几行，没有净利润本身
ONLY_GROWTH = """2025A 2026E 2027E
归母净利润增长率 235.9 103.0 82.0
归属于母公司净利润(%) 235.9 103.0 82.0
归母净利润同比(%) 235.9 103.0 82.0"""


def load(path):
    spec = importlib.util.spec_from_file_location("eps_under_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=DEFAULT)
    a = ap.parse_args()
    if not a.file.exists():
        print(f"== 盈利预测检查:{a.file}\n  ❌ 文件不存在\n❌ 1 项不过")
        return 1
    m = load(a.file)
    fails = 0

    def ok(name, get, cond):
        """get() 取值，cond(值) 判对错。函数缺了、抛异常都记不过，不让整个检查崩掉。"""
        nonlocal fails
        try:
            got = get()
            good = bool(cond(got))
        except Exception as e:  # noqa: BLE001 —— 旧版缺函数 / 接口不同时要记成不过
            got, good = f"{type(e).__name__}: {e}", False
        fails += not good
        print(f"  {'✅' if good else '❌'} {name}" + ("" if good else f"\n       得到 {got}"))

    near = lambda want: (lambda x: x is not None and abs(x - want) < 1e-6)
    none = lambda x: x is None

    print(f"== 盈利预测检查:{a.file}")
    print("-- 年份(读 PDF 表头)")
    for name, text, this, nxt, want in (
            ("中文表:16.30 在 2026E 那列", CN, 16.3, 22.4, 2026),
            ("国金版式:9.418 在 2026E 那列(表头不带 A、三位小数)", GJ, 9.418, 9.952, 2026),
            ("英文 FY 表头:21.12 在 FY26E 那列", EN, 21.12, 30.36, 2026),
            ("碰巧相等但下一个数不是次年 → 不认", FAKE, 16.3, 22.4, None),
            ("表头不到 3 个连续年份 → 不认", SHORT, 16.3, 22.4, None)):
        ok(name, lambda t=text, x=this, y=nxt: m.eps_year(t, x, y), lambda g, w=want: g == w)

    print("-- 归母净利润(读 PDF 同一张表，换成亿元)")
    old_t, new_t = np_text("19350", "19.47"), np_text("19320", "13.86")
    py = lambda t, y: (lambda: m.profit_year(t, y))
    ok("19350 百万元 → 193.5 亿;跳过两句正文和增长率那行", py(old_t, 2026), near(193.5))
    ok("新一篇 19320 → 193.2 亿", py(new_t, 2026), near(193.2))
    ok("按表头对年份:2027 那列是 352.1 亿", py(old_t, 2027), near(352.1))
    ok("表里没有的年份 → None", py(old_t, 2028), none)
    ok("没有净利润那行 → None", py(np_text("", "13.86", with_profit=False), 2026), none)
    ok("一行写完、单位亿元 → 不再除", py(ONE_LINE_YI, 2026), near(193.5))
    ok("单位写在表头前面(单位：百万元)", py(UNIT_BEFORE_HEADER, 2026), near(193.5))
    ok("哪儿都没写单位 → None(不猜)", py(NO_UNIT, 2026), none)
    ok("群益附表:数据里有个 2020 也照样认出表头", py(QUNYI, 2026), near(334.75))
    ok("「归属母公司股东净利润」也认", py(XINAN, 2026), near(193.5))
    ok("「归母净利」也认", py(TAIPING, 2026), near(193.5))
    ok("盈利预测调整表(表头三段重复)→ None", py(ADJUST, 2026), none)
    ok("只有增长率那几行 → None", py(ONLY_GROWTH, 2026), none)
    ok("净利润格子空着 → None,不拿下一行每股收益顶上", py(BLANK_PROFIT, 2026), none)

    print("-- 前后两篇比:调高 / 调低 / 持平")
    cc = lambda o, n: (lambda: m.classify_change(o, n))
    ok("送转:每股收益 19.47→13.86,净利润 193.5→193.2 → 持平、比的是净利润",
       lambda: m.classify_change({"eps": 19.47, "profit": m.profit_year(old_t, 2026)},
                                 {"eps": 13.86, "profit": m.profit_year(new_t, 2026)}),
       lambda c: c["label"] == "持平" and c["basis"] == "net_profit")
    ok("净利润 27020→22830 百万元 → 调低 -15.5%",
       cc({"eps": 27.18, "profit": 27020 / 100}, {"eps": 22.97, "profit": 22830 / 100}),
       lambda c: c["label"] == "调低" and round(c["pct"], 1) == -15.5)
    ok("读不出净利润 → 比每股收益(-28.8% 调低),注明可能受送转影响",
       cc({"eps": 19.47, "profit": None}, {"eps": 13.86, "profit": None}),
       lambda c: c["basis"] == "eps" and "送转" in c["note"] and c["label"] == "调低"
       and round(c["pct"], 1) == -28.8)
    ok("只有一边读出净利润 → 也只能比每股收益",
       cc({"eps": 19.47, "profit": 193.5}, {"eps": 13.86, "profit": None}),
       lambda c: c["basis"] == "eps")
    ok("亏损从 -10 亿收窄到 -5 亿 → 调高 +50%(分母取绝对值)",
       cc({"eps": -1.0, "profit": -10.0}, {"eps": -0.5, "profit": -5.0}),
       lambda c: c["label"] == "调高" and round(c["pct"], 1) == 50.0)
    ok("差 0.9% → 持平;差 1% 以上才算改",
       cc({"eps": 10.0, "profit": 100.0}, {"eps": 10.0, "profit": 100.9}),
       lambda c: c["label"] == "持平")
    ok("差 1.5% → 调高(线在 1%,不是旧的 2%)",
       cc({"eps": 10.0, "profit": 100.0}, {"eps": 10.0, "profit": 101.5}),
       lambda c: c["label"] == "调高")

    print("-- 汇总(summarize 用上面的比法)")
    rows = [
        {"date": "2026-08-25", "org": "A", "info": "a1", "fy": 2026, "eps": [10.0, 20.0, None]},
        {"date": "2026-10-20", "org": "A", "info": "a2", "fy": 2026, "eps": [12.0, 18.0, None]},
        {"date": "2025-11-01", "org": "B", "info": "b1", "fy": 2026, "eps": [8.0, 12.0, None]},
        {"date": "2026-09-01", "org": "B", "info": "b2", "fy": 2026, "eps": [8.0, 13.0, None]},
        {"date": "2026-09-05", "org": "C", "info": "c1", "fy": None, "eps": [99.0, 99.0, None]},
    ]
    s26 = lambda: m.summarize(rows, 2026, "2026-10-30")
    s27 = lambda: m.summarize(rows, 2027, "2026-10-30")
    ok("FY2026 最新中位数 = median(12, 8) = 10", lambda: s26()["median"], near(10.0))
    ok("FY2026 近 30 天调高 1 家(A 10→12),没有调低",
       lambda: (s26()["up30"], s26()["down30"]), lambda g: g == (1, 0))
    ok("B 8→8 不算调整", lambda: s26()["flat90"], lambda g: g >= 1)
    ok("FY2027:A 20→18 记为调低，B 12→13 记为调高",
       lambda: (s27()["up90"], s27()["down90"]), lambda g: g == (1, 1))
    ok("年份读不出的(C)不进任何统计",
       lambda: [r["org"] for r in s26()["revisions"] + s27()["revisions"]] + list(s26()["latest"]),
       lambda g: "C" not in g)

    split = [  # D 送转前后两篇;E 读不出净利润
        {"date": "2026-04-26", "org": "D", "info": "d1", "fy": 2026,
         "eps": [19.47, 35.42, None], "profit": [193.5, 352.1, None]},
        {"date": "2026-08-25", "org": "D", "info": "d2", "fy": 2026,
         "eps": [13.86, 25.2, None], "profit": [193.2, 351.4, None]},
        {"date": "2026-07-20", "org": "E", "info": "e1", "fy": 2026, "eps": [13.27, 17.4, None]},
        {"date": "2026-09-29", "org": "E", "info": "e2", "fy": 2026, "eps": [12.88, 23.75, None]},
    ]
    sp = lambda: m.summarize(split, 2026, "2026-10-30")
    rev = lambda org: (lambda: [r for r in sp()["revisions"] if r["org"] == org][0])
    ok("D 送转那次记「持平」,不记调低", rev("D"),
       lambda r: r["label"] == "持平" and r["basis"] == "net_profit")
    ok("E 只有每股收益 → 按每股收益记调低，带「送转」注", rev("E"),
       lambda r: r["basis"] == "eps" and r["label"] == "调低" and "送转" in r["note"])
    ok("近 90 天:调低 1(E)、没改 1(D)", lambda: (sp()["down90"], sp()["flat90"]),
       lambda g: g == (1, 1))
    ok("净利润中位数只算读出净利润的那家(D 193.2)", lambda: sp()["median_profit"], near(193.2))
    report = lambda: "\n".join(m.lines(split, "2026-10-30", (2026,)))
    ok("报告里写的是净利润 193.5 → 193.2 持平，没有「-28.8%」", report,
       lambda t: "193.5" in t and "193.2" in t and "持平" in t and "-28.8" not in t)
    ok("报告里每股收益那条注明可能受送转影响", report, lambda t: "可能受送转影响" in t)

    print("-- 读 PDF(resolve):年份 + 净利润，缓存")
    calls = []
    texts = {"x1": CN, "x2": old_t}
    fetch = lambda info: (calls.append(info), info.encode())[1]
    to_text = lambda b: texts[b.decode()]
    with tempfile.TemporaryDirectory() as d:
        rr = [{"date": "2025-09-17", "org": "山西", "info": "x1", "eps": [16.3, 22.4, None]},
              {"date": "2026-04-26", "org": "开源", "info": "x2", "eps": [9.59, 19.47, 35.42]}]
        got = m.resolve(rr, Path(d), fetch, to_text)
        got2 = m.resolve([dict(r) for r in rr], Path(d), fetch, to_text)
        ok("读出年份并缓存:第二次不再下载",
           lambda: ([r.get("fy") for r in got + got2], len(calls)),
           lambda g: g == ([2026, 2025, 2026, 2025], 2))
        ok("同一篇读出三年的归母净利润(亿元)",
           lambda: [round(x, 2) if x else x for x in got[1]["profit"]],
           lambda g: g == [95.32, 193.5, 352.1])
        ok("第二次从缓存读，净利润也在", lambda: (got2[1]["profit"], got[1]["profit"]),
           lambda g: g[0] == g[1])
        ok("表里没有净利润那行 → profit 全是 None", lambda: got[0]["profit"],
           lambda g: all(x is None for x in g))
        ok("不改传进来的 rows(返回新的)", lambda: (sorted(rr[0]), sorted(rr[1])),
           lambda g: "fy" not in g[0] and "profit" not in g[1])

    with tempfile.TemporaryDirectory() as d:
        # 2026 那格净利润和每股收益对不上(算出来的股本差了一半)—— 读到了别的数，整篇净利润不用
        bad = old_t.replace("\n19350\n", "\n30000\n")
        rr = [{"date": "2026-04-26", "org": "开源", "info": "y1", "eps": [9.59, 19.47, 35.42]}]
        ok("各年「净利润 ÷ 每股收益」差太多 → 整篇不用净利润",
           lambda: m.resolve(rr, Path(d), lambda info: b"", lambda b: bad)[0],
           lambda r: r["fy"] == 2025 and all(x is None for x in r["profit"]))

    print("-- 读 PDF 失败:下载 / 抽字出错不进缓存,下次重试;读到了字但认不出年份才进缓存")
    with tempfile.TemporaryDirectory() as d:
        tries = []

        def flaky(info):
            tries.append(info)
            if len(tries) == 1:
                raise TimeoutError("下载超时")
            return info.encode()
        rr = [{"date": "2026-04-26", "org": "开源", "info": "x2", "eps": [9.59, 19.47, 35.42]}]
        first = m.resolve(rr, Path(d), flaky, lambda b: old_t)
        cache = lambda: json.loads((Path(d) / "eps_profit.json").read_text(encoding="utf-8"))  # noqa: E731
        ok("第一次下载超时:这篇 fy 记 None,但不写进缓存", lambda: (first[0]["fy"], "x2" in cache()),
           lambda g: g == (None, False))
        second = m.resolve(rr, Path(d), flaky, lambda b: old_t)
        ok("第二次重新下载,读出 2025", lambda: (second[0]["fy"], len(tries)), lambda g: g == (2025, 2))
    with tempfile.TemporaryDirectory() as d:
        n = []
        rr = [{"date": "2026-04-26", "org": "开源", "info": "z1", "eps": [9.59, 19.47, 35.42]}]
        fetch = lambda info: (n.append(info), b"x")[1]  # noqa: E731
        m.resolve(rr, Path(d), fetch, lambda b: "这份 PDF 里没有预测表")
        again = m.resolve(rr, Path(d), fetch, lambda b: "这份 PDF 里没有预测表")
        ok("读到了字但认不出年份 → 进缓存,第二次不再下载(fy 仍是 None,由调用方数出来)",
           lambda: (again[0]["fy"], len(n)), lambda g: g == (None, 1))

    print("✅ 全过" if not fails else f"❌ {fails} 项不过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
