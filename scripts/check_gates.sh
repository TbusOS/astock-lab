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
# 3e~3g 是 2026-10-29 第一次正式复核前加的:journal 缺概率不拦、probs=None 崩、
# 动作做没做看不出;preport 三季报后动态 PE 低估三分之一、两只一起跑小节串位;
# 大盘层(market)的计算。三道都不联网,每道都拿旧版反向跑过 —— 旧版必须不过。
run "3e journal 行为"     "$PY" "$LAB/scripts/check_journal.py"
run "3f preport 不联网部分" "$PY" "$LAB/scripts/check_preport.py"
run "3g 大盘层计算"       "$PY" "$LAB/tools/market.py" --self-test
# 3h(2026-10-30):复核季度预测时只看「落在区间里」会两头看错 —— 第一次核三季度,
# 5 只按区间只中 2 只,可 5 只偏中枢都不超过历史中位 12.7%。
run "3h 季度预测评分"     "$PY" "$LAB/scripts/check_review_scoring.py"
# 3i(2026-10-30):东财研报接口的「本年」EPS 不是发布年,年份要从 PDF 表头读,
# 读错会把「换了年份」算成「上调 40%」。
# (2026-11-02)上下调改比归母净利润:送转股后每股收益变小，看着像调低 28.8%,净利润其实没改。
run "3i 研报盈利预测(年份、净利润)" "$PY" "$LAB/scripts/check_eps_revisions.py"
# 3j~3y(2026-11-03):预测看板(skills/forecast-board)和它用到的取数改动。
# 都不联网、不碰真实数据,在临时目录里搭工作台,用合成行情、合成答卷。
# 3n~3p 管的是看板依赖的三处取数:腾讯日线缺整年、行业表的领先指标有没有取数器、
# 云厂资本开支加上融资租赁 —— 这三处错了,看板照样出页面,只是数不对。
run "3j 看板:统计底线、交易日、历史同类情形" "$PY" "$LAB/scripts/check_board_engine.py"
run "3k 看板:输入包和事件"   "$PY" "$LAB/scripts/check_board_store.py"
run "3l 看板:账户和动作"     "$PY" "$LAB/scripts/check_board_account.py"
run "3m 看板:页面和图"       "$PY" "$LAB/scripts/check_board_pages.py"
run "3n 日线不缺整年"         "$PY" "$LAB/scripts/check_market_fetch.py"
run "3o 领先指标都有取数器或登记成缺口" "$PY" "$LAB/scripts/check_inputs_contract.py"
run "3p 云厂资本开支(现金 + 融资租赁)" "$PY" "$LAB/scripts/check_capex_definition.py"
run "3q 看板:board.py 各子命令" "$PY" "$LAB/scripts/check_board_cli.py"
run "3r 看板:出题和结算"     "$PY" "$LAB/scripts/check_board_questions.py"
run "3s 看板:原因概率和重跑" "$PY" "$LAB/scripts/check_board_review.py"
run "3t 看板:评分和成绩页"   "$PY" "$LAB/scripts/check_board_scoring.py"
run "3u 看板:结算记录、补记收盘价、错题证据" "$PY" "$LAB/scripts/check_board_settle.py"
# 3v、3w 是从 3j、3k 拆出来的(文件超过 800 行),用例没变;3x、3y 是合并前最后一轮修的那批。
run "3v 看板:系统算法"         "$PY" "$LAB/scripts/check_board_system.py"
run "3w 看板:两个 AI 作答"     "$PY" "$LAB/scripts/check_board_ai.py"
run "3x 看板:没取到、没查成、过期的写到页面上" "$PY" "$LAB/scripts/check_board_notices.py"
run "3y 看板:页面上的字和数从哪来" "$PY" "$LAB/scripts/check_board_page_text.py"
run "3z 看板:历史补考(不偷看、带子覆盖率)" "$PY" "$LAB/scripts/check_board_history.py"
run "3z2 看板:按规则买卖补考(不偷看、已知答案、成本)" "$PY" "$LAB/scripts/check_board_rules.py"
run "3z3 历史表:估值(公告后才用、分位只看以前)" "$PY" "$LAB/scripts/check_hist_valuation.py"
run "3z4 历史表:海外同行(美股收盘次日才用)" "$PY" "$LAB/scripts/check_hist_overseas.py"
run "3z5 历史表:云厂开支(按 SEC 提交日)" "$PY" "$LAB/scripts/check_hist_capex.py"
run "3z6 历史表:A 股每季业绩(按首次公告日)" "$PY" "$LAB/scripts/check_hist_earnings.py"
run "3z7 历史表:券商上调下调(按研报发布日)" "$PY" "$LAB/scripts/check_hist_consensus.py"
run "3z8 逐条考想法的打分(含错开月份检验)" "$PY" "$LAB/scripts/check_board_thinking.py"
run "3z9 看板第一屏(怎么看 / 价格可能晃多大 / 对照)" "$PY" "$LAB/scripts/check_board_judge.py"
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
  run "7 skill 几份副本一致"  bash "$LAB/scripts/check_skill_sync.sh"
else
  echo "── 7 skill 几份副本一致 ──"
  echo "   ⏭  跳过(没设 SKILL_MIRROR;只在你另存了一份副本时才需要)"
  echo
fi

echo "════════════════"
[ "$fail" -gt 0 ] && { echo "❌ $fail 道检查不过 —— 不要 push。"; exit 1; }
echo "✅ 全过。"
