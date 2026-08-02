/**
 * 项目列表页
 * 新建项目时调用后端 API，每个项目自动创建独立的 Agent 团队
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  Card, Table, Button, Tag, Space, Modal, Form, Input,
  message, Row, Col, Statistic, Empty, Tooltip,
} from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  PlusOutlined, ProjectOutlined, RocketOutlined,
  CheckCircleOutlined, ReloadOutlined, ArrowRightOutlined, EditOutlined, DeleteOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;
const ARCHIVE_DIR_KEY = 'metis_archive_dir';

const isTauriRuntime = () => Boolean((window as any).__TAURI_INTERNALS__);

const safeArchiveName = (name: string, fallback: string) => {
  const cleaned = (name || fallback).replace(/[\\/:*?"<>|]+/g, '_').trim();
  return cleaned || fallback;
};

const archiveFilenameFromDisposition = (disposition: string | null, fallback: string) => {
  if (!disposition) return fallback;
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return safeArchiveName(decodeURIComponent(utf8Match[1]), fallback);
    } catch {
      return safeArchiveName(utf8Match[1], fallback);
    }
  }
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return safeArchiveName(plainMatch?.[1] || fallback, fallback);
};

interface Project {
  id: string;
  name: string;
  description: string;
  status: string;
  created_at: number;
  subprojects_count: number;
  agents_count: number;
}

const statusConfig: Record<string, { color: string; text: string }> = {
  planning:      { color: 'blue',    text: '规划中' },
  team_building: { color: 'purple',  text: '组建团队' },
  initializing:  { color: 'cyan',    text: '初始化' },
  executing:     { color: 'green',   text: '执行中' },
  completed:     { color: 'success', text: '已完成' },
  failed:        { color: 'error',   text: '失败' },
};

const ProjectList: React.FC = () => {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [createModal, setCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editModal, setEditModal] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editTarget, setEditTarget] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const createIdempotencyKey = useRef<string | null>(null);
  const navigate = useNavigate();

  const fetchProjects = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API}/projects`);
      const ordered = [...(res.data.projects || [])].sort(
        (left: Project, right: Project) => Number(right.created_at || 0) - Number(left.created_at || 0)
      );
      setProjects(ordered);
    } catch {
      // 后端未启动时显示空列表，不用 mock 数据
      setProjects([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchProjects(); }, []);

  const handleCreate = async (values: any) => {
    setCreating(true);
    try {
      if (!createIdempotencyKey.current) {
        createIdempotencyKey.current = globalThis.crypto?.randomUUID?.()
          || `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      }
      const res = await axios.post(`${API}/projects`, {
        name: values.name,
        description: values.description,
      }, {
        headers: { 'Idempotency-Key': createIdempotencyKey.current },
      });
      const { project_id, agents } = res.data;
      message.success(
        `项目「${values.name}」已创建，独立 Agent 团队就绪（PM: ${agents.pm}）`
      );
      setCreateModal(false);
      createIdempotencyKey.current = null;
      form.resetFields();
      await fetchProjects();
      // 自动跳转到新项目的 PM 组长页面
      navigate(`/projects/${project_id}/pm-team`);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败，请检查后端是否运行');
    } finally {
      setCreating(false);
    }
  };

  const openEdit = (record: Project) => {
    setEditTarget(record);
    editForm.setFieldsValue({ name: record.name, description: record.description });
    setEditModal(true);
  };

  const handleDelete = (record: Project) => {
    Modal.confirm({
      title: '删除项目',
      content: (
        <div>
          <p>确定要删除项目 <strong>「{record.name}」</strong> 吗？</p>
          <p className="text-red-500 text-xs mt-1">此操作不可恢复，项目下所有 Agent、任务和对话记录将一并删除。</p>
        </div>
      ),
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setDeleting(record.id);
        try {
          await axios.delete(`${API}/projects/${record.id}`);
          message.success(`项目「${record.name}」已删除`);
          await fetchProjects();
        } catch (e: any) {
          message.error(e.response?.data?.detail || '删除失败');
        } finally {
          setDeleting(null);
        }
      },
    });
  };

  const handleEdit = async (values: any) => {
    if (!editTarget) return;
    setEditing(true);
    try {
      await axios.patch(`${API}/projects/${editTarget.id}`, {
        name: values.name,
        description: values.description,
      });
      message.success('项目信息已更新');
      setEditModal(false);
      editForm.resetFields();
      setEditTarget(null);
      await fetchProjects();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '更新失败');
    } finally {
      setEditing(false);
    }
  };

  const handleArchive = async (record: Project) => {
    const base = API.replace(/\/$/, '');
    const url = `${base}/projects/${record.id}/archive/download`;
    const configuredDir = localStorage.getItem(ARCHIVE_DIR_KEY)?.trim();

    if (!isTauriRuntime() || !configuredDir) {
      if (isTauriRuntime() && !configuredDir) {
        message.info('请先在设置中配置归档目录，当前使用下载兜底');
      }
      window.open(url, '_blank', 'noopener,noreferrer');
      return;
    }

    try {
      const response = await fetch(url, { credentials: 'include' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const buffer = await response.arrayBuffer();
      const fallback = `${safeArchiveName(record.name, 'project')}_${record.id}.zip`;
      const filename = archiveFilenameFromDisposition(response.headers.get('content-disposition'), fallback);
      const { invoke } = await import('@tauri-apps/api/core');
      const savedPath = await invoke<string>('save_project_archive', {
        directory: configuredDir,
        filename,
        bytes: Array.from(new Uint8Array(buffer)),
      });
      message.success(`已归档到：${savedPath}`);
    } catch (e: any) {
      message.error(e?.message ? `归档失败：${e.message}` : '归档失败');
    }
  };

  const stats = {
    total: projects.length,
    executing: projects.filter(p => p.status === 'executing').length,
    completed: projects.filter(p => p.status === 'completed').length,
  };

  const compactDescription = (description = '') =>
    description.length > 15 ? `${description.slice(0, 15)}...` : description;

  const columns = [
    {
      title: '项目名称',
      key: 'name',
      render: (_: any, record: Project) => (
        <div>
          <div className="font-medium">{record.name}</div>
          <div className="text-xs text-gray-400">{compactDescription(record.description)}</div>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => {
        const cfg = statusConfig[status] || { color: 'default', text: status };
        return <Tag color={cfg.color}>{cfg.text}</Tag>;
      },
    },
    {
      title: 'Agent 数',
      dataIndex: 'agents_count',
      key: 'agents_count',
      width: 90,
      render: (n: number) => (
        <Tooltip title="本项目专属 Agent（不含核心 Agent）">
          <Tag color="blue">{n} 个</Tag>
        </Tooltip>
      ),
    },
    {
      title: '子项目数',
      dataIndex: 'subprojects_count',
      key: 'subprojects_count',
      width: 90,
      render: (n: number) => <Tag>{n} 个</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (t: number) => t
        ? new Date(t * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 260,
      render: (_: any, record: Project) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(record)}
          >
            编辑
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<ArrowRightOutlined />}
            onClick={() => navigate(`/projects/${record.id}/pm-team`)}
          >
            进入
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleArchive(record)}
          >
            下载
          </Button>
          <Button
            danger
            size="small"
            icon={<DeleteOutlined />}
            loading={deleting === record.id}
            onClick={() => handleDelete(record)}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-xl font-semibold m-0">项目管理</h2>
          <div className="text-xs text-gray-400 mt-1">
            每个项目拥有独立的 Agent 团队，互不干扰
          </div>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchProjects}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { createIdempotencyKey.current = null; setCreateModal(true); }}>
            新建项目
          </Button>
        </Space>
      </div>

      {/* 统计 — 阶段看板质量指标风格 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={8}>
          <div className="metis-stat-card">
            <Statistic
              title="总项目数"
              value={stats.total}
              prefix={<ProjectOutlined style={{ fontSize: 18 }} />}
              valueStyle={{ color: '#1677ff', fontWeight: 700 }}
            />
            <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 4 }}>所有独立 Agent 团队</div>
          </div>
        </Col>
        <Col xs={24} sm={8}>
          <div className="metis-stat-card">
            <Statistic
              title="执行中"
              value={stats.executing}
              prefix={<RocketOutlined style={{ fontSize: 18 }} />}
              valueStyle={{ color: '#52c41a', fontWeight: 700 }}
            />
            <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 4 }}>阶段看板活跃阶段</div>
          </div>
        </Col>
        <Col xs={24} sm={8}>
          <div className="metis-stat-card">
            <Statistic
              title="已完成"
              value={stats.completed}
              prefix={<CheckCircleOutlined style={{ fontSize: 18 }} />}
              valueStyle={{ color: '#722ed1', fontWeight: 700 }}
            />
            <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 4 }}>项目全生命周期完成</div>
          </div>
        </Col>
      </Row>

      {/* 项目列表 — 卡片化 */}
      <Card 
        title={
          <div className="metis-section-header" style={{ marginBottom: 0 }}>
            <ProjectOutlined className="metis-header-icon" style={{ color: '#1677ff' }} />
            <span className="metis-header-title">项目列表</span>
          </div>
        }
        className="metis-card" 
        style={{ border: 'none' }}
      >
        {projects.length === 0 && !loading ? (
          <Empty
            description="暂无项目，点击「新建项目」开始"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModal(true)}>
              新建第一个项目
            </Button>
          </Empty>
        ) : (
          <Table
            columns={columns}
            dataSource={projects}
            rowKey="id"
            loading={loading}
            pagination={{ pageSize: 10 }}
          />
        )}
      </Card>

      {/* 新建项目弹窗 */}
      <Modal
        title="新建项目"
        open={createModal}
        onCancel={() => { createIdempotencyKey.current = null; setCreateModal(false); form.resetFields(); }}
        footer={null}
        width={480}
      >
        <div className="text-xs text-gray-400 mb-4 p-3 bg-blue-50 rounded">
          创建后系统将自动为本项目实例化一套独立的核心 Agent：
          <strong> PM · HR · PG · Supervisor · CCB</strong>
          <br />与其他项目的 Agent 完全隔离，互不影响。
        </div>
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            label="项目名称"
            name="name"
            rules={[{ required: true, message: '请输入项目名称' }]}
          >
            <Input placeholder="例如：电商平台 v2.0" />
          </Form.Item>
          <Form.Item
            label="项目描述"
            name="description"
            rules={[{ required: true, message: '请输入项目描述' }]}
          >
            <Input.TextArea
              rows={3}
              placeholder="简要描述项目目标和范围，PM Agent 将基于此开始需求分析"
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={creating}>
                创建并进入 PM 对话
              </Button>
              <Button onClick={() => { createIdempotencyKey.current = null; setCreateModal(false); form.resetFields(); }}>取消</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑项目弹窗 */}
      <Modal
        title="编辑项目"
        open={editModal}
        onCancel={() => { setEditModal(false); editForm.resetFields(); setEditTarget(null); }}
        footer={null}
        width={480}
      >
        <Form form={editForm} layout="vertical" onFinish={handleEdit}>
          <Form.Item
            label="项目名称"
            name="name"
            rules={[{ required: true, message: '请输入项目名称' }]}
          >
            <Input placeholder="例如：电商平台 v2.0" />
          </Form.Item>
          <Form.Item
            label="项目描述"
            name="description"
            rules={[{ required: true, message: '请输入项目描述' }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={editing}>
                保存
              </Button>
              <Button onClick={() => { setEditModal(false); editForm.resetFields(); setEditTarget(null); }}>
                取消
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default ProjectList;
