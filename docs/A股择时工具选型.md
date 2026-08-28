# A 股择时/投资建议工具选型报告

> 目标需求:给一只**已持仓且浮亏**的 A 股(评测用例:中际旭创 300308,假设成本 1100、现价 846、浮亏约 27%),
> 得到「该长线还是短线持有 · 什么价位买入 · 什么价位卖出 · 套牢仓该扛还是割」这类结合个人持仓的择时建议。
> 调研日期:2026-08-26。评测机器:受限网络 + 全局代理(东财 `push2.eastmoney.com` 被墙)。

---

## 0. 一句话结论

**首选 `AI_Investment_Manager`**(你本地已有的仓):它原生吃持仓成本、给长/短线判断、给「扛还是割」的行动计划,
数据源默认走新浪+腾讯,在这台被墙的机器上本来就能跑,且部署极轻。
想要更深的分析用 `TradingAgents-CN`,想要「这套规则历史上赚不赚」的回测证据用 `KHunter`。三者不冲突,可主用前者。

**一个必须先接受的前提**:没有任何工具能「预测」股价。所谓目标价/信号/建议,本质都是
基于历史行情、技术指标或大模型的**概率性参考,不是确定性预测,不保证盈利**。它们能帮你做的是
「给一套有依据的止损/解套/加仓价位和情景判断」,不是「告诉你一定涨回去」。

---

## 1. 候选与评测范围

| 仓 | 星标 | 路线 | 结论 |
|---|---|---|---|
| **Einsphoton/AI_Investment_Manager**(本地) | — | 持仓管理 + LLM 建议 | ✅ 最对口 |
| hsliuping/TradingAgents-CN | 31366 | 多智能体 LLM 投研 | 深度更强,但不吃成本、部署重 |
| ling-0729/KHunter | 377 | 规则量化 + 回测 | 不对口,但回测是独门 |
| ravixalgorithm/OpenStock | — | 通用行情站(Next.js) | 按需跳过未细读 |

数据源均已核对到 `file:line`(见各节),不是凭印象。

---

## 2. 横向对照(按需求逐项)

| 需求 | AI_Investment_Manager | TradingAgents-CN | KHunter |
|---|---|---|---|
| 填持仓成本算浮亏 | ✅ 原生,成本喂进模型 | ❌ 只收「代码+日期」 | ❌ |
| 长线/短线判断 | ✅ `time_horizon` 短/中/长 | ❌ | ❌ |
| 套牢仓「扛还是割」 | ✅ `action_plan` 分情景 | ⚠️ 只给买/持/卖,不针对你的仓 | ❌ |
| 卖出价 / 止损 | ✅ `target_price` + `stop_loss` | ⚠️ 单一目标价(缺失时「现价×1.15」硬凑) | ⚠️ 机械模板(支撑×0.95 / ×1.2) |
| 买入价位 | ⚠️ 无独立字段,靠模型写进文本 | ⚠️ 技术分析师文本里有 | ⚠️ 现价±1% |
| 300308 数据源本机可用 | ✅ 默认新浪+腾讯 | ❌ 默认 akshare 走被墙东财,须改 tushare | ✅ 腾讯 + TickFlow |
| 建议怎么产生 | 纯 LLM(单轮) | 纯 LLM(多智能体辩论,更深) | 纯规则,**有回测** |
| 回测验证 | ❌ | ❌ | ✅ 独门 |
| 本机部署难度 | ✅ 极轻(9 个 pip 包) | ❌ 重(多数据商+多 agent) | 中(需先 init 全市场) |
| 要 LLM key | ✅ 任意 OpenAI 兼容(DeepSeek 最省) | ✅ 同 | ❌ 不用 |

---

## 3. 各仓详情(带代码证据)

### 3.1 AI_Investment_Manager —— 首选

- **本质**:自托管 Web 应用,把你的持仓(含成本价)+ 实时行情 + 基本面拼成 prompt 交给 OpenAI 兼容大模型产出结论。本身几乎无量化/规则引擎。
- **技术栈**:前端 React18+TS+Vite+AntD;后端 Python3.12+FastAPI+SQLAlchemy;DB SQLite。后端依赖仅 9 个包,**无 pandas/akshare/tushare**(`backend/requirements.txt`)。
- **数据源**(`backend/data_source.py`,手写 HTTP):
  - A 股默认报价 = **新浪**(`DEFAULT_STOCK_PROVIDERS` :38);K 线回退 **腾讯→东财→Yahoo**(`get_history` :864);基本面回退 **腾讯→东财→新浪**(`get_fundamentals` :1266)。
  - 东财只是备胎 → **本机被墙不致命**,300308 报价/K线/PE 都能拿到。唯一注意:按名称搜标的走 `searchadapter.eastmoney.com`(:381),被墙则按代码 300308 直接录入绕开。
- **核心功能「资产分析」**(最贴需求):端点 `/api/analysis/run-stream`,prompt 在 `main.py:1074-1096`,对每只持仓返回:
  - `final_suggestion`:BUY/SELL/HOLD/**ADD/REDUCE**
  - `target_price` 目标价、`stop_loss` 止损
  - `time_horizon`:**SHORT/MEDIUM/LONG**
  - `position_diagnosis`:结合买入价/现价/盈亏/仓位的诊断
  - `action_plan`:持有/加仓/减仓/止损的**触发条件**(即「扛还是割」)
  - 前端展示 `frontend/src/pages/Assets.tsx:1131-1208`
- **吃持仓成本**:字段 `Asset.buy_price`(`models.py:16`),浮亏 `total_pnl/pnl_percent`(`ai_service.py:224`),成本喂进 prompt(`recommendation.py:28-33`)。**这是它相对 TradingAgents-CN 的最大优势。**
- **LLM**:仅 OpenAI 兼容(`openai` SDK),可配 `base_url` → DeepSeek/通义/硅基流动/本地 vLLM 都行,默认 `gpt-4o-mini`。**key 在网页设置页填,存本地 DB**(`ai_service.py:216`),不读环境变量。
- **短板**:无独立「买入价位区间」字段;纯 LLM 单轮,无量化/回测校验,结论质量随模型而变。

### 3.2 TradingAgents-CN —— 深度更强,但不吃成本、本机部署有坑

- **本质**:多智能体 LLM 框架,分析师/研究员(多空辩论)/交易员/风控多角色产出「买入/持有/卖出 + 目标价」研报。定位研究工具。
- **输出**:结构化 `action/target_price/confidence/risk_score/reasoning`(`tradingagents/graph/signal_processing.py`)。技术分析师文本里有支撑/压力/突破买入价/跌破卖出价,但**不进结构化字段**。
- **不给持有周期**(搜遍 prompt 无「长线/短线」输出);**不吃持仓成本**(`propagate(company_name, trade_date)` 只两个参数)。
- **目标价隐患**:模型没吐价时用 `_smart_price_estimation()` 按「现价×1.15/×0.95」硬编码凑(`signal_processing.py:296`)。
- **本机障碍**:默认数据源 akshare,A 股**历史日线 `stock_zh_a_hist` 走被墙的东财且无新浪兜底**。绕过:`DEFAULT_CHINA_DATA_SOURCE=tushare` + `TUSHARE_TOKEN`(tushare 走自己域名不受墙)+ 一个 DeepSeek key。
- **省钱**:官方文档推荐 DeepSeek V3 性价比最高。

### 3.3 KHunter —— 不对口,但回测是独门

- **本质**:全市场批量**选股扫描 + 规则量化回测**系统,不是单票顾问。15 种选股策略 + 5 种择时策略 + 完整回测。纯规则,**零 LLM**。
- 对单票只能给:今天有没有触发买入策略(触发则给「现价±1% 买、支撑×0.95 止损、支撑×1.2 止盈」机械模板,`trading/trading_plan_generator.py:317-354`);MA/MACD/KDJ/RSI/布林打分的技术评级。
- **不吃成本、不给长短线**。
- **独门**:回测引擎 `trading/backtest_engine.py` 能算历史胜率/最大回撤/夏普——纯 LLM 给不了的证据。
- **本机**:核心 K 线走腾讯 gtimg(可直连)+ TickFlow 第三方(连通性需实测);东财只影响资金面/板块的五维打分。须先 `python main.py init` 落全市场数据。

### 4.1 两个前提
1. **一个大模型 key**,推荐 **DeepSeek**(最省钱,`api.deepseek.com` 国内直连、不受这台机器的墙影响)。
   - ⚠️ **key 不要贴进对话**。本项目在网页设置页填 key、存本地 DB,天然不经过 Claude,符合安全约定。
2. **代理冲突**:后端 requests 会吃全局代理,可能把本可直连的新浪/腾讯代理坏(与 astock 同一个坑)。
   启动时对行情摘掉代理,LLM 请求该走代理走代理。

### 4.2 启动步骤(参考 `dev.sh`)
```bash
cd $LAB/repos/AI_Investment_Manager
./dev.sh          # 建 venv → 装后端依赖 → npm install → 起后端:8000 + 前端:5173
# 浏览器开 http://localhost:5173
```
- 依赖:后端 9 个 pip 包(轻);前端需 Node/npm(或用 docker 单端口 + 内置打包产物)。
- 行情不需要 key;**唯一硬门槛是 LLM key**(设置页填)。

### 4.3 用起来
1. 设置页填 DeepSeek key(base_url `https://api.deepseek.com`,模型 `deepseek-chat`)。
2. 资产页录入:代码 `300308`、买入单价 `1100.00`、持有数量。
3. 跑「资产分析」→ 得到浮亏诊断 + 长/中/短线 + 建议(持有/减仓/加仓)+ 目标价 + 止损 + 行动计划。

---

## 5. 附:本机 A 股行情速查工具(已就绪)

调研期间已建独立行情工具(与本选型配套,不依赖任何 LLM):

- 脚本:`$LAB/tools/astock.py`(权威副本在 本仓 skill `astock-quote`)
- 用法:`astock 300308 --l5`(快照+五档)、`--tick`(逐笔)、`--daily`(日线)
- 关键坑同源:**东财被墙 → 用新浪+腾讯,且只对查行情这一个进程去代理**,不动 shell 全局(Claude Code 仍走代理)。

---

## 6. 风险提示(务必读)

- 上述所有工具的输出都是**参考**,非投资建议,不保证盈利。A 股高波动个股(如 300308)尤甚。
- LLM 路线的结论**质量完全取决于所配模型**,且可能「一本正经地编数字」(如硬编码凑的目标价),使用时要交叉验证。
- 真要用于决策,建议:LLM 做情境化解读 + KHunter 回测当证据 + 自己的风险承受度,三者结合,不迷信单一输出。
