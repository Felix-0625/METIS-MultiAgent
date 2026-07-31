/**
 * 系统设置页面
 * - 默认 API 配置（支持主流模型快速选择 + 完全自定义）
 * - 本地数据持久化（手动保存 / 快照）
 * - Gitee 仓库绑定与手动推送
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Form, Input, Button, Space, message, Divider, Tabs,
  Tag, Alert, Row, Col, InputNumber, Slider, Typography, Select,
} from 'antd';
import {
  SaveOutlined, CloudUploadOutlined, CloudDownloadOutlined, CodeOutlined,
  ApiOutlined, GithubOutlined, DatabaseOutlined, HistoryOutlined,
  CheckCircleOutlined, WarningOutlined, ThunderboltOutlined, PlayCircleOutlined,
  FolderOpenOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';
import { apiClient } from '../services/api';
import IdeIntegration from './IdeIntegration';

const { Text } = Typography;
const { Option, OptGroup } = Select;
const API = API_BASE_URL;
const ARCHIVE_DIR_KEY = 'metis_archive_dir';

// ─── 主流模型预设 ─────────────────────────────────────────────────────────────
interface ModelPreset {
  label: string;
  model: string;
  api_base: string;
  note?: string;
}

const MODEL_PRESETS: Record<string, ModelPreset[]> = {
  'OpenAI / ChatGPT': [
    { label: 'GPT-5.5 Pro（推荐）', model: 'gpt-5.5-pro', api_base: 'https://api.openai.com/v1', note: '旗舰顶配/110万Token' },
    { label: 'GPT-5.5 Instant', model: 'gpt-5.5-instant', api_base: 'https://api.openai.com/v1', note: '极速日常' },
    { label: 'GPT-5.5 Nano', model: 'gpt-5.5-nano', api_base: 'https://api.openai.com/v1', note: '低成本批量' },
    { label: 'GPT-4o', model: 'gpt-4o', api_base: 'https://api.openai.com/v1', note: '经典多模态' },
    { label: 'GPT-4o mini', model: 'gpt-4o-mini', api_base: 'https://api.openai.com/v1', note: '高性价比' },
    { label: 'GPT-3.5 Turbo', model: 'gpt-3.5-turbo', api_base: 'https://api.openai.com/v1', note: '入门低价' },
  ],
  'Anthropic (Claude)': [
    { label: 'Claude Opus 4.8（推荐）', model: 'claude-opus-4-20250514', api_base: 'https://api.anthropic.com/v1', note: '旗舰/100万Token/深度推理' },
    { label: 'Claude Sonnet 4.6', model: 'claude-sonnet-4-20250514', api_base: 'https://api.anthropic.com/v1', note: '均衡主力/代码多模态' },
    { label: 'Claude Sonnet 4.5', model: 'claude-sonnet-4-20241022', api_base: 'https://api.anthropic.com/v1', note: '企业通用/200K上下文' },
  ],
  'Google (Gemini)': [
    { label: 'Gemini 3.5 Pro（推荐）', model: 'gemini-3.5-pro', api_base: 'https://generativelanguage.googleapis.com/v1beta/openai', note: '旗舰/100万Token/多模态' },
    { label: 'Gemini 3.5 Flash', model: 'gemini-3.5-flash', api_base: 'https://generativelanguage.googleapis.com/v1beta/openai', note: '高速低价/实时语音' },
    { label: 'Gemini 3.1 Ultra', model: 'gemini-3.1-ultra', api_base: 'https://generativelanguage.googleapis.com/v1beta/openai', note: '私有化部署' },
  ],
  'xAI (Grok)': [
    { label: 'Grok 4.3（推荐）', model: 'grok-4.3', api_base: 'https://api.x.ai/v1', note: '旗舰/百万上下文/实时联网' },
    { label: 'Grok Build 0.1', model: 'grok-build-0.1', api_base: 'https://api.x.ai/v1', note: '轻量低价/基础对话' },
  ],
  '深度求索 (DeepSeek)': [
    { label: 'DeepSeek V4-Pro（推荐）', model: 'deepseek-v4-pro', api_base: 'https://api.deepseek.com', note: '旗舰/100万上下文/数学代码' },
    { label: 'DeepSeek V4-Flash', model: 'deepseek-v4-flash', api_base: 'https://api.deepseek.com', note: '高吞吐企业API' },
  ],
  '阿里通义 (Qwen)': [
    { label: 'Qwen3.7-Max（推荐）', model: 'qwen3.7-max', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '旗舰/128K上下文/深度推理' },
    { label: 'Qwen3.7-Plus', model: 'qwen3.7-plus', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '企业均衡主力/32K' },
    { label: 'Qwen3.7-Flash', model: 'qwen3.7-flash', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '极速低价/8K' },
    { label: 'Qwen-VL（视觉）', model: 'qwen-vl-max', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '多模态视觉' },
    { label: 'Qwen-Audio（语音）', model: 'qwen-audio', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '语音识别' },
    { label: 'Qwen-Code（代码）', model: 'qwen-coder-plus', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '代码专项' },
    { label: 'Qwen 嵌入模型', model: 'text-embedding-v3', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', note: '向量嵌入' },
  ],
  '月之暗面 (Kimi)': [
    { label: 'Kimi K2.6（推荐）', model: 'kimi-k2.6', api_base: 'https://api.moonshot.cn/v1', note: '200万Token/超长文本' },
    { label: 'Kimi K2.6 Code', model: 'kimi-k2.6-code', api_base: 'https://api.moonshot.cn/v1', note: '代码专项/多文件重构' },
  ],
  '智谱AI (GLM)': [
    { label: 'GLM-5.2 Ultra（推荐）', model: 'glm-5.2-ultra', api_base: 'https://open.bigmodel.cn/api/paas/v4', note: '旗舰/100万Token/长Agent' },
    { label: 'GLM-5.2 Standard', model: 'glm-5.2-standard', api_base: 'https://open.bigmodel.cn/api/paas/v4', note: '均衡通用/32K' },
  ],
  'MiniMax': [
    { label: 'MiniMax M3 Ultra（推荐）', model: 'MiniMax-M3-Ultra', api_base: 'https://api.minimax.chat/v1', note: '多模态旗舰/100万Token' },
    { label: 'MiniMax M3 Flash', model: 'MiniMax-M3-Flash', api_base: 'https://api.minimax.chat/v1', note: '高并发极速版' },
    { label: 'MiniMax M2.7', model: 'MiniMax-M2.7', api_base: 'https://api.minimax.chat/v1', note: '纯文本旗舰/200K上下文' },
    { label: 'MiniMax M2.7 Highspeed', model: 'MiniMax-M2.7-highspeed', api_base: 'https://api.minimax.chat/v1', note: '极速/实时对话' },
  ],
  '本地 Ollama（免费）': [
    { label: 'Qwen2.5 7B', model: 'qwen2.5:7b', api_base: 'http://localhost:11434/v1', note: '推荐' },
    { label: 'Qwen2.5 14B', model: 'qwen2.5:14b', api_base: 'http://localhost:11434/v1' },
    { label: 'Llama 3.2 3B', model: 'llama3.2:3b', api_base: 'http://localhost:11434/v1', note: '轻量' },
    { label: 'Llama 3.1 8B', model: 'llama3.1:8b', api_base: 'http://localhost:11434/v1' },
    { label: 'Mistral 7B', model: 'mistral:7b', api_base: 'http://localhost:11434/v1' },
    { label: '自定义 Ollama 模型', model: '', api_base: 'http://localhost:11434/v1', note: '手动填写模型名' },
  ],
};

interface GiteeStatus {
  initialized: boolean;
  has_remote: boolean;
  remote_url?: string;
  pending_changes: number;
  recent_commits: string[];
}

const Settings: React.FC = () => {
  const [defaultApiForm] = Form.useForm();
  const [giteeForm] = Form.useForm();
  const [giteeStatus, setGiteeStatus] = useState<GiteeStatus>({
    initialized: false, has_remote: false, pending_changes: 0, recent_commits: [],
  });
  const [saving, setSaving] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [snapshotting, setSnapshotting] = useState(false);
  const [lastSaved, setLastSaved] = useState<string>('');
  const [selectedPreset, setSelectedPreset] = useState<string>('');
  const [testing, setTesting] = useState(false);
  const [clearingApiKey, setClearingApiKey] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; latency_ms: number; message: string } | null>(null);
  const [archiveDir, setArchiveDir] = useState<string>(() => localStorage.getItem(ARCHIVE_DIR_KEY) || '');

  const fetchStatus = async () => {
    try {
      const [defaultRes, giteeRes]: [any, any] = await Promise.all([
        apiClient.get(`/config/api/default`),
        apiClient.get(`/gitee/status`),
      ]);
      // apiClient interceptor 已将 response.data 解包，directly 是数据对象
      defaultApiForm.setFieldsValue({
        model: defaultRes.model || '',
        api_base: defaultRes.api_base || '',
        api_key: '',
        max_tokens: defaultRes.max_tokens || 20480,
        temperature: defaultRes.temperature ?? 0.7,
        generator_model: defaultRes.generator?.model || defaultRes.model || '',
        generator_temperature: defaultRes.generator?.temperature ?? 0.1,
        reviewer_model: defaultRes.reviewer?.model || defaultRes.model || '',
        reviewer_temperature: defaultRes.reviewer?.temperature ?? 0,
      });
      // giteeRes 同样已解包
      setGiteeStatus(giteeRes);
    } catch {
      defaultApiForm.setFieldsValue({
        model: 'gpt-4o',
        api_base: 'https://api.openai.com/v1',
        max_tokens: 20480,
        temperature: 0.7,
        generator_model: '',
        generator_temperature: 0.1,
        reviewer_model: '',
        reviewer_temperature: 0,
      });
    }
  };

  useEffect(() => { fetchStatus(); }, []);

  const handlePresetSelect = (value: string) => {
    setSelectedPreset(value);
    for (const group of Object.values(MODEL_PRESETS)) {
      const preset = group.find(p => `${p.model}||${p.api_base}` === value);
      if (preset) {
        defaultApiForm.setFieldsValue({
          model: preset.model,
          api_base: preset.api_base,
          generator_model: preset.model,
          reviewer_model: preset.model,
        });
        break;
      }
    }
  };

  const handleSaveDefaultApi = async (values: any) => {
    setSaving(true);
    try {
      const payload: Record<string, any> = {};
      if (values.model) payload.model = values.model;
      if (values.api_base) payload.api_base = values.api_base;
      if (values.api_key) payload.api_key = values.api_key;
      if (values.max_tokens) payload.max_tokens = values.max_tokens;
      if (values.temperature !== undefined) payload.temperature = values.temperature;
      payload.generator = {
        model: values.generator_model,
        temperature: values.generator_temperature ?? 0.1,
      };
      payload.reviewer = {
        model: values.reviewer_model,
        temperature: values.reviewer_temperature ?? 0,
      };
      await apiClient.put(`/config/api/default`, payload);
      message.success('你的 API 配置已保存（仅对你生效）');
    } catch {
      message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleLocalSave = async () => {
    setSaving(true);
    try {
      const res = await axios.post(`${API}/data/save`);
      setLastSaved(res.data.saved_at);
      message.success('数据已保存到本地');
    } catch {
      message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleSnapshot = async () => {
    setSnapshotting(true);
    try {
      const res = await axios.post(`${API}/data/snapshot`);
      message.success(`快照已创建：${res.data.snapshot_path}`);
    } catch {
      message.error('快照创建失败');
    } finally {
      setSnapshotting(false);
    }
  };

  const handleClearApiKey = async () => {
    setClearingApiKey(true);
    try {
      await apiClient.put(`/config/api/default`, { api_key: '' });
      defaultApiForm.setFieldValue('api_key', '');
      message.success('已清除当前账号保存的 API Key');
    } catch {
      message.error('清除 API Key 失败');
    } finally {
      setClearingApiKey(false);
    }
  };

  const handleSaveArchiveDir = () => {
    const value = archiveDir.trim();
    if (!value) {
      localStorage.removeItem(ARCHIVE_DIR_KEY);
      message.success('归档目录配置已清空');
      return;
    }
    localStorage.setItem(ARCHIVE_DIR_KEY, value);
    message.success('归档目录已保存');
  };

  const handleConfigGitee = async (values: any) => {
    try {
      const res = await axios.post(`${API}/gitee/config`, {
        repo_url: values.repo_url,
        token: values.token,
      });
      if (res.data.success) {
        message.success('Gitee 仓库配置成功');
        giteeForm.setFieldValue('token', '');
        fetchStatus();
      } else {
        message.error(res.data.error || '配置失败');
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '配置失败');
    }
  };

  const handlePush = async () => {
    setPushing(true);
    try {
      const res = await axios.post(`${API}/gitee/push`, { commit_message: '' });
      if (res.data.success) {
        message.success(res.data.message);
        fetchStatus();
      } else {
        message.error(res.data.error || '推送失败');
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '推送失败，请检查 Gitee 配置');
    } finally {
      setPushing(false);
    }
  };

  const handlePull = async () => {
    setPulling(true);
    try {
      const res = await axios.post(`${API}/gitee/pull`);
      if (res.data.success) {
        message.success(res.data.message);
        fetchStatus();
      } else {
        message.error(res.data.error || '拉取失败');
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '拉取失败');
    } finally {
      setPulling(false);
    }
  };

  return (
    <div style={{ padding: 24, minHeight: '100vh' }}>
      <Tabs
        defaultActiveKey="system"
        size="large"
        items={[
          {
            key: 'system',
            label: <span><ApiOutlined /> 系统设置</span>,
            children: (
              <div className="space-y-5 max-w-3xl">
                <Card
                  title={<Space><ApiOutlined /> 你的 API 配置</Space>}
                  extra={<Text type="secondary" className="text-xs">仅对你生效（按账号隔离），不影响其他用户</Text>}
                >
                  <div className="mb-4">
                    <div className="text-sm font-medium mb-2 flex items-center gap-1">
                      <ThunderboltOutlined className="text-yellow-500" />
                      快速选择模型（自动填充 URL 和模型名）
                    </div>
                    <Select
                      style={{ width: '100%' }}
                      placeholder="选择一个预设模型，或直接在下方手动填写..."
                      value={selectedPreset || undefined}
                      onChange={handlePresetSelect}
                      showSearch
                      optionFilterProp="label"
                      allowClear
                      onClear={() => setSelectedPreset('')}
                    >
                      {Object.entries(MODEL_PRESETS).map(([group, presets]) => (
                        <OptGroup key={group} label={group}>
                          {presets.map(p => (
                            <Option
                              key={`${p.model}||${p.api_base}`}
                              value={`${p.model}||${p.api_base}`}
                              label={`${group} ${p.label}`}
                            >
                              <div className="flex items-center justify-between">
                                <span>{p.label}</span>
                                {p.note && <Tag color="blue" className="text-xs ml-2">{p.note}</Tag>}
                              </div>
                              <div className="text-xs text-gray-400 font-mono">{p.model || '（手动填写）'}</div>
                            </Option>
                          ))}
                        </OptGroup>
                      ))}
                    </Select>
                    <div className="text-xs text-gray-400 mt-1">
                      选择后可在下方修改任意字段，最终以下方填写的内容为准
                    </div>
                  </div>

                  <Divider className="my-3" />

                  <Form form={defaultApiForm} layout="vertical" onFinish={handleSaveDefaultApi}>
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item
                          label="模型名称"
                          name="model"
                          extra={<span className="text-xs text-gray-400">如：deepseek-v4-pro、gpt-4o、claude-3-5-sonnet-20241022</span>}
                        >
                          <Input placeholder="输入模型名称" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          label="API Endpoint（Base URL）"
                          name="api_base"
                          extra={<span className="text-xs text-gray-400">以 /v1 结尾，如：https://api.deepseek.com/v1</span>}
                        >
                          <Input placeholder="https://api.openai.com/v1" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Form.Item
                      label="API Key"
                      name="api_key"
                      extra={
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs text-gray-400">你的 Key 仅自己可见和使用，不会泄露给其他用户</span>
                          <Button
                            type="link"
                            danger
                            size="small"
                            loading={clearingApiKey}
                            onClick={handleClearApiKey}
                          >
                            清除已保存 Key
                          </Button>
                        </div>
                      }
                    >
                      <Input.Password placeholder="sk-... 或其他格式的 API Key" />
                    </Form.Item>
                    <Divider orientation="left" plain>生成与审查模型分工</Divider>
                    <Alert
                      className="mb-3"
                      type="info"
                      showIcon
                      message="模型输出只提供候选实现和审查意见；测试、构建、Docker 与 API 门禁仍由真实执行证据裁决。"
                    />
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item label="生成模型" name="generator_model">
                          <Input placeholder="留空时继承上方兼容模型" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item label="生成 Temperature" name="generator_temperature">
                          <Slider min={0} max={2} step={0.1} marks={{ 0: '0', 0.1: '0.1', 1: '1', 2: '2' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item label="审查模型" name="reviewer_model">
                          <Input placeholder="留空时继承上方兼容模型" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item label="审查 Temperature" name="reviewer_temperature">
                          <Slider min={0} max={2} step={0.1} marks={{ 0: '0', 1: '1', 2: '2' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item
                          label="Max Tokens（最大输出长度）"
                          name="max_tokens"
                          extra={<span className="text-xs text-gray-400">复杂代码生成建议调高单次输出 tokens，例如 8192 或更高，避免输出被截断。</span>}
                        >
                          <InputNumber min={256} max={128000} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          label="Temperature（创造性，0=精确，1=创意）"
                          name="temperature"
                        >
                          <Slider min={0} max={2} step={0.1} marks={{ 0: '0', 0.7: '0.7', 1: '1', 2: '2' }} />
                        </Form.Item>
                      </Col>
                    </Row>

                    <Alert
                      className="mb-3"
                      type="info"
                      showIcon
                      message="各平台 API Key 获取地址"
                      description={
                        <div className="text-xs space-y-1 mt-1">
                          <div>🔵 <strong>DeepSeek</strong>：<a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">platform.deepseek.com/api_keys</a></div>
                          <div>🟢 <strong>OpenAI</strong>：<a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">platform.openai.com/api-keys</a></div>
                          <div>🟠 <strong>Claude</strong>：<a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com/settings/keys</a></div>
                          <div>🔴 <strong>Gemini</strong>：<a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">aistudio.google.com/app/apikey</a></div>
                          <div>🟣 <strong>Kimi</strong>：<a href="https://platform.moonshot.cn/console/api-keys" target="_blank" rel="noreferrer">platform.moonshot.cn/console/api-keys</a></div>
                          <div>🟡 <strong>Qwen</strong>：<a href="https://dashscope.console.aliyun.com/apiKey" target="_blank" rel="noreferrer">dashscope.console.aliyun.com/apiKey</a></div>
                          <div>⚪ <strong>MiniMax</strong>：<a href="https://platform.minimax.chat/user-center/basic-information/interface-key" target="_blank" rel="noreferrer">platform.minimax.chat</a></div>
                          <div>🟤 <strong>Ollama（本地）</strong>：无需 Key，先运行 <code>ollama pull 模型名</code></div>
                        </div>
                      }
                    />

                    <Form.Item>
                      <Space wrap>
                        <Button
                          type="primary"
                          htmlType="submit"
                          icon={<SaveOutlined />}
                          loading={saving}
                          aria-label="保存 API 配置"
                          data-testid="save-api-config"
                        >
                          保存
                        </Button>
                        <Button
                          icon={<PlayCircleOutlined />}
                          loading={testing}
                          onClick={async () => {
                            setTesting(true);
                            setTestResult(null);
                            try {
                              const res: any = await apiClient.post(`/config/api/test`);
                              setTestResult(res);
                              if (res.success) {
                                message.success(`连接成功，延迟 ${res.latency_ms}ms`);
                              } else {
                                message.error(`连接失败：${res.message}`);
                              }
                            } catch (e: any) {
                              setTestResult({ success: false, latency_ms: 0, message: e.response?.data?.detail || '请求失败' });
                            } finally {
                              setTesting(false);
                            }
                          }}
                        >
                          测试连接
                        </Button>
                      </Space>
                    </Form.Item>
                    {testResult && (
                      <Alert
                        type={testResult.success ? 'success' : 'error'}
                        showIcon
                        message={testResult.success
                          ? `✅ API 连接正常，延迟 ${testResult.latency_ms}ms`
                          : `❌ 连接失败：${testResult.message}`}
                        className="mb-3"
                        closable
                        onClose={() => setTestResult(null)}
                      />
                    )}
                  </Form>
                </Card>
              </div>
            ),
          },
          {
            key: 'persistence',
            label: <span><DatabaseOutlined /> 本地持久化</span>,
            children: (
              <div className="space-y-5 max-w-3xl">
                <Card title={<Space><DatabaseOutlined /> 本地数据持久化</Space>}>
                  <Alert
                    message="数据自动保存机制"
                    description="每次创建项目、修改 Agent、导入 Skill 后系统会自动保存到 backend/data/ 目录。后端启动时自动加载，关闭时自动保存。"
                    type="info"
                    showIcon
                    className="mb-4"
                  />
                  <Space wrap>
                    <Button icon={<SaveOutlined />} onClick={handleLocalSave} loading={saving}>
                      立即保存到本地
                    </Button>
                    <Button icon={<HistoryOutlined />} onClick={handleSnapshot} loading={snapshotting}>
                      创建快照（带时间戳）
                    </Button>
                  </Space>
                  {lastSaved && (
                    <div className="mt-3 text-xs text-gray-400">
                      <CheckCircleOutlined className="text-green-500 mr-1" />
                      上次保存：{lastSaved}
                    </div>
                  )}
                  <Divider />
                  <div className="text-xs text-gray-400">
                    <div>📁 数据存储位置：<code>multimind/backend/data/</code></div>
                    <div className="mt-1">📄 包含：projects.json · agents_config.json · skills.json</div>
                    <div className="mt-1">📸 快照文件：snapshot_YYYYMMDD_HHMMSS.json（不会自动推送到 Gitee）</div>
                  </div>
                </Card>
                <Card title={<Space><FolderOpenOutlined /> 项目归档目录</Space>}>
                  <Alert
                    message="Web 版归档会下载 zip；桌面版配置目录后，项目归档会直接写入该本机文件夹。"
                    type="info"
                    showIcon
                    className="mb-4"
                  />
                  <Space.Compact style={{ width: '100%' }}>
                    <Input
                      value={archiveDir}
                      onChange={(e) => setArchiveDir(e.target.value)}
                      placeholder="例如：D:\\MeTis归档"
                    />
                    <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveArchiveDir}>
                      保存
                    </Button>
                  </Space.Compact>
                </Card>
              </div>
            ),
          },
          {
            key: 'gitee',
            label: <span><GithubOutlined /> Gitee 同步</span>,
            children: (
              <div className="space-y-5 max-w-3xl">
                <Card
                  title={<Space><GithubOutlined /> Gitee 仓库绑定</Space>}
                  extra={
                    giteeStatus.has_remote
                      ? <Tag color="green" icon={<CheckCircleOutlined />}>已绑定</Tag>
                      : <Tag color="default">未绑定</Tag>
                  }
                >
                  <Alert
                    message="手动推送说明"
                    description="系统不会自动提交到 Gitee。你需要手动点击「推送到 Gitee」按钮。推送前会先保存最新数据。"
                    type="warning"
                    showIcon
                    className="mb-4"
                  />

                  {giteeStatus.initialized && (
                    <div className="mb-4 p-3 bg-gray-50 rounded text-xs space-y-1">
                      <div>
                        <span className="text-gray-500">仓库地址：</span>
                        <span className="font-mono">{giteeStatus.remote_url || '未配置'}</span>
                      </div>
                      <div>
                        <span className="text-gray-500">待推送变更：</span>
                        <span className={giteeStatus.pending_changes > 0 ? 'text-orange-500 font-medium' : 'text-green-500'}>
                          {giteeStatus.pending_changes} 个文件
                        </span>
                      </div>
                      {giteeStatus.recent_commits.length > 0 && (
                        <div>
                          <div className="text-gray-500 mb-1">最近提交：</div>
                          {giteeStatus.recent_commits.map((c, i) => (
                            <div key={i} className="font-mono text-gray-600 pl-2">{c}</div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  <Form form={giteeForm} layout="vertical" onFinish={handleConfigGitee}>
                    <Form.Item
                      label="Gitee 仓库地址"
                      name="repo_url"
                      rules={[{ required: true, message: '请输入仓库地址' }]}
                    >
                      <Input placeholder="https://gitee.com/your-username/your-repo.git" />
                    </Form.Item>
                    <Form.Item
                      label="Gitee 个人访问令牌（Token）"
                      name="token"
                      rules={[{ required: true, message: '请输入 Token' }]}
                      extra={
                        <span className="text-xs text-gray-400">
                          在 Gitee → 设置 → 私人令牌 中生成，需要 projects 权限
                        </span>
                      }
                    >
                      <Input.Password placeholder="输入 Gitee 个人访问令牌" />
                    </Form.Item>
                    <Form.Item>
                      <Button type="primary" htmlType="submit" icon={<GithubOutlined />}>
                        绑定 Gitee 仓库
                      </Button>
                    </Form.Item>
                  </Form>

                  <Divider />

                  <Space wrap>
                    <Button
                      type="primary"
                      icon={<CloudUploadOutlined />}
                      onClick={handlePush}
                      loading={pushing}
                      disabled={!giteeStatus.has_remote}
                    >
                      手动推送到 Gitee
                    </Button>
                    <Button
                      icon={<CloudDownloadOutlined />}
                      onClick={handlePull}
                      loading={pulling}
                      disabled={!giteeStatus.has_remote}
                      danger
                    >
                      从 Gitee 拉取（覆盖本地）
                    </Button>
                  </Space>
                  {!giteeStatus.has_remote && (
                    <div className="mt-2 text-xs text-gray-400">
                      <WarningOutlined className="mr-1" />
                      请先绑定 Gitee 仓库后再推送
                    </div>
                  )}
                </Card>
              </div>
            ),
          },
          {
            key: 'ide',
            label: <span><CodeOutlined /> IDE 集成</span>,
            children: <IdeIntegration />,
          },
        ]}
      />
    </div>
  );
};

export default Settings;
