#!/usr/bin/env bash
# desensitize:skip —— ⚠ 本文件**不能**被 scripts/desensitize.py 改写:
#   下面的正则里本来就写着要查的那些词,被替换掉之后闸会查一个不存在的模式,
#   于是永远全绿,而你以为它在把关。2026-08-28 真发生过一次。
#
# check_public_safe —— 公开仓零个人信息闸。**push 前必须跑,退出码 0 才准推。**
#
# 用法:bash scripts/check_public_safe.sh [目录]   (默认:仓根)
#
# 为什么必须是脚本不是「发布前记得看一眼」:
#   ① 个人信息一旦推上公开仓,即使随后 force push 删掉,**GitHub 的
#      事件流、fork、爬虫快照、搜索索引都已经拿到了** —— 撤不回来。
#      这是不可逆操作,不可逆操作只能靠机器把关。
#   ② 「我记得脱敏过了」在长会话里不可靠。每次同步、每次新增文档都要重跑。
#
# 查的是**个人痕迹**,不是敏感词:真实成交价、家目录用户名、主机名、
# 私有仓名、个人设备、真实持仓记录。股票代码是公开信息,不查。
set -u
ROOT="${1:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
cd "$ROOT" || exit 2

fail=0
hit() { printf "  ❌ %-26s %s\n" "$1" "$2"; fail=$((fail+1)); }
ok()  { printf "  ✅ %s\n" "$1"; }

# 只查会进 git 的文件;.git 与 data/ 不查
scan() {  # scan <名称> <grep 正则> [额外排除正则]
  local name="$1" pat="$2" excl="${3:-}"
  local out
  out=$(grep -rainE "$pat" . \
        --exclude-dir=.git --exclude-dir=data --exclude-dir=repos \
        --exclude-dir=__pycache__ --exclude-dir=.venv \
        --exclude=desensitize.py --exclude=check_public_safe.sh \
        --exclude='personal.patterns*' --exclude='personal.rules*' 2>/dev/null)
  [ -n "$excl" ] && out=$(printf '%s' "$out" | grep -vE "$excl")
  out=$(printf '%s' "$out" | grep -v '^$')
  if [ -n "$out" ]; then
    # ⚠ printf '%s' 不带换行,单行结果 wc -l 会算成 0 —— 用 grep -c 数行
    hit "$name" "$(printf '%s\n' "$out" | grep -c .) 处"
    printf '%s\n' "$out" | head -5 | sed 's/^/       /'
  else
    ok "$name"
  fi
}

echo "== 公开仓个人信息闸 =="
echo "   目标:$ROOT"
echo

# 1 真实成交价:精确到小数点后三位的价格,几乎一定是真实持仓成本
scan "无真实成交价(x.xxx)" '[0-9]{2,5}\.[0-9]{3}([^0-9]|$)' '[0-9]{4}-[0-9]{2}-[0-9]{2}|版本|version|v[0-9]+\.[0-9]+\.[0-9]{3}'

# 2 家目录与用户名
scan "无绝对家目录路径"   '/home/[a-z][a-z0-9_-]+|/Users/[a-z][a-z0-9_-]+'
scan "无主机名/用户名"     '@[a-z0-9-]+\.local'

# 3 私有仓与内部资产
# ⚠ 不要写成 `-private\b`:_lab_root() 的候选路径里有 astock-lab-private,
#    那是**给使用者自己的私有副本留的路径约定**,不是引用作者的私有仓。
#    只查真实存在的私有仓名与内部目录。
scan "无私有仓引用"        'claude-tools/internal|/internal/'
scan "无脱敏破句(泛)"      '本仓 [一二三四两0-9]|在 本仓|本仓 仓|本仓-'

# 4 个人设备与环境
scan "无个人设备"          'mac-?mini|我的电脑|我这台|家里那台'
scan "无个人 venv 命名"    'akshare-venv'

# 5 真实决策记录(journal 的实际内容不该进公开仓)
scan "无真实决策记录"      '"stock": *"[0-9]{6}".*"price"|principles\.jsonl.*[0-9]{6}'

# 6 拆仓遗留:旧仓名与脱敏留下的破句
#    2026-08-28 实测漏了 9 处 —— 脱敏是正则替换,替换完不看上下文就会留下
#    「真身在 本仓 两个 skill 里」这种读不通的句子,以及没被规则覆盖的旧仓名。
# ⚠ 要词边界:公开仓自己就叫 a-stock-lab,裸写 stock-lab 会把 astock-lab/tools 也命中
scan "无旧仓名残留"        '(^|[^a-z-])stock-lab/(tools|docs|repos)|claude-tools/stock-lab'

# 7 凭证类(不该有,但零成本再查一遍)
scan "无凭证"              'ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY|password *= *["'"'"'][^"'"'"']'

# ── 你自己的标识清单(不进仓)──────────────────────────────────────────────
# ★ 为什么单独放一个文件而不是写在本脚本里:
#   **这份清单本身就是敏感的。**「要查 <某登录名>、<某私有仓>、<某项目代号>」这行字
#   一旦发布,等于给任何人一份「该找什么」的索引 —— 比它防住的单点泄露更糟。
#   2026-08-28 实测:公开仓的这个脚本当时就写着作者的用户名、5 个私有仓名、
#   7 个内部项目代号,而闸把自己排除在扫描外,所以一直没报。
#
# 格式:每行 `标签<TAB>正则`,# 开头是注释。见 personal.patterns.example。
PERSONAL="$(dirname "$(readlink -f "$0")")/personal.patterns"
if [ -f "$PERSONAL" ]; then
  echo "-- 你自己的标识清单($(grep -cvE '^\s*(#|$)' "$PERSONAL") 条)--"
  while IFS=$'\t' read -r plabel ppat; do
    case "$plabel" in ''|\#*) continue ;; esac
    [ -z "$ppat" ] && continue
    scan "$plabel" "$ppat"
  done < "$PERSONAL"
else
  printf "  ⏭  没有 scripts/personal.patterns —— 只跑了通用检查\n"
  printf "     你自己的用户名、主机名、私有仓名、内部代号要单独列进那个文件\n"
  printf "     (它被 .gitignore 掉,不会流向公开仓)。抄 personal.patterns.example\n"
fi

echo
if [ "$fail" -gt 0 ]; then
  echo "❌ $fail 项命中 —— **不要 push**。"
  echo "   先跑 scripts/desensitize.py <路径> 处理批量替换，"
  echo "   剩下的手改，然后重跑本闸直到全绿。"
  exit 1
fi
echo "✅ 全绿 —— 公开仓无个人信息，可以 push。"
