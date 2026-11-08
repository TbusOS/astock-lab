#!/usr/bin/env bash
# install_daily_check —— 每个交易日收盘后,在这台 Mac 上自动跑一次 board.py check --notify。
#
# 它做什么:生成 launchd 定时任务 ~/Library/LaunchAgents/com.astock.board-check.plist,
#   周一到周五 15:30(本机时区)跑 board.py check --notify:刷新收盘价,查卖出线、AI 写的改主意条件、
#   到期的题,写 status.json、重画页面,碰到了就弹系统通知。节假日也会跑一次,只是没有新收盘,查不出新东西。
# 为什么用 launchd 不用 cron:合盖睡着错过的那一次,launchd 醒来后会补跑;cron 直接跳过。
#   关机错过的不补(下一个交易日照常跑)。
# 为什么默认只打印:装定时任务前先给人看一眼。加 --install 才写文件并 launchctl load。
#
# 用法:
#   export STOCK_LAB=<内部仓>                                   # 配置和存档在那里
#   bash skills/forecast-board/scripts/install_daily_check.sh            # 只打印 plist,不写任何文件
#   bash skills/forecast-board/scripts/install_daily_check.sh --install  # 写入并加载(已装过的先卸再装)
#   卸载:launchctl unload ~/Library/LaunchAgents/com.astock.board-check.plist && rm 那个文件
# python 用 $ASTOCK_VENV/bin/python,没设就用本仓 .venv(和 scripts/check_gates.sh 同一个约定)。
# 日志写到 ~/Library/Logs/astock-board-check.log。
set -euo pipefail

LABEL="com.astock.board-check"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PY="${ASTOCK_VENV:-$REPO/.venv}/bin/python"
BOARD="$REPO/tools/board.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/astock-board-check.log"
HOUR=15
MINUTE=30

INSTALL=0
for a in "$@"; do
  case "$a" in
    --install) INSTALL=1 ;;
    *) echo "不认识的参数 $a(只有 --install)" >&2; exit 2 ;;
  esac
done

: "${STOCK_LAB:?先 export STOCK_LAB=<内部仓>(配置和存档在那里;定时任务里要写死这个路径)}"
[ -d "$STOCK_LAB" ] || { echo "STOCK_LAB=$STOCK_LAB 不是目录" >&2; exit 1; }
[ -x "$PY" ] || { echo "找不到 $PY:设 ASTOCK_VENV=<装好依赖的 venv 目录>" >&2; exit 1; }
[ -e "$BOARD" ] || { echo "找不到 $BOARD" >&2; exit 1; }
[ -f "$STOCK_LAB/private/config/board.json" ] || echo "⚠ $STOCK_LAB/private/config/board.json 不在,check 会跑不起来" >&2

# 定时按本机时区算。要的是北京时间 15:30:时区缩写是 CST、而且比 UTC 早 8 小时
# (美国中部时间的缩写也是 CST,只看缩写分不出来)。
if [ "$(date +%Z)" != "CST" ] || [ "$(date +%z)" != "+0800" ]; then
  echo "⚠ 本机时区是 $(date +%Z)($(date +%z)),不是北京时间:定时按本机的 ${HOUR}:${MINUTE} 跑," \
       "要换算成北京时间 15:30 再改下面的 Hour / Minute" >&2
fi

# launchd 给的 PATH 只有系统目录;读借款看板要 node,把它所在的目录加进去
PATH_VAL="/usr/bin:/bin:/usr/sbin:/sbin"
if NODE="$(command -v node 2>/dev/null)"; then
  PATH_VAL="$(dirname "$NODE"):$PATH_VAL"
else
  echo "⚠ 找不到 node:重画页面时读不了借款看板(持仓会显示过期)" >&2
fi

xml() {  # 路径里的 & < > 转成 XML 实体
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$PY")</string>
    <string>$(xml "$BOARD")</string>
    <string>check</string>
    <string>--notify</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>STOCK_LAB</key><string>$(xml "$STOCK_LAB")</string>
    <key>PATH</key><string>$(xml "$PATH_VAL")</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
EOF
  for d in 1 2 3 4 5; do
    echo "    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>"
  done
  cat <<EOF
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
}

if [ "$INSTALL" = 0 ]; then
  plist
  echo "(只打印,没写文件。确认无误后加 --install 写到 $PLIST 并加载)" >&2
  exit 0
fi

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
TMP="$(mktemp "${TMPDIR:-/tmp}/board-check.XXXXXX")"
plist > "$TMP"
plutil -lint "$TMP" >/dev/null
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
fi
mv "$TMP" "$PLIST"
launchctl load "$PLIST"
echo "✅ 装好了:$PLIST(周一到周五 ${HOUR}:${MINUTE} 跑 board.py check --notify,日志 $LOG)"
