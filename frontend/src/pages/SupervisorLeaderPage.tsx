/**
 * 监督组长页面
 * - 展示所有阶段的监督状态（通过/未通过/待审查）
 * - 与监督组长 Agent 对话（了解项目全貌，能回答阶段规划/质检状况等问题）
 * - 显示每个阶段的问题列表和整体进度
 */
import React, { useState, useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import {
  Button, Tag, Badge, Alert, Spin, Divider, Input, Card, Progress, message, Modal, Collapse,
} from 'antd';
import {
  SafetyOutlined, CheckCircleOutlined, CloseCircleOutlined,
  ClockCircleOutlined, SendOutlined, ReloadOutlined, BugOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';
import { FinalAcceptancePanel, SupervisorRunPanel } from './PhaseBoard/QualityEvidencePanel';
import { FinalQAStatus, SupervisorRun } from './PhaseBoard/types';
import { normalizeSupervisorRun } from './PhaseBoard/supervisorRunModel';

const API = API_BASE_URL;
const { TextArea } = Input;
const FINAL_QA_TERMINAL_STATES = new Set([
  'passed', 'needs_manual', 'failed', 'infrastructure_blocked', 'interrupted',
  'quality_regressed', 'no_progress', 'qa_blocked', 'awaiting_manual_fix',
]);

interface PhaseStatus {
  member_id: string;
  name: string;
  assigned_phase_id: string | null;
  assigned_phase_name: string | null;
  status: string;
  phase_passed: boolean;
  total_issues: number;
  open_issues: number;
  issues: Issue[];
}

interface Issue {
  id: string;
  message: string;
  file_path: string;
  severity: string;
  status: string;
  fix_hint: string;
  responsible_agent_role: string;
  fix_rounds: number;
  needs_manual_reason?: string;
}

interface ChatMsg {
  role: 'user' | 'assistant';
  content: string;
  ts: number;
}

interface PhaseInfo {
  phase_id: string;
  name: string;
  status: string;
  user_confirmed?: boolean;
  supervisor_run_summary?: SupervisorRun | null;
}

const SupervisorLeaderPage: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const [phases, setPhases] = useState<PhaseInfo[]>([]);
  const [phaseStatuses, setPhaseStatuses] = useState<PhaseStatus[]>([]);
  const [loading, setLoading] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [sending, setSending] = useState(false);
  const [inputVal, setInputVal] = useState('');
  const [selectedPhaseId, setSelectedPhaseId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const finalQAPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [finalQARunning, setFinalQARunning] = useState(false);
  const [finalQA, setFinalQA] = useState<FinalQAStatus>({ status: 'not_started', round: 0, total_rounds: 5, logs: [], user_reports: [] });
  const [finalQAOpen, setFinalQAOpen] = useState(false);
  const [supervisorRuns, setSupervisorRuns] = useState<Record<string, SupervisorRun>>({});

  useEffect(() => {
    if (!projectId) return;
    loadData();
    void loadFinalQA();
  }, [projectId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs]);

  useEffect(() => () => {
    if (finalQAPollRef.current) clearInterval(finalQAPollRef.current);
  }, []);

  const activeSupervisorRunKey = Object.values(supervisorRuns)
    .filter(run => run.active)
    .map(run => `${run.run_id}:${run.updated_at || ''}`)
    .sort()
    .join('|');

  useEffect(() => {
    if (!projectId || phases.length === 0 || !activeSupervisorRunKey) return;
    const timer = setInterval(() => void loadSupervisorRuns(phases), 2500);
    return () => clearInterval(timer);
  }, [projectId, phases, activeSupervisorRunKey]);

  const loadFinalQA = async () => {
    try {
      const res = await axios.get(`${API}/projects/${projectId}/final-qa/status`);
      const next = res.data as FinalQAStatus;
      setFinalQA(next);
      const terminal = FINAL_QA_TERMINAL_STATES.has(next.status);
      const active = !terminal && next.status !== 'not_started';
      setFinalQARunning(active);
      if (terminal && finalQAPollRef.current) {
        clearInterval(finalQAPollRef.current);
        finalQAPollRef.current = null;
      } else if (active && !finalQAPollRef.current) {
        finalQAPollRef.current = setInterval(() => void loadFinalQA(), 2500);
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '最终验收状态加载失败');
    }
  };

  const startFinalQA = async () => {
    setFinalQARunning(true);
    setFinalQAOpen(true);
    try {
      await axios.post(`${API}/projects/${projectId}/final-qa`);
      await loadFinalQA();
    } catch (e: any) {
      setFinalQARunning(false);
      message.error(e.response?.data?.detail || '全项目质检启动失败');
    }
  };

  const loadSupervisorRuns = async (phaseList: PhaseInfo[]) => {
    if (!projectId || phaseList.length === 0) return;
    const results = await Promise.all(phaseList.map(async phase => {
      try {
        const res = await axios.get(
          `${API}/projects/${projectId}/phases/${phase.phase_id}/auto-repair/status`,
          { timeout: 15000 },
        );
        const run = (res.data?.supervisor_run || phase.supervisor_run_summary) as SupervisorRun | undefined;
        return run ? [phase.phase_id, normalizeSupervisorRun(run)] as const : null;
      } catch {
        const fallback = phase.supervisor_run_summary;
        return fallback ? [phase.phase_id, normalizeSupervisorRun(fallback)] as const : null;
      }
    }));
    setSupervisorRuns(Object.fromEntries(
      results.filter((item): item is readonly [string, SupervisorRun] => !!item),
    ));
  };

  const loadData = async () => {
    setLoading(true);
    try {
      // 加载阶段列表
      const phaseRes = await axios.get(`${API}/projects/${projectId}/phases`);
      const phaseList: PhaseInfo[] = phaseRes.data.phases || [];
      setPhases(phaseList);

      // 同一阶段响应同时包含成员状态和可选的 Supervisor 运行摘要。
      const supPhases: PhaseStatus[] = phaseRes.data.supervisor_phases || [];
      setPhaseStatuses(supPhases);
      await loadSupervisorRuns(phaseList);
    } catch (e) {
      message.error('加载失败');
    } finally {
      setLoading(false);
    }
  };

  const sendMsg = async () => {
    const text = inputVal.trim();
    if (!text) return;
    if (!selectedPhaseId) {
      message.warning('请先选择阶段');
      return;
    }
    setInputVal('');
    const newMsgs: ChatMsg[] = [...msgs, { role: 'user', content: text, ts: Date.now() }];
    setMsgs(newMsgs);
    setSending(true);
    try {
      const res = await axios.post(`${API}/projects/${projectId}/phases/${selectedPhaseId}/supervisor-chat`, {
        message: text,
        history: msgs.slice(-10).map(m => ({ role: m.role, content: m.content })),
      });
      setMsgs([...newMsgs, { role: 'assistant', content: res.data.reply || '', ts: Date.now() }]);
    } catch {
      message.error('发送失败');
    } finally {
      setSending(false);
    }
  };

  const totalPhases = phases.length;
  const completedPhases = phases.filter(p => p.status === 'completed').length;
  const activePhases = phases.filter(p => p.status === 'active' || p.status === 'reviewing').length;
  const allPhasesCompleted = totalPhases > 0 && phases.every(p => p.user_confirmed === true);
  const finalQAStarted = finalQA.status !== 'not_started';

  // 找到有问题的阶段（有 open issues 的）
  const problematicPhases = phaseStatuses.filter(ps =>
    ps.assigned_phase_id && ps.open_issues > 0
  );

  // 统计 needs_manual 问题
  const manualIssues = phaseStatuses.flatMap(ps =>
    (ps.issues || []).filter(i => i.status === 'needs_manual')
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* 顶部：总体监督状态 */}
      <Card size="small" style={{ borderRadius: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <SafetyOutlined style={{ color: '#1677ff', fontSize: 18 }} />
            <span style={{ fontWeight: 600, fontSize: 15 }}>Supervisor 监督组长</span>
          </div>
          <Button size="small" icon={<ReloadOutlined />} onClick={loadData}>刷新</Button>
        </div>

        <div style={{ display: 'flex', gap: 24, marginBottom: 12 }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#1677ff' }}>{totalPhases}</div>
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>总阶段</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#52c41a' }}>{completedPhases}</div>
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>已完成</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#fa8c16' }}>{activePhases}</div>
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>进行中</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#ff4d4f' }}>{manualIssues.length}</div>
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>需人工</div>
          </div>
        </div>

        {totalPhases > 0 && (
          <Progress
            percent={Math.round(completedPhases / totalPhases * 100)}
            format={() => `${completedPhases}/${totalPhases} 完成`}
            strokeColor={{ '0%': '#1677ff', '100%': '#52c41a' }}
            size="small"
          />
        )}
      </Card>

      {Object.keys(supervisorRuns).length > 0 && (
        <Card size="small" title="Supervisor 状态机" style={{ borderRadius: 8 }}>
          <Alert
            type="info"
            showIcon
            message="业务质检固定为 5 轮；基础设施或模型失败不计入业务轮次，关键 Agent 失败和缺少验证证据均不得完成。"
            style={{ marginBottom: 10 }}
          />
          <Collapse
            size="small"
            items={Object.entries(supervisorRuns).map(([phaseId, run]) => {
              const phase = phases.find(item => item.phase_id === phaseId);
              return {
                key: phaseId,
                label: `${phase?.name || phaseId} · ${run.state} · 第 ${Math.min(run.qa_round || 0, 5)}/5 轮`,
                children: <SupervisorRunPanel run={run} />,
              };
            })}
          />
        </Card>
      )}

      <Card size="small" style={{ borderRadius: 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <div>
            <div style={{ fontWeight: 600 }}>全项目质检循环</div>
            <div style={{ fontSize: 12, color: '#8c8c8c', marginTop: 4 }}>扫描全部项目文件、检查跨模块冲突并自动返工；每轮生成用户报告。</div>
          </div>
          <Button
            type="primary"
            icon={<CheckCircleOutlined />}
            loading={finalQARunning}
            disabled={!allPhasesCompleted}
            onClick={() => finalQAStarted ? setFinalQAOpen(true) : void startFinalQA()}
          >
            {finalQARunning ? '质检循环中' : finalQAStarted ? '查看最终验收' : '开始全项目质检循环'}
          </Button>
        </div>
        {!allPhasesCompleted && <Alert style={{ marginTop: 10 }} type="warning" showIcon message="所有阶段确认完成后才能启动全项目质检" />}
        {finalQA.status !== 'not_started' && (
          <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Tag color={finalQA.status === 'passed' ? 'success' : ['needs_manual', 'failed', 'infrastructure_blocked'].includes(finalQA.status) ? 'error' : 'processing'}>
              {finalQA.status === 'infrastructure_blocked' ? '验收基础设施阻断' : finalQA.status}
            </Tag>
            <span style={{ fontSize: 12 }}>第 {finalQA.round || 0}/{finalQA.total_rounds || 5} 轮</span>
            {finalQA.restored_from_persisted_result && <Tag color="blue">刷新后已恢复</Tag>}
            <Button size="small" onClick={() => { setFinalQAOpen(true); void loadFinalQA(); }}>查看验收证据</Button>
          </div>
        )}
      </Card>

      <Modal open={finalQAOpen} title="全项目最终验收" width={820} footer={null} onCancel={() => setFinalQAOpen(false)}>
        <FinalAcceptancePanel
          state={finalQA}
          actionLoading={finalQARunning}
          onAction={(action) => {
            if (action === 'retry_acceptance') void startFinalQA();
          }}
        />
        <Divider>质检轮次报告</Divider>
        {(finalQA.user_reports || []).length === 0 ? (
          <Alert type="info" showIcon message={finalQARunning ? '质检进行中，报告生成后将自动显示' : '暂无质检报告'} />
        ) : (
          <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
            {(finalQA.user_reports || []).map((report: any, index: number) => (
              <Card key={`${report.round}-${report.subproject_id}-${index}`} size="small" style={{ marginBottom: 10 }}
                title={`第 ${report.round} 轮 · ${report.name || report.subproject_id}`}
                extra={<Tag color={report.passed ? 'success' : 'error'}>{report.passed ? '通过' : `${report.error_count || 0} 错误 / ${report.warning_count || 0} 警告`}</Tag>}>
                <div style={{ whiteSpace: 'pre-wrap', fontSize: 12, lineHeight: 1.7 }}>
                  {report.report || `评分 ${report.score || 0}，错误 ${report.error_count || 0}，警告 ${report.warning_count || 0}`}
                </div>
              </Card>
            ))}
          </div>
        )}
      </Modal>

      {loading && (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin size="large" tip="加载中..."><div style={{height:60}} /></Spin>
        </div>
      )}

      {/* 阶段监督状态列表 */}
      {!loading && phaseStatuses.length > 0 && (
        <Card size="small" title="各阶段监督状态" style={{ borderRadius: 8 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {phaseStatuses.map(ps => {
              if (!ps.assigned_phase_id) return null;
              const phase = phases.find(p => p.phase_id === ps.assigned_phase_id);
              const isSelected = selectedPhaseId === ps.assigned_phase_id;
              const openIssues = (ps.issues || []).filter(i => i.status === 'open');
              const manualIssuesInPhase = (ps.issues || []).filter(i => i.status === 'needs_manual');

              return (
                <div
                  key={ps.member_id}
                  onClick={() => setSelectedPhaseId(isSelected ? null : ps.assigned_phase_id)}
                  style={{
                    padding: '10px 12px',
                    borderRadius: 6,
                    border: `1px solid ${isSelected ? '#1677ff' : ps.phase_passed ? '#b7eb8f' : ps.open_issues > 0 ? '#ffccc7' : '#e8e8e8'}`,
                    background: isSelected ? '#e6f4ff' : ps.phase_passed ? '#f6ffed' : ps.open_issues > 0 ? '#fff2f0' : '#fafafa',
                    cursor: 'pointer',
                    transition: 'all 0.2s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {ps.phase_passed
                        ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
                        : ps.open_issues > 0
                          ? <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
                          : <ClockCircleOutlined style={{ color: '#8c8c8c' }} />
                      }
                      <span style={{ fontWeight: 500, fontSize: 13 }}>
                        {ps.assigned_phase_name || ps.assigned_phase_id}
                      </span>
                      {phase && (
                        <Tag color={phase.status === 'completed' ? 'success' : phase.status === 'active' ? 'processing' : 'default'} style={{ fontSize: 10 }}>
                          {phase.status === 'completed' ? '已完成' : phase.status === 'active' ? '进行中' : '待启动'}
                        </Tag>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      {openIssues.length > 0 && <Badge count={openIssues.length} style={{ backgroundColor: '#ff4d4f' }} />}
                      {manualIssuesInPhase.length > 0 && (
                        <Tag color="warning" icon={<BugOutlined />} style={{ fontSize: 10 }}>
                          {manualIssuesInPhase.length} 需人工
                        </Tag>
                      )}
                      {ps.phase_passed && <Tag color="success" style={{ fontSize: 10 }}>质检通过</Tag>}
                    </div>
                  </div>

                  {/* 展开：问题列表 */}
                  {isSelected && ps.issues && ps.issues.length > 0 && (
                    <div style={{ marginTop: 10, borderTop: '1px solid #f0f0f0', paddingTop: 8 }}>
                      <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>问题列表（点击阶段标题选中，可在下方对话中提问）</div>
                      {ps.issues.map(issue => (
                        <div key={issue.id} style={{
                          padding: '4px 8px',
                          marginBottom: 4,
                          borderRadius: 4,
                          background: issue.status === 'needs_manual' ? '#fffbe6' : issue.status === 'fixed' ? '#f6ffed' : '#fff2f0',
                          border: `1px solid ${issue.status === 'needs_manual' ? '#ffe58f' : issue.status === 'fixed' ? '#b7eb8f' : '#ffccc7'}`,
                          fontSize: 11,
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Tag
                              color={issue.severity === 'error' ? 'error' : 'warning'}
                              style={{ fontSize: 9, margin: 0 }}
                            >
                              {issue.severity}
                            </Tag>
                            <Tag
                              color={issue.status === 'needs_manual' ? 'orange' : issue.status === 'fixed' ? 'success' : issue.status === 'fixing' ? 'processing' : 'error'}
                              style={{ fontSize: 9, margin: 0 }}
                            >
                              {issue.status === 'needs_manual' ? '需人工' : issue.status === 'fixed' ? '已修复' : issue.status === 'fixing' ? '修复中' : '待修复'}
                            </Tag>
                            <span style={{ flex: 1, color: '#595959' }}>{issue.message}</span>
                          </div>
                          {issue.file_path && (
                            <div style={{ color: '#8c8c8c', marginTop: 2, paddingLeft: 2 }}>
                              📄 {issue.file_path}
                            </div>
                          )}
                          {issue.fix_rounds > 0 && (
                            <div style={{ color: '#fa8c16', fontSize: 10, marginTop: 2 }}>
                              已尝试修复 {issue.fix_rounds} 次
                            </div>
                          )}
                          {issue.needs_manual_reason && (
                            <div style={{ color: '#d48806', fontSize: 10, marginTop: 2 }}>
                              ⚠️ {issue.needs_manual_reason}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {!loading && phaseStatuses.length === 0 && phases.length === 0 && (
        <Alert
          type="info"
          message="尚未启动任何阶段"
          description="请先在「阶段看板」与 PM 组长确认规划并启动阶段，监督组长将自动跟踪各阶段质检状态。"
          showIcon
        />
      )}

      {/* 监督组长对话 */}
      <Card
        size="small"
        style={{ borderRadius: 8 }}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <SafetyOutlined style={{ color: '#52c41a' }} />
            <span>与监督组长对话</span>
            {selectedPhaseId && (
              <Tag color="blue" style={{ fontSize: 10 }}>
                当前关注：{phases.find(p => p.phase_id === selectedPhaseId)?.name || selectedPhaseId}
              </Tag>
            )}
          </div>
        }
      >
        <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 8 }}>
          监督组长知道项目总体规划，可以回答：各阶段状态、质检问题原因、修复建议、是否可进入下一阶段等。
          点击上方阶段可聚焦特定阶段提问。
        </div>

        {/* 对话历史 */}
        <div style={{
          height: 320,
          overflowY: 'auto',
          background: '#1e1e2e',
          borderRadius: 8,
          padding: '12px',
          marginBottom: 10,
        }}>
          {msgs.length === 0 && (
            <div style={{ color: '#8b949e', fontSize: 12, textAlign: 'center', paddingTop: 80 }}>
              发送消息开始与监督组长沟通，可询问任何阶段的质检状况...
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} style={{
              display: 'flex',
              justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
              marginBottom: 10,
            }}>
              <div style={{
                maxWidth: '80%',
                padding: '8px 12px',
                borderRadius: m.role === 'user' ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
                background: m.role === 'user' ? '#1677ff' : '#2d333b',
                color: m.role === 'user' ? '#fff' : '#e6edf3',
                fontSize: 12,
                whiteSpace: 'pre-wrap',
                lineHeight: 1.6,
              }}>
                {m.content}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {/* 快捷提问 */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
          {[
            '当前项目整体进度如何？',
            '哪些阶段还有未解决的问题？',
            '有哪些 needs_manual 问题需要人工处理？',
            '现在可以进入下一阶段吗？',
          ].map(q => (
            <Button
              key={q}
              size="small"
              style={{ fontSize: 11 }}
              onClick={() => setInputVal(q)}
            >
              {q}
            </Button>
          ))}
        </div>

        {/* 输入框 */}
        <div style={{ display: 'flex', gap: 8 }}>
          <TextArea
            value={inputVal}
            onChange={e => setInputVal(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); } }}
            placeholder="与监督组长沟通质检状况、修复进度..."
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={sending}
            style={{ flex: 1, fontSize: 13 }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={sending}
            onClick={sendMsg}
            disabled={!inputVal.trim()}
          >
            发送
          </Button>
        </div>
      </Card>
    </div>
  );
};

export default SupervisorLeaderPage;
