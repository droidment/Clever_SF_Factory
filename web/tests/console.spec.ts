import { test, expect } from '@playwright/test';

test('guide deep link and demo account switching update visible permissions', async ({ page }) => {
  let actor = 'Public visitor';
  const roles: Record<string, string[]> = {creator:['viewer','requester'], approver:['viewer','approver'], admin:['admin','viewer','requester','approver','operator'], viewer:['viewer']};
  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    await route.fulfill({json:{...(await response.json()), mode:'anthropic', model:'claude-sonnet-5', actor,
      roles:roles[actor.replace('demo:', '')] || ['viewer'], demo_login_enabled:true, draft_review_required:true,
      demo_accounts:[{username:'creator',label:'Ticket Creator'},{username:'approver',label:'Approver'},{username:'admin',label:'Admin'},{username:'viewer',label:'Viewer'}]}});
  });
  await page.route('**/api/session/login', async route => { actor = 'demo:' + route.request().postDataJSON().username; await route.fulfill({json:{ok:true}}); });
  await page.goto('/#how-it-works');
  await expect(page.getByRole('heading', {name:'How it works',exact:true})).toBeVisible();
  await expect(page.getByRole('heading', {name:'The durable runtime is built, but is not running here'})).toBeVisible();
  await page.getByRole('tab', {name:/Review/}).click();
  await expect(page.getByRole('tabpanel')).toContainText('different account');
  await page.getByRole('button', {name:'Overview',exact:true}).click();
  for (const name of ['creator', 'approver', 'admin', 'viewer']) {
    await page.getByRole('button', {name:/Switch account/}).click();
    await page.getByRole('combobox', {name:'User ID',exact:true}).selectOption(name);
    await page.getByLabel('Shared demo password', {exact:true}).fill('test-password');
    await page.getByRole('button', {name:'Sign in',exact:true}).click();
    await expect(page.getByRole('dialog')).not.toBeVisible();
    if (name === 'creator' || name === 'admin') await expect(page.getByRole('button', {name:'New request',exact:true})).toBeVisible();
    else await expect(page.getByRole('button', {name:'New request',exact:true})).not.toBeVisible();
  }
});

test('Anthropic workspace discloses paid drafts and disables deployment', async ({ page }) => {
  // This is a UI fixture, not a live provider call. Backend inference is exercised
  // separately through the actual Anthropic SDK with a mocked HTTP transport.
  await page.route('**/api/config', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ json: {
      ...(await response.json()), mode: 'anthropic', model: 'claude-sonnet-5', draft_only: true,
    } });
  });
  await page.goto('/');
  await expect(page.getByText('CLAUDE · DRAFTS', { exact: true })).toBeVisible();
  await expect(page.getByText(/API usage is billed/)).toBeVisible();
  await expect(page.getByText('AI drafts ready', { exact: true })).toBeVisible();
  await expect(page.getByText('Verified completions', { exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: 'New request', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: /Include deployment/ })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.screenshot({ path: 'test-results/anthropic-draft-workspace.png', fullPage: true });
});

test('overview renders measured data and a registry', async ({ page, request }) => {
  await expect
    .poll(async () => {
      try {
        return (await request.get('/healthz')).status();
      } catch {
        return 0;
      }
    })
    .toBe(200);
  await request.post('/api/demo/seed');
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Factory overview' })).toBeVisible();
  await expect(page.getByText('Customer portal activity feed', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Agent registry', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Agent registry', exact: true })).toBeVisible();
  await page.getByText('Backend agent', { exact: true }).click();
  await expect(
    page.locator('details[open]').getByText('repo:read, branch:write', { exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('intake progresses to a simulated result and displays evidence', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'New request', exact: true }).click();
  const title = `Browser regression ${Date.now()}`;
  await page.getByLabel('Request title', { exact: true }).fill(title);
  await page
    .getByLabel('Description', { exact: true })
    .fill('Fix the regression in the order callback handler and create verifiable test evidence.');
  await page
    .getByLabel(/^Acceptance criteria/)
    .fill('Duplicate callbacks are idempotent\nRegression tests pass');
  await page.getByRole('button', { name: 'Submit request', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByText('Simulation finished — no code was delivered', { exact: true })).toBeVisible({
    timeout: 35000,
  });
  await page.getByRole('tab', { name: /Artifacts/ }).click();
  await page.getByText('test-report.md', { exact: true }).click();
  await expect(
    page.locator('details[open]').getByText(/No repository, model, build system, or deployment was accessed/),
  ).toBeVisible();
  await page.getByRole('tab', { name: 'Execution plan', exact: true }).click();
  await page.screenshot({ path: 'test-results/workflow-desktop.png', fullPage: true });
});

test('clarification is submitted and workflow resumes', async ({ page, request }) => {
  const title = `Clarify browser ${Date.now()}`;
  const response = await request.post('/api/requests', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: {
      title,
      description: 'Investigate the callback handler and determine requirements for a safe fix.',
      requestor: 'Browser test',
      repository: 'platform/order-service',
    },
  });
  const { request_id } = await response.json();
  await expect
    .poll(async () => (await (await request.get(`/api/requests/${request_id}`)).json()).status)
    .toBe('waiting_for_human');
  await page.goto('/');
  await page.getByText(title, { exact: true }).first().click();
  await page
    .getByLabel('Acceptance criteria · one per line', { exact: true })
    .fill('Report identifies the root cause');
  await page
    .getByLabel('Decision notes', { exact: true })
    .fill('Investigate the duplicate callback path and provide evidence.');
  await page.getByRole('button', { name: 'Submit clarification', exact: true }).click();
  await expect(page.getByText('Simulation finished — no code was delivered', { exact: true })).toBeVisible({
    timeout: 35000,
  });
});

test('release decision resumes an approved workflow', async ({ page, request }) => {
  const title = `Release browser ${Date.now()}`;
  const response = await request.post('/api/requests', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: {
      title,
      description: 'Fix the callback retry behavior, test it, and release the validated change.',
      requestor: 'Browser test',
      repository: 'platform/order-service',
      acceptance_criteria: ['Callbacks are idempotent'],
      deploy: true,
    },
  });
  const { request_id } = await response.json();
  await expect
    .poll(async () => (await (await request.get(`/api/requests/${request_id}`)).json()).status, {
      timeout: 30000,
    })
    .toBe('waiting_for_human');
  await page.goto('/');
  await page.getByLabel('Search requests').fill(title);
  await page.getByText(title, { exact: true }).first().click();
  await page
    .getByLabel('Decision notes', { exact: true })
    .fill('Reviewed the demonstration artifacts and approve this simulated release.');
  await page.getByRole('button', { name: 'Approve release', exact: true }).click();
  await expect(page.getByText('Simulation finished — no code was delivered', { exact: true })).toBeVisible({
    timeout: 20000,
  });
});

test('mobile layout and overview screenshot', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Factory overview' })).toBeVisible();
  await page.screenshot({ path: 'test-results/overview-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'New request', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'test-results/overview-mobile.png', fullPage: true });
  await page.getByRole('button', { name: 'New request', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
});
