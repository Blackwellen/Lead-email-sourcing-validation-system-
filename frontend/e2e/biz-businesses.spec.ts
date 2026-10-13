import { test, expect } from '@playwright/test';
import { apiClient, expectNoHorizontalScroll, expectNoSeriousViolations, login } from './support/biz';

/**
 * Real backend (DB lead_fe_b, synthetic example.com data). Run: e2e/support/biz-backend.sh seed && E2E_BASE_URL=http://localhost:3102 E2E_NO_SERVER=1 npx playwright test e2e/biz-*.spec.ts --workers=1
 */
test.describe.configure({ mode: 'serial' });

const rows = (page: import('@playwright/test').Page) => page.locator('table[aria-label], table').filter({ has: page.locator('caption', { hasText: 'Businesses' }) }).locator('tbody tr[data-row]');

test.describe('businesses list', () => {
  test.beforeEach(async ({ page }) => { await login(page); });

  test('keyset pagination loads past 200 rows with an honest total', async ({ page }) => {
    await page.goto('/businesses');
    await expect(page.getByRole('heading', { name: 'Businesses', level: 1 })).toBeVisible();
    await expect(page.getByText(/50 loaded of \d+/)).toBeVisible();
    await page.getByRole('combobox', { name: 'Rows per load' }).click();
    await page.getByRole('option', { name: '200' }).click();
    await expect(page.getByText(/200 loaded of/)).toBeVisible();
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(page.getByText('End of results')).toBeVisible();
    expect(await rows(page).count()).toBeGreaterThan(200);
  });

  test('search and filters narrow the list through the URL', async ({ page }) => {
    await page.goto('/businesses');
    await page.getByRole('searchbox', { name: 'Search businesses' }).fill('Acme Synthetic');
    await expect(page).toHaveURL(/q=Acme(\+|%20)Synthetic/);
    await expect(rows(page)).toHaveCount(2);
    await page.getByRole('button', { name: 'Remove filter' }).count();

    await page.goto('/businesses?domain_status=review');
    await expect(rows(page).first()).toBeVisible();
    const n = await rows(page).count();
    for (let i = 0; i < Math.min(n, 10); i++) await expect(rows(page).nth(i)).toContainText('Needs review');

    await page.goto('/businesses');
    await page.getByRole('combobox', { name: 'Country' }).click();
    await page.getByRole('option', { name: 'Ireland (IE)' }).click();
    await expect(page).toHaveURL(/country=IE/);
    await expect(page.getByRole('list', { name: 'Active filters' })).toContainText('Country: IE');
    const first = rows(page).first();
    await expect(first).toContainText('IE');
    // email availability + chips
    await page.getByRole('combobox', { name: 'Emails' }).click();
    await page.getByRole('option', { name: 'No email found' }).click();
    await expect(page).toHaveURL(/email_availability=none/);
    await page.getByRole('button', { name: 'Clear all' }).click();
    await expect(page).not.toHaveURL(/country=/);
  });

  test('saved views store and re-apply filters', async ({ page }) => {
    await page.goto('/businesses?country=DE&domain_status=confirmed');
    await expect(rows(page).first()).toBeVisible();
    await page.getByRole('button', { name: 'Views' }).click();
    await page.getByRole('menuitem', { name: 'Save current filters…' }).click();
    await page.getByLabel('View name').fill('German confirmed');
    await page.getByRole('button', { name: 'Save view' }).click();
    await page.goto('/businesses');
    await page.getByRole('button', { name: 'Views' }).click();
    await page.getByRole('menuitem', { name: /German confirmed/ }).click();
    await expect(page).toHaveURL(/country=DE/);
    await expect(page).toHaveURL(/domain_status=confirmed/);
  });

  test('bulk: add to category, queue refresh and export selection', async ({ page }) => {
    const api = await apiClient(page);
    const cats = await api.get('/api/categories');
    const target = cats.find((c: { children: unknown[]; enabled: boolean }) => c.enabled && c.children.length === 0) ?? cats[0];
    await page.goto('/businesses?q=Synthetic%20Business%200');
    await expect(rows(page).first()).toBeVisible();
    await rows(page).nth(0).getByRole('checkbox').check();
    await rows(page).nth(1).getByRole('checkbox').check();
    const bulk = page.getByRole('region', { name: 'Bulk actions' });
    await expect(bulk).toContainText('2 selected');

    await bulk.getByRole('button', { name: 'Add to category' }).click();
    await page.getByRole('combobox', { name: 'Category' }).last().click();
    await page.getByRole('option', { name: target.name, exact: false }).first().click();
    await page.getByRole('button', { name: 'Add to category' }).last().click();
    await expect(page.getByText(/2 businesses moved to the category/)).toBeVisible();

    await rows(page).nth(0).getByRole('checkbox').check();
    await rows(page).nth(1).getByRole('checkbox').check();
    await bulk.getByRole('button', { name: 'Queue refresh' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Queue refresh' }).click();
    await expect(page.getByText(/Refresh queued: \d+ jobs? for 2 businesses/)).toBeVisible();

    const before = (await api.get('/api/exports?limit=1')).items[0]?.id ?? 0;
    await rows(page).nth(0).getByRole('checkbox').check();
    await rows(page).nth(1).getByRole('checkbox').check();
    await bulk.getByRole('button', { name: 'Export selection' }).click();
    await page.getByRole('button', { name: 'Create export' }).click();
    await expect(page.getByText(/Export #\d+ created for 2 businesses/)).toBeVisible();
    const after = (await api.get('/api/exports?limit=1')).items[0];
    expect(after.id).toBeGreaterThan(before);
  });

  test('admin merge shows a preview, needs acknowledgement and merges via the API', async ({ page }) => {
    const api = await apiClient(page);
    await page.goto('/businesses?q=Acme%20Synthetic%20Plumbing');
    await expect(rows(page)).toHaveCount(2);
    await rows(page).nth(0).getByRole('checkbox').check();
    await rows(page).nth(1).getByRole('checkbox').check();
    await page.getByRole('region', { name: 'Bulk actions' }).getByRole('button', { name: 'Merge duplicates…' }).click();
    const dlg = page.getByRole('dialog', { name: 'Merge duplicate businesses' });
    await dlg.getByRole('radio', { name: 'Keep Acme Synthetic Plumbing Ltd' }).click();
    await expect(dlg.getByTestId('merge-preview')).toContainText('1 business will be marked as merged');
    const go = dlg.getByRole('button', { name: /Merge 1 into selected/ });
    await expect(go).toBeDisabled();
    await dlg.getByRole('checkbox', { name: /I understand/ }).check();
    await go.click();
    await expect(page.getByText(/Merged 1 into #\d+/)).toBeVisible();
    const limited = (await api.get('/api/businesses?q=Acme%20Synthetic%20Plumbing&include_merged=true')).items.find((b: { legal_name: string }) => b.legal_name.endsWith('Limited'));
    expect(limited.merged_into_id).not.toBeNull();
    await page.goto('/businesses?q=Acme%20Synthetic%20Plumbing');
    await expect(rows(page)).toHaveCount(1);
  });

  test('viewers cannot see mutating bulk actions', async ({ page }) => {
    await login(page, 'viewer');
    await page.goto('/businesses?q=Synthetic%20Business%200');
    await rows(page).nth(0).getByRole('checkbox').check();
    const bulk = page.getByRole('region', { name: 'Bulk actions' });
    await expect(bulk).toBeVisible();
    await expect(bulk.getByRole('button', { name: 'Add to category' })).toHaveCount(0);
    await expect(bulk.getByRole('button', { name: 'Merge duplicates…' })).toHaveCount(0);
  });

  test('XSS payload in a business name is rendered inertly', async ({ page }) => {
    await page.goto('/businesses?q=XSS');
    const cell = rows(page).first();
    await expect(cell).toContainText('<img src=x onerror="window.__xss=1">');
    await expect(cell).toContainText('<script>window.__xss=1</script>');
    expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
    expect(await page.locator('table img[src="x"], table script').count()).toBe(0);
    await cell.getByRole('link').first().click();
    await expect(page.getByRole('heading', { level: 1 })).toContainText('<script>window.__xss=1</script>');
    expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  });

  test('accessibility and 375px layout', async ({ page }) => {
    await page.goto('/businesses');
    await expect(rows(page).first()).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.getByTestId('theme-toggle').first().click();
    await expectNoSeriousViolations(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/businesses');
    await expect(rows(page).first()).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});
