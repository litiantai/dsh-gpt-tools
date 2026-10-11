import { test, expect } from '@playwright/test';
test('deferred validation finding shows its requirement and never retries the merged batch', async ({ page }) => {
  const product={id:'backlog',name:'缺陷衔接项目',status:'active',version:1,created:1,updated:1};
  const requirement={id:'finding',title:'修复扫描能力预检',status:'queued',updated:9};
  const issue={id:'issue',delivery_id:'batch',pr_url:'https://github.com/example/repo/pull/2',
    title:'能力确认前允许提交扫描',path:'',severity:'',status:'backlog',created:2,updated:9,
    batch_title:'已合并的交付',requirement,retryable:false,reason:'该验收缺陷已转入需求池，修复进度以关联需求为准。'};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/backlog/delivery-board',r=>r.fulfill({json:{prs:[],release_prs:[],issues:[issue]}}));
  await page.route('**/api/products/backlog/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/requirements/finding/context',r=>r.fulfill({json:{record:{...requirement,evidence:'需求关联的原始失败证据'},related:[],missing:[]}}));
  await page.goto('/autopilot?project=backlog');
  await page.getByRole('button',{name:'问题修复 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  await expect(drawer.getByText('已转需求 · 排队等待',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('button',{name:'重试修复',exact:true})).toHaveCount(0);
  await drawer.getByRole('button',{name:'查看关联需求',exact:true}).click();
  await expect(page.locator('.ant-modal-content').getByText('需求关联的原始失败证据',{exact:true})).toBeVisible();
});
