/* ===================================================================
 *  合成数据 —— scripts/check_board_account.py 用
 * ===================================================================
 *  写法照抄借款看板的 债务数据.js(window.XXX_SEED = { … }、键不带引号、有尾逗号),
 *  数字都是编的。前两笔是检查里那道「合成月供」的题:
 *    分期每月 1000、3 期、首期 2026-11-15;先息后本每月息 300、本金 5000、2027-01-15 到期
 *  后两笔不该算进月供:一笔已还清的一次性借款、一笔还没到账的额度。
 *
 *  字段说明：
 *    category: "installment"=分期  "interest"=先息后本  "lump"=一次性  "pending"=待到账
 *    分期(installment) 用：principal本金 / periods期数 / monthly月供 / dueDay每月几号 / startDate首次还款日
 *    先息后本(interest) 用：principal本金 / monthInterest每月利息 / interestDay每月还息日 /
 *                          startDate首次还息日 / dueDate到期还本日
 *    一次性(lump)       用：principal本金 / dueDate应还日
 *    待到账(pending)    用：principal额度
 * =================================================================== */

window.DEBT_SEED = {
  version: 2,
  cashInBank: 0,              // 银行卡里留的现金
  cashSpent: 0,
  todo: [],
  items: [
    {id:1, name:"样例分期", channel:"样例银行", category:"installment", principal:2900, periods:3, monthly:1000.00, dueDay:15, startDate:"2026-11-15", autoPaid:true, paidPeriods:0, cleared:false, note:""},
    {id:2, name:"样例先息后本", channel:"样例平台", category:"interest", principal:5000, monthInterest:300.00, interestDay:15, startDate:"2026-08-15", dueDate:"2027-01-15", cleared:false, note:""},
    {id:3, name:"样例已还清", channel:"样例平台", category:"lump", principal:800, dueDate:"2026-12-09", cleared:true, note:"已还清,不该算进去"},
    {id:4, name:"样例待到账", channel:"样例银行", category:"pending", principal:3000, cleared:false, note:"还没到账,不用还"},
  ]
};
