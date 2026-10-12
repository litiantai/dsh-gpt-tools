import {test,expect} from '@playwright/test';

const roles=Object.fromEntries(['discovery','implementation','verification','acceptance'].map(k=>[k,{provider:'codex',model:'gpt-6.1-sol'}]));
const base={id:'design-fixture',version:1,name:'多业务线设计测试',source:'/fixture/design',goal:'报告生成',status:'active',agents:roles,
  policy:{deepseek_off_peak_only:true,probe_seconds:300,inspection_seconds:3600,runs_per_day:5,tokens_per_day:0,discovery_per_day:10,execution_seconds:600,max_revisions:3,idle_seconds:300,observation_seconds:300}};

test.beforeEach(async({page})=>{
  await page.route('**/api/products/design-fixture/delivery-board',r=>r.fulfill({json:{prs:[],issues:[],release_prs:[]}}));
  await page.route('**/api/products/design-fixture/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/design-fixture/automation',r=>r.fulfill({json:{tokens_used:0,tasks_used:0,queued:0}}));
});

test('竞品三个独立可选输入保存，不启动研究；高峰期开关取反保存',async({page})=>{
  let product={...base}; let config:Record<string,unknown>={};let researchCalls=0;let saved:Record<string,unknown>={};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/design-fixture/intelligence/settings',async r=>{
    if(r.request().method()==='POST'){config=r.request().postDataJSON().config;product={...product,version:product.version+1};}
    await r.fulfill({json:{config,version:product.version,capabilities:{research:{reason:'测试执行器'}}}});
  });
  await page.route('**/api/products/design-fixture/intelligence/research_jobs',r=>{researchCalls++;return r.fulfill({json:{}});});
  await page.route('**/api/products/design-fixture/configure',async r=>{saved=r.request().postDataJSON().config;product={...product,...saved,version:product.version+1};await r.fulfill({json:product});});
  await page.goto('/autopilot?project=design-fixture&view=settings&tab=research');
  await page.getByLabel('竞品名称',{exact:true}).fill('Example');await page.getByLabel('竞品名称',{exact:true}).press('Enter');
  await page.getByLabel('重点比较方向').fill('报告生成体验');
  await page.getByLabel('分析目标').fill('');
  await page.getByRole('button',{name:'保存设置',exact:true}).click();
  await expect.poll(()=>config.competitor_names).toEqual(['Example']);
  expect(config.comparison_focus).toBe('报告生成体验');expect(config.analysis_goal).toBe('');expect(researchCalls).toBe(0);
  await page.getByRole('tab',{name:'运行策略',exact:true}).click();
  const allow=page.getByRole('switch',{name:'允许高峰期使用 DeepSeek',exact:true});
  await expect(allow).not.toBeChecked();await allow.click();
  await page.getByRole('button',{name:'保存项目设置',exact:true}).click();
  await expect.poll(()=>saved.policy).toMatchObject({deepseek_off_peak_only:false});
  expect(saved).not.toHaveProperty('agents');
  await page.screenshot({path:'.playwright/peak-setting.png'});
});

test('业务路线与联合项目展示版本证据、所属仓库及沙箱 UI 预览',async({page})=>{
  await page.route('**/api/products',r=>r.fulfill({json:[base]}));
  await page.route('**/api/products/design-fixture/inspection-routes',r=>r.fulfill({json:{routes:[{id:'route',version:1,title:'报告生成链路',business_line:'财经报告',environment:'isolated',enabled:true,status:'ready',definition:{source_commit:'abc',steps:[{title:'查看报告',expected:'报告内容可见'}]}}],plans:[],rounds:[]}}));
  await page.goto('/autopilot?project=design-fixture&view=evidence&tab=inspection_routes');
  await expect(page.getByText('财经报告',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'报告生成链路'}).click();
  await expect(page.getByRole('dialog').getByText('报告内容可见',{exact:false}).first()).toBeVisible();
  await page.getByRole('button',{name:'Close',exact:true}).click();
  await page.route('**/api/products/design-fixture/requirement-groups',r=>r.fulfill({json:[{id:'group',version:1,title:'联合报告需求',status:'partially_merged',members:[{project_id:'front',project_name:'报告前端',repository:'https://github.com/example/front',scope:'报告页',status:'accepted',prs:['https://github.com/example/front/pull/1'],release_pr:'https://github.com/example/front/pull/2'},{project_id:'back',project_name:'报告后端',repository:'https://github.com/example/back',scope:'报告接口',status:'online',prs:['https://github.com/example/back/pull/1']}],designs:[{id:'ui-v1',kind:'ui',product_id:'front',content:{html:'<h1>报告预览</h1>',fields:['title:string']}}]}]}));
  await page.goto('/autopilot?project=design-fixture&view=tasks&tab=requirement_groups');
  await expect(page.getByText('部分合并',{exact:true})).toBeVisible();
  await expect(page.getByRole('link',{name:'报告前端 · 需求 PR'})).toBeVisible();
  await expect(page.getByRole('link',{name:'报告后端 · 需求 PR'})).toBeVisible();
  await page.getByText('UI 设计 · ui-v1',{exact:true}).click();
  await expect(page.locator('iframe')).toHaveAttribute('sandbox','allow-scripts');
  await expect(page.frameLocator('iframe').getByRole('heading',{name:'报告预览'})).toBeVisible();
  await expect.poll(()=>page.locator('iframe').evaluate(node=>node.closest('.ant-collapse-content')!.getBoundingClientRect().height)).toBeGreaterThan(500);
  await expect(page.getByText('记录不存在',{exact:true})).toHaveCount(0);
  await page.screenshot({path:'.playwright/linked-projects.png',fullPage:true});
});
