/**
 * 项目 Agent 团队页面
 * 展示当前项目内的所有 Agent，以及每个子项目的唯一负责人
 * 无小组制：Agent 直接挂在项目下，每个子项目有且只有一个负责人
 */

import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Card, Table, Tag, Button, Badge, Space, Modal, Form,
  Input, Select, message, Tooltip, Empty, Divider, Row, Col, Statistic, Progress,
} from 'antd';
import {
  PlusOutlined, RobotOutlined, UserOutlined,
  LinkOutlined, DisconnectOutlined, ReloadOutlined,
  PlayCircleOutlined, ThunderboltOutlined,
  ArrowRightOutlined, InfoCircleOutlined, CheckCircleOutlined,
} from '@ant-design/icons';
import { Alert } from 'antd';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;

interface AgentInfo {
  id: string;
  role: string;
  skill_names: string[];
  subproject_id?: string;
  phase_id?: string;
  status: string;
  progress?: number;
  error?: string;
  message?: string;
  created_at: number;
  project_id: string;
}

interface SubprojectInfo {
  id: string;
  name: string;
  description: string;
  agent_id?: string;
  agent?: AgentInfo;
  status: string;
  progress: number;
}

const statusConfig: Record<string, { color: string; text: string; badge: 'success' | 'processing' | 'error' | 'default' }> = {
  idle:              { color: 'default', text: '待机',       badge: 'default' },
  queued:            { color: 'blue',    text: '排队中',     badge: 'processing' },
  working:           { color: 'blue',    text: '执行中',     badge: 'processing' },
  running:           { color: 'blue',    text: '执行中',     badge: 'processing' },
  in_progress:       { color: 'blue',    text: '执行中',     badge: 'processing' },
  fixing:            { color: 'orange',  text: '修复中',     badge: 'processing' },
  re_checking:       { color: 'purple',  text: '复检中',     badge: 'processing' },
  fix_required:      { color: 'orange',  text: '待修复',     badge: 'error' },
  completed:         { color: 'green',   text: '已完成',     badge: 'success' },
  failed:            { color: 'red',     text: '执行失败',   badge: 'error' },
  error:             { color: 'red',     text: '执行异常',   badge: 'error' },
  fix_limit_reached: { color: 'red',     text: '需人工处理', badge: 'error' },
  blocked:           { color: 'red',     text: '阻塞',       badge: 'error' },
  cancelled:         { color: 'default', text: '已取消',     badge: 'default' },
};

const ACTIVE_AGENT_STATUSES = new Set([
  'queued', 'working', 'running', 'in_progress', 'fixing', 're_checking',
]);

const ProjectAgents: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [subprojects, setSubprojects] = useState<SubprojectInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [createModal, setCreateModal] = useState(false);
  const [assignModal, setAssignModal] = useState<{ open: boolean; subprojectId: string }>({ open: false, subprojectId: '' });
  const [executingAgents, setExecutingAgents] = useState<Set<string>>(new Set());
  const [executingAll, setExecutingAll] = useState(false);
  const [project, setProject] = useState<any>(null);
  const [fetchError, setFetchError] = useState('');
  const [form] = Form.useForm();

  const fetchData = async (showLoading = true) => {
    if (!projectId) return;
    if (showLoading) setLoading(true);
    try {
      const [agentsRes, spRes, projectRes] = await Promise.all([
        axios.get(`${API}/projects/${projectId}/agents`),
        axios.get(`${API}/projects/${projectId}/subprojects/list`),
        axios.get(`${API}/projects/${projectId}`),
      ]);
      setAgents(agentsRes.data.agents || []);
      setSubprojects(spRes.data.subprojects || []);
      setProject(projectRes.data);
      setFetchError('');
    } catch (e: any) {
      setFetchError(e.response?.data?.detail || e.message || 'Agent 状态加载失败');
    } finally {
      if (showLoading) setLoading(false);
    }
  };

  useEffect(() => {
    void fetchData(true);
  }, [projectId]);

  const hasActiveAgents = agents.some(agent => ACTIVE_AGENT_STATUSES.has(agent.status));
  const hasPhaseOwnedAgents = agents.some(agent => Boolean(agent.phase_id));

  useEffect(() => {
    if (!projectId) return;
    const interval = window.setInterval(
      () => void fetchData(false),
      hasActiveAgents ? 4000 : 15000,
    );
    return () => window.clearInterval(interval);
  }, [projectId, hasActiveAgents]);

  // 创建新 Agent
  const handleCreate = async (values: any) => {
    try {
      const res = await axios.post(`${API}/projects/${projectId}/agents`, {
        role: values.role,
        skills: values.skills || [],
        subproject_id: values.subproject_id,
      });
      // 如果指定了子项目，同步更新子项目的负责人
      if (values.subproject_id) {
        const sp = subprojects.find(s => s.id === values.subproject_id);
        if (sp) {
          await axios.post(`${API}/projects/${projectId}/subprojects/assign`, {
            id: sp.id,
            name: sp.name,
            description: sp.description,
            agent_id: res.data.agent_id,
          });
        }
      }
      message.success('Agent 创建成功');
      setCreateModal(false);
      form.resetFields();
      fetchData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败');
    }
  };

  // 分配负责人
  const handleAssign = async (subprojectId: string, agentId: string) => {
    const sp = subprojects.find(s => s.id === subprojectId);
    if (!sp) return;
    try {
      await axios.post(`${API}/projects/${projectId}/subprojects/assign`, {
        id: subprojectId,
        name: sp.name,
        description: sp.description,
        agent_id: agentId || null,
      });
      message.success('负责人已更新');
      setAssignModal({ open: false, subprojectId: '' });
      fetchData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '分配失败');
    }
  };

  // 触发单个 Agent 开始工作
  const handleExecuteAgent = async (agentId: string) => {
    setExecutingAgents(prev => new Set(prev).add(agentId));
    try {
      const res = await axios.post(
        `${API}/projects/${projectId}/agents/${agentId}/execute`,
        null,
        { headers: { 'Idempotency-Key': crypto.randomUUID() } },
      );
      message.success(`${res.data.subproject || 'Agent'} 已开始工作，代码将写入项目文件夹`);
      setTimeout(fetchData, 2000); // 2秒后刷新状态
    } catch (e: any) {
      message.error(e.response?.data?.detail || '触发失败');
    } finally {
      setExecutingAgents(prev => { const s = new Set(prev); s.delete(agentId); return s; });
    }
  };

  // 触发所有 Agent 开始工作
  const handleExecuteAll = async () => {
    setExecutingAll(true);
    try {
      const res = await axios.post(`${API}/projects/${projectId}/execute-all`);
      message.success(res.data.message || '所有 Agent 已开始工作');
      setTimeout(fetchData, 2000);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '触发失败');
    } finally {
      setExecutingAll(false);
    }
  };

  // 统计
  const stats = {
    total: agents.length,
    idle: agents.filter(a => a.status === 'idle').length,
    working: agents.filter(a => ACTIVE_AGENT_STATUSES.has(a.status)).length,
    assigned: subprojects.filter(s => s.agent_id).length,
  };

  // Agent 列表列
  const agentColumns = [
    {
      title: 'Agent ID',
      dataIndex: 'id',
      key: 'id',
      width: 140,
      render: (id: string) => <Tag className="font-mono text-xs">{id}</Tag>,
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      render: (role: string) => (
        <Space>
          <RobotOutlined className="text-blue-500" />
          <span className="font-medium">{role}</span>
        </Space>
      ),
    },
    {
      title: '已授权 Skill',
      dataIndex: 'skill_names',
      key: 'skill_names',
      render: (skills: string[]) => (
        <Space wrap size={4}>
          {skills.length > 0
            ? skills.map(s => <Tag key={s} color="blue" className="text-xs">{s}</Tag>)
            : <span className="text-gray-400 text-xs">暂无</span>}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (status: string) => {
        const cfg = statusConfig[status] || { badge: 'default' as const, text: status };
        return <Badge status={cfg.badge} text={cfg.text || '未知状态'} />;
      },
    },
    {
      title: '负责子项目',
      key: 'subproject',
      render: (_: any, record: AgentInfo) => {
        const sp = subprojects.find(s => s.agent_id === record.id);
        return sp
          ? <Tag color="purple">{sp.name}</Tag>
          : <span className="text-gray-400 text-xs">未分配</span>;
      },
    },
    {
      title: '进度',
      key: 'progress',
      width: 140,
      render: (_: any, record: AgentInfo) => {
        const sp = subprojects.find(s => s.agent_id === record.id);
        const prog = sp?.progress || 0;
        const status = record.status;
        return (
          <div className="flex flex-col gap-0.5">
            <Progress
              percent={prog}
              size="small"
              strokeColor={prog === 100 ? '#52c41a' : ACTIVE_AGENT_STATUSES.has(status) ? '#1890ff' : '#d9d9d9'}
              showInfo={false}
            />
            <span className="text-xs text-gray-400">
              {prog === 100 ? '✅ 完成' : ACTIVE_AGENT_STATUSES.has(status) ? `⏳ ${prog}%` : `${prog}%`}
            </span>
          </div>
        );
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      render: (_: any, record: AgentInfo) => (
        <Tooltip title={record.phase_id ? '阶段 Agent 由阶段看板的执行协调器统一调度' : '调用 LLM 生成代码，写入项目文件夹'}>
          <Button
            size="small"
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={executingAgents.has(record.id)}
            disabled={Boolean(record.phase_id)}
            onClick={() => handleExecuteAgent(record.id)}
            style={{ backgroundColor: '#52c41a', borderColor: '#52c41a' }}
          >
            开始工作
          </Button>
        </Tooltip>
      ),
    },
  ];

  // 子项目列表列
  const spColumns = [
    {
      title: '子项目',
      key: 'name',
      render: (_: any, record: SubprojectInfo) => (
        <div>
          <div className="font-medium">{record.name}</div>
          <div className="text-xs text-gray-400">{record.description}</div>
        </div>
      ),
    },
    {
      title: '负责人 Agent',
      key: 'agent',
      width: 220,
      render: (_: any, record: SubprojectInfo) => {
        const agent = agents.find(a => a.id === record.agent_id);
        if (!agent) {
          return (
            <Button
              size="small"
              type="dashed"
              icon={<LinkOutlined />}
              onClick={() => setAssignModal({ open: true, subprojectId: record.id })}
            >
              分配负责人
            </Button>
          );
        }
        return (
          <Space>
            <Tag color="blue" icon={<RobotOutlined />}>{agent.role}</Tag>
            <Tooltip title="更换负责人">
              <Button
                size="small"
                type="text"
                icon={<DisconnectOutlined />}
                onClick={() => setAssignModal({ open: true, subprojectId: record.id })}
              />
            </Tooltip>
          </Space>
        );
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (status: string) => {
        const map: Record<string, string> = {
          pending: 'default', executing: 'blue', completed: 'success', failed: 'error',
        };
        const textMap: Record<string, string> = {
          pending: '待开始', executing: '执行中', completed: '已完成', failed: '失败',
        };
        return <Tag color={map[status] || 'default'}>{textMap[status] || status}</Tag>;
      },
    },
    {
      title: '进度',
      dataIndex: 'progress',
      key: 'progress',
      width: 80,
      render: (p: number) => <span className="text-sm font-medium">{p}%</span>,
    },
  ];

  // 阶段提示配置
  const phaseHints: Record<string, { type: 'info' | 'warning' | 'success'; msg: string; next: string }> = {
    planning:     { type: 'info',    msg: '当前处于【规划期】：请先在「PM 组长」页面完成需求讨论并确认总规划，系统随后生成阶段看板。', next: '下一步：确认总规划 → 进入阶段看板' },
    team_building:{ type: 'warning', msg: '当前处于【组建期】：请在此页面创建 Agent 并分配子项目负责人，完成后点击「全部执行」或顶部「全部执行 →」进入执行期。', next: '下一步：创建 Agent → 分配子项目 → 点击「全部执行」' },
    running:      { type: 'info',    msg: '当前处于【执行期】：Agent 正在执行各子项目。请在阶段看板确认所有 Agent 均为“已完成”，再启动质检循环。', next: '下一步：阶段看板 → 核对 Agent 终态 → 启动质检循环' },
    executing:    { type: 'info',    msg: '当前处于【执行期】：Agent 正在执行各子项目。请在阶段看板确认所有 Agent 均为“已完成”，再启动质检循环。', next: '下一步：阶段看板 → 核对 Agent 终态 → 启动质检循环' },
    qa:           { type: 'warning', msg: '当前处于【质检期】：请先确认所有 Agent 执行成功，再核对质检报告与最终整体质检。当前前端不会提供绕过验收门禁的签核操作。', next: '下一步：核对 Agent 终态 → 修复问题 → 在文件管理运行最终整体质检' },
    completed:    { type: 'success', msg: '后端已将项目标记为完成。请仍以最终整体质检和运行验收证据为准；该状态不代表文件已自动归档。', next: '请核对最终质检证据后下载项目归档' },
  };
  const status = project?.status || 'planning';
  const hint = project ? (phaseHints[status] || phaseHints['planning']) : null;

  return (
    <div className="space-y-4">
      {fetchError && (
        <Alert
          type="error"
          showIcon
          message="Agent 状态加载失败，当前页面不会使用演示数据"
          description={fetchError}
          action={<Button size="small" onClick={() => void fetchData(true)}>重试</Button>}
        />
      )}
      {/* 当前阶段提示 + 下一步任务 */}
      {hint && <Alert
        type={hint.type}
        showIcon
        icon={hint.type === 'success' ? <CheckCircleOutlined /> : <InfoCircleOutlined />}
        message={<span className="font-medium text-sm">{hint.msg}</span>}
        description={
          hint.type !== 'success' && (
            <span className="text-xs text-gray-500 flex items-center gap-1">
              <ArrowRightOutlined /> {hint.next}
            </span>
          )
        }
      />}

      {/* 统计 */}
      <Row gutter={16}>
        <Col span={6}>
          <Card size="small">
            <Statistic title="项目 Agent 总数" value={stats.total} prefix={<RobotOutlined />} valueStyle={{ color: '#1890ff' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="空闲" value={stats.idle} valueStyle={{ color: '#52c41a' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="执行中" value={stats.working} valueStyle={{ color: '#1890ff' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="已分配子项目" value={stats.assigned} suffix={`/ ${subprojects.length}`} />
          </Card>
        </Col>
      </Row>

      {/* Agent 列表 */}
      <Card
        title={<Space><RobotOutlined /> 项目 Agent（本项目专属，与其他项目隔离）</Space>}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void fetchData(true)} size="small">刷新</Button>
            {agents.length > 0 && (
              <Button
                icon={<ThunderboltOutlined />}
                loading={executingAll}
                disabled={hasPhaseOwnedAgents}
                onClick={handleExecuteAll}
                style={{ backgroundColor: '#fa8c16', borderColor: '#fa8c16', color: '#fff' }}
              >
                全部执行
              </Button>
            )}
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModal(true)}>
              创建 Agent
            </Button>
          </Space>
        }
      >
        {agents.length === 0 ? (
          <Empty description="暂无 Agent，请先通过 PM 对话生成规划书，再由 HR 创建团队" />
        ) : (
          <Table
            columns={agentColumns}
            dataSource={agents}
            rowKey="id"
            loading={loading}
            pagination={false}
            size="small"
          />
        )}
      </Card>

      {/* 子项目负责人分配 */}
      <Card title="子项目负责人分配（每个子项目唯一负责人）">
        {subprojects.length === 0 ? (
          <Empty description="暂无子项目，请先在 PM 对话中完成需求规划" />
        ) : (
          <Table
            columns={spColumns}
            dataSource={subprojects}
            rowKey="id"
            pagination={false}
            size="small"
          />
        )}
      </Card>

      {/* 创建 Agent 弹窗 */}
      <Modal
        title="创建新 Agent"
        open={createModal}
        onCancel={() => { setCreateModal(false); form.resetFields(); }}
        footer={null}
        width={480}
      >
        <div className="text-xs text-gray-400 mb-4">
          新 Agent 仅属于本项目，与其他项目的 Agent 完全隔离
        </div>
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item label="角色" name="role" rules={[{ required: true, message: '请输入角色' }]}>
            <Input placeholder="例如：前端开发、后端开发、安全审计" />
          </Form.Item>
          <Form.Item label="授权 Skill（从全局 Skill 池选取）" name="skills">
            <Select
              mode="tags"
              placeholder="输入 Skill 名称后按回车，或从 Skill 池搜索"
              options={[
                { value: 'React', label: 'React' },
                { value: 'TypeScript', label: 'TypeScript' },
                { value: 'Python', label: 'Python' },
                { value: 'FastAPI', label: 'FastAPI' },
                { value: 'Node.js', label: 'Node.js' },
                { value: 'PostgreSQL', label: 'PostgreSQL' },
                { value: 'Docker', label: 'Docker' },
                { value: 'OWASP', label: 'OWASP' },
              ]}
            />
          </Form.Item>
          <Form.Item label="初始负责子项目（可选）" name="subproject_id">
            <Select
              allowClear
              placeholder="选择要负责的子项目"
              options={subprojects
                .filter(s => !s.agent_id)
                .map(s => ({ value: s.id, label: s.name }))}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit">创建</Button>
              <Button onClick={() => { setCreateModal(false); form.resetFields(); }}>取消</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 分配负责人弹窗 */}
      <Modal
        title="分配子项目负责人"
        open={assignModal.open}
        onCancel={() => setAssignModal({ open: false, subprojectId: '' })}
        footer={null}
        width={400}
      >
        <div className="text-xs text-gray-400 mb-4">
          每个子项目只能有一个负责人，同一 Agent 不能同时负责多个子项目
        </div>
        <div className="space-y-2">
          {agents.map(agent => {
            const currentSp = subprojects.find(s => s.agent_id === agent.id);
            const isOccupied = currentSp && currentSp.id !== assignModal.subprojectId;
            return (
              <div
                key={agent.id}
                className={`flex items-center justify-between p-3 rounded border ${isOccupied ? 'opacity-40 cursor-not-allowed bg-gray-50' : 'hover:bg-blue-50 cursor-pointer border-gray-200'}`}
                onClick={() => !isOccupied && handleAssign(assignModal.subprojectId, agent.id)}
              >
                <Space>
                  <RobotOutlined className="text-blue-500" />
                  <div>
                    <div className="font-medium text-sm">{agent.role}</div>
                    <div className="text-xs text-gray-400">{agent.id}</div>
                  </div>
                </Space>
                {isOccupied
                  ? <Tag color="orange">已负责 {currentSp?.name}</Tag>
                  : <Tag color="green">可分配</Tag>}
              </div>
            );
          })}
          <Divider />
          <Button
            block
            danger
            onClick={() => handleAssign(assignModal.subprojectId, '')}
          >
            移除负责人
          </Button>
        </div>
      </Modal>
    </div>
  );
};

export default ProjectAgents;
