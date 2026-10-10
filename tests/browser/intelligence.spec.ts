import {test,expect} from '@playwright/test';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';

function seed(action:string,pid='',conversation='') {
  const meta=JSON.parse(readFileSync('.dashboard/e2e.json','utf8'));
  return JSON.parse(execFileSync('python3',['-c',`
import json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()),str(Path.cwd()/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.intake import draft
control=Control(Store(Path(sys.argv[1])))
ledger=control.ledger
action,pid,conversation=sys.argv[2:5]
if action=='project':
 p=ledger.create('products',{'name':'智能协作测试','source':sys.argv[5],'goal':'手机提需求','intelligence':{'enabled':True},'policy':{'deepseek_off_peak_only':False}},'paused')
 print(json.dumps(p))
else:
 with ledger.store.transaction() as db:
  messages=ledger.scoped('chat_messages',pid,db,conversation_id=conversation)
  user=next(r for r in messages if r['role']=='user')
  ledger.update('chat_messages',user['id'],user['version'],{},'completed',db)
  value={'title':'手机附件需求','goal':'手机提交反馈','scope':'需求输入','scenario':'在手机输入需求','impact':'便捷提需求','evidence':'用户上传的需求文档','acceptance':['360px 可提交附件'],'questions':[]}
  requirement=draft(ledger,pid,value,'chat',user['id'],db)
  ledger.create('chat_messages',{'product_id':pid,'conversation_id':conversation,'role':'assistant','content':'已整理需求：手机提交反馈，范围为需求输入。验收：360px 可提交附件。回复“确认”继续。','requirement_ids':[requirement['id']],'requirement_versions':{requirement['id']:requirement['version']}},'completed',db=db)
 print(json.dumps(requirement))
`,meta.state,action,pid,conversation,meta.project],{encoding:'utf8'}));
}

for(const width of [360,390,430,1440]) {
  test(`AI 对话附件到确认，宽度 ${width}`,async({page})=>{
    await page.setViewportSize({width,height:850});
    const project=seed('project');
    await page.goto(`/autopilot?project=${project.id}&view=intelligence&tab=chat`);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.pure-dialogue .ant-card,.pure-dialogue .ant-table,.pure-dialogue .ant-tabs')).toHaveCount(0);
    await page.getByRole('textbox',{name:'对话内容'}).fill('增加手机上传需求文档');
    await page.getByLabel('添加截图或文档',{exact:true}).setInputFiles({name:'需求.md',mimeType:'text/markdown',buffer:Buffer.from('手机界面应能上传附件')});
    await expect(page.getByText('需求.md',{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByText('增加手机上传需求文档',{exact:true})).toBeVisible();
    await expect(page).toHaveURL(/conversation=/);
    const conversation=new URL(page.url()).searchParams.get('conversation')!;
    seed('answer',project.id,conversation);
    await expect(page.getByText('已整理需求：',{exact:false})).toBeVisible();
    await page.getByRole('textbox',{name:'对话内容'}).fill('确认');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByText('已确认，先调查补齐可执行验收证据',{exact:false})).toBeVisible();
    await page.reload();
    await expect(page.getByText('已整理需求：',{exact:false})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
    const composer=await page.locator('.dialogue-composer').boundingBox();
    expect(composer!.y+composer!.height).toBeLessThanOrEqual(850);
    await page.getByRole('button',{name:'项目简介',exact:true}).click();
    const left=page.getByRole('dialog',{name:'项目简介'});
    await expect(left).toBeVisible();
    await expect.poll(async()=>Math.abs((await left.boundingBox())!.x)).toBeLessThanOrEqual(1);
    await expect(left.getByText('手机提需求',{exact:true})).toBeVisible();
    await expect(page.locator('.ant-drawer-left')).toBeVisible();
    await page.screenshot({path:`.playwright/intelligence-left-${width}.png`,fullPage:true,animations:'disabled'});
    await page.keyboard.press('Escape');
    await expect(left).not.toBeVisible();
    await page.getByRole('button',{name:'历史对话',exact:true}).click();
    const right=page.getByRole('dialog',{name:'历史对话'});
    await expect(right).toBeVisible();
    await expect.poll(async()=>{const box=await right.boundingBox();return Math.abs(box!.x+box!.width-width);}).toBeLessThanOrEqual(1);
    await expect(page.locator('.ant-drawer-right')).toBeVisible();
    await page.screenshot({path:`.playwright/intelligence-right-${width}.png`,fullPage:true,animations:'disabled'});
    await right.getByRole('button',{name:'新对话',exact:true}).click();
    await expect(right).not.toBeVisible();
    await expect(page.getByText('想做点什么？')).toBeVisible();
    await page.getByRole('button',{name:'历史对话',exact:true}).click();
    await page.getByRole('dialog',{name:'历史对话'}).getByRole('button',{name:'增加手机上传需求文档',exact:false}).click();
    await expect(page.getByText('已确认，先调查补齐可执行验收证据',{exact:false})).toBeVisible();
    await expect(page.getByRole('dialog',{name:'历史对话'})).not.toBeVisible();
    await page.screenshot({path:`.playwright/intelligence-${width}.png`,fullPage:true,animations:'disabled'});
  });
}

test('在对话中触发研究并从左抽屉进入设置',async({page})=>{
  const project=seed('project');
  await page.goto(`/autopilot?project=${project.id}&view=intelligence`);
  await page.getByRole('textbox',{name:'对话内容'}).fill('发现竞品');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect(page.getByText('已排队发现新竞品，完成后会在这里回复。')).toBeVisible();
  await page.getByRole('button',{name:'项目简介',exact:true}).click();
  await page.getByRole('button',{name:'模型与运行设置',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'模型与运行设置'})).toBeVisible();
  await expect(page.getByText('自动研究竞品',{exact:true})).toHaveCount(0);
  await expect(page.getByText('竞品分析模型',{exact:true})).toHaveCount(0);
  await page.keyboard.press('Escape');
  const navigation=page.locator('.project-primary-tabs');
  await navigation.getByRole('tab',{name:'竞品分析',exact:true}).click();
  await expect(page).toHaveURL(/view=competitors/);
  await expect(page.getByRole('heading',{name:'竞品名单'})).toBeVisible();
  await expect(page.getByRole('heading',{name:'研究记录'})).toBeVisible();
  await expect(page.getByText('发现新竞品',{exact:true})).toHaveCount(2);
  await expect(page.getByRole('textbox',{name:'对话内容'})).toHaveCount(0);
  await navigation.getByRole('tab',{name:'项目设置',exact:true}).click();
  await page.getByRole('tab',{name:'竞品分析配置',exact:true}).click();
  await expect(page.getByText('竞品分析模型',{exact:true})).toBeVisible();
  await page.getByRole('switch',{name:'自动研究竞品'}).click();
  await page.getByRole('button',{name:'保存设置',exact:true}).click();
  await expect(page.getByRole('button',{name:'保存设置',exact:true})).toBeDisabled();
  await page.reload();
  await expect(page.getByRole('switch',{name:'自动研究竞品'})).toBeChecked();
  await navigation.getByRole('tab',{name:'AI 对话',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'对话内容'})).toBeVisible();
  await page.getByRole('button',{name:'项目简介',exact:true}).click();
  await page.getByRole('button',{name:'模型与运行设置',exact:true}).click();
  await page.getByRole('switch',{name:'Agent 异步协作'}).click();
  await page.getByRole('button',{name:'保存设置',exact:true}).click();
  await expect(page.getByRole('button',{name:'保存设置',exact:true})).toBeDisabled();
  const settings=await (await page.request.get(`/api/products/${project.id}/intelligence/settings`)).json();
  expect(settings.config.research_enabled).toBe(true);
  expect(settings.config.collaboration_enabled).toBe(true);
  await page.goto(`/autopilot?project=${project.id}&view=intelligence&tab=competitors`);
  await expect(page.getByRole('heading',{name:'竞品名单'})).toBeVisible();
});
