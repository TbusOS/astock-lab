---
name: astock-quote
description: >
  在本机(受限网络 + 全局代理)查 A 股行情的工具与踩坑记录。核心事实=**行情用新浪
  hq.sinajs.cn + 腾讯逐笔直连;东财要按主机分开看**——datacenter-web(龙虎榜/业绩
  等报表)本机稳定可用,push2/push2his(快照/K线)会被限流,不适合当行情主源
  (2026-08-27 实测修正了此前「东财整体被墙」的错误结论)。而 Claude Code 和其它
  命令又必须走代理,所以**只能对查行情这一个进程去代理**(os.environ.pop),不能动
  shell 全局。工具 scripts/astock.py 支持快照/五档/逐笔/日线,代码前缀自动判
  (6→sh 0/3→sz 4/8→bj)。用前先跑 scripts/check_sources.sh 确认源可达。
  触发词:A股 / 股价 / 行情 / 股票代码 / akshare / 五档 / 盘口 / 逐笔 /
  中际旭创 / 300308 / stock quote / 东财限流 / push2.eastmoney / 龙虎榜 /
  datacenter-web / 查股票 / astock。
---

# astock-quote — A 股行情速查(本机可用)

## 一句话

本机查 A 股行情用 `scripts/astock.py`(新浪/腾讯源),**行情别拿东财当主源**——
push2/push2his 会被限流。但东财报表类(龙虎榜等)本机是通的,该用就用。
查行情要**单独去代理**,但别动全局代理(Claude Code 还得用)。

## 为什么会有这个 skill(核心踩坑,别再犯)

1. **东财行情类接口会被限流**(2026-08-27 用 efinance 实测,修正了 08-26
   「东财整体被墙」的结论)。分主机看:
   - `push2.eastmoney.com` / `push2his.eastmoney.com`(快照 / K线 / 分时):
     **初测 10/10 通,连续请求后掉到 0/5**,表现为 `ConnectionError` / 502 /
     `http=000`。所以 akshare 的 `_em` 系列(`stock_zh_a_spot_em` /
     `stock_bid_ask_em` / `stock_zh_a_hist`)在本机**不可靠**,不是代码 bug,
     重试硬刚只会更糟。
   - `datacenter-web.eastmoney.com`(龙虎榜 / 业绩快报 / 股东户数等报表):
     **稳定可用**,大量请求后仍 5/5。东财独有的这类数据**本机直接取,不用绕
     家用 Mac**。
   - 明文 `http://` 到东财基本不通(502),`https://` 才行。
2. **能通的是新浪 + 腾讯**。快照/五档走新浪 `hq.sinajs.cn`,逐笔走腾讯
   (akshare `stock_zh_a_tick_tx_js`),日线走新浪(`stock_zh_a_daily`)。都要
   **去代理**才通(公司代理会把这些国内站也搅坏)。
3. **去代理只能作用于查行情这一个进程**。用户明确要求:Claude Code 本身、其它
   命令仍要走代理。所以脚本用 `os.environ.pop(...)` 只清自己进程的代理变量,
   **绝不 `unset`/改 shell 全局或 `~/.bashrc` 里的代理**。

## 环境

- akshare 装在独立 venv:`$VENV`(基于 uv 的 python3.11;系统 python3
  是 3.8 太旧且 `/usr` 不可写,不用)。
- 权威脚本就是本 skill 的 `scripts/`;`$LAB/tools/` 下的
  `astock.py` / `efdata.py` / `holdings_check.py` / `check_sources.sh` 都是指向它的软链,
  `alias astock` / `alias efdata` / `alias hcheck` 指到那些软链。改脚本走本 skill 真实路径。
- A 股工作台总入口:`$LAB/`(工具 / 文档 / 9 个开源仓 / 会话归档)。

## 三个脚本的分工(别拿错工具)

| 要什么 | 用哪个 | 源 |
|---|---|---|
| 实时行情 / 五档 / 逐笔 / 日线 | `astock` | 新浪 + 腾讯,本机稳定 |
| 龙虎榜 / 股东户数 / 全市场业绩 / 可转债 / 基金 | `efdata` | 东财(报表类主机),本机稳定 |
| 持仓业绩体检 + PE/PB 分位 | `hcheck` | baostock,不依赖东财 |
| **一份完整的持仓决策报告** | **`preport`** | 上面全部 + 解禁 / 融资融券 / 海外同业 |
| **需求源头:北美云厂 Capex** | **`capex`** | SEC EDGAR XBRL(官方免费,需合规 UA) |
| **外部视角:这种增速能撑多久** | **`baserate`** | 东财全市场季度业绩(在 skill `stock-analysis-workflow`) |

`preport <代码>:<成本>` 跑八层:持仓状况 / 基本面(含环比 ROE) / 估值(含动态 PE 与 PEG) /
筹码杠杆(股东户数变化 + 融资余额 + 十大股东) / 解禁 / 技术位(均线 + 成本历史分位) /
**海外同业对照** / **基准率校准**。`--md 文件` 存 markdown,季报后跑一次即可。

第七层是它存在的主要理由:A 股光模块 / 半导体的需求端在海外,只看 A 股会漏掉
「海外同行在涨而 A 股在跌」这类背离(2026-08-27 实测 300502 与海外同业背离 33 个百分点)。

第八层解决另一个问题:**前七层全是这一家的数据 = 内部视角**,人在内部视角下会
系统性高估「这种好状态还能持续多久」。第八层调 `baserate`(在 skill
`stock-analysis-workflow` 里)拿全市场历史,给第九层每条判据配一个基准率 ——
「净利同比保持 >50%(当前 +91%)　—— 同类历史基准率 **24%**」。
判据线和基准率线**必须是同一个数**,这条有可执行闸 `check_baserate_wiring.py`。
`--no-baserate` 跳过(热缓存约 2 秒,冷缓存约 30 秒)。

`efdata` 是 efinance 的封装,**18 个稳定子命令 + 6 个本机取不到的**(失败会打印替代方案,
退出码 2)。`efdata --list` 看全部,`efdata --check` 探测当前可用性。
每个子命令拿到什么(含真实返回列)见 `stock-lab/docs/04-efdata数据全集.md`。
**别拿 efdata 当行情工具** —— 东财的行情类接口在本机取不到,那是 astock 的活。

## 用法

```bash
astock 300308            # 行情快照(alias 已在 ~/.bashrc)
astock 300308 --l5       # 附五档盘口
astock 300308 --tick     # 附逐笔成交(默认 20 笔)
astock 300308 --tick=40  # 逐笔看 40 笔
astock 300308 --daily    # 附最近日线
astock 300308 600519 000001   # 多只(单只失败不影响其余)

# 不用 alias 时的完整写法:
$VENV/bin/python <此skill>/scripts/astock.py 300308 --l5
```

代码前缀自动判:`6`→sh(主板/科创)、`0/3`→sz(深主板/创业)、`4/8`→bj(北交所)。

### 持仓业绩体检 holdings_check.py

长线持有景气成长股(光模块等)时,判去留要靠**业绩验证**而非成本价。每季度财报后跑一次:

```bash
alias hcheck='$VENV/bin/python <此skill>/scripts/holdings_check.py'
hcheck 300502:400.00 300308:1100.00   # 代码:成本(成本可省)
```

拉最新季报和去年同期比,输出:营收/净利同比、毛利率变化、PE(TTM)、浮亏,并给判断:
- 净利同比 >50% = 拿住 · 0~50% = 减速警惕 · <0 = 业绩破位(卖出信号)
- 毛利率同比 持平/升 = OK · 下滑 >3pp = 警惕

数据源:财务=新浪/雪球(`stock_financial_abstract`);PE/市值=腾讯;价=新浪。**消息面(订单/1.6T放量/研报目标价/解禁)脚本不含联网,让 Claude 用 WebSearch 补。**

## 用前先跑这道闸

```bash
bash <此skill>/scripts/check_sources.sh   # 退出码 0 = 新浪+腾讯可达,astock 可用
```

换网络/换机器后如果这道闸报东财反而通了,可考虑切回东财源(字段更全)。

## 数据源对照

| 内容 | 源 | akshare / 接口 |
|---|---|---|
| 快照 + 五档 | 新浪 `hq.sinajs.cn` | 直接 requests(脚本自解析) |
| 逐笔成交 | 腾讯 | `stock_zh_a_tick_tx_js` |
| 日线 | 新浪 | `stock_zh_a_daily` |
| ~~实时全量/买卖盘~~ | ~~东财~~ | ~~`_em` 系列~~ **本机不可用** |

## 已知限制

- 新浪免费源只给 5 档;10 档(买十/卖十)需 Level-2 付费行情。
- 快照有延迟,非 tick 级;做研究/回顾够,做严肃量化不够。
- 盘后拿到的五档是残单,盘中(9:30–15:00)才实时跳动。
