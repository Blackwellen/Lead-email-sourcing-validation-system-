import { test, expect, type Page } from '@playwright/test';
import { apiClient, expectNoHorizontalScroll, expectNoSeriousViolations, login } from './support/biz';

test.describe.configure({ mode: 'serial' });
const stamp = Date.now().toString(36);
const NAME = `E2E Category ${stamp}`;
const NAME2 = `E2E Merge Target ${stamp}`;

async function openMenu(page: Page, name: string) {
  await page.getByRole('button', { name: `Actions for ${name}`, exact: true }).click();
}

test.describe('categories', () => {
  test.beforeEach(async ({ page }) => { await login(page); });

  test('tree table shows counts and rolls up; rows link to the directory', async ({ page }) => {
    await page.goto('/categories');
    const table = page.getByRole('table', { name: 'Business categories' });
    await expect(table).toBeVisible();
    for (const h of ['Category', 'Businesses', 'Emails', 'Published', 'Eligible', 'Countries', 'Last run']) await expect(table.getByRole('columnheader', { name: h })).toBeVisible();
    await expect(table.locator('tbody tr').first()).toBeVisible();
    // collapse/expand a parent
    const toggles = table.getByRole('button', { name: /^Collapse / });
    const before = await table.locator('tbody tr').count();
    await toggles.first().click();
    expect(await table.locator('tbody tr').count()).toBeLessThan(before);
    await table.getByRole('button', { name: /^Expand / }).first().click();
    expect(await table.locator('tbody tr').count()).toBe(before);
    await page.getByRole('searchbox', { name: 'Filter categories' }).fill('plumb');
    await expect(table.getByRole('link', { name: /Plumb/i }).first()).toBeVisible();
  });

  test('create, edit, synonyms, merge, disable/enable and delete', async ({ page }) => {
    const api = await apiClient(page);
    await page.goto('/categories');
    // create
    await page.getByRole('button', { name: 'New category' }).click();
    let dlg = page.getByRole('dialog', { name: 'New category' });
    await dlg.getByRole('button', { name: 'Create category' }).click();
    await expect(dlg.getByText('Enter a name.')).toBeVisible();
    await dlg.getByLabel('Name').fill(NAME);
    const kw = dlg.getByRole('combobox', { name: 'Keywords', exact: true });
    await kw.click();
    await page.getByRole('combobox', { name: 'Search options' }).fill('e2e-keyword');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Escape');
    await dlg.getByRole('button', { name: 'Create category' }).click();
    await expect(page.getByText(`Category “${NAME}” created`)).toBeVisible();
    const row = page.getByRole('row', { name: new RegExp(NAME) });
    await expect(row).toBeVisible();
    let created = (await api.get('/api/categories')).find((c: { name: string }) => c.name === NAME);
    expect(created.keywords).toContain('e2e-keyword');

    // edit
    await openMenu(page, NAME);
    await page.getByRole('menuitem', { name: 'Edit…' }).click();
    dlg = page.getByRole('dialog', { name: new RegExp(`Edit “${NAME}”`) });
    await dlg.getByLabel('Name').fill(`${NAME} (edited)`);
    await dlg.getByRole('button', { name: 'Save changes' }).click();
    await expect(page.getByText(`Category “${NAME} (edited)” updated`)).toBeVisible();
    await expect(page.getByRole('link', { name: `${NAME} (edited)` })).toBeVisible();

    // synonyms
    await openMenu(page, `${NAME} (edited)`);
    await page.getByRole('menuitem', { name: 'Edit synonyms…' }).click();
    dlg = page.getByRole('dialog', { name: /Synonyms for/ });
    await dlg.getByRole('combobox', { name: 'Synonyms', exact: true }).click();
    await page.getByRole('combobox', { name: 'Search options' }).fill('e2e-synonym');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Escape');
    await dlg.getByRole('button', { name: 'Save synonyms' }).click();
    await expect(page.getByText('Synonyms saved')).toBeVisible();
    created = (await api.get('/api/categories')).find((c: { name: string }) => c.name.startsWith(NAME));
    expect(created.synonyms).toContain('e2e-synonym');

    // second category to merge into
    await page.getByRole('button', { name: 'New category' }).click();
    await page.getByRole('dialog', { name: 'New category' }).getByLabel('Name').fill(NAME2);
    await page.getByRole('dialog', { name: 'New category' }).getByRole('button', { name: 'Create category' }).click();
    await expect(page.getByText(`Category “${NAME2}” created`)).toBeVisible();

    // disable / enable
    await openMenu(page, `${NAME} (edited)`);
    await page.getByRole('menuitem', { name: 'Disable…' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Disable' }).click();
    await expect(page.getByRole('row', { name: new RegExp(`${NAME} \\(edited\\)`) })).toContainText('Disabled');
    await openMenu(page, `${NAME} (edited)`);
    await page.getByRole('menuitem', { name: 'Enable' }).click();
    await expect(page.getByRole('row', { name: new RegExp(`${NAME} \\(edited\\)`) })).not.toContainText('Disabled');

    // merge (admin) into the second category: synonyms carry over
    await openMenu(page, `${NAME} (edited)`);
    await page.getByRole('menuitem', { name: 'Merge into…' }).click();
    dlg = page.getByRole('dialog', { name: /Merge “/ });
    await dlg.getByRole('combobox', { name: 'Merge into' }).click();
    await page.getByRole('option', { name: NAME2 }).click();
    await dlg.getByRole('button', { name: 'Merge category' }).click();
    await expect(page.getByText(/Merged: 0 businesses and 0 sub-categories moved/)).toBeVisible();
    const target = (await api.get('/api/categories')).find((c: { name: string }) => c.name === NAME2);
    expect(target.synonyms).toEqual(expect.arrayContaining(['e2e-synonym']));
    expect(target.keywords).toContain('e2e-keyword');

    // delete (empty category) needs confirmation
    await openMenu(page, NAME2);
    await page.getByRole('menuitem', { name: 'Delete…' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Delete category' }).click();
    await expect(page.getByText('Category deleted')).toBeVisible();
    await expect(page.getByRole('link', { name: NAME2 })).toHaveCount(0);
  });

  test('deleting a category that still has businesses is refused by the server', async ({ page }) => {
    const api = await apiClient(page);
    const flat = (await api.get('/api/categories')).flatMap(function f(c: { children: unknown[]; business_count: number; name: string }): unknown[] { return [c, ...(c.children as typeof c[]).flatMap(f)]; }) as { name: string; business_count: number }[];
    const busy = flat.find((c) => c.business_count > 0)!;
    await page.goto('/categories');
    await page.getByRole('searchbox', { name: 'Filter categories' }).fill(busy.name);
    await openMenu(page, busy.name);
    await page.getByRole('menuitem', { name: 'Delete…' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Delete category' }).click();
    await expect(page.getByText(/This category still has businesses or sub-categories/)).toBeVisible();
  });

  test('taxonomy import previews with a dry run, then imports; export downloads definitions', async ({ page }) => {
    const slug = `e2e-imp-${stamp}`;
    const json = JSON.stringify({ categories: [{ slug, name: `E2E Imported ${stamp}`, level: 'category', enabled: true, keywords: ['x'], synonyms: [], excluded_keywords: [], sic_codes: [], osm_tags: [], default_sources: [] }] });
    await page.goto('/categories');
    await page.getByRole('button', { name: 'Import taxonomy' }).click();
    const dlg = page.getByRole('dialog', { name: 'Import taxonomy' });
    await expect(dlg.getByRole('button', { name: 'Import', exact: true })).toBeDisabled();
    await dlg.locator('input[type=file]').setInputFiles({ name: 'tax.json', mimeType: 'application/json', buffer: Buffer.from(json) });
    await dlg.getByRole('button', { name: 'Preview (dry run)' }).click();
    await expect(dlg.getByTestId('import-preview')).toContainText('1 to create');
    const api = await apiClient(page);
    expect((await api.get('/api/categories')).some((c: { slug: string }) => c.slug === slug)).toBe(false);
    await dlg.getByRole('button', { name: 'Import', exact: true }).click();
    await expect(dlg.getByTestId('import-preview')).toContainText('Imported');
    expect((await api.get('/api/categories')).some((c: { slug: string }) => c.slug === slug)).toBe(true);
    // invalid file shows server problems
    await dlg.locator('input[type=file]').setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{"categories":[{"slug":"Bad Slug"}]}') });
    await dlg.getByRole('button', { name: 'Preview (dry run)' }).click();
    await expect(dlg.getByRole('alert')).toContainText(/name|slug/);
    await page.keyboard.press('Escape');
    // export definitions
    const [download] = await Promise.all([page.waitForEvent('download'), (async () => {
      await page.getByRole('button', { name: /Export definitions/ }).click();
      await page.getByRole('menuitem', { name: 'JSON' }).click();
    })()]);
    expect(download.suggestedFilename()).toBe('taxonomy.json');
  });

  test('category directory lists its contacts with the shared filters', async ({ page }) => {
    const api = await apiClient(page);
    const flat = (await api.get('/api/categories')).flatMap(function f(c: { children: unknown[]; email_count: number; id: number; name: string }): unknown[] { return [c, ...(c.children as typeof c[]).flatMap(f)]; }) as { id: number; name: string; email_count: number }[];
    const withEmails = flat.find((c) => c.email_count > 0)!;
    await page.goto(`/categories/${withEmails.id}`);
    await expect(page.getByRole('heading', { name: withEmails.name, level: 1 })).toBeVisible();
    await expect(page.getByText('Default source strategies')).toBeVisible();
    const table = page.getByRole('table', { name: `Contacts in ${withEmails.name}` });
    await expect(table.locator('tbody tr[data-row]').first()).toBeVisible();
    await page.getByRole('combobox', { name: 'Reachability' }).click();
    await page.getByRole('option', { name: 'Risky' }).click();
    await expect(page).toHaveURL(/reachability=risky/);
    await page.goto('/categories/99999999');
    await expect(page.getByRole('heading', { name: 'Category not found' })).toBeVisible();
  });

  test('viewers cannot create or manage categories', async ({ page }) => {
    await login(page, 'viewer');
    await page.goto('/categories');
    await expect(page.getByRole('table', { name: 'Business categories' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'New category' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Import taxonomy' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Actions for/ })).toHaveCount(0);
  });

  test('accessibility and 375px layout', async ({ page }) => {
    const api = await apiClient(page);
    const first = (await api.get('/api/categories'))[0];
    await page.goto('/categories');
    await expect(page.getByRole('table', { name: 'Business categories' })).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.getByRole('button', { name: 'New category' }).click();
    await expectNoSeriousViolations(page);
    await page.keyboard.press('Escape');
    await page.goto(`/categories/${first.id}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/categories');
    await expect(page.getByRole('table', { name: 'Business categories' })).toBeVisible();
    await expectNoHorizontalScroll(page);
    await page.goto(`/categories/${first.id}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});
