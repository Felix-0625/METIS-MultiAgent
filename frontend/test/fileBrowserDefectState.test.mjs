import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildRepairApplyTarget,
  canRepairDefect,
  canonicalUiPath,
  countDefectFilesUnder,
  defectStatusPresentation,
} from '../src/pages/fileBrowserDefectState.ts'

const high = { identity_confidence: 'high', requires_identity_review: false }

test('only server-authorized open and needs_manual defects are actionable', () => {
  assert.equal(canRepairDefect({ ...high, status: 'open', action_allowed: true }), true)
  assert.equal(canRepairDefect({ ...high, status: 'needs_manual', action_allowed: true }), true)
  assert.equal(canRepairDefect({ ...high, status: 'fixing', action_allowed: false }), false)
  assert.equal(canRepairDefect({
    ...high,
    status: 'pending_verification',
    action_allowed: false,
  }), false)
  assert.equal(canRepairDefect({
    status: 'needs_manual',
    action_allowed: false,
    identity_confidence: 'low',
    requires_identity_review: true,
  }), false)
})

test('presents open, needs_manual, fixing and pending verification distinctly', () => {
  assert.match(defectStatusPresentation([
    { ...high, status: 'open', action_allowed: true },
  ]).label, /待处理/)
  assert.match(defectStatusPresentation([
    { ...high, status: 'needs_manual', action_allowed: true },
  ]).label, /需人工整改/)
  assert.match(defectStatusPresentation([
    { ...high, status: 'fixing', action_allowed: false },
  ]).label, /处理中/)
  assert.match(defectStatusPresentation([
    { ...high, status: 'pending_verification', action_allowed: false },
  ]).label, /权威复检/)
})

test('canonical recursive directory counts include slash variants only in scope', () => {
  const paths = [
    'backend\\api\\routes.py',
    './backend/core/state.py',
    'frontend/src/App.tsx',
  ]
  assert.equal(canonicalUiPath('./backend\\api\\routes.py'), 'backend/api/routes.py')
  assert.equal(countDefectFilesUnder(paths, 'backend'), 2)
  assert.equal(countDefectFilesUnder(paths, 'backend/api'), 1)
  assert.equal(countDefectFilesUnder(paths, 'src'), 0)
})

test('apply target stays bound to the selected canonical defect on one file', () => {
  const symbolA = { id: 'issue-symbol-a', file_path: 'src/shared.py' }
  const symbolB = { id: 'issue-symbol-b', file_path: 'src/shared.py' }
  const plans = {
    'issue-symbol-a': 'plan-a',
    'issue-symbol-b': 'plan-b',
  }
  assert.deepEqual(buildRepairApplyTarget(symbolA, plans), {
    defect_id: 'issue-symbol-a',
    file_path: 'src/shared.py',
    confirmed_plan_version: 'plan-a',
  })
  assert.deepEqual(buildRepairApplyTarget(symbolB, plans), {
    defect_id: 'issue-symbol-b',
    file_path: 'src/shared.py',
    confirmed_plan_version: 'plan-b',
  })
})
