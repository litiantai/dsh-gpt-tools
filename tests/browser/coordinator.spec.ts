import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';

function seed() {
  const meta=JSON.parse(readFileSync('.dashboard/e2e.json','utf8'));
  return JSON.parse(execFileSync('python3',['-B','-c',`
import json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
l=Ledger(Store(Path(sys.argv[1])))
p=l.create('products',{'name':'项目协调回归','source':sys.argv[2],'goal':'验证协调与额度','agents':{'acceptance':{'provider':'codex','model':'test-model'}},'policy':{'deepseek_off_peak_only':False}},'active')
l.create('coordinators',{'product_id':p['id'],'reason':'已追加一轮，等待开发槽','last_check':1791625200},'idle',ident='coordinator-'+p['id'])
l.create('coordination_decisions',{'product_id':p['id'],'reason':'部分拒收检查由失败转通过，继续修复项目隔离','selection':{'provider':'codex','model':'test-model'},'result':{'action':'repair','run_id':'notification-task'}},'applied')
print(json.dumps(p))
`,meta.state,meta.project],{encoding:'utf8'}));
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
  await page.getByRole('switch',{name:'启用项目协调'}).click();
  await page.getByRole('button',{name:'保存协调设置',exact:true}).click();
  await expect(page.getByText('协调配置已保存',{exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByRole('switch',{name:'启用项目协调'})).not.toBeChecked();
  await page.goto(`/autopilot?project=${p.id}&view=tasks&tab=coordinator`);
  await expect(page.getByText('已停用或项目暂停',{exact:true})).toBeVisible();
  await expect(page.getByRole('cell',{name:'追加返修',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'立即评估',exact:true})).toBeDisabled();
  await page.screenshot({path:'.playwright/coordinator.png',fullPage:true});
});
