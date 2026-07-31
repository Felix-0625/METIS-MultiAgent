import type {
  AutoRepairStatus,
  SupervisorAgentTask,
  SupervisorNextAction,
  SupervisorQARound,
  SupervisorRun,
  SupervisorWaitingFor,
} from './types';

export const SUPERVISOR_QA_ROUND_LIMIT = 5;

export const SUPERVISOR_RUN_STATE_LABELS: Record<string, string> = {
  waiting_engineer: '等待工程师完成',
  verifying: '完整验证中',
  qa_running: '业务质检中',
  blocked: '已阻断，等待人工处理',
  infrastructure_failed: '外部基础设施失败',
  model_failed: '模型调用失败',
  completed: '已完成',
};

const taskFailureStates = new Set(['failed', 'timeout', 'blocked', 'cancelled']);

export function supervisorRunFromStatus(status?: AutoRepairStatus | null): SupervisorRun | null {
  if (!status) return null;
  if (status.supervisor_run) return normalizeSupervisorRun(status.supervisor_run);
  if (!status.supervisor_state) return null;
  return {
    run_id: '',
    phase_id: status.phase_id,
    state: status.supervisor_state,
    active: status.running,
    qa_round: status.round || 0,
    max_qa_rounds: SUPERVISOR_QA_ROUND_LIMIT,
    waiting_for: status.waiting_for,
    next_action: status.next_action,
    rounds: [],
  };
}

export function normalizeSupervisorRun(run: SupervisorRun): SupervisorRun {
  const rounds = run.rounds || [];
  const latest = rounds[rounds.length - 1];
  return {
    ...run,
    state: run.state || run.status || 'idle',
    qa_round: run.qa_round ?? latest?.round_number ?? run.business_rounds_used ?? 0,
    qa_round_id: run.qa_round_id || run.active_qa_round_id || latest?.qa_round_id || null,
    max_qa_rounds: run.max_qa_rounds || SUPERVISOR_QA_ROUND_LIMIT,
    rounds,
  };
}

function objectText(value: SupervisorWaitingFor | SupervisorNextAction): string {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.filter(Boolean).join('、');
  const text = value.message || ('label' in value ? value.label : undefined)
    || value.name
    || value.type
    || value.id;
  return typeof text === 'string' ? text : '';
}

export const waitingForText = (value?: SupervisorWaitingFor): string => objectText(value || null);
export const nextActionText = (value?: SupervisorNextAction): string => objectText(value || null);

export function failedCriticalTasks(tasks?: SupervisorAgentTask[]): SupervisorAgentTask[] {
  return (tasks || []).filter(task => task.critical && taskFailureStates.has(task.status));
}

export function roundHasNewBlocker(round?: SupervisorQARound | null): boolean {
  if (!round) return false;
  if ((round.counts?.new || 0) <= 0) return false;
  const newIssues = (round.issues || []).filter(issue => {
    const lifecycle = issue.lifecycle || issue.status;
    return lifecycle === 'new' || issue.first_seen_round === round.round_number;
  });
  // Counts are authoritative even when the compact API omits issue details.
  return newIssues.length > 0 || !(round.issues || []).length;
}

export function evidenceCommandCount(round?: SupervisorQARound | null): number {
  if (!round) return 0;
  if (round.commands?.length) return round.commands.length;
  if (Array.isArray(round.evidence)) return round.evidence.filter(item => item.command).length;
  return round.evidence?.commands?.length || 0;
}

export function completionGateProblems(run?: SupervisorRun | null): string[] {
  if (!run || run.state !== 'completed') return [];
  const problems: string[] = [];
  const rounds = run.rounds || [];
  const latest = rounds[rounds.length - 1];
  if (run.completion_gate && run.completion_gate.passed !== true) problems.push('后端唯一完成门禁未通过');
  if (!latest) problems.push('缺少质检轮次证据');
  if (latest && evidenceCommandCount(latest) === 0) problems.push('缺少真实验证命令');
  if (latest && (latest.counts?.blocking || latest.counts?.remaining || 0) > 0) problems.push('仍有未解决阻断');
  if (rounds.some(round => failedCriticalTasks(round.agent_tasks).length > 0)) problems.push('存在关键 Agent 失败');
  if (run.active) problems.push('运行仍标记为 active');
  return problems;
}
