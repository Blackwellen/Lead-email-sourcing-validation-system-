import { test, expect } from '@playwright/test';
import { apiAs, login, orgId, requireBackend, reseed, trackPageErrors } from './support/disc';

/** DASH-016 Discovery Intelligence against the real backend (lead_fe_d). Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();
test.beforeAll(() => { reseed(); });

test('overview: totals, opportunities split, industries with "not enough data"', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'viewer');
  await page.goto('/discovery');
  const opp = page.getByTestId('missing-contact-opportunities');
  await expect(opp.getByTestId('opp-incomplete_research')).toContainText('Incomplete research');
  await expect(opp.getByTestId('opp-genuinely_unavailable')).toContainText('Genuinely unavailable');
  // counts come from the seeded completeness rows (3 + 3), never estimated
  await expect(opp.getByTestId('opp-incomplete_research')).toContainText('3');
  await expect(opp.getByTestId('opp-genuinely_unavailable')).toContainText('3');
  await expect(opp.getByTestId('opp-incomplete_research').getByRole('link').first()).toHaveAttribute('href', /\/businesses\/\d+/);
  // industries below the minimum sample show the explicit placeholder, the one above it a rate
  const ind = page.getByTestId('industries-table');
  await expect(ind.getByRole('row', { name: /Software/ })).toContainText('%');
  expect(await ind.getByTestId('not-enough-data').count()).toBeGreaterThan(0);
  await expect(ind.getByTestId('not-enough-data').first()).toHaveText(/—\s+not enough data/);
  await expect(page.getByTestId('restricted-sources')).toContainText('Restricted');
  // tri-state counts table uses distinct labels
  await expect(page.getByRole('table', { name: 'Businesses per completeness step and state' })).toContainText('Not researched yet');
  await expect(page.getByRole('table', { name: 'Businesses per completeness step and state' })).toContainText('No – researched');
  done();
});

test('source effectiveness: measured values vs "not enough data" (never invented)', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/discovery?tab=effectiveness');
  const t = page.getByTestId('effectiveness-table');
  const big = t.getByTestId('eff-row-companies_house_bulk');
  const small = t.getByTestId('eff-row-osm_geofabrik');
  // 1200 unique / 5000 records = 240 per 1000; 480/1200 = 400; 6% duplicates; 2% failures; 70 contacts/hour
  for (const v of ['240', '400', '216.7', '116.7', '50', '4.5', '410', '6%', '2%', '70']) await expect(big).toContainText(v);
  await expect(big.getByTestId('not-enough-data')).toHaveCount(0);
  // 4 records / 1 job: every ratio is below the minimum sample -> all ten cells say so
  await expect(small.getByTestId('not-enough-data')).toHaveCount(10);
  await expect(small).not.toContainText('NaN');

  await page.getByRole('tab', { name: 'Scheduler' }).click();
  await expect(page).toHaveURL(/tab=scheduler/);
  await expect(page.getByTestId('scheduler-table').getByRole('row').nth(1)).toBeVisible();
});

test('monitors: create, disable/enable, run now, delete (admin)', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'operator');
  await page.goto('/discovery?tab=monitors');
  await page.getByTestId('new-monitor').click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('What to watch').click();
  await page.getByRole('option', { name: 'Website changes' }).click();
  await dlg.getByRole('button', { name: 'Create monitor' }).click();
  await expect(dlg.getByText('Give the monitor a name.')).toBeVisible();
  await dlg.getByLabel('Name').fill('Disc e2e website monitor');
  await dlg.getByLabel('Re-check sites older than (days)').fill('999');
  await dlg.getByRole('button', { name: 'Create monitor' }).click();
  await expect(dlg.getByText('Enter a whole number between 1 and 365.')).toBeVisible();
  await dlg.getByLabel('Re-check sites older than (days)').fill('30');
  await dlg.getByLabel('Frequency').click();
  await page.getByRole('option', { name: 'Daily' }).click();
  await dlg.getByRole('button', { name: 'Create monitor' }).click();
  await expect(dlg).toBeHidden();

  const card = page.getByTestId('monitors-list').getByRole('listitem').filter({ hasText: 'Disc e2e website monitor' });
  await expect(card).toContainText('Website changes');
  await expect(card).toContainText('Older Than Days: 30');
  await card.getByRole('switch', { name: /Enable monitor/ }).click();
  await expect(card.getByText('Disabled', { exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Run now' })).toBeDisabled();
  await card.getByRole('switch', { name: /Enable monitor/ }).click();
  await expect(card.getByText('Enabled', { exact: true }).first()).toBeVisible();
  await card.getByRole('button', { name: 'Run now' }).click();
  await expect(page.getByText(/Queued \(job #\d+\)|Already queued/)).toBeVisible();
  // operators cannot delete
  await expect(card.getByRole('button', { name: /Delete monitor/ })).toHaveCount(0);

  await login(page, 'admin');
  await page.reload();
  const card2 = page.getByTestId('monitors-list').getByRole('listitem').filter({ hasText: 'Disc e2e website monitor' });
  await card2.getByRole('button', { name: /Delete monitor/ }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click();
  await expect(card2).toHaveCount(0);
  const api = await apiAs(page, 'admin');
  const audit = await api.get('/api/audit?action=discovery.monitor_*&limit=20');
  const actions = audit.items.map((a: { action: string }) => a.action);
  for (const a of ['discovery.monitor_create', 'discovery.monitor_disable', 'discovery.monitor_enable', 'discovery.monitor_run_now', 'discovery.monitor_delete']) expect(actions).toContain(a);
  done();
});

test('similar-business finder explains results and expands them into a run', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'operator');
  const hub = await orgId(page, 'Disc Hub Holdings Ltd');
  await page.goto('/discovery?tab=similar');
  await page.getByRole('button', { name: 'Find similar businesses' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Choose a seed business.' })).toBeVisible();
  await page.getByRole('combobox', { name: 'Seed business' }).click();
  await page.getByPlaceholder('Type a company name or domain…').fill('Disc Hub');
  await page.getByRole('option', { name: /Disc Hub Holdings Ltd/ }).click();
  await page.getByRole('button', { name: 'Find similar businesses' }).click();
  const results = page.getByTestId('similar-results');
  await expect(results).toBeVisible();
  const items = results.getByTestId('similar-item');
  expect(await items.count()).toBeGreaterThan(5);
  await expect(items.first()).toContainText('Disc Software Lookalike');
  await items.first().locator('summary').click();
  await expect(items.first()).toContainText('Industry code (SIC)');
  await expect(items.first()).toContainText('Accreditations: not used');
  await expect(items.first().locator('summary')).toContainText(/= sic=/);

  await expect(page.getByTestId('expand-into-run')).toBeDisabled();
  await page.getByRole('combobox', { name: 'Target run' }).click();
  await page.getByRole('option', { name: /Disc e2e target run/ }).click();
  await page.getByTestId('expand-into-run').click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Queue into run' }).click();
  await expect(page.getByTestId('expand-result')).toContainText(/Queued \d+ business/);
  const api = await apiAs(page, 'admin');
  const audit = await api.get('/api/audit?action=discovery.similar_expand&limit=5');
  expect(audit.items[0].detail.seed.org_id).toBe(hub);
  done();
});

test('viewer cannot use the finder or create monitors', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/discovery?tab=similar');
  await expect(page.getByText('Operators only')).toBeVisible();
  await page.getByRole('tab', { name: 'Monitors' }).click();
  await expect(page.getByTestId('monitors-list')).toBeVisible();
  await expect(page.getByTestId('new-monitor')).toHaveCount(0);
  await expect(page.getByRole('switch')).toHaveCount(0);
});
