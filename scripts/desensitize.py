#!/usr/bin/env python3
"""desensitize —— 把个人痕迹换成中性示例。公开仓与任何镜像副本共用这一份规则。

用法:
    python3 scripts/desensitize.py <目录或文件>...
    python3 scripts/desensitize.py --dry <目录>      # 只看会改什么,不写

为什么要写成脚本而不是手改:
    ① 公开仓和镜像副本各存一份 skill,**必须逐字节相同**才能让 md5 对账闸有意义。
       手改两遍必然不一致。
    ② 以后每次从镜像同步过来都要再跑一次 —— 手动流程会漏,脚本不会。
    ③ 替换规则本身就是「哪些算个人痕迹」的权威定义,写在代码里才有人维护。

替换的不是「敏感词」而是**个人痕迹**:真实持仓成本、家目录用户名、
本机专属路径、私有仓引用、个人设备。股票代码本身是公开信息,保留作示例。
"""

import argparse
import re
import sys
from pathlib import Path

# (正则, 替换, 说明) —— 顺序有意义:先长后短,避免部分替换
# ★ 你自己的替换规则(用户名 / 私有仓名 / 内部代号)放 personal.rules,**不进仓**。
#   理由和 check_public_safe.sh 的 personal.patterns 一样:
#   「把 <你的登录名> 换成 user」这行字本身就泄露了 <你的登录名>。
#   2026-08-28 实测:这个文件当时就写着作者两台机器的用户名和 5 个私有仓名,
#   而它被排除在闸的扫描外(否则会自命中),所以一直没报。
#   格式:每行 `正则<TAB>替换<TAB>说明`,# 开头是注释。见 personal.rules.example。
def _load_personal():
    f = Path(__file__).resolve().parent / "personal.rules"
    if not f.exists():
        return []
    out = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        p = ln.split("\t")
        if len(p) >= 2:
            out.append((p[0], p[1], p[2] if len(p) > 2 else "个人规则"))
    return out


# ── 通用规则:与「作者是谁」无关,可以公开 ──────────────────────────────────
GENERIC = [
    # 精确到小数点后三位的价格,几乎一定是真实成交价
    (r"\b\d{2,5}\.\d{3}\b", "<成本价>", "疑似真实成交价"),
    # 任何人的家目录绝对路径
    (r"/home/[a-z][a-z0-9_-]*", "$HOME", "Linux 家目录"),
    (r"/Users/[a-z][a-z0-9_-]*", "$HOME", "macOS 家目录"),
    (r"~/akshare-venv", "$VENV", "常见 venv 路径"),
    # 个人设备与网络环境的描述
    (r"家用 ?mac-?mini|家用 ?Mac ?mini|家里的 ?mac-?mini",
     "另一台网络不受限的机器", "个人设备"),
    (r"mac-?mini", "另一台机器", "个人设备"),
    (r"公司网 ?\+ ?全局代理", "受限网络 + 全局代理", "工作环境"),
    (r"公司网", "受限网络", "工作环境"),
    (r"公司机(?:器)?", "这台受限机器", "工作环境"),
    (r"家里(?:可用|能用|可以)", "网络不受限时可用", "个人环境"),
]

# 个人规则**先跑** —— 它们更具体(如整条私有仓路径),先替换掉才不会被
# 通用的「/home/<任意用户名>」规则切成半截。
RULES = _load_personal() + GENERIC



def apply(text):
    hits = []
    for pat, rep, why in RULES:
        n = len(re.findall(pat, text))
        if n:
            text = re.sub(pat, rep, text)
            hits.append((why, n))
    return text, hits


def main():
    ap = argparse.ArgumentParser(prog="desensitize")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--dry", action="store_true", help="只报告，不写文件")
    a = ap.parse_args()

    # ⚠ 必须排除自己:本文件正文就是那张替换表,跑到自己头上会把规则也换掉
    me = Path(__file__).resolve()

    files = []
    for p in a.paths:
        p = Path(p)
        if p.is_dir():
            files += [f for f in p.rglob("*")
                      if f.is_file() and f.suffix in
                      {".md", ".py", ".sh", ".html", ".mjs", ".txt", ".css"}
                      and "__pycache__" not in f.parts]
        elif p.is_file():
            files.append(p)
    files = [f for f in files if f.resolve() != me]

    # ⚠ 带 `desensitize:skip` 标记的文件不改。
    #   为什么需要这个:闸(check_public_safe.sh)的正则里**本来就写着要查的那些词**,
    #   跑到它头上会把正则改掉 —— 闸从此查一个不存在的模式,永远全绿,
    #   而你以为它在把关。2026-08-28 真踩过:某个私有仓名被替换掉,
    #   闸当场开始扫自己的说明文字,同时不再查真正的私有仓名。
    kept = []
    for f in files:
        try:
            head = f.read_text(encoding="utf-8")[:2000]
        except (UnicodeDecodeError, OSError):
            continue
        if "desensitize:skip" in head:
            print(f"  ⏭  跳过(带 skip 标记):{f}")
            continue
        kept.append(f)
    files = kept

    total, changed = 0, 0
    for f in sorted(files):
        try:
            src = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out, hits = apply(src)
        if out == src:
            continue
        changed += 1
        total += sum(n for _, n in hits)
        detail = "，".join(f"{w}×{n}" for w, n in hits)
        print(f"  {'[dry] ' if a.dry else ''}{f}：{detail}")
        if not a.dry:
            f.write_text(out, encoding="utf-8")
    print(f"\n{changed} 个文件、{total} 处{'待改' if a.dry else '已改'}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
