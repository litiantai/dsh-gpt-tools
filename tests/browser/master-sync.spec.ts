import {test,expect} from '@playwright/test';

test('sync master opens terminal, shows plan and preserves blocked branch reasons',async({page})=>{
  const project={id:'sync-fixture',name:'同步测试项目',goal:'验证 master 合入分支',status:'active',git:{enabled:true},delivery_repository:'/fixture/repo'};
  let count=0;
  let job:any={status:'idle',events:[],branches:[]};
  await page.route('**/api/products',r=>r.fulfill({json:[project]}));
  await page.route('**/api/products/sync-fixture/sync-master',async r=>{
    if(r.request().method()==='POST'){
      expect(r.request().postDataJSON().operation_id).toBeTruthy(); count++;
      job={status:'planning',reason:'正在分析 feat-one 的冲突',master_commit:'abc123',branches:[{branch:'feat-one',status:'running'}],events:[{at:Date.now()/1000,kind:'command',text:'$ git fetch origin'},{at:Date.now()/1000,kind:'plan',text:'冲突处理方案',detail:{summary:'保留 master 修复与 feat 新功能'}}]};
    }
    await r.fulfill({json:job});
  });
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'同步 master',exact:true}).click();
  await expect(page.getByText('最新 master → feat / release')).toBeVisible();
  await expect(page.getByRole('log')).toContainText('$ git fetch origin');
  await expect(page.getByRole('log')).toContainText('保留 master 修复与 feat 新功能');
  await expect(page.getByRole('button',{name:'重新同步',exact:true})).toBeDisabled();
  job={...job,status:'blocked',reason:'部分分支需要处理',branches:[{branch:'feat-one',status:'pass',reason:'已同步 master'},{branch:'release-one',status:'blocked',reason:'工作区有未提交改动，提交后可重新同步'}]};
  await expect(page.getByText('工作区有未提交改动，提交后可重新同步',{exact:false})).toBeVisible({timeout:10000});
  await expect(page.getByRole('button',{name:'重新同步',exact:true})).toBeEnabled();
  expect(count).toBe(1);
  await page.screenshot({path:'.playwright/master-sync-terminal.png'});
});
