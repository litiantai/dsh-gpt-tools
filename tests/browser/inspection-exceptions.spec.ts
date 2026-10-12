import {test,expect} from '@playwright/test';

test('inspection exceptions leave the pending node and remain available as evidence',async({page})=>{
  await page.goto('/autopilot');
  const {product,abnormal}=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const post=async(path:string,body:Record<string,unknown>)=>{
      const response=await fetch(`/api${path}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({...body,operation_id:crypto.randomUUID()})});
      if(!response.ok)throw new Error(await response.text());
      return response.json();
    };
    const product=await post('/products',{config:{name:'巡检异常测试',source:'/isolated/inspection-exceptions',goal:'异常只留存证据'}});
    const abnormal=await post('/signals',{product_id:product.id,signal:{source:'inspection',code:'PROJECT_HEALTH',summary:'运行巡检超时记录',evidence:{status:'blocked',reason:'timed out'}}});
    await post('/signals',{product_id:product.id,signal:{source:'feedback',code:'feedback',summary:'正常待处理反馈',evidence:'用户反馈'}});
    return {product,abnormal};
  });
  expect(abnormal.status).toBe('abnormal');
  await page.goto(`/autopilot?project=${product.id}`);
  await page.getByRole('button',{name:'巡检信号 1',exact:true}).click();
  const drawer=page.getByRole('dialog',{name:'链路节点 · 任务与证据'});
  await expect(drawer.getByText('正常待处理反馈',{exact:true})).toBeVisible();
  await expect(drawer.getByText('运行巡检超时记录',{exact:true})).toHaveCount(0);
  await drawer.locator('.ant-drawer-close').click();
  await page.getByRole('tab',{name:'巡查与证据',exact:true}).click();
  await page.getByRole('tab',{name:'巡检异常 (1)',exact:true}).click();
  await expect(page.getByText(/异常每日北京时间 09:00 自动复扫/)).toBeVisible();
  const row=page.locator(`tr[data-row-key="${abnormal.id}"]`);
  await expect(row.getByText('异常',{exact:true})).toBeVisible();
  await expect(row.getByText('运行巡检超时记录',{exact:true})).toBeVisible();
  await row.getByRole('button',{name:'详情',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'巡检异常记录'})).toContainText('timed out');
  await page.reload();
  await expect(row.getByText('异常',{exact:true})).toBeVisible();
});

test('manual rescan submits once and displays durable queue state',async({page})=>{
  let calls=0;
  const product={id:'rescan-product',name:'每日复扫',status:'active',source:'/fixture',version:1};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/signals',r=>r.fulfill({json:[{id:'exception',product_id:product.id,source:'inspection',status:'classified',requirement_id:'environment-req',summary:'登录超时',rescan_status:'queued',evidence:{status:'blocked',reason:'timeout'}}]}));
  await page.route(`**/api/products/${product.id}/inspection-routes/rescan`,r=>{calls++;return r.fulfill({json:{id:'rescan-round',status:'queued'}});});
  await page.goto(`/autopilot?project=${product.id}&view=evidence&tab=inspection_exceptions`);
  await page.getByRole('button',{name:'重新扫描',exact:true}).click();
  await expect(page.getByText(/重新扫描已入队，轮次 rescan-r/)).toBeVisible();
  await expect(page.getByText('已排队',{exact:true})).toBeVisible();
  expect(calls).toBe(1);
});
