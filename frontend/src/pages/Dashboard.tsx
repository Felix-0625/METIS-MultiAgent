/**
 * 首页仪表盘 - 精简版
 * 只展示真实有用的信息：项目列表、核心 Agent 状态、Skill 池概况
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Row, Col, Statistic, List, Tag, Button, Space, Typography, Empty, Alert, Steps,
} from 'antd';
import {
  ProjectOutlined, RocketOutlined, CheckCircleOutlined,
  ApiOutlined, PlusOutlined, ArrowRightOutlined, RobotOutlined,
  SettingOutlined, CloseOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const { Title, Text } = Typography;
const API = API_BASE_URL;

interface Project {
  id: string;
  name: string;
  description: string;
  status: string;
  agents_count: number;
  subprojects_count: number;
  created_at: number;
}

interface SkillStatus {
  total_skills: number;
  active_skills: number;
}

const statusConfig: Record<string, { color: string; text: string }> = {
  planning:      { color: 'blue',    text: '规划中' },
  team_building: { color: 'purple',  text: '组建团队' },
  initializing:  { color: 'cyan',    text: '初始化' },
  executing:     { color: 'green',   text: '执行中' },
  completed:     { color: 'success', text: '已完成' },
};

// 核心 Agent 说明（固定，每个项目都有这套）
const coreAgents = [
  { type: 'PM',  name: 'PM Agent',         desc: '需求分析 · 规划书 · 子项目拆解',   color: '#722ed1' },
  { type: 'HR',  name: 'HR Agent',          desc: '动态创建执行 Agent · Skill 授权',  color: '#fa8c16' },
  { type: 'SUP', name: 'Supervisor Agent',  desc: '任务调度 · 进度监控 · 质检触发',   color: '#f5222d' },
  { type: 'PG',  name: 'PG Agent',          desc: '文件归档 · 版本控制 · 证书验证',   color: '#1890ff' },
  { type: 'CCB', name: 'CCB Agent',         desc: '变更仲裁 · 风险评估 · 冲突调解',   color: '#52c41a' },
];

// 新手引导步骤
const GUIDE_STEPS = [
  { title: '配置 API Key', desc: '前往「设置」页面配置 LLM API Key，否则 Agent 无法工作', action: '去设置', path: '/settings' },
  { title: '新建项目', desc: '在「项目管理」页面创建项目，填写项目名称和描述', action: '去新建', path: '/projects' },
  { title: '与 PM 组长对话', desc: '进入项目后，在「PM 组长」页面描述需求，生成总规划', action: null, path: null },
  { title: '确认阶段看板', desc: '总规划确认后，在「阶段看板」逐阶段启动，Agent 自动开始工作', action: null, path: null },
  { title: '质检 & 确认完成', desc: '每个阶段完成后点击「质检」，通过后手动「确认阶段完成」', action: null, path: null },
];

const Dashboard: React.FC = () => {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [skillStatus, setSkillStatus] = useState<SkillStatus>({ total_skills: 0, active_skills: 0 });
  const [loading, setLoading] = useState(false);
  const [showGuide, setShowGuide] = useState(() => {
    // 首次进入显示引导，用户关闭后不再显示
    return localStorage.getItem('guide_dismissed') !== '1';
  });

  useEffect(() => {
    const fetchAll = async () => {
      setLoading(true);
      try {
        const [projRes, skillRes] = await Promise.allSettled([
          axios.get(`${API}/projects`),
          axios.get(`${API}/skills/status`),
        ]);
        if (projRes.status === 'fulfilled') {
          setProjects(projRes.value.data.projects || []);
        } else {
          // 后端未启动时的 mock 数据
          setProjects([
            { id: 'proj-demo', name: '示例项目（后端未连接）', description: '请启动后端服务', status: 'planning', agents_count: 5, subprojects_count: 0, created_at: Date.now() / 1000 },
          ]);
        }
        if (skillRes.status === 'fulfilled') setSkillStatus(skillRes.value.data);
      } finally {
        setLoading(false);
      }
    };
    fetchAll();
  }, []);

  const stats = {
    total: projects.length,
    active: projects.filter(p => ['executing', 'team_building', 'initializing', 'planning'].includes(p.status)).length,
    completed: projects.filter(p => p.status === 'completed').length,
  };

  return (
    <div className="space-y-5">
      {/* 新手引导 */}
      {showGuide && (
        <Card
          size="small"
          style={{ border: '1px solid #1677ff', background: '#f0f5ff' }}
          title={<span style={{ color: '#1677ff', fontWeight: 600 }}>🚀 快速上手指南</span>}
          extra={
            <Button
              type="text" size="small" icon={<CloseOutlined />}
              onClick={() => { localStorage.setItem('guide_dismissed', '1'); setShowGuide(false); }}
            >
              不再显示
            </Button>
          }
        >
          <Steps
            size="small"
            direction="horizontal"
            items={GUIDE_STEPS.map((s, i) => ({
              title: (
                <span style={{ fontSize: 12 }}>
                  {s.title}
                  {s.action && s.path && (
                    <Button
                      type="link" size="small"
                      style={{ fontSize: 11, padding: '0 4px' }}
                      onClick={() => navigate(s.path!)}
                    >
                      {s.action} →
                    </Button>
                  )}
                </span>
              ),
              description: <span style={{ fontSize: 11, color: '#595959' }}>{s.desc}</span>,
            }))}
          />
        </Card>
      )}

      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <Title level={4} className="m-0">AI 多 Agent 项目管理系统</Title>
          <Text type="secondary" className="text-xs">
            每个项目拥有独立的 Agent 团队 · Skill 池全局共享 · 子项目唯一负责人制
          </Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/projects')}>
          新建项目
        </Button>
      </div>

      {/* 统计 */}
      <Row gutter={16}>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="总项目数"
              value={stats.total}
              prefix={<ProjectOutlined />}
              valueStyle={{ color: '#1890ff' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="进行中"
              value={stats.active}
              prefix={<RocketOutlined />}
              valueStyle={{ color: '#52c41a' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="已完成"
              value={stats.completed}
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: '#722ed1' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="Skill 池"
              value={skillStatus.active_skills}
              suffix={`/ ${skillStatus.total_skills}`}
              prefix={<ApiOutlined />}
              valueStyle={{ color: '#fa8c16' }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        {/* 最近项目 */}
        <Col span={15}>
          <Card
            title="项目列表"
            size="small"
            extra={
              <Button type="link" size="small" onClick={() => navigate('/projects')}>
                全部 <ArrowRightOutlined />
              </Button>
            }
          >
            {projects.length === 0 ? (
              <Empty
                description="暂无项目"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              >
                <Button type="primary" size="small" onClick={() => navigate('/projects')}>
                  新建第一个项目
                </Button>
              </Empty>
            ) : (
              <List
                dataSource={projects.slice(0, 5)}
                renderItem={(project) => {
                  const cfg = statusConfig[project.status] || { color: 'default', text: project.status };
                  return (
                    <List.Item
                      actions={[
                        <Button
                          type="link"
                          size="small"
                          key="enter"
                          onClick={() => navigate(`/projects/${project.id}/pm-team`)}
                        >
                          进入 <ArrowRightOutlined />
                        </Button>,
                      ]}
                    >
                      <List.Item.Meta
                        title={
                          <Space size={6}>
                            <span className="font-medium">{project.name}</span>
                            <Tag color={cfg.color} className="text-xs">{cfg.text}</Tag>
                          </Space>
                        }
                        description={
                          <Space size={12} className="text-xs text-gray-400">
                            <span>{project.description}</span>
                            <span>
                              <RobotOutlined /> {project.agents_count} 个 Agent
                            </span>
                            <span>{project.subprojects_count} 个子项目</span>
                          </Space>
                        }
                      />
                    </List.Item>
                  );
                }}
              />
            )}
          </Card>
        </Col>

        {/* 核心 Agent 架构说明 */}
        <Col span={9}>
          <Card title="核心 Agent（每个项目独立实例）" size="small">
            <div className="space-y-2">
              {coreAgents.map(agent => (
                <div
                  key={agent.type}
                  className="flex items-start gap-3 p-2 rounded hover:bg-gray-50"
                >
                  <div
                    className="w-8 h-8 rounded flex items-center justify-center text-white text-xs font-bold flex-shrink-0"
                    style={{ backgroundColor: agent.color }}
                  >
                    {agent.type}
                  </div>
                  <div>
                    <div className="text-sm font-medium">{agent.name}</div>
                    <div className="text-xs text-gray-400">{agent.desc}</div>
                  </div>
                </div>
              ))}
              <div className="mt-2 pt-2 border-t text-xs text-gray-400">
                + 执行 Agent 由 HR 根据规划书动态创建，每个子项目有唯一负责人
              </div>
            </div>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Dashboard;
