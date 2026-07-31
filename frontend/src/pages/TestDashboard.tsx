/**
 * 测试数据看板 - 展示 100 个 Trae Solo 模拟测试的结果
 * 对接后端 /metrics/summary 和 /projects API
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Row, Col, Statistic, Table, Progress, Tag, Spin, Empty, 
  Typography, Space, Alert, Tabs
} from 'antd';
import {
  CheckCircleOutlined, ClockCircleOutlined, SyncOutlined,
  RocketOutlined, BugOutlined, FileTextOutlined, DashboardOutlined,
  ThunderboltOutlined, CodeOutlined
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer
} from 'recharts';

const { Title, Text } = Typography;
const API = API_BASE_URL;

const COLORS = ['#52c41a', '#1890ff', '#faad14', '#f5222d', '#722ed1'];

interface ProjectMetrics {
  project_id: string;
  project_name: string;
  efficiency: {
    e2e_hours: number | null;
    phase_durations: Array<{
      phase_id: string;
      phase_name: string;
      status: string;
      duration_seconds: number | null;
      started_at: number;
      completed_at: number | null;
    }>;
  };
  data_completeness: {
    has_phases: boolean;
    has_code: boolean;
    total_issues: number;
    total_lines: number;
  };
}

interface Project {
  id: string;
  name: string;
  status: string;
  created_at: number;
}

const TestDashboard: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<any>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [metricsData, setMetricsData] = useState<ProjectMetrics[]>([]);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    try {
      // 1. 获取汇总数据
      const summaryRes = await axios.get(`${API}/metrics/summary`);
      setSummary(summaryRes.data);

      // 2. 获取项目列表
      const projRes = await axios.get(`${API}/projects`);
      const projList = projRes.data.projects || [];
      setProjects(projList);

      // 3. 获取每个项目的详细指标
      const metricsPromises = projList.map((p: Project) =>
        axios.get(`${API}/projects/${p.id}/metrics`)
          .then(res => res.data)
          .catch(() => null)
      );
      const metrics = await Promise.all(metricsPromises);
      setMetricsData(metrics.filter(m => m !== null));
    } catch (error) {
      console.error('加载数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '80vh' }}>
        <Spin size="large" tip="加载测试数据..."><div style={{height:60}} /></Spin>
      </div>
    );
  }

  if (!projects.length) {
    return (
      <Empty
        description="暂无测试数据"
        style={{ marginTop: 100 }}
      />
    );
  }

  // 统计计算
  const totalProjects = projects.length;
  const allPhases = metricsData.flatMap(m => m.efficiency?.phase_durations || []);
  const totalPhases = allPhases.length;
  const completedPhases = allPhases.filter(p => p.status === 'completed').length;
  const reviewingPhases = allPhases.filter(p => p.status === 'reviewing').length;
  const donePhases = completedPhases + reviewingPhases;
  const phaseRate = totalPhases ? Math.round((donePhases / totalPhases) * 100) : 0;

  const projectsWithCode = metricsData.filter(m => m.data_completeness?.has_code).length;
  const totalCodeLines = metricsData.reduce((sum, m) => sum + (m.data_completeness?.total_lines || 0), 0);
  const totalIssues = metricsData.reduce((sum, m) => sum + (m.data_completeness?.total_issues || 0), 0);

  // 项目状态分布
  const statusDist: Record<string, number> = {};
  projects.forEach(p => {
    statusDist[p.status] = (statusDist[p.status] || 0) + 1;
  });
  const statusChartData = Object.entries(statusDist).map(([name, value]) => ({ name, value }));

  // 阶段状态分布
  const phaseStatusDist: Record<string, number> = {};
  allPhases.forEach(p => {
    phaseStatusDist[p.status] = (phaseStatusDist[p.status] || 0) + 1;
  });
  const phaseStatusData = Object.entries(phaseStatusDist).map(([name, value]) => ({ name, value }));

  // 阶段耗时分布（只统计有耗时数据的）
  const phaseDurationData = metricsData
    .flatMap(m => m.efficiency?.phase_durations || [])
    .filter(p => p.duration_seconds !== null)
    .map(p => ({
      name: p.phase_name,
      duration: Math.round((p.duration_seconds || 0) / 60), // 转为分钟
    }));

  // 表格数据
  const tableData = projects.map((p, idx) => {
    const m = metricsData[idx];
    const phases = m?.efficiency?.phase_durations || [];
    const totalP = phases.length;
    const doneP = phases.filter(ph => ph.status === 'completed' || ph.status === 'reviewing').length;
    return {
      key: p.id,
      id: p.id,
      name: p.name,
      status: p.status,
      phases: `${doneP}/${totalP}`,
      phasesRate: totalP ? Math.round((doneP / totalP) * 100) : 0,
      code: m?.data_completeness?.total_lines || 0,
      issues: m?.data_completeness?.total_issues || 0,
      time: m?.efficiency?.e2e_hours || '-',
    };
  });

  const columns = [
    { title: '项目ID', dataIndex: 'id', key: 'id', width: 120, ellipsis: true },
    { title: '项目名', dataIndex: 'name', key: 'name', width: 180, ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => {
        const colorMap: Record<string, string> = {
          completed: 'green',
          planning: 'blue',
          in_progress: 'orange',
          failed: 'red',
        };
        return <Tag color={colorMap[status] || 'default'}>{status}</Tag>;
      },
    },
    {
      title: '阶段进度',
      dataIndex: 'phases',
      key: 'phases',
      width: 150,
      render: (text: string, record: any) => (
        <Space>
          <Text>{text}</Text>
          <Progress
            type="circle"
            percent={record.phasesRate}
            width={30}
            strokeColor={record.phasesRate === 100 ? '#52c41a' : '#1890ff'}
          />
        </Space>
      ),
    },
    { title: '代码行数', dataIndex: 'code', key: 'code', width: 100, align: 'right' as const },
    { title: '问题数', dataIndex: 'issues', key: 'issues', width: 80, align: 'right' as const },
    {
      title: '耗时(h)',
      dataIndex: 'time',
      key: 'time',
      width: 100,
      align: 'right' as const,
      render: (time: any) => typeof time === 'number' ? time.toFixed(1) : '-',
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 24 }}>
        <Title level={3}>
          <DashboardOutlined /> 测试数据看板
        </Title>
      </div>

      {/* 核心指标卡片 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="测试项目总数"
              value={totalProjects}
              prefix={<RocketOutlined />}
              valueStyle={{ color: '#1890ff' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="阶段推进率"
              value={phaseRate}
              suffix="%"
              prefix={<ThunderboltOutlined />}
              valueStyle={{ color: phaseRate >= 80 ? '#52c41a' : phaseRate >= 50 ? '#faad14' : '#f5222d' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {donePhases}/{totalPhases} 阶段完成
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="代码产出项目"
              value={projectsWithCode}
              suffix={`/ ${totalProjects}`}
              prefix={<CodeOutlined />}
              valueStyle={{ color: '#722ed1' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              总代码行: {totalCodeLines.toLocaleString()}
            </Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title="问题总数"
              value={totalIssues}
              prefix={<BugOutlined />}
              valueStyle={{ color: '#f5222d' }}
            />
          </Card>
        </Col>
      </Row>

      {/* 图表区域 */}
      <Tabs
        defaultActiveKey="overview"
        items={[
          {
            key: 'overview',
            label: '📊 总览',
            children: (
              <Row gutter={[16, 16]}>
                <Col xs={24} lg={12}>
                  <Card title="项目状态分布" bordered={false}>
                    <ResponsiveContainer width="100%" height={300}>
                      <PieChart>
                        <Pie
                          data={statusChartData}
                          cx="50%"
                          cy="50%"
                          labelLine={false}
                          label={(props: any) => {
                            const { name, percent } = props;
                            return `${name || ''} (${((percent || 0) * 100).toFixed(0)}%)`;
                          }}
                          outerRadius={100}
                          fill="#8884d8"
                          dataKey="value"
                        >
                          {statusChartData.map((entry, index) => (
                            <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                          ))}
                        </Pie>
                        <Tooltip />
                      </PieChart>
                    </ResponsiveContainer>
                  </Card>
                </Col>
                <Col xs={24} lg={12}>
                  <Card title="阶段状态分布" bordered={false}>
                    <ResponsiveContainer width="100%" height={300}>
                      <BarChart data={phaseStatusData}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="name" />
                        <YAxis />
                        <Tooltip />
                        <Legend />
                        <Bar dataKey="value" fill="#1890ff" name="数量" />
                      </BarChart>
                    </ResponsiveContainer>
                  </Card>
                </Col>
              </Row>
            ),
          },
          {
            key: 'details',
            label: '📋 明细',
            children: (
              <Card bordered={false}>
                <Table
                  columns={columns}
                  dataSource={tableData}
                  pagination={{
                    pageSize: 20,
                    showSizeChanger: true,
                    showTotal: (total) => `共 ${total} 个项目`,
                  }}
                  scroll={{ x: 1000 }}
                  size="small"
                />
              </Card>
            ),
          },
          {
            key: 'performance',
            label: '⚡ 性能',
            children: (
              <Row gutter={[16, 16]}>
                <Col xs={24}>
                  <Card title="阶段耗时分析（前20个有数据的阶段）" bordered={false}>
                    <ResponsiveContainer width="100%" height={400}>
                      <BarChart data={phaseDurationData.slice(0, 20)}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="name" angle={-45} textAnchor="end" height={100} />
                        <YAxis label={{ value: '分钟', angle: -90, position: 'insideLeft' }} />
                        <Tooltip />
                        <Legend />
                        <Bar dataKey="duration" fill="#52c41a" name="耗时(分钟)" />
                      </BarChart>
                    </ResponsiveContainer>
                  </Card>
                </Col>
              </Row>
            ),
          },
        ]}
      />
    </div>
  );
};

export default TestDashboard;
