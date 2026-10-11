import { test, expect, type Page } from '@playwright/test';

const product={id:'plan-project',name:'开发方案预览测试',status:'active',version:1};
const markdown=['# 独立开发方案','','## 实现步骤','','- **拆分详情 Tab**','- [x] 支持预览','- [ ] 完成验收','','| 模块 | 改动 |','| --- | --- |','| 任务详情 | 新增方案页 |','','```ts','const ready = true;','```','','[设计文档](https://example.com/design)','','<script>window.planInjected = true</script>','','[不安全链接](javascript:alert(1))'].join('\n');

async function setup(page:Page,plan?:string) {
  const run={id:'plan-run',product_id:product.id,title:'独立方案任务',status:'planning',plan};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route('**/api/runs/plan-run/context',r=>r.fulfill({json:{record:run,related:[],missing:[]}}));
  await page.goto(`/autopilot?project=${product.id}&view=tasks&tab=runs`);
  await page.getByRole('button',{name:'详情',exact:true}).click();
  return page.getByRole('dialog');
}

test('development plan has a dedicated tab with safe GFM preview and unchanged source',async ({page})=>{
  const dialog=await setup(page,markdown);
  await expect(dialog.getByRole('tabpanel',{name:'概况与判断',exact:true})).not.toContainText('独立开发方案');
  await dialog.getByRole('tab',{name:'开发方案',exact:true}).click();
  const panel=dialog.getByRole('tabpanel',{name:'开发方案',exact:true});
  await expect(panel.getByRole('heading',{name:'独立开发方案',exact:true})).toBeVisible();
  await expect(panel.getByRole('table')).toContainText('新增方案页');
  await expect(panel.getByRole('checkbox').first()).toBeChecked();
  await expect(panel.locator('pre code')).toHaveText('const ready = true;\n');
  await expect(panel.getByRole('link',{name:'设计文档'})).toHaveAttribute('rel','noopener noreferrer');
  await expect(panel.locator('script')).toHaveCount(0);
  await expect(panel.locator('a[href^="javascript:"]')).toHaveCount(0);
  await panel.getByText('源码',{exact:true}).click();
  expect(await panel.locator('.plan-source').textContent()).toBe(markdown);
  await panel.getByText('Markdown 预览',{exact:true}).click();
  await expect(panel.getByRole('heading',{name:'独立开发方案',exact:true})).toBeVisible();
  await page.screenshot({path:'.playwright/development-plan.png'});
  await page.setViewportSize({width:390,height:844});
  await expect(panel.getByRole('heading',{name:'独立开发方案',exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('requirement plans resolve JSON receipts on linked tasks and remove duplicates',async ({page})=>{
  const requirement={id:'plan-requirement',product_id:product.id,title:'关联方案需求',status:'accepted'};
  const run={id:'plan-run',title:'关联开发任务',receipts:[{result:JSON.stringify({plan:markdown})},{result:{plan:markdown}}]};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/requirements',r=>r.fulfill({json:[requirement]}));
  await page.route('**/api/requirements/plan-requirement/context',r=>r.fulfill({json:{record:requirement,related:[{kind:'runs',record:run}],missing:[]}}));
  await page.goto(`/autopilot?project=${product.id}&view=requirements&tab=requirements`);
  await page.getByRole('button',{name:'详情',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('tab',{name:'开发方案',exact:true}).click();
  await expect(dialog.getByText('研发任务 · 关联开发任务',{exact:true})).toBeVisible();
  await expect(dialog.getByRole('heading',{name:'独立开发方案',exact:true})).toHaveCount(1);
});

test('development plan shows an empty state before a plan is generated',async ({page})=>{
  const dialog=await setup(page);
  await dialog.getByRole('tab',{name:'开发方案',exact:true}).click();
  await expect(dialog.getByText('尚未生成开发方案',{exact:true})).toBeVisible();
});
