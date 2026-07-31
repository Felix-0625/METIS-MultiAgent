/**
 * 项目实时进度面板
 * 通过 SSE 订阅后端 /projects/{id}/progress/stream
 * 每 2 秒自动刷新，展示：
 *   - 每个执行 Agent 的状态、进度、生成文件、修复任务
 *   - 每个子项目的状态
 *   - 质检摘要（通过/失败/评分）
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  Card, Tag, Progress, Collapse, Badge, Tooltip, Button, Typography, Space, Alert,
} from 'antd';
import {
  CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined,
  WarningOutlined, FileTextOutlined, ReloadOutlined, BugOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import { API_BASE_URL } from '../services/apiBase';

const { Text, Paragraph } = Typography;
const { Panel } = Collapse;
const API = API_BASE_URL;

interface AgentSnapshot {
  id: string;
  role: string;
  subproject_id: string;
  subproject_name: string;
  status: string;
  progress: number;
  output_files: string[];
  fix_required: boolean;
  fix_task: string;
  needs_rewrite: boolean;
  last_log: string;
}

interface SubprojectSnapshot {
  id: string;
  name: string;
  status: string;
  progress: number;
  agent_id: string;
}

interface QCSummaryItem {
  passed: boolean;
  score: number;
  status: string;
  error_count: number;
  warning_count: number;
  responsible_agent_id: string;
  needs_rewrite: boolean;
}

interface ProgressData {
  project_id: string;
  project_status: string;
  agents: AgentSnapshot[];
  subprojects: SubprojectSnapshot[];
  qc_summary: Record<string, QCSummaryItem>;
  timestamp: number;
}

interface Props {
  projectId: string;
  onQCReport?: (spId: string) => void;
}

const STATUS_COLOR: Record<string, string> = {
  idle: 'default',
  working: 'processing',
  completed: 'success',
  failed: 'error',
  fix_required: 'warning',
  in_progress: 'processing',
  pending: 'default',
  passed: 'success',
};

const STATUS_LABEL: Record<string, string> = {
  idle: '空闲',
  working: '开发中',
  completed: '已完成',
  failed: '失败',
  fix_required: '待修复',
  in_progress: '进行中',
  pending: '待开始',
  passed: '通过',
};

const ProjectProgressPanel: React.FC<Props> = ({ projectId, onQCReport }) => {
  const [data, setData] = useState<ProgressData | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const esRef = useRef<EventSource | null>(null);

  const connect = () => {
    if (esRef.current) {
      esRef.current.close();
    }
    setError('');
    const es = new EventSource(`${API}/projects/${projectId}/progress/stream`);
    esRef.current = es;

    es.onopen = () => setConnected(true);

    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as ProgressData & { error?: string };
        if (!parsed.error) {
          setData(parsed);
        }
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      setConnected(false);
      setError('连接中断，将在 5 秒后重连...');
      es.close();
      setTimeout(connect, 5000);
    };
  };

  useEffect(() => {
    if (!projectId) return;
    connect();
    return () => {
      esRef.current?.close();
    };
  }, [projectId]);

  if (!data) {
    return (
      <Card size="small" title="实时进度" style={{ marginBottom: 12 }}>
        <div style={{ textAlign: 'center', padding: '20px 0', color: '#999' }}>
          <LoadingOutlined style={{ marginRight: 8 }} />
          {connected ? '等待数据...' : '连接中...'}
        </div>
      </Card>
    );
  }

  const { agents, subprojects, qc_summary, project_status } = data;

  // 统计
  const totalAgents = agents.length;
  const doneAgents = agents.filter(a => a.status === 'completed').length;
  const fixAgents = agents.filter(a => a.fix_required).length;
  const qcEntries = Object.entries(qc_summary);
  const qcPassed = qcEntries.filter(([, v]) => v.passed).length;

  return (
    <div style={{ fontSize: 13 }}>
      {/* 连接状态 */}
      {error && (
        <Alert
          type="warning"
          message={error}
          style={{ marginBottom: 8 }}
          action={<Button size="small" onClick={connect}>立即重连</Button>}
        />
      )}

      {/* 总览 */}
      <Card
        size="small"
        title={
          <Space>
            <span>项目进度</span>
            <Tag color={STATUS_COLOR[project_status] || 'default'}>
              {project_status}
            </Tag>
            {connected && <Badge status="processing" text="实时" />}
          </Space>
        }
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={connect}>
            重连
          </Button>
        }
        style={{ marginBottom: 8 }}
      >
        <Space wrap>
          <Text type="secondary">Agent：</Text>
          <Text strong>{doneAgents}/{totalAgents} 完成</Text>
          {fixAgents > 0 && (
            <Tag color="warning" icon={<WarningOutlined />}>
              {fixAgents} 个待修复
            </Tag>
          )}
          {qcEntries.length > 0 && (
            <Text type="secondary">
              质检：{qcPassed}/{qcEntries.length} 通过
            </Text>
          )}
        </Space>
      </Card>

      {/* 执行 Agent 列表 */}
      {agents.length > 0 && (
        <Card size="small" title="执行 Agent 状态" style={{ marginBottom: 8 }}>
          {agents.map(agent => (
            <div
              key={agent.id}
              style={{
                padding: '8px 0',
                borderBottom: '1px solid #f0f0f0',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <Space>
                  <Text strong>{agent.role}</Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    → {agent.subproject_name || agent.subproject_id}
                  </Text>
                </Space>
                <Tag color={STATUS_COLOR[agent.status] || 'default'}>
                  {STATUS_LABEL[agent.status] || agent.status}
                </Tag>
              </div>

              {/* 进度条（开发中时显示） */}
              {agent.status === 'working' && (
                <Progress
                  percent={agent.progress}
                  size="small"
                  status="active"
                  style={{ marginTop: 4 }}
                />
              )}

              {/* 最新日志 */}
              {agent.last_log && (
                <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
                  {agent.last_log}
                </Text>
              )}

              {/* 生成的文件 */}
              {agent.output_files.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  {agent.output_files.slice(0, 3).map(f => (
                    <Tag key={f} icon={<FileTextOutlined />} style={{ fontSize: 11, marginBottom: 2 }}>
                      {f.split('/').pop()}
                    </Tag>
                  ))}
                  {agent.output_files.length > 3 && (
                    <Tag style={{ fontSize: 11 }}>+{agent.output_files.length - 3} 个文件</Tag>
                  )}
                </div>
              )}

              {/* 修复任务提示 */}
              {agent.fix_required && (
                <Alert
                  type={agent.needs_rewrite ? 'error' : 'warning'}
                  icon={agent.needs_rewrite ? <BugOutlined /> : <ToolOutlined />}
                  message={
                    <span style={{ fontSize: 11 }}>
                      {agent.needs_rewrite
                        ? '⚠️ 问题较多，建议重新生成代码'
                        : '需要修复质检问题'}
                    </span>
                  }
                  style={{ marginTop: 4, padding: '2px 8px' }}
                  showIcon
                />
              )}
            </div>
          ))}
        </Card>
      )}

      {/* 子项目状态 */}
      {subprojects.length > 0 && (
        <Card size="small" title="子项目状态" style={{ marginBottom: 8 }}>
          {subprojects.map(sp => (
            <div
              key={sp.id}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                padding: '4px 0',
                borderBottom: '1px solid #f0f0f0',
              }}
            >
              <Text style={{ fontSize: 12 }}>{sp.name}</Text>
              <Space>
                {sp.status === 'in_progress' && (
                  <Progress
                    percent={sp.progress}
                    size="small"
                    style={{ width: 80 }}
                    showInfo={false}
                  />
                )}
                <Tag color={STATUS_COLOR[sp.status] || 'default'} style={{ fontSize: 11 }}>
                  {STATUS_LABEL[sp.status] || sp.status}
                </Tag>
              </Space>
            </div>
          ))}
        </Card>
      )}

      {/* 质检摘要 */}
      {qcEntries.length > 0 && (
        <Card size="small" title="质检摘要">
          {qcEntries.map(([spId, qc]) => {
            const sp = subprojects.find(s => s.id === spId);
            return (
              <div
                key={spId}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '4px 0',
                  borderBottom: '1px solid #f0f0f0',
                }}
              >
                <Space>
                  {qc.passed
                    ? <CheckCircleOutlined style={{ color: '#52c41a' }} />
                    : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
                  }
                  <Text style={{ fontSize: 12 }}>{sp?.name || spId}</Text>
                </Space>
                <Space>
                  <Text style={{ fontSize: 11, color: qc.score >= 80 ? '#52c41a' : qc.score >= 60 ? '#faad14' : '#ff4d4f' }}>
                    {qc.score}分
                  </Text>
                  {qc.error_count > 0 && (
                    <Tag color="error" style={{ fontSize: 11 }}>{qc.error_count} 错误</Tag>
                  )}
                  {qc.warning_count > 0 && (
                    <Tag color="warning" style={{ fontSize: 11 }}>{qc.warning_count} 警告</Tag>
                  )}
                  {onQCReport && (
                    <Button
                      size="small"
                      type="link"
                      style={{ fontSize: 11, padding: 0 }}
                      onClick={() => onQCReport(spId)}
                    >
                      详情
                    </Button>
                  )}
                </Space>
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
};

export default ProjectProgressPanel;
