import {test, expect} from '@playwright/test';

test('manually promote a signal from its node and queue it with persistent high priority', async ({page})=>{
  await page.goto('/autopilot');
  const {product,signal}=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const post=async(path:string,body:Record<string,unknown>)=>{
      const response=await fetch(`/api${path}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({...body,operation_id:crypto.randomUUID()})});
      if(!response.ok)throw new Error(await response.text());
      return response.json();
    };
    const product=await post('/products',{config:{name:'手动入池测试',source:'/isolated/manual-requirements',goal:'验证人工优先入池'}});
    const signal=await post('/signals',{product_id:product.id,signal:{source:'feedback',code:'manual-priority',summary:'通知能力实现',evidence:'任务完成后未展示通知'}});
    await post('/requirements',{product_id:product.id,title:'普通需求',evidence:'普通证据',acceptance:['正常显示'],impact:'普通影响'});
    return {product,signal};
  });
  await page.goto(`/autopilot?project=${product.id}`);
  await page.getByRole('button',{name:'巡检信号 1',exact:true}).click();
  const drawer=page.getByRole('dialog',{name:'链路节点 · 任务与证据'});
  await drawer.getByRole('button',{name:'加入需求池',exact:true}).click();
  const modal=page.getByRole('dialog',{name:'手动加入需求池'});
  await expect(modal.getByLabel('需求名称',{exact:true})).toHaveValue('通知能力实现');
  await expect(modal.getByLabel('问题证据与复现步骤',{exact:true})).toHaveValue('任务完成后未展示通知');
  await modal.getByRole('button',{name:'高优先级入池'}).click();
  await expect(modal).toBeVisible();
  await modal.getByLabel('用户影响',{exact:true}).fill('无法及时看到任务结果');
  await modal.getByLabel('验收条件',{exact:true}).fill('任务完成后展示通知\n点击通知可查看任务详情');
  await modal.getByRole('button',{name:'高优先级入池'}).click();
  await expect(modal).toBeHidden();
  await expect(drawer.getByRole('button',{name:'加入需求池',exact:true})).toHaveCount(0);
  await drawer.locator('.ant-drawer-close').click();
  await expect(page.getByRole('button',{name:'巡检信号 0',exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'需求池',exact:true}).click();
  const firstRow=page.locator('.project-record-table tbody tr.ant-table-row').first();
  await expect(firstRow).toContainText('通知能力实现');
  await expect(firstRow).toContainText('高优先级 · 插队');
  await firstRow.getByRole('button',{name:'优先加入开发队列',exact:true}).click();
  await expect(firstRow.getByRole('button',{name:'优先加入开发队列',exact:true})).toHaveCount(0);
  await page.reload();
  await expect(firstRow).toContainText('高优先级 · 插队');
  const state=await page.evaluate(async({id,productId})=>({
    signal:await (await fetch(`/api/signals/${id}`)).json(),
    requirements:(await (await fetch('/api/requirements')).json()).filter((r:{product_id:string})=>r.product_id===productId),
    runs:(await (await fetch('/api/runs')).json()).filter((r:{product_id:string})=>r.product_id===productId),
  }),{id:signal.id,productId:product.id});
  expect(state.signal.status).toBe('classified');
  expect(state.requirements).toHaveLength(2);
  expect(state.runs).toHaveLength(1);
  expect(state.runs[0]).toMatchObject({priority:0,queue_first:true,requirement_id:state.signal.requirement_id});
  await page.getByRole('tab',{name:'监测信号 1',exact:true}).click();
  await expect(page.getByRole('button',{name:'加入需求池',exact:true})).toHaveCount(0);
});

test('failed promotion keeps input so the user can correct and retry', async ({page})=>{
  const product={id:'promotion-error',name:'入池失败测试',status:'paused',version:1,created:1,updated:1};
  const signal={id:'promotion-signal',product_id:product.id,summary:'测试信号',evidence:'测试证据',status:'pending',version:1,created:1,updated:1};
  await page.route('**/api/products',route=>route.fulfill({json:[product]}));
  await page.route('**/api/signals',route=>route.fulfill({json:[signal]}));
  await page.route(`**/api/signals/${signal.id}/promote`,route=>route.fulfill({status:409,json:{error:'信号已更新，请刷新后重试'}}));
  await page.goto(`/autopilot?project=${product.id}&view=requirements&tab=signals`);
  await page.getByRole('button',{name:'加入需求池',exact:true}).click();
  const modal=page.getByRole('dialog',{name:'手动加入需求池'});
  await modal.getByLabel('用户影响',{exact:true}).fill('用户无法查看结果');
  await modal.getByLabel('验收条件',{exact:true}).fill('能够查看结果');
  await modal.getByRole('button',{name:'高优先级入池'}).click();
  await expect(page.getByText('信号已更新，请刷新后重试',{exact:true})).toBeVisible();
  await expect(modal.getByLabel('用户影响',{exact:true})).toHaveValue('用户无法查看结果');
  await expect(modal.getByRole('button',{name:'高优先级入池'})).toBeEnabled();
});
