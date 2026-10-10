import {test,expect,type Page} from '@playwright/test';
const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
const capabilities={scan_api:true,scan_api_version:1,adapter_version:1,features:['repository_scans']};
// 收集所有 /api/scans 请求，用于证明预检未完成或失败时不会触达扫描接口。
function trackScanRequests(page:Page){
  const requests:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/scans')requests.push(request.url());});
  return requests;
}
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
test('repository scan does not request /scans before the backend capability is confirmed',async({page})=>{
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=()=>resolve();});
  await page.route('**/api/autopilot/capabilities',async route=>{await gate;return route.fulfill(json(capabilities));});
  const requests=trackScanRequests(page);
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  // 确认中：入口禁用且 /scans 零请求，避免对尚未确认的接口发起写请求。
  await expect(dialog.getByText('正在确认后端扫描接口与当前项目接入能力')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(requests).toHaveLength(0);
  release();
  await expect(dialog.getByText('正在确认后端扫描接口与当前项目接入能力')).toBeHidden();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeEnabled();
  await expect.poll(()=>requests.length).toBeGreaterThan(0);
});
test('repository scan blocks submission when the backend lacks the scan interface',async({page})=>{
  const compatible={id:'compatible-product',name:'通用项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:1,kind:'command',capabilities:['verify']}};
  // 旧后端：没有 /autopilot/capabilities 路由，返回 404「接口不存在」。
  await page.route('**/api/autopilot/capabilities',route=>route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'接口不存在'})}));
  await page.route(url=>url.pathname.startsWith('/api/products'),route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([compatible]));
    if(path==='/api/products/compatible-product')return route.fulfill(json(compatible));
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  const requests=trackScanRequests(page);
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  // 项目能力匹配，仍因后端缺少扫描接口被拦截，证明门控来自后端能力确认而非适配器错配。
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeHidden();
  await expect(dialog.getByText('当前后端不支持仓库扫描接口')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(requests).toHaveLength(0);
});
test('repository scan blocks submission when the capability query fails',async({page})=>{
  await page.route('**/api/autopilot/capabilities',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'内部错误'})}));
  const requests=trackScanRequests(page);
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByText('当前后端不支持仓库扫描接口')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(requests).toHaveLength(0);
});
test('repository scan blocks submission when the runtime capability mismatches',async({page})=>{
  const legacy={id:'legacy-product',name:'遗留项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:1,kind:'legacy',capabilities:[]}};
  await page.route('**/api/autopilot/capabilities',route=>route.fulfill(json(capabilities)));
  await page.route(url=>url.pathname.startsWith('/api/products'),route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([legacy]));
    if(path==='/api/products/legacy-product')return route.fulfill(json(legacy));
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByText('当前后端不支持仓库扫描接口')).toBeHidden();
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeVisible();
  await expect(dialog.getByText('adapter_spec.kind 必须为 command 且包含 verify 能力')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
});
test('repository scan blocks submission when the adapter protocol version mismatches',async({page})=>{
  const future={id:'future-product',name:'版本错配项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:2,kind:'command',capabilities:['verify']}};
  await page.route('**/api/autopilot/capabilities',route=>route.fulfill(json(capabilities)));
  await page.route(url=>url.pathname.startsWith('/api/products'),route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([future]));
    if(path==='/api/products/future-product')return route.fulfill(json(future));
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByText('当前后端不支持仓库扫描接口')).toBeHidden();
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeVisible();
  await expect(dialog.getByText('协议版本必须为 1')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
});
