import { test, expect } from '@playwright/test';

test('monitoring keeps the actual receipt and shows overall evidence without inventing checks',async ({page})=>{
  const result={status:'pass',health:{host:'ready'},monitor_mode:'full'};
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'monitor-context',name:'回执关联测试',status:'active',version:1,
    last_probe:1791515515,last_probe_result:result,
    receipts:[{call_id:'probe-call',evidence_id:'probe-evidence',action:'probe',at:1791515514,result}],
    last_inspect:1791515520,last_inspect_result:{status:'blocked',checks:[{name:'隔离页面检查',status:'blocked',reason:'页面未就绪'}]}}]}));
  await page.route('**/api/evidence/probe-evidence/context',r=>r.fulfill({json:{record:{id:'probe-evidence',call_id:'probe-call',phase:'probe',at:1791515514,details:result},related:[],missing:[]}}));
  await page.goto('/autopilot?project=monitor-context&view=overview&tab=monitoring');
  await page.getByRole('alert').filter({hasText:'运行监测 · 通过'}).getByRole('button',{name:'查看回执'}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await expect(dialog.getByText('当前记录及直接关联记录未提供逐项验收或检查明细')).toBeVisible();
  await expect(dialog.getByRole('tabpanel',{name:'验收与检查',exact:true}).getByText('已就绪',{exact:true})).toBeVisible();
  await expect(dialog.getByText(/共 .* 项检查/)).toHaveCount(0);
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog.getByRole('tab',{name:'关联记录 (1)',exact:true})).toBeVisible();
  await expect(dialog.getByRole('tabpanel',{name:'执行回执',exact:true}).locator('.record-receipts > .ant-card')).toHaveCount(1);
  await expect(dialog.getByText('运行监测',{exact:true})).toBeVisible();
  await dialog.getByText('查看步骤详情').click();
  await expect(dialog.getByRole('tabpanel',{name:'执行回执',exact:true}).getByText('已就绪',{exact:true})).toBeVisible();
  await page.screenshot({path:'.playwright/monitor-receipt-context.png'});
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'隔离巡检'}).getByRole('button',{name:'查看回执'}).click();
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await expect(dialog.getByText('隔离页面检查',{exact:true})).toBeVisible();
  await expect(dialog.getByText('页面未就绪',{exact:true})).toBeVisible();
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog.getByText('体验巡检',{exact:true})).toBeVisible();
});

test('requirement detail resolves nested task checks, receipts and navigable records',async ({page})=>{
  const product={id:'linked-context',name:'需求证据关联',status:'active',version:1};
  const requirement={id:'req-context',product_id:product.id,title:'缺失关联的需求',status:'accepted',version:1,acceptance:['页面可正常加载']};
  const run={id:'run-context',product_id:product.id,title:'对应研发任务',status:'accepted',requirement_id:requirement.id,
    receipts:[{action:'verify',call_id:'verify-context',at:1791515514,result:JSON.stringify({status:'pass',reason:'实际任务验证完成',checks:[{name:'关联任务页面检查',status:'pass',required:true}]})}]};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:[requirement]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route(`**/api/requirements/${requirement.id}/context`,r=>r.fulfill({json:{record:requirement,related:[{kind:'runs',record:run}],missing:[{kind:'signals',id:'missing-source'}]}}));
  await page.route(`**/api/runs/${run.id}/context`,r=>r.fulfill({json:{record:run,related:[{kind:'requirements',record:requirement}],missing:[]}}));
  await page.goto(`/autopilot?project=${product.id}&view=requirements&tab=requirements`);
  await page.getByRole('button',{name:'详情',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await expect(dialog.getByText('页面可正常加载',{exact:true})).toBeVisible();
  await expect(dialog.getByText('关联任务页面检查',{exact:true})).toBeVisible();
  await expect(dialog.getByText('研发任务 · 对应研发任务 · 检查结果',{exact:true})).toBeVisible();
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog.getByRole('tabpanel',{name:'执行回执',exact:true}).locator('.record-note').getByText('实际任务验证完成',{exact:true})).toBeVisible();
  await dialog.getByRole('tab',{name:'关联记录 (1)'}).click();
  await expect(dialog.getByText('部分引用记录不存在或不属于当前项目')).toBeVisible();
  await dialog.getByRole('button',{name:'查看关联详情'}).click();
  await expect(dialog.getByRole('button',{name:'返回原记录'})).toBeVisible();
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog.getByRole('tabpanel',{name:'执行回执',exact:true}).locator('.record-note').getByText('实际任务验证完成',{exact:true})).toBeVisible();
  await dialog.getByRole('button',{name:'返回原记录'}).click();
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await page.screenshot({path:'.playwright/requirement-linked-checks.png'});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('a failed context request keeps local evidence and reports loading failure',async ({page})=>{
  const product={id:'failed-context',name:'关联失败回退',status:'active',version:1};
  const run={id:'failed-run',product_id:product.id,title:'保留本地证据',status:'blocked',checks:[{name:'已有检查结果',status:'blocked'}]};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route('**/api/runs/failed-run/context',r=>r.fulfill({status:500,json:{error:'测试读取失败'}}));
  await page.goto(`/autopilot?project=${product.id}&view=tasks&tab=runs`);
  await page.getByRole('button',{name:'详情',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('关联记录加载失败，当前仅展示已取得的内容')).toBeVisible();
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await expect(dialog.getByText('已有检查结果',{exact:true})).toBeVisible();
  await expect(dialog.getByRole('button',{name:'重试',exact:true})).toBeVisible();
});
