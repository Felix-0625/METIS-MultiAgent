/**
 * Skill 池管理页面
 * 按 Agent 类型分类：PM / Supervisor / HR / PG / CCB / 通用
 * 全局共享，所有项目共用同一个 Skill 池
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Table, Tag, Button, Input, Space, Modal, Form,
  message, Row, Col, Badge, Tooltip, Empty, Divider, Tabs,
} from 'antd';
import {
  PlusOutlined, SearchOutlined, ReloadOutlined,
  DeleteOutlined, AppstoreOutlined, ApartmentOutlined, EditOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;
const authFetch = (input: RequestInfo | URL, init: RequestInit = {}) =>
  fetch(input, { ...init, credentials: 'include' });

interface Skill {
  id: string;
  name: string;
  description: string;
  version: string;
  content?: string;
  tags: string[];
  source: string;
  status: string;
  created_at?: number;
  capability_type?: { type: string; confidence: number };
  application_scenario?: { business_domain: string; roles: string[] };
}

interface GroupedSkills {
  pm: Skill[];
  supervisor: Skill[];
  hr: Skill[];
  pg: Skill[];
  ccb: Skill[];
  common: Skill[];
  other: Skill[];
}

// Agent 类型配置
const AGENT_CATEGORIES: { key: keyof GroupedSkills; label: string; color: string; emoji: string }[] = [
  { key: 'pm',         label: 'PM Agent',         color: '#722ed1', emoji: '💼' },
  { key: 'supervisor', label: 'Supervisor Agent', color: '#f5222d', emoji: '🔍' },
  { key: 'hr',         label: 'HR Agent',         color: '#fa8c16', emoji: '👥' },
  { key: 'pg',         label: 'PG Agent',         color: '#1890ff', emoji: '💻' },
  { key: 'ccb',        label: 'CCB Agent',        color: '#13c2c2', emoji: '🔄' },
  { key: 'common',     label: '通用',              color: '#52c41a', emoji: '🌐' },
  { key: 'other',      label: '其他',              color: '#8c8c8c', emoji: '📦' },
];

// 专业领域颜色映射
const DOMAIN_COLORS: Record<string, string> = {
  '需求分析': '#722ed1',
  '项目管理': '#f5222d',
  '团队管理': '#fa8c16',
  '代码开发': '#1890ff',
  '质量保障': '#13c2c2',
  '变更管理': '#eb2f96',
  '文档写作': '#52c41a',
  'AI能力':   '#faad14',
  '文件处理': '#8c8c8c',
  '其他':     '#d9d9d9',
};

const DOMAIN_EMOJIS: Record<string, string> = {
  '需求分析': '📋', '项目管理': '📅', '团队管理': '👥',
  '代码开发': '💻', '质量保障': '✅', '变更管理': '🔄',
  '文档写作': '📝', 'AI能力': '🤖', '文件处理': '📁', '其他': '📦',
};

// 过滤掉 agent 分类 key 和来源标签，只保留真正的能力标签（最多3个）
const CATEGORY_KEYS = new Set(['pm', 'supervisor', 'hr', 'pg', 'ccb', 'common', 'other', 'manual', 'url', 'file', 'system']);
function getCoreTagsOnly(tags: string[]): string[] {
  return (tags || []).filter((t: string) => !CATEGORY_KEYS.has(t)).slice(0, 3);
}

const SkillPool: React.FC = () => {
  const [allSkills, setAllSkills] = useState<Skill[]>([]);
  const [grouped, setGrouped] = useState<GroupedSkills>({
    pm: [], supervisor: [], hr: [], pg: [], ccb: [], common: [], other: [],
  });
  const [domainGrouped, setDomainGrouped] = useState<Record<string, Skill[]>>({});
  const [loading, setLoading] = useState(false);
  const [viewMode, setViewMode] = useState<'agent' | 'domain'>('agent');
  const [selectedCategory, setSelectedCategory] = useState<keyof GroupedSkills | 'all'>('all');
  const [selectedDomain, setSelectedDomain] = useState<string>('all');
  const [searchText, setSearchText] = useState('');
  const [importModal, setImportModal] = useState(false);
  const [form] = Form.useForm();
  // Skill Agent 导入相关状态
  const [ingestTab, setIngestTab] = useState<string>('url');
  const [ingestUrl, setIngestUrl] = useState('');
  const [ingestLoading, setIngestLoading] = useState(false);
  const [ingestResult, setIngestResult] = useState<any>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  // 编辑弹窗状态
  const [editModal, setEditModal] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const [editForm] = Form.useForm();
  const [editLoading, setEditLoading] = useState(false);

  const fetchSkills = async () => {
    setLoading(true);
    try {
      const [listRes, groupedRes, domainRes] = await Promise.all([
        axios.get(`${API}/skills`),
        axios.get(`${API}/skills/grouped`),
        axios.get(`${API}/skills/grouped-by-domain`),
      ]);
      setAllSkills(listRes.data.skills || []);
      setGrouped(groupedRes.data.grouped || {
        pm: [], supervisor: [], hr: [], pg: [], ccb: [], common: [], other: [],
      });
      setDomainGrouped(domainRes.data.grouped || {});
    } catch {
      message.error('加载 Skill 池失败，请确认后端已启动');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchSkills(); }, []);

  const handleImport = async (values: any) => {
    try {
      await axios.post(`${API}/skills/import`, {
        name: values.name,
        description: values.description,
        version: values.version || '1.0.0',
        content: values.content,
        source: 'manual',
      });
      message.success('Skill 导入成功');
      setImportModal(false);
      form.resetFields();
      fetchSkills();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '导入失败');
    }
  };

  const handleDelete = async (skillId: string) => {
    try {
      await axios.delete(`${API}/skills/${skillId}`);
      message.success('已移入回收站（7天内可恢复）');
      fetchSkills();
    } catch {
      message.error('删除失败');
    }
  };

  // 当前分类展示的 Skill 列表（按 Agent 类型视图）
  const displaySkillsByAgent: Skill[] = (() => {
    let base: Skill[] = selectedCategory === 'all' ? allSkills : (grouped[selectedCategory] || []);
    base = base.filter(s => s.status !== 'deleted');
    if (searchText) {
      const q = searchText.toLowerCase();
      base = base.filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        (s.tags || []).some(t => t.toLowerCase().includes(q))
      );
    }
    return base;
  })();

  // 当前分类展示的 Skill 列表（按专业领域视图）
  const displaySkillsByDomain: Skill[] = (() => {
    let base: Skill[] = selectedDomain === 'all'
      ? allSkills
      : (domainGrouped[selectedDomain] || []);
    base = base.filter(s => s.status !== 'deleted');
    if (searchText) {
      const q = searchText.toLowerCase();
      base = base.filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        (s.tags || []).some(t => t.toLowerCase().includes(q))
      );
    }
    return base;
  })();

  const displaySkills = viewMode === 'agent' ? displaySkillsByAgent : displaySkillsByDomain;
  const activeCount = allSkills.filter(s => s.status !== 'deleted').length;

  const columns = [
    {
      title: 'Skill 名称',
      key: 'name',
      render: (_: any, record: Skill) => (
        <div>
          <div className="font-medium">{record.name}</div>
          <div className="text-xs text-gray-400 mt-0.5">{record.description}</div>
        </div>
      ),
    },
    {
      title: '标签',
      key: 'tags',
      width: 240,
      render: (_: any, record: Skill) => {
        const coreTags = getCoreTagsOnly(record.tags || []);
        return (
          <Space size={2} style={{ flexWrap: 'nowrap' }}>
            {coreTags.map(t => (
              <Tag key={t} style={{ fontSize: 11, margin: 0 }}>{t}</Tag>
            ))}
            {coreTags.length === 0 && <span className="text-xs text-gray-300">—</span>}
          </Space>
        );
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_: any, record: Skill) => (
        <Space size={2}>
          <Tooltip title="编辑">
            <Button
              type="text" size="small"
              icon={<EditOutlined />}
              onClick={() => {
                setEditingSkill(record);
                editForm.setFieldsValue({
                  name: record.name,
                  description: record.description,
                  version: record.version,
                  content: record.content || '',
                  tags: (record.tags || []).join(', '),
                });
                setEditModal(true);
              }}
            />
          </Tooltip>
          <Tooltip title="移入回收站">
            <Button
              type="text" danger size="small"
              icon={<DeleteOutlined />}
              onClick={() => handleDelete(record.id)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-xl font-semibold m-0">Skill 池</h2>
          <div className="text-xs text-gray-400 mt-1">
            全局共享 · 所有项目共用 · HR Agent 创建成员时自动按角色分配
          </div>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={fetchSkills}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setImportModal(true)}>
            导入 Skill
          </Button>
        </Space>
      </div>

      <Row gutter={16}>
        {/* 左侧：分类导航（Tabs 切换两种视图） */}
        <Col span={6}>
          <Card size="small" styles={{ body: { padding: 0 } }}>
            <Tabs
              activeKey={viewMode}
              onChange={k => {
                setViewMode(k as 'agent' | 'domain');
                setSearchText('');
              }}
              size="small"
              centered
              items={[
                {
                  key: 'agent',
                  label: <span><AppstoreOutlined /> 按角色</span>,
                  children: (
                    <div className="px-2 pb-2">
                      {/* 全部 */}
                      <div
                        className={`flex items-center justify-between px-3 py-2 rounded cursor-pointer mb-1 transition-colors ${
                          selectedCategory === 'all' ? 'bg-blue-50 border border-blue-200' : 'hover:bg-gray-50'
                        }`}
                        onClick={() => setSelectedCategory('all')}
                      >
                        <span className="text-sm font-medium">🗂️ 全部</span>
                        <Badge count={activeCount} showZero style={{ backgroundColor: '#1890ff' }} />
                      </div>
                      <Divider className="my-2" />
                      {AGENT_CATEGORIES.map(cat => {
                        const count = (grouped[cat.key] || []).filter(s => s.status !== 'deleted').length;
                        const isSelected = selectedCategory === cat.key;
                        return (
                          <div
                            key={cat.key}
                            className={`flex items-center justify-between px-3 py-2 rounded cursor-pointer mb-1 transition-colors ${
                              isSelected ? 'border' : 'hover:bg-gray-50'
                            }`}
                            style={isSelected ? { borderColor: cat.color, backgroundColor: `${cat.color}10` } : {}}
                            onClick={() => setSelectedCategory(cat.key)}
                          >
                            <span className="text-sm">
                              <span className="mr-1">{cat.emoji}</span>
                              <span style={isSelected ? { color: cat.color, fontWeight: 600 } : {}}>{cat.label}</span>
                            </span>
                            <Badge count={count} showZero style={{ backgroundColor: count > 0 ? cat.color : '#d9d9d9' }} />
                          </div>
                        );
                      })}
                    </div>
                  ),
                },
                {
                  key: 'domain',
                  label: <span><ApartmentOutlined /> 按领域</span>,
                  children: (
                    <div className="px-2 pb-2">
                      {/* 全部 */}
                      <div
                        className={`flex items-center justify-between px-3 py-2 rounded cursor-pointer mb-1 transition-colors ${
                          selectedDomain === 'all' ? 'bg-blue-50 border border-blue-200' : 'hover:bg-gray-50'
                        }`}
                        onClick={() => setSelectedDomain('all')}
                      >
                        <span className="text-sm font-medium">🗂️ 全部</span>
                        <Badge count={activeCount} showZero style={{ backgroundColor: '#1890ff' }} />
                      </div>
                      <Divider className="my-2" />
                      {Object.entries(domainGrouped).map(([domain, skills]) => {
                        const count = skills.filter(s => s.status !== 'deleted').length;
                        const isSelected = selectedDomain === domain;
                        const color = DOMAIN_COLORS[domain] || '#8c8c8c';
                        const emoji = DOMAIN_EMOJIS[domain] || '📦';
                        return (
                          <div
                            key={domain}
                            className={`flex items-center justify-between px-3 py-2 rounded cursor-pointer mb-1 transition-colors ${
                              isSelected ? 'border' : 'hover:bg-gray-50'
                            }`}
                            style={isSelected ? { borderColor: color, backgroundColor: `${color}10` } : {}}
                            onClick={() => setSelectedDomain(domain)}
                          >
                            <span className="text-sm">
                              <span className="mr-1">{emoji}</span>
                              <span style={isSelected ? { color, fontWeight: 600 } : {}}>{domain}</span>
                            </span>
                            <Badge count={count} showZero style={{ backgroundColor: count > 0 ? color : '#d9d9d9' }} />
                          </div>
                        );
                      })}
                    </div>
                  ),
                },
              ]}
            />
          </Card>
        </Col>

        {/* 右侧：Skill 列表 */}
        <Col span={18}>
          <Card
            title={
              <span>
                {viewMode === 'agent'
                  ? (selectedCategory === 'all' ? '全部 Skill' : AGENT_CATEGORIES.find(c => c.key === selectedCategory)?.label + ' Skill')
                  : (selectedDomain === 'all' ? '全部 Skill' : selectedDomain + ' · Skill')}
                <span className="text-gray-400 text-sm font-normal ml-2">（{displaySkills.length} 个）</span>
              </span>
            }
            size="small"
            extra={
              <Input
                placeholder="搜索 Skill 名称/描述/标签..."
                prefix={<SearchOutlined />}
                value={searchText}
                onChange={e => setSearchText(e.target.value)}
                style={{ width: 220 }}
                size="small"
                allowClear
              />
            }
          >
            <Table
              columns={columns}
              dataSource={displaySkills}
              rowKey="id"
              loading={loading}
              pagination={{ pageSize: 10, size: 'small' }}
              size="small"
              locale={{ emptyText: <Empty description="暂无 Skill" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            />
          </Card>
        </Col>
      </Row>

      {/* 编辑弹窗 */}
      <Modal
        title={`编辑 Skill：${editingSkill?.name || ''}`}
        open={editModal}
        onCancel={() => { setEditModal(false); setEditingSkill(null); editForm.resetFields(); }}
        onOk={() => editForm.submit()}
        okText="保存"
        cancelText="取消"
        confirmLoading={editLoading}
        width={580}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={async (values) => {
            if (!editingSkill) return;
            setEditLoading(true);
            try {
              const tagsArr = (values.tags || '')
                .split(',')
                .map((t: string) => t.trim())
                .filter(Boolean);
              await axios.patch(`${API}/skills/${editingSkill.id}`, {
                name: values.name,
                description: values.description,
                version: values.version,
                content: values.content || undefined,
                tags: tagsArr,
              });
              message.success('Skill 已更新');
              setEditModal(false);
              setEditingSkill(null);
              editForm.resetFields();
              fetchSkills();
            } catch (e: any) {
              message.error(e.response?.data?.detail || '更新失败');
            } finally {
              setEditLoading(false);
            }
          }}
        >
          <Form.Item label="Skill 名称" name="name" rules={[{ required: true, message: '请输入名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item label="描述" name="description" rules={[{ required: true, message: '请输入描述' }]}>
            <Input />
          </Form.Item>
          <Form.Item label="版本" name="version">
            <Input placeholder="1.0.0" />
          </Form.Item>
          <Form.Item
            label="Skill 内容（详细描述、使用方式、适用场景等）"
            name="content"
          >
            <Input.TextArea
              rows={8}
              placeholder="描述该 Skill 的详细能力、使用方式、适用场景、注意事项..."
              showCount
            />
          </Form.Item>
          <Form.Item
            label="标签（逗号分隔，如：pm, analysis, requirements）"
            name="tags"
          >
            <Input placeholder="pm, analysis, requirements" />
          </Form.Item>
          <div className="text-xs text-gray-400 -mt-2">
            角色标签（pm/supervisor/hr/pg/ccb/common）决定该 Skill 归属哪个 Agent 分类
          </div>
        </Form>
      </Modal>

      {/* 导入弹窗 */}
      <Modal
        title="导入 Skill"
        open={importModal}
        onCancel={() => { setImportModal(false); form.resetFields(); setIngestResult(null); setIngestTab('url'); }}
        footer={null}
        width={620}
      >
        <div className="text-xs text-gray-400 mb-3">
          支持三种方式：URL 抓取（GitHub/任意链接）、文件上传（.md/.txt）、手动填写
        </div>
        <Tabs activeKey={ingestTab} onChange={setIngestTab} size="small" items={[
          {
            key: 'url',
            label: 'URL 导入',
            children: (
              <div className="space-y-3 pt-2">
                <div>
                  <div className="text-xs text-gray-500 mb-1">Skill 文件 URL（支持 GitHub blob 链接自动转 raw）</div>
                  <Input
                    placeholder="https://github.com/user/repo/blob/main/SKILL.md"
                    value={ingestUrl}
                    onChange={e => setIngestUrl(e.target.value)}
                    allowClear
                  />
                </div>
                <Button
                  type="primary"
                  loading={ingestLoading}
                  disabled={!ingestUrl.trim()}
                  onClick={async () => {
                    setIngestLoading(true); setIngestResult(null);
                    try {
                      const r = await authFetch(`${API}/skills/ingest`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ url: ingestUrl.trim(), auto_confirm: false }),
                      });
                      setIngestResult(await r.json());
                    } finally { setIngestLoading(false); }
                  }}
                >解析</Button>
              </div>
            ),
          },
          {
            key: 'file',
            label: '文件上传',
            children: (
              <div className="space-y-3 pt-2">
                <div className="text-xs text-gray-500 mb-1">上传 .md 或 .txt 格式的 Skill 文件</div>
                <input
                  type="file"
                  accept=".md,.txt"
                  className="text-sm"
                  onChange={async e => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    setIngestLoading(true); setIngestResult(null);
                    const fd = new FormData();
                    fd.append('file', file);
                    fd.append('auto_confirm', 'false');
                    try {
                      const r = await authFetch(`${API}/skills/ingest/file`, { method: 'POST', body: fd });
                      setIngestResult(await r.json());
                    } finally { setIngestLoading(false); }
                  }}
                />
                {ingestLoading && <div className="text-xs text-blue-500">解析中...</div>}
              </div>
            ),
          },
          {
            key: 'manual',
            label: '手动填写',
            children: (
              <Form form={form} layout="vertical" onFinish={handleImport} className="pt-2">
                <Form.Item label="Skill 名称" name="name" rules={[{ required: true }]}>
                  <Input placeholder="例如：React-TypeScript" />
                </Form.Item>
                <Form.Item label="描述" name="description" rules={[{ required: true }]}>
                  <Input placeholder="简要描述该 Skill 的用途" />
                </Form.Item>
                <Form.Item label="版本" name="version">
                  <Input placeholder="1.0.0" />
                </Form.Item>
                <Form.Item
                  label="内容（详细描述，至少 100 字）"
                  name="content"
                  rules={[{ required: true }, { min: 100, message: '内容至少 100 字' }]}
                >
                  <Input.TextArea rows={6} placeholder="描述该 Skill 的详细能力、使用方式、适用场景..." />
                </Form.Item>
                <Form.Item>
                  <Space>
                    <Button type="primary" htmlType="submit">导入</Button>
                    <Button onClick={() => { setImportModal(false); form.resetFields(); }}>取消</Button>
                  </Space>
                </Form.Item>
              </Form>
            ),
          },
        ]} />

        {/* 解析结果 + 确认入库 */}
        {ingestResult && (
          <div className="mt-4 border rounded p-3 bg-gray-50 text-sm space-y-2">
            {ingestResult.success === false ? (
              <div className="text-red-500">❌ 解析失败：{ingestResult.error}</div>
            ) : (
              <>
                <div className="font-medium text-green-700">✅ 解析成功（{ingestResult.parse_method === 'frontmatter' ? 'Frontmatter 格式' : '推断格式'}）</div>
                <div><span className="text-gray-500">名称：</span>{ingestResult.skill?.name}</div>
                <div><span className="text-gray-500">描述：</span>{ingestResult.skill?.description}</div>
                <div><span className="text-gray-500">适用 Agent：</span>{(ingestResult.skill?.for_agents || []).join('、') || '通用'}</div>
                <div><span className="text-gray-500">标签：</span>{(ingestResult.skill?.tags || []).join('、')}</div>
                {ingestResult.classification && (
                  <div><span className="text-gray-500">能力类型：</span>{ingestResult.classification.capability_type?.type} — {ingestResult.classification.capability_type?.reason}</div>
                )}
                {(ingestResult.warnings || []).length > 0 && (
                  <div className="text-yellow-600 text-xs">⚠️ {ingestResult.warnings.join('；')}</div>
                )}
                <div className="pt-2">
                  <Button
                    type="primary"
                    size="small"
                    loading={confirmLoading}
                    onClick={async () => {
                      setConfirmLoading(true);
                      try {
                        await authFetch(`${API}/skills/confirm`, {
                          method: 'POST',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ skill_id: ingestResult.skill_id }),
                        });
                        setImportModal(false);
                        setIngestResult(null);
                        setIngestUrl('');
                        fetchSkills();
                      } finally { setConfirmLoading(false); }
                    }}
                  >确认入库</Button>
                  <Button size="small" className="ml-2" onClick={() => setIngestResult(null)}>重新解析</Button>
                </div>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
};

export default SkillPool;
