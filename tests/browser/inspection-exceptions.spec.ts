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
  const row=page.locator(`tr[data-row-key="${abnormal.id}"]`);
  await expect(row.getByText('异常',{exact:true})).toBeVisible();
  await expect(row.getByText('运行巡检超时记录',{exact:true})).toBeVisible();
  await row.getByRole('button',{name:'详情',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'巡检异常记录'})).toContainText('timed out');
  await page.reload();
  await expect(row.getByText('异常',{exact:true})).toBeVisible();
});
