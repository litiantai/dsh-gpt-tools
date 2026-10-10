import {test,expect} from '@playwright/test';
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
