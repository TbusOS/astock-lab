#!/usr/bin/env python3
"""desensitize —— 把个人痕迹换成中性示例。公开仓与 whetstone 镜像共用这一份规则。

用法:
    python3 scripts/desensitize.py <目录或文件>...
    python3 scripts/desensitize.py --dry <目录>      # 只看会改什么,不写

为什么要写成脚本而不是手改:
    ① 公开仓和 whetstone 各存一份 skill,**必须逐字节相同**才能让 md5 对账闸有意义。
       手改两遍必然不一致。
    ② 以后每次从 whetstone 同步过来都要再跑一次 —— 手动流程会漏,脚本不会。
    ③ 替换规则本身就是「哪些算个人痕迹」的权威定义,写在代码里才有人维护。

替换的不是「敏感词」而是**个人痕迹**:真实持仓成本、家目录用户名、
本机专属路径、私有仓引用、个人设备。股票代码本身是公开信息,保留作示例。
"""

import argparse
import re
import sys
from pathlib import Path

# (正则, 替换, 说明) —— 顺序有意义:先长后短,避免部分替换
RULES = [
    # 真实持仓成本 → 明显是示例的整数值。精确到小数点后三位一看就是真成交价
    (r"498\.957",  "400.00",  "真实成本价 → 示例值"),
    (r"1157\.897", "1100.00", "真实成本价 → 示例值"),
    # 家目录与用户名
    (r"/home/zhangbh/claude-tools/whetstone-skills-private/skills",
     "$LAB/skills", "私有仓 skill 路径 → 本仓相对"),
    (r"~/claude-tools/whetstone-skills-private/skills",
     "$LAB/skills", "私有仓 skill 路径 → 本仓相对"),
    (r"/home/zhangbh/claude-tools/stock-lab", "$LAB", "本机路径 → 变量"),
    (r"~/claude-tools/stock-lab", "$LAB", "本机路径 → 变量"),
    (r"/home/zhangbh/akshare-venv", "$VENV", "本机 venv → 变量"),
    (r"~/akshare-venv", "$VENV", "本机 venv → 变量"),
    (r"/home/zhangbh", "$HOME", "家目录 → 变量"),
    # macOS 侧的家目录 —— 2026-08-28 被闸抓到过:只写 /home/ 会漏掉 /Users/
    (r"/Users/sky/stock-lab", "$LAB", "另一台机器的路径 → 变量"),
    (r"/Users/sky", "$HOME", "macOS 家目录 → 变量"),
    (r"MacBook sky|\bsky 的|\bsky\b(?= 的?终端)", "另一台机器", "设备昵称"),
    (r"\bzhangbh\b", "user", "用户名"),
    # 私有仓引用
    (r"whetstone-skills-private", "本仓", "私有仓名"),
    (r"\bwhetstone\b", "本仓", "私有仓名"),
    # 个人设备与网络环境
    (r"家用 ?mac-?mini|家用 ?Mac ?mini|家里的 ?mac-?mini", "另一台网络不受限的机器", "个人设备"),
    (r"mac-?mini", "另一台机器", "个人设备"),
    (r"公司网 ?\+ ?全局代理", "受限网络 + 全局代理", "工作环境"),
    (r"公司网", "受限网络", "工作环境"),
    (r"公司机(?:器)?", "这台受限机器", "工作环境"),
    (r"家里(?:可用|能用|可以)", "网络不受限时可用", "个人环境"),
    (r"TbusOS 自有", "作者自有", "账号名"),
    # 私有仓名 —— 2026-08-28 被闸抓到:公开文档点名私有仓等于宣告它存在,
    # 而且那几段还引用了它的内部文件路径和实现缺陷
    (r"`repos/a-stock-ai/[^`]*`", "某自研脚手架的设计文档", "私有仓内部路径"),
    (r"repos/a-stock-ai", "某自研脚手架", "私有仓内部路径"),
    (r"a-stock-ai(?:（自家仓）|\(自家仓\))?", "某自研脚手架", "私有仓名"),
    (r"自家仓", "一个自研脚手架", "私有仓指代"),
]


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
    #   而你以为它在把关。2026-08-28 真踩过:whetstone 被换成「本仓」,
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
