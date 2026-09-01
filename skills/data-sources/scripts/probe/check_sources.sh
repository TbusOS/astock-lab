#!/usr/bin/env bash
# astock 数据源连通性闸。
#
# 2026-08-27 实测修正:此前记的「东财整体被墙」是错的,东财要**按主机分开看**:
#   datacenter-web.eastmoney.com  报表类(龙虎榜/业绩/股东户数) —— 公司 Linux 稳定可用
#   push2 / push2his.eastmoney.com 行情推送类(快照/K线/分时) —— 能连但会被限流,
#                                   连续请求后从 200 掉到 502/连接失败
#   明文 http:// 到东财基本不通(502),https:// 才行
# 所以:龙虎榜这类东财独有数据在公司 Linux 直接取即可,不必绕家用 Mac;
#       行情/K线用新浪+腾讯(本来就更稳),不要拿东财当主源。
#
# 只对本次探测去代理,不动 shell 全局环境(与 astock.py 同一原则)。
# 退出码:0 = 新浪+腾讯都通(astock 可用);非 0 = 必需源不可达。
set -u

probe() {  # name url [referer] -> 打印 http 码,200 返回 0
  local name="$1" url="$2" code
  code=$(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
         curl -s -m 12 -o /dev/null -w "%{http_code}" \
         -A "Mozilla/5.0 (X11; Linux x86_64) Chrome/120" \
         -H "Referer: https://finance.sina.com.cn" "$url" 2>/dev/null)
  printf "  %-30s http=%s\n" "$name" "${code:-超时}"
  [ "$code" = "200" ]
}

echo "== 必需源:新浪 + 腾讯(去代理直连)=="
ok=0
probe "新浪快照 hq.sinajs"  "https://hq.sinajs.cn/list=sz300308"  || ok=1
probe "腾讯逐笔 sqt.gtimg"  "https://web.sqt.gtimg.cn/q=sz300308" || ok=1

echo "== 东财报表类(datacenter-web,预期可用)=="
if probe "龙虎榜 datacenter-web" \
   "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&pageSize=1&columns=ALL"; then
  echo "    → 可用:龙虎榜/业绩快报/股东户数等东财独有数据可直接在本机取"
else
  echo "    → 不可达:这类数据改到另一台网络不受限的机器 取"
fi

echo "== 东财行情类(push2his,限流敏感,失败属正常)=="
if probe "K线 push2his" \
   "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.300308&klt=101&fqt=1&beg=20260801&end=20500101&fields1=f1&fields2=f51,f53"; then
  echo "    → 这次通了,但连续请求会被限流;K线仍建议用新浪/腾讯"
else
  echo "    → 不通(限流或抖动),属预期;K线用新浪/腾讯,别重试硬刚"
fi

if [ "$ok" -eq 0 ]; then
  echo "结果:新浪+腾讯可达,astock 可用。"
else
  echo "结果:有必需源不可达,astock 会失败——检查网络/代理。"
fi
exit "$ok"
