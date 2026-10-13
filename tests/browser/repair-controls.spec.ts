import {test, expect} from '@playwright/test';

test('a successful receipt mentioning a repaired timeout stays successful',async ({page})=>{
  const reason='上次 Timeout 已修复，当前代码通过';
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'repair-success',name:'成功回执验证',status:'active',version:1,
    last_probe:1791515515,last_probe_result:{status:'pass',reason}}]}));
  await page.goto('/autopilot?project=repair-success&view=overview&tab=monitoring');
  await page.getByRole('button',{name:'查看回执',exact:true}).first().click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText(reason,{exact:true})).toBeVisible();
  await expect(dialog.getByText('等待相关服务响应超时。请检查服务是否可用，稍后重试。',{exact:true})).toHaveCount(0);
});

test('an assertion timeout is presented as a failed test rather than a service outage',async ({page})=>{
  const reason='Error: expect(locator).toBeVisible() failed\nTimeout: 5000ms';
  await page.route('**/api/products',r=>r.fulfill({json:[{id:'repair-assertion',name:'断言失败验证',status:'active',version:1,
    last_probe:1791515515,last_probe_result:{status:'fail',reason}}]}));
  await page.goto('/autopilot?project=repair-assertion&view=overview&tab=monitoring');
  await page.getByRole('button',{name:'查看回执',exact:true}).first().click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('候选版本的测试断言未通过。请查看失败用例及实际结果，修复后重新验证。',{exact:true})).toBeVisible();
  await expect(dialog.getByText('等待相关服务响应超时。请检查服务是否可用，稍后重试。',{exact:true})).toHaveCount(0);
});
