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
} from '@ant-design/icons';
import { Alert } from 'antd';
import axios from 'axios';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as ChartTooltip,
  ResponsiveContainer, PieChart, Pie, Cell,
} from 'recharts';
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
  started_at?: number;
  finished_at?: number;
  output_files?: string[];
  domains?: string[];
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
  const [projectMetrics, setProjectMetrics] = useState<any>(null);
  const [fetchError, setFetchError] = useState('');
  const [form] = Form.useForm();

  const fetchData = async (showLoading = true) => {
    if (!projectId) return;
    if (showLoading) setLoading(true);
    try {
      const [agentsRes, spRes, projectRes, metricsRes] = await Promise.all([
        axios.get(`${API}/projects/${projectId}/agents`),
        axios.get(`${API}/projects/${projectId}/subprojects/list`),
        axios.get(`${API}/projects/${projectId}`),
        axios.get(`${API}/projects/${projectId}/metrics`),
      ]);
      setAgents(agentsRes.data.agents || []);
      setSubprojects(spRes.data.subprojects || []);
      setProject(projectRes.data);
      setProjectMetrics(metricsRes.data);
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
    roles: new Set(agents.map(a => a.role).filter(Boolean)).size,
    idle: agents.filter(a => a.status === 'idle').length,
    working: agents.filter(a => ACTIVE_AGENT_STATUSES.has(a.status)).length,
    completed: agents.filter(a => a.status === 'completed').length,
    failed: agents.filter(a => a.status === 'failed').length,
    assigned: subprojects.filter(s => s.agent_id).length,
  };
  const completedTasks = subprojects.filter(s => s.status === 'completed').length;
  const outputFileCount = new Set(agents.flatMap(a => a.output_files || [])).size;
  const completedDurations = agents
    .filter(a => a.started_at && a.finished_at && a.finished_at >= a.started_at)
    .map(a => Number(a.finished_at) - Number(a.started_at));
  const averageDurationSeconds = completedDurations.length > 0
    ? Math.round(completedDurations.reduce((sum, value) => sum + value, 0) / completedDurations.length)
    : null;
  const skillAuthorizedAgents = agents.filter(a => a.skill_names?.length > 0).length;
  const durationChartData = agents
    .filter(a => a.started_at && a.finished_at && a.finished_at >= a.started_at)
    .map(a => ({
      name: (a.subproject_id ? subprojects.find(s => s.id === a.subproject_id)?.name : a.role) || a.role,
      seconds: Math.round(Number(a.finished_at) - Number(a.started_at)),
    }));
  const outputChartData = agents
    .map(a => ({ name: a.role || a.id, files: new Set(a.output_files || []).size }))
    .filter(item => item.files > 0);
  const taskStatusData = [
    { name: '已完成', value: completedTasks, color: '#52c41a' },
    { name: '执行中', value: subprojects.filter(s => ['working', 'executing', 'in_progress'].includes(s.status)).length, color: '#1677ff' },
    { name: '待开始', value: subprojects.filter(s => ['pending', 'idle'].includes(s.status)).length, color: '#bfbfbf' },
    { name: '失败', value: subprojects.filter(s => ['failed', 'error', 'blocked'].includes(s.status)).length, color: '#ff4d4f' },
  ].filter(item => item.value > 0);

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
      title: '已授权 Skill',
      dataIndex: 'skill_names',
      key: 'skill_names',
      width: '38%',
      render: (skills: string[]) => (
        <Space wrap size={4}>
          {skills?.length > 0
            ? skills.map(s => <Tag key={s} color="blue" className="text-xs">{s}</Tag>)
            : <span className="text-gray-400 text-xs">暂无授权</span>}
        </Space>
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
  ];

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold mb-1">项目团队</h2>
        <div className="text-sm text-gray-500">查看成员角色、授权能力和职责分配；任务进度统一在进度看板查看。</div>
      </div>
      {fetchError && (
        <Alert
          type="error"
          showIcon
          message="Agent 状态加载失败，当前页面不会使用演示数据"
          description={fetchError}
          action={<Button size="small" onClick={() => void fetchData(true)}>重试</Button>}
        />
      )}
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
            <Statistic title="角色类型" value={stats.roles} valueStyle={{ color: '#722ed1' }} />
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
        title={<Space><RobotOutlined /> 团队成员名单</Space>}
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void fetchData(true)} size="small">刷新</Button>
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

      <Card title="团队数据看板">
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Statistic title="任务完成率" value={subprojects.length ? Math.round(completedTasks / subprojects.length * 100) : 0} suffix="%" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="实际产出文件" value={outputFileCount} suffix="个" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="Agent 平均执行时长" value={averageDurationSeconds ?? '-'} suffix={averageDurationSeconds == null ? undefined : '秒'} />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="Skill 授权覆盖" value={agents.length ? Math.round(skillAuthorizedAgents / agents.length * 100) : 0} suffix="%" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="阶段一次通过率" value={projectMetrics?.quality?.first_pass_rate != null ? Math.round(projectMetrics.quality.first_pass_rate * 100) : '-'} suffix={projectMetrics?.quality?.first_pass_rate == null ? undefined : '%'} />
          </Col>
          <Col span={24}><Divider style={{ margin: '4px 0' }} /></Col>
          <Col xs={24} lg={8}>
            <div className="font-medium mb-3">任务状态分布</div>
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={taskStatusData} dataKey="value" nameKey="name" innerRadius={48} outerRadius={82} label>
                    {taskStatusData.map(item => <Cell key={item.name} fill={item.color} />)}
                  </Pie>
                  <ChartTooltip />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </Col>
          <Col xs={24} lg={8}>
            <div className="font-medium mb-3">Agent 执行时长（秒）</div>
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={durationChartData} margin={{ top: 8, right: 8, left: 0, bottom: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" hide />
                  <YAxis />
                  <ChartTooltip />
                  <Bar dataKey="seconds" name="执行时长" fill="#1677ff" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Col>
          <Col xs={24} lg={8}>
            <div className="font-medium mb-3">Agent 交付文件数</div>
            <div style={{ height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={outputChartData} margin={{ top: 8, right: 8, left: 0, bottom: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" hide />
                  <YAxis allowDecimals={false} />
                  <ChartTooltip />
                  <Bar dataKey="files" name="交付文件" fill="#722ed1" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Col>
        </Row>
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
