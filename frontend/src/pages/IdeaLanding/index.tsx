/**
 * IdeaLanding — 想法落地工作台（主入口）
 *
 * 整合三阶段视图：
 * - Phase 1: StressTest — 压力测试对话
 * - Phase 2: RequirementDoc — 需求文档预览
 * - Phase 3: Optimization — 优化补充与交付
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Button, Input, Tag, Tooltip, Modal, Select,
  Dropdown, Badge, Empty, Spin, Divider,
} from 'antd';
import type { MenuProps } from 'antd';
import {
  PlusOutlined, PushpinOutlined, PushpinFilled, TagsOutlined,
  DeleteOutlined, EditOutlined, FileTextOutlined,
  RocketOutlined, BulbOutlined, CheckCircleOutlined,
  MoreOutlined, ArrowRightOutlined,
  DownloadOutlined, CopyOutlined,
} from '@ant-design/icons';
import { message as antMessage } from 'antd';
import { apiClient } from '../../services/api';

import {
  PHASES, CATEGORIES, PHASE_COLORS,
  fmtTime, extractAllTags,
} from './types';
import type { ConvListItem, ChatMessage, ConvDetail } from './types';
import StressTest from './StressTest';
import RequirementDoc from './RequirementDoc';
import Optimization from './Optimization';

const { TextArea } = Input;

// ─── 对话列表项组件 ──────────────────────────────────────────────────────────

const ConvItem: React.FC<{
  conv: ConvListItem;
  active: boolean;
  onClick: () => void;
  onPin: (pinned: boolean) => void;
  onDelete: () => void;
  onEdit: () => void;
}> = ({ conv, active, onClick, onPin, onDelete, onEdit }) => {
  const menuItems: MenuProps['items'] = [
    {
      key: 'pin',
      icon: conv.pinned ? <PushpinFilled /> : <PushpinOutlined />,
      label: conv.pinned ? '取消置顶' : '置顶',
      onClick: (e) => { e.domEvent.stopPropagation(); onPin(!conv.pinned); },
    },
    {
      key: 'edit',
      icon: <EditOutlined />,
      label: '编辑标题/标签',
      onClick: (e) => { e.domEvent.stopPropagation(); onEdit(); },
    },
    { type: 'divider' },
    {
      key: 'delete',
      icon: <DeleteOutlined />,
      label: '删除',
      danger: true,
      onClick: (e) => { e.domEvent.stopPropagation(); onDelete(); },
    },
  ];

  return (
    <div
      onClick={onClick}
      style={{
        padding: '10px 12px',
        cursor: 'pointer',
        borderRadius: 6,
        marginBottom: 2,
        background: active ? '#e6f4ff' : 'transparent',
        border: active ? '1px solid #91caff' : '1px solid transparent',
        transition: 'all 0.15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        {conv.pinned && <PushpinFilled style={{ fontSize: 10, color: '#faad14', flexShrink: 0 }} />}
        <span style={{
          flex: 1, fontWeight: 500, fontSize: 13, color: '#1f1f1f',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {conv.title || '新对话'}
        </span>
        <Tag
          color={PHASE_COLORS[conv.current_phase] || 'default'}
          style={{ fontSize: 10, padding: '0 4px', lineHeight: '16px', flexShrink: 0 }}
        >
          {PHASES.find(p => p.key === conv.current_phase)?.icon} {conv.phase_name}
        </Tag>
        <Dropdown menu={{ items: menuItems }} trigger={['click']}>
          <Button
            type="text" size="small"
            icon={<MoreOutlined />}
            style={{ padding: 0, height: 20, width: 20, flexShrink: 0 }}
            onClick={e => e.stopPropagation()}
          />
        </Dropdown>
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginTop: 2 }}>
        <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
          {conv.tags.map(tag => (
            <Tag key={tag} style={{ fontSize: 10, padding: '0 4px', lineHeight: '16px', margin: 0 }}>{tag}</Tag>
          ))}
          {conv.has_requirements_doc && (
            <Tag color="green" style={{ fontSize: 10, padding: '0 4px', lineHeight: '16px', margin: 0 }}>
              <FileTextOutlined /> 需求文档
            </Tag>
          )}
        </div>
        <span style={{ fontSize: 10, color: '#bfbfbf', flexShrink: 0 }}>{fmtTime(conv.updated_at)}</span>
      </div>
      {conv.last_message && (
        <div style={{ marginTop: 4, fontSize: 11, color: '#8c8c8c', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {conv.last_message}
        </div>
      )}
    </div>
  );
};

// ─── 文档面板（切换 RequirementDoc / Optimization）───────────────────────────

const DocPanel: React.FC<{
  requirementsDoc: string;
  currentPhase: number;
  convTitle: string;
  onClose: () => void;
}> = ({ requirementsDoc, currentPhase, convTitle, onClose }) => {
  const [activeTab, setActiveTab] = useState('doc');

  return (
    <div style={{
      width: 360, flexShrink: 0, background: '#fff',
      borderLeft: '1px solid #f0f0f0',
      display: 'flex', flexDirection: 'column', overflow: 'hidden',
    }}>
      {/* 头部 */}
      <div style={{
        padding: '10px 14px', borderBottom: '1px solid #f0f0f0',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
      }}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>📵 文档 & 优化</span>
        <Button type="text" size="small" onClick={onClose} style={{ color: '#8c8c8c' }}>收起</Button>
      </div>

      {/* Tab 切换 */}
      <div style={{
        display: 'flex', borderBottom: '1px solid #f0f0f0', flexShrink: 0,
        padding: '0 14px', background: '#fff',
      }}>
        <button
          onClick={() => setActiveTab('doc')}
          style={{
            padding: '8px 12px', fontSize: 12, cursor: 'pointer',
            background: 'none', border: 'none', outline: 'none',
            borderBottom: activeTab === 'doc' ? '2px solid #1677ff' : '2px solid transparent',
            color: activeTab === 'doc' ? '#1677ff' : '#595959',
            fontWeight: activeTab === 'doc' ? 600 : 400,
            marginBottom: -1, lineHeight: '20px',
          }}
        >
          <FileTextOutlined /> 需求文档
        </button>
        <button
          onClick={() => setActiveTab('optimize')}
          style={{
            padding: '8px 12px', fontSize: 12, cursor: 'pointer',
            background: 'none', border: 'none', outline: 'none',
            borderBottom: activeTab === 'optimize' ? '2px solid #1677ff' : '2px solid transparent',
            color: activeTab === 'optimize' ? '#1677ff' : '#595959',
            fontWeight: activeTab === 'optimize' ? 600 : 400,
            marginBottom: -1, lineHeight: '20px',
          }}
        >
          <BulbOutlined style={{ color: currentPhase >= 3 ? '#52c41a' : '#8c8c8c' }} /> 优化建议
          {currentPhase >= 3 && <Badge dot style={{ marginLeft: 4 }} />}
        </button>
      </div>

      {/* Tab 内容 */}
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
        {activeTab === 'doc' ? (
          <RequirementDoc requirementsDoc={requirementsDoc} convTitle={convTitle} />
        ) : (
          <Optimization requirementsDoc={requirementsDoc} currentPhase={currentPhase} convTitle={convTitle} />
        )}
      </div>
    </div>
  );
};

// ─── 主页面组件 ──────────────────────────────────────────────────────────────

const IdeaLanding: React.FC = () => {
  // 对话列表
  const [convList, setConvList] = useState<ConvListItem[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [filterCategory, setFilterCategory] = useState<string>('');
  const [filterTag, setFilterTag] = useState<string>('');
  const [allTags, setAllTags] = useState<string[]>([]);

  // 当前对话
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [convDetail, setConvDetail] = useState<ConvDetail | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [contextSummary, setContextSummary] = useState('');
  const [currentPhase, setCurrentPhase] = useState(1);
  const [requirementsDoc, setRequirementsDoc] = useState('');
  const [showDocPanel, setShowDocPanel] = useState(false);
  const [initialLoadDone, setInitialLoadDone] = useState(false);

  // 输入
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  // 弹窗
  const [editModal, setEditModal] = useState<{ open: boolean; conv: ConvListItem | null }>({ open: false, conv: null });
  const [editTitle, setEditTitle] = useState('');
  const [editTags, setEditTags] = useState<string[]>([]);
  const [editCategory, setEditCategory] = useState('默认');
  const [editTagInput, setEditTagInput] = useState('');
  const [generatingDoc, setGeneratingDoc] = useState(false);

  const msgBoxRef = useRef<HTMLDivElement>(null);

  // ── 加载对话列表 ────────────────────────────────────────────────────────
  const loadConvList = useCallback(async (autoRestoreActive = false) => {
    setListLoading(true);
    try {
      const params: Record<string, string> = {};
      if (filterCategory) params.category = filterCategory;
      if (filterTag) params.tag = filterTag;
      const res: any = await apiClient.get('/idea-landing/conversations', { params });
      const convs: ConvListItem[] = res.conversations || [];
      setConvList(convs);
      setAllTags(extractAllTags(convs));

      if (autoRestoreActive && !activeConvId) {
        const serverActiveId: string | null = res.active_conv_id || null;
        const toRestore = serverActiveId || (convs.length > 0 ? convs[0].conv_id : null);
        if (toRestore) {
          setActiveConvId(toRestore);
          await loadConvDetailInternal(toRestore);
        }
      }
    } catch {
      // 静默
    } finally {
      setListLoading(false);
    }
  }, [filterCategory, filterTag, activeConvId]);

  // ── 加载对话详情（内部版，不依赖 state）──────────────────────────────────
  const loadConvDetailInternal = async (convId: string) => {
    try {
      const d: ConvDetail = await apiClient.get(`/idea-landing/conversations/${convId}`) as any;
      setConvDetail(d);
      setMessages(
        d.messages.map((m, i) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
          ts: Date.now() + i,
        }))
      );
      setContextSummary(d.context_summary || '');
      setCurrentPhase(d.current_phase || 1);
      setRequirementsDoc(d.requirements_doc || '');
      if (d.requirements_doc) setShowDocPanel(true);
    } catch {
      // 静默
    }
  };

  // ── 首次挂载 ────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!initialLoadDone) {
      setInitialLoadDone(true);
      loadConvList(true);
    }
  }, []);

  // ── 过滤条件变化时重新加载 ──────────────────────────────────────────────
  useEffect(() => {
    if (initialLoadDone) {
      loadConvList(false);
    }
  }, [filterCategory, filterTag]);

  // ── 滚动到底部 ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (msgBoxRef.current) {
      msgBoxRef.current.scrollTop = msgBoxRef.current.scrollHeight;
    }
  }, [messages, sending]);

  // ── 加载对话详情（公开版）───────────────────────────────────────────────
  const loadConvDetail = async (convId: string) => {
    try {
      const d: ConvDetail = await apiClient.get(`/idea-landing/conversations/${convId}`) as any;
      setConvDetail(d);
      setMessages(
        d.messages.map((m, i) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
          ts: Date.now() + i,
        }))
      );
      setContextSummary(d.context_summary || '');
      setCurrentPhase(d.current_phase || 1);
      setRequirementsDoc(d.requirements_doc || '');
      if (d.requirements_doc) setShowDocPanel(true);
      else setShowDocPanel(false);
    } catch {
      antMessage.error('加载对话失败');
    }
  };

  const handleSelectConv = async (convId: string) => {
    if (convId === activeConvId) return;
    setActiveConvId(convId);
    setMessages([]);
    setConvDetail(null);
    setRequirementsDoc('');
    setShowDocPanel(false);
    await loadConvDetail(convId);
    await apiClient.post(`/idea-landing/conversations/${convId}/switch`).catch(() => {});
  };

  // ── 新建对话 ────────────────────────────────────────────────────────────
  const handleNewConversation = async () => {
    try {
      const res: any = await apiClient.post('/idea-landing/conversations', {
        title: '新对话',
        tags: [],
        category: '默认',
      });
      const newConvId = res.conv_id;
      setActiveConvId(newConvId);
      setMessages([]);
      setContextSummary('');
      setCurrentPhase(1);
      setRequirementsDoc('');
      setShowDocPanel(false);
      setConvDetail(null);
      await loadConvList(false);
    } catch {
      antMessage.error('创建对话失败');
    }
  };

  // ── 发送消息 ────────────────────────────────────────────────────────────
  const handleSend = async () => {
    if (!input.trim() || sending) return;
    const text = input.trim();
    setInput('');

    let convId = activeConvId;
    if (!convId) {
      try {
        const res: any = await apiClient.post('/idea-landing/conversations', {
          title: text.slice(0, 30),
          tags: [], category: '默认',
        });
        convId = res.conv_id;
        setActiveConvId(convId);
      } catch {
        antMessage.error('创建对话失败');
        return;
      }
    }

    const userMsg: ChatMessage = { role: 'user', content: text, ts: Date.now() };
    const newMsgs = [...messages, userMsg];
    setMessages(newMsgs);
    setSending(true);

    try {
      const res: any = await apiClient.post('/idea-landing/chat', {
        message: text,
        conv_id: convId,
        history: messages.slice(-12).map(m => ({ role: m.role, content: m.content })),
        context_summary: contextSummary,
      });

      const reply = res.reply || '';
      const finalMsgs: ChatMessage[] = [...newMsgs, { role: 'assistant', content: reply, ts: Date.now() }];
      setMessages(finalMsgs);

      if (res.summary) setContextSummary(res.summary);
      if (res.current_phase) setCurrentPhase(res.current_phase);
      if (res.requirements_doc) {
        setRequirementsDoc(res.requirements_doc);
        setShowDocPanel(true);
      }
      if (res.phase_advanced) {
        antMessage.success(`阶段已推进：${res.phase_name}`);
      }

      await loadConvList(false);
    } catch (e: any) {
      antMessage.error(e.response?.data?.detail || '发送失败');
    } finally {
      setSending(false);
    }
  };

  // ── 手动推进阶段 ────────────────────────────────────────────────────────
  const handleAdvancePhase = async () => {
    if (!activeConvId) return;
    try {
      const res: any = await apiClient.post(`/idea-landing/conversations/${activeConvId}/advance-phase`);
      if (res.success) {
        setCurrentPhase(res.new_phase);
        antMessage.success(`已推进到阶段 ${res.new_phase}：${res.phase_name}`);
        if (res.new_phase === 2 && !requirementsDoc) {
          antMessage.info('正在自动生成需求文档，请稍候..');
          await handleGenerateDoc();
        }
        await loadConvList(false);
      } else {
        antMessage.warning(res.error || '已是最后阶段');
      }
    } catch {
      antMessage.error('推进失败');
    }
  };

  // ── 生成需求文档 ────────────────────────────────────────────────────────
  const handleGenerateDoc = async () => {
    if (!activeConvId) return;
    setGeneratingDoc(true);
    antMessage.loading({ content: '正在整合压力测试内容生成需求文档...', key: 'gen-doc', duration: 0 });
    try {
      const res: any = await apiClient.post(`/idea-landing/conversations/${activeConvId}/generate-doc`);
      if (res.success) {
        setRequirementsDoc(res.requirements_doc);
        setShowDocPanel(true);
        antMessage.success({ content: '需求文档已生成', key: 'gen-doc' });
        await loadConvList(false);
      } else {
        antMessage.error({ content: res.error || '生成失败', key: 'gen-doc' });
      }
    } catch {
      antMessage.error({ content: '生成失败', key: 'gen-doc' });
    } finally {
      setGeneratingDoc(false);
    }
  };

  // ── 置顶 ────────────────────────────────────────────────────────────────
  const handlePin = async (convId: string, pinned: boolean) => {
    await apiClient.post(`/idea-landing/conversations/${convId}/pin`, { pinned }).catch(() => {});
    await loadConvList(false);
  };

  // ── 删除 ────────────────────────────────────────────────────────────────
  const handleDelete = async (convId: string) => {
    try {
      await apiClient.delete(`/idea-landing/conversations/${convId}`);
      if (activeConvId === convId) {
        setActiveConvId(null);
        setMessages([]);
        setConvDetail(null);
        setRequirementsDoc('');
        setShowDocPanel(false);
      }
      await loadConvList(false);
      antMessage.success('已删除');
    } catch {
      antMessage.error('删除失败');
    }
  };

  // ── 编辑元数据 ──────────────────────────────────────────────────────────
  const handleOpenEdit = (conv: ConvListItem) => {
    setEditModal({ open: true, conv });
    setEditTitle(conv.title);
    setEditTags(conv.tags);
    setEditCategory(conv.category || '默认');
    setEditTagInput('');
  };

  const handleSaveEdit = async () => {
    const { conv } = editModal;
    if (!conv) return;
    try {
      await apiClient.patch(`/idea-landing/conversations/${conv.conv_id}`, {
        title: editTitle,
        tags: editTags,
        category: editCategory,
      });
      setEditModal({ open: false, conv: null });
      await loadConvList(false);
      if (conv.conv_id === activeConvId) {
        setConvDetail(prev => prev ? { ...prev, title: editTitle, tags: editTags, category: editCategory } : null);
      }
    } catch {
      antMessage.error('保存失败');
    }
  };

  // ── 阶段状态栏 ──────────────────────────────────────────────────────────
  const renderPhaseBar = () => (
    <div style={{
      display: 'flex', gap: 6, padding: '8px 16px',
      background: '#fafafa', borderBottom: '1px solid #f0f0f0',
      flexShrink: 0, flexWrap: 'wrap',
    }}>
      {PHASES.map((p) => {
        const isDone = convDetail?.phase_completed?.[String(p.key)] || false;
        const isCurrent = currentPhase === p.key;
        return (
          <div key={p.key} style={{
            display: 'flex', alignItems: 'center', gap: 4, padding: '3px 10px',
            borderRadius: 16, fontSize: 12, fontWeight: isCurrent ? 600 : 400,
            background: isCurrent ? p.color + '15' : 'transparent',
            border: `1px solid ${isCurrent ? p.color : '#e8e8e8'}`,
            color: isDone ? '#52c41a' : isCurrent ? p.color : '#8c8c8c',
          }}>
            {isDone ? <CheckCircleOutlined /> : <span>{p.icon}</span>}
            {p.label}
            {isCurrent && (
              <Tag
                color={p.color === '#fa8c16' ? 'orange' : p.color === '#1677ff' ? 'blue' : 'green'}
                style={{ fontSize: 10, margin: 0, padding: '0 4px', lineHeight: '16px' }}
              >
                进行中
              </Tag>
            )}
          </div>
        );
      })}
      <div style={{ flex: 1 }} />
      {activeConvId && currentPhase < 3 && (
        <Tooltip title={`确认当前阶段完成，推进到「${PHASES[currentPhase]?.label || '下一阶段'}」`}>
          <Button size="small" icon={<ArrowRightOutlined />} onClick={handleAdvancePhase} style={{ fontSize: 11 }}>
            推进阶段
          </Button>
        </Tooltip>
      )}
      {activeConvId && currentPhase >= 2 && (
        <Button
          size="small" icon={<FileTextOutlined />}
          loading={generatingDoc}
          onClick={handleGenerateDoc}
          style={{ fontSize: 11 }}
          type={requirementsDoc ? 'default' : 'primary'}
        >
          {requirementsDoc ? '重新生成文档' : '生成需求文档'}
        </Button>
      )}
      {requirementsDoc && (
        <Button
          size="small"
          icon={<FileTextOutlined />}
          onClick={() => setShowDocPanel(v => !v)}
          type={showDocPanel ? 'primary' : 'default'}
          style={{ fontSize: 11 }}
        >
          {showDocPanel ? '收起' : '查看文档'}
        </Button>
      )}
    </div>
  );

  // ── 编辑弹窗 ────────────────────────────────────────────────────────────
  const renderEditModal = () => (
    <Modal
      title="编辑对话信息"
      open={editModal.open}
      onOk={handleSaveEdit}
      onCancel={() => setEditModal({ open: false, conv: null })}
      okText="保存"
      cancelText="取消"
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>标题</div>
          <Input value={editTitle} onChange={e => setEditTitle(e.target.value)} placeholder="对话标题" />
        </div>
        <div>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>分类</div>
          <Select value={editCategory} onChange={setEditCategory} style={{ width: '100%' }}>
            {CATEGORIES.map(c => <Select.Option key={c} value={c}>{c}</Select.Option>)}
          </Select>
        </div>
        <div>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
            标签
            {allTags.length > 0 && (
              <span style={{ marginLeft: 6, color: '#bfbfbf', fontWeight: 400 }}>已有标签快速选择：</span>
            )}
          </div>
          {allTags.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
              {allTags.map(tag => (
                <Tag
                  key={tag}
                  style={{
                    cursor: 'pointer', fontSize: 11,
                    background: editTags.includes(tag) ? '#e6f4ff' : '#fafafa',
                    borderColor: editTags.includes(tag) ? '#91caff' : '#d9d9d9',
                    color: editTags.includes(tag) ? '#1677ff' : '#595959',
                  }}
                  onClick={() => {
                    if (editTags.includes(tag)) {
                      setEditTags(prev => prev.filter(t => t !== tag));
                    } else {
                      setEditTags(prev => [...prev, tag]);
                    }
                  }}
                >
                  {editTags.includes(tag) ? '✅' : ''}{tag}
                </Tag>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 6 }}>
            {editTags.map(tag => (
              <Tag key={tag} closable onClose={() => setEditTags(prev => prev.filter(t => t !== tag))}>
                {tag}
              </Tag>
            ))}
          </div>
          <Input
            size="small"
            value={editTagInput}
            onChange={e => setEditTagInput(e.target.value)}
            placeholder="输入新标签后按 Enter 添加"
            onKeyDown={e => {
              if (e.key === 'Enter' && editTagInput.trim()) {
                setEditTags(prev => [...new Set([...prev, editTagInput.trim()])]);
                setEditTagInput('');
              }
            }}
            suffix={
              <Button type="text" size="small" icon={<TagsOutlined />} onClick={() => {
                if (editTagInput.trim()) {
                  setEditTags(prev => [...new Set([...prev, editTagInput.trim()])]);
                  setEditTagInput('');
                }
              }} />
            }
          />
        </div>
      </div>
    </Modal>
  );

  // ── 渲染 ────────────────────────────────────────────────────────────────
  return (
    <div className="metis-idea-workspace" style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: '#f5f5f5' }}>
      {/* 左侧：对话列表 */}
      <div className="metis-idea-sidebar" style={{
        width: 260, flexShrink: 0, background: '#fff',
        borderRight: '1px solid #f0f0f0',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* 头部 */}
        <div className="metis-idea-sidebar-header" style={{ padding: '14px 12px 10px', borderBottom: '1px solid #f0f0f0', flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontWeight: 700, fontSize: 15, color: '#1f1f1f' }}>
              <BulbOutlined style={{ color: '#faad14', marginRight: 6 }} />
              想法落地
            </span>
            <Button type="primary" size="small" icon={<PlusOutlined />} onClick={handleNewConversation}>
              新建
            </Button>
          </div>
          {/* 分类过滤 */}
          <div style={{ marginBottom: 4 }}>
            <Select
              size="small" style={{ width: '100%' }} placeholder="按分类筛选"
              allowClear value={filterCategory || undefined}
              onChange={v => setFilterCategory(v || '')}
            >
              {CATEGORIES.map(c => <Select.Option key={c} value={c}>{c}</Select.Option>)}
            </Select>
          </div>
          {/* 标签过滤 */}
          <Select
            size="small" style={{ width: '100%' }} placeholder="按标签筛选"
            allowClear value={filterTag || undefined}
            onChange={v => setFilterTag(v || '')}
            showSearch
            filterOption={(input, option) =>
              (option?.value as string || '').toLowerCase().includes(input.toLowerCase())
            }
            notFoundContent={<span style={{ fontSize: 12, color: '#bfbfbf' }}>暂无标签</span>}
          >
            {allTags.map(tag => <Select.Option key={tag} value={tag}>{tag}</Select.Option>)}
          </Select>
        </div>

        {/* 对话列表 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px 8px' }}>
          {listLoading ? (
            <div style={{ textAlign: 'center', padding: 24 }}><Spin size="small" /></div>
          ) : convList.length === 0 ? (
            <div style={{ textAlign: 'center', color: '#bfbfbf', fontSize: 12, padding: 24 }}>
              <BulbOutlined style={{ fontSize: 24, display: 'block', marginBottom: 8 }} />
              暂无对话，点击「新建」开始
            </div>
          ) : (
            convList.map(conv => (
              <ConvItem
                key={conv.conv_id}
                conv={conv}
                active={conv.conv_id === activeConvId}
                onClick={() => handleSelectConv(conv.conv_id)}
                onPin={(pinned) => handlePin(conv.conv_id, pinned)}
                onDelete={() => handleDelete(conv.conv_id)}
                onEdit={() => handleOpenEdit(conv)}
              />
            ))
          )}
        </div>

        {/* 阶段说明 */}
        <div style={{ padding: '10px 12px', borderTop: '1px solid #f0f0f0', flexShrink: 0 }}>
          <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 6 }}>工作流程</div>
          {PHASES.map(p => (
            <div key={p.key} style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 4 }}>
              <span style={{ fontSize: 12 }}>{p.icon}</span>
              <div>
                <span style={{ fontSize: 11, fontWeight: 600, color: '#262626' }}>{p.label}</span>
                <div style={{ fontSize: 10, color: '#8c8c8c' }}>{p.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 右侧：对话区 + 文档面板 */}
      <div className="metis-idea-main" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {/* 顶部标题栏 */}
        <div className="metis-idea-header" style={{
          padding: '10px 20px', background: '#fff', borderBottom: '1px solid #f0f0f0',
          display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0,
        }}>
          {activeConvId && convDetail ? (
            <>
              <span style={{ fontWeight: 600, fontSize: 15, color: '#1f1f1f' }}>
                {convDetail.title || '新对话'}
              </span>
              <Tag color={PHASE_COLORS[currentPhase] || 'default'} style={{ fontSize: 12 }}>
                {PHASES.find(p => p.key === currentPhase)?.icon} 阶段 {currentPhase}：{PHASES.find(p => p.key === currentPhase)?.label}
              </Tag>
              {convDetail.tags.map(tag => <Tag key={tag} style={{ fontSize: 11 }}>{tag}</Tag>)}
              {convDetail.category && convDetail.category !== '默认' && (
                <Tag color="purple" style={{ fontSize: 11 }}>{convDetail.category}</Tag>
              )}
            </>
          ) : (
            <span style={{ fontSize: 14, color: '#8c8c8c' }}>
              <BulbOutlined style={{ color: '#faad14', marginRight: 6 }} />
              选择或新建一个对话
            </span>
          )}
        </div>

        {/* 阶段进度栏 */}
        {(activeConvId || messages.length > 0) && renderPhaseBar()}

        {/* 内容区：聊天 + 文档 */}
        <div className="metis-idea-content" style={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
          <StressTest
            messages={messages}
            sending={sending}
            input={input}
            onInputChange={setInput}
            onSend={handleSend}
            currentPhase={currentPhase}
            requirementsDoc={requirementsDoc}
            activeConvId={activeConvId}
            convDetail={convDetail}
            msgBoxRef={msgBoxRef}
          />
          {showDocPanel && requirementsDoc && (
            <DocPanel
              requirementsDoc={requirementsDoc}
              currentPhase={currentPhase}
              convTitle={convDetail?.title || '需求文档'}
              onClose={() => setShowDocPanel(false)}
            />
          )}
        </div>
      </div>

      {/* 编辑弹窗 */}
      {renderEditModal()}
    </div>
  );
};

export default IdeaLanding;
