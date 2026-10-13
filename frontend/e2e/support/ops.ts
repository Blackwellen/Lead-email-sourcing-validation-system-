import { execFileSync } from 'node:child_process';
import { createHmac } from 'node:crypto';
import { expect, request as pwRequest, type APIRequestContext, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/**
 * Helpers for the ops-*.spec.ts suites. They run against a REAL backend (DB lead_fe_c, API :8103) and a Next server (:3103)
 * with SYNTHETIC rows only. See ops-backend.sh / ops-seed.sql. Run with `--workers=1` (specs share one database).
 */
export const API = process.env.E2E_API || 'http://localhost:8103';
export const USERS = {
  admin: { email: 'admin@e2e.test', password: 'Adm1n-Passw0rd-xyz!Q' },
  operator: { email: 'operator@e2e.test', password: 'Op3rator-Passw0rd-xyz!Q' },
  viewer: { email: 'viewer@e2e.test', password: 'V1ewer-Passw0rd-xyz!Q' },
} as const;
export type RoleName = keyof typeof USERS;

/** Sign in through the Next proxy so the session cookie belongs to the page's origin. */
export async function loginAs(page: Page, role: RoleName, creds: { email: string; password: string; totp_code?: string } = USERS[role]) {
  const res = await page.request.post('/api/auth/login', { data: creds });
  expect(res.ok(), `login ${creds.email}: ${res.status()} ${await res.text()}`).toBeTruthy();
}

/** Direct API client for assertions (own cookie jar, talks to the backend, not the Next proxy; CSRF header set). */
export async function apiAs(role: RoleName, creds: { email: string; password: string; totp_code?: string } = USERS[role]): Promise<APIRequestContext> {
  const login = await pwRequest.newContext({ baseURL: API });
  const res = await login.post('/api/auth/login', { data: creds });
  expect(res.ok(), `api login ${creds.email}: ${res.status()}`).toBeTruthy();
  const csrf = (await res.json()).csrf_token as string;
  const state = await login.storageState();
  await login.dispose();
  return pwRequest.newContext({ baseURL: API, storageState: state, extraHTTPHeaders: { 'X-CSRF-Token': csrf } });
}

/** psql against the e2e database (synthetic fixtures only). */
export function sql(statement: string): string {
  return execFileSync('psql', ['-h', 'localhost', '-U', 'lead', '-d', 'lead_fe_c', '-v', 'ON_ERROR_STOP=1', '-tA', '-c', statement], { env: { ...process.env, PGPASSWORD: 'lead' }, encoding: 'utf8' }).trim();
}

/* ------------------------------------------------------------------ TOTP (RFC 6238, SHA-1, 30 s, 6 digits) */
function base32Decode(s: string): Buffer {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const ch of s.replace(/=+$/, '').toUpperCase()) {
    const v = alphabet.indexOf(ch);
    if (v < 0) throw new Error(`bad base32 char ${ch}`);
    bits += v.toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
export function totp(secret: string, atMs = Date.now()): string {
  const counter = Math.floor(atMs / 1000 / 30);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const h = createHmac('sha1', base32Decode(secret)).update(buf).digest();
  const o = h[h.length - 1] & 0xf;
  const code = ((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3];
  return String(code % 1_000_000).padStart(6, '0');
}

/* ------------------------------------------------------------------ a11y / layout */
export async function expectNoSeriousA11y(page: Page, label: string) {
  const res = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const bad = res.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.length}) ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`), `axe on ${label}`).toEqual([]);
}
export async function expectNoHorizontalScroll(page: Page, label: string) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(over, `horizontal overflow on ${label}`).toBeLessThanOrEqual(1);
}

export async function resetSystemState() {
  const a = await apiAs('admin');
  await a.put('/api/system/maintenance', { data: { enabled: false, message: '' } });
  await a.post('/api/system/resume-processing');
  await a.dispose();
}

/** Minimal RFC 4180 CSV parser (quoted fields, doubled quotes, embedded newlines). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += c;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => !(r.length === 1 && r[0] === ''));
}
