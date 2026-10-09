import { test, expect } from '@playwright/test';

test('online lists and counts requirements, keeping shared PRs and duplicate runs separate from the count', async ({page})=>{
  const base={product_id:'online-project',version:1,created:1,updated:1};
  const product={...base,id:'online-project',name:'上线需求项目',status:'active'};
  const batch={...base,id:'batch',title:'release-20261009',status:'online',pr_url:'https://github.com/example/repo/pull/30'};
  const requirement={...base,id:'req-a',title:'修复行情展示',status:'online',delivery_id:'batch',evidence:'已验证行情恢复'};
  let requirements=[requirement,{...requirement,id:'req-b',title:'修复搜索结果'},
    {...requirement,id:'req-waiting',title:'仅合入 release 的需求',status:'delivered'},
    {...requirement,id:'req-foreign',title:'其他项目需求',product_id:'foreign'}];
  const pr={id:'req-review-a',requirement_id:'req-review-a',requirement_title:'评审需求甲',pr_url:'https://github.com/example/repo/pull/31',number:31,title:'共享评审 PR',status:'unreviewed',git_status:'open',updated:1};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:requirements}));
  await page.route('**/api/deliveries',r=>r.fulfill({json:[batch]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[
    {...base,id:'run-a',title:'任务甲',requirement_id:'req-a',status:'online'},
    {...base,id:'run-a-retry',title:'任务甲重试',requirement_id:'req-a',status:'online'},
    {...base,id:'run-b',title:'任务乙',requirement_id:'req-b',status:'online'},
  ]}));
  await page.route('**/api/products/online-project/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/online-project/delivery-board',r=>r.fulfill({json:{
    prs:[pr,{...pr,id:'req-review-b',requirement_id:'req-review-b',requirement_title:'评审需求乙'},
      {...pr,id:'duplicate',number:32,pr_url:'https://github.com/example/repo/pull/32'},
      {...pr,id:'closed',requirement_id:'closed',git_status:'closed'},
      {...pr,id:'merged',requirement_id:'req-waiting',status:'merged',git_status:'merged'}],
    release_prs:[],issues:[],
  }}));
  await page.goto('/autopilot?project=online-project');
  await expect(page.getByRole('button',{name:'代码评审 2',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'已上线 2',exact:true}).click();
  const drawer=page.getByRole('dialog',{name:'已上线 · 需求记录',exact:true});
  await expect(drawer.getByText('2 条需求',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('button',{name:'修复行情展示',exact:true})).toBeVisible();
  await expect(drawer.getByRole('button',{name:'修复搜索结果',exact:true})).toBeVisible();
  for(const name of ['release-20261009','仅合入 release 的需求','其他项目需求','任务甲','任务甲重试','任务乙']) {
    await expect(drawer.getByText(name,{exact:true})).toHaveCount(0);
  }
  await expect(drawer.getByRole('button',{name:'展开问题'})).toHaveCount(0);
  await expect(drawer.getByRole('link',{name:'查看 PR'})).toHaveCount(2);
  await drawer.getByRole('button',{name:'修复行情展示',exact:true}).click();
  await expect(page.locator('.ant-modal-content').getByText('已验证行情恢复',{exact:true})).toBeVisible();
  await page.locator('.ant-modal-close').click();
  await drawer.getByRole('searchbox',{name:'搜索当前记录'}).fill('搜索');
  await expect(drawer.getByText('1 条需求',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('button',{name:'修复行情展示',exact:true})).toHaveCount(0);
  await drawer.locator('.ant-drawer-close').click();
  requirements=requirements.map(r=>({...r,status:'delivered'}));
  await page.getByRole('button',{name:'刷新项目',exact:true}).click();
  await page.getByRole('button',{name:'已上线 0',exact:true}).click();
  await expect(drawer.getByText('0 条需求',{exact:true})).toBeVisible();
  await expect(drawer.getByText('该节点当前无待处理任务',{exact:true})).toBeVisible();
});
