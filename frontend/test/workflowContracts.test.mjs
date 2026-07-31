import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { isFinalQARunning } from '../src/pages/finalQaState.ts';

test('Final QA recovery states use the backend lifecycle contract', () => {
  assert.equal(isFinalQARunning('recovery_required'), true);
  assert.equal(isFinalQARunning('recovery_blocked'), false);
  assert.equal(isFinalQARunning('passed'), false);
});

test('PhaseBoard does not bypass the locked phase coordinator', () => {
  const source = readFileSync(
    new URL('../src/pages/PhaseBoard/index.tsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(source, /agents\/\$\{[^}]+\}\/execute/);
  assert.match(source, /phases\/\$\{phaseId\}\/auto-repair/);
});

test('ProjectAgents disables legacy execution for phase-owned agents', () => {
  const source = readFileSync(
    new URL('../src/pages/ProjectAgents.tsx', import.meta.url),
    'utf8',
  );
  assert.match(source, /disabled=\{Boolean\(record\.phase_id\)\}/);
  assert.match(source, /disabled=\{hasPhaseOwnedAgents\}/);
});

test('signoff client exposes status and mutation as separate gates', () => {
  const source = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf8');
  assert.match(source, /signoff\/status/);
  assert.match(source, /post\(`\/projects\/\$\{projectId\}\/signoff`\)/);
});
