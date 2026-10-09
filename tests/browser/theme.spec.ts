import { test, expect, type Locator } from '@playwright/test';

// Check rendered colors rather than token values: CSS inheritance and portals caused the regression.
async function expectReadableSurface(locator: Locator, dark?: boolean) {
  const result = await locator.evaluate(element => {
    const rgb = (value: string) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = (values: number[]) => values.map(v => {
      const c = v / 255;
      return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
    }).reduce((sum, c, i) => sum + c * [.2126, .7152, .0722][i], 0);
    const style = getComputedStyle(element);
    let parent: Element | null = element;
    let background = style.backgroundColor;
    while (parent && (background === 'rgba(0, 0, 0, 0)' || background === 'transparent')) {
      parent = parent.parentElement;
      if (parent) background = getComputedStyle(parent).backgroundColor;
    }
    const bg = luminance(rgb(background));
    const fg = luminance(rgb(style.color));
    return { bg, contrast: (Math.max(bg, fg) + .05) / (Math.min(bg, fg) + .05) };
  });
  if (dark !== undefined) expect(result.bg)[dark ? 'toBeLessThan' : 'toBeGreaterThan'](dark ? .12 : .8);
  expect(result.contrast).toBeGreaterThanOrEqual(4.5);
}

test('dark portals and light pages keep readable, consistent colors through navigation', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const product = { id: 'theme', name: '主题检查项目', status: 'observing', version: 1, created: 1, updated: 1 };
  const delivery = { id: 'theme-delivery', product_id: 'theme', title: '首次源码基线', status: 'delivered',
    branch: 'release-20261008', version: 1, created: 1, updated: 1, pr_url: 'https://github.com/example/project/pull/1' };
  await page.route('**/api/products', r => r.fulfill({ json: [product] }));
  await page.route('**/api/runs', r => r.fulfill({ json: [delivery] }));
  await page.route('**/api/products/theme/workbench', r => r.fulfill({ json: { sessions: [], reviews: [], events: [] } }));
  await page.goto('/autopilot?project=theme');
  await expect(page.getByRole('heading', { name: product.name, exact: true })).toBeVisible();
  await expectReadableSurface(page.getByRole('button', { name: '已上线 0', exact: true }), false);
  await page.screenshot({ path: '.playwright/theme-dark-overview.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '已交付未上线 1', exact: true }).click();
  const drawer = page.locator('.ant-drawer-content');
  await expect(drawer).toBeVisible();
  await expectReadableSurface(drawer.locator('th').first(), true);
  await expectReadableSurface(drawer.locator('.text-link').first(), true);
  await expectReadableSurface(drawer.getByRole('link', { name: '查看 PR' }), true);
  await page.screenshot({ path: '.playwright/theme-dark-drawer.png', fullPage: false, animations: 'disabled' });
  await drawer.getByRole('combobox', { name: '筛选记录状态' }).click();
  await expectReadableSurface(page.locator('.ant-select-dropdown:visible .ant-select-item').first(), true);
  await page.keyboard.press('Escape');
  await drawer.getByRole('button', { name: '详情', exact: true }).click();
  const modal = page.locator('.ant-modal-content');
  await expectReadableSurface(modal, true);
  await modal.getByRole('tab', { name: '原始记录' }).click();
  const rawRecord = modal.getByRole('tabpanel', { name: '原始记录' });
  await rawRecord.locator('summary').click();
  await expectReadableSurface(rawRecord.locator('.log'), true);
  await page.screenshot({ path: '.playwright/theme-dark-details.png', fullPage: false, animations: 'disabled' });
  await modal.getByRole('button', { name: 'Close', exact: true }).click();
  await drawer.locator('.ant-drawer-close').click();
  await page.getByRole('button', { name: /记录需求/ }).click();
  await expectReadableSurface(page.locator('.ant-modal-content:visible .ant-btn-primary'));
  await page.locator('.ant-modal-content:visible').getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('link', { name: '全部会话', exact: true }).click();
  await expect(page.getByRole('heading', { name: '会话管理', exact: true })).toBeVisible();
  await expectReadableSurface(page.locator('th').first(), false);
  await expectReadableSurface(page.locator('.text-link').first(), false);
  await page.locator('.text-link').first().click();
  await expectReadableSurface(page.locator('.ant-drawer-content'), false);
  await page.screenshot({ path: '.playwright/theme-light-drawer.png', fullPage: true, animations: 'disabled' });
  await page.locator('.ant-drawer-close').click();
  await page.getByRole('link', { name: '监工总览', exact: true }).click();
  await expect(page.getByRole('heading', { name: '总览', exact: true })).toBeVisible();
  await page.screenshot({ path: '.playwright/theme-light-overview.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('link', { name: '持续研发', exact: true }).click();
  await page.getByRole('button', { name: '已交付未上线 1', exact: true }).click();
  await expectReadableSurface(drawer.locator('th').first(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '.playwright/theme-dark-mobile.png', fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});
