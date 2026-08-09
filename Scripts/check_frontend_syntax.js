#!/usr/bin/env node
/**
 * 前端脚本语法检查工具
 * ---------------------------------
 * 从 frontend_prototype.html 中提取所有 <script> 内联块（跳过带 src 的外链脚本），
 * 用 node 的 vm 模块做语法解析检查。任何块解析失败即以非 0 退出码报错。
 *
 * 用法：node Scripts/check_frontend_syntax.js [html文件路径]
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const target = process.argv[2]
  || path.join(__dirname, '..', 'frontend_prototype.html');

const html = fs.readFileSync(target, 'utf8');

// 匹配 <script ...>...</script>，捕获属性与内容
const scriptRe = /<script([^>]*)>([\s\S]*?)<\/script>/gi;

let blockIndex = 0;
let checked = 0;
let failed = 0;
let match;

while ((match = scriptRe.exec(html)) !== null) {
  blockIndex += 1;
  const attrs = match[1] || '';
  const code = match[2] || '';

  // 外链脚本没有内联代码，跳过
  if (/\ssrc\s*=/i.test(attrs)) continue;
  // 非 JS 类型（如 application/json、text/template）跳过
  const typeMatch = attrs.match(/type\s*=\s*["']([^"']+)["']/i);
  if (typeMatch) {
    const t = typeMatch[1].toLowerCase();
    const jsTypes = ['text/javascript', 'application/javascript', 'module'];
    if (!jsTypes.includes(t)) continue;
  }
  if (!code.trim()) continue;

  // 计算该块在文件中的起始行号，便于定位报错
  const startLine = html.slice(0, match.index).split('\n').length;

  checked += 1;
  try {
    // 只做语法解析，不执行
    new vm.Script(code, { filename: `${path.basename(target)}#script${blockIndex}` });
    console.log(`OK   script#${blockIndex} (starts at line ${startLine}, ${code.split('\n').length} lines)`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL script#${blockIndex} (starts at line ${startLine}): ${err.message}`);
    if (err.stack) {
      const relevant = err.stack.split('\n').slice(0, 6).join('\n');
      console.error(relevant);
    }
  }
}

console.log(`\nchecked ${checked} inline script block(s), ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
