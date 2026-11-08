#!/usr/bin/env python3
"""check_judgment —— 查一次「按当前事实做的判断」五样东西写全了没有。

它是什么:references/prediction-thinking.md 第 2 节要求每次判断写齐五样 ——
事实、关键日期、几种情况 + 概率、什么情况算判断错、记下来到期核对。这个脚本逐项查。
没有它会怎样:长对话里很容易拿一组波动模拟的数当回答(2026-11-03 真发生过),
或者几种情况的概率加起来不是 100、没写什么时候核对,事后没法打分。
为什么只查这几样:这几样机器能判断(有没有、加起来对不对、日期在不在判断日之后);
事实写得对不对、情况分得合不合理,机器判断不了,靠人看。

用法:
    python3 check_judgment.py <文件> [--date YYYY-MM-DD]          markdown / 纯文本
    printf '%s' "<草稿>" | python3 check_judgment.py --stdin --date 2026-11-03
    python3 check_judgment.py --journal <journal.jsonl> --id 81     journal 里的一条
    python3 check_judgment.py --self-test                           反向自测
退出码:0 全过;1 有不过的;2 用法错或读不到输入。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

MIN_FACTS = 3                     # 至少几条带数字的事实
MIN_SCENARIOS = 2
PROB_TOL = 1                      # 概率加起来允许差几个百分点(四舍五入)
FALSIFY_MIN_CHARS = 10

_SEG = re.compile(r"[;;。\n]")
_NUM_UNIT = re.compile(r"\d[\d.,]*\s*(%|亿|万|美元|元|倍|个点)")
_SCEN = re.compile(r"(?:^|[\s,,;;((:：])([A-D]|[甲乙丙丁])\s*[::]?\s*(\d{1,3})\s*%")
_YMD = re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")
_YM = re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})(?![\d-])")
_MD = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])")
_CN_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_FALSIFY = re.compile(r"(算错|判断错|算判断错|改判)")
_REVIEW = re.compile(r"复核")
_LABEL = re.compile(r"(尚未验证|没验证|已验证)")


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def future_dates(text: str, today: date) -> set[date]:
    """文本里所有晚于 today 的日期。只写月-日的按 today 那年算,早于 today 的不往下一年挪
    (挪了会把「11-02 收盘」这种过去的日子算成明年)。只写年-月的按那个月 1 号算。"""
    out = set()
    for y, m, d in _YMD.findall(text):
        out.add(_safe_date(int(y), int(m), int(d)))
    stripped = _YMD.sub(" ", text)
    for y, m in _YM.findall(stripped):
        out.add(_safe_date(int(y), int(m), 1))
    stripped = _YM.sub(" ", stripped)
    for m, d in _MD.findall(stripped) + _CN_MD.findall(stripped):
        out.add(_safe_date(today.year, int(m), int(d)))
    return {x for x in out if x is not None and x > today}


def scenarios(text: str) -> dict[str, int]:
    """「A 30%」「B:45%」「甲 20%」这类写法 → {标签: 概率};同一标签只取第一次出现。"""
    got: dict[str, int] = {}
    for label, p in _SCEN.findall(text):
        got.setdefault(label, int(p))
    return got


def fact_count(text: str) -> int:
    return sum(1 for seg in _SEG.split(text) if _NUM_UNIT.search(seg))


def check(text: str, today: date, falsify: str | None = None, review: date | None = None) -> list[tuple[str, bool, str]]:
    """返回 [(编号 名称, 过没过, 说明)]。falsify / review 是 journal 里单独的字段,
    给了就用它们,没给就从正文里找。"""
    res = []
    n_fact = fact_count(text)
    res.append(("J1 当前事实", "事实" in text and n_fact >= MIN_FACTS,
                f"要写出「事实」并至少 {MIN_FACTS} 条带数字的事实,找到 {n_fact} 条"
                + ("" if "事实" in text else ";全文没有「事实」两个字(只给波动模拟不算判断)")))
    fut = future_dates(text, today) - ({review} if review else set())
    need = 1 if review else 2              # 正文里还要容下复核日那一个
    res.append(("J2 关键日期", len(fut) >= need,
                f"要有判断日 {today} 之后的日期(接下来哪天出什么事实),找到 {sorted(str(d) for d in fut)}"))
    sc = scenarios(text)
    total = sum(sc.values())
    res.append(("J3 几种情况+概率", len(sc) >= MIN_SCENARIOS and abs(total - 100) <= PROB_TOL,
                f"要至少 {MIN_SCENARIOS} 种情况(写成「A 30%」),概率加起来 100;找到 {sc},合计 {total}"))
    if falsify is not None:
        ok4, why4 = len(falsify.strip()) >= FALSIFY_MIN_CHARS, f"journal 的 falsify 有 {len(falsify.strip())} 个字"
    else:
        ok4, why4 = bool(_FALSIFY.search(text)), "正文要写「什么情况算判断错 / 改判」"
    res.append(("J4 什么情况算错", ok4, why4))
    res.append(("J5 标明验证状态", bool(_LABEL.search(text)), "要写「尚未验证」或「已验证」"))
    if review is not None:
        ok6, why6 = review > today, f"复核日 {review} 要晚于判断日 {today}"
    else:
        near = any(future_dates(m.group(0), today) for m in re.finditer(r"复核.{0,30}", text))
        ok6, why6 = bool(_REVIEW.search(text)) and near, "正文要写「复核」并紧跟一个判断日之后的日期"
    res.append(("J6 到期核对", ok6, why6))
    return res


def from_journal(path: Path, entry_id: int) -> tuple[str, date, str, date | None]:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("id") == entry_id:
            logged = date.fromisoformat(str(r.get("logged_at", ""))[:10])
            cd = r.get("check_date")
            return (r.get("thesis") or "", logged, r.get("falsify") or "",
                    date.fromisoformat(cd) if cd else None)
    raise KeyError(f"{path} 里没有 #{entry_id}")


def report(rows) -> int:
    for name, ok, why in rows:
        print(f"  {'✅' if ok else '❌'} {name}" + ("" if ok else f":{why}"))
    bad = sum(1 for _, ok, _ in rows if not ok)
    print("✅ 五样写全了" if not bad else f"❌ {bad} 项没写全 —— 补上再给 user")
    return 1 if bad else 0


# ── 反向自测 ────────────────────────────────────────────────────────────────
GOOD = """判断(尚未验证):
事实:云厂三季度资本开支同比 +107.6%;海外同行下季度营收指引比上季高 25%;券商仍给四季度收入环比 +27%;按 2027 年平均预测只有约 8 倍市盈率。
关键日期:英伟达财报 11-18;云厂 2027 年开支计划 2027-01 底。
A 30%:底在 12 月,现价附近;B 45%:底在 1 月,约现价 0.85 倍;C 25%:底在 3 月,约现价 0.7 倍。
什么情况算判断错:英伟达指引超预期但股价两周内仍创新低,就改判往 C。
复核:2027-03-31。"""
VOL_ONLY = "半年内最低收盘:100 种走法里 50 种低于 340 元,25 种低于 261 元,10 种低于 197 元。"


def self_test() -> int:
    d = date(2026, 11, 3)
    fails = 0

    def expect(name, text, want_bad, **kw):
        nonlocal fails
        rows = check(text, d, **kw)
        bad = {n.split()[0] for n, ok, _ in rows if not ok}
        good = bad == set(want_bad) if want_bad is not None else not bad
        print(f"  {'✓' if good else '✗'} {name}:没过的 {sorted(bad) or '无'}")
        fails += not good

    expect("写全的判断 → 全过", GOOD, [])
    expect("只给波动模拟 → 没有事实、情况、算错、标注、复核", VOL_ONLY, ["J1", "J2", "J3", "J4", "J5", "J6"])
    expect("概率加起来 90 → J3", GOOD.replace("B 45%", "B 35%"), ["J3"])
    expect("日期都在判断日以前 → J2、J6",
           GOOD.replace("11-18", "10-18").replace("2027-01", "2026-01").replace("2027-03-31", "2026-03-31"),
           ["J2", "J6"])
    expect("没写什么情况算错 → J4", GOOD.replace("什么情况算判断错", "另外").replace("改判", "看看"), ["J4"])
    expect("没标验证状态 → J5", GOOD.replace("(尚未验证)", ""), ["J5"])
    expect("「11-02 收盘」这种过去的月-日不算关键日期",
           GOOD.replace("英伟达财报 11-18;云厂 2027 年开支计划 2027-01 底。", "11-02 收盘以后。"), ["J2"])
    expect("journal 字段:falsify 空 → J4",
           GOOD.replace("什么情况算判断错", "另外").replace("改判", "看看"), ["J4"], falsify="", review=date(2027, 3, 31))
    expect("journal 字段:复核日早于判断日 → J6", GOOD, ["J6"], falsify="指引超预期但两周内仍创新低", review=date(2026, 10, 1))
    print("全部通过" if not fails else f"{fails} 项没过")
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="查一次按当前事实做的判断,五样东西写全了没有")
    ap.add_argument("file", nargs="?", help="markdown / 纯文本草稿")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读草稿")
    ap.add_argument("--date", help="判断日 YYYY-MM-DD(不写:journal 用 logged_at,其余用今天)")
    ap.add_argument("--journal", help="journal.jsonl 路径")
    ap.add_argument("--id", type=int, help="journal 里的编号")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    try:
        if a.journal:
            if a.id is None:
                ap.error("--journal 要配 --id")
            text, logged, falsify, review = from_journal(Path(a.journal), a.id)
            today = date.fromisoformat(a.date) if a.date else logged
            print(f"journal #{a.id}(判断日 {today})")
            return report(check(text, today, falsify=falsify, review=review))
        text = sys.stdin.read() if a.stdin else Path(a.file).read_text(encoding="utf-8") if a.file else None
    except (OSError, KeyError, ValueError) as e:
        print(f"读不到输入:{e}", file=sys.stderr)
        return 2
    if text is None:
        ap.error("给一个文件、--stdin 或 --journal")
    today = date.fromisoformat(a.date) if a.date else date.today()
    return report(check(text, today))


if __name__ == "__main__":
    sys.exit(main())
