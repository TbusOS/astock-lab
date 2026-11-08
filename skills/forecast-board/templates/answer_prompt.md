# 股票预测作答

你要给 {{N}} 只 A 股股票({{CODES}})作一次概率预测。下面先是这次能用的全部材料(一份冻结的输入包),后面是题目、规定和答卷骨架。

<input_package>
{{PACKAGE_JSON}}
</input_package>

关于上面的输入包:

- inputs 里每项带来源(source)、数据截至哪天(as_of)、取数结果(status:ok 取到 · partial 只取到一部分 · stale 这次没取到、沿用上次的值 · failed 取不到)。
- 日线只留了最近 250 个收盘。prices.<代码> 里 hfq 是后复权、qfq 是前复权(前复权的最后一根就是 as_of 那天的实际收盘价)。
- missing_required 是这次该有、但缺了的数据;gap: 开头的是还没有取数工具的指标。
- account 是持股数、还款和现金:month_need 这个月要还的钱、schedule 往后每月要还的钱、margin_debt 融资负债、cash 现在能取出来的现金、free_cash 用现金把从这个月到 free_cash_until 那个月(含这个月)要还的钱都留出来以后还剩多少(负数就是到那个月底为止还差这么多)。为 null 表示这次没有提供。拿还款当卖出理由时,要分清是这个月的现金不够(cash < month_need),还是后面几个月的缺口(free_cash < 0)。research_notes 是查证笔记,为 null 表示没有。
- baseline、system 是两层参考预测,说明见第四节。

## 一、硬规定(不合格的答卷会被退回重答)

1. 每个数都必须给。不许留空、不许写 null(只有 action.shares、action.amount 不适用时写 null)、不许说「判断不了」;拿不准也要给一个你认为最接近的概率。
2. 概率一律是 0~100 的整数,意思是「100 种可能的走法里有几种」。
3. 每组分类概率(bottom.month_probs、bottom.price_bins、buy.month_probs)各自加起来正好 100。
4. 类别必须一字不差地用给定的:月份键、价格分段的比值、"later",一个不多、一个不少。
5. 分位价 q 的 10 / 25 / 50 / 75 / 90 要严格递增。
6. 只用输入包里的材料作答,不联网、不查别的资料。另一个 AI 拿到的是同一份材料、同一份提示词,两边要能公平比较。
7. 只输出一个 JSON 对象,结构照第五节的骨架;JSON 前后不要写任何别的字。

## 二、时间和价格

- as_of = {{AS_OF}}:输入包里最后一个收盘价的日期。
- start = {{START}}:预测期的第一个交易日(as_of 的下一个交易日)。下面说的「第 N 个交易日」从 start 数起,start 是第 1 个。交易日按创业板指的交易日数。
- 每只股票的 start_close(在输入包里,第四节也列了)是 as_of 那天的收盘价。答卷里所有绝对价格(q 分位价、buy.zones 的价位)都用和 start_close 同一种价。

## 三、每道题问什么

每只股票答下面五块:

- horizons."63" / "126" / "250":第 63、126、250 个交易日收盘时——
  - p_up:收盘价高于 start_close 的概率(持平算不涨)。
  - q:那天收盘价的 10 / 25 / 50 / 75 / 90 分位价。
- bottom:as_of 收盘加上往后第 1~{{BOTTOM_WINDOW}} 个交易日的收盘里,最低的那一个——
  - month_probs:最低收盘出现在哪个月。后面每天都比 start_close 高(起点就是最低)的,算第一个月。
  - price_bins:最低收盘 ÷ start_close 落在哪一段,每段写 [下限比值, 上限比值, 概率]。前几段含下限、不含上限,最后一段 [0.9, 1.0] 两头都含(起点就是最低时比值正好是 1.0)。第四节给了每段对应的价位,只是方便看;答卷里写比值。
- buy:
  - month_probs:「{{BUY_EVENT}}」第一次成立落在哪个月,只看第 1~{{BUY_HORIZON}} 个交易日;这么多天里都没成立的记 "later"。
  - zones:几个候选买入价,每个写 [价格, 概率]。概率 = 从 start 到第 {{ZONE_DAYS}} 个交易日(含)之间,有某天收盘价 ≤ 这个价的概率。价越低,概率不能越高。至少给 3 个价位。
  - conditions:你认为买入前还要看到的条件,写成能检查的句子(这一项不打分)。
- action:你建议现在怎么做。
  - kind:sell 卖出 · trim 减仓 · hold 持有 · wait 等待(手里没有,先不买)· buy_batches 分批买入。
  - shares:涉及多少股(整数),不适用写 null;amount:涉及多少钱(元),不适用写 null。这两个键都要写出来。
  - deadline:这个动作最晚哪天做完,YYYY-MM-DD。
  - why:理由。
  - change_if:改主意的条件,至少一条,每条 {"if": 能检查的条件, "then": 改成什么动作}。
- evidence:support 支持你判断的依据 · against 反对的依据 · unverified 用到了但没核实的。每条一句话,写明来自输入包的哪一项。
- judgment:「怎么看」—— 按今天的事实做的判断,会放在看板第一屏,回答三件事:手上的该不该卖、要不要买回、这个行业还值不值得待。
  - fact:一两句已知事实(只写事实,不写推测);view:据此做的判断(以「据此判断:」开头)。
  - holding / rebuy / industry:三件事各一个 {"pick", "answer", "when", "why"}。pick 从给定的几个词里选一个
    (holding:sell 卖 · trim 减一部分 · hold 拿着 · none 手里没有;rebuy:buy 买 · wait 先等等 · no 不买;
    industry:stay 值得待 · watch 值得跟踪 · leave 不值得待);answer 是一句短答案;
    when 是一句适用条件(这个答案在什么前提下成立,30 字以内,会和答案并排露在第一屏);why 写完整理由(展开才看)。
  - scenarios:接下来 2~4 种情况,每种 {"label": "A"…, "p", "what", "low_when", "low_price", "how"}。
    p 是你按今天的事实给的主观概率(0~100 的整数,几种加起来正好 100);what 说这种情况是什么样;
    low_when、low_price 是到复核日(review)为止,这段时间的最低收盘价大约在什么时候、多少钱
    (low_price 写一个数,或 [下限, 上限],和 start_close 同一种价);how 说价位怎么估出来的。
  - change_if:看到什么就要重新检查这个判断,写成以后能核对的条件,口吻用「出现……会削弱……的判断,要重新检查……」,
    不要写成「这就说明……」那种证明口吻 —— 一个新事实只是让某种情况更可能或更不可能,不是证明。
  - key_dates:接下来能看出是哪种情况的日子,每条 {"date": "YYYY-MM-DD" 或 "YYYY-MM", "what", "why"},至少一条在 as_of 之后。
  - facts:判断用到的事实,至少 3 条,每条 {"kind", "text", "source"}。kind 从 需求 / 供给 / 情绪 / 资金 / 估值 / 公司 / 其他 里选;
    source 写这条出自输入包里哪一项(inputs 的键名,或 research_notes 查证笔记、account、rules)。**事实只能来自输入包**,没有出处的不要写。
  - review:到哪天核对这个判断,YYYY-MM-DD,要在 as_of 之后。

why、change_if、conditions、evidence、judgment 这些文字会原样放到用户看的页面上:用大白话写,不用自己造的简称和行话;专业词第一次出现时用半句话说明它是什么。
judgment 里另外注意:不写「改判」「往 C 改」这类说法,写「提高情况 C 的可能」;不写「中间五成」,写「一半的结果落在……」;
「× 7」「× 8」这类估价算式只写在 scenarios 的 how 里,answer、when、what、low_when 里不写。

## 四、已定规则和两层参考

已定规则(用户确认过的,即输入包里的 rules):

{{RULES}}

规则里的键:sell_lines 卖出线(收盘跌破就全卖,空就是没有);cash_months 可取现金要留够从这个月起几个月的还款,不够的差额靠卖股票补;sell_order 卖的先后(前面那只卖够或卖光才轮到后面);sell_ratio 没写 sell_order 时按这个比例分,有闲钱买入时也按它分;monthly_cash_need_fallback 借款数据读不出来时每月按多少算。没出现的键就是没有这条规则。

不同意这些规则时,action 仍然照你的判断给出,并在 why 里写「建议改规则:……,理由……」。

两层参考(输入包里的 baseline、system):

- 统计底线(baseline):只按最近 120 个交易日的波动幅度做随机模拟,不带方向,不是看涨看跌的判断;它的「涨的概率」接近 50 是设计如此。
- 系统算法(system):几个领先指标给统计底线加了一个方向。这套方法尚未验证,只作参考。
- 输入包里这两层的价格以各自的 start_price 为起点,可能是后复权价,和 start_close 不是同一个数;下面的表已经换算成 start_close 的价。
- 你可以不同意参考层,不同意就在 why 或 evidence 里写理由。

{{STOCK_TABLES}}

## 五、答卷骨架

把每个 null 换成你的数(action.shares、action.amount 不适用的留 null),把「…」和 kind 那一行换成你的内容;键名、月份、价格分段的比值都不要改:

```json
{{SKELETON}}
```

再说一遍:每个数都必须给,概率是 0~100 的整数,每组分类加起来正好 100,只输出一个 JSON 对象。
