import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';

test('exhaustion warns and a today-only limit can be saved without changing the default', async ({page})=>{
  const errors:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/autopilot');
  const product=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/products',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({operation_id:crypto.randomUUID(),config:{name:'今日额度测试',source:'/isolated/quota',goal:'验证今日额度',policy:{tokens_per_day:10000000}}})});
    if(!response.ok)throw new Error(await response.text());
    return response.json();
  });
  const fixture=JSON.parse(await readFile('.dashboard/e2e.json','utf8'));
  execFileSync('python3',['-c',`import sqlite3,sys,datetime
from zoneinfo import ZoneInfo
c=sqlite3.connect(sys.argv[1]+'/dashboard.sqlite3')
c.execute('INSERT INTO auto_budget VALUES (?,?,?,?)',(sys.argv[2],datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),'tokens',10009795))
c.commit()`,fixture.state,product.id]);
  await page.goto(`/autopilot?project=${product.id}`);
  const alert=page.locator('.ant-alert').filter({hasText:'今日开发 Token 额度已用尽'});
  await expect(alert).toBeVisible();
  await expect(alert).toContainText('10,009,795 / 10,000,000');
  await alert.getByRole('button',{name:'调整今日上限'}).click();
  const dialog=page.getByRole('dialog',{name:'调整今日 Token 上限'});
  await expect(dialog).toContainText('次日零点恢复默认上限 10,000,000');
  await dialog.getByLabel('今日 Token 总上限',{exact:true}).fill('9000000');
  await expect(dialog).toContainText('保存后仍会等待额度恢复');
  await dialog.getByLabel('今日 Token 总上限',{exact:true}).fill('15000000');
  await dialog.getByRole('button',{name:'保存今日上限'}).click();
  await expect(dialog).not.toBeVisible();
  await expect(alert).toHaveCount(0);
  await expect(page.getByText('今日开发 Token：10,009,795 / 15,000,000（今日临时上限）',{exact:true})).toBeVisible();
  const saved=await page.evaluate(async id=>(await fetch(`/api/products/${id}`)).json(),product.id);
  expect(saved.policy.tokens_per_day).toBe(10000000);
  expect(saved.today_token_limit.limit).toBe(15000000);
  await page.reload();
  await expect(page.getByText('今日开发 Token：10,009,795 / 15,000,000（今日临时上限）',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'调整今日上限',exact:true}).click();
  await page.getByLabel('今日 Token 总上限',{exact:true}).fill('0');
  await page.getByRole('button',{name:'保存今日上限',exact:true}).click();
  await expect(page.getByText('今日开发 Token：10,009,795 / 不限（今日临时上限）',{exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:'.playwright/today-quota-mobile.png',fullPage:true});
  expect(errors).toEqual([]);
});
