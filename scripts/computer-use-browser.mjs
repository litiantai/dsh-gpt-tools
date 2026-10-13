/** 隔离浏览器执行器：仅接受受约束的界面动作，不执行模型生成代码。 */
import { chromium } from '@playwright/test';
import { createInterface } from 'node:readline';
let browser, context, page, origin, allowed, readOnly = false, readonlyPaths = [];
let errors = [], fullPage = false;
function logError(value) { errors.push(String(value).slice(0,2000)); if(errors.length>100)errors.shift(); }
const viewport = { width: 1440, height: 1000 };
function permitted(url) { try { return allowed.has(new URL(url).origin); } catch { return false; } }
function number(value, max) { if (!Number.isFinite(value) || value < 0 || value >= max) throw Error('坐标无效'); return value; }
async function command(input) {
  if (input.command === 'open') {
    origin = new URL(input.origin).origin;
    if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origin)) throw Error('必须使用分支本机实例');
    fullPage = input.full_page === true; readOnly = input.read_only === true; readonlyPaths = input.readonly_paths || []; errors = [];
    allowed = new Set([origin, ...(input.allowed_origins || []).map(x => new URL(x).origin)]);
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext({ viewport, serviceWorkers: 'block', acceptDownloads: false,
      ...(input.storage_state ? { storageState: input.storage_state } : {}) });
    let session = {};
    if (input.session_bootstrap) {
      if (readOnly) throw Error('本机会话初始化仅支持隔离实例');
      const localPath = value => {
        if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || Array.from(value).some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 92)) throw Error('本机会话路径必须为同源绝对路径');
        const url = new URL(value, origin);
        if (url.origin !== origin || url.username || url.password || url.hash) throw Error('本机会话路径超出实例范围');
        return url.href;
      };
      const bootstrapUrl = localPath(input.session_bootstrap.path);
      const probeUrl = localPath(input.session_bootstrap.probe_path);
      const initialized = await context.request.get(bootstrapUrl, {maxRedirects:0, timeout:10000});
      if (!initialized.ok()) throw Error(`本机会话初始化失败：HTTP ${initialized.status()}`);
      const probe = await context.request.get(probeUrl, {maxRedirects:0, timeout:10000});
      if (!probe.ok()) throw Error(`本机会话探测失败：HTTP ${probe.status()}`);
      session = {session_ready:true, bootstrap_status:initialized.status(), probe_status:probe.status()};
    }
    await context.route('**/*', route => {
      const request = route.request(); const url = new URL(request.url());
      const safe = !readOnly || (['GET','HEAD'].includes(request.method()) &&
        (!['fetch','xhr'].includes(request.resourceType()) || readonlyPaths.includes(url.pathname + url.search)));
      return permitted(url.href) && safe ? route.continue() : route.abort('blockedbyclient');
    });
    await context.routeWebSocket('**/*', socket => {
      const url = socket.url().replace(/^ws:/, 'http:').replace(/^wss:/, 'https:');
      if (!readOnly && permitted(url)) socket.connectToServer(); else socket.close();
    });
    context.on('page', popup => { if (page && popup !== page) popup.close().catch(() => {}); });
    page = await context.newPage();
    page.on('pageerror',error=>logError(error.message));
    page.on('console',message=>{if(message.type()==='error')logError(message.text());});
    page.on('requestfailed',request=>logError(request.url()+': '+request.failure()?.errorText));
    page.on('dialog', dialog => dialog.dismiss());
    page.setDefaultTimeout(10000);
    const entry=new URL(input.entry_url || origin);
    if (entry.origin!==origin || entry.username || entry.password) throw Error('登录入口超出实例范围');
    await page.goto(entry.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
    return { viewport, ...session };
  }
  if (input.command === 'close') { await browser?.close(); return { closed: true }; }
  if (!page || page.isClosed()) throw Error('浏览器会话丢失');
  if (input.command === 'screenshot') {
    if (!permitted(page.url())) throw Error('页面超出测试访问范围');
    await page.screenshot({ path: input.path, fullPage });
    return { url: page.url(), title: await page.title(), viewport, text: (await page.locator('body').innerText()).slice(-12000), errors };
  }
  if (input.command === 'inspect_api') {
    const url = new URL(input.path, origin);
    if (url.origin !== origin || (readOnly && !readonlyPaths.includes(url.pathname + url.search))) throw Error('接口不在只读范围');
    const method=input.method || 'GET';
    if (!['GET','POST','PUT','PATCH','DELETE'].includes(method) || (readOnly && method!=='GET')) throw Error('主实例禁止接口写入');
    const response = await context.request.fetch(url.href, {method,data:input.body?JSON.parse(input.body):undefined,maxRedirects:0,timeout:(input.step?.wait_seconds || 45)*1000});
    return {ok:response.ok(), text:(await response.text()).slice(0,30000), status:response.status()};
  }
  if (input.command === 'inspect_wait') {
    const step=input.step; let settled=true; const deadline=Date.now()+(step.wait_seconds || 45)*1000;
    try {
      if (step.wait_selector) await page.locator(step.wait_selector).waitFor({state:'visible',timeout:Math.max(1,deadline-Date.now())});
      if (step.wait_text) await page.getByText(step.wait_text,{exact:false}).first().waitFor({state:'visible',timeout:Math.max(1,deadline-Date.now())});
    } catch { settled=false; }
    const text=await page.locator('body').innerText();
    return {settled,matched:!step.expected_text || text.includes(step.expected_text)};
  }
  if (input.command === 'inspect_action') {
    const step=input.step;
    if (readOnly && step.action !== 'navigate') throw Error('主实例禁止交互写入');
    if (step.action === 'navigate') {
      const url=new URL(step.path,origin);
      if(url.origin!==origin || url.username || url.password) throw Error('页面超出巡检范围');
      await page.goto(url.href,{waitUntil:'domcontentloaded'});
    } else if (step.action === 'click') await page.locator(step.selector).click();
    else if (step.action === 'fill') await page.locator(step.selector).fill(step.text);
    else if (step.action === 'press') {
      if (!/^(Enter|Tab|Escape|ArrowDown|ArrowUp|Space)$/.test(step.text)) throw Error('按键不受支持');
      await page.locator(step.selector).press(step.text);
    } else throw Error('未知巡检动作');
    return {url:page.url()};
  }
  if (input.command !== 'action') throw Error('未知命令');
  if (readOnly) throw Error('主实例只允许受控巡检动作');
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
