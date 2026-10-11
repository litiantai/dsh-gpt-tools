import {test, expect} from '@playwright/test';

test('manual retries are displayed separately and their original blocking evidence remains accessible', async ({page})=>{
  const product={id:'manual-retry-project',name:'人工重试验证',status:'active',version:1};
  const run={id:'manual-retry-run',product_id:product.id,title:'超过三次仍可人工重试',status:'blocked',version:9,
    revisions:3,extra_revisions:0,manual_retry_count:7,reason:'本轮验证失败'};
  const evidence={id:'manual-retry-evidence',product_id:product.id,run_id:run.id,title:'人工阻塞重试',status:'recorded',
    phase:'manual_retry',details:{source:'human',manual_retry_count:7,reason:'原始验收未通过',from:'blocked',to:'developing',counts_toward_revisions:false}};
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/runs',r=>r.fulfill({json:[run]}));
  await page.route(`**/api/runs/${run.id}/context`,r=>r.fulfill({json:{record:run,related:[{kind:'evidence',record:evidence}],missing:[]}}));
  await page.route(`**/api/evidence/${evidence.id}/context`,r=>r.fulfill({json:{record:evidence,related:[{kind:'runs',record:run}],missing:[]}}));
  await page.goto(`/autopilot?project=${product.id}&view=tasks&tab=runs`);
  await expect(page.getByRole('button',{name:'重试',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'详情',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText(/基础返修 3 次 · 额外返修 0 次 · 人工重试 7 次（不占返修额度）/)).toBeVisible();
  await dialog.getByRole('tab',{name:'关联记录 (1)',exact:true}).click();
  await dialog.getByRole('button',{name:'查看关联详情'}).click();
  await expect(dialog.getByText('原始验收未通过',{exact:true})).toBeVisible();
  await expect(dialog.getByText('是否计入返修额度',{exact:true})).toBeVisible();
});
