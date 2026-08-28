#!/usr/bin/env bash
# 股票分析工作台自检 —— 换机器 / 换网络后先跑这条。
#
# 检查四件事,任何一件缺了就打印修复命令:
#   1. 工作台目录与工具在不在(含 baserate —— preport 第 8 层的硬依赖)
#   2. venv 与关键依赖在不在
#   3. 数据源可达性(必需源 / 领先指标源 / 已知不可用源分开报)
#   4. 领先指标能不能真取到数(不是只 ping 通,是真调一次)
#
# 退出码:0 = 能做完整分析;1 = 只能做部分;2 = 做不了
set -u

# 工作台根:$STOCK_LAB → 从本脚本位置往上找 .astock-lab-root → 老默认路径
_find_lab() {
  [ -n "${STOCK_LAB:-}" ] && [ -d "$STOCK_LAB" ] && { printf '%s' "$STOCK_LAB"; return; }
  d=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
  while [ "$d" != "/" ]; do
    [ -e "$d/.astock-lab-root" ] && { printf '%s' "$d"; return; }
    d=$(dirname "$d")
  done
  for c in "$HOME/astock-lab-private" "$HOME/astock-lab" \
           "$HOME/claude-tools/astock-lab-private" "$HOME/claude-tools/astock-lab" \
           "$HOME/claude-tools/stock-lab"; do
    [ -d "$c" ] && { printf '%s' "$c"; return; }
  done
  pwd
}
LAB="$(_find_lab)"
# venv:$ASTOCK_VENV → 仓内 .venv(install.sh 建的就是这个)
VENV="${ASTOCK_VENV:-$LAB/.venv}"
PY="$VENV/bin/python"
fail=0; warn=0

hr() { printf '%s\n' "──────────────────────────────────────────────────────────"; }
ok()   { printf "  [OK  ] %s\n" "$1"; }
bad()  { printf "  [缺  ] %s\n" "$1"; fail=$((fail+1)); }
soft() { printf "  [注意] %s\n" "$1"; warn=$((warn+1)); }

echo "== 1. 工作台与工具 =="
if [ -d "$LAB" ]; then ok "工作台 $LAB"; else
  bad "工作台不存在:$LAB"
  echo "        修复:git clone <stock-lab 私有仓> $LAB"
fi
for t in astock.py efdata.py holdings_check.py position_report.py capex.py \
         consensus.py baserate.py journal.py probe_sources.py \
         check_baserate_wiring.py check_sources.sh; do
  if [ -e "$LAB/tools/$t" ]; then ok "tools/$t"
  else bad "tools/$t 缺失或软链断了"; fi
done

# baserate 属于**另一个 skill**,preport 第 8 层靠 _load_baserate() 按候选路径找它。
# 软链在不代表 import 得到(比如 本仓 仓没 clone 全),所以真 import 一次。
if [ -x "$PY" ]; then
  if $PY -c "
import sys; sys.path.insert(0, '$LAB/tools')
import position_report as pr
sys.exit(0 if pr.br is not None else 1)" 2>/dev/null; then
    ok "preport 能找到 baserate(第 8 层基准率可用)"
  else
    soft "preport 找不到 baserate —— 第 8 层会降级,报告里的判据不带历史基准率"
    echo "        修复:确认 本仓/skills/stock-analysis-workflow/scripts/baserate.py 在"
  fi
fi

# 基准率缓存:冷缓存首跑要拉 12 期全市场业绩(约 30 秒),提前说一声免得以为卡死
_pc="$LAB/data/perf_cache"
if [ -d "$_pc" ] && [ "$(ls -1 "$_pc" 2>/dev/null | wc -l)" -ge 8 ]; then
  ok "基准率缓存 $(ls -1 "$_pc" | wc -l) 期($_pc)"
else
  soft "基准率缓存为空 —— preport 第一次跑第 8 层要拉全市场业绩,约 30 秒"
fi

hr
echo "== 2. venv 与依赖 =="
if [ -x "$PY" ]; then
  ok "venv $VENV ($($PY -V 2>&1))"
  for m in akshare baostock pandas requests; do
    if $PY -c "import $m" 2>/dev/null; then ok "  $m"; else bad "  缺 $m"; fi
  done
  # ⚠ edgartools 的 import 名是 edgar,不是 edgartools(实测踩过)
  if $PY -c "import edgar" 2>/dev/null; then ok "  edgartools(import edgar)"
  else soft "  缺 edgartools —— 只影响 capex --engine both 的交叉验证,不影响主流程"
       echo "        修复:$VENV/bin/pip install edgartools"; fi
  # ⚠ efinance 通常**不是 pip 装的**,是从 stock-lab/repos/efinance clone 加载的
  if $PY -c "import efinance" 2>/dev/null; then ok "  efinance(pip 装)"
  elif [ -d "$LAB/repos/efinance/efinance" ]; then
       if $PY -c "import sys;sys.path.insert(0,'$LAB/repos/efinance');import efinance" 2>/dev/null; then
         ok "  efinance(从 repos/efinance 加载,efdata.py 会自动回落到这条路径)"
       else
         bad "  repos/efinance 在但导入失败 —— 缺依赖"
         echo "        修复:$VENV/bin/pip install retry multitasking rich jsonpath"
       fi
  else
       bad "  efinance 既没 pip 装也没 clone —— efdata 与 preport 的筹码层会失效"
       echo "        修复:git clone https://github.com/Micro-sheep/efinance.git $LAB/repos/efinance"
       echo "              $VENV/bin/pip install retry multitasking rich jsonpath"
  fi
else
  bad "venv 不存在:$VENV"
  echo "        修复:uv venv --python 3.11 $VENV && \\"
  echo "              $VENV/bin/pip install akshare baostock efinance retry multitasking rich jsonpath edgartools"
fi

hr
echo "== 3. 数据源可达性 =="
probe() {  # name url [extra-header]
  local name="$1" url="$2" hdr="${3:-}" code
  if [ -n "$hdr" ]; then
    code=$(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
           curl -s -m 12 -o /dev/null -w "%{http_code}" -A "$hdr" "$url" 2>/dev/null)
  else
    code=$(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
           curl -s -m 12 -o /dev/null -w "%{http_code}" \
           -A "Mozilla/5.0 (X11; Linux x86_64) Chrome/120" \
           -H "Referer: https://finance.sina.com.cn" "$url" 2>/dev/null)
  fi
  printf "  %-34s http=%s\n" "$name" "${code:-超时}"
  [ "$code" = "200" ]
}

echo "-- 必需源(缺了做不了基础分析)--"
probe "新浪行情 hq.sinajs"  "https://hq.sinajs.cn/list=sz300502" || bad "新浪不可达"
probe "腾讯 sqt.gtimg"      "https://web.sqt.gtimg.cn/q=sz300502" || soft "腾讯不可达(逐笔受影响)"

echo "-- 领先指标源(缺了只能看后视镜)--"
SECUA="${SEC_UA:-astock-lab-user your-email@example.com}"
probe "SEC EDGAR(领先指标)" \
  "https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/PaymentsToAcquirePropertyPlantAndEquipment.json" \
  "$SECUA" || { bad "SEC EDGAR 不可达 —— Capex 这条领先指标就断了"
                echo "        403 多半是 UA 不合规:export SEC_UA='你的名字 你的邮箱'"; }

echo "-- 东财报表类(龙虎榜/股东户数/业绩)--"
probe "东财 datacenter-web" \
  "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&pageSize=1&columns=ALL" \
  || soft "东财报表类不可达(龙虎榜/股东户数取不到)"

echo "-- 已知不可用(失败属预期,不计入失败数)--"
probe "东财 push2his(预期失败)" \
  "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.300308&klt=101&fqt=1&beg=20260801&end=20500101&fields1=f1&fields2=f51" \
  && soft "push2his 这次通了 —— 比预期好,但别当主源"

hr
echo "== 4. 领先指标实取(真调一次,不是只 ping)=="
if [ -x "$PY" ] && [ -e "$LAB/tools/capex.py" ]; then
  if out=$(timeout 120 "$PY" "$LAB/tools/capex.py" --tickers MSFT --quarters 2 2>&1); then
    if echo "$out" | grep -q "同比"; then
      line=$(echo "$out" | grep -E '^\| 20' | tail -1)
      ok "capex 取数成功:$line"
    else
      bad "capex 跑了但没出数据"
    fi
  else
    bad "capex 执行失败"
  fi
else
  soft "跳过(venv 或 capex.py 不在)"
fi

hr
if [ "$fail" -eq 0 ] && [ "$warn" -eq 0 ]; then
  echo "结果:全部就绪,可以做完整分析(领先+滞后)。"
  echo "下一步:capex --md c.md && preport <代码>:<成本> --md p.md"
  exit 0
elif [ "$fail" -eq 0 ]; then
  echo "结果:核心可用($warn 项提示),可以分析。"
  exit 0
elif [ "$fail" -le 3 ]; then
  echo "结果:有 $fail 项缺失,只能做部分分析 —— 按上面的修复命令补。"
  exit 1
else
  echo "结果:$fail 项缺失,当前做不了分析。先按上面的修复命令重建环境。"
  exit 2
fi
