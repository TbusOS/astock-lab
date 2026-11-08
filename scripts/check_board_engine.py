#!/usr/bin/env python3
"""check_board_engine —— 预测看板(skills/forecast-board)计算部分的检查。

用法:
    python3 scripts/check_board_engine.py        # 退出码 0 才算过

不联网、不碰真实数据:
  - 配置相关的用例在临时目录里造一份合成的 board.json / local.json,
    用 STOCK_LAB 指过去,跑完还原环境变量;
  - 取数相关的用例把 market 模块里的 _get_json 换成假服务器,
    查的是「向腾讯要的是哪只、哪种复权」和「拿回来怎么整理」,
    同时顺带走一遍修过的 fetch_daily(复权键缺失要报错);
  - 统计底线和历史同类情形的用例用合成价格和固定种子,不取真实行情。

结构:每个模块一个函数(check_fb_common / check_prices ...),
后面的任务往这里加自己模块的函数,再登记进 CHECKS。
系统算法(system_forecast)那块在 check_board_system.py(这个文件超过 800 行时拆出去的)。
"""
import json
import math
import os
import random
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FB_SCRIPTS = ROOT / "skills" / "forecast-board" / "scripts"
# 按本仓路径找模块,不走 tools/ 软链或已安装的副本
sys.path.insert(0, str(FB_SCRIPTS))

import fb_common  # noqa: E402  先于 prices(market.py 会删代理变量,见 fb_common.ORIG_ENV)
import analogs  # noqa: E402
import baseline  # noqa: E402
import prices  # noqa: E402

FAILS = 0


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:400]))
    if not cond:
        FAILS += 1


def raises(fn, exc):
    """跑 fn,返回抛出的异常(类型对得上才算);没抛返回 None。"""
    try:
        fn()
    except exc as e:
        return e
    return None


@contextmanager
def env(**kw):
    """临时设环境变量,退出时原样还原(原来没有的就删掉)。"""
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── fb_common ───────────────────────────────────────────────────────────────
SAMPLE_BOARD = {
    "stocks": [{"code": "000001", "name": "样例一"}],
    "horizons": [63, 126, 250],
    "holidays_2027": ["2027-01-01"],
}


def _write_lab(lab: Path, board=True, local=True, host_entry=True):
    cfg = lab / "private" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    if board:
        (cfg / "board.json").write_text(json.dumps(SAMPLE_BOARD, ensure_ascii=False), encoding="utf-8")
    if local:
        hosts = {"另一台机器.local": {"board_out_dir": "/不该用到/out", "dashboard_dir": "/不该用到/dash"}}
        if host_entry:
            hosts[socket.gethostname()] = {
                "_备注": "合成数据",
                "playwright_root": str(lab / "pw"),
                "board_out_dir": str(lab / "out"),
                "dashboard_dir": str(lab / "dash"),
            }
        (cfg / "local.json").write_text(json.dumps({"hosts": hosts}, ensure_ascii=False), encoding="utf-8")


def check_fb_common():
    print("== fb_common:配置、存档目录、北京时间")
    with tempfile.TemporaryDirectory() as td:
        lab = Path(td).resolve()
        _write_lab(lab)
        with env(STOCK_LAB=lab):
            ok("_lab_root 认 STOCK_LAB", fb_common._lab_root() == lab, fb_common._lab_root())
            cfg = fb_common.load_config()
            ok("board.json 的键原样带出", cfg.get("stocks") == SAMPLE_BOARD["stocks"]
               and cfg.get("horizons") == [63, 126, 250], cfg)
            ok("合并本机的 board_out_dir", cfg.get("board_out_dir") == str(lab / "out"), cfg)
            ok("合并本机的 dashboard_dir", cfg.get("dashboard_dir") == str(lab / "dash"), cfg)
            ok("本机那一项的其他键不带进来", "playwright_root" not in cfg and "_备注" not in cfg, cfg)
            ok("archive_root = <工作台>/data/forecasts",
               fb_common.archive_root() == lab / "data" / "forecasts", fb_common.archive_root())

    with tempfile.TemporaryDirectory() as td:
        lab = Path(td).resolve()
        _write_lab(lab, board=False)
        with env(STOCK_LAB=lab):
            e = raises(fb_common.load_config, FileNotFoundError)
            ok("缺 board.json → FileNotFoundError,写出路径",
               e is not None and str(lab / "private" / "config" / "board.json") in str(e), e)

    with tempfile.TemporaryDirectory() as td:
        lab = Path(td).resolve()
        _write_lab(lab, local=False)
        with env(STOCK_LAB=lab):
            e = raises(fb_common.load_config, FileNotFoundError)
            ok("缺 local.json → FileNotFoundError,写出路径",
               e is not None and str(lab / "private" / "config" / "local.json") in str(e), e)

    with tempfile.TemporaryDirectory() as td:
        lab = Path(td).resolve()
        _write_lab(lab, host_entry=False)
        with env(STOCK_LAB=lab):
            e = raises(fb_common.load_config, KeyError)
            ok("local.json 里没有本机 → KeyError,写出主机名",
               e is not None and socket.gethostname() in str(e), e)

    n = fb_common.now_cn()
    ok("now_cn 带时区,偏移 +8 小时", n.tzinfo is not None and n.utcoffset() == timedelta(hours=8), n)
    ok("now_cn 是现在", abs(n - datetime.now(timezone.utc)) < timedelta(seconds=5), n)


# ── prices ──────────────────────────────────────────────────────────────────
REQUESTS = []          # 假服务器收到的 (代码, 起, 止, 复权)
FAKE = {"drop_adjusted": False}


def fake_get_json(url, params=None):
    """腾讯日线的假服务器:工作日一根,不复权 10、后复权 30;可以模拟缺复权键。"""
    sym, _, s, e, _n, fq = params["param"].split(",")
    REQUESTS.append((sym, s, e, fq))
    d, end, day, hfq = date.fromisoformat(s), date.fromisoformat(e), [], []
    while d <= end:
        if d.weekday() < 5:
            day.append([d.isoformat(), "1", "10.0", "10.0", "10.0", "100"])
            hfq.append([d.isoformat(), "1", "30.0", "30.0", "30.0", "100"])
        d += timedelta(days=1)
    bars = {"day": day}
    if fq == "hfq" and not FAKE["drop_adjusted"] and not sym.startswith(("sz399", "sh000")):
        bars = {"hfqday": hfq}
    return {"data": {sym: bars}}


def check_prices():
    print("== prices:交易日、后复权价、未来交易日")
    ok("prices 用的是本仓的 market.py",
       Path(prices.market.__file__).resolve()
       == ROOT / "skills" / "stock-analysis-workflow" / "scripts" / "market.py",
       prices.market.__file__)

    # 计划里写定的四条
    cal = ["2026-11-02", "2026-11-03", "2026-11-04"]
    ok("第 2 个交易日", prices.nth_trading_day(cal, "2026-11-02", 2) == "2026-11-04",
       prices.nth_trading_day(cal, "2026-11-02", 2))
    ok("日历没走到返回 None", prices.nth_trading_day(cal, "2026-11-02", 5) is None,
       prices.nth_trading_day(cal, "2026-11-02", 5))
    ok("停牌顺延取前一天",
       prices.close_on_or_before({"2026-11-02": 10.0}, "2026-11-04") == ("2026-11-02", 10.0),
       prices.close_on_or_before({"2026-11-02": 10.0}, "2026-11-04"))
    d = prices.future_trading_days(date(2027, 2, 5), 2, {"2027-02-08"})
    ok("跳过周末和节假日", [x.isoformat() for x in d] == ["2027-02-05", "2027-02-09"], d)

    # 边界
    ok("第 0 个交易日 = 起点当天", prices.nth_trading_day(cal, "2026-11-03", 0) == "2026-11-03")
    wk = ["2026-11-06", "2026-11-09", "2026-11-10"]   # 周五、下周一、周二
    ok("起点是周六:从前一个交易日(周五)起数",
       prices.nth_trading_day(wk, "2026-11-07", 1) == "2026-11-09",
       prices.nth_trading_day(wk, "2026-11-07", 1))
    ok("日历从周六起取(第一天是周一):周一算第 1 个",
       prices.nth_trading_day(wk[1:], "2026-11-07", 1) == "2026-11-09",
       prices.nth_trading_day(wk[1:], "2026-11-07", 1))
    ok("n 为负数报错", raises(lambda: prices.nth_trading_day(cal, "2026-11-02", -1), ValueError) is not None)
    s = {"2026-11-04": 12.0, "2026-11-02": 10.0, "2026-11-03": 11.0}   # 故意乱序
    ok("当天有价就取当天", prices.close_on_or_before(s, "2026-11-03") == ("2026-11-03", 11.0),
       prices.close_on_or_before(s, "2026-11-03"))
    ok("起点之前没有价 → None", prices.close_on_or_before(s, "2026-11-01") is None,
       prices.close_on_or_before(s, "2026-11-01"))
    ok("空序列 → None", prices.close_on_or_before({}, "2026-11-01") is None)
    sat = prices.future_trading_days(date(2027, 2, 6), 1, set())
    ok("起点是周六 → 第一个是下周一", [x.isoformat() for x in sat] == ["2027-02-08"], sat)
    ok("n=0 → 空", prices.future_trading_days(date(2027, 2, 5), 0, set()) == [])
    long = prices.future_trading_days(date(2027, 1, 1), 250, {"2027-01-01"})
    ok("250 个里没有周末、没有节假日、严格递增",
       len(long) == 250 and all(x.weekday() < 5 for x in long)
       and date(2027, 1, 1) not in long and all(a < b for a, b in zip(long, long[1:])),
       (len(long), long[:3]))

    # 腾讯代码
    cases = {"300308": "sz300308", "002353": "sz002353", "000001": "sz000001",
             "600519": "sh600519", "688328": "sh688328", "900901": "sh900901",
             "200002": "sz200002", "515880": "sh515880", "562500": "sh562500",
             "159915": "sz159915", "160706": "sz160706", "sz399006": "sz399006",
             "SH000001": "sh000001"}
    got = {c: prices.tencent_symbol(c) for c in cases}
    ok("6 位代码 → 腾讯代码(沪 6/9/5,深 0/2/3/15/16;带前缀的原样)", got == cases, got)
    bad = [c for c in ("12345", "830799", "430047", "110059", "abcdef", "", "sz39900")
           if raises(lambda c=c: prices.tencent_symbol(c), ValueError) is None]
    ok("认不出的代码报错(北交所、债券、位数不对)", bad == [], bad)

    # 联网的两个函数:换上假服务器。时间一律传固定的 now,结果不随跑检查的时刻变
    real = prices.market._get_json
    prices.market._get_json = fake_get_json
    sh = fb_common.now_cn().tzinfo
    after = datetime(2026, 11, 20, 16, 0, tzinfo=sh)       # 收盘以后,当天那根照留
    try:
        REQUESTS.clear()
        c = prices.trading_calendar("2026-11-02", "2026-11-10", now=after)
        ok("trading_calendar 用创业板指不复权日线",
           REQUESTS and all(r[0] == "sz399006" and r[3] == "" for r in REQUESTS), REQUESTS)
        ok("trading_calendar 返回日期、去掉周末、升序",
           c == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
                 "2026-11-09", "2026-11-10"], c)

        REQUESTS.clear()
        h = prices.hfq_closes("300308", "2026-11-02", now=after)
        ok("hfq_closes 要的是 sz300308 的后复权",
           REQUESTS and all(r[0] == "sz300308" and r[3] == "hfq" for r in REQUESTS), REQUESTS)
        ok("hfq_closes 读的是后复权价(30)不是不复权价(10)",
           h and set(h.values()) == {30.0}, list(h.items())[:3])
        ok("hfq_closes 返回 {日期: 收盘},日期升序",
           isinstance(h, dict) and list(h) == sorted(h) and "2026-11-02" in h, list(h)[:3])

        ok("hfq_closes 取到 now 那天", max(h) == "2026-11-20" and REQUESTS[-1][2] == "2026-11-20",
           (max(h), REQUESTS[-1]))

        # 盘中取数:腾讯把当天的实时价当成当天那根,15:05 以前去掉
        at10 = datetime(2026, 11, 10, 10, 0, tzinfo=sh)
        c10 = prices.trading_calendar("2026-11-02", "2026-11-10", now=at10)
        ok("10:00 取日历:去掉今天 11-10,昨天 11-09 留着", c10[-1] == "2026-11-09" and "2026-11-10" not in c10, c10)
        c1530 = prices.trading_calendar("2026-11-02", "2026-11-10", now=datetime(2026, 11, 10, 15, 30, tzinfo=sh))
        ok("15:30 取日历:今天 11-10 留着", c1530[-1] == "2026-11-10", c1530)
        edge = [prices.trading_calendar("2026-11-02", "2026-11-10", now=datetime(2026, 11, 10, 15, m, tzinfo=sh))[-1]
                for m in (4, 5)]
        ok("15:04 去掉、15:05 留着", edge == ["2026-11-09", "2026-11-10"], edge)
        cnext = prices.trading_calendar("2026-11-02", "2026-11-10", now=datetime(2026, 11, 11, 10, 0, tzinfo=sh))
        ok("第二天 10:00 取到 11-10 为止:11-10 是昨天,不去掉", cnext[-1] == "2026-11-10", cnext)
        h10 = prices.hfq_closes("300308", "2026-11-02", now=at10)
        ok("10:00 取后复权:今天 11-10 去掉,前面的都在", max(h10) == "2026-11-09" and len(h10) == 6, sorted(h10))
        utc = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)     # = 北京 10:00
        ok("带别的时区的 now 换算成北京时间再比", prices.trading_calendar("2026-11-02", "2026-11-10", now=utc)[-1]
           == "2026-11-09")
        ok("drop_live_bar 不改传进来的列表", (lambda b: (prices.drop_live_bar(b, at10), b)[1])(
            [("2026-11-10", 1.0)]) == [("2026-11-10", 1.0)])

        FAKE["drop_adjusted"] = True
        e = raises(lambda: prices.hfq_closes("300308", "2026-11-02", now=after), ValueError)
        ok("缺后复权数据时报错,不拿不复权价顶替", e is not None, e)
    finally:
        prices.market._get_json = real
        FAKE["drop_adjusted"] = False


# ── baseline ────────────────────────────────────────────────────────────────
def _random_walk(seed, n=400, vol=0.02):
    """合成价格:从 100 起,每天对数收益 N(0, vol)。"""
    rng = random.Random(seed)
    px = [100.0]
    for _ in range(n):
        px.append(px[-1] * math.exp(rng.gauss(0, vol)))
    return px


HS = ("63", "126", "250")
NOV3 = date(2026, 11, 3)


def check_baseline():
    print("== baseline:不带方向的模拟、各层共用的分类")
    px = _random_walk(1)
    b1 = baseline.baseline_forecast(px, NOV3, set(), seed=7)
    b2 = baseline.baseline_forecast(px, NOV3, set(), seed=7)
    ok("同一种子结果相同", b1 == b2)
    ok("换种子结果不同", baseline.baseline_forecast(px, NOV3, set(), seed=8) != b1)
    p_up = {h: b1["horizons"][h]["p_up"] for h in HS}
    ok("不带方向:涨的种数在 47~53(三个期限都是)", all(47 <= v <= 53 for v in p_up.values()), p_up)
    ok("p_up 是整数", all(isinstance(v, int) for v in p_up.values()), p_up)
    ok("分位递增", all(list(b1["horizons"][h]["q"].values()) == sorted(b1["horizons"][h]["q"].values())
                       for h in HS), b1["horizons"])
    ok("分位键是 10/25/50/75/90", all(list(b1["horizons"][h]["q"]) == ["10", "25", "50", "75", "90"]
                                    for h in HS), b1["horizons"]["63"])
    mid = {h: b1["horizons"][h]["q"]["50"] / px[-1] for h in HS}
    ok("零漂移:中位价离起点不到 2%", all(abs(v - 1) < 0.02 for v in mid.values()), mid)
    ok("起点价 = 最后一个收盘", b1["start_price"] == px[-1], b1.get("start_price"))
    sigma = float(np.std(np.diff(np.log(px[-121:])), ddof=1))
    ok("年化波动 = 最近 120 天日对数收益标准差 × √250",
       abs(b1["vol_annual"] - sigma * math.sqrt(250)) < 1e-4, (b1["vol_annual"], sigma))
    ok("layer = baseline", b1["layer"] == "baseline")

    # 见底那一块(分类各层共用)
    bot = b1["bottom"]
    cats = baseline.categories(NOV3, set())
    ok("见底月份加起来 100", sum(bot["month_probs"].values()) == 100, bot["month_probs"])
    ok("见底月份的键 == categories 的 bottom_months(顺序也一样)",
       list(bot["month_probs"]) == cats["bottom_months"], (list(bot["month_probs"]), cats["bottom_months"]))
    ok("没有单独的 start 键", "start" not in bot["month_probs"], bot["month_probs"])
    ok("window = 126", bot["window"] == 126, bot.get("window"))
    ok("价位分段加起来 100", sum(p for _, _, p in bot["price_bins"]) == 100, bot["price_bins"])
    ok("价位分段 == categories 的 price_bins",
       [[lo, hi] for lo, hi, _ in bot["price_bins"]] == cats["price_bins"], bot["price_bins"])
    ok("最低价分位递增、都不高于起点",
       list(bot["price_q"].values()) == sorted(bot["price_q"].values())
       and max(bot["price_q"].values()) <= px[-1], bot["price_q"])

    # categories 本身
    ok("categories 三个键", sorted(cats) == ["bottom_months", "buy_months", "price_bins"], cats)
    ok("见底月份:2026-11 起到 2027-04(126 个工作日)",
       cats["bottom_months"] == ["2026-11", "2026-12", "2027-01", "2027-02", "2027-03", "2027-04"],
       cats["bottom_months"])
    ok("买点月份:2026-11 到 2027-10 共 12 个月,最后是 later",
       cats["buy_months"][0] == "2026-11" and cats["buy_months"][-2] == "2027-10"
       and cats["buy_months"][-1] == "later" and len(cats["buy_months"]) == 13, cats["buy_months"])
    ok("价位分段固定六段",
       cats["price_bins"] == [[0.0, 0.5], [0.5, 0.6], [0.6, 0.7], [0.7, 0.8], [0.8, 0.9], [0.9, 1.0]],
       cats["price_bins"])
    c2 = baseline.categories(date(2027, 1, 29), {"2027-01-29"}, bottom_window=3, buy_horizon=3)
    ok("节假日传得进去:起点那天放假 → 第一个月是下个月",
       c2["bottom_months"] == ["2027-02"] and c2["buy_months"] == ["2027-02", "later"], c2)

    # 第 k 步对哪一天、起点最低并进第一个月、价位分段的边界 —— 手造 5 条路径
    # 2026-11-27 周五起 3 个交易日:11-27、11-30、12-01
    paths = np.array([[101.0, 102.0, 103.0],    # 起点最低 → 11 月;比值 1.0 → 最后一段
                      [99.0, 98.0, 97.0],       # 第 3 步最低 = 12-01 → 12 月;0.97
                      [55.0, 70.0, 80.0],       # 第 1 步 = 11-27 → 11 月;0.55
                      [100.5, 40.0, 120.0],     # 第 2 步 = 11-30 → 11 月;0.40
                      [60.0, 70.0, 80.0]])      # 第 1 步 → 11 月;正好 0.6 → [0.6, 0.7] 段
    hb = baseline.bottom_block(paths, 100.0, date(2026, 11, 27), set(), 3)
    ok("手造路径:11 月 80、12 月 20(起点最低并进 11 月,第 k 步对第 k 个交易日)",
       hb["month_probs"] == {"2026-11": 80, "2026-12": 20}, hb["month_probs"])
    ok("手造路径:价位分段 20/20/20/0/0/40(0.6 归 [0.6,0.7],1.0 归最后一段)",
       [p for _, _, p in hb["price_bins"]] == [20, 20, 20, 0, 0, 40], hb["price_bins"])
    ok("路径比窗口短报错,不悄悄少看几天",
       raises(lambda: baseline.bottom_block(paths, 100.0, date(2026, 11, 27), set(), 4), ValueError)
       is not None)

    # 随机数的约定(系统算法要逐位复现)
    ok("draw_normals = default_rng(seed).standard_normal((n_paths, steps))",
       np.array_equal(baseline.draw_normals(7, 5, 3), np.random.default_rng(7).standard_normal((5, 3))))
    small = baseline.baseline_forecast(px, NOV3, set(), n_paths=500, seed=7)
    z = np.random.default_rng(7).standard_normal((500, 250))
    manual = px[-1] * np.exp(np.cumsum(sigma * z, axis=1))
    want = {h: int(np.floor(100 * np.mean(manual[:, int(h) - 1] > px[-1]) + 0.5)) for h in HS}
    got = {h: small["horizons"][h]["p_up"] for h in HS}
    ok("手算(同一种子、零漂移、第 h 列)的涨的种数和函数一致", got == want, (got, want))

    # 最大余数法
    lr = baseline.largest_remainder
    ok("最大余数法:[1,1,1] → [34,33,33]", lr([1, 1, 1]) == [34, 33, 33], lr([1, 1, 1]))
    ok("最大余数法:[2,1] → [67,33]", lr([2, 1]) == [67, 33], lr([2, 1]))
    ok("最大余数法:[0,5] → [0,100]", lr([0, 5]) == [0, 100], lr([0, 5]))
    rr = random.Random(3)
    sums = {sum(lr([rr.randint(0, 50) for _ in range(rr.randint(1, 12))] + [1])) for _ in range(200)}
    ok("最大余数法:随机 200 组都加起来 100", sums == {100}, sums)
    ok("最大余数法:全是 0 报错", raises(lambda: lr([0, 0]), ValueError) is not None)

    # 输入不对就报错
    ok("收盘不够 lookback+1 个报错",
       raises(lambda: baseline.baseline_forecast(px[:120], NOV3, set()), ValueError) is not None)
    ok("有 0 价报错",
       raises(lambda: baseline.baseline_forecast(px[:-1] + [0.0], NOV3, set()), ValueError) is not None)


# ── analogs ─────────────────────────────────────────────────────────────────
def _days(n):
    return [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]


def _ep(day, dtb, bdrop, complete=True, back=None, f63=None):
    """手造一段(summarize 的用例用)。"""
    return {"date": day, "dd": 0.5, "fwd": {"63": f63, "126": None, "250": None},
            "days_to_bottom": dtb, "bottom_drop": bdrop, "days_back": back,
            "max_in_window": 0.1, "min_in_window": -0.1, "complete": complete}


def check_analogs():
    print("== analogs:跌破一年高点 50% 的历史情形")
    # 计划里的样例:260 天平、60 天从 100 跌到 41、再从 40 起每天涨 0.5
    crash = [100.0] * 260 + [100 - i for i in range(60)] + [40 + i * 0.5 for i in range(200)]
    ds = _days(len(crash))
    eps = analogs.drawdown_episodes(ds, crash, threshold=0.5)
    ok("一段大跌只算一次", len(eps) == 1, eps)
    e = eps[0] if eps else {}
    # 跌破 50% 在第 310 根(价 50);最低 40.0 是反弹段的第一根,在第 320 根
    ok("见底天数", e.get("days_to_bottom") == 10, e.get("days_to_bottom"))
    ok("跌破那天", e.get("date") == ds[310], e.get("date"))
    ok("跌破那天离一年高点跌了 50%", abs(e.get("dd", 0) - 0.5) < 1e-9, e.get("dd"))
    ok("最低点离一年高点跌了 60%", abs(e.get("bottom_drop", 0) - 0.6) < 1e-9, e.get("bottom_drop"))
    ok("窗口里最低比跌破价低 20%", abs(e.get("min_in_window", 0) + 0.2) < 1e-9, e.get("min_in_window"))
    ok("窗口里最高比跌破价高 96%(第 436 根 98)", abs(e.get("max_in_window", 0) - 0.96) < 1e-9,
       e.get("max_in_window"))
    f = e.get("fwd", {})
    ok("63 天后涨 33%、126 天后涨 96%、250 天后数据不够 → None",
       abs((f.get("63") or 0) - 0.33) < 1e-9 and abs((f.get("126") or 0) - 0.96) < 1e-9
       and f.get("250", 1) is None, f)
    ok("回到跌破价用了 30 天(第 340 根回到 50)", e.get("days_back") == 30, e.get("days_back"))
    ok("观察窗口满了", e.get("complete") is True, e.get("complete"))

    stay = [100.0] * 260 + [100 - i for i in range(60)] + [40.0] * 300
    n_stay = len(analogs.drawdown_episodes(_days(len(stay)), stay))
    ok("跌破后一直趴在线下超过 gap 天,也只算一次", n_stay == 1, n_stay)

    two = crash + [140.0] * 260 + [140 - 2 * i for i in range(60)]
    e2 = analogs.drawdown_episodes(_days(len(two)), two)
    ok("两次真的大跌算两段,第二段在第 815 根",
       len(e2) == 2 and e2[1]["date"] == _days(len(two))[815], [x["date"] for x in e2])

    wob = [100.0] * 260 + [100 - i for i in range(52)] + [60.0] * 10 + [45.0] * 10 + [60.0] * 100
    n_wob = len(analogs.drawdown_episodes(_days(len(wob)), wob))
    n_wob5 = len(analogs.drawdown_episodes(_days(len(wob)), wob, gap=5))
    ok("12 天里跌破两次,gap=120 算一次、gap=5 算两次", (n_wob, n_wob5) == (1, 2), (n_wob, n_wob5))

    pre = [100.0] * 200 + [40.0] * 200
    n_pre = len(analogs.drawdown_episodes(_days(len(pre)), pre))
    ok("数据开头就已经在线下(看不到哪天跌破的)不算", n_pre == 0, n_pre)

    cut = crash[:361]
    ec = analogs.drawdown_episodes(_days(len(cut)), cut)
    ok("最近的一段:窗口没满标 complete=False,63 天后还没到 → None",
       len(ec) == 1 and ec[0]["complete"] is False and ec[0]["fwd"]["63"] is None
       and ec[0]["days_to_bottom"] == 10, ec)
    edge = analogs.drawdown_episodes(_days(437), crash[:437])
    ok("最后一根正好是跌破后第 126 天 → 窗口算满", len(edge) == 1 and edge[0]["complete"] is True, edge)
    ok("不满 lookback 天 → 空", analogs.drawdown_episodes(_days(100), [1.0] * 100) == [])
    ok("日期和收盘个数不一样报错",
       raises(lambda: analogs.drawdown_episodes(_days(3), [1.0, 2.0]), ValueError) is not None)

    # summarize:条件子集 = 见底不早于 already_days、最深不浅于 already_drop、窗口满了
    hand = [_ep("2018-01-02", 3, 0.55, back=10, f63=0.2),     # 见底太早 → 排除
            _ep("2019-03-04", 40, 0.70, back=80, f63=-0.1),   # 留下
            _ep("2020-05-06", 90, 0.58, f63=0.3),             # 没跌这么深 → 排除
            _ep("2021-07-08", 21, 0.60),                      # 正好等于两条线 → 留下
            _ep("2026-10-08", 30, 0.65, complete=False)]      # 两条线都过了,但窗口没满 → 排除
    s = analogs.summarize_episodes(hand, already_days=21, already_drop=0.6)
    ok("全部段数 5、窗口满了的 4", s["n"] == 5 and s["n_complete"] == 4, s)
    ok("条件子集:2019 那段和正好压线的 2021 那段",
       s["given"]["n"] == 2 and s["given"]["dates"] == ["2019-03-04", "2021-07-08"], s["given"])
    ok("全部那组按见底用时分桶(21 天算一个月内,没满窗口的不算)",
       s["all"]["days_to_bottom"]["buckets"] == {"一周内": 1, "一周到一个月": 1, "一到三个月": 1, "三个月以上": 1},
       s["all"]["days_to_bottom"])
    ok("63 天后:3 段有数,2 段涨", s["all"]["fwd"]["63"]["n"] == 3 and s["all"]["fwd"]["63"]["up"] == 2,
       s["all"]["fwd"])
    note = s["sample_note"]
    ok("sample_note 写了段数和幸存者偏差",
       "5 段" in note and "只看了这几只股票" in note and "后来成了龙头" in note and "幸存者偏差" in note, note)
    ok("sample_note 写了条件子集几段", "有 2 段" in note, note)
    coded = [{**x, "code": "000001"} for x in hand]
    ok("段上带 code 时 sample_note 列出代码",
       "000001" in analogs.summarize_episodes(coded, 21, 0.6)["sample_note"])
    ok("already_drop 不在 0~1 报错",
       raises(lambda: analogs.summarize_episodes(hand, 20, 55), ValueError) is not None)
    ok("already_days 负数报错",
       raises(lambda: analogs.summarize_episodes(hand, -1, 0.5), ValueError) is not None)
    empty = analogs.summarize_episodes([], 0, 0.5)
    ok("一段都没有也能出结果", empty["n"] == 0 and "0 段" in empty["sample_note"], empty)


CHECKS = [check_fb_common, check_prices, check_baseline, check_analogs]


def main():
    for fn in CHECKS:
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
