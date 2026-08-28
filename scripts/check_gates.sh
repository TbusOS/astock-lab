#!/usr/bin/env bash
# check_gates —— 一条命令跑完本仓所有闸。**push 前跑这个。**
#
# 用法:bash scripts/check_gates.sh          退出码 0 才准 push
#
# 为什么要有总闸:单个闸再好,散着放就会漏跑。
# 长会话 / 隔几周回来,记得跑哪几个闸这件事本身就不可靠。
set -u
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PY="${ASTOCK_VENV:-$LAB/.venv}/bin/python"
[ -x "$PY" ] || PY=python3

fail=0
run() {  # run <名称> <命令...>
  local name="$1"; shift
  echo "── $name ──"
  if "$@"; then echo "   ✅ 过"; else echo "   ❌ 不过"; fail=$((fail+1)); fi
  echo
}

echo "════ 提交前闸 ════"
echo
run "1 公开仓零个人信息"  bash "$LAB/scripts/check_public_safe.sh"
run "2 _lab_root 多份一致" "$PY" "$LAB/scripts/check_lab_root_sync.py"
run "3 判据线 == 基准率线" "$PY" "$LAB/tools/check_baserate_wiring.py"
run "3b 先判赛道再跑策略" "$PY" "$LAB/scripts/check_sector_contract.py"
run "4 软链无断链"        bash -c '
  b=0
  for l in '"$LAB"'/tools/*; do [ -e "$l" ] || { echo "   断链 $l"; b=1; }; done
  exit $b'
run "5 py 语法"           bash -c '
  b=0
  for f in $(find '"$LAB"'/skills -name "*.py" ! -path "*__pycache__*"); do
    python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" || { echo "   $f"; b=1; }
  done
  exit $b'
run "6 sh 语法"           bash -c '
  b=0
  for f in $(find '"$LAB"' -name "*.sh" ! -path "*/.git/*" ! -path "*/repos/*"); do
    bash -n "$f" || { echo "   $f"; b=1; }
  done
  exit $b'

if [ -n "${SKILL_MIRROR:-}" ]; then
  run "7 skill 副本对账"  bash "$LAB/scripts/check_skill_sync.sh"
else
  echo "── 7 skill 副本对账 ──"
  echo "   ⏭  跳过(没设 SKILL_MIRROR;只在你另存了一份副本时才需要)"
  echo
fi

echo "════════════════"
[ "$fail" -gt 0 ] && { echo "❌ $fail 道闸不过 —— 不要 push。"; exit 1; }
echo "✅ 全过。"
