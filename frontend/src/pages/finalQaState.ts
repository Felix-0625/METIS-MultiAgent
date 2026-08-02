export const FINAL_QA_TERMINAL_STATUSES = new Set([
  'passed',
  'needs_manual',
  'awaiting_engineer_repair',
  'failed_recovery',
  'failed',
  'infrastructure_blocked',
  'interrupted',
  'quality_regressed',
  'no_progress',
  'qa_blocked',
  'awaiting_manual_fix',
  'recovery_blocked',
]);

export const isFinalQARunning = (status: string): boolean => (
  status !== 'not_started' && !FINAL_QA_TERMINAL_STATUSES.has(status)
);
