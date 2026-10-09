#!/usr/bin/env node
// Deterministic DOM receipt for legacy --dump-dom checks; waits for the real SPA to render.
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const url=process.argv.at(-1);
if(process.env.THSOCTOP_AUTOPILOT_ISOLATED!=='1' || !process.env.THSOCTOP_APP_SUPPORT || !/^http:\/\/127\.0\.0\.1:\d+\//.test(url))throw Error('Isolated local page required');
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.goto(url,{waitUntil:'domcontentloaded'});
 await page.locator('#root').waitFor({state:'visible',timeout:25000});
 await page.waitForFunction(()=>document.querySelector('#root')?.textContent?.trim().length>20,{timeout:25000});
 const dir=join(process.env.THSOCTOP_APP_SUPPORT,'browser-evidence');mkdirSync(dir,{recursive:true});
 await page.screenshot({path:join(dir,'gallery-render.png'),fullPage:true});
 const html=await page.content();
 writeFileSync(join(dir,'gallery-render.json'),JSON.stringify({status:'pass',path:new URL(url).pathname,text:(await page.locator('#root').innerText()).slice(0,3000)}));
 await browser.close();
 process.stdout.write(html);
} finally {await browser.close();}
