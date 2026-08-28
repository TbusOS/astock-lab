---
name: finance-pdf-report
description: >
  出金融 / 投资分析类 PDF 报告的固定版式与工具链。核心=**HTML + playwright 打印,
  不用 md2pdf**(md2pdf 不支持行内粗体和颜色,出来纯黑白)。版式规范:结论前置、
  四个 KPI 封面、语义色(涨绿 788c5d / 跌橙 d97757 / 中性灰 6b6a5f)、
  数据用内联 SVG 画图不用截图、每张表配来源、**末尾必须有逐条数据来源表**。
  含可直接复制的 CSS 模板、html2pdf.mjs 脚本、页码页眉配置、A4 分页控制、
  交付前的逐页渲染自检(不能只看"生成成功")。
  触发词:金融 PDF / 投资报告 PDF / 分析报告 PDF / 出 PDF / 报告版式 /
  持仓报告 / 研报格式 / html 转 pdf / playwright pdf / md2pdf 黑白 /
  报告要好看 / 带图表的 PDF。
  **边界**:只管「金融分析报告怎么排版成 PDF」;不管分析方法(那是
  stock-analysis-workflow)、不管取数(astock-quote)、不管通用网页设计(anthropic-design)。
---

# finance-pdf-report —— 金融分析 PDF 的固定版式

## 为什么不用 md2pdf

`md-to-pdf` skill 的 `md2pdf.py` 用 PyMuPDF Story 渲染,**单字体、无颜色、
行内 `**粗体**` 渲染成常规字重**。金融报告靠粗体和颜色区分"涨/跌/警告",
黑白版等于把最重要的一层信息丢了。实测对比(2026-08-27,同一份 13.6K 报告):

| | md2pdf | HTML + playwright |
|---|---|---|
| 行内粗体 | ❌ 渲染成常规 | ✅ |
| 颜色 | ❌ 全黑白 | ✅ 语义色 |
| SVG 图表 | ❌ 不支持 | ✅ 内联 SVG 原生渲染 |
| 页眉页脚页码 | 只有页码 | ✅ 可自定义 |
| 体积 | 13.1 MB(嵌整套 CJK 字体) | **1.4 MB** |

**结论:金融报告一律走 HTML → playwright。**md2pdf 只适合纯文本备份。

---

## 一　工具链

```bash
# playwright 在 sky-skills 的 node_modules 里,脚本必须在那个目录跑
cd ~/claude-tools/sky-skills
node <本 skill>/scripts/html2pdf.mjs <报告.html> <输出.pdf> ["页脚文字"]
```

脚本见 `scripts/html2pdf.mjs`。它做三件事:
`waitUntil:'networkidle'` 等图表渲染完 → `emulateMedia('print')` 应用 `@page` 规则
→ `printBackground:true` 保留底色(**不加这个所有背景色会丢**)。

---

## 二　版式规范(照抄)

### 结构顺序 —— 结论必须在第一页

```
封面区    标签(报告类型·日期) + 大标题 + 一行说明 + 四个 KPI 卡
结论块    橙色左边框 + 浅橙底,一句话结论 + 两三行理由     ← 第一屏就要看到
1 操作建议  三档表(现在/加仓/减仓)+ 触发判据表
2 证据     领先指标优先,每条配图
3 风险     变差的部分,不藏
4 基本面估值 滞后指标放后面当验证
5 技术位
6 数据完备性 诚实说缺什么
末尾       **逐条数据来源表**(必须有)
```

**读者最想知道"接下来怎么做",不是"数据是什么"。**把结论埋在第 5 页 = 失败。

### 语义色(固定,不要自创)

| 用途 | 色值 | 场景 |
|---|---|---|
| 主强调 / 警示 / 你的成本线 | `#d97757` 深 `#c2613f` | 标题数字、结论块、亏损 |
| 正面 / 增长 | `#788c5d` 深 `#5d7045` | 同比为正、目标价上行 |
| 中性 / 次要 | `#6b6a5f` 深 `#4a4a42` | 说明文字、无变化 |
| 提示 / 待观察 | `#c9913f` 深 `#8a5a2a` | 名不副实、限流敏感 |
| 数据 / 分类 | `#6a9bcc` 深 `#4a7bab` | 图例第三类 |
| 墨(骨架) | `#141413` | 正文、主体柱 |

**红涨绿跌是 A 股习惯,但报告里用"橙=风险/亏损、绿=正面"更清楚**,
因为报告要同时表达"涨跌"和"好坏",两者不总是一致(股价跌但基本面好)。

### 字号与间距(A4,`font-size:11.5px` 基准)

| 元素 | 字号 | 说明 |
|---|---|---|
| 大标题 | 27px / 600 | 封面唯一 |
| 章节 h2 | 18px / 600 + 下边框 | 前缀橙色序号 |
| 小节 h3 | 13.5px / 600 | |
| 正文 | 11.5px / 1.65 | |
| 表格 | 10.5px,表头 10px | 表格永远比正文小一号 |
| 图内 SVG 文字 | ≥ 9px | 小于 9px 打印后看不清 |
| figcaption | 9.5px 灰 | **每张图必须有** |

### 图表规则

- **用内联 SVG,不要截图**。矢量在 PDF 里无损,截图会糊。
- **每张图配 figcaption**,一句话说这张图证明了什么,不是重复标题。
- 负值柱的标签要放在**柱子外侧**,否则被柱子盖住(实测踩过)。
- 堆叠柱图必须有图例(色块 + 名称),放右上角。
- 数字直接标在柱子上方,不要让读者去比对坐标轴。

### 分页控制

```css
.pb    { page-break-before: always; }   /* 强制新页 */
.avoid { page-break-inside: avoid; }    /* 表格/图不要被腰斩 */
```

**每张表和每张图都加 `.avoid`** —— 被分页腰斩的表格是最常见的翻车。

---

## 三　CSS 模板

完整可复制模板在 `templates/report.css`;HTML 骨架在 `templates/report.html`
(骨架里带一份最小示例报告 —— 封面 / 结论块 / 三档操作表 / SVG 图 / 数据来源表,
`node scripts/html2pdf.mjs templates/report.html /tmp/x.pdf` 直接就能渲出来看)。
关键点:

```css
@page { size: A4; margin: 14mm 12mm; }
body  { background:#faf9f5; color:#141413; font-size:11.5px; line-height:1.65;
        font-family:"Noto Sans SC","Source Han Sans SC","PingFang SC",sans-serif; }
.wrap { max-width: 186mm; margin:0 auto; }   /* A4 可用宽度 */
```

配色和字体直接复用 skill `anthropic-design` 的 `assets/anthropic.css`,
把它 copy 到报告同级的 `assets/` 下,HTML 里相对引用 —— 这样报告文件夹整个拷走仍能渲染。

---

## 四　数据来源表(强制)

**每份金融报告末尾必须有逐条数据来源表。**没有来源的数字不可复核,
读者(包括三个月后的你自己)无法判断该不该信。

```markdown
| 数据 | 来源 | 时效 | 工具 |
|---|---|---|---|
| 行情 / 技术位 | 新浪 hq.sinajs.cn | 实时 | astock |
| 财务 / 股东户数 / 龙虎榜 | 东财 datacenter-web | 中报 08-25 | efdata / preport |
| 估值历史分位 | baostock | 2020 至今 | hcheck |
| 券商一致预期 | 同花顺 | 19 家机构 | consensus |
| 机构评级变动 | 巨潮 | 近 30 天 | consensus |
| 北美云厂 Capex | **SEC EDGAR XBRL 官方** | 至 2026Q2 | capex |
| 云厂下期指引 | **SEC 8-K EX-99 新闻稿原文** | 最新一份 | capex --guidance |
| 目标价 / 产业进展 | 网页搜索 | 手工 | WebSearch |
```

**时效那一列不能省** —— 「股东户数截止 06-30」和「行情实时」是完全不同的证据强度。

正文里每张表的下方也要标来源,不要只在末尾给一次。

---

## 五　交付前自检(不能只看"生成成功")

`md2pdf` 会打印 "PDF generated" 但内容可能是空页或断字。**必须渲染出来逐页看**:

需要 `pymupdf`(`pip install pymupdf`),`scripts/check_pdf.py` 也靠它:

```bash
python3 - <<'EOF'
import fitz
d = fitz.open("报告.pdf")
print(f"页数 {d.page_count}  书签 {len(d.get_toc())}")
for i in range(d.page_count):
    t = d[i].get_text().strip()
    d[i].get_pixmap(dpi=95).save(f"/tmp/p{i+1}.png")
    print(f"  p{i+1}: {len(t):>4} 字符  {t.split(chr(10))[0][:40] if t else '(空页!)'}")
EOF
```

然后**用 Read 工具真的看几页图**,重点查:

- [ ] 无空页、无 `(空页!)`
- [ ] 表格没被分页腰斩
- [ ] 图表标签没被柱子盖住(负值柱尤其容易)
- [ ] 中文没有方框 / 乱码
- [ ] 颜色出来了(没有 `printBackground` 会全白)
- [ ] 页码正确
- [ ] **末尾有数据来源表**

另外跑禁黑话闸:

```bash
python3 ~/.claude/skills/tech-writing-gate/scripts/check_buzzwords.py 报告.html
```

---

## 六　常见翻车(都踩过)

| 症状 | 原因 | 修 |
|---|---|---|
| 背景色全白 | 没加 `printBackground:true` | 加上 |
| 图表标签消失 | 负值柱盖住了左侧标签 | 标签移到柱子外侧 |
| 中文方框 | 字体栈没有 CJK | `"Noto Sans SC","PingFang SC"` 打头 |
| 表格被腰斩 | 没加 `page-break-inside: avoid` | 每张表都加 |
| 13 MB 巨大 PDF | 用 md2pdf 嵌了整套 CJK 字体 | 改用 playwright |
| 行内粗体不加粗 | md2pdf 单字体 | 改用 playwright |
| CSS 没生效 | HTML 引用的相对路径不对 | assets 与报告同级,或用绝对路径 |
| 图表模糊 | 用了截图 | 改内联 SVG |

---

相关 skill:`anthropic-design`(配色与 CSS 来源)· `stock-analysis-workflow`(报告内容该写什么)
· `astock-quote`(数据从哪来)· `tech-writing-gate`(发布前禁黑话闸)。
