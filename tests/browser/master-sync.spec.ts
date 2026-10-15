import { readFile } from 'node:fs/promises';
import { test, expect, type Page } from '@playwright/test';

type Job = {
  status: string;
  reason?: string;
  master_commit?: string;
  branches: { branch: string; status: string; reason?: string; workspace?: string }[];
  events: { at: number; kind: string; text: string; detail?: unknown }[];
};

const project = {
  id: 'sync-fixture', name: '同步测试项目', goal: '验证 master 合入分支',
  status: 'active', git: { enabled: true }, delivery_repository: '/fixture/repo',
};
const syncPath = '**/api/products/sync-fixture/sync-master';
const eventTime = 1791950400;
const runningJob = (): Job => ({
  status: 'planning', reason: '正在分析 feat-one 的冲突', master_commit: 'abc123456789def',
  branches: [{ branch: 'feat-one', status: 'running', reason: '分析冲突方案' }],
  events: [
    { at: eventTime, kind: 'command', text: '$ git fetch origin' },
    { at: eventTime + 1, kind: 'plan', text: '冲突处理方案', detail: { summary: '保留 master 修复与 feat 新功能' } },
  ],
});

async function openTerminal(page: Page) {
  await page.goto('/autopilot?project=sync-fixture');
  await page.getByRole('button', { name: '同步 master', exact: true }).click();
  const terminal = page.getByRole('dialog');
  await expect(terminal).toBeVisible();
  await expect.poll(async () => {
    const bounds = await terminal.boundingBox();
    return bounds ? bounds.x + bounds.width : Number.POSITIVE_INFINITY;
  }).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  return terminal;
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/products', route => route.fulfill({ json: [project] }));
});

test('opening an idle terminal waits for explicit start and reopening does not submit again', async ({ page }) => {
  let posts = 0;
  let reads = 0;
  let job: Job = { status: 'idle', branches: [], events: [] };
  await page.route(syncPath, async route => {
    if (route.request().method() === 'POST') {
      expect(route.request().postDataJSON().operation_id).toBeTruthy();
      posts++;
      job = runningJob();
    } else {
      reads++;
    }
    await route.fulfill({ json: job });
  });

  const terminal = await openTerminal(page);
  await expect(terminal.getByRole('button', { name: '开始同步', exact: true })).toBeEnabled();
  expect(reads).toBeGreaterThan(0);
  expect(posts).toBe(0);
  await terminal.getByRole('button', { name: '开始同步', exact: true }).click();
  await expect(terminal.getByRole('log', { name: '分支同步执行日志' })).toContainText('$ git fetch origin');
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeDisabled();
  expect(posts).toBe(1);

  await terminal.getByRole('button', { name: /Close|关闭/ }).click();
  await expect(terminal).not.toBeVisible();
  await page.getByRole('button', { name: '同步 master', exact: true }).click();
  await expect(terminal.getByRole('log', { name: '分支同步执行日志' })).toContainText('保留 master 修复与 feat 新功能');
  expect(posts).toBe(1);
});

test('opening an existing running job only reads its progress and prevents duplicate submission', async ({ page }) => {
  let posts = 0;
  await page.route(syncPath, route => {
    if (route.request().method() === 'POST') posts++;
    return route.fulfill({ json: { ...runningJob(), status: 'running', reason: '正在同步 feat-one' } });
  });

  const terminal = await openTerminal(page);
  await expect(terminal).toContainText('正在同步 feat-one');
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeDisabled();
  await terminal.getByRole('button', { name: '刷新终端', exact: true }).click();
  await expect(terminal.getByRole('log', { name: '分支同步执行日志' })).toContainText('$ git fetch origin');
  expect(posts).toBe(0);
});

test('blocked results preserve each branch reason, conflict plan and workspace before retry', async ({ page }) => {
  let posts = 0;
  let job: Job = {
    ...runningJob(), status: 'blocked', reason: '部分分支需要处理',
    branches: [
      { branch: 'feat-one', status: 'pass', reason: '已同步 master' },
      { branch: 'release-one', status: 'blocked', reason: '工作区有未提交改动，提交后可重新同步' },
      { branch: 'feat-conflict', status: 'blocked', reason: '冲突修复未通过，请检查保留工作区', workspace: '/fixture/conflict-worktree' },
    ],
    events: [
      ...runningJob().events,
      { at: eventTime + 2, kind: 'error', text: 'feat-conflict 冲突修复未通过', detail: { files: ['src/report.ts'], plan: '保留 master 修复与 feat 新功能' } },
    ],
  };
  await page.route(syncPath, route => {
    if (route.request().method() === 'POST') {
      posts++;
      job = { ...runningJob(), status: 'queued' };
    }
    return route.fulfill({ json: job });
  });

  const terminal = await openTerminal(page);
  await expect(terminal).toContainText('完成');
  await expect(terminal).toContainText('需处理');
  await expect(terminal).toContainText('feat-one');
  await expect(terminal).toContainText('release-one');
  await expect(terminal).toContainText('工作区有未提交改动，提交后可重新同步');
  await expect(terminal).toContainText('/fixture/conflict-worktree');
  const log = terminal.getByRole('log', { name: '分支同步执行日志' });
  await expect(log).toContainText('保留 master 修复与 feat 新功能');
  await expect(log).toContainText('src/report.ts');
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeEnabled();
  expect(posts).toBe(0);
  await terminal.screenshot({ path: '.playwright/master-sync-terminal.png' });
  await terminal.getByRole('button', { name: '重新同步', exact: true }).click();
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeDisabled();
  expect(posts).toBe(1);
});

test('log search filters commands and plan detail while copy and download preserve all loaded events', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.route(syncPath, route => route.fulfill({ json: { ...runningJob(), status: 'completed', reason: '全部分支已同步 master' } }));
  const terminal = await openTerminal(page);
  const log = terminal.getByRole('log', { name: '分支同步执行日志' });
  const search = terminal.getByPlaceholder('搜索命令、日志或方案');
  await expect(log).toContainText('$ git fetch origin');
  await expect(log).toContainText('保留 master 修复与 feat 新功能');

  const following = terminal.getByRole('switch', { name: '自动跟随', exact: true }).or(terminal.getByRole('checkbox', { name: '自动跟随', exact: true }));
  await expect(following).toBeChecked();
  await following.click();
  await expect(following).not.toBeChecked();
  await following.click();
  await expect(following).toBeChecked();

  await search.fill('feat 新功能');
  await expect(log).toContainText('保留 master 修复与 feat 新功能');
  await expect(log).not.toContainText('$ git fetch origin');
  await search.fill('git fetch');
  await expect(log).toContainText('$ git fetch origin');
  await expect(log).not.toContainText('冲突处理方案');
  await search.fill('不存在的日志关键词');
  await expect(log).not.toContainText('$ git fetch origin');
  await expect(log).not.toContainText('保留 master 修复与 feat 新功能');

  await terminal.getByRole('button', { name: '复制日志', exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('$ git fetch origin');
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain('保留 master 修复与 feat 新功能');
  const downloadPromise = page.waitForEvent('download');
  await terminal.getByRole('button', { name: '下载日志', exact: true }).click();
  const download = await downloadPromise;
  const downloadedPath = await download.path();
  expect(downloadedPath).toBeTruthy();
  const downloaded = await readFile(downloadedPath!, 'utf8');
  expect(downloaded).toContain('$ git fetch origin');
  expect(downloaded).toContain('保留 master 修复与 feat 新功能');
  await search.fill('');
  await expect(log).toContainText('$ git fetch origin');
  await expect(log).toContainText('冲突处理方案');
  await terminal.getByRole('combobox', { name: '日志类型' }).press('ArrowDown');
  await page.getByTitle('方案', { exact: true }).click();
  await expect(log).toContainText('保留 master 修复与 feat 新功能');
  await expect(log).not.toContainText('$ git fetch origin');
  await terminal.getByRole('combobox', { name: '日志类型' }).press('ArrowDown');
  await page.getByTitle('全部日志', { exact: true }).last().click();
  await expect(log).toContainText('$ git fetch origin');
});

test('scrolling up pauses automatic following and jumping to latest restores it across reopening', async ({ page }) => {
  let job: Job = {
    ...runningJob(), status: 'running',
    events: Array.from({ length: 40 }, (_, index) => ({
      at: eventTime + index, kind: 'output', text: `第 ${index + 1} 条同步输出\n正在检查分支文件和 master 差异`,
    })),
  };
  await page.route(syncPath, route => route.fulfill({ json: job }));
  const terminal = await openTerminal(page);
  const log = terminal.getByRole('log', { name: '分支同步执行日志' });
  const following = terminal.getByRole('switch', { name: '自动跟随', exact: true });
  const distanceFromLatest = () => log.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
  await expect(log).toContainText('第 40 条同步输出');
  await expect.poll(distanceFromLatest).toBeLessThanOrEqual(2);
  await log.hover();
  await page.mouse.wheel(0, -600);
  await expect(following).not.toBeChecked();
  await expect.poll(distanceFromLatest).toBeGreaterThan(80);
  job = { ...job, events: [...job.events, { at: eventTime + 41, kind: 'output', text: '新的同步输出已到达' }] };
  await terminal.getByRole('button', { name: '刷新终端', exact: true }).click();
  await expect(log).toContainText('新的同步输出已到达');
  await expect(following).not.toBeChecked();
  await expect.poll(distanceFromLatest).toBeGreaterThan(80);
  await terminal.getByRole('button', { name: '跳到最新', exact: true }).click();
  await expect(following).toBeChecked();
  await expect.poll(distanceFromLatest).toBeLessThanOrEqual(2);
  await terminal.getByRole('button', { name: /Close|关闭/ }).click();
  await expect(terminal).not.toBeVisible();
  await page.getByRole('button', { name: '同步 master', exact: true }).click();
  await expect(terminal).toBeVisible();
  await expect(following).toBeChecked();
  await expect.poll(distanceFromLatest).toBeLessThanOrEqual(2);
});

test('a failed progress read keeps the latest running job and prevents restart until recovery', async ({ page }) => {
  let failReads = false;
  let posts = 0;
  let job: Job = { ...runningJob(), status: 'running', reason: '正在同步 feat-one' };
  await page.route(syncPath, route => {
    if (route.request().method() === 'POST') posts++;
    if (failReads && route.request().method() === 'GET') {
      return route.fulfill({ status: 503, json: { error: '同步进度读取暂不可用' } });
    }
    return route.fulfill({ json: job });
  });

  const terminal = await openTerminal(page);
  const log = terminal.getByRole('log', { name: '分支同步执行日志' });
  await expect(log).toContainText('$ git fetch origin');
  failReads = true;
  await terminal.getByRole('button', { name: '刷新终端', exact: true }).click();
  await expect(terminal).toContainText('同步进度读取暂不可用');
  await expect(terminal).toContainText('正在同步 feat-one');
  await expect(terminal).toContainText('abc123456789');
  await expect(log).toContainText('保留 master 修复与 feat 新功能');
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeDisabled();
  expect(posts).toBe(0);

  failReads = false;
  job = { ...job, status: 'completed', reason: '全部分支已同步 master', branches: [{ branch: 'feat-one', status: 'pass', reason: '已同步 master' }] };
  await terminal.getByRole('button', { name: '刷新终端', exact: true }).click();
  await expect(terminal).not.toContainText('同步进度读取暂不可用');
  await expect(terminal).toContainText('全部分支已同步 master');
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeEnabled();
  expect(posts).toBe(0);
});

test('an initial read failure shows unknown state and requires recovery before starting a job', async ({ page }) => {
  let failReads = true;
  let posts = 0;
  await page.route(syncPath, route => {
    if (route.request().method() === 'POST') posts++;
    return route.fulfill(failReads
      ? { status: 503, json: { error: '暂时无法读取同步记录' } }
      : { json: { status: 'idle', branches: [], events: [] } });
  });
  const terminal = await openTerminal(page);
  await expect(terminal.getByText('状态未知', { exact: true })).toBeVisible();
  await expect(terminal).toContainText('暂时无法读取同步记录');
  await expect(terminal.getByRole('button', { name: '开始同步', exact: true })).toBeDisabled();
  expect(posts).toBe(0);
  failReads = false;
  await terminal.getByRole('button', { name: '刷新终端', exact: true }).click();
  await expect(terminal.getByText('尚未同步', { exact: true })).toBeVisible();
  await expect(terminal.getByRole('button', { name: '开始同步', exact: true })).toBeEnabled();
  expect(posts).toBe(0);
});

test('mobile terminal contains long branch names and conflict paths without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const branch = 'feat-report-long-branch-name-with-release-master-conflict';
  const workspace = '/fixture/worktrees/very-long-project-name/conflict-master-report-release';
  await page.route(syncPath, route => route.fulfill({ json: {
    ...runningJob(), status: 'blocked', reason: '请处理冲突后重新同步',
    branches: [{ branch, status: 'blocked', reason: '请检查保留工作区', workspace }],
  } }));
  const terminal = await openTerminal(page);
  await expect(terminal).toContainText(branch);
  await expect(terminal).toContainText(workspace);
  await expect(terminal.getByRole('button', { name: '重新同步', exact: true })).toBeEnabled();
  await expect.poll(async () => {
    const bounds = await terminal.boundingBox();
    return bounds ? bounds.x + bounds.width : Number.POSITIVE_INFINITY;
  }).toBeLessThanOrEqual(391);
  const bounds = await terminal.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(-1);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(391);
  await page.screenshot({ path: '.playwright/master-sync-terminal-mobile.png' });
  const log = terminal.getByRole('log', { name: '分支同步执行日志' });
  await log.scrollIntoViewIfNeeded();
  await expect(log).toContainText('保留 master 修复与 feat 新功能');
  expect(await log.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  const search = terminal.getByPlaceholder('搜索命令、日志或方案');
  await search.fill('feat 新功能');
  await expect(log).not.toContainText('$ git fetch origin');
  await page.screenshot({ path: '.playwright/master-sync-terminal-mobile-log.png' });
});
