#!/usr/bin/env python3
"""md2html —— 把本仓生成的 markdown 报告渲染成带版式的 HTML。

用法:
    python3 md2html.py 报告.md 报告.html ["副标题"]
    python3 -c "import md2html; html = md2html.render(md_text, title='…')"

为什么不用现成的 markdown 库:
    ① 公开仓要**零额外依赖**能跑(装 akshare 那一堆已经够沉了)
    ② 我们**完全控制输入** —— markdown 是本仓自己 say() 出来的,
       只有 h1/h2/h3、表格、引用、粗体、代码块、列表这几种,不需要通用解析器
    ③ 需要往里塞语义色和 .avoid 分页控制,通用库反而要事后再改一遍 DOM

语义色跟 templates/report.css 一致:
    橙 = 风险/亏损/警示   绿 = 正面/增长   灰 = 中性   金 = 待观察
表格里带 +N% / -N% 的单元格会自动上色;🔴 ⚠️ 开头的引用块自动变风险色。
"""

from __future__ import annotations

import html as _html
import re
import sys
from pathlib import Path

CSS = """
:root{--ink:#141413;--paper:#faf9f5;--line:#e6e4dd;--accent:#d97757;--accent-d:#c2613f;
--good:#788c5d;--good-d:#5d7045;--muted:#6b6a5f;--muted-d:#4a4a42;--warn:#c9913f;
--warn-d:#8a5a2a;--data:#6a9bcc}
@page{size:A4;margin:14mm 12mm}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-size:11.5px;line-height:1.65;margin:0;
font-family:"Noto Sans SC","Source Han Sans SC","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:186mm;margin:0 auto;padding:18px 14px 28px}
h1{font-size:27px;font-weight:600;margin:4px 0 6px;line-height:1.25}
h2{font-size:18px;font-weight:600;border-bottom:1px solid var(--line);
padding-bottom:5px;margin:22px 0 10px}
h2 .n{color:var(--accent);margin-right:6px}
h3{font-size:13.5px;font-weight:600;margin:14px 0 6px}
p{margin:6px 0}
table{width:100%;border-collapse:collapse;font-size:10.5px;margin:8px 0 10px;
page-break-inside:avoid}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{background:#f2f0e9;font-size:10px;font-weight:600}
tr:nth-child(even) td{background:#fbfaf6}
.up{color:var(--good-d);font-weight:600}
.down{color:var(--accent-d);font-weight:600}
blockquote{margin:8px 0;padding:7px 11px;border-left:2px solid var(--muted);
background:#f4f2ec;color:var(--muted-d);font-size:10.5px;page-break-inside:avoid}
blockquote.risk{border-color:var(--accent);background:#fdf3ee;color:var(--accent-d)}
blockquote.warn{border-color:var(--warn);background:#fbf5ea;color:var(--warn-d)}
blockquote.good{border-color:var(--good);background:#f2f5ec;color:var(--good-d)}
pre{background:#f2f0e9;border:1px solid var(--line);border-radius:4px;padding:9px 11px;
overflow-x:auto;font-size:10px;page-break-inside:avoid}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px}
pre code{font-size:inherit}
ul,ol{margin:6px 0;padding-left:20px}
li{margin:3px 0}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.meta{color:var(--muted);font-size:10px;margin:0 0 14px}
.foot{margin-top:22px;padding-top:8px;border-top:1px solid var(--line);
font-size:9.5px;color:var(--muted)}
a{color:var(--data);text-decoration:none}
"""

# +12.3% / -4.5% / +1,234 户 —— 表格单元格里的涨跌自动上色
_NUM = re.compile(r"^\*{0,2}([+-])[\d,]+(?:\.\d+)?\s*(?:%|pp|户|亿|万)?\*{0,2}$")


def _inline(t: str) -> str:
    """行内:粗体、代码、链接。先转义再放标签,避免内容里的 < > 破坏结构。"""
    t = _html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


# ⚠ 「正数=绿」这条规则对某些行是**反的**:「回本需涨 +22%」不是好消息,
#   「距解禁 +30 天」也不是。这类标签下的数字不上色 ——
#   宁可不着色,也不要把坏消息染成绿的(2026-08-28 实测踩到)。
_NO_COLOR_LABEL = re.compile(r"回本|需涨|需跌|距|解禁|成本|天数|期限")


def _cell(t: str, label: str = "") -> str:
    """表格单元格:数值涨跌上色。用**原始文本**判断,不是渲染后的。

    label 是本行第一列,用来判断「正数到底是好是坏」—— 光看符号会出错。
    """
    raw = t.strip()
    cls = ""
    m = _NUM.match(raw)
    if m and not _NO_COLOR_LABEL.search(label):
        cls = ' class="up"' if m.group(1) == "+" else ' class="down"'
    return f"<td{cls}>{_inline(raw)}</td>"


def _quote_class(text: str) -> str:
    if text.startswith(("🔴", "❌", "⚠️", "⚠")):
        return " class=\"risk\"" if text.startswith(("🔴", "❌")) else " class=\"warn\""
    if text.startswith(("✅", "🔺")):
        return " class=\"good\""
    return ""


def render(md: str, title: str = "报告", subtitle: str = "") -> str:
    lines = md.split("\n")
    out, i = [], 0
    n = len(lines)

    while i < n:
        ln = lines[i]

        # 代码块
        if ln.startswith("```"):
            j = i + 1
            buf = []
            while j < n and not lines[j].startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append("<pre><code>" + _html.escape("\n".join(buf)) + "</code></pre>")
            i = j + 1
            continue

        # 表格:当前行是 | … |,下一行是分隔行
        if ln.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            j = i + 2
            body = []
            while j < n and lines[j].startswith("|"):
                body.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            t = ["<table><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr>"]
            for row in body:
                lab = row[0] if row else ""
                t.append("<tr>" + "".join(_cell(c, lab) for c in row) + "</tr>")
            t.append("</table>")
            out.append("".join(t))
            i = j
            continue

        # 引用块(连续多行合成一段)
        if ln.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            text = " ".join(x for x in buf if x)
            out.append(f"<blockquote{_quote_class(text)}>{_inline(text)}</blockquote>")
            continue

        # 列表
        if re.match(r"^\s*[-*] ", ln):
            buf = []
            while i < n and re.match(r"^\s*[-*] ", lines[i]):
                buf.append(re.sub(r"^\s*[-*] ", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in buf) + "</ul>")
            continue

        # 标题。h2 的前导数字染成橙色序号
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lvl, txt = len(m.group(1)), m.group(2)
            if lvl == 2:
                mm = re.match(r"^(\d+)(.*)$", txt)
                inner = (f'<span class="n">{mm.group(1)}</span>{_inline(mm.group(2))}'
                         if mm else _inline(txt))
                out.append(f"<h2>{inner}</h2>")
            else:
                out.append(f"<h{lvl}>{_inline(txt)}</h{lvl}>")
            i += 1
            continue

        if ln.strip() == "---":
            out.append("<hr>")
            i += 1
            continue

        if ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1

    sub = f'<p class="meta">{_html.escape(subtitle)}</p>' if subtitle else ""
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<meta charset=\"utf-8\">\n"
        f"<title>{_html.escape(title)}</title>\n<style>{CSS}</style>\n"
        f'<body><div class="wrap">{sub}\n' + "\n".join(out) +
        '\n<div class="foot">本报告是机械判据，不是投资建议。'
        '所有决策和后果都是你自己的。</div></div></body></html>\n'
    )


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    sub = sys.argv[3] if len(sys.argv) > 3 else ""
    md = src.read_text(encoding="utf-8")
    t = next((l.lstrip("# ").strip() for l in md.split("\n") if l.startswith("# ")), src.stem)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(md, title=t, subtitle=sub), encoding="utf-8")
    print(f"已写入 {dst}（{dst.stat().st_size/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
