import { test, expect } from '@playwright/test';

test('PR list defaults to unmerged and repair list contains each unfinished issue', async ({ page }) => {
  const base = { product_id:'history',version:1,created:1,updated:1 };
  const product = { ...base,id:'history',name:'PR 与问题项目',status:'active' };
  const pr = { id:'pr1',pr_url:'https://github.com/example/repo/pull/1',number:1,requirement_id:'req1',requirement_title:'修复数据类型',title:'修复基线',status:'unreviewed',git_status:'open',updated:1 };
  const issue = { id:'issue1',pr_url:pr.pr_url,title:'缺少输入校验',path:'a.ts',severity:'medium',status:'repairing',created:1,updated:1,batch_title:'交付' };
  let issues = [issue,{...issue,id:'issue2',title:'错误处理缺失',path:'b.ts'}];
  await page.route('**/api/products', r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/history/delivery-board', r=>r.fulfill({json:{
    prs:[pr,{...pr,id:'pr2',pr_url:'https://github.com/example/repo/pull/2',number:2,title:'已合并基线',status:'merged'},
      {...pr,id:'pr3',pr_url:'https://github.com/example/repo/pull/3',number:3,title:'关闭的 PR',status:'unreviewed',git_status:'closed'}],release_prs:[{...pr,id:'release',pr_url:'https://github.com/example/repo/pull/4',number:4,title:'每日上线 PR',status:'approved',delivery_status:'validating'}],issues,checked_at:1,
  }}));
  await page.route('**/api/products/history/workbench', r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/requirements/req1/context', r=>r.fulfill({json:{record:{id:'req1',title:'修复数据类型',status:'accepted',evidence:'PR 对应的需求依据'},related:[],missing:[]}}));
  await page.goto('/autopilot?project=history');
  await page.getByRole('button',{name:'代码评审 1',exact:true}).click();
  const drawer=page.locator('.ant-drawer-content');
  await expect(drawer.getByRole('link',{name:'#1 修复基线',exact:true})).toHaveCount(1);
  await expect(drawer.getByRole('link',{name:'#2 已合并基线',exact:true})).toHaveCount(0);
  await expect(drawer.getByRole('link',{name:'#3 关闭的 PR',exact:true})).toHaveCount(0);
  await expect(drawer.getByText('修复数据类型',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('link',{name:'#4 每日上线 PR',exact:true})).toHaveCount(0);
  await drawer.getByRole('button',{name:'查看证据',exact:true}).click();
  await expect(page.locator('.ant-modal-content').getByText('PR 对应的需求依据',{exact:true})).toBeVisible();
  await page.locator('.ant-modal-close').click();
  await drawer.getByText('未合并（默认）',{exact:true}).click();
  await page.getByText('全部 PR',{exact:true}).click();
  await expect(drawer.getByRole('link',{name:'#2 已合并基线',exact:true})).toBeVisible();
  await expect(drawer.getByText('代码评审历史')).toHaveCount(0);
  await drawer.locator('.ant-drawer-close').click();
  await page.getByRole('button',{name:'待合并 1',exact:true}).click();
  await expect(drawer.getByRole('link',{name:'#4 每日上线 PR',exact:true})).toBeVisible();
  await expect(drawer.getByRole('link',{name:'#1 修复基线',exact:true})).toHaveCount(0);
  await drawer.locator('.ant-drawer-close').click();
  await page.getByRole('button',{name:'问题修复 2',exact:true}).click();
  await expect(drawer.getByText('缺少输入校验',{exact:true})).toBeVisible();
  await expect(drawer.getByText('错误处理缺失',{exact:true})).toBeVisible();
  await expect(drawer.getByRole('link',{name:'PR #1',exact:true})).toHaveCount(2);
  issues=[];
  await drawer.locator('.ant-drawer-close').click();
  await page.getByRole('button',{name:'刷新项目',exact:true}).click();
  await page.getByRole('button',{name:'问题修复 0',exact:true}).click();
  await expect(drawer.getByText('缺少输入校验',{exact:true})).toHaveCount(0);
});
