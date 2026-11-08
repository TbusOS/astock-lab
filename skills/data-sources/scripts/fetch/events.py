#!/usr/bin/env python3
"""events —— 美股财报日历(纳斯达克)和云厂 SEC 新文件扫描。

用法:
    events.py calendar                                # 关注名单未来 21 天(美国日期)的财报,换成北京时间
    events.py calendar --days 14 --watch NVDA,FN      # 只看这几家、只看 14 天
    events.py calendar --start 2026-11-16 --days 3 --json
    events.py sec --since 2026-10-15                  # 几家云厂这之后交给 SEC 的文件 + 「年份 + 资本开支」原句
    events.py sec --since 2026-10-15 --tickers AMZN,ORCL --json
    events.py sec --here                              # 本机能连 SEC 时直接在本机跑(默认送到 mini 上跑)

为什么要有(2026-11-02):
    ① 财报日期:同行、下游、云厂的财报会直接带动 A 股光模块那几只,事先要知道哪天、北京几点。
       纳斯达克的日历接口 api.nasdaq.com/api/calendar/earnings?date= 本机能直连,一天一页。
    ② SEC 新文件:capex.py 只读季度数字和 8-K 新闻稿里的指引。2026-11-02 就漏了亚马逊 10-Q 里
       「2027 年现金资本开支会明显增加」这句 —— 它在 10-Q 的流动性讨论里,不在新闻稿里。
       这里把关注名单的新文件列出来,正文(8-K 还读附件 99)里同一句话同时出现
       「年份(20xx、next year、财年简写 FY27)」和「capital expenditure / capex / property and equipment」
       就摘原文(甲骨文 10-28 投资者日那句「peak capex in FY27-FY28 … step-down in FY29」只写 FY27)。
       ⚠ 摘句规则只认这几个词:亚马逊同一份 10-Q 里「800 亿美元技术设施采购贷款」那句
       写的是 technology infrastructure assets,不会被摘到,要人看。

北京时间怎么换:纳斯达克只给「盘前 / 盘后 / 没给」三种。
    盘后(美东下午 4 点以后)= 北京第二天早上;盘前(美东早上开盘前)= 北京当天晚上。
    美国夏令时、冬令时差一小时,但落在「早上 / 晚上」哪一段不变,所以只写到这一级。

在哪跑:SEC 本机连不上,默认把**本文件整份**从标准输入送到 mini 上执行
(ssh mini 'cd ~/astock-lab && .venv/bin/python - sec-here …'),mini 上不需要有这份代码,
所以本机改了摘句规则,mini 上马上就是新的。只用标准库。

被谁用:预测看板(forecast-board/inputs.py 的 events.calendar / events.sec 两项)和平时分析用同一份。
检查:scripts/check_board_store.py 的 check_events(不联网)。
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 关注名单:光模块同行、上游、下游、云厂。公开代码,看板那边可以用配置里的名单覆盖。
WATCH_US = ["NVDA", "FN", "COHR", "LITE", "AAOI", "ANET", "AMD", "ALAB", "SMCI", "CRWV",
            "NBIS", "CSCO", "AVGO", "MRVL", "ORCL"]
RELATION = {
    "NVDA": "下游:GPU,光模块最大的拉货方", "FN": "同行:光模块代工(英伟达供应链)",
    "COHR": "同行:光模块 / 光芯片", "LITE": "同行:光芯片 / 光模块", "AAOI": "同行:光模块",
    "ANET": "下游:数据中心交换机", "AMD": "下游:GPU", "ALAB": "同链:互联芯片",
    "SMCI": "下游:AI 服务器", "CRWV": "云厂:GPU 云", "NBIS": "云厂:GPU 云",
    "CSCO": "下游:网络设备", "AVGO": "同链:交换芯片 / 定制芯片", "MRVL": "同链:光 DSP 芯片",
    "ORCL": "云厂", "MSFT": "云厂", "GOOGL": "云厂", "AMZN": "云厂", "META": "云厂",
}
# SEC 用的中央索引号(CIK),公开信息
SEC_CIKS = {"MSFT": 789019, "GOOGL": 1652044, "AMZN": 1018724, "META": 1326801,
            "ORCL": 1341439, "NVDA": 1045810}

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={}"
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                  "Accept": "application/json, text/plain, */*"}
_BEIJING = {"time-after-hours": (1, "早上"), "time-pre-market": (0, "晚上")}
NOT_SUPPLIED = "时间未定"
DEFAULT_DAYS = 21               # 14 天会漏掉 15 天后才发财报的英伟达这种

# SEC:要带联系方式的 User-Agent(SEC 的规矩,不带会 403),请求间隔 ≥ 0.15 秒
DEFAULT_UA = "astock-lab-user your-email@example.com"
SEC_GAP = 0.15
# 要看的文件类型:季报年报、8-K、发债发股。持股变动(3/4/5/144)、委托书之类不看
FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "424B2", "424B3", "424B5", "FWP", "S-3", "S-3ASR", "F-3"}
_EXHIBIT = re.compile(r"ex-?99|dex99", re.I)
MAX_EXHIBITS = 3
MAX_SENTENCES = 8               # 每份文件最多摘几句(10-Q 里资本开支的句子能有十几句)
MAX_SENTENCE_CHARS = 600        # 比这长的多半是表格被拍平成一串,不当句子

_CAPEX_YEAR = re.compile(r"\b20\d\d\b|next year|\bFY ?(?:20)?\d\d\b", re.I)
# 往后看的词。plan 只认 plan / plans / planned / planning(不认 plant —— property, plant and equipment);
# project 不收(多半是名词「项目」)
_AHEAD = re.compile(r"\b(?:expect\w*|anticipat\w*|plan(?:s|ned|ning)?|intend\w*|outlook|guidance|forecast\w*|will)\b",
                    re.I)
_CAPEX_WORD = re.compile(r"capital expenditure|capex|property and equipment", re.I)
_SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"(\$])")


# ── 财报日历 ────────────────────────────────────────────────────────────────
def beijing_time(date_us: str, time_code: str) -> str:
    """纳斯达克的时间标记 → 北京时间的说法。盘后 → 次日早上;盘前 → 当天晚上;其余 → 时间未定。"""
    if time_code not in _BEIJING:
        return NOT_SUPPLIED
    shift, part = _BEIJING[time_code]
    return f"{(date.fromisoformat(date_us) + timedelta(days=shift)).isoformat()} {part}"


def nasdaq_fetch(day: str, proxy: str | None = None, timeout: int = 20) -> dict:
    """一天的财报日历原始 JSON。proxy 不给 = 按当前环境变量(没有代理变量就直连)。"""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(NASDAQ_URL.format(day), headers=NASDAQ_HEADERS)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def nasdaq_calendar(days: list[str], watch: set[str], fetch=None) -> list[dict]:
    """days(美国日期)里 watch 名单的财报,每条
    {"date_us", "symbol", "name", "time", "beijing", "relation"}。watch 为空 = 全列。
    fetch(day) → 那天的原始 JSON,默认 nasdaq_fetch;哪一天取失败异常原样往上抛
    (调用方决定换代理重试还是记失败,不悄悄少几天)。周末那几天接口回 rows = null、rCode 200,算没有;
    rCode 不是 200 算取失败,抛 RuntimeError。"""
    fetch = fetch or nasdaq_fetch
    want = {s.upper() for s in (watch or ())}
    out = []
    for day in days:
        page = fetch(day) or {}
        rc = (page.get("status") or {}).get("rCode")
        if rc is not None and rc != 200:
            raise RuntimeError(f"纳斯达克 {day} 回 rCode {rc}:{page.get('message')}")
        rows = (page.get("data") or {}).get("rows") or []
        for r in rows:
            sym = str(r.get("symbol") or "").upper()
            if want and sym not in want:
                continue
            code = r.get("time") or ""
            out.append({"date_us": day, "symbol": sym, "name": r.get("name") or "", "time": code,
                        "beijing": beijing_time(day, code), "relation": RELATION.get(sym, "")})
    return out


def us_today() -> date:
    """美东今天的日期(纳斯达克日历按美国日期排)。没有时区库时按 UTC−5 算。"""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except (ImportError, KeyError):
        tz = timezone(timedelta(hours=-5))
    return datetime.now(tz).date()


def next_days(n: int, start: date | None = None) -> list[str]:
    """从 start(默认美东今天)起 n 个自然日。"""
    start = start or us_today()
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ── 摘句 ────────────────────────────────────────────────────────────────────
def html_to_text(raw) -> str:
    """去标签、反转义、空白压成一个空格。"""
    t = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def split_sentences(text: str) -> list[str]:
    """按句号、问号、叹号后跟大写字母(或引号、括号、$)切句。U.S. 这类缩写会被切开,不影响摘句。"""
    return [s.strip() for s in _SENT_END.split(text or "") if s.strip()]


def extract_capex_sentences(text: str) -> list[str]:
    """同一句里同时有「年份」和「capital expenditure / capex / property and equipment」的句子。
    年份 = 20xx、next year,或财年简写 FY27 / FY 2027(甲骨文只这么写)。
    去重;超过 MAX_SENTENCE_CHARS 字的(表格拍平)不要;往后看的句子(expect / anticipate / plan …)
    排前面、其余按原文顺序,最多 MAX_SENTENCES 句 —— 10-Q 里「折旧 / 应付设备款」这类旧数字的句子很多,
    不排序的话,真正说明年打算的那句会被挤出去(2026-11-02 亚马逊 10-Q 那句排在第 7)。"""
    hits = []
    for s in split_sentences(text):
        if len(s) <= MAX_SENTENCE_CHARS and s not in hits and _CAPEX_YEAR.search(s) and _CAPEX_WORD.search(s):
            hits.append(s)
    ahead = [s for s in hits if _AHEAD.search(s)]
    return (ahead + [s for s in hits if s not in ahead])[:MAX_SENTENCES]


# ── SEC(在能连 SEC 的机器上跑)──────────────────────────────────────────────
_last = [0.0]


def _sec_get(url: str) -> bytes:
    gap = time.time() - _last[0]
    if gap < SEC_GAP:
        time.sleep(SEC_GAP - gap)
    req = urllib.request.Request(url, headers={"User-Agent": os.environ.get("SEC_UA", DEFAULT_UA),
                                               "Accept-Encoding": "gzip"})      # 只会解 gzip,不要 deflate
    try:
        with urllib.request.urlopen(req, timeout=40) as f:
            raw = f.read()
            return gzip.decompress(raw) if f.headers.get("Content-Encoding") == "gzip" else raw
    finally:
        _last[0] = time.time()


def _archive(cik: int, acc: str, name: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{name}"


def _exhibits(cik: int, acc: str, get) -> list[str]:
    """8-K 的附件 99(新闻稿、投资者日材料)文件名,最多 MAX_EXHIBITS 个。"""
    idx = json.loads(get(_archive(cik, acc, "index.json")))
    names = [x.get("name", "") for x in (idx.get("directory") or {}).get("item") or []]
    return [n for n in names if _EXHIBIT.search(n) and n.lower().endswith((".htm", ".html", ".txt"))][:MAX_EXHIBITS]


def _filing(tk: str, cik: int, d: str, form: str, acc: str, doc: str, get) -> dict:
    """一份文件:正文(8-K 加附件 99)里摘句。哪一页取失败写进 note,不整份放弃。"""
    item = {"ticker": tk, "date": d, "form": form, "doc": doc, "url": _archive(cik, acc, doc),
            "capex_sentences": [], "read": []}
    names, notes = [doc], []
    if form.startswith("8-K"):
        try:
            names += _exhibits(cik, acc, get)
        except Exception as e:
            notes.append(f"附件清单没取到 {type(e).__name__}")
    for name in names:
        try:
            text = html_to_text(get(_archive(cik, acc, name)))
        except Exception as e:
            notes.append(f"{name} 没取到 {type(e).__name__}")
            continue
        item["read"].append(name)
        for s in extract_capex_sentences(text):
            if s not in item["capex_sentences"]:
                item["capex_sentences"].append(s)
    if notes:
        item["note"] = ";".join(notes)
    return item


def scan_here(ciks: dict, since: str, get=None) -> dict:
    """在本进程里扫:每家 since(含)以后、FORMS 里那几类文件。
    返回 {"filings": [...], "failed": [{"ticker", "why"}]};一家的文件列表取不到进 failed,别家照扫。
    get(url) → bytes,默认带 SEC 要求的 User-Agent 和限速。"""
    get = get or _sec_get
    filings, failed = [], []
    for tk, cik in ciks.items():
        try:
            recent = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))["filings"]["recent"]
        except Exception as e:
            failed.append({"ticker": tk, "why": f"{type(e).__name__}: {str(e)[:80]}"})
            continue
        for i, form in enumerate(recent.get("form") or []):
            d = recent["filingDate"][i]
            base = form[:-2] if form.endswith("/A") else form
            if d < since or base not in FORMS:
                continue
            filings.append(_filing(tk, cik, d, form, recent["accessionNumber"][i],
                                   recent["primaryDocument"][i], get))
    return {"filings": filings, "failed": failed}


# ── 送到 mini 上跑 ───────────────────────────────────────────────────────────
def ssh_python(script: str, args: list[str], host: str = "mini", workdir: str = "~/astock-lab",
               env: dict | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    """在 host 上 cd workdir,用那边 .venv 的 python 从标准输入执行 script,参数是 args。
    返回 CompletedProcess(不看退出码,调用方自己判)。env 传给本机的 ssh 进程
    (看板传启动时的环境,免得被取数脚本删掉的代理变量影响 ssh 的配置)。
    远端先设闹钟 signal.alarm(timeout − 30):本机等到超时只会杀掉 ssh,远端那个 python 还会挂着
    (mini 上没有 timeout 命令);闹钟让它早 30 秒自己退出。脚本从标准输入读进来再 exec,
    所以脚本开头的 from __future__ 照样有效。"""
    boot = (f"import signal,sys; signal.alarm({max(timeout - 30, 10)}); sys.argv[0]='-'; "
            "exec(compile(sys.stdin.read(),'<stdin>','exec'),{'__name__':'__main__'})")
    cmd = f"cd {workdir} && .venv/bin/python -c {shlex.quote(boot)} " + " ".join(shlex.quote(a) for a in args)
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, cmd],
                          input=script, capture_output=True, text=True, timeout=timeout, env=env)


def last_json(text: str):
    """stdout 里最后一行 JSON(前面可能有提示行)。"""
    for line in reversed((text or "").splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise ValueError(f"输出里没有 JSON:{(text or '')[-200:]!r}")


def _default_remote(script: str, args: list[str]) -> str:
    p = ssh_python(script, args)
    if p.returncode != 0:
        raise RuntimeError(f"mini 上执行失败(退出码 {p.returncode}):{p.stderr.strip()[-300:]}")
    return p.stdout


def sec_scan(ciks: dict, since: str, run_remote=None) -> dict:
    """把本文件整份送到 mini,在那边跑 scan_here。返回 {"filings", "failed"}。
    run_remote(script, args) → 远端 stdout,默认 ssh mini。"""
    run_remote = run_remote or _default_remote
    pairs = ",".join(f"{tk}={int(cik)}" for tk, cik in ciks.items())
    out = run_remote(Path(__file__).read_text(encoding="utf-8"), ["sec-here", "--since", since, "--ciks", pairs])
    return last_json(out)


def sec_new_filings(ciks: dict, since: str, run_remote=None) -> list[dict]:
    """关注名单 since 以后的新文件,每条
    {"ticker", "date", "form", "doc", "url", "capex_sentences": [str], "read": [读过的文件名]}
    (哪页没取到另有 "note")。在 mini 上跑;要知道哪几家整家没取到,用 sec_scan。"""
    return sec_scan(ciks, since, run_remote)["filings"]


# ── 命令行 ──────────────────────────────────────────────────────────────────
def _ciks_arg(s: str | None) -> dict:
    """'MSFT,AMZN' 或 'MSFT=789019,XYZ=123';不在默认表里又没给编号的报错。"""
    if not s:
        return dict(SEC_CIKS)
    out = {}
    for part in (p.strip() for p in s.split(",") if p.strip()):
        tk, _, num = part.partition("=")
        tk = tk.upper()
        if num:
            out[tk] = int(num)
        elif tk in SEC_CIKS:
            out[tk] = SEC_CIKS[tk]
        else:
            raise SystemExit(f"{tk} 不在默认名单里,写成 {tk}=<CIK 编号>")
    return out


def cmd_calendar(a) -> int:
    start = date.fromisoformat(a.start) if a.start else None
    watch = {w.strip().upper() for w in a.watch.split(",") if w.strip()} if a.watch else set(WATCH_US)
    days = next_days(a.days, start)
    rows = nasdaq_calendar(days, watch)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    print(f"## 财报日历 {days[0]} ~ {days[-1]}(美国日期,{len(watch)} 家关注)\n")
    if not rows:
        print("(这段时间关注名单里没有财报)")
    else:
        print("| 美国日期 | 代码 | 公司 | 北京时间 | 跟持股的关系 |\n|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['date_us']} | {r['symbol']} | {r['name']} | {r['beijing']} | {r['relation'] or '—'} |")
    print("\n来源:纳斯达克财报日历 api.nasdaq.com;盘后 = 北京次日早上,盘前 = 北京当天晚上。")
    return 0


def cmd_sec(a) -> int:
    ciks = _ciks_arg(a.tickers)
    since = a.since or (date.today() - timedelta(days=30)).isoformat()
    if a.here:
        res = scan_here(ciks, since)
    else:
        res = sec_scan(ciks, since, run_remote=lambda s, args: _remote_on(a.host, s, args))
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
        return 2 if res["failed"] else 0
    print(f"## {', '.join(ciks)}:{since} 以来交给 SEC 的文件\n")
    for f in res["filings"]:
        print(f"- {f['date']} {f['ticker']} {f['form']} {f['doc']}  {f['url']}")
        for s in f["capex_sentences"]:
            print(f"    「{s}」")
        if f.get("note"):
            print(f"    ({f['note']})")
    for x in res["failed"]:
        print(f"- ❌ {x['ticker']} 文件列表没取到:{x['why']}")
    if not res["filings"]:
        print("(没有新文件)")
    print("\n摘句规则:同一句里有「20xx / next year / FY27」和「capital expenditure / capex / property and equipment」;"
          "说以后打算的句子排前面。")
    return 2 if res["failed"] else 0


def _remote_on(host: str, script: str, args: list[str]) -> str:
    p = ssh_python(script, args, host=host)
    if p.returncode != 0:
        raise SystemExit(f"{host} 上执行失败(退出码 {p.returncode}):{p.stderr.strip()[-300:]}")
    return p.stdout


def cmd_sec_here(a) -> int:
    """mini 那边执行的入口:扫完只打一行 JSON。"""
    print(json.dumps(scan_here(_ciks_arg(a.ciks), a.since), ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="events", description="美股财报日历 + 云厂 SEC 新文件扫描",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calendar", help="关注名单未来几天的财报(北京时间)")
    c.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"看几个自然日,从美东今天(或 --start)算起,默认 {DEFAULT_DAYS}")
    c.add_argument("--start", help="从哪天(美国日期 YYYY-MM-DD)开始,默认美东今天")
    c.add_argument("--watch", help=f"逗号分隔的代码,默认 {','.join(WATCH_US)}")
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("sec", help="云厂 SEC 新文件 + 「年份 + 资本开支」原句")
    s.add_argument("--since", help="YYYY-MM-DD,默认 30 天前")
    s.add_argument("--tickers", help=f"逗号分隔,默认 {','.join(SEC_CIKS)};名单外的写 代码=CIK")
    s.add_argument("--host", default="mini", help="在哪台机器上跑(要能连 SEC),默认 mini")
    s.add_argument("--here", action="store_true", help="本机能连 SEC 时直接在本机跑")
    s.add_argument("--json", action="store_true")
    h = sub.add_parser("sec-here", help="(内部)在本机扫并打一行 JSON,sec 命令送到 mini 上执行的就是它")
    h.add_argument("--since", required=True)
    h.add_argument("--ciks", required=True, help="MSFT=789019,AMZN=1018724")
    a = p.parse_args(argv)
    return {"calendar": cmd_calendar, "sec": cmd_sec, "sec-here": cmd_sec_here}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
