import { test, expect } from '@playwright/test';
import { apiAs, login, requireBackend, trackPageErrors } from './support/disc';

/** DASH-015 Source Explorer against the real backend (lead_fe_d). Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();

const SECRET = 'sk-e2e-DO-NOT-ECHO-7f3a9';

test.beforeAll(async ({ browser }) => {
  // reset brave_search to "not configured, disabled"
  const page = await browser.newPage({ baseURL: process.env.E2E_BASE_URL || 'http://localhost:3104' });
  const api = await apiAs(page, 'admin');
  await api.post('/api/source-explorer/brave_search/disable');
  await api.post('/api/source-explorer/brave_search/configure', { config: {}, remove_keys: ['api_key', 'base_url'] });
  await page.close();
});

test('search, filters and result details', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'viewer');
  await page.goto('/sources/explore');
  await expect(page.getByTestId('explorer-results')).toBeVisible();
  const total = await page.getByTestId('explorer-count').textContent();
  expect(total).toMatch(/30 shown of \d+/);

  await page.getByRole('searchbox', { name: 'Search sources' }).fill('companies house');
  await expect(page).toHaveURL(/q=companies/);
  await expect(page.getByTestId('explorer-card-companies_house_bulk')).toBeVisible();
  const card = page.getByTestId('explorer-card-companies_house_bulk');
  for (const t of ['Open Government Licence v3.0', 'Free', 'No credentials', 'Bulk file', 'Automated access permitted']) await expect(card.getByText(t, { exact: false }).first()).toBeVisible();

  // country + business type filters drive the transparent score breakdown
  await page.goto('/sources/explore?country=GB&business_type=software');
  const ch = page.getByTestId('explorer-card-companies_house_bulk');
  await expect(ch.getByTestId('score-button')).toHaveText(/Score 50/);
  await ch.getByTestId('score-button').click();
  const bd = page.getByTestId('score-breakdown');
  await expect(bd.getByRole('row', { name: /Covers the chosen country \+30/ })).toBeVisible();
  await expect(bd.getByRole('row', { name: /Automated access permitted \+10/ })).toBeVisible();
  await expect(bd.getByRole('row', { name: /All industries \+5/ })).toBeVisible();
  await expect(bd.getByRole('row', { name: /Already configured \+5/ })).toBeVisible();
  await expect(bd.getByRole('row', { name: /Total 50/ })).toBeVisible();
  await page.keyboard.press('Escape');

  // access-method filter: restricted sources carry the explicit permission status
  await page.goto('/sources/explore?access_method=restricted&cost=paid');
  await expect(page.getByTestId('explorer-results')).toBeVisible();
  const statuses = page.getByTestId('automation-status');
  expect(await statuses.count()).toBeGreaterThan(3);
  for (const s of await statuses.allTextContents()) expect(s).toContain('Restricted — manual import or written permission only');
  // restricted entries show their "Paid" cost
  await expect(page.getByTestId('explorer-results').getByText('Paid').first()).toBeVisible();

  // licence filter + no-results state
  await page.goto('/sources/explore?licence=zzz-no-such-licence');
  await expect(page.getByText('No sources match')).toBeVisible();
  done();
});

test('detail drawer shows configuration requirements; viewers are read-only', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/sources/explore?q=brave');
  await page.getByRole('button', { name: /Details and configuration for Brave/ }).click();
  const drawer = page.getByTestId('explorer-drawer');
  await expect(drawer.getByTestId('config-requirements')).toContainText('api_key');
  await expect(drawer.getByTestId('config-requirements')).toContainText('Needs configuration before use');
  await expect(drawer.getByTestId('viewer-readonly')).toBeVisible();
  await expect(drawer.getByTestId('enable-source')).toHaveCount(0);
  await expect(drawer.getByTestId('configure-form')).toHaveCount(0);
  await expect(page).toHaveURL(/open=brave_search/);
  // server enforces it too
  const api = await apiAs(page, 'viewer');
  expect((await api.raw.post('/api/source-explorer/brave_search/enable')).status()).toBe(403);

  // a restricted catalogue entry explains why it cannot be enabled
  await page.goto('/sources/explore?open=yell');
  await expect(page.getByTestId('restricted-note')).toBeVisible();
  await expect(page.getByTestId('permission-status')).toHaveText('Not automatable');
});

test('admin configures a secret (write-only, never echoed), enables and disables', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'admin');
  await page.goto('/sources/explore?open=brave_search');
  const drawer = page.getByTestId('explorer-drawer');
  await expect(drawer.getByTestId('enable-source')).toBeDisabled();
  await expect(drawer.getByText('Configure this source (credentials / dataset) before enabling it.')).toBeVisible();

  const secret = drawer.getByTestId('cfg-input-api_key');
  await expect(secret).toHaveAttribute('type', 'password');
  await expect(secret).toHaveValue('');
  await secret.fill(SECRET);
  await drawer.getByRole('button', { name: 'Save configuration' }).click();
  await expect(drawer.getByTestId('configure-saved')).toContainText('api_key');
  await expect(secret).toHaveValue('');
  await expect(drawer.getByText('A value is stored (hidden). Type a new value to replace it.')).toBeVisible();
  await expect(drawer.getByTestId('config-requirements')).toContainText('api_key');
  expect(await page.content()).not.toContain(SECRET);

  // the API returns only a mask
  const detail = await (await page.request.get('/api/source-explorer/brave_search')).json();
  expect(detail.config.api_key).toBe('********');
  expect(JSON.stringify(detail)).not.toContain(SECRET);

  // reload: still not shown anywhere
  await page.reload();
  await expect(drawer.getByTestId('cfg-input-api_key')).toHaveValue('');
  expect(await page.content()).not.toContain(SECRET);

  // enable, then disable with confirmation
  await drawer.getByTestId('enable-source').click();
  await expect(drawer.getByText('Enabled', { exact: true }).first()).toBeVisible();
  await drawer.getByTestId('disable-source').click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Disable' }).click();
  await expect(drawer.getByTestId('enable-source')).toBeVisible();
  const after = await (await page.request.get('/api/source-explorer/brave_search')).json();
  expect(after.state.enabled).toBe(false);

  // the audit trail recorded the configure without the secret
  const admin = await apiAs(page, 'admin');
  const audit = await admin.get('/api/audit?action=source_explorer.*&limit=10');
  const conf = audit.items.find((a: { action: string }) => a.action === 'source_explorer.configure');
  expect(conf).toBeTruthy();
  expect(JSON.stringify(audit)).not.toContain(SECRET);
  done();
});

test('invalid configuration key is rejected with a message', async ({ page }) => {
  await login(page, 'admin');
  await page.goto('/sources/explore?open=brave_search');
  const drawer = page.getByTestId('explorer-drawer');
  await drawer.getByRole('button', { name: 'Add a setting' }).click();
  await drawer.getByLabel('Setting name').fill('Bad-Key');
  await drawer.getByLabel('Value').fill('x');
  await drawer.getByRole('button', { name: 'Save configuration' }).click();
  await expect(drawer.getByTestId('configure-error')).toContainText('not a valid setting name');
});

test('operator: no enable/configure (admin-only per API), but can import a dataset', async ({ page }) => {
  await login(page, 'operator');
  await page.goto('/sources/explore?open=brave_search');
  const drawer = page.getByTestId('explorer-drawer');
  await expect(drawer.getByTestId('admin-only-note')).toBeVisible();
  await expect(drawer.getByTestId('configure-form')).toHaveCount(0);
  await expect(drawer.getByTestId('enable-source')).toHaveCount(0);
  const api = await apiAs(page, 'operator');
  expect((await api.raw.post('/api/source-explorer/brave_search/configure', { config: { api_key: 'x' } })).status()).toBe(403);
  expect((await api.raw.post('/api/source-explorer/brave_search/enable')).status()).toBe(403);

  await page.goto('/sources/explore?open=abn_lookup_au');
  const form = drawer.getByTestId('import-form');
  await expect(form).toBeVisible();
  await form.getByLabel('Dataset file').setInputFiles({ name: 'abn-sample.csv', mimeType: 'text/csv', buffer: Buffer.from('abn,name\n12345678901,Example Pty Ltd\n') });
  await form.getByRole('button', { name: 'Upload dataset' }).click();
  await expect(form.getByTestId('import-result')).toContainText('Imported CSV');
  // a disguised executable is rejected by the server and the reason is shown
  await form.getByLabel('Dataset file').setInputFiles({ name: 'evil.csv', mimeType: 'text/csv', buffer: Buffer.from('MZ\x90\x00binary') });
  await form.getByRole('button', { name: 'Upload dataset' }).click();
  await expect(form.getByTestId('import-error')).toContainText('executable');
});
