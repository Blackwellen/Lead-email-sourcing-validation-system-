import { defineConfig, devices } from '@playwright/test';
import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';

/** Prefer the sandbox's preinstalled Chromium (/opt/pw-browsers) when present; otherwise Playwright's own download. */
function findChromium(): string | undefined {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
  const candidates = [
    path.join(root, 'chromium', 'chrome'),
    path.join(root, 'chromium', 'chrome-linux', 'chrome'),
  ];
  try {
    for (const d of readdirSync(root)) {
      if (d.startsWith('chromium-')) candidates.push(path.join(root, d, 'chrome-linux', 'chrome'));
    }
  } catch { /* directory missing */ }
  return candidates.find((c) => existsSync(c));
}

const executablePath = findChromium();
const baseURL = process.env.E2E_BASE_URL || 'http://localhost:3000';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    launchOptions: { executablePath, args: ['--no-sandbox'] },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // Reuse a running dev server; otherwise start one (the backend is NOT required for foundation pages).
  webServer: process.env.E2E_NO_SERVER ? undefined : {
    command: 'npm run dev',
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
