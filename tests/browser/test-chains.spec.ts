import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

function seed() {
  const meta=JSON.parse(readFileSync('.dashboard/e2e.json','utf8'));
  return JSON.parse(execFileSync('python3',['-c',`
import json,sys,base64
from pathlib import Path
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.test_chains import enqueue_generation,revise,make_run
from autopilot.evidence import Recorder
control=Control(Store(Path(sys.argv[1])));ledger=control.ledger
p=ledger.create('products',{'name':'用户操作验收','source':sys.argv[2],'goal':'报告生成','computer_use':{'enabled':True},'policy':{'deepseek_off_peak_only':False}},'paused')
r=ledger.create('requirements',{'product_id':p['id'],'title':'报告需求','acceptance':['报告完成']},'queued')
c=enqueue_generation(ledger,p,r)
c=revise(ledger,c,{'title':'报告生成链路','steps':[{'id':'report','goal':'点击生成报告','expected':'显示完成报告','loop':True,'observation_action':'none','acceptance_indices':[0]}]},r['id'])
t=ledger.create('runs',{'product_id':p['id'],'title':'分支报告任务','workspace':sys.argv[2],'branch':'feat-report','commit':'abc123','requirement_id':r['id']},'accepted')
x=make_run(ledger,p,[c],t)
rec=Recorder(ledger.store,p['id'],'报告生成','界面操作','branch')
image=ledger.store.state/'fixture.png';image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
e=rec.step('点击生成报告','点击','显示完成报告','已显示完成报告',screenshot=image,details={'chain_id':c['id'],'step_id':'report'})
x=ledger.update('test_chain_runs',x['id'],x['version'],{'instance':{'commit':'abc123'},'evidence_ids':[e['id']]},'pass')
print(json.dumps({'product':p,'chain':c,'run':x}))
`,meta.state,meta.project],{encoding:'utf8'}));
}

test('链路基准、追加预期与运行快照持久化', async({page})=>{
  const {product,run}=seed();
  await page.goto(`/autopilot?project=${product.id}&view=testing&tab=test_chains`);
  await expect(page.locator('.project-primary-tabs').getByRole('tab',{name:'测试链路',exact:true})).toBeVisible();
  await expect(page.getByText('报告生成链路',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'运行记录',exact:true}).click();
  await page.getByRole('button',{name:'分支报告任务',exact:true}).click();
  await expect(page.getByText('实际：已显示完成报告',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'确认正确并保存基准'}).click();
  await expect(page.getByRole('button',{name:'已保存正确基准'})).toBeDisabled();
  await page.getByRole('dialog',{name:'运行证据与预期对照'}).getByRole('button',{name:'关闭',exact:true}).click();
  await page.getByRole('tab',{name:'链路库',exact:true}).click();
  await page.getByRole('button',{name:'编辑 / 追加预期'}).click();
  await page.getByText('只截图等待',{exact:true}).first().click();
  await page.getByText('打开指定进度页后截图',{exact:true}).click();
  await page.getByRole('textbox',{name:'进度页面路径'}).first().fill('/reports/progress');
  await page.getByRole('button',{name:'追加步骤 / 预期'}).click();
  await page.getByRole('textbox',{name:'用户操作 / 目标'}).last().fill('查看下载');
  await page.getByRole('textbox',{name:'预期正确结果'}).last().fill('可下载报告');
  await page.getByRole('button',{name:'确定',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'编辑链路与追加预期'})).not.toBeVisible();
  await expect(page.getByText('v2',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'查看正确基准'})).toBeVisible();
  const original=await (await page.request.get(`/api/products/${product.id}/test-chains/runs/${run.id}`)).json();
  expect(original.snapshots[0].definition.steps).toHaveLength(1);
  expect(original.snapshots[0].definition.steps[0].observation_action).toBe('none');
  const saved=await (await page.request.get(`/api/products/${product.id}/test-chains`)).json();
  expect(saved.versions.find((v:{number:number})=>v.number===2).definition.steps[0].observation_url).toBe('/reports/progress');
  await page.getByRole('button',{name:'运行',exact:true}).click();
  await page.getByLabel('分支任务与提交').click();
  await page.locator('.ant-select-item-option-content').filter({hasText:'分支报告任务'}).click();
  await page.getByRole('button',{name:'确定',exact:true}).click();
  await expect(page.getByRole('button',{name:'停止运行'})).toBeVisible();
  await page.getByRole('button',{name:'停止运行'}).click();
  await expect(page.getByRole('dialog',{name:'运行证据与预期对照'}).getByText('已停止',{exact:true})).toBeVisible();
  await page.getByRole('dialog',{name:'运行证据与预期对照'}).getByRole('button',{name:'关闭',exact:true}).click();
  await page.reload();
  await expect(page.getByText('v2',{exact:false})).toBeVisible();
  await page.screenshot({path:'.playwright/test-chains-library.png',fullPage:true});
});

test('观测配置保存后刷新仍保留',async({page})=>{
  const {product}=seed();
  await page.goto(`/autopilot?project=${product.id}&view=testing&tab=test_chain_settings`);
  await expect(page.getByRole('spinbutton',{name:'观测间隔（秒）'})).toHaveValue('30');
  await page.getByRole('spinbutton',{name:'观测间隔（秒）'}).fill('45');
  await page.getByRole('button',{name:'保存测试配置'}).click();
  await expect(page.getByText('测试配置已保存')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('spinbutton',{name:'观测间隔（秒）'})).toHaveValue('45');
});
