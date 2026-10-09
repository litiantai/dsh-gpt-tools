import { chromium } from '@playwright/test';
const [origin, destination] = process.argv.slice(2);
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw Error('必须使用隔离实例');
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  await page.goto(origin, {waitUntil:'networkidle', timeout:20000});
  await page.screenshot({path:destination, fullPage:true});
} finally { await browser.close(); }
