import { test, expect, type Page } from '@playwright/test';

async function createProduct(page:Page,name:string) {
  return page.evaluate(async name=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/products',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({operation_id:crypto.randomUUID(),config:{name,source:'/isolated/notifications',goal:'验证通知设置界面'}})});
    if(!response.ok)throw new Error(await response.text());
    return response.json();
  },name);
}

function notificationView() {
  return {
    config_version:0,enabled:true,sender_masked:'old****@example.test',sender_configured:true,credential_configured:true,
    recipients:['ops@example.test'],events:{run_report:true,alert:false,quota_exhausted:false,competitor:false,morning_retro:false},
    smtp:{host:'smtp.example.test',port:465,tls:'ssl'},morning_hour:7,recent:[],stats:{sent:0,partial:0,queued:0,failed:0,blocked:0},
  };
}

// 通知设置只验证界面行为：不联网验证授权码、不发送真实邮件。
test('notification settings render, mask sender and never echo the credential', async ({page})=>{
  const errors:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/autopilot');
  const product=await createProduct(page,'通知设置测试');
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
  await expect(page.getByRole('button',{name:'发送测试简报',exact:true})).toBeDisabled();
  const saved=await page.evaluate(async id=>(await fetch(`/api/products/${id}/notifications`)).json(),product.id);
  expect(saved.config_version).toBe(1);
  expect(saved.credential_configured).toBe(false);
  expect(errors).toEqual([]);
});

test('notification drafts keep their baseline through polling and reject a second editor',async ({page,context})=>{
  await page.goto('/autopilot');
  const product=await createProduct(page,'通知并发编辑测试');
  const initial=notificationView();
  let current=structuredClone(initial);
  let staleReads=0;
  const versions:number[]=[];
  await context.route(`**/api/products/${product.id}/notifications`,async route=>{
    // Return one old poll after a successful save, while deliveries may continue refreshing.
    const stale=route.request().frame().page()===page && staleReads>0;
    if(stale)staleReads--;
    await route.fulfill({json:stale?initial:current});
  });
  await context.route(`**/api/products/${product.id}/notifications/configure`,async route=>{
    const body=route.request().postDataJSON();
    versions.push(body.config_version);
    if(body.config_version!==current.config_version){
      await route.fulfill({status:409,json:{error:'通知配置已被其他操作修改；当前草稿已保留'}});
      return;
    }
    current={...current,...body.config,config_version:current.config_version+1,
      sender_masked:body.config.sender?'new****@example.test':current.sender_masked,stats:{...current.stats,sent:3}};
    if(route.request().frame().page()===page)staleReads=1;
    await route.fulfill({json:current});
  });
  const url=`/autopilot?project=${product.id}&view=settings&tab=notifications`;
  await page.goto(url);
  await expect(page.getByLabel('SMTP 服务器')).toHaveValue(initial.smtp.host);
  const other=await context.newPage();
  await other.goto(url);
  await expect(other.getByLabel('SMTP 服务器')).toHaveValue(initial.smtp.host);

  await other.getByLabel('发件人邮箱').fill('second@example.test');
  await other.getByLabel('邮箱授权码').fill('second-fake-code');
  await other.getByLabel('SMTP 服务器').fill('smtp.second.example.test');
  await other.getByLabel('新增收件人').fill('second-ops@example.test');
  await other.getByRole('button',{name:'添加收件人',exact:true}).click();

  await page.getByLabel('发件人邮箱').fill('first@example.test');
  await page.getByLabel('邮箱授权码').fill('first-fake-code');
  await page.getByLabel('SMTP 服务器').fill('smtp.first.example.test');
  await page.getByRole('switch',{name:'启用邮件通知',exact:true}).click();
  await page.getByLabel('新增收件人').fill('first-ops@example.test');
  await page.getByRole('button',{name:'添加收件人',exact:true}).click();
  await page.getByRole('button',{name:'保存通知设置',exact:true}).click();
  await expect(page.getByText('邮件通知设置已保存',{exact:true})).toBeVisible();
  await expect.poll(()=>staleReads).toBe(0);
  await expect(page.getByLabel('SMTP 服务器')).toHaveValue('smtp.first.example.test');
  await expect(page.getByLabel('邮箱授权码')).toHaveValue('');
  await expect(page.getByLabel('发件人邮箱')).toHaveValue('');

  await other.bringToFront();
  await expect(other.getByText(/SMTP 已接受 3 ·/)).toBeVisible({timeout:10000});
  await expect(other.getByLabel('SMTP 服务器')).toHaveValue('smtp.second.example.test');
  await expect(other.getByLabel('发件人邮箱')).toHaveValue('second@example.test');
  await expect(other.getByLabel('邮箱授权码')).toHaveValue('second-fake-code');
  await expect(other.getByText('second-ops@example.test',{exact:true})).toBeVisible();
  await other.getByRole('button',{name:'保存通知设置',exact:true}).click();
  await expect(other.getByText('通知配置已被其他操作修改；当前草稿已保留',{exact:true})).toBeVisible();
  await expect(other.getByLabel('邮箱授权码')).toHaveValue('second-fake-code');
  await expect(other.getByLabel('SMTP 服务器')).toHaveValue('smtp.second.example.test');
  expect(versions).toEqual([0,0]);
  expect(current.smtp.host).toBe('smtp.first.example.test');
  await other.screenshot({path:'.playwright/notification-conflict.png',fullPage:true});

  await other.getByRole('button',{name:'放弃修改并载入最新配置',exact:true}).click();
  await expect(other.getByLabel('SMTP 服务器')).toHaveValue('smtp.first.example.test');
  await expect(other.getByLabel('邮箱授权码')).toHaveValue('');
  await expect(other.getByLabel('发件人邮箱')).toHaveValue('');
  await expect(other.getByText('second-ops@example.test',{exact:true})).toHaveCount(0);
  await expect(other.getByText('first-ops@example.test',{exact:true})).toBeVisible();
  await other.getByLabel('SMTP 服务器').fill('smtp.latest.example.test');
  await other.getByRole('button',{name:'保存通知设置',exact:true}).click();
  await expect(other.getByText('邮件通知设置已保存',{exact:true})).toBeVisible();
  expect(versions).toEqual([0,0,1]);
  expect(current.config_version).toBe(2);
});

test('notification drafts and credentials do not move between projects',async ({page})=>{
  await page.goto('/autopilot');
  const first=await createProduct(page,'通知草稿项目甲');
  const second=await createProduct(page,'通知草稿项目乙');
  const views={
    [first.id]:notificationView(),
    [second.id]:{...notificationView(),config_version:4,sender_masked:'oth****@example.test',recipients:['other-ops@example.test'],smtp:{host:'smtp.other.example.test',port:465,tls:'ssl'}},
  };
  for(const [id,view] of Object.entries(views)){
    await page.route(`**/api/products/${id}/notifications`,route=>route.fulfill({json:view}));
  }
  await page.goto(`/autopilot?project=${first.id}&view=settings&tab=notifications`);
  await expect(page.getByLabel('SMTP 服务器')).toHaveValue('smtp.example.test');
  await page.getByLabel('发件人邮箱').fill('draft@example.test');
  await page.getByLabel('邮箱授权码').fill('project-a-fake-code');
  await page.getByLabel('新增收件人').fill('draft-ops@example.test');
  await page.getByRole('button',{name:'添加收件人',exact:true}).click();
  await page.locator('.ant-select[aria-label="选择项目"] .ant-select-selector').click();
  await page.getByTitle('通知草稿项目乙',{exact:true}).click();
  await expect(page.getByRole('heading',{name:'通知草稿项目乙',exact:true})).toBeVisible();
  await expect(page.getByLabel('SMTP 服务器')).toHaveValue('smtp.other.example.test');
  await expect(page.getByLabel('发件人邮箱')).toHaveValue('');
  await expect(page.getByLabel('邮箱授权码')).toHaveValue('');
  await expect(page.getByText('other-ops@example.test',{exact:true})).toBeVisible();
  await expect(page.getByText('draft-ops@example.test',{exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'保存通知设置',exact:true})).toBeDisabled();
});

test('cancelled notifications disclose withdrawn recipients without claiming delivery',async ({page})=>{
  await page.goto('/autopilot');
  const product=await createProduct(page,'通知撤回状态测试');
  const view={...notificationView(),stats:{...notificationView().stats,cancelled:1},recent:[{
    id:'withdrawn-notification',kind:'test',subject:'已撤回的测试简报',status:'cancelled',created:100,attempts:1,
    error:'未接受邮件的收件人已撤回，停止发送',receipt:{message_id:'<fixture@example.test>',withdrawn:['removed@example.test']},
  }]};
  await page.route(`**/api/products/${product.id}/notifications`,route=>route.fulfill({json:view}));
  await page.goto(`/autopilot?project=${product.id}&view=settings&tab=notifications`);
  const row=page.getByRole('row').filter({hasText:'已撤回的测试简报'});
  await expect(row.getByText('已取消',{exact:true})).toBeVisible();
  await expect(row.getByText('已撤回收件人：removed@example.test',{exact:true})).toBeVisible();
  await expect(row.getByText('SMTP 已接受',{exact:true})).toHaveCount(0);
  await expect(page.getByText(/SMTP 已接受 0 ·.*已取消 1/)).toBeVisible();
});
