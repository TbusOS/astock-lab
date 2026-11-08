#!/usr/bin/env python3
"""attribution —— 一道题错了,给每个可能的原因一个概率;人确认后只追加存档。

它是什么:到期核对时,每道错题列出七个候选原因,各给一个 0~100 的整数,加起来正好 100。
程序先按几条规则给初值(suggest),人看过证据后改定(confirm),确认人和时间一起存下来。
没有它会怎样:错了只能说「判断不了」—— 分不清是数据没取全、算法有问题,还是本来就有三成不涨。
硬规定:不许说判断不了,每个原因都要给概率,哪怕很小。

为什么是「起点均分 + 加分」而不是直接判一个原因:几条规则看到的都只是线索,不是定论;
加分以后各项仍然都大于 0,人确认时从这个初值往上下调,比从一张白纸开始更快。

suggest 的规则(每条最多触发一次;「这道题」= q 那一层的答案):
  起点     七个原因各 100/7 分
  data_gap +30  这道题用到的输入(used_keys)里有一项 partial / failed / stale,
                或者输入包里根本没有这一项(没取到等于没取全)
  system   +20  系统层得分比底线差(scored_layers 里 system > baseline,分数越低越好),
                并且这道题跟着系统:这道题就是系统层的;或者是 AI 的,且 AI 和系统给
                「实际发生的那一项」的概率差不到 15
  ai       +30  这道题是 AI 的,AI 和系统给「实际发生的那一项」的概率差 ≥ 15(推翻了系统),
                并且 AI 的得分比系统差
  luck     +20  这道题给「实际发生的那一项」的概率 ≥ 30(说七成会涨,本来就有三成不涨)
  最后按比例换成整数,用最大余数法让总和正好 100(baseline.largest_remainder)。
  data_error / impl_error / unforeseeable 没有自动规则,只拿起点那份,靠人确认时调。
「实际发生的那一项」:是非题(含区间题)结果是 1 取 pct、是 0 取 100 − pct;分类题取实际那一类的数。
「涨的种数差」就是是非题的 pct 差 —— 两边同一个结果,概率差和 pct 差相等,所以分类题也能用同一条。

存档(都在 archive_root(),只追加,每行一条 JSON,带 id 和 at):
  reviews.jsonl        原因概率的确认记录。同一道题可以确认多次(重跑出了新证据),以最后一次为准。
                       who 是 user 的才算人确认;AI 写的(演示、提议)成绩页只当提议显示,
                       也不进按原因汇总(board_scores.pick_reviews / is_human)
  improvements.jsonl   要改的地方。add_improvement 加一项(状态 todo);mark_improved 另追加一行
                       状态行指向那一项,不改原来那行;load_improvements 把状态行合进去
写法:追加模式打开、写一行、flush + fsync;从不重写、不截断。
  - 一律 ensure_ascii:每行都是纯 ASCII,备注里的 U+2028 / U+2029 / U+0085 这类「也算换行」的字符
    写成 \\uXXXX,读的时候也只按 "\\n" 切行(str.splitlines 会在这些字符上切断一条记录)。
  - 最后一行末尾缺换行:能解析的(完整的一条,只是没换行)先补一个换行再写;
    解析不了的是写到一半崩了 —— 读的时候跳过并往 stderr 打警告,但不再往后追加(抛 ValueError):
    接着写会让那半行变成中间行,整个文件就读不了了,又不许截断,只能等人处理。
  - 中间某行坏了照样抛 ValueError(带行号),只有最后那半行可以跳过。

检查:scripts/check_board_review.py。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import fb_common  # 先于 baseline:baseline 会加载 prices → market.py,它一 import 就删掉代理变量
import prices
from baseline import largest_remainder

CAUSES = ["luck", "system", "ai", "data_gap", "data_error", "impl_error", "unforeseeable"]
CAUSE_LABELS = {"luck": "正常的运气", "system": "系统算法", "ai": "AI 判断", "data_gap": "数据没取全",
                "data_error": "数据内容或单位错", "impl_error": "程序实现错误",
                "unforeseeable": "事先没法知道的事"}
BONUS = {"data_gap": 30, "system": 20, "ai": 30, "luck": 20}
BAD_STATUS = ("partial", "failed", "stale")
AI_LAYERS = ("claude", "codex")
FOLLOW_GAP = 15          # AI 和系统给实际那一项的概率差 ≥ 这个数算推翻,< 算跟着
LUCK_MIN = 30            # 给实际那一项的概率 ≥ 这个数,错了也可能只是运气
REVIEWS = "reviews.jsonl"
IMPROVEMENTS = "improvements.jsonl"
IMPROVEMENT_LABELS = {"todo": "待改", "done": "已改"}


# ── 给初值 ──────────────────────────────────────────────────────────────────
def items_of(inputs: dict) -> dict:
    """输入包 → {键: 一项}。存档时可能包了一层 {"items": …, "missing_required": …},两种都认
    (输入项的键是 prices.<代码>、capex 这类,不会叫 items)。"""
    inner = inputs.get("items") if isinstance(inputs, dict) else None
    return inner if isinstance(inner, dict) else (inputs or {})


def p_hit(forecast, outcome) -> int | None:
    """这份答案给「实际发生的那一项」的概率(0~100);没有概率或还没有结果返回 None。"""
    if not isinstance(forecast, dict) or outcome is None:
        return None
    if "pct" in forecast:
        return forecast["pct"] if outcome == 1 else (100 - forecast["pct"] if outcome == 0 else None)
    v = forecast.get(outcome, 0) if isinstance(outcome, str) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _worse(scores: dict, a: str, b: str) -> bool:
    """a 层的分数比 b 层差(分数越低越好);任一层没有分数(缺这一层、或是 None)就不算。"""
    sa, sb = scores.get(a), scores.get(b)
    num = (int, float)
    return (isinstance(sa, num) and isinstance(sb, num) and not isinstance(sa, bool)
            and not isinstance(sb, bool) and sa > sb)


def _gap_reason(items: dict, used_keys: list[str]) -> str | None:
    bad = [f"{k} {(items.get(k) or {}).get('status') or '输入包里没有'}"
           for k in used_keys if not isinstance(items.get(k), dict) or items[k].get("status") in BAD_STATUS]
    return f"用到的输入不全:{'、'.join(bad)}" if bad else None


def reasons(q: dict, scored_layers: dict[str, float], inputs: dict[str, dict], used_keys: list[str],
            forecasts: dict[str, dict] | None = None) -> list[dict]:
    """触发了哪几条规则:[{"cause", "points", "why"}],顺序同模块说明。why 可以直接当证据给人看。
    forecasts = 同一道题各层的答案 {作者: forecast};q 自己那层的答案不用重复给。
    没给系统层的答案时,「跟没跟系统」判断不了,system / ai 两条都不触发。"""
    author, outcome = q.get("author"), (q.get("resolution") or {}).get("outcome")
    fcs = {**(forecasts or {}), author: q.get("forecast")}
    own, sys_hit = p_hit(fcs[author], outcome), p_hit(fcs.get("system"), outcome)
    out = []
    why = _gap_reason(items_of(inputs), list(used_keys))
    if why:
        out.append({"cause": "data_gap", "points": BONUS["data_gap"], "why": why})
    gap = abs(own - sys_hit) if own is not None and sys_hit is not None else None
    follows = author == "system" or (author in AI_LAYERS and gap is not None and gap < FOLLOW_GAP)
    if follows and _worse(scored_layers, "system", "baseline"):
        out.append({"cause": "system", "points": BONUS["system"],
                    "why": f"系统层得分 {scored_layers['system']:.4g} 比底线 {scored_layers['baseline']:.4g} 差,"
                           f"{'这道就是系统层的' if author == 'system' else f'{author} 跟着系统(差 {gap})'}"})
    if author in AI_LAYERS and gap is not None and gap >= FOLLOW_GAP and _worse(scored_layers, author, "system"):
        out.append({"cause": "ai", "points": BONUS["ai"],
                    "why": f"{author} 推翻了系统(给实际那一项 {own} 对 {sys_hit},差 {gap}),"
                           f"得分 {scored_layers[author]:.4g} 比系统 {scored_layers['system']:.4g} 差"})
    if own is not None and own >= LUCK_MIN:
        out.append({"cause": "luck", "points": BONUS["luck"],
                    "why": f"当时给实际发生的那一项 {own}(≥ {LUCK_MIN}),本来就有这么大的机会"})
    return out


def suggest(q: dict, scored_layers: dict[str, float], inputs: dict[str, dict], used_keys: list[str],
            forecasts: dict[str, dict] | None = None) -> dict[str, int]:
    """七个原因的建议概率 {原因: 整数},顺序同 CAUSES,加起来正好 100,永远不空。
    q = 这道题的评分记录(题目 + "resolution");scored_layers = 各层在这道题上的分数
    (Brier 或区间得分,越低越好);inputs = 冻结的输入包;used_keys = 这道题用到的输入键
    (系统层的题用 system_keys);forecasts 见 reasons。规则和分数见模块说明。
    要连证据一起存的(board_settle.with_suggest)先调 reasons,再用 probs_from 换成概率,规则只跑一遍。"""
    return probs_from(reasons(q, scored_layers, inputs, used_keys, forecasts))


def probs_from(fired: list[dict]) -> dict[str, int]:
    """reasons 的结果 → 七个原因的概率 {原因: 整数},加起来正好 100。"""
    n = len(CAUSES)
    weights = dict.fromkeys(CAUSES, 100)          # 起点各 100/n 分,整体乘 n 免得算小数
    for r in fired:
        weights[r["cause"]] += r["points"] * n
    return dict(zip(CAUSES, largest_remainder([weights[c] for c in CAUSES])))


def system_keys(code: str, inputs: dict[str, dict]) -> list[str]:
    """系统层一道题用到的输入键:本股日线、创业板指日线、五个信号读的键(system_forecast 里
    signals_from_inputs 读的那几项),加上输入包里含本股的同行对 peer.<A>.<B>。"""
    keys = [f"prices.{code}", f"index.{prices.CALENDAR_INDEX}", "market.regime", f"consensus.{code}",
            f"eps_revisions.{code}", "capex", f"valuation.{code}"]
    peers = []
    for k in sorted(items_of(inputs)):
        parts = k.split(".")
        if len(parts) == 3 and parts[0] == "peer" and parts[1] != parts[2] and code in parts[1:]:
            peers.append(k)
    return keys + peers


# ── 只追加的存档 ────────────────────────────────────────────────────────────
def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else fb_common.archive_root()


def _stamp() -> dict:
    return {"id": uuid.uuid4().hex, "at": fb_common.now_cn().isoformat(timespec="seconds")}


def _parses(raw: bytes) -> bool:
    try:
        json.loads(raw.decode("utf-8"))
        return True
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def _append(path: Path, rec: dict) -> dict:
    """追加一行纯 ASCII 的 JSON 并落盘。最后一行缺换行:完整的先补换行;写到一半的抛 ValueError、不写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(rec, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    with open(path, "a+b") as f:            # 追加模式:不管读到哪,写总在文件末尾
        f.seek(0)
        data = f.read()
        if data and not data.endswith(b"\n"):
            tail = data[data.rfind(b"\n") + 1:]
            if tail.strip() and not _parses(tail):
                raise ValueError(f"{path} 最后一行没写完整(写到一半崩了),不能接着往后写:"
                                 "先人工处理那一行(补全或挪走整个文件),别截断前面的行")
            line = b"\n" + line
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return rec


def _read_lines(path: Path) -> list[dict]:
    """按 "\n" 切行读回(不用 splitlines:它会在 U+2028 / U+0085 这些字符上把一条记录切断)。
    最后一行没换行又解析不了 = 写到一半崩了:跳过并往 stderr 打警告;中间的坏行抛 ValueError。"""
    if not path.is_file():
        return []
    parts = path.read_bytes().split(b"\n")
    out = []
    for i, raw in enumerate(parts):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            if i == len(parts) - 1:        # 文件不以换行结尾,这是最后那半行
                print(f"⚠ {path} 第 {i + 1} 行没写完整,读的时候跳过了(内容开头:{raw[:80]!r})", file=sys.stderr)
                continue
            raise ValueError(f"{path} 第 {i + 1} 行不是合法 JSON:{e}") from e
    return out


def _check_probs(probs: dict) -> dict[str, int]:
    unknown = sorted(set(probs) - set(CAUSES))
    if unknown:
        raise ValueError(f"不认识的原因 {unknown},只有 {CAUSES}")
    bad = {k: v for k, v in probs.items() if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100}
    if bad:
        raise ValueError(f"概率要是 0~100 的整数,这几项不是:{bad}")
    full = {c: probs.get(c, 0) for c in CAUSES}
    if sum(full.values()) != 100:
        raise ValueError(f"各原因加起来要正好 100,现在是 {sum(full.values())}")
    return full


def _need_text(name: str, v) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} 不能为空")
    return v


def confirm(qid: str, probs: dict[str, int], who: str, note: str, evidence: list[str],
            root: Path | None = None) -> dict:
    """一道题的原因概率记一条,追加到 <root>/reviews.jsonl,返回写进去的那条。
    人确认(board.py confirm 不写 --who,who = "user")和 AI 写的记录(演示、提议,who = "claude" / "codex")
    都走这里;成绩页只把 who 是 user 的当人确认、算进按原因汇总,别的只当提议显示(board_scores.is_human)。
    probs 可以只写几项,没写的补 0;要求整数、加起来 100。不合格抛 ValueError,一行都不写。"""
    _need_text("题号", qid)
    _need_text("确认人", who)
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        raise ValueError(f"证据要是字符串列表,得到 {evidence!r}")
    rec = {**_stamp(), "qid": qid, "probs": _check_probs(probs), "who": who, "note": note or "",
           "evidence": list(evidence)}
    return _append(_root(root) / REVIEWS, rec)


def load_reviews(root: Path | None = None) -> list[dict]:
    """reviews.jsonl 按写入顺序读回;没有文件给空列表。"""
    return _read_lines(_root(root) / REVIEWS)


def aggregate(reviews: list[dict]) -> dict[str, float]:
    """按原因汇总份量:每道题按 概率 ÷ 100 计,七项都在。同一道题确认了几次只算最后一次
    (列表里排在后面的;load_reviews 给的就是写入顺序)。例 data_gap 1.2 = 约 1.2 道题的份量。"""
    last = {r["qid"]: r["probs"] for r in reviews}
    return {c: sum(p.get(c, 0) for p in last.values()) / 100 for c in CAUSES}


def add_improvement(cause: str, file: str, desc: str, root: Path | None = None) -> dict:
    """「要改的地方」加一项(状态 todo),追加到 <root>/improvements.jsonl,返回这一项。"""
    if cause not in CAUSES:
        raise ValueError(f"不认识的原因 {cause!r},只有 {CAUSES}")
    rec = {**_stamp(), "kind": "item", "cause": cause, "file": _need_text("文件", file),
           "desc": _need_text("说明", desc), "status": "todo"}
    return _append(_root(root) / IMPROVEMENTS, rec)


def mark_improved(item_id: str, commit: str, root: Path | None = None) -> dict:
    """把一项标成已改:另追加一行状态行 {"kind": "status", "item": 那一项的 id, "status": "done", "commit"},
    原来那行不动。编号不存在抛 KeyError,没写提交号抛 ValueError,都不写。"""
    _need_text("提交号", commit)
    if item_id not in {it["id"] for it in load_improvements(root)}:
        raise KeyError(f"improvements.jsonl 里没有编号 {item_id!r}")
    rec = {**_stamp(), "kind": "status", "item": item_id, "status": "done", "commit": commit}
    return _append(_root(root) / IMPROVEMENTS, rec)


def load_improvements(root: Path | None = None) -> list[dict]:
    """当前状态:每项一条,按加入顺序;状态行合进去(多条以最后一条为准),
    带 status、commit(没改过是 None)、done_at。状态行指向不存在的项说明文件被手改坏了,抛 ValueError。"""
    items: dict[str, dict] = {}
    for rec in _read_lines(_root(root) / IMPROVEMENTS):
        if rec.get("kind") == "item":
            items[rec["id"]] = {**rec, "commit": None, "done_at": None}
        elif rec.get("kind") == "status":
            if rec.get("item") not in items:
                raise ValueError(f"improvements.jsonl 的状态行 {rec.get('id')} 指向不存在的项 {rec.get('item')}")
            items[rec["item"]] = {**items[rec["item"]], "status": rec["status"], "commit": rec.get("commit"),
                                  "done_at": rec.get("at")}
    return list(items.values())
