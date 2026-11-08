#!/usr/bin/env python3
"""check_board_cli —— 预测看板命令入口 board.py(各子命令)和每日检查定时任务脚本的检查。

用法:
    python3 scripts/check_board_cli.py        # 退出码 0 才算过

不联网、不碰真实数据、不调真的 claude / codex、不碰 ~/Library/LaunchAgents:
  - 每块在临时目录里搭一个工作台(STOCK_LAB 指过去),配置、存档、决策日志、页面都写在那里;
  - 行情、取数器、AI 答卷用 board_io 认的四个环境变量换成假的(BOARD_NOW / BOARD_FAKE_PRICES /
    BOARD_FAKE_INPUTS / BOARD_FAKE_AI),数都是合成的(复用 check_board_pages 的那套合成行情);
  - 定时任务脚本只跑「打印」那一档,HOME 指到临时目录。
写法和 check_board_pages.py 一样:每块一个函数,登记进 CHECKS。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FB = ROOT / "skills" / "forecast-board"
BOARD = FB / "scripts" / "board.py"
INSTALL = FB / "scripts" / "install_daily_check.sh"
sys.path.insert(0, str(FB / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

import fb_common  # noqa: E402,F401  先于 prices / baseline,和正式运行的顺序一样
import attribution  # noqa: E402
import baseline  # noqa: E402
import prices  # noqa: E402
import check_board_pages as pages  # noqa: E402  合成行情、合成答卷

FAILS = 0
HOL = pages.HOL
CODES = pages.CODES
NOW = "2026-11-02T16:00:00"          # 周一收盘以后
LATER_NOW = "2027-03-01T16:00:00"    # 3 个月题已到期
FAKE_ENV = ("BOARD_NOW", "BOARD_FAKE_PRICES", "BOARD_FAKE_INPUTS", "BOARD_FAKE_AI")


def ok(name, cond, got=""):
    global FAILS
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"\n       得到:{got!r}"[:600]))
    if not cond:
        FAILS += 1


# ── 合成数据 ────────────────────────────────────────────────────────────────
DATES = pages._days_back(date.fromisoformat(pages.AS_OF), 500)
QFQ = {"300308": pages._series(500, pages.CLOSE["300308"], 35.0, 0.2, 0.0008),
       "300502": pages._series(500, pages.CLOSE["300502"], 28.0, 0.25, -0.0004)}
IDX = pages._series(500, 2000.0, 50.0, 0.1, 0.0)


def fake_items() -> dict:
    return json.loads(json.dumps(pages._items(DATES, QFQ, IDX)))


def fake_prices(extra: dict | None = None) -> dict:
    """{"index", "hfq", "close"};extra = {"index": [[d, c]], "close": {代码: [[d, c]]}, "hfq": {...}} 接在后面。"""
    out = {"index": [[d, c] for d, c in zip(DATES, IDX)],
           "hfq": {c: [[d, round(x * pages.HFQ_K[c], 2)] for d, x in zip(DATES, QFQ[c])] for c in CODES},
           "close": {c: [[d, x] for d, x in zip(DATES, QFQ[c])] for c in CODES}}
    for k, v in (extra or {}).items():
        if k == "index":
            out["index"] += v
        else:
            for c, rows in v.items():
                out[k][c] = out[k].get(c, []) + rows
    return out


def later_bars() -> dict:
    """2026-11-02 到 2027-06-30 的合成日线:300308 一路跌到 250,300502 涨到 200,指数涨到 2100。"""
    days, d = [], date(2026, 11, 2)
    while d <= date(2027, 6, 30):
        if d.weekday() < 5 and d.isoformat() not in HOL:
            days.append(d.isoformat())
        d += timedelta(days=1)
    n = len(days)
    path = {"300308": (330.0, 250.0), "300502": (150.0, 200.0)}
    close = {c: [[day, round(a + (b - a) * (i + 1) / n, 2)] for i, day in enumerate(days)] for c, (a, b) in path.items()}
    return {"index": [[day, round(2000 + 100 * (i + 1) / n, 2)] for i, day in enumerate(days)], "close": close,
            "hfq": {c: [[day, round(x * pages.HFQ_K[c], 2)] for day, x in rows] for c, rows in close.items()}}


def fake_answers(claude_502: int = 40, dup_zone: str | None = None) -> dict:
    """两份合成答卷。claude_502 = Claude 给 300502 的 3 个月涨的种数;
    dup_zone = 给这个作者多一个和第一个买入价位只差 1e-7 的价位:价不同,答卷过得了校验;
               换成比值(6 位小数)一样,拆题时题号撞了 → 这一层整层没出题。"""
    start = prices.future_trading_days(date.fromisoformat(pages.AS_OF) + timedelta(days=1), 1, set(HOL))[0]
    cats = baseline.categories(start, set(HOL))
    p63 = {"300502": (claude_502, 58), "300308": (52, 49)}
    acts = {"300502": (("wait", "合成:先等"), ("buy_batches", "合成:分两批买")),
            "300308": (("hold", "合成:拿着"), ("trim", "合成:减一点"))}
    out = {a: {"stocks": {c: pages._ai_block(c, cats, p63[c][i], *acts[c][i]) for c in CODES}}
           for i, a in enumerate(("claude", "codex"))}
    if dup_zone:
        zones = out[dup_zone]["stocks"]["300308"]["buy"]["zones"]
        zones.append([zones[0][0] + 1e-7, zones[0][1]])
    return out


# ── 临时工作台 ──────────────────────────────────────────────────────────────
class Lab:
    def __init__(self, tmp: Path, sell_lines: dict | None = None):
        self.tmp, self.lab, self.out = tmp, tmp / "lab", tmp / "pages"
        cfg = self.lab / "private" / "config"
        cfg.mkdir(parents=True)
        (tmp / "dash").mkdir()
        board = {"stocks": [{"code": "300502", "name": "合成乙", "sector": "AI算力链"},
                            {"code": "300308", "name": "合成甲", "sector": "AI算力链"}],
                 "peers": [["300308", "300502"]], "sell_lines": sell_lines or {"300308": 100.0},
                 "sell_ratio": {"300308": 2, "300502": 1}, "holidays_2027": HOL, "n_paths": 300,
                 "history_since": "2015-01-01"}
        local = {"hosts": {socket.gethostname(): {"board_out_dir": str(self.out), "dashboard_dir": str(tmp / "dash")}}}
        (cfg / "board.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
        (cfg / "local.json").write_text(json.dumps(local), encoding="utf-8")
        self.config = board
        self.env = {k: v for k, v in os.environ.items() if k not in FAKE_ENV and k != "PYTHONPATH"}
        self.env.update(STOCK_LAB=str(self.lab), BOARD_NOW=NOW)

    @property
    def root(self) -> Path:
        return self.lab / "data" / "forecasts"

    def put(self, name: str, obj) -> str:
        p = self.tmp / name
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def fakes(self, prices_obj=None, inputs_obj=None, ai_obj=None):
        if prices_obj is not None:
            self.env["BOARD_FAKE_PRICES"] = self.put("prices.json", prices_obj)
        if inputs_obj is not None:
            self.env["BOARD_FAKE_INPUTS"] = self.put("inputs.json", inputs_obj)
        if ai_obj is not None:
            self.env["BOARD_FAKE_AI"] = self.put("ai.json", ai_obj)

    def run(self, *args, now: str | None = None, timeout: int = 300):
        env = dict(self.env, **({"BOARD_NOW": now} if now else {}))
        p = subprocess.run([sys.executable, str(BOARD), *args], capture_output=True, text=True, env=env,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def runs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(r for d in self.root.iterdir() if d.is_dir() and d.name[:2] == "20" for r in d.iterdir()
                      if r.is_dir())

    def jsonl(self, name: str) -> list[dict]:
        return attribution._read_lines(self.root / name)

    def status(self) -> dict:
        p = self.root / "status.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="check-cli-"))


# ── 检查 ────────────────────────────────────────────────────────────────────
def check_check_sell_line():
    """brief 第 1 步:收盘跌破卖出线 → status.json 的 triggered 里有含卖出线数字的一条,退出码 0(合成数字)。"""
    tmp = _tmp()
    try:
        lab = Lab(tmp, sell_lines={"300308": 100.0})
        lab.fakes(prices_obj={"index": [], "hfq": {},
                              "close": {"300308": [["2026-10-30", 98.0], ["2026-11-02", 95.0]],
                                        "300502": [["2026-11-02", 150.0]]}})
        rc, out, err = lab.run("check", "--no-notify")
        st = lab.status()
        ok("check:退出码 0", rc == 0, (rc, out, err))
        hit = [t for t in st.get("triggered") or [] if "100" in f"{t.get('what')}{t.get('detail')}"]
        ok("check:triggered 里有含卖出线 100 的一条,代码对", len(hit) == 1 and hit[0]["code"] == "300308", st)
        ok("check:status.json 照约定有 checked_at / triggered / due", {"checked_at", "triggered", "due"} <= set(st)
           and st["due"] == [] and st["checked_at"].startswith("2026-11-02"), st)
        lab.fakes(prices_obj={"index": [], "hfq": {}, "close": {"300308": [["2026-11-02", 101.0]],
                                                               "300502": [["2026-11-02", 150.0]]}})
        rc, out, err = lab.run("check", "--no-notify")
        ok("check:没跌破就不触发", rc == 0 and lab.status().get("triggered") == [], (rc, lab.status(), err))
        # 两只都取不到(假行情里日线不是 [[日期, 价]],取数器抛错):status.json 照写,写明哪两只没查成
        lab.fakes(prices_obj={"index": [], "hfq": {}, "close": {"300308": "坏的", "300502": "坏的"}})
        rc, out, err = lab.run("check", "--no-notify", now="2026-11-03T16:00:00")
        st = lab.status()
        ok("check:全部取不到 → 退出码 1,status.json 照写:没触发、两只都列进 unchecked、日期是这次的",
           rc == 1 and "Traceback" not in err and st.get("triggered") == [] and st.get("checked_at", "").startswith("2026-11-03")
           and sorted(u["code"] for u in st.get("unchecked") or []) == ["300308", "300502"]
           and all("收盘价取不到" in u["what"] for u in st["unchecked"]), (rc, st, err[-400:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    tmp = _tmp()
    try:        # 一只取不到,另一只照常比:它跌破卖出线不能被丢掉(合成数字)
        lab = Lab(tmp, sell_lines={"300502": 200.0, "300308": 100.0})
        lab.fakes(prices_obj={"index": [], "hfq": {}, "close": {"300308": "坏的", "300502": [["2026-11-02", 150.0]]}})
        rc, out, err = lab.run("check", "--no-notify")
        st = lab.status()
        ok("check:一只取不到 → 退出码 1,另一只跌破卖出线照写进 triggered 和输出",
           rc == 1 and [t["code"] for t in st.get("triggered") or []] == ["300502"]
           and "200" in st["triggered"][0]["what"] and "300502 收盘跌破卖出线 200" in out, (rc, st, out, err[-300:]))
        ok("check:取不到的那只写进 unchecked(代码、哪一项、原因),stderr 也写明",
           [u["code"] for u in st.get("unchecked") or []] == ["300308"] and st["unchecked"][0]["detail"]
           and "没查成:300308 收盘价取不到" in err, (st.get("unchecked"), err[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_notify_text():
    import board_settle
    s = board_settle.osa_script('卖出线 "100" \\ 换行\n后')
    ok("通知:引号和反斜杠转义、换行变空格", s == 'display notification "卖出线 \\"100\\" \\\\ 换行 后" '
       'with title "预测看板"', s)
    msg = board_settle.note_text([{"code": "300308", "what": "收盘跌破卖出线 100"}], ["a", "b"], [])
    ok("通知:写出触发的条件和到期题数", "300308 收盘跌破卖出线 100" in msg and "2 道题到期" in msg, msg)
    msg = board_settle.note_text([{"code": "300502", "what": "收盘跌破卖出线 200"}], [],
                                 [{"code": "300308", "what": "收盘价取不到", "detail": "x"},
                                  {"code": "", "what": "到期题没查成", "detail": "y"}])
    ok("通知:触发的和没查成的一起写", "300502 收盘跌破卖出线 200" in msg and "没查成:300308、到期题" in msg, msg)
    pc = board_settle.price_condition
    cases = [("合成:收盘跌破 297", ("down", 297.0)), ("收盘价低于 297.5 元", ("down", 297.5)),
             ("收盘跌破 60 日线", None), ("创业板指收盘站上 2000 点", None),
             ("Q4 净利润 <47.5亿", None), ("收盘跌破 280 或者站上 360", None), ("券商盈利预测转为调高", None)]
    bad = [(t, pc(t), want) for t, want in cases if pc(t) != want]
    ok("改主意条件:只认一句话里只有一个本股价位的条件", not bad, bad)
    multi = ["连续两天收盘站上 360", "周线收盘跌破 280", "收盘连续 3 天低于 300", "3 个交易日收盘都在 300 以下"]
    bad = [(t, pc(t)) for t in multi if pc(t) is not None]
    ok("改主意条件:要看不止一天的收盘(连续几天、周线)→ 交给人看,不拿一天的收盘报「已触发」", not bad, bad)


def check_inputs_check():
    tmp = _tmp()
    try:
        lab = Lab(tmp)
        lab.fakes(inputs_obj=fake_items())
        rc, out, err = lab.run("inputs", "--check")
        ok("inputs --check:只缺还没工具的指标 → 退出码 0", rc == 0, (rc, out[-400:], err[-400:]))
        ok("inputs --check:打印每项状态和缺什么", "prices.300308" in out and "取到" in out
           and "还没有取数工具" in out and "海外同业股价" in out, out[-600:])
        ok("inputs --check:不出预测、不写存档", not lab.runs())
        bad = fake_items()
        bad["prices.300308"] = {"raise": "合成:接口超时"}
        lab.fakes(inputs_obj=bad)
        rc, out, err = lab.run("inputs", "--check", "300308")
        ok("inputs --check:必需的日线取不到 → 退出码 1,写出是哪项", rc == 1 and "取不到(必需)" in out
           and "300308" in out, (rc, out[-600:]))
        lab.fakes(inputs_obj=fake_items())
        rc, out, err = lab.run("inputs", "--check", "600000")
        ok("inputs --check:没配行业的代码 → 退出码 1", rc == 1 and "没配行业" in out, (rc, out[-400:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_recompute():
    """recompute 只读 --inputs 那个文件,stdout 只有一段 JSON;缺 run_meta 报清楚。"""
    import board_layers
    import replay
    tmp = _tmp()
    try:
        meta = {"as_of": pages.AS_OF, "start": "2026-11-02", "codes": list(CODES), "holidays": HOL,
                "horizons": [63, 126, 250], "bottom_window": 126, "buy_horizon": 250, "n_paths": 200,
                "lookback": 120, "validated": {}}
        f = tmp / "inputs.json"
        f.write_text(json.dumps({"items": fake_items(), "run_meta": meta}, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("STOCK_LAB", "PYTHONPATH", *FAKE_ENV)}
        env["STOCK_LAB"] = str(tmp / "没有这个目录")
        p = subprocess.run([sys.executable, str(BOARD), "recompute", "--inputs", str(f), "--seed", "7"],
                           capture_output=True, text=True, env=env, timeout=120)
        ok("recompute:退出码 0", p.returncode == 0, p.stderr[-600:])
        try:
            got = replay.parse_recompute(p.stdout)
        except ValueError:
            got = {}
        ok("recompute:stdout 整段是一段 JSON {baseline, system}(进度不许混进来),每层两只",
           set(got) == {"baseline", "system"} and all(set(got[k]) == set(CODES) for k in got), p.stdout[:200])
        ok("recompute:统计底线带买点那块", all("buy" in got["baseline"][c] and "start_price" in got["baseline"][c]
                                              for c in CODES) if got else False)
        same = board_layers.compute_layers(fake_items(), meta, 7)
        ok("recompute:和进程内 compute_layers 逐位相同", json.loads(json.dumps(same)) == got)
        flat = tmp / "flat.json"
        flat.write_text(json.dumps(fake_items(), ensure_ascii=False), encoding="utf-8")
        p = subprocess.run([sys.executable, str(BOARD), "recompute", "--inputs", str(flat), "--seed", "7"],
                           capture_output=True, text=True, env=env, timeout=120)
        ok("recompute:老的平铺输入包(没有 run_meta)→ 报清楚,stdout 空", p.returncode != 0 and not p.stdout.strip()
           and "run_meta" in p.stderr, (p.returncode, p.stdout[:100], p.stderr[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _forecast_lab(tmp: Path) -> Lab:
    lab = Lab(tmp)
    lab.fakes(prices_obj=fake_prices(), inputs_obj=fake_items(), ai_obj=fake_answers(claude_502=10))
    return lab


def check_forecast(lab: Lab):
    notes = lab.tmp / "查证.md"
    notes.write_text("# 合成查证笔记\n\n- 合成:某公司财报要点(出处:合成链接)\n", encoding="utf-8")
    rc, out, err = lab.run("forecast", "--notes", str(notes))
    ok("forecast:退出码 0", rc == 0, (rc, out[-600:], err[-1200:]))
    runs = lab.runs()
    ok("forecast:建了一次存档", len(runs) == 1, runs)
    if not runs:
        return None
    run = runs[0]
    names = sorted(p.name for p in (run / "answers").iterdir())
    ok("forecast:四份答卷", names == ["baseline.json", "claude.json", "codex.json", "system.json"], names)
    raw = json.loads((run / "inputs.json").read_text(encoding="utf-8"))
    meta = raw.get("run_meta") or {}
    need = {"as_of", "start", "codes", "holidays", "horizons", "bottom_window", "buy_horizon", "n_paths",
            "lookback", "validated", "start_close", "p0_hfq"}
    ok("forecast:inputs.json 是 {items, run_meta, missing_required},run_meta 带全",
       set(raw) >= {"items", "run_meta", "missing_required"} and need <= set(meta), sorted(meta))
    ok("forecast:as_of = 最后一个收盘,start = 下一个交易日", meta.get("as_of") == pages.AS_OF
       and meta.get("start") == "2026-11-02", (meta.get("as_of"), meta.get("start")))
    ok("forecast:起点价是不复权收盘", meta.get("start_close") == pages.CLOSE, meta.get("start_close"))
    nt = raw["items"].get("notes") or {}
    ok("forecast:查证笔记作为 used_by=ai 的一项放进输入包", nt.get("used_by") == "ai" and "合成查证笔记" in str(nt.get("value"))
       and nt.get("status") == "ok", nt)
    ok("forecast:缺什么照存(只有还没工具的指标)", raw["missing_required"]
       and all(m.startswith("gap:") for m in raw["missing_required"]), raw["missing_required"])
    b = json.loads((run / "answers" / "baseline.json").read_text(encoding="utf-8"))
    ok("forecast:统计底线每只都有 start_price 和买点",
       all("start_price" in b["stocks"][c] and b["stocks"][c]["buy"]["month_probs"] for c in CODES), list(b["stocks"]))
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    ok("forecast:manifest 记了种子和提交号", isinstance(man.get("seed"), int) and man.get("git_head"), man.get("seed"))
    qs = json.loads((run / "questions.json").read_text(encoding="utf-8"))
    ids = [q["id"] for q in qs["questions"]]
    ok("forecast:四层的题 + 买点规则题,题号不重复", len(ids) == len(set(ids))
       and {q["author"] for q in qs["questions"]} == {"baseline", "system", "claude", "codex", "rule"}, len(ids))
    ok("forecast:没出题的买入价位记下来", isinstance(qs.get("skipped"), list))
    jr = [json.loads(x) for x in (lab.lab / "data" / "journal.jsonl").read_text(encoding="utf-8").splitlines() if x]
    ok("forecast:决策日志每只一条,写着存档编号", len(jr) == 2 and all(r["thesis"].startswith("预测存档 ") for r in jr)
       and {r["code"] for r in jr} == set(CODES), jr)
    jf = run / "journal.json"
    ok("forecast:存档里记下决策日志编号(journal.json),两边互相指", jf.is_file()
       and json.loads(jf.read_text(encoding="utf-8")).get("ids") == [r["id"] for r in jr], jf.is_file())
    end63 = prices.future_trading_days(date.fromisoformat(meta["start"]), 63, set(meta["holidays"]))[-1].isoformat()
    ok("forecast:决策日志复核日 = 这次 3 个月题的到期日(从起点日数,不从今天数)",
       all(r["check_date"] == end63 for r in jr), (end63, [r["check_date"] for r in jr]))
    pg = sorted(p.name for p in lab.out.iterdir()) if lab.out.is_dir() else []
    ok("forecast:画了三个页面", pg == sorted(["看板.html", "成绩.html", "看板_手机版.html"]), pg)
    ok("forecast:输出写了存档目录和决策日志编号", str(run.name) in out and "#" in out, out[-400:])
    # recompute 用存档里的 inputs.json 和种子,逐位复现两层(旧算法 + 原输入)
    p = subprocess.run([sys.executable, str(BOARD), "recompute", "--inputs", str(run / "inputs.json"),
                        "--seed", str(man["seed"])], capture_output=True, text=True, timeout=120,
                       env={k: v for k, v in lab.env.items() if k != "STOCK_LAB"})
    got = json.loads(p.stdout) if p.returncode == 0 else {}
    s = json.loads((run / "answers" / "system.json").read_text(encoding="utf-8"))
    ok("forecast + recompute:存档的两层能逐位复现", got.get("baseline") == b["stocks"]
       and got.get("system") == s["stocks"], p.stderr[-400:])
    return run


def check_forecast_refuses(lab: Lab):
    before = lab.runs()
    rc, out, err = lab.run("forecast", now="2026-11-02T10:30:00")
    ok("forecast:交易日盘中拒绝(日线最后一根是实时价)", rc == 2 and "盘中" in err and lab.runs() == before, (rc, err))
    bad = fake_items()
    bad["prices.300308"] = {"raise": "合成:接口超时"}
    lab.fakes(inputs_obj=bad)
    rc, out, err = lab.run("forecast")
    ok("forecast:日线这次没取到(沿用旧值)→ 不出预测,退出码 1", rc == 1 and "prices.300308" in err
       and lab.runs() == before, (rc, err[-400:]))
    # 数据源缓存没更新:后复权少了最后一天、前复权有。起点价(前复权)和结算基准(后复权)会是两个不同日子的价
    lag = fake_items()
    lag["prices.300308"]["value"]["hfq"] = lag["prices.300308"]["value"]["hfq"][:-1]
    lab.fakes(inputs_obj=lag)
    rc, out, err = lab.run("forecast")
    ok("forecast:后复权、前复权最后一天对不上 → 不出预测,退出码 1,写明哪只、各是哪天",
       rc == 1 and "prices.300308" in err and "对不上" in err and lag["prices.300308"]["value"]["hfq"][-1][0] in err
       and lab.runs() == before, (rc, err[-400:]))
    lab.fakes(inputs_obj=fake_items())


def check_add_question(lab: Lab):
    args = ["add-question", "--code", "300308", "--type", "above_on", "--ratio", "1.05", "--day", "2027-01-29",
            "--prob", "60", "--author", "claude", "--note", "合成:聊天里说的"]
    rc, out, err = lab.run(*args)
    rows = lab.jsonl("chat_questions.jsonl")
    ok("add-question:写入 1 道题,退出码 0", rc == 0 and len(rows) == 1, (rc, out, err[-400:]))
    q = rows[0] if rows else {}
    ok("add-question:开题日 = 最后一个收盘,起点后复权价、概率、来源都在",
       q.get("opened") == pages.AS_OF and q.get("forecast") == {"pct": 60} and q.get("source") == "chat"
       and q.get("p0_hfq") == round(pages.CLOSE["300308"] * pages.HFQ_K["300308"], 2) and q.get("note"), q)
    ok("add-question:题号按 questions.make_question 的规则", q.get("id") == "chat/2026-10-30/claude/300308/above_on/r1.05-d2027-01-29",
       q.get("id"))
    ok("add-question:记下开题日的不复权收盘 p0_close(成绩页写价位线用)", q.get("p0_close") == pages.CLOSE["300308"], q)
    rc, out, err = lab.run(*args)
    ok("add-question:同号的题再加一次报错,不多写", rc != 0 and len(lab.jsonl("chat_questions.jsonl")) == 1
       and "已经有" in err, (rc, err[-300:]))
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "touch", "--ratio", "0.8", "--n", "63",
                           "--prob", "120", "--author", "codex", "--note", "合成")
    ok("add-question:概率超出 0~100 报错", rc != 0 and len(lab.jsonl("chat_questions.jsonl")) == 1, (rc, err[-200:]))
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "multiple_by", "--k", "2", "--n", "250",
                           "--end", "2027-06-30", "--prob", "10", "--author", "codex", "--note", "合成")
    ok("add-question:--n 和 --end 只能写一个", rc != 0 and len(lab.jsonl("chat_questions.jsonl")) == 1, rc)
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "multiple_by", "--ratio", "2", "--n", "250",
                           "--prob", "10", "--author", "codex", "--note", "合成")
    ok("add-question:倍数题写成 --ratio 报错(不悄悄忽略)", rc != 0 and "--k" in err
       and len(lab.jsonl("chat_questions.jsonl")) == 1, (rc, err[-200:]))
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "above_on", "--ratio", "1.1",
                           "--day", "2026-10-30", "--prob", "50", "--author", "codex", "--note", "合成")
    ok("add-question:日子不晚于开题日报错", rc != 0 and len(lab.jsonl("chat_questions.jsonl")) == 1, (rc, err[-200:]))
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "touch", "--ratio", "0.8", "--n", "63",
                           "--prob", "30", "--author", "codex", "--note", "合成:往下碰")
    ok("add-question:往下碰价题(方向按比值定)", rc == 0 and lab.jsonl("chat_questions.jsonl")[-1]["params"].get("dir") == "down",
       (rc, err[-300:]))
    rc, out, err = lab.run("add-question", "--code", "300308", "--type", "above_on", "--ratio", "1.05",
                           "--day", "2027-01-29", "--opened", "2026-08-12", "--prob", "70", "--author", "claude",
                           "--note", "合成:由旧记录 #9 换算")
    q = lab.jsonl("chat_questions.jsonl")[-1]
    ok("add-question:开题日早于第一次存档 → 来源 journal,题号以 journal/ 开头,输出写明只做原文比对",
       rc == 0 and q.get("source") == "journal" and q["id"] == "journal/2026-08-12/claude/300308/above_on/r1.05-d2027-01-29"
       and "上线前 journal 旧记录" in out, (rc, q, out, err[-200:]))


def check_second_forecast(lab: Lab, first: Path):
    time.sleep(1.1)                                # 存档目录按秒命名
    lab.fakes(ai_obj=fake_answers(claude_502=90))  # 同一天第二版,Claude 换了说法(看 resolve 分组带不带 run)
    rc, out, err = lab.run("forecast", "--no-codex")
    runs = lab.runs()
    ok("forecast --no-codex:又建了一次存档", rc == 0 and len(runs) == 2, (rc, err[-600:]))
    if len(runs) < 2:
        return
    new = runs[-1]
    ok("forecast --no-codex:没有 codex 答卷", not (new / "answers" / "codex.json").exists()
       and (new / "answers" / "claude.json").exists())
    sup = first / "superseded.json"
    ok("forecast:同一天的旧版标成被取代、指向新版", sup.is_file()
       and json.loads(sup.read_text(encoding="utf-8")).get("by") == new.name)


def check_update(lab: Lab):
    items = fake_items()
    items["events.calendar"]["value"] = [{"date_us": "2026-11-20", "symbol": "ZZZZ", "name": "合成新财报",
                                          "beijing": "2026-11-21 05:00", "relation": "合成"}]
    lab.fakes(prices_obj=fake_prices({"close": {"300308": [["2026-11-02", 290.0]], "300502": [["2026-11-02", 155.0]]}}),
              inputs_obj=items)
    rc, out, err = lab.run("update")
    page = (lab.out / "看板.html").read_text(encoding="utf-8") if (lab.out / "看板.html").is_file() else ""
    ok("update:退出码 0,重画了页面", rc == 0 and page, (rc, err[-600:]))
    ok("update:页面用刷新后的收盘", "2026-11-02 收盘" in page and "290" in page)
    ok("update:财报日历刷新了", "ZZZZ" in page)
    ok("update:不改预测(存档数没变)", len(lab.runs()) == 2)


def check_daily(lab: Lab):
    rc, out, err = lab.run("check", "--no-notify")
    st = lab.status()
    hit = [t for t in st.get("triggered") or [] if "297" in str(t.get("what"))]
    ok("check:AI 的改主意条件(收盘跌破 297)被触发", rc == 0 and hit and hit[0]["code"] == "300308"
       and "Claude" in hit[0]["what"], (rc, st, err[-400:]))
    ok("check:还没有到期的题", st.get("due") == [], st.get("due"))
    ok("check:页面顶栏标出触发", "收盘跌破 297" in (lab.out / "看板.html").read_text(encoding="utf-8"))
    ok("check:重画页面接着用 update 刷新过的财报日历(不退回存档那天的)",
       "ZZZZ" in (lab.out / "看板.html").read_text(encoding="utf-8"))


def check_resolve(lab: Lab):
    broken = fake_prices(later_bars())
    broken["hfq"]["300308"] = "坏的"               # 300308 的后复权日线取数抛错
    lab.fakes(prices_obj=broken)
    rc, out, err = lab.run("check", "--no-notify", now=LATER_NOW)
    st = lab.status()
    due1 = st.get("due") or []
    ok("check:一只的后复权日线取不到 → 只影响它的到期题;另一只照列,改主意条件照触发,退出码 1",
       rc == 1 and due1 and all("/300502/" in q for q in due1) and any("297" in t["what"] for t in st.get("triggered") or [])
       and [u["code"] for u in st.get("unchecked") or []] == ["300308"] and "到期题没查成" in st["unchecked"][0]["what"],
       (rc, len(due1), st.get("unchecked"), err[-300:]))
    ok("check:页面顶栏列出没查成的", "没查成:300308 到期题没查成" in (lab.out / "看板.html").read_text(encoding="utf-8"))
    rc, out, err = lab.run("resolve", now=LATER_NOW)
    recs = lab.jsonl("outcomes.jsonl")
    ok("resolve:一只的后复权日线取不到 → 只结另一只的题,退出码 1,写明哪只",
       rc == 1 and len(recs) == len(due1) and all(r["code"] == "300502" for r in recs)
       and "300308 后复权日线取不到" in err, (rc, len(recs), len(due1), err[-300:]))
    lab.fakes(prices_obj=fake_prices(later_bars()))
    rc, out, err = lab.run("check", "--no-notify", now=LATER_NOW)
    due = lab.status().get("due") or []
    ok("check(3 个月后):列出剩下能结算的题(含聊天题、journal 题)", rc == 0 and len(due) > 10
       and any(q.startswith("chat/") for q in due) and any(q.startswith("journal/") for q in due)
       and not set(due) & set(due1), (rc, len(due), err[-400:]))
    rc, out, err = lab.run("resolve", now=LATER_NOW)
    recs = lab.jsonl("outcomes.jsonl")
    ok("resolve:退出码 0,两次合起来结算数 = 两次 check 列的到期题数", rc == 0 and len(recs) == len(due1) + len(due),
       (rc, len(recs), len(due1), len(due), err[-600:]))
    r1, r2 = (f"{r.parent.name}/{r.name}/claude/300502/up/n63" for r in lab.runs()[:2])
    by_id = {r["id"]: r for r in recs}
    s1 = (by_id.get(r1) or {}).get("suggest") or {}
    ok("resolve:同一天两次存档各算各的 —— 第一次 Claude 推翻系统又错得多,「AI 判断」那条加分了",
       s1.get("ai", 0) >= 20 and by_id[r1]["resolution"]["outcome"] == 1, (s1, by_id.get(r1, {}).get("forecast")))
    ok("resolve:第二次 Claude 说对了,不带原因初值", r2 in by_id and "suggest" not in by_id[r2], by_id.get(r2))
    ok("resolve:每条带 resolution 和同期大盘比值", recs and all(isinstance(r.get("resolution"), dict)
                                                       and isinstance(r["resolution"].get("index_ratio"), float)
                                                       and isinstance(r["resolution"].get("stock_ratio"), float)
                                                       for r in recs))
    closes = {c: dict(rows) for c, rows in fake_prices(later_bars())["close"].items()}
    px = [r for r in recs if r["code"] in closes and r["resolution"].get("end_day") in closes[r["code"]]]
    ok("resolve:结算记录带起点和那天的不复权收盘(start_close、end_close)", px and all(
        r["resolution"].get("end_close") == closes[r["code"]][r["resolution"]["end_day"]]
        and r["resolution"].get("start_close") == closes[r["code"]].get(str(r["opened"])[:10]) for r in px),
       [(r["id"], r["resolution"].get("start_close"), r["resolution"].get("end_close")) for r in px[:3]])
    up = [r for r in recs if r["author"] == "claude" and r["code"] == "300308" and r["type"] == "up"]
    ok("resolve:300308 一路跌,3 个月涨没涨 = 0", up and up[0]["resolution"]["outcome"] == 0, up[:1])
    sg = [r for r in recs if isinstance(r.get("suggest"), dict)]
    ok("resolve:错题带原因初值,七项加起来 100", sg and all(set(r["suggest"]) == set(attribution.CAUSES)
                                                    and sum(r["suggest"].values()) == 100 for r in sg), len(sg))
    ok("resolve:对的题不带原因初值", all("suggest" not in r for r in recs if r["author"] != "rule"
                                    and r["resolution"]["outcome"] == 1 and (r.get("forecast") or {}).get("pct", 0) >= 50))
    ok("resolve:聊天里加的题、journal 题也结算了", any(r["id"].startswith("chat/") for r in recs)
       and any(r["id"].startswith("journal/") and r.get("source") == "journal" for r in recs))
    rc, out, err = lab.run("resolve", now=LATER_NOW)
    ok("resolve:再跑一次不重复写", rc == 0 and len(lab.jsonl("outcomes.jsonl")) == len(recs), (rc, err[-300:]))
    rc, out, err = lab.run("check", "--no-notify", now=LATER_NOW)
    ok("check:结算以后不再算到期", rc == 0 and lab.status().get("due") == [], lab.status().get("due"))
    sc = (lab.out / "成绩.html").read_text(encoding="utf-8")
    ok("成绩页:比对卡写扣掉同期大盘", "扣掉同期大盘偏" in sc)
    ok("成绩页:结果写那天的收盘价", "元,后复权算是起点价的" in sc)
    return sg


def check_confirm(lab: Lab, wrong: list[dict]):
    qid = wrong[0]["id"] if wrong else "x"
    rc, out, err = lab.run("confirm", "--qid", qid, "--probs", "data_gap=60,system=30,luck=10", "--note", "合成确认")
    rows = lab.jsonl("reviews.jsonl")
    ok("confirm:写进 reviews.jsonl,没写的原因补 0", rc == 0 and len(rows) == 1 and rows[0]["qid"] == qid
       and rows[0]["probs"]["data_gap"] == 60 and rows[0]["probs"]["ai"] == 0, (rc, err[-300:]))
    rc, out, err = lab.run("confirm", "--qid", qid, "--probs", "data_gap=60,system=30", "--note", "x")
    ok("confirm:加起来不是 100 → 报错,不写", rc != 0 and len(lab.jsonl("reviews.jsonl")) == 1, (rc, err[-200:]))
    rc, out, err = lab.run("confirm", "--qid", "没有/这/道/题", "--probs", "luck=100", "--note", "x")
    ok("confirm:题号不在结算记录里 → 报错", rc != 0 and len(lab.jsonl("reviews.jsonl")) == 1, (rc, err[-200:]))


def check_replay(lab: Lab, run: Path):
    import board_layers
    import board_settle
    import render
    old = {k: os.environ.get(k) for k in ("STOCK_LAB", "BOARD_NOW")}
    os.environ.update(STOCK_LAB=str(lab.lab), BOARD_NOW=LATER_NOW)
    try:
        def fake_runner(repo, ref, inputs, seed, frozen_run):
            pkg = json.loads(Path(inputs).read_text(encoding="utf-8"))
            out = board_layers.compute_layers(pkg["items"], pkg["run_meta"], seed)
            out["system"]["300308"]["horizons"]["63"]["p_up"] += 3      # 假装新代码让它变了
            return "f" * 40, json.dumps(out)
        ns = argparse.Namespace(run=f"{run.parent.name}/{run.name}", code="HEAD", corrected=None)
        rc = board_settle.cmd_replay(ns, runner=fake_runner)
        rows = lab.jsonl("replays.jsonl")
        r = rows[-1] if rows else {}
        ok("replay:结果追加到 replays.jsonl", rc == 0 and len(rows) == 1
           and {"at", "run", "code_ref", "corrected", "compare"} <= set(r), r)
        cmp = r.get("compare") or {}
        ok("replay:compare 是和存档两层比的差", (cmp.get("system") or {}).get("300308", {}).get("p_up", {}).get("63") == 3
           and (cmp.get("baseline") or {}).get("300308", {}).get("p_up", {}).get("63") == 0, cmp.get("system"))
        data = render.build_board_data(run, lab.config | {"board_out_dir": str(lab.out)}, date(2027, 3, 1))
        sc = render.render_scores(data)
        ok("成绩页:旧题重跑那栏列出重跑结果", "还没有存下来的重跑结果" not in sc and "代码 HEAD" in sc
           and "3 个月 +3" in sc, sc[sc.find("旧题重跑"):sc.find("旧题重跑") + 400])
        ns = argparse.Namespace(run="2026-01-01/000000", code="HEAD", corrected=None)
        ok("replay:存档不在 → 退出码 1", board_settle.cmd_replay(ns, runner=fake_runner) == 1)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def check_sources(lab: Lab):
    import render
    import scoring
    recs = lab.jsonl("outcomes.jsonl")
    data = render.build_board_data(lab.runs()[-1], lab.config | {"board_out_dir": str(lab.out)}, date(2027, 3, 1))
    sc = data["scores"]
    jid = "journal/2026-08-12/claude/300308/above_on/r1.05-d2027-01-29"
    jq = [j for j in sc.get("journal_questions") or [] if j["id"] == jid]
    ok("成绩数:journal 题列在 journal_questions(换算说明、概率、结算结果)", len(jq) == 1 and jq[0]["note"] == "合成:由旧记录 #9 换算"
       and jq[0]["prob"] == 70 and jq[0]["actual"], jq)
    ok("成绩数:journal 题不进比对卡、错题", jid not in {c["id"] for c in sc["cards"]} | {w["id"] for w in sc["wrong"]})
    graded = [r for r in recs if r.get("source") != "journal"]
    n_ok = scoring.layer_report(graded, "2027-03-01")["layers"]["claude"]["n"]
    n_all = scoring.layer_report(recs, "2027-03-01")["layers"]["claude"]["n"]
    ok("成绩数:Claude 那一层的题数不算 journal 题(聊天题照算)", sc["report"]["layers"]["claude"]["n"] == n_ok == n_all - 1
       and any(r["id"].startswith("chat/") and r["author"] == "claude" for r in graded), (sc["report"]["layers"]["claude"]["n"],
                                                                                          n_ok, n_all))
    bv = sc["report"]["by_version"]
    ok("成绩数:聊天题按版本分组叫「聊天」,没有 unknown", "聊天" in bv and "unknown" not in bv, sorted(bv))
    html = render.render_scores(data)
    sec = html[html.find("<h2>journal 旧记录原文比对</h2>"):]
    sec = sec[:sec.find("</section>")]
    ok("成绩页:journal 旧记录单独一栏,写换算说明和结果", "合成:由旧记录 #9 换算" in sec and "结果:" in sec
       and "100 种里 70 种" in sec, sec[:300])
    ver = html[html.find("<h3>改之后才出的新题</h3>"):]
    ok("成绩页:聊天题那组写「聊天里给的判断」", "聊天里给的判断:" in ver[:ver.find("</section>")])


def check_fixed_config():
    """期限、见底窗口、买点期限是定死的:配置里写了别的值 → forecast 不出预测、写明哪几项;写成一样的照常。"""
    tmp = _tmp()
    try:
        lab = Lab(tmp)
        cfg = lab.lab / "private" / "config" / "board.json"
        board = json.loads(cfg.read_text(encoding="utf-8"))
        cfg.write_text(json.dumps({**board, "horizons": [63, 126], "bottom_window": 100}, ensure_ascii=False),
                       encoding="utf-8")
        lab.fakes(prices_obj=fake_prices(), inputs_obj=fake_items(), ai_obj=fake_answers())
        rc, out, err = lab.run("forecast")
        ok("forecast:配置改了定死的期限、窗口 → 退出码 2,写明哪两项,不建存档", rc == 2 and "horizons" in err
           and "bottom_window" in err and "定死" in err and not lab.runs(), (rc, err[-400:], len(lab.runs())))
        cfg.write_text(json.dumps({**board, "horizons": [63, 126, 250], "bottom_window": 126, "buy_horizon": 250},
                                  ensure_ascii=False), encoding="utf-8")
        rc, out, err = lab.run("forecast")
        ok("forecast:配置里写成和固定值一样 → 照常出预测", rc == 0 and len(lab.runs()) == 1, (rc, err[-400:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_layer_without_questions():
    """一层的答卷过了校验但拆不出题:存档照写,单独一行写明是哪一层、为什么,退出码 1。"""
    tmp = _tmp()
    try:
        lab = Lab(tmp)
        lab.fakes(prices_obj=fake_prices(), inputs_obj=fake_items(), ai_obj=fake_answers(dup_zone="codex"))
        rc, out, err = lab.run("forecast")
        run = (lab.runs() or [None])[-1]
        qs = json.loads((run / "questions.json").read_text(encoding="utf-8")) if run else {}
        ok("forecast:一层整层没出题 → 退出码 1,单独一行写明哪一层和原因", rc == 1 and "codex 整层没出题" in err
           and "题号重复" in err, (rc, err[-500:]))
        ok("forecast:存档照写(manifest、codex 答卷都在),questions.json 记下这一层",
           run is not None and (run / "manifest.json").is_file() and (run / "answers" / "codex.json").is_file()
           and [e["layer"] for e in qs.get("layers_without_questions") or []] == ["codex"]
           and not any(q["author"] == "codex" for q in qs.get("questions") or [])
           and any(q["author"] == "claude" for q in qs.get("questions") or []), qs.get("layers_without_questions"))
        ok("forecast:「整层没出题」不混进没出题的买入价位", "没出题的买入价位 0 处" in out, out[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_add_question_no_run():
    """还没有任何存档时加的题按 journal 处理。"""
    tmp = _tmp()
    try:
        lab = Lab(tmp)
        lab.fakes(prices_obj=fake_prices())
        rc, out, err = lab.run("add-question", "--code", "300502", "--type", "touch", "--ratio", "1.2", "--n", "63",
                               "--prob", "25", "--author", "codex", "--note", "合成:还没存档时说的")
        rows = lab.jsonl("chat_questions.jsonl")
        ok("add-question:还没有存档 → 来源 journal", rc == 0 and len(rows) == 1 and rows[0].get("source") == "journal"
           and rows[0]["id"].startswith("journal/2026-10-30/"), (rc, rows, err[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_forecast_flow():
    tmp = _tmp()
    try:
        lab = _forecast_lab(tmp)
        run = check_forecast(lab)
        if run is None:
            return
        check_forecast_refuses(lab)
        check_add_question(lab)
        check_second_forecast(lab, run)
        check_update(lab)
        check_daily(lab)
        wrong = check_resolve(lab)
        check_sources(lab)
        check_confirm(lab, wrong)
        check_replay(lab, run)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_install_script():
    tmp = _tmp()
    try:
        venv = tmp / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
        home, lab = tmp / "home", tmp / "lab"
        home.mkdir()
        lab.mkdir()
        env = dict(os.environ, HOME=str(home), STOCK_LAB=str(lab), ASTOCK_VENV=str(tmp / "venv"))
        p = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=60)
        out = p.stdout
        ok("定时任务:不加 --install 只打印,退出码 0", p.returncode == 0 and out.lstrip().startswith("<?xml"), p.stderr[-300:])
        ok("定时任务:周一到周五 15:30 跑 board.py check --notify",
           all(f"<key>Weekday</key><integer>{d}</integer>" in out for d in range(1, 6))
           and out.count("<integer>15</integer>") == 5 and out.count("<integer>30</integer>") == 5
           and "<string>check</string>" in out and "<string>--notify</string>" in out and "tools/board.py" in out)
        ok("定时任务:带上 STOCK_LAB", f"<string>{lab}</string>" in out)
        ok("定时任务:没碰 ~/Library", not (home / "Library").exists())
        if shutil.which("plutil"):
            f = tmp / "x.plist"
            f.write_text(out, encoding="utf-8")
            lint = subprocess.run(["plutil", "-lint", str(f)], capture_output=True, text=True)
            ok("定时任务:plist 格式合法(plutil -lint)", lint.returncode == 0, lint.stdout + lint.stderr)
        env.pop("STOCK_LAB")
        p = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=60)
        ok("定时任务:没设 STOCK_LAB 就报错", p.returncode != 0 and "STOCK_LAB" in p.stderr, p.stderr[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_wiring():
    link = ROOT / "tools" / "board.py"
    ok("tools/board.py 是相对软链,指向 board.py", link.is_symlink() and not os.readlink(link).startswith("/")
       and link.resolve() == BOARD.resolve(), os.readlink(link) if link.is_symlink() else None)
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    ok("install.sh 生成的 aliases.sh 带 board", "alias board='$VENV/bin/python $LAB/tools/board.py'" in text)
    skill = (FB / "SKILL.md").read_text(encoding="utf-8")
    ok("SKILL.md 的目录树列出整份重写的三个状态文件", all(f"── {n}" in skill for n in
                                                    ("status.json", "events.json", "actions_seen.json")), "")
    tmp = _tmp()
    try:      # skill 副本比对要覆盖这个分支改过、新加的几个 skill:镜像里少了 forecast-board 就要报
        names = ("astock-quote", "stock-analysis-workflow", "finance-pdf-report", "data-sources", "forecast-from-facts")
        for k in names:
            shutil.copytree(ROOT / "skills" / k, tmp / k, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        sync = ROOT / "scripts" / "check_skill_sync.sh"
        p = subprocess.run(["bash", str(sync), str(tmp)], capture_output=True, text=True, timeout=60)
        ok("skill 副本比对:镜像里没有 forecast-board → 不过,写出是哪个", p.returncode != 0 and "forecast-board" in p.stdout,
           p.stdout[-300:])
        shutil.copytree(ROOT / "skills" / "forecast-board", tmp / "forecast-board",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        p = subprocess.run(["bash", str(sync), str(tmp)], capture_output=True, text=True, timeout=60)
        ok("skill 副本比对:六个都一样 → 过", p.returncode == 0 and "6 个 skill" in p.stdout, p.stdout[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    p = subprocess.run([sys.executable, str(link), "--help"], capture_output=True, text=True, timeout=60)
    ok("从软链跑 --help 列出全部子命令", p.returncode == 0 and all(
        c in p.stdout for c in ("inputs", "update", "forecast", "resolve", "check", "replay", "recompute",
                                "add-question", "confirm")), p.stdout[-400:] + p.stderr[-400:])


CHECKS = [check_notify_text, check_wiring, check_install_script, check_recompute, check_check_sell_line,
          check_inputs_check, check_add_question_no_run, check_layer_without_questions, check_fixed_config,
          check_forecast_flow]


def main():
    for fn in CHECKS:
        print(f"── {fn.__name__}")
        fn()
    print()
    print("✅ 全过" if not FAILS else f"❌ {FAILS} 项不过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
