import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Col, Input, List, message, Modal, Popconfirm, Row, Space, Tabs, Tag, Typography } from 'antd';
import { ApiOutlined, CheckCircleOutlined, CopyOutlined, DeleteOutlined, EyeOutlined, KeyOutlined, PlusOutlined, SafetyOutlined } from '@ant-design/icons';
import axios from 'axios';

const { Paragraph, Text, Title } = Typography;
const MCP_URL = 'https://metis-multiagent.up.railway.app/api/mcp';
const API = '/api';

const brands = {
  trae: { name: 'Trae', icon: 'https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/trae.svg', color: '#5b6cff' },
  vscode: { name: 'VS Code', icon: 'https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/visualstudiocode.svg', color: '#007acc' },
  cursor: { name: 'Cursor', icon: 'https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/cursor.svg', color: '#111827' },
  claude: { name: 'Claude Code', icon: 'https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/anthropic.svg', color: '#d97757' },
  codex: { name: 'Codex', icon: 'https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/openai.svg', color: '#10a37f' },
} as const;

type BrandKey = keyof typeof brands;

interface MCPTokenItem {
  token_id: string;
  name: string;
  masked_token: string;
  created_at: number;
}

const Brand: React.FC<{ type: BrandKey; compact?: boolean }> = ({ type, compact }) => {
  const brand = brands[type];
  return (
    <Space size={8}>
      <span style={{ width: compact ? 22 : 30, height: compact ? 22 : 30, borderRadius: 7, background: '#fff', border: '1px solid #e5e7eb', display: 'grid', placeItems: 'center', overflow: 'hidden' }}>
        <img src={brand.icon} alt={`${brand.name} logo`} style={{ width: '72%', height: '72%', objectFit: 'contain' }} />
      </span>
      <Text strong style={{ color: compact ? undefined : brand.color }}>{brand.name}</Text>
    </Space>
  );
};

const CodeBlock: React.FC<{ code: string }> = ({ code }) => (
  <div style={{ position: 'relative', marginTop: 10, padding: '18px 16px', borderRadius: 12, background: '#111827' }}>
    <Button
      size="small"
      icon={<CopyOutlined />}
      onClick={() => navigator.clipboard.writeText(code).then(() => message.success('配置已复制'))}
      style={{ position: 'absolute', top: 10, right: 10, color: '#cbd5e1', borderColor: '#475569', background: '#1f2937' }}
    >
      复制
    </Button>
    <pre style={{ margin: 0, paddingRight: 70, overflowX: 'auto', whiteSpace: 'pre-wrap', color: '#86efac', fontSize: 12, lineHeight: 1.7 }}>{code}</pre>
  </div>
);

const httpConfig = JSON.stringify({
  mcpServers: {
    metis: {
      url: MCP_URL,
      headers: { Authorization: 'Bearer <METIS_ACCESS_TOKEN>' },
    },
  },
}, null, 2);

const vscodeConfig = JSON.stringify({
  servers: {
    metis: {
      type: 'http',
      url: MCP_URL,
      headers: { Authorization: 'Bearer ${input:metisToken}' },
    },
  },
  inputs: [{
    type: 'promptString',
    id: 'metisToken',
    description: 'METIS access token',
    password: true,
  }],
}, null, 2);

const codexConfig = `[mcp_servers.metis]
url = "${MCP_URL}"
bearer_token_env_var = "METIS_ACCESS_TOKEN"`;

const configPanel = (type: BrandKey, path: string, config = httpConfig) => (
  <div style={{ display: 'grid', gap: 16 }}>
    <Alert
      type="info"
      showIcon
      message={<Brand type={type} compact />}
      description={<>在 <code>{path}</code> 中加入以下远程 MCP 配置，然后重启客户端。</>}
    />
    <Card size="small" title="远程 MCP 配置"><CodeBlock code={config} /></Card>
    <Alert type="warning" showIcon message="令牌不是大模型 API Key" description="请使用你的 METIS 登录访问令牌替换占位符；不要把令牌提交到 Git。" />
  </div>
);

const IdeIntegration: React.FC = () => {
  const [activeTab, setActiveTab] = useState('overview');
  const [tokens, setTokens] = useState<MCPTokenItem[]>([]);
  const [createdToken, setCreatedToken] = useState('');
  const [revealedToken, setRevealedToken] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [revealTarget, setRevealTarget] = useState<MCPTokenItem | null>(null);
  const [password, setPassword] = useState('');
  const [tokenLoading, setTokenLoading] = useState(false);

  const clearSensitiveToken = () => {
    setCreatedToken('');
    setRevealedToken('');
    setPassword('');
    setRevealTarget(null);
  };

  const loadTokens = async () => {
    const { data } = await axios.get(`${API}/mcp/tokens`);
    setTokens(Array.isArray(data.tokens) ? data.tokens : []);
  };

  useEffect(() => {
    loadTokens().catch(() => message.error('MCP Token 列表加载失败'));
    const hideSensitiveToken = () => {
      if (document.hidden) clearSensitiveToken();
    };
    document.addEventListener('visibilitychange', hideSensitiveToken);
    window.addEventListener('blur', clearSensitiveToken);
    return () => {
      document.removeEventListener('visibilitychange', hideSensitiveToken);
      window.removeEventListener('blur', clearSensitiveToken);
    };
  }, []);

  useEffect(() => clearSensitiveToken(), [activeTab]);

  const generateToken = async () => {
    const name = createName.trim();
    if (!name) {
      message.warning('请输入 Token 名称');
      return;
    }
    setTokenLoading(true);
    try {
      const { data } = await axios.post(`${API}/mcp/tokens`, { name });
      setCreatedToken(data.token);
      setCreateOpen(false);
      setCreateName('');
      await loadTokens();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || 'MCP Token 生成失败');
    } finally {
      setTokenLoading(false);
    }
  };

  const revealToken = async () => {
    if (!revealTarget || !password) return;
    setTokenLoading(true);
    try {
      const { data } = await axios.post(`${API}/mcp/tokens/${revealTarget.token_id}/reveal`, { password });
      setRevealedToken(data.token);
      setPassword('');
    } catch (error: any) {
      message.error(error?.response?.data?.detail || 'Token 查看失败');
    } finally {
      setTokenLoading(false);
    }
  };

  const revokeToken = async (tokenId: string) => {
    try {
      await axios.delete(`${API}/mcp/tokens/${tokenId}`);
      clearSensitiveToken();
      await loadTokens();
      message.success('MCP Token 已撤销');
    } catch (error: any) {
      message.error(error?.response?.data?.detail || 'MCP Token 撤销失败');
    }
  };

  const items = [
    {
      key: 'overview',
      label: '接入说明',
      children: (
        <div style={{ display: 'grid', gap: 18 }}>
          <Alert
            type="success"
            showIcon
            icon={<CheckCircleOutlined />}
            message="METIS MCP 服务已上线"
            description={<><Text>远程地址：</Text><Text copyable code>{MCP_URL}</Text></>}
          />
          <Card
            title="访问令牌"
            extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建 Token</Button>}
            style={{ borderRadius: 14 }}
          >
            <List
              locale={{ emptyText: '尚未创建 MCP Token' }}
              dataSource={tokens}
              renderItem={item => (
                <List.Item
                  actions={[
                    <Button key="view" type="link" icon={<EyeOutlined />} onClick={() => {
                      clearSensitiveToken();
                      setRevealTarget(item);
                    }}>查看</Button>,
                    <Popconfirm
                      key="revoke"
                      title="撤销这个 Token？"
                      description="使用它的客户端将立即断开。"
                      okText="撤销"
                      cancelText="取消"
                      onConfirm={() => revokeToken(item.token_id)}
                    >
                      <Button type="link" danger icon={<DeleteOutlined />}>撤销</Button>
                    </Popconfirm>,
                  ]}
                >
                  <List.Item.Meta
                    title={<Space><Text strong>{item.name}</Text><Tag>••••{item.masked_token.slice(-4)}</Tag></Space>}
                    description={`创建于 ${new Date(item.created_at * 1000).toLocaleString()}`}
                  />
                </List.Item>
              )}
            />
            <Paragraph type="secondary" style={{ margin: '12px 0 0' }}>
              Token 相互独立，可分别用于不同客户端；查看完整 Token 时需要重新验证账户密码。
            </Paragraph>
          </Card>
          <Row gutter={[14, 14]}>
            {(Object.keys(brands) as BrandKey[]).map(key => (
              <Col xs={24} sm={12} lg={8} key={key}>
                <Card hoverable onClick={() => setActiveTab(key)} style={{ borderRadius: 14, height: '100%' }}>
                  <Brand type={key} />
                  <Paragraph type="secondary" style={{ margin: '12px 0 0' }}>查看配置并连接 METIS 工具。</Paragraph>
                </Card>
              </Col>
            ))}
          </Row>
          <Card title="可用工具" style={{ borderRadius: 14 }}>
            <Space wrap>
              <Tag color="blue">task_execute · 项目与任务操作</Tag>
              <Tag color="purple">file_operate · 项目文件操作</Tag>
              <Tag color="cyan">agent_query · 状态与进度查询</Tag>
            </Space>
          </Card>
          <Alert type="info" showIcon icon={<SafetyOutlined />} message="权限说明" description="MCP 使用当前登录用户权限，项目与文件仍按账号隔离；服务不会继承 IDE 身份。" />
        </div>
      ),
    },
    { key: 'trae', label: <Brand type="trae" compact />, children: configPanel('trae', 'Trae 设置 → MCP') },
    { key: 'vscode', label: <Brand type="vscode" compact />, children: configPanel('vscode', '.vscode/mcp.json', vscodeConfig) },
    { key: 'cursor', label: <Brand type="cursor" compact />, children: configPanel('cursor', '.cursor/mcp.json') },
    { key: 'claude', label: <Brand type="claude" compact />, children: configPanel('claude', '~/.claude/mcp.json') },
    {
      key: 'codex',
      label: <Brand type="codex" compact />,
      children: (
        <div style={{ display: 'grid', gap: 16 }}>
          <Alert type="info" showIcon message={<Brand type="codex" compact />} description={<>将配置写入 <code>~/.codex/config.toml</code>，并通过环境变量提供令牌。</>} />
          <Card size="small" title="config.toml"><CodeBlock code={codexConfig} /></Card>
          <Card size="small" title="PowerShell 会话变量"><CodeBlock code={'$env:METIS_ACCESS_TOKEN="<你的 METIS 登录访问令牌>"'} /></Card>
          <Alert type="warning" showIcon message="不要把真实令牌写入 config.toml 或提交到仓库" />
        </div>
      ),
    },
  ];

  return (
    <div style={{ maxWidth: 1050, margin: '0 auto' }}>
      <div style={{ padding: '28px 30px', marginBottom: 20, borderRadius: 18, color: '#fff', background: 'linear-gradient(135deg, #111827, #1d4ed8 58%, #0891b2)' }}>
        <Space align="start">
          <ApiOutlined style={{ fontSize: 26, marginTop: 5 }} />
          <div>
            <Title level={2} style={{ color: '#fff', margin: 0 }}>MCP 服务</Title>
            <Text style={{ color: 'rgba(255,255,255,.8)' }}>让开发工具安全调用 METIS 的项目、文件和 Agent 能力。</Text>
          </div>
        </Space>
      </div>
      <Tabs activeKey={activeTab} onChange={setActiveTab} items={items} tabBarGutter={20} />
      <div style={{ marginTop: 14, color: '#94a3b8', fontSize: 12 }}><KeyOutlined /> 连接失败时，请先检查服务地址、登录令牌和客户端是否支持远程 HTTP MCP。</div>
      <Modal
        open={createOpen}
        title="新建 MCP Token"
        okText="生成"
        cancelText="取消"
        confirmLoading={tokenLoading}
        onCancel={() => { setCreateOpen(false); setCreateName(''); }}
        onOk={generateToken}
      >
        <Input
          autoFocus
          maxLength={64}
          placeholder="例如：Trae 工作台"
          value={createName}
          onChange={event => setCreateName(event.target.value)}
          onPressEnter={generateToken}
        />
      </Modal>
      <Modal
        open={Boolean(createdToken)}
        title="请保存 MCP Token"
        okText="复制并关闭"
        cancelText="关闭"
        onCancel={clearSensitiveToken}
        onOk={() => navigator.clipboard.writeText(createdToken).then(() => {
          message.success('Token 已复制');
          clearSensitiveToken();
        })}
      >
        <Alert type="warning" showIcon message="关闭、切页或窗口失焦后将自动隐藏。再次查看需要验证账户密码。" />
        <CodeBlock code={createdToken} />
      </Modal>
      <Modal
        open={Boolean(revealTarget)}
        title={`查看 Token：${revealTarget?.name || ''}`}
        okText={revealedToken ? '复制并关闭' : '验证密码'}
        cancelText="关闭"
        confirmLoading={tokenLoading}
        onCancel={clearSensitiveToken}
        onOk={() => {
          if (!revealedToken) {
            revealToken();
            return;
          }
          navigator.clipboard.writeText(revealedToken).then(() => {
            message.success('Token 已复制');
            clearSensitiveToken();
          });
        }}
      >
        {revealedToken ? (
          <CodeBlock code={revealedToken} />
        ) : (
          <Input.Password
            autoFocus
            placeholder="请输入当前账户密码"
            value={password}
            onChange={event => setPassword(event.target.value)}
            onPressEnter={revealToken}
          />
        )}
      </Modal>
    </div>
  );
};

export default IdeIntegration;
