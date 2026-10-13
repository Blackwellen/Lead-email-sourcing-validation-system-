import { test, expect } from '@playwright/test';
import { apiLogin, createRunViaApi, expectNoSeriousViolations, hasBackend, uniqueName } from './runs-helpers';

test.skip(!hasBackend, 'E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD not set (needs a real backend)');

test.describe('accessibility and responsiveness of run pages (real backend)', () => {
  let runId = 0;
  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage({ baseURL: process.env.E2E_BASE_URL });
    await apiLogin(page);
    runId = await createRunViaApi(page, uniqueName('E2E a11y'));
    await page.close();
  });

  const paths = () => ['/dashboard', '/runs', '/runs/new', `/runs/${runId}`];

  for (const [i, label] of ['/dashboard', '/runs', '/runs/new', '/runs/[id]'].entries()) {
    test(`axe: ${label} has no serious or critical violations`, async ({ page }) => {
      await apiLogin(page);
      await page.goto(paths()[i]);
      await page.waitForLoadState('networkidle');
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await expectNoSeriousViolations(page);
    });
    test(`no horizontal scroll at 375px on ${label}`, async ({ page }) => {
      await apiLogin(page);
      await page.setViewportSize({ width: 375, height: 800 });
      await page.goto(paths()[i]);
      await page.waitForLoadState('networkidle');
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      const overflow = await page.evaluate(() => Math.max(document.documentElement.scrollWidth - document.documentElement.clientWidth, (document.querySelector('main')?.scrollWidth ?? 0) - (document.querySelector('main')?.clientWidth ?? 0)));
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }

  test('axe and 375px on every wizard step', async ({ page }) => {
    await apiLogin(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/runs/new');
    await page.getByLabel('Run name').fill(uniqueName('E2E a11y wizard'));
    await expectNoSeriousViolations(page);
    for (let step = 2; step <= 5; step++) {
      await page.getByRole('button', { name: /^Next:/ }).click();
      await expect(page.getByTestId('step-heading')).toContainText(`Step ${step} of 5`);
      if (step === 2) await page.getByLabel('Domains', { exact: true }).fill('example.com');
      await page.waitForLoadState('networkidle');
      await expectNoSeriousViolations(page);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `step ${step} overflow`).toBeLessThanOrEqual(0);
      if (step === 5) break;
    }
  });
});
