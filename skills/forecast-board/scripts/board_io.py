#!/usr/bin/env python3
"""board_io —— board.py 各子命令共用的:现在几点、取行情、取数器、AI 作答器,和几样读写小工具。

它是什么:取行情、取数、调两个 AI 这三样要联网或调外部命令。集中在这里,各子命令只管「做什么」;
检查时要换成假的,也只换这一处。
没有它会怎样:每个子命令各写一遍「15:05 以前去掉当天那根」「假数据从哪读」,迟早有一处漏掉,
盘中的实时价就被当成收盘拿去结算。

检查用的四个环境变量(scripts/check_board_cli.py 用,真跑时不要设):
  BOARD_NOW          'YYYY-MM-DDTHH:MM:SS',当作北京时间的现在(判盘中、定今天)
  BOARD_FAKE_PRICES  JSON 文件 {"index": [[日期, 收盘]], "hfq": {代码: [[日期, 价]]}, "close": {代码: [[日期, 价]]}}
                     index = 创业板指不复权(也是交易日历),hfq = 后复权,close = 不复权
  BOARD_FAKE_INPUTS  JSON 文件 {键: 取数器的返回({"value", "source", "as_of", …})或 {"raise": 原因}}
  BOARD_FAKE_AI      JSON 文件 {作者: 答卷}
假行情也只给到「今天」为止,并且照样在北京时间 15:05 以前去掉当天那根,和真的一样。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import fb_common  # 先于 prices:prices 会加载 market.py,它一 import 就删掉代理变量
import prices

ENV_NOW, ENV_PRICES, ENV_INPUTS, ENV_AI = "BOARD_NOW", "BOARD_FAKE_PRICES", "BOARD_FAKE_INPUTS", "BOARD_FAKE_AI"
CLOSE_DAYS = 20             # 刷新收盘价时往前取多少个自然日(长假也够)
NO_BARS = "no_bars"         # 近 CLOSE_DAYS 天一根日线都没有(多半停牌):和「取不到」(网络、接口)分开写


def now() -> datetime:
    """北京时间的现在(带时区)。设了 BOARD_NOW 就用它(不带时区的当作北京时间)。"""
    s = os.environ.get(ENV_NOW)
    if not s:
        return fb_common.now_cn()
    t = datetime.fromisoformat(s)
    tz = fb_common.now_cn().tzinfo
    return t.astimezone(tz) if t.tzinfo else t.replace(tzinfo=tz)


def progress(msg: str) -> None:
    """进度打到 stderr:recompute 的 stdout 只能有那一段 JSON,其他子命令也照这个规矩。"""
    print(msg, file=sys.stderr, flush=True)


def _fake(name: str):
    path = os.environ.get(name)
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── 行情 ────────────────────────────────────────────────────────────────────
class Market:
    """三种日线,都只给到今天、15:05 以前去掉当天那根(prices.drop_live_bar):
    index(start)      创业板指不复权 [(日期, 收盘)] —— 交易日历和买点题的指数均线都用它
    hfq(code, start)  个股后复权 {日期: 价} —— 结算比值
    close(code, start) 个股不复权 [(日期, 收盘)] —— 和卖出线、AI 写的价位比"""

    def __init__(self, t: datetime):
        self.t = t
        self.today = t.date().isoformat()
        self.fake = _fake(ENV_PRICES)

    def _cut(self, bars, start: str) -> list[tuple[str, float]]:
        rows = sorted((str(d), float(c)) for d, c in bars if start <= str(d) <= self.today)
        return prices.drop_live_bar(rows, self.t)

    def index(self, start: str) -> list[tuple[str, float]]:
        if self.fake is not None:
            return self._cut(self.fake.get("index") or [], start)
        return self._cut(prices.market.fetch_daily(prices.CALENDAR_INDEX, start, self.today, fq=""), start)

    def hfq(self, code: str, start: str) -> dict[str, float]:
        if self.fake is not None:
            return dict(self._cut((self.fake.get("hfq") or {}).get(code) or [], start))
        return prices.hfq_closes(code, start, now=self.t)

    def close(self, code: str, start: str) -> list[tuple[str, float]]:
        if self.fake is not None:
            return self._cut((self.fake.get("close") or {}).get(code) or [], start)
        sym = prices.tencent_symbol(code)
        return self._cut(prices.market.fetch_daily(sym, start, self.today, fq=""), start)


def failure(code: str, what: str, err: Exception) -> dict:
    """一项没查成:{"code", "what", "detail"},和 status.json 里 triggered 的条目同一个样子。"""
    return {"code": code, "what": what, "detail": f"{type(err).__name__}: {str(err)[:160]}"}


def failure_text(f: dict) -> str:
    return f"{f.get('code') or ''} {f.get('what')}({f.get('detail')})".strip()


def latest_closes(market: Market, codes: list[str], today: date) -> tuple[dict, list[dict], list[dict]]:
    """每只最近一个收盘 → ({代码: [日期, 收盘]}, 取不到的 [failure(...)], 近期没有日线的 [{"code", "what", "detail"}])。
    出错(网络、接口)和「近 20 天没有日线」(长期停牌)分开写,但两种都是这只没有新收盘:
    调用的地方都要写到页面 / status.json 上,不能只打在 stderr。一只出错不影响其他几只。"""
    out, errors, notes = {}, [], []
    start = (today - timedelta(days=CLOSE_DAYS)).isoformat()
    for c in codes:
        try:
            bars = market.close(c, start)
        except Exception as e:          # 网络、接口格式……都记下来,不让一只拖垮整次检查
            errors.append(failure(c, "收盘价取不到", e))
            continue
        if bars:
            out[c] = [bars[-1][0], bars[-1][1]]
        else:
            notes.append({"code": c, "what": f"近 {CLOSE_DAYS} 天没有日线(停牌?)", "detail": f"{start} 以来一根都没有",
                          "kind": NO_BARS})
    return out, errors, notes


# ── 取数器、AI 作答器 ───────────────────────────────────────────────────────
def collectors(config: dict, keys=None):
    """取数器 {键: fn(config)}。没设 BOARD_FAKE_INPUTS 而且 keys 不给 → None(inputs.collect 按配置展开默认的);
    keys 给了只留这几项。"""
    fake = _fake(ENV_INPUTS)
    if fake is None:
        if keys is None:
            return None
        import inputs
        full = inputs.expand(config)
        return {k: full[k] for k in keys if k in full}

    def make(got):
        def fn(_cfg):
            if "raise" in got:
                raise RuntimeError(got["raise"])
            return got
        return fn
    return {k: make(v) for k, v in fake.items() if keys is None or k in keys}


def ai_runner(author: str):
    """AI 作答器:没设 BOARD_FAKE_AI → None(ai_answer 用真的 claude / codex);设了就原样交回假答卷。"""
    fake = _fake(ENV_AI)
    if fake is None:
        return None
    import ai_answer

    def run(prompt: str, workdir: Path):
        if author not in fake:
            raise ai_answer.RunFailed(f"假答卷里没有 {author}")
        return ai_answer.RunOutput(json.dumps(fake[author], ensure_ascii=False), model="假的(BOARD_FAKE_AI)",
                                   exit_code=0)
    return run


# ── 文件 ────────────────────────────────────────────────────────────────────
def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, obj) -> None:
    """整份重写的状态文件(status.json):先写临时文件再换名,不会留下写了一半的。存档文件不用它(存档只追加)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_rel(run: Path) -> str:
    """存档目录 → 题号和页面里用的「日期/时分秒」。"""
    return f"{Path(run).parent.name}/{Path(run).name}"


def find_run(arg: str) -> Path:
    """--run 的值 → 存档目录:写完整路径,或存档根下的「日期/时分秒」都行;要有 manifest.json(跑完了的)。"""
    for cand in (Path(arg).expanduser(), fb_common.archive_root() / arg):
        if (cand / "manifest.json").is_file():
            return cand
    raise FileNotFoundError(f"找不到存档 {arg}:写「日期/时分秒」(存档根 {fb_common.archive_root().name} 下)"
                            "或完整路径,而且那次要跑完(有 manifest.json)")


def write_pages(run: Path, config: dict, today: date, closes: dict | None = None,
                events: dict | None = None, notices: list | None = None) -> list[Path]:
    """用一次存档重画三个页面,写到配置里的 board_out_dir。closes / events / notices 见 render.build_board_data。"""
    import render
    out = config.get("board_out_dir")
    if not out:
        raise KeyError("配置里没有 board_out_dir(页面写到哪)")
    data = render.build_board_data(run, config, today, closes=closes, events=events, notices=notices)
    paths = render.write_all(data, Path(out))
    record_actions_seen(run, data, today)
    return paths


def record_actions_seen(run: Path, data: dict, today: date) -> None:
    """这次画页面时每只股票的动作种类和从哪天起,整份重写 <存档根>/actions_seen.json(状态文件,不是存档)。
    下次画页面时 build_board_data 读回来:动作一直挂着,最晚日期不动;新出现的动作从出现那天数。"""
    import board_data
    seen = data.get("actions_seen") or {}
    write_json_atomic(Path(run).parent.parent / board_data.SEEN_FILE, {**seen, "saved_at": today.isoformat()})
