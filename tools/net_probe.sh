#!/usr/bin/env bash
# 当场自测:这台机器此刻能通哪些数据源/LLM 端点。
# 连通性随 Clash Verge 的模式/节点变化,不是固定的 —— 用前先跑这个看当前状态。
# 用法: bash net_probe.sh
set -u

echo "== 出口 IP =="
curl -s -m 10 https://ipinfo.io/json 2>/dev/null \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ",d.get("ip"),d.get("country"),d.get("org"))' 2>/dev/null \
  || echo "  (取不到,可能海外服务也被挡)"

echo "== 代理/TUN 进程 =="
p=$(ps -Ao comm 2>/dev/null | grep -iE 'clash|surge|mihomo|v2ray|xray|singbox' | sort -u | head -1)
[ -n "$p" ] && echo "  Clash/代理在跑: $p" || echo "  未见代理进程"

echo "== 端点连通(http 码;200/401/403=网络通,000=不通) =="
probe(){ printf "  %-20s %s\n" "$2" "$(curl -s -m 10 -o /dev/null -w '%{http_code}' ${3:+-H "$3"} "$1" 2>/dev/null)"; }
echo "  -- 国内(东财/DeepSeek 需国内直连;命令行只有 TUN 关的机器才走直连) --"
probe "https://push2.eastmoney.com/api/qt/stock/get?fields=f43&secid=0.300502" "东财 push2"
probe "https://hq.sinajs.cn/list=sz300502" "新浪 hq" "Referer: https://finance.sina.com.cn"
probe "https://qt.gtimg.cn/q=sz300502" "腾讯 gtimg"
probe "https://api.deepseek.com" "DeepSeek API"
echo "  -- 海外(Claude/OpenAI 需出海) --"
probe "https://www.google.com" "google"
probe "https://api.anthropic.com/v1/models" "Anthropic API"
probe "https://api.openai.com/v1/models" "OpenAI API"

cat <<'EOF'

判读:
  东财/新浪/腾讯=200 且 DeepSeek=401 → 国内直连正常 → astock/holdings_check/东财数据/DeepSeek 都能用
  Anthropic/OpenAI=401/403、google=200  → 出海正常 → 可用 Claude/OpenAI 跑 LLM 分析
  想两边都通:Clash 用「规则模式」,国内域名(eastmoney/sinajs/deepseek)走 DIRECT,海外走代理
EOF
