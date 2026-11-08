"""scoring —— 预测看板的评分:这套预测准不准,有没有越改越准。

纯计算,不联网、不读文件。输入是「评分记录」:
    评分记录 = 题目 + 结算结果,即 {**question, "resolution": resolution}
    question   见 questions.py(id / run / author / code / type / opened / params / forecast)
    resolution 见 questions.resolve()(outcome / value / scorable_after ...)

规则:
- 二分类概率存 0~100 整数,这里用 p = pct / 100;分类题 {类别: 整数} 同样除以 100。
- 区间题:forecast 里存比值 lo/hi,params 里的 lo/hi 是分位名("10"/"90")。
  区间得分用对数比值算(x = ln 结算比值),alpha = 1 − (hi 分位 − lo 分位)/100:
  25–75 → 0.5,10–90 → 0.2。区间题只进区间得分和覆盖比例,不进 Brier。
- author "rule"(买点规则,没有概率)不进任何平均,单列 {"n","wins","win_rate"}。
- outcome 为 None(没出现买点)的不计分,记在 "unscored";
  scorable_after 晚于 today 的、或还没有结算结果的记在 "pending",都不进分数。
"""
import json
import math
from datetime import date

MIN_NEFF_FOR_CHART = 20          # 独立样本数不到这个数,不画「谁更准」的图
LAYERS = ("baseline", "system", "claude", "codex")
RULE_AUTHOR = "rule"
UNKNOWN_VERSION = "unknown"


# ── 单题打分 ────────────────────────────────────────────────────────────────
def brier(p: float, o: int) -> float:
    """二分类 Brier:p 是说「会发生」的概率(0~1),o 是 0 / 1。越小越好。"""
    return (p - o) ** 2


def brier_multi(probs: dict[str, float], outcome: str) -> float:
    """多类 Brier:各类概率与「实际那一类 = 1,其余 = 0」的差平方和。"""
    keys = set(probs) | {outcome}
    return sum((probs.get(k, 0.0) - (1.0 if k == outcome else 0.0)) ** 2 for k in keys)


def interval_score(lo: float, hi: float, x: float, alpha: float) -> float:
    """区间得分(Gneiting & Raftery):宽度 + 落在外面的罚。越小越好。lo / hi / x 用对数比值。"""
    return ((hi - lo) + 2 / alpha * (lo - x) * (x < lo) + 2 / alpha * (x - hi) * (x > hi))


def coverage(hits: list[bool]) -> float | None:
    """区间包住实际值的比例;没有样本返回 None。"""
    return sum(1 for h in hits if h) / len(hits) if hits else None


def n_eff(questions: list[dict]) -> int:
    """独立样本数:开题那周的 ISO 年周 + 题型 + 期限(params)相同的算一个。
    同一周里各只股票对同一道题的结果常常一起涨一起跌,不能当各自独立的证据。"""
    keys = set()
    for q in questions:
        iso = date.fromisoformat(str(q["opened"])[:10]).isocalendar()
        keys.add((iso[0], iso[1], q["type"], json.dumps(q.get("params") or {}, sort_keys=True)))
    return len(keys)


# ── 汇总 ────────────────────────────────────────────────────────────────────
def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _alpha(params: dict) -> float:
    return 1 - (int(params["hi"]) - int(params["lo"])) / 100


def _direction_hit(forecast: dict, outcome) -> float:
    pct = forecast["pct"]
    if pct == 50:
        return 0.5
    return 1.0 if (pct > 50) == (outcome == 1) else 0.0


def _score_one(r: dict):
    """一条记录 → (分数字典, None);算不了 → (None, 原因)。分数字典只含这条有的项。"""
    f, res, t, o = r["forecast"], r["resolution"], r["type"], r["resolution"]["outcome"]
    if not isinstance(f, dict):
        return None, "forecast 不是 dict(没有概率)"
    if t == "interval":
        x = res.get("value")
        if x is None:
            return None, "区间题没有结算比值"
        if not (isinstance(x, (int, float)) and x > 0 and f.get("lo", 0) > 0 and f.get("hi", 0) > 0):
            return None, "比值 ≤ 0,取不了对数"
        hit = f["lo"] <= x <= f["hi"]
        return {"iscore": interval_score(math.log(f["lo"]), math.log(f["hi"]), math.log(x), _alpha(r["params"])),
                "cov": hit, "level": str(int(r["params"]["hi"]) - int(r["params"]["lo"]))}, None
    if "pct" in f:
        if o not in (0, 1):
            return None, "二分类题的结果不是 0 / 1"
        b = brier(f["pct"] / 100, int(o))
        out = {"brier": b, "type_brier": b}
        if t == "up":
            out["dir"] = _direction_hit(f, o)
        return out, None
    m = brier_multi({k: v / 100 for k, v in f.items()}, o)
    return {"multi": m, "type_brier": m}, None


def _agg(items: list) -> dict:
    """items = [(记录, 分数字典)] → 各项指标。n / n_eff 只算这些已打分的记录。"""
    def pick(k):
        return [sc[k] for _, sc in items if k in sc]
    cov_lv: dict[str, list[bool]] = {}
    by_type: dict[str, list[float]] = {}
    for r, sc in items:
        if "cov" in sc:
            cov_lv.setdefault(sc["level"], []).append(sc["cov"])
        if "type_brier" in sc:
            by_type.setdefault(r["type"], []).append(sc["type_brier"])
    return {"n": len(items), "n_eff": n_eff([r for r, _ in items]), "direction_hit": _mean(pick("dir")),
            "brier": _mean(pick("brier")), "brier_multi": _mean(pick("multi")),
            "brier_by_type": {t: _mean(v) for t, v in sorted(by_type.items())},
            "interval_score": _mean(pick("iscore")), "coverage": coverage(pick("cov")),
            "coverage_by_level": {k: coverage(v) for k, v in sorted(cov_lv.items())}}


_DIFF_KEYS = ("direction_hit", "brier", "brier_multi", "interval_score", "coverage")


def question_key(r: dict) -> tuple:
    """同一道题(不管哪一层答的)的键:同一次存档(run)里代码、题型、参数、开题日都一样。
    结算时给错题配原因初值(board_settle)、成绩页两个 AI 比(board_scores)、这里对统计底线的差值,
    都用这一个函数。带上 run:同一天出两版预测,两版的题开题日、参数都一样,不带 run 会混成一道,
    后一版的分数和前一版的搅在一起。"""
    return (r.get("run"), r.get("code"), r.get("type"), json.dumps(r.get("params") or {}, sort_keys=True),
            str(r.get("opened"))[:10])


def _by_layer(items: list) -> tuple:
    """→ (各层指标, 各层对 baseline 的差值)。差值只在该层和 baseline 都答了的题上算,
    并带 n_matched(共同题数);没有共同题则那一层的差值只有 {"n_matched": 0}。"""
    by = {a: [it for it in items if it[0]["author"] == a] for a in LAYERS}
    by = {a: v for a, v in by.items() if v}
    layers = {a: _agg(v) for a, v in by.items()}
    diff = {}
    if "baseline" in by:
        bkeys = {question_key(r) for r, _ in by["baseline"]}
        for a, v in by.items():
            if a == "baseline":
                continue
            shared = {question_key(r) for r, _ in v} & bkeys
            ma = _agg([it for it in v if question_key(it[0]) in shared])
            mb = _agg([it for it in by["baseline"] if question_key(it[0]) in shared])
            diff[a] = {"n_matched": len(shared),
                       **{k: ma[k] - mb[k] for k in _DIFF_KEYS if ma[k] is not None and mb[k] is not None}}
    return layers, diff


def layer_report(scored: list[dict], today: str, versions: dict[str, str] | None = None) -> dict:
    """按层汇总。scored = 评分记录列表(见模块说明);today = 'YYYY-MM-DD',
    只算 scorable_after ≤ today 的;没有 scorable_after 的当没到期(pending)。
    versions = {run: 代码提交号}(来自各 run 的 manifest),给了就再按提交号分组出同样的指标
    (manifest 里没有的 run 归 "unknown")。
    返回 {"layers", "diff_vs_baseline", "rule", "totals", "unscored", "pending", "skipped",
    "by_version"}。totals 里 n_total = scored + pending + unscored + rule + skipped,可核对。
    diff_vs_baseline 只比两边都答了的题(带 n_matched)。"""
    rules, items, pending, unscored = [], [], 0, 0
    skipped: dict[str, int] = {}
    for r in scored:
        res = r.get("resolution")
        sa = str((res or {}).get("scorable_after") or "")[:10]
        if not res or not sa or sa > today[:10]:
            pending += 1
        elif res.get("outcome") is None:
            unscored += 1
        elif r["author"] == RULE_AUTHOR:
            rules.append(r)
        else:
            sc, why = _score_one(r)
            if sc is None:
                skipped[why] = skipped.get(why, 0) + 1
            else:
                items.append((r, sc))
    layers, diff = _by_layer(items)
    wins = sum(1 for r in rules if r["resolution"]["outcome"] == 1)
    rule = {"n": len(rules), "wins": wins, "win_rate": wins / len(rules) if rules else None}
    by_version = {}
    if versions:
        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(versions.get(it[0]["run"], UNKNOWN_VERSION), []).append(it)
        for v, its in groups.items():
            lay, df = _by_layer(its)
            by_version[v] = {"layers": lay, "diff_vs_baseline": df}
    n_skipped = sum(skipped.values())
    totals = {"n_total": len(scored), "scored": len(items), "pending": pending, "unscored": unscored,
              "rule": len(rules), "skipped": n_skipped, "skipped_reasons": skipped}
    return {"layers": layers, "diff_vs_baseline": diff, "rule": rule, "totals": totals,
            "unscored": unscored, "pending": pending, "skipped": n_skipped, "by_version": by_version}
