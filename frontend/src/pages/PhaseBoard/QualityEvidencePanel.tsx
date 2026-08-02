import React from 'react';
import { Alert, Button, Card, Collapse, Progress, Tag, Timeline } from 'antd';
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  ReloadOutlined,
  SafetyOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import {
  AcceptanceStep,
  AutoRepairStatus,
  FinalQAStatus,
  RuntimeAcceptanceEvidence,
  SupervisorQARound,
  SupervisorRun,
} from './types';
import {
  completionGateProblems,
  failedCriticalTasks,
  nextActionText,
  normalizeSupervisorRun,
  roundHasNewBlocker,
  supervisorRunFromStatus,
  SUPERVISOR_QA_ROUND_LIMIT,
  SUPERVISOR_RUN_STATE_LABELS,
  waitingForText,
} from './supervisorRunModel';

const STATUS_META: Record<string, { label: string; color: string; alert: 'success' | 'info' | 'warning' | 'error' }> = {
  not_started: { label: '未开始', color: 'default', alert: 'info' },
  idle: { label: '未开始', color: 'default', alert: 'info' },
  starting: { label: '准备中', color: 'processing', alert: 'info' },
  running: { label: '执行中', color: 'processing', alert: 'info' },
  continuing: { label: '继续质检中', color: 'processing', alert: 'info' },
  rewriting: { label: '返修中', color: 'processing', alert: 'info' },
  rebuild_started: { label: '阶段重构中', color: 'processing', alert: 'info' },
  restored: { label: '已恢复', color: 'blue', alert: 'info' },
  passed: { label: '已通过', color: 'success', alert: 'success' },
  quality_regressed: { label: '质量回退', color: 'error', alert: 'error' },
  no_progress: { label: '未收敛', color: 'warning', alert: 'warning' },
  qa_blocked: { label: '质检阻断', color: 'error', alert: 'error' },
  infrastructure_blocked: { label: '验收基础设施阻断', color: 'error', alert: 'error' },
  awaiting_decision: { label: '等待决策', color: 'warning', alert: 'warning' },
  awaiting_manual_fix: { label: '等待人工修复', color: 'warning', alert: 'warning' },
  needs_manual: { label: '需要人工处理', color: 'warning', alert: 'warning' },
  interrupted: { label: '已中断', color: 'error', alert: 'error' },
  failed: { label: '失败', color: 'error', alert: 'error' },
  error: { label: '异常', color: 'error', alert: 'error' },
  waiting_engineer: { label: '等待工程师完成', color: 'warning', alert: 'warning' },
  verifying: { label: '完整验证中', color: 'processing', alert: 'info' },
  qa_running: { label: '业务质检中', color: 'processing', alert: 'info' },
  blocked: { label: '已阻断', color: 'error', alert: 'error' },
  infrastructure_failed: { label: '外部基础设施失败', color: 'error', alert: 'error' },
  model_failed: { label: '模型调用失败', color: 'error', alert: 'error' },
  completed: { label: '已完成', color: 'success', alert: 'success' },
  pending: { label: '待执行', color: 'default', alert: 'info' },
  succeeded: { label: '成功', color: 'success', alert: 'success' },
  timeout: { label: '超时', color: 'error', alert: 'error' },
  blocked_agent: { label: '阻断', color: 'error', alert: 'error' },
  cancelled: { label: '已取消', color: 'default', alert: 'warning' },
};

const statusMeta = (status?: string) => STATUS_META[status || ''] || {
  label: status || '未知状态', color: 'default', alert: 'info' as const,
};

const STEP_LABELS: Record<string, string> = {
  queued: '进入验收队列',
  initializing: '初始化验收',
  infrastructure_preflight: '验收基础设施预检',
  snapshot_push: '推送交付快照',
  isolated_deploy: '隔离环境部署',
  health_check: '服务健康检查',
  frontend_check: '前端可访问性检查',
  business_api_check: '核心业务 API 验收',
  llm_quality_review: '全项目质量复核',
  repair: '问题返修',
  runtime_acceptance: '隔离运行验收',
  completed: '生成最终结论',
};

const stepLabel = (step: AcceptanceStep, index: number) => step.label
  || STEP_LABELS[step.name || '']
  || step.name
  || `步骤 ${index + 1}`;

const shortDigest = (value?: string) => value ? `${value.slice(0, 12)}${value.length > 12 ? '…' : ''}` : '未记录';

const evidenceLogs = (evidence?: RuntimeAcceptanceEvidence | null) => {
  const values = [...(evidence?.logs || []), ...(evidence?.build_logs || [])];
  return Array.from(new Set(values.filter(Boolean)));
};

const compactEvidenceValue = (value: unknown): string => {
  if (value == null || value === '') return '未记录';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const status = record.status || record.result || record.summary;
    return typeof status === 'string' ? status : `${Object.keys(record).length} 个字段`;
  }
  return String(value);
};

const SupervisorRoundEvidenceView: React.FC<{ round: SupervisorQARound }> = ({ round }) => {
  const evidenceObject = !Array.isArray(round.evidence) ? round.evidence : undefined;
  const evidenceItems = Array.isArray(round.evidence) ? round.evidence : [];
  const commands = round.commands || evidenceObject?.commands || evidenceItems
    .filter(item => item.command)
    .map(item => ({ command: item.command, exit_code: item.exit_code, kind: item.kind }));
  const logs = [
    ...(round.verification_log || []),
    ...(evidenceObject?.logs || []),
    ...(evidenceObject?.verification_logs || []),
    ...evidenceItems.map(item => item.log || '').filter(Boolean),
  ];
  const evidenceKinds: Array<[string, unknown]> = [
    ['测试', evidenceObject?.tests || round.evidence_by_kind?.test || round.evidence_by_kind?.tests],
    ['构建', evidenceObject?.build || round.evidence_by_kind?.build],
    ['服务', evidenceObject?.service || round.evidence_by_kind?.service],
    ['API', evidenceObject?.api || round.evidence_by_kind?.api],
    ['Docker', evidenceObject?.docker || round.evidence_by_kind?.docker],
    ['部署', evidenceObject?.deployment || round.evidence_by_kind?.deployment],
  ];
  return (
    <div style={{ fontSize: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
        {evidenceKinds.map(([label, value]) => (
          <Tag key={label} color={value == null ? 'default' : 'blue'}>{label}：{compactEvidenceValue(value)}</Tag>
        ))}
      </div>
      {commands.length > 0 ? commands.map((command, index) => (
        <div key={`${command.command}-${index}`} style={{ display: 'flex', gap: 8, marginBottom: 4, alignItems: 'flex-start' }}>
          <Tag color={command.exit_code === 0 ? 'success' : command.exit_code == null ? 'default' : 'error'} style={{ flexShrink: 0 }}>
            {command.kind || '命令'} · exit {command.exit_code ?? '未记录'}
          </Tag>
          <code style={{ overflowWrap: 'anywhere' }}>{command.command}</code>
        </div>
      )) : <Alert type="warning" showIcon message="本轮尚无真实验证命令证据" />}
      {logs.length > 0 && (
        <pre style={{ maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap', margin: '8px 0 0', fontSize: 11 }}>{logs.slice(-30).join('\n')}</pre>
      )}
    </div>
  );
};

export const SupervisorRunPanel: React.FC<{ run?: SupervisorRun | null; compact?: boolean }> = ({ run: rawRun, compact = false }) => {
  if (!rawRun) return null;
  const run = normalizeSupervisorRun(rawRun);
  const rounds = run.rounds || [];
  const latest = rounds[rounds.length - 1];
  const stateMeta = statusMeta(run.state);
  const waiting = waitingForText(run.waiting_for);
  const next = nextActionText(run.next_action);
  const completionProblems = completionGateProblems(run);
  const newBlocker = roundHasNewBlocker(latest);
  const criticalFailures = rounds.flatMap(round => failedCriticalTasks(round.agent_tasks));
  const configuredLimit = run.max_qa_rounds || SUPERVISOR_QA_ROUND_LIMIT;
  const displayedRound = Math.min(run.qa_round || 0, SUPERVISOR_QA_ROUND_LIMIT);

  return (
    <Card
      size="small"
      data-testid="supervisor-run-panel"
      title={<span><SafetyOutlined /> Supervisor 质检状态机</span>}
      extra={<Tag color={stateMeta.color}>{SUPERVISOR_RUN_STATE_LABELS[run.state] || stateMeta.label}</Tag>}
      style={{ marginTop: 10 }}
    >
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        <Tag color="blue">业务质检轮次 {displayedRound}/{SUPERVISOR_QA_ROUND_LIMIT}</Tag>
        {configuredLimit !== SUPERVISOR_QA_ROUND_LIMIT && <Tag color="error">后端轮次上限异常：{configuredLimit}</Tag>}
        {run.run_id && <Tag>run_id：<code>{run.run_id}</code></Tag>}
        {run.qa_round_id && <Tag>qa_round_id：<code>{run.qa_round_id}</code></Tag>}
        <Tag color={run.active ? 'processing' : 'default'}>{run.active ? '运行中' : '非活动'}</Tag>
      </div>
      {(waiting || next) && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8, marginBottom: 10 }}>
          {waiting && <Alert type="warning" showIcon message="当前等待对象" description={waiting} />}
          {next && <Alert type="info" showIcon message="下一动作" description={next} />}
        </div>
      )}
      {run.failure_reason && <Alert type="error" showIcon message="失败原因" description={run.failure_reason} style={{ marginBottom: 10 }} />}
      {newBlocker && <Alert type="error" showIcon message="本轮出现新增问题，状态机必须立即阻断，不能按问题数量缩小判定为收敛" style={{ marginBottom: 10 }} />}
      {criticalFailures.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`${criticalFailures.length} 个关键 Agent 处于失败终态，禁止完成`}
          description={criticalFailures.map(task => `${task.agent_id}: ${task.status}${task.error ? `（${task.error}）` : ''}`).join('；')}
          style={{ marginBottom: 10 }}
        />
      )}
      {completionProblems.length > 0 && (
        <Alert type="error" showIcon message="完成门禁证据不完整" description={completionProblems.join('；')} style={{ marginBottom: 10 }} />
      )}
      {!compact && rounds.length > 0 && (
        <Collapse
          size="small"
          items={[...rounds].reverse().map(round => {
            const counts = round.counts || {};
            const failedAgents = failedCriticalTasks(round.agent_tasks);
            const issueCount = round.issues?.length || 0;
            return {
              key: round.qa_round_id || String(round.round_number),
              label: (
                <div data-testid={`supervisor-round-${round.round_number}`} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
                  <strong>第 {round.round_number}/{SUPERVISOR_QA_ROUND_LIMIT} 轮</strong>
                  <Tag color={round.state === 'completed' ? 'success' : 'processing'}>{round.state}</Tag>
                  <span style={{ fontSize: 12, color: '#595959' }}>
                    总数 {counts.total ?? issueCount} · 阻断 {counts.blocking ?? 0} · 已修复 {counts.fixed ?? 0} · 剩余 {counts.remaining ?? 0} · 新增 {counts.new ?? 0} · 重复 {counts.repeated ?? 0}
                  </span>
                </div>
              ),
              children: (
                <div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8, fontSize: 12 }}>
                    <Tag>qa_round_id：<code>{round.qa_round_id}</code></Tag>
                    {round.commit && <Tag>关联 commit：<code>{round.commit}</code></Tag>}
                    <Tag color={round.scope_snapshot == null ? 'warning' : 'blue'}>测试/验收范围：{compactEvidenceValue(round.scope_snapshot)}</Tag>
                    <Tag color={round.issue_snapshot == null ? 'warning' : 'blue'}>问题快照：{compactEvidenceValue(round.issue_snapshot)}</Tag>
                  </div>
                  {(round.agent_tasks || []).length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                      {(round.agent_tasks || []).map((task, index) => (
                        <Tag
                          key={`${task.agent_id}-${task.task_id || index}`}
                          color={task.status === 'succeeded' ? 'success' : ['failed', 'timeout', 'blocked'].includes(task.status) ? 'error' : task.status === 'running' ? 'processing' : 'default'}
                        >
                          {task.critical ? '关键 ' : ''}{task.agent_id}：{task.status}{task.error ? ` · ${task.error}` : ''}
                        </Tag>
                      ))}
                      {failedAgents.length > 0 && <Tag color="error">本轮禁止通过</Tag>}
                    </div>
                  )}
                  {(round.issues || []).length > 0 && (
                    <Collapse
                      size="small"
                      ghost
                      items={[{
                        key: 'issues',
                        label: `稳定问题清单（${issueCount}）`,
                        children: (round.issues || []).map(issue => (
                          <div key={issue.issue_id} style={{ padding: '5px 0', borderBottom: '1px solid #f0f0f0', fontSize: 12 }}>
                            <div><Tag color={issue.severity === 'critical' || issue.severity === 'error' ? 'error' : 'warning'}>{issue.severity || 'unknown'}</Tag><strong>{issue.issue_id}</strong> · {issue.status || issue.lifecycle || 'unknown'}</div>
                            {issue.message && <div style={{ marginTop: 3 }}>{issue.message}</div>}
                            <div style={{ color: '#8c8c8c', overflowWrap: 'anywhere' }}>
                              {issue.file_path || '未关联文件'}{issue.fingerprint ? ` · 指纹 ${issue.fingerprint}` : ''}
                            </div>
                          </div>
                        )),
                      }]}
                    />
                  )}
                  <Collapse
                    size="small"
                    ghost
                    items={[{ key: 'evidence', label: '修复与验证证据', children: <SupervisorRoundEvidenceView round={round} /> }]}
                  />
                </div>
              ),
            };
          })}
        />
      )}
      {!compact && rounds.length === 0 && <Alert type="info" showIcon message="尚无持久化业务质检轮次" />}
    </Card>
  );
};

const RuntimeEvidence: React.FC<{
  evidence?: RuntimeAcceptanceEvidence | null;
  workspaceCurrent?: boolean | null;
  artifactDigest?: string;
}> = ({ evidence, workspaceCurrent, artifactDigest }) => {
  if (!evidence && workspaceCurrent == null && !artifactDigest) return null;
  const meta = statusMeta(evidence?.status);
  const logs = evidenceLogs(evidence);
  return (
    <Card size="small" title="运行验收证据" style={{ marginTop: 10 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, fontSize: 12 }}>
        {evidence?.status && <Tag color={meta.color}>运行验收：{meta.label}</Tag>}
        {workspaceCurrent != null && (
          <Tag color={workspaceCurrent ? 'success' : 'error'}>
            工作区快照：{workspaceCurrent ? '当前有效' : '已变化'}
          </Tag>
        )}
        {evidence?.cached && <Tag color="blue">复用已验证证据</Tag>}
        {evidence?.actionable === false && <Tag>非项目代码问题</Tag>}
      </div>
      <div style={{ marginTop: 8, color: '#595959', fontSize: 12, lineHeight: 1.7 }}>
        {evidence?.deploy_id && <div>部署 ID：<code>{evidence.deploy_id}</code></div>}
        {evidence?.stage && <div>验收步骤：{evidence.stage}</div>}
        {evidence?.summary && <div>结果：{evidence.summary}</div>}
        <div>交付快照：<code>{shortDigest(evidence?.artifact_sha256 || artifactDigest)}</code></div>
        {evidence?.acceptance_key && <div>验收键：<code>{shortDigest(evidence.acceptance_key)}</code></div>}
        {evidence?.service_url && (
          <div>服务地址：<a href={evidence.service_url} target="_blank" rel="noreferrer">{evidence.service_url}</a></div>
        )}
      </div>
      {evidence?.steps?.length ? (
        <Timeline
          style={{ marginTop: 14 }}
          items={evidence.steps.map((step, index) => ({
            dot: stepIcon(step.status),
            children: <span style={{ fontSize: 12 }}>{stepLabel(step, index)} · {statusMeta(step.status).label}</span>,
          }))}
        />
      ) : null}
      {logs.length > 0 && (
        <Collapse
          size="small"
          style={{ marginTop: 8 }}
          items={[{
            key: 'runtime-logs',
            label: `运行日志（${logs.length}）`,
            children: <pre style={{ maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0, fontSize: 11 }}>{logs.slice(-30).join('\n')}</pre>,
          }]}
        />
      )}
    </Card>
  );
};

export const PhaseQualityRunPanel: React.FC<{ state?: AutoRepairStatus | null }> = ({ state }) => {
  if (!state || (state.status === 'idle' && !(state.round_history || []).length && !state.supervisor_run && !state.supervisor_state)) return null;
  const supervisorRun = supervisorRunFromStatus(state);
  const meta = statusMeta(state.status);
  const history = state.round_history || [];
  const trend = history.map(item => item.blocking_count);
  const nonBlockingWarnings = state.status === 'passed'
    ? (state.review_result?.issues || []).filter(issue => issue.severity === 'warning')
    : [];
  const terminalMessage = state.action_required?.message
    || (state.status === 'passed' ? '本阶段质检与返修闭环已经通过。' : undefined);
  return (
    <>
    <SupervisorRunPanel run={supervisorRun} />
    <Card
      size="small"
      title={<span><SafetyOutlined /> 质检循环结果</span>}
      extra={<Tag color={meta.color}>{meta.label}</Tag>}
      style={{ marginTop: 10 }}
    >
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, fontSize: 12, marginBottom: 10 }}>
        <Tag>质检运行 {state.lifetime_qc_runs ?? history.length}</Tag>
        <Tag>返修尝试 {state.repair_attempts ?? state.round ?? 0}/{state.max_repairs_per_cycle ?? 5}</Tag>
        {trend.length > 0 && <Tag color={trend[trend.length - 1] === 0 ? 'success' : 'blue'}>阻断趋势 {trend.join(' → ')}</Tag>}
        {state.repair_batch && (
          <Tag color={state.repair_batch.failed > 0 ? 'error' : 'processing'}>
            返修任务 {state.repair_batch.completed}/{state.repair_batch.total}
            {state.repair_batch.status === 'completed' && (state.repair_batch.files_changed ? ' · 文件已更新' : ' · 文件未变化')}
          </Tag>
        )}
      </div>
      {terminalMessage && <Alert type={meta.alert} showIcon message={terminalMessage} style={{ marginBottom: 10 }} />}
      {nonBlockingWarnings.length > 0 && (
        <Alert
          type="info"
          showIcon
          message={`已通过；另有 ${nonBlockingWarnings.length} 条非阻塞优化建议`}
          description="这些建议不消耗返修次数，也不会阻止确认阶段完成。可在后续优化时处理。"
          style={{ marginBottom: 10 }}
        />
      )}
      {history.length > 0 && (
        <Timeline
          items={history.map(item => {
            const convergence = item.convergence;
            const newCount = convergence?.new_blockers?.length || 0;
            const color = item.passed ? 'green' : newCount > 0 ? 'red' : 'orange';
            return {
              color,
              dot: item.passed ? <CheckCircleOutlined /> : newCount > 0 ? <CloseCircleOutlined /> : <ClockCircleOutlined />,
              children: (
                <div data-testid={`qc-round-${item.qc_run}`}>
                  <div style={{ fontWeight: 600 }}>第 {item.qc_run} 次质检 · 评分 {item.score ?? 0}</div>
                  <div style={{ color: '#595959', fontSize: 12 }}>
                    阻断 {item.blocking_count} · 警告 {item.warning_count} · 已修复 {item.fixed_count}
                    {item.repair_attempt > 0 ? ` · 对应第 ${item.repair_attempt} 次返修` : ''}
                  </div>
                  {convergence && (
                    <div style={{ color: newCount > 0 ? '#cf1322' : '#8c8c8c', fontSize: 11 }}>
                      {convergence.status || '收敛检查'}：{convergence.before} → {convergence.after}
                      {convergence.resolved_blockers.length > 0 ? `，解决 ${convergence.resolved_blockers.length}` : ''}
                      {newCount > 0 ? `，新增阻断 ${newCount}` : '，无新增阻断'}
                    </div>
                  )}
                </div>
              ),
            };
          })}
        />
      )}
      {state.action_required?.options?.length ? (
        <div style={{ fontSize: 12, color: '#8c8c8c' }}>
          后端允许操作：{state.action_required.options.map(option => <Tag key={option}>{option}</Tag>)}
        </div>
      ) : null}
      <RuntimeEvidence evidence={state.review_result?.runtime_acceptance} />
    </Card>
    </>
  );
};

const fallbackSteps = (state: FinalQAStatus): AcceptanceStep[] => {
  const runtime = state.runtime_acceptance;
  return [
    { name: 'snapshot', label: '冻结交付快照', status: state.artifact_digest ? 'passed' : state.status === 'not_started' ? 'pending' : 'running' },
    { name: 'quality', label: '全项目确定性质检', status: state.round ? (state.status === 'passed' ? 'passed' : 'running') : 'pending' },
    { name: 'runtime', label: '隔离运行验收', status: runtime?.status || (state.current_step === 'runtime_acceptance' ? 'running' : 'pending') },
    { name: 'result', label: '最终验收结论', status: ['passed', 'failed', 'needs_manual', 'infrastructure_blocked'].includes(state.status) ? state.status : 'pending' },
  ];
};

const stepIcon = (status?: string) => {
  if (status === 'passed' || status === 'completed') return <CheckCircleOutlined style={{ color: '#52c41a' }} />;
  if (['failed', 'error', 'infrastructure_blocked'].includes(status || '')) return <CloseCircleOutlined style={{ color: '#ff4d4f' }} />;
  if (['running', 'processing', 'restored'].includes(status || '')) return <ReloadOutlined spin style={{ color: '#1677ff' }} />;
  return <ClockCircleOutlined style={{ color: '#bfbfbf' }} />;
};

export const FinalAcceptancePanel: React.FC<{
  state: FinalQAStatus;
  actionLoading?: boolean;
  onAction?: (action: string) => void;
}> = ({ state, actionLoading, onAction }) => {
  const meta = statusMeta(state.status);
  const steps = state.steps?.length ? state.steps : fallbackSteps(state);
  const allowed = new Set(state.action_required?.options || []);
  const retryAllowed = allowed.has('retry_acceptance');
  const unsupportedActions = [...allowed].filter(option => option !== 'retry_acceptance');
  const progress = state.total_items
    ? Math.round(((state.completed_items || 0) / state.total_items) * 100)
    : state.status === 'passed' ? 100 : 0;
  return (
    <div data-testid="final-acceptance-panel">
      <Alert
        type={meta.alert}
        showIcon
        message={<span>最终验收：{meta.label}{state.restored_from_persisted_result ? '（刷新后已恢复）' : ''}</span>}
        description={state.action_required?.message || state.message || (state.status === 'passed' ? '当前交付快照已通过最终验收。' : undefined)}
      />
      {![
        'not_started', 'passed', 'failed', 'needs_manual', 'infrastructure_blocked',
        'interrupted', 'quality_regressed', 'no_progress', 'qa_blocked', 'awaiting_manual_fix',
      ].includes(state.status) && (
        <Progress percent={progress} status="active" style={{ marginTop: 12 }} />
      )}
      <Timeline
        style={{ marginTop: 18 }}
        items={steps.map((step, index) => ({
          dot: stepIcon(step.status),
          children: (
            <div data-testid={`acceptance-step-${step.name || index}`}>
              <div style={{ fontWeight: 600 }}>{stepLabel(step, index)}</div>
              <div style={{ color: '#8c8c8c', fontSize: 12 }}>
                {statusMeta(step.status).label}{step.message ? ` · ${step.message}` : ''}
              </div>
            </div>
          ),
        }))}
      />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
        {state.current_step && <Tag color="processing">当前步骤：{state.current_step}</Tag>}
        {state.error_category && <Tag color="error">错误分类：{state.error_category}</Tag>}
        {state.rule_version && <Tag>规则版本：{state.rule_version}</Tag>}
        {state.retryable != null && <Tag color={state.retryable ? 'blue' : 'default'}>{state.retryable ? '可重试' : '不可自动重试'}</Tag>}
      </div>
      <RuntimeEvidence
        evidence={state.runtime_acceptance}
        workspaceCurrent={state.workspace_digest_current}
        artifactDigest={state.artifact_digest}
      />
      {state.logs?.length ? (
        <Collapse
          size="small"
          style={{ marginTop: 10 }}
          items={[{
            key: 'final-qa-logs',
            label: `最终质检日志（${state.logs.length}）`,
            children: <pre style={{ maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0, fontSize: 11 }}>{state.logs.slice(-40).join('\n')}</pre>,
          }]}
        />
      ) : null}
      {(retryAllowed || unsupportedActions.length > 0) && (
        <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
          {retryAllowed && (
            <Button type="primary" icon={<ReloadOutlined />} loading={actionLoading} onClick={() => onAction?.('retry_acceptance')}>
              重新执行最终验收
            </Button>
          )}
          {unsupportedActions.length > 0 && (
            <span style={{ fontSize: 12, color: '#8c8c8c' }}>
              后端建议：{unsupportedActions.map(option => <Tag key={option} icon={<WarningOutlined />}>{option}</Tag>)}
            </span>
          )}
        </div>
      )}
    </div>
  );
};
