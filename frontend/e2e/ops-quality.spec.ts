import { test, expect, type Page } from '@playwright/test';
import { expectNoHorizontalScroll, expectNoSeriousA11y, loginAs, sql } from './support/ops';

/** Accessibility (axe: 0 serious/critical) and 375px layout for the ops pages, light and dark. Run with --workers=1. */
test.describe.configure({ mode: 'serial' });

const PAGES: { name: string; url: string; ready: (p: Page) => Promise<void>; prep?: (p: Page) => Promise<void> }[] = [
  { name: 'sources', url: '/sources', ready: async (p) => { await expect(p.getByTestId('sources-table')).toBeVisible(); } },
  { name: 'exports', url: '/exports', ready: async (p) => { await expect(p.getByTestId('preview-count')).not.toHaveText('—'); await expect(p.getByTestId('exports-table')).toBeVisible(); } },
  { name: 'exports-listmonk', url: '/exports', ready: async (p) => { await p.getByTestId('preset-listmonk_ready').click(); await expect(p.getByTestId('listmonk-note')).toBeVisible(); } },
  { name: 'system', url: '/system', ready: async (p) => { await expect(p.getByTestId('card-api')).toBeVisible(); await expect(p.getByTestId('failed-jobs-table').or(p.getByText('No failed jobs'))).toBeVisible(); } },
  { name: 'settings-account', url: '/settings', ready: async (p) => { await expect(p.getByRole('heading', { name: 'Change password' })).toBeVisible(); } },
  { name: 'settings-users', url: '/settings?tab=users', ready: async (p) => { await expect(p.getByTestId('users-table')).toBeVisible(); } },
  { name: 'settings-worker', url: '/settings?tab=worker', ready: async (p) => { await expect(p.getByLabel('Concurrent jobs per worker')).toBeVisible(); } },
  { name: 'settings-source-policies', url: '/settings?tab=source-policies', ready: async (p) => { await expect(p.getByLabel('Blocked domains')).toBeVisible(); } },
  { name: 'settings-validation', url: '/settings?tab=validation', ready: async (p) => { await p.getByRole('button', { name: 'Add country row' }).click(); await expect(p.getByLabel('Country code, row 1')).toBeVisible(); } },
  { name: 'settings-concurrency', url: '/settings?tab=concurrency', ready: async (p) => { await expect(p.getByLabel('Batch size')).toBeVisible(); } },
  { name: 'settings-retention', url: '/settings?tab=retention', ready: async (p) => { await expect(p.getByTestId('run-retention')).toBeVisible(); } },
  { name: 'settings-exports', url: '/settings?tab=exports', ready: async (p) => { await expect(p.getByLabel('Maximum rows per export')).toBeVisible(); } },
  { name: 'settings-countries', url: '/settings?tab=countries', ready: async (p) => { await expect(p.getByRole('link', { name: /Open categories/ })).toBeVisible(); } },
  { name: 'settings-notifications', url: '/settings?tab=notifications', ready: async (p) => { await expect(p.getByLabel('Webhook URL')).toBeVisible(); } },
  { name: 'settings-audit', url: '/settings?tab=audit', ready: async (p) => { await expect(p.getByTestId('audit-table')).toBeVisible(); } },
  { name: 'settings-backups', url: '/settings?tab=backups', ready: async (p) => { await expect(p.getByLabel('Retention (days)')).toBeVisible(); } },
];

test.beforeAll(() => {
  // a failed job so the failed-jobs table, retry buttons and queue chart are exercised
  sql(`DELETE FROM sourcing_runs WHERE name='e2e-ops-quality'`);
  const id = sql(`INSERT INTO sourcing_runs (name, configuration_snapshot, target_count, target_definition, uniqueness, status, counters, archived) VALUES ('e2e-ops-quality', '{}', 5, 'emails', 'email', 'paused', '{}', false) RETURNING id`).split('\n')[0];
  sql(`INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, checkpoint, state, priority, attempts, error_code, error_detail, finished_at) VALUES (${id}, 'verify', 'q-1', '{}', '{}', 'failed', 10, 2, 'dns_timeout', 'synthetic', now()), (${id}, 'crawl', 'q-2', '{}', '{}', 'queued', 10, 0, NULL, NULL, NULL)`);
});
test.afterAll(() => { sql(`DELETE FROM sourcing_runs WHERE name='e2e-ops-quality'`); });

for (const theme of ['light', 'dark'] as const) {
  test.describe(`axe, ${theme}`, () => {
    for (const pg of PAGES) {
      test(`${pg.name}`, async ({ page }) => {
        await page.addInitScript((t) => { try { localStorage.setItem('lead-theme', t); } catch { /* */ } }, theme);
        await loginAs(page, 'admin');
        await page.goto(pg.url);
        await pg.ready(page);
        if (theme === 'dark') await expect(page.locator('html')).toHaveClass(/dark/);
        await expectNoSeriousA11y(page, `${pg.name} (${theme})`);
      });
    }
  });
}

test.describe('axe with overlays open', () => {
  test('sources drawer, upload dialog, system confirm dialog, settings MFA setup, users create dialog', async ({ page }) => {
    await loginAs(page, 'admin');
    await page.goto('/sources');
    await page.getByRole('button', { name: /Details for Imported business/ }).click();
    await expect(page.getByRole('dialog').getByText('Administrator controls')).toBeVisible();
    await expectNoSeriousA11y(page, 'source drawer');
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'Upload file' }).click();
    await expectNoSeriousA11y(page, 'upload dialog');
    await page.keyboard.press('Escape');

    await page.goto('/system');
    await page.getByRole('button', { name: 'Turn maintenance on' }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await expectNoSeriousA11y(page, 'maintenance confirm');
    await page.keyboard.press('Escape');

    await page.goto('/settings?tab=users');
    await page.getByTestId('create-user').click();
    await expectNoSeriousA11y(page, 'create user dialog');

    await page.goto('/settings');
    await page.getByTestId('mfa-start').click();
    await expect(page.getByTestId('mfa-qr')).toBeVisible();
    await expectNoSeriousA11y(page, 'MFA setup (QR)');
  });
});

test.describe('375px layout', () => {
  test.use({ viewport: { width: 375, height: 800 } });
  for (const pg of PAGES) {
    test(`${pg.name} has no horizontal scroll`, async ({ page }) => {
      await loginAs(page, 'admin');
      await page.goto(pg.url);
      await pg.ready(page);
      await expectNoHorizontalScroll(page, `${pg.name} @375`);
    });
  }
  test('sources drawer fits the viewport', async ({ page }) => {
    await loginAs(page, 'admin');
    await page.goto('/sources');
    await page.getByRole('button', { name: /Details for Imported business/ }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.waitForTimeout(600); // slide-in animation
    await expectNoHorizontalScroll(page, 'drawer @375');
    const box = await page.getByRole('dialog').boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(376);
  });
});
