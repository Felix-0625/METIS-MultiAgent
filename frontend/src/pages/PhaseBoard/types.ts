import { API_BASE_URL } from '../../services/apiBase';

// ─── 类型定义 ─────────────────────────────────────────────────────────────

export interface PhaseInfo {
  phase_id: string;
  name: string;
  description: string;
  duration: string;
  status: 'pending' | 'active' | 'qa_pending' | 'reviewing' | 'completed' | 'needs_rework' | 'qa_blocked' | 'failed' | 'in_progress';
  agents: string[];
  order: number;
  reviewed: boolean;
  review_passed: boolean;
  user_confirmed?: boolean;
  started_at?: number | null;
  expert_requirements?: ExpertReq[];
  plan_generated?: boolean;
  qc_round?: number;
  qc_fixed_count?: number;
  qc_score?: number;
  supervision?: { can_proceed: boolean; open_errors: number; reviewed: boolean; message: string };
  agent_details?: { id?: string; agent_id?: string; role: string; name?: string; status?: string; progress?: number; fix_attempt?: number }[];
}

export interface Issue {
  id: string;
  message: string;
  file_path: string;
  responsible_agent_id: string;
  responsible_agent_role: string;
  severity: string;
  status: 'open' | 'fixing' | 'fixed';
  fix_hint: string;
}

export interface ReviewResult {
  phase_id: string;
  report: string;
  issues: Issue[];
  passed: boolean;
  error_count: number;
  warning_count?: number;
  fixed_count?: number;
  qc_round?: number;
  score?: number;
  runtime_acceptance?: RuntimeAcceptanceEvidence | null;
}

export type AutoRepairDecision = 'manual_fix' | 'retry_cycle' | 'rebuild_phase';

export interface AutoRepairIssue {
  id?: string;
  severity?: string;
  message?: string;
  fix_hint?: string;
  status?: string;
  responsible_agent_id?: string;
  responsible_agent_role?: string;
}

export interface AutoRepairActionRequired {
  round?: number;
  message?: string;
  options?: AutoRepairDecision[];
}

export interface AutoRepairStatus {
  phase_id: string;
  running: boolean;
  round: number;
  total_rounds: number;
  lifetime_qc_runs?: number;
  repair_attempts?: number;
  max_repairs_per_cycle?: number;
  status:
    | 'idle'
    | 'starting'
    | 'running'
    | 'continuing'
    | 'rewriting'
    | 'passed'
    | 'quality_regressed'
    | 'no_progress'
    | 'qa_blocked'
    | 'interrupted'
    | 'awaiting_decision'
    | 'awaiting_manual_fix'
    | 'rebuild_started'
    | 'needs_manual'
    | 'error'
    | 'failed'
    | string;
  messages: Array<{ role: ChatMsg['role']; content: string; ts: number; event_id?: string; round?: number }>;
  action_required?: AutoRepairActionRequired | null;
  needs_manual: boolean;
  issue_report: Record<string, AutoRepairIssue[]>;
  review_result?: ReviewResult | null;
  latest_qc?: AutoRepairRoundResult | null;
  round_history?: AutoRepairRoundResult[];
  repair_batch?: {
    round: number;
    status: string;
    total: number;
    completed: number;
    failed: number;
    files_changed?: boolean;
    changed_files?: string[];
  } | null;
  /** Persisted Supervisor state machine. Older backends may omit these fields. */
  supervisor_state?: SupervisorRunState | string;
  waiting_for?: SupervisorWaitingFor;
  next_action?: SupervisorNextAction;
  supervisor_run?: SupervisorRun | null;
}

export type SupervisorRunState =
  | 'waiting_engineer'
  | 'verifying'
  | 'qa_running'
  | 'blocked'
  | 'infrastructure_failed'
  | 'model_failed'
  | 'completed';

export type SupervisorAgentTaskStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'blocked'
  | 'cancelled';

export type SupervisorWaitingFor = string | string[] | null | {
  type?: string;
  id?: string;
  name?: string;
  message?: string;
  [key: string]: unknown;
};

export type SupervisorNextAction = string | null | {
  type?: string;
  label?: string;
  message?: string;
  [key: string]: unknown;
};

export interface SupervisorIssue {
  issue_id: string;
  fingerprint?: string;
  status?: string;
  severity?: string;
  message?: string;
  file_path?: string;
  lifecycle?: string;
  first_seen_round?: number;
  last_seen_round?: number;
  [key: string]: unknown;
}

export interface SupervisorEvidenceCommand {
  command: string;
  exit_code?: number | null;
  kind?: string;
  [key: string]: unknown;
}

export interface SupervisorRoundEvidence {
  commands?: SupervisorEvidenceCommand[];
  tests?: unknown;
  build?: unknown;
  service?: unknown;
  api?: unknown;
  docker?: unknown;
  deployment?: unknown;
  logs?: string[];
  verification_logs?: string[];
  [key: string]: unknown;
}

export interface SupervisorAgentTask {
  agent_id: string;
  status: SupervisorAgentTaskStatus | string;
  critical?: boolean;
  error?: string | null;
  task_id?: string;
  [key: string]: unknown;
}

export interface SupervisorRoundCounts {
  total?: number;
  blocking?: number;
  fixed?: number;
  remaining?: number;
  new?: number;
  repeated?: number;
}

export interface SupervisorQARound {
  qa_round_id: string;
  round_number: number;
  state: string;
  scope_snapshot?: unknown;
  issue_snapshot?: unknown;
  counts?: SupervisorRoundCounts;
  issues?: SupervisorIssue[];
  commit?: string | null;
  evidence?: SupervisorRoundEvidence | Array<SupervisorEvidenceCommand & { log?: string; passed?: boolean }>;
  commands?: SupervisorEvidenceCommand[];
  evidence_by_kind?: Record<string, unknown[]>;
  verification_log?: string[];
  agent_tasks?: SupervisorAgentTask[];
  [key: string]: unknown;
}

export interface SupervisorRun {
  run_id: string;
  phase_id: string;
  state: SupervisorRunState | string;
  status?: SupervisorRunState | string;
  active: boolean;
  qa_round: number;
  business_rounds_used?: number;
  qa_round_id?: string | null;
  active_qa_round_id?: string | null;
  max_qa_rounds: number;
  waiting_for?: SupervisorWaitingFor;
  next_action?: SupervisorNextAction;
  failure_reason?: string | null;
  started_at?: number | string | null;
  updated_at?: number | string | null;
  completed_at?: number | string | null;
  rounds?: SupervisorQARound[];
  completion_gate?: {
    dependencies_ready?: boolean;
    critical_agents_succeeded?: boolean;
    no_blockers?: boolean;
    evidence_complete?: boolean;
    passed?: boolean;
  };
  [key: string]: unknown;
}

export interface AutoRepairRoundResult {
  qc_run: number;
  repair_attempt: number;
  passed: boolean;
  score: number;
  blocking_count: number;
  warning_count: number;
  fixed_count: number;
  checked_at: number;
  issues: Issue[];
  convergence?: {
    status: string;
    before: number;
    after: number;
    resolved_blockers: string[];
    new_blockers: string[];
  };
}

export interface RuntimeAcceptanceEvidence {
  enabled?: boolean;
  passed?: boolean;
  status?: string;
  stage?: string;
  summary?: string;
  deploy_id?: string;
  service_url?: string;
  artifact_sha256?: string;
  acceptance_key?: string;
  source?: string;
  actionable?: boolean;
  cached?: boolean;
  steps?: AcceptanceStep[];
  logs?: string[];
  build_logs?: string[];
  [key: string]: unknown;
}

export interface AcceptanceStep {
  name?: string;
  label?: string;
  status?: string;
  message?: string;
  started_at?: number | string | null;
  finished_at?: number | string | null;
  evidence?: Record<string, unknown> | null;
}

export interface FinalQAActionRequired {
  message?: string;
  options?: string[];
}

export interface FinalQAStatus {
  project_id?: string;
  status: string;
  message?: string;
  round?: number;
  total_rounds?: number;
  logs?: string[];
  user_reports?: Array<Record<string, any>>;
  total_items?: number;
  completed_items?: number;
  current_item?: { name?: string; [key: string]: unknown } | null;
  current_step?: string | null;
  steps?: AcceptanceStep[];
  action_required?: FinalQAActionRequired | null;
  runtime_acceptance?: RuntimeAcceptanceEvidence | null;
  workspace_digest_current?: boolean | null;
  restored_from_persisted_result?: boolean;
  artifact_digest?: string;
  rule_version?: string;
  error_category?: string;
  retryable?: boolean;
  all_passed?: boolean;
  qc_summary?: {
    score?: number;
    error_count?: number;
    warning_count?: number;
    fixed_count?: number;
  };
  needs_manual?: AutoRepairIssue[];
  failed_reason?: string;
}

export interface AutoRepairAction extends AutoRepairActionRequired {
  phaseId: string;
  status: string;
  issueReport: Record<string, AutoRepairIssue[]>;
}

export interface DefectTicket {
  defect_id: string;
  severity: 'P0' | 'P1' | 'P2';
  layer: string;
  file_path: string;
  line_no: number | null;
  message: string;
  fix_hint: string;
  status: 'open' | 'pending_review' | 'approved' | 'fixing' | 'fixed' | 'verified' | 'escalated' | 'manual';
  fix_rounds: number;
  fix_proposal: string | null;
  proposal_approved: boolean;
  forbidden_zone: string[];
  escalation_reason: string | null;
  arbiter_decision: string | null;
}

export interface RepairSummary {
  subproject_id: string;
  total_defects: number;
  total_rounds: number;
  by_status: Record<string, number>;
  by_severity: { P0: number; P1: number; P2: number };
  round_scores: number[];
  escalated: boolean;
  batches_count: number;
}

export interface ArbiterResult {
  intervene: boolean;
  reasons: string[];
  decision: string | null;
  decision_id?: string;
  action_hint?: string;
}

export interface ChatMsg { role: 'user' | 'assistant' | 'system'; content: string; ts: number; type?: 'pm_fix_plan' | 'qc_report' | 'normal'; event_id?: string; round?: number; }
export interface FileRecord { file_path: string; agent_role: string; agent_id: string; phase_id: string; }

// 阶段规划（从PM对话中解析）
export interface PhasePlan {
  tasks: string[];       // 任务规划列表
  roles: string[];       // 人员规划列表
  coversPhasePlan: boolean;  // 是否覆盖了阶段原始规划
}

export interface ExpertReq {
  task_id: string;
  task_name: string;
  task_description: string;
  implementation?: string;
  implementation_method?: string;
  tech_stack?: string[];
  required_role: string;
  responsibilities?: string[];
  personnel_count?: number;
  personnel_allocation?: string[];
  required_domains: string[];
  required_skills: string[];
  acceptance_criteria: string[];
  priority: string;
}

// ─── 工具：从对话历史中解析任务/人员规划 ──────────────────────────────────
export function parsePlanFromMessages(msgs: ChatMsg[], phaseDesc: string): PhasePlan {
  const allText = msgs
    .filter(m => m.role === 'assistant')
    .map(m => m.content)
    .join('\n');

  const taskLines: string[] = [];
  const roleLines: string[] = [];

  allText.split('\n').forEach(line => {
    const t = line.trim();
    if (!t) return;
    if (/工程师|开发|测试|设计|运维|架构|前端|后端|全栈|qa|pm|hr/i.test(t) &&
        /需要|安排|负责|人员|角色|成员/i.test(t)) {
      roleLines.push(t.replace(/^[-—•\d.]\s*/, ''));
    }
    else if (/^[-—•]\s+.{5,}/.test(t) || /^\d+[.)]\s+.{5,}/.test(t)) {
      taskLines.push(t.replace(/^[-—•\d.)]\s*/, ''));
    }
  });

  const descKeywords = phaseDesc.split(/[,，。.。\s]+/).filter(k => k.length > 1).slice(0, 5);
  const coversPhasePlan = descKeywords.some(kw => allText.includes(kw)) && taskLines.length > 0;

  return {
    tasks: taskLines.slice(0, 10),
    roles: roleLines.slice(0, 6),
    coversPhasePlan,
  };
}

export function compactIssueAdvice(issue: Issue): string {
  const rawHint = (issue.fix_hint || '').replace(/\s+/g, ' ').trim();
  const rawMsg = (issue.message || '').replace(/\s+/g, ' ').trim();
  const hint = rawHint && rawHint.length < 120 ? rawHint : rawMsg;
  const concise = hint.length > 80 ? `${hint.slice(0, 80)}…` : hint;
  return concise || '定位相关代码，按质检要求最小化修改并回归验证';
}

export function formatIssueForPm(issue: Issue, index?: number): string {
  const prefix = typeof index === 'number' ? `${index + 1}. ` : '';
  return [
    `${prefix}问题：${issue.message}`,
    `   文件：${issue.file_path || '未知'}`,
    `   负责人：${issue.responsible_agent_role || '未知'}`,
    `   核心建议：${compactIssueAdvice(issue)}`,
  ].join('\n');
}

// ─── 常量 ─────────────────────────────────────────────────────────────────
export const API = API_BASE_URL;
