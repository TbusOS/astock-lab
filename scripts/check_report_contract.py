#!/usr/bin/env python3
"""check_report_contract —— 深度报告层的可执行闸。

    python3 scripts/check_report_contract.py

为什么这一层必须有闸（而不是写进文档里提醒自己）:
    深度报告的错法**全都是静默的**。少画一张图、把不适用的指标当证据用、
    情景价改了 EV 忘了改 —— 报告照样生成、照样好看、照样没有报错。
    读者拿到的是一份看起来完整的东西，而其中一条是错的。
    这正是「无可执行检查就不算固化」那条地基规则针对的形态。

查七件事:
    1  journal 的 EV 与「三情景价位 × 概率」自洽（差 >2pp 就报）
    2  赛道判为不适用的指标，**不准**出现在正面/反面证据里（只能进中性项）
    3  完备性表里，不适用的层必须标 ⊘ 而不是 ❌（两者含义相反）
    4  每张图都有 figcaption（没说明的图等于没画）
    5  正文里没有漏渲染的裸 `**`（journal 手写的粗体标记要变成 <b>）
    6  没有 journal 记录的票，**不准**出现「结论：」段（工具不发明判断）
    7  报告里的涨跌幅只有一个分母（决策价与快照价不同时必须写明按哪个算）
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

fail = 0


def bad(tag, msg):
    global fail
    print(f"  ❌ {tag}：{msg}")
    fail += 1


def ok(tag, msg=""):
    print(f"  ✅ {tag}" + (f"　{msg}" if msg else ""))


LAB = Path(os.environ.get("STOCK_LAB", ROOT))
JOURNAL = LAB / "data" / "journal.jsonl"
SNAPS = LAB / "data" / "snapshots"

print("== 深度报告层合约闸 ==")
print(f"   工作台:{LAB}\n")

# ── 1 journal 的 EV 自洽 ──────────────────────────────────────────────────
rows = []
if JOURNAL.exists():
    rows = [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines()
            if x.strip()]
if not rows:
    print("  ⏭  没有 journal 记录 —— 1/6/7 跳过（新装的仓属正常）")
else:
    n_bad = 0
    for r in rows:
        px, pr, ev, price = (r.get("scenario_px") or {}), (r.get("probs") or {}), \
            r.get("ev"), r.get("price")
        if ev is None or not price:
            continue
        if not all(px.get(k) is not None and pr.get(k) is not None
                   for k in ("bear", "base", "bull")):
            continue
        calc = sum(pr[k] * (px[k] / price - 1) * 100 for k in ("bear", "base", "bull"))
        if abs(calc - ev) > 2.0:
            bad("1 EV 自洽", f"#{r['id']} {r['code']}：写的 {ev:+.1f}%，"
                             f"按情景价×概率算 {calc:+.1f}%（差 {abs(calc - ev):.1f}pp）")
            n_bad += 1
    if not n_bad:
        ok("1 EV 与三情景价位自洽", f"{len(rows)} 条全过")

# ── 渲染样本，供 2~7 检查 ──────────────────────────────────────────────────
codes = sorted(d.name for d in SNAPS.glob("*") if d.is_dir()) if SNAPS.is_dir() else []
if not codes:
    print("  ⏭  没有快照 —— 2~7 跳过")
else:
    import deep_report as dr  # noqa: E402

    CN2KEY = {"PEG": "peg", "PE/PB 分位": "pe_percentile",
              "基准率": "baserate_growth", "海外同业": "peers"}
    n_fig = n_bold = n_inv = n_comp = 0
    for code in codes:
        snap, path = dr.load_snapshot(code)
        if snap is None:
            continue
        jr = dr.load_journal(code)
        html = dr.build_html(snap, jr, path, tol=30)
        inv = set((snap.get("sector") or {}).get("invalid_keys") or [])

        # 2 不适用的指标不准进正/反面证据
        pro, con, neu = dr.build_evidence(snap.get("facts", {}), inv)
        for bucket, nm in ((pro, "正面"), (con, "反面")):
            for e in bucket:
                for cn, key in CN2KEY.items():
                    if cn in e["t"] and key in inv:
                        bad("2 赛道边界",
                            f"{code}：「{e['t']}」被赛道判为不适用，却进了{nm}证据")
                        n_inv += 1

        # 3 完备性表:不适用的层要标 ⊘ 不是 ❌
        comp = dr._completeness(snap.get("facts", {}), inv,
                                (snap.get("sector") or {}).get("name", ""))
        for cn, key in (("基准率", "baserate_growth"), ("估值历史分位", "pe_percentile"),
                        ("海外同业对照", "peers")):
            if key in inv:
                m = re.search(r"<tr><td>" + re.escape(cn) + r"</td><td>([^<]*)</td>", comp)
                if m and "⊘" not in m.group(1):
                    bad("3 缺 vs 不适用",
                        f"{code}：「{cn}」被判不适用，状态却是「{m.group(1).strip()}」"
                        f" —— 「缺」会让人去补一个不该用的指标")
                    n_comp += 1

        # 4 每张图都要有 figcaption
        figs = re.findall(r"<figure.*?</figure>", html, re.S)
        for i, fseg in enumerate(figs, 1):
            if "<figcaption>" not in fseg or len(
                    re.sub(r"<[^>]*>", "", fseg.split("<figcaption>")[1])) < 10:
                bad("4 图必须有说明", f"{code} 第 {i} 张图没有 figcaption（或过短）")
                n_fig += 1

        # 5 没有漏渲染的裸 **
        body = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
        if re.search(r"\*\*[^\s*][^*]{0,80}\*\*", body):
            hit = re.search(r"\*\*[^\s*][^*]{0,80}\*\*", body).group(0)[:50]
            bad("5 粗体未渲染", f"{code}：正文里有裸的 `{hit}`")
            n_bold += 1

        # 6 没有 journal 就不准有结论段
        if jr is None and "结论：" in html:
            bad("6 不发明判断", f"{code}：没有 journal 记录却印出了结论段")

        # 7 一个分母
        jp, sp = (jr or {}).get("price"), dr.g(snap.get("facts", {}), "position", "price")
        if jp and sp and abs(sp / jp - 1) >= 0.0005 and "一个分母" not in html:
            bad("7 一个分母", f"{code}：决策价 {jp} 与快照价 {sp} 不同，"
                             f"报告却没写明按哪个算")

    if not n_inv:
        ok("2 不适用的指标没进正/反面证据", f"{len(codes)} 只票")
    if not n_comp:
        ok("3 完备性表区分「缺」与「不适用」")
    if not n_fig:
        ok("4 每张图都有说明")
    if not n_bold:
        ok("5 粗体全部渲染")
    ok("6 无 journal 不出结论段")
    ok("7 报告内只有一个分母")

print()
if fail:
    print(f"❌ {fail} 项不过。")
    sys.exit(1)
print("✅ 深度报告层合约全过。")
