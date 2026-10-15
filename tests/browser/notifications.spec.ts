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

test('notification failures expose redacted SSL diagnostics and preserve the draft', async ({page})=>{
  const productId='notification-failure';
  const credential='synthetic-credential-canary';
  const errorInfo={
    code:'TLS_CONNECTION_LOST',
    message:'与邮件服务器的加密连接意外中断。',
    suggestion:'请检查网络、代理规则及服务端口和加密方式后重试。',
  };
  const failure={
    error:errorInfo.message+errorInfo.suggestion,
    error_info:errorInfo,
    diagnostic:{
      reason:'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1082)',
      credential,
    },
  };
  await page.route('**/api/products',route=>route.fulfill({json:[{
    id:productId,name:'通知失败测试',status:'paused',version:1,created:1,updated:1,
  }]}));
  await page.route(`**/api/products/${productId}/notifications`,route=>route.fulfill({json:{
    enabled:true,sender_configured:true,sender_masked:'sav****@example.test',credential_configured:true,
    smtp:{host:'smtp.qq.com',port:465,tls:'ssl'},morning_hour:7,
    recipients:['saved@example.test'],events:{run_report:false},recent:[],stats:{},
  }}));
  for(const action of ['configure','send-test']){
    await page.route(`**/api/products/${productId}/notifications/${action}`,
      route=>route.fulfill({status:500,json:failure}));
  }
  await page.goto(`/autopilot?project=${productId}&view=settings&tab=notifications`);
  await expect(page.getByText('sav****@example.test')).toBeVisible();
  await page.getByLabel('发件人邮箱').fill('draft@example.test');
  await page.getByLabel('邮箱授权码').fill(credential);
  await page.getByLabel('SMTP 服务器').fill('smtp.draft.example.test');
  await page.getByLabel('每日复盘时间',{exact:true}).fill('9');
  await page.getByLabel('新增收件人').fill('draft-ops@example.test');
  await page.getByRole('button',{name:'添加收件人'}).click();
  await page.getByLabel('运行节点生成报告').click();

  for(const action of ['保存通知设置','发送测试简报']){
    await page.getByRole('button',{name:action}).click();
    await expect(page.getByText(errorInfo.message+errorInfo.suggestion,{exact:true})).toBeVisible();
    const details=page.locator('details.record-raw').filter({hasText:'UNEXPECTED_EOF_WHILE_READING'});
    await expect(details).toHaveCount(1);
    if(!await details.evaluate(element=>element.hasAttribute('open'))){
      await details.getByText('技术详情',{exact:true}).click();
    }
    await expect(details.locator('pre')).toBeVisible();
    await expect(details.locator('pre')).toContainText('EOF occurred in violation of protocol');
    await expect(details.locator('pre')).toContainText('[已隐藏]');
    await expect(details.locator('pre')).not.toContainText(credential);
    await expect(page.getByLabel('发件人邮箱')).toHaveValue('draft@example.test');
    await expect(page.getByLabel('邮箱授权码')).toHaveValue(credential);
    await expect(page.getByLabel('SMTP 服务器')).toHaveValue('smtp.draft.example.test');
    await expect(page.getByLabel('每日复盘时间',{exact:true})).toHaveValue('9');
    await expect(page.getByText('draft-ops@example.test',{exact:true})).toBeVisible();
    await expect(page.getByLabel('运行节点生成报告')).toBeChecked();
  }
});
