import { test, expect } from '@playwright/test';
import { apiAs, loginAs, resetSystemState, isBackendUp, requireBackend } from './support/ops';

/** DASH-011 /sources against the real backend (see e2e/support/ops-backend.sh). Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();

const CSV_OK = 'name,country,website,city\nZeta Ltd,GB,https://zeta.example.com,Leeds\nEta GmbH,DE,eta.example.org,Berlin\n=cmd|calc,GB,,\n';

test.afterAll(async () => {
  if (!isBackendUp()) return;
  const a = await apiAs('admin');
  await a.post('/api/sources/csv_import/disable');
  await a.put('/api/sources/csv_import/config', { data: { config: {} } });
  await a.dispose();
  await resetSystemState();
});

test('table shows adapters; manual-import-only sources are clearly marked and cannot be enabled', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/sources');
  await expect(page.getByRole('heading', { name: 'Sources', level: 1 })).toBeVisible();
  const table = page.getByTestId('sources-table');
  await expect(table).toBeVisible();
  for (const h of ['Source and licence', 'Access', 'Businesses', 'Emails', 'Enabled']) await expect(table.getByRole('columnheader', { name: h })).toBeVisible();

  // a catalogue-only registry that forbids automated collection
  await page.getByRole('searchbox', { name: 'Search sources' }).fill('brreg');
  const row = page.getByTestId('source-row-dir_brreg_no');
  await expect(row).toBeVisible();
  await expect(row.getByText('Manual import only', { exact: true })).toBeVisible();
  await expect(row.getByText('Automated collection not permitted')).toBeVisible();
  await expect(row.getByRole('switch')).toBeDisabled();

  await row.getByRole('button', { name: /Details for/ }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByTestId('manual-only-note')).toContainText('Automated collection not permitted — manual import only');
  await expect(drawer.getByText('Licence, attribution and terms')).toBeVisible();
  await expect(drawer.getByText('Open data under NLOD', { exact: false }).first()).toBeVisible();
  await expect(drawer.getByRole('switch', { name: /Enable/ })).toBeDisabled();

  // server refuses too
  const a = await apiAs('admin');
  const res = await a.post('/api/sources/dir_brreg_no/enable');
  expect(res.status()).toBe(409);
  await a.dispose();
});

test('admin enables and disables a source; state is verified through the API', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/sources');
  await page.getByRole('searchbox', { name: 'Search sources' }).fill('csv_import');
  const toggle = page.getByTestId('toggle-csv_import');
  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  const a = await apiAs('admin');
  expect((await (await a.get('/api/sources/csv_import')).json()).enabled).toBe(true);
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  expect((await (await a.get('/api/sources/csv_import')).json()).enabled).toBe(false);
  // the audit log records both changes
  const audit = await (await a.get('/api/audit', { params: { action: 'source.*', limit: 5 } })).json();
  expect(audit.items.map((i: { action: string }) => i.action)).toEqual(expect.arrayContaining(['source.enable', 'source.disable']));
  await a.dispose();
});

test('config JSON editor validates permitted keys, then saves; server path errors are shown', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/sources');
  await page.getByRole('searchbox', { name: 'Search sources' }).fill('csv_import');
  await page.getByRole('button', { name: /Details for Imported business/ }).click();
  const drawer = page.getByRole('dialog');
  const editor = drawer.getByLabel('Configuration (JSON)');
  const save = drawer.getByRole('button', { name: 'Save configuration' });
  await expect(drawer.getByText('Permitted keys:')).toContainText('path');

  await editor.fill('{ not json');
  await expect(drawer.getByText(/Not valid JSON/)).toBeVisible();
  await expect(save).toBeDisabled();

  await editor.fill('[1,2]');
  await expect(drawer.getByText('must be a JSON object')).toBeVisible();

  await editor.fill('{"bogus_key": 1}');
  await expect(drawer.getByText(/not a permitted setting/)).toBeVisible();
  await expect(save).toBeDisabled();

  await editor.fill('{"path": "/etc/passwd"}');
  await expect(save).toBeEnabled();
  await save.click();
  await expect(drawer.getByTestId('config-server-error')).toContainText('uploaded through the Sources page');

  await editor.fill('{"country_code": "GB"}');
  await save.click();
  await expect(page.getByText('Configuration saved')).toBeVisible();
  const a = await apiAs('admin');
  expect((await (await a.get('/api/sources/csv_import')).json()).config).toEqual({ country_code: 'GB' });
  await a.dispose();
});

test('priority save and health refresh', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/sources');
  await page.getByRole('searchbox', { name: 'Search sources' }).fill('csv_import');
  await page.getByRole('button', { name: /Details for Imported business/ }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByText('Administrator controls')).toHaveCount(0); // operator: no admin controls
  await drawer.getByRole('button', { name: 'Refresh health' }).click();
  await expect(drawer.getByText(/checked /)).toBeVisible();
  await expect(drawer.getByText(/never checked/)).toHaveCount(0);

  await page.keyboard.press('Escape');
  const admin = await apiAs('admin');
  const before = (await (await admin.get('/api/sources/csv_import')).json()).priority;
  await loginAs(page, 'admin');
  await page.goto('/sources');
  await page.getByRole('searchbox', { name: 'Search sources' }).fill('csv_import');
  await page.getByRole('button', { name: /Details for Imported business/ }).click();
  const d2 = page.getByRole('dialog');
  await d2.getByLabel('Priority').fill('-5');
  await d2.getByRole('button', { name: 'Save priority' }).click();
  await expect(d2.getByText('whole number between 0 and 10000')).toBeVisible();
  await d2.getByLabel('Priority').fill('77');
  await d2.getByRole('button', { name: 'Save priority' }).click();
  await expect(page.getByText('Priority saved')).toBeVisible();
  expect((await (await admin.get('/api/sources/csv_import')).json()).priority).toBe(77);
  await admin.put('/api/sources/csv_import/priority', { data: { priority: before } });
  await admin.dispose();
});

test('upload: a malicious file is rejected with the server message; a valid CSV shows the validation report', async ({ page }) => {
  await loginAs(page, 'operator');
  await page.goto('/sources');
  await page.getByRole('button', { name: 'Upload file' }).click();
  const dialog = page.getByRole('dialog');
  const input = dialog.getByTestId('source-file-input');

  // client-side extension check
  await input.setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
  await expect(dialog.getByText('Only .csv and .xlsx files can be uploaded')).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Upload and validate' })).toBeDisabled();

  // an executable disguised as CSV: the server sniffs the content
  await input.setInputFiles({ name: 'evil.csv', mimeType: 'text/csv', buffer: Buffer.concat([Buffer.from('MZ\x90\x00'), Buffer.from('binary payload')]) });
  await dialog.getByRole('button', { name: 'Upload and validate' }).click();
  await expect(dialog.getByTestId('upload-error')).toContainText('looks like something else');

  // HTML with script, also disguised
  await input.setInputFiles({ name: 'page.csv', mimeType: 'text/csv', buffer: Buffer.from('<!DOCTYPE html><script>alert(1)</script>') });
  await dialog.getByRole('button', { name: 'Upload and validate' }).click();
  await expect(dialog.getByTestId('upload-error')).toContainText('Only CSV, JSON or Excel');

  // a macro-style zip masquerading as xlsx
  await input.setInputFiles({ name: 'macro.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('PK\x03\x04not-really-a-zip') });
  await dialog.getByRole('button', { name: 'Upload and validate' }).click();
  await expect(dialog.getByTestId('upload-error')).toContainText('valid Excel');

  // valid file
  await input.setInputFiles({ name: 'synthetic.csv', mimeType: 'text/csv', buffer: Buffer.from(CSV_OK) });
  await dialog.getByRole('button', { name: 'Upload and validate' }).click();
  const result = dialog.getByTestId('upload-result');
  await expect(result).toContainText('File accepted');
  await expect(result).toContainText('Rows read');
  await expect(result).toContainText('Formula cells dropped');
  await expect(result.getByText('csv_import').first()).toBeVisible();
  // operators cannot apply it to the source configuration (admin only)
  await expect(dialog.getByRole('button', { name: /Use for/ })).toHaveCount(0);
});

test('admin can point the import source at an uploaded file', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/sources');
  await page.getByRole('button', { name: 'Upload file' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByTestId('source-file-input').setInputFiles({ name: 'synthetic.csv', mimeType: 'text/csv', buffer: Buffer.from(CSV_OK) });
  await dialog.getByRole('button', { name: 'Upload and validate' }).click();
  await dialog.getByRole('button', { name: /Use for/ }).click();
  await expect(page.getByText(/now points at the uploaded file/)).toBeVisible();
  const a = await apiAs('admin');
  const cfg = (await (await a.get('/api/sources/csv_import')).json()).config;
  expect(cfg.path).toMatch(/uploads\/[0-9a-f]{32}\.csv$/);
  await a.dispose();
});

test('viewer is read-only: no upload, no switches', async ({ page }) => {
  await loginAs(page, 'viewer');
  await page.goto('/sources');
  await expect(page.getByTestId('sources-table')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Upload file' })).toHaveCount(0);
  await expect(page.getByRole('switch')).toHaveCount(0);
  const a = await apiAs('viewer');
  expect((await a.post('/api/sources/csv_import/enable')).status()).toBe(403);
  expect((await a.post('/api/sources/upload', { multipart: { file: { name: 'x.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n') } } })).status()).toBe(403);
  await a.dispose();
});
