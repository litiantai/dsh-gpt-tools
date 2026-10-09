/** 在临时目录验证目标分支与候选版数据互读、重启及备份恢复；不读取真实用户数据。 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, cpSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';

const workspace = process.cwd();
const require = createRequire(join(workspace, 'package.json'));
const { build } = require('esbuild');
const base = process.env.THSOCTOP_COMPAT_BASE;
const head = process.env.THSOCTOP_COMPAT_HEAD;
assert.match(base || '', /^[a-f0-9]{40,64}$/);
assert.match(head || '', /^[a-f0-9]{40,64}$/);
const git = (...args) => execFileSync('git', args, { cwd: workspace });
assert.equal(git('rev-parse', 'HEAD').toString().trim(), head);
const changed = git('diff', '--name-only', base, head).toString().trim().split('\n')
  .filter(path => /(store|state|migration|credentials|paths)\.(ts|rs)$/.test(path));
const covered = new Set(['packages/dsh-alerts/src/store.ts', 'packages/dsh-alerts/src/indicator-store.ts',
  'packages/dsh-research/src/store.ts', 'packages/shared/src/resource-state.ts']);
assert.ok(changed.every(path => covered.has(path)), `尚无兼容验证的持久化文件：${changed.filter(path => !covered.has(path)).join(', ')}`);
const root = mkdtempSync(join(tmpdir(), 'thsoctop-compat-'));
const old = join(root, 'base');
mkdirSync(old);
execFileSync('tar', ['-xf', '-', '-C', old], { input: git('archive', base, 'packages/dsh-alerts/src') });

async function moduleFrom(dir, source, name) {
  const outfile = join(root, `${name}.cjs`);
  await build({ entryPoints: [join(dir, source)], outfile, bundle: true, platform: 'node', format: 'cjs' });
  return require(outfile);
}
function restore(file, backup) {
  writeFileSync(file, '{interrupted write');
  cpSync(backup, file);
  assert.deepEqual(readFileSync(file), readFileSync(backup));
}
try {
  const { AlertsStore: OldAlerts } = await moduleFrom(old, 'packages/dsh-alerts/src/store.ts', 'old-alerts');
  const { AlertsStore: NewAlerts } = await moduleFrom(workspace, 'packages/dsh-alerts/src/store.ts', 'new-alerts');
  const alertsFile = join(root, 'alerts.json');
  const oldAlerts = new OldAlerts(alertsFile);
  const rule = oldAlerts.addRule({ name: '旧版价格预警', trigger: { kind: 'price', code: '600519', market: 'SH', direction: 'above', price: 1700 } });
  oldAlerts.recordFire(rule, { id: 'historical-event', ruleId: rule.id, message: '历史提醒', firedAt: '2026-09-28T02:00:00Z' });
  oldAlerts.updateSettings({ pollMs: 12000, quietHours: { start: '22:00', end: '07:00' } });
  oldAlerts.flush();
  const before = oldAlerts.snapshot();
  cpSync(alertsFile, `${alertsFile}.bak`);
  const newAlerts = new NewAlerts(alertsFile);
  assert.deepEqual(newAlerts.snapshot(), before);
  newAlerts.updateRule(rule.id, { trigger: { ...rule.trigger, price: 1800 } });
  newAlerts.flush();
  assert.deepEqual(new NewAlerts(alertsFile).snapshot(), newAlerts.snapshot());
  assert.deepEqual(new OldAlerts(alertsFile).snapshot(), newAlerts.snapshot());
  assert.deepEqual(newAlerts.snapshot().history, before.history);
  assert.deepEqual(newAlerts.snapshot().settings, before.settings);
  restore(alertsFile, `${alertsFile}.bak`);
  assert.deepEqual(new NewAlerts(alertsFile).snapshot(), before);
  assert.deepEqual(new OldAlerts(alertsFile).snapshot(), before);
  assert.equal(statSync(alertsFile).mode & 0o777, 0o600);
  console.log('PASS 预警：旧版写入→新版读取和编辑→双方重启读取→备份恢复，历史与设置保留');

  const { IndicatorStore: OldIndicators } = await moduleFrom(old, 'packages/dsh-alerts/src/indicator-store.ts', 'old-indicators');
  const { IndicatorStore: NewIndicators } = await moduleFrom(workspace, 'packages/dsh-alerts/src/indicator-store.ts', 'new-indicators');
  const indicatorFile = join(root, 'indicators.json');
  const oldIndicators = new OldIndicators(indicatorFile);
  for (const id of ['one', 'two']) oldIndicators.add({ id, name: id, code: '300033.SZ', trigger: { kind: 'rsi_extreme', period: 14, threshold: 70, direction: 'over' } });
  oldIndicators.recordFired(['one:2026-09-28', 'two:2026-09-28']);
  cpSync(indicatorFile, `${indicatorFile}.bak`);
  const newIndicators = new NewIndicators(indicatorFile);
  assert.deepEqual(newIndicators.rules(), oldIndicators.rules());
  assert.deepEqual(newIndicators.firedKeys(), oldIndicators.firedKeys());
  newIndicators.update('one', { trigger: { kind: 'ma_cross', period: 20, direction: 'above' } });
  assert.deepEqual(newIndicators.firedKeys(), ['two:2026-09-28']);
  for (const Store of [OldIndicators, NewIndicators]) {
    assert.deepEqual(new Store(indicatorFile).rules(), newIndicators.rules());
    assert.deepEqual(new Store(indicatorFile).firedKeys(), newIndicators.firedKeys());
  }
  restore(indicatorFile, `${indicatorFile}.bak`);
  assert.deepEqual(new NewIndicators(indicatorFile).rules(), oldIndicators.rules());
  assert.deepEqual(new NewIndicators(indicatorFile).firedKeys(), oldIndicators.firedKeys());
  console.log('PASS 指标：旧规则与去重状态兼容，新版编辑后旧版可读，备份可恢复');

  const { ResearchStore, parseAutomationInput } = await moduleFrom(workspace, 'packages/dsh-research/src/store.ts', 'research');
  const researchFile = join(root, 'research.json');
  const research = new ResearchStore(researchFile);
  const automation = research.create(parseAutomationInput({ name: '恢复测试', prompt: '这是仅用于数据恢复验证的报告模板', skillIds: ['skill-finance-analysis'], schedule: { kind: 'manual' } }));
  const run = research.addRun(automation, 'manual');
  const html = join(root, 'report.html');
  writeFileSync(html, '<html>独立报告正文</html>');
  research.publish({ id: 'report-1', runId: run.id, path: html, title: '恢复测试', createdAt: '2026-10-08T00:00:00Z', sources: ['fixture'] });
  research.updateRun(run.id, { status: 'succeeded' });
  const saved = structuredClone(research.snapshot());
  assert.deepEqual(new ResearchStore(researchFile).snapshot(), saved);
  cpSync(researchFile, `${researchFile}.bak`);
  const reportBefore = readFileSync(html);
  // 旧版预警模块读写自己的文件，不应覆盖新版独立索引与 HTML。
  const rollback = new OldAlerts(alertsFile);
  rollback.updateSettings({ pollMs: 15000 }); rollback.flush();
  assert.deepEqual(new ResearchStore(researchFile).snapshot(), saved);
  research.update(automation.id, { enabled: false });
  restore(researchFile, `${researchFile}.bak`);
  assert.deepEqual(new ResearchStore(researchFile).snapshot(), saved);
  assert.deepEqual(readFileSync(html), reportBefore);
  assert.equal(statSync(researchFile).mode & 0o777, 0o600);
  console.log('PASS 研究索引：新建、重启、旧版共存及恢复，报告正文保持不变');

  const { ResourceCache } = await moduleFrom(workspace, 'packages/shared/src/resource-state.ts', 'cache');
  const cache = new ResourceCache();
  await cache.read('quote', 10000, async () => ({ value: 'old' }));
  assert.equal((await new ResourceCache().read('quote', 10000, async () => ({ value: 'fresh' }))).value, 'fresh');
  console.log('PASS ResourceCache 为内存缓存；重新实例化后重新取数，不迁移持久化数据');
  console.log(JSON.stringify({ status: 'pass', base, head, covered: changed }));
} finally {
  rmSync(root, { recursive: true, force: true });
}
