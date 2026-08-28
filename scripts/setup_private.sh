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

echo "== 3. 私有副本的落地页 =="
# 放在 .github/ 而不是根目录:GitHub 选 README 的顺序是
# .github/ → 根目录 → docs/,所以这份会盖过根 README.md 显示在仓库首页。
# **而根 README.md 一个字都不用改** —— 它跟上游逐字节相同,
# 于是从上游同步时永远不会在 README 上起冲突。
# 直接改根 README 加个横幅的话,上游每次动 README 开头你都要解一次冲突。
if [ ! -f .github/README.md ]; then
  mkdir -p .github
  _up=$(git remote get-url upstream 2>/dev/null || echo "<公开仓>")
  cat > .github/README.md <<PRIVEOF
# 这是 astock-lab 的**私有副本**

> 工具、skills、文档的**真身在上游**：$_up
> 这里 = 上游全部 + 你自己的数据。

## 先看清楚你在哪个仓

| | 上游（公开） | 这里（私有） |
|---|---|---|
| 工具 / skills / 文档 | **真身，在这改** | 从上游同步来的副本 |
| \`private/\` 持仓、报告、笔记 | 没有 | ✅ 只在这 |
| \`data/journal.jsonl\` 决策记录 | 没有 | ✅ 只在这 |

**改工具、改 skill、改文档要去上游改**，然后同步下来。
在这里改的话，下次同步很可能冲突，而且改动传不回去（除非走
\`docs/08-私有副本.md\` 里的 format-patch 流程）。

## 日常

\`\`\`bash
bash scripts/sync_from_upstream.sh --check   # 上游有什么更新
bash scripts/sync_from_upstream.sh           # 合下来
git push origin main                         # 推到你自己的私有仓
\`\`\`

## ⚠ 别推错地方

\`origin\` 是你的私有仓，\`upstream\` 是公开仓。
**永远不要 \`git push upstream\`** —— 那会把你的持仓成本和决策记录推上公开仓，
而且推上去就撤不回来（GitHub 的事件流、fork、爬虫快照都已经拿到了）。

\`\`\`bash
git remote -v        # 确认一下再动手
\`\`\`

## 工具怎么用

看根目录的 \`README.md\` —— 那是上游那份，工具用法完全一样。
PRIVEOF
  echo "  ✅ .github/README.md（GitHub 首页会显示它，不是根 README.md）"
else
  echo "  已存在 .github/README.md，不覆盖"
fi

echo "== 4. remote 检查 =="
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
