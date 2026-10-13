import { test, expect } from '@playwright/test';
import { apiClient, expectNoHorizontalScroll, expectNoSeriousViolations, findBusiness, login } from './support/biz';

test.describe.configure({ mode: 'serial' });

test.describe('business detail', () => {
  let id: number;
  test.beforeEach(async ({ page }) => {
    await login(page);
    id = (await findBusiness(page, 'Acme Synthetic Plumbing Ltd'))[0].id;
  });

  test('all tabs render real data', async ({ page }) => {
    await page.goto(`/businesses/${id}`);
    await expect(page.getByRole('heading', { name: 'Acme Synthetic Plumbing Ltd', level: 1 })).toBeVisible();
    await expect(page.getByRole('tab')).toHaveText([/Overview/, /Contact information/, /Domains \/ websites/, /Contacts/, /Emails/, /Sources \/ evidence/, /Verification history/, /Linked runs/]);
    await expect(page.getByText('E2EACME01')).toBeVisible();

    await page.getByRole('tab', { name: /Contact information/ }).click();
    await expect(page.getByText('EX1 0AA')).toBeVisible();

    await page.getByRole('tab', { name: /^Contacts/ }).click();
    await expect(page.getByText('Jane Synthetic')).toBeVisible();
    await expect(page.getByText('Managing Director')).toBeVisible();

    await page.getByRole('tab', { name: /^Emails/ }).click();
    const table = page.getByRole('table', { name: 'E-mail addresses of this business' });
    await expect(table.getByRole('link', { name: 'jane.synthetic@acme-synthetic.example.com' })).toBeVisible();
    await expect(table.getByText('Role speculative')).toBeVisible();

    await page.getByRole('tab', { name: /Sources/ }).click();
    const list = page.getByRole('list', { name: 'Evidence sources' });
    await expect(list.getByRole('link', { name: 'https://acme-synthetic.example.com/contact' })).toHaveAttribute('rel', /noopener/);
    // non-http(s) source is shown as inert text, never as a link
    await expect(list.getByText('javascript:alert(1)')).toBeVisible();
    await expect(list.getByRole('link', { name: 'javascript:alert(1)' })).toHaveCount(0);

    await page.getByRole('tab', { name: /Verification history/ }).click();
    await expect(page.getByRole('table', { name: /Verification history/ })).toContainText('V07_smtp');

    await page.getByRole('tab', { name: /Linked runs/ }).click();
    await expect(page.getByRole('link', { name: 'E2E synthetic run' })).toBeVisible();
  });

  test('manual domain correction requires a reason, is recorded as evidence and audited', async ({ page }) => {
    const api = await apiClient(page);
    await page.goto(`/businesses/${id}`);
    await page.getByRole('tab', { name: /Domains/ }).click();
    const card = page.getByTestId('domain-acme-synthetic.example.com');
    await expect(card).toContainText('Needs review');
    await expect(card).toContainText('Name Match');
    await card.getByRole('button', { name: 'Confirm' }).click();
    const dlg = page.getByRole('dialog', { name: 'Correct website match' });
    await dlg.getByRole('button', { name: 'Save decision' }).click();
    await expect(dlg.getByRole('alert')).toContainText('Explain the reason');
    await dlg.getByLabel('Reason').fill('Verified on the registry: same address and director as the website footer.');
    await dlg.getByRole('button', { name: 'Save decision' }).click();
    await expect(page.getByText('Website decision saved and recorded in the audit log')).toBeVisible();
    await expect(card).toContainText('Confirmed');
    await expect(card).toContainText('Manually decided');
    const manual = card.getByTestId('manual-evidence');
    await expect(manual).toContainText('Manual decision: Confirm');
    await expect(manual).toContainText('Reason: Verified on the registry');
    await expect(manual.locator('time')).toBeVisible();

    const audit = await api.get('/api/audit?action=business.domain_override&limit=5');
    const hit = audit.items.find((a: { object_id: string; detail: { decision: string } }) => a.object_id === String(id) && a.detail.decision === 'confirm');
    expect(hit).toBeTruthy();
    expect(hit.detail.note).toContain('Verified on the registry');
  });

  test('set a different domain, then reject', async ({ page }) => {
    await page.goto(`/businesses/${id}`);
    await page.getByRole('tab', { name: /Domains/ }).click();
    await page.getByRole('button', { name: 'Set a different domain…' }).click();
    const dlg = page.getByRole('dialog', { name: 'Correct website match' });
    await dlg.getByRole('textbox', { name: 'Domain', exact: true }).fill('acme-synthetic-new.example.com');
    await dlg.getByLabel('Reason').fill('Company rebranded; new site confirmed by phone.');
    await dlg.getByRole('button', { name: 'Save decision' }).click();
    await expect(page.getByTestId('domain-acme-synthetic-new.example.com')).toContainText('Confirmed');
    await page.getByTestId('domain-acme-synthetic-new.example.com').getByRole('button', { name: 'Reject' }).click();
    await page.getByRole('dialog').getByLabel('Reason').fill('Typo, wrong domain entered.');
    await page.getByRole('button', { name: 'Save decision' }).click();
    await expect(page.getByTestId('domain-acme-synthetic-new.example.com')).toContainText('Mismatch');
  });

  test('viewers cannot correct domains', async ({ page }) => {
    await login(page, 'viewer');
    await page.goto(`/businesses/${id}`);
    await page.getByRole('tab', { name: /Domains/ }).click();
    await expect(page.getByRole('button', { name: 'Confirm' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Set a different domain…' })).toHaveCount(0);
  });

  test('404 for an unknown business', async ({ page }) => {
    await page.goto('/businesses/99999999');
    await expect(page.getByRole('heading', { name: 'Business not found' })).toBeVisible();
  });

  test('accessibility (every tab) and 375px layout', async ({ page }) => {
    await page.goto(`/businesses/${id}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    for (const t of ['Overview', 'Contact information', 'Domains', '^Contacts', '^Emails', 'Sources', 'Verification history', 'Linked runs']) {
      await page.getByRole('tab', { name: new RegExp(t) }).click();
      await expectNoSeriousViolations(page);
    }
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto(`/businesses/${id}`);
    await page.getByRole('tab', { name: /^Emails/ }).click();
    await expectNoHorizontalScroll(page);
  });
});
