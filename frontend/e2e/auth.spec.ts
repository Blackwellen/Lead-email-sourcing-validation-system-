import { test, expect } from '@playwright/test';

/**
 * Optional end-to-end auth flow; requires the backend + a seeded admin:
 *   E2E_ADMIN_EMAIL=admin@example.com E2E_ADMIN_PASSWORD=... npx playwright test e2e/auth.spec.ts
 */
const email = process.env.E2E_ADMIN_EMAIL;
const password = process.env.E2E_ADMIN_PASSWORD;

test.describe('auth (needs backend)', () => {
  test.skip(!email || !password, 'E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD not set');

  test('wrong password shows an error, correct login reaches the shell, logout returns to /login', async ({ page }) => {
    await page.goto('/login');
    await page.getByLabel('Email', { exact: true }).fill(email!);
    await page.getByLabel('Password', { exact: true }).fill('definitely-wrong');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByTestId('login-error')).toBeVisible();

    await page.getByLabel('Password', { exact: true }).fill(password!);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL(/\/dashboard/);
    await expect(page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Source Explorer' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();

    await page.getByTestId('user-menu').click();
    await page.getByRole('menuitem', { name: 'Sign out' }).click();
    await expect(page).toHaveURL(/\/login/);
  });
});
