#!/usr/bin/env python3
"""archive —— 预测存档:只追加,不覆盖。

每次跑预测,整套输入、各人的回答、代码版本都冻结进
    <存档根>/<日期>/<时分秒>/
以后拿它结算打分、原样重放。任何文件写过就不再改:
  - new_run 用 mkdir(exist_ok=False) 原子建目录,同一秒重复建会报 FileExistsError
  - write_json_once 先写临时文件,再 os.link 到正式名;正式名已存在报 FileExistsError,
    中途崩了也不会留下写了一半的正式文件
  - 重跑不改旧目录,只在旧目录里多写一个 superseded.json 指向新目录

存档放在内部仓(含个人卖出线等),绝不写进公开仓。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import fb_common
import prices

REPO = Path(__file__).resolve().parents[3]          # 正在跑的这份代码所在的仓库
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{6}$")


def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else fb_common.archive_root()


def new_run(as_of: str, now: datetime | None = None, root: Path | None = None) -> Path:
    """建 <root>/<as_of>/<HHMMSS>/。已存在抛 FileExistsError。"""
    if not _DATE_RE.match(as_of):
        raise ValueError(f"as_of 必须是 YYYY-MM-DD,得到 {as_of!r}")
    now = now or fb_common.now_cn()
    day_dir = _root(root) / as_of
    day_dir.mkdir(parents=True, exist_ok=True)
    run = day_dir / now.strftime("%H%M%S")
    run.mkdir(exist_ok=False)
    return run


def _write_bytes_once(path: Path, data: bytes) -> None:
    """先写临时文件并 fsync,再 os.link 成正式名:正式名要么不存在,要么是完整文件。
    正式名已存在抛 FileExistsError。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)          # 目标已存在会抛 FileExistsError
    finally:
        os.unlink(tmp)


def write_json_once(path: Path, obj) -> None:
    """写 JSON,文件已存在就抛 FileExistsError,绝不覆盖。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    _write_bytes_once(path, (text + "\n").encode("utf-8"))


def _write_text_once(path: Path, text: str) -> None:
    _write_bytes_once(path, text.encode("utf-8"))


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env=fb_common.ORIG_ENV, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败(仓库 {repo}):{r.stderr.strip()}")
    return r.stdout


UNTRACKED_DIRS = ("skills/", "scripts/")
UNTRACKED_EXTS = (".py", ".md", ".json", ".sh", ".mjs")
UNTRACKED_MAX = 1024 * 1024


def _freeze_untracked(run: Path, repo: Path) -> tuple[list[str], list[dict]]:
    """把 skills/、scripts/ 下没进 git 的代码文件原样抄进 <run>/untracked/。
    返回 (抄了的路径, 没抄的路径和原因)。其他目录(如 .superpowers/)忽略。"""
    out = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    taken, skipped = [], []
    for rel in sorted(x for x in out.split("\0") if x):
        if not rel.startswith(UNTRACKED_DIRS):
            continue
        if not rel.endswith(UNTRACKED_EXTS):
            skipped.append({"path": rel, "reason": "扩展名不在范围内"})
            continue
        src = Path(repo) / rel
        try:
            size = src.stat().st_size
            if size > UNTRACKED_MAX:
                skipped.append({"path": rel, "reason": f"超过 1 MB({size} 字节)"})
                continue
            data = src.read_bytes()
        except OSError as e:
            skipped.append({"path": rel, "reason": f"读不了:{e}"})
            continue
        _write_bytes_once(Path(run) / "untracked" / rel, data)
        taken.append(rel)
    return taken, skipped


def write_manifest(run: Path, config: dict, seed: int, repo: Path | None = None) -> dict:
    """记下这次跑的是哪版代码:提交号、工作区是否干净、未提交改动全文(code.diff)、
    Python 版本、配置副本、随机种子。"""
    run = Path(run)
    repo = Path(repo) if repo is not None else REPO
    head = _git(repo, "rev-parse", "HEAD").strip()
    tracked_clean = _git(repo, "status", "--porcelain", "--untracked-files=no").strip() == ""
    _write_text_once(run / "code.diff", _git(repo, "diff", "HEAD", "--binary"))
    untracked, skipped = _freeze_untracked(run, repo)
    clean = tracked_clean and not untracked
    manifest = {
        "created_at": fb_common.now_cn().isoformat(timespec="seconds"),
        "git_head": head,
        "clean": clean,
        "untracked": untracked,
        "untracked_skipped": skipped,
        "python": sys.version.split()[0],
        "seed": seed,
        "config": config,
    }
    write_json_once(run / "manifest.json", manifest)
    return manifest


def save_inputs(run: Path, items) -> None:
    write_json_once(Path(run) / "inputs.json", items)


def save_answer(run: Path, author: str, obj: dict) -> None:
    if not re.fullmatch(r"[\w.\-]+", author):
        raise ValueError(f"作者名只许字母数字下划线点横线,得到 {author!r}")
    write_json_once(Path(run) / "answers" / f"{author}.json", obj)


def list_runs(root: Path | None = None, complete_only: bool = True) -> list[Path]:
    """按时间从早到晚列出存档目录。complete_only(默认)跳过没有 manifest.json 的
    (跑到一半崩了的)目录。被 supersede 的旧目录照样列出。"""
    base = _root(root)
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and _DATE_RE.match(d.name):
            out += [r for r in sorted(d.iterdir()) if r.is_dir() and _TIME_RE.match(r.name)
                    and (not complete_only or (r / "manifest.json").is_file())]
    return out


def latest_run(root: Path | None = None, before: str | None = None,
               complete_only: bool = True) -> Path | None:
    """最新一次存档;before 给了('YYYY-MM-DD')只看日期严格早于它的。
    complete_only 同 list_runs;被 supersede 的目录也会被选中。"""
    runs = list_runs(root, complete_only)
    if before is not None:
        runs = [r for r in runs if r.parent.name < before]
    return runs[-1] if runs else None


def supersede(old: Path, new: Path, note: str) -> None:
    """在旧目录写 superseded.json 指向新目录,旧文件一个字节不动。"""
    write_json_once(Path(old) / "superseded.json", {
        "by": str(Path(new).name if Path(new).parent == Path(old).parent
                  else f"{Path(new).parent.name}/{Path(new).name}"),
        "note": note,
        "at": fb_common.now_cn().isoformat(timespec="seconds"),
    })


def _rel(run: Path, lab: Path) -> str:
    try:
        return str(Path(run).resolve().relative_to(Path(lab).resolve()))
    except ValueError:
        return str(run)


class JournalLinkError(RuntimeError):
    """link_journal 中途失败。written_ids 是失败前已经写进决策日志的编号。"""

    def __init__(self, msg: str, written_ids: list[int]):
        super().__init__(f"{msg}(失败前已写入的编号:{written_ids})")
        self.written_ids = written_ids


def link_journal(run: Path, codes: list[str], start_prices: dict, holidays, *, start: date,
                 lab: Path | None = None, journal: Path | None = None) -> list[int]:
    """每只股票在决策日志里记一条 watch,返回新编号。
    复核日 = 从 start(这次存档的起点日,as_of 之后第一个交易日)起数的第 63 个交易日(start 算第 1 个),
    和这次 3 个月题的到期日是同一天。不从「今天」数:周一开盘前出的预测 as_of 是上周五、start 是周一,
    从今天的后一天数会晚一个交易日。
    先检查所有代码都有起点价,缺了就一条也不写;中途某条失败抛 JournalLinkError。"""
    missing = [c for c in codes if c not in start_prices]
    if missing:
        raise KeyError(f"没有起点价,不能写决策日志:{missing}")
    lab = Path(lab) if lab is not None else fb_common._lab_root()
    days = prices.future_trading_days(start, 63, set(holidays))
    check = days[-1].isoformat()
    journal = Path(journal) if journal is not None else \
        REPO / "skills" / "stock-analysis-workflow" / "scripts" / "journal.py"
    env = {**fb_common.ORIG_ENV, "STOCK_LAB": str(lab)}
    ids: list[int] = []
    for code in codes:
        r = subprocess.run(
            [sys.executable, str(journal), "log", code, "--action", "watch",
             "--price", str(start_prices[code]),
             "--thesis", f"预测存档 {_rel(run, lab)}",
             "--falsify", "见存档里各题的结算规则",
             "--check", check],
            capture_output=True, text=True, env=env, timeout=60)
        m = re.search(r"已记录 #(\d+)", r.stdout)
        if r.returncode != 0 or not m:
            raise JournalLinkError(f"journal.py 记录 {code} 失败:{(r.stderr or r.stdout).strip()}", ids)
        ids.append(int(m.group(1)))
    return ids
