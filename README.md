# astock-lab

**A 股个股分析工作台 —— 带方法的那种。**

零成本、零 API key，15 个取数与分析工具，外加 **3 个 Agent Skill**：
装上以后，AI 不只是知道**怎么取数**，还知道**该按什么顺序看、哪些指标是领先的、
结论该怎么下、下完怎么复核**。

```bash
git clone https://github.com/TbusOS/astock-lab.git ~/astock-lab
cd ~/astock-lab && bash install.sh       # 建 .venv、装依赖、链 skills、生成别名
source aliases.sh                        # 下面所有命令都靠它;不 source 就用全路径
export SEC_UA='你的名字 你的邮箱'        # SEC 要求，不设会被限流

preport 300308:1100                      # 九层决策报告
```

不想用别名就写全路径：`~/astock-lab/.venv/bin/python ~/astock-lab/tools/position_report.py 300308:1100`。
`install.sh` **不会动你的 `~/.bashrc`** —— 它只生成 `aliases.sh`，要不要常驻由你决定
（`echo "source ~/astock-lab/aliases.sh" >> ~/.bashrc`）。

---

## 为什么不只是一堆取数脚本

取数工具满地都是。真正难的是**拿到数据之后怎么想**。

绝大多数股票数据是**后视镜**：财报、股东户数、估值分位、龙虎榜 —— 全是已经发生的事。
而投资投的是未来。所以这个仓的核心不是数据管道，是这四件事：

| | 是什么 | 落在哪 |
|---|---|---|
| **分清领先与滞后** | 只有少数指标真正领先：北美云厂 Capex（领先业绩 1-2 季）、海外同业股价、云厂下季度指引、券商一致预期的**变动方向** | `capex` · `consensus` · `preport` 第 7 层 |
| **预期差** | 不预测价格。市值反推市场隐含的预期 → 找市场在担心什么 → 用领先指标去**证否**。已被证否的担忧才是可下注的判断 | `stock-analysis-workflow` §1.5 |
| **赔率 × 概率** | 三情景算**概率加权期望值**，不是只算赔率。EV ≤ 0 不下注 | 同上 |
| **产业一手数据** | 研报 PDF 免费可下且文本层完好 —— 实测抽到「产能扩至 2836 万只/年（+86.58%）」「1.6T Q2 出货量较 Q1 明显增长」这类**真数字**，不是只有方向 | `research --dig` |
| **基准率** | 「净利 +91%」听着很强，但没有参照系。历史上同样位置的公司四个季度后还能保持的**只有 24%** —— 这个数直接约束你能给 bull 情景多高的概率 | `baserate`，已自动接进 `preport` 第 8 层 |

最后一条最容易被跳过，也最值钱。人只盯着一家公司时会系统性高估「这种好状态还能持续多久」，
因为脑子里装的是这家公司的故事，没有参照系。**基准率就是那个参照系。**

---

## 三个 Agent Skill

`install.sh` 会把它们链进 `~/.claude/skills/`。装上之后，你只要说
「帮我看看 300308 该不该加仓」，AI 就知道该调哪些工具、按什么顺序、
结论该怎么组织、哪些话不能说。

| Skill | 管什么 | 边界 |
|---|---|---|
| **`stock-analysis-workflow`** | **分析方法**：领先/滞后指标、预期差、赔率、基准率、自我进化、判据模板、报告结构、七种常见误判 | 不管选股、不管量化回测 |
| `astock-quote` | 取数工具与每个数据源的坑（哪个接口能用、为什么、拿不到时替代是什么） | 不管怎么分析 |
| `finance-pdf-report` | 出金融 PDF 的固定版式（结论前置、语义色、内联 SVG、强制数据来源表） | 不管内容写什么 |

不装 Claude Code 也能用 —— 三个 `SKILL.md` 本身就是可读的方法文档。

---

## 自我进化

分析方法如果不复核，做十年也只是重复十年。这个仓内建一条从记录到复核到蒸馏的完整循环：

```bash
# ① 给结论时记一笔，写清什么情况算你错了
journal log 300308 --price 846 --action hold \
        --bear 0.3 --base 0.45 --bull 0.25 --ev 11.5 \
        --thesis "市场担心 1.6T 放量不及预期；云厂 Capex 指引已证否这条" \
        --falsify "三季报净利同比跌破 30%，或毛利率掉 5pp" --check 2026-10-31

# ② 到期回来复核
journal review

# ③ 复核后结掉。--outcome 必填(right/partial/wrong)，--principle 是重点
journal close 1 --outcome partial \
        --actual-price 720 --note "增速守住但毛利掉了 2pp，方向对幅度错" \
        --principle "海外同业已转跌时，A 股的背离不是买点是滞后"

# ④ 看命中率，以及你预估的期望收益和实际差多少
journal stats
journal stats --html kb.html   # 给人看的知识库视图
```

**一份数据两种呈现**：给 AI 的是 `data/principles.jsonl` 与快照里的 `reasoning` 段
（结构化，下一轮分析直接读回去）；给人的是那个 HTML 视图 ——
原则库、待复核、决策时间线、概率校准。

这是 **EvolveR 式的三阶段**（在线交互 → 离线自蒸馏成原则 → 用原则改进决策）。
关键在第三步：**存下来的必须是抽象原则，不是「这次 300308 怎么样了」**，
否则下次换只票就用不上。

原则会在下一次分析开始时被读回来（`journal stats` 是流程的第 ⓪ 步）。

---

## 工具

下面用的是 `source aliases.sh` 之后的短名字。每个都支持 `--help`。

```bash
astock 300308 --l5 --tick        # 行情 / 五档 / 逐笔 / 日线
efdata --list                    # 东财报表数据 18 个子命令
hcheck 300308:1100               # 持仓体检 + PE/PB 历史分位
preport 300308:1100 --md r.md    # ★ 九层决策报告 → markdown
preport 300308:1100 --html r.html # 同上，带版式的 HTML（可打成 PDF）
capex --guidance                 # ★ 北美云厂 Capex + 他们自己给的下期指引
consensus 300308                 # ★ 券商一致预期 + 评级变动方向
research 300308 --dig 3          # ★ 券商研报全文 PDF + 产业量价抽句
baserate calibrate 300308        # 基准率(已自动进 preport 第 8 层)
journal stats                    # 决策复核与已蒸馏的原则
probe_sources --only blocked     # 哪些数据拿不到、为什么、替代是什么
```

`preport` 是主力，九层一次跑完：

```
1 持仓状况   2 基本面   3 估值   4 筹码杠杆   5 解禁风险
6 技术位     7 海外同业(需求端在海外，只看 A 股会漏背离)
8 基准率(外部视角)     9 判据小结(每条判据自带历史基准率)

0 与上次对比 —— 有历史快照时自动插到最前面
```

每跑一次自动落一份**结构化快照** `data/snapshots/<代码>/<日期>.json`，
下次再跑第 0 节就出「这次 vs 上次」：净利同比 110% → 91%（▼19pp）、
毛利率离判据线还有多远、股东户数增减多少。

**HTML 给人看，JSON 给机器 diff** —— 两份 HTML 做对比只会得到排版噪音。
这是自我进化的证据层：`journal` 记**决策**，快照记**证据**，第 0 节显示**变化**。

第 9 层长这样 —— **判据不再是光秃秃的阈值**：

```
- 净利同比保持 >50%（当前 +91%）　—— 同类历史基准率 24%（起始 ≥ 90% 的公司里）
- 毛利率不掉 3pp 以上（当前 48.4%）　—— 同类历史基准率 66%
- 净利同比跌破 30% —— 历史上同类公司 4 个季度后有 69% 会跌破这条线
```

用法详解见 [`docs/02-工具用法.md`](docs/02-工具用法.md)。

---

## 数据源

全部零成本零 key：新浪（行情）· 腾讯（逐笔）· 东财 datacenter-web（报表）·
baostock（估值分位）· 同花顺 + 巨潮（一致预期）· **SEC EDGAR XBRL 官方**（云厂 Capex）。

**可达性按接口路径分，不按主机、不按协议** —— 同一个域名下不同路径的可达性可以完全相反。
详见 [`docs/01-环境与数据源.md`](docs/01-环境与数据源.md)。

拿不到的数据也写清楚了（为什么拿不到、试过什么、替代是什么）：
`probe_sources --only blocked`。**「封装库坏了」不等于「数据拿不到」** ——
akshare 的机构调研封装报 TypeError，但直连底层接口正常返回。

---

## 提交前跑闸

```bash
bash scripts/check_gates.sh      # 退出码 0 才 push
```

六道：零个人信息 · `_lab_root` 多份一致 · 判据线==基准率线 · 软链无断链 · py 语法 · sh 语法。

**每条硬规矩都配了可执行检查** —— 写进文档的规矩会被忘，能跑的检查不会。

---

## 目录

```
astock-lab/
├── README.md
├── LICENSE                 # MIT
├── install.sh              # 建 venv + 装依赖 + 链 skills + 生成 aliases.sh
├── .astock-lab-root        # 根标记，脚本靠它定位工作台（别删）
├── skills/                 # ★ 三个 Agent Skill（真身）
│   ├── stock-analysis-workflow/   分析方法 + 5 个脚本
│   ├── astock-quote/              取数工具 + 8 个脚本
│   └── finance-pdf-report/        PDF 版式 + 模板 + 3 个脚本
├── tools/                  # 15 个扁平入口，除 net_probe.sh 外都软链到 skills/*/scripts/
├── docs/                   # 环境 / 用法 / 数据全集 / 思维框架对比 / 选型 / 私有副本
├── scripts/                # 闸与维护脚本
├── private/                # 你自己的东西（持仓、报告、笔记）—— 本仓只有个占位
├── data/                   # 取数落盘与决策记录（不受 git 管）
├── repos/                  # 第三方开源仓（install.sh 拉，不受 git 管）
└── aliases.sh              # install.sh 生成，含本机绝对路径（不受 git 管）
```

---

## 想留一份自己的私有副本

分析记录、真实持仓成本、生成的报告这些**不该进公开仓**，但你自己要留。
做法是 clone 一份当私有副本，把公开仓挂成 `upstream`：

```bash
git clone https://github.com/TbusOS/astock-lab.git ~/astock-lab-private
cd ~/astock-lab-private
git remote rename origin upstream
git remote add origin <你自己的私有仓>
bash scripts/setup_private.sh      # 建 private/ 目录并让 data/ 可被 track
```

之后 `bash scripts/sync_from_upstream.sh` 拉公开仓的更新，你的数据不受影响。
细节见 [`docs/08-私有副本.md`](docs/08-私有副本.md)。

---

## 免责

**本仓输出的是机械判据，不是投资建议。**所有决策和后果都是你自己的。
基准率是历史统计不是预测；一致预期是卖方观点不是事实；
任何「目标价」都建立在一串会被证否的假设上。

MIT License。
