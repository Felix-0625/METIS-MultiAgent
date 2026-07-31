import { chromium } from 'playwright';

const base = process.env.METIS_E2E_WEB || 'https://metis-cho3.onrender.com';
const login = process.env.METIS_E2E_LOGIN;
const password = process.env.METIS_E2E_PASSWORD;
if (!login || !password) throw new Error('Missing E2E credentials');

const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
const projectName = `UI-Heavy-E2E-${stamp}`;
const requirements = [
  '创建一个可直接打开运行的响应式番茄钟与任务管理单页应用。',
  '总规划只能包含 1 个阶段，只能安排 1 个全栈工程师，agent_count 必须为 1；禁止拆分测试、前端或后端专家。',
  '必须生成完整可运行文件，至少包含 index.html、README.md；使用原生 HTML/CSS/JavaScript，不依赖构建工具或外部服务。',
  '用户可新增、完成、删除任务；计时器支持开始、暂停、重置；数据使用 localStorage 持久化。',
  '不得只交付设计、伪代码、TODO 或静态截图。',
].join('');

const browser = await chromium.launch({
  executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const consoleErrors = [];
page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
page.on('pageerror', error => consoleErrors.push(`pageerror: ${error.message}`));

const shot = async name => {
  await page.screenshot({ path: `test-results/${name}.png`, fullPage: true });
};

try {
  await page.goto(`${base}/app/login`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.getByPlaceholder('用户名或邮箱').fill(login);
  await page.getByPlaceholder('密码').fill(password);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/app\/projects(?:$|\?)/, { timeout: 180_000 });
  console.log('PASS browser login');

  await page.locator('button').filter({ hasText: '新建项目' }).first().click();
  await page.getByLabel('项目名称').fill(projectName);
  await page.getByLabel('项目描述').fill(requirements);
  await Promise.all([
    page.waitForURL(/\/app\/projects\/[^/]+\/pm-team/, { timeout: 180_000 }),
    page.locator('button').filter({ hasText: '创建并进入 PM 对话' }).click(),
  ]);
  const projectId = page.url().match(/\/projects\/([^/]+)\//)?.[1];
  if (!projectId) throw new Error(`Cannot parse project id from ${page.url()}`);
  console.log(`PROJECT ${projectId}`);
  await shot('01-project-created');

  const input = page.getByPlaceholder(/描述项目需求/);
  await input.fill(requirements);
  await page.locator('button').filter({ hasText: /^发送$/ }).click();
  const generatePlan = page.locator('button').filter({ hasText: '生成总规划草稿' });
  await generatePlan.waitFor({ state: 'visible' });
  await generatePlan.click();
  const confirmPlan = page.locator('button').filter({ hasText: '确认总规划' });
  await confirmPlan.waitFor({ state: 'visible', timeout: 600_000 });
  await shot('02-plan-generated');
  await confirmPlan.click();
  await page.waitForURL(/\/app\/projects\/[^/]+\/phase-board/, { timeout: 300_000 });
  console.log('PASS PM plan confirmed');

  let startPhase = page.locator('button').filter({ hasText: '启动阶段' }).first();
  if (await startPhase.count() === 0) {
    await page.locator('button').filter({ hasText: '阶段PM' }).first().click();
    const generatePhasePlan = page.locator('button').filter({ hasText: '生成阶段规划' });
    await generatePhasePlan.waitFor({ state: 'visible', timeout: 180_000 });
    await generatePhasePlan.click();
    startPhase = page.locator('button').filter({ hasText: '开始执行阶段' });
    await startPhase.waitFor({ state: 'visible', timeout: 600_000 });
    await startPhase.waitFor({ state: 'attached' });
  } else {
    await startPhase.waitFor({ state: 'visible', timeout: 180_000 });
  }
  await startPhase.click();
  await page.getByText(/阶段已启动/).waitFor({ state: 'visible', timeout: 180_000 }).catch(() => {});
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.locator('button').filter({ hasText: '质检循环' }).first().waitFor({ state: 'visible', timeout: 180_000 });
  console.log('PASS phase start and refresh recovery');

  const phaseData = await page.evaluate(async pid => {
    const response = await fetch(`/api/projects/${pid}/phases`, { credentials: 'include' });
    return { ok: response.ok, status: response.status, body: await response.json() };
  }, projectId);
  if (!phaseData.ok || !phaseData.body.phases?.length) throw new Error(`No phases: ${JSON.stringify(phaseData)}`);
  const phaseId = phaseData.body.phases[0].phase_id;

  const chooseManual = await page.evaluate(async ({ pid, phase }) => {
    const response = await fetch(`/api/projects/${pid}/phases/${phase}/auto-repair?user_decision=manual_fix`, {
      method: 'POST', credentials: 'include',
    });
    return { ok: response.ok, status: response.status, body: await response.json() };
  }, { pid: projectId, phase: phaseId });
  if (!chooseManual.ok || chooseManual.body.status?.status !== 'awaiting_manual_fix') {
    throw new Error(`manual_fix failed: ${JSON.stringify(chooseManual)}`);
  }
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  const manual = page.locator('[data-testid="qc-action-manual-fix"], button:has-text("自行修改")').first();
  const retry = page.locator('[data-testid="qc-action-retry-cycle"], button:has-text("继续质检循环")').first();
  const rebuild = page.locator('[data-testid="qc-action-rebuild-phase"], button:has-text("阶段重构")').first();
  await Promise.all([
    manual.waitFor({ state: 'visible', timeout: 180_000 }),
    retry.waitFor({ state: 'visible', timeout: 180_000 }),
    rebuild.waitFor({ state: 'visible', timeout: 180_000 }),
  ]);
  await shot('03-three-decisions-restored');
  console.log('PASS three decisions and awaiting_manual_fix refresh recovery');

  await manual.click();
  await page.waitForURL(new RegExp(`/app/engineer/${projectId}`), { timeout: 180_000 });
  console.log('PASS manual fix navigation');

  await page.goto(`${base}/app/projects/${projectId}/phase-board`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
  await retry.waitFor({ state: 'visible', timeout: 180_000 });
  await retry.click();
  await retry.waitFor({ state: 'hidden', timeout: 180_000 });
  console.log('PASS continue quality loop');

  const manualAgain = await page.evaluate(async ({ pid, phase }) => {
    const response = await fetch(`/api/projects/${pid}/phases/${phase}/auto-repair?user_decision=manual_fix`, {
      method: 'POST', credentials: 'include',
    });
    return { ok: response.ok, status: response.status, body: await response.json() };
  }, { pid: projectId, phase: phaseId });
  if (!manualAgain.ok) throw new Error(`manual_fix before rebuild failed: ${JSON.stringify(manualAgain)}`);
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  await rebuild.waitFor({ state: 'visible', timeout: 180_000 });
  await rebuild.click();
  const confirmRebuild = page.locator('button').filter({ hasText: '确认重构' });
  await confirmRebuild.waitFor({ state: 'visible' });
  await confirmRebuild.click();
  await confirmRebuild.waitFor({ state: 'hidden', timeout: 300_000 });
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.getByText(/执行失败/).waitFor({ state: 'visible', timeout: 5_000 }).then(() => {
    throw new Error('Rebuilt phase contains failed agents');
  }).catch(error => {
    if (error.message === 'Rebuilt phase contains failed agents') throw error;
  });
  await shot('04-rebuild-started');
  console.log('PASS phase rebuild started without immediate failed agents');
  console.log(`BROWSER_E2E_PASSED project=${projectId} phase=${phaseId} consoleErrors=${consoleErrors.length}`);
  if (consoleErrors.length) console.log(`BROWSER_CONSOLE_ERRORS ${JSON.stringify(consoleErrors.slice(0, 20))}`);
} catch (error) {
  await shot('failure').catch(() => {});
  console.error(`BROWSER_E2E_FAILED ${error.stack || error}`);
  if (consoleErrors.length) console.error(`BROWSER_CONSOLE_ERRORS ${JSON.stringify(consoleErrors.slice(0, 20))}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
