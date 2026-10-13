import { test, expect } from '@playwright/test';
import { safeNext } from '../lib/utils';
import { login, requireBackend } from './support/disc';

/** Open-redirect hardening of `?next=` (lib/utils safeNext) and the Content-Security-Policy header. */

const HOSTILE = ['/\\evil.com', '//evil.com', '/\t/evil.com', '/\\\\evil.com', 'javascript:alert(1)', 'https://evil.com/x', '/%5Cevil.com', ' /runs', '/login?next=/x', '\\/evil.com'];

test.describe('safeNext (unit)', () => {
  for (const v of HOSTILE) {
    test(`rejects ${JSON.stringify(v)}`, () => {
      const out = safeNext(v);
      expect(out === '/dashboard' || (out.startsWith('/') && !out.startsWith('//') && !out.includes('\\')), `got ${out}`).toBeTruthy();
      expect(new URL(out, 'http://localhost').origin).toBe('http://localhost');
    });
  }
  test('rejects the obvious ones to the fallback', () => {
    for (const v of ['/\\evil.com', '//evil.com', '/\t/evil.com', 'javascript:alert(1)', 'https://evil.com/x']) expect(safeNext(v)).toBe('/dashboard');
  });
  test('keeps a valid same-origin path with query and hash', () => {
    expect(safeNext('/runs?tab=active')).toBe('/runs?tab=active');
    expect(safeNext('/businesses/12?x=1#emails')).toBe('/businesses/12?x=1#emails');
    expect(safeNext(null)).toBe('/dashboard');
  });
});

test.describe('in the browser', () => {
  requireBackend();
  for (const next of ['/\\evil.com', '//evil.com', '/\t/evil.com', 'javascript:alert(1)']) {
    test(`signed-in /login?next=${JSON.stringify(next)} stays on this origin`, async ({ page, baseURL }) => {
      await login(page, 'admin');
      await page.goto(`/login?next=${encodeURIComponent(next)}`);
      await expect(page).toHaveURL(/\/dashboard/);
      expect(new URL(page.url()).origin).toBe(new URL(baseURL!).origin);
    });
  }
  test('signed-in /login?next=/runs?tab=active goes there', async ({ page }) => {
    await login(page, 'admin');
    await page.goto(`/login?next=${encodeURIComponent('/runs?tab=active')}`);
    await expect(page).toHaveURL(/\/runs\?tab=active$/);
  });

  test('pages send a strict CSP and run without CSP violations', async ({ page }) => {
    const violations: string[] = [];
    await page.exposeFunction('__cspViolation', (v: string) => { violations.push(v); });
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', (e) => (window as unknown as { __cspViolation: (v: string) => void }).__cspViolation(`${e.violatedDirective} ${e.blockedURI}`));
    });
    await login(page, 'admin');
    const res = await page.goto('/dashboard');
    const csp = res!.headers()['content-security-policy'] ?? '';
    for (const d of ["default-src 'self'", "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'", "connect-src 'self'", "img-src 'self' data:", "object-src 'none'"]) expect(csp).toContain(d);
    expect(csp).not.toContain('unsafe-eval');
    expect(res!.headers()['x-frame-options']).toBe('DENY');
    for (const url of ['/dashboard', '/discovery', '/discovery/network', '/sources/explore', '/opportunities', '/data-quality', '/settings']) {
      await page.goto(url);
      await page.waitForLoadState('networkidle');
      await expect(page.locator('h1')).toBeVisible();
    }
    expect(violations).toEqual([]);
  });
});
