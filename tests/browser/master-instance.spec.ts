import {test,expect} from '@playwright/test';

test('master inspection and post-merge acceptance show separate receipts',async ({page})=>{
  const instance={instance_role:'master',commit:'a'.repeat(40),origin:'http://127.0.0.1:9000'};
  const project={id:'master-instance-test',name:'主实例分工',status:'active',version:1,git:{enabled:true},
    last_inspect:1791524749,last_inspect_result:{status:'pass',reason:'master 实例只读接口巡检完成',instance},
    last_master_sync:1791524740,last_master_sync_result:{status:'pass',reason:'master 实例版本已就绪',instance},
    last_final_acceptance:1791524750,last_final_acceptance_result:{status:'blocked',reason:'缺少原需求的只读效果断言',instance}};
  await page.route('**/api/products',route=>route.fulfill({json:[project]}));
  await page.goto('/autopilot?project=master-instance-test&view=overview&tab=monitoring');
  const alert=page.getByRole('alert').filter({hasText:'master 最终验收'});
  await expect(alert).toContainText('已阻塞');
  await expect(page.getByRole('alert').filter({hasText:'master 巡检'})).toContainText('通过');
  await expect(page.getByRole('alert').filter({hasText:'master 实例更新'})).toContainText('通过');
  await alert.getByRole('button',{name:'查看回执'}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('tab',{name:'执行回执'}).click();
  await expect(dialog).toContainText('master 最终验收');
});
