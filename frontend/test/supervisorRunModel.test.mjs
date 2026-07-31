import assert from 'node:assert/strict';
import test from 'node:test';

import {
  completionGateProblems,
  failedCriticalTasks,
  normalizeSupervisorRun,
  roundHasNewBlocker,
  supervisorRunFromStatus,
  SUPERVISOR_QA_ROUND_LIMIT,
} from '../src/pages/PhaseBoard/supervisorRunModel.ts';

test('adapts top-level supervisor fields without inventing extra QA rounds', () => {
  const run = supervisorRunFromStatus({
    phase_id: 'phase-1',
    running: true,
    round: 2,
    status: 'running',
    total_rounds: 2,
    messages: [],
    needs_manual: false,
    issue_report: {},
    supervisor_state: 'waiting_engineer',
    waiting_for: { type: 'engineer', id: 'eng-1' },
  });

  assert.equal(run?.state, 'waiting_engineer');
  assert.equal(run?.qa_round, 2);
  assert.equal(run?.max_qa_rounds, SUPERVISOR_QA_ROUND_LIMIT);
});

test('normalizes persisted backend aliases without losing the active round', () => {
  const run = normalizeSupervisorRun({
    run_id: 'run-persisted',
    phase_id: 'phase-1',
    status: 'verifying',
    state: '',
    active: true,
    business_rounds_used: 3,
    active_qa_round_id: 'qa-4',
    max_qa_rounds: 5,
    rounds: [{ qa_round_id: 'qa-4', round_number: 4, state: 'qa_running' }],
  });
  assert.equal(run.state, 'verifying');
  assert.equal(run.qa_round, 4);
  assert.equal(run.qa_round_id, 'qa-4');
});

test('treats authoritative new issue count as an immediate blocker', () => {
  assert.equal(roundHasNewBlocker({
    qa_round_id: 'qa-2',
    round_number: 2,
    state: 'qa_running',
    counts: { total: 2, blocking: 1, remaining: 1, new: 1, repeated: 0 },
    issues: [],
  }), true);
});

test('critical failed, timeout, blocked, and cancelled agents fail the gate', () => {
  const tasks = ['failed', 'timeout', 'blocked', 'cancelled', 'succeeded'].map((status, index) => ({
    agent_id: `agent-${index}`,
    status,
    critical: true,
  }));
  assert.deepEqual(failedCriticalTasks(tasks).map(task => task.status), ['failed', 'timeout', 'blocked', 'cancelled']);
});

test('completed state still exposes missing evidence and unresolved blockers', () => {
  const problems = completionGateProblems({
    run_id: 'run-1',
    phase_id: 'phase-1',
    state: 'completed',
    active: false,
    qa_round: 5,
    max_qa_rounds: 5,
    rounds: [{
      qa_round_id: 'qa-5',
      round_number: 5,
      state: 'completed',
      counts: { blocking: 1, remaining: 1 },
      evidence: { commands: [] },
      agent_tasks: [{ agent_id: 'qa-agent', status: 'timeout', critical: true }],
    }],
  });

  assert.ok(problems.includes('缺少真实验证命令'));
  assert.ok(problems.includes('仍有未解决阻断'));
  assert.ok(problems.includes('存在关键 Agent 失败'));
});

test('a completed run with successful commands and no blockers passes the display gate', () => {
  const problems = completionGateProblems({
    run_id: 'run-2',
    phase_id: 'phase-1',
    state: 'completed',
    active: false,
    qa_round: 1,
    max_qa_rounds: 5,
    rounds: [{
      qa_round_id: 'qa-1',
      round_number: 1,
      state: 'completed',
      counts: { blocking: 0, remaining: 0 },
      evidence: { commands: [{ command: 'npm test', exit_code: 0, kind: 'test' }] },
      agent_tasks: [{ agent_id: 'qa-agent', status: 'succeeded', critical: true }],
    }],
  });

  assert.deepEqual(problems, []);
});
