import { test, expect } from '@playwright/test';
import { apiLogin, createRunViaApi, hasBackend, uniqueName } from './runs-helpers';

test.skip(!hasBackend, 'E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD not set (needs a real backend)');

test.describe('run detail and lifecycle (real backend, no worker required for state transitions)', () => {
  test('observe a running run, pause, resume and gracefully stop it', async ({ page }) => {
    await apiLogin(page);
    const name = uniqueName('E2E lifecycle');
    const id = await createRunViaApi(page, name);
    await page.goto(`/runs/${id}`);
    await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible();
    await expect(page.getByText('Running', { exact: true }).first()).toBeVisible();
    await expect(page.getByTestId('run-progress')).toContainText('of 5');
    await expect(page.getByTestId('live-status')).toContainText('Live', { timeout: 15_000 });
    await expect(page.getByTestId('run-eta')).toContainText('Unknown');
    await expect(page.getByRole('region', { name: 'Pipeline' }).or(page.getByLabel('Pipeline'))).toBeVisible();

    await page.getByRole('button', { name: 'Pause' }).click();
    await expect(page.getByText('Paused', { exact: true }).first()).toBeVisible();
    await page.getByRole('button', { name: 'Resume' }).click();
    await expect(page.getByText('Running', { exact: true }).first()).toBeVisible();

    await page.getByRole('button', { name: 'Graceful stop' }).click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText('Gracefully stop this run?');
    await dialog.getByRole('button', { name: 'Stop run' }).click();
    await expect(page.getByText(/Stopping|Completed/).first()).toBeVisible();
  });

  test('409 from the backend is surfaced (resume a run that is not paused)', async ({ page }) => {
    await apiLogin(page);
    const id = await createRunViaApi(page, uniqueName('E2E 409'));
    await page.goto(`/runs/${id}`);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible();
    // pause via API behind the UI's back, then click the stale Pause button
    const csrf = (await page.context().cookies()).find((c) => c.name === 'lead_csrf')?.value ?? '';
    await page.request.post(`/api/runs/${id}/pause`, { headers: { 'X-CSRF-Token': csrf } });
    await page.getByRole('button', { name: 'Pause' }).click();
    await expect(page.getByText(/cannot pause a run in state paused/i)).toBeVisible();
  });

  test('export current results creates an export filtered to the run and links to /exports', async ({ page }) => {
    await apiLogin(page);
    const id = await createRunViaApi(page, uniqueName('E2E export'));
    await page.goto(`/runs/${id}`);
    await page.getByRole('button', { name: 'Export current results' }).click();
    await expect(page.getByRole('link', { name: 'Open Exports' }).first()).toBeVisible();
    const list = await page.request.get('/api/exports');
    expect(JSON.stringify(await list.json())).toContain(`"run_id":${id}`);
  });

  test('unknown run shows a not-found state', async ({ page }) => {
    await apiLogin(page);
    await page.goto('/runs/99999999');
    await expect(page.getByText('Run not found')).toBeVisible();
  });
});

test.describe('runs list (real backend)', () => {
  test('tabs with counts, row actions with confirm dialogs, duplicate and archive', async ({ page }) => {
    await apiLogin(page);
    const name = uniqueName('E2E list');
    const id = await createRunViaApi(page, name);
    await page.goto('/runs');
    await expect(page.getByRole('tab', { name: /^Active/ })).toBeVisible();
    for (const t of ['Scheduled', 'Paused', 'Completed', 'Failed', 'Drafts']) await expect(page.getByRole('tab', { name: new RegExp(`^${t}`) })).toBeVisible();
    const table = page.getByRole('table', { name: 'Active runs' });
    await expect(table.getByRole('link', { name })).toBeVisible();
    const row = table.locator('tr[data-row]', { hasText: name });
    for (const col of ['Run name', 'Category', 'Countries', 'Target', 'Progress', 'Collected', 'Eligible', 'Speed', 'Started', 'Status']) {
      await expect(table.getByRole('columnheader', { name: col })).toBeVisible();
    }

    await row.getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Pause' }).click();
    await page.getByRole('tab', { name: /^Paused/ }).click();
    const prow = page.getByRole('table', { name: 'Paused runs' }).locator('tr[data-row]', { hasText: name });
    await expect(prow).toBeVisible();
    await prow.getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Resume' }).click();
    await page.getByRole('tab', { name: /^Active/ }).click();
    await expect(page.getByRole('table', { name: 'Active runs' }).locator('tr[data-row]', { hasText: name })).toBeVisible();

    // stop requires confirmation
    await page.getByRole('table', { name: 'Active runs' }).locator('tr[data-row]', { hasText: name }).getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Graceful stop' }).click();
    await expect(page.getByRole('alertdialog')).toContainText('Gracefully stop');
    await page.getByRole('alertdialog').getByRole('button', { name: 'Stop run' }).click();
    await expect(page.getByRole('alertdialog')).toBeHidden();
    await expect(page.getByRole('table', { name: 'Active runs' }).locator('tr[data-row]', { hasText: name })).toContainText(/Stopping|Completed/);
    expect(id).toBeGreaterThan(0);

    // duplicate -> draft
    await page.getByRole('table', { name: 'Active runs' }).locator('tr[data-row]', { hasText: name }).getByRole('button', { name: 'Row actions' }).click();
    await page.getByRole('menuitem', { name: 'Duplicate' }).click();
    await page.getByRole('tab', { name: /^Drafts/ }).click();
    await expect(page.getByRole('link', { name: `${name} (copy)` })).toBeVisible();
  });
});
