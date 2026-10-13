import { test, expect, type Page } from '@playwright/test';
import { execSync } from 'node:child_process';
import { apiClient, expectNoHorizontalScroll, expectNoSeriousViolations, login } from './support/biz';

test.describe.configure({ mode: 'serial' });
const stamp = Date.now().toString(36);
const CSV_BATCH = `E2E CSV batch ${stamp}`;
const FILTER_BATCH = `E2E catch-all re-verify ${stamp}`;

const TABS = ['Overview', 'Batch Jobs', 'Invalid', 'Inconclusive', 'Catch-all', 'Needs Recheck', 'Verification History'];

async function someEmails(page: Page, n: number) {
  const api = await apiClient(page);
  return ((await api.get(`/api/contacts?q=person2&limit=${n}`)).items as { email: string }[]).map((i) => i.email);
}

test.describe('verification centre', () => {
  test.beforeEach(async ({ page }) => { await login(page); });

  test('all seven tabs render against the real API', async ({ page }) => {
    await page.goto('/verification');
    await expect(page.getByRole('tab')).toHaveText(TABS.map((t) => new RegExp(t)));
    await expect(page.getByText('Reachability distribution')).toBeVisible();
    await expect(page.getByText('“Safe” means no adverse evidence for a published address. It is not a delivery guarantee.')).toBeVisible();
    for (const [tab, caption] of [['Invalid', 'Invalid addresses'], ['Inconclusive', 'Inconclusive addresses'], ['Catch-all', 'Catch-all addresses'], ['Needs Recheck', 'Needs recheck addresses']] as const) {
      await page.getByRole('tab', { name: new RegExp(tab) }).click();
      await expect(page.getByRole('table', { name: caption })).toBeVisible();
      await expect(page.locator('tbody tr[data-row]').first()).toBeVisible();
    }
    await expect(page).toHaveURL(/tab=needs_recheck/);
    await page.getByRole('tab', { name: /Verification History/ }).click();
    await expect(page.getByRole('table', { name: 'Verification history' })).toBeVisible();
    await expect(page.locator('tbody tr[data-row]').first()).toContainText(/V\d\d_/);
    await page.getByRole('tab', { name: /Batch Jobs/ }).click();
    await expect(page.getByRole('table', { name: 'Verification batches' }).or(page.getByText('No verification batches yet'))).toBeVisible();
  });

  test('invalid tab only lists addresses with invalid reachability', async ({ page }) => {
    await page.goto('/verification?tab=invalid');
    const rs = page.locator('tbody tr[data-row]');
    await expect(rs.first()).toBeVisible();
    for (let i = 0; i < Math.min(await rs.count(), 10); i++) await expect(rs.nth(i)).toContainText('Invalid');
  });

  test('CSV import creates a batch, shows the estimate and skipped unknown addresses, then pause/resume/cancel', async ({ page }) => {
    const known = await someEmails(page, 3);
    expect(known.length).toBe(3);
    const csv = ['email', ...known, 'nobody@not-in-database.example.com', 'ghost@not-in-database.example.org'].join('\n');
    await page.goto('/verification');
    await page.getByRole('button', { name: 'Import CSV to verify' }).click();
    const dlg = page.getByRole('dialog', { name: 'Import a CSV to verify' });
    await dlg.locator('input[type=file]').setInputFiles({ name: 'to-verify.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
    await expect(dlg.getByTestId('file-name')).toContainText('to-verify.csv · 5 addresses');
    await dlg.getByLabel('Batch name').fill(CSV_BATCH);
    // honest estimate and limitations
    const est = dlg.getByTestId('estimate');
    await expect(est).toContainText('About 5 addresses');
    await expect(est).toContainText('Syntax and normalisation');
    await est.getByText('What this cannot tell you').click();
    await expect(est).toContainText('no check can guarantee a message reaches an inbox');
    // mode switch updates the estimate and warns about SMTP when diagnostics are disabled
    await dlg.getByRole('radio', { name: 'Deep' }).click();
    await expect(est).toContainText('SMTP diagnostics');
    await expect(est).toContainText('not enabled on this deployment');
    await dlg.getByRole('radio', { name: 'Fast' }).click();
    await dlg.getByRole('button', { name: 'Start batch' }).click();
    const queued = page.getByRole('dialog', { name: 'Batch queued' });
    const summary = queued.getByTestId('upload-summary');
    await expect(summary).toContainText('Rows read: 5');
    await expect(summary).toContainText('Matched in the database: 3');
    await expect(summary).toContainText('Skipped, unknown address: 2');
    await queued.getByRole('button', { name: 'Done' }).click();

    await expect(page).toHaveURL(/tab=batches/);
    const row = page.locator('tbody tr[data-row]').filter({ hasText: CSV_BATCH });
    await expect(row).toBeVisible();
    await expect(row).toContainText('0 / 3 done');
    const api = await apiClient(page);
    const batches = await api.get('/api/verification/batches');
    expect(batches.items.find((b: { name: string; total: number }) => b.name === CSV_BATCH).total).toBe(3);

    await row.getByRole('button', { name: 'Pause' }).click();
    await expect(row).toContainText('Paused');
    await row.getByRole('button', { name: 'Resume' }).click();
    await expect(row).not.toContainText('Paused');
    await row.getByRole('button', { name: 'Cancel' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel batch' }).click();
    await expect(row).toContainText('Cancelled');
  });

  test('a non-CSV-able upload with no matching addresses is rejected with a clear message', async ({ page }) => {
    await page.goto('/verification');
    await page.getByRole('button', { name: 'Import CSV to verify' }).click();
    const dlg = page.getByRole('dialog', { name: 'Import a CSV to verify' });
    await dlg.locator('input[type=file]').setInputFiles({ name: 'unknown.csv', mimeType: 'text/csv', buffer: Buffer.from('email\nzzz@nowhere-at-all.example.net\n') });
    await dlg.getByRole('button', { name: 'Start batch' }).click();
    await expect(page.getByText('Could not start the batch')).toBeVisible();
    await expect(page.getByText(/None of the uploaded addresses exist/)).toBeVisible();
  });

  test('start a batch from a tab’s filters', async ({ page }) => {
    await page.goto('/verification?tab=catch_all');
    await page.getByRole('button', { name: 'Re-verify these…' }).click();
    const dlg = page.getByRole('dialog', { name: 'Start a verification batch' });
    await dlg.getByLabel('Batch name').fill(FILTER_BATCH);
    await expect(dlg.getByTestId('estimate')).toContainText('addresses');
    await dlg.getByRole('radio', { name: 'Custom' }).click();
    await dlg.getByRole('checkbox', { name: 'SPF / DMARC presence' }).check();
    await dlg.getByRole('button', { name: 'Start batch' }).click();
    const queued = page.getByRole('dialog', { name: 'Batch queued' });
    await expect(queued).toBeVisible();
    await queued.getByRole('button', { name: 'Done' }).click();
    const api = await apiClient(page);
    const b = (await api.get('/api/verification/batches')).items.find((x: { name: string }) => x.name === FILTER_BATCH);
    expect(b.mode).toBe('custom');
    expect(b.total).toBeGreaterThan(0);
  });

  test('failed jobs in a batch can be inspected', async ({ page }) => {
    const name = `E2E failing batch ${stamp}`;
    const emails = await someEmails(page, 2);
    const api = await apiClient(page);
    const ids = ((await api.get(`/api/contacts?q=${emails[0]}&limit=1`)).items as { id: number }[]).map((i) => i.id);
    const b = await api.post('/api/verification/batches', { name, mode: 'fast', email_ids: ids });
    // there is no worker in this environment, so mark the queued job failed the way a worker would
    execSync(`PGPASSWORD=lead psql -h localhost -U lead -d lead_fe_b -c "UPDATE sourcing_jobs SET state='failed', error_code='dns_timeout', error_detail='synthetic failure for e2e', attempts=3 WHERE run_id=${b.run_id}"`, { stdio: 'pipe' });
    await page.goto('/verification?tab=batches');
    const row = page.locator('tbody tr[data-row]').filter({ hasText: name });
    await row.getByRole('button', { name: /Inspect 1 failure/ }).click();
    const sheet = page.getByRole('dialog', { name: new RegExp(`Failures: ${name}`) });
    await expect(sheet.getByRole('list', { name: 'Failed jobs' })).toContainText('dns_timeout: synthetic failure for e2e');
    await expect(sheet.getByRole('link', { name: `Address #${ids[0]}` })).toBeVisible();
  });

  test('export results from a tab creates a validation-audit export', async ({ page }) => {
    const api = await apiClient(page);
    const before = (await api.get('/api/exports?limit=1')).items[0]?.id ?? 0;
    await page.goto('/verification?tab=invalid');
    await page.getByRole('button', { name: 'Export results' }).click();
    await expect(page.getByText(/Export #\d+ created/)).toBeVisible();
    const ex = (await api.get('/api/exports?limit=1')).items[0];
    expect(ex.id).toBeGreaterThan(before);
    expect(ex.preset).toBe('validation_audit');
    expect(ex.filters.reachability).toEqual(['invalid']);
  });

  test('delivery-events import reports what was inserted', async ({ page }) => {
    const [email] = await someEmails(page, 1);
    await page.goto('/verification');
    await page.getByRole('button', { name: 'Import delivery events' }).click();
    const dlg = page.getByRole('dialog', { name: 'Import delivery events' });
    const csv = `email,event_type,campaign,occurred_at,detail\n${email},delivered,e2e-campaign,2026-01-02T10:00:00Z,synthetic\nunknown@not-in-database.example.com,delivered,e2e-campaign,2026-01-02T10:00:00Z,synthetic\n`;
    await dlg.locator('input[type=file]').setInputFiles({ name: 'events.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
    await dlg.getByRole('button', { name: 'Import events' }).click();
    const s = dlg.getByTestId('delivery-summary');
    await expect(s).toContainText('Received');
    await expect(s.locator('div', { hasText: 'Inserted' }).first()).toContainText('1');
    await expect(s.locator('div', { hasText: 'Unknown address (skipped)' }).first()).toContainText('1');
  });

  test('SMTP diagnostics panel: status and domain permission CRUD for admins', async ({ page }) => {
    await page.goto('/verification');
    await expect(page.getByRole('heading', { name: /SMTP diagnostics/ })).toContainText('Disabled');
    await expect(page.getByRole('list', { name: 'SMTP problems' }).getByText(/SMTP diagnostics are disabled/)).toBeVisible();
    await page.getByLabel('Domain', { exact: true }).fill('e2e-owned.example.com');
    await page.getByRole('button', { name: 'Authorise' }).click();
    await expect(page.getByText('Describe the authorisation basis')).toBeVisible();
    await page.getByLabel('Authorisation basis').fill('Domain owned and operated by us (e2e).');
    await page.getByRole('button', { name: 'Authorise' }).click();
    const table = page.getByRole('table', { name: 'Domain permissions for SMTP diagnostics' });
    await expect(table).toContainText('e2e-owned.example.com');
    await expect(table).toContainText('Domain owned and operated by us (e2e).');
    await page.getByRole('button', { name: 'Remove permission for e2e-owned.example.com' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Remove' }).click();
    await expect(page.getByText('Permission removed')).toBeVisible();
    await expect(table).toHaveCount(0);
  });

  test('operators see no domain-permission form; viewers see no import buttons', async ({ page }) => {
    await login(page, 'operator');
    await page.goto('/verification');
    await expect(page.getByRole('button', { name: 'Import CSV to verify' })).toBeVisible();
    await expect(page.getByRole('form', { name: 'Authorise a domain' })).toHaveCount(0);
    await login(page, 'viewer');
    await page.goto('/verification');
    await expect(page.getByRole('heading', { name: 'Verification', level: 1 })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Import CSV to verify' })).toHaveCount(0);
  });

  test('accessibility on every tab and the import dialog; 375px layout', async ({ page }) => {
    await page.goto('/verification');
    await expect(page.getByText('Reachability distribution')).toBeVisible();
    for (const t of TABS) {
      await page.getByRole('tab', { name: new RegExp(t) }).click();
      await page.waitForLoadState('networkidle');
      await expectNoSeriousViolations(page);
    }
    await page.getByRole('button', { name: 'Import CSV to verify' }).click();
    await expectNoSeriousViolations(page);
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/verification');
    await expect(page.getByText('Reachability distribution')).toBeVisible();
    await expectNoHorizontalScroll(page);
    await page.goto('/verification?tab=invalid');
    await expect(page.locator('tbody tr[data-row]').first()).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});
