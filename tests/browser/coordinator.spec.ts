import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';

function seed(coordinator?:Record<string,unknown>) {
  const meta=JSON.parse(readFileSync('.dashboard/e2e.json','utf8'));
  return JSON.parse(execFileSync('python3',['-B','-c',`
import json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
l=Ledger(Store(Path(sys.argv[1])))
data={'name':'项目协调回归','source':sys.argv[2],'goal':'验证协调与额度','agents':{'acceptance':{'provider':'codex','model':'test-model'}},'policy':{'deepseek_off_peak_only':False}}
if sys.argv[3] != 'null': data['coordinator']=json.loads(sys.argv[3])
p=l.create('products',data,'active')
l.create('coordinators',{'product_id':p['id'],'reason':'已追加一轮，等待开发槽','last_check':1791625200},'idle',ident='coordinator-'+p['id'])
l.create('coordination_decisions',{'product_id':p['id'],'reason':'部分拒收检查由失败转通过，继续修复项目隔离','selection':{'provider':'codex','model':'test-model'},'result':{'action':'repair','run_id':'notification-task'}},'applied')
print(json.dumps(p))
`,meta.state,meta.project,JSON.stringify(coordinator ?? null)],{encoding:'utf8'}));
}

function product(productId:string,changes?:Record<string,unknown>) {
  const meta=JSON.parse(readFileSync('.dashboard/e2e.json','utf8'));
  return JSON.parse(execFileSync('python3',['-B','-c',`
import json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
l=Ledger(Store(Path(sys.argv[1])))
p=l.get('products',sys.argv[2])
if sys.argv[3] != 'null': p=l.update('products',p['id'],p['version'],json.loads(sys.argv[3]))
print(json.dumps(p))
`,meta.state,productId,JSON.stringify(changes ?? null)],{encoding:'utf8'}));
}

test('项目协调：决定、设置、立即评估与刷新',async({page})=>{
  const p=seed();
  await page.goto(`/autopilot?project=${p.id}&view=tasks&tab=coordinator`);
  await expect(page.getByText('每项目一个协调 Agent',{exact:true})).toBeVisible();
  await expect(page.getByRole('cell',{name:'追加返修',exact:true})).toBeVisible();
  await expect(page.getByText('部分拒收检查由失败转通过，继续修复项目隔离',{exact:true})).toBeVisible();
  await expect(page.getByText('今日协调 Token（不扣项目额度）',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'立即评估',exact:true}).click();
  await expect(page.getByText('已安排重新评估',{exact:true})).toBeVisible();
  await page.goto(`/autopilot?project=${p.id}&view=settings&tab=coordination_settings`);
  await expect(page.getByRole('button',{name:'保存协调设置',exact:true})).toBeVisible();
  await page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'}).click();
  await page.getByRole('switch',{name:'启用项目协调'}).click();
  await page.getByRole('button',{name:'保存协调设置',exact:true}).click();
  await expect(page.getByText('协调配置已保存',{exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByRole('switch',{name:'启用项目协调'})).not.toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeChecked();
  await page.goto(`/autopilot?project=${p.id}&view=tasks&tab=coordinator`);
  await expect(page.getByText('已停用或项目暂停',{exact:true})).toBeVisible();
  await expect(page.getByRole('cell',{name:'追加返修',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'立即评估',exact:true})).toBeDisabled();
  await page.screenshot({path:'.playwright/coordinator.png',fullPage:true});
});

test('协调设置：首次配置读取完成前禁止编辑，载入后恢复操作',async({page})=>{
  const p=seed();
  let release!:()=>void;
  let intercepted!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  const initialRequest=new Promise<void>(resolve=>{intercepted=resolve;});
  let first=true;
  await page.route(`**/api/products/${p.id}/coordinator`,async route=>{
    if(first && route.request().method()==='GET') {
      first=false;
      intercepted();
      await gate;
    }
    await route.continue();
  });

  try {
    await page.goto(`/autopilot?project=${p.id}&view=settings&tab=coordination_settings`);
    await initialRequest;
    await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeDisabled();
    await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeDisabled();
    await expect(page.getByRole('button',{name:'沿用验收模型',exact:true})).toBeDisabled();
    await expect(page.getByRole('button',{name:'保存协调设置',exact:true})).toBeDisabled();
  } finally {
    release();
  }

  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeEnabled();
  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeEnabled();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).not.toBeChecked();
  await expect(page.getByRole('button',{name:'沿用验收模型',exact:true})).toBeEnabled();
  await expect(page.getByRole('button',{name:'保存协调设置',exact:true})).toBeEnabled();
});

test('协调设置：默认开启，后台无关项目更新后仍可保存草稿',async({page})=>{
  const p=seed();
  await page.goto(`/autopilot?project=${p.id}&view=settings&tab=coordination_settings`);
  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).not.toBeChecked();
  await page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'}).click();

  const updated=product(p.id,{goal:'后台更新了项目进展'});
  expect(updated.version).toBeGreaterThan(p.version);
  const responsePromise=page.waitForResponse(r=>r.url().endsWith(`/api/products/${p.id}/coordinator/settings`) && r.request().method()==='POST');
  await page.getByRole('button',{name:'保存协调设置',exact:true}).click();
  const response=await responsePromise;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toMatchObject({
    version:p.version,base_config:{enabled:true,human_authority:false,model:null},
  });
  expect(response.request().postDataJSON().config).toEqual({human_authority:true});
  await expect(page.getByText('协调配置已保存',{exact:true})).toBeVisible();
  expect(product(p.id)).toMatchObject({goal:'后台更新了项目进展',coordinator:{enabled:true,human_authority:true,model:null}});

  await page.reload();
  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeChecked();
});

test('协调设置：不同字段的并发修改合并且不覆盖后台配置',async({page})=>{
  const p=seed();
  await page.goto(`/autopilot?project=${p.id}&view=settings&tab=coordination_settings`);
  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).not.toBeChecked();
  await page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'}).click();

  product(p.id,{coordinator:{enabled:false,human_authority:false,model:null}});
  const responsePromise=page.waitForResponse(r=>r.url().endsWith(`/api/products/${p.id}/coordinator/settings`) && r.request().method()==='POST');
  await page.getByRole('button',{name:'保存协调设置',exact:true}).click();
  const response=await responsePromise;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON().config).toEqual({human_authority:true});
  await expect(page.getByText('协调配置已保存',{exact:true})).toBeVisible();
  expect(product(p.id).coordinator).toEqual({enabled:false,human_authority:true,model:null});

  await page.reload();
  await expect(page.getByRole('switch',{name:'启用项目协调'})).not.toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeChecked();
});

test('协调设置：同字段并发修改明确报错并保留本地草稿',async({page})=>{
  const initialModel={provider:'codex',model:'fixture-original-model'};
  const concurrentModel={provider:'codex',model:'fixture-concurrent-model'};
  const p=seed({enabled:true,human_authority:false,model:initialModel});
  await page.goto(`/autopilot?project=${p.id}&view=settings&tab=coordination_settings`);
  const settings=page.getByRole('tabpanel',{name:'项目协调',exact:true});
  await expect(settings.getByText('fixture-original-model · 已保存，目录未确认',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'沿用验收模型',exact:true}).click();
  await page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'}).click();

  const updated=product(p.id,{coordinator:{enabled:true,human_authority:false,model:concurrentModel}});
  const responsePromise=page.waitForResponse(r=>r.url().endsWith(`/api/products/${p.id}/coordinator/settings`) && r.request().method()==='POST');
  await page.getByRole('button',{name:'保存协调设置',exact:true}).click();
  const response=await responsePromise;
  expect(response.status()).toBe(409);
  expect(response.request().postDataJSON().config).toEqual({human_authority:true,model:null});
  expect(response.request().postDataJSON().base_config.model).toEqual(initialModel);
  const saveError=page.locator('.ant-alert').filter({hasText:'协调设置保存失败，当前修改已保留'});
  await expect(saveError).toBeVisible();
  await expect(saveError.locator('.record-text')).toHaveText(/协调配置已被其他操作修改.*当前草稿已保留/);
  await expect(page.getByRole('switch',{name:'启用项目协调'})).toBeChecked();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).toBeChecked();
  await expect(settings.getByText('选择工具提供的模型',{exact:true})).toBeVisible();
  await expect(page.getByText('协调配置已保存',{exact:true})).not.toBeVisible();
  expect(product(p.id)).toMatchObject({version:updated.version,coordinator:{enabled:true,human_authority:false,model:concurrentModel}});

  await page.getByRole('button',{name:'放弃协调修改',exact:true}).click();
  await expect(settings.getByText('fixture-concurrent-model · 已保存，目录未确认',{exact:true})).toBeVisible();
  await expect(page.getByRole('switch',{name:'授权 Watch 代行人工协调决策'})).not.toBeChecked();
  await expect(saveError).not.toBeVisible();
  await expect(page.getByRole('button',{name:'放弃协调修改',exact:true})).toBeDisabled();
  expect(product(p.id)).toMatchObject({version:updated.version,coordinator:{enabled:true,human_authority:false,model:concurrentModel}});
});
