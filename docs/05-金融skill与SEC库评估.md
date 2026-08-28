# 金融类 Agent Skill 与 SEC EDGAR 库评估

> 2026-08-27 逐仓 clone + 实测数据源可达性 + 实跑对比。不是看 README 星数。

## 一句话结论

- **Anthropic 官方 skills 仓没有金融类**（只有 pdf / xlsx / pptx / docx / design 那些）。
- 社区四个金融 skill 仓里，**三个锁在付费 API 上，在本机等于废的**。
- **SEC EDGAR 有成熟库 edgartools**（MIT、免费、无需 key），值得用来取数，
  但它**不解决「累计 vs 单季」这个坑** —— 自写的折算逻辑仍然必要。

## 一　四个社区金融 skill 仓

全部 clone 在 `repos/` 下，可自行翻看。

| 仓 | skill 数 | 提交数 / 最后更新 | 主数据源 | 本机可用性 |
|---|---|---|---|---|
| `InvestSkill`（yennanliu） | 27 | 78 / 2026-08-26 | stockanalysis.com（200）+ **sec.gov**（200）+ barchart（200） | **可用** |
| `claude-trading-skills`（tradermonty） | 92 | 232 / 2026-08-23 | financialmodelingprep（**401 付费**）206 处引用 | 基本不可用 |
| `gauss314-skills` | 32 | 32 / 2026-06-14 | Yahoo（**403/429**）41 处 + finnhub + investing | 基本不可用 |
| `octagon-skills`（OctagonAI） | 67 | 50 / 2026-06-05 | 自家 MCP `app.octagonai.co` 265 处引用 | **完全不可用**（要它家订阅） |

### 数据源可达性实测（2026-08-27，公司 Linux）

| 源 | 直连 | 走代理 | 判断 |
|---|---|---|---|
| stockanalysis.com | 200 | 200 | 可用 |
| **data.sec.gov / www.sec.gov** | **200** | 200 | 可用，但 **User-Agent 必须带联系方式**，否则 403 |
| barchart.com | 200 | 200 | 可用 |
| Nasdaq API | 200 | 200 | 可用 |
| financialmodelingprep | 401 | 401 | 付费 |
| finviz | 403 | 301 | 挡爬虫 |
| Yahoo Finance | **403** | **429** | 不可用 |
| stooq | JS 挑战页 | 同 | 脚本取不到 |

> `www.sec.gov` 一开始返回 403，不是被墙 —— SEC **强制要求** User-Agent 声明联系方式。
> 换成 `stock-lab-research admin@example.com` 格式后立刻 200。这是 SEC 的明文规定，
> 同时要求请求频率 < 10 次/秒。

### 这些 skill 仓真正值钱的部分

**不是数据管道，是分析方法。**我们自己的取数管道（新浪 + 腾讯 + baostock +
东财 datacenter 系 + SEC EDGAR）在本机是通的，而它们的多半不通。

值得借鉴的是它们的**分析清单**：`earnings-call-analysis`（财报电话会怎么读）、
`competitor-analysis`（同业怎么比）、`short-interest`（空头持仓怎么看）、
`position-ladder`（仓位怎么分批）。这些是「看什么、怎么判断」的做法，
换个数据源照样成立。

**建议**：`InvestSkill` 值得精读它的 SKILL.md 学方法，
数据层继续用我们自己的。另外三个仓留着当参考，不投入接入。

## 二　SEC EDGAR：edgartools vs 自写

`capex.py` 支持 `--engine raw|edgartools|both` 两个引擎，可直接对比。

### edgartools 好在哪

| 维度 | edgartools | 自写 raw |
|---|---|---|
| 依赖 | 新增 20 个包（httpx / pyarrow / pydantic / lxml 等） | **零额外依赖**（只用标准库 urllib） |
| 取数 | 一次 `get_facts()` 拿全部概念（MSFT 32671 行） | 逐个标签试 `companyconcept` |
| 标签差异 | 自动，不用管 | 要维护 `TAG_CANDIDATES` 列表 |
| 财季标注 | 自带 `fiscal_year` / `fiscal_period` | 要自己按日期推 |
| 速度 | 0.5–2.8 秒/家 | 约 0.3 秒/家（单标签） |
| 附带能力 | 10-K/8-K/13F/Form 4 全解析、`peer_comparison`、`to_llm_context` | 只有 Capex |
| 代码量 | 约 30 行调用 | 约 130 行 |

### edgartools 不解决的那个坑（实测）

**它的 `fiscal_period` 标注会把累计记录标成单季。**谷歌实测：

| period_start | period_end | 天数 | 值 | edgartools 标注 |
|---|---|---|---|---|
| 2025-01-01 | 2025-03-31 | 89 | 17.20 B | Q1 |
| 2025-01-01 | 2025-06-30 | **180** | **39.64 B** | **Q2** ← 这是 H1 累计 |
| 2025-01-01 | 2025-09-30 | 272 | 63.60 B | Q3 ← 这是 9M 累计 |

照 `fiscal_period == "Q2"` 取，**谷歌 Q2 会读成 39.64 B，真实值是
39.64 − 17.20 = 22.44 B，偏高 76%**。

改用 `filter_by_period_type("quarterly")` 呢？它确实滤掉了累计记录 —— 但**谷歌就只剩 Q1**
（一年 4 个季度只剩 1 个），因为谷歌只把 Q1 报成 89 天的离散期间。

**两条路都得靠差分补齐。**所以 `_to_quarters()` 那段逻辑，用不用 edgartools 都得写。

### 三个必须处理的 XBRL 坑（自写和用库都一样）

1. **各家标签不同**。MSFT / GOOGL / META 用 `PaymentsToAcquirePropertyPlantAndEquipment`；
   **AMZN 2025 年起改用 `PaymentsToAcquireProductiveAssets`**，旧标签在 2025 年后完全没数据。
2. **有的公司只报年初至今累计**（谷歌），单季必须相邻相减。
3. **364 天的记录不一定是财年**。亚马逊同时存着滚动 12 个月
   （`start 2024-04-01 → end 2025-03-31`），按天数判会误当财年。
   正确做法：按 `start` 分组，同一 start 的一串才是累计序列，
   且首条自身 ≤100 天才认这组是财年累计。

### 交叉验证结果

`capex --engine both` 对四大云厂逐季比对：

| 公司 | 季度数 | 结果 |
|---|---|---|
| MSFT | 72 | ✅ 完全一致 |
| GOOGL | 46 | ✅ 完全一致 |
| AMZN | 35 | ✅ 完全一致 |
| META | 58 | ✅ 完全一致 |

**211 个季度零差异。**自写逻辑得到独立验证，两个引擎都保留。

### 结论

- **默认用 `raw`**：零依赖、够快、结果已验证。换机器 clone 就能跑。
- **`edgartools` 保留**：当交叉验证的第二意见，以及以后要 10-K 正文 / 13F /
  内部人交易时直接用它，不用再造轮子。
- **`both` 用来回归**：SEC 改数据格式时，两引擎不一致会立刻暴露出来。

## 三　装 edgartools 的一个副作用

它把 `httpx` 升到 0.28.1，与 `mootdx 0.11.7` 声明的 `httpx<0.26` 冲突。
**不影响** —— mootdx 早已弃用（TCP 7709 通但取数返回空，见 `01-环境与数据源.md`）。
如果以后要恢复 mootdx，得处理这个版本冲突。
