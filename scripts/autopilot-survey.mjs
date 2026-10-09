// Read-only UI survey of the explicitly isolated desktop Host, preserving every step.
import {chromium} from '@playwright/test';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
const root=process.argv[2];
if(!root)throw new Error('Missing isolated environment');
const ready=JSON.parse(await readFile(join(root,'support/host-ready.private.json'),'utf8'));
const output=join(root,'survey',String(Date.now()));await mkdir(output,{recursive:true});
const steps=[];const errors=[];
const record=(action,body)=>JSON.parse(execFileSync('python3',[join(dirname(fileURLToPath(import.meta.url)),'autopilot-evidence.py')],{input:JSON.stringify({state:process.env.DSH_AUTOPILOT_STATE || dirname(dirname(root)),action,...body}),encoding:'utf8'}));
const inspectionId=process.argv[3]?record('start',{product_id:process.argv[3],title:'登录后主链路实时巡查',method:'Playwright 逐场景操作，操作前后截图；等待数据加载后再次取证，每步即时写入监控台账'}).id:undefined;
const clean=s=>String(s).replace(/([?&]token=)[^&\s]+/g,'$1[redacted]');
const browser=await chromium.launch();const page=await browser.newPage({viewport:{width:1500,height:950}});
page.on('pageerror',e=>errors.push(clean(e.message)));
const capture=async(title,action,expected,status='observed')=>{
  const stem=String(steps.length+1).padStart(2,'0');
  const screenshot=join(output,stem+'.png');await page.screenshot({path:screenshot,fullPage:true});
  const body=clean(await page.locator('body').innerText());await writeFile(join(output,stem+'.txt'),body);
  steps.push({title,action,expected,actual:body.slice(-6000),status,screenshot,at:Date.now()/1000,errors:[...errors]});
  if(inspectionId)record('step',{inspection_id:inspectionId,step:{title,action,expected,actual:body.slice(-6000),status,screenshot,details:{browser_errors:[...errors]}}});
  await writeFile(join(output,'survey.json'),JSON.stringify({steps,complete:false},null,2));
};
try {
  await page.goto(ready.url);await page.locator('#root').waitFor();
  const notice=page.getByRole('button',{name:'Continue',exact:true});
  await notice.waitFor({state:'visible',timeout:5000}).catch(()=>{});
  if(await notice.isVisible()){await capture('首次进入工作台','打开隔离 Host','显示产品工作台');await notice.click();}
  await capture('财经助手首页','查看登录后的独立测试工作台','入口和输入区可见');
  for(const name of ['我的自选','证券行情','市场概览','灵感广场','研究报告','桌宠工作台']){
    await capture('进入'+name+'之前','记录当前页面','保留操作前状态');
    await page.getByText(name,{exact:true}).first().click();
    await page.waitForTimeout(1800);
    await capture(name+'页面','点击导航：'+name,'内容可见，错误和空状态可解释');
    if(['我的自选','市场概览'].includes(name)){
      const settled=await page.waitForFunction(()=>!(/正在读取账号…|正在加载…|刷新中…/.test(document.body.innerText)),{},{timeout:45000}).then(()=>true,()=>false);
      await capture(name+'加载后状态','等待请求完成，最多 45 秒', '数据显示，或给出可操作的错误反馈',settled?'observed':'blocked');
    }
  }
  await writeFile(join(output,'survey.json'),JSON.stringify({steps,complete:true,errors},null,2));
  if(inspectionId)record('finish',{inspection_id:inspectionId,status:'observed',judgement:'界面场景巡查完成，截图、可见结果和浏览器错误均已保存；待独立归因，不以入口可达代替功能验收。'});
  console.log(JSON.stringify({output,steps:steps.length,errors:errors.length}));
} catch(e) {
  await capture('巡查中断','执行界面巡查','全部场景可访问','fail').catch(()=>{});
  await writeFile(join(output,'survey.json'),JSON.stringify({steps,complete:false,error:clean(e)},null,2));
  if(inspectionId)record('finish',{inspection_id:inspectionId,status:'blocked',judgement:clean(e)});
  console.log(JSON.stringify({output,steps:steps.length,error:clean(e)}));process.exitCode=1;
} finally {await browser.close();}
