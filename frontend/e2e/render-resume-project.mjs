import { chromium } from 'playwright';

const base = process.env.METIS_E2E_WEB;
const login = process.env.METIS_E2E_LOGIN;
const password = process.env.METIS_E2E_PASSWORD;
const projectId = process.env.METIS_E2E_PROJECT_ID;
if (!base || !login || !password || !projectId) throw new Error('Missing E2E runtime input');

const browser = await chromium.launch({
  executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const shot = name => page.screenshot({ path: `test-results/${name}.png`, fullPage: true });

try {
  await page.goto(`${base}/app/login`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.getByPlaceholder('用户名或邮箱').fill(login);
  await page.getByPlaceholder('密码').fill(password);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/app\/projects(?:$|\?)/, { timeout: 180_000 });
  await page.goto(`${base}/app/projects/${projectId}/phase-board`, { waitUntil: 'domcontentloaded', timeout: 180_000 });

  let phaseData = await page.evaluate(async pid => {
    const response = await fetch(`/api/projects/${pid}/phases`, { credentials: 'include' });
    return { ok: response.ok, status: response.status, body: await response.json() };
  }, projectId);
  if (!phaseData.ok || !phaseData.body.phases?.length) throw new Error(`No phases: ${JSON.stringify(phaseData)}`);
  const phaseId = phaseData.body.phases[0].phase_id;
  const phase = phaseData.body.phases[0];

  if (!phase.started_at && !(phase.agents || []).length && !(phase.agent_details || []).length) {
    const directStart = page.locator('button').filter({ hasText: '启动阶段' }).first();
    let startResponse;
    if (await directStart.count()) {
      [startResponse] = await Promise.all([
        page.waitForResponse(response => response.url().includes(`/phases/${phaseId}/start`), { timeout: 600_000 }),
        directStart.click({ timeout: 180_000 }),
      ]);
    } else {
      await page.locator('button').filter({ hasText: '阶段PM' }).first().click();
      const execute = page.locator('button').filter({ hasText: '开始执行阶段' });
      await execute.waitFor({ state: 'visible', timeout: 180_000 });
      if (!await execute.isEnabled()) {
        const generate = page.locator('button').filter({ hasText: /^生成阶段规划$/ });
        await generate.waitFor({ state: 'visible', timeout: 180_000 });
        await generate.click();
      }
      [startResponse] = await Promise.all([
        page.waitForResponse(response => response.url().includes(`/phases/${phaseId}/start`), { timeout: 600_000 }),
        execute.click({ timeout: 600_000 }),
      ]);
    }
    const startBody = await startResponse.text();
    console.log(`START_RESPONSE status=${startResponse.status()} body=${startBody.slice(0, 1000)}`);
    if (!startResponse.ok()) throw new Error(`Phase start HTTP ${startResponse.status()}: ${startBody.slice(0, 1000)}`);
    await page.waitForTimeout(3_000);
  }
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.getByText('项目质量指标总览').waitFor({ state: 'visible', timeout: 180_000 });
  await shot('05-phase-started-refreshed');
  phaseData = await page.evaluate(async pid => (await fetch(`/api/projects/${pid}/phases`, { credentials: 'include' })).json(), projectId);
  const refreshed = phaseData.phases?.find(item => item.phase_id === phaseId);
  if (!refreshed?.started_at && !(refreshed?.agents || []).length && !(refreshed?.agent_details || []).length) {
    throw new Error(`Phase start was not persisted: ${JSON.stringify(refreshed)}`);
  }
  console.log(`PASS phase start persisted phase=${phaseId}`);

  const manualState = await page.evaluate(async ({ pid, phase }) => {
    const response = await fetch(`/api/projects/${pid}/phases/${phase}/auto-repair?user_decision=manual_fix`, {
      method: 'POST', credentials: 'include',
    });
    return { ok: response.ok, status: response.status, body: await response.json() };
  }, { pid: projectId, phase: phaseId });
  if (!manualState.ok || manualState.body.status?.status !== 'awaiting_manual_fix') {
    throw new Error(`manual_fix backend contract failed: ${JSON.stringify(manualState)}`);
  }
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  const manual = page.locator('[data-testid="qc-action-manual-fix"], button:has-text("自行修改")').first();
  const retry = page.locator('[data-testid="qc-action-retry-cycle"], button:has-text("继续质检循环")').first();
  const rebuild = page.locator('[data-testid="qc-action-rebuild-phase"], button:has-text("阶段重构")').first();
  await manual.waitFor({ state: 'visible', timeout: 180_000 });
  await retry.waitFor({ state: 'visible', timeout: 30_000 });
  await rebuild.waitFor({ state: 'visible', timeout: 30_000 });
  await shot('06-three-decisions');
  console.log('PASS three decision controls restored after refresh');

  await manual.click();
  await page.waitForURL(new RegExp(`/app/engineer/${projectId}`), { timeout: 180_000 });
  console.log('PASS manual fix UI');
  await page.goto(`${base}/app/projects/${projectId}/phase-board`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
  await retry.waitFor({ state: 'visible', timeout: 180_000 });
  await retry.click();
  await retry.waitFor({ state: 'hidden', timeout: 180_000 });
  console.log('PASS retry cycle UI');

  const secondPause = await page.evaluate(async ({ pid, phase }) => {
    const response = await fetch(`/api/projects/${pid}/phases/${phase}/auto-repair?user_decision=manual_fix`, {
      method: 'POST', credentials: 'include',
    });
    return response.ok;
  }, { pid: projectId, phase: phaseId });
  if (!secondPause) throw new Error('Second manual pause failed');
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  await rebuild.waitFor({ state: 'visible', timeout: 180_000 });
  await rebuild.click();
  await page.locator('button').filter({ hasText: '确认重构' }).click();
  await page.waitForTimeout(5_000);
  const rebuildStatus = await page.evaluate(async ({ pid, phase }) => {
    const response = await fetch(`/api/projects/${pid}/phases/${phase}/auto-repair/status`, { credentials: 'include' });
    return response.json();
  }, { pid: projectId, phase: phaseId });
  if (rebuildStatus.status !== 'rebuild_started') throw new Error(`Rebuild status: ${JSON.stringify(rebuildStatus)}`);
  console.log('PASS rebuild phase UI and backend status');
  await shot('07-rebuild-started');
  console.log(`RESUME_E2E_PASSED project=${projectId} phase=${phaseId}`);
} catch (error) {
  await shot('resume-failure').catch(() => {});
  console.error(`RESUME_E2E_FAILED ${error.stack || error}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
