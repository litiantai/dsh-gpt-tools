import { test, expect } from '@playwright/test';

// 通知设置只验证界面行为：不联网验证授权码、不发送真实邮件。
test('notification settings render, mask sender and never echo the credential', async ({page})=>{
  const errors:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/autopilot');
  const product=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/products',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({operation_id:crypto.randomUUID(),config:{name:'通知设置测试',source:'/isolated/notifications',goal:'验证通知设置界面'}})});
    if(!response.ok)throw new Error(await response.text());
    return response.json();
  });
  await page.goto(`/autopilot?project=${product.id}&view=settings&tab=notifications`);
  await expect(page.getByText('邮件在本机直接发送')).toBeVisible();
  for(const label of ['运行节点生成报告','告警','额度用尽提示','竞品发现-分析','每日早上七点给出复盘报告']){
    await expect(page.getByLabel(label)).toBeVisible();
  }
  await page.getByLabel('发件人邮箱').fill('sender@example.test');
  await page.getByLabel('新增收件人').fill('ops@example.test');
  await page.getByRole('button',{name:'添加收件人'}).click();
  await expect(page.getByText('ops@example.test')).toBeVisible();
  await page.getByLabel('运行节点生成报告').click();
  await page.getByRole('button',{name:'保存通知设置'}).click();
  await expect(page.getByText('sen****@example.test')).toBeVisible();
  await expect(page.getByLabel('邮箱授权码')).toHaveValue('');
  await expect(page.getByText('尚未配置授权码')).toHaveCount(0);
  expect(errors).toEqual([]);
});
