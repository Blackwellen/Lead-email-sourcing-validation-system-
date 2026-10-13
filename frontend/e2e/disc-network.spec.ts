import { test, expect } from '@playwright/test';
import { apiAs, login, orgId, requireBackend, trackPageErrors } from './support/disc';

/** DASH-017 Business Network against the real backend (lead_fe_d). Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();

test('choose a business, render the bounded graph, select a node, open the business', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'viewer');
  await page.goto('/discovery/network');
  await expect(page.getByRole('heading', { name: 'Choose a business' })).toBeVisible();
  // the table fallback is available even before choosing
  await expect(page.getByRole('table', { name: 'Business relationships' })).toBeVisible();

  await page.getByRole('combobox', { name: 'Business', exact: true }).click();
  await page.getByPlaceholder('Type a company name or domain…').fill('Disc Hub');
  await page.getByRole('option', { name: /Disc Hub Holdings Ltd/ }).click();
  const graph = page.getByTestId('network-graph');
  await expect(graph).toBeVisible();
  // depth 1: hub + 12 partners, 12 edges
  await expect(page.getByTestId('graph-summary')).toHaveText('13 businesses · 12 relationships · depth 1');
  await expect(graph.locator('[data-node-id]')).toHaveCount(13);
  await expect(graph.locator('line[data-relation]')).toHaveCount(12);
  // every relation type appears in the legend with a text label + code (not colour-only)
  const legend = page.getByTestId('network-legend');
  for (const t of ['Branch of (BR)', 'Subsidiary of (SU)', 'Supplier of (SP)', 'Member of group (MG)', 'Awarded by (AW)', 'Confirmed website (thick ring + dot)']) await expect(legend).toContainText(t);
  await expect(page.getByTestId('truncated-notice')).toHaveCount(0);

  // depth selector 2 adds the ring
  await page.getByRole('combobox', { name: 'Depth' }).click();
  await page.getByRole('option', { name: '2 hops' }).click();
  await expect(page.getByTestId('graph-summary')).toHaveText('19 businesses · 18 relationships · depth 2');

  // click a node -> side panel -> open business
  const partner = await orgId(page, 'Disc Partner 02 Ltd');
  await graph.locator(`[data-node-id="${partner}"]`).click();
  const panel = page.getByTestId('node-panel');
  await expect(panel.getByRole('heading', { name: 'Disc Partner 02 Ltd' })).toBeVisible();
  await expect(panel).toContainText('Subsidiary of');
  await expect(panel).toContainText('Supplier of');
  await expect(panel.getByRole('link', { name: 'source' }).first()).toHaveAttribute('href', /^https:\/\/disc-/);
  await panel.getByTestId('open-business').click();
  await expect(page).toHaveURL(new RegExp(`/businesses/${partner}$`));
  done();
});

test('keyboard: focus the graph, arrow to a node and select it with Enter', async ({ page }) => {
  await login(page, 'viewer');
  const hub = await orgId(page, 'Disc Hub Holdings Ltd');
  await page.goto(`/discovery/network?org=${hub}`);
  const graph = page.getByTestId('network-graph');
  await expect(graph).toBeVisible();
  await graph.focus();
  await expect(graph).toHaveAttribute('aria-activedescendant', `gnode-${hub}`);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('node-panel').getByRole('heading', { name: 'Disc Hub Holdings Ltd' })).toBeVisible();
  await page.keyboard.press('ArrowRight');
  const active = await graph.getAttribute('aria-activedescendant');
  expect(active).not.toBe(`gnode-${hub}`);
  await page.keyboard.press('Enter');
  const id = active!.replace('gnode-', '');
  const name = await page.locator(`#${active}`).getAttribute('aria-label');
  await expect(page.getByTestId('node-panel').getByRole('heading', { level: 3 })).toHaveText(name!.split(',')[0]);
  expect(Number(id)).toBeGreaterThan(0);
  await page.keyboard.press('End');
  await page.keyboard.press('Home');
  await expect(graph).toHaveAttribute('aria-activedescendant', `gnode-${hub}`);
});

test('large neighbourhood is capped at 300 nodes with a truncation notice', async ({ page }) => {
  await login(page, 'viewer');
  const mega = await orgId(page, 'Disc Mega Group Ltd');
  await page.goto(`/discovery/network?org=${mega}&limit=300`);
  await expect(page.getByTestId('truncated-notice')).toContainText('Truncated');
  await expect(page.getByTestId('graph-summary')).toContainText('300 businesses');
  await expect(page.getByTestId('network-graph').locator('[data-node-id]')).toHaveCount(300);
  // with 50 the notice reports the smaller cap
  await page.goto(`/discovery/network?org=${mega}&limit=50`);
  await expect(page.getByTestId('graph-summary')).toContainText('50 businesses');
  await expect(page.getByTestId('truncated-notice')).toBeVisible();
});

test('relationships table: filters, keyset paging, keyboard row open', async ({ page }) => {
  await login(page, 'viewer');
  const mega = await orgId(page, 'Disc Mega Group Ltd');
  await page.goto(`/discovery/network?org=${mega}`);
  const table = page.getByRole('table', { name: 'Business relationships' });
  await expect(table.getByRole('row')).toHaveCount(51); // header + first page of 50
  await expect(page.getByText(/50 loaded/)).toBeVisible();
  await page.getByRole('button', { name: 'Load more' }).click();
  await expect(table.getByRole('row')).toHaveCount(101);
  // relationship filter (no "supplier_of" around the mega group)
  await page.getByRole('combobox', { name: 'Relationship', exact: true }).click();
  await page.getByRole('option', { name: 'Supplier of' }).click();
  await expect(page.getByText('Nothing here yet')).toBeVisible();
  await page.getByRole('button', { name: 'Clear all' }).click();
  await expect(table.getByRole('row').nth(1)).toBeVisible();
  // min confidence 90+: seeded confidences are 0.50-0.99, so only part of them
  await page.getByRole('combobox', { name: 'Min confidence' }).click();
  await page.getByRole('option', { name: '90+' }).click();
  await expect(table.getByRole('row')).toHaveCount(51);
  // keyboard: first row, Enter opens the subject business
  await table.getByRole('row').nth(1).focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/businesses\/\d+$/);
});

test('expand network: operator only, bounded and confirmed', async ({ page }) => {
  await login(page, 'viewer');
  const hub = await orgId(page, 'Disc Hub Holdings Ltd');
  const ring = await orgId(page, 'Disc Ring 0 Ltd');
  await page.goto(`/discovery/network?org=${hub}`);
  await expect(page.getByTestId('network-graph')).toBeVisible();
  await expect(page.getByTestId('expand-network')).toHaveCount(0);

  await login(page, 'operator');
  await page.reload();
  await page.getByTestId('expand-network').click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('Page budget').fill('500');
  await dlg.getByRole('button', { name: 'Queue expansion' }).click();
  await expect(dlg.getByTestId('expand-error')).toContainText('between 1 and 200');
  await dlg.getByLabel('Page budget').fill('20');
  await dlg.getByLabel('Maximum depth').click();
  await page.getByRole('option', { name: '2 hops' }).click();
  await dlg.getByRole('button', { name: 'Queue expansion' }).click();
  await expect(page.getByText(/Network expansion queued \(job #\d+, depth 2, budget 20\)|identical expansion/)).toBeVisible();

  // a business without a confirmed website: the API refuses and the dialog says why
  await page.goto(`/discovery/network?org=${ring}`);
  await page.getByTestId('expand-network').click();
  await page.getByRole('dialog').getByRole('button', { name: 'Queue expansion' }).click();
  await expect(page.getByTestId('expand-error')).toContainText('confirmed official website');
  const api = await apiAs(page, 'admin');
  const audit = await api.get('/api/audit?action=network.expand&limit=5');
  expect(audit.items[0].detail).toMatchObject({ max_depth: 2, budget: 20 });
});
