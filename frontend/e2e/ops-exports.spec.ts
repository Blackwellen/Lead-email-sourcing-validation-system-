import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { apiAs, loginAs, parseCsv, sql, USERS } from './support/ops';
import { request as pwRequest } from '@playwright/test';

/** DASH-012 /exports against the real backend. Run with --workers=1; seed: e2e/support/ops-seed.sql. */
test.describe.configure({ mode: 'serial' });

async function pickPreset(page: Page, id: string) {
  await page.getByTestId(`preset-${id}`).click();
}
async function createAndWait(page: Page) {
  const before = await page.getByTestId('exports-table').locator('tbody tr').count().catch(() => 0);
  await page.getByTestId('create-export').click();
  await expect(page.getByTestId('exports-table').locator('tbody tr')).toHaveCount(before + 1);
  const row = page.getByTestId('exports-table').locator('tbody tr').first();
  await expect(row.getByText('Done')).toBeVisible({ timeout: 20_000 });
  return row;
}
async function downloadText(page: Page, row: ReturnType<Page['locator']>) {
  const [dl] = await Promise.all([page.waitForEvent('download'), row.getByRole('button', { name: /Download export/ }).click()]);
  const path = await dl.path();
  return { name: dl.suggestedFilename(), buf: readFileSync(path!) };
}

test('Listmonk preset: explanation, forced CSV, create, download and verify columns/eligibility', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/exports');
  await expect(page.getByRole('heading', { name: 'Exports', level: 1 })).toBeVisible();
  await expect(page.getByTestId('preset-columns')).toBeVisible();
  // 8 presets
  await expect(page.getByRole('radiogroup', { name: 'Export preset' }).getByRole('radio')).toHaveCount(8);
  await pickPreset(page, 'listmonk_ready');
  const note = page.getByTestId('listmonk-note');
  await expect(note).toContainText('email');
  await expect(note).toContainText('attributes');
  await expect(note).toContainText('marketing status is eligible');
  await expect(note).toContainText('not suppressed');
  await expect(page.getByRole('radio', { name: 'Excel (XLSX)' })).toBeDisabled();
  await expect(page.getByRole('radio', { name: 'CSV', exact: true })).toBeChecked();
  await expect(page.getByTestId('preview-count')).toHaveText('3');

  const row = await createAndWait(page);
  await expect(row).toContainText('Listmonk-ready');
  await expect(row.getByText('3', { exact: true }).first()).toBeVisible();
  const { name, buf } = await downloadText(page, row);
  expect(name).toMatch(/^export-\d+-listmonk_ready\.csv$/);
  const rows = parseCsv(buf.toString('utf8'));
  expect(rows[0]).toEqual(['email', 'name', 'attributes']);
  const emails = rows.slice(1).map((r) => r[0]).sort();
  expect(emails).toEqual(['ann@alpha-plumbing.example.test', 'bob@beta-bakery.example.test', 'contact@delta-dental.example.test']);
  expect(emails).not.toContain('suppressed@delta-dental.example.test'); // suppressed never exported
  expect(emails).not.toContain('gus@gamma.example.test'); // consent_required, not eligible
  // attributes is a JSON object
  expect(() => JSON.parse(rows[1][2])).not.toThrow();
  // formula-injection: a contact called "=SUM(1+1)" is prefixed with an apostrophe
  const bob = rows.find((r) => r[0].startsWith('bob@'))!;
  expect(bob[1]).toBe("'=SUM(1+1)");
});

test('full export: 30 columns, formula-injection cells are neutralised, note explains it', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/exports');
  await pickPreset(page, 'full_intelligence');
  await expect(page.getByTestId('formula-note')).toContainText('prefixed with an apostrophe');
  await expect(page.getByRole('radio', { name: 'CSV', exact: true })).toBeChecked();
  const row = await createAndWait(page);
  const { buf } = await downloadText(page, row);
  const rows = parseCsv(buf.toString('utf8'));
  expect(rows[0]).toHaveLength(30);
  expect(rows[0]).toEqual(expect.arrayContaining(['Email Address', 'Email Status', 'Confidence Score', 'Company Name', 'Marketing Eligibility', 'Suppression Status']));
  const ci = rows[0].indexOf('Company Name');
  const beta = rows.find((r) => r[0] === 'bob@beta-bakery.example.test')!;
  expect(beta[ci].startsWith("'=HYPERLINK(")).toBe(true);
  // no cell anywhere in the file starts with a raw formula character
  for (const r of rows.slice(1)) for (const c of r) expect(/^[=+\-@\t\r]/.test(c), `unsafe cell ${c}`).toBe(false);
  // the suppressed address is included in the full export but labelled
  const sup = rows.find((r) => r[0] === 'suppressed@delta-dental.example.test')!;
  expect(sup[rows[0].indexOf('Suppression Status')]).toBe('suppressed');
});

test('filters change the live count; the history shows them in a popover; XLSX and JSONL work', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/exports');
  await pickPreset(page, 'full_intelligence');
  await expect(page.getByTestId('preview-count')).toHaveText('7');
  await page.getByRole('combobox', { name: 'Country' }).click();
  await page.getByPlaceholder(/search/i).first().fill('Germany');
  await page.getByRole('option', { name: /Germany/ }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('preview-count')).toHaveText('1');
  // preview agrees with the API
  const a = await apiAs('operator');
  const p = await (await a.post('/api/exports/preview', { data: { preset: 'full_intelligence', format: 'csv', filters: { country: ['DE'] } } })).json();
  expect(p.estimated_rows).toBe(1);
  expect(p.capped).toBe(false);

  // invalid input is reported inline and blocks creation
  await page.getByLabel('Minimum confidence (0–100)').fill('250');
  await expect(page.getByText('Minimum confidence must be a number from 0 to 100')).toBeVisible();
  await expect(page.getByTestId('create-export')).toBeDisabled();
  await page.getByLabel('Minimum confidence (0–100)').fill('50');

  await page.getByRole('radio', { name: 'JSON Lines' }).click();
  const row = await createAndWait(page);
  await row.getByRole('button', { name: /Filters for export/ }).click();
  const pop = page.getByRole('dialog');
  await expect(pop).toContainText('Country');
  await expect(pop).toContainText('DE');
  await expect(pop).toContainText('Min confidence');
  await page.keyboard.press('Escape');
  const { name, buf } = await downloadText(page, row);
  expect(name).toMatch(/\.jsonl$/);
  const lines = buf.toString('utf8').trim().split('\n').map((l) => JSON.parse(l));
  expect(lines).toHaveLength(1);
  expect(lines[0]['Email Address']).toBe('gus@gamma.example.test');
  await a.dispose();

  // xlsx
  await page.getByRole('radio', { name: 'Excel (XLSX)' }).click();
  const xrow = await createAndWait(page);
  const x = await downloadText(page, xrow);
  expect(x.name).toMatch(/\.xlsx$/);
  expect(x.buf.subarray(0, 2).toString()).toBe('PK');
});

test('running exports are polled; failed exports show their error; delete asks for confirmation', async ({ page }) => {
  const id = sql(`INSERT INTO exports (created_by, preset, format, filters, status, row_count) VALUES ((SELECT id FROM users WHERE email='${USERS.admin.email}'), 'one_per_domain', 'csv', '{}', 'running', 0) RETURNING id`).split('\n')[0];
  await loginAs(page, 'admin');
  await page.goto('/exports');
  const row = page.getByTestId(`export-row-${id}`);
  await expect(row.getByText('Running')).toBeVisible();
  await expect(page.getByText('Updating while exports are running')).toBeVisible();
  await expect(row.getByRole('button', { name: `Delete export ${id}` })).toBeDisabled(); // cannot delete while running
  sql(`UPDATE exports SET status='failed', error='RuntimeError: synthetic failure for e2e' WHERE id=${id}`);
  await expect(row.getByText('Failed')).toBeVisible({ timeout: 15_000 }); // no reload: polling
  await expect(row).toContainText('synthetic failure for e2e');
  await expect(row.getByRole('button', { name: /Download export/ })).toBeDisabled();

  await row.getByRole('button', { name: `Delete export ${id}` }).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText(`Delete export #${id}?`);
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: `Delete export ${id}` }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete export' }).click();
  await expect(row).toHaveCount(0);
  const a = await apiAs('admin');
  expect((await a.get(`/api/exports/${id}`)).status()).toBe(404);
  await a.dispose();
});

test('downloads need authentication; exports are private to their creator', async ({ page }) => {
  const a = await apiAs('admin');
  const list = await (await a.get('/api/exports')).json();
  const adminId = (await (await a.get('/api/me')).json()).user.id;
  const done = list.items.find((e: { status: string; created_by: number }) => e.status === 'done' && e.created_by === adminId);
  expect(done).toBeTruthy();
  const anon = await pwRequest.newContext({ baseURL: 'http://localhost:8103' });
  expect((await anon.get(done.download_url)).status()).toBe(401);
  await anon.dispose();
  const op = await apiAs('operator');
  expect((await op.get(done.download_url)).status()).toBe(403); // admin's export, operator is not the creator
  await op.dispose();
  await a.dispose();
  await loginAs(page, 'operator');
  await page.goto('/exports');
  await expect(page.getByTestId('exports-table')).toBeVisible();
  await expect(page.getByTestId('exports-table')).not.toContainText('User #');
});

test('viewer cannot create exports; operator can', async ({ page }) => {
  await loginAs(page, 'viewer');
  await page.goto('/exports');
  await expect(page.getByRole('heading', { name: 'Exports', level: 1 })).toBeVisible();
  await expect(page.getByText('Your role is read-only')).toBeVisible();
  await expect(page.getByTestId('create-export')).toHaveCount(0);
  await expect(page.getByRole('form', { name: 'Export builder' })).toHaveCount(0);
  const v = await apiAs('viewer');
  expect((await v.post('/api/exports', { data: { preset: 'full_intelligence', format: 'csv', filters: {} } })).status()).toBe(403);
  expect((await v.post('/api/exports/preview', { data: { preset: 'full_intelligence', format: 'csv', filters: {} } })).status()).toBe(403);
  await v.dispose();
  await loginAs(page, 'operator');
  await page.goto('/exports');
  await expect(page.getByTestId('create-export')).toBeVisible();
});
