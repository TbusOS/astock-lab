---
name: forecast-board
description: >
  股票预测看板:给持有或关注的几只股票,按 3 个月 / 半年 / 一年出**带概率**的预测 ——
  涨的概率、10/25/50/75/90 分位价、最低点落在哪个月和哪个价位、下次什么时候买。
  三层分开存、分开评分:统计底线(按波动做不带方向的随机模拟)、系统算法(几个领先指标给方向)、
  两个 AI(Claude 和 codex 拿同一份冻结的输入包各答一次)。每次预测冻结存档、只追加;
  到期自动结算,错题给出每个原因的概率;能拿旧存档用新代码重算,看改了以后有没有变准。
  出两个静态页面:看板.html(第一屏:现在 → 怎么看(按当时事实的判断,回答该不该卖、要不要买回、行业还值不值得待)→ 价格可能晃多大 → 对照;下面「细节」里是结论、动作、见底、买点)、成绩.html(各层成绩、错在哪里、重跑),
  外加一个手机单文件版。
  触发词:预测看板 / board / 出预测 / 更新预测 / 涨的概率 / 涨多少跌多少 / 什么时候见底 /
  什么时候反弹 / 下次买点 / 该持有还是卖 / 结算 / 到期核对 / 成绩页 / 错在哪里 / 原因概率 /
  重跑 / 改了以后有没有变准 / 每日检查 / 卖出线触发 / 改主意条件。
  **边界**:只管「按期限出带概率的预测、存档、结算、评分、出页面」。
  一只股票怎么分析、三情景估值在 forecast-from-facts 和 stock-analysis-workflow;
  取数工具在 data-sources;借款和还款明细在借款看板(本 skill 只读)。
---

# forecast-board —— 预测看板

## 一句话

**预测只有拿到期的结果来核,才知道准不准。** 所以每道题都给概率、冻结存档、到期结算,
错了要说出是哪一层、哪个原因,再拿旧题重算,看改动有没有用。

## 什么时候用

| 场合 | 做什么 |
|---|---|
| 想知道一只股票未来 3 个月 / 半年 / 一年偏涨还是偏跌、涨跌多少、什么时候见底、下次什么时候买 | 先打开 `看板.html`;要新的判断就 `board forecast` |
| **给任何预测或操作建议之前** | 先 `board inputs --check <代码>`,把列出来缺的项写进结论 |
| 聊天里给了一个带概率的判断(「三个月内碰到某价的概率 30%」) | `board add-question` 记成一道题,到期和别的题一起核 |
| 每个交易日收盘后 | `board check`(装了定时任务就自动跑) |
| 复核日到了、卖出线或改主意条件触发了 | `board resolve` 结算,再决定要不要 `board forecast` |
| 改了算法、修了取数 | `board replay` 拿旧存档重算,和原来比 |

## 四条硬规定

| 规定 | 为什么 | 程序里怎么保证 |
|---|---|---|
| **每道题每一层都给概率**,不许写「判断不了」;没检验过的方法照样出数,标「尚未验证」 | 不给数就没法核,预测系统也就没法改进 | 答卷逐项校验:是非题 0~100 的整数,分类题各项加起来正好 100;不合格重问一次,还不合格整份标「作答失败」;哪一层整层没出题,`forecast` 退出码 1 并写出是哪一层 |
| **输入冻结** | 行情一刷新,预测不能悄悄跟着变;重算要能复现原来的数 | 每项输入连同来源、截至哪天、公开时间、取数结果(取到 / 只取到一部分 / 用了旧值 / 取不到)存进 `inputs.json`;代码提交号、未提交改动全文、随机种子存进 `manifest.json`;`recompute` 只读这一个文件,不读配置、不联网 |
| **存档只追加** | 旧版被删、被覆盖,就核不了当时到底说了什么 | 每次预测一个新目录,文件写过就不再改(同名已存在直接报错);同一天重出时,旧版只多一个 `superseded.json`,照样到期核对;结算、原因、重算记录都是追加的 jsonl,同一题号以最后一行为准 |
| **两个 AI 用同一份输入包、同一份提示词作答** | 只有这样,比谁的误差低才公平 | 提示词只存一份 `ai/prompt.md`,第二个 AI 生成的和它不一样就拒绝;Claude 用 `claude -p` 作答,不给工具、不读设置文件和记忆;codex 用 `codex exec`;两边的原始答卷都存下来 |

## 三层预测

| 层 | 是什么 | 谁出 |
|---|---|---|
| 统计底线 | 按最近 120 个交易日的波动做随机模拟,**不带方向**(中位价 = 起点价):+63 / +126 / +250 个交易日的分位价、期末高于起点的比例、期间最低点在哪个月和哪个价位段 | `baseline.py` |
| 系统算法 | 在同一套模拟上加一个方向:大盘在不在 60 日线上、同行涨跌差、券商盈利预测上调还是下调、云厂资本开支、估值高低,五个信号各投一票再加权。大盘、券商、云厂开支三票 2026-11-03 起权重 0(逐条考想法时都没显出用处,大盘还是反的;只记不用),现在只有估值、同行两票在起作用;没检验过的信号权重减半;信号全是 0 时和统计底线逐位相同 | `system_forecast.py` |
| AI 判断 | Claude 和 codex 各看同一份输入包作答,可以不同意系统算法,不同意要写理由 | `ai_answer.py` |

> 这些层预测的是什么、历史补考考的是什么:见 `../stock-analysis-workflow/references/prediction-thinking.md`。
> 一句话:按当前事实判断几种情况和概率;历史只用来考这套想法在当时的信息下灵不灵,不把历史比例搬到未来。

- 期限固定 63 / 126 / 250 个交易日,分位固定 10 / 25 / 50 / 75 / 90。交易日按创业板指的日线日期数。
- 结算比的是**后复权**收盘的比值,所以中间分红、送转不会把结果带偏。
- 动作(持有 / 减仓 / 卖出;空仓时等待 / 分批买)在账户层统一出一份:配置里定好的规则优先(卖出线、现金留够几个月的还款、按比例还是按顺序卖、仓位上限),AI 不同意规则时另起一行写「AI 建议改规则」,改不改由人定。

## 命令速查

```bash
export STOCK_LAB=<内部仓>       # 配置和存档都在内部仓;不设会落到代码仓,那里没有配置,直接报错
source aliases.sh               # 之后用 board;不 source 就写 $VENV/bin/python $LAB/tools/board.py
```

| 命令 | 做什么 |
|---|---|
| `board inputs --check [代码…]` | 只取数,列出每项的状态、截至哪天、给谁用,以及缺什么。不写代码 = 配置里全部。不出预测、不写存档。必需项取不到时退出码 1 |
| `board forecast [--no-codex] [--notes 查证笔记.md] [--seed N]` | 取数 → 统计底线 → 系统算法 → 两个 AI → 存档 → 出题 → 写决策日志 → 画页面。交易日 09:15~15:05 拒绝(退出码 2);日线取不到或只有旧值也拒绝。起点 = 最后一个收盘的下一个交易日。`--notes` 的笔记只给 AI 参考;种子默认用最后一个收盘日期的数字 |
| `board update` | 刷新收盘价、财报日历、SEC 新文件和持仓,用最新一次存档重画页面。**不改预测** |
| `board check [--notify]` | 每日检查:收盘价比卖出线、比两个 AI 写的改主意条件,列出到期的题,写 `status.json`,重画页面;`--notify` 时有触发就弹系统通知。一只取不到不影响另一只,取不到的列进「没查成」,退出码 1 |
| `board resolve` | 结算所有到期的题,追加到 `outcomes.jsonl`;错题附上程序给的原因初值 |
| `board add-question …` | 把聊天里的判断记成一道题,见下面 |
| `board confirm --qid <题号> --probs data_gap=60,system=30,luck=10 --note "…" [--evidence "…"]…` | 人确认一道错题的原因概率,七项加起来 100,没写的算 0。**不写 `--who` 就是 user**,只有 user 写的算人确认;写别的名字照样记下,成绩页只当提议 |
| `board replay --run <日期/时分秒> --code <提交号\|HEAD\|run> [--corrected 修正输入.json]` | 用指定版本的代码重算那次存档的统计底线和系统算法,和原来比,记进 `replays.jsonl`。`run` = 存档当时那份代码(提交号 + 当时的未提交改动)。临时代码目录跑完就删 |
| `board recompute --inputs <文件> --seed N` | 给 `replay` 用的:只读那个输入包,标准输出只有一段 JSON |

原因的七个键:`luck` 正常的运气 · `system` 系统算法 · `ai` AI 判断 · `data_gap` 数据没取全 ·
`data_error` 数据内容或单位错 · `impl_error` 程序实现错误 · `unforeseeable` 事先没法知道的事。

`--corrected` 的修正输入只能改数据,每一项改过的都要有 `public_at` 且不晚于原存档的日期 ——
后来才公布的数只能拿来解释,不能算成当时做得到的预测;原来有的项一个都不能删。

**加题**(三种题型):

```bash
# 某天收盘高于某价:--ratio = 价位 ÷ 开题日收盘
board add-question --code 300308 --type above_on --ratio 1.05 --day 2027-01-29 \
      --prob 70 --author claude --note "哪次聊天说的、怎么换算的"
# 期间碰到某价:--n(往后几个交易日)和 --end(截止日)二选一;--dir up/down 不写就按比值定
board add-question --code 300308 --type touch --ratio 0.8 --n 63 --prob 30 --author codex --note "…"
# 某日以前到某个倍数
board add-question --code 300502 --type multiple_by --k 2 --end 2027-06-30 --prob 10 --author claude --note "…"
```

`--opened` 不写 = 最后一个收盘那天。开题日早于第一次存档的,算「换算进来的旧记录」:
照样结算,但只在成绩页单独一栏做原文比对,不进各层成绩。同一个人、同一天、同样的题只记一次。

**退出码**:0 做完了;1 做了但有要处理的(取不到数据、题号重复、有题结算不了……);2 用法不对,或盘中不出预测。

**每日自动检查**(macOS 定时任务,周一到周五 15:30 跑 `board check --notify`):

```bash
STOCK_LAB=<内部仓> bash skills/forecast-board/scripts/install_daily_check.sh            # 只打印要装的内容,不写任何文件
STOCK_LAB=<内部仓> bash skills/forecast-board/scripts/install_daily_check.sh --install  # 真装
```

定时任务里写的是这份代码的绝对路径,**要从长期留着的那份代码目录装**,不要从临时的工作目录装。

## 文件在哪

路径都相对 `$STOCK_LAB`(内部仓)。公开仓里不放这些文件。

```
private/config/board.json        看哪几只(stocks)、同行对(peers)、卖出线(sell_lines,可以空)、两只的卖出比例(sell_ratio)、
                                 现金留够几个月的还款(cash_months,不写 = 1)、卖的先后(sell_order,写了就不按比例)、
                                 节假日(holidays_<年>)、手工登记的关键日期(key_dates)、关注的美股(watch_us / watch_sec)……
private/config/local.json        每台机器各一项:{"hosts": {"<主机名>": {"board_out_dir": 页面写到哪, "dashboard_dir": 借款看板目录}}}
private/portfolio/account.json   仓位上限:单票最大可承受亏损、单票硬上限(占净资产的百分比)
data/journal.jsonl               决策日志:每次 forecast 给每只股票记一条,写上存档编号,两边互相指
data/forecasts/
├── <日期>/<时分秒>/             一次预测一个目录,写过就不改
│   ├── manifest.json            代码提交号、工作区干不干净、随机种子(这个文件最后写,有它才算跑完)
│   ├── code.diff  untracked/    当时的未提交改动、没进 git 的代码文件(replay --code run 用它还原)
│   ├── inputs.json              {items: 每项输入, run_meta: 起点、期限、节假日……, missing_required}
│   ├── answers/<层>.json         baseline / system / claude / codex 四份答卷
│   ├── ai/prompt.md  ai/<作者>/  两个 AI 收到的同一份提示词、各自的原始答卷和日志
│   ├── questions.json           这次出的题
│   ├── journal.json             这次在决策日志里记的编号(日志那边写「预测存档 <目录>」,两边互相指)
│   ├── analogs.json             历史同类深跌统计(取不到长日线时没有)
│   └── superseded.json          同一天被新版取代时才有
├── outcomes.jsonl               结算记录(同一题号以最后一行为准)
├── chat_questions.jsonl         add-question 加的题
├── reviews.jsonl                原因概率的确认记录(人确认和 AI 提议都在这里,看 who)
├── improvements.jsonl           「要改的地方」:原因类别、代码文件、状态
├── replays.jsonl                replay 的结果
├── status.json                  每日检查的结果(这个文件整份重写,不是存档)
├── events.json                  update 刷新的财报日历和 SEC 新文件(整份重写;只给它刷新时那次存档用)
└── actions_seen.json            每次画页面记的每只股票的动作和从哪天起(整份重写;「最晚哪天做」靠它)
```

页面写到 `local.json` 里本机的 `board_out_dir`:`看板.html`、`成绩.html`、`看板_手机版.html`
(两页合成一个文件,AirDrop 到手机离线能看)。持股数、现金、每月还款从 `dashboard_dir` 的
`券商数据.js`、`债务数据.js` 只读;超过 7 天没更新,页面顶栏变色,动作改成「先更新持仓」。
卖出、减仓的「最晚哪天做」= 这次存档的起点日和这种动作第一次出现那天里较晚那天之后的第 2 个交易日(第一次出现那天记在 `actions_seen.json`);分批买的最晚日期是买点窗口的最后一天。过了还没做,页面写「已过最晚日期 …,还没做」。

给 AI 的输入包里,账户数据只有持股数和还款数(每月要还多少、融资负债);成本、截图价、净资产、现金不给。

## 代码在哪

`skills/forecast-board/scripts/`,入口是 `board.py`(`tools/board.py` 是它的软链):

| 文件 | 管什么 |
|---|---|
| `board.py` | 命令入口,按子命令分给下面几个 |
| `board_forecast.py` · `board_settle.py` · `board_layers.py` · `board_io.py` | inputs / forecast / update;resolve / check / add-question / confirm / replay;两层的计算;时钟、行情、文件 |
| `inputs.py` | 冻结输入包:每项输入的取数器 |
| `baseline.py` · `system_forecast.py` · `prices.py` · `analogs.py` | 统计底线、系统算法、交易日和后复权价、历史同类深跌 |
| `ai_answer.py` | 两个 AI 作答 |
| `archive.py` | 只追加的存档 |
| `questions.py` · `scoring.py` · `attribution.py` · `replay.py` | 出题和结算、评分、原因概率、重算 |
| `account.py` · `actions.py` · `position_limit.py` | 读借款看板、账户层动作、仓位上限 |
| `board_data.py` · `board_scores.py` · `render.py` · `render_scores.py` · `svg.py` · `templates/` | 页面要的数和页面本身 |
| `judgment.py` · `board_judge.py` · `render_judge.py` | 第一屏:AI 答卷里「怎么看」那一块的骨架和校验;拼「怎么看」「价格可能晃多大」「对照」的数;排版和图 |

## 检查

都不联网、不碰真实数据,在临时目录搭工作台,用合成行情和合成答卷;都登记在 `scripts/check_gates.sh`(3j~3y):

```bash
python3 scripts/check_board_engine.py      # 统计底线、交易日、历史同类情形
python3 scripts/check_board_system.py      # 系统算法
python3 scripts/check_board_store.py       # 输入包、财报日历、SEC 摘句、存档
python3 scripts/check_board_ai.py          # 两个 AI 作答(提示词、校验、重问、思考深度)
python3 scripts/check_board_account.py     # 账户、动作、仓位上限
python3 scripts/check_board_pages.py       # 页面和图(fixture:scripts/fixtures/board/board_data.json)
python3 scripts/check_board_judge.py       # 第一屏:「怎么看」校验和只丢这一块、卖出线那格按规则写、codex 不同意、对照、图
python3 scripts/check_board_cli.py         # board.py 各子命令、定时任务脚本
python3 scripts/check_board_questions.py   # 出题和结算
python3 scripts/check_board_review.py      # 原因概率、重算
python3 scripts/check_board_scoring.py     # 评分、成绩页
python3 scripts/check_board_settle.py      # 结算记录、补记收盘价、错题证据
python3 scripts/check_board_notices.py     # 没取到、没查成、过期的写到页面和 status.json 上
python3 scripts/check_board_page_text.py   # 页面上的字和数从哪来
```

页面改了以后另跑 `design-review` 的 `check_objective.mjs`(页面报错、对比度、手机宽度、图里的字)。

## 还没有的

- **成绩页「沿计算链逐步看」还没有**:营收 → 利润率 → 净利润 → 估值倍数 → 股价 → 动作,
  每一步预测和实际各是多少、偏在哪一步。页面上那一块现在写的是「还没有能沿计算链……逐步核对的题」,
  没有任何一步的预测和实际。
- **看板「见底和反弹」里,每个反弹信号历史上出现时离低点多远,还没有**。现在只写每个信号亮没亮,
  页面上注明「这一版还没算」。
- 两个 AI 写的改主意条件,`check` 只认「一句话里一个本股收盘价位」的写法(如「收盘跌破某价」);
  说均线、指数、利润的,要看连续几天、周线的,只报条数,要人看。
- **重跑(replay)的结果还不用来更新原因概率**:`replays.jsonl` 存下了、成绩页「旧题重跑」也列出来了,
  但程序给错题的原因初值(`attribution`)没读它,要人看过重跑结果再用 `confirm` 改。
- 第一批 3 个月题要到 2027 年 2 月初才到期。在那之前,成绩页「改了以后有没有变准」那一栏没有新题的成绩,
  只有换算进来的旧记录和季度业绩比对。
- 换算进来的旧记录没有冻结的输入包,程序给的原因初值几乎是平均分,原因概率要靠人按证据定。
