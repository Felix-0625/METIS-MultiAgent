/**
 * IDE 集成页面
 * 说明如何在 Trae / VSCode / Cursor / Claude Code 中调用本系统
 * 提供 MCP Server 配置、REST API 文档、使用流程
 */

import React, { useState } from 'react';
import {
  Card, Tabs, Tag, Button, Alert,
  Typography, Row, Col, message,
} from 'antd';
import {
  CopyOutlined, CheckCircleOutlined, ApiOutlined,
} from '@ant-design/icons';

const { Title, Text, Paragraph } = Typography;

const API_BASE = 'http://localhost:8000';

// 代码块组件
const CodeBlock: React.FC<{ code: string; lang?: string }> = ({ code, lang = 'json' }) => {
  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    message.success('已复制到剪贴板');
  };
  return (
    <div className="relative bg-gray-900 rounded-lg p-4 my-2">
      <Button
        size="small"
        icon={<CopyOutlined />}
        className="absolute top-2 right-2 text-gray-400 border-gray-600"
        onClick={handleCopy}
        style={{ backgroundColor: 'transparent', color: '#9ca3af', borderColor: '#4b5563' }}
      >
        复制
      </Button>
      <pre className="text-green-400 text-xs overflow-x-auto whitespace-pre-wrap m-0 pr-16">
        {code}
      </pre>
    </div>
  );
};

const IdeIntegration: React.FC = () => {
  const [activeTab, setActiveTab] = useState('overview');

  // MCP Server 配置
  const mcpConfig = JSON.stringify({
    "mcpServers": {
      "multimind": {
        "url": "http://localhost:8000/mcp",
        "description": "MultiMind - Multi-AI Agent Project Management System",
        "tools": [
          "create_project", "analyze_requirements", "get_progress",
          "list_agents", "import_skill", "push_to_gitee"
        ]
      }
    }
  }, null, 2);

  // Trae 配置
  const traeConfig = JSON.stringify({
    "ai.agent.endpoint": "http://localhost:8000",
    "ai.agent.systemName": "MultiMind",
    "ai.agent.autoConnect": true
  }, null, 2);

  // VSCode settings.json
  const vscodeConfig = JSON.stringify({
    "aiAgentSystem.endpoint": "http://localhost:8000",
    "aiAgentSystem.autoSave": true,
    "aiAgentSystem.defaultModel": "gpt-4o"
  }, null, 2);

  // REST API 示例
  const apiExamples = {
    createProject: `# 创建项目
curl -X POST ${API_BASE}/projects \\
  -H "Content-Type: application/json" \\
  -d '{"name": "我的项目", "description": "项目描述"}'`,

    analyzeReq: `# PM Agent 分析需求
curl -X POST ${API_BASE}/projects/{project_id}/analyze \\
  -H "Content-Type: application/json" \\
  -d '{"requirements": "开发一个电商平台，需要用户管理、商品管理、订单系统"}'`,

    getProgress: `# 查询项目进度
curl ${API_BASE}/projects/{project_id}/progress`,

    listAgents: `# 查看员工池（所有项目的 Agent）
curl ${API_BASE}/agents`,

    pushGitee: `# 手动推送到 Gitee
curl -X POST ${API_BASE}/gitee/push \\
  -H "Content-Type: application/json" \\
  -d '{"commit_message": "项目数据备份"}'`,
  };

  // 使用流程步骤
  const workflowSteps = [
    {
      title: '启动系统',
      description: '在终端运行后端服务',
      code: 'cd ai-agent-system/backend\npython main.py',
    },
    {
      title: '在 IDE 中打开项目',
      description: '用 Trae/VSCode/Cursor 打开你的开发项目',
      code: 'code /path/to/your/project\n# 或在 Trae 中直接打开',
    },
    {
      title: '通过 REST API 创建项目',
      description: '在 IDE 终端中调用 API，或通过 Web UI 操作',
      code: apiExamples.createProject,
    },
    {
      title: '与 PM Agent 对话',
      description: '描述需求，PM Agent 自动生成规划书和子项目',
      code: apiExamples.analyzeReq,
    },
    {
      title: '查看进度 / 与 Supervisor 对话',
      description: '监控执行进度，处理阻塞，触发质检',
      code: apiExamples.getProgress,
    },
    {
      title: '保存 / 推送到 Gitee',
      description: '手动触发数据备份，不会自动提交',
      code: apiExamples.pushGitee,
    },
  ];

  const tabItems = [
    {
      key: 'overview',
      label: '📋 使用流程',
      children: (
        <div className="space-y-4">
          <Alert
            message="系统定位"
            description="本系统作为后端服务运行，配合 Trae / VSCode / Cursor / Claude Code 等 IDE 使用。IDE 负责代码编写，本系统负责项目管理、Agent 调度和流程控制。"
            type="info"
            showIcon
          />
          <div className="space-y-3">
            {workflowSteps.map((step, i) => (
              <Card key={i} size="small">
                <div className="flex items-start gap-3">
                  <div className="w-7 h-7 rounded-full bg-blue-500 text-white flex items-center justify-center text-sm font-bold flex-shrink-0">
                    {i + 1}
                  </div>
                  <div className="flex-1">
                    <div className="font-medium text-sm">{step.title}</div>
                    <div className="text-xs text-gray-500 mb-1">{step.description}</div>
                    <CodeBlock code={step.code} lang="bash" />
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>
      ),
    },
    {
      key: 'trae',
      label: '🔧 Trae',
      children: (
        <div className="space-y-4">
          <Alert
            message="Trae 集成方式"
            description="Trae 支持通过 MCP Server 协议直接调用本系统的工具，也可以通过 REST API 在终端中调用。"
            type="info"
            showIcon
          />
          <Card title="方式一：MCP Server 配置" size="small">
            <Paragraph className="text-sm text-gray-600">
              在 Trae 的 MCP 配置文件中添加以下内容（通常在 <code>~/.trae/mcp.json</code> 或设置面板中）：
            </Paragraph>
            <CodeBlock code={mcpConfig} />
            <Alert
              message="注意：MCP Server 端点需要后端实现 /mcp 路由（当前版本使用 REST API 方式）"
              type="warning"
              showIcon
              className="mt-2"
            />
          </Card>
          <Card title="方式二：在 Trae 终端中使用 REST API" size="small">
            <Paragraph className="text-sm text-gray-600">
              直接在 Trae 内置终端中运行 curl 命令，或让 Trae 的 AI 助手调用 API：
            </Paragraph>
            <CodeBlock code={apiExamples.createProject} lang="bash" />
            <CodeBlock code={apiExamples.analyzeReq} lang="bash" />
          </Card>
          <Card title="方式三：Trae 设置（如支持）" size="small">
            <CodeBlock code={traeConfig} />
          </Card>
        </div>
      ),
    },
    {
      key: 'vscode',
      label: '💙 VSCode / Cursor',
      children: (
        <div className="space-y-4">
          <Alert
            message="VSCode / Cursor 集成方式"
            description="通过内置终端调用 REST API，或安装扩展（如 REST Client）直接发送请求。Cursor 的 AI 功能可以直接读取 API 响应并辅助开发。"
            type="info"
            showIcon
          />
          <Card title="settings.json 配置" size="small">
            <Paragraph className="text-sm text-gray-600">
              在 VSCode/Cursor 的 <code>.vscode/settings.json</code> 中添加：
            </Paragraph>
            <CodeBlock code={vscodeConfig} />
          </Card>
          <Card title="REST Client 扩展（推荐）" size="small">
            <Paragraph className="text-sm text-gray-600">
              安装 <strong>REST Client</strong> 扩展后，创建 <code>api.http</code> 文件：
            </Paragraph>
            <CodeBlock code={`### 创建项目
POST http://localhost:8000/projects
Content-Type: application/json

{
  "name": "我的项目",
  "description": "项目描述"
}

### PM Agent 分析需求
POST http://localhost:8000/projects/{{project_id}}/analyze
Content-Type: application/json

{
  "requirements": "开发一个电商平台"
}

### 查看进度
GET http://localhost:8000/projects/{{project_id}}/progress

### 查看员工池
GET http://localhost:8000/agents`} lang="http" />
          </Card>
          <Card title="Cursor AI 提示词模板" size="small">
            <Paragraph className="text-sm text-gray-600">
              在 Cursor 中使用以下提示词让 AI 自动调用本系统：
            </Paragraph>
            <CodeBlock code={`你是一个项目管理助手，可以通过 REST API 调用 AI Agent 系统（http://localhost:8000）。
当用户描述项目需求时：
1. 先调用 POST /projects 创建项目
2. 再调用 POST /projects/{id}/analyze 分析需求
3. 最后展示规划结果

系统 API 文档：http://localhost:8000/docs`} lang="text" />
          </Card>
        </div>
      ),
    },
    {
      key: 'claude',
      label: '🤖 Claude Code',
      children: (
        <div className="space-y-4">
          <Alert
            message="Claude Code 集成方式"
            description="Claude Code 支持通过 MCP 协议调用外部工具，也可以在对话中直接使用 bash 工具调用 REST API。"
            type="info"
            showIcon
          />
          <Card title="MCP 配置（~/.claude/mcp.json）" size="small">
            <CodeBlock code={mcpConfig} />
          </Card>
          <Card title="在 Claude Code 对话中使用" size="small">
            <Paragraph className="text-sm text-gray-600">
              直接告诉 Claude Code 调用 API：
            </Paragraph>
            <CodeBlock code={`# 在 Claude Code 中输入：
请帮我创建一个项目，调用 http://localhost:8000/projects，
项目名称是"电商平台"，描述是"B2C 电商系统"。
然后分析需求：需要用户管理、商品管理、订单系统、支付集成。`} lang="text" />
          </Card>
          <Card title="CLAUDE.md 项目配置" size="small">
            <Paragraph className="text-sm text-gray-600">
              在项目根目录创建 <code>CLAUDE.md</code>，让 Claude Code 自动了解系统：
            </Paragraph>
            <CodeBlock code={`# AI Agent System 集成

## 系统地址
- 后端 API: http://localhost:8000
- API 文档: http://localhost:8000/docs
- Web UI: http://localhost:5173

## 常用操作
- 创建项目: POST /projects
- 分析需求: POST /projects/{id}/analyze  
- 查看进度: GET /projects/{id}/progress
- 员工池: GET /agents
- 推送 Gitee: POST /gitee/push

## 注意事项
- 不要自动推送到 Gitee，需要用户手动确认
- 每个项目的 Agent 记忆独立，不要混用 project_id`} lang="markdown" />
          </Card>
        </div>
      ),
    },
    {
      key: 'codex',
      label: 'Codex 🤖',
      children: (
        <div className="space-y-4">
          <Card title="MCP 服务器配置" size="small">
            <p>在 Codex 中配置以下 MCP Server 即可调用本系统：</p>
            <CodeBlock code={JSON.stringify({mcpServers:{metis:{url:"http://localhost:8000/mcp"}}}, null, 2)} />
          </Card>
        </div>
      ),
    },
    {
      key: 'api',
      label: '📡 REST API',
      children: (
        <div className="space-y-4">
          <Alert
            message={<span>完整 API 文档：<a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">{API_BASE}/docs</a>（Swagger UI）</span>}
            type="success"
            showIcon
            icon={<ApiOutlined />}
          />
          <Row gutter={16}>
            {[
              { title: '项目管理', apis: [
                { method: 'POST', path: '/projects', desc: '创建项目' },
                { method: 'GET', path: '/projects', desc: '列出所有项目' },
                { method: 'DELETE', path: '/projects/{id}', desc: '删除项目' },
              ]},
              { title: 'PM Agent', apis: [
                { method: 'POST', path: '/projects/{id}/analyze', desc: '分析需求' },
                { method: 'POST', path: '/projects/{id}/plan/generate', desc: '生成规划书' },
                { method: 'GET', path: '/projects/{id}/subprojects', desc: '获取子项目' },
              ]},
              { title: '员工池', apis: [
                { method: 'GET', path: '/agents', desc: '所有 Agent（按项目）' },
                { method: 'PUT', path: '/agents/{id}/config', desc: '配置 Agent API' },
                { method: 'DELETE', path: '/agents/{id}/config', desc: '重置 Agent API' },
              ]},
              { title: '数据 & Gitee', apis: [
                { method: 'POST', path: '/data/save', desc: '手动保存到本地' },
                { method: 'POST', path: '/data/snapshot', desc: '创建快照' },
                { method: 'POST', path: '/gitee/push', desc: '手动推送到 Gitee' },
                { method: 'GET', path: '/gitee/status', desc: '查看 Gitee 状态' },
              ]},
            ].map(group => (
              <Col span={12} key={group.title}>
                <Card title={group.title} size="small" className="mb-3">
                  {group.apis.map(api => (
                    <div key={api.path} className="flex items-center gap-2 py-1 border-b last:border-0">
                      <Tag
                        color={api.method === 'GET' ? 'green' : api.method === 'POST' ? 'blue' : api.method === 'DELETE' ? 'red' : 'orange'}
                        className="text-xs w-14 text-center"
                      >
                        {api.method}
                      </Tag>
                      <code className="text-xs text-gray-600 flex-1">{api.path}</code>
                      <span className="text-xs text-gray-400">{api.desc}</span>
                    </div>
                  ))}
                </Card>
              </Col>
            ))}
          </Row>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold m-0">IDE 集成</h2>
        <div className="text-xs text-gray-400 mt-1">
          配合 Trae / VSCode / Cursor / Claude Code 使用 · REST API 接入 · 流程化项目管理
        </div>
      </div>

      <div className="flex gap-2 flex-wrap">
        {[
          { label: 'Trae', color: '#1890ff' },
          { label: 'VSCode', color: '#007acc' },
          { label: 'Cursor', color: '#000' },
          { label: 'Claude Code', color: '#d97706' },
          { label: 'Codex', color: '#10b981' },
        ].map(ide => (
          <Tag key={ide.label} style={{ backgroundColor: ide.color, color: '#fff', border: 'none' }}>
            {ide.label}
          </Tag>
        ))}
        <Tag color="green" icon={<CheckCircleOutlined />}>REST API</Tag>
        <Tag color="purple" icon={<ApiOutlined />}>MCP Protocol</Tag>
      </div>

      <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
    </div>
  );
};

export default IdeIntegration;
