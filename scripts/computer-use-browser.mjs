/** 隔离浏览器执行器：仅接受受约束的界面动作，不执行模型生成代码。 */
import { chromium } from '@playwright/test';
import { createInterface } from 'node:readline';
let browser, context, page, origin, allowed;
const viewport = { width: 1440, height: 1000 };
function permitted(url) { try { return allowed.has(new URL(url).origin); } catch { return false; } }
function number(value, max) { if (!Number.isFinite(value) || value < 0 || value >= max) throw Error('坐标无效'); return value; }
async function command(input) {
  if (input.command === 'open') {
    origin = new URL(input.origin).origin;
    if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origin)) throw Error('必须使用分支本机实例');
    allowed = new Set([origin, ...(input.allowed_origins || []).map(x => new URL(x).origin)]);
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext({ viewport, serviceWorkers: 'block', acceptDownloads: false,
      ...(input.storage_state ? { storageState: input.storage_state } : {}) });
    await context.route('**/*', route => permitted(route.request().url()) ? route.continue() : route.abort('blockedbyclient'));
    await context.routeWebSocket('**/*', socket => {
      const url = socket.url().replace(/^ws:/, 'http:').replace(/^wss:/, 'https:');
      if (permitted(url)) socket.connectToServer(); else socket.close();
    });
    context.on('page', popup => { if (page && popup !== page) popup.close().catch(() => {}); });
    page = await context.newPage();
    page.on('dialog', dialog => dialog.dismiss());
    page.setDefaultTimeout(10000);
    await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 30000 });
    return { viewport };
  }
  if (input.command === 'close') { await browser?.close(); return { closed: true }; }
  if (!page || page.isClosed()) throw Error('浏览器会话丢失');
  if (input.command === 'screenshot') {
    if (!permitted(page.url())) throw Error('页面超出测试访问范围');
    await page.screenshot({ path: input.path });
    return { url: page.url(), title: await page.title(), viewport };
  }
  if (input.command !== 'action') throw Error('未知命令');
  const a = input.action;
  switch (a.type) {
    case 'click': await page.mouse.click(number(a.x, viewport.width), number(a.y, viewport.height)); break;
    case 'type':
      if (typeof a.text !== 'string' || a.text.length > 10000) throw Error('输入内容无效');
      await page.keyboard.insertText(a.text); break;
    case 'key':
      if (!/^(Enter|Tab|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|Space|Control\+A|Meta\+A)$/.test(a.key)) throw Error('不支持此按键');
      await page.keyboard.press(a.key); break;
    case 'scroll':
      if (!Number.isFinite(a.dy) || Math.abs(a.dy) > 2000) throw Error('滚动距离无效');
      await page.mouse.wheel(0, a.dy); break;
    case 'navigate': {
      if (typeof a.url !== 'string') throw Error('导航地址无效');
      const url = new URL(a.url, origin);
      if (url.origin !== origin || url.username || url.password) throw Error('导航必须在测试实例内');
      await page.goto(url.href, { waitUntil: 'domcontentloaded' }); break;
    }
    case 'reload': await page.reload({ waitUntil: 'domcontentloaded' }); break;
    case 'wait': break;
    default: throw Error('动作类型无效');
  }
  return { url: page.url() };
}
for await (const line of createInterface({ input: process.stdin })) {
  try { process.stdout.write(JSON.stringify({ ok: true, result: await command(JSON.parse(line)) }) + '\n'); }
  catch (error) { process.stdout.write(JSON.stringify({ ok: false, error: String(error.message || error) }) + '\n'); }
}
await browser?.close();
