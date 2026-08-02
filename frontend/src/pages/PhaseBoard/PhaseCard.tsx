import React, { useEffect, useRef, useState } from 'react';
import { Button, Tag, Badge, Input, Alert, Tooltip } from 'antd';
import {
  PlayCircleOutlined, AuditOutlined, CheckCircleOutlined,
  LoadingOutlined, FileOutlined, UserOutlined, EditOutlined,
  BugOutlined, OrderedListOutlined, CrownOutlined, SafetyOutlined,
  RocketOutlined, ReloadOutlined,
  DownOutlined, RightOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { AutoRepairStatus, PhaseInfo, ReviewResult, FileRecord, API } from './types';
import { PhaseQualityRunPanel } from './QualityEvidencePanel';

const AGENT_STATUS_LABELS: Record<string, string> = {
  idle: '待机',
  queued: '排队中',
  working: '执行中',
  running: '执行中',
  in_progress: '执行中',
  fixing: '修复中',
  re_checking: '复检中',
  fix_required: '待修复',
  completed: '已完成',
  failed: '执行失败',
  error: '执行异常',
  fix_limit_reached: '需人工处理',
  blocked: '阻塞',
  cancelled: '已取消',
};

interface PhaseCardProps {
  phase: PhaseInfo;
  idx: number;
  totalPhases: number;
  review?: ReviewResult;
  autoRepairState?: AutoRepairStatus;
  files: FileRecord[];
  phaseAgents: any[];
  isPending: boolean;
  isActive: boolean;
  isCompleted: boolean;
  canStart: boolean;
  canReview: boolean;
  reviewBlockedReason: string;
  reviewPassed: boolean;
  userConfirmed: boolean;
  isLastPhase: boolean;
  editingPhase: string | null;
  editDesc: string;
  openPmChat: string | null;
  openSupChat: string | null;
  startingPhase: string | null;
  reviewingPhase: string | null;
  confirmingPhase: string | null;
  batchSubmitting: string | null;
  resettingPhase: string | null;
  rerunningAgent: string | null;
  projectId: string;
  needsRework: boolean;
  // Callbacks
  onSetTaskPreview: (id: string | null) => void;
  onTogglePmChat: (id: string) => void;
  onToggleSupChat: (id: string) => void;
  onStartPhase: (id: string) => void;
  onRunReview: (id: string, name: string) => void;
  onRunAutoRepair?: (id: string, name: string) => void;
  onConfirmComplete: (id: string, name: string) => void;
  onEditPhase: (id: string, desc: string) => void;
  onSetEditDesc: (desc: string) => void;
  onSaveDesc: (id: string) => void;
  onCancelEdit: () => void;
  onSubmitIssues: (id: string) => void;
  onIssueClick: (issue: any, phaseId: string) => void;
  onNavigateEngineer: (url: string) => void;
  onRerunAgent: (agentId: string) => void;
  onResetPhase?: (id: string, name: string) => void;
  children?: React.ReactNode;
}

const PhaseCard: React.FC<PhaseCardProps> = ({
  phase, idx, totalPhases, review, autoRepairState, files, phaseAgents,
  isPending, isActive, isCompleted, canStart, canReview, reviewBlockedReason, reviewPassed,
  userConfirmed, isLastPhase, editingPhase, editDesc,
  openPmChat, openSupChat, startingPhase, reviewingPhase,
  confirmingPhase, batchSubmitting, resettingPhase, rerunningAgent, projectId, needsRework,
  onSetTaskPreview, onTogglePmChat, onToggleSupChat,
  onStartPhase, onRunReview, onRunAutoRepair, onConfirmComplete,
  onEditPhase, onSetEditDesc, onSaveDesc, onCancelEdit,
  onSubmitIssues, onIssueClick, onNavigateEngineer, onRerunAgent,
  onResetPhase,
  children,
}) => {
  const isFailedPhase = phase.status === 'failed';
  const isQaBlocked = phase.status === 'qa_blocked';
  const isReviewing = phase.status === 'reviewing' || phase.status === 'qa_pending';
  const qcRound = review?.qc_round ?? phase.qc_round ?? 0;
  const qcFixedCount = review?.fixed_count ?? phase.qc_fixed_count ?? 0;
  const qcSummary = qcRound > 0
    ? `质检第 ${qcRound} 轮通过${qcFixedCount > 0 ? `，累计自动修复 ${qcFixedCount} 个问题` : '，未发现需修复问题'}`
    : '质检记录尚未生成';
  const borderColor = isCompleted ? '#52c41a' : isFailedPhase ? '#ff4d4f' : isActive ? '#1677ff' : '#e8e8e8';
  const boxShadow = isActive ? '0 0 0 2px rgba(22,119,255,0.1)' : undefined;
  const [expanded, setExpanded] = useState(!isCompleted);
  const wasCompleted = useRef(isCompleted);

  useEffect(() => {
    if (isCompleted && !wasCompleted.current) setExpanded(false);
    if (isActive) setExpanded(true);
    wasCompleted.current = isCompleted;
  }, [isActive, isCompleted]);

  return (
    <div style={{
      background: '#fff', borderRadius: 8,
      border: `1px solid ${borderColor}`,
      boxShadow,
    }}>
      {/* ── Phase Header ── */}
      <div style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            width: 32, height: 32, borderRadius: '50%', display: 'flex',
            alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 13,
            background: isCompleted ? '#f6ffed' : isActive ? '#e6f4ff' : '#f5f5f5',
            color: isCompleted ? '#52c41a' : isActive ? '#1677ff' : '#8c8c8c',
          }}>
            {isCompleted ? <CheckCircleOutlined /> : idx + 1}
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontWeight: 600, fontSize: 14 }}>{phase.name}</span>
              <Badge
                status={isCompleted ? 'success' : (isFailedPhase || isQaBlocked) ? 'error' : needsRework ? 'warning' : isActive ? 'processing' : 'default'}
                text={<span style={{ fontSize: 11 }}>
                  {isCompleted ? '已完成' : isFailedPhase ? '执行失败' : isQaBlocked ? '质检阻断' : needsRework ? '自动返工中' : isReviewing ? '质检中/待确认' : isActive ? '执行中' : '待启动'}
                </span>}
              />
              {phase.duration && <Tag style={{ fontSize: 10 }}>{phase.duration}</Tag>}
            </div>
            {editingPhase === phase.phase_id ? (
              <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
                <Input size="small" value={editDesc} onChange={e => onSetEditDesc(e.target.value)} style={{ width: 300, fontSize: 12 }} />
                <Button size="small" type="primary" onClick={() => onSaveDesc(phase.phase_id)}>保存</Button>
                <Button size="small" onClick={onCancelEdit}>取消</Button>
              </div>
            ) : (
              <div style={{ fontSize: 12, color: '#595959', marginTop: 2, display: 'flex', alignItems: 'center', gap: 6 }}>
                {phase.description}
                {(isActive || isPending) && (
                  <EditOutlined
                    style={{ fontSize: 11, color: '#8c8c8c', cursor: 'pointer' }}
                    onClick={() => { onEditPhase(phase.phase_id, phase.description); }}
                  />
                )}
              </div>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <Button
            size="small"
            type="text"
            icon={expanded ? <DownOutlined /> : <RightOutlined />}
            aria-label={expanded ? `收起${phase.name}` : `展开${phase.name}`}
            onClick={() => setExpanded(value => !value)}
          >
            {expanded ? '收起' : '展开'}
          </Button>
          <Button size="small" icon={<OrderedListOutlined />} onClick={() => onSetTaskPreview(phase.phase_id)}>
            任务预览
          </Button>
          {/* 修复：阶段PM按钮始终可用，不限制状态 */}
          <Button
            size="small" icon={<CrownOutlined />}
            type={openPmChat === phase.phase_id ? 'primary' : 'default'}
            onClick={() => onTogglePmChat(phase.phase_id)}
          >
            阶段PM
          </Button>
          {isPending && (
            <Tooltip title={canStart ? '创建本阶段专家 Agent 并开始执行' : '请先完成并确认前一阶段'}>
              <Button
                size="small"
                type="primary"
                icon={<RocketOutlined />}
                loading={startingPhase === phase.phase_id}
                disabled={!canStart || (!!startingPhase && startingPhase !== phase.phase_id)}
                onClick={() => onStartPhase(phase.phase_id)}
              >
                分配专家并执行
              </Button>
            </Tooltip>
          )}
          {/* 重置阶段按钮：只在active状态下显示 */}
          {isActive && onResetPhase && (
            <Tooltip title="重置阶段：删除所有Agent、清空质检结果，回到待启动状态">
              <Button
                size="small"
                icon={<ReloadOutlined />}
                danger
                loading={resettingPhase === phase.phase_id}
                disabled={!!resettingPhase && resettingPhase !== phase.phase_id}
                onClick={() => onResetPhase(phase.phase_id, phase.name)}
              >
                重置阶段
              </Button>
            </Tooltip>
          )}
          {isActive && (
            <>
              <Button
                size="small" icon={<SafetyOutlined />}
                type={openSupChat === phase.phase_id ? 'primary' : 'default'}
                onClick={() => onToggleSupChat(phase.phase_id)}
              >
                监督Agent
              </Button>
              {onRunAutoRepair && (
                <Tooltip title={canReview ? '监督者全量质检→对应专家修复→全量复检，自动循环直到通过' : reviewBlockedReason}>
                  <Button
                    size="small"
                    icon={<RocketOutlined />}
                    loading={reviewingPhase === phase.phase_id}
                    disabled={!canReview || (!!reviewingPhase && reviewingPhase !== phase.phase_id)}
                    style={{ borderColor: '#722ed1', color: '#722ed1' }}
                    onClick={() => onRunAutoRepair(phase.phase_id, phase.name)}
                  >
                    质检循环
                  </Button>
                </Tooltip>
              )}
            </>
          )}
          {isActive && reviewPassed && !userConfirmed && (
            <Tooltip title="质检通过，手动确认后才能进入下一阶段">
              <Button
                type="primary" size="small"
                style={{ background: '#52c41a', borderColor: '#52c41a' }}
                icon={confirmingPhase === phase.phase_id ? <LoadingOutlined /> : <CheckCircleOutlined />}
                loading={confirmingPhase === phase.phase_id}
                onClick={() => onConfirmComplete(phase.phase_id, phase.name)}
              >
                确认阶段完成
              </Button>
            </Tooltip>
          )}
          {isActive && !reviewPassed && review && (
            <Tooltip title="请先通过质检">
              <Button size="small" disabled>确认阶段完成</Button>
            </Tooltip>
          )}
        </div>
      </div>

      {/* ── Phase Body (shown when active or completed) ── */}
      {expanded && (isActive || isCompleted) && (
        <div style={{ padding: '0 16px 14px', borderTop: '1px solid #f0f0f0', paddingTop: 12 }}>
          {isActive && !canReview && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="质检暂不可用"
              description={reviewBlockedReason}
            />
          )}
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>
                本阶段Agent（{phaseAgents.length}）
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {phaseAgents.length === 0 ? (
                  <span style={{ fontSize: 11, color: '#bfbfbf' }}>暂无</span>
                ) : phaseAgents.map((a: any) => {
                  const agentId = a.id || a.agent_id;
                  const status = String(a.status || 'idle').toLowerCase();
                  const isLimitReached = status === 'fix_limit_reached';
                  const isWorking = ['working', 'queued', 'running', 'in_progress', 'fixing', 're_checking'].includes(status);
                  const fixAttempt = a.fix_attempt || 0;
                  const canRerun = !!agentId && !isWorking && ['failed', 'error', 'fix_required', 'fix_limit_reached', 'completed', 'idle'].includes(status);
                  const isRerunning = rerunningAgent === agentId;
                  return (
                    <Tooltip key={agentId || `${a.role}-${Math.random()}`} title={
                      isLimitReached
                        ? `已自动修复3次仍未通过，点击「重跑」手动重新执行`
                        : fixAttempt > 0
                        ? `第${fixAttempt} 次自动修复中`
                        : status === 'completed'
                        ? `已完成，点击「重跑」可重新生成`
                        : status === 'failed' || status === 'error'
                        ? `执行失败，点击「重跑」重新执行`
                        : !agentId
                        ? `缺少 Agent ID，无法重跑`
                        : undefined
                    }>
                      <div style={{
                        display: 'flex', alignItems: 'center', gap: 4,
                        padding: '3px 8px', borderRadius: 4,
                        border: `1px solid ${isLimitReached ? '#ff4d4f' : '#e8e8e8'}`,
                        fontSize: 11,
                        background: isLimitReached ? '#fff2f0' : undefined,
                        cursor: canRerun && !rerunningAgent ? 'pointer' : undefined,
                        opacity: rerunningAgent && !isRerunning ? 0.55 : 1,
                      }}
                        onClick={canRerun && !rerunningAgent ? () => onRerunAgent(agentId) : undefined}
                      >
                        <UserOutlined style={{ color: isLimitReached ? '#ff4d4f' : '#8c8c8c' }} />
                        <span style={{ color: isLimitReached ? '#ff4d4f' : undefined }}>{a.role}</span>
                        <span style={{ color: '#8c8c8c', fontSize: 10 }}>{AGENT_STATUS_LABELS[status] || status}</span>
                        {fixAttempt > 0 && !isLimitReached && <span style={{ color: '#fa8c16', fontSize: 10 }}>修复{fixAttempt}</span>}
                        {isLimitReached && <span style={{ color: '#ff4d4f', fontSize: 10 }}>需人工</span>}
                        {isRerunning
                          ? <LoadingOutlined style={{ color: '#1677ff', fontSize: 10 }} />
                          : canRerun && <span style={{ color: '#1677ff', fontSize: 10, textDecoration: 'underline' }}>重跑</span>}
                        <Badge status={isLimitReached || status === 'failed' || status === 'error' ? 'error' : isWorking ? 'processing' : status === 'completed' ? 'success' : status === 'fix_required' ? 'warning' : 'default'} />
                      </div>
                    </Tooltip>
                  );
                })}
              </div>
            </div>
            {files.length > 0 && (
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>本阶段文件（{files.length}）</div>
                <div style={{ maxHeight: 80, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 3 }}>
                  {files.map((f, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
                      <FileOutlined style={{ color: '#1677ff', flexShrink: 0 }} />
                      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.file_path}</span>
                      <Tag style={{ fontSize: 10, flexShrink: 0 }}>{f.agent_role}</Tag>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* ── QC Alert (review not passed) ── */}
          {review && !review.passed && (() => {
            const openCount = review.issues.filter((i: any) => i.status === 'open').length;
            const manualCount = review.issues.filter((i: any) => i.status === 'needs_manual').length;
            if (openCount === 0 && manualCount === 0) return null;
            const onlyManual = openCount === 0 && manualCount > 0;
            return (
              <Alert
                type={onlyManual ? 'info' : 'warning'}
                showIcon
                style={{ marginTop: 10, fontSize: 12 }}
                message={
                  onlyManual
                    ? `剩余 ${manualCount} 个问题需人工介入，可以先确认阶段完成，再交给全栈工程师处理`
                    : `质检发现 ${openCount} 个问题待修复${manualCount > 0 ? `，${manualCount} 个需人工介入` : ''}`
                }
                description={
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginTop: 6 }}>
                    <span style={{ fontSize: 11, color: '#595959' }}>
                      {onlyManual
                        ? '自动修复已达上限，请在全栈工程师工作台进行人工整改。'
                        : isLastPhase
                        ? '点击「一键返工」，最后阶段PM自动分析所有问题并触发对应专家修复，修复完成后重新质检。'
                        : '非最后阶段的问题，请在最后阶段质检时统一返工。'}
                    </span>
                    <div style={{ display: 'flex', gap: 8, flexShrink: 0, flexWrap: 'wrap' }}>
                      <Button size="small" onClick={() => onToggleSupChat(phase.phase_id)}>查看问题</Button>
                      {onlyManual ? (
                        <Button
                          size="small" type="primary"
                          style={{ background: '#722ed1', borderColor: '#722ed1' }}
                          icon={<BugOutlined />}
                          onClick={() => onNavigateEngineer(`/engineer/${projectId}?tab=repair`)}
                        >
                          🜜 交给全栈工程师
                        </Button>
                      ) : isLastPhase ? (() => {
                        const openIssuesForAlert = review.issues.filter((i: any) => i.status === 'open');
                        const fileSetForAlert = new Set(openIssuesForAlert.map((i: any) => i.file_path || '（未知文件）'));
                        const batchFileCount = Math.min(fileSetForAlert.size, 5);
                        const totalFileCount = fileSetForAlert.size;
                        return (
                          <Button
                            size="small" danger
                            icon={batchSubmitting === phase.phase_id ? <LoadingOutlined /> : <PlayCircleOutlined />}
                            loading={batchSubmitting === phase.phase_id}
                            onClick={() => onSubmitIssues(phase.phase_id)}
                          >
                            一键返工（{batchFileCount}/{totalFileCount} 个文件）
                          </Button>
                        );
                      })() : null}
                    </div>
                  </div>
                }
              />
            );
          })()}

          {/* ── Completion Banner ── */}
          {isCompleted && (
            <div style={{
              marginTop: 10, padding: '8px 14px', border: '1px solid #52c41a',
              borderRadius: 6, background: '#f6ffed', color: '#389e0d',
              fontSize: 12, display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span style={{ fontSize: 14 }}>✅</span>
              <span>该阶段已完成；{qcSummary}</span>
            </div>
          )}

          {/* ── Review Passed Banner ── */}
          {reviewPassed && !isCompleted && (
            <div style={{
              marginTop: 10, padding: '8px 14px', border: '1px solid #fa8c16',
              borderRadius: 6, background: '#fff7e6', color: '#d46b08',
              fontSize: 12, display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span style={{ fontSize: 14 }}>⏳</span>
              <span>{qcSummary}；要开启下一阶段，请点击【确认阶段完成】</span>
            </div>
          )}
          <PhaseQualityRunPanel state={autoRepairState} />
        </div>
      )}

      {/* ── Children (PM/Supervisor Chat panels from index.tsx) ── */}
      {expanded && children}
    </div>
  );
};

export default PhaseCard;
