import { test, expect } from '@playwright/test';

test('global update notice shows remaining wait and releases with current identity', async ({page}) => {
  let value:any = {active:true,job_id:'fixture',commit:'abc',phase:'observing',label:'更新观察中',
    remaining_seconds:121,ends_at:Date.now()/1000+121,can_release:true,
    impact:'所有项目的新任务派发、重试和配置修改暂缓；查询和停止操作仍可使用。'};
  let sent:any;
  await page.route('**/api/platform-update', r=>r.fulfill({json:value}));
  await page.route('**/api/platform-update/release', async r=>{
    sent=r.request().postDataJSON(); value={...value,release_requested:true,can_release:false};
    await r.fulfill({json:value});
  });
  await page.goto('/autopilot');
  await expect(page.getByText('更新观察中',{exact:true})).toBeVisible();
  await expect(page.getByText(/剩余约 3 分钟/)).toBeVisible();
  await page.getByRole('button',{name:'手动结束观察'}).click();
  expect(sent).toMatchObject({job_id:'fixture',commit:'abc'});
  expect(sent.operation_id).toBeTruthy();
  await expect(page.getByRole('button',{name:'正在核对健康'})).toBeDisabled();
  value={active:false};
  await expect(page.getByText('更新观察中',{exact:true})).not.toBeVisible({timeout:10000});
});

test('switching and rollback never offer an enabled release control', async ({page}) => {
  await page.route('**/api/platform-update',r=>r.fulfill({json:{active:true,phase:'switching',label:'切换版本',can_release:false,impact:'任务等待派发'}}));
  await page.goto('/autopilot');
  await expect(page.getByText('切换版本',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'手动结束观察'})).toBeDisabled();
  await page.screenshot({path:'.playwright/platform-update.png'});
});
