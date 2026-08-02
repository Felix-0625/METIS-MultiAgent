import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Button, Tag, Badge, Progress, Modal, Input, Alert,
  Skeleton, Spin, Tooltip, Divider, message, Upload, Card, Row, Col, Statistic, Collapse,
} from 'antd';
import {
  PlayCircleOutlined, AuditOutlined, CheckCircleOutlined,
  ClockCircleOutlined, LoadingOutlined, FileOutlined,
  UserOutlined, EditOutlined, BugOutlined,
  ReloadOutlined, CrownOutlined, SafetyOutlined, PaperClipOutlined,
  TeamOutlined, OrderedListOutlined, WarningOutlined,
  ExperimentOutlined, ThunderboltOutlined, SecurityScanOutlined,
  SafetyCertificateOutlined, RiseOutlined, FallOutlined,
  BarChartOutlined, DashboardOutlined, InfoCircleOutlined, ArrowRightOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import {
  PhaseInfo, Issue, ReviewResult, ChatMsg, FileRecord,
  DefectTicket, RepairSummary, ArbiterResult, ExpertReq,
  PhasePlan, AutoRepairAction, AutoRepairDecision, AutoRepairStatus,
  API, parsePlanFromMessages, formatIssueForPm,
} from './types';
import TeamHealth from './TeamHealth';
import ReviewChain from './ReviewChain';
import PhaseCard from './PhaseCard';
import DefectList from './DefectList';
import CrossStageConflicts from './CrossStageConflicts';
import PlanSidebar from './PlanSidebar';
import { wsService } from '../../services/websocket';

const { TextArea } = Input;
const FILE_BATCH_LIMIT = 5;

const apiErrorText = (error: any, fallback: string): string => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message.trim()) {
      return detail.message;
    }
    try { return JSON.stringify(detail); } catch { /* fall through */ }
  }
  return typeof error?.message === 'string' && error.message.trim()
    ? error.message
    : fallback;
};

// ─── 通用对话面板（终端风格，支持文件上传）────────────────────────────────────
const ChatPanel: React.FC<{
  title: string;
  prefix: string;
  color: string;
  messages: ChatMsg[];
  sending: boolean;
  onSend: (text: string) => void;
  placeholder?: string;
  extraContent?: React.ReactNode;
  projectId?: string;
  agentType?: 'pm' | 'supervisor';
  bottomActions?: React.ReactNode;
  height?: number | string;
}> = ({ title, prefix, color, messages, sending, onSend, placeholder, extraContent, projectId, agentType = 'pm', bottomActions, height = 380 }) => {
  const [val, setVal] = useState('');
  const [uploading, setUploading] = useState(false);
  const attachedTextsRef = useRef<string[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const scrollBoxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollBoxRef.current) {
      scrollBoxRef.current.scrollTop = scrollBoxRef.current.scrollHeight;
    }
  }, [messages, sending]);

  const send = () => {
    if (!val.trim() && attachedTextsRef.current.length === 0) return;
    if (sending) return;
    const userText = val.trim();
    const attachedContent = attachedTextsRef.current.join('\n\n');
    const fullText = attachedContent
      ? (userText ? `${userText}\n\n${attachedContent}` : attachedContent)
      : userText;
    setVal('');
    attachedTextsRef.current = [];
    onSend(fullText);
  };

  const handleUpload = async (file: File) => {
    if (!projectId) return false;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('agent_type', agentType);
      formData.append('files', file);
      const res: any = await axios.post(`${API}/projects/${projectId}/upload-file`, formData,
        { headers: { 'Content-Type': 'multipart/form-data' } });
      const extractedText = res.data.combined_text || '';
      if (extractedText) {
        attachedTextsRef.current = [...attachedTextsRef.current, `[file:${file.name}]\n${extractedText}`];
        setVal(prev => prev ? `${prev} [${file.name}]` : `[${file.name}]`);
        message.success(`${file.name} 已附加（${extractedText.length} 字符）`);
      } else {
        message.warning(`${file.name} 未提取到文本`);
      }
    } catch { message.error(`${file.name} 上传失败`); }
    finally { setUploading(false); }
    return false;
  };

  return (
    <div style={{ background: '#0d1117', borderRadius: 8, border: '1px solid #30363d', display: 'flex', flexDirection: 'column', height }}>
      <div style={{ background: '#161b22', padding: '7px 12px', borderRadius: '8px 8px 0 0', borderBottom: '1px solid #30363d', display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        <div style={{ display: 'flex', gap: 5 }}>
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#ff5f57' }} />
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#febc2e' }} />
          <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#28c840' }} />
        </div>
        <span style={{ color: '#8b949e', fontSize: 11, fontFamily: 'monospace' }}>{title}</span>
      </div>
      <div ref={scrollBoxRef} style={{ flex: 1, overflowY: 'auto', padding: '10px 14px', fontFamily: 'monospace', fontSize: 12, minHeight: 0 }}>
        {messages.length === 0 && (
          <div style={{ color: '#484f58', fontSize: 11, marginTop: 8 }}>
            <span style={{ color }}>{prefix}&gt; </span>
            <span>waiting for input...</span>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ marginBottom: 10 }}>
            {m.role === 'assistant' ? (
              <div>
                <span style={{ color }}>{prefix}&gt; </span>
                <div style={{ color: '#c9d1d9', marginTop: 3, paddingLeft: 14, whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{m.content}</div>
              </div>
            ) : m.role === 'system' ? (
              <div>
                <span style={{ color: '#8b949e' }}>qc&gt; </span>
                <span style={{ color: '#d2a8ff', whiteSpace: 'pre-wrap' }}>{m.content}</span>
              </div>
            ) : (
              <div>
                <span style={{ color: '#f0f6fc' }}>you&gt; </span>
                <span style={{ color: '#ffa657' }}>{m.content}</span>
              </div>
            )}
          </div>
        ))}
        {sending && (
          <div style={{ color: '#8b949e' }}>
            <span style={{ color }}>{prefix}&gt; </span>
            <span>thinking <LoadingOutlined style={{ marginLeft: 4 }} /></span>
          </div>
        )}
        {extraContent}
        <div ref={endRef} />
      </div>
      <div style={{ borderTop: '1px solid #30363d', padding: '6px 12px', display: 'flex', gap: 8, alignItems: 'flex-end', flexShrink: 0 }}>
        <span style={{ color, fontFamily: 'monospace', fontSize: 12, flexShrink: 0 }}>you&gt;</span>
        <TextArea rows={1} value={val} onChange={e => setVal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          placeholder={placeholder || '输入消息...'}
          style={{ background: 'transparent', border: 'none', color: '#f0f6fc', fontFamily: 'monospace', fontSize: 12, resize: 'none', flex: 1, padding: 0 }}
          disabled={sending} />
        {projectId && (
          <Upload multiple showUploadList={false} beforeUpload={handleUpload}
            accept=".txt,.md,.pdf,.docx,.doc,.json,.csv,.yaml,.yml" disabled={uploading || sending}>
            <Tooltip title="上传文件">
              <Button type="text" size="small" style={{ color: '#8b949e', flexShrink: 0 }}
                icon={uploading ? <LoadingOutlined /> : <PaperClipOutlined />} />
            </Tooltip>
          </Upload>
        )}
        <Button type="text" size="small" onClick={send} loading={sending} style={{ color, flexShrink: 0 }}>send</Button>
      </div>
      {bottomActions && (
        <div style={{ borderTop: '1px solid #30363d', padding: '8px 12px', flexShrink: 0 }}>
          {bottomActions}
        </div>
      )}
    </div>
  );
};

// ─── 主组件 ───────────────────────────────────────────────────────────────
const PhaseBoard: React.FC = () => {
  const navigate = useNavigate();
  const { id: projectId } = useParams<{ id: string }>();

  const [phases, setPhases] = useState<PhaseInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [phaseLoadError, setPhaseLoadError] = useState<string | null>(null);
  const [allAgents, setAllAgents] = useState<Record<string, any>>({});
  const [phaseFiles, setPhaseFiles] = useState<Record<string, FileRecord[]>>({});

  const [pmChats, setPmChats] = useState<Record<string, ChatMsg[]>>({});
  const [supChats, setSupChats] = useState<Record<string, ChatMsg[]>>({});
  const [pmSending, setPmSending] = useState<Record<string, boolean>>({});
  const [supSending, setSupSending] = useState<Record<string, boolean>>({});

  const [reviewResults, setReviewResults] = useState<Record<string, ReviewResult>>(() => {
    try {
      const v = sessionStorage.getItem(`phase_reviews_${projectId}`);
      return v ? JSON.parse(v) : {};
    } catch { return {}; }
  });
  const setAndSaveReview = (updater: (prev: Record<string, ReviewResult>) => Record<string, ReviewResult>) => {
    setReviewResults(prev => {
      const next = updater(prev);
      try { sessionStorage.setItem(`phase_reviews_${projectId}`, JSON.stringify(next)); } catch {}
      return next;
    });
  };

  const [confirmedPhases, setConfirmedPhases] = useState<Set<string>>(new Set());
  const [confirmingPhase, setConfirmingPhase] = useState<string | null>(null);

  const [issueModal, setIssueModal] = useState<{ open: boolean; issue: Issue | null; phaseId: string; suggestedFix: string }>({ open: false, issue: null, phaseId: '', suggestedFix: '' });
  const [issueEdit, setIssueEdit] = useState('');
  const [submittingIssue, setSubmittingIssue] = useState(false);

  const [startingPhase, setStartingPhase] = useState<string | null>(null);
  const [editingPhase, setEditingPhase] = useState<string | null>(null);
  const [editDesc, setEditDesc] = useState('');

  const [openPmChat, setOpenPmChat] = useState<string | null>(null);
  const [openSupChat, setOpenSupChat] = useState<string | null>(null);

  const [taskPreviewPhase, setTaskPreviewPhase] = useState<string | null>(null);
  const [taskPreviewDesc, setTaskPreviewDesc] = useState('');
  const [savingTaskDesc, setSavingTaskDesc] = useState(false);

  const [projectMetrics, setProjectMetrics] = useState<any>(null);
  const [qcResultsSummary, setQcResultsSummary] = useState<any>(null);

  const getPhaseAgentSnapshots = (phase: PhaseInfo): any[] => {
    const details = phase.agent_details || [];
    if ((phase.agents || []).length === 0) return details;
    return phase.agents
      .map(agentId => (
        allAgents[agentId]
        || details.find(agent => (agent.id || agent.agent_id) === agentId)
      ))
      .filter(Boolean);
  };

  const getPhaseReviewGate = (phaseId: string): { allowed: boolean; reason: string } => {
    const phase = phases.find(item => item.phase_id === phaseId);
    if (!phase) return { allowed: false, reason: '阶段数据尚未加载，请刷新后重试' };
    const agents = getPhaseAgentSnapshots(phase);
    if (agents.length === 0) {
      return { allowed: false, reason: '本阶段尚无可验证的 Agent，不能启动质检' };
    }
    const incomplete = agents.filter(agent => String(agent.status || 'unknown').toLowerCase() !== 'completed');
    if (incomplete.length === 0) return { allowed: true, reason: '' };

    const activeCount = incomplete.filter(agent => (
      ['queued', 'working', 'running', 'in_progress', 'fixing', 're_checking']
        .includes(String(agent.status || '').toLowerCase())
    )).length;
    const failedCount = incomplete.filter(agent => (
      ['failed', 'error', 'fix_required', 'fix_limit_reached', 'blocked', 'cancelled']
        .includes(String(agent.status || '').toLowerCase())
    )).length;
    if (activeCount > 0) {
      return { allowed: false, reason: `仍有 ${activeCount} 个 Agent 正在执行，全部成功完成后才能质检` };
    }
    if (failedCount > 0) {
      return { allowed: false, reason: `存在 ${failedCount} 个失败或待修复 Agent，请先重跑并确认全部完成` };
    }
    return { allowed: false, reason: `仍有 ${incomplete.length} 个 Agent 未达到已完成状态，暂不能质检` };
  };

  const loadAllPhaseHistories = async (phaseList: PhaseInfo[]) => {
    for (const phase of phaseList) {
      const pid = phase.phase_id;
      try {
        const [pmRes, supRes]: any[] = await Promise.all([
          axios.get(`${API}/projects/${projectId}/chat-history/pm_phase_${pid}`).catch(() => ({ data: { messages: [] } })),
          axios.get(`${API}/projects/${projectId}/chat-history/sup_phase_${pid}`).catch(() => ({ data: { messages: [] } })),
        ]);
        if (pmRes.data.messages?.length > 0) setPmChats(c => ({ ...c, [pid]: pmRes.data.messages }));
        if (supRes.data.messages?.length > 0) setSupChats(c => ({ ...c, [pid]: supRes.data.messages }));
      } catch { /* silent */ }
    }
  };

  const loadPhases = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/phases`);
      const phaseList = res.data.phases || [];
      setPhases(phaseList);
      setPhaseLoadError(null);
      if (phaseList.length > 0 && Object.keys(pmChats).length === 0) loadAllPhaseHistories(phaseList);
      for (const phase of phaseList) {
        const pid = phase.phase_id;
        if (reviewResults[pid]) {
          try {
            const issRes: any = await axios.get(`${API}/projects/${projectId}/phases/${pid}/issues`);
            if (issRes.data.issues?.length > 0) {
              setAndSaveReview(prev => {
                const old = prev[pid];
                if (!old) return prev;
                return {
                  ...prev,
                  [pid]: { ...old, issues: issRes.data.issues, passed: issRes.data.passed, error_count: issRes.data.open_count || 0 },
                };
              });
            }
          } catch { /* silent */ }
        }
      }
    } catch (e: any) {
      setPhaseLoadError(e.response?.data?.detail || e.message || '阶段数据加载失败');
    } finally { setLoading(false); }
  };

  const savePmHistory = async (phaseId: string, msgs: ChatMsg[]) => {
    try {
      await axios.post(`${API}/projects/${projectId}/chat-history/pm_phase_${phaseId}`, {
        messages: msgs.map(m => ({ role: m.role, content: m.content, ts: m.ts, ...(m.type ? { type: m.type } : {}) })),
      });
    } catch { /* silent */ }
  };
  const saveSupHistory = async (phaseId: string, msgs: ChatMsg[]) => {
    try {
      await axios.post(`${API}/projects/${projectId}/chat-history/sup_phase_${phaseId}`, {
        messages: msgs.map(m => ({ role: m.role, content: m.content, ts: m.ts })),
      });
    } catch { /* silent */ }
  };

  const loadAgents = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/agents`);
      const m: Record<string, any> = {};
      (res.data.agents || []).forEach((a: any) => { m[a.id] = a; });
      setAllAgents(m);
    } catch { /* silent */ }
  };

  const loadPhaseFiles = async (phaseId: string) => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/phases/${phaseId}/files`);
      setPhaseFiles(prev => ({ ...prev, [phaseId]: res.data.files || [] }));
    } catch { /* silent */ }
  };

  const loadProjectMetrics = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/metrics`);
      setProjectMetrics(res.data);
    } catch { /* silent */ }
  };

  const loadQcResults = async () => {
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/qc/results`);
      setQcResultsSummary(res.data);
    } catch { /* silent */ }
  };

  useEffect(() => {
    if (!projectId) return;

    // ????
    loadPhases();
    loadAgents();
    loadProjectMetrics();
    loadQcResults();

    // ?? WebSocket
    wsService.connect(projectId);

    // ?? WebSocket ??????????????????
    const unsubConnected = wsService.on('connected', () => {
      console.log('[PhaseBoard] WebSocket ???');
    });
    // WebSocket 只携带“数据已变化”的通知，页面仍通过 HTTP 拉取最新快照。
    // 因此无需用服务端内存版本号丢弃事件；服务重启后版本号可能回退。
    const unsubPhases = wsService.on('phases_updated', () => loadPhases());
    const unsubAgents = wsService.on('agents_updated', () => loadAgents());
    const unsubMetrics = wsService.on('metrics_updated', () => loadProjectMetrics());
    const unsubQc = wsService.on('qc_updated', () => loadQcResults());

    // Agent 执行是后台异步任务：即使 WebSocket 已连接，也持续拉取终态，
    // 避免漏掉事件后把 queued/failed 长期显示为旧状态。
    const t = setInterval(() => {
      loadAgents();
      loadPhases();
      loadProjectMetrics();
      loadQcResults();
    }, 5000);

    return () => {
      clearInterval(t);
      unsubConnected();
      unsubPhases();
      unsubAgents();
      unsubMetrics();
      unsubQc();
      wsService.disconnect();
    };
  }, [projectId]);

  const startPhase = async (phaseId: string) => {
    setStartingPhase(phaseId);
    try {
      const res: any = await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/start`);
      const agentCount = res.data.created_agents?.length || 0;
      message.success({
        content: (
          <span>
            阶段已启动，{agentCount} 个Agent已自动开始工作。<br />
            <span style={{ fontSize: 12, color: '#595959' }}>
              下一步：等待 Agent 完成后，点击「质检循环」自动完成检查与返工
            </span>
          </span>
        ),
        duration: 5,
      });
      await loadPhases(); await loadAgents();
    } catch (e: any) { message.error(e.response?.data?.detail || '启动失败'); }
    finally { setStartingPhase(null); }
  };

  const savePhaseDesc = async (phaseId: string) => {
    try {
      await axios.patch(`${API}/projects/${projectId}/phases/${phaseId}`, { description: editDesc });
      message.success('任务描述已更新'); setEditingPhase(null); await loadPhases();
    } catch { message.error('保存失败'); }
  };

  const sendPmMsg = async (phaseId: string, text: string) => {
    const prev = pmChats[phaseId] || [];
    const newMsgs: ChatMsg[] = [...prev, { role: 'user', content: text, ts: Date.now() }];
    setPmChats(c => ({ ...c, [phaseId]: newMsgs }));
    setPmSending(s => ({ ...s, [phaseId]: true }));
    try {
      const res: any = await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/pm-chat`, {
        message: text, history: prev.slice(-8).map(m => ({ role: m.role, content: m.content })),
      });
      const finalMsgs: ChatMsg[] = [...newMsgs, { role: 'assistant', content: res.data.reply || '', ts: Date.now() }];
      setPmChats(c => ({ ...c, [phaseId]: finalMsgs }));
      await savePmHistory(phaseId, finalMsgs);
    } catch { message.error('发送失败'); }
    finally { setPmSending(s => ({ ...s, [phaseId]: false })); }
  };

  const sendSupMsg = async (phaseId: string, text: string) => {
    const prev = supChats[phaseId] || [];
    const newMsgs: ChatMsg[] = [...prev, { role: 'user', content: text, ts: Date.now() }];
    setSupChats(c => ({ ...c, [phaseId]: newMsgs }));
    setSupSending(s => ({ ...s, [phaseId]: true }));
    try {
      const res: any = await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/supervisor-chat`, {
        message: text, history: prev.slice(-8).map(m => ({ role: m.role, content: m.content })),
      });
      const finalMsgs: ChatMsg[] = [...newMsgs, { role: 'assistant', content: res.data.reply || '', ts: Date.now() }];
      setSupChats(c => ({ ...c, [phaseId]: finalMsgs }));
      await saveSupHistory(phaseId, finalMsgs);
    } catch { message.error('发送失败'); }
    finally { setSupSending(s => ({ ...s, [phaseId]: false })); }
  };

  // ── 自动修复循环 ──
  const [autoRepairPhase, setAutoRepairPhase] = useState<string | null>(null);
  const [autoRepairStates, setAutoRepairStates] = useState<Record<string, AutoRepairStatus>>({});
  const [autoRepairAction, setAutoRepairAction] = useState<AutoRepairAction | null>(null);
  const [decisionSubmitting, setDecisionSubmitting] = useState<AutoRepairDecision | null>(null);
  const autoRepairPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const autoRepairPollInFlightRef = useRef(false);
  const autoRepairPollFailuresRef = useRef(0);
  const autoRepairRestoreKeyRef = useRef<string | null>(null);
  const [showDecisionModal, setShowDecisionModal] = useState(false);
  const allowedAutoRepairDecisions = new Set<AutoRepairDecision>(
    autoRepairAction?.options || [],
  );
  const autoRepairDecisionTitle = autoRepairAction?.status === 'passed_with_handoff'
    ? '质检完成，存在待修问题'
    : autoRepairAction?.status === 'quality_regressed'
    ? '返修导致质量回退'
    : autoRepairAction?.status === 'no_progress'
      ? '返修未产生有效进展'
      : autoRepairAction?.status === 'interrupted'
        ? '质检循环被中断'
        : autoRepairAction?.status === 'qa_blocked'
          ? '质检循环被阻断'
          : autoRepairAction?.status === 'awaiting_manual_fix'
            ? '等待人工修复'
          : '质检循环达到自动返修上限';
  const autoRepairDecisionDescription = [
    allowedAutoRepairDecisions.has('manual_fix')
      ? (autoRepairAction?.status === 'passed_with_handoff'
        ? '待修问题已按文件聚合，可进入全栈工程师工作台继续处理'
        : '自行修改会暂停循环并进入全栈工程师工作台')
      : '',
    allowedAutoRepairDecisions.has('retry_cycle') ? '继续质检循环会基于当前文件再检查和修复' : '',
    allowedAutoRepairDecisions.has('rebuild_phase') ? '阶段重构会保留快照、重置本阶段 Agent 并携带问题清单重新生成任务' : '',
  ].filter(Boolean).join('；') || '后端未提供可执行操作，请刷新状态或查看问题证据。';

  const stopAutoRepairPolling = () => {
    if (autoRepairPollRef.current) {
      clearInterval(autoRepairPollRef.current);
      autoRepairPollRef.current = null;
    }
    autoRepairPollInFlightRef.current = false;
    autoRepairPollFailuresRef.current = 0;
  };

  const startAutoRepairPolling = (phaseId: string) => {
    stopAutoRepairPolling();
    setAutoRepairPhase(phaseId);
    void pollAutoRepairStatus(phaseId);
    autoRepairPollRef.current = setInterval(() => void pollAutoRepairStatus(phaseId), 2000);
  };

  const runAutoRepair = async (phaseId: string, phaseName: string, decision?: AutoRepairDecision) => {
    if (!decision) {
      const gate = getPhaseReviewGate(phaseId);
      if (!gate.allowed) {
        message.warning(gate.reason);
        return;
      }
    }
    if ((!decision && autoRepairPhase) || decisionSubmitting) {
      message.info('质检循环正在运行，请勿重复点击');
      return;
    }
    setAutoRepairPhase(phaseId);
    if (decision) setDecisionSubmitting(decision);
    if (!decision) {
      // 首次启动
      setSupChats(c => ({
        ...c,
        [phaseId]: [...(c[phaseId] || []), { role: 'system', content: `🚀 启动「${phaseName}」质检循环...`, ts: Date.now() }],
      }));
      setOpenSupChat(phaseId);
    } else {
      // 用户决策
      setSupChats(c => ({
        ...c,
        [phaseId]: [
          ...(c[phaseId] || []),
          {
            role: 'system',
            content: decision === 'rebuild_phase'
              ? '🔄 用户选择：带问题清单重构阶段'
              : decision === 'manual_fix'
                ? '🛠 用户选择：暂停循环并自行修改'
                : '⏭ 用户选择：继续质检循环',
            ts: Date.now(),
          },
        ],
      }));
    }

    try {
      const res: any = await axios.post(
        `${API}/projects/${projectId}/phases/${phaseId}/auto-repair`,
        null,
        { params: { user_decision: decision || undefined }, timeout: 180000 },
      );
      if (!res.data.success) {
        message.error(res.data.message || '启动失败');
        setAutoRepairPhase(null);
        return;
      }
      setShowDecisionModal(false);
      setAutoRepairAction(null);
      const returnedState = res.data.status as AutoRepairStatus | undefined;
      if (returnedState && returnedState.running === false) {
        await applyAutoRepairStatus(phaseId, returnedState, true);
      } else {
        startAutoRepairPolling(phaseId);
      }
    } catch (e: any) {
      message.error(apiErrorText(e, '自动修复启动失败'));
      setAutoRepairPhase(null);
      if (decision) setShowDecisionModal(true);
    } finally {
      setDecisionSubmitting(null);
    }
  };

  const applyAutoRepairStatus = async (phaseId: string, state: AutoRepairStatus, notify: boolean) => {
    if (
      !state.running
      && state.review_result?.passed === true
      && state.status !== 'passed_with_handoff'
    ) {
      state = {
        ...state,
        status: 'passed',
        action_required: undefined,
        needs_manual: false,
      };
    }
    setAutoRepairStates(current => ({ ...current, [phaseId]: state }));
    const actionMessage = state.action_required?.message;
    const hasActionMessage = !!actionMessage && (state.messages || []).some(
      item => item.content.includes(actionMessage),
    );
    const terminalFallback = !state.running && (state.messages || []).length === 0
      ? [{
          role: 'system' as const,
          content: state.status === 'passed'
            ? '✅ 质检已完成并通过，等待确认阶段完成'
            : `❌ 质检流程已结束：${state.action_required?.message || state.status}`,
          ts: Date.now(),
        }]
      : [];
    const actionFallback = !state.running && actionMessage && !hasActionMessage
      ? [{ role: 'system' as const, content: `❌ ${actionMessage}`, ts: Date.now() }]
      : [];
    const newMsgs: ChatMsg[] = [
      ...(state.messages || []).map(m => ({
      role: m.role || 'system', content: m.content, ts: (m.ts || 0) * 1000,
      event_id: m.event_id, round: m.round,
      })),
      ...terminalFallback,
      ...actionFallback,
    ];
    if (newMsgs.length > 0) {
      setSupChats(c => {
        const prev = c[phaseId] || [];
        const eventKey = (m: ChatMsg) => m.event_id
          ? `event:${m.event_id}`
          : `${m.role}:${m.content}:${m.round ?? ''}:${m.ts}`;
        const existing = new Set(prev.map(eventKey));
        const toAppend = newMsgs.filter(m => !existing.has(eventKey(m)));
        if (toAppend.length === 0) return c;
        const merged = [...prev, ...toAppend];
        void saveSupHistory(phaseId, merged);
        return { ...c, [phaseId]: merged };
      });
    }

    if (state.review_result) {
      setAndSaveReview(prev => ({ ...prev, [phaseId]: state.review_result! }));
    }

    if (state.running) {
      setAutoRepairPhase(phaseId);
      return;
    }

    stopAutoRepairPolling();
    setAutoRepairPhase(null);
    await Promise.all([loadPhases(), loadPhaseFiles(phaseId)]);

    if (state.status === 'passed') {
      // A terminal pass invalidates a dialog opened from an earlier state.
      setAutoRepairAction(current => current?.phaseId === phaseId ? null : current);
      setShowDecisionModal(false);
      if (notify) message.success('✅ 自动修复完成，质检通过！');
      // The auto-repair loop has already run and persisted the authoritative
      // QA result. Starting another inspection here would create a second result against
      // the same revision and can turn a just-passed cycle back into failure.
    } else if (state.status === 'passed_with_handoff' && state.action_required) {
      setAutoRepairAction({
        phaseId,
        status: state.status,
        ...(state.action_required || {}),
        issueReport: state.issue_report || {},
      });
      setShowDecisionModal(true);
      if (notify) message.warning(state.action_required.message);
    } else if (state.action_required) {
      setAutoRepairAction({
        phaseId,
        status: state.status,
        ...(state.action_required || {}),
        issueReport: state.issue_report || {},
      });
      setShowDecisionModal(true);
      const lastMessage = [...newMsgs].reverse().find(m => m.content)?.content;
      if (notify) message.error(lastMessage || '质检被阻断，请查看真实原因后重试');
    } else if (state.status === 'rebuild_started') {
      setAndSaveReview(prev => {
        const next = { ...prev };
        delete next[phaseId];
        return next;
      });
      await loadAgents();
      if (notify) message.success('阶段已携带问题清单自动启动全量重构');
    } else if (state.status === 'needs_manual') {
      if (notify) message.warning('⚠ 自动修复仍未通过，文件已标记 needs_manual，可进入全栈工程师工作台处理');
    } else if ([
      'error', 'failed', 'infrastructure_blocked', 'interrupted',
      'quality_regressed', 'no_progress', 'qa_blocked',
    ].includes(state.status)) {
      const lastMessage = [...(state.messages || [])].reverse().find(m => m.content)?.content;
      message.error(lastMessage || '质检循环执行失败，请检查 Agent 状态后重试');
    } else if (state.status === 'idle') {
      message.error('后端没有返回本次质检状态，流程未确认完成，请重试');
    }
  };

  const pollAutoRepairStatus = async (phaseId: string) => {
    if (autoRepairPollInFlightRef.current) return;
    autoRepairPollInFlightRef.current = true;
    try {
      const res: any = await axios.get(
        `${API}/projects/${projectId}/phases/${phaseId}/auto-repair/status`,
        { timeout: 15000 },
      );
      if (res.data?.status === 'idle') {
        autoRepairPollFailuresRef.current += 1;
        if (autoRepairPollFailuresRef.current >= 3) {
          stopAutoRepairPolling();
          setAutoRepairPhase(null);
          const content = '❌ 后端连续未返回质检状态，本次流程未确认完成';
          setSupChats(c => ({
            ...c,
            [phaseId]: [...(c[phaseId] || []), { role: 'system', content, ts: Date.now() }],
          }));
          message.error(content);
        }
        return;
      }
      autoRepairPollFailuresRef.current = 0;
      await applyAutoRepairStatus(phaseId, res.data as AutoRepairStatus, true);
    } catch (e: any) {
      autoRepairPollFailuresRef.current += 1;
      if (autoRepairPollFailuresRef.current === 3) {
        message.warning(apiErrorText(e, '质检状态暂时无法获取，正在自动重试'));
      }
      if (autoRepairPollFailuresRef.current >= 10) {
        stopAutoRepairPolling();
        setAutoRepairPhase(null);
        message.error('质检状态连续获取失败，请检查网络后刷新页面恢复');
      }
    } finally {
      autoRepairPollInFlightRef.current = false;
    }
  };

  const openManualRepair = async () => {
    if (!autoRepairAction || decisionSubmitting) return;
    setDecisionSubmitting('manual_fix');
    setAutoRepairPhase(autoRepairAction.phaseId);
    try {
      if (autoRepairAction.status !== 'passed_with_handoff') {
        const decisionRes: any = await axios.post(
          `${API}/projects/${projectId}/phases/${autoRepairAction.phaseId}/auto-repair`,
          null,
          { params: { user_decision: 'manual_fix' }, timeout: 30000 },
        );
        if (!decisionRes.data.success) throw new Error(decisionRes.data.message || '暂停质检循环失败');
      }
      await axios.post(
        `${API}/projects/${projectId}/phases/${autoRepairAction.phaseId}/transfer-to-engineer`,
        null,
        { timeout: 30000 },
      );
      setShowDecisionModal(false);
      navigate(`/engineer/${projectId}?tab=repair`);
    } catch (e: any) {
      setAutoRepairPhase(null);
      message.error(e.response?.data?.detail || e.message || '问题清单移交失败，请重试');
    } finally {
      setDecisionSubmitting(null);
    }
  };

  const confirmPhaseRebuild = () => {
    if (!autoRepairAction || decisionSubmitting) return;
    Modal.confirm({
      title: '确认阶段重构',
      content: '系统会保留重构前快照，重置本阶段 Agent，并携带当前问题清单重新生成全部任务。确认继续吗？',
      okText: '确认重构',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => runAutoRepair(autoRepairAction.phaseId, '', 'rebuild_phase'),
    });
  };

  // 刷新或返回阶段看板后，从后端恢复运行中的循环或待决策弹窗。
  useEffect(() => {
    if (!projectId || loading || phases.length === 0) return;
    const restoreKey = `${projectId}:${phases.map(p => p.phase_id).join(',')}`;
    if (autoRepairRestoreKeyRef.current === restoreKey) return;
    autoRepairRestoreKeyRef.current = restoreKey;

    void (async () => {
      const candidates = phases;
      const states = await Promise.all(candidates.map(async p => {
        try {
          const res: any = await axios.get(
            `${API}/projects/${projectId}/phases/${p.phase_id}/auto-repair/status`,
            { timeout: 15000 },
          );
          return { phaseId: p.phase_id, state: res.data as AutoRepairStatus };
        } catch { return null; }
      }));
      const loadedStates = states
        .filter((item): item is { phaseId: string; state: AutoRepairStatus } => !!item)
        .map(item => ({
          ...item,
          state: !item.state.running && item.state.review_result?.passed === true
            ? {
                ...item.state,
                status: 'passed' as const,
                action_required: undefined,
                needs_manual: false,
              }
            : item.state,
        }));
      setAutoRepairStates(current => ({
        ...current,
        ...Object.fromEntries(loadedStates.map(item => [item.phaseId, item.state])),
      }));
      const restoredReviews = Object.fromEntries(
        loadedStates
          .filter(item => !!item.state.review_result)
          .map(item => [item.phaseId, item.state.review_result!]),
      );
      if (Object.keys(restoredReviews).length > 0) {
        setAndSaveReview(current => ({ ...current, ...restoredReviews }));
      }
      const completedPhaseIds = new Set(
        phases
          .filter(phase => phase.status === 'completed')
          .map(phase => phase.phase_id),
      );
      const actionableStates = loadedStates.filter(item =>
        !completedPhaseIds.has(item.phaseId)
        && item.state.review_result?.passed !== true,
      );
      // Prefer the latest unfinished phase. Every restored action remains bound
      // to its own phaseId, so an older phase cannot populate a newer phase UI.
      const resumable = [...actionableStates].reverse().find(item => item.state.running)
        || [...actionableStates].reverse().find(item => !!item.state.action_required);
      if (!resumable) return;
      setOpenSupChat(resumable.phaseId);
      await applyAutoRepairStatus(resumable.phaseId, resumable.state, false);
      if (resumable.state.running) startAutoRepairPolling(resumable.phaseId);
    })();
  }, [projectId, loading, phases]);

  // 清理轮询
  useEffect(() => {
    return stopAutoRepairPolling;
  }, []);

  const [projectDoneModal, setProjectDoneModal] = useState(false);
  const [rerunningAgent, setRerunningAgent] = useState<string | null>(null);

  const confirmPhaseComplete = async (phaseId: string, phaseName: string) => {
    setConfirmingPhase(phaseId);
    try {
      await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/confirm-complete`);
      setConfirmedPhases(prev => new Set([...prev, phaseId]));
      if (openSupChat === phaseId) setOpenSupChat(null);
      if (openPmChat === phaseId) setOpenPmChat(null);
      await loadPhases();
      const freshPhases = (await axios.get(`${API}/projects/${projectId}/phases`)).data.phases || [];
      const isLastPhase = freshPhases[freshPhases.length - 1]?.phase_id === phaseId;
      if (isLastPhase) {
        setProjectDoneModal(true);
      } else {
        message.success({
          content: (
            <span>
              「${phaseName}」已确认完成。<br />
              <span style={{ fontSize: 12, color: '#595959' }}>
                下一步：在下一阶段卡片点击「启动阶段」
              </span>
            </span>
          ),
          duration: 4,
        });
      }
    } catch (e: any) { message.error(e.response?.data?.detail || '确认失败'); }
    finally { setConfirmingPhase(null); }
  };

  const submitIssueToPm = async () => {
    if (!issueModal.issue) return;
    setSubmittingIssue(true);
    const phaseId = issueModal.phaseId;
    const issue = issueModal.issue;
    try {
      const res: any = await axios.post(
        `${API}/projects/${projectId}/phases/${phaseId}/issues/${issue.id}/submit-to-pm`,
        { user_description: issueEdit }
      );
      const userMsg: ChatMsg = {
        role: 'user',
        content: `【单条质检问题反馈】\n${formatIssueForPm(issue)}\n用户补充：${issueEdit || '无'}\n\n请只给出：原因一句话、修改点、给负责Agent的返工指令。`,
        ts: Date.now(),
      };
      const pmReply: ChatMsg = {
        role: 'assistant', content: res.data.pm_analysis || `已收到，安排 ${issue.responsible_agent_role} 修复`,
        ts: Date.now() + 1, type: 'pm_fix_plan',
      };
      const prev = pmChats[phaseId] || [];
      const newMsgs = [...prev, userMsg, pmReply];
      setPmChats(c => ({ ...c, [phaseId]: newMsgs }));
      await savePmHistory(phaseId, newMsgs);
      setReviewResults(prev => {
        const r = prev[phaseId]; if (!r) return prev;
        return { ...prev, [phaseId]: { ...r, issues: r.issues.map(i => i.id === issue.id ? { ...i, status: 'fixing' as const } : i) } };
      });
      setIssueModal({ open: false, issue: null, phaseId: '', suggestedFix: '' });
      setOpenPmChat(phaseId); setOpenSupChat(null);
      message.success('问题已提交给阶段PM');
    } catch { message.error('提交失败'); }
    finally { setSubmittingIssue(false); }
  };

  const [batchSubmitting, setBatchSubmitting] = useState<string | null>(null);
  const submitAllIssuesToPm = async (phaseId: string) => {
    const review = reviewResults[phaseId];
    if (!review) return;
    const openIssues = review.issues.filter(i => i.status === 'open');
    if (openIssues.length === 0) { message.info('没有待处理的问题'); return; }

    const fileMap = new Map<string, typeof openIssues>();
    for (const issue of openIssues) {
      const key = issue.file_path || '（未知文件）';
      if (!fileMap.has(key)) fileMap.set(key, []);
      fileMap.get(key)!.push(issue);
    }
    const fileKeys = Array.from(fileMap.keys()).slice(0, FILE_BATCH_LIMIT);
    const batchIssues = fileKeys.flatMap(k => fileMap.get(k)!);
    const remainingFiles = fileMap.size - fileKeys.length;

    setBatchSubmitting(phaseId);
    try {
      const res: any = await axios.post(
        `${API}/projects/${projectId}/phases/${phaseId}/issues/batch-submit-to-pm`,
        { issue_ids: batchIssues.map(i => i.id), user_note: '' }
      );

      const batchResults: Array<{ issue_id: string; pm_analysis: string }> = res.data.results || [];
      const submittedIds = batchResults.map(r => r.issue_id);

      const prev = pmChats[phaseId] || [];
      const newMsgs: ChatMsg[] = [...prev];

      const fileGroupText = fileKeys.map((filePath, fi) => {
        const issues = fileMap.get(filePath)!;
        const issueLines = issues.map((issue, idx) => formatIssueForPm(issue, idx)).join('\n');
        return `文件 ${fi + 1}：${filePath}\n${issueLines}`;
      }).join('\n\n');

      newMsgs.push({
        role: 'user',
        content: `【按文件批量反馈，本次${fileKeys.length} 个文件/ ${batchIssues.length} 个问题${remainingFiles > 0 ? `，还有${remainingFiles} 个文件待下次` : ''}】\n\n${fileGroupText}\n\n请按文件逐条给出简洁返工指令。`,
        ts: Date.now(),
      });

      const pmReplyLines = batchResults.map((r, idx) => {
        const issue = batchIssues.find(i => i.id === r.issue_id);
        return `问题 ${idx + 1}${issue ? `（${issue.file_path || issue.responsible_agent_role}）：` : ''}\n${r.pm_analysis}`;
      });
      newMsgs.push({
        role: 'assistant',
        content: pmReplyLines.join('\n\n---\n\n'),
        ts: Date.now() + 1,
        type: 'pm_fix_plan',
      });

      setPmChats(c => ({ ...c, [phaseId]: newMsgs }));
      await savePmHistory(phaseId, newMsgs);

      setReviewResults(prev => {
        const r = prev[phaseId]; if (!r) return prev;
        return {
          ...prev,
          [phaseId]: {
            ...r,
            issues: r.issues.map(i => submittedIds.includes(i.id) ? { ...i, status: 'fixing' as const } : i),
          },
        };
      });

      setOpenPmChat(phaseId); setOpenSupChat(null);
      const remainMsg = remainingFiles > 0
        ? `，还有${remainingFiles} 个文件待修复后重新质检再提交`
        : '';
      message.success(`已提交${fileKeys.length} 个文件（${submittedIds.length} 个问题）给阶段PM${remainMsg}`);
    } catch {
      message.error('批量提交失败');
    } finally {
      setBatchSubmitting(null);
    }
  };

  const [repairSummary, setRepairSummary] = useState<Record<string, RepairSummary>>({});
  const [arbiterResult, setArbiterResult] = useState<Record<string, ArbiterResult>>({});
  const [startingRepair, setStartingRepair] = useState<string | null>(null);

  const startRepairLoop = async (phaseId: string, subprojectId: string) => {
    setStartingRepair(subprojectId);
    try {
      const res: any = await axios.post(`${API}/projects/${projectId}/repair/start`, { subproject_id: subprojectId });
      if (res.data.escalated) {
        setArbiterResult(prev => ({ ...prev, [subprojectId]: res.data.arbiter_result }));
        message.warning(`仲裁者介入：${res.data.arbiter_result?.action_hint || res.data.message}`);
      } else {
        setRepairSummary(prev => ({ ...prev, [subprojectId]: res.data.summary }));
        message.success(res.data.message || `已生成${res.data.batches?.length || 0} 个修复批次`);
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '启动修复循环失败');
    } finally {
      setStartingRepair(null);
    }
  };

  const [triggeringFix, setTriggeringFix] = useState<string | null>(null);
  const triggerAgentFix = async (phaseId: string, issue: Issue) => {
    setTriggeringFix(issue.id);
    try {
      await runAutoRepair(phaseId, phases.find(p => p.phase_id === phaseId)?.name || phaseId);
    } finally {
      setTriggeringFix(null);
    }
  };

  const [resettingPhase, setResettingPhase] = useState<string | null>(null);
  const resetPhase = async (phaseId: string, phaseName: string) => {
    Modal.confirm({
      title: '确认重置阶段',
      content: `确定要重置阶段「${phaseName}」吗？这将删除该阶段的所有Agent、清空质检结果，阶段将回到待启动状态。`,
      okText: '确认重置',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setResettingPhase(phaseId);
        try {
          const res: any = await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/reset`);
          message.success(res.data.message || `阶段「${phaseName}」已重置`);
          await loadPhases();
          await loadAgents();
          // 清除该阶段的聊天记录和质检结果
          setPmChats(prev => ({ ...prev, [phaseId]: [] }));
          setSupChats(prev => ({ ...prev, [phaseId]: [] }));
          setReviewResults(prev => {
            const newResults = { ...prev };
            delete newResults[phaseId];
            return newResults;
          });
        } catch (e: any) {
          message.error(e.response?.data?.detail || '重置失败');
        } finally {
          setResettingPhase(null);
        }
      },
    });
  };

  if (loading) return (
    <div style={{ padding: 24 }}>
      <Card>
        <Skeleton active paragraph={{ rows: 3 }} />
        <Divider />
        <Skeleton active paragraph={{ rows: 2 }} />
        <Divider />
        <Skeleton active paragraph={{ rows: 4 }} />
      </Card>
    </div>
  );
  if (phaseLoadError && phases.length === 0) return (
    <div style={{ padding: 24 }}>
      <Alert
        type="error"
        showIcon
        message="阶段看板加载失败"
        description={phaseLoadError}
        action={<Button size="small" icon={<ReloadOutlined />} onClick={() => void loadPhases()}>重新加载</Button>}
      />
    </div>
  );
  if (phases.length === 0) return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 300, color: '#8c8c8c', gap: 12, padding: '48px 24px' }}>
      <ClockCircleOutlined style={{ fontSize: 48, marginBottom: 8 }} />
      <p style={{ margin: 0 }}>尚未创建阶段，请先在「PM 组长」页面确认总规划</p>
      <Button type="primary" icon={<ArrowRightOutlined />} onClick={() => navigate(`/projects/${projectId}/pm-team`)}>
        前往 PM 组长
      </Button>
    </div>
  );

  const completedCount = phases.filter(p => p.status === 'completed' || confirmedPhases.has(p.phase_id)).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {phaseLoadError && (
        <Alert
          type="warning"
          showIcon
          closable
          message="阶段数据刷新失败，当前显示的是上一次成功加载的数据"
          description={phaseLoadError}
          action={<Button size="small" onClick={() => void loadPhases()}>立即重试</Button>}
          onClose={() => setPhaseLoadError(null)}
        />
      )}
      <TeamHealth />

      <ReviewChain phases={phases} confirmedPhases={confirmedPhases} completedCount={completedCount} onRefresh={loadPhases} />

      {phases.map((phase, idx) => {
        // Restore a started phase from persisted evidence even if an older
        // backend briefly reported pending while its agents were queued.
        const hasStarted = !!phase.started_at || (phase.agents?.length || 0) > 0 || (phase.agent_details?.length || 0) > 0;
        const isCompleted = !!phase.user_confirmed || confirmedPhases.has(phase.phase_id);
        const isPending = phase.status === 'pending' && !hasStarted;
        const isActive = !isCompleted && (
          hasStarted || phase.status === 'active' || phase.status === 'reviewing' ||
          phase.status === 'qa_pending' || phase.status === 'needs_rework' ||
          phase.status === 'in_progress' || phase.status === 'failed' ||
          phase.status === 'qa_blocked'
        );
        const needsRework = phase.status === 'needs_rework';
        const prevPhase = idx > 0 ? phases[idx - 1] : null;
        const prevDone = !prevPhase
          || prevPhase.user_confirmed === true
          || confirmedPhases.has(prevPhase.phase_id);
        const canStart = isPending && prevDone;
        const review = reviewResults[phase.phase_id];
        const files = phaseFiles[phase.phase_id] || [];
        const agentIds = phase.agents || [];
        const agentDetails = phase.agent_details || [];
        const phaseAgents = agentIds.length > 0
          ? agentIds
              .map(id => allAgents[id] || agentDetails.find(agent => (agent.id || agent.agent_id) === id))
              .filter(Boolean)
          : agentDetails;
        const reviewGate = getPhaseReviewGate(phase.phase_id);
        const sup = phase.supervision;
        const reviewPassed = review?.passed || (sup?.can_proceed && sup?.reviewed);
        const userConfirmed = !!phase.user_confirmed || confirmedPhases.has(phase.phase_id);
        const isLastPhase = idx === phases.length - 1;

        return (
          <PhaseCard
            key={phase.phase_id}
            phase={phase}
            idx={idx}
            totalPhases={phases.length}
            review={review}
            autoRepairState={autoRepairStates[phase.phase_id]}
            files={files}
            phaseAgents={phaseAgents}
            isPending={isPending}
            isActive={isActive}
            isCompleted={!!isCompleted}
            canStart={canStart}
            canReview={reviewGate.allowed}
            reviewBlockedReason={reviewGate.reason}
            reviewPassed={!!reviewPassed}
            userConfirmed={userConfirmed}
            isLastPhase={isLastPhase}
            needsRework={needsRework}
            editingPhase={editingPhase}
            editDesc={editDesc}
            openPmChat={openPmChat}
            openSupChat={openSupChat}
            startingPhase={startingPhase}
            reviewingPhase={autoRepairPhase}
            confirmingPhase={confirmingPhase}
            batchSubmitting={batchSubmitting}
            resettingPhase={resettingPhase}
            rerunningAgent={rerunningAgent}
            projectId={projectId!}
            onSetTaskPreview={setTaskPreviewPhase}
            onTogglePmChat={(id) => setOpenPmChat(openPmChat === id ? null : id)}
            onToggleSupChat={(id) => setOpenSupChat(openSupChat === id ? null : id)}
            onStartPhase={startPhase}
            onRunReview={runAutoRepair}
            onRunAutoRepair={runAutoRepair}
            onConfirmComplete={confirmPhaseComplete}
            onEditPhase={(id, desc) => { setEditingPhase(id); setEditDesc(desc); }}
            onSetEditDesc={setEditDesc}
            onSaveDesc={savePhaseDesc}
            onCancelEdit={() => setEditingPhase(null)}
            onSubmitIssues={submitAllIssuesToPm}
            onIssueClick={(issue, pid) => {
              setIssueModal({ open: true, issue, phaseId: pid, suggestedFix: issue.fix_hint || issue.message });
              setIssueEdit(issue.fix_hint || issue.message);
            }}
            onNavigateEngineer={(url) => {
              (async () => {
                try {
                  await axios.post(`${API}/projects/${projectId}/phases/${phase.phase_id}/transfer-to-engineer`);
                } catch { /* silent */ }
              })();
              navigate(url);
            }}
            onRerunAgent={async (agentId) => {
              if (rerunningAgent) return;
              setRerunningAgent(agentId);
              try {
                const phase = phases.find(item => (
                  (item.agents || []).includes(agentId)
                  || (item.agent_details || []).some(agent => (agent.id || agent.agent_id) === agentId)
                ));
                if (!phase) {
                  message.error('找不到该 Agent 所属阶段，无法启动协调修复');
                  return;
                }
                await runAutoRepair(phase.phase_id, phase.name);
              } finally {
                setRerunningAgent(null);
              }
            }}
            onResetPhase={resetPhase}
          >
            {/* PM Chat Panel */}
            {openPmChat === phase.phase_id && (
              <div style={{ padding: '0 16px 14px' }}>
                <Divider style={{ margin: '8px 0' }} />
                <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 8 }}>
                  与阶段PM对话，敲定细则后点击侧边栏「开始当前阶段」将规划交给HR
                </div>
                <div style={{ display: 'flex', gap: 12 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <ChatPanel
                      title={`phase-pm :: ${phase.name}`}
                      prefix="pm" color="#faad14"
                      messages={pmChats[phase.phase_id] || []}
                      sending={pmSending[phase.phase_id] || false}
                      onSend={text => sendPmMsg(phase.phase_id, text)}
                      placeholder="与阶段PM讨论本阶段任务细则..."
                      projectId={projectId}
                      agentType="pm"
                    height="min(40vh, 360px)"
                    />
                  </div>
                  <PlanSidebar
                    messages={pmChats[phase.phase_id] || []}
                    phaseDesc={phase.description}
                    phaseId={phase.phase_id}
                    projectId={projectId!}
                    onStartPhase={() => startPhase(phase.phase_id)}
                    starting={startingPhase === phase.phase_id}
                    phaseStarted={isActive || isCompleted}
                    initialExpertReqs={phase.expert_requirements || []}
                    initialPlanGenerated={phase.plan_generated || (phase.expert_requirements?.length || 0) > 0}
                    chatHeight={360}
                    onRefresh={async () => {
                      try {
                        const res: any = await axios.get(`${API}/projects/${projectId}/chat-history/pm_phase_${phase.phase_id}`);
                        if (res.data.messages?.length > 0) setPmChats(c => ({ ...c, [phase.phase_id]: res.data.messages }));
                        message.success('规划已刷新');
                      } catch { message.error('刷新失败'); }
                    }}
                  />
                </div>
              </div>
            )}

            {/* Supervisor Chat Panel */}
            {openSupChat === phase.phase_id && (
              <div style={{ padding: '0 16px 14px' }}>
                <Divider style={{ margin: '8px 0' }} />
                <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 8 }}>
                  监督Agent - 质检报告 / 问题沟通 / 反馈给PM
                </div>
                <ChatPanel
                  title={`supervisor :: ${phase.name}`}
                  prefix="sup" color="#52c41a"
                  messages={supChats[phase.phase_id] || []}
                  sending={supSending[phase.phase_id] || false}
                  onSend={text => sendSupMsg(phase.phase_id, text)}
                  placeholder="与监督Agent沟通质检问题和修改方案..."
                  projectId={projectId}
                  agentType="supervisor"
                    height="min(45vh, 420px)"
                  extraContent={review && review.issues.length > 0 ? (
                    <div style={{ marginTop: 10, borderTop: '1px solid #30363d', paddingTop: 10 }}>
                      <DefectList
                        review={review}
                        phaseId={phase.phase_id}
                        projectId={projectId!}
                        isLastPhase={isLastPhase}
                        batchSubmitting={batchSubmitting}
                        onBatchSubmit={submitAllIssuesToPm}
                        onIssueClick={(issue, pid) => {
                          setIssueModal({ open: true, issue, phaseId: pid, suggestedFix: issue.fix_hint || issue.message });
                          setIssueEdit(issue.fix_hint || issue.message);
                        }}
                        onTriggerFix={triggerAgentFix}
                        triggeringFix={triggeringFix}
                        onNavigateEngineer={() => {
                          (async () => {
                            try {
                              await axios.post(`${API}/projects/${projectId}/phases/${phase.phase_id}/transfer-to-engineer`);
                            } catch { /* silent */ }
                          })();
                          navigate(`/engineer/${projectId}?tab=repair`);
                        }}
                      />
                    </div>
                  ) : null}
                />
              </div>
            )}
          </PhaseCard>
        );
      })}

      {/* 任务预览弹窗 */}
      <Modal
        title={`任务预览 - ${phases.find(p => p.phase_id === taskPreviewPhase)?.name || ''}`}
        open={!!taskPreviewPhase}
        width={600}
        onCancel={() => { setTaskPreviewPhase(null); setTaskPreviewDesc(''); }}
        afterOpenChange={(open) => {
          if (open && taskPreviewPhase) {
            const p = phases.find(ph => ph.phase_id === taskPreviewPhase);
            setTaskPreviewDesc(p?.description || '');
          }
        }}
        footer={[
          <Button key="cancel" onClick={() => { setTaskPreviewPhase(null); setTaskPreviewDesc(''); }}>关闭</Button>,
          <Button key="save" type="primary" loading={savingTaskDesc} onClick={async () => {
            if (!taskPreviewPhase) return;
            setSavingTaskDesc(true);
            try {
              await axios.patch(`${API}/projects/${projectId}/phases/${taskPreviewPhase}`, { description: taskPreviewDesc });
              message.success('任务描述已保存');
              setPhases(prev => prev.map(p => p.phase_id === taskPreviewPhase ? { ...p, description: taskPreviewDesc } : p));
              setTaskPreviewPhase(null); setTaskPreviewDesc('');
            } catch { message.error('保存失败'); }
            finally { setSavingTaskDesc(false); }
          }}>保存修改</Button>,
        ]}
      >
        {(() => {
          const p = phases.find(ph => ph.phase_id === taskPreviewPhase);
          if (!p) return null;
          const pmMsgs = pmChats[p.phase_id] || [];
          const pmPlan = parsePlanFromMessages(pmMsgs, p.description);
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Tag color={p.status === 'completed' ? 'success' : p.status === 'active' ? 'processing' : 'default'}>
                  {p.status === 'completed' ? '已完成' : p.status === 'active' ? '进行中' : '待启动'}
                </Tag>
                {p.duration && <Tag>{p.duration}</Tag>}
                <span style={{ fontSize: 12, color: '#8c8c8c', alignSelf: 'center' }}>
                  阶段 {phases.findIndex(ph => ph.phase_id === p.phase_id) + 1} / {phases.length}
                </span>
              </div>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
                  任务描述
                  <span style={{ fontSize: 11, color: '#8c8c8c', fontWeight: 400, marginLeft: 8 }}>可直接编辑修改</span>
                </div>
                <TextArea rows={4} value={taskPreviewDesc} onChange={e => setTaskPreviewDesc(e.target.value)} placeholder="输入阶段任务描述..." style={{ fontSize: 13 }} />
              </div>
              {pmPlan.tasks.length > 0 ? (
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
                    与阶段PM确认的任务清单
                    <span style={{ fontSize: 11, color: '#52c41a', fontWeight: 400, marginLeft: 8 }}>来自对话记录</span>
                  </div>
                  <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', borderRadius: 6, padding: '10px 14px' }}>
                    {pmPlan.tasks.map((t, i) => (
                      <div key={i} style={{ fontSize: 12, padding: '3px 0', borderBottom: i < pmPlan.tasks.length - 1 ? '1px solid #d9f7be' : 'none', lineHeight: 1.6 }}>
                        {i + 1}. {t}
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div style={{ background: '#fffbe6', border: '1px solid #ffe58f', borderRadius: 6, padding: '10px 14px', fontSize: 12, color: '#8c8c8c' }}>
                  尚未与阶段PM讨论任务细则。点击「阶段PM」按钮开始对话，对话后任务清单将自动显示在此处。
                </div>
              )}
              {pmPlan.roles.length > 0 && (
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>人员规划</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {pmPlan.roles.map((r, i) => <Tag key={i} color="blue" style={{ fontSize: 12 }}>{r}</Tag>)}
                  </div>
                </div>
              )}
            </div>
          );
        })()}
      </Modal>

      <Modal
        open={showDecisionModal}
        title={autoRepairDecisionTitle}
        maskClosable={false}
        closable={!decisionSubmitting}
        onCancel={() => { if (!decisionSubmitting) setShowDecisionModal(false); }}
        footer={[
          allowedAutoRepairDecisions.has('manual_fix') &&
          <Button
            key="manual"
            data-testid="qc-action-manual-fix"
            icon={<EditOutlined />}
            loading={decisionSubmitting === 'manual_fix'}
            disabled={!!decisionSubmitting && decisionSubmitting !== 'manual_fix'}
            onClick={() => void openManualRepair()}
          >
            {autoRepairAction?.status === 'passed_with_handoff' ? '进入全栈工程师' : '自行修改'}
          </Button>,
          allowedAutoRepairDecisions.has('retry_cycle') &&
          <Button
            key="retry"
            data-testid="qc-action-retry-cycle"
            type="primary"
            icon={<ReloadOutlined />}
            loading={decisionSubmitting === 'retry_cycle'}
            disabled={!!decisionSubmitting && decisionSubmitting !== 'retry_cycle'}
            onClick={() => autoRepairAction?.phaseId && void runAutoRepair(autoRepairAction.phaseId, '', 'retry_cycle')}
          >
            继续质检循环
          </Button>,
          allowedAutoRepairDecisions.has('rebuild_phase') &&
          <Button
            key="rebuild"
            data-testid="qc-action-rebuild-phase"
            danger
            icon={<ThunderboltOutlined />}
            loading={decisionSubmitting === 'rebuild_phase'}
            disabled={!!decisionSubmitting && decisionSubmitting !== 'rebuild_phase'}
            onClick={confirmPhaseRebuild}
          >
            阶段重构
          </Button>,
        ].filter(Boolean)}
      >
        <Alert
          data-testid="phase-qc-decision"
          type="warning"
          showIcon
          message={autoRepairAction?.message || '仍有未解决问题，已按文件聚合。'}
          description={autoRepairDecisionDescription}
        />
        <div style={{ maxHeight: '45vh', overflowY: 'auto', marginTop: 10 }}>
        {Object.entries(autoRepairAction?.issueReport || {}).map(([file, issues]) => (
          <div key={file} style={{ marginTop: 10, padding: 10, border: '1px solid #f0f0f0', borderRadius: 6 }}>
            <div style={{ fontWeight: 600, fontSize: 12 }}>{file}</div>
            {(issues || []).map((issue, index) => (
              <div key={index} style={{ fontSize: 12, color: '#595959', marginTop: 4 }}>
                [{issue.severity || 'warning'}] {issue.message || '未提供问题描述'}
                {issue.fix_hint && <div style={{ color: '#8c8c8c', marginTop: 2 }}>建议：{issue.fix_hint}</div>}
              </div>
            ))}
          </div>
        ))}
        </div>
      </Modal>

      {/* 项目全部阶段完成弹窗 */}
      <Modal
        open={projectDoneModal}
        title="所有阶段已完成！"
        onCancel={() => setProjectDoneModal(false)}
        footer={[
          <Button key="close" onClick={() => setProjectDoneModal(false)}>稍后处理</Button>,
          <Button key="supervisor" type="primary" icon={<SafetyOutlined />}
            onClick={() => { setProjectDoneModal(false); navigate(`/app/projects/${projectId}/supervisor`); }}>
            进入监督组长全项目质检
          </Button>,
        ]}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Alert type="success" message="恭喜！所有开发阶段均已通过质检并确认完成。" showIcon />
          <div style={{ fontSize: 13, color: '#595959', lineHeight: 1.8 }}>
            下一步建议：
            <ul style={{ margin: '6px 0', paddingLeft: 20 }}>
              <li>代码整改：处理所有 needs_manual 状态的遗留问题</li>
              <li>撰写文档：生成用户手册、API 文档、部署手册</li>
              <li>文件归档：对项目文件进行分类</li>
              <li>项目问答：可回答关于项目任何技术或业务问题</li>
            </ul>
          </div>
        </div>
      </Modal>

      {/* 反馈问题给阶段PM弹窗 */}
      <Modal
        title="反馈问题给阶段PM"
        open={issueModal.open}
        onCancel={() => setIssueModal({ open: false, issue: null, phaseId: '', suggestedFix: '' })}
        onOk={submitIssueToPm}
        okText="提交给阶段PM"
        cancelText="取消"
        confirmLoading={submittingIssue}
      >
        {issueModal.issue && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ background: '#fff7e6', border: '1px solid #ffd591', borderRadius: 6, padding: '8px 12px', fontSize: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>质检识别的问题：</div>
              <div>{issueModal.issue.message}</div>
              {issueModal.issue.file_path && <div style={{ color: '#8c8c8c', marginTop: 4 }}>涉及文件：{issueModal.issue.file_path}</div>}
              <div style={{ color: '#8c8c8c', marginTop: 4 }}>负责 Agent：{issueModal.issue.responsible_agent_role}</div>
            </div>
            <div>
              <div style={{ fontSize: 12, color: '#595959', marginBottom: 6 }}>修改说明（已根据质检建议自动填充，可修改）：</div>
              <TextArea rows={4} value={issueEdit} onChange={e => setIssueEdit(e.target.value)} placeholder="描述需要如何修改.." />
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default PhaseBoard;
