import { test, expect } from '@playwright/test';

test('old monitoring exception is Chinese by default and diagnostic is opt-in and redacted',async ({page})=>{
  const raw='<urlopen error [Errno 61] Connection refused> api_key=canary-secret';
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'error-copy',name:'异常中文验证',status:'active',version:1,
    last_probe:1791515515,last_probe_result:{status:'blocked',reason:raw}}]}));
  await page.goto('/autopilot?project=error-copy&view=overview&tab=monitoring');
  const alert=page.getByRole('alert').filter({hasText:'运行监测 · 已阻塞'});
  await expect(alert.getByText('暂时无法连接应用服务。请确认服务已启动，稍后重试。',{exact:true})).toBeVisible();
  expect(await alert.innerText()).not.toContain('Connection refused');
  expect(await page.locator('body').innerText()).not.toContain('Connection refused');
  await alert.getByText('技术详情',{exact:true}).click();
  expect(await alert.innerText()).toContain('Connection refused');
  expect(await alert.innerText()).not.toContain('canary-secret');
  await alert.getByText('技术详情',{exact:true}).click();
  await alert.getByRole('button',{name:'查看回执'}).click();
  const dialog=page.getByRole('dialog');
  expect(await dialog.innerText()).not.toContain('Connection refused');
  await page.screenshot({path:'.playwright/chinese-error-receipt.png',fullPage:true});
});

test('legacy quota and unknown tool errors stay readable in task tables',async ({page})=>{
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'error-tasks',name:'任务异常测试',status:'active',version:1}]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[
    {id:'quota-task',product_id:'error-tasks',title:'模型任务',status:'blocked',reason:'QUOTA: Insufficient Balance (request_id: fixture)'},
    {id:'unknown-task',product_id:'error-tasks',title:'未知任务',status:'blocked',reason:'StrangeException: unexpected internal value'}]}));
  await page.goto('/autopilot?project=error-tasks&view=tasks&tab=runs');
  await expect(page.getByText('模型账户余额不足，本次操作未能完成。请恢复模型账户余额后重试。',{exact:true})).toBeVisible();
  await expect(page.getByText('本次操作未能完成，具体原因暂未识别。请展开技术详情查看诊断信息。',{exact:true})).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('Insufficient Balance');
  expect(await page.locator('body').innerText()).not.toContain('StrangeException');
});

test('management fetch failure shows a Chinese message without native Error prefix',async ({page})=>{
  await page.route('**/api/products',r=>r.fulfill({status:503,json:{error:'HTTP Error 503: Service Unavailable'}}));
  await page.goto('/autopilot');
  await expect(page.getByText('管理服务暂时不可用。请稍后重试。',{exact:true})).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('HTTP Error');
});
