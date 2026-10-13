import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function expectNoSeriousViolations(page: import('@playwright/test').Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(' | ')})`)).toEqual([]);
}

test.describe('foundation', () => {
  test('login renders and degrades gracefully without a backend session', async ({ page }) => {
    await page.goto('/login');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expect(page.getByLabel('Email', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Password', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
    // validation
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText('Enter your email address')).toBeVisible();
    // first-run help
    await page.getByText('First time here?').click();
    await expect(page.getByText('create-admin').first()).toBeVisible();
  });

  test('unauthenticated visit to the app redirects to /login', async ({ page }) => {
    await page.goto('/runs');
    await expect(page).toHaveURL(/\/login\?next=%2Fruns/);
  });

  test('axe: /login has no serious or critical violations', async ({ page }) => {
    await page.goto('/login');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expectNoSeriousViolations(page);
  });

  test('axe: /design has no serious or critical violations (light and dark)', async ({ page }) => {
    await page.goto('/design');
    await expect(page.getByRole('heading', { name: 'Design system', level: 1 })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Example contacts' })).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.getByTestId('theme-toggle').first().click();
    await expect(page.locator('html')).toHaveClass(/dark/);
    await expectNoSeriousViolations(page);
  });

  for (const path of ['/login', '/design']) {
    test(`no horizontal scroll at 375px on ${path}`, async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 800 });
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }

  test('theme toggle switches and persists', async ({ page }) => {
    await page.goto('/login');
    const html = page.locator('html');
    await expect(html).not.toHaveClass(/dark/); // light is the default
    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveClass(/dark/);
    await page.reload();
    await expect(html).toHaveClass(/dark/);
    await page.getByTestId('theme-toggle').click();
    await expect(html).not.toHaveClass(/dark/);
  });

  test('data table: load more, sorting and keyboard navigation (design page)', async ({ page }) => {
    await page.goto('/design');
    const table = page.getByRole('table', { name: 'Example contacts' });
    await expect(table.locator('tbody tr[data-row]').first()).toBeVisible();
    await expect(table.locator('tbody tr[data-row]')).toHaveCount(25);
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(table.locator('tbody tr[data-row]')).toHaveCount(50);
    const first = table.locator('tbody tr[data-row]').first();
    await first.focus();
    await page.keyboard.press('ArrowDown');
    await expect(table.locator('tbody tr[data-row]').nth(1)).toBeFocused();
    await page.keyboard.press(' ');
    await expect(page.getByText('1 selected')).toBeVisible();
  });
});
