---
name: data-sources
description: >
  A 股分析的**数据源层** —— 所有取数、可达性探测、落盘、出数据文档的代码都在这里。
  三件事:① `probe/` 逐条真调探测每个源还通不通(清单由实测生成,不手写);
  ② `fetch/` 把数据抓下来按类别落盘,每个文件带来源/抓取时间/成败的信封;
  ③ `digest/` 把落盘数据渲染成给人读的明细文档(MD + PDF),能直接发给别人。
  含海外上下游(云厂 capex / 光模块同业 / 半导体设备 / 油服)—— 那是 A 股的领先指标不是背景资料。
  触发词:数据源 / 取数 / 抓数据 / 数据源清单 / 数据全不全 / 缺什么数据 / 数据源可达性 /
  probe_all_sources / fetch_all / data_digest / 数据落盘 / 数据目录 / 海外同业数据 /
  云厂 capex / 一致预期 / 机构调研 / 业绩预告 / 评级变动。
  **边界**:只管「把数据拿到、存好、说清楚有什么」;不管怎么分析(stock-analysis-workflow)、
  不管怎么排版投资报告(finance-pdf-report)、不管外部研报 PDF 怎么解析(extern-research-ingest)。
---

# data-sources —— 数据源层

## 一句话

**数据源清单不能手写,必须由每次真调生成。**

2026-09-01 踩过:SKILL.md 和报告里记着「拿不到」的有三条是错的
(Motley Fool 电话会纪要 / roic.ai / stockanalysis.com 实测全是 200),
而这三条正好卡着「云厂下期 capex 指引」这个最领先的指标。凭记忆维护清单必然过期。

## 目录

```
skills/data-sources/scripts/
├── probe/     探可达性 —— 先知道什么还能拿
│   ├── probe_all_sources.py   41 条探针,--json / --markdown 出清单
│   ├── probe_sources.py       老的分档探测(仍在用)
│   ├── check_sources.sh       快速三源连通性
│   └── net_probe.sh           网络层排查
├── fetch/     取数 —— 拿到并落盘
│   ├── fetch_all.py           一条命令抓全,按类别落 data/raw/
│   ├── astock.py              行情/五档/逐笔/日线(新浪 + 腾讯)
│   ├── efdata.py              东财报表类(股东户数/龙虎榜/全市场业绩)
│   ├── consensus.py           一致预期 + 评级变动
│   ├── research.py            券商研报(列表 + PDF 全文)
│   ├── capex.py               SEC EDGAR XBRL 云厂/油服资本开支
│   ├── commodity.py           油价(新浪外盘期货)
│   ├── rigcount.py            北美钻机数
│   └── fx.py                  人民币中间价(CFETS)
└── digest/    出文档 —— 让人看得懂手上有什么
    └── data_digest.py         data/raw/ → 数据源明细 MD + PDF
```

`tools/` 下是这些脚本的**扁平软链索引**。`$LAB/tools/x.py` 这个路径在文档里被引用
几十次,所以索引保持扁平、脚本可以随便挪目录 —— 挪完重建软链即可。

## 三步工作流

```bash
export SEC_UA='astock-lab research <你的邮箱>'   # SEC 强制要求,缺了一律 403

# ① 先探:哪些源还通
$VENV/bin/python tools/probe_all_sources.py --json /tmp/s.json --markdown /tmp/s.md
#    退出码 0 = 必需源全通;1 = 有必需源不通

# ② 再抓:按类别落盘
$VENV/bin/python tools/fetch_all.py --codes 300502,300308 --peers --macro
$VENV/bin/python tools/fetch_all.py --transcripts <Motley Fool URL> ...

# ③ 出文档:给人看的
$VENV/bin/python tools/data_digest.py --code 300502 --overseas --pdf
```

## 落盘结构

```
data/raw/
├── quotes/ financials/ forecast/ consensus/ ratings/
├── surveys/ announcements/ research/ chips/     ← A 股,按 <code>/<date>.json
├── overseas/ <ticker>/<date>.json               ← 海外上下游
├── transcripts/ <会议>/<date>.txt               ← 电话会纪要正文
├── macro/ <date>-<项>.json
└── meta/ <date>.json                            ← 本次抓取的健康汇总
```

每个文件是**信封**不是裸数据:

```json
{"source":"baostock query_forecast_report","url":"...","fetched_at":"2026-09-01T23:42:11",
 "params":{...},"ok":true,"rows":4,"error":null,"data":[...]}
```

时效、出处、成败因此是免费得到的 —— 不用另建 freshness 表,
也不会出现「文件在但不知道是哪天哪个源抓的」。

## 硬规矩

| 规矩 | 为什么 |
|---|---|
| **清单由实测生成,不手写** | 手写的必然过期,而且过期时**看起来是对的** |
| **抓失败的条目留在盘上,标 `ok:false`** | 静默丢弃会让下游以为这类数据根本不存在。「今天没抓到」和「不存在」对决策的含义完全相反 |
| **每条带 `fetched_at`** | 股东户数截止 6-30 和实时价并排而不标日期,读的人会当成同时点事实 —— 2026-09-01 的报告就是这么错的 |
| **加新源:先加探针,再写取数** | 顺序反了就会出现「脚本里写着能拿,实际早拿不到」 |
| **海外是领先指标不是背景** | 云厂 capex 决定光模块,油服 capex 与钻机数决定油气设服,前道设备商决定半导体设备 |
| **两套一致预期都抓** | 同花顺只有境内;Yahoo 池里含外资行(实测新易盛 2026E 营收下沿 544.3 亿正是高盛那份的数)。**分歧本身是信号** |
| **评级回看 90 天不是 30 天** | 高盛 2026-07-20 把新易盛目标价 841→633(−25%),落在 30 天窗外,我们的报告写成「零下调」 |

## 已知的坑

1. **macOS 上 `requests` 会读系统代理**(经 `_scproxy`,与环境变量无关),`os.environ.pop` 挡不住;`curl`/`git`/`ssh` 则不读。
2. **Clash TUN 在 L3 路由层截流**,`os.environ.pop` 是应用层,治不了。换机器要先在 Clash 规则层放行国内财经域名,而 Yahoo/SEC/Motley Fool 反而要走代理出海。
3. **SEC EDGAR 的 UA 必须含联系邮箱**,否则 403,云厂 capex 整块废掉。
4. **CFETS `pageSize ≥ 100` 返回 403** 是分页超限不是被封。
5. **akshare / efinance 内部很多调用不带 timeout**,进程级 `socket.setdefaulttimeout()` 是唯一可靠兜底,否则挂死在 CLOSE_WAIT。
6. **东财要按主机分**:`datacenter-web`(报表类)稳定;`push2`/`push2his`(行情与资金流)时通时不通。同域名两种行为。
7. **复权基准要对齐**:目标价/成本价与日线必须换算到同一基准。用 `query_adjust_factor`。

## 完整清单

`docs/DATA-SOURCES.md`(+ `.pdf`)—— 41 条逐条实测,每条带官方链接、用途、领先/滞后属性,
以及**确认拿不到的**那些(附证据日期,别再重复试)。
