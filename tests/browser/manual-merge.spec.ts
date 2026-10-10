import { test, expect } from '@playwright/test';

test('manual release merge submits asynchronously and keeps progress visible', async ({page})=>{
  const product={id:'manual',name:'手动合并项目',status:'active',version:1};
  let submitted:Record<string,unknown>|undefined;
  let pr={id:'release',delivery_id:'batch',delivery_version:7,pr_url:'https://github.com/example/repo/pull/2',number:2,
    title:'20261009 每日代码交付',status:'unreviewed',git_status:'open',updated:1,head_branch:'release-20261009',base_branch:'master',
    manual_merge_allowed:true,manual_merge_reason:'',progress:{status:'collecting',reason:''}};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/manual/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/manual/delivery-board',r=>r.fulfill({json:{prs:[],release_prs:[pr],issues:[],checked_at:1}}));
  await page.route('**/api/deliveries/batch/manual-merge',async r=>{
    submitted=r.request().postDataJSON();
    pr={...pr,delivery_version:8,manual_merge_allowed:false,manual_merge_reason:'已提交手动合并，正在按流程处理',progress:{status:'syncing_release',reason:'手动合并已提交'}};
    await r.fulfill({json:{status:'syncing_release'}});
  });
  await page.goto('/autopilot?project=manual');
  await page.getByRole('button',{name:'待合并 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  const button=drawer.getByRole('button',{name:'手动合并',exact:true});
  await expect(button).toBeEnabled();
  await expect(button).toBeInViewport();
  const titleCell=drawer.getByRole('cell').filter({has:page.getByRole('link',{name:'#2 20261009 每日代码交付'})});
  await expect(titleCell).toBeVisible();
  const titleBounds=await titleCell.boundingBox();
  expect(titleBounds!.width).toBeGreaterThan(150);
  expect(titleBounds!.height).toBeLessThan(120);
  await page.screenshot({path:'.playwright/manual-merge.png'});
  await button.click();
  await expect.poll(()=>submitted?.version).toBe(7);
  expect(submitted?.operation_id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByText('已提交手动合并，验证通过后自动合入；后续成果进入新批次',{exact:true})).toBeVisible();
  await expect(drawer.getByText('同步上线 MR · 等待执行',{exact:true})).toBeVisible();
  await expect(button).toBeDisabled();
  await expect(drawer.getByRole('button',{name:'查看证据',exact:true})).toBeEnabled();
  pr={...pr,status:'merged',git_status:'merged',progress:{status:'online',reason:''}};
  await expect(button).toHaveCount(0,{timeout:12000});
});

test('manual merge errors preserve the row and allow retry', async ({page})=>{
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'error',name:'合并失败项目',status:'active',version:1}]}));
  await page.route('**/api/products/error/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/error/delivery-board',r=>r.fulfill({json:{prs:[],issues:[],release_prs:[{
    id:'release',delivery_id:'batch',delivery_version:1,number:2,title:'每日交付',pr_url:'https://github.com/example/repo/pull/2',
    status:'unreviewed',git_status:'open',manual_merge_allowed:true,updated:1,
  }]}}));
  await page.route('**/api/deliveries/batch/manual-merge',r=>r.fulfill({status:409,json:{error:'记录已更新，请刷新后重试'}}));
  await page.goto('/autopilot?project=error');
  await page.getByRole('button',{name:'待合并 1',exact:true}).click();
  const button=page.locator('.ant-drawer-content').getByRole('button',{name:'手动合并',exact:true});
  await button.click();
  await expect(page.locator('.ant-message-error')).toBeVisible();
  await expect(button).toBeEnabled();
});

test('blocked release can retry its failed phase and request errors keep retry available', async ({page})=>{
  let pr={id:'release',delivery_id:'batch',delivery_version:9,number:2,title:'每日交付',pr_url:'https://github.com/example/repo/pull/2',
    status:'unreviewed',git_status:'open',delivery_status:'blocked',manual_merge_allowed:false,retryable:true,retry_reason:'',updated:1,
    progress:{status:'blocked',reason:'模型调用失败'}};
  let submitted:Record<string,unknown>|undefined;
  let fail=true;
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'retry',name:'重试合并项目',status:'active',version:1}]}));
  await page.route('**/api/products/retry/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/retry/delivery-board',r=>r.fulfill({json:{prs:[],issues:[],release_prs:[pr]}}));
  await page.route('**/api/deliveries/batch/retry',async r=>{
    submitted=r.request().postDataJSON();
    if(fail){await r.fulfill({status:409,json:{error:'记录已更新，请刷新后重试'}});return;}
    pr={...pr,delivery_version:10,delivery_status:'syncing_release',retryable:false,progress:{status:'syncing_release',reason:''}};
    await r.fulfill({json:{status:'syncing_release'}});
  });
  await page.goto('/autopilot?project=retry');
  await page.getByRole('button',{name:'待合并 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  const retry=drawer.getByRole('button',{name:'重试',exact:true});
  await expect(drawer.getByRole('button',{name:'手动合并',exact:true})).toBeDisabled();
  await expect(retry).toBeEnabled();
  await expect(retry).toBeInViewport();
  await retry.click();
  await expect(page.locator('.ant-message-error')).toBeVisible();
  await expect(retry).toBeEnabled();
  fail=false;
  await retry.click();
  await expect.poll(()=>submitted?.version).toBe(9);
  expect(submitted?.operation_id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByText('已提交重试，将从异常中断的阶段继续',{exact:true})).toBeVisible();
  await expect(retry).toHaveCount(0);
  await expect(drawer.getByText('同步上线 MR · 等待执行',{exact:true})).toBeVisible();
});

test('release retry is disabled while its previous result needs reconciliation', async ({page})=>{
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'uncertain',name:'待核对项目',status:'active',version:1}]}));
  await page.route('**/api/products/uncertain/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/uncertain/delivery-board',r=>r.fulfill({json:{prs:[],issues:[],release_prs:[{
    id:'release',delivery_id:'batch',delivery_version:1,number:2,title:'每日交付',pr_url:'https://github.com/example/repo/pull/2',
    status:'unreviewed',git_status:'open',delivery_status:'blocked',retryable:false,retry_reason:'当前执行结果待核对',updated:1,
  }]}}));
  await page.goto('/autopilot?project=uncertain');
  await page.getByRole('button',{name:'待合并 1',exact:true}).click();
  await expect(page.locator('.ant-drawer-content').getByRole('button',{name:'重试',exact:true})).toBeDisabled();
});
