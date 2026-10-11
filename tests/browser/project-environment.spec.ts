import { test, expect } from '@playwright/test';

test('project runtime check can be repeated after installation without changing settings',async ({page})=>{
  const product={id:'runtime-check',name:'环境检测样例',status:'paused',version:1};
  const environment={stacks:['java','react'],modules:[
    {path:'backend',stacks:['java'],selection:'detected',evidence:['backend/pom.xml']},
    {path:'web',stacks:['react'],selection:'detected',evidence:['web/package.json']},
  ],warnings:[]};
  let checks=0;
  await page.route('**/api/products',r=>r.fulfill({json:[product]}));
  await page.route('**/api/products/runtime-check/delivery-board',r=>r.fulfill({json:{prs:[],issues:[],release_prs:[]}}));
  await page.route('**/api/products/runtime-check/workbench',r=>r.fulfill({json:{sessions:[],reviews:[],events:[]}}));
  await page.route('**/api/products/runtime-check/environment',r=>r.fulfill({json:environment}));
  await page.route('**/api/products/runtime-check/runtime-check',r=>{
    checks++;
    return r.fulfill({json:{...environment,checked_at:1791590400,status:checks===1?'blocked':'ready',checks:[
      {tool:'javac',modules:['backend'],status:checks===1?'missing':'installed',version:checks===1?undefined:'javac 21.0.9',
        path:checks===1?undefined:'/local/jdk/bin/javac',install_hint:'请安装完整 JDK，并设置 JAVA_HOME。'},
    ]}});
  });
  await page.goto('/autopilot?project=runtime-check&view=settings&tab=environment');
  const panel=page.getByRole('region',{name:'项目技术栈与本机运行时'});
  await expect(panel.getByText('backend/pom.xml')).toBeVisible();
  await expect(panel.getByText('React 前端').first()).toBeVisible();
  expect(checks).toBe(0);
  await panel.getByRole('button',{name:'检测本机运行时',exact:true}).click();
  await expect(panel.getByText('本机运行时未就绪',{exact:true})).toBeVisible();
  await expect(panel.getByText('请安装完整 JDK，并设置 JAVA_HOME。')).toBeVisible();
  await panel.getByRole('button',{name:'重新检测本机运行时'}).click();
  await expect(panel.getByText('本机所需工具均可运行')).toBeVisible();
  await expect(panel.getByText('javac 21.0.9')).toBeVisible();
  await expect(page.getByRole('button',{name:'保存项目设置',exact:true})).toBeDisabled();
  expect(checks).toBe(2);
  await expect(page.getByText('记录不存在',{exact:true})).toHaveCount(0);
  await page.screenshot({path:'.playwright/project-runtime-check.png'});
});


test('scan shows detected Java before startup completes and allows runtime recheck',async ({page})=>{
  await page.route('**/api/scans',r=>r.fulfill({json:[{id:'java-scan',source:'/fixture/java',status:'running',version:1,updated:1791590400}]}));
  const environment={stacks:['java'],modules:[{path:'.',stacks:['java'],evidence:['pom.xml']}],warnings:[]};
  await page.route('**/api/scans/java-scan/environment',r=>r.fulfill({json:environment}));
  await page.route('**/api/scans/java-scan/runtime-check',r=>r.fulfill({json:{...environment,status:'blocked',checked_at:1791590400,checks:[{tool:'mvn',modules:['.'],status:'missing',install_hint:'请安装 Maven。'}]}}));
  await page.goto('/autopilot');
  await page.getByRole('button',{name:'接入仓库',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('button',{name:'/fixture/java',exact:true}).click();
  await expect(dialog.getByText('pom.xml',{exact:true})).toBeVisible();
  await dialog.getByRole('button',{name:'检测本机运行时',exact:true}).click();
  await expect(dialog.getByText('请安装 Maven。')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'重新扫描',exact:true})).toBeDisabled();
});
