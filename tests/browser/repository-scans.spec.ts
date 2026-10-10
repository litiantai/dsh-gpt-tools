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
  await dialog.getByRole('button',{name:'扫描新仓库'}).click();
  await expect(page.getByText('该仓库已有扫描正在执行或等待扫描，请查看已有记录，完成后再重试')).toBeVisible();
  await expect(dialog.getByText('共 1 个仓库，1 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  await page.screenshot({path:'.playwright/repository-scans.png',fullPage:true});
});

test('repository rows group history, prefer running scans and keep retry ancestry accessible',async({page})=>{
  const source='/Users/example/project';
  const common={version:1,product_id:'',source,repository_key:source};
  const scans=[
    {...common,id:'latest',status:'queued',created:600,updated:600},
    {...common,id:'running',status:'running',previous_scan_id:'second',created:400,updated:550},
    {...common,id:'third',status:'pass',created:300,updated:500},
    {...common,id:'second',status:'pass',created:200,updated:350},
    {...common,id:'first',source:'/Users/example/alias',status:'pass',created:100,updated:900},
    {...common,id:'remote',source:'https://example.com/project.git',repository_key:'https://example.com/project',status:'pass',created:450,updated:450},
  ];
  let retryRequests=0;
  await page.route('**/api/scans',r=>r.fulfill({json:scans}));
  await page.route('**/api/scans/remote/retry',async r=>{
    expect(r.request().postDataJSON().version).toBe(1);retryRequests++;
    const newer={...scans[5],id:'remote-retry',status:'queued',previous_scan_id:'remote',created:1000,updated:1000};
    scans.unshift(newer);await r.fulfill({json:newer});
  });
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('共 2 个仓库，6 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  const local=dialog.getByRole('row').filter({has:page.getByRole('button',{name:source,exact:true})});
  await expect(local.getByText('扫描中',{exact:true})).toBeVisible();
  await expect(local.getByText('进行中及排队共 2 次')).toBeVisible();
  await expect(local.getByRole('button',{name:'重新扫描',exact:true})).toBeDisabled();
  await local.getByRole('button',{name:'展开行'}).click();
  const history=dialog.getByLabel('扫描历史');
  await expect(history.getByRole('button',{name:'查看详情',exact:true})).toHaveCount(5);
  await history.getByRole('button',{name:'重新扫描 · 查看上次'}).click();
  await expect(dialog.getByText('second',{exact:true})).toBeVisible();
  await history.getByRole('button',{name:'查看详情',exact:true}).last().click();
  await expect(dialog.getByText('first',{exact:true})).toBeVisible();
  await page.screenshot({path:'.playwright/repository-grouped-history.png',fullPage:true});
  const remote=dialog.getByRole('row').filter({has:page.getByRole('button',{name:'https://example.com/project.git',exact:true})});
  await remote.getByRole('button',{name:'重新扫描',exact:true}).click();
  await expect(dialog.getByText('共 2 个仓库，7 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  await expect(remote.getByRole('button',{name:'重新扫描',exact:true})).toBeDisabled();
  expect(retryRequests).toBe(1);
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
