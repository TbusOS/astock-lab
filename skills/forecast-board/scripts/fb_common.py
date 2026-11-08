#!/usr/bin/env python3
"""fb_common —— 预测看板各模块共用的几样东西:工作台在哪、配置、存档目录、北京时间。

配置分两份,都在内部仓(公开仓里不放个人数据和本机路径):
    <工作台>/private/config/board.json   看哪几只、卖出线、期限、节假日……(跟着内部仓同步)
    <工作台>/private/config/local.json   每台机器专属的路径,按主机名分项:
        {"hosts": {"<主机名>": {"board_out_dir": "...", "dashboard_dir": "..."}}}
load_config() 把 board.json 和本机那一项里的 board_out_dir、dashboard_dir 合成一个 dict。
主机名用 socket.gethostname(),跟 finance-pdf-report 的 html2pdf.mjs 用的 os.hostname() 是同一个值。

工作台没设 STOCK_LAB 时,_lab_root() 会落到代码所在的仓库(公开仓有 .astock-lab-root 标记),
那里没有 private/config/board.json —— 真实运行要先 export STOCK_LAB=<内部仓>。
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST_KEYS = ("board_out_dir", "dashboard_dir")

# 进程启动时的环境变量(含代理),在 import 本模块那一刻拍下来。
# 为什么要拍:market.py、industry.py 这些取数脚本一 import 就把 http(s)_proxy / all_proxy
# 从 os.environ 里删掉(国内的源直连更稳);之后再起的子进程(ssh、claude、codex)
# 和少数要走代理的请求就拿不到代理了。这些地方传 env=ORIG_ENV,或从这里取代理地址。
# ⚠ 必须先 import 本模块、再 import prices / market,否则拍到的已经是删过的环境。
ORIG_ENV = dict(os.environ)


# ── 工作台根目录定位 ────────────────────────────────────────────────────────
# ⚠ 这段在多个脚本里各有一份,**必须逐字一致**,由 scripts/check_lab_root_sync.py 比对。
#   为什么不做成共享模块:这几个脚本分属不同 skill,跨 skill import 要先解决
#   「怎么找到那个模块」—— 那是同一个问题,套娃解决不了。
def _lab_root():
    """工作台根目录。顺序:$STOCK_LAB → 往上找 .astock-lab-root → 老默认路径。"""
    import os as _os
    e = _os.environ.get("STOCK_LAB")
    if e and Path(e).is_dir():
        return Path(e)
    for d in Path(__file__).resolve().parents:
        if (d / ".astock-lab-root").exists():
            return d
    for d in (Path.home() / "astock-lab-private", Path.home() / "astock-lab",
              Path.home() / "claude-tools" / "astock-lab-private",
              Path.home() / "claude-tools" / "astock-lab"):
        if d.is_dir():
            return d
    return Path.cwd()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到 {path}。预测看板的配置在内部仓里,先 export STOCK_LAB=<内部仓>;"
            f"现在的工作台是 {_lab_root()}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} 不是合法 JSON:{e}") from e


def load_config() -> dict:
    """board.json + local.json 里本机那一项的 board_out_dir、dashboard_dir。

    每次调用都重新定位工作台、重新读文件(不缓存),改了配置马上生效。
    缺文件抛 FileNotFoundError(写出完整路径);local.json 里没有本机、
    或本机那一项缺这两个键,抛 KeyError(写出主机名和要补的键)——
    不悄悄用默认目录,免得看板写到没人看的地方。
    """
    cfg_dir = _lab_root() / "private" / "config"
    board = _read_json(cfg_dir / "board.json")
    local_path = cfg_dir / "local.json"
    hosts = _read_json(local_path).get("hosts") or {}
    host = socket.gethostname()
    entry = hosts.get(host) or {}
    missing = [k for k in HOST_KEYS if not entry.get(k)]
    if missing:
        raise KeyError(f"{local_path} 的 hosts 里没有本机 {host!r} 的 {', '.join(missing)},"
                       f"照已有那几台机器的写法加一项")
    return {**board, **{k: entry[k] for k in HOST_KEYS}}


def archive_root() -> Path:
    """预测存档的根目录:<工作台>/data/forecasts。只返回路径,不建目录。"""
    return _lab_root() / "data" / "forecasts"


def _shanghai():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except (ImportError, KeyError):   # ZoneInfoNotFoundError 是 KeyError 的子类
        # 没有时区数据库时退回固定 +8:中国 1991 年以后不实行夏令时,两者等价
        return timezone(timedelta(hours=8), "Asia/Shanghai")


def now_cn() -> datetime:
    """现在的北京时间(带时区)。"""
    return datetime.now(_shanghai())
