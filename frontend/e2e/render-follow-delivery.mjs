import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';

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
const context = await browser.newContext();
const page = await context.newPage();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

const api = async (path, init = {}) => page.evaluate(async ({ path, init }) => {
  const response = await fetch(path, { credentials: 'include', ...init });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  return { ok: response.ok, status: response.status, body };
}, { path, init });

try {
  await page.goto(`${base}/app/login`, { waitUntil: 'domcontentloaded', timeout: 180_000 });
  await page.getByPlaceholder('用户名或邮箱').fill(login);
  await page.getByPlaceholder('密码').fill(password);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/app\/projects(?:$|\?)/, { timeout: 180_000 });

  const phaseResult = await api(`/api/projects/${projectId}/phases`);
  if (!phaseResult.ok || !phaseResult.body.phases?.length) throw new Error(`phases: ${JSON.stringify(phaseResult)}`);
  const phaseId = phaseResult.body.phases[0].phase_id;
  const agentDeadline = Date.now() + 30 * 60_000;
  let lastStates = '';
  while (Date.now() < agentDeadline) {
    const result = await api(`/api/projects/${projectId}/agents`);
    if (!result.ok) throw new Error(`agents: ${JSON.stringify(result)}`);
    const agents = result.body.agents || [];
    const states = Object.fromEntries(agents.filter(a => a.phase_id === phaseId).map(a => [a.id, a.status]));
    const marker = JSON.stringify(states);
    if (marker !== lastStates) {
      console.log(`AGENTS ${marker}`);
      lastStates = marker;
    }
    const phaseAgents = agents.filter(a => a.phase_id === phaseId);
    if (phaseAgents.length && phaseAgents.every(a => ['completed', 'failed', 'fix_limit_reached', 'cancelled'].includes(a.status))) {
      const failures = phaseAgents.filter(a => a.status !== 'completed');
      if (failures.length) throw new Error(`agent failures: ${JSON.stringify(failures.map(a => ({ id: a.id, status: a.status, error: a.error, message: a.message })))}`);
      if (phaseAgents.some(a => !(a.output_files?.length || a.files_written?.length))) {
        throw new Error(`completed agent missing output files: ${JSON.stringify(phaseAgents)}`);
      }
      console.log(`AGENT_OUTPUTS ${JSON.stringify(phaseAgents.map(a => ({ id: a.id, output_files: a.output_files, files_written: a.files_written, message: a.message })))}`);
      console.log(`PASS agents completed count=${phaseAgents.length}`);
      break;
    }
    await sleep(10_000);
  }
  if (Date.now() >= agentDeadline) throw new Error(`agent timeout: ${lastStates}`);

  let qa = await api(`/api/projects/${projectId}/phases/${phaseId}/auto-repair/status`);
  if (!qa.ok) throw new Error(`qa status: ${JSON.stringify(qa)}`);
  if (!qa.body.running && !['passed', 'awaiting_decision', 'needs_manual'].includes(qa.body.status)) {
    const start = await api(`/api/projects/${projectId}/phases/${phaseId}/auto-repair`, { method: 'POST' });
    if (!start.ok) throw new Error(`qa start: ${JSON.stringify(start)}`);
  }
  const qaDeadline = Date.now() + 30 * 60_000;
  let continued = false;
  let qaMarker = '';
  while (Date.now() < qaDeadline) {
    qa = await api(`/api/projects/${projectId}/phases/${phaseId}/auto-repair/status`);
    if (!qa.ok) throw new Error(`qa poll: ${JSON.stringify(qa)}`);
    const marker = `${qa.body.status}:${qa.body.round}:${qa.body.messages?.length || 0}`;
    if (marker !== qaMarker) { console.log(`QA ${marker}`); qaMarker = marker; }
    if (qa.body.status === 'passed') break;
    if (qa.body.status === 'awaiting_decision' && !continued) {
      const resume = await api(`/api/projects/${projectId}/phases/${phaseId}/auto-repair?user_decision=retry_cycle`, { method: 'POST' });
      if (!resume.ok) throw new Error(`qa continue: ${JSON.stringify(resume)}`);
      continued = true;
    } else if (['needs_manual', 'error', 'failed'].includes(qa.body.status) || (qa.body.status === 'awaiting_decision' && continued)) {
      throw new Error(`qa terminal failure: ${JSON.stringify(qa.body).slice(0, 4000)}`);
    }
    await sleep(8_000);
  }
  if (qa.body.status !== 'passed') throw new Error(`qa timeout: ${JSON.stringify(qa.body).slice(0, 2000)}`);
  console.log('PASS phase QA');

  const confirm = await api(`/api/projects/${projectId}/phases/${phaseId}/confirm-complete`, { method: 'POST' });
  if (!confirm.ok) throw new Error(`confirm phase: ${JSON.stringify(confirm)}`);
  console.log('PASS phase confirmed');

  const filesBeforeFinal = await api(`/api/projects/${projectId}/files`);
  console.log(`PROJECT_FILES ${JSON.stringify(filesBeforeFinal.body).slice(0, 5000)}`);

  const finalStart = await api(`/api/projects/${projectId}/final-qa`, { method: 'POST' });
  if (!finalStart.ok) throw new Error(`final QA start: ${JSON.stringify(finalStart)}`);
  const finalDeadline = Date.now() + 30 * 60_000;
  let finalStatus;
  let finalMarker = '';
  while (Date.now() < finalDeadline) {
    const result = await api(`/api/projects/${projectId}/final-qa/status`);
    if (!result.ok) throw new Error(`final QA poll: ${JSON.stringify(result)}`);
    finalStatus = result.body;
    const marker = `${finalStatus.status}:${finalStatus.round}:${finalStatus.completed_items}/${finalStatus.total_items}`;
    if (marker !== finalMarker) { console.log(`FINAL_QA ${marker}`); finalMarker = marker; }
    if (finalStatus.status === 'passed' && finalStatus.all_passed !== false) break;
    if (['needs_manual', 'error', 'failed'].includes(finalStatus.status)) throw new Error(`final QA failed: ${JSON.stringify(finalStatus).slice(0, 4000)}`);
    await sleep(8_000);
  }
  if (finalStatus?.status !== 'passed') throw new Error(`final QA timeout: ${JSON.stringify(finalStatus)}`);
  console.log('PASS final QA');

  const archive = await context.request.get(`${base}/api/projects/${projectId}/archive/download`);
  if (!archive.ok()) throw new Error(`archive HTTP ${archive.status()}: ${(await archive.text()).slice(0, 1000)}`);
  const bytes = await archive.body();
  if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('archive is not ZIP');
  await mkdir('test-results', { recursive: true });
  const archivePath = `test-results/${projectId}.zip`;
  await writeFile(archivePath, bytes);
  console.log(`DELIVERY_ARCHIVE ${archivePath} bytes=${bytes.length}`);
  console.log(`FOLLOW_DELIVERY_PASSED project=${projectId} phase=${phaseId}`);
} catch (error) {
  console.error(`FOLLOW_DELIVERY_FAILED ${error.stack || error}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
