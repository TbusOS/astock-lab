#!/usr/bin/env python3
"""金融 PDF 交付前自检 —— 查空页、渲染每页成 PNG、查数据来源表。

用法:  python3 check_pdf.py 报告.pdf [输出目录]
退出码:0 = 可交付;1 = 有问题
"""
import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    sys.exit("需要 pymupdf:pip install pymupdf")

if len(sys.argv) < 2:
    sys.exit(__doc__)
pdf = Path(sys.argv[1])
outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else pdf.parent / "_check"
outdir.mkdir(parents=True, exist_ok=True)

d = fitz.open(pdf)
size_mb = pdf.stat().st_size / 1048576
print(f"文件 {pdf.name}　{size_mb:.2f} MB　{d.page_count} 页　"
      f"{len(d.get_toc())} 书签")
print()

bad = 0
alltext = ""
for i in range(d.page_count):
    t = d[i].get_text().strip()
    alltext += t
    png = outdir / f"p{i + 1}.png"
    d[i].get_pixmap(dpi=95).save(png)
    first = t.split("\n")[0][:44] if t else "(空页!)"
    flag = ""
    if not t:
        flag = "  ← 空页"
        bad += 1
    elif len(t) < 80:
        flag = "  ← 内容极少,确认是否正常"
    print(f"  p{i + 1}: {len(t):>5} 字符  {first}{flag}")

print()
if size_mb > 8:
    print(f"⚠ 体积 {size_mb:.1f} MB 偏大 —— 多半是用了 md2pdf 嵌整套 CJK 字体,"
          f"改用 playwright 一般 < 2 MB")
    bad += 1

if "数据来源" not in alltext and "来源" not in alltext:
    print("⚠ 全文没有「数据来源」—— 金融报告必须有逐条来源表(SKILL.md §4)")
    bad += 1
else:
    print("✅ 找到数据来源段")

print(f"\n每页已渲染到 {outdir}/ —— **用 Read 工具真的看几页**,机器查不出"
      f"「标签被柱子盖住」「表格被腰斩」这类问题。")
print(f"\n结果:{'有 %d 处问题' % bad if bad else '机器检查通过'}")
sys.exit(1 if bad else 0)
