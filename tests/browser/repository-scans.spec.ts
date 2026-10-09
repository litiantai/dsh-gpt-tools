import {test,expect} from '@playwright/test';
test('repository onboarding records scan requests and distinguishes startup from acceptance',async({page})=>{
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('仓库接入与启动扫描',{exact:true})).toBeVisible();
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await dialog.getByRole('button',{name:'扫描新仓库'}).click();
  await expect(dialog.getByRole('button',{name:'/isolated/scan-fixture',exact:true})).toBeVisible();
  await expect(dialog.getByRole('button',{name:'重新扫描'})).toBeDisabled();
  await expect(dialog.getByText('独立于启动扫描，需进入研发验收流程')).toBeVisible();
  await page.screenshot({path:'.playwright/repository-scans.png',fullPage:true});
});
