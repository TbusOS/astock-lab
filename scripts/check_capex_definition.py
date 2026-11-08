#!/usr/bin/env python3
"""check_capex_definition —— 云厂资本开支「现金购建 + 融资租赁」的取法检查。不联网,读 fixture。

用法:
    python3 scripts/check_capex_definition.py                    # 查本仓的 capex.py
    python3 scripts/check_capex_definition.py --capex <旧版路径>   # 反向验证:旧版必须不过

为什么有这道检查(2026-11-02 拿财联社那张图比对):
  亚马逊、谷歌、Meta 2025 年资本开支和我们从 SEC 取的差 3% 以内,微软对不上 ——
  我们只取了现金购建(PaymentsToAcquirePropertyPlantAndEquipment),
  微软每年还有 200 多亿美元融资租赁新增资产
  (RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability)没算。
  Meta 的指引也含融资租赁本金(FinanceLeasePrincipalPayments)。

fixture(scripts/fixtures/board/xbrl_msft_*.json)是 2026-11-02 从 SEC 取的微软三个标签,
只留 end >= 2024-06-30 的记录,全是公开数据。
"""
import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "board"
DEFAULT_CAPEX = HERE.parent / "skills" / "data-sources" / "scripts" / "fetch" / "capex.py"

T_CASH = "PaymentsToAcquirePropertyPlantAndEquipment"
T_ROU = "RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability"
T_FLP = "FinanceLeasePrincipalPayments"

fails = 0


def ok(name, cond, got=""):
    global fails
    if not cond:
        fails += 1
    print(("✅ " if cond else "❌ ") + name + (f"  ← {got}" if got != "" else ""))


def load_capex(path):
    spec = importlib.util.spec_from_file_location("capex_under_test", str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_getter(only_cumulative=False, drop_lease_end=None, fail_cik=None):
    """假的 SEC 取数函数:按 URL 里的标签返回 fixture;没有 fixture 的标签抛错(像 404)。
    only_cumulative=True 时去掉所有「不从财年初起」的单季记录,只留年初至今累计,
    用来验证差分倒推出的单季和直报一致。
    drop_lease_end:把融资租赁里 end 等于这个日期的记录删掉(造一个租赁缺口)。
    fail_cik:URL 里带这个 CIK 的请求一律抛错(造一家取数失败的公司)。
    公司代码表(company_tickers.json)也由这里答,给 main() 用。
    注意:GOOGL / META 的用例没有它们自己的 fixture,借用微软的现金购建 fixture 来测
    「没有租赁标签」「租赁用哪个标签」这类和数值无关的逻辑,不代表它们的真实数字。"""
    def get(url):
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "MSFT", "cik_str": 789019, "title": "MICROSOFT CORP"},
                    "1": {"ticker": "GOOGL", "cik_str": 1652044, "title": "Alphabet Inc."}}
        if fail_cik and f"CIK{fail_cik}" in url:
            raise RuntimeError("模拟 SEC 取数失败")
        tag = url.rsplit("/", 1)[-1].replace(".json", "")
        f = FIX / f"xbrl_msft_{tag}.json"
        if not f.exists():
            raise RuntimeError(f"404 {tag}")
        d = json.loads(f.read_text(encoding="utf-8"))
        if only_cumulative:
            d["units"]["USD"] = [x for x in d["units"]["USD"]
                                 if x["start"][5:] == "07-01"]
        if drop_lease_end and tag in (T_ROU, T_FLP):
            d["units"]["USD"] = [x for x in d["units"]["USD"]
                                 if x["end"] != drop_lease_end]
        return d
    return get


def b(x):
    return round(x / 1e9, 2)


def main():
    global fails
    ap = argparse.ArgumentParser()
    ap.add_argument("--capex", default=str(DEFAULT_CAPEX))
    path = ap.parse_args().capex
    print(f"检查对象:{path}\n")
    try:
        m = load_capex(path)
    except Exception as e:
        print(f"❌ 加载失败 {type(e).__name__}: {e}")
        return 1

    ok("常量 LEASE_TAGS 存在且内容正确",
       getattr(m, "LEASE_TAGS", None) == {"MSFT": T_ROU, "META": T_FLP},
       getattr(m, "LEASE_TAGS", None))

    try:
        d = m.fetch_capex("0000789019", ticker="MSFT", get=make_getter())
        rows = {r["q"]: r for r in m.company_rows(d, 8)}
        r = rows.get("2025-09-30")
        ok("微软 2025-09-30 那季有数据", r is not None, sorted(rows))
        if r:
            ok("微软 cash == 19.39", abs(b(r["cash"]) - 19.39) <= 0.01, b(r["cash"]))
            ok("微软 lease == 9.15", abs(b(r["lease"]) - 9.15) <= 0.01, b(r["lease"]))
            ok("微软 total == 28.54", abs(b(r["total"]) - 28.54) <= 0.01, b(r["total"]))
            ok("total == cash + lease", abs(r["total"] - r["cash"] - r["lease"]) < 1)
        ok("微软用的是融资租赁新增资产标签", d.get("lease_tag") == T_ROU, d.get("lease_tag"))
    except Exception as e:
        ok("微软取数不报错", False, f"{type(e).__name__}: {e}")
        d = None

    # 只报累计时,差分后的单季要和直报一致(2024-12-31 / 2025-03-31 两季有直报)
    try:
        dc = m.fetch_capex("0000789019", ticker="MSFT", get=make_getter(True))
        for e, cash_want, lease_want in (("2024-12-31", 15.804, 6.435),
                                         ("2025-03-31", 16.745, 3.241)):
            ok(f"只给累计 → {e} cash 差分 == 直报 {cash_want}",
               e in dc["q"] and abs(b(dc["q"][e]) - cash_want) <= 0.01,
               b(dc["q"].get(e, 0)))
            ok(f"只给累计 → {e} lease 差分 == 直报 {lease_want}",
               e in dc["lease"] and abs(b(dc["lease"][e]) - lease_want) <= 0.01,
               b(dc["lease"].get(e, 0)))
    except Exception as e:
        ok("累计差分不报错", False, f"{type(e).__name__}: {e}")

    # 没有融资租赁标签的公司:lease == 0,表头注明
    try:
        dg = m.fetch_capex("0001652044", ticker="GOOGL", get=make_getter())
        grows = m.company_rows(dg, 8)
        ok("无融资租赁标签的公司 lease 全是 0",
           bool(grows) and all(r["lease"] == 0 for r in grows),
           [r["lease"] for r in grows][:3])
        ok("无融资租赁标签的公司 total == cash",
           all(r["total"] == r["cash"] for r in grows))
        md = "\n".join(m.company_md_lines("GOOGL", "谷歌", "Alphabet", "0001652044", dg, 8))
        ok("表头注明「此公司无单列融资租赁」", "此公司无单列融资租赁" in md)
        dm = m.fetch_capex("0000789019", ticker="MSFT", get=make_getter())
        mdm = "\n".join(m.company_md_lines("MSFT", "微软", "Microsoft", "0000789019", dm, 8))
        ok("有融资租赁的公司表头不写「无单列」", "此公司无单列融资租赁" not in mdm)
        ok("markdown 三列都在", all(k in mdm for k in ("现金购建", "融资租赁", "合计")))
        dmeta = m.fetch_capex("0001326801", ticker="META", get=make_getter())
        ok("Meta 用融资租赁本金标签", dmeta.get("lease_tag") == T_FLP, dmeta.get("lease_tag"))
    except Exception as e:
        ok("无租赁标签公司的检查不报错", False, f"{type(e).__name__}: {e}")
        dg = None

    # --json 的结构
    try:
        allq = {"MSFT": d, "GOOGL": dg}
        out = m.build_json(allq, 8, {})
        ok("json 顶层只有 quarters / guidance / gaps / failed",
           sorted(out) == ["failed", "gaps", "guidance", "quarters"], sorted(out))
        ok("正常数据 gaps 和 failed 都是空", out["gaps"] == [] and out["failed"] == [],
           (out["gaps"], out["failed"]))
        q = {x["q"]: x for x in out["quarters"]}
        x = q.get("2025-09-30")
        ok("json 有 2025-09-30", x is not None, sorted(q))
        if x:
            ok("json 季度键齐全",
               sorted(x) == ["by", "q", "sum_cash", "sum_total", "yoy_cash", "yoy_total"],
               sorted(x))
            ok("json 每家三列", sorted(x["by"]["MSFT"]) == ["cash", "lease", "total"])
            ok("json 微软 28.54", abs(x["by"]["MSFT"]["total"] - 28.54) <= 0.01,
               x["by"]["MSFT"]["total"])
            ok("json 合计 = 两家之和",
               abs(x["sum_total"] - (28.54 + 19.39)) <= 0.02, x["sum_total"])
            ok("json 现金合计 = 两家现金之和", abs(x["sum_cash"] - 38.79) <= 0.02, x["sum_cash"])
            # 基期 2024-09-30:(14.923+4.332)+14.923 = 34.178;现在 47.935
            ok("json total 同比约 +40.3%", abs(x["yoy_total"] - 40.3) <= 0.2, x["yoy_total"])
            ok("json cash 同比约 +30.0%(现金 19.39 ÷ 14.92,单位十亿美元)", abs(x["yoy_cash"] - 30.0) <= 0.2, x["yoy_cash"])
            ok("json 数值保留两位小数",
               x["by"]["MSFT"]["cash"] == round(x["by"]["MSFT"]["cash"], 2))
        # fixture 只有 2024-09-30 起的数据:2024-12-31 / 2025-03-31 / 2025-06-30 找不到去年同季
        no_base = ["2024-12-31", "2025-03-31", "2025-06-30"]
        nt = sorted(v["q"] for v in out["quarters"] if v["yoy_total"] is None)
        nc = sorted(v["q"] for v in out["quarters"] if v["yoy_cash"] is None)
        ok("yoy_total 为 null 的恰好是没有基期的三季", nt == no_base, nt)
        ok("yoy_cash 为 null 的恰好是没有基期的三季", nc == no_base, nc)
        ok("json 可序列化", bool(json.dumps(out)))
        g = m.build_json(allq, 8, {"META": {"date": "2025-10-29", "hits": ["Capex will be X."]}})
        ok("json guidance 条目结构",
           g["guidance"] == [{"ticker": "META", "date": "2025-10-29", "text": "Capex will be X."}],
           g["guidance"])
    except Exception as e:
        ok("--json 结构检查不报错", False, f"{type(e).__name__}: {e}")

    # 租赁缺口:删掉微软 2025-09-30 的租赁记录。
    # 连带效果(真实行为,不是检查写错):FY26 的累计链是 07-01 起的 09-30 → 12-31 → 03-31 → 06-30,
    # 第一环(09-30)没了,剩下的首条超过 100 天,整条链按规则弃用;2026-06-30 那季没有直报单季,
    # 只能靠这条链差分,所以也变成缺口。两个缺口都该被报出来。
    try:
        GAP = "2025-09-30"
        dgap = m.fetch_capex("0000789019", ticker="MSFT",
                             get=make_getter(drop_lease_end=GAP))
        dg2 = m.fetch_capex("0001652044", ticker="GOOGL", get=make_getter())
        og = m.build_json({"MSFT": dgap, "GOOGL": dg2}, 8, {})
        ok("缺口出现在 gaps(2025-09-30 和被连带的 2026-06-30)",
           og["gaps"] == [{"ticker": "MSFT", "q": GAP, "what": "lease"},
                          {"ticker": "MSFT", "q": "2026-06-30", "what": "lease"}], og["gaps"])
        nt = sorted(v["q"] for v in og["quarters"] if v["yoy_total"] is None)
        nc = sorted(v["q"] for v in og["quarters"] if v["yoy_cash"] is None)
        ok("缺口那季、连带缺口那季、以及缺口的下一年同季,yoy_total 置 null",
           nt == ["2024-12-31", "2025-03-31", "2025-06-30", GAP, "2026-06-30", "2026-09-30"], nt)
        ok("yoy_cash 不受缺口影响", nc == ["2024-12-31", "2025-03-31", "2025-06-30"], nc)
        mdg = "\n".join(m.company_md_lines("MSFT", "微软", "Microsoft", "0000789019", dgap, 8))
        ok("markdown 缺口那行标 *", f"| {GAP} |" in mdg and "0.00 B*" in mdg)
        ok("markdown 缺口那行的合计同比是 —",
           [l for l in mdg.splitlines() if l.startswith(f"| {GAP} |")][0].split("|")[5].strip() == "—")
    except Exception as e:
        ok("租赁缺口检查不报错", False, f"{type(e).__name__}: {e}")

    # main():一家失败 → failed 列出、退出码 2;全失败 → 1;全成功 → 0
    def run_main(argv, getter):
        saved = m._get
        m._get = getter
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = m.main(argv)
        finally:
            m._get = saved
        return rc, out.getvalue(), err.getvalue()

    try:
        base = ["--tickers", "MSFT,GOOGL", "--quarters", "8", "--json"]
        rc, so, se = run_main(base, make_getter(fail_cik="0001652044"))
        j = json.loads(so)
        ok("一家取失败:退出码 2", rc == 2, rc)
        ok("failed 列出失败的公司和原因",
           len(j["failed"]) == 1 and j["failed"][0]["ticker"] == "GOOGL"
           and j["failed"][0]["why"], j["failed"])
        ok("失败行走 stderr,stdout 只有 JSON", "GOOGL" in se and so.lstrip().startswith("{"))
        ok("sum 只含取到数的公司", all(list(v["by"]) == ["MSFT"] for v in j["quarters"]))
        rc, so, se = run_main(["--tickers", "GOOGL", "--json"], make_getter(fail_cik="0001652044"))
        ok("一家都没取到:退出码 1", rc == 1, rc)
        ok("一家都没取到:JSON 里 failed 有它", [f["ticker"] for f in json.loads(so)["failed"]] == ["GOOGL"])
        rc, so, se = run_main(base, make_getter())
        ok("全部正常:退出码 0", rc == 0 and json.loads(so)["failed"] == [], rc)
        rc, so, se = run_main(base, make_getter(drop_lease_end="2025-09-30"))
        ok("租赁缺口:stderr 有警告、gaps 进 JSON、退出码仍是 0",
           "2025-09-30" in se and len(json.loads(so)["gaps"]) == 2 and rc == 0, (rc, se[:80]))
    except Exception as e:
        ok("main() 退出码检查不报错", False, f"{type(e).__name__}: {e}")

    print(f"\n{'全部通过' if not fails else f'{fails} 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
