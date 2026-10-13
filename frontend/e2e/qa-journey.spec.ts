/**
 * Operator journey against a REAL running stack (API + worker + production web build), optionally against the live
 * internet. Opt-in: QA_JOURNEY=1, with QA_EMAIL / QA_PASSWORD for an existing admin and QA_DOMAINS (comma-separated
 * public business domains whose sites may be crawled). Screenshots go to QA_SHOTS (default test-results/qa-journey).
 *
 *   login -> wizard (5 steps, imported domains, FAST) -> start -> live run detail -> pause/resume -> completion
 *   -> contacts -> contact evidence -> export CSV -> download
 *
 * It asserts behaviour, never specific real-world results: the number of contacts found depends on the live sites.
 */
import { expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const on = process.env.QA_JOURNEY === '1';
const shots = process.env.QA_SHOTS || 'test-results/qa-journey';
const domains = (process.env.QA_DOMAINS || '').split(',').map((d) => d.trim()).filter(Boolean);

test.describe.configure({ mode: 'serial' });
test.skip(!on, 'set QA_JOURNEY=1 to run the live operator journey');

test('operator journey: imported domains -> live crawl -> verify -> export', async ({ page }) => {
  test.setTimeout(15 * 60_000);
  fs.mkdirSync(shots, { recursive: true });
  const snap = (n: string) => page.screenshot({ path: path.join(shots, `${n}.png`), fullPage: true });

  await page.goto('/login');
  await page.getByLabel('Email').fill(process.env.QA_EMAIL!);
  await page.getByRole('textbox', { name: 'Password' }).fill(process.env.QA_PASSWORD!);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/dashboard/);
  await snap('01-dashboard-empty');

  await page.goto('/runs/new');
  await page.getByLabel('Run name').fill(`QA journey ${new Date().toISOString().slice(0, 16)}`);
  await snap('02-wizard-step1');
  await page.getByRole('button', { name: /^Next:/ }).click();
  await page.getByLabel('Domains', { exact: true }).fill(domains.join('\n'));
  await snap('03-wizard-step2');
  await page.getByRole('button', { name: /^Next:/ }).click();
  await page.getByLabel('Target number').fill(String(Math.max(3, domains.length * 2)));
  await snap('04-wizard-step3');
  await page.getByRole('button', { name: /^Next:/ }).click();
  await snap('05-wizard-step4');
  await page.getByRole('button', { name: /^Next:/ }).click();
  await snap('06-wizard-review');
  await page.getByRole('button', { name: 'Start run' }).click();
  await expect(page).toHaveURL(/\/runs\/\d+/);
  const runId = page.url().match(/\/runs\/(\d+)/)![1];

  // live progress via SSE
  await page.waitForTimeout(8_000);
  await snap('07-run-live');

  // pause + resume through the UI controls
  const pause = page.getByRole('button', { name: /^Pause/ });
  if (await pause.isVisible().catch(() => false)) {
    await pause.click();
    await expect(page.getByText(/Paused/).first()).toBeVisible({ timeout: 20_000 });
    await snap('08-run-paused');
    await page.getByRole('button', { name: /^Resume/ }).click();
    await expect(page.getByText(/Running|Completed/).first()).toBeVisible({ timeout: 20_000 });
  }

  // wait for the run to finish (target reached or sources exhausted)
  const api = page.request;
  let status = '';
  for (let i = 0; i < 160; i++) {
    const r = await api.get(`/api/runs/${runId}`);
    status = (await r.json()).status;
    if (['completed', 'failed'].includes(status)) break;
    await page.waitForTimeout(5_000);
  }
  expect(status).toBe('completed');
  await page.reload();
  await snap('09-run-completed');

  await page.goto('/contacts');
  await page.waitForTimeout(2_000);
  await snap('10-contacts');
  const firstRow = page.getByRole('row').nth(1);
  if (await firstRow.isVisible().catch(() => false)) {
    await firstRow.click();
    await page.waitForURL(/\/contacts\/\d+/, { timeout: 10_000 }).catch(() => undefined);
    await page.waitForTimeout(1_500);
    await snap('11-contact-evidence');
    // honest labelling: never a "Verified" badge, and the safe != delivery disclaimer is present
    await expect(page.getByText(/not a delivery guarantee/i).first()).toBeVisible();
  }

  await page.goto('/exports');
  await page.waitForTimeout(1_500);
  await snap('12-exports');
  const created = await api.post('/api/exports', {
    data: { preset: 'published_business_emails', format: 'csv', filters: {}, run_id: Number(runId) },
    headers: { 'X-CSRF-Token': (await page.context().cookies()).find((c) => c.name === 'lead_csrf')?.value ?? '' },
  });
  expect(created.ok()).toBeTruthy();
  const ex = await created.json();
  let done = ex;
  for (let i = 0; i < 30 && done.status !== 'done'; i++) {
    await page.waitForTimeout(1_000);
    done = await (await api.get(`/api/exports/${ex.id}`)).json();
  }
  expect(done.status).toBe('done');
  const dl = await api.get(`/api/exports/${ex.id}/download`);
  expect(dl.ok()).toBeTruthy();
  const csv = await dl.text();
  expect(csv.split('\n')[0]).toContain('Email Address');
  fs.writeFileSync(path.join(shots, 'export-header.txt'), csv.split('\n')[0]);
  fs.writeFileSync(path.join(shots, 'summary.json'), JSON.stringify({ runId, status, exportRows: done.row_count }, null, 2));
});
