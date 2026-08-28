#!/usr/bin/env bash
# setup_private —— 把本仓的一份 clone 改造成「你自己的私有副本」。
#
# 用法(在私有 clone 里跑,不要在公开仓里跑):
#   bash scripts/setup_private.sh
#
# 它做三件事:
#   1. 建 private/ 目录(持仓、报告、会话记录都放这)
#   2. 放一个 data/.gitignore 让 data/ 里的分析记录能被你的仓 track
#   3. 检查 remote 是不是配对了(origin=你的私有仓, upstream=公开仓)
#
# ★ 第 2 步的原理:公开仓的 .gitignore 写的是 `data/*` 而不是 `data/`。
#   git 不允许「父目录被排除后再反选子文件」—— 写成 data/ 的话你永远
#   没法 track 自己的数据。写成 data/* 只排除内容不排除目录本身,
#   于是一个 data/.gitignore(内容 `!*`)就能把它们收回来。
#   而那个文件**只存在于你这边**,从公开仓同步时零冲突。
set -eu
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$LAB"

echo "== 1. private/ 目录 =="
mkdir -p private/{portfolio,reports,sessions,notes}
for d in portfolio reports sessions notes; do
  [ -f "private/$d/.gitkeep" ] || touch "private/$d/.gitkeep"
done
if [ ! -f private/README.md ]; then
  cat > private/README.md <<'EOF'
# private/ —— 只属于这台机器的东西

公开仓的 `.gitignore` 里有 `private/`，所以**这个目录永远不会流向公开仓**。
从公开仓同步更新时它也不受影响。

| 子目录 | 放什么 |
|---|---|
| `portfolio/` | 真实持仓与成本价 |
| `reports/` | 生成的分析报告（md / html / pdf） |
| `sessions/` | 会话记录与摘要 |
| `notes/` | 随手记 |

分析记录（`journal` 写的 `data/journal.jsonl` 与 `data/principles.jsonl`）
不在这里，在 `data/` —— 由 `data/.gitignore` 让它们被 track。
EOF
fi
echo "  ✅ private/{portfolio,reports,sessions,notes}"

echo "== 2. 让 private/ 与 data/ 可被 track =="
cat > private/.gitignore <<'EOF'
# 这个文件只存在于私有副本。
# 公开仓的 .gitignore 写的是 `private/*`（只排除内容，不排除目录本身），
# 所以这里可以把内容反选回来。从公开仓同步时不会冲突：公开仓没有这个文件。
!*

# 但原始会话 transcript 不进仓（后写的规则覆盖前面的 !*）：
#   ① 体积无上限，一次长会话就是几 MB，仓会越来越沉
#   ② 里面是完整的工具输出与环境细节（本机 IP、路径、临时凭据的上下文），
#      即使是私有仓也没必要长期留存
#   ③ 有价值的是你自己整理的摘要，不是流水
# 要留就留 sessions/ 下的摘要 .md。
sessions/raw/
EOF
echo "  ✅ private/.gitignore"
cat > data/.gitignore <<'EOF'
# 这个文件只存在于私有副本。
# 公开仓的 .gitignore 写的是 `data/*`（只排除内容，不排除目录本身），
# 所以这里可以把内容反选回来 —— 分析记录、原则、缓存都进你自己的仓。
# 从公开仓同步时本文件不会冲突：公开仓根本没有它。
!*
# 但缓存的 parquet 是可以重新拉的，占地方，不进仓
perf_cache/
EOF
echo "  ✅ data/.gitignore"

echo "== 3. remote 检查 =="
o=$(git remote get-url origin 2>/dev/null || echo "")
u=$(git remote get-url upstream 2>/dev/null || echo "")
[ -n "$o" ] && echo "  origin  : $o"   || echo "  ⚠ 没有 origin —— 设成你自己的私有仓"
[ -n "$u" ] && echo "  upstream: $u"   || echo "  ⚠ 没有 upstream —— 设成公开仓"
if [ -n "$o" ] && [ "$o" = "$u" ]; then
  echo "  ❌ origin 和 upstream 是同一个 —— 你会把私有数据推进公开仓"
  echo "     修:git remote set-url origin <你自己的私有仓>"
  exit 1
fi
case "$o" in
  *astock-lab.git|*astock-lab)
    echo "  ❌ origin 指向公开仓 —— 私有数据会被推上去"
    echo "     修:git remote rename origin upstream && git remote add origin <你的私有仓>"
    exit 1 ;;
esac

echo
echo "✅ 私有副本就绪。"
echo "   拉公开仓更新:bash scripts/sync_from_upstream.sh"
