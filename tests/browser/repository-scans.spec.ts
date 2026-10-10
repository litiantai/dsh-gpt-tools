import {test,expect} from '@playwright/test';
for(const capability of [undefined, 0, '1', true, 1.5]) {
  test(`scan capability ${String(capability)} blocks requests until rechecked`,async({page})=>{
    let supported=false;
    await page.route('**/api/runtime-identity',route=>route.fulfill({json:{capabilities:{repository_scans:supported?1:capability}}}));
    const requests:string[]=[];
    await page.route('**/api/scans',route=>{requests.push(route.request().method());return route.fulfill({json:[]});});
    await page.goto('/autopilot');
    await page.getByRole('button',{name:'接入仓库',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await expect(dialog.getByText('管理服务需要升级')).toBeVisible();
    await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
    await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
    expect(requests).toEqual([]);
    supported=true;
    await dialog.getByRole('button',{name:'重新核对'}).click();
    await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeEnabled();
    await expect.poll(()=>requests.length).toBeGreaterThan(0);
  });
}

test('identity failure blocks scan requests and explains recovery',async({page})=>{
  await page.route('**/api/runtime-identity',route=>route.fulfill({status:503,json:{error:'unavailable'}}));
  let requests=0;
  await page.route('**/api/scans',route=>{requests++;return route.fulfill({json:[]});});
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible();
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(requests).toBe(0);
});

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
test('repository scan blocks submission when the runtime capability mismatches',async({page})=>{
  const legacy={id:'legacy-product',name:'遗留项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:1,kind:'legacy',capabilities:[]}};
  const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
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
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeVisible();
  await expect(dialog.getByText('adapter_spec.kind 必须为 command 且包含 verify 能力')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
});
test('repository scan blocks submission when the adapter protocol version mismatches',async({page})=>{
  const future={id:'future-product',name:'版本错配项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:2,kind:'command',capabilities:['verify']}};
  const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
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
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeVisible();
  await expect(dialog.getByText('协议版本必须为 1')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
});
test('repository scan warns and sends no scan request when the backend predates the capability',async({page})=>{
  // 隔离复现“新版前端连接旧版后端”：旧服务的运行身份没有 capabilities.repository_scans。
  const legacyIdentity={product_id:'legacy-product',commit:'old',release_id:'old'};
  const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
  await page.route('**/api/runtime-identity',route=>route.fulfill(json(legacyIdentity)));
  const scanRequests:string[]=[];
  page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/scans'||path.startsWith('/api/scans/'))scanRequests.push(request.url());});
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('管理服务需要升级')).toBeVisible();
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  await page.waitForTimeout(300);
  expect(scanRequests).toEqual([]);
});
