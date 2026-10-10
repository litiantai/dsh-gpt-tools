import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test('project workflow isolates records, opens nodes and supports the canvas controls', async ({page})=>{
  const fixture=JSON.parse(await readFile('.dashboard/e2e.json','utf8'));
  await page.goto('/autopilot');
  const data=await page.evaluate(async ({source})=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const post=async(path:string,body:Record<string,unknown>)=>{
      const r=await fetch('/api'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({...body,operation_id:crypto.randomUUID()})});
      if(!r.ok)throw new Error(await r.text());return r.json();
    };
    const first=await post('/products',{config:{name:'测试项目甲',source,goal:'财经助手主链路'}});
    const second=await post('/products',{config:{name:'测试项目乙',source:'/isolated/unrelated',goal:'不同项目'}});
    await post('/requirements',{product_id:first.id,title:'项目甲的行情缺陷',evidence:'isolated fixture',acceptance:['正常展示'],impact:'阻塞主链路'});
    await post('/requirements',{product_id:second.id,title:'不属于当前项目的需求',evidence:'isolated fixture',acceptance:['正常展示'],impact:'other'});
    return {first,second};
  },{source:fixture.project});
  await page.goto(`/autopilot?project=${data.first.id}`);
  await expect(page.getByRole('heading',{name:'测试项目甲',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'需求池 1',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'需求池 1',exact:true}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog').getByText('项目甲的行情缺陷',{exact:true})).toBeVisible();
  await page.locator('.ant-drawer-close').click();
  await page.getByRole('tab',{name:'需求池',exact:true}).click();
  await expect(page.getByText('项目甲的行情缺陷',{exact:true})).toBeVisible();
  await expect(page.getByText('不属于当前项目的需求',{exact:true})).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('tab',{name:'需求池',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'工作概览',exact:true}).click();
  await page.getByTitle('放大',{exact:true}).click();
  await expect(page.getByText('115%',{exact:true})).toBeVisible();
  await page.getByTitle('适应画布',{exact:true}).click();
  for(const size of [{width:1440,height:900},{width:1920,height:1080},{width:390,height:844}]){
    await page.setViewportSize(size);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:`.playwright/project-flow-${size.width}.png`,fullPage:true});
  }
  await page.setViewportSize({width:1440,height:900});
  await page.getByRole('tab',{name:'任务流水线',exact:true}).click();
  await page.getByRole('tab',{name:'执行会话',exact:true}).click();
  await expect(page.getByText('看板集成测试会话',{exact:true})).toBeVisible();
});

test('project tabs preserve navigation and settings save human units without losing execution configuration', async ({page})=>{
  await page.goto('/autopilot');
  const product=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/products',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({operation_id:crypto.randomUUID(),config:{
      name:'导航与策略测试',source:'/isolated/navigation-fixture',goal:'导航与策略验证',
      agents:{discovery:{provider:'codex',model:'fixture-model',bin:'/fixture/codex'},implementation:{provider:'harness',model:'fixture-model',model_provider:'fixture'},verification:{provider:'codex',model:'fixture-model'},acceptance:{provider:'codex',model:'fixture-model'}},
    }})});
    if(!response.ok)throw new Error(await response.text());
    return response.json();
  });
  await page.goto(`/autopilot?project=${product.id}&view=evidence&tab=evaluations`);
  await expect(page.getByRole('tab',{name:'实践评测',exact:true})).toHaveAttribute('aria-selected','true');
  await page.reload();
  await expect(page.getByRole('tab',{name:'实践评测',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'项目设置',exact:true}).click();
  await page.getByRole('tab',{name:'运行策略',exact:true}).click();
  await expect(page.getByLabel('发布前连续空闲',{exact:true})).toHaveValue('5');
  await expect(page.getByLabel('体验巡查间隔',{exact:true})).toHaveValue('6');
  await page.getByLabel('发布前连续空闲',{exact:true}).fill('7');
  await page.getByLabel('每日需求处理上限',{exact:true}).fill('5');
  await page.getByLabel('每日 Token 上限',{exact:true}).fill('10000000');
  await expect(page.getByRole('switch',{name:'DeepSeek 仅空闲时段运行',exact:true})).toBeChecked();
  await page.getByRole('switch',{name:'DeepSeek 仅空闲时段运行',exact:true}).click();
  await page.getByRole('tab',{name:'模型分工',exact:true}).click();
  await page.getByRole('tab',{name:'运行策略',exact:true}).click();
  await expect(page.getByLabel('发布前连续空闲',{exact:true})).toHaveValue('7');
  // A runtime-only version change must not invalidate the settings draft.
  await page.getByRole('button',{name:'仅监测',exact:true}).click();
  await expect(page.getByText('运行模式已更新',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'保存项目设置',exact:true}).click();
  await expect(page.getByText('项目配置已保存',{exact:true})).toBeVisible();
  const saved=await page.evaluate(async id=>(await fetch(`/api/products/${id}`)).json(),product.id);
  expect(saved.policy.idle_seconds).toBe(420);
  expect(saved.policy.runs_per_day).toBe(5);
  expect(saved.policy.tokens_per_day).toBe(10000000);
  expect(saved.policy.deepseek_off_peak_only).toBe(false);
  expect(saved.policy.inspection_seconds).toBe(21600);
  expect(saved.agents).toEqual(product.agents);
  expect(saved.status).toBe('observing');
  await page.getByRole('tab',{name:'巡查与证据',exact:true}).click();
  await expect(page.getByRole('tab',{name:'实践评测',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'项目设置',exact:true}).click();
  await expect(page.getByRole('tab',{name:'运行策略',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('link',{name:'平台设置',exact:true}).click();
  await page.getByRole('link',{name:'持续研发',exact:true}).click();
  await expect(page.getByRole('heading',{name:'导航与策略测试',exact:true})).toBeVisible();
  await expect(page.getByRole('tab',{name:'运行策略',exact:true})).toHaveAttribute('aria-selected','true');
  await page.reload();
  await expect(page.getByLabel('发布前连续空闲',{exact:true})).toHaveValue('7');
  await page.getByLabel('发布前连续空闲',{exact:true}).fill('8');
  await expect(page.getByRole('switch',{name:'DeepSeek 仅空闲时段运行',exact:true})).not.toBeChecked();
  // A second editor changing the actual configuration must still be protected.
  await page.evaluate(async id=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const latest=await (await fetch(`/api/products/${id}`)).json();
    const response=await fetch(`/api/products/${id}/configure`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
      body:JSON.stringify({operation_id:crypto.randomUUID(),version:latest.version,config:{policy:{...latest.policy,idle_seconds:540}}})});
    if(!response.ok)throw new Error(await response.text());
  },product.id);
  await page.getByRole('button',{name:'保存项目设置',exact:true}).click();
  await expect(page.getByText('保存失败，当前修改已保留',{exact:true})).toBeVisible();
  await expect(page.getByLabel('发布前连续空闲',{exact:true})).toHaveValue('8');
  await expect(page.getByRole('button',{name:'保存项目设置',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'放弃修改',exact:true}).click();
  await expect(page.getByLabel('发布前连续空闲',{exact:true})).toHaveValue('9');
  await expect(page.getByText('保存失败，当前修改已保留',{exact:true})).toHaveCount(0);
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:`.playwright/project-settings-${width}.png`,fullPage:true});
  }
});

test('readable task checks and daily reports retain technical details behind disclosure', async ({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  const product={id:'readable',name:'可读记录测试',source:'/fixture',goal:'可读记录',status:'active',version:1,created:1,updated:1,daily_report_enabled:true,nightly_attribution:true};
  const run={id:'run-readable',product_id:product.id,title:'行情展示修复',status:'accepted',version:1,created:1,updated:1,
    summary:JSON.stringify({status:'pass',reason:'中文名称已经显示',checks:[{name:'类型检查',status:'pass',required:true}]}),
    checks:[{name:'类型检查',status:'pass',required:true},{name:'外部行情连通',status:'blocked',required:false,reason:'休市期间待核对'},{name:'未知结果',required:true}],
    receipts:[{action:'verify',at:1,result:{status:'pass',reason:'已执行定向检查'}}]};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route('**/api/daily_reports',r=>r.fulfill({json:[{id:'daily',product_id:product.id,title:'2026-10-06 研发日报与复盘',status:'blocked',window_start:1,cutoff:2,exempt_usage:1234,
    attribution_signal_ids:['s1','s2'],attribution_results:[{status:'pass',signal_ids:['s1','s2'],attributions:[{signal_id:'s1',outcome:'requirement',reason:'名称映射缺陷形成修复需求'},{signal_id:'s2',outcome:'no_issue',reason:'重复巡检未发现新问题'}]}],daily_requirements:[{id:'req-nightly',title:'晚间归因需求',classification:'development'}],
    reason:'一项复验受环境限制，不能认定通过',stats:{accepted_count:1},audits:[{run_id:run.id,title:run.title,status:'blocked',reason:'测试服务未启动'}],
    retrospective:{status:'pass',summary:'已完成日报',problems:['复验环境尚未就绪'],lessons:['先核对测试环境'],next_actions:['恢复环境后再复验']}}]}));
  await page.goto('/autopilot?project=readable&view=tasks&tab=runs');
  await page.getByRole('button',{name:'详情',exact:true}).first().click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('中文名称已经显示',{exact:true})).toBeVisible();
  await dialog.getByRole('tab',{name:'验收与检查'}).click();
  await expect(dialog.getByText('非必需项',{exact:true})).toBeVisible();
  await expect(dialog.getByText('未记录结果',{exact:true})).toBeVisible();
  await expect(dialog.locator('.record-raw pre').first()).not.toBeVisible();
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog.getByText('独立验证',{exact:true})).toBeVisible();
  await expect(dialog.locator('.record-note').getByText('已执行定向检查',{exact:true})).toBeVisible();
  await page.goto('/autopilot?project=readable&view=evidence&tab=daily');
  await expect(page.getByText('每日 19:00 · 北京时间',{exact:true})).toBeVisible();
  await expect(page.getByText('晚间归因需求',{exact:true})).toBeVisible();
  await page.getByText('第 1 批 · 2 条 · 归因完成',{exact:true}).click();
  await expect(page.getByText('重复巡检未发现新问题',{exact:true})).toBeVisible();
  await expect(page.getByText('测试服务未启动',{exact:true})).toBeVisible();
  await expect(page.getByText('恢复环境后再复验',{exact:true})).toBeVisible();
  await expect(page.getByText(/1,234 Token（不扣开发额度）/)).toBeVisible();
  await page.screenshot({path:'.playwright/daily-reports-readable.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('legacy health is visible without implying automatic release readiness',async ({page})=>{
  const result={status:'pass',monitor_mode:'basic',release_monitor_available:false,reason:'正式应用在线，基础健康检查通过；当前版本未接入自动发布监测。',health:{host:'ready',activity:{known:false},desktop:{known:false},application_version:'0.1.0'}};
  await page.route('**/api/products',route=>route.fulfill({json:[{id:'legacy',name:'旧版兼容验证',status:'active',version:1,created:1,updated:1,last_probe:1,last_probe_result:result}]}));
  await page.goto('/autopilot?project=legacy&view=overview&tab=monitoring');
  const alert=page.getByRole('alert').filter({hasText:'运行监测 · 上次结果：基础健康正常 · 发布监测未接入'});
  await expect(alert).toBeVisible();
  await expect(alert).toHaveClass(/ant-alert-warning/);
  await alert.getByRole('button',{name:'查看回执'}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('基础健康正常 · 发布监测未接入',{exact:true})).toBeVisible();
  await expect(dialog.getByText('基础健康检查（兼容旧版）',{exact:true})).toBeVisible();
  await expect(dialog.getByText('自动发布监测已接入',{exact:true})).toBeVisible();
  await page.screenshot({path:'.playwright/legacy-monitor.png',fullPage:true});
});

test('investigation progress and external retry time are readable in the requirement pool',async ({page})=>{
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'progress',name:'调查推进验证',status:'active',version:1,created:1,updated:1}]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:[
    {id:'investigating',product_id:'progress',title:'调查行情路由',status:'investigating',classification:'investigation',reason:'正在只读调查根因并补充验收证据',created:1,updated:1},
    {id:'waiting',product_id:'progress',title:'登录桥环境检查',status:'awaiting_external',classification:'environment',reason:'上游服务暂时不可用',next_investigation:1791522000,created:1,updated:1},
    {id:'develop',run_id:'active-run',product_id:'progress',title:'已取证进入开发',status:'queued',classification:'development',next_investigation:1791522000,created:1,updated:1}
  ]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[{id:'active-run',product_id:'progress',requirement_id:'develop',status:'developing',reason:'正在修复已复现问题',created:1,updated:1}]}));
  await page.goto('/autopilot?project=progress&view=requirements&tab=requirements');
  await expect(page.getByText('调查取证中 · 等待执行',{exact:true})).toBeVisible();
  await expect(page.getByText('等待外部条件（自动复查）',{exact:true})).toBeVisible();
  await expect(page.getByRole('columnheader',{name:'下次调查'})).toBeVisible();
  await expect(page.getByText('上游服务暂时不可用',{exact:true})).toBeVisible();
  const developing=page.getByRole('row').filter({hasText:'已取证进入开发'});
  await expect(developing.getByText('开发中 · 等待执行',{exact:true})).toBeVisible();
  await expect(developing.getByText('正在修复已复现问题',{exact:true})).toBeVisible();
  await expect(developing.getByRole('cell').nth(4)).toHaveText('—');
});

test('Git delivery models are separately configurable and survive catalog errors', async ({page})=>{
  await page.route('**/api/reviewers/*/models', route=>route.fulfill({json:{provider:'codex',models:[{id:'review-a',name:'Review A'},{id:'review-b',name:'Review B'}],fetched_at:1791440000,source:'fixture',stale:false,error:null}}));
  await page.route('**/api/reviewers/*/models/refresh', route=>route.fulfill({status:503,json:{error:'模型目录暂不可用'}}));
  await page.goto('/autopilot');
  const product=await page.evaluate(async()=>{
    const {csrf}=await (await fetch('/api/bootstrap')).json();
    const response=await fetch('/api/products',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({operation_id:crypto.randomUUID(),config:{
      name:'代码交付模型测试',source:'/isolated/delivery-fixture',goal:'可配置评审与修复模型',
      agents:{discovery:{provider:'codex',model:'review-a'},implementation:{provider:'harness',model:'implementation-old',model_provider:'fixture'},verification:{provider:'codex',model:'review-a'},acceptance:{provider:'codex',model:'acceptance-old'}},
      code_review:{reviewer:{provider:'codex',model:'review-a'},fixer:{provider:'codex',model:'review-b'},max_revisions:3},
    }})});
    if(!response.ok)throw new Error(await response.text());return response.json();
  });
  await page.goto(`/autopilot?project=${product.id}&view=settings&tab=git`);
  await expect(page.getByRole('tab',{name:'Git 与代码评审',exact:true})).toHaveAttribute('aria-selected','true');
  const pane=page.getByRole('tabpanel',{name:'Git 与代码评审'});
  const review=pane.locator('.reviewer-select').nth(0);
  const fix=pane.locator('.reviewer-select').nth(1);
  await expect(review).toContainText('Review A');
  await expect(fix).toContainText('Review B');
  await review.locator('.ant-select-selector').nth(1).click();
  await page.locator('.ant-select-dropdown:visible').getByTitle('Review B (review-b)',{exact:true}).click();
  await pane.getByLabel('每批次每日自动修复轮数',{exact:true}).fill('0');
  await page.getByRole('button',{name:'保存项目设置',exact:true}).click();
  await expect(page.getByText('项目配置已保存',{exact:true})).toBeVisible();
  const saved=await page.evaluate(async id=>(await fetch(`/api/products/${id}`)).json(),product.id);
  expect(saved.code_review.reviewer.model).toBe('review-b');
  expect(saved.code_review.fixer.model).toBe('review-b');
  expect(saved.code_review.max_revisions).toBe(0);
  expect(saved.agents.acceptance.model).toBe('acceptance-old');
  expect(saved.agents.implementation.model).toBe('implementation-old');
  await review.getByRole('button',{name:'刷新模型'}).click();
  await expect(review).toContainText('模型目录暂不可用');
  await expect(review).toContainText('Review B');
  await page.reload();
  await expect(pane.locator('.reviewer-select').first()).toContainText('Review B');
  await expect(pane.getByLabel('每批次每日自动修复轮数',{exact:true})).toHaveValue('0');
  await page.screenshot({path:'.playwright/git-review-settings.png',fullPage:true});
});

test('overview exposes runtime credential blockers and clears them after recheck', async ({page})=>{
  const products=[{id:'git-auth',name:'运行时认证项目',status:'active',git:{url:'https://github.com/example/repo',enabled:false}}, {id:'no-git',name:'未配置仓库项目',status:'active'}];
  await page.route('**/api/products',r=>r.fulfill({json:products}));
  await page.route('**/api/products/*/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  let ready=false;
  await page.route('**/api/products/git-auth/git-status',r=>r.fulfill({json:{status:ready?'pass':'blocked',code:ready?'ready':'missing_credentials',reason:'本地运行时未找到 GitHub 凭据，推送、创建 PR 和合并已阻塞。',checked_at:1791440000}}));
  await page.goto('/autopilot?project=git-auth&view=overview');
  const alert=page.locator('#project-git-issue');
  await expect(alert).toBeVisible();
  await expect(alert).toContainText('缺少 GitHub 凭据');
  await expect(page.getByRole('button',{name:/需要关注/})).toContainText('1');
  await page.screenshot({path:'.playwright/git-runtime-credentials.png',fullPage:true});
  await page.locator('.ant-select[aria-label="选择项目"] .ant-select-selector').click();
  await page.getByTitle('未配置仓库项目',{exact:true}).click();
  await expect(alert).toHaveCount(0);
  await page.locator('.ant-select[aria-label="选择项目"] .ant-select-selector').click();
  await page.getByTitle('运行时认证项目',{exact:true}).click();
  await expect(alert).toBeVisible();
  ready=true;
  await alert.getByRole('button',{name:'重新检查'}).click();
  await expect(alert).toHaveCount(0);
});

test('GitHub token is saved separately, masked, and cleared after validation',async ({page})=>{
  const product={id:'token-form',name:'Token 配置验证',status:'paused',version:1,git:{url:'https://github.com/example/repo',enabled:false}};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/token-form/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  let saved=false;
  let requestBody:Record<string,unknown> | undefined;
  await page.route('**/api/products/token-form/git-token',r=>{
    if(r.request().method()==='GET')return r.fulfill({json:{configured:saved}});
    requestBody=r.request().postDataJSON();
    if(requestBody?.token==='invalid')return r.fulfill({status:400,json:{error:'Token 格式无效'}});
    saved=true;return r.fulfill({json:{saved:true}});
  });
  await page.route('**/api/products/token-form/git-status',r=>r.fulfill({json:{status:saved?'pass':'blocked',reason:'缺少凭据'}}));
  await page.goto('/autopilot?project=token-form&view=settings&tab=git');
  const input=page.getByRole('tabpanel',{name:'Git 与代码评审'}).getByLabel('本机 GitHub Token',{exact:true});
  await expect(input).toHaveAttribute('type','password');
  await input.fill('invalid');
  await page.getByRole('button',{name:'验证并保存 Token',exact:true}).click();
  await expect(page.getByText('Token 格式无效',{exact:true})).toBeVisible();
  await input.fill('github_pat_TEST_ONLY_0123456789');
  await page.getByRole('button',{name:'验证并保存 Token',exact:true}).click();
  await expect(page.getByText('GitHub Token 已验证并保存到本机',{exact:true})).toBeVisible();
  await expect(input).toHaveValue('');
  expect(requestBody?.token).toBe('github_pat_TEST_ONLY_0123456789');
  await expect(page.getByRole('button',{name:'保存项目设置',exact:true})).toBeDisabled();
  await page.reload();
  await expect(input).toHaveValue('');
  await expect(page.getByText(/已保存 Token；输入新值可替换/)).toBeVisible();
});
