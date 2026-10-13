import { test, expect, type Page } from '@playwright/test';
import { apiAs, login, orgId, requireBackend, reseed, trackPageErrors } from './support/disc';

/** DASH-019 Data Quality workbench + completeness tri-state, against the real backend (lead_fe_d). Run with --workers=1. */
// Not serial: tests run in file order with --workers=1, and one backend failure must not skip the others.
requireBackend();
test.beforeAll(() => { reseed(); });

const table = (page: Page, label: RegExp) => page.getByRole('table', { name: label });
const tabCount = (page: Page, id: string) => page.getByTestId(`dq-tab-${id}`);

async function runAction(page: Page, action: string, reason: string | null) {
  await page.getByTestId(`dq-action-${action}`).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toBeVisible();
  if (reason !== null) await dlg.getByTestId('dq-reason').fill(reason);
  await dlg.getByRole('button').last().click();
  return dlg;
}

test('summary counts and read-only viewer', async ({ page }) => {
  await login(page, 'viewer');
  await page.goto('/data-quality');
  // every tab count equals the API summary (other suites' fixtures may share this DB, so compare rather than hard-code)
  const sum = await (await page.request.get('/api/data-quality/summary')).json();
  for (const it of sum.issues as { issue_type: string; count: number }[]) {
    await expect(tabCount(page, it.issue_type)).toHaveText(new RegExp(`${it.count.toLocaleString('en-US')}$`));
  }
  const n = (k: string) => (sum.issues as { issue_type: string; count: number }[]).find((i) => i.issue_type === k)!.count;
  expect(n('duplicate_companies')).toBeGreaterThanOrEqual(2);
  expect(n('unconfirmed_domains')).toBeGreaterThanOrEqual(3);
  await expect(page.getByTestId('dq-summary')).toContainText('open issues');
  await expect(table(page, /Duplicate companies issues/).getByRole('row').filter({ hasText: /Disc (Duplicate Alpha|Twin Beta) Ltd/ })).toHaveCount(2);
  await expect(page.getByText('Read-only: review actions need the Operator role.')).toBeVisible();
  await expect(page.getByRole('checkbox', { name: 'Select all loaded rows' })).toHaveCount(0);
  const api = await apiAs(page, 'viewer');
  expect((await api.raw.post('/api/data-quality/actions', { action: 'mark_reviewed', ids: [1], reason: 'nope' })).status()).toBe(403);
});

test('operator: confirm duplicate needs a reason, shows a result summary and audit note; merge is admin-only', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'operator');
  await page.goto('/data-quality');
  const t = table(page, /Duplicate companies issues/);
  const row = t.getByRole('row').filter({ hasText: 'Disc Duplicate Alpha Ltd' });
  await row.getByRole('checkbox').check();
  await expect(page.getByRole('region', { name: 'Bulk actions' })).toContainText('1 selected');
  await expect(page.getByTestId('dq-action-merge')).toHaveCount(0);
  await expect(page.getByText('Merging needs the Admin role.')).toBeVisible();

  const dlg = await runAction(page, 'confirm_duplicate', null);
  await expect(dlg.getByTestId('dq-reason-error')).toHaveText('Enter a reason of at least 3 characters.');
  await dlg.getByTestId('dq-reason').fill('Same company number on the register (e2e)');
  await dlg.getByRole('button', { name: 'Confirm duplicate' }).click();
  await expect(dlg).toBeHidden();
  const res = page.getByTestId('dq-result');
  await expect(res).toContainText('Confirm duplicate: 1 of 1 done');
  await expect(page.getByTestId('dq-audit-note')).toContainText('data_quality.confirm_duplicate');
  await expect(page.getByTestId('dq-audit-note')).toContainText('Same company number on the register (e2e)');
  await expect(row).toContainText('Confirmed duplicate');

  // the server refuses a merge from an operator, too
  const api = await apiAs(page, 'operator');
  const r = await api.raw.post('/api/data-quality/actions', { action: 'merge', ids: [1], reason: 'trying' });
  expect(r.status()).toBe(403);
  done();
});

test('admin merges the confirmed duplicate; audit entry with reason is listed', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'admin');
  await page.goto('/data-quality');
  const t = table(page, /Duplicate companies issues/);
  await t.getByRole('row').filter({ hasText: 'Disc Duplicate Alpha Ltd' }).getByRole('checkbox').check();
  const dlg = await runAction(page, 'merge', 'Confirmed duplicate, merging (e2e)');
  await expect(dlg).toBeHidden();
  await expect(page.getByTestId('dq-result')).toContainText('Merge: 1 of 1 done');
  await expect(t.getByRole('row').filter({ hasText: 'Disc Duplicate Alpha Ltd' })).toHaveCount(0);
  await expect(page.getByTestId('dq-audit-list')).toContainText('data_quality.merge');
  await expect(page.getByTestId('dq-audit-list')).toContainText('Confirmed duplicate, merging (e2e)');
  const api = await apiAs(page, 'admin');
  const audit = await api.get('/api/audit?action=data_quality.merge&limit=1');
  expect(audit.items[0].detail.reason).toBe('Confirmed duplicate, merging (e2e)');
  expect(audit.items[0].detail.before).toBeTruthy();
  done();
});

test('batch domain decisions, re-validation/suppression and recrawl map to the right ids', async ({ page }) => {
  await login(page, 'operator');
  // all three unconfirmed domains at once
  await page.goto('/data-quality?tab=unconfirmed_domains');
  const t = table(page, /Unconfirmed domains issues/);
  const disc = t.getByRole('row').filter({ hasText: /Disc Unconfirmed Domain \d Ltd/ });
  await expect(disc).toHaveCount(3);
  const before = Number((await tabCount(page, 'unconfirmed_domains').textContent())!.replace(/\D/g, ''));
  for (let i = 0; i < 3; i++) await disc.nth(i).getByRole('checkbox').check();
  await expect(page.getByRole('region', { name: 'Bulk actions' })).toContainText('3 selected');
  await runAction(page, 'confirm_domain', 'Checked the imprint pages (e2e)');
  await expect(page.getByTestId('dq-result')).toContainText('Confirm domain: 3 of 3 done');
  await expect(disc).toHaveCount(0);
  await expect(tabCount(page, 'unconfirmed_domains')).toHaveText(new RegExp(`${(before - 3).toLocaleString('en-US')}$`));

  // stale e-mails: suppress one (destructive confirm)
  await page.getByRole('tab', { name: /Stale e-mails/ }).click();
  const s = table(page, /Stale e-mails issues/);
  const stale = s.getByRole('row').filter({ hasText: 'Disc Stale Mail Ltd' });
  await expect(stale).toHaveCount(3);
  await stale.first().getByRole('checkbox').check();
  await page.getByTestId('dq-action-suppress').click();
  const dlg = page.getByRole('alertdialog');
  await dlg.getByTestId('dq-reason').fill('Mailbox closed (e2e)');
  await dlg.getByRole('button', { name: 'Suppress' }).click();
  await expect(page.getByTestId('dq-result')).toContainText('Suppress: 1 of 1 done');
  await expect(stale).toHaveCount(2);

  // conflicting evidence row is keyed by a synthetic id; recrawl must use its organisation id
  await page.getByRole('tab', { name: /Conflicting evidence/ }).click();
  const c = table(page, /Conflicting evidence issues/);
  await c.getByRole('row').filter({ hasText: 'Disc Two Websites Ltd' }).getByRole('checkbox').check();
  await runAction(page, 'queue_recrawl', 'Two websites recorded (e2e)');
  await expect(page.getByTestId('dq-result')).toContainText(/Queue re-crawl: 1 of 1 done|Queue re-crawl: 0 of 1 done, 1 skipped/);
  const api = await apiAs(page, 'admin');
  const conflict = await orgId(page, 'Disc Two Websites Ltd');
  const audit = await api.get('/api/audit?action=data_quality.queue_recrawl&limit=1');
  expect(audit.items[0].object_id).toBe(String(conflict));
});

test('missing websites: research classification is tri-state, never "no" for unresearched', async ({ page }) => {
  await login(page, 'operator');
  await page.goto('/data-quality?tab=missing_websites&classification=incomplete_research');
  const t = table(page, /Missing websites issues/);
  await expect(t.getByRole('row').nth(1)).toBeVisible();
  const states = await t.getByTestId('tri-state').evaluateAll((els) => els.map((e) => e.getAttribute('data-state')));
  expect(new Set(states)).toEqual(new Set(['unknown']));
  await expect(t.locator('[data-testid=tri-state][data-state="unknown"]').first()).toHaveText('Not researched yet');
  await expect(t.locator('[data-testid=tri-state][data-state="unknown"]').first()).not.toHaveClass(/warning|danger/);
  // flags sheet from a row
  await t.getByRole('row').nth(1).getByRole('button', { name: /Research completeness of/ }).click();
  await expect(page.getByRole('dialog').getByTestId('completeness-panel')).toBeVisible();
});

test('business detail: completeness panel shows the four distinct states', async ({ page }) => {
  const done = trackPageErrors(page);
  await login(page, 'viewer');
  const probe = await orgId(page, 'Disc Completeness Probe Ltd');
  await page.goto(`/businesses/${probe}`);
  await expect(page.getByTestId('completeness-summary')).toContainText('31');
  await page.getByTestId('completeness-tab').click();
  const panel = page.getByTestId('completeness-panel');
  const flag = (k: string) => panel.getByTestId(`flag-${k}`).getByTestId('tri-state');
  await expect(flag('official_website')).toHaveAttribute('data-state', 'yes');
  await expect(flag('official_website')).toHaveText('Yes');
  await expect(flag('contact_page_inspected')).toHaveAttribute('data-state', 'no');
  await expect(flag('contact_page_inspected')).toHaveText('No – researched');
  await expect(flag('email_infrastructure_checked')).toHaveAttribute('data-state', 'in_progress');
  await expect(flag('email_infrastructure_checked')).toHaveText('In progress');
  await expect(flag('recently_checked')).toHaveAttribute('data-state', 'unknown');
  await expect(flag('recently_checked')).toHaveText('Not researched yet');
  // "unknown" must not share the "no" styling
  const cls = async (k: string) => (await flag(k).getAttribute('class')) ?? '';
  expect(await cls('recently_checked')).toContain('neutral');
  expect(await cls('contact_page_inspected')).toContain('warning');
  expect(await cls('recently_checked')).not.toContain('warning');
  await expect(panel).toContainText('Genuinely unavailable');
  await expect(panel).toContainText('never checked');
  done();
});
