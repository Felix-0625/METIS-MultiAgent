/**
 * PM 组长对话页面
 * 职责：与用户沟通需求 → 制定总规划 → 划分阶段 → 用户确认
 * 确认后阶段看板自动更新，不在此页面启动项目
 */

import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Button, Input, Tag, Steps, Modal, Divider, Alert, Upload, Tooltip, Segmented,
} from 'antd';
import {
  SendOutlined, CheckCircleOutlined, LoadingOutlined,
  EditOutlined, CrownOutlined, UserOutlined, FileTextOutlined,
  ArrowRightOutlined, PaperClipOutlined, ReloadOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { message as antMessage } from 'antd';
import { API_BASE_URL } from '../services/apiBase';
import {
  buildRequirementRevisionOperations,
  completeRequirementAttachmentFrom,
  effectiveMessageIntent,
  type MessageIntent,
  type RequirementAttachment,
} from '../utils/pmRequirementIntent';

const API = API_BASE_URL;
const { TextArea } = Input;

const FLOW_STEPS = [
  { title: '描述需求', desc: '告诉 PM 组长项目目标' },
  { title: '讨论规划', desc: 'PM 组长制定总规划和阶段划分' },
  { title: '确认总规划', desc: '用户确认后阶段看板自动更新' },
];

interface ChatMsg {
  role: 'user' | 'assistant';
  content: string;       // 显示内容（不含附件原文）
  fullContent?: string;  // 当前会话的对话上下文（含附件），不是 canonical requirements
  ts: number;
}

interface CanonicalRequirementsRef {
  revision: number;
  digest: string;
}

interface Phase {
  phase_id: string;
  name: string;
  description: string;
  duration: string;
  roles_needed: string[];
  agent_count: number;
}

interface DraftPlan {
  project_overview: string;
  core_features: string[];
  phases: Phase[];
  total_duration: string;
  risks: string[];
}

const textArray = (value: unknown): string[] => (
  Array.isArray(value)
    ? value.map(item => valueText(item)).filter((item): item is string => Boolean(item))
    : []
);

const normalizeDraftPlan = (raw: any): DraftPlan => ({
  project_overview: valueText(raw?.project_overview) || '',
  core_features: textArray(raw?.core_features),
  phases: (Array.isArray(raw?.phases) ? raw.phases : []).map((phase: any, index: number) => ({
    phase_id: valueText(phase?.phase_id) || `phase-${index + 1}`,
    name: valueText(phase?.name) || `阶段 ${index + 1}`,
    description: valueText(phase?.description) || '',
    duration: valueText(phase?.duration) || '待定',
    roles_needed: textArray(phase?.roles_needed),
    agent_count: Number.isFinite(Number(phase?.agent_count)) ? Number(phase.agent_count) : 0,
  })),
  total_duration: valueText(raw?.total_duration) || '待定',
  risks: textArray(raw?.risks),
});

type PlanStatus =
  | 'idle'
  | 'generating'
  | 'generated'
  | 'validation_failed'
  | 'model_failed'
  | 'saved'
  | 'confirmed';

interface PlanViolation {
  field: string;
  message: string;
  layer?: string;
  code?: string;
  expected?: string;
  actual?: string;
}

interface PlanProcessState {
  status: PlanStatus;
  message: string;
  violations: PlanViolation[];
  metadata: Record<string, unknown>;
  attempts: PlanStatus[];
}

const PLAN_STATUS_META: Record<PlanStatus, {
  label: string;
  color: string;
  alertType: 'info' | 'success' | 'warning' | 'error';
}> = {
  idle: { label: '尚未生成', color: 'default', alertType: 'info' },
  generating: { label: '生成中', color: 'processing', alertType: 'info' },
  generated: { label: '已生成', color: 'cyan', alertType: 'success' },
  validation_failed: { label: '校验失败', color: 'error', alertType: 'error' },
  model_failed: { label: '模型失败', color: 'error', alertType: 'error' },
  saved: { label: '已保存', color: 'blue', alertType: 'success' },
  confirmed: { label: '已确认', color: 'success', alertType: 'success' },
};

const valueText = (value: unknown): string | undefined => {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value); } catch { return String(value); }
};

const normalizeField = (value: unknown, fallback = '总规划'): string => {
  if (Array.isArray(value)) return value.map(String).join('.');
  if (typeof value !== 'string' || !value.trim()) return fallback;
  return value.replace(/^\//, '').replace(/\//g, '.') || fallback;
};

// 后端校验器可能返回字符串、JSON Schema 风格对象，或 field -> errors 映射。
// 这里统一为可直接展示的字段级违规项，避免把所有失败压缩成“请重试”。
const normalizeViolations = (raw: unknown, parentField = ''): PlanViolation[] => {
  if (raw === undefined || raw === null || raw === '') return [];
  if (Array.isArray(raw)) return raw.flatMap(item => normalizeViolations(item, parentField));
  if (typeof raw === 'string') {
    const text = raw.trim();
    if (!text) return [];
    const parsed = text.match(/^(?:\[([^\]]+)\]\s*)?([A-Za-z_][\w.[\]/-]*):\s*(.+)$/);
    return [{
      field: normalizeField(parsed?.[2], parentField || '总规划'),
      code: parsed?.[1],
      message: parsed?.[3] || text,
    }];
  }
  if (typeof raw !== 'object') {
    return [{ field: parentField || '总规划', message: String(raw) }];
  }

  const item = raw as Record<string, unknown>;
  const fieldValue = item.field ?? item.path ?? item.instance_path ?? item.instancePath ?? item.pointer;
  const messageValue = item.message ?? item.detail ?? item.reason ?? item.description;
  if (fieldValue !== undefined || messageValue !== undefined) {
    return [{
      field: normalizeField(fieldValue, parentField || '总规划'),
      message: valueText(messageValue) || '未通过约束校验',
      layer: valueText(item.layer),
      code: valueText(item.code ?? item.rule ?? item.keyword ?? item.type),
      expected: valueText(item.expected),
      actual: valueText(item.actual ?? item.received),
    }];
  }

  return Object.entries(item).flatMap(([field, value]) => {
    const nextField = parentField ? `${parentField}.${field}` : field;
    return normalizeViolations(value, nextField);
  });
};

const responseViolations = (payload: any): PlanViolation[] => {
  const detail = payload?.detail && typeof payload.detail === 'object' ? payload.detail : undefined;
  const artifact = payload?.draft_plan || payload?.blocked_draft || detail?.draft_plan;
  const validation = payload?.validation || payload?.validation_result || artifact?.artifact_metadata?.validation;
  const detailValidation = detail?.validation || detail?.validation_result;
  const candidates = [
    validation?.violations,
    validation?.errors,
    validation?.issues,
    validation?.field_errors,
    detailValidation?.violations,
    detailValidation?.errors,
    detailValidation?.issues,
    detailValidation?.field_errors,
    payload?.violations,
    payload?.errors,
    detail?.violations,
    detail?.errors,
    payload?.draft_blocked_reason
      ? { path: 'draft_plan', code: 'blocked_draft', message: payload.draft_blocked_reason }
      : undefined,
  ];
  const seen = new Set<string>();
  const seenMessages = new Set<string>();
  return candidates.flatMap(candidate => normalizeViolations(candidate)).filter(item => {
    const key = `${item.field}|${item.layer || ''}|${item.code || ''}|${item.message}|${item.expected || ''}|${item.actual || ''}`;
    if (seen.has(key)) return false;
    if (item.field === '总规划' && seenMessages.has(item.message)) return false;
    seen.add(key);
    seenMessages.add(item.message);
    return true;
  });
};

const statusFromResponse = (payload: any, fallback: PlanStatus): PlanStatus => {
  const detail = payload?.detail && typeof payload.detail === 'object' ? payload.detail : undefined;
  const artifact = payload?.draft_plan || payload?.blocked_draft || detail?.draft_plan;
  const modelStatus = payload?.model_status || payload?.generation?.model_status || detail?.model_status || detail?.generation?.model_status;
  const rawStatus = String(payload?.status || detail?.status || modelStatus || artifact?.status || '').toLowerCase();
  const violations = responseViolations(payload);
  const validation = payload?.validation || payload?.validation_result || artifact?.artifact_metadata?.validation;
  const detailValidation = detail?.validation || detail?.validation_result;
  const validationFailed = validation?.valid === false || detailValidation?.valid === false;
  // A model attempt may fail while a deterministic contract fallback is
  // successfully validated and saved. The terminal artifact status wins;
  // model attempt failures remain visible in the attempts timeline.
  if (payload?.success !== false && detail?.success !== false && !validationFailed && violations.length === 0) {
    if (rawStatus === 'confirmed') return 'confirmed';
    if (rawStatus === 'saved') return 'saved';
  }
  if (violations.length > 0 || validationFailed || rawStatus.includes('validation') || rawStatus.includes('contract') || rawStatus.includes('schema')) {
    return 'validation_failed';
  }
  if (String(modelStatus || '').toLowerCase() === 'model_failed') return 'model_failed';
  if (payload?.success === false || detail?.success === false) return 'model_failed';
  const exact: PlanStatus[] = ['generating', 'generated', 'validation_failed', 'model_failed', 'saved', 'confirmed'];
  if (exact.includes(rawStatus as PlanStatus)) return rawStatus as PlanStatus;
  if (rawStatus.includes('confirm') || payload?.plan_confirmed === true || detail?.plan_confirmed === true) return 'confirmed';
  if (rawStatus.includes('model') || rawStatus.includes('generation') || rawStatus.includes('invalid_json')) return 'model_failed';
  if (rawStatus.includes('save')) return 'saved';
  return fallback;
};

const processFromResponse = (payload: any, fallback: PlanStatus, fallbackMessage: string): PlanProcessState => {
  const status = statusFromResponse(payload, fallback);
  const detail = payload?.detail;
  const message = valueText(payload?.message)
    || (typeof detail === 'string' ? detail : valueText(detail?.message))
    || valueText(payload?.draft_blocked_reason)
    || fallbackMessage;
  const artifact = payload?.draft_plan || payload?.blocked_draft || detail?.draft_plan || {};
  const artifactMetadata = artifact?.artifact_metadata && typeof artifact.artifact_metadata === 'object'
    ? artifact.artifact_metadata : {};
  const responseMetadata = payload?.metadata && typeof payload.metadata === 'object'
    ? payload.metadata
    : (detail?.metadata && typeof detail.metadata === 'object' ? detail.metadata : {});
  const validation = payload?.validation || payload?.validation_result || artifactMetadata?.validation || {};
  const metadata: Record<string, unknown> = {
    ...artifactMetadata,
    ...responseMetadata,
    contract_version: responseMetadata.contract_version ?? validation.contract_version ?? artifact?.project_contract?.contract_version,
    plan_version: responseMetadata.plan_version ?? artifact?.plan_version ?? artifact?.version,
    phase_plan_version: responseMetadata.phase_plan_version ?? artifact?.phase_plan_version,
    requirements_revision: payload?.requirements_revision
      ?? detail?.requirements_revision
      ?? responseMetadata.requirements_revision
      ?? artifactMetadata.requirements_revision,
    requirements_digest: payload?.requirements_digest
      ?? detail?.requirements_digest
      ?? responseMetadata.requirements_digest
      ?? artifactMetadata.requirements_digest
      ?? artifact?.project_contract?.requirements_digest,
    source: responseMetadata.source ?? artifact?.source,
  };
  const rawAttempts = payload?.generation?.attempts || detail?.generation?.attempts || [];
  const attempts = (Array.isArray(rawAttempts) ? rawAttempts : []).flatMap((attempt: any) => {
    const attemptStatus = statusFromResponse(attempt, 'idle');
    return attemptStatus === 'idle' ? [] : [attemptStatus];
  });
  return { status, message, violations: responseViolations(payload), metadata, attempts };
};

const metadataEntries = (metadata: Record<string, unknown>): Array<[string, string]> => {
  const labels: Record<string, string> = {
    contract_version: '契约版本',
    plan_version: '规划版本',
    phase_plan_version: '阶段版本',
    requirements_revision: '需求修订',
    requirements_digest: '需求摘要',
    source: '来源',
    generated_by: '生成来源',
    auto_corrections: '自动修正',
  };
  return Object.entries(labels).flatMap(([key, label]) => {
    const value = valueText(metadata[key]);
    return value === undefined ? [] : [[label, value] as [string, string]];
  });
};

// canonical_requirements 的正文只由服务端维护。前端仅持有不可变版本引用，
// synthesize 时回传 revision + digest，绝不从聊天历史重建用户约束。
const canonicalRequirementsRefFrom = (payload: any): CanonicalRequirementsRef | null => {
  const detail = payload?.detail && typeof payload.detail === 'object' ? payload.detail : {};
  const artifact = payload?.draft_plan || payload?.blocked_draft || detail?.draft_plan || {};
  const metadata = payload?.metadata && typeof payload.metadata === 'object'
    ? payload.metadata
    : (detail?.metadata && typeof detail.metadata === 'object' ? detail.metadata : {});
  const artifactMetadata = artifact?.artifact_metadata && typeof artifact.artifact_metadata === 'object'
    ? artifact.artifact_metadata : {};
  const revisionValue = payload?.requirements_revision
    ?? detail?.requirements_revision
    ?? metadata?.requirements_revision
    ?? artifactMetadata?.requirements_revision;
  const digestValue = payload?.requirements_digest
    ?? detail?.requirements_digest
    ?? metadata?.requirements_digest
    ?? artifactMetadata?.requirements_digest
    ?? artifact?.project_contract?.requirements_digest;
  const revision = Number(revisionValue);
  const digest = typeof digestValue === 'string' ? digestValue.trim().toLowerCase() : '';
  if (!Number.isInteger(revision) || revision < 1 || !/^sha256:[0-9a-f]{64}$/.test(digest)) {
    return null;
  }
  return { revision, digest };
};

const sha256Text = async (text: string): Promise<string> => {
  const bytes = new TextEncoder().encode(text);
  const digest = await window.crypto.subtle.digest('SHA-256', bytes);
  return `sha256:${Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('')}`;
};

const PMTeamChat: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [flowStep, setFlowStep] = useState(0);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [draftPlan, setDraftPlan] = useState<DraftPlan | null>(null);
  const [planProcess, setPlanProcess] = useState<PlanProcessState>({
    status: 'idle', message: '与 PM 组长明确需求后生成总规划草稿', violations: [], metadata: {}, attempts: [],
  });
  const [planConfirmed, setPlanConfirmed] = useState(false);
  const [creatingPlan, setCreatingPlan] = useState(false);
  const [contextSummary, setContextSummary] = useState('');
  const [showModify, setShowModify] = useState(false);
  const [modifyText, setModifyText] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [attachments, setAttachments] = useState<RequirementAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [canonicalRequirements, setCanonicalRequirements] = useState<CanonicalRequirementsRef | null>(null);
  const [messageIntent, setMessageIntent] = useState<MessageIntent>('replace');
  const msgBoxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (projectId) {
      // 路由切换时先清空上一项目的本地态，避免旧草稿短暂污染新项目。
      setDraftPlan(null);
      setPlanConfirmed(false);
      setCanonicalRequirements(null);
      setMessageIntent('replace');
      setAttachments([]);
      setPlanProcess({ status: 'idle', message: '正在加载规划状态', violations: [], metadata: {}, attempts: [] });
      setFlowStep(0);
      loadHistory();
      loadPlan();
    }
  }, [projectId]);
  // 只滚动消息容器内部，不触发页面级滚动
  useEffect(() => {
    if (msgBoxRef.current) {
      msgBoxRef.current.scrollTop = msgBoxRef.current.scrollHeight;
    }
  }, [msgs, sending]);

  // 加载持久化对话历史
  const loadHistory = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/chat-history/pm_leader`);
      if (res.data.messages?.length > 0) {
        setMsgs(res.data.messages.map((m: any) => ({
          role: m.role,
          content: m.content,
          ts: m.ts || Date.now(),
        })));
        setFlowStep(1);
      }
    } catch { /* 静默 */ }
  };

  // 保存对话历史（只保存显示内容，不保存附件原文）
  const saveHistory = async (messages: ChatMsg[]) => {
    try {
      await axios.post(`${API}/projects/${projectId}/chat-history/pm_leader`, {
        messages: messages.map(m => ({ role: m.role, content: m.content, ts: m.ts })),
      });
    } catch { /* 静默 */ }
  };

  const loadPlan = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/pm-team/plan`);
      const canonicalRef = canonicalRequirementsRefFrom(res.data);
      if (canonicalRef) {
        setCanonicalRequirements(canonicalRef);
        setMessageIntent('append');
      }
      const restoredPlan = res.data.draft_plan || res.data.final_plan;
      if (restoredPlan) {
        setDraftPlan(normalizeDraftPlan(restoredPlan));
        setFlowStep(res.data.plan_confirmed ? 2 : 1);
      }
      setPlanConfirmed(Boolean(res.data.plan_confirmed));
      if (res.data.plan_confirmed) {
        setPlanProcess(processFromResponse(res.data, 'confirmed', '总规划已确认'));
      } else if (restoredPlan) {
        // 能由查询接口恢复的草稿已经持久化，刷新后明确显示为“已保存”。
        setPlanProcess(processFromResponse(res.data, 'saved', '总规划草稿已保存'));
      } else {
        const restored = processFromResponse(res.data, 'idle', '尚未生成总规划草稿');
        if (restored.status !== 'idle' || restored.violations.length > 0) setPlanProcess(restored);
      }
    } catch { /* 静默 */ }
  };

  const sendMsg = async () => {
    if (!input.trim() || sending) return;
    const displayText = input.trim();
    const attachmentSnapshot = [...attachments];
    const intent = effectiveMessageIntent(messageIntent, attachmentSnapshot);
    const attachmentDiscussionText = attachmentSnapshot
      .map(attachment => `=== 文件：${attachment.name} ===\n${attachment.text}`)
      .join('\n\n');
    // 发给后端的内容：用户文字 + 附件上下文（附件内容不显示在气泡里）
    const fullText = attachmentDiscussionText
      ? `${displayText}\n\n【参考附件内容】\n${attachmentDiscussionText}`
      : displayText;
    const userMsg: ChatMsg = { role: 'user', content: displayText, fullContent: fullText, ts: Date.now() };
    const newMsgs: ChatMsg[] = [...msgs, userMsg];
    setSending(true);
    if (flowStep === 0) setFlowStep(1);
    try {
      const revisionOperations = buildRequirementRevisionOperations(
        intent,
        displayText,
        attachmentSnapshot,
      );
      if (revisionOperations.length > 0) {
        let expected = canonicalRequirements ?? { revision: 0, digest: '' };
        for (const operation of revisionOperations) {
          const revisionRes: any = await axios.post(
            `${API}/projects/${projectId}/pm-team/requirements/revise`,
            {
              content: operation.content,
              expected_revision: expected.revision,
              expected_digest: expected.digest,
              source: operation.source,
              replace: operation.replace,
              supersedes: [],
            },
          );
          const revisedRef = canonicalRequirementsRefFrom(revisionRes.data);
          if (!revisedRef) {
            throw new Error('服务端未返回可验证的需求修订引用');
          }
          expected = revisedRef;
        }
        const revisedRef = expected;
        setCanonicalRequirements(revisedRef);
        setDraftPlan(null);
        setPlanConfirmed(false);
        setPlanProcess({
          status: 'idle',
          message: '需求已更新，请重新生成总规划草稿',
          violations: [],
          metadata: {
            requirements_revision: revisedRef.revision,
            requirements_digest: revisedRef.digest,
          },
          attempts: [],
        });
        setMessageIntent('append');
      }

      setMsgs(newMsgs);
      setInput('');
      setAttachments([]);
      // 传 fullContent（含附件）作为历史，确保 Agent 记得附件内容
      // 这里只发送讨论上下文；canonical requirements 已由显式 revise API 更新。
      const res: any = await axios.post(`${API}/projects/${projectId}/pm-team/chat`, {
        message: fullText,
        history: msgs.slice(-12).map(m => ({ role: m.role, content: m.fullContent || m.content })),
        context_summary: contextSummary,
      });
      const canonicalRef = canonicalRequirementsRefFrom(res.data);
      if (canonicalRef) setCanonicalRequirements(canonicalRef);
      const reply = res.data.reply || '';
      const finalMsgs: ChatMsg[] = [...newMsgs, { role: 'assistant', content: reply, ts: Date.now() }];
      setMsgs(finalMsgs);
      await saveHistory(finalMsgs);
      if (res.data.summary) setContextSummary(res.data.summary);
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      const failureMessage = typeof detail === 'string'
        ? detail
        : detail?.message || e.message || '发送失败';
      const refreshedRef = canonicalRequirementsRefFrom(e.response?.data);
      if (refreshedRef) setCanonicalRequirements(refreshedRef);
      antMessage.error(failureMessage);
    } finally {
      setSending(false);
    }
  };

  // 生成总规划草稿（直接用 PM 组长一次性生成，不走多成员分析，速度快）
  const createPlan = async () => {
    if (attachments.length > 0) {
      antMessage.warning('请先发送已附加文件，使服务端保存新的需求修订后再生成规划');
      return;
    }
    // 刷新可能先恢复聊天再恢复规划元数据；生成前从服务端再取一次权威引用。
    let requirementsRef = canonicalRequirements;
    if (!requirementsRef) {
      try {
        const res: any = await axios.get(`${API}/projects/${projectId}/pm-team/plan`);
        requirementsRef = canonicalRequirementsRefFrom(res.data);
        if (requirementsRef) setCanonicalRequirements(requirementsRef);
      } catch { /* synthesize 前统一给出 fail-closed 提示 */ }
    }
    if (!requirementsRef) {
      antMessage.warning('服务端尚未保存可验证的用户需求，请先发送需求消息');
      return;
    }
    setCreatingPlan(true);
    setPlanProcess({ status: 'generating', message: '正在生成并校验总规划草稿', violations: [], metadata: {}, attempts: ['generating'] });
    // 显示进度提示（生成规划通常需要 10-30 秒）
    const progressKey = 'plan-progress';
    antMessage.loading({ content: '正在生成总规划草稿，通常需要 10-30 秒，请稍候...', key: progressKey, duration: 0 });
    try {
      const synthRes: any = await axios.post(`${API}/projects/${projectId}/pm-team/synthesize`, {
        requirements_revision: requirementsRef.revision,
        requirements_digest: requirementsRef.digest,
        fast_mode: true,
      }, { timeout: 150000 });
      const nextCanonicalRef = canonicalRequirementsRefFrom(synthRes.data);
      if (nextCanonicalRef) setCanonicalRequirements(nextCanonicalRef);
      const synthesisState = processFromResponse(synthRes.data, 'generated', '总规划草稿已生成并通过校验');
      if (synthRes.data?.success === false || synthesisState.status === 'validation_failed' || synthesisState.status === 'model_failed') {
        const failed = synthesisState.status === 'generated'
          ? { ...synthesisState, status: 'model_failed' as PlanStatus }
          : synthesisState;
        setPlanProcess(failed);
        antMessage.error(failed.status === 'validation_failed' ? '总规划未通过契约校验' : failed.message);
        return;
      }
      const plan = synthRes.data?.draft_plan;
      if (plan && typeof plan === 'object') {
        setDraftPlan(normalizeDraftPlan(plan));
        setFlowStep(1);
        setPlanProcess(synthesisState);
        antMessage.success('总规划草稿已生成，请查看右侧并确认');
      } else {
        setPlanProcess({ ...synthesisState, status: 'model_failed',
          message: synthesisState.message || '模型响应中缺少可用的 draft_plan',
          violations: synthesisState.violations.length > 0
            ? synthesisState.violations
            : [{ field: 'draft_plan', message: '响应不是有效的规划对象' }],
        });
        antMessage.error('生成失败：模型响应中缺少可用规划');
      }
    } catch (e: any) {
      antMessage.destroy(progressKey);
      const failed = processFromResponse(e.response?.data || {}, 'model_failed', e.message || '生成失败');
      setPlanProcess(failed);
      antMessage.error(failed.status === 'validation_failed' ? '总规划未通过契约校验' : failed.message);
    } finally {
      antMessage.destroy(progressKey);
      setCreatingPlan(false);
    }
  };

  // 确认总规划 → 同步到阶段看板
  const confirmPlan = async (modifications = '') => {
    if (!canonicalRequirements) {
      antMessage.error('缺少服务端需求修订引用，请刷新规划后重试');
      return;
    }
    setConfirming(true);
    try {
      const res: any = await axios.post(`${API}/projects/${projectId}/pm-team/confirm-plan`, {
        modifications,
        requirements_revision: canonicalRequirements.revision,
        requirements_digest: canonicalRequirements.digest,
      });
      const confirmationState = processFromResponse(res.data, 'saved', '总规划尚未确认');
      if (confirmationState.status === 'confirmed') {
        // 等待 phases/init 完成后再更新状态和跳转，避免阶段看板空白
        try {
          await axios.post(`${API}/projects/${projectId}/phases/init`);
        } catch {
          // phases/init 失败不阻断流程，阶段看板会显示空状态提示用户
        }
        setPlanConfirmed(true);
        setFlowStep(2);
        setPlanProcess(confirmationState);
        setShowModify(false);
        antMessage.success({ content: '✅ 总规划已确认，阶段看板已更新！', duration: 2 });
        // 等 success 提示显示完再跳转
        await new Promise(r => setTimeout(r, 1500));
        navigate(`/projects/${projectId}/phase-board`);
      } else {
        const failed = confirmationState;
        setPlanProcess(failed);
        if (failed.status === 'validation_failed') {
          antMessage.error('确认被阻止：总规划未通过契约校验');
        } else if (failed.status === 'model_failed') {
          antMessage.error(failed.message);
        } else {
          antMessage.info(failed.message);
        }
        setShowModify(false);
      }
    } catch (e: any) {
      const failed = processFromResponse(e.response?.data || {}, 'model_failed', e.message || '确认失败');
      setPlanProcess(failed);
      antMessage.error(failed.message);
    } finally {
      setConfirming(false);
    }
  };

  // 上传文件：逐文件保留完整提取结果，删除标签会删除对应正文。
  const handleUpload = async (file: File) => {
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('agent_type', 'pm');
      formData.append('files', file);
      const res: any = await axios.post(
        `${API}/projects/${projectId}/upload-file`,
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } }
      );
      const extracted = completeRequirementAttachmentFrom(res.data);
      if (!extracted) {
        const rawFile = Array.isArray(res.data?.files) ? res.data.files[0] : null;
        const reason = rawFile?.reason || res.data?.reason || 'extraction_incomplete';
        antMessage.error(`「${file.name}」解析不完整（${reason}），未加入需求修订`);
        return false;
      }
      const digest = await sha256Text(extracted.text);
      if (attachments.some(item => item.digest === digest)) {
        antMessage.info(`「${file.name}」内容已附加，无需重复加入`);
        return false;
      }
      const uid = window.crypto.randomUUID?.()
        ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      setAttachments(previous => [...previous, {
        uid,
        name: extracted.name || file.name,
        digest,
        text: extracted.text,
      }]);
      setMessageIntent('append');
      antMessage.success(`「${file.name}」已完整解析并附加`);
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      const failedFile = Array.isArray(detail?.files) ? detail.files[0] : null;
      const reason = failedFile?.reason || detail?.message || '上传失败';
      antMessage.error(`「${file.name}」未附加（${reason}）`);
    } finally {
      setUploading(false);
    }
    return false;
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  };
  const guideStep = planConfirmed ? 2 : draftPlan ? 1 : 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', gap: 12, minHeight: 0 }}>
      {/* 流程步骤 */}
      <div className="metis-card" style={{ padding: '12px 16px', flexShrink: 0 }}>
        <Steps current={guideStep} size="small" items={FLOW_STEPS.map((s, i) => ({
          title: s.title,
          description: i === guideStep ? <span style={{ fontSize: 11, color: '#1677ff' }}>{s.desc}</span> : undefined,
          status: i < guideStep ? 'finish' : i === guideStep ? 'process' : 'wait',
          icon: i < guideStep ? <CheckCircleOutlined /> : undefined,
        }))} />
      </div>

      <div style={{ display: 'flex', gap: 12, flex: 1, minHeight: 0 }}>
        {/* 左侧：对话区 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', background: '#fff', borderRadius: 8, border: '1px solid #e8e8e8', minWidth: 0, overflow: 'hidden' }}>
          {/* 头部 */}
          <div style={{ padding: '10px 16px', borderBottom: '1px solid #f0f0f0', display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
            <CrownOutlined style={{ color: '#faad14' }} />
            <span style={{ fontWeight: 600, fontSize: 14 }}>PM 组长</span>
            <span style={{ fontSize: 11, color: '#8c8c8c' }}>负责总规划和阶段划分</span>
            {planConfirmed && <Tag color="success">总规划已确认</Tag>}
          </div>

          {/* 消息列表 */}
          <div ref={msgBoxRef} style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 12, minHeight: 0 }}>
            {msgs.length === 0 && (
              <div style={{ textAlign: 'center', color: '#bfbfbf', marginTop: 40 }}>
                <CrownOutlined style={{ fontSize: 40, display: 'block', marginBottom: 12 }} />
                <p style={{ fontSize: 14 }}>请描述您的项目目标和核心需求</p>
                <p style={{ fontSize: 12, marginTop: 4 }}>PM 组长将帮您制定总规划和阶段划分</p>
                <p style={{ fontSize: 11, color: '#d9d9d9', marginTop: 4 }}>注意：PM 组长只负责规划，不讨论代码实现</p>
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} style={{ display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start' }}>
                {m.role === 'assistant' && (
                  <div style={{ width: 28, height: 28, borderRadius: '50%', background: '#fffbe6', display: 'flex', alignItems: 'center', justifyContent: 'center', marginRight: 8, flexShrink: 0, marginTop: 2 }}>
                    <CrownOutlined style={{ color: '#faad14', fontSize: 12 }} />
                  </div>
                )}
                <div style={{
                  maxWidth: '78%', borderRadius: 8, padding: '8px 12px', fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.6,
                  background: m.role === 'user' ? '#1677ff' : '#f5f5f5',
                  color: m.role === 'user' ? '#fff' : '#262626',
                }}>
                  {/* 只显示 content（不含附件原文） */}
                  {m.content}
                </div>
                {m.role === 'user' && (
                  <div style={{ width: 28, height: 28, borderRadius: '50%', background: '#e6f4ff', display: 'flex', alignItems: 'center', justifyContent: 'center', marginLeft: 8, flexShrink: 0, marginTop: 2 }}>
                    <UserOutlined style={{ color: '#1677ff', fontSize: 12 }} />
                  </div>
                )}
              </div>
            ))}
            {sending && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ width: 28, height: 28, borderRadius: '50%', background: '#fffbe6', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <LoadingOutlined style={{ color: '#faad14', fontSize: 12 }} />
                </div>
                <div style={{ background: '#f5f5f5', borderRadius: 8, padding: '8px 12px', fontSize: 13, color: '#8c8c8c' }}>PM 组长思考中...</div>
              </div>
            )}
          </div>

          {/* 输入区 */}
          <div style={{ padding: 12, borderTop: '1px solid #f0f0f0', flexShrink: 0 }}>
            {planConfirmed ? (
              <Alert
                type="success"
                showIcon
                message="总规划已确认，阶段看板已更新"
                action={
                  <Button size="small" type="primary" icon={<ArrowRightOutlined />}
                    onClick={() => navigate(`/projects/${projectId}/phase-board`)}>
                    前往阶段看板
                  </Button>
                }
              />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <Alert
                  type={canonicalRequirements ? 'success' : 'info'}
                  showIcon
                  message={canonicalRequirements
                    ? '需求已保存：下一步点击“生成标准规划草稿”'
                    : '第一步：提交完整需求；格式不限，PM 组长会统一整理'}
                />
                {/* 已附加文件标签 */}
                {attachments.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {attachments.map(attachment => (
                      <Tag key={attachment.uid} icon={<PaperClipOutlined />} closable
                        onClose={() => {
                          setAttachments(previous => (
                            previous.filter(item => item.uid !== attachment.uid)
                          ));
                        }}
                        style={{ fontSize: 11 }}>
                        {attachment.name}
                      </Tag>
                    ))}
                    <span style={{ fontSize: 11, color: '#8c8c8c', alignSelf: 'center' }}>
                      （每个保留附件将成为独立需求事件）
                    </span>
                  </div>
                )}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <Segmented
                    size="small"
                    value={messageIntent}
                    onChange={value => setMessageIntent(value as MessageIntent)}
                    options={[
                      {
                        label: '仅咨询',
                        value: 'discussion',
                        disabled: attachments.length > 0,
                      },
                      { label: '追加或澄清', value: 'append' },
                      { label: '提交完整需求', value: 'replace' },
                    ]}
                    disabled={sending}
                  />
                  <span style={{
                    fontSize: 11,
                    color: messageIntent === 'replace' ? '#d97706' : '#8c8c8c',
                  }}>
                    {messageIntent === 'discussion'
                      ? '只参与讨论，不改变需求修订'
                      : messageIntent === 'append'
                        ? '补充内容会并入已保存需求，并使旧草稿失效'
                        : '格式不限；本次内容将成为生成规划的完整需求'}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <TextArea
                    autoSize={{ minRows: 2, maxRows: 6 }}
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={handleKey}
                    placeholder={
                      messageIntent === 'discussion'
                        ? '输入讨论或追问（不会更新需求）'
                        : messageIntent === 'replace'
                          ? '输入完整需求，可使用自然语言、列表或粘贴文档内容'
                          : '输入需要补充或澄清的需求'
                    }
                    disabled={sending}
                    style={{ flex: 1, resize: 'none' }}
                  />
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, alignSelf: 'flex-end' }}>
                    <Tooltip title="上传文件（内容将随下条消息发送，不显示在对话中）">
                      <Upload multiple showUploadList={false} beforeUpload={handleUpload}
                        accept=".txt,.md,.pdf,.docx,.doc,.json,.csv,.yaml,.yml"
                        disabled={uploading || sending}>
                        <Button size="small" icon={uploading ? <LoadingOutlined /> : <PaperClipOutlined />} loading={uploading}>
                          附件
                        </Button>
                      </Upload>
                    </Tooltip>
                    {msgs.length > 0 && !sending && (
                      <Tooltip title="重新发送上一条消息">
                        <Button size="small" icon={<ReloadOutlined />}
                          onClick={() => { const last = msgs.filter(m => m.role === 'user').slice(-1)[0]; if (last) { setInput(last.content); } }}>
                          重试
                        </Button>
                      </Tooltip>
                    )}
                    <Button type="primary" size="small" icon={<SendOutlined />} onClick={sendMsg}
                      loading={sending} disabled={!input.trim()}>
                      {messageIntent === 'discussion' ? '发送咨询' : canonicalRequirements ? '更新需求' : '保存需求'}
                    </Button>
                  </div>
                </div>
                {/* 生成总规划草稿按钮 */}
                <Button block icon={<FileTextOutlined />} loading={creatingPlan}
                  onClick={createPlan}
                  disabled={!canonicalRequirements || sending || creatingPlan}
                  style={{ marginTop: 4 }}>
                  生成标准规划草稿
                </Button>
              </div>
            )}
          </div>
        </div>

        {/* 右侧：总规划预览（始终显示，无草稿时显示占位） */}
        <div style={{ width: 380, flexShrink: 0, background: '#fff', borderRadius: 8, border: `1px solid ${draftPlan ? '#e8e8e8' : '#f0f0f0'}`, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ padding: '10px 14px', borderBottom: '1px solid #f0f0f0', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
            <div className="metis-section-header" style={{ marginBottom: 0 }}>
              <FileTextOutlined className="metis-header-icon" style={{ color: '#5b5ea6', fontSize: 14 }} />
              <span className="metis-header-title" style={{ fontSize: 13 }}>总规划草稿</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Tag color={PLAN_STATUS_META[planProcess.status].color} style={{ fontSize: 10, marginInlineEnd: 0 }}>
                {planProcess.status} · {PLAN_STATUS_META[planProcess.status].label}
              </Tag>
              <Tooltip title="刷新">
              <ReloadOutlined style={{ fontSize: 12, color: '#8c8c8c', cursor: 'pointer' }} onClick={async () => { await loadPlan(); antMessage.success('规划已刷新'); }} />
              </Tooltip>
            </div>
          </div>
          {planProcess.status !== 'idle' && (
            <Alert
              type={PLAN_STATUS_META[planProcess.status].alertType}
              showIcon
              icon={planProcess.status === 'generating' ? <LoadingOutlined /> : undefined}
              message={planProcess.message}
              description={(planProcess.violations.length > 0 || planProcess.attempts.length > 0 || metadataEntries(planProcess.metadata).length > 0) ? (
                <div style={{ marginTop: 4 }}>
                  {planProcess.attempts.length > 0 && (
                    <div style={{ marginBottom: 5 }}>
                      状态记录：{planProcess.attempts.map((status, index) => (
                        <React.Fragment key={`${status}-${index}`}>
                          {index > 0 && <span> → </span>}
                          <Tag color={PLAN_STATUS_META[status].color} style={{ fontSize: 9, marginInlineEnd: 0 }}>{status}</Tag>
                        </React.Fragment>
                      ))}
                    </div>
                  )}
                  {planProcess.violations.length > 0 && (
                    <>
                      <div style={{ fontWeight: 600, marginBottom: 3 }}>违规项（{planProcess.violations.length}）</div>
                      <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                        {planProcess.violations.map((violation, index) => (
                          <li key={`${violation.field}-${violation.layer || ''}-${violation.code || ''}-${index}`} style={{ marginBottom: 3 }}>
                            <span style={{ fontFamily: 'monospace', fontWeight: 600 }}>{violation.field}</span>
                            {(violation.layer || violation.code) && (
                              <Tag color="red" style={{ fontSize: 9, marginInlineStart: 5 }}>
                                {[violation.layer, violation.code].filter(Boolean).join('/')}
                              </Tag>
                            )}
                            <div>{violation.message}</div>
                            {(violation.expected !== undefined || violation.actual !== undefined) && (
                              <div style={{ color: '#8c8c8c' }}>
                                {violation.expected !== undefined && <span>期望：{violation.expected}</span>}
                                {violation.expected !== undefined && violation.actual !== undefined && <span>；</span>}
                                {violation.actual !== undefined && <span>实际：{violation.actual}</span>}
                              </div>
                            )}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                  {metadataEntries(planProcess.metadata).length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 5 }}>
                      {metadataEntries(planProcess.metadata).map(([label, value]) => (
                        <Tag key={label} style={{ fontSize: 9 }}>{label}：{value}</Tag>
                      ))}
                    </div>
                  )}
                </div>
              ) : undefined}
              style={{ margin: '10px 14px 0', fontSize: 11 }}
            />
          )}
          {!draftPlan ? (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', color: '#bfbfbf', padding: 24, textAlign: 'center' }}>
              <FileTextOutlined style={{ fontSize: 36, marginBottom: 12 }} />
              <p style={{ fontSize: 13 }}>草稿将在这里显示</p>
              <p style={{ fontSize: 11, marginTop: 4 }}>与 PM 组长描述需求后，点击「生成总规划草稿」</p>
            </div>
          ) : (
            <>
              <div style={{ flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 12, minHeight: 0 }}>
                <div>
                  <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 4 }}>项目目标</div>
                  <div style={{ fontSize: 12, color: '#262626', lineHeight: 1.6 }}>{draftPlan.project_overview}</div>
                </div>
                {draftPlan.core_features?.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 4 }}>核心功能</div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {draftPlan.core_features.map((f, i) => <Tag key={i} style={{ fontSize: 11 }}>{f}</Tag>)}
                    </div>
                  </div>
                )}
                <Divider style={{ margin: '4px 0' }} />
                {draftPlan.phases?.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>阶段划分（{draftPlan.phases.length} 个阶段）</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {draftPlan.phases.map((p, i) => (
                        <div key={p.phase_id || i} style={{ border: '1px solid #e8e8e8', borderRadius: 6, padding: '8px 10px', fontSize: 12 }}>
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                            <span style={{ fontWeight: 600, color: '#1677ff' }}>阶段{i + 1}：{p.name}</span>
                            <Tag style={{ fontSize: 10 }}>{p.duration || '待定'}</Tag>
                          </div>
                          <div style={{ color: '#595959', marginBottom: 4, lineHeight: 1.5 }}>{p.description}</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
                            {p.roles_needed?.map((r, j) => <Tag key={j} color="blue" style={{ fontSize: 10 }}>{r}</Tag>)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {draftPlan.risks?.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 3 }}>
                      <span style={{ color: '#d97706' }}>▲</span> 风险提示
                    </div>
                    {draftPlan.risks.map((r, i) => (
                      <div key={i} style={{ fontSize: 11, color: '#fa8c16', marginBottom: 2 }}>• {r}</div>
                    ))}
                  </div>
                )}
              </div>
              <div style={{ padding: '10px 14px', borderTop: '1px solid #f0f0f0', flexShrink: 0 }}>
                {!planConfirmed ? (
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button size="small" block icon={<EditOutlined />} onClick={() => setShowModify(true)}>
                      提出修改
                    </Button>
                    <Button type="primary" size="small" block icon={<CheckCircleOutlined />}
                      loading={confirming} onClick={() => confirmPlan()}
                      disabled={planProcess.status === 'generating' || planProcess.status === 'validation_failed' || planProcess.status === 'model_failed'}>
                      确认总规划
                    </Button>
                  </div>
                ) : (
                  <Button type="primary" block icon={<ArrowRightOutlined />}
                    onClick={() => navigate(`/projects/${projectId}/phase-board`)}
                    style={{ background: '#52c41a', borderColor: '#52c41a' }}>
                    前往阶段看板
                  </Button>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* 修改意见弹窗：只记录意见，不触发 confirm-plan */}
      <Modal title="提出修改意见" open={showModify}
        onCancel={() => setShowModify(false)}
        onOk={() => {
          if (!modifyText.trim()) {
            antMessage.warning('请填写修改意见');
            return;
          }
          setShowModify(false);
          const text = `【需求澄清】${modifyText.trim()}`;
          setModifyText('');
          setInput(text);
          setMessageIntent('append');
          antMessage.info('修改意见已放入“追加/澄清需求”，请检查后点击“更新需求”');
        }}
        okText="放入需求更新" cancelText="取消">
        <TextArea rows={4} placeholder="请描述希望修改的内容..."
          value={modifyText} onChange={e => setModifyText(e.target.value)} />
        <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 8 }}>
          修改意见会先进入结构化需求修订；发送成功后旧草稿失效，需要重新生成。
        </div>
      </Modal>
    </div>
  );
};

export default PMTeamChat;
