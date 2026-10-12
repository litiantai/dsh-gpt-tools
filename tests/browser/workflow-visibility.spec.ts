import { test, expect } from '@playwright/test';

test('human allowance requests one uncounted repair from the waiting task', async ({page})=>{
  const product={id:'manual-repair',name:'人工允许返修项目',status:'active',version:1,created:1,updated:1};
  let run={id:'repair-run',product_id:product.id,title:'通知能力实现',status:'waiting_for_reply',version:3,created:1,updated:1,reason:'返修已达上限，请人工确认授权范围',revisions:3,extra_revisions:0};
  const requests:Record<string,unknown>[]=[];
  let reject=true;
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:[]}));
  await page.route('**/api/deliveries',r=>r.fulfill({json:[]}));
  await page.route(`**/api/products/${product.id}/workbench`,r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route(`**/api/products/${product.id}/delivery-board`,r=>r.fulfill({json:{prs:[],issues:[]}}));
  await page.route(`**/api/runs/${run.id}/allow-revision`,async r=>{
    requests.push(r.request().postDataJSON());
    if(reject){await r.fulfill({status:409,json:{error:'记录已更新，请刷新后重试'}});return;}
    run={...run,status:'developing',version:4};
    await r.fulfill({json:run});
  });
  await page.goto(`/autopilot?project=${product.id}`);
  await page.getByRole('button',{name:'异常阻塞 1',exact:true}).click();
  const drawer=page.getByRole('dialog',{name:'链路节点 · 任务与证据'});
  const allow=drawer.getByRole('button',{name:'人工允许返修',exact:true});
  await expect(drawer.getByRole('button',{name:'人工修复',exact:true})).toHaveCount(0);
  await allow.click();
  await expect(page.getByText('记录已更新，请刷新后重试',{exact:true})).toBeVisible();
  await expect(allow).toBeEnabled();
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({version:3});
  reject=false;
  await allow.click();
  await expect(page.getByText('已允许一轮返修，不计入返修次数',{exact:true})).toBeVisible();
  await expect(allow).toHaveCount(0);
  expect(requests).toHaveLength(2);
  await page.locator('.ant-drawer-close').click();
  await expect(page.getByRole('button',{name:'开发执行 1',exact:true})).toBeVisible();
});

test('waiting replies and unmapped states remain visible after a task leaves blocked', async ({page})=>{
  const product={id:'flow-visibility',name:'流程可见性项目',status:'active',version:1,created:1,updated:1};
  const record=(id:string,status:string,title:string,product_id=product.id)=>({id,status,title,product_id,version:1,created:1,updated:1});
  let notification={...record('notification','blocked','通知能力实现'),reason:'返修已达上限，等待人工确认'};
  const otherRuns=[record('unknown','future_review_state','新增状态任务'),record('pause','pausing','暂停中的任务'),
    record('develop','developing','正常开发任务'),record('done','completed','已完成任务'),record('online','online','已上线任务'),
    record('cancel','cancelled','已取消任务'),record('rollback','rolled_back','已回滚任务'),
    record('other','waiting_for_reply','其他项目任务','another-project')];
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[notification,...otherRuns]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:[]}));
  await page.route('**/api/deliveries',r=>r.fulfill({json:[]}));
  await page.route(`**/api/products/${product.id}/workbench`,r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route(`**/api/products/${product.id}/delivery-board`,r=>r.fulfill({json:{prs:[],issues:[]}}));
  await page.goto(`/autopilot?project=${product.id}`);
  await expect(page.getByRole('button',{name:'开发执行 1',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'已终止 2',exact:true})).toBeVisible();
  await expect(page.locator('.project-summary-bar button').filter({hasText:'需要关注'}).locator('strong')).toHaveText('3');
  await page.getByRole('button',{name:'异常阻塞 3',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  await expect(drawer.locator('tbody tr[data-row-key]')).toHaveCount(3);
  for(const title of ['通知能力实现','新增状态任务','暂停中的任务']) {
    await expect(drawer.getByText(title,{exact:true})).toBeVisible();
  }
  for(const title of ['正常开发任务','已完成任务','已上线任务','已取消任务','已回滚任务','其他项目任务']) {
    await expect(drawer.getByText(title,{exact:true})).toHaveCount(0);
  }
  notification={...notification,status:'waiting_for_reply'};
  await expect(drawer.getByText('等待协作回复',{exact:true})).toBeVisible({timeout:12000});
  await expect(drawer.getByText('通知能力实现',{exact:true})).toBeVisible();
  await expect(drawer.getByText(notification.reason,{exact:true})).toBeVisible();
  await expect(drawer.locator('tbody tr[data-row-key]')).toHaveCount(3);
  await page.locator('.ant-drawer-close').click();
  await expect(page.getByRole('button',{name:'异常阻塞 3',exact:true})).toBeVisible();
  await page.reload();
  await page.getByRole('button',{name:'异常阻塞 3',exact:true}).click();
  await expect(drawer.getByText('通知能力实现',{exact:true})).toBeVisible();
  await expect(drawer.getByText('等待协作回复',{exact:true})).toBeVisible();
});

test('requirement watch and durable help plans are visible in project coordination', async ({page})=>{
  const product={id:'watch-plan',name:'需求检查与帮助方案',status:'active',version:1,created:1,updated:1};
  const help_plan={basis:'控制器检查发现通知凭证隔离失败',steps:['隔离凭证存储并补回归'],resources:['implementation 一轮开发槽'],verification:['跨项目凭证回归通过'],stop_conditions:['两轮无进展转人工']};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/watch-plan/coordinator',r=>r.fulfill({json:{version:1,enabled:true,config:{enabled:true},model:{provider:'codex',model:'fixture'},coordination_tokens:120,
    instance:{status:'idle',last_check:1791782995,reason:'等待已批准协调的资源',watch:{requirements_checked:4,tasks_checked:2,abnormal_count:1,findings:[{run_id:'notify',title:'通知能力实现',status:'blocked',reason:'凭证隔离检查失败',action:'评估帮助与资源协调'}]}},
    decisions:[{id:'plan-1',status:'applied',created:1791782995,help_plan,reason:'安排一轮凭证隔离修复',result:{action:'repair',run_id:'notify'}}]}}));
  await page.goto('/autopilot?project=watch-plan&view=tasks&tab=coordinator');
  await expect(page.getByText('4 条需求 · 2 个未结束任务 · 1 项待关注',{exact:true})).toBeVisible();
  await expect(page.getByRole('cell',{name:'通知能力实现',exact:true})).toBeVisible();
  await expect(page.getByRole('cell',{name:'评估帮助与资源协调',exact:true})).toBeVisible();
  await page.locator('tr[data-row-key="plan-1"] .ant-table-row-expand-icon').click();
  for(const text of [help_plan.basis,...help_plan.steps,...help_plan.resources,...help_plan.verification,...help_plan.stop_conditions]) {
    await expect(page.locator('.ant-descriptions').getByText(text,{exact:true})).toBeVisible();
  }
  await page.reload();
  await expect(page.getByRole('cell',{name:'已记录 · 展开查看',exact:true})).toBeVisible();
});
