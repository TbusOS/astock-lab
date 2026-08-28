#!/usr/bin/env bash
# sync_from_upstream —— 把公开仓的更新合进你的私有副本。
#
# 用法:
#   bash scripts/sync_from_upstream.sh            # 看有什么更新,问你要不要合
#   bash scripts/sync_from_upstream.sh --check    # 只看,不合
#   bash scripts/sync_from_upstream.sh --yes      # 直接合,不问
#
# 为什么是 merge 不是 rebase:
#   你的私有副本有公开仓永远不会有的提交(持仓、报告、决策记录)。
#   rebase 会把这些提交重写在上游之上 —— 每次同步都改写你自己的历史,
#   而且一旦有冲突就在半路停住,状态很难解释。merge 只加一个合并提交,
#   你的历史原样保留,冲突也只在真正重叠的文件上出现。
set -eu
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$LAB"

MODE="ask"
for a in "$@"; do
  case "$a" in
    --check) MODE="check" ;;
    --yes|-y) MODE="yes" ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
  esac
done

git remote get-url upstream >/dev/null 2>&1 || {
  echo "❌ 没有 upstream remote。"
  echo "   加上:git remote add upstream https://github.com/TbusOS/astock-lab.git"
  exit 2; }

# 只拦**已跟踪文件的未提交改动** —— merge 撞上这些是最难收拾的情况。
# 未跟踪文件(?? 开头)不拦:私有副本几乎总有新报告、新数据、新笔记,
# 那些不参与 merge。真撞上同名文件时 git 自己会报
# "untracked working tree file would be overwritten",信息比这里拦得更准。
dirty=$(git status --porcelain | grep -v '^??' || true)
if [ -n "$dirty" ]; then
  echo "❌ 已跟踪文件有未提交改动,先 commit 或 stash 再同步:"
  printf '%s\n' "$dirty" | sed 's/^/     /'
  exit 1
fi
untracked=$(git status --porcelain | grep -c '^??' || true)
[ "$untracked" -gt 0 ] && echo "（有 $untracked 个未跟踪文件，不影响同步）"

echo "拉取 upstream…"
git fetch -q upstream

BR=$(git rev-parse --abbrev-ref HEAD)
n=$(git rev-list --count HEAD..upstream/main 2>/dev/null || echo 0)
if [ "$n" = "0" ]; then
  echo "✅ 已是最新,公开仓没有新提交。"
  exit 0
fi

echo
echo "公开仓领先 $n 个提交:"
git log --oneline --no-decorate HEAD..upstream/main | sed 's/^/  /'
echo
echo "会动到的文件:"
git diff --stat HEAD upstream/main -- . ':(exclude)private' ':(exclude)data' | sed 's/^/  /'
echo

[ "$MODE" = "check" ] && { echo "(--check:只看不合)"; exit 0; }

if [ "$MODE" = "ask" ]; then
  printf "合进当前分支 %s? [y/N] " "$BR"
  read -r ans
  case "$ans" in y|Y|yes) ;; *) echo "取消。"; exit 0 ;; esac
fi

if git merge --no-edit upstream/main; then
  echo
  echo "✅ 合完。private/ 与 data/ 是你独有的,不会被上游动到。"
  echo "   建议跑一遍闸:bash scripts/check_gates.sh"
  echo "   然后推到你自己的私有仓:git push origin $BR"
else
  echo
  echo "⚠ 有冲突,解完后:git add <文件> && git commit"
  echo "  最可能冲突的是 .gitignore 和 README —— 那是你改过又被上游也改了的文件。"
  exit 1
fi
