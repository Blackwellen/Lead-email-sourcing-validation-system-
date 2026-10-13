import { expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/** Real-backend credentials: E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD (admin created with `python -m app.cli create-admin --password-stdin`). */
export const email = process.env.E2E_ADMIN_EMAIL;
export const password = process.env.E2E_ADMIN_PASSWORD;
export const hasBackend = !!email && !!password;

/** Sign in through the real API (cookies land in the browser context), avoiding the login rate limit across many tests. */
let cached: Awaited<ReturnType<ReturnType<Page['context']>['cookies']>> | null = null;
export async function apiLogin(page: Page) {
  if (cached) { await page.context().addCookies(cached); return; }
  const res = await page.request.post('/api/auth/login', { data: { email, password } });
  expect(res.ok(), `login failed: ${res.status()}`).toBeTruthy();
  cached = await page.context().cookies();
}

/** Sign in through the real login form. */
export async function uiLogin(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Email', { exact: true }).fill(email!);
  await page.getByLabel('Password', { exact: true }).fill(password!);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/\/dashboard/);
}

export async function expectNoSeriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(' | ')})`)).toEqual([]);
}

export const uniqueName = (prefix: string) => `${prefix} ${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

/** Create + start (or draft) a run directly through the API. Returns the run id. */
export async function createRunViaApi(page: Page, name: string, opts: { action?: 'draft' | 'start'; domains?: string[]; count?: number } = {}) {
  const csrf = (await page.context().cookies()).find((c) => c.name === 'lead_csrf')?.value;
  const res = await page.request.post('/api/runs', {
    headers: csrf ? { 'X-CSRF-Token': csrf } : {},
    data: {
      action: opts.action ?? 'start',
      config: { name, countries: ['GB'], imported_domains: opts.domains ?? ['example.com'], target: { count: opts.count ?? 5 } },
    },
  });
  expect(res.ok(), `create run failed: ${res.status()} ${await res.text()}`).toBeTruthy();
  return (await res.json()).id as number;
}

/** Fill steps 1-2 of the wizard minimally (name + imported domains) and stop on step 3. */
export async function fillWizardToStep3(page: Page, name: string, domains = 'example.com\nexample.org') {
  await page.getByLabel('Run name').fill(name);
  await page.getByRole('button', { name: /^Next:/ }).click();
  await expect(page.getByTestId('step-heading')).toContainText('Step 2 of 5');
  await page.getByLabel('Domains', { exact: true }).fill(domains);
  await page.getByRole('button', { name: /^Next:/ }).click();
  await expect(page.getByTestId('step-heading')).toContainText('Step 3 of 5');
}
