import { test, expect, type Page } from '@playwright/test';
import { apiAs, loginAs, resetSystemState, sql } from './support/ops';

/** DASH-013 /system against the real backend. Run with --workers=1. No worker process runs in this environment, which is itself the outage scenario. */
test.describe.configure({ mode: 'serial' });

const RUN = `INSERT INTO sourcing_runs (name, configuration_snapshot, target_count, target_definition, uniqueness, status, counters, archived, started_at) VALUES ('e2e-ops-run', '{}', 10, 'emails', 'email', 'running', '{}', false, now()) RETURNING id`;

function cleanup() {
  sql(`DELETE FROM worker_heartbeats WHERE worker_id LIKE 'e2e-%'`);
  sql(`DELETE FROM sourcing_runs WHERE name='e2e-ops-run'`); // jobs cascade
}
test.beforeAll(cleanup);
test.afterAll(async () => { cleanup(); await resetSystemState(); });

const heartbeat = (id: string, agoSeconds: number, rss = 512, cpu = 33, jobs = 2) =>
  sql(`INSERT INTO worker_heartbeats (worker_id, hostname, pid, last_seen_at, rss_mb, cpu_percent, active_jobs, stats) VALUES ('${id}', 'host-${id}', 1, now() - interval '${agoSeconds} seconds', ${rss}, ${cpu}, ${jobs}, '{}') ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at`);

async function gotoSystem(page: Page) {
  await page.goto('/system');
  await expect(page.getByRole('heading', { name: 'System', level: 1 })).toBeVisible();
  await expect(page.getByTestId('card-api')).toBeVisible();
}

test('health cards, resource gauges, diagnostics render from the real API', async ({ page }) => {
  await loginAs(page, 'admin');
  await gotoSystem(page);
  await expect(page.getByTestId('card-api')).toContainText('OK');
  await expect(page.getByTestId('card-db')).toContainText('OK');
  await expect(page.getByTestId('card-db')).toContainText(/Round-trip [\d.]+ ms/);
  for (const g of ['CPU usage', 'Memory usage', 'Disk usage']) await expect(page.getByRole('progressbar', { name: g })).toBeVisible();
  await expect(page.getByTestId('card-dns')).toContainText('DNS timeouts');
  await expect(page.getByTestId('card-smtp')).toContainText('Diagnostics: Disabled');
  await expect(page.getByTestId('card-browser')).toContainText('Playwright');
  await expect(page.getByRole('heading', { name: 'Queue backlog by stage' })).toBeVisible();
});

test('worker outage: no heartbeat shows a danger banner, a stale worker warns, a fresh one clears it', async ({ page }) => {
  await loginAs(page, 'admin');
  await gotoSystem(page);
  const banner = page.getByTestId('worker-outage-banner');
  await expect(banner).toContainText('No worker heartbeat');
  await expect(page.getByTestId('card-workers')).toContainText('Down');

  // only a stale worker -> "all stale"
  heartbeat('e2e-w-stale', 600);
  await page.getByRole('button', { name: 'Refresh health' }).click();
  await expect(page.getByTestId('worker-outage-banner')).toContainText('All workers are stale');
  await expect(page.getByTestId('workers-table')).toContainText('stale');

  // a healthy worker alongside: warning (not outage), table shows rss / cpu / active jobs / last seen
  heartbeat('e2e-w-ok', 2, 640, 41, 3);
  await page.getByRole('button', { name: 'Refresh health' }).click();
  await expect(page.getByTestId('worker-outage-banner')).toHaveCount(0);
  await expect(page.getByTestId('stale-worker-warning')).toContainText('e2e-w-stale');
  const ok = page.getByTestId('workers-table').getByRole('row', { name: /e2e-w-ok/ });
  await expect(ok).toContainText('640 MB');
  await expect(ok).toContainText('41%');
  await expect(ok).toContainText('3');
  await expect(page.getByTestId('card-workers')).toContainText('1 of 2 healthy');

  sql(`DELETE FROM worker_heartbeats WHERE worker_id LIKE 'e2e-%'`);
});

test('failed jobs are listed with retry; queue table and chart data reflect them; restart-failed re-queues everything', async ({ page }) => {
  const runId = sql(RUN).split('\n')[0];
  sql(`INSERT INTO sourcing_jobs (run_id, stage, dedupe_key, payload, checkpoint, state, priority, attempts, error_code, error_detail, finished_at) VALUES
    (${runId}, 'verify', 'e2e-1', '{}', '{}', 'failed', 10, 2, 'dns_timeout', 'synthetic DNS timeout', now()),
    (${runId}, 'verify', 'e2e-2', '{}', '{}', 'dead', 10, 4, 'http_503', 'synthetic upstream 503', now()),
    (${runId}, 'crawl',  'e2e-3', '{}', '{}', 'failed', 10, 1, 'robots_blocked', 'synthetic robots block', now()),
    (${runId}, 'crawl',  'e2e-4', '{}', '{}', 'queued', 10, 2, NULL, NULL, NULL)`);
  const a = await apiAs('admin');
  await loginAs(page, 'admin');
  await gotoSystem(page);

  const q = page.getByTestId('queue-table');
  await expect(q.getByRole('row', { name: /verify\s+Failed\s+1/ })).toBeVisible();
  await expect(q.getByRole('row', { name: /verify\s+Dead\s+1/ })).toBeVisible();
  await expect(q.getByRole('row', { name: /crawl\s+Queued\s+1/ })).toBeVisible();
  await expect(page.getByTestId('failed-count')).toHaveText('2');
  await expect(page.getByText(/Retry counts/)).toContainText('2 attempts: 1');

  const jobs = page.getByTestId('failed-jobs-table');
  await expect(jobs).toContainText('dns_timeout');
  await expect(jobs).toContainText('synthetic upstream 503');
  const robots = jobs.getByRole('row', { name: /robots_blocked/ });
  const jobId = (await robots.getByRole('button', { name: /Retry job/ }).getAttribute('aria-label'))!.replace(/\D/g, '');
  await robots.getByRole('button', { name: /Retry job/ }).click();
  await expect(page.getByText('Job re-queued')).toBeVisible();
  await expect(jobs.getByText('robots_blocked')).toHaveCount(0);
  expect(sql(`SELECT state FROM sourcing_jobs WHERE id=${jobId}`)).toBe('queued');

  // restart all failed + dead (confirm dialog first)
  await page.getByRole('button', { name: 'Restart failed jobs' }).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText('retry count reset');
  await expect(dlg).toContainText('de-duplicated');
  await dlg.getByRole('button', { name: 'Restart failed jobs' }).click();
  await expect(page.getByText(/Re-queued 2 failed jobs/)).toBeVisible();
  const h = await (await a.get('/api/system/health')).json();
  expect(h.failed_jobs).toBe(0);
  expect(h.dead_jobs).toBe(0);
  await a.dispose();
  sql(`DELETE FROM sourcing_runs WHERE name='e2e-ops-run'`);
});

test('pause-all / resume: confirm dialogs explain safety; run state verified via the API', async ({ page }) => {
  const runId = Number(sql(RUN).split('\n')[0]);
  const a = await apiAs('admin');
  await loginAs(page, 'admin');
  await gotoSystem(page);

  await page.getByRole('button', { name: 'Pause all runs' }).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText('completes its current step');
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  expect(sql(`SELECT status FROM sourcing_runs WHERE id=${runId}`)).toBe('running'); // cancel changes nothing

  await page.getByRole('button', { name: 'Pause all runs' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Pause all runs' }).click();
  await expect(page.getByText(/Paused \d+ running run/)).toBeVisible();
  expect(sql(`SELECT status FROM sourcing_runs WHERE id=${runId}`)).toBe('paused');
  expect((await (await a.get('/api/system/health')).json()).paused_by_pause_all).toContain(runId);
  await expect(page.getByText(/paused by “Pause all”/).first()).toBeVisible();

  await page.getByRole('button', { name: 'Resume processing' }).click();
  await expect(page.getByRole('alertdialog')).toContainText('Runs paused individually stay paused');
  await page.getByRole('alertdialog').getByRole('button', { name: 'Resume processing' }).click();
  await expect(page.getByText(/Resumed \d+ run/)).toBeVisible();
  expect(sql(`SELECT status FROM sourcing_runs WHERE id=${runId}`)).toBe('running');
  expect((await (await a.get('/api/system/health')).json()).paused_by_pause_all).toEqual([]);
  // audit trail
  const audit = await (await a.get('/api/audit', { params: { action: 'system.*', limit: 10 } })).json();
  expect(audit.items.map((i: { action: string }) => i.action)).toEqual(expect.arrayContaining(['system.pause_all', 'system.resume_processing']));
  await a.dispose();
  sql(`DELETE FROM sourcing_runs WHERE name='e2e-ops-run'`);
});

test('maintenance mode: confirm with message, global banner, writes rejected with 503, then off', async ({ page }) => {
  const a = await apiAs('admin');
  const op = await apiAs('operator');
  await loginAs(page, 'admin');
  await gotoSystem(page);
  await page.getByRole('button', { name: 'Turn maintenance on' }).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText('rejected with a 503');
  await expect(dlg).toContainText('does not');
  await dlg.getByLabel(/Message shown to users/).fill('Database upgrade in progress');
  await dlg.getByRole('button', { name: 'Turn on' }).click();
  await expect(page.getByText('Maintenance mode is on').first()).toBeVisible();
  await expect(page.getByTestId('maintenance-banner')).toContainText('Database upgrade in progress');
  const m = await (await a.get('/api/system/maintenance')).json();
  expect(m.enabled).toBe(true);
  expect(m.message).toBe('Database upgrade in progress');
  // a normal write is refused with 503 maintenance_mode
  const blocked = await op.post('/api/exports', { data: { preset: 'full_intelligence', format: 'csv', filters: {} } });
  expect(blocked.status()).toBe(503);
  expect((await blocked.json()).error.code).toBe('maintenance_mode');

  await page.getByRole('button', { name: 'Turn maintenance off' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Turn off' }).click();
  await expect(page.getByText('Maintenance mode is off')).toBeVisible();
  expect((await (await a.get('/api/system/maintenance')).json()).enabled).toBe(false);
  await expect(page.getByTestId('maintenance-banner')).toHaveCount(0, { timeout: 15_000 });
  await a.dispose(); await op.dispose();
});

test('SMTP egress check: explains port 25, reports an honest blocked/open/unknown result and persists it', async ({ page }) => {
  await loginAs(page, 'admin');
  await gotoSystem(page);
  const smtp = page.getByTestId('card-smtp');
  await expect(smtp).toContainText('Most cloud providers block outbound port 25');
  await smtp.getByRole('button', { name: 'Check outbound port 25' }).click();
  const dlg = page.getByRole('alertdialog');
  await expect(dlg).toContainText('No e-mail or data is sent');
  await dlg.getByRole('button', { name: 'Run check' }).click();
  await expect(page.getByTestId('egress-detail')).toBeVisible({ timeout: 20_000 });
  const status = (await page.getByTestId('egress-status').innerText()).replace('Outbound port 25:', '').trim();
  expect(['Open', 'Blocked', 'Unknown']).toContain(status);
  const a = await apiAs('admin');
  const h = await (await a.get('/api/system/health')).json();
  expect(h.smtp.egress_port_25).toBe(status.toLowerCase()); // what the UI shows is what the server recorded
  expect(h.smtp.egress_checked_at).toBeTruthy();
  await a.dispose();
});

test('auto-refresh polls health and can be switched off; Refresh health fetches immediately', async ({ page }) => {
  await loginAs(page, 'admin');
  const hits: number[] = [];
  page.on('request', (r) => { if (r.url().includes('/api/system/health')) hits.push(Date.now()); });
  await gotoSystem(page);
  const sw = page.getByRole('switch', { name: /Auto-refresh/ });
  await expect(sw).toBeChecked();
  const n0 = hits.length;
  await page.waitForRequest((r) => r.url().includes('/api/system/health'), { timeout: 15_000 }); // interval tick
  expect(hits.length).toBeGreaterThan(n0);
  await sw.click();
  await expect(sw).not.toBeChecked();
  const n1 = hits.length;
  await page.waitForTimeout(500);
  await page.getByRole('button', { name: 'Refresh health' }).click();
  await expect.poll(() => hits.length).toBeGreaterThan(n1);
});

test('non-admins cannot use /system', async ({ page }) => {
  for (const role of ['operator', 'viewer'] as const) {
    await loginAs(page, role);
    await page.goto('/system');
    await expect(page.getByText('You don’t have access to this page')).toBeVisible();
    await expect(page.getByTestId('card-api')).toHaveCount(0);
    const a = await apiAs(role);
    expect((await a.post('/api/system/pause-all')).status()).toBe(403);
    expect((await a.post('/api/system/smtp-egress-check')).status()).toBe(403);
    expect((await a.put('/api/system/maintenance', { data: { enabled: true } })).status()).toBe(403);
    await a.dispose();
  }
});
