#!/usr/bin/env python3
"""ai_answer —— 两个 AI(Claude、codex)拿同一份冻结输入包、同一份提示词,各自作答。

它是什么:预测的第三层「AI 最终判断」。程序把这次能用的材料冻结成一份输入包,
生成一份提示词,两个 AI 各自非交互地答,答卷按固定结构校验后存档。
没有它会怎样:AI 的判断只留在聊天里,带着对话历史和记忆,两个 AI 看到的材料也不一样,
事后既没法打分,也没法说「谁更准」—— 差别可能只是材料不同。

为什么这么做:
  - 同一份提示词:提示词只存一份 <run>/ai/prompt.md;第二个 AI 生成的提示词和它不一样就拒绝。
  - 不带进别的东西:Claude 用 `claude -p`,不给工具、不读任何设置文件(CLAUDE.md、记忆、hooks)、
    不用 MCP、不存会话,工作目录是只放输入包的临时目录;codex 用只读沙箱,关掉网络搜索、
    联网插件(apps)、MCP 服务、hooks、命令行和代码执行、AGENTS.md。两边都只能看提示词作答。
    ⚠ 只写 `-c web_search="disabled"` 关不掉 codex 的联网:它还能经 apps 插件
    (codex_apps/search_service.web_run)上网,2026-11-02 实测过,所以另外 --disable apps。
  - 每个数都要给:答卷不合格(缺数、加起来不是 100、类别对不上……)就把错误清单附在
    提示词后面重问一次;还不合格就存 {"author", "failed": True, "errors"},绝不替 AI 补默认数。
  - 「怎么看」(judgment)单独校验(judgment.py):第一次不合格一起重问;第二次只有它不合格,
    就只把那几只的 judgment 换成失败记录,答卷其余部分照用(不让第一屏那一块拖垮整份答卷)。

入口:
  build_package(...)                 → 输入包(日线只留最近 250 个收盘)
  build_prompt(package)              → 提示词全文(templates/answer_prompt.md 填空)
  run_claude / run_codex(prompt, wd) → RunOutput(原始输出文本,带 model、exit_code、log)
  parse_answer(text)                 → dict(取第一个完整 JSON 对象,认 ```json 代码块)
  validate_answer(obj, codes, …)     → [错误](空 = 合格)
  answer(author, package, run, runner=None) → 答卷(同时存进 run/answers/<author>.json)

存档里留下什么(run = 一次预测的存档目录):
  ai/prompt.md                 两边收到的同一份提示词
  ai/<作者>/attemptN.out.md    每次的原始输出
  ai/<作者>/attemptN.log       命令行日志(codex 的日志里能看出有没有联网、调工具)
  ai/<作者>/attempt2.prompt.md 重问时的提示词(原提示词 + 错误清单)
  ai/<作者>/meta.json          模型标识、思考深度(effort)、提示词哈希、每次的起止时间、退出码、错误
                               思考深度:codex 读日志头的 reasoning effort: 行(它跟着本机 codex 配置走);
                               claude -p 没传这个参数,记 default。成绩页比两个 AI 时,条件不一样要看得到。
  answers/<作者>.json          合格答卷,或失败记录

⚠ import 顺序:先 fb_common(拍下带代理的环境 ORIG_ENV),再 archive(它会加载 market.py,
   market.py 一 import 就删掉代理变量)。两个命令行都要走代理,子进程一律用 ORIG_ENV。
检查:scripts/check_board_ai.py 的 check_ai_answer(不联网,runner 用假的)。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import fb_common  # 必须在 archive 之前,见上面
import archive
import judgment

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "answer_prompt.md"
HORIZONS = ("63", "126", "250")
QUANTS = ("10", "25", "50", "75", "90")
KINDS = ("sell", "trim", "hold", "wait", "buy_batches")
LATER = "later"
KEEP_CLOSES = 250           # 输入包里的日线只留这么多根,提示词不至于太长
BOTTOM_WINDOW = 126         # 和 baseline.categories 的默认值一样
BUY_HORIZON = 250
ZONE_DAYS = 63              # buy.zones:这么多个交易日以内收盘跌到某价
AI_TIMEOUT = 900            # 秒;超时算这次没答成
RETRY_ECHO = 20000          # 重问时附上一次原始输出的前多少个字
CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_BUY_EVENT = "创业板指收盘在 60 日线上,并且本股收盘在 60 日线上(同一天)"

CLAUDE_ARGS = ["claude", "-p", "--model", CLAUDE_MODEL, "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence", "--output-format", "text"]
# codex 要关掉的功能:能上网的(apps、浏览器、电脑操作)、能读本机文件的(命令行、代码执行、
# 看图、技能搜索)、会往对话里加东西的(hooks、插件、记忆)。
CODEX_DISABLE = ("apps", "browser_use", "browser_use_external", "in_app_browser", "computer_use",
                 "hooks", "plugins", "memories", "shell_tool", "unified_exec", "code_mode_host",
                 "multi_agent", "image_generation", "view_image", "skill_search")
# codex 日志里这几种开头的行 = 它调了外部工具(联网搜索、MCP)
_TOOL_LINE = re.compile(r"^(mcp: |web[ _]search\b).*$", re.I | re.M)
_MODEL_LINE = re.compile(r"^model:\s*(\S+)", re.M)
_EFFORT_LINE = re.compile(r"^reasoning effort:\s*(\S+)", re.M)
CLAUDE_EFFORT = "default"   # CLAUDE_ARGS 没传思考深度,用 claude 自己的默认值
_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n(.*?)```", re.S)
_MONTH = re.compile(r"\d{4}-\d{2}")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return fb_common.now_cn().isoformat(timespec="seconds")


# ── 输入包 ──────────────────────────────────────────────────────────────────
def _iso(x, what: str) -> str:
    if isinstance(x, date):
        return x.isoformat()
    try:
        return date.fromisoformat(str(x)).isoformat()
    except ValueError as e:
        raise ValueError(f"{what} 要是 YYYY-MM-DD,得到 {x!r}") from e


def _is_series(v) -> bool:
    """[[日期, 数], …] 这种日线。"""
    return isinstance(v, list) and bool(v) and all(
        isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], str) for x in v)


def _trim_inputs(items: dict, keep: int) -> dict:
    """每项 value 里的日线只留最后 keep 根,其余原样。返回新字典。"""
    def trim(value):
        if not isinstance(value, dict):
            return value
        return {k: (v[-keep:] if _is_series(v) and len(v) > keep else v) for k, v in value.items()}
    return {k: ({**it, "value": trim(it.get("value"))} if isinstance(it, dict) else it)
            for k, it in (items or {}).items()}


# 给 AI 的账户数据:持股数、还款数(每月要还的钱、融资负债),加上可取现金和后续缺口 —— 这些是账户主人同意给的。
# 可取现金和缺口是后来加的:不给的话 AI 以为现金是 0,会拿「凑这个月的还款」当卖出理由,
# 分不清是这个月缺钱还是后面几个月缺钱(2026-11-03 真跑时两份答卷都这样)。
# 成本、截图价、净资产、备注都不给 —— 看板自己算动作照旧读完整的账户数据,只在这里筛。
AI_ACCOUNT_KEYS = ("asof", "holdings", "margin_debt", "month_need", "need_month", "schedule",
                   "cash", "free_cash", "free_cash_until")


def account_for_ai(account: dict | None) -> dict | None:
    """完整的账户数据(account.account_state)→ 给 AI 的那一份:只留 AI_ACCOUNT_KEYS,持仓每只只留股数。"""
    if not isinstance(account, dict):
        return None
    out = {k: account.get(k) for k in AI_ACCOUNT_KEYS if k != "holdings"}
    out["holdings"] = {str(c): {"shares": (h or {}).get("shares")} for c, h in (account.get("holdings") or {}).items()}
    return out


def build_package(as_of, start, codes, start_close: dict, categories: dict, inputs: dict,
                  missing_required: list, baseline: dict, system: dict, account: dict | None = None,
                  rules: dict | None = None, research_notes: str | None = None,
                  bottom_window: int = BOTTOM_WINDOW, buy_horizon: int = BUY_HORIZON) -> dict:
    """两个 AI 共用的冻结输入包。start 是 as_of 的下一个交易日;start_close 是答卷里
    绝对价格用的那种价(as_of 那天的收盘)。返回的是深拷贝,之后改传进来的东西不影响它;
    能存成 JSON 也在这里顺带确认。"""
    codes = [str(c) for c in codes or []]
    if not codes:
        raise ValueError("codes 是空的")
    for name, d in (("start_close", start_close), ("baseline", baseline), ("system", system)):
        lack = [c for c in codes if c not in (d or {})]
        if lack:
            raise ValueError(f"{name} 里没有 {lack}")
    bad = [c for c in codes if not (_is_num(start_close[c]) and start_close[c] > 0)]
    if bad:
        raise ValueError(f"起点价要是大于 0 的数:{ {c: start_close[c] for c in bad} }")
    lack = [k for k in ("bottom_months", "buy_months", "price_bins") if not (categories or {}).get(k)]
    if lack:
        raise ValueError(f"categories 缺 {lack}")
    pkg = {"as_of": _iso(as_of, "as_of"), "start": _iso(start, "start"), "codes": codes,
           "start_close": {c: float(start_close[c]) for c in codes}, "categories": categories,
           "windows": {"bottom": bottom_window, "buy": buy_horizon, "zones": ZONE_DAYS},
           "inputs": _trim_inputs(inputs, KEEP_CLOSES), "missing_required": list(missing_required or []),
           "baseline": {c: baseline[c] for c in codes}, "system": {c: system[c] for c in codes},
           "account": account_for_ai(account), "rules": rules or {}, "research_notes": research_notes}
    return json.loads(json.dumps(pkg, ensure_ascii=False))


# ── 提示词 ──────────────────────────────────────────────────────────────────
def _p(x) -> str:
    return f"{x:.2f}" if _is_num(x) else "?"


def _bin_label(lo, hi, last: bool) -> str:
    return f"[{lo}, {hi}]" if last else f"[{lo}, {hi})"


def _layer_lines(title: str, layer: dict, close: float) -> list[str]:
    """参考层的数,价位按 close ÷ 该层 start_price 换算成答卷用的价。"""
    sp = layer.get("start_price")
    k = close / sp if _is_num(sp) and sp > 0 else 1.0
    note = f"价位已按 start_close 换算,系数 {k:.4f}" if k != 1.0 else "价位和 start_close 同一种价"
    out = [f"- {title}({note}):"]
    for h, b in sorted((layer.get("horizons") or {}).items(), key=lambda kv: int(kv[0])):
        q = b.get("q") or {}
        qs = " / ".join(_p(q[x] * k) if _is_num(q.get(x)) else "?" for x in QUANTS)
        out.append(f"  - 第 {h} 个交易日:涨的概率 {b.get('p_up')};分位价 10/25/50/75/90 = {qs}")
    bottom = layer.get("bottom") or {}
    if bottom.get("month_probs"):
        out.append("  - 最低点月份:" + " · ".join(f"{m} {v}" for m, v in bottom["month_probs"].items()))
    bins = bottom.get("price_bins") or []
    if bins:
        out.append("  - 最低价分段:" + " · ".join(
            f"{_bin_label(r[0], r[1], i == len(bins) - 1)} {r[2]}" for i, r in enumerate(bins)))
    buy = (layer.get("buy") or {}).get("month_probs")
    if buy:
        out.append("  - 买点月份:" + " · ".join(f"{m} {v}" for m, v in buy.items()))
    return out


def _stock_tables(pkg: dict) -> str:
    blocks = []
    for c in pkg["codes"]:
        close = pkg["start_close"][c]
        bins = pkg["categories"]["price_bins"]
        lines = [f"### {c}", "", f"- start_close:{_p(close)}", "- 最低价分段(答卷写比值;后面的价位只是方便看):"]
        lines += [f"  - {_bin_label(lo, hi, i == len(bins) - 1)}:{_p(lo * close)} ~ {_p(hi * close)}"
                  for i, (lo, hi) in enumerate(bins)]
        lines += _layer_lines("统计底线,不带方向", pkg["baseline"][c], close)
        sys_layer = pkg["system"][c]
        extra = f";方向 tilt = {sys_layer['tilt']}" if "tilt" in sys_layer else ""
        unval = "、".join(sys_layer.get("unvalidated") or []) or "无"
        lines += _layer_lines(f"系统算法,尚未验证{extra};没检验过的信号:{unval}", sys_layer, close)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _buy_event(pkg: dict) -> str:
    for c in pkg["codes"]:
        cond = ((pkg["system"].get(c) or {}).get("buy") or {}).get("conditions")
        if cond:
            return ",并且".join(cond) + "(同一天)"
    return DEFAULT_BUY_EVENT


def _skeleton(pkg: dict) -> str:
    cats = pkg["categories"]
    one = {"horizons": {h: {"p_up": None, "q": dict.fromkeys(QUANTS)} for h in HORIZONS},
           "bottom": {"month_probs": dict.fromkeys(cats["bottom_months"]),
                      "price_bins": [[lo, hi, None] for lo, hi in cats["price_bins"]]},
           "buy": {"month_probs": dict.fromkeys(cats["buy_months"]),
                   "zones": [[None, None], [None, None], [None, None]], "conditions": ["…"]},
           "action": {"kind": "sell / trim / hold / wait / buy_batches 选一个", "shares": None,
                      "amount": None, "deadline": "YYYY-MM-DD", "why": "…",
                      "change_if": [{"if": "…", "then": "…"}]},
           "evidence": {"support": ["…"], "against": ["…"], "unverified": ["…"]},
           "judgment": judgment.skeleton()}
    return json.dumps({"stocks": {c: one for c in pkg["codes"]}}, ensure_ascii=False, indent=2)


def _compact(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _package_text(pkg: dict) -> str:
    """整份输入包,仍是合法 JSON;inputs / baseline / system 每项一行,好读。"""
    rows = []
    for k in sorted(pkg):
        v = pkg[k]
        if k in ("inputs", "baseline", "system") and isinstance(v, dict) and v:
            inner = ",\n".join(f"  {_compact(kk)}: {_compact(vv)}" for kk, vv in sorted(v.items()))
            rows.append(f" {_compact(k)}: {{\n{inner}\n }}")
        else:
            rows.append(f" {_compact(k)}: {_compact(v)}")
    return "{\n" + ",\n".join(rows) + "\n}"


def build_prompt(package: dict) -> str:
    """templates/answer_prompt.md 填空。同一份输入包永远生成同一份提示词(两个 AI 共用)。"""
    pkg = package
    win = pkg.get("windows") or {}
    rules = pkg.get("rules") or {}
    values = {
        "N": str(len(pkg["codes"])), "CODES": "、".join(pkg["codes"]),
        "AS_OF": pkg["as_of"], "START": pkg["start"],
        "BOTTOM_WINDOW": str(win.get("bottom", BOTTOM_WINDOW)), "BUY_HORIZON": str(win.get("buy", BUY_HORIZON)),
        "ZONE_DAYS": str(win.get("zones", ZONE_DAYS)), "BUY_EVENT": _buy_event(pkg),
        "RULES": (json.dumps(rules, ensure_ascii=False, indent=1, sort_keys=True) if rules
                  else "(这次没有给已定规则)"),
        "STOCK_TABLES": _stock_tables(pkg), "SKELETON": _skeleton(pkg), "PACKAGE_JSON": _package_text(pkg),
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    unused = set(values) - set(_PLACEHOLDER.findall(template))
    if unused:
        raise ValueError(f"模板 {TEMPLATE} 里没有这些占位符:{sorted(unused)}")
    # 一次替换完:填进去的内容(输入包、研究笔记)里就算出现 {{X}} 也不会再被替换
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


# ── 调两个命令行 ────────────────────────────────────────────────────────────
class RunOutput(str):
    """AI 的原始输出文本(本身就是 str),另带这次调用的记录:model、effort(思考深度)、exit_code、
    log(命令行日志)。"""

    def __new__(cls, text: str, model: str | None = None, exit_code: int | None = None, log: str = "",
                effort: str | None = None):
        obj = super().__new__(cls, text)
        obj.model, obj.exit_code, obj.log, obj.effort = model, exit_code, log, effort
        return obj


class RunFailed(RuntimeError):
    """这次调用没答成:超时、退出码不是 0、命令不存在、codex 联网了……"""

    def __init__(self, msg: str, exit_code: int | None = None, log: str = ""):
        super().__init__(msg)
        self.exit_code, self.log = exit_code, log


def _tail(s, n: int = 800) -> str:
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]


def _log_text(s) -> str:
    return s.decode("utf-8", "replace") if isinstance(s, bytes) else (s or "")


def _call(argv: list[str], prompt: str, workdir: Path, timeout: int, who: str):
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=str(workdir),
                           env=fb_common.ORIG_ENV, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RunFailed(f"{who} 超时({timeout} 秒)", None, _log_text(e.stderr)) from e
    except OSError as e:
        raise RunFailed(f"{who} 起不来:{e}") from e
    if p.returncode != 0:
        raise RunFailed(f"{who} 退出码 {p.returncode}:{_tail(p.stderr or p.stdout)}", p.returncode, p.stderr)
    return p


def run_claude(prompt: str, workdir: Path, timeout: int = AI_TIMEOUT) -> RunOutput:
    """`claude -p` 作答:没有工具、不读设置文件和 CLAUDE.md、不用 MCP、不存会话;
    工作目录 = workdir(只放输入包的临时目录),提示词走标准输入。"""
    p = _call(CLAUDE_ARGS, prompt, workdir, timeout, "claude")
    return RunOutput(p.stdout, model=CLAUDE_MODEL, exit_code=p.returncode, log=p.stderr, effort=CLAUDE_EFFORT)


def codex_argv(workdir: Path) -> list[str]:
    argv = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", str(workdir),
            "-c", 'web_search="disabled"', "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0"]
    for feature in CODEX_DISABLE:
        argv += ["--disable", feature]
    return argv + ["-o", str(Path(workdir) / "out.md"), "-"]


def run_codex(prompt: str, workdir: Path, timeout: int = AI_TIMEOUT) -> RunOutput:
    """`codex exec` 作答(见 codex_argv);答案读 -o 写的 out.md,模型标识读日志头的 model: 行。
    日志里出现联网搜索或 MCP 调用 → RunFailed:两边拿到的材料不一样了,这次作答作废。"""
    p = _call(codex_argv(workdir), prompt, workdir, timeout, "codex")
    used = _TOOL_LINE.findall(p.stderr or "")
    if used:
        raise RunFailed(f"codex 联网或调了外部工具({'; '.join(used[:3])}),两边材料不一样了,作答作废",
                        p.returncode, p.stderr)
    out = Path(workdir) / "out.md"
    if not out.is_file():
        raise RunFailed(f"codex 没写出 {out.name}:{_tail(p.stderr)}", p.returncode, p.stderr)
    m, eff = _MODEL_LINE.search(p.stderr or ""), _EFFORT_LINE.search(p.stderr or "")
    return RunOutput(out.read_text(encoding="utf-8"), model=m.group(1) if m else None,
                     exit_code=p.returncode, log=p.stderr, effort=eff.group(1) if eff else None)


# ── 解析 ────────────────────────────────────────────────────────────────────
def parse_answer(text: str) -> dict:
    """先找 ```json 代码块,再从头找第一个能完整解析的 {…}。顶层必须是对象。找不到抛 ValueError。"""
    text = str(text)
    for m in _FENCE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("回答里找不到完整的 JSON 对象")


# ── 校验 ────────────────────────────────────────────────────────────────────
def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _prob(x) -> bool:
    return _is_int(x) and 0 <= x <= 100


def _text(x) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _is_date(x) -> bool:
    if not (isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)):
        return False
    try:
        date.fromisoformat(x)
    except ValueError:
        return False
    return True


def _check_horizons(c: str, hz, horizons) -> list[str]:
    if not isinstance(hz, dict):
        return [f"{c}.horizons 缺了"]
    out = []
    for h in map(str, horizons):
        where, b = f"{c}.horizons.{h}", hz.get(str(h))
        if not isinstance(b, dict):
            out.append(f"{where} 缺了")
            continue
        if not _prob(b.get("p_up")):
            out.append(f"{where}.p_up 要是 0~100 的整数,得到 {b.get('p_up')!r}")
        q = b.get("q")
        bad = [k for k in QUANTS if not (isinstance(q, dict) and _is_num(q.get(k)) and q[k] > 0)]
        if bad:
            out.append(f"{where}.q 的 {'、'.join(bad)} 分位要是大于 0 的数")
            continue
        out += [f"{where}.q 分位要严格递增:{a} 分位 {q[a]} 不小于 {b2} 分位 {q[b2]}"
                for a, b2 in zip(QUANTS, QUANTS[1:]) if not q[a] < q[b2]]
    return out


def _check_probs(where: str, probs, keys=None, key_ok=None) -> list[str]:
    """一组分类概率:每项 0~100 整数、加起来 100;给了 keys 就要一字不差,没给就用 key_ok 查键的写法。"""
    if not isinstance(probs, dict) or not probs:
        return [f"{where} 缺了或是空的"]
    out = []
    bad = [k for k, v in probs.items() if not _prob(v)]
    if bad:
        out.append(f"{where} 里 {'、'.join(bad)} 要是 0~100 的整数")
    if keys is not None:
        miss, extra = [k for k in keys if k not in probs], [k for k in probs if k not in keys]
        if miss:
            out.append(f"{where} 少了类别 {'、'.join(miss)}(类别要和给定的一字不差)")
        if extra:
            out.append(f"{where} 多了类别 {'、'.join(extra)}(类别要和给定的一字不差)")
    elif key_ok is not None:
        out += [f"{where} 的类别 {k!r} 写法不对" for k in probs if not key_ok(k)]
    if not bad and sum(probs.values()) != 100:
        out.append(f"{where} 加起来 {sum(probs.values())},要正好 100")
    return out


def _check_bins(where: str, bins, cat_bins=None) -> list[str]:
    if not isinstance(bins, list) or not bins:
        return [f"{where} 缺了或是空的"]
    if not all(isinstance(r, list) and len(r) == 3 and _is_num(r[0]) and _is_num(r[1]) for r in bins):
        return [f"{where} 每一项要是 [下限比值, 上限比值, 概率]"]
    out = []
    probs = [r[2] for r in bins]
    if not all(_prob(p) for p in probs):
        out.append(f"{where} 的概率要是 0~100 的整数")
    elif sum(probs) != 100:
        out.append(f"{where} 加起来 {sum(probs)},要正好 100")
    if cat_bins is not None:
        same = len(bins) == len(cat_bins) and all(
            abs(r[0] - lo) < 1e-9 and abs(r[1] - hi) < 1e-9 for r, (lo, hi) in zip(bins, cat_bins))
        if not same:
            out.append(f"{where} 的分段要和给定的一样(写比值,按顺序):{cat_bins}")
    elif any(r[0] >= r[1] for r in bins):
        out.append(f"{where} 每段的下限要小于上限")
    return out


def _check_zones(where: str, zones) -> list[str]:
    if not isinstance(zones, list) or not zones:
        return [f"{where} 至少给一个价位"]
    if not all(isinstance(z, list) and len(z) == 2 and _is_num(z[0]) and z[0] > 0 and _prob(z[1]) for z in zones):
        return [f"{where} 每一项要是 [价格(大于 0), 0~100 的整数概率]"]
    if len({z[0] for z in zones}) != len(zones):
        return [f"{where} 的价格不能重复"]
    srt = sorted(zones, key=lambda z: z[0])
    if any(a[1] > b[1] for a, b in zip(srt, srt[1:])):
        return [f"{where} 前后矛盾:价越低,跌到它的概率不能越高"]
    return []


def _check_buy(c: str, buy, cats) -> list[str]:
    where = f"{c}.buy"
    if not isinstance(buy, dict):
        return [f"{where} 缺了"]
    keys = cats["buy_months"] if cats else None
    out = _check_probs(f"{where}.month_probs", buy.get("month_probs"), keys,
                       lambda k: k == LATER or _MONTH.fullmatch(k) is not None)
    if keys is None and isinstance(buy.get("month_probs"), dict) and LATER not in buy["month_probs"]:
        out.append(f"{where}.month_probs 少了 {LATER!r}(期限内都没出现)")
    out += _check_zones(f"{where}.zones", buy.get("zones"))
    cond = buy.get("conditions")
    if not (isinstance(cond, list) and cond and all(_text(x) for x in cond)):
        out.append(f"{where}.conditions 至少写一条条件(文字)")
    return out


def _check_action(c: str, a) -> list[str]:
    where = f"{c}.action"
    if not isinstance(a, dict):
        return [f"{where} 缺了"]
    out = []
    if a.get("kind") not in KINDS:
        out.append(f"{where}.kind 要是 {' / '.join(KINDS)} 之一,得到 {a.get('kind')!r}")
    if "shares" not in a or not (a["shares"] is None or (_is_int(a["shares"]) and a["shares"] >= 0)):
        out.append(f"{where}.shares 要是不小于 0 的整数,不适用写 null")
    if "amount" not in a or not (a["amount"] is None or (_is_num(a["amount"]) and a["amount"] >= 0)):
        out.append(f"{where}.amount 要是不小于 0 的数,不适用写 null")
    if not _is_date(a.get("deadline")):
        out.append(f"{where}.deadline 要是 YYYY-MM-DD,得到 {a.get('deadline')!r}")
    if not _text(a.get("why")):
        out.append(f"{where}.why 要写理由")
    ci = a.get("change_if")
    if not (isinstance(ci, list) and ci and all(
            isinstance(x, dict) and _text(x.get("if")) and _text(x.get("then")) for x in ci)):
        out.append(f'{where}.change_if 至少一条 {{"if": 条件, "then": 改成什么}}')
    return out


def _check_evidence(c: str, ev) -> list[str]:
    if not isinstance(ev, dict):
        return [f"{c}.evidence 缺了"]
    return [f"{c}.evidence.{k} 要是一串文字(可以是空列表)" for k in ("support", "against", "unverified")
            if not (isinstance(ev.get(k), list) and all(_text(x) for x in ev[k]))]


def validate_answer(obj: dict, codes: list[str], horizons=HORIZONS, categories: dict | None = None) -> list[str]:
    """答卷的错误清单,空 = 合格。每条错误点出位置(股票.块.字段)。
    categories 给了(输入包总会给)就要求三组分类的类别和它一字不差:
    bottom.month_probs 的键 = bottom_months,buy.month_probs 的键 = buy_months(含 later),
    bottom.price_bins = price_bins 的比值分段、按顺序。"""
    if not isinstance(obj, dict) or not isinstance(obj.get("stocks"), dict):
        return ['答卷要是 {"stocks": {代码: …}} 这样的 JSON 对象']
    stocks, codes = obj["stocks"], [str(c) for c in codes]
    out = [f"stocks 里缺 {c}" for c in codes if c not in stocks]
    out += [f"stocks 里多了没问的 {c}" for c in stocks if c not in codes]
    for c in (c for c in codes if c in stocks):
        s = stocks[c]
        if not isinstance(s, dict):
            out.append(f"{c} 要是对象")
            continue
        out += _check_horizons(c, s.get("horizons"), horizons)
        bottom = s.get("bottom") if isinstance(s.get("bottom"), dict) else {}
        out += _check_probs(f"{c}.bottom.month_probs", bottom.get("month_probs"),
                            categories["bottom_months"] if categories else None,
                            lambda k: _MONTH.fullmatch(k) is not None)
        out += _check_bins(f"{c}.bottom.price_bins", bottom.get("price_bins"),
                           categories["price_bins"] if categories else None)
        out += _check_buy(c, s.get("buy"), categories)
        out += _check_action(c, s.get("action"))
        out += _check_evidence(c, s.get("evidence"))
    return out


# ── 作答 ────────────────────────────────────────────────────────────────────
RUNNERS = {"claude": run_claude, "codex": run_codex}


def _save_prompt(path: Path, prompt: str) -> None:
    """提示词只存一份。已有且内容不同 → 两个 AI 拿到的不是同一份,拒绝。"""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old != prompt:
            raise ValueError(f"{path} 里已有另一份提示词(sha256 {sha256(old)[:12]} ≠ {sha256(prompt)[:12]}):"
                             "两个 AI 必须拿同一份输入包和提示词")
        return
    archive._write_text_once(path, prompt)


def _retry_prompt(prompt: str, raw: str | None, errors: list[str]) -> str:
    """原提示词 + 错误清单 + 上一次的原始输出。上次是调用失败(超时等)、没有输出,就原样再问。"""
    if raw is None:
        return prompt
    shown = raw if len(raw) <= RETRY_ECHO else raw[:RETRY_ECHO] + "\n…(后面截掉了)"
    return (prompt + "\n\n## 上一次的回答没通过检查\n\n问题:\n" + "\n".join(f"- {e}" for e in errors)
            + "\n\n上一次的原始输出:\n\n<previous_output>\n" + shown + "\n</previous_output>\n\n"
            "改正上面每一个问题,重新输出完整答卷。仍然只输出一个 JSON 对象。")


def _one_try(runner, ask: str, package: dict, lenient: bool = False) -> tuple[str | None, dict, dict | None, str]:
    """跑一次:建只放输入包的临时目录 → runner → 解析 → 校验。
    返回 (原始输出,调用失败时 None;这次的记录;合格的答卷,不合格时 None;命令行日志)。
    「怎么看」(judgment)那一块单独校验:lenient=False 时它不合格也算这次不合格(好重问);
    lenient=True(最后一次)时只把不合格的那几只的 judgment 换成失败记录,答卷其余部分照用,
    错误记在 rec["judgment_errors"]。"""
    rec = {"started_at": _now(), "model": None, "effort": None, "exit_code": None}
    workdir = Path(tempfile.mkdtemp(prefix="fb-ai-"))
    try:
        (workdir / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=1, sort_keys=True),
                                              encoding="utf-8")
        try:
            out = runner(ask, workdir)
        except Exception as e:      # 超时、退出码不是 0、命令不存在……都算这次没答成,照实记下
            rec.update(ended_at=_now(), exit_code=getattr(e, "exit_code", None), errors=[f"调用失败:{e}"])
            return None, rec, None, _log_text(getattr(e, "log", ""))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    rec.update(ended_at=_now(), model=getattr(out, "model", None), effort=getattr(out, "effort", None),
               exit_code=getattr(out, "exit_code", None))
    log = _log_text(getattr(out, "log", ""))
    try:
        obj = parse_answer(out)
    except ValueError as e:
        rec["errors"] = [str(e)]
        return str(out), rec, None, log
    core = validate_answer(obj, package["codes"], categories=package["categories"])
    jerr = judgment.check_all(obj, package["codes"], package["as_of"], judgment.known_sources(package))
    if core or (jerr and not lenient):
        rec["errors"] = core + jerr
        return str(out), rec, None, log
    rec["errors"], rec["judgment_errors"] = [], jerr
    return str(out), rec, (judgment.mark_failed(obj, jerr) if jerr else obj), log


def answer(author: str, package: dict, run: Path, runner=None) -> dict:
    """author 作答一次:不合格把错误附上重问一次,还不合格存失败记录。
    runner(prompt, workdir) -> str,不给就按作者用 run_claude / run_codex(检查时换成假的)。
    返回答卷:{"author", "model", "as_of", "stocks"};失败是 {"author", "failed": True, "errors"}。
    同一作者在同一个 run 里只能答一次(文件只写一次,再答抛 FileExistsError)。"""
    if not re.fullmatch(r"[\w.\-]+", author or ""):
        raise ValueError(f"作者名只许字母数字下划线点横线,得到 {author!r}")
    if runner is None:
        if author not in RUNNERS:
            raise ValueError(f"不认识的作者 {author!r}:只有 {sorted(RUNNERS)},别的要自己给 runner")
        runner = RUNNERS[author]
    run = Path(run)
    prompt = build_prompt(package)
    _save_prompt(run / "ai" / "prompt.md", prompt)
    adir = run / "ai" / author
    if adir.exists():
        raise FileExistsError(f"{adir} 已存在:同一作者在同一次存档里只答一次")
    attempts, raw, obj = [], None, None
    for n in (1, 2):
        ask = prompt if n == 1 else _retry_prompt(prompt, raw, attempts[-1]["errors"])
        if ask != prompt:
            archive._write_text_once(adir / f"attempt{n}.prompt.md", ask)
        raw, rec, obj, log = _one_try(runner, ask, package, lenient=n == 2)
        if raw is not None:
            archive._write_text_once(adir / f"attempt{n}.out.md", raw)
        if log:     # codex 的日志里能看出它有没有联网、调工具,留着备查
            archive._write_text_once(adir / f"attempt{n}.log", log)
        attempts.append({"n": n, "prompt_sha256": sha256(ask), **rec,
                         "out_file": f"attempt{n}.out.md" if raw is not None else None,
                         "log_file": f"attempt{n}.log" if log else None})
        if obj is not None:
            break
    model = next((a["model"] for a in reversed(attempts) if a["model"]), None)
    effort = next((a["effort"] for a in reversed(attempts) if a.get("effort")), None)
    archive.write_json_once(adir / "meta.json", {
        "author": author, "model": model, "effort": effort, "prompt_sha256": sha256(prompt),
        "prompt_file": "../prompt.md",
        "timeout_s": AI_TIMEOUT, "failed": obj is None, "attempts": attempts})
    if obj is None:
        result = {"author": author, "failed": True,
                  "errors": [f"第 {a['n']} 次:{e}" for a in attempts for e in a["errors"]]}
    else:
        result = {"author": author, "model": model or "未知(runner 没报模型)", "as_of": package["as_of"],
                  "stocks": obj["stocks"]}
    archive.save_answer(run, author, result)
    return result
