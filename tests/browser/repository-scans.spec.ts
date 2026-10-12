import {test,expect,type Page} from '@playwright/test';
const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
// 收集所有 /api/scans 请求，用于证明预检未完成或失败时不会触达扫描接口。
function trackScanRequests(page:Page){
  const requests:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/scans')requests.push(request.url());});
  return requests;
}
// 只收集提交型 /api/scans 写请求，列表读取不计入，用于证明预检失败时不会发起写操作。
function trackScanWrites(page:Page){
  const writes:string[]=[];
  page.on('request',request=>{const url=new URL(request.url());if(url.pathname==='/api/scans'&&request.method()==='POST')writes.push(request.url());});
  return writes;
}

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

// 缺陷回归：运行身份“先成功后失败”时查询库保留缓存 data（repository_scans 仍为 1），
// 旧门控只看 isSuccess+data 会继续放行写操作与轮询；修复后 identity.isError 必须撤销缓存能力。
test('identity refresh failure revokes cached scan capability until recovery',async({page})=>{
  let unavailable=false;
  await page.route('**/api/runtime-identity',route=>route.fulfill(unavailable
    ? {status:503,json:{error:'unavailable'}}
    : {json:{capabilities:{repository_scans:1}}}));
  // 分别统计 /api/scans 的读取与提交：证明缓存 data 保留时不放行写操作，且轮询已冻结。
  let reads=0;
  const writes:string[]=[];
  await page.route('**/api/scans',route=>{
    if(route.request().method()==='POST'){
      writes.push(route.request().url());
      return route.fulfill(json({id:'created',source:'/isolated/scan-fixture',status:'queued',version:1,updated:2}));
    }
    reads++;
    return route.fulfill(json([{id:'cached-scan',source:'/isolated/cached',repository_key:'/isolated/cached',status:'pass',version:1,updated:1}]));
  });
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  const submit=dialog.getByRole('button',{name:'扫描新仓库'});
  const retry=dialog.getByRole('button',{name:'重新扫描',exact:true});
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  // 首次成功：能力放行，缓存列表渲染，扫描与重新扫描均可点击。
  await expect(dialog.getByText('共 1 个仓库，1 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  await expect(submit).toBeEnabled();
  await expect(retry).toBeEnabled();
  // 后台刷新失败：identity.isError 撤销缓存能力（查询库仍保留 repository_scans=1 的 data）。
  unavailable=true;
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible({timeout:10000});
  // 失败语义只提示“无法核对管理服务”，不得叠加“管理服务需要升级”。
  await expect(dialog.getByText('管理服务需要升级')).toBeHidden();
  await expect(submit).toBeDisabled();
  await expect(retry).toBeDisabled();
  // 缓存 data 仍在（列表未清空），但门控已关闭。
  await expect(dialog.getByText('共 1 个仓库，1 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  // 等待失败后的下一个身份轮询周期结束，再让可能仍在途的读取结算。
  await page.waitForResponse(response=>response.url().endsWith('/api/runtime-identity') && response.status()===503);
  await page.waitForTimeout(500);
  const stoppedAt=reads;
  // 观察至少一个完整 /scans 轮询周期：缓存能力不得让轮询继续。
  await page.waitForTimeout(3200);
  expect(reads).toBe(stoppedAt);
  // 即使对禁用入口强行点击，act() 守卫也不得发起任何 /api/scans 写请求。
  await submit.click({force:true});
  expect(writes).toEqual([]);
  // 恢复：重新核对成功后，写入口与轮询自动恢复。
  unavailable=false;
  await dialog.getByRole('button',{name:'重新核对'}).click();
  await expect(submit).toBeEnabled();
  await expect.poll(()=>reads).toBeGreaterThan(stoppedAt);
  // 恢复过程中同样不得因为缓存 data 而发起任何写请求。
  expect(writes).toEqual([]);
});

// 缺陷回归（写入口 A）：缓存中仍有 pass 扫描记录时，运行身份“先成功后失败”必须同时关闭
// “登记项目并进入观察模式”写入口，不能因为查询库保留 data（repository_scans 仍为 1）而放行。
test('identity refresh failure blocks onboarding the cached pass scan',async({page})=>{
  let unavailable=false;
  await page.route('**/api/runtime-identity',route=>route.fulfill(unavailable
    ? {status:503,json:{error:'unavailable'}}
    : {json:{capabilities:{repository_scans:1}}}));
  const onboards:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname.endsWith('/onboard'))onboards.push(request.url());});
  await page.route('**/api/scans',route=>route.fulfill(json([
    {id:'cached-pass',source:'/isolated/cached',repository_key:'/isolated/cached',status:'pass',version:1,product_id:'',created:1,updated:1}
  ])));
  await page.route('**/api/scans/cached-pass/environment',route=>route.fulfill(json({stacks:[],modules:[],warnings:[]})));
  await page.route('**/api/scans/cached-pass/onboard',route=>route.fulfill(json({id:'cached-pass',source:'/isolated/cached',status:'pass',version:2,product_id:'new-product',updated:2})));
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  // 选中缓存中的 pass 扫描记录：首次成功时 onboard 写入口可用。
  await dialog.getByRole('button',{name:'/isolated/cached',exact:true}).click();
  const onboard=dialog.getByRole('button',{name:'登记项目并进入观察模式'});
  await expect(onboard).toBeEnabled();
  // 后台刷新失败：缓存列表仍在，但 identity.isError 必须撤销 onboard 写入口。
  unavailable=true;
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible({timeout:10000});
  await expect(dialog.getByText('共 1 个仓库，1 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  await expect(onboard).toBeDisabled();
  // 即使对禁用入口强行点击，也只显示“无法核对管理服务”，不得发起 onboard 写请求。
  await onboard.click({force:true});
  expect(onboards).toEqual([]);
});

// 缺陷回归（写入口 B）：当前项目场景下，运行身份“先成功后失败”必须关闭
// “应用扫描配置到当前项目”写入口，且不得在 act() 之前的项目详情请求上放行。
test('identity refresh failure blocks applying the scan config to the current project',async({page})=>{
  const product={id:'apply-product',name:'应用项目',goal:'fixture',status:'active',version:1};
  let unavailable=false;
  await page.route('**/api/runtime-identity',route=>route.fulfill(unavailable
    ? {status:503,json:{error:'unavailable'}}
    : {json:{capabilities:{repository_scans:1}}}));
  const productReads:string[]=[];
  page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/products/apply-product')productReads.push(request.url());});
  await page.route(url=>url.pathname.startsWith('/api/products'),route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([product]));
    if(path==='/api/products/apply-product')return route.fulfill(json({...product,adapter_spec:{version:1,kind:'command',capabilities:['verify']}}));
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  const applies:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/scans/cached-pass/apply')applies.push(request.url());});
  await page.route('**/api/scans',route=>route.fulfill(json([
    {id:'cached-pass',source:'/isolated/cached',repository_key:'/isolated/cached',status:'pass',version:1,product_id:'',created:1,updated:1}
  ])));
  await page.route('**/api/scans/cached-pass/environment',route=>route.fulfill(json({stacks:[],modules:[],warnings:[]})));
  await page.goto('/autopilot?project=apply-product');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('button',{name:'/isolated/cached',exact:true}).click();
  const apply=dialog.getByRole('button',{name:'应用扫描配置到当前项目'});
  await expect(apply).toBeEnabled();
  // 后台刷新失败：缓存列表仍在，但 identity.isError 必须撤销 apply 写入口。
  unavailable=true;
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible({timeout:10000});
  await expect(dialog.getByText('共 1 个仓库，1 次扫描。展开仓库可查看历史记录。')).toBeVisible();
  await expect(apply).toBeDisabled();
  const stoppedReads=productReads.length;
  // 即使对禁用入口强行点击，也不得发起 apply 写请求，也不得额外读取项目详情。
  await apply.click({force:true});
  expect(applies).toEqual([]);
  expect(productReads.length).toBe(stoppedReads);
});

// 缺陷回归（缓存详情 A）：运行身份“先成功后失败”后缓存列表仍保留记录，扫描详情继续渲染；
// 但详情内的环境查询继承全局 3s 轮询、“检测本机运行时”按钮此前无门控。
// 修复后 identity.isError 必须同时关闭环境轮询与运行时检测，已缓存展示保持只读。
test('identity refresh failure stops cached scan environment polling and runtime checks',async({page})=>{
  let unavailable=false;
  await page.route('**/api/runtime-identity',route=>route.fulfill(unavailable
    ? {status:503,json:{error:'unavailable'}}
    : {json:{capabilities:{repository_scans:1}}}));
  let environmentReads=0;
  await page.route('**/api/scans/cached-pass/environment',route=>{environmentReads++;return route.fulfill(json({stacks:[],modules:[],warnings:[]}));});
  const runtimeChecks:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/scans/cached-pass/runtime-check')runtimeChecks.push(request.url());});
  await page.route('**/api/scans/cached-pass/runtime-check',route=>route.fulfill(json({stacks:[],modules:[],warnings:[],status:'ready'})));
  await page.route('**/api/scans',route=>route.fulfill(json([
    {id:'cached-pass',source:'/isolated/cached',repository_key:'/isolated/cached',status:'pass',version:1,product_id:'',created:1,updated:1}
  ])));
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  // 选中缓存中的 pass 扫描记录：首次成功时环境查询已触达，运行时检测入口可用。
  await dialog.getByRole('button',{name:'/isolated/cached',exact:true}).click();
  const check=dialog.getByRole('button',{name:'检测本机运行时'});
  await expect(check).toBeEnabled();
  await expect.poll(()=>environmentReads).toBeGreaterThan(0);
  // 后台刷新失败：缓存详情仍在，但环境查询与运行时检测必须随身份失败一并停止。
  unavailable=true;
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible({timeout:10000});
  await expect(dialog.getByText('管理服务需要升级')).toBeHidden();
  // 等待失败后的下一个身份轮询周期结束，再让可能仍在途的环境请求结算。
  await page.waitForResponse(response=>response.url().endsWith('/api/runtime-identity') && response.status()===503);
  await page.waitForTimeout(500);
  const stoppedAt=environmentReads;
  // 观察至少一个完整的环境轮询周期：缓存能力不得让环境查询继续。
  await page.waitForTimeout(3200);
  expect(environmentReads).toBe(stoppedAt);
  await expect(check).toBeDisabled();
  // 即使对禁用入口强行点击，也不得发起 runtime-check 检测请求。
  await check.click({force:true});
  await page.waitForTimeout(200);
  expect(runtimeChecks).toEqual([]);
  // 恢复：重新核对成功后环境查询与运行时检测自动放行。
  unavailable=false;
  await dialog.getByRole('button',{name:'重新核对'}).click();
  await expect(check).toBeEnabled();
  await expect.poll(()=>environmentReads).toBeGreaterThan(stoppedAt);
});

// 缺陷回归（缓存详情 B）：应用配置在点击后先 await 项目详情，等待期间身份刷新失败时，
// 已进入的 async 闭包仍持旧的 checking/blocked，详情返回后可能仍提交 apply。
// 修复后必须在 await 之后重新读取实时门控，详情返回也不得发起 /scans/<id>/apply。
test('identity refresh failure while the apply product lookup is in flight blocks the scan write',async({page})=>{
  const product={id:'apply-product',name:'应用项目',goal:'fixture',status:'active',version:1};
  let unavailable=false;
  await page.route('**/api/runtime-identity',route=>route.fulfill(unavailable
    ? {status:503,json:{error:'unavailable'}}
    : {json:{capabilities:{repository_scans:1}}}));
  // 点击“应用”后 apply 处理函数会再次读取项目详情；挂起该请求以制造在途窗口。
  let holdProduct=false;
  let releaseProduct!:()=>void;
  const productGate=new Promise<void>(resolve=>{releaseProduct=resolve;});
  await page.route(url=>url.pathname.startsWith('/api/products'),async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([product]));
    if(path==='/api/products/apply-product'){
      if(holdProduct)await productGate;
      return route.fulfill(json({...product,adapter_spec:{version:1,kind:'command',capabilities:['verify']}}));
    }
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  const applies:string[]=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/scans/cached-pass/apply')applies.push(request.url());});
  await page.route('**/api/scans',route=>route.fulfill(json([
    {id:'cached-pass',source:'/isolated/cached',repository_key:'/isolated/cached',status:'pass',version:1,product_id:'',created:1,updated:1}
  ])));
  await page.route('**/api/scans/cached-pass/environment',route=>route.fulfill(json({stacks:[],modules:[],warnings:[]})));
  await page.route('**/api/scans/cached-pass/apply',route=>route.fulfill(json({id:'cached-pass',status:'pass',version:2,product_id:'apply-product',updated:2})));
  await page.goto('/autopilot?project=apply-product');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('button',{name:'/isolated/cached',exact:true}).click();
  const apply=dialog.getByRole('button',{name:'应用扫描配置到当前项目'});
  // 首次成功：项目能力已确认，apply 入口可用（证明 await 前的门控检查通过）。
  await expect(apply).toBeEnabled();
  // 点击后挂起项目详情，随后让身份下一次轮询失败，最后释放详情请求。
  holdProduct=true;
  await apply.click();
  unavailable=true;
  await expect(dialog.getByText('无法核对管理服务')).toBeVisible({timeout:10000});
  releaseProduct();
  await page.waitForTimeout(500);
  // 即使详情随后返回，也不得用旧闭包状态提交 apply 写请求。
  expect(applies).toEqual([]);
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

test('repository scan does not request /scans before the backend capability is confirmed',async({page})=>{
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=()=>resolve();});
  await page.route('**/api/runtime-identity',async route=>{await gate;return route.fulfill(json({capabilities:{repository_scans:1}}));});
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

test('repository scan blocks submission while the project capability is loading',async({page})=>{
  const product={id:'loading-product',name:'加载中项目',goal:'fixture',status:'paused',version:1};
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=()=>resolve();});
  await page.route('**/api/runtime-identity',route=>route.fulfill(json({capabilities:{repository_scans:1}})));
  await page.route(url=>url.pathname.startsWith('/api/products'),async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([product]));
    if(path==='/api/products/loading-product'){
      await gate;
      return route.fulfill(json({...product,adapter_spec:{version:1,kind:'command',capabilities:['verify']}}));
    }
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  const writes=trackScanWrites(page);
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  // 项目能力查询未完成：入口禁用且不发起提交请求。
  await expect(dialog.getByText('正在确认后端扫描接口与当前项目接入能力')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(writes).toEqual([]);
  release();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeEnabled();
});

test('repository scan blocks submission when the project capability query fails',async({page})=>{
  const product={id:'failed-product',name:'查询失败项目',goal:'fixture',status:'paused',version:1};
  await page.route('**/api/runtime-identity',route=>route.fulfill(json({capabilities:{repository_scans:1}})));
  await page.route(url=>url.pathname.startsWith('/api/products'),route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/products')return route.fulfill(json([product]));
    if(path==='/api/products/failed-product')return route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'内部错误'})});
    if(path.endsWith('/delivery-board'))return route.fulfill(json({prs:[],release_prs:[],issues:[]}));
    if(path.endsWith('/workbench'))return route.fulfill(json({sessions:[],reviews:[],events:[]}));
    return route.fulfill(json({}));
  });
  const writes=trackScanWrites(page);
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByText('当前项目接入能力查询失败')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  expect(writes).toEqual([]);
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
  // 放行唯一来源是运行身份声明；/api/autopilot/capabilities 仅作兼容声明，不作为门控依据。
  await page.route('**/api/runtime-identity',route=>route.fulfill(json({capabilities:{repository_scans:1}})));
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
  await expect(dialog.getByText('管理服务需要升级')).toBeHidden();
  await expect(dialog.getByText('当前项目接入能力不匹配')).toBeVisible();
  await expect(dialog.getByText('adapter_spec.kind 必须为 command 且包含 verify 能力')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
});

test('repository scan blocks submission when the adapter protocol version mismatches',async({page})=>{
  const future={id:'future-product',name:'版本错配项目',goal:'fixture',status:'paused',version:1,
    adapter_spec:{version:2,kind:'command',capabilities:['verify']}};
  // 放行唯一来源是运行身份声明；/api/autopilot/capabilities 仅作兼容声明，不作为门控依据。
  await page.route('**/api/runtime-identity',route=>route.fulfill(json({capabilities:{repository_scans:1}})));
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
  await expect(dialog.getByText('管理服务需要升级')).toBeHidden();
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

test('repository scan blocks submission when runtime identity cannot be read',async({page})=>{
  // 身份查询失败同样属于“能力未确认”：必须提示并停发扫描请求，不能沿用任何“受支持”印象。
  await page.route('**/api/runtime-identity',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'identity unavailable'})}));
  const scanRequests:string[]=[];
  page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/scans'||path.startsWith('/api/scans/'))scanRequests.push(request.url());});
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog.getByText('管理服务需要升级')).toBeVisible();
  await expect(dialog.getByText(/无法读取运行身份/)).toBeVisible();
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  await page.waitForTimeout(300);
  expect(scanRequests).toEqual([]);
});
test('repository scan re-checks capability after the backend is downgraded instead of reusing the cached verdict',async({page})=>{
  // 版本切换回归：先连接声明能力的后端让前端缓存“受支持”，关闭抽屉后换成旧后端再打开。
  // 新会话不得复用缓存结论，必须重新核对；核对完成前不能发送任何 /api/scans 请求。
  const json=(body:unknown)=>({status:200,contentType:'application/json',body:JSON.stringify(body)});
  let identity:Record<string,unknown>={product_id:'legacy-product',commit:'new',release_id:'new',capabilities:{repository_scans:1}};
  await page.route('**/api/runtime-identity',route=>route.fulfill(json(identity)));
  const scanRequests:string[]=[];
  page.on('request',request=>{const path=new URL(request.url()).pathname;if(path==='/api/scans'||path.startsWith('/api/scans/'))scanRequests.push(path);});
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  let dialog=page.getByRole('dialog');
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeEnabled();
  await expect(dialog.getByText('管理服务需要升级')).toHaveCount(0);
  // 证明受支持路径确实会向 /api/scans 发起请求，且该结论已进入前端查询缓存。
  await expect.poll(()=>scanRequests.length).toBeGreaterThan(0);
  // antd Drawer 的关闭按钮沿用 zh-CN 无障碍名称（只有 Modal 硬编码成 Close），
  // 因此这里按抽屉关闭按钮的固定类名点击，避免语言差异让关闭步骤永远等不到元素。
  await dialog.locator('.ant-drawer-close').click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  // 等待超过全局 staleTime，使缓存结论在重新打开时看起来“可复用”。
  await page.waitForTimeout(1700);
  identity={product_id:'legacy-product',commit:'old',release_id:'old'};
  scanRequests.length=0;
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  dialog=page.getByRole('dialog');
  await expect(dialog.getByText('管理服务需要升级')).toBeVisible();
  await dialog.getByLabel('代码仓库地址').fill('/isolated/scan-fixture');
  await expect(dialog.getByRole('button',{name:'扫描新仓库'})).toBeDisabled();
  await page.waitForTimeout(500);
  expect(scanRequests).toEqual([]);
});
