import { expect, type APIResponse, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/** Credentials for the users created by biz-backend.sh setup (DB lead_fe_b). */
export const USERS = {
  admin: { email: 'admin-b@example.com', password: 'Tr0ub4dor&3-horse-staple' },
  operator: { email: 'operator-b@example.com', password: 'Tr0ub4dor&3-horse-staple' },
  viewer: { email: 'viewer-b@example.com', password: 'Tr0ub4dor&3-horse-staple' },
};

/** Log in through the real API (same-origin via the Next proxy); the session cookie lands in the browser context. */
export async function login(page: Page, who: keyof typeof USERS = 'admin') {
  const r = await page.request.post('/api/auth/login', { data: USERS[who] });
  expect(r.ok(), await r.text()).toBeTruthy();
  const j = await r.json();
  return { csrf: j.csrf_token as string };
}

/** Tiny authenticated API client for test setup/verification. */
export async function apiClient(page: Page, who: keyof typeof USERS = 'admin') {
  const { csrf } = await login(page, who);
  const h = { 'X-CSRF-Token': csrf };
  const json = async (r: APIResponse) => { expect(r.ok(), `${r.url()} -> ${r.status()} ${await r.text()}`).toBeTruthy(); return r.json(); };
  return {
    get: async (p: string) => json(await page.request.get(p)),
    post: async (p: string, data?: unknown) => json(await page.request.post(p, { data, headers: h })),
    raw: { get: (p: string) => page.request.get(p), post: (p: string, data?: unknown) => page.request.post(p, { data, headers: h }) },
  };
}

export async function expectNoSeriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).join(' | ')})`)).toEqual([]);
}

export async function expectNoHorizontalScroll(page: Page) {
  await page.waitForLoadState('networkidle');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

export async function findBusiness(page: Page, q: string) {
  const api = await apiClient(page);
  const r = await api.get(`/api/businesses?q=${encodeURIComponent(q)}&limit=5`);
  return r.items as { id: number; legal_name: string }[];
}
