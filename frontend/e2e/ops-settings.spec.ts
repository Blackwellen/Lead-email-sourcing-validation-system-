import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { API, apiAs, loginAs, resetSystemState, totp, USERS, isBackendUp, requireBackend } from './support/ops';

/** DASH-014 /settings (+ RBAC for the ops pages) against the real backend. Run with --workers=1. */
test.describe.configure({ mode: 'serial' });
requireBackend();

const stamp = Date.now();
const created: string[] = [];
async function makeUser(admin: APIRequestContext, role: 'operator' | 'viewer' | 'admin', tag: string) {
  const email = `${tag}-${stamp}@e2e-ops.example.com`;
  const password = 'Fr3sh-Passw0rd-xyz!Q';
  const res = await admin.post('/api/users', { data: { email, password, role } });
  expect(res.status(), await res.text()).toBe(201);
  created.push(email);
  return { email, password, id: (await res.json()).id as number };
}

test.afterAll(async () => {
  if (!isBackendUp()) return;
  const a = await apiAs('admin');
  await a.put('/api/settings/worker', { data: { concurrency: 8, poll_interval_s: 1, lease_seconds: 120, max_job_attempts: 4, max_rss_mb: null } });
  const v = await (await a.get('/api/settings/validation')).json();
  await a.put('/api/settings/validation', { data: { ...v, marketing_policy: {} } });
  const users = await (await a.get('/api/users', { params: { limit: 200 } })).json();
  for (const u of users.items) if (created.includes(u.email)) await a.patch(`/api/users/${u.id}`, { data: { is_active: false } });
  await a.dispose();
  await resetSystemState();
});

test('worker settings: change persists across reload, is audited, and the audit viewer shows it', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=worker');
  const field = page.getByLabel('Concurrent jobs per worker');
  await expect(field).toHaveValue('8');
  const save = page.getByRole('button', { name: 'Save changes' });
  await expect(save).toBeDisabled(); // pristine
  await field.fill('9');
  await expect(page.getByText('Unsaved changes')).toBeVisible();
  await save.click();
  await expect(page.getByText('Worker settings saved')).toBeVisible();
  await expect(page.getByText('Unsaved changes')).toHaveCount(0);

  await page.reload();
  await expect(page.getByLabel('Concurrent jobs per worker')).toHaveValue('9');
  const a = await apiAs('admin');
  expect((await (await a.get('/api/settings/worker')).json()).concurrency).toBe(9);
  const audit = await (await a.get('/api/audit', { params: { action: 'settings.update', object_id: 'worker', limit: 1 } })).json();
  expect(audit.items[0].detail.values.concurrency).toBe(9);
  expect(audit.items[0].user_email).toBe(USERS.admin.email);

  // audit viewer in the UI
  await page.goto('/settings?tab=audit');
  await page.getByLabel('Action').fill('settings.update');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  const table = page.getByTestId('audit-table');
  await expect(table.locator('tbody tr').first()).toContainText('settings.update');
  await expect(table.locator('tbody tr').first()).toContainText('settings · worker');
  await expect(table.locator('tbody tr').first()).toContainText(USERS.admin.email);
  await table.getByRole('button', { name: /Show details/ }).first().click();
  await expect(table).toContainText('"concurrency": 9');
  await page.getByLabel('User id').fill('abc');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page.getByText('User id must be a number')).toBeVisible();
  await page.getByRole('button', { name: 'Clear' }).click();
  await page.getByLabel('Action').fill('nothing.matches.this');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page.getByText('No audit entries match')).toBeVisible();

  await a.put('/api/settings/worker', { data: { concurrency: 8, poll_interval_s: 1, lease_seconds: 120, max_job_attempts: 4, max_rss_mb: null } });
  await a.dispose();
});

test('validation: client-side errors block saving; a server 422 is mapped onto the field', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=worker');
  const field = page.getByLabel('Concurrent jobs per worker');
  await field.fill('0');
  await expect(page.getByText('Concurrency must be at least 1')).toBeVisible();
  await page.getByRole('button', { name: 'Save changes' }).click(); // blocked by the form, nothing is sent
  await expect(page.getByText('Worker settings saved')).toHaveCount(0);
  await expect(page.getByText('Concurrency must be at least 1')).toBeVisible();
  await field.fill('');
  await expect(page.getByText('Concurrency must be a number')).toBeVisible();

  // let the REAL server reject a value the client would never send (tamper with the request body)
  await field.fill('12');
  await page.route('**/api/settings/worker', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    return route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), concurrency: 9999 }) });
  });
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText(/less than or equal to 512/)).toBeVisible();
  await page.unroute('**/api/settings/worker');
  const a = await apiAs('admin');
  expect((await (await a.get('/api/settings/worker')).json()).concurrency).toBe(8); // nothing saved
  await a.dispose();
});

test('unsaved-changes guard: tab switch and in-app navigation ask first', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=worker');
  await page.getByLabel('Concurrent jobs per worker').fill('11');
  const messages: string[] = [];
  page.once('dialog', (d) => { messages.push(d.message()); void d.dismiss(); });
  await page.getByRole('tab', { name: 'Concurrency' }).click();
  await expect(page.getByRole('tab', { name: 'Worker' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Concurrent jobs per worker')).toHaveValue('11');
  expect(messages[0]).toContain('unsaved changes');

  page.once('dialog', (d) => { messages.push(d.message()); void d.dismiss(); });
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Sources' }).click();
  await expect(page).toHaveURL(/\/settings/);
  expect(messages).toHaveLength(2);

  page.once('dialog', (d) => void d.accept());
  await page.getByRole('tab', { name: 'Concurrency' }).click();
  await expect(page.getByRole('tab', { name: 'Concurrency' })).toHaveAttribute('aria-selected', 'true');
  await expect(page).toHaveURL(/tab=concurrency/);
  await page.getByRole('tab', { name: 'Worker' }).click(); // clean now: no prompt
  await expect(page.getByLabel('Concurrent jobs per worker')).toHaveValue('8');
});

test('marketing policy editor: not-legal-advice note, add/validate/save/remove rows', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=validation');
  await expect(page.getByTestId('not-legal-advice')).toContainText('This is not legal advice');
  await page.getByRole('button', { name: 'Add country row' }).click();
  const code = page.getByLabel('Country code, row 1');
  await code.fill('gbr');
  await expect(page.getByText(/2-letter upper-case country code/)).toBeVisible();
  await page.getByRole('button', { name: 'Save changes' }).click(); // invalid: nothing is sent
  await expect(page.getByText('Validation defaults saved')).toHaveCount(0);
  await code.fill('gb');
  await page.getByRole('combobox', { name: 'Role mailbox, row 1' }).click();
  await page.getByRole('option', { name: 'Eligible' }).click();
  await page.getByRole('switch', { name: 'Allow eligible, row 1' }).click();
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText('Validation defaults saved')).toBeVisible();
  const a = await apiAs('admin');
  const v = await (await a.get('/api/settings/validation')).json();
  expect(v.marketing_policy.GB).toMatchObject({ role: 'eligible', allow_eligible: true });

  await page.reload();
  await expect(page.getByLabel('Country code, row 1')).toHaveValue('GB');
  await page.getByRole('button', { name: 'Remove row 1' }).click();
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText('Validation defaults saved')).toBeVisible();
  expect((await (await a.get('/api/settings/validation')).json()).marketing_policy).toEqual({});
  await a.dispose();
});

test('data retention: run-now is confirmed and reports what was deleted', async ({ page }) => {
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=retention');
  await page.getByTestId('run-retention').click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText('permanently deleted');
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByTestId('retention-result')).toHaveCount(0);
  await page.getByTestId('run-retention').click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete expired data' }).click();
  await expect(page.getByTestId('retention-result')).toBeVisible();
  const a = await apiAs('admin');
  const audit = await (await a.get('/api/audit', { params: { action: 'settings.retention_run', limit: 1 } })).json();
  expect(audit.items).toHaveLength(1);
  await a.dispose();
});

test('MFA enrolment: QR (drawn locally) + secret + URI, confirm with a computed TOTP, sign-in then needs a code, disable', async ({ page }) => {
  const admin = await apiAs('admin');
  const u = await makeUser(admin, 'operator', 'mfa');
  const external: string[] = [];
  page.on('request', (r) => { const h = new URL(r.url()).hostname; if (!['localhost', '127.0.0.1'].includes(h) && !r.url().startsWith('data:')) external.push(r.url()); });
  await loginAs(page, 'operator', u);
  await page.goto('/settings');
  await expect(page.getByText('Two-step verification is off').first()).toBeVisible();
  await page.getByTestId('mfa-start').click();
  const setup = page.getByTestId('mfa-setup');
  await expect(setup.getByTestId('mfa-qr')).toBeVisible();
  const path = await setup.getByTestId('mfa-qr').locator('path').getAttribute('d');
  expect(path!.length).toBeGreaterThan(200);
  const secret = (await setup.getByTestId('mfa-secret').innerText()).trim();
  const uri = (await setup.getByTestId('mfa-uri').innerText()).trim();
  expect(secret).toMatch(/^[A-Z2-7]{16,}$/);
  expect(uri).toContain('otpauth://totp/');
  expect(uri).toContain(`secret=${secret}`);
  expect(uri).toContain(encodeURIComponent(u.email).replace('%40', '%40'));

  // wrong code first
  await page.getByLabel('Verification code').fill('000000');
  await page.getByRole('button', { name: 'Confirm and turn on' }).click();
  await expect(page.getByText(/not valid/i).first()).toBeVisible();
  await page.getByLabel('Verification code').fill(totp(secret));
  await page.getByRole('button', { name: 'Confirm and turn on' }).click();
  await expect(page.getByText('Two-step verification is on').first()).toBeVisible();
  expect(external).toEqual([]); // QR drawn client-side: no third-party requests

  // the server now requires a code at sign-in
  const anon = await pwRequest.newContext({ baseURL: API });
  const noCode = await anon.post('/api/auth/login', { data: { email: u.email, password: u.password } });
  expect(noCode.status()).toBe(401);
  expect((await noCode.json()).error.code).toBe('mfa_required');
  const withCode = await anon.post('/api/auth/login', { data: { email: u.email, password: u.password, totp_code: totp(secret) } });
  expect(withCode.ok()).toBeTruthy();
  await anon.dispose();

  // disable (password + code)
  await page.getByTestId('mfa-disable-open').click();
  const off = page.getByRole('form', { name: 'Turn off two-step verification' });
  await off.getByLabel('Password').fill('wrong-password-here');
  await off.getByLabel('Verification code').fill(totp(secret));
  await off.getByRole('button', { name: 'Turn off two-step verification' }).click();
  await expect(off.getByText(/incorrect/i).first()).toBeVisible();
  await off.getByLabel('Password').fill(u.password);
  await off.getByRole('button', { name: 'Turn off two-step verification' }).click();
  await expect(page.getByText('Two-step verification is off').first()).toBeVisible();
  const users = await (await admin.get('/api/users', { params: { limit: 200 } })).json();
  expect(users.items.find((x: { email: string }) => x.email === u.email).totp_enabled).toBe(false);
  await admin.dispose();
});

test('change password: server errors land on the right field; success signs in with the new password only', async ({ page }) => {
  const admin = await apiAs('admin');
  const u = await makeUser(admin, 'viewer', 'pw');
  await loginAs(page, 'viewer', u);
  await page.goto('/settings');
  const form = page.getByTestId('form-password');
  await form.getByLabel('Current password').fill('not-the-password-123');
  await form.getByLabel('New password', { exact: true }).fill('An0ther-Passw0rd-xyz!Q');
  await form.getByLabel('Confirm new password').fill('An0ther-Passw0rd-xyz!Q');
  await form.getByRole('button', { name: 'Change password' }).click();
  await expect(form.getByText('The current password is incorrect.')).toBeVisible();

  await form.getByLabel('Current password').fill(u.password);
  await form.getByLabel('New password', { exact: true }).fill('password1234');
  await form.getByLabel('Confirm new password').fill('password1234');
  await form.getByRole('button', { name: 'Change password' }).click();
  await expect(form.getByText(/too common|distinct/i)).toBeVisible();

  await form.getByLabel('New password', { exact: true }).fill('An0ther-Passw0rd-xyz!Q');
  await form.getByLabel('Confirm new password').fill('Mismatch-Passw0rd-xyz!Q');
  await expect(form.getByText('The passwords do not match')).toBeVisible();
  await form.getByLabel('Confirm new password').fill('An0ther-Passw0rd-xyz!Q');
  await form.getByRole('button', { name: 'Change password' }).click();
  await expect(page.getByText('Password changed. Other sessions were signed out.')).toBeVisible();

  const anon = await pwRequest.newContext({ baseURL: API });
  expect((await anon.post('/api/auth/login', { data: { email: u.email, password: u.password } })).status()).toBe(401);
  expect((await anon.post('/api/auth/login', { data: { email: u.email, password: 'An0ther-Passw0rd-xyz!Q' } })).ok()).toBeTruthy();
  await anon.dispose();
  await admin.dispose();
});

test('users: create, change role (confirmed), reset password (shown once), deactivate (confirmed)', async ({ page }) => {
  const admin = await apiAs('admin');
  await loginAs(page, 'admin');
  await page.goto('/settings?tab=users');
  const table = page.getByTestId('users-table');
  await expect(table).toContainText(USERS.admin.email);
  await expect(page.getByRole('combobox', { name: `Role for ${USERS.admin.email}` })).toBeDisabled(); // cannot demote yourself
  await expect(page.getByRole('button', { name: `Deactivate ${USERS.admin.email}` })).toBeDisabled();

  const email = `ui-user-${stamp}@e2e-ops.example.com`;
  created.push(email);
  await page.getByTestId('create-user').click();
  const dlg = page.getByRole('dialog');
  await dlg.getByLabel('Email').fill(email);
  await dlg.getByLabel('Initial password').fill('short');
  await dlg.getByRole('button', { name: 'Create user' }).click();
  await expect(dlg.getByText('Use at least 12 characters')).toBeVisible();
  await dlg.getByLabel('Initial password').fill('Cr3ated-Passw0rd-xyz!Q');
  await dlg.getByRole('button', { name: 'Create user' }).click();
  await expect(page.getByText('User created')).toBeVisible();
  const row = page.getByTestId(`user-row-${email}`);
  await expect(row).toContainText('Active');
  await expect(row).toContainText('operator', { ignoreCase: true });

  // duplicate email -> mapped onto the field
  await page.getByTestId('create-user').click();
  await page.getByRole('dialog').getByLabel('Email').fill(email);
  await page.getByRole('dialog').getByLabel('Initial password').fill('Cr3ated-Passw0rd-xyz!Q');
  await page.getByRole('dialog').getByRole('button', { name: 'Create user' }).click();
  await expect(page.getByRole('dialog').getByText('already exists')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click();

  // role change
  await row.getByRole('combobox', { name: `Role for ${email}` }).click();
  await page.getByRole('option', { name: 'Viewer' }).click();
  await expect(page.getByRole('alertdialog')).toContainText('signed out of every session');
  await page.getByRole('alertdialog').getByRole('button', { name: 'Change role' }).click();
  await expect(page.getByText('User updated')).toBeVisible();
  const list = async () => (await (await admin.get('/api/users', { params: { limit: 200 } })).json()).items as { email: string; role: string; is_active: boolean }[];
  expect((await list()).find((u) => u.email === email)!.role).toBe('viewer');

  // reset password with generated one-time password
  await row.getByRole('button', { name: `Reset password for ${email}` }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Reset password' }).click();
  const temp = page.getByTestId('temp-password');
  await expect(temp).toBeVisible();
  const tempPw = (await temp.locator('code').innerText()).trim();
  expect(tempPw.length).toBeGreaterThan(16);
  await page.getByRole('dialog').getByRole('button', { name: 'Done' }).click();
  const anon = await pwRequest.newContext({ baseURL: API });
  expect((await anon.post('/api/auth/login', { data: { email, password: tempPw } })).ok()).toBeTruthy();
  await anon.dispose();

  // deactivate
  await row.getByRole('button', { name: `Deactivate ${email}` }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Deactivate' }).click();
  await expect(row).toContainText('Deactivated');
  expect((await list()).find((u) => u.email === email)!.is_active).toBe(false);
  await admin.dispose();
});

test('RBAC: operators and viewers see only account + read-only sections; admin APIs refuse them', async ({ page }) => {
  for (const role of ['operator', 'viewer'] as const) {
    await loginAs(page, role);
    await page.goto('/dashboard');
    const nav = page.getByRole('navigation', { name: 'Primary' });
    await expect(nav.getByRole('link', { name: 'Exports' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'System' })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: 'Settings' })).toHaveCount(0);

    await page.goto('/settings?tab=users'); // direct URL to an admin tab falls back to Account
    const tabs = page.getByRole('tablist', { name: 'Settings sections' });
    await expect(tabs.getByRole('tab', { name: 'Account & security' })).toHaveAttribute('aria-selected', 'true');
    for (const hidden of ['Users', 'Worker', 'Source policies', 'Concurrency', 'Data retention', 'Audit log', 'Backups']) await expect(tabs.getByRole('tab', { name: hidden })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Change password' })).toBeVisible();

    await page.goto('/settings?tab=validation');
    await expect(page.getByText('Read-only: only administrators can change these settings.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save changes' })).toHaveCount(0);
    await expect(page.getByLabel('Result stays fresh (days)')).toBeDisabled();

    const a = await apiAs(role);
    expect((await a.get('/api/settings/worker')).status()).toBe(403);
    expect((await a.get('/api/settings')).ok()).toBeTruthy();
    expect((await (await a.get('/api/settings')).json()).worker ?? null).toBeNull();
    expect((await a.put('/api/settings/validation', { data: { default_mode: 'fast' } })).status()).toBe(403);
    expect((await a.get('/api/users')).status()).toBe(403);
    expect((await a.get('/api/audit')).status()).toBe(403);
    expect((await a.post('/api/settings/retention/run')).status()).toBe(403);
    await a.dispose();
  }
});
