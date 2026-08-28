#!/usr/bin/env bash
# check_skill_sync —— 本仓 skills/ 与另一份副本逐字节对账。
#
# 用法:
#   SKILL_MIRROR=/path/to/other/skills bash scripts/check_skill_sync.sh
#   bash scripts/check_skill_sync.sh /path/to/other/skills
#   退出码 0 = 两份完全一致
#
# 什么时候需要:
#   如果你在别处也留了一份这些 skill(比如你自己的 skill 库、另一台机器、
#   团队共享目录),**两份就会漂**。改了一边忘了另一边,而两边都能跑、
#   都不报错 —— 你会在几周后发现同一个工具在两台机器上行为不同,
#   然后花很久才想到是副本不一致。
#
#   这道闸把「记得同步」变成「跑一条命令」。不一致时它告诉你差在哪个文件,
#   同步方向由你决定(它不自动改任何东西)。
set -u
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
MIRROR="${1:-${SKILL_MIRROR:-}}"

if [ -z "$MIRROR" ]; then
  echo "用法:SKILL_MIRROR=<另一份 skills 目录> bash $0"
  echo "     或 bash $0 <另一份 skills 目录>"
  echo
  echo "没有第二份副本就不需要跑这个闸。"
  exit 0
fi
[ -d "$MIRROR" ] || { echo "❌ 镜像目录不存在:$MIRROR"; exit 2; }

echo "== skill 副本对账 =="
echo "   本仓:$LAB/skills"
echo "   镜像:$MIRROR"
echo

diffs=0
for k in astock-quote stock-analysis-workflow finance-pdf-report; do
  a="$LAB/skills/$k"; b="$MIRROR/$k"
  if [ ! -d "$b" ]; then
    printf "  ❌ %-26s 镜像里没有\n" "$k"; diffs=$((diffs+1)); continue
  fi
  # 只比会进 git 的文件;__pycache__ 与 pyc 不算
  out=$(diff -rq --exclude=__pycache__ --exclude='*.pyc' "$a" "$b" 2>&1)
  if [ -z "$out" ]; then
    n=$(find "$a" -type f ! -path '*__pycache__*' ! -name '*.pyc' | wc -l)
    printf "  ✅ %-26s %s 个文件一致\n" "$k" "$n"
  else
    printf "  ❌ %-26s 有差异:\n" "$k"
    printf '%s\n' "$out" | sed 's/^/       /'
    diffs=$((diffs+1))
  fi
done

echo
if [ "$diffs" -gt 0 ]; then
  echo "❌ $diffs 个 skill 两份不一致。"
  echo "   定好哪边是真身,rsync 过去,再重跑本闸:"
  echo "     rsync -a --delete --exclude __pycache__ --exclude '*.pyc' \\"
  echo "           $LAB/skills/<名>/ $MIRROR/<名>/"
  exit 1
fi
echo "✅ 三个 skill 两份逐字节一致。"
