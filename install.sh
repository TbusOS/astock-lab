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
  # 必需 —— 少一个就有整类数据拿不到,所以不设成可选:
  #   akshare/baostock  A 股行情与财务的主干
  #   yfinance          海外上下游的目标价/分析师预估/**评级变动**(A 股也能查到,
  #                     而且它的一致预期池里含外资行,与同花顺不是同一套)
  #   pymupdf           研报 PDF:版面文本 + 表格候选 + **逐页渲染**。
  #                     关键的数常常只在图里,不渲染就等于没读到
  "$VENV/bin/pip" install -q akshare baostock pandas requests numpy pyarrow \
                            yfinance pymupdf
  echo "  ✅ 必需依赖装好"
  echo "  可选:$VENV/bin/pip install edgartools     # capex --engine both 的交叉验证"
  echo "  可选:npm i playwright && npx playwright install chromium  # 出 PDF"
  echo "        出 PDF 时设 PLAYWRIGHT_ROOT 指到装了 playwright 的目录"

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

echo "== 3. 别名 =="
# 生成一个可 source 的别名文件,而不是直接改用户的 ~/.bashrc ——
# 往别人的 shell 配置里写东西是很冒犯的,而且卸载时没人记得删。
cat > "$LAB/aliases.sh" <<ALIASEOF
# 由 install.sh 生成。用法:source $LAB/aliases.sh
# 想每次开终端都有:echo "source $LAB/aliases.sh" >> ~/.bashrc
export STOCK_LAB="$LAB"
alias astock='$VENV/bin/python $LAB/tools/astock.py'
alias efdata='$VENV/bin/python $LAB/tools/efdata.py'
alias hcheck='$VENV/bin/python $LAB/tools/holdings_check.py'
alias preport='$VENV/bin/python $LAB/tools/position_report.py'
alias capex='$VENV/bin/python $LAB/tools/capex.py'
alias consensus='$VENV/bin/python $LAB/tools/consensus.py'
alias baserate='$VENV/bin/python $LAB/tools/baserate.py'
alias journal='$VENV/bin/python $LAB/tools/journal.py'
alias research='$VENV/bin/python $LAB/tools/research.py'
alias sectors='$VENV/bin/python $LAB/tools/sectors.py'
alias dreport='$VENV/bin/python $LAB/tools/deep_report.py'
alias probe_sources='$VENV/bin/python $LAB/tools/probe_sources.py'
ALIASEOF
echo "  ✅ $LAB/aliases.sh"
echo "     source $LAB/aliases.sh        # 本次会话生效"
echo "     echo \"source $LAB/aliases.sh\" >> ~/.bashrc   # 每次都生效"

echo
echo "== 4. 自检 =="
bash "$LAB/tools/bootstrap_check.sh" || true
echo
echo "下一步:"
echo "  source $LAB/aliases.sh"
echo "  export SEC_UA='你的名字 你的邮箱'          # SEC 要求,不设会被限流"
echo "  astock 300308"
echo "  preport 300308:1100"
