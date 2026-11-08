/* ===================================================================
 *  合成数据 —— scripts/check_board_account.py 用
 * ===================================================================
 *  写法照抄借款看板的 券商数据.js(window.XXX_SEED = { … }、键不带引号、有尾逗号),
 *  数字都是编的,不是任何人的真实持仓。
 *  故意把新的一笔放在旧的前面:读的人要按 date 取最新,不能取最后一个。
 *
 *  字段说明(单位元):
 *    date        截图日期
 *    totalAssets 总资产（= 股票市值 + 现金）
 *    marketValue 股票市值
 *    marginDebt  融资负债（本金 + 利息 + 费用）
 *    netAssets   净资产（= 总资产 − 融资负债）
 *    ratio       维持担保比例（%）
 *    cashOut     可取现金
 *    holdings    持仓：name 名称 / code 代码 / shares 股数 / price 截图价 / cost 成本价
 * =================================================================== */

window.BROKER_SEED = {
  version: 3,
  broker: "样例证券",
  liquidationRatio: 130,
  warnRatio: 150,
  targetRatio: 300,
  snapshots: [
    {date:"2026-10-28", totalAssets:153000.00, marketValue:150000.00, marginDebt:20000.00, netAssets:133000.00, ratio:765.00, cashOut:1800.00,
     holdings:[
       {name:"样例甲", code:"300308", shares:300, price:300.00, cost:400.00},
       {name:"样例乙", code:"300502", shares:400, price:150.00, cost:250.00},
     ],
     note:"合成:新的一笔"},
    {date:"2026-10-15", totalAssets:200000.00, marketValue:200000.00, marginDebt:50000.00, netAssets:150000.00, ratio:400.00, cashOut:0,
     holdings:[
       {name:"样例甲", code:"300308", shares:500, price:250.00, cost:400.00},
       {name:"样例乙", code:"300502", shares:500, price:150.00, cost:250.00},
     ],
     note:"合成:旧的一笔"},
  ]
};
