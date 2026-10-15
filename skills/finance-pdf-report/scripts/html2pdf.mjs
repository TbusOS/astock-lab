// HTML → PDF（金融报告固定版式）
// 用法（任意目录都能跑，脚本自己找 playwright）：
//   node <skill>/scripts/html2pdf.mjs 报告.html 输出.pdf ["页脚文字"]
//   PLAYWRIGHT_ROOT=/path/to/node_modules/.. 可显式指定
//
// ⚠ 为什么不直接 `import { chromium } from 'playwright'`：
//   ESM 的 import 按**脚本自身位置**解析 node_modules，不按 cwd。
//   本脚本住在仓内的 skills/ 下（那里没有 node_modules），所以裸 import 必然
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
import { fileURLToPath } from 'url';

// 本机专属路径(比如这台机器的 sky-skills 装在哪)**不写进公开仓**,写在内部仓:
//   <内部仓>/private/config/local.json → {"hosts": {"<主机名>": {"playwright_root": "..."}}}
// 主机名用 os.hostname();内部仓按 $STOCK_LAB → 脚本所在仓 → ~/astock-lab-private 的顺序找。
function playwrightRootFromPrivateConfig() {
  const labs = [process.env.STOCK_LAB];
  let d = path.dirname(fileURLToPath(import.meta.url));
  while (d !== path.dirname(d)) {
    if (fs.existsSync(path.join(d, '.astock-lab-root'))) { labs.push(d); break; }
    d = path.dirname(d);
  }
  labs.push(path.join(os.homedir(), 'astock-lab-private'),
            path.join(os.homedir(), 'claude-tools', 'astock-lab-private'));
  for (const lab of labs.filter(Boolean)) {
    const f = path.join(lab, 'private', 'config', 'local.json');
    if (!fs.existsSync(f)) continue;
    try {
      const host = (JSON.parse(fs.readFileSync(f, 'utf8')).hosts || {})[os.hostname()];
      if (host && host.playwright_root) return { dir: host.playwright_root, file: f };
    } catch (e) { console.error(`读 ${f} 失败:${e.message}`); }
  }
  return null;
}

function loadPlaywright() {
  const cfg = playwrightRootFromPrivateConfig();
  const cands = [
    process.env.PLAYWRIGHT_ROOT,
    cfg && cfg.dir,
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
    '  cd <sky-skills 仓> && npm i playwright && npx playwright install chromium\n' +
    `然后把那个目录写进内部仓 private/config/local.json 的 hosts["${os.hostname()}"].playwright_root,\n` +
    '或临时指定:PLAYWRIGHT_ROOT=<含 node_modules 的目录> node ...\n' +
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
