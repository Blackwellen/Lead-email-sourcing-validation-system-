import { test, expect, type Page } from '@playwright/test';
import { apiClient, expectNoHorizontalScroll, expectNoSeriousViolations, login } from './support/biz';

test.describe.configure({ mode: 'serial' });

const rows = (page: Page) => page.locator('table').filter({ has: page.locator('caption', { hasText: /^Contacts$/ }) }).locator('tbody tr[data-row]');

async function contactId(page: Page, q: string) {
  const api = await apiClient(page);
  return (await api.get(`/api/contacts?q=${encodeURIComponent(q)}&limit=1`)).items[0].id as number;
}

test.describe('contacts list', () => {
  test.beforeEach(async ({ page }) => { await login(page); });

  test('renders all evidence columns without a green “verified” icon for syntax validity', async ({ page }) => {
    await page.goto('/contacts');
    await expect(rows(page).first()).toBeVisible();
    await expect(page.getByText(/50 loaded of/)).toBeVisible();
    const table = page.getByRole('table', { name: 'Contacts' });
    for (const h of ['Email', 'Contact person', 'Business', 'Confidence', 'Mail routing', 'Reachability', 'Catch-all', 'Last checked']) await expect(table.getByRole('columnheader', { name: new RegExp(h) })).toBeVisible();
    await expect(rows(page).first().getByRole('meter')).toBeVisible();
    // syntax-only evidence is never shown with a success (green) style and never says "verified"
    await expect(page.getByText(/verified/i)).toHaveCount(0);
    const syntaxBadge = page.getByText('Syntax OK').first();
    if (await syntaxBadge.count()) expect(await syntaxBadge.evaluate((el) => (el.closest('span[class*="success"]') ? 'success' : 'neutral'))).toBe('neutral');
  });

  test('advanced filters, chips and saved views', async ({ page }) => {
    await page.goto('/contacts');
    await page.getByRole('combobox', { name: 'Reachability' }).click();
    await page.getByRole('option', { name: 'Risky' }).click();
    await expect(page).toHaveURL(/reachability=risky/);
    await expect(rows(page).first()).toBeVisible();
    for (let i = 0; i < Math.min(await rows(page).count(), 8); i++) await expect(rows(page).nth(i)).toContainText('Risky');
    await page.getByText('Advanced filters').click();
    await page.getByLabel('Min confidence (0-100)').fill('60');
    await page.getByLabel('Min confidence (0-100)').press('Enter');
    await expect(page).toHaveURL(/min_confidence=60/);
    await expect(page.getByRole('list', { name: 'Active filters' })).toContainText('Min confidence: 60');
    await page.getByRole('button', { name: 'Views' }).click();
    await page.getByRole('menuitem', { name: 'Save current filters…' }).click();
    await page.getByLabel('View name').fill('Risky 60+');
    await page.getByRole('button', { name: 'Save view' }).click();
    await page.getByRole('button', { name: 'Clear all' }).click();
    await expect(page).not.toHaveURL(/reachability/);
    await page.getByRole('button', { name: 'Views' }).click();
    await page.getByRole('menuitem', { name: /Risky 60\+/ }).click();
    await expect(page).toHaveURL(/reachability=risky/);
    await expect(page).toHaveURL(/min_confidence=60/);
  });

  test('cursor load-more past 200 rows', async ({ page }) => {
    await page.goto('/contacts');
    await page.getByRole('combobox', { name: 'Rows per load' }).click();
    await page.getByRole('option', { name: '100' }).click();
    await expect(page.getByText(/100 loaded of/)).toBeVisible();
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(page.getByText(/200 loaded/)).toBeVisible();
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(page.locator('tbody tr[data-row]').nth(200)).toBeVisible();
  });

  test('suppress with a reason, then unsuppress', async ({ page }) => {
    const email = 'person140@synthetic-014.example.com';
    const id = await contactId(page, email);
    await page.goto(`/contacts?q=${email}`);
    await expect(rows(page)).toHaveCount(1);
    await rows(page).first().getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Suppress…' }).click();
    const dlg = page.getByRole('dialog', { name: /Suppress/ });
    await dlg.getByRole('combobox', { name: 'Reason' }).click();
    await page.getByRole('option', { name: 'Opt-out request' }).click();
    await dlg.getByLabel('Note').fill('Replied to ask us to stop.');
    await dlg.getByRole('button', { name: 'Suppress' }).click();
    await expect(page.getByText(/Suppressed \(email\); 1 address affected/)).toBeVisible();
    await expect(rows(page).first()).toContainText('Suppressed');
    const api = await apiClient(page);
    const d = await api.get(`/api/contacts/${id}`);
    expect(d.marketing.suppressed).toBe(true);
    expect(d.suppressions[0].reason).toBe('opt_out');

    await rows(page).first().getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Unsuppress (address)' }).click();
    await expect(page.getByText(/Suppression removed/)).toBeVisible();
    await expect(rows(page).first()).not.toContainText('Suppressed');
  });

  test('revalidate enqueues a verify job; needs-review toggles', async ({ page }) => {
    const api = await apiClient(page);
    const before = (await api.get('/api/verification/overview')).verify_queue.queued ?? 0;
    await page.goto('/contacts?q=person120@synthetic-012.example.com');
    await expect(rows(page)).toHaveCount(1);
    await rows(page).first().getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Re-validate' }).click();
    await expect(page.getByText(/Re-validation queued/)).toBeVisible();
    const after = (await api.get('/api/verification/overview')).verify_queue.queued ?? 0;
    expect(after).toBe(before + 1);
    await rows(page).first().getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Mark needs review' }).click();
    await expect(rows(page).first()).toContainText('Review');
  });

  test('bulk export of a permitted selection creates an export without suppressed rows', async ({ page }) => {
    const api = await apiClient(page);
    const before = (await api.get('/api/exports?limit=1')).items[0]?.id ?? 0;
    await page.goto('/contacts?q=person1');
    await expect(rows(page).nth(1)).toBeVisible();
    await rows(page).nth(0).getByRole('checkbox').check();
    await rows(page).nth(1).getByRole('checkbox').check();
    await page.getByRole('region', { name: 'Bulk actions' }).getByRole('button', { name: 'Export permitted' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Create export' }).click();
    await expect(page.getByText(/Export #\d+ created \(suppressed addresses excluded\)/)).toBeVisible();
    const ex = (await api.get('/api/exports?limit=1')).items[0];
    expect(ex.id).toBeGreaterThan(before);
    expect(ex.filters.exclude_suppressed).toBe(true);
  });

  test('accessibility and 375px layout', async ({ page }) => {
    await page.goto('/contacts');
    await expect(rows(page).first()).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.getByText('Advanced filters').click();
    await expectNoSeriousViolations(page);
    await page.getByTestId('theme-toggle').first().click();
    await expectNoSeriousViolations(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/contacts');
    await expect(rows(page).first()).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});

test.describe('contact detail', () => {
  let id: number;
  test.beforeEach(async ({ page }) => { await login(page); id = await contactId(page, 'jane.synthetic@acme-synthetic.example.com'); });

  test('shows evidence, five statuses, explanation, breakdown and honest SMTP state', async ({ page }) => {
    await page.goto(`/contacts/${id}`);
    await expect(page.getByRole('heading', { level: 1 })).toContainText('jane.synthetic@acme-synthetic.example.com');
    await expect(page.getByText('Published', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Corroborated').first()).toBeVisible();
    // disclaimer
    await expect(page.getByRole('note').first()).toContainText('“Safe” is not a delivery guarantee');
    // five independent statuses side by side
    const five = page.getByRole('region').filter({ hasText: 'Five independent statuses' }).first();
    await expect(page.getByText('Five independent statuses')).toBeVisible();
    for (const s of ['Syntax OK', 'Accepts mail', 'Not tested', 'Risky', 'Needs review']) await expect(page.getByRole('list').filter({ hasText: 'Policy triage, independent of validity' }).getByText(s, { exact: true })).toBeVisible();
    void five;
    // explanation sentences
    const why = page.getByTestId('classification-explanation');
    await expect(why).toContainText('published and corroborated');
    await expect(why).toContainText('named individual: Jane Synthetic');
    await expect(why).toContainText('catch-all');
    // score breakdown with caps
    await expect(page.getByRole('list', { name: 'Score components' })).toContainText('Source Quality');
    await expect(page.getByRole('progressbar', { name: /Domain Mail 15 of 15 points/ })).toBeVisible();
    await expect(page.getByText('Catch All Suspected (score limited to 70)')).toBeVisible();
    await expect(page.getByText('Catch All Penalty')).toBeVisible();
    // per-stage evidence; SMTP explicitly not tested
    const smtp = page.getByTestId('stage-V07_smtp');
    await expect(smtp).toContainText('Not tested');
    await expect(smtp).toContainText('No SMTP connection was made');
    await expect(page.getByTestId('stage-V03_mx')).toContainText('Accepts mail');
    // catch-all card
    await expect(page.getByText(/looks like a catch-all from passive evidence/)).toBeVisible();
    // sources: http(s) link with noopener, non-http as plain text
    const sources = page.getByTestId('sources-list');
    const link = sources.getByRole('link', { name: 'https://acme-synthetic.example.com/contact' });
    await expect(link).toHaveAttribute('rel', /noopener/);
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(sources.getByText('javascript:alert(1)')).toBeVisible();
    await expect(sources.getByRole('link', { name: 'javascript:alert(1)' })).toHaveCount(0);
    // delivery evidence + freshness
    await expect(page.getByText('Historical delivery events (imported feedback)')).toBeVisible();
    await expect(page.getByText(/Delivered: 1/)).toBeVisible();
    await expect(page.getByText('Fresh', { exact: true }).or(page.getByText('Stale', { exact: true })).first()).toBeVisible();
  });

  test('an inferred address with no validation shows explicit “not tested / not recorded” states', async ({ page }) => {
    const inferred = await contactId(page, 'info@acme-synthetic.example.com');
    await page.goto(`/contacts/${inferred}`);
    await expect(page.getByText('Inferred', { exact: true }).first()).toBeVisible();
    await expect(page.getByTestId('classification-explanation')).toContainText('never been validated');
    await expect(page.getByTestId('stage-V07_smtp')).toContainText('No SMTP diagnostic has been recorded');
    await expect(page.getByTestId('stage-V01_syntax')).toContainText('Not recorded');
    await expect(page.getByText(/nothing publishes it/)).toBeVisible();
  });

  test('suppress and unsuppress from the detail page; revalidate; needs review', async ({ page }) => {
    await page.goto(`/contacts/${id}`);
    await page.getByRole('button', { name: 'Re-validate' }).click();
    await expect(page.getByText(/Re-validation queued|already queued/)).toBeVisible();
    await page.getByRole('button', { name: 'Mark needs review' }).click();
    await expect(page.getByText('Flagged: needs review')).toBeVisible();
    await page.getByRole('button', { name: 'Clear needs review' }).click();
    await expect(page.getByText('Flagged: needs review')).toHaveCount(0);

    await page.getByRole('button', { name: 'Suppress…' }).click();
    const dlg = page.getByRole('dialog', { name: /Suppress/ });
    await dlg.getByRole('button', { name: 'Suppress' }).click();
    await expect(page.getByText('Suppressed: do not use for marketing')).toBeVisible();
    await expect(page.getByLabel('Suppression entries')).toContainText('Email');
    await page.getByRole('button', { name: 'Unsuppress' }).first().click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Unsuppress' }).click();
    await expect(page.getByText('Suppressed: do not use for marketing')).toHaveCount(0);
  });

  test('admin delete needs the address typed and keeps a suppression record', async ({ page }) => {
    const target = await contactId(page, 'person130@synthetic-013.example.com');
    await page.goto(`/contacts/${target}`);
    await page.getByRole('button', { name: 'Delete…' }).click();
    const dlg = page.getByRole('dialog', { name: 'Permanently delete this address?' });
    const go = dlg.getByRole('button', { name: 'Delete permanently' });
    await expect(go).toBeDisabled();
    await dlg.getByLabel(/Type .* to confirm/).fill('person130@synthetic-013.example.com');
    await expect(go).toBeEnabled();
    await go.click();
    await expect(page).toHaveURL(/\/contacts$/);
    const api = await apiClient(page);
    expect((await api.raw.get(`/api/contacts/${target}`)).status()).toBe(404);
    const sup = await api.get('/api/suppressions?q=person130@synthetic-013.example.com');
    expect(sup.items.length).toBe(1);
  });

  test('operators cannot delete; viewers have no action buttons', async ({ page }) => {
    await login(page, 'operator');
    await page.goto(`/contacts/${id}`);
    await expect(page.getByRole('button', { name: 'Re-validate' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete…' })).toHaveCount(0);
    await login(page, 'viewer');
    await page.goto(`/contacts/${id}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Re-validate' })).toHaveCount(0);
  });

  test('accessibility and 375px layout', async ({ page }) => {
    await page.goto(`/contacts/${id}`);
    await expect(page.getByTestId('classification-explanation')).toBeVisible();
    await expectNoSeriousViolations(page);
    await page.getByTestId('theme-toggle').first().click();
    await expectNoSeriousViolations(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto(`/contacts/${id}`);
    await expect(page.getByTestId('classification-explanation')).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});
