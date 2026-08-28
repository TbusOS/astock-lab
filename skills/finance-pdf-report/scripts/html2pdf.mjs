// HTML → PDF（金融报告固定版式）
// 用法（任意目录都能跑，脚本自己找 playwright）：
//   node <skill>/scripts/html2pdf.mjs 报告.html 输出.pdf ["页脚文字"]
//   PLAYWRIGHT_ROOT=/path/to/node_modules/.. 可显式指定
//
// ⚠ 为什么不直接 `import { chromium } from 'playwright'`：
//   ESM 的 import 按**脚本自身位置**解析 node_modules，不按 cwd。
//   本脚本住在 本仓 私有仓（没有 node_modules），所以裸 import 必然
//   ERR_MODULE_NOT_FOUND —— 哪怕你 cd 到装了 playwright 的目录也没用。
//   解法：用 createRequire 从候选目录逐个解析。
//
// 三个关键参数，缺一个就翻车：
//   waitUntil:'networkidle'  等 webfont 和图表渲染完
//   emulateMedia('print')    应用 @page 与 page-break-*，否则分页失控
//   printBackground:true     保留底色 —— 不加的话所有背景色/色块全白
import path from 'path';
import fs from 'fs';
import os from 'os';
import { createRequire } from 'module';

function loadPlaywright() {
  const cands = [
    process.env.PLAYWRIGHT_ROOT,
    process.cwd(),
    path.join(os.homedir(), 'claude-tools', 'sky-skills'),
    path.join(os.homedir(), 'claude-tools', '本仓'),
    path.join(os.homedir(), 'android-llm-bridge', 'web'),
  ].filter(Boolean);
  for (const dir of cands) {
    try {
      const req = createRequire(path.join(dir, 'noop.js'));
      return { pw: req('playwright'), from: dir };
    } catch { /* 试下一个 */ }
  }
  console.error(
    '找不到 playwright。装一次:\n' +
    '  cd ~/claude-tools/sky-skills && npm i playwright && npx playwright install chromium\n' +
    '或指定:PLAYWRIGHT_ROOT=<含 node_modules 的目录> node ...\n' +
    '已试过:' + cands.join(' , '));
  process.exit(2);
}

const [, , src, out, footerText] = process.argv;
if (!src || !out) {
  console.error('用法: node html2pdf.mjs <报告.html> <输出.pdf> ["页脚文字"]');
  process.exit(2);
}
if (!fs.existsSync(src)) { console.error(`找不到 ${src}`); process.exit(2); }

const { pw, from } = loadPlaywright();
const foot = footerText || path.basename(src, '.html');
const browser = await pw.chromium.launch();
const page = await browser.newPage();
await page.goto('file://' + path.resolve(src), { waitUntil: 'networkidle' });
await page.emulateMedia({ media: 'print' });
await page.pdf({
  path: out,
  format: 'A4',
  printBackground: true,
  margin: { top: '12mm', bottom: '12mm', left: '10mm', right: '10mm' },
  displayHeaderFooter: true,
  headerTemplate: '<span></span>',
  footerTemplate:
    `<div style="width:100%;font-size:8px;color:#8c8a7d;text-align:center;` +
    `font-family:sans-serif;">${foot} · 第 <span class="pageNumber"></span>` +
    ` / <span class="totalPages"></span> 页</div>`,
});
await browser.close();
const kb = (fs.statSync(out).size / 1024).toFixed(0);
console.log(`PDF ok: ${out} (${kb} KB)　playwright from ${from}`);
console.log('⚠ 别只看这行 —— 跑 scripts/check_pdf.py 并用 Read 看几页再交付');
