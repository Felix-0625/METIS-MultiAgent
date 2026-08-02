/**
 * 项目调整页面 v2 — 工作群模式
 * 所有角色（PM / HR / 阶段负责人 / 质检 / 专家）都在同一个对话流里响应
 * 用户只在两个时机需要手动操作：
 *   1. 确认 HR 分工方案（开始第一阶段）
 *   2. 每个阶段完成后确认开启下一阶段
 */
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { Button, Input, Spin, Tag, message, Badge } from 'antd';
import {
  SendOutlined, LoadingOutlined, TeamOutlined, ReloadOutlined,
  CheckCircleOutlined, SyncOutlined, WarningOutlined, ClockCircleOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;
const { TextArea } = Input;

// ─── 类型 ─────────────────────────────────────────────────────────────────────

type MsgRole = 'user' | 'pm' | 'hr' | 'phase_lead' | 'qa' | 'system' | 'log';

interface GroupMsg {
  role: MsgRole;
  content: string;
  ts: number;
  adj_id?: string;       // 关联工单 ID（system 消息附带）
  phase_index?: number;  // 阶段序号（phase_lead 附带）
}

interface AdjPhase {
  phase_index: number;
  tasks: string[];
  status: 'pending' | 'executing' | 'qc_running' | 'done' | 'needs_manual';
  qc_passed?: boolean;
}

interface AdjTask {
  task_id: string;
  expert_role: string;
  description: string;
  files: string[];
  depends_on: string[];
  priority: string;
  status: 'pending' | 'executing' | 'done' | 'failed';
  error?: string;
}

interface Adjustment {
  id: string;
  title: string;
  description: string;
  impact_analysis: string;
  tasks: AdjTask[];
  phases: AdjPhase[];
  current_phase_index: number;
  status: string;
  created_at: number;
  updated_at: number;
  exec_log: string[];
  qc_results: Record<string, any>;
  snapshot_version?: number;
}

// ─── 角色样式配置 ──────────────────────────────────────────────────────────────

const ROLE_CFG: Record<MsgRole, { label: string; color: string; prefix: string }> = {
  user:       { label: '你',       color: '#ffa657', prefix: 'you'       },
  pm:         { label: 'PM组长',   color: '#58a6ff', prefix: 'pm'        },
  hr:         { label: 'HR',       color: '#3fb950', prefix: 'hr'        },
  phase_lead: { label: '阶段负责', color: '#d2a8ff', prefix: 'phase-lead'},
  qa:         { label: '质检',     color: '#f97583', prefix: 'qa'        },
  system:     { label: '系统',     color: '#8b949e', prefix: 'sys'       },
  log:        { label: '日志',     color: '#484f58', prefix: '·'         },
};

// ─── 阶段确认卡（内嵌在 system 消息里）────────────────────────────────────────

const PhaseConfirmCard: React.FC<{
  adjId: string;
  phaseIndex: number;
  adj: Adjustment | null;
  onConfirm: (adjId: string, phaseIndex: number) => void;
  confirming: boolean;
}> = ({ adjId, phaseIndex, adj, onConfirm, confirming }) => {
  if (!adj) return null;
  const phase = adj.phases[phaseIndex];
  if (!phase) return null;

  // 已经在执行或完成，只显示状态不显示按钮
  const isDone = phase.status === 'done';
  const isExecuting = phase.status === 'executing' || phase.status === 'qc_running';
  const needsManual = phase.status === 'needs_manual';
  const isPending = phase.status === 'pending';

  const phaseTasks = adj.tasks.filter(t => phase.tasks.includes(t.task_id));

  return (
    <div style={{
      marginTop: 8, background: '#161b22', border: '1px solid #1d6fa4',
      borderRadius: 6, overflow: 'hidden', fontSize: 11, fontFamily: 'monospace',
    }}>
      {/* 头部 */}
      <div style={{ background: '#1c2a3a', padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: '#58a6ff' }}>阶段 {phaseIndex + 1}</span>
        <span style={{ color: '#f0f6fc', fontWeight: 600 }}>{adj.title}</span>
        {isDone && <span style={{ marginLeft: 'auto', color: '#3fb950' }}>✅ 已完成</span>}
        {isExecuting && <span style={{ marginLeft: 'auto', color: '#58a6ff' }}><SyncOutlined spin /> 执行中</span>}
        {needsManual && <span style={{ marginLeft: 'auto', color: '#ff7b72' }}>⚠️ 需人工</span>}
        {isPending && <span style={{ marginLeft: 'auto', color: '#8b949e' }}>⏳ 等待确认</span>}
      </div>

      {/* 任务列表 */}
      <div style={{ padding: '6px 10px' }}>
        {phaseTasks.map((t, i) => (
          <div key={t.task_id} style={{ display: 'flex', gap: 6, padding: '2px 0', borderBottom: i < phaseTasks.length - 1 ? '1px solid #21262d' : 'none' }}>
            <span style={{ color: '#484f58', minWidth: 16 }}>{i + 1}.</span>
            <span style={{ color: '#ffa657' }}>{t.expert_role}</span>
            <span style={{ color: '#8b949e', flex: 1 }}>：{t.description.slice(0, 55)}{t.description.length > 55 ? '...' : ''}</span>
            {(isExecuting || isDone) && (
              <span style={{ color: t.status === 'done' ? '#3fb950' : t.status === 'executing' ? '#58a6ff' : t.status === 'failed' ? '#ff7b72' : '#484f58' }}>
                {t.status === 'done' ? '✓' : t.status === 'executing' ? '⟳' : t.status === 'failed' ? '✗' : '○'}
              </span>
            )}
          </div>
        ))}
      </div>

      {/* 质检结果 */}
      {isDone && adj.qc_results[String(phaseIndex)] && (
        <div style={{ padding: '4px 10px', borderTop: '1px solid #30363d', color: adj.qc_results[String(phaseIndex)].passed ? '#3fb950' : '#ffa657' }}>
          {adj.qc_results[String(phaseIndex)].passed ? '✅ 质检通过' : `⚠️ 质检发现 ${adj.qc_results[String(phaseIndex)].issues?.length || 0} 个问题`}
          {adj.snapshot_version && <span style={{ marginLeft: 8, color: '#58a6ff' }}>📦 v{adj.snapshot_version}</span>}
        </div>
      )}

      {/* 确认按钮：第一阶段显示「确认分工，开始执行」，后续阶段显示「开启阶段N」 */}
      {isPending && (
        <div style={{ padding: '8px 10px', borderTop: '1px solid #30363d' }}>
          <button
            onClick={() => onConfirm(adjId, phaseIndex)}
            disabled={confirming}
            style={{
              width: '100%', background: confirming ? '#1c2a3a' : '#1d6fa4', border: 'none',
              borderRadius: 4, color: '#fff', fontSize: 11, padding: '5px 0',
              cursor: confirming ? 'not-allowed' : 'pointer', fontFamily: 'monospace',
            }}
          >
            {confirming ? '执行中...' : phaseIndex === 0 ? '✓ 确认分工方案，开始执行' : `✓ 确认阶段${phaseIndex}已完成，开启阶段${phaseIndex + 1}`}
          </button>
        </div>
      )}
    </div>
  );
};

// ─── 主组件 ──────────────────────────────────────────────────────────────────

const ProjectAdjustment: React.FC<{ projectId?: string }> = ({ projectId: projectIdProp }) => {
  const { id: routeProjectId } = useParams<{ id: string }>();
  const projectId = projectIdProp || routeProjectId;

  const [msgs, setMsgs] = useState<GroupMsg[]>([]);
  const [sending, setSending] = useState(false);
  const [adjustments, setAdjustments] = useState<Adjustment[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const logCursorRef = useRef<Record<string, number>>({});
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 用 ref 跟踪上一次每个工单的状态，用于检测状态变化
  const prevAdjStatusRef = useRef<Record<string, string>>({});

  // 自动滚到底
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [msgs, sending]);

  const loadAdjs = useCallback(async (): Promise<Adjustment[]> => {
    if (!projectId) return [];
    try {
      const res: any = await axios.get(`${API}/projects/${projectId}/adjustments`);
      const list: Adjustment[] = res.data.adjustments || [];
      setAdjustments(list);
      return list;
    } catch { return []; }
  }, [projectId]);

  // 把工单执行日志 + 状态变化同步到对话流
  // 修复：直接接收最新 adjs 参数，不依赖闭包 state，消除 race condition
  const syncLogs = useCallback((adjs: Adjustment[]) => {
    const newMsgs: GroupMsg[] = [];
    for (const adj of adjs) {
      // 任何有日志的工单都同步（不限状态），确保最后一批日志不丢失
      const cursor = logCursorRef.current[adj.id] ?? 0;
      const newLines = adj.exec_log.slice(cursor);
      if (newLines.length) {
        logCursorRef.current[adj.id] = adj.exec_log.length;
        for (const line of newLines) {
          // 质检相关日志用 qa 角色突出显示
          const role: MsgRole = (line.includes('质检') || line.includes('QC') || line.includes('✅') || line.includes('❌'))
            ? 'qa' : 'log';
          newMsgs.push({ role, content: line, ts: Date.now() });
        }
      }

      // 检测状态变化 → 生成结构化消息
      const prevStatus = prevAdjStatusRef.current[adj.id];
      const curStatus = adj.status;
      if (prevStatus !== curStatus) {
        prevAdjStatusRef.current[adj.id] = curStatus;

        // 阶段完成：插入「确认开启下一阶段」确认卡
        if (curStatus === 'phase_done') {
          const nextIdx = adj.current_phase_index + 1;
          if (nextIdx < adj.phases.length) {
            setMsgs(prev => {
              const hasPrompt = prev.some(
                m => m.role === 'system' && m.adj_id === adj.id && m.phase_index === nextIdx
              );
              if (hasPrompt) return prev;
              return [...prev, {
                role: 'system',
                content: `⏸ 阶段${adj.current_phase_index + 1}已完成，请确认后开启阶段${nextIdx + 1}`,
                ts: Date.now(),
                adj_id: adj.id,
                phase_index: nextIdx,
              }];
            });
          }
        }

        // 整体完成：追加成功消息
        if (curStatus === 'done' && prevStatus && prevStatus !== 'done') {
          newMsgs.push({
            role: 'qa',
            content: `✅ 所有阶段执行完毕，质检通过${adj.snapshot_version ? `，已生成版本快照 v${adj.snapshot_version}` : ''}`,
            ts: Date.now(),
          });
        }

        // needs_manual：追加警告
        if (curStatus === 'needs_manual' && prevStatus && prevStatus !== 'needs_manual') {
          newMsgs.push({
            role: 'qa',
            content: `⚠️ 执行完成，部分问题经多轮返工仍未解决，请前往文件管理页手动整改`,
            ts: Date.now(),
          });
        }

        if (['awaiting_final_qa', 'pending_final_qa'].includes(curStatus) && prevStatus !== curStatus) {
          newMsgs.push({
            role: 'qa',
            content: '✅ 调整阶段已完成，等待项目 Final QA 验证后才会正式结束。',
            ts: Date.now(),
          });
        }

        if (curStatus === 'recovery_required' && prevStatus !== 'recovery_required') {
          newMsgs.push({
            role: 'qa',
            content: '⚠️ 调整执行中断，需要恢复。请等待服务端恢复或刷新查看最新状态，不要重复提交工单。',
            ts: Date.now(),
          });
        }

        // qc_running：追加质检开始提示
        if (curStatus === 'qc_running' && prevStatus === 'executing') {
          newMsgs.push({
            role: 'qa',
            content: `🔍 阶段执行完毕，质检中（最多 5 轮自动返工）...`,
            ts: Date.now(),
          });
        }
      }

      // 阶段级状态变化：检测每个阶段的质检结果
      adj.phases.forEach((phase, idx) => {
        const phaseKey = `${adj.id}-phase-${idx}`;
        const prevPhaseStatus = prevAdjStatusRef.current[phaseKey];
        if (prevPhaseStatus !== phase.status) {
          prevAdjStatusRef.current[phaseKey] = phase.status;
          if (phase.status === 'done' && prevPhaseStatus === 'qc_running') {
            const qc = adj.qc_results?.[String(idx)];
            if (qc) {
              newMsgs.push({
                role: 'qa',
                content: qc.passed
                  ? `✅ 阶段${idx + 1}质检通过`
                  : `⚠️ 阶段${idx + 1}质检发现 ${qc.issues?.length || 0} 个问题，已触发自动返工`,
                ts: Date.now(),
              });
            }
          }
        }
      });
    }
    if (newMsgs.length) setMsgs(prev => [...prev, ...newMsgs]);
  }, []);

  useEffect(() => {
    loadAdjs();
    // 修复：setInterval 里直接调 loadAdjs 拿最新数据再 syncLogs，不再依赖闭包 state
    pollingRef.current = setInterval(() => {
      loadAdjs().then(syncLogs);
    }, 3000);
    return () => { if (pollingRef.current) clearInterval(pollingRef.current); };
  }, [loadAdjs, syncLogs]);

  // 发送消息
  const send = async () => {
    if (!input.trim() || sending || !projectId) return;
    const text = input.trim();
    setInput('');
    setMsgs(prev => [...prev, { role: 'user', content: text, ts: Date.now() }]);
    setSending(true);
    try {
      // 历史：只把 user/pm/hr 消息发给后端
      const history = msgs.filter(m => ['user', 'pm', 'hr'].includes(m.role))
        .slice(-10).map(m => ({
          role: m.role === 'user' ? 'user' : 'assistant',
          content: m.content,
        }));
      const res: any = await axios.post(`${API}/projects/${projectId}/adjustments/chat`, { message: text, history });
      const newMsgs: GroupMsg[] = (res.data.messages || []).map((m: any) => ({
        role: m.role as MsgRole,
        content: m.content,
        ts: Date.now(),
        adj_id: m.adj_id,
        phase_index: m.phase_index,
      }));
      setMsgs(prev => [...prev, ...newMsgs]);
      if (res.data.has_plan) {
        // 加载新工单，并为第一阶段追加确认卡
        const adjs = await loadAdjs();
        const newAdj = adjs.find(a => a.id === res.data.adjustment_plan?.adjustment_id);
        if (newAdj && newAdj.phases.length > 0) {
          setMsgs(prev => [...prev, {
            role: 'system',
            content: `📋 分工方案已就绪，共 ${newAdj.phases.length} 个阶段，请确认后开始执行`,
            ts: Date.now(),
            adj_id: newAdj.id,
            phase_index: 0,
          }]);
        }
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  // 确认阶段
  const confirmPhase = async (adjId: string, phaseIndex: number) => {
    if (!projectId) return;
    const key = `${adjId}-${phaseIndex}`;
    setConfirming(key);
    try {
      await axios.post(`${API}/projects/${projectId}/adjustments/${adjId}/confirm-phase`, {
        adjustment_id: adjId,
        phase_index: phaseIndex,
      });
      setMsgs(prev => [...prev, {
        role: 'system',
        content: `▶ 阶段${phaseIndex + 1}已开始执行，专家就位...`,
        ts: Date.now(),
      }]);
      await loadAdjs();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '确认失败');
    } finally {
      setConfirming(null);
    }
  };

  const getAdj = (adjId?: string) => adjustments.find(a => a.id === adjId) || null;

  // 统计
  const doneCount = adjustments.filter(a => a.status === 'done').length;
  const activeCount = adjustments.filter(a => [
    'executing', 'qc_running', 'awaiting_final_qa', 'pending_final_qa', 'recovery_required',
  ].includes(a.status)).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 0 }}>
      {/* 顶栏 */}
      <div style={{ background: '#fff', borderBottom: '1px solid #e8e8e8', padding: '10px 16px', display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
        <div>
          <span style={{ fontWeight: 600, fontSize: 14 }}>📝 项目调整</span>
          <span style={{ fontSize: 12, color: '#8c8c8c', marginLeft: 8 }}>工作群模式 — 描述需求后全程自动执行</span>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {activeCount > 0 && <Badge count={activeCount} color="#1677ff"><Tag color="processing">执行中</Tag></Badge>}
          <Tag color="success">{doneCount} 已完成</Tag>
          <Button size="small" icon={<ReloadOutlined />} onClick={loadAdjs}>刷新</Button>
        </div>
      </div>

      {/* 工作群 */}
      <div style={{
        flex: 1, background: '#0d1117', display: 'flex', flexDirection: 'column', minHeight: 0,
      }}>
        {/* 群标题栏 */}
        <div style={{
          background: '#161b22', padding: '8px 14px', borderBottom: '1px solid #30363d',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <div style={{ display: 'flex', gap: 5 }}>
            {['#ff5f57', '#febc2e', '#28c840'].map(c => (
              <div key={c} style={{ width: 10, height: 10, borderRadius: '50%', background: c }} />
            ))}
          </div>
          <TeamOutlined style={{ color: '#58a6ff' }} />
          <span style={{ color: '#8b949e', fontSize: 12, fontFamily: 'monospace' }}>
            # project-adjustment-team
          </span>
          <span style={{ marginLeft: 'auto', fontSize: 11, color: '#484f58' }}>
            PM · HR · 阶段负责 · 质检 · 专家
          </span>
        </div>

        {/* 消息流 */}
        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 14px', fontFamily: 'monospace', fontSize: 12, minHeight: 0 }}>
          {msgs.length === 0 && (
            <div style={{ color: '#484f58', paddingTop: 8 }}>
              <span style={{ color: '#58a6ff' }}>pm&gt; </span>
              <span>项目已完成，随时可以告诉我要调整什么。我会分析需求，HR 拆解分工，各阶段专家自动执行，过程全程在这里展示。</span>
            </div>
          )}

          {msgs.map((m, i) => {
            const cfg = ROLE_CFG[m.role];

            // 日志行（细粒度执行日志）
            if (m.role === 'log') {
              return (
                <div key={i} style={{ marginBottom: 2, paddingLeft: 14 }}>
                  <span style={{
                    fontSize: 11, color:
                      m.content.includes('✅') ? '#3fb950' :
                      m.content.includes('❌') ? '#ff7b72' :
                      m.content.includes('⚠️') ? '#ffa657' :
                      m.content.includes('▶') ? '#58a6ff' :
                      '#484f58',
                  }}>
                    {m.content}
                  </span>
                </div>
              );
            }

            // system 消息（可能附带阶段确认卡）
            if (m.role === 'system') {
              const adj = m.adj_id ? getAdj(m.adj_id) : null;
              const showCard = adj && m.phase_index !== undefined;
              return (
                <div key={i} style={{ marginBottom: 10, paddingLeft: 14 }}>
                  <span style={{ color: '#8b949e', fontSize: 11 }}>{m.content}</span>
                  {showCard && (
                    <PhaseConfirmCard
                      adjId={m.adj_id!}
                      phaseIndex={m.phase_index!}
                      adj={adj}
                      onConfirm={confirmPhase}
                      confirming={confirming === `${m.adj_id}-${m.phase_index}`}
                    />
                  )}
                </div>
              );
            }

            // 普通消息
            return (
              <div key={i} style={{ marginBottom: 12 }}>
                <span style={{ color: cfg.color }}>{cfg.prefix}&gt; </span>
                <div style={{
                  color: m.role === 'user' ? cfg.color : '#c9d1d9',
                  marginTop: m.role === 'user' ? 0 : 4,
                  paddingLeft: m.role === 'user' ? 0 : 14,
                  whiteSpace: 'pre-wrap', lineHeight: 1.7, display: m.role === 'user' ? 'inline' : 'block',
                }}>
                  {m.content}
                </div>
              </div>
            );
          })}

          {sending && (
            <div style={{ color: '#8b949e', marginBottom: 8 }}>
              <span style={{ color: '#58a6ff' }}>pm&gt; </span>
              <span>thinking <LoadingOutlined style={{ marginLeft: 4 }} /></span>
            </div>
          )}
        </div>

        {/* 输入区 */}
        <div style={{ borderTop: '1px solid #30363d', padding: '8px 14px', display: 'flex', gap: 8, alignItems: 'flex-end', background: '#0d1117' }}>
          <span style={{ color: '#58a6ff', fontFamily: 'monospace', fontSize: 12, flexShrink: 0 }}>you&gt;</span>
          <TextArea
            autoSize={{ minRows: 1, maxRows: 4 }}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
            placeholder="描述调整需求，例：登录页改为手机号验证码登录..."
            style={{ background: 'transparent', border: 'none', color: '#f0f6fc', fontFamily: 'monospace', fontSize: 12, resize: 'none', flex: 1, padding: 0 }}
            disabled={sending}
          />
          <Button type="text" size="small" onClick={send} loading={sending} style={{ color: '#58a6ff', flexShrink: 0 }}>
            <SendOutlined />
          </Button>
        </div>

      </div>
    </div>
  );
};

export default ProjectAdjustment;
