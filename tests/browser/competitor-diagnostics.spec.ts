import {test, expect, type Page} from '@playwright/test';

const productId = 'competitor-diagnostics';
const secrets = [
  'inline-key-canary', 'object-key-canary', 'nested-auth-canary',
  'nested-password-canary', 'legacy-token-canary', 'fallback-cookie-canary', 'failed-token-canary', 'relevance-key-canary',
];
const competitorReason = '与本项目同属 AI coding agent 工作流，重点比较代码审查及研究能力。api_key=relevance-key-canary';
const safeCompetitorReason = '与本项目同属 AI coding agent 工作流，重点比较代码审查及研究能力。api_key=[已隐藏]';
const translatedError = '本次操作未能完成，具体原因暂未识别。请展开技术详情查看诊断信息。';
const records = [
  {title: '结构化诊断竞品', markers: ['structured-original-marker', 'nested-message-marker', 'nested-array-marker']},
  {title: '旧字符串诊断研究', markers: ['legacy-diagnostic-marker']},
  {title: '备用详情研究', markers: ['detail-fallback-marker']},
  {title: '结构化失败研究', markers: ['failed-research-marker']},
];

async function mockResearch(page: Page) {
  await page.route('**/api/products', route => route.fulfill({json: [
    {id: productId, name: '竞品诊断回归', status: 'paused', version: 1},
  ]}));
  await page.route(`**/api/products/${productId}/intelligence/competitors**`, route => route.fulfill({json: {
    items: [{
      id: 'structured-competitor', name: records[0].title, status: 'active', updated: 1791515515,
      reason: translatedError,
      diagnostic: {original: {reason: competitorReason}, detail: null},
      result: {
        summary: '已保留分析结果',
        diagnostic: {
          original: 'structured-original-marker api_key=inline-key-canary',
          api_key: 'object-key-canary',
          detail: [
            {message: 'nested-message-marker', authorization: 'Bearer nested-auth-canary'},
            ['nested-array-marker', {password: 'nested-password-canary'}],
          ],
        },
      },
    }],
    next_cursor: null,
  }}));
  await page.route(`**/api/products/${productId}/intelligence/research_jobs**`, route => route.fulfill({json: {
    items: [
      {
        id: 'legacy-research', title: records[1].title, status: 'failed', updated: 1791515515,
        reason: '研究失败，请查看技术详情',
        result: {diagnostic: 'legacy-diagnostic-marker token=legacy-token-canary'},
      },
      {
        id: 'fallback-research', title: records[2].title, status: 'failed', updated: 1791515515,
        reason: '研究失败，请查看技术详情',
        result: {diagnostic: '', detail: {message: 'detail-fallback-marker', context: [{cookie: 'fallback-cookie-canary'}]}},
      },
      {
        id: 'structured-failed-research', title: records[3].title, status: 'failed', updated: 1791515515,
        reason: '研究失败，请查看技术详情',
        diagnostic: {original: {reason: '研究执行失败，不应替换中文错误说明'}, detail: null},
        result: {status: 'fail', diagnostic: {original: {reason: 'failed-research-marker', token: 'failed-token-canary'}, detail: null}},
      },
    ],
    next_cursor: null,
  }}));
}

async function assertDiagnostics(page: Page) {
  await expect(page.getByRole('heading', {name: '竞品名单', exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: '研究记录', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '开始分析', exact: true})).toBeVisible();
  const competitor = page.locator('.competitor-analysis article').filter({hasText: records[0].title});
  await expect(competitor.locator('p').first()).toHaveText(safeCompetitorReason);
  await expect(competitor).not.toContainText(translatedError);
  const failedResearch = page.locator('.competitor-analysis article').filter({hasText: records[3].title});
  await expect(failedResearch.locator('p').first()).toHaveText('研究失败，请查看技术详情');
  await expect(failedResearch).not.toContainText('研究执行失败，不应替换中文错误说明');

  for (const record of records) {
    const entry = page.locator('.competitor-analysis article').filter({hasText: record.title});
    await expect(entry).toBeVisible();
    const details = entry.locator('details.record-raw');
    await expect(details.locator('summary')).toHaveText('技术详情');
    await expect(details).not.toHaveAttribute('open', '');
    await expect(details.locator('pre')).toBeHidden();
  }
  const collapsedText = await page.locator('body').innerText();
  for (const marker of records.flatMap(record => record.markers)) expect(collapsedText).not.toContain(marker);

  for (const record of records) {
    const entry = page.locator('.competitor-analysis article').filter({hasText: record.title});
    await entry.getByText('技术详情', {exact: true}).click();
    const diagnostic = entry.locator('details.record-raw pre');
    await expect(diagnostic).toBeVisible();
    for (const marker of record.markers) await expect(diagnostic).toContainText(marker);
    await expect(diagnostic).toContainText('[已隐藏]');
    await expect(diagnostic).not.toContainText('[object Object]');
  }

  const structured = JSON.parse(await page.locator('.competitor-analysis article')
    .filter({hasText: records[0].title}).locator('pre').innerText());
  expect(structured.original).toBe('structured-original-marker api_key=[已隐藏]');
  expect(structured.api_key).toBe('[已隐藏]');
  expect(structured.detail[0].authorization).toBe('[已隐藏]');
  expect(structured.detail[1]).toEqual(['nested-array-marker', {password: '[已隐藏]'}]);
  const visibleText = await page.locator('body').innerText();
  for (const secret of secrets) expect(visibleText).not.toContain(secret);
}

for (const entry of ['click', 'direct'] as const) {
  test(`竞品诊断支持结构化数据、脱敏和刷新：${entry === 'click' ? '点击入口' : '直达链接'}`, async ({page}) => {
    const pageErrors: string[] = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await mockResearch(page);

    if (entry === 'click') {
      await page.goto(`/autopilot?project=${productId}&view=overview&tab=monitoring`);
      await expect(page.getByRole('heading', {name: '竞品诊断回归', exact: true})).toBeVisible();
      await page.locator('.project-primary-tabs').getByRole('tab', {name: '竞品分析', exact: true}).click();
    } else {
      await page.goto(`/autopilot?project=${productId}&view=competitors&overviewTab=monitoring&tab=competitors&tasksTab=runs&intelligenceTab=chat&requirementsTab=requirements&settingsTab=coordination_settings&competitorsTab=competitors`);
    }

    await expect(page).toHaveURL(/view=competitors/);
    await assertDiagnostics(page);
    expect(pageErrors).toEqual([]);
    await page.reload();
    await expect(page).toHaveURL(/view=competitors/);
    await assertDiagnostics(page);
    expect(pageErrors).toEqual([]);
  });
}
