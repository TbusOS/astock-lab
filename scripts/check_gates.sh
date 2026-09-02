#!/usr/bin/env bash
# check_gates —— 一条命令跑完本仓所有检查。**push 前跑这个。**
#
# 用法:bash scripts/check_gates.sh          退出码 0 才准 push
#
# 为什么要有总入口:单个检查再好,散着放就会漏跑。
# 长会话 / 隔几周回来,「记得跑哪几个」这件事本身就不可靠。
#
# 2026-09-02 实测过一次:`check_flow_contract.py` 不在这个列表里,
# 于是 8 条破坏测试里能被它抓到的那几条,**跑总检查一条都抓不到**。
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

echo "════ 提交前检查 ════"
echo
run "1 公开仓零个人信息"  bash "$LAB/scripts/check_public_safe.sh"
run "2 _lab_root 多份一致" "$PY" "$LAB/scripts/check_lab_root_sync.py"
run "3 判断条件线 == 基准率线" "$PY" "$LAB/tools/check_baserate_wiring.py"
run "3b 先判行业再跑策略" "$PY" "$LAB/scripts/check_sector_contract.py"
run "3c 深度报告层合约" "$PY" "$LAB/scripts/check_report_contract.py"
# 3d 判断流程的合约 —— **这一道以前不在总检查里**。它是唯一能抓住
# 「概率算错 / 决策链偷读机构预测 / 行业表漂了 / 报告里写死横向数」的检查,
# 而 2026-09-02 之前只有手打全路径才会跑到,等于默认不跑。
# `--raw` 默认是 data/raw:公开仓没有数据盘,那几条上盘的检查会自己说「跳过」。
# ⚠ `--raw` 默认按**当前目录**解析,所以这个脚本要在两个仓根各跑一次:
#   公开仓(没有 data/raw)→ 要读数据的那几项打 ⏭ 跳过并说明;
#   私有仓(有 data/raw)→ 同一批项目真的查一遍。
#   「目录不存在」= 跳过,「目录在但内容缺」= 红 —— 规则在 check_flow_contract 里。
run "3d 判断流程合约"     "$PY" "$LAB/tools/check_flow_contract.py" --raw "${ASTOCK_RAW:-data/raw}"
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
[ "$fail" -gt 0 ] && { echo "❌ $fail 道检查不过 —— 不要 push。"; exit 1; }
echo "✅ 全过。"
