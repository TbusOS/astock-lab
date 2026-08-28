#!/usr/bin/env bash
# install —— 建 venv、装依赖、把 skills 链进 ~/.claude/skills/。
#
# 用法:
#   bash install.sh              # 全装
#   bash install.sh --no-skills  # 只建环境,不碰 ~/.claude/skills/
#   bash install.sh --skills     # 只链 skills,不建环境
#
# 幂等:重复跑安全。已存在的 venv 不重建,已存在的软链原地更新。
set -eu
LAB="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VENV="${ASTOCK_VENV:-$LAB/.venv}"
DO_ENV=1; DO_SKILLS=1
for a in "$@"; do
  case "$a" in
    --no-skills) DO_SKILLS=0 ;;
    --skills)    DO_ENV=0 ;;
    -h|--help)   sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数:$a"; exit 2 ;;
  esac
done

if [ "$DO_ENV" = 1 ]; then
  echo "== 1. Python 环境 =="
  if [ -x "$VENV/bin/python" ]; then
    echo "  venv 已存在:$VENV ($("$VENV/bin/python" -V 2>&1))"
  else
    PY=""
    for c in python3.11 python3.12 python3; do
      command -v $c >/dev/null 2>&1 && { PY=$c; break; }
    done
    [ -z "$PY" ] && { echo "  ❌ 找不到 python3"; exit 2; }
    echo "  用 $PY 建 venv → $VENV"
    "$PY" -m venv "$VENV"
  fi
  echo "  装依赖(几分钟)…"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q akshare baostock pandas requests numpy pyarrow
  echo "  ✅ 必需依赖装好"
  echo "  可选:$VENV/bin/pip install edgartools     # capex --engine both 的交叉验证"
  echo "  可选:npm i playwright && npx playwright install chromium  # 出 PDF"

  # efinance:pip 版常年落后,直接 clone 源码用
  if [ ! -d "$LAB/repos/efinance/efinance" ]; then
    echo "  clone efinance → repos/efinance"
    mkdir -p "$LAB/repos"
    git clone -q --depth 1 https://github.com/Micro-sheep/efinance.git \
      "$LAB/repos/efinance" || echo "  ⚠ clone 失败,手动补:见 README"
  fi
  "$VENV/bin/pip" install -q retry multitasking rich jsonpath 2>/dev/null || true
fi

if [ "$DO_SKILLS" = 1 ]; then
  echo "== 2. Agent Skills =="
  D="$HOME/.claude/skills"
  mkdir -p "$D"
  for k in astock-quote stock-analysis-workflow finance-pdf-report; do
    if [ -e "$D/$k" ] && [ ! -L "$D/$k" ]; then
      echo "  ⚠ $D/$k 已存在且不是软链 —— 跳过,不覆盖你已有的东西"
      continue
    fi
    ln -sfn "$LAB/skills/$k" "$D/$k"
    echo "  ✅ $D/$k -> $LAB/skills/$k"
  done
  echo
  echo "  这三个 skill 让 AI 知道**怎么分析**,不只是**怎么取数**:"
  echo "    stock-analysis-workflow  分析方法:领先/滞后指标、预期差、赔率、基准率、自我进化"
  echo "    astock-quote             取数工具与各数据源的坑"
  echo "    finance-pdf-report       出金融 PDF 的版式"
fi

echo
echo "== 3. 自检 =="
bash "$LAB/tools/bootstrap_check.sh" || true
echo
echo "下一步:"
echo "  export SEC_UA='你的名字 你的邮箱'          # SEC 要求,不设会被限流"
echo "  $VENV/bin/python $LAB/tools/astock.py 300308"
echo "  $VENV/bin/python $LAB/tools/position_report.py 300308:1100"
