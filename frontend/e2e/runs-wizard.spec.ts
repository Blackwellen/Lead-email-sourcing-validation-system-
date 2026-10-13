import { test, expect } from '@playwright/test';
import { apiLogin, fillWizardToStep3, hasBackend, uiLogin, uniqueName } from './runs-helpers';

test.skip(!hasBackend, 'E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD not set (needs a real backend)');

test.describe('run wizard (real backend)', () => {
  test('login through the form, then create a category-based run through all 5 steps with an imported-domain source and start it', async ({ page }) => {
    const name = uniqueName('E2E category run');
    await uiLogin(page);
    await page.goto('/runs/new');
    await expect(page.getByRole('heading', { name: 'New sourcing run', level: 1 })).toBeVisible();
    await expect(page.getByTestId('step-heading')).toContainText('Step 1 of 5: What businesses');

    // Step 1: name, countries, regions, category tree (search), keywords
    await page.getByLabel('Run name').fill(name);
    await page.getByRole('combobox', { name: 'Countries' }).click();
    await page.getByRole('combobox', { name: 'Search countries…' }).fill('Ireland');
    await page.getByRole('option', { name: /Ireland \(IE\)/ }).click();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('list', { name: 'Selected values' }).first()).toContainText('Ireland');

    await page.getByRole('searchbox', { name: 'Search categories' }).fill('electrical');
    await page.getByLabel('Commercial electrical contractors').check();
    await expect(page.getByRole('list', { name: 'Selected categories' })).toContainText('Commercial electrical contractors');
    await page.getByRole('combobox', { name: 'Keywords' }).click();
    await page.getByPlaceholder('Type and press Enter').first().fill('rewiring');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('complementary', { name: 'Targeting summary' })).toContainText('rewiring');
    await expect(page.getByRole('complementary', { name: 'Targeting summary' })).toContainText('Commercial electrical contractors');
    await page.getByRole('button', { name: /^Next:/ }).click();

    // Step 2: sources load from the real registry; recommended preset; imported domains
    await expect(page.getByTestId('step-heading')).toContainText('Step 2 of 5');
    await expect(page.getByRole('list', { name: 'Sources' })).toBeVisible();
    await expect(page.getByText('Coverage plan')).toBeVisible();
    await page.getByLabel('Domains', { exact: true }).fill('example.com\nexample.org\nwww.example.net/about');
    await expect(page.getByText('3 valid domains')).toBeVisible();
    await page.getByRole('button', { name: /^Next:/ }).click();

    // Step 3: contacts
    await expect(page.getByTestId('step-heading')).toContainText('Step 3 of 5');
    await page.getByLabel('Target number').fill('5');
    await page.getByLabel('Several per company, with a maximum').check();
    await page.getByLabel('Maximum contacts per company').fill('2');
    await expect(page.getByRole('complementary', { name: 'Expected trade-offs' })).toContainText('From the estimate');
    await page.getByRole('button', { name: /^Next:/ }).click();

    // Step 4: validation + speed; Advanced discovery is collapsed by default
    await expect(page.getByTestId('step-heading')).toContainText('Step 4 of 5');
    await expect(page.getByText('Desired is not measured.')).toBeVisible();
    await expect(page.getByLabel('Batch size')).toBeHidden();
    await page.getByRole('button', { name: /Advanced discovery/ }).click();
    await expect(page.getByRole('radio', { name: /Balanced/ }).first()).toBeVisible();
    await page.getByRole('button', { name: /^Next:/ }).click();

    // Step 5: review + start
    await expect(page.getByTestId('step-heading')).toContainText('Step 5 of 5');
    await expect(page.getByText('Estimated infrastructure and expectations')).toBeVisible();
    await expect(page.getByText(name).first()).toBeVisible();
    await expect(page.getByText('Data restrictions and warnings')).toBeVisible();
    const start = page.getByRole('button', { name: 'Start run' });
    await expect(start).toBeEnabled({ timeout: 15_000 });
    await start.click();
    await expect(page).toHaveURL(/\/runs\/\d+$/);
    await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible();
    await expect(page.getByTestId('run-progress')).toContainText('of 5');
  });

  test('validation errors are displayed per step (client and server side)', async ({ page }) => {
    await apiLogin(page);
    await page.goto('/runs/new');
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByText('Give the run a name')).toBeVisible();
    await expect(page.getByTestId('step-heading')).toContainText('Step 1 of 5');

    await page.getByLabel('Run name').fill(uniqueName('E2E validation'));
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByTestId('step-heading')).toContainText('Step 2 of 5');
    // neither a source nor domains
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByText('Choose at least one source or import a list of domains.')).toBeVisible();
    // invalid domains
    await page.getByLabel('Domains', { exact: true }).fill('example.com\nnot a domain!!\nhttp://');
    await expect(page.getByText(/invalid/).first()).toBeVisible();
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByText(/entries are not valid domains|entry is not a valid domain/)).toBeVisible();
    await expect(page.getByTestId('step-heading')).toContainText('Step 2 of 5');
    await page.getByLabel('Domains', { exact: true }).fill('example.com');
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByTestId('step-heading')).toContainText('Step 3 of 5');
    // numeric validation
    await page.getByLabel('Target number').fill('0');
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByText('Must be at least 1').first()).toBeVisible();
    await expect(page.getByTestId('step-heading')).toContainText('Step 3 of 5');
    // schedule in the past is rejected before start
    await page.getByLabel('Target number').fill('3');
    await page.getByRole('button', { name: /^Next:/ }).click();
    await page.getByLabel('Start at (optional)').fill('2020-01-01T10:00');
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByText('The scheduled time must be in the future.').first()).toBeVisible();
  });

  test('stepper is keyboard accessible and can jump back to completed steps', async ({ page }) => {
    await apiLogin(page);
    await page.goto('/runs/new');
    await fillWizardToStep3(page, uniqueName('E2E keyboard'));
    const nav = page.getByRole('navigation', { name: 'Wizard steps' });
    await expect(nav.getByRole('button').nth(2)).toHaveAttribute('aria-current', 'step');
    await nav.getByRole('button').nth(0).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('step-heading')).toContainText('Step 1 of 5');
    await expect(nav.getByRole('button').nth(0)).toHaveAttribute('aria-current', 'step');
  });

  test('draft autosave: server draft is created, recovered via ?draft= and offered as "Resume draft"', async ({ page }) => {
    await apiLogin(page);
    const name = uniqueName('E2E draft');
    await page.goto('/runs/new');
    await fillWizardToStep3(page, name, 'draft-example.com');
    await expect(page.getByTestId('save-status')).toContainText('Draft saved', { timeout: 15_000 });
    await expect(page).toHaveURL(/\/runs\/new\?draft=\d+/);
    const draftUrl = page.url();

    // reload on the draft URL restores the form
    await page.goto(draftUrl);
    await expect(page.getByLabel('Run name')).toHaveValue(name);
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByLabel('Domains', { exact: true })).toHaveValue('draft-example.com');

    // opening a fresh wizard offers to resume the latest draft
    await page.goto('/runs/new');
    const prompt = page.getByRole('region', { name: 'Resume draft' });
    await expect(prompt).toContainText(name);
    await prompt.getByRole('button', { name: 'Resume draft' }).click();
    await expect(page).toHaveURL(/\/runs\/new\?draft=\d+/);
    await expect(page.getByLabel('Run name')).toHaveValue(name);

    // the draft shows in the Drafts tab
    await page.goto('/runs?tab=drafts');
    await expect(page.getByRole('link', { name })).toBeVisible();
  });

  test('save as preset and load it at the start of a new run', async ({ page }) => {
    await apiLogin(page);
    const presetName = uniqueName('E2E preset');
    await page.goto('/runs/new');
    await fillWizardToStep3(page, uniqueName('E2E preset source'));
    await page.getByRole('button', { name: /^Next:/ }).click();
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByTestId('step-heading')).toContainText('Step 5 of 5');
    await page.getByRole('button', { name: 'Save as preset' }).click();
    await page.getByLabel('Preset name').fill(presetName);
    await page.getByRole('button', { name: 'Save preset' }).click();
    await expect(page.getByText(`Preset “${presetName}” saved`)).toBeVisible();

    await page.goto('/runs/new');
    await page.getByRole('combobox', { name: 'Load preset' }).click();
    await page.getByRole('option', { name: presetName }).click();
    await expect(page.getByText(`Loaded preset “${presetName}”`)).toBeVisible();
    await page.getByRole('button', { name: /^Next:/ }).click();
    await expect(page.getByLabel('Domains', { exact: true })).toHaveValue('example.com\nexample.org');
  });

  test('save draft button works from the review step', async ({ page }) => {
    await apiLogin(page);
    await page.goto('/runs/new');
    await fillWizardToStep3(page, uniqueName('E2E manual draft'));
    await page.getByRole('button', { name: /^Next:/ }).click();
    await page.getByRole('button', { name: /^Next:/ }).click();
    await page.getByRole('button', { name: 'Save draft' }).click();
    await expect(page.getByText('Draft saved').first()).toBeVisible();
  });
});
