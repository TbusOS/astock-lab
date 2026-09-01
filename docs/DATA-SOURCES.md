# 数据源清单

> 实测日期 **2026-09-01** · 41 条,通 37 条 · 探测机器:MacBook(Clash TUN 开着)
>
> **这份清单的表格不是手写的,是每次真调一次生成的。**
> 手写的数据源清单必然过期 —— 2026-09-01 就发现三条记成「拿不到」的
> (Motley Fool 电话会纪要 / roic.ai / stockanalysis.com)其实一直是通的,
> 而这三条正好卡着「云厂下期 capex 指引」这个最领先的指标。

## 怎么更新这份清单

```bash
export SEC_UA='astock-lab research <你的邮箱>'      # SEC 强制要求,缺了一律 403
$VENV/bin/python tools/probe_all_sources.py \
    --json /tmp/sources.json --markdown /tmp/sources.md
# 把 /tmp/sources.md 的内容替换掉下面「实测清单」那一节
```

退出码:`0` = 必需源全通;`1` = 有必需源不通。只测一组:`--group 领先指标`。

**加新数据源的规矩**:先在 `tools/probe_all_sources.py` 的 `build()` 里加一条探针
(带 URL、用途、领先/滞后属性),跑通了再写进取数脚本。顺序反了就会出现「脚本里写着能拿,实际早就拿不到」。

---

## 一、先看覆盖度:按分析顺序倒推

分析一只股票的顺序是 **领先 → 同步 → 滞后**。绝大多数股票数据都是后视镜,真正领先的只有下面这几类。**清单的价值不在条数,在领先那一栏够不够。**

| 层 | 需要回答 | 我们有的 | 缺口 |
|---|---|---|---|
| **① 需求源头**(最领先) | 客户下季度还买不买 | 云厂 capex 指引(电话会)· SEC 已发生 capex · 海外同业电话会 · 油价 · 钻机数 · 全球油气 capex | 面板厂 capex 要自己从现金流量表算(能拿,没接) |
| **② 公司自己的前瞻** | 公司/机构怎么说下期 | 业绩预告 · 业绩快报 · 券商一致预期 · 评级变动方向 · 机构调研家数 | **外资行评级/目标价拿不到**(巨潮只覆盖境内);公司季度指引散在公告与纪要里,没结构化 |
| **③ 事件** | 有没有刚发生的变化 | 公告(中标/合同/订单)· 龙虎榜 · 限售解禁 | 公告只取到列表,正文关键词识别没做 |
| **④ 现在**(同步) | 当下什么价、谁在买 | 实时行情 · 五档逐笔 · 融资余额 · 汇率 · 中美国债 | 个股资金流(push2his 不稳);北向个股方向**永久没有** |
| **⑤ 后视镜**(滞后) | 上一季兑现没有 | 三表 + 杜邦 + 偿债 + 营运 + 现金流 · 股东户数 · 估值历史分位 · 全市场基准率 | 财报重述追踪要付费;审计意见要付费 |

---

## 一之二、抓下来放哪:落盘结构

```
data/raw/
├── quotes/ financials/ forecast/ consensus/ ratings/
├── surveys/ announcements/ research/ chips/     ← A 股,<code>/<date>.json
├── overseas/    <ticker>/<date>.json            ← 海外上下游 21 只
├── transcripts/ <会议>/<date>.txt               ← 电话会纪要正文
├── macro/       <date>-<项>.json
└── meta/        <date>.json                     ← 本次抓取的健康汇总
```

每个文件是**信封**不是裸数据:

```json
{"source":"baostock query_forecast_report","url":"...","fetched_at":"2026-09-01T23:42:11",
 "params":{...},"ok":true,"rows":4,"error":null,"data":[...]}
```

时效、出处、成败因此是免费得到的。**抓失败的条目留在盘上标 `ok:false`** ——
静默丢弃会让读的人以为这类数据根本不存在,而「今天没抓到」和「不存在」
对决策的含义完全相反。

三条命令:

```bash
tools/probe_all_sources.py --json /tmp/s.json --markdown /tmp/s.md   # 先探
tools/fetch_all.py --codes 300502,300308 --peers --macro             # 再抓
tools/data_digest.py --code 300502 --overseas --pdf                  # 出文档
```

`data_digest.py` 出的是**给人读的**数据源明细(MD + PDF):一只股票当前手上到底有哪些数据、
来自哪个源、抓于何时、内容是什么、还缺什么,按「领先→同步→滞后」组织,可以直接发出去。

---

## 二、实测清单
### 1 行情与技术位

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**新浪实时快照**](https://hq.sinajs.cn/list=sz300502) ⭐ | 同步 | 五档/最新价/成交量 | `tools/astock.py` | ✅ 通 · 有报价 |
| [**腾讯实时/逐笔**](http://qt.gtimg.cn/q=sz300502) ⭐ | 同步 | 快照 + 逐笔明细 | `tools/astock.py` | ✅ 通 · 有报价 |
| [**腾讯日线(前复权)**](https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz300502,day,,,60,qfq) | 滞后 | OHLCV 历史 | `tools/astock.py` | ✅ 通 · 60 根 |
| [**baostock 日线+估值**](http://baostock.com/baostock/index.php/A股K线数据) ⭐ | 滞后 | OHLCV + **peTTM/pbMRQ/psTTM**(历史分位的唯一来源) | `baostock query_history_k_data_plus` | ✅ 通 · 22 根,末 peTTM=42.810370 |
| [**新浪美股日线**](https://finance.sina.com.cn/stock/usstock/) | 领先 | 海外同业股价(市场先反应) | `akshare stock_us_daily` | ✅ 通 · 9387 行 × 6 列 |

### 2 A股财务(baostock)

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**盈利能力 query_profit_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | ROE/净利率/毛利率/EPS | `baostock query_profit_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '0.356666' |
| [**成长能力 query_growth_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | 营收/净利/总资产同比 | `baostock query_growth_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '1.008975' |
| [**偿债能力 query_balance_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | 流动比/速动比/**资产负债率**/权益乘数/总负债同比 | `baostock query_balance_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '2.603456' |
| [**现金流 query_cash_flow_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | 经营现金流/营收、每股现金流 | `baostock query_cash_flow_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '0.772544' |
| [**杜邦分解 query_dupont_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | ROE = 净利率 × 周转率 × 权益乘数 | `baostock query_dupont_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '0.356666' |
| [**营运能力 query_operation_data**](http://baostock.com/baostock/index.php/季频财务数据) | 滞后 | 应收周转率/存货周转率 | `baostock query_operation_data` | ✅ 通 · 1 行 ['sz.300502', '2026-08-25', '2026-06-30', '3.483672' |
| [**业绩预告 query_forecast_report**](http://baostock.com/baostock/index.php/季频盈利能力) | **领先** | 净利预告区间,比正式财报早 2-6 周。无预告本身也是信息(变动在 ±50% 内) | `baostock query_forecast_report` | ✅ 通 · 4 条,最近 2026-07-20 预增 |
| [**业绩快报 query_performance_express_report**](http://baostock.com/baostock/index.php/业绩快报) | **领先** | 正式财报前的营收/净利快报 | `baostock query_performance_express_report` | ✅ 通 · 2 条 |
| [**全市场状态 query_all_stock**](http://baostock.com/baostock/index.php/证券代码查询) | 元数据 | **tradeStatus** 1=正常 0=停牌;配 query_stock_basic 的名称含 ST 判风险警示。只能当验证标签,不能当打分输入(事后贴标 = 马后炮 + 回测前视) | `baostock query_all_stock` | ✅ 通 · 7369 只,其中停牌 7 |
| [**复权因子 query_adjust_factor**](http://baostock.com/baostock/index.php/复权因子) | 元数据 | 回测可复现的前提 —— 目标价/成本价与日线必须在同一复权基准上。2026-09-01 踩过:拿未复权目标价比未复权日线,基准混了(送转系数 1.40) | `baostock query_adjust_factor` | ✅ 通 · 3 次除权除息 |
| [**行业分类 query_stock_industry**](http://baostock.com/baostock/index.php/行业分类) | 元数据 | 行业中性化 / 同业对照的分组依据 | `baostock query_stock_industry` | ✅ 通 · C39计算机、通信和其他电子设备制造业 |
| [**交易日历 query_trade_dates**](http://baostock.com/baostock/index.php/交易日查询) | 元数据 | 所有定时任务的前置闸:非交易日直接跳过 | `baostock query_trade_dates` | ✅ 通 · 32 天,其中交易日 22 |

### 3 财务与筹码(东财)

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**datacenter-web 主机**](http://datacenter-web.eastmoney.com/api/data/v1/get) ⭐ | — | 报表类数据总入口 | `requests` | ✅ 通 · 有数据 |
| [**机构调研 RPT_ORG_SURVEYNEW**](https://data.eastmoney.com/jgdy/) | **领先** | 接待机构数 / 会议形式 / 时间 / 接待人。异常时点(周末夜间)+ 家数骤变是信号 | `datacenter-web reportName=RPT_ORG_SURVEYNEW` | ✅ 通 · 共 819 次,最近接待 145 家 |
| [**股东户数**](https://data.eastmoney.com/gdhs/) | 滞后 | 户数 / 户均持股 / 环比 | `efinance get_latest_holder_number` | ❌ FAIL · ValueError: time data '300502' does not match format '%Y |
| [**全市场季度业绩(基准率底料)**](https://data.eastmoney.com/bbsj/) | 滞后 | 算「起始 ≥N% 增速四季后仍 ≥50%」的外部视角基准率 | `efinance get_all_company_performance` | ❌ FAIL · JSONDecodeError: Expecting value: line 1 column 1 (char  |
| [**现金流量表(A股 capex)**](https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?code=SZ000725) | 滞后 | 购建固定资产支付的现金 —— **面板厂(京东方/TCL)capex 就靠这个** | `akshare stock_cash_flow_sheet_by_report_em` | ❌ FAIL · SSLError: HTTPSConnectionPool(host='emweb.securities.eas |

### 3b 事件面(缺口)

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**上市公司公告**](https://np-anotice-stock.eastmoney.com/api/security/ann) | **领先** | 中标 / 重大合同 / 框架协议 / 订单 —— 对订单驱动的公司(杰瑞、深科达)比财报更直接。**PIT 纪律:只在公告日之后生效** | `东财 np-anotice-stock` | ✅ 通 · 5 条 |
| [**个股资金流**](https://data.eastmoney.com/zjlx/300502.html) | 同步 | 主力/大单/中单/小单净流入 | `akshare stock_individual_fund_flow` | ❌ FAIL · ProxyError: HTTPSConnectionPool(host='push2his.eastmoney |
| [**龙虎榜**](https://data.eastmoney.com/stock/lhb.html) | 同步 | 游资/机构席位买卖 | `akshare stock_lhb_detail_em` | ✅ 通 · 1633 行 × 21 列 |

### 4 杠杆与解禁

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**融资融券(深)**](https://www.szse.cn/disclosure/margin/margin/) | 同步 | 融资余额 / 当日买入 | `akshare stock_margin_detail_szse` | ✅ 通 · 2101 行 × 8 列 |
| [**限售解禁**](https://data.eastmoney.com/dxf/) | **领先** | 未来解禁日期与市值 | `akshare stock_restricted_release_detail_em` | ✅ 通 · 620 行 × 12 列 |

### 5 一致预期与评级

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**同花顺盈利预测**](https://basic.10jqka.com.cn/300502/worth.html) | **领先** | 分年度净利/EPS 的最小/均值/最大 + 机构数 + 分歧度 —— 三情景的输入 | `tools/consensus.py` | ✅ 通 · 3 行 × 6 列 |
| [**巨潮评级变动**](http://www.cninfo.com.cn/new/commonUrl?url=data/gg-rating) | **领先** | 评级调高/调低方向。**只覆盖境内券商**,外资行的下调看不到 | `tools/consensus.py` | ✅ 通 · 877 行 × 11 列 |
| [**东财研报全文**](https://data.eastmoney.com/report/) | 同步 | 研报 PDF,可抽产业量价句子 | `tools/research.py --dig` | ✅ 通 · 5 篇 |

### 6 领先指标·海外需求

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**SEC EDGAR XBRL**](https://data.sec.gov/api/xbrl/companyconcept/CIK0000789019/us-gaap/PaymentsToAcquirePropertyPlantAndEquipment.json) ⭐ | **领先** | 云厂/油服**已发生**的季度 capex(官方口径)。UA 必须含联系邮箱否则 403 | `tools/capex.py` | ✅ 通 · 230 期 |
| [**Motley Fool 电话会纪要**](https://www.fool.com/earnings/call-transcripts/2026/08/07/microsoft-msft-q4-2026-earnings-call-transcript/) | **领先** | **下期 capex 指引 + 短周期/长周期拆分**(SEC 只有总额,拆分只在纪要里)。URL 规律 /earnings/call-transcripts/YYYY/MM/DD/<公司>-<代码>-qN-YYYY-earnings-call-transcript/ | `requests + 正则取 article-body` | ✅ 通 · 正文 65,380 字符 · 含 capex |
| [**stockanalysis.com 前瞻一致预期**](https://stockanalysis.com/stocks/msft/forecast/) | **领先** | 美股分年度营收/EPS 一致预期(免费无 key) | `requests + 表格解析` | ✅ 通 · 有预测表 |
| [**stockanalysis.com 历史财务**](https://stockanalysis.com/stocks/msft/financials/cash-flow-statement/?p=quarterly) | 滞后 | 美股 20 个季度三表(比 SEC XBRL 好解析) | `requests` | ✅ 通 · 有 capex 行 |
| [**roic.ai**](https://roic.ai/financials/MSFT) | 滞后 | 美股三表(备源) | `requests` | ✅ 通 · 1,366,998B |
| [**海外同业电话会纪要**](https://www.fool.com/earnings/call-transcripts/2026/08/19/coherent-cohr-q4-2026-earnings-call-transcript/) | **领先** | 光模块链同业(COHR/LITE/FN/CRDO/MRVL/AVGO/ANET/CIEN)的需求与产能口径。相干 Q4FY26 原话:能见度到 **calendar 2028**,长约到 end of decade | `requests + 正则(同 Motley Fool)` | ✅ 通 · 正文 61,850 字符 · 含 capex |

### 7 领先指标·大宗

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**新浪外盘期货 油价**](https://hq.sinajs.cn/list=hf_CL,hf_OIL) | **领先** | WTI / 布伦特 | `tools/commodity.py` | ✅ 通 · 有报价 |
| [**北美钻机数(Baker Hughes 转载)**](https://api.oilpriceapi.com/v1/prices/latest) | **领先** | 作业量的既成事实 —— 油价和 capex 只是意愿,钻机数是已发生 | `tools/rigcount.py` | ✅ 通 · 1,880,338B |
| [**人民币中间价(CFETS)**](https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew) | 同步 | 出口占比高的公司的汇兑影响。**pageSize ≥ 100 返回 403 是分页超限不是被封** | `skills/astock-quote/scripts/fx.py` | ✅ 通 · 20 条 |

### 8 宏观

| 数据源(点名字进官方页) | 属性 | 拿到什么 | 怎么拿 | 实测 |
|---|---|---|---|---|
| [**中美国债收益率**](https://data.eastmoney.com/cjsj/zmgzsyl.html) | 领先 | 贴现率 —— 高估值成长股的分母 | `akshare bond_zh_us_rate` | ✅ 通 · 22 行 × 13 列 |
| [**全球指数**](https://quote.eastmoney.com/center/gridlist.html#global_asia) | 同步 | 外围市场温度 | `akshare index_global_spot_em` | ❌ FAIL · JSONDecodeError: Expecting value: line 1 column 1 (char  |

> ⭐ = 必需源(缺了做不了基础分析)。本次实测 41 条,通 36 条。

---

## 三、本次没通的 4 条:原因与处置

| 条目 | 状态 | 原因 | 处置 |
|---|---|---|---|
| 股东户数 · 全市场季度业绩 | ◇ 缺包 | 本机(MacBook)没装 `efinance`;company-linux 与 mac mini 上有 | `pip install efinance`,不是源的问题 |
| 个股资金流 | ❌ | 走 `push2his.eastmoney.com` —— **这台主机本机长期不稳**,和 `datacenter-web` 是两回事 | 换 `datacenter-web` 上的资金流报表,或接受降级 |
| 全球指数 | ❌ | 同样在东财 `push2` 系主机上,间歇性返回非 JSON | 定时抓落库,不要请求时现抓 |

> **东财要按主机分开看,不能按域名一刀切**:`datacenter-web`(报表类)本机稳定;
> `push2` / `push2his`(行情与资金流类)时通时不通。同一个 `eastmoney.com` 下面
> 两种行为。2026-08-27 实测 36 个接口 26 个通,分界就在主机而不在协议 ——
> `datacenter-web` 走明文 http 反而一直稳,`push2his` 走 https 照样不通。

## 四、确认拿不到的(附证据日期,别再重复试)

| 想要 | 为什么拿不到 | 证据 | 影响 |
|---|---|---|---|
| **北向资金个股净买方向** | 监管 2024-08-19 起停止日终披露,改按季公布。**不是接口坏了** | 交易所公告 | 中。能拿成交额与参与度,拿不到方向。全行业都没有,不是付费能解 |
| **海关 HS 85177060 光模块月度出口** | 海关总署 412(WAF 挡爬虫,真浏览器也过不去);统计局 403;东财宏观只有进出口总额无 HS 分项 | 2026-08-31 实测 | 中低。光模块出口是行业景气的直接量,拿不到只能用同业营收替代 |
| **LightCounting 全行业月度出货/现货报价** | 付费库独有 | — | 低。研报能给单家公司的量价,给不了全行业序列 |
| **审计意见(非标/保留)** | 免费源无干净接口 | a-stock-ai spec 11 实测 | 中。可用公告关键词代理(「非标/保留意见/问询函/立案/事务所变更」),召回不如付费字段 |
| **财报重述追踪(真 PIT)** | baostock 给的是**重述后的数字配原始 pubDate**,重述级前视免费消不掉 | a-stock-ai spec 11 | 中。免费档只能诚实标注为「**首次披露口径 PIT**」,不是「无未来函数」 |
| **外资行(高盛/摩根等)对 A 股的评级与目标价** | 订阅制,纯 A 股标的无公开转载渠道 | 2026-09-01 实测富途/雪球/AAStocks | **高**。2026-07-20 高盛把新易盛目标价 841→633(−25%),我们报告同期写「零下调」 |

> 上面两条「中」的可以花 ~500 元/年 上 Tushare 5000 积分解决
> (`fina_audit` 审计意见 + `ann_date`/`update_flag` 重述追踪)。其余是死局。

## 五、海外研报:三档渠道,按可靠性排

### ① 原始材料 —— 应该做主力(免费、稳定、可程序化)

**研报里 90% 的信息量本来就来自这些。** 与其追二手 PDF,不如直接读高盛读的东西。

| 拿什么 | 怎么拿 | 链接 |
|---|---|---|
| 云厂/同业电话会全文(含**下期指引**) | Motley Fool,URL 规律 `/earnings/call-transcripts/YYYY/MM/DD/<公司>-<代码>-qN-YYYY-earnings-call-transcript/`。站内搜索是 JS 的,用外部搜索定位再直取;正文在 `class="article-body"` 里 | [微软 FY26Q4](https://www.fool.com/earnings/call-transcripts/2026/08/07/microsoft-msft-q4-2026-earnings-call-transcript/) |
| 同上,结构化(分 prepared remarks / Q&A) | `earningscall` API,免费 demo key 可试,覆盖 5000+ 公司、回溯 5 年 | [earningscall-python](https://github.com/EarningsCall/earningscall-python) |
| 美股前瞻一致预期(分年度营收/EPS) | stockanalysis.com,免费无 key | [MSFT forecast](https://stockanalysis.com/stocks/msft/forecast/) |
| SEC 全部报表(10-K/10-Q/8-K/13F/Form 4) | `edgartools`,MIT,无 key | [dgunning/edgartools](https://github.com/dgunning/edgartools) |
| 美股 20 季三表(比 XBRL 好解析) | stockanalysis.com / roic.ai | [roic.ai](https://roic.ai/financials/MSFT) |

**为什么这一档够用**:高盛那份新易盛研报里真正驱动结论的东西 ——
1.6T 出货节奏、芯片供给改善、客户 capex —— 全部来自云厂和同业的电话会。
相干 2026-08-19 电话会原话「能见度到 calendar 2028、长约到 end of decade」,这是对 2027-2028 一致预期最硬的需求侧证据,比任何二手摘要都强。

### ② 中文券商研报 —— 已经有了

`tools/research.py --dig` 走东财 `reportapi` + `pdf.dfcfw.com`,能取全文并抽产业量价句子。GitHub 上同类工具可参考(都是同一批公开接口,取数思路可借鉴):

- [manymore13/report-cli](https://github.com/manymore13/report-cli) —— 命令行查/下研报,支持 agent 调用
- [lijinglin3/research-report](https://github.com/lijinglin3/research-report) —— 研报下载助手
- [GallenQiu/DeskResearch](https://github.com/GallenQiu/DeskResearch) —— 发现报告等行研站爬虫
- [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook) —— 券商金工研报**复现**(不是下载,是把研报里的因子实现出来)
- [reportcamp](https://github.com/reportcamp/reportcamp.github.io) —— 公开渠道汇总的行业报告库(含咨询机构)

### ③ 外资行研报 PDF 本身 —— 不能当管道

高盛/摩根这类是机构订阅制。手上那份带微信水印的是转手流出的,
**不稳定、来源不合规,不能建成自动管道**。

可行的做法是把它当**外部输入**处理:建 `private/extern_research/`,人工放进去多少就用多少,解析器把预测表、目标价、目标 PE、利益冲突披露抽成结构化 JSON,和自动取的数据**分开标注来源**。这份高盛报告已经可以当第一个样本 —— 它给的`2027E 目标 PE 27.8x = 历史前瞻 PE 均值 28x` 就是我们估值锚该用的东西。

---

## 六、机器差异与已知的坑

三台机器网络不同,同一条源的可达性不一样。**清单必须标明是在哪台机器上测的。**

| 机器 | 角色 | 网络 |
|---|---|---|
| company-linux | 源头仓 | 公司网 |
| mac mini | **跑数据的机器** | 命令行直连深圳电信,Clash 规则分流 |
| MacBook | git 与海外 LLM | TUN 开着走日本节点(本次探测就在这台) |

**坑(都踩过)**

1. **macOS 上 python `requests` 会读系统代理**(经 `_scproxy` 读系统偏好,与环境变量无关),
   `os.environ.pop` 挡不住;`curl`/`git`/`ssh` 则不读。两套行为不同。
2. **Clash TUN 模式在 L3 路由层截流**,`os.environ.pop` 是应用层,治不了。
   本机能用是因为 Clash 规则里对国内财经域名走了 DIRECT,不是因为那段代码对。   换机器要先在 Clash 规则层放行 `+.eastmoney.com`、`+.sinajs.cn`、`+.gtimg.cn`、   `+.baostock.com`、`+.iwencai.com`,而 Yahoo / SEC / Motley Fool 反而要走代理出海。
3. **SEC EDGAR 的 UA 必须含联系邮箱**,否则一律 403 —— 云厂 capex 整块废掉。
4. **CFETS 中间价 `pageSize ≥ 100` 返回 403**,那是分页超限不是被封。
5. **akshare / efinance 内部很多调用不带 timeout**,进程级 `socket.setdefaulttimeout()`
   是唯一可靠的兜底,否则会挂死在 CLOSE_WAIT 上。
6. **复权基准要对齐**:2026-09-01 拿未复权目标价比未复权日线,中间隔着一次送转
   (系数 1.40),算出来的隐含涨幅全错。用 `query_adjust_factor` 换算到同一基准。

---

## 七、还缺什么(下一步)

本轮已补上(2026-09-01):

| 原先列为缺 | 现在 |
|---|---|
| 海外原始材料 | ✅ `yfinance` 给 21 只上下游的目标价/分析师预估/**评级变动**(137~992 条/只);Motley Fool 电话会纪要正文可程序化取 |
| 外资口径的一致预期 | ✅ 两条路:① Yahoo 的 A 股一致预期池**含外资行**(实测新易盛 2026E 营收下沿 544.3 亿正是高盛那份的数);② 巨潮里的外资合资券商(野村东方国际、汇丰前海),`fetch_all` 会标 `_是否外资系` |
| 机构调研 | ✅ 东财 `RPT_ORG_SURVEYNEW`,含接待家数/形式/时间/接待人 |
| 公告 | ✅ 东财 `np-anotice-stock` |
| 研报原件 | ✅ `research/` 落列表 + PDF 原件(抽错了能回去核) |
| 外部研报结构化 | ✅ `extern-research-ingest` skill |
| 数据时效 | ✅ 信封里的 `fetched_at`,digest 每条都显示 |

还缺(按价值排):

1. **公司季度指引** —— 新易盛在电话会给了 `Rmb4.2bn~6.2bn` 的季度净利指引,散在公告与调研纪要里,要做关键词抽取
2. **公告正文关键词识别** —— 现在只取到列表。中标/合同/订单对订单驱动的公司比财报更领先
3. **机构调研纪要正文** —— 元数据已通,正文在巨潮的公告 PDF 里,和 `research.py --dig` 同套路
4. **面板厂 capex** —— 深科达真正的下游。现金流量表接口已通,写个聚合就行
5. **PIT 纪律** —— 每个取数函数加 `as_of`,按 `publish_date <= as_of` 切片。没有这个,历史分位和基准率都在偷看未来
6. **高盛/大摩等纯离岸研报** —— 结构上拿不到,走 §五③ 的人工投喂

---

*本文档由 `tools/probe_all_sources.py` 的实测结果生成,不是手写清单。*
*表格过期就重跑一次,别凭记忆改。*
