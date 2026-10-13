import { test, expect, type Page } from '@playwright/test';
import { expectNoHorizontalScroll, expectNoSeriousA11y, login, orgId, requireBackend, setTheme, trackPageErrors } from './support/disc';

/** axe (0 serious/critical) in light + dark and 375px layout for the five discovery pages and the completeness tab. */
test.describe.configure({ mode: 'serial' });
requireBackend();

const PAGES: { name: string; url: (p: Page) => Promise<string>; ready: (p: Page) => Promise<void> }[] = [
  { name: 'source-explorer', url: async () => '/sources/explore?country=GB', ready: async (p) => { await expect(p.getByTestId('explorer-results')).toBeVisible(); } },
  { name: 'source-explorer-drawer', url: async () => '/sources/explore?open=brave_search', ready: async (p) => { await expect(p.getByTestId('configure-form')).toBeVisible(); } },
  { name: 'discovery-overview', url: async () => '/discovery', ready: async (p) => { await expect(p.getByTestId('missing-contact-opportunities')).toBeVisible(); } },
  { name: 'discovery-effectiveness', url: async () => '/discovery?tab=effectiveness', ready: async (p) => { await expect(p.getByTestId('effectiveness-table')).toBeVisible(); } },
  { name: 'discovery-scheduler', url: async () => '/discovery?tab=scheduler', ready: async (p) => { await expect(p.getByTestId('scheduler-table')).toBeVisible(); } },
  { name: 'discovery-monitors', url: async () => '/discovery?tab=monitors', ready: async (p) => { await expect(p.getByTestId('monitors-list')).toBeVisible(); } },
  { name: 'discovery-similar', url: async () => '/discovery?tab=similar', ready: async (p) => { await expect(p.getByTestId('similar-form')).toBeVisible(); } },
  { name: 'network', url: async (p) => `/discovery/network?org=${await orgId(p, 'Disc Hub Holdings Ltd')}&depth=2`, ready: async (p) => { await expect(p.getByTestId('network-graph')).toBeVisible(); await expect(p.getByRole('table', { name: 'Business relationships' })).toBeVisible(); } },
  { name: 'opportunities', url: async () => '/opportunities', ready: async (p) => { await expect(p.getByRole('table', { name: 'Opportunity signals' })).toBeVisible(); await expect(p.getByTestId('signal-facets')).toBeVisible(); } },
  { name: 'data-quality', url: async () => '/data-quality', ready: async (p) => { await expect(p.getByRole('table', { name: /Duplicate companies issues/ })).toBeVisible(); await expect(p.getByTestId('dq-audit-list').or(p.getByText('No data-quality actions recorded yet.'))).toBeVisible(); } },
  { name: 'data-quality-missing', url: async () => '/data-quality?tab=missing_websites', ready: async (p) => { await expect(p.getByRole('table', { name: /Missing websites issues/ })).toBeVisible(); } },
  { name: 'business-completeness', url: async (p) => `/businesses/${await orgId(p, 'Disc Completeness Probe Ltd')}`, ready: async (p) => { await p.getByTestId('completeness-tab').click(); await expect(p.getByTestId('completeness-panel')).toBeVisible(); } },
];

for (const theme of ['light', 'dark'] as const) {
  for (const pg of PAGES) {
    test(`axe ${pg.name} (${theme})`, async ({ page }) => {
      const done = trackPageErrors(page);
      await setTheme(page, theme);
      await login(page, 'admin');
      await page.goto(await pg.url(page));
      await pg.ready(page);
      if (theme === 'dark') await expect(page.locator('html')).toHaveClass(/dark/);
      await expectNoSeriousA11y(page);
      done();
    });
  }
}

test.describe('375px', () => {
  test.use({ viewport: { width: 375, height: 800 } });
  for (const pg of PAGES) {
    test(`no horizontal scroll: ${pg.name}`, async ({ page }) => {
      const done = trackPageErrors(page);
      await login(page, 'admin');
      await page.goto(await pg.url(page));
      await pg.ready(page);
      await page.waitForLoadState('networkidle');
      await expectNoHorizontalScroll(page);
      done();
    });
  }
});
