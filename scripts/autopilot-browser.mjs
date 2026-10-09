// Real browser/desktop acceptance against a separately deployed candidate and separate user data.
import { chromium } from '@playwright/test';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { acceptanceFailure, waitForResearchReply } from './autopilot-chat-check.mjs';
const support=process.env.THSOCTOP_APP_SUPPORT;
const home=process.env.THSOCTOP_DSH_HOME;
const runtime=process.env.THSOCTOP_RUNTIME_DIR;
if (!support || !home || !runtime || process.env.THSOCTOP_AUTOPILOT_ISOLATED!=='1') throw new Error('AUTOPILOT_BLOCKED: missing isolation');
const app=process.env.THSOCTOP_TEST_APPLICATION || resolve('apps/desktop/src-tauri/target/release/bundle/macos/thsoctop.app/Contents/MacOS/thsoctop');
if (!existsSync(app)) throw new Error('AUTOPILOT_BLOCKED: candidate desktop is missing');
const evidence=join(support,'browser-evidence',String(Date.now()));mkdirSync(evidence,{recursive:true});
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const child=process.env.THSOCTOP_AUTOPILOT_PRESTARTED==='1'?null:spawn(app,[],{env:process.env,stdio:'ignore'});
let browser, page;
const steps=[];
const capture=async(title,action,expected,actual,status='pass')=>{
 const screenshot=join(evidence,`${steps.length+1}.png`);await page.screenshot({path:screenshot,fullPage:true});
 steps.push({title,action,expected,actual,status,screenshot});
 writeFileSync(join(evidence,'steps.json'),JSON.stringify(steps,null,2));
};
const checks=[];
try {
  let url;
  for(let i=0;i<60;i++) {
    if(child && child.exitCode!==null)throw new Error('Candidate desktop exited');
    try {
      const event=JSON.parse(readFileSync(join(support,'host-ready.private.json'),'utf8'));
      if(event.type==='ready' && event.url)url=event.url;
    }catch{}
    if(url)break;await delay(1000);
  }
  if(!url)throw new Error('AUTOPILOT_BLOCKED: candidate host did not become ready');
  const origin=new URL(url).origin;
  const api=async(path,body)=>{
    const response=await fetch(origin+path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:undefined);
    const result=await response.json();
    if(!response.ok || !result.ok)throw new Error(`API check failed: ${path} (${response.status})`);
    return result.data;
  };
  browser=await chromium.launch({headless:true});
  page=await browser.newPage({viewport:{width:1440,height:1000}});
  const failures=[];page.on('pageerror',e=>failures.push(e.message));
  await page.goto(url,{waitUntil:'domcontentloaded'});
  await page.locator('#root').waitFor({state:'visible',timeout:30000});
  const notice=page.getByRole('button',{name:'Continue',exact:true});
  await notice.waitFor({state:'visible',timeout:5000}).catch(()=>{});
  if(await notice.isVisible())await notice.click();
  const select=async(name)=>{
    await capture('进入'+name+'前','记录操作前界面','保留完整操作过程','当前页面已保存');
    await page.getByRole('button',{name,exact:true}).first().click({timeout:15000});
  };
  await select('我的自选');checks.push({name:'自选面板可进入',status:'pass'});
  const status=await api('/ths-octop/api/status');
  if(!status.signedIn) {
    const body=await page.locator('body').innerText();
    if(!/登录|未登录|暂不可用/.test(body))throw new Error('Missing actionable unavailable-data state');
    checks.push({name:'未登录降级反馈',status:'pass'});
    throw new Error('AUTOPILOT_BLOCKED: isolated test account is not signed in; live data/report acceptance was not run');
  }
  const list=await api('/ths-octop-watchlist/api/list');
  const stocks=list.items || list.stocks || list.watchlist;
  if(!Array.isArray(stocks) || !stocks.length)throw new Error('AUTOPILOT_BLOCKED: isolated test account has no watchlist');
  const stock=stocks.find(s=>s.kind==='stock' && s.market!=='HK') || stocks.find(s=>s.marketId==='33') || stocks[0];
  await page.getByRole('button',{name:new RegExp(stock.code)}).first().waitFor({timeout:60000});
  await capture('账号自选','读取隔离测试账号自选','有真实股票及行情',`读取 ${stocks.length} 只自选`);
  await page.getByRole('button',{name:new RegExp(stock.code)}).first().click();
  await page.locator('.thsoctop-chart canvas').first().waitFor({state:'visible',timeout:60000});
  if(await page.getByText(/行情图没能渲染/).count())throw Error('Chart reported rendering failure');
  await capture('自选进入 K 线','点击自选股票','实际图表容器中绘出 K 线',`${stock.code} canvas 已显示`);
  checks.push({name:'自选进入行情与图表',status:'pass'});
  await page.getByRole('button',{name:'返回上一页',exact:true}).click();
  await select('市场概览');
  await page.getByText('7×24 快讯',{exact:true}).waitFor({timeout:30000});
  await page.locator('.thsoctop-news').first().waitFor({state:'visible',timeout:60000});
  await capture('市场概览与快讯','打开市场概览，记录快讯时间和连板梯队','保留真实上游数据的展示现场',(await page.locator('body').innerText()).slice(-5000));
  await select('灵感广场');
  await page.getByRole('button',{name:'使用模板',exact:true}).first().click();
  const draft=page.getByRole('textbox',{name:'同款草稿内容',exact:true});
  await draft.waitFor({state:'visible'});
  if(!(await draft.inputValue()).trim())throw new Error('Template did not produce a draft');
  await capture('模板生成草稿','点击本地研究模板','生成可编辑草稿','草稿内容非空且可编辑');
  await draft.fill('验收测试：请仅回复“研究会话已就绪”，不要取数或生成报告。');
  await page.getByRole('button',{name:/发送到.*会话|发送到.*对话/}).click();
  await waitForResearchReply(page);
  await capture('研究模板发送','发送草稿到隔离对话','创建会话并送达用户输入',(await page.locator('body').innerText()).slice(-1500));
  checks.push({name:'研究模板生成草稿并发送到隔离会话',status:'pass'});
  const automation=await api('/ths-octop-research/api/automations/create',{
    name:'自主验收研究报告',prompt:'验收范围：只查询一个商品指数或一个期货品种，生成简短 HTML 行情快照。必须真实取数，列出品种、价格、数据时间、来源及风险提示；不扩展其他研究章节。',
    skillIds:['skill-market-overview-report'],schedule:{kind:'manual'},enabled:true});
  const id=automation.id || automation.automation?.id;
  if(!id)throw new Error('Research automation receipt missing');
  await api('/ths-octop-research/api/automations/run',{id});
  let report;
  for(let i=0;i<240;i++) {
    const reports=await api('/ths-octop-research/api/reports');
    report=(reports.versions || reports.reports || []).find(v=>v.automationId===id);
    if(report)break;
    const runs=await api(`/ths-octop-research/api/runs?automationId=${id}`);
    if(runs.runs?.[0]?.status==='failed')throw Error('Research generation failed: '+runs.runs[0].error);
    await delay(2000);
  }
  if(!report)throw new Error('Research report did not complete within the acceptance budget');
  await select('研究报告');
  await page.getByRole('button',{name:'查看最新版',exact:true}).first().click();
  await page.locator('.research-preview iframe').first().waitFor();
  const frame=page.frameLocator('.research-preview iframe').first();
  await frame.locator('body').waitFor();
  const htmlText=await frame.locator('body').innerText();
  if(htmlText.trim().length<80)throw Error('Report preview has no substantive content');
  await capture('真实报告生成及打开','在隔离环境运行报告任务，再打开最新版','报告发布成功且 HTML 预览有真实内容',`版本 ${report.id}；数据截至 ${report.dataAsOf}；${htmlText.slice(0,1000)}`);
  await page.screenshot({path:join(evidence,'report.png')});
  checks.push({name:'研究报告生成和打开',status:'pass'});
  const key=readFileSync(join(home,'thsoctop/autopilot-monitor.key'),'utf8').trim();
  const monitor=await (await fetch(origin+'/ths-octop/api/autopilot/status',{headers:{Authorization:`Bearer ${key}`}})).json();
  if(!monitor.data?.desktop?.known)throw new Error('Desktop bridge activity not observable');
  checks.push({name:'桌宠与会话活动监测',status:'pass'});
  await select('桌宠工作台');
  await capture('桌宠工作台','打开桌宠工作台并核对原生桥状态','原生桌面桥已就绪且入口可访问','desktop.known=true');
  if(failures.length)throw new Error('Browser runtime errors: '+failures.join('; '));
  writeFileSync(join(evidence,'result.json'),JSON.stringify({status:'pass',checks},null,2));
  console.log(JSON.stringify({status:'pass',checks}));
} catch(error) {
  const result = {...acceptanceFailure(error), checks};
  if(page)await capture('验收中断','执行主链路验收','所有必需场景通过',String(error),result.status).catch(()=>{});
  writeFileSync(join(evidence,'result.json'),JSON.stringify(result,null,2));
  if(process.env.THSOCTOP_AUTOPILOT_RESULT)writeFileSync(process.env.THSOCTOP_AUTOPILOT_RESULT,JSON.stringify(result,null,2));
  console.error(String(error));process.exitCode=2;
} finally {
  await browser?.close();child?.kill('SIGTERM');
}
