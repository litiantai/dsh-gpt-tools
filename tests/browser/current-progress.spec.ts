import { test, expect } from '@playwright/test';

test('current repair progress refreshes in an open drawer and retry follows eligibility', async ({page})=>{
  const base={product_id:'live',version:4,created:1,updated:10};
  const product={...base,id:'live',name:'当前状态项目',status:'active'};
  const batch={...base,id:'batch',title:'当前交付',status:'syncing_feature'};
  let issue:any={id:'issue',delivery_id:'batch',delivery_version:4,pr_url:'https://github.com/example/repo/pull/1',title:'输入校验缺失',path:'a.ts',severity:'high',status:'waiting_update',delivery_status:'syncing_feature',created:1,updated:10,batch_title:'当前交付',reason:'平台更新：健康观察，新任务等待派发',retryable:false,retry_reason:'等待平台更新',last_attempt:{status:'blocked',reason:'旧提交已变化',updated:3}};
  let dispatch:any={status:'observing',reason:issue.reason};
  let submitted:any;
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/deliveries',r=>r.fulfill({json:[batch]}));
  await page.route('**/api/products/live/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/live/delivery-board',r=>r.fulfill({json:{prs:[],issues:[issue],dispatch}}));
  await page.route('**/api/deliveries/batch/retry',async r=>{submitted=r.request().postDataJSON();await r.fulfill({json:{}});});
  await page.goto('/autopilot?project=live');
  await page.getByRole('button',{name:'问题修复 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  await expect(drawer.getByText('等待平台更新',{exact:true})).toBeVisible();
  await expect(drawer.getByText('旧提交已变化',{exact:true})).not.toBeVisible();
  await expect(drawer.getByRole('button',{name:'重试修复'})).toBeDisabled();
  dispatch=null;
  issue={...issue,status:'running',reason:'',call:{id:'new',action:'review_feature',started:11},retry_reason:'当前执行尚未结束'};
  await expect(drawer.getByText('Code Review · feat → release · 执行中',{exact:true})).toBeVisible({timeout:12000});
  await expect(drawer.getByRole('button',{name:'重试修复'})).toBeInViewport();
  await page.screenshot({path:'.playwright/current-progress.png'});
  await drawer.locator('summary').click();
  await expect(drawer.getByText('旧提交已变化',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('button',{name:'重试修复'})).toBeDisabled();
  issue={...issue,status:'blocked',call:null,reason:'本轮模型连接失败',retryable:true,retry_reason:'',delivery_version:5};
  await expect(drawer.getByText('本轮模型连接失败',{exact:true})).toBeVisible({timeout:12000});
  await drawer.getByRole('button',{name:'重试修复'}).click();
  await expect.poll(()=>submitted?.version).toBe(5);
  expect(submitted.operation_id).toMatch(/^[0-9a-f-]{36}$/);
});

test('monitoring and open record detail show the active call instead of old failure', async ({page})=>{
  const base={product_id:'live',version:1,created:1,updated:10};
  let product:any={...base,id:'live',name:'进行中项目',status:'active',last_probe:1,last_probe_result:{status:'blocked',reason:'上一轮监测失败'},call:{id:'probe-new',action:'probe',started:10}};
  let run:any={...base,id:'run',title:'验证运行状态',status:'developing',reason:'旧失败原因',result:{failure_kind:'agent_execution',reason:'上一轮模型失败'},call:{id:'develop-new',action:'develop',started:10}};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route('**/api/products/live/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/live/delivery-board',r=>r.fulfill({json:{prs:[],issues:[]}}));
  await page.route('**/api/runs/run/context',r=>r.fulfill({json:{record:run,related:[],missing:[]}}));
  await page.goto('/autopilot?project=live');
  const monitor=page.getByRole('alert').filter({hasText:'运行监测 · 执行中'});
  await expect(monitor).toBeVisible();
  await expect(monitor).toContainText('本轮开始于');
  await expect(monitor).toContainText('上次结果');
  await expect(monitor.getByText('上一轮监测失败',{exact:true})).not.toBeVisible();
  product={...product,call:null,last_probe:12,last_probe_result:{status:'pass',reason:'新一轮通过'}};
  await expect(page.getByRole('alert').filter({hasText:'运行监测 · 上次结果：通过'})).toBeVisible({timeout:12000});
  await page.getByRole('button',{name:'开发执行 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  await expect(drawer.getByText('开发实现 · 执行中',{exact:true})).toBeVisible();
  await expect(drawer.getByText('Agent 运行异常',{exact:true})).toHaveCount(0);
  await expect(drawer.getByText('旧失败原因',{exact:true})).toHaveCount(0);
  await drawer.getByRole('button',{name:'详情',exact:true}).click();
  const modal=page.locator('.ant-modal-content');
  const progress=modal.getByRole('tabpanel',{name:'概况与判断'}).getByRole('alert');
  await expect(progress).toContainText('开发实现 · 执行中');
  run={...run,status:'verifying',call:{id:'verify-new',action:'verify',started:13}};
  await expect(progress).toContainText('独立验证 · 执行中',{timeout:12000});
  run={...run,status:'cancelling',reason:'等待验证进程停止'};
  await expect(progress).toContainText('正在取消 · 等待执行结束',{timeout:12000});
});


test('an open inspection summary follows the refreshed record', async ({page})=>{
  const product={id:'inspection-live',name:'巡查状态项目',status:'active',version:1,created:1,updated:1};
  let inspection={id:'inspection',product_id:product.id,title:'启动巡查',status:'running',created:1,judgement:''};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/inspections',r=>r.fulfill({json:[inspection]}));
  await page.route('**/api/inspections/inspection/context',r=>r.fulfill({json:{record:inspection,related:[],missing:[]}}));
  await page.goto('/autopilot?project=inspection-live&view=evidence&tab=inspections');
  await page.getByRole('button',{name:'启动巡查',exact:true}).click();
  const modal=page.locator('.ant-modal-content');
  await expect(modal.locator('p').filter({hasText:'判断结论：'})).toContainText('巡查仍在进行中');
  inspection={...inspection,status:'pass',judgement:'启动及停止均已核实'};
  await expect(modal.locator('p').filter({hasText:'判断结论：'})).toContainText('启动及停止均已核实',{timeout:12000});
});


test('review phases use the current linked review and queued phases stay explicit', async ({page})=>{
  const product={id:'phase-live',name:'审查阶段项目',status:'active',version:1};
  const records=['plan_review','acceptance_review','syncing','repairing'].map(status=>({id:status,product_id:product.id,title:status,status,created:1,updated:1}));
  records.push({id:'linked',product_id:product.id,title:'已有审查',status:'plan_review',created:1,updated:1,review_id:'review'} as typeof records[number]);
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:records}));
  await page.route('**/api/products/phase-live/workbench',r=>r.fulfill({json:{sessions:[],reviews:[{id:'review',status:'running',created:1}],events:[]}}));
  await page.goto('/autopilot?project=phase-live&view=tasks&tab=runs');
  for(const id of ['plan_review','acceptance_review','syncing','repairing']) {
    await expect(page.getByRole('row').filter({has:page.getByRole('button',{name:id,exact:true})})).toContainText('等待执行');
  }
  await expect(page.getByRole('row').filter({has:page.getByRole('button',{name:'已有审查',exact:true})})).toContainText('方案审查 · 执行中');
});
