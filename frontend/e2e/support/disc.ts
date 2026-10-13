import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test, type APIResponse, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/**
 * Helpers for the disc-*.spec.ts suites: REAL backend (DB lead_fe_d, API :8104) + production Next build (:3104) with
 * SYNTHETIC rows from disc_seed.py. See disc-backend.sh. Run with --workers=1 (specs share and mutate one database).
 */
export const API = process.env.E2E_DISC_API || 'http://localhost:8104';
const PW = 'Tr0ub4dor&3-horse-staple';
export const USERS = {
  admin: { email: 'admin-d@example.com', password: PW },
  operator: { email: 'operator-d@example.com', password: PW },
  viewer: { email: 'viewer-d@example.com', password: PW },
} as const;
export type Who = keyof typeof USERS;

export function requireBackend() {
  test.beforeAll(async () => {
    const up = await fetch(`${API}/healthz`).then((r) => r.ok).catch(() => false);
    test.skip(!up, `disc e2e needs the real backend at ${API}; start it with e2e/support/disc-backend.sh`);
  });
}

/** Re-run the idempotent synthetic seed (resets every disc fixture). */
export function reseed() {
  const sh = path.join(__dirname, 'disc-backend.sh');
  return execFileSync('bash', [sh, 'seed'], { encoding: 'utf8', timeout: 120_000 });
}

/** Sign in through the Next proxy (cookie belongs to the page origin). Returns the CSRF token. */
export async function login(page: Page, who: Who = 'admin') {
  const r = await page.request.post('/api/auth/login', { data: USERS[who] });
  expect(r.ok(), `login ${who}: ${r.status()} ${await r.text()}`).toBeTruthy();
  return (await r.json()).csrf_token as string;
}

export async function apiAs(page: Page, who: Who = 'admin') {
  const csrf = await login(page, who);
  const h = { 'X-CSRF-Token': csrf };
  const json = async (r: APIResponse) => { expect(r.ok(), `${r.url()} -> ${r.status()} ${await r.text()}`).toBeTruthy(); return r.status() === 204 ? null : r.json(); };
  return {
    get: async (p: string) => json(await page.request.get(p)),
    post: async (p: string, data?: unknown) => json(await page.request.post(p, { data, headers: h })),
    patch: async (p: string, data?: unknown) => json(await page.request.patch(p, { data, headers: h })),
    del: async (p: string) => json(await page.request.delete(p, { headers: h })),
    raw: { post: (p: string, data?: unknown) => page.request.post(p, { data, headers: h }) },
  };
}

export async function orgId(page: Page, name: string): Promise<number> {
  const r = await page.request.get(`/api/businesses?q=${encodeURIComponent(name)}&limit=5`);
  expect(r.ok()).toBeTruthy();
  const items = (await r.json()).items as { id: number; legal_name: string }[];
  const hit = items.find((i) => i.legal_name === name);
  expect(hit, `business '${name}' not found – run disc-backend.sh seed`).toBeTruthy();
  return hit!.id;
}

export async function setTheme(page: Page, theme: 'light' | 'dark') {
  await page.addInitScript((t) => { try { localStorage.setItem('lead-theme', t); } catch { /* ignore */ } }, theme);
}

export async function expectNoSeriousA11y(page: Page, opts: { exclude?: string[] } = {}) {
  let b = new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']);
  for (const e of opts.exclude ?? []) b = b.exclude(e);
  const results = await b.analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.slice(0, 5).map((n) => n.target.join(' ')).join(' | ')})`)).toEqual([]);
}

export async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, 'page scrolls horizontally').toBeLessThanOrEqual(0);
}

/** Fail the test on any uncaught page error (React crashes, hydration errors). */
export function trackPageErrors(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  return () => expect(errors, 'uncaught page errors').toEqual([]);
}
