import { test, expect } from '@playwright/test';
import { login, requireBackend, trackPageErrors } from './support/disc';

/** DASH-018 Opportunity Signals against the real backend (lead_fe_d; 40 seeded signals). Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();

const rowsOf = (page: import('@playwright/test').Page) => page.getByRole('table', { name: 'Opportunity signals' }).locator('tbody tr[data-row]');

test('facets, filters and keyset paging', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'viewer');
  await page.goto('/opportunities');
  const facets = page.getByTestId('signal-facets');
  await expect(facets).toContainText('40 signals');
  await expect(facets.getByRole('button', { name: /Hiring 10/ })).toBeVisible();
  await expect(page.getByText('40 loaded of 40')).toBeVisible();

  // facet button = type filter (aria-pressed), counts come from the API
  await facets.getByRole('button', { name: /Hiring 10/ }).click();
  await expect(facets.getByRole('button', { name: /Hiring 10/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(page).toHaveURL(/signal_type=hiring/);
  await expect(page.getByText('10 loaded of 10')).toBeVisible();
  for (const t of await rowsOf(page).locator('td:nth-child(2)').allTextContents()) expect(t).toBe('Hiring');

  // country facet
  await facets.getByRole('button', { name: /^DE \d+/ }).click();
  await expect(page).toHaveURL(/country=DE/);
  const n = await rowsOf(page).count();
  expect(n).toBeGreaterThan(0);
  for (const t of await rowsOf(page).locator('td:first-child').allTextContents()) expect(t).toContain('· DE');

  // min confidence + text search
  await page.goto('/opportunities?min_confidence=0.9');
  for (const v of await rowsOf(page).getByRole('meter').evaluateAll((els) => els.map((e) => Number(e.getAttribute('aria-valuenow'))))) expect(v).toBeGreaterThanOrEqual(90);
  await page.goto('/opportunities');
  await page.getByRole('searchbox', { name: 'Search signals' }).fill('signal 1');
  await expect(page).toHaveURL(/q=signal/);
  await expect(rowsOf(page)).toHaveCount(10); // "Disc signal 10".."19" (titles are zero-padded)

  // observation date range: the last 5 days (signals are observed one per day)
  const day = (d: number) => new Date(Date.now() - d * 86400_000).toISOString().slice(0, 10);
  await page.goto(`/opportunities?from=${day(4)}&to=${day(0)}`);
  await expect(page.getByText(/^5 loaded of 5$/)).toBeVisible();
  // inverted range is explained, not sent
  await page.goto(`/opportunities?from=${day(0)}&to=${day(4)}`);
  await expect(page.getByRole('alert').filter({ hasText: 'after the “to” date' })).toBeVisible();

  // paging with a smaller page size
  await page.goto('/opportunities');
  await page.getByRole('combobox', { name: 'Rows per load' }).click();
  await page.getByRole('option', { name: '25' }).click();
  await expect(page.getByText('25 loaded of 40')).toBeVisible();
  await page.getByRole('button', { name: 'Load more' }).click();
  await expect(page.getByText('40 loaded of 40')).toBeVisible(); // total persists after "Load more"
  done();
});

test('each signal shows source URL (safe link), observed / occurred dates, confidence and the business', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/opportunities?q=signal 04');
  const row = rowsOf(page).first();
  await expect(row).toContainText('Disc signal 04');
  const link = row.getByTestId('signal-source').getByRole('link');
  await expect(link).toHaveAttribute('href', 'https://news.example.com/disc/4');
  await expect(link).toHaveAttribute('rel', /noopener/);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(row.locator('time')).toHaveCount(2); // observed + occurred
  await expect(row.getByRole('meter')).toBeVisible();
  await expect(row.getByRole('link', { name: /Disc Partner/ })).toHaveAttribute('href', /\/businesses\/\d+$/);

  // signal without occurred_at says so
  await page.goto('/opportunities?q=signal 00');
  await expect(rowsOf(page).first()).toContainText('Not stated');

  // hostile javascript: URL is rendered as inert text
  await page.goto('/opportunities?q=hostile');
  const hostile = rowsOf(page).first().getByTestId('signal-source');
  await expect(hostile).toContainText('javascript:alert');
  await expect(hostile.getByRole('link')).toHaveCount(0);

  // row opens the business (keyboard)
  await page.goto('/opportunities?q=signal 04');
  await rowsOf(page).first().focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/businesses\/\d+$/);
});

test('sort by confidence (server-side)', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/opportunities');
  await page.getByRole('button', { name: /Confidence/ }).click();
  await expect(page.getByRole('columnheader', { name: /Confidence/ })).toHaveAttribute('aria-sort', 'ascending');
  const vals = await rowsOf(page).getByRole('meter').evaluateAll((els) => els.map((e) => Number(e.getAttribute('aria-valuenow'))));
  expect(vals).toEqual([...vals].sort((a, b) => a - b));
});
