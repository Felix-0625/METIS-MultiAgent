/**
 * 进度看板 v3
 * - 每个子项目展开显示产出文件目录（从 workspace 真实读取）
 * - 子项目全部完成后自动弹出「进入下一步」提示
 * - Supervisor 监督状态实时显示
 * - 质检摘要 + 阻塞预警
 * - 5 秒轮询（执行期），15 秒轮询（其他阶段）
 */

import React, { useEffect, useState, useRef } from 'react';
import {
  Card, Table, Tag, Progress, Statistic, Row, Col, Button, Spin, Empty,
  Tooltip, List, Alert, Collapse, Modal, message,
} from 'antd';
import {
  CheckCircleOutlined, ClockCircleOutlined, ExclamationCircleOutlined,
  RocketOutlined, SafetyCertificateOutlined, ProjectOutlined, TeamOutlined,
  ReloadOutlined, FolderOpenOutlined, FileTextOutlined, DownloadOutlined,
  EyeOutlined, WarningOutlined, BugOutlined, ArrowRightOutlined, DashboardOutlined,
} from '@ant-design/icons';
import { useParams, useNavigate } from 'react-router-dom';
import { apiClient } from '../services/api';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;
const { Panel } = Collapse;

const statusConfig: Record<string, { color: string; text: string }> = {
  pending:     { color: 'default',    text: '待开始' },
  planning:    { color: 'processing', text: '规划中' },
  in_progress: { color: 'blue',       text: '执行中' },
  executing:   { color: 'blue',       text: '执行中' },
  qa_testing:  { color: 'orange',     text: '质检中' },
  completed:   { color: 'success',    text: '已完成' },
  failed:      { color: 'error',      text: '失败'   },
};

type AgentDisplayState = 'waiting' | 'executing' | 'completed' | 'failed';

const normalizeAgentState = (value?: string): AgentDisplayState => {
  const status = String(value || '').toLowerCase();
  if (['completed', 'succeeded', 'success', 'done'].includes(status)) return 'completed';
  if (['failed', 'error', 'blocked', 'cancelled', 'canceled', 'timeout'].includes(status)) return 'failed';
  if (['working', 'running', 'in_progress', 'executing', 'fixing', 're_checking'].includes(status)) return 'executing';
  return 'waiting';
};

const agentStateMeta: Record<AgentDisplayState, { color: string; text: string }> = {
  waiting: { color: 'default', text: '等待' },
  executing: { color: 'blue', text: '执行中' },
  completed: { color: 'success', text: '已完成' },
  failed: { color: 'error', text: '失败' },
};

const ProgressBoard: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [project, setProject] = useState<any>(null);
  const [tasks, setTasks] = useState<any[]>([]);
  const [phases, setPhases] = useState<any[]>([]);
  const [supervisorLog, setSupervisorLog] = useState<string>('');
  // 每个 agent 的执行状态（含 output_files）
  const [agentStatuses, setAgentStatuses] = useState<Record<string, any>>({});
  // 文件树（workspace 真实目录）
  const [fileTree, setFileTree] = useState<any[]>([]);
  const [allDoneNotified, setAllDoneNotified] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchData = async (showLoading = false) => {
    if (!projectId) return;
    if (showLoading) setLoading(true);
    try {
      const [projectData, taskData, fileData, phaseData] = await Promise.all([
        apiClient.get(`/projects/${projectId}`),
        apiClient.get(`/projects/${projectId}/tasks/list`).catch(() => ({ tasks: [] })),
        apiClient.get(`/projects/${projectId}/files`).catch(() => ({ tree: [] })),
        apiClient.get(`/projects/${projectId}/phases`).catch(() => ({ phases: [] })),
      ]);
      setProject(projectData);
      setTasks((taskData as any).tasks || []);
      setFileTree((fileData as any).tree || []);
      setPhases((phaseData as any).phases || []);

      // 拉取每个 agent 的执行状态
      const agents: Record<string, any> = (projectData as any).agents || {};
      const statusMap: Record<string, any> = {};
      await Promise.all(
        Object.keys(agents).map(async (agentId) => {
          try {
            const agentStatus = await apiClient.get(`/projects/${projectId}/agents/${agentId}/status`);
            statusMap[agentId] = agentStatus;
          } catch {
            statusMap[agentId] = agents[agentId];
          }
        })
      );
      setAgentStatuses(statusMap);
    } catch { /* 静默 */ }
    finally { if (showLoading) setLoading(false); }
  };

  const fetchSupervisorLog = async () => {
    if (!projectId) return;
    try {
      const res: any = await apiClient.get(`/projects/${projectId}/chat-history/supervisor`);
      const msgs = res.messages || [];
      const last = [...msgs].reverse().find((m: any) => m.role === 'assistant');
      if (last) setSupervisorLog(last.content?.slice(0, 300) || '');
    } catch { /* 静默 */ }
  };

  useEffect(() => {
    fetchData(true);
    fetchSupervisorLog();
  }, [projectId]);

  // 动态轮询：执行期 5s，其他 15s
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    const status = project?.status || 'planning';
    const interval = (status === 'running' || status === 'executing') ? 5000 : 15000;
    intervalRef.current = setInterval(() => {
      fetchData();
      fetchSupervisorLog();
    }, interval);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [projectId, project?.status]);

  const subprojects: any[] = Array.from(
    new Map(
      (project?.subprojects || []).map((task: any) => [String(task.id || ''), task]),
    ).values(),
  );
  const agents: Record<string, any> = project?.agents || {};
  const phaseIds = new Set(
    phases.map(phase => String(phase.phase_id || phase.id || '')),
  );
  // phase-1/phase-2 等记录是阶段容器，只负责分组，不是需要分配专家的任务。
  const workItems = subprojects.filter(task => {
    const taskId = String(task.id || '');
    const phaseId = String(task.phase_id || '');
    return !(taskId === phaseId && phaseIds.has(taskId));
  }).map(task => {
    const execStatus = task.agent_id ? agentStatuses[task.agent_id] : undefined;
    const agent = task.agent_id ? agents[task.agent_id] : undefined;
    const authoritativeStatus = execStatus?.status || agent?.status || task.status;
    const displayState = normalizeAgentState(authoritativeStatus);
    const progress = execStatus?.progress !== undefined
      ? Number(execStatus.progress)
      : agent?.progress !== undefined
        ? Number(agent.progress)
        : Number(task.progress || 0);
    return {
      ...task,
      status: displayState === 'executing' ? 'in_progress' : displayState === 'waiting' ? 'pending' : displayState,
      progress: displayState === 'completed' ? 100 : Math.max(0, Math.min(progress, 99)),
      _agentDisplayState: displayState,
    };
  });

  const total = workItems.length;
  const completed = workItems.filter(s => s.status === 'completed').length;
  const inProgress = workItems.filter(s => ['in_progress', 'executing'].includes(s.status)).length;
  const failed = workItems.filter(s => s.status === 'failed').length;
  const failedDetails = workItems
    .filter(s => s.status === 'failed')
    .map(s => {
      const agentStatus = s.agent_id ? agentStatuses[s.agent_id] : undefined;
      const reason = agentStatus?.error || s.error || agents[s.agent_id]?.error;
      return `${s.name || s.id}: ${reason || '未返回具体错误，请查看执行日志'}`;
    });
  const overallProgress = total > 0
    ? Math.round(workItems.reduce((acc, s) => acc + (s.progress || 0), 0) / total)
    : 0;
  const phaseGroups = (() => {
    const known = phases.map((phase, index) => ({
      id: String(phase.phase_id || phase.id || `phase-${index + 1}`),
      name: phase.name || `阶段 ${index + 1}`,
      status: String(phase.status || 'pending'),
      order: index,
    }));
    const knownIds = new Set(known.map(phase => phase.id));
    const unknownIds = [...new Set(
      workItems.map(task => String(task.phase_id || 'unassigned')),
    )].filter(id => !knownIds.has(id));
    return [
      ...known,
      ...unknownIds.map((id, index) => ({
        id,
        name: id === 'unassigned' ? '未分配阶段' : id,
        status: 'pending',
        order: known.length + index,
      })),
    ].map(phase => {
      const phaseTasks = workItems.filter(
        task => String(task.phase_id || 'unassigned') === phase.id,
      );
      const done = phaseTasks.filter(
        task => task.status === 'completed' || Number(task.progress || 0) >= 100,
      ).length;
      const progress = phaseTasks.length
        ? Math.round(phaseTasks.reduce((sum, task) => sum + Number(task.progress || 0), 0) / phaseTasks.length)
        : 0;
      return { ...phase, tasks: phaseTasks, done, progress };
    }).filter(phase => phase.tasks.length > 0 || phase.id !== 'unassigned');
  })();

  // 子项目全部完成时弹出提示（只弹一次）
  useEffect(() => {
    if (
      total > 0 && completed === total && !allDoneNotified &&
      project?.status === 'running'
    ) {
      setAllDoneNotified(true);
      Modal.confirm({
        title: '🎉 所有子项目已完成！',
        content: '所有 Agent 已完成工作，可以触发四层质检（QA/Perf/Sec/UXO）了。',
        okText: '触发质检 →',
        cancelText: '稍后再说',
        onOk: async () => {
          try {
            await apiClient.post(`/projects/${projectId}/qc/trigger-all`);
            message.success('质检已触发！');
            navigate(`/projects/${projectId}/qa`);
          } catch (e: any) {
            message.error(e.response?.data?.detail || '触发失败');
          }
        },
      });
    }
  }, [completed, total, project?.status]);

  // 质检任务摘要
  const qcTasks = tasks.filter(t =>
    t.title?.startsWith('QA-') || t.title?.startsWith('Perf-') ||
    t.title?.startsWith('Sec-') || t.title?.startsWith('UXO-')
  );
  const qcPassed = qcTasks.filter(t => t.status === 'completed').length;
  const qcFailed = qcTasks.filter(t => t.status === 'failed').length;

  const isDeliverableFile = (path: string) => {
    const normalized = path.replace(/\\/g, '/');
    if (
      normalized.startsWith('.project/') ||
      normalized.startsWith('uploads/') ||
      normalized.endsWith('.log')
    ) return false;
    return /\.(html|css|js|jsx|ts|tsx|py|json|ya?ml|sql|sh|md)$/i.test(normalized);
  };

  // Agent 产出必须来自该 Agent 的执行状态，不能回退成整个项目工作区。
  const getOutputFiles = (agentId: string): string[] => {
    const execStatus = agentStatuses[agentId];
    const fromStatus: string[] = execStatus?.output_files || agents[agentId]?.output_files || [];
    return [...new Set(fromStatus.filter(isDeliverableFile))];
  };

  const columns = [
    {
      title: 'ID', dataIndex: 'id', key: 'id', width: 100,
      render: (id: string) => <Tag className="font-mono text-xs">{id}</Tag>,
    },
    {
      title: '子项目', dataIndex: 'name', key: 'name',
      render: (name: string, record: any) => (
        <div>
          <div className="font-medium">{name}</div>
          <div className="text-xs text-gray-400 truncate max-w-xs">{record.description}</div>
        </div>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 100,
      render: (status: string, record: any) => {
        const state = (record._agentDisplayState || normalizeAgentState(status)) as AgentDisplayState;
        const meta = agentStateMeta[state];
        const cfg = { color: meta.color, text: meta.text };
        return <Tag color={cfg.color}>{cfg.text}</Tag>;
      },
    },
    {
      title: '进度', dataIndex: 'progress', key: 'progress', width: 160,
      render: (p: number) => (
        <Progress percent={p || 0} size="small" strokeColor={(p || 0) === 100 ? '#52c41a' : '#1890ff'} />
      ),
    },
    {
      title: '负责 Agent', dataIndex: 'agent_id', key: 'agent_id', width: 140,
      render: (agentId: string) => {
        const agent = agents[agentId];
        if (!agent) return <span className="text-gray-400 text-xs">未分配</span>;
        return (
          <div>
            <div className="text-xs font-medium">{agent.role}</div>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <DashboardOutlined style={{ color: '#5b5ea6', fontSize: 18 }} />
          {project?.name || '项目'}
          <span style={{ fontSize: 12, fontWeight: 400, color: '#8c8c8c', marginLeft: 4 }}>进度看板</span>
        </h2>
        <div className="flex items-center gap-2">
          {project?.workspace && (
            <Tooltip title={`本地路径：${project.workspace}`}>
              <Tag icon={<FolderOpenOutlined />} color="blue" className="cursor-default text-xs">
                {project.workspace_rel}
              </Tag>
            </Tooltip>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => fetchData(true)} loading={loading} size="small">刷新</Button>
        </div>
      </div>

      <Spin spinning={loading}>
        {/* 统计卡片 */}
        <Row gutter={12} className="mb-4">
          <Col span={5}><Card size="small"><Statistic title="子项目总数" value={total} prefix={<ProjectOutlined className="text-blue-500" />} /></Card></Col>
          <Col span={5}><Card size="small"><Statistic title="已完成" value={completed} valueStyle={{ color: '#52c41a' }} prefix={<CheckCircleOutlined />} /></Card></Col>
          <Col span={5}><Card size="small"><Statistic title="执行中" value={inProgress} valueStyle={{ color: '#1890ff' }} prefix={<RocketOutlined />} /></Card></Col>
          <Col span={4}><Card size="small"><Statistic title="失败" value={failed} valueStyle={{ color: failed > 0 ? '#ff4d4f' : '#52c41a' }} prefix={<ExclamationCircleOutlined />} /></Card></Col>
          <Col span={5}><Card size="small"><Statistic title="整体进度" value={overallProgress} suffix="%" prefix={<TeamOutlined className="text-purple-500" />} /></Card></Col>
        </Row>

        {/* 整体进度条 */}
        <Card size="small" className="mb-4">
          <div className="flex items-center gap-3">
            <span className="text-sm text-gray-500 whitespace-nowrap">整体进度</span>
            <Progress
              percent={overallProgress}
              strokeColor={{ '0%': '#108ee9', '100%': '#87d068' }}
              format={p => `${p ?? 0}%`}
              className="flex-1"
            />
          </div>
        </Card>

        {/* 全部完成提示 */}
        {total > 0 && completed === total && project?.status === 'running' && (
          <Alert
            type="success"
            showIcon
            icon={<CheckCircleOutlined />}
            message="🎉 所有子项目已完成！"
            description="可以点击顶部「触发质检 →」进入四层质检阶段，或点击下方按钮。"
            className="mb-4"
            action={
              <Button
                type="primary"
                size="small"
                icon={<ArrowRightOutlined />}
                onClick={async () => {
                  try {
                    await apiClient.post(`/projects/${projectId}/qc/trigger-all`);
                    message.success('质检已触发！');
                    navigate(`/projects/${projectId}/qa`);
                  } catch (e: any) {
                    message.error(e.response?.data?.detail || '触发失败');
                  }
                }}
              >
                触发质检
              </Button>
            }
          />
        )}

        {/* Supervisor 监督状态 */}
        {supervisorLog && (
          <Alert
            type="info"
            icon={<EyeOutlined />}
            showIcon
            message={<span className="font-medium text-sm"><SafetyCertificateOutlined style={{ marginRight: 6 }} />Supervisor 最新监督反馈</span>}
            description={<span className="text-xs text-gray-600">{supervisorLog}...</span>}
            className="mb-4"
            action={<Button size="small" onClick={fetchSupervisorLog}>刷新</Button>}
          />
        )}

        {/* 阻塞预警 */}
        {failed > 0 && (
          <Alert
            type="error"
            icon={<WarningOutlined />}
            showIcon
            message={<><WarningOutlined style={{ marginRight: 6 }} />有 {failed} 个子项目执行失败</>}
            description={
              <List
                size="small"
                dataSource={failedDetails}
                renderItem={(detail: string) => <List.Item>{detail}</List.Item>}
              />
            }
            className="mb-4"
          />
        )}

        {/* 质检摘要 */}
        {qcTasks.length > 0 && (
          <Card
            size="small"
            title={<span className="text-sm">🔍 质检摘要（{qcPassed}/{qcTasks.length} 通过）</span>}
            className="mb-4"
            extra={<Tag color={qcFailed > 0 ? 'error' : 'success'}>{qcFailed > 0 ? `${qcFailed} 项未通过` : '全部通过'}</Tag>}
          >
            <div className="flex flex-wrap gap-2">
              {qcTasks.map(t => (
                <Tag
                  key={t.id}
                  color={t.status === 'completed' ? 'success' : t.status === 'failed' ? 'error' : 'processing'}
                  icon={t.status === 'completed' ? <CheckCircleOutlined /> : t.status === 'failed' ? <BugOutlined /> : <ClockCircleOutlined />}
                >
                  {t.title?.replace(/^(QA|Perf|Sec|UXO)-/, '')}
                </Tag>
              ))}
            </div>
          </Card>
        )}

        {/* 子项目详情（展开查看产出文件） */}
        <Card
          title={<span className="text-sm">子项目详情（展开查看产出文件目录）</span>}
          className="mb-4"
          size="small"
        >
          {subprojects.length === 0 ? (
            <Empty description="暂无子项目，请先与 PM Agent 完成规划并点击「启动项目 →」" />
          ) : (
            <Collapse
              key={phaseGroups.map(phase => phase.id).join('|')}
              defaultActiveKey={phaseGroups
                .filter(phase => phase.status !== 'completed')
                .map(phase => phase.id)}
              size="small"
            >
              {phaseGroups.map(phase => (
                <Panel
                  key={phase.id}
                  header={(
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                      <strong style={{ minWidth: 220 }}>{phase.name}</strong>
                      <Tag color={phase.status === 'completed' ? 'success' : 'processing'}>
                        {phase.done}/{phase.tasks.length} 已完成
                      </Tag>
                      <Progress
                        percent={phase.progress}
                        size="small"
                        style={{ maxWidth: 260, margin: 0 }}
                      />
                    </div>
                  )}
                >
                  <Table
                    dataSource={phase.tasks}
                    columns={columns}
                    rowKey="id"
                    pagination={false}
                    size="small"
                    expandable={{
                expandedRowRender: (record: any) => {
                  const agent = agents[record.agent_id];
                  const execStatus = agentStatuses[record.agent_id];
                  const state = normalizeAgentState(execStatus?.status || agent?.status || record.status);
                  const meta = agentStateMeta[state];
                  const outputFiles = getOutputFiles(record.agent_id);
                  const logs: string[] = execStatus?.logs || [];
                  const summary: string = execStatus?.summary || '';
                  return (
                    <div className="p-3 bg-gray-50 rounded space-y-2">
                      {/* Agent 信息 */}
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">负责 Agent：{agent?.role || '未分配'}</span>
                        {(execStatus?.status || agent?.status) && (
                          <Tag color={meta.color}>
                            {meta.text}
                          </Tag>
                        )}
                        {execStatus?.progress !== undefined && execStatus.progress > 0 ? (
                          <span className="text-xs text-gray-400">{execStatus.progress}%</span>
                        ) : execStatus?.match_score !== undefined ? (
                          <Progress percent={Math.min(execStatus.match_score * 5, 100)} size="small" style={{ width: 80 }}
                            format={() => `${Math.min(execStatus.match_score * 5, 100)}%`} />
                        ) : (
                          <span className="text-xs text-gray-300">等待执行</span>
                        )}
                      </div>

                      {/* 执行摘要 */}
                      {summary && (
                        <div className="text-xs text-gray-600 bg-white p-2 rounded border">
                          📝 {summary}
                        </div>
                      )}

                      {/* 产出文件目录 */}
                      <div>
                        <div className="text-xs font-medium text-gray-500 mb-1">
                          📁 产出文件目录（{outputFiles.length} 个文件）
                        </div>
                        {outputFiles.length > 0 ? (
                          <List
                            size="small"
                            dataSource={outputFiles}
                            renderItem={(file: string) => (
                              <List.Item
                                className="py-1"
                                actions={[
                                  <a
                                    key="dl"
                                    href={`${API}/projects/${projectId}/files/download?path=${encodeURIComponent(file)}`}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-xs"
                                  >
                                    <DownloadOutlined /> 下载
                                  </a>
                                ]}
                              >
                                <FileTextOutlined className="text-blue-400 mr-2" />
                                <span className="text-xs font-mono">{file}</span>
                              </List.Item>
                            )}
                          />
                        ) : (
                          <div className="text-xs text-gray-400 bg-white p-2 rounded border">
                            {execStatus?.status === 'working'
                              ? '⏳ Agent 正在生成代码；文件会在 LLM 返回并落盘后显示。若已写入 workspace，会自动从真实文件树补充展示。'
                              : execStatus?.status === 'failed'
                              ? `❌ 执行失败：${execStatus?.error || '未知错误'}`
                              : '暂无产出文件。点击顶部「全部执行 →」或在 Agent 团队页面点「开始工作」'}
                          </div>
                        )}
                      </div>

                      {/* 执行日志（折叠） */}
                      {logs.length > 0 && (
                        <Collapse size="small" ghost>
                          <Panel header={<span className="text-xs text-gray-400">执行日志（{logs.length} 条）</span>} key="logs">
                            <div className="bg-black text-green-400 p-2 rounded text-xs font-mono max-h-40 overflow-auto">
                              {logs.map((log, i) => <div key={i}>{log}</div>)}
                            </div>
                          </Panel>
                        </Collapse>
                      )}
                    </div>
                  );
                },
                    }}
                  />
                </Panel>
              ))}
            </Collapse>
          )}
        </Card>

        {/* workspace 文件树（完整目录结构） */}
        {fileTree.length > 0 && (
          <Collapse size="small" ghost>
            <Panel
              header={
                <span className="text-sm">
                  📂 项目工作区文件树（完整目录）
                  <Tag className="ml-2" color="blue">{project?.workspace_rel}</Tag>
                </span>
              }
              key="filetree"
            >
              <div className="space-y-1">
                {fileTree.map(node => (
                  <div key={node.key}>
                    <div className="flex items-center gap-1 py-0.5">
                      {node.type === 'file' ? (
                        <FileTextOutlined className="text-gray-400 text-xs" />
                      ) : (
                        <FolderOpenOutlined className="text-yellow-500" />
                      )}
                      <span className="text-sm font-medium">{node.type === 'file' ? node.title : `${node.title}/`}</span>
                      {node.type === 'file' ? (
                        <Tag color="purple" className="text-xs">根目录文件</Tag>
                      ) : (
                        <Tag color={node.title === 'output' ? 'green' : node.title === 'src' ? 'blue' : 'default'} className="text-xs">
                          {node.title === 'output' ? '✅ 合格产出' : node.title === 'src' ? '代码' : node.title === 'docs' ? '文档' : node.title === 'tests' ? '测试' : node.title}
                        </Tag>
                      )}
                      <span className="text-xs text-gray-400">
                        {node.type === 'file' && node.size ? `${(node.size / 1024).toFixed(1)}KB` : `(${(node.children || []).length} 项)`}
                      </span>
                    </div>
                    {node.type !== 'file' && (node.children || []).slice(0, 5).map((child: any) => (
                      <div key={child.key} className="ml-6 flex items-center gap-1 py-0.5">
                        <FileTextOutlined className="text-gray-400 text-xs" />
                        <span className="text-xs font-mono text-gray-600">{child.title}</span>
                        {child.size && <span className="text-xs text-gray-300">{(child.size / 1024).toFixed(1)}KB</span>}
                      </div>
                    ))}
                    {node.type !== 'file' && (node.children || []).length > 5 && (
                      <div className="ml-6 text-xs text-gray-400">...还有 {node.children.length - 5} 个文件</div>
                    )}
                  </div>
                ))}
              </div>
            </Panel>
          </Collapse>
        )}
      </Spin>
    </div>
  );
};

export default ProgressBoard;
