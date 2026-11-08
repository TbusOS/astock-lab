#!/usr/bin/env python3
"""replay —— 拿一次存档冻结的输入包,用指定版本的代码重算统计底线和系统算法两层。

它是什么:错题的原因里,「数据没取全」和「算法有问题」光看结果分不开。重跑把两样拆开换:
    旧算法 + 原输入        先确认能复现当时的结果
    旧算法 + 修正后的输入  看数据问题的影响
    新算法 + 原输入        看算法修改的影响
    新算法 + 修正后的输入  看两者一起的影响
没有它会怎样:只能凭感觉说「补了数据就对了」,说不出补数据到底让结论动了多少。

规矩:
  - 交给 recompute 的文件自带全部材料:{"items": {键: 一项}, "run_meta": {as_of、start、codes、holidays、
    期限等}}(board.py forecast 存的 inputs.json 就是这个形状)。recompute 不读配置,只读 --inputs 那个文件;
    子进程去掉 STOCK_LAB 和 PYTHONPATH,免得读到别处的数据或模块。
  - 修正后的输入只能改数据:
      · 改过或新加的每一项都要有 public_at 且不晚于存档的 as_of(check_corrected)——
        后来才公布的数只能拿来解释,不能算成当时做得到的预测;
      · 原来有的项一个都不能少(删掉一项等于假装当时没取到);
      · run_meta 要么不写,要么和原来一字不差;交给 recompute 的 run_meta 一律用原来那份。
    不合格直接拒绝,不跑。replay 自己在临时目录拼出 {"items": 修正后的项, "run_meta": 原来的} 交给 recompute。
  - 代码版本:git worktree add 到系统临时目录;临时目录落在任何 git 工作区里(包括要重跑的那个仓库)
    就报错。跑完不管成败都 git worktree remove --force --force(锁住的也删),再删临时目录。
    code_ref 写 RUN_CODE("run")= 存档当时那份代码:manifest 的提交号 + code.diff(未提交改动)
    + untracked/(没进 git 的文件)。存档时有文件没抄下来(manifest 的 untracked_skipped 非空)照样跑,
    但结果里 frozen 记 False(不是原样还原)。
  - 跑的命令(board.py 的 recompute 子命令):
        <venv python> <worktree>/skills/forecast-board/scripts/board.py recompute --inputs <文件> --seed <种子>
    stdout 只有一段 JSON {"baseline": {代码: 统计底线}, "system": {代码: 系统算法}}。种子取 manifest 里的。
    venv:$ASTOCK_VENV → 本仓 .venv(和 scripts/check_gates.sh 同一个约定)。

入口:replay(run, code_ref, corrected=None, repo=None, runner=None) → 两层结果 + 用的版本;
     compare(a, b) → 两次结果的差(b − a);check_corrected(items, as_of) → 不合格的键。
检查:scripts/check_board_review.py(编排用假 runner;另有一块在一次性 git 仓库里真跑 worktree)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath

import fb_common  # 先于 archive:archive 会加载 market.py,它一 import 就删掉代理变量
import archive

REPO = archive.REPO                       # 正在跑的这份代码所在的仓库
BOARD_REL = Path("skills") / "forecast-board" / "scripts" / "board.py"
RUN_CODE = "run"                          # code_ref 写这个 = 存档当时那份代码(提交号 + 未提交改动)
LAYERS = ("baseline", "system")
DROP_ENV = ("STOCK_LAB", "PYTHONPATH")
TIMEOUT = 900
NO_RECOMPUTE = "这个版本还没有 recompute 命令"


# ── 修正后的输入 ────────────────────────────────────────────────────────────
def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def check_corrected(items: dict[str, dict], as_of: str) -> list[str]:
    """修正的那几项里,不能证明 as_of 当天或以前已经公开的键(排好序):public_at 缺失、
    写不成日期、或晚于 as_of。public_at 带时刻的按日期比。"""
    cut = _day(as_of) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)) else None
    if cut is None:
        raise ValueError(f"as_of 要是 YYYY-MM-DD,得到 {as_of!r}")
    return sorted(k for k, v in items.items()
                  if (d := _day((v or {}).get("public_at") or "")) is None or d > cut)


def _split(obj, where: str) -> tuple[dict, dict | None, bool]:
    """输入包 → (各项, run_meta, 有没有写 run_meta)。认 {"items", "run_meta"} 和老的平铺两种。"""
    if not isinstance(obj, dict):
        raise ValueError(f"{where} 要是 JSON 对象")
    if isinstance(obj.get("items"), dict):
        return obj["items"], obj.get("run_meta"), "run_meta" in obj
    if "run_meta" in obj:
        raise ValueError(f"{where} 写了 run_meta 却没有 items")
    return obj, None, False


def corrected_package(run: Path, corrected: Path) -> tuple[dict, list[str]]:
    """→ (交给 recompute 的输入包 {"items": 修正后的项, "run_meta": 原来的}, 改过或新加的键)。
    少了原来的项、run_meta 和原来不一样、改过的项没证明当时已公开,都抛 ValueError。
    原输入包是老的平铺形状(没有 run_meta)时,拼出来的也不带 run_meta。"""
    as_of = run.parent.name
    o_items, o_meta, _ = _split(json.loads((run / "inputs.json").read_text(encoding="utf-8")), "原输入包")
    c_items, c_meta, has_meta = _split(json.loads(Path(corrected).read_text(encoding="utf-8")), "修正文件")
    if has_meta and c_meta != o_meta:
        raise ValueError("修正文件只能改数据(items):run_meta 要么不写,要么和原来一字不差;"
                         "起点日、股票清单、节假日、期限这些一改,重跑的就不是当时那道题了")
    removed = sorted(set(o_items) - set(c_items))
    if removed:
        raise ValueError(f"修正文件少了原来有的项 {removed}:修正只能改或补,不能删"
                         "(删掉等于假装当时没取到这项数据)")
    changed = sorted(k for k, v in c_items.items() if o_items.get(k) != v)
    bad = check_corrected({k: c_items[k] for k in changed}, as_of)
    if bad:
        raise ValueError(f"修正后的输入里这几项没证明 {as_of} 当时已经公开(public_at 缺失或更晚):{bad};"
                         "后来才公布的数只能拿来解释,不能拿来重跑")
    pkg = {"items": c_items, **({"run_meta": o_meta} if o_meta is not None else {})}
    return pkg, changed


# ── 存档当时那份代码 ────────────────────────────────────────────────────────
def _safe_rel(rel) -> str:
    """manifest 里没进 git 的文件路径:要是仓库内的相对路径,不许绝对路径、不许 ..。"""
    p = PurePosixPath(rel) if isinstance(rel, str) else None
    if p is None or not rel.strip() or p.is_absolute() or ".." in p.parts or "\\" in rel or rel.startswith("~"):
        raise ValueError(f"manifest 的 untracked 里有不合格的路径 {rel!r}(要是仓库内的相对路径)")
    return rel


def check_frozen(run: Path, manifest: dict) -> list[dict]:
    """版本写 run 时先查存档够不够还原:工作区不干净却没有 code.diff → FileNotFoundError;
    untracked 路径不合格 → ValueError。返回 manifest 的 untracked_skipped(存档时没抄下来的文件)。"""
    if manifest.get("clean") is not True and not (run / "code.diff").is_file():
        raise FileNotFoundError(f"{run / 'code.diff'} 不在,而存档时工作区不干净:没法还原当时的代码")
    for rel in manifest.get("untracked") or []:
        _safe_rel(rel)
    return list(manifest.get("untracked_skipped") or [])


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _apply_frozen(wt: Path, run: Path) -> None:
    """把存档当时的未提交改动(code.diff)和没进 git 的文件(untracked/)放进 worktree。"""
    diff = run / "code.diff"
    if diff.is_file() and diff.read_text(encoding="utf-8").strip():
        archive._git(wt, "apply", str(diff.resolve()))
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    for rel in manifest.get("untracked") or []:
        src, dst = run / "untracked" / _safe_rel(rel), wt / rel
        if not _inside(dst, wt):
            raise ValueError(f"untracked 路径 {rel!r} 落到了 worktree 外面")
        if not src.is_file():
            raise FileNotFoundError(f"存档 manifest 列了 {rel},但 {src} 不在")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


# ── 在某个版本的代码里跑 recompute ──────────────────────────────────────────
def venv_python(env=None) -> Path:
    """$ASTOCK_VENV/bin/python,没设就用本仓 .venv;找不到抛 RuntimeError。"""
    env = os.environ if env is None else env
    py = Path(env.get("ASTOCK_VENV") or REPO / ".venv") / "bin" / "python"
    if not py.exists():
        raise RuntimeError(f"找不到 {py}:设 ASTOCK_VENV=<装好依赖的 venv 目录>")
    return py


def _in_work_tree(path: Path) -> bool:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                       text=True, env=fb_common.ORIG_ENV, timeout=60)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _run_board(py: Path, board: Path, inputs: Path, seed: int, cwd: Path, timeout: int) -> str:
    env = {k: v for k, v in fb_common.ORIG_ENV.items() if k not in DROP_ENV}
    try:
        r = subprocess.run([str(py), str(board), "recompute", "--inputs", str(inputs), "--seed", str(seed)],
                           capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"recompute 超过 {timeout} 秒没跑完,已停掉") from e
    if r.returncode != 0:
        if "invalid choice" in r.stderr and "recompute" in r.stderr:
            raise RuntimeError(f"{NO_RECOMPUTE}(board.py 不认 recompute)")
        raise RuntimeError(f"recompute 失败(退出码 {r.returncode}):{(r.stderr or r.stdout).strip()[-800:]}")
    return r.stdout


def _remove_worktree(repo: Path, wt: Path) -> None:
    try:
        archive._git(repo, "worktree", "remove", "--force", "--force", str(wt))
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"⚠ 删 worktree {wt} 失败:{e}\n  临时目录照删;仓库里可能留下这条登记,"
              f"清法:git -C {repo} worktree unlock {wt}(锁住了才要)再 git -C {repo} worktree prune",
              file=sys.stderr)


def run_recompute(repo: Path, code_ref: str, inputs: Path, seed: int, frozen_run: Path | None = None,
                  python: Path | None = None, timeout: int = TIMEOUT) -> tuple[str, str]:
    """在 repo 的 code_ref 版本里跑 board.py recompute,返回 (提交号, stdout)。
    frozen_run 给了就先把那次存档的未提交改动和没进 git 的文件放进去。
    临时目录在 git 工作区里就报错;worktree 和临时目录不管成败都删掉。"""
    repo = Path(repo).resolve()
    py = Path(python) if python is not None else venv_python()
    sha = archive._git(repo, "rev-parse", "--verify", f"{code_ref}^{{commit}}").strip()
    tmp = Path(tempfile.mkdtemp(prefix="board-replay-")).resolve()
    wt = tmp / f"code-{sha[:12]}"
    try:
        if _inside(tmp, repo) or _in_work_tree(tmp):
            raise RuntimeError(f"临时目录 {tmp} 落在 git 工作区里,换个 TMPDIR 再跑")
        archive._git(repo, "worktree", "add", "--detach", str(wt), sha)
        if frozen_run is not None:
            _apply_frozen(wt, Path(frozen_run))
        board = wt / BOARD_REL
        if not board.is_file():
            raise RuntimeError(f"{NO_RECOMPUTE}({code_ref} = {sha[:7]} 里没有 {BOARD_REL})")
        return sha, _run_board(py, board, Path(inputs).resolve(), seed, tmp, timeout)
    finally:
        if wt.exists():
            _remove_worktree(repo, wt)
        shutil.rmtree(tmp, ignore_errors=True)


def parse_recompute(text: str) -> dict:
    """recompute 的 stdout → {"baseline": dict, "system": dict};不合约定抛 ValueError。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"recompute 输出的不是 JSON:{e};开头是 {text[:200]!r}") from e
    bad = [k for k in LAYERS if not isinstance((obj if isinstance(obj, dict) else {}).get(k), dict)]
    if bad:
        raise ValueError(f"recompute 输出缺 {bad}(要是 {{代码: 结果}} 的 dict)")
    return {k: obj[k] for k in LAYERS}


# ── 入口 ────────────────────────────────────────────────────────────────────
def replay(run: Path, code_ref: str, corrected: Path | None = None, repo: Path | None = None,
           runner=None) -> dict:
    """用 code_ref 版本的代码重算 run 那次存档的两层。返回
    {"run", "code": {"ref", "commit", "frozen", "untracked_skipped"}, "inputs"(原输入包或修正文件),
     "corrected": bool, "changed": [改过或新加的键], "seed", "baseline": {代码: …}, "system": {代码: …}}。
    code_ref:提交号 / 分支 / HEAD,或 RUN_CODE 表示存档当时那份代码;frozen 只在原样还原时为 True。
    repo 默认是本仓。runner(repo, code_ref, inputs, seed, frozen_run) → (提交号, stdout),
    默认 run_recompute;检查时换成假的。修正时交给 runner 的是临时拼出来的文件,跑完删掉。"""
    if not isinstance(code_ref, str) or not code_ref.strip() or code_ref.startswith("-"):
        raise ValueError(f"code_ref 要是提交号 / 分支 / HEAD / {RUN_CODE},得到 {code_ref!r}")
    run = Path(run)
    for need in (run / "manifest.json", run / "inputs.json", *([Path(corrected)] if corrected else [])):
        if not need.is_file():
            raise FileNotFoundError(f"{need} 不在:存档不完整或修正文件路径写错,没法重跑")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    frozen = code_ref == RUN_CODE
    skipped = check_frozen(run, manifest) if frozen else []
    ref, seed = (manifest["git_head"] if frozen else code_ref), int(manifest["seed"])
    source = (Path(corrected) if corrected is not None else run / "inputs.json").resolve()
    tmp = None
    try:
        if corrected is None:
            inputs, changed = source, []
        else:
            pkg, changed = corrected_package(run, Path(corrected))
            tmp = Path(tempfile.mkdtemp(prefix="board-replay-inputs-"))
            inputs = (tmp / "inputs.json").resolve()
            inputs.write_text(json.dumps(pkg, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        sha, out = (runner or run_recompute)(Path(repo) if repo is not None else REPO, ref, inputs, seed,
                                             run if frozen else None)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return {"run": str(run),
            "code": {"ref": code_ref, "commit": sha, "frozen": frozen and not skipped, "untracked_skipped": skipped},
            "inputs": str(source), "corrected": corrected is not None, "changed": changed, "seed": seed,
            **parse_recompute(out)}


def _p_ups(layer: dict) -> dict:
    return {h: v["p_up"] for h, v in ((layer or {}).get("horizons") or {}).items()
            if isinstance(v, dict) and v.get("p_up") is not None}


def compare(a: dict, b: dict) -> dict:
    """两次结果(replay 的返回或 recompute 的输出)的差,一律 b − a:
    {层: {代码: {"p_up": {期限: 差}, "bottom_months": {月份: 差}}}}。
    期限只比两边都有的;见底月份取并集,一边没有的月份按 0 算;
    只在一边有的股票写 {"missing_in": "a" | "b"}。"""
    out = {}
    for name in LAYERS:
        la, lb = a.get(name) or {}, b.get(name) or {}
        rows = {}
        for code in sorted(set(la) | set(lb)):
            if code not in la or code not in lb:
                rows[code] = {"missing_in": "a" if code not in la else "b"}
                continue
            pa, pb = _p_ups(la[code]), _p_ups(lb[code])
            ma = ((la[code].get("bottom") or {}).get("month_probs")) or {}
            mb = ((lb[code].get("bottom") or {}).get("month_probs")) or {}
            rows[code] = {"p_up": {h: pb[h] - pa[h] for h in sorted(set(pa) & set(pb), key=int)},
                          "bottom_months": {m: mb.get(m, 0) - ma.get(m, 0) for m in sorted(set(ma) | set(mb))}}
        out[name] = rows
    return out
