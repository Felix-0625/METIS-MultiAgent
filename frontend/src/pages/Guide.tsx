/**
 * 使用说明页面
 * - 平台使用方法
 * - 注意事项
 * - API Key 配置指引
 */
import React from 'react';
import { Card, Typography, Divider, Alert, Space, Steps, Tag } from 'antd';
import {
  BulbOutlined, SettingOutlined, ApiOutlined, KeyOutlined,
  WarningOutlined, CheckCircleOutlined, ThunderboltOutlined,
  UserOutlined, ProjectOutlined, TeamOutlined, CodeOutlined,
  PlayCircleOutlined, SafetyOutlined, QuestionCircleOutlined,
  AppstoreOutlined, PartitionOutlined, AuditOutlined,
} from '@ant-design/icons';

const { Title, Paragraph, Text } = Typography;

const Guide: React.FC = () => {
  return (
    <div style={{ padding: 24, minHeight: '100vh', maxWidth: 900, margin: '0 auto' }}>
      <Title level={3} style={{ marginBottom: 24, color: '#1e1b1b' }}>
        📖 MeTis 使用说明
      </Title>

      {/* 快速开始 */}
      <Card style={{ marginBottom: 20 }} title={<Space><PlayCircleOutlined /> 快速开始（4 步上手）</Space>}>
        <Steps
          direction="vertical"
          size="small"
          current={-1}
          items={[
            {
              title: <span style={{ fontWeight: 600 }}>配置 API Key</span>,
              description: (
                <div style={{ marginTop: 4 }}>
                  进入<Text code>系统设置</Text>页面，选择模型（支持 DeepSeek / OpenAI / Claude / Gemini / Qwen / Ollama 等），
                  填入你自己申请的 API Key，点击「测试连接」确认可用后保存。
                  <br />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    API Key 仅你个人使用，不会共享给其他用户。平台本身不提供 Key，请自行到各平台官网申请。
                  </Text>
                </div>
              ),
              icon: <KeyOutlined />,
            },
            {
              title: <span style={{ fontWeight: 600 }}>创建你的第一个项目</span>,
              description: (
                <div style={{ marginTop: 4 }}>
                  进入<Text code>项目</Text>页面，点击「新建项目」，填写项目名称和描述。
                  系统会自动让 AI 分析需求并生成子项目拆解。
                </div>
              ),
              icon: <ProjectOutlined />,
            },
            {
              title: <span style={{ fontWeight: 600 }}>查看 AI 自动推演</span>,
              description: (
                <div style={{ marginTop: 4 }}>
                  创建项目后，AI 会自动组建 PM 团队、分析需求、拆解任务、组建人力资源。
                  你可以在<Text code>进度看板</Text>中实时查看项目进展。
                </div>
              ),
              icon: <TeamOutlined />,
            },
            {
              title: <span style={{ fontWeight: 600 }}>审查和交付</span>,
              description: (
                <div style={{ marginTop: 4 }}>
                  每个阶段完成后，AI 会自动质检和审查。你可以在<Text code>阶段看板</Text>中查看各阶段的输出物，
                  确认无误后进入下一阶段，直到项目交付。
                </div>
              ),
              icon: <CheckCircleOutlined />,
            },
          ]}
        />
      </Card>

      {/* 核心功能 */}
      <Card style={{ marginBottom: 20 }} title={<Space><AppstoreOutlined /> 核心功能</Space>}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {[
            { icon: <BulbOutlined />, title: '想法落地', desc: 'AI 帮你把模糊想法转化为结构化需求和项目框架' },
            { icon: <TeamOutlined />, title: '多 Agent 协作', desc: 'PM / HR / Supervisor / Engineer 等专业 Agent 自动分工' },
            { icon: <PartitionOutlined />, title: '阶段看板', desc: '可视化项目阶段进度，冲突检测，审查链' },
            { icon: <CodeOutlined />, title: '全栈工程能力', desc: 'AI 工程师独立完成代码生成、调试到部署' },
            { icon: <AuditOutlined />, title: '质量保障', desc: '自动化测试看板、缺陷追踪，质量可量化' },
            { icon: <SafetyOutlined />, title: 'CCB 审批链', desc: '高风险操作需 CCB 审批，租约锁防并发写冲突' },
          ].map((item, i) => (
            <div key={i} style={{
              padding: '12px 16px', borderRadius: 8,
              background: '#fafafa', border: '1px solid #f0f0f0',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ color: '#5b5ea6', fontSize: 16 }}>{item.icon}</span>
                <Text strong style={{ fontSize: 14 }}>{item.title}</Text>
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>{item.desc}</Text>
            </div>
          ))}
        </div>
      </Card>

      {/* 注意事项 */}
      <Card style={{ marginBottom: 20 }} title={<Space><WarningOutlined /> 注意事项</Space>}>
        <Alert
          type="warning"
          showIcon
          icon={<WarningOutlined />}
          message="使用前必读"
          description={
            <div style={{ fontSize: 13, lineHeight: 2 }}>
              <div><Tag color="red">重要</Tag> <strong>API Key 由你自己提供</strong>——本平台不提供 LLM API Key，请在"设置"页面配置你自己的 Key。</div>
              <div><Tag color="red">重要</Tag> <strong>API 费用由你承担</strong>——每次 AI 对话、代码生成都会消耗 API 额度，请留意各平台的账单。</div>
              <div><Tag color="orange">注意</Tag> <strong>生成的代码需人工审查</strong>——AI 生成的代码可能有 bug 或安全漏洞，上线前请人工检查。</div>
              <div><Tag color="orange">注意</Tag> <strong>不要在代码中硬编码密钥</strong>——Agent 生成代码时不会有意包含密钥，但请自行检查输出。</div>
              <div><Tag color="blue">提示</Tag> <strong>建议使用 DeepSeek</strong>——性价比高（约 ¥1/百万 token），国内可直接访问。</div>
              <div><Tag color="blue">提示</Tag> <strong>支持本地模型</strong>——可使用 Ollama 运行本地 LLM（如 Qwen2.5 / Llama 3.1），完全免费且数据不出本机。</div>
              <div><Tag color="blue">提示</Tag> <strong>项目可保存到 Gitee</strong>——在"设置 → Gitee 同步"中绑定仓库，手动推送备份。</div>
              <div><Tag color="default">说明</Tag> <strong>管理员初始密码</strong>——Docker 部署后控制台输出，首次登录后请立即修改。</div>
            </div>
          }
        />
      </Card>

      {/* API Key 获取指南 */}
      <Card style={{ marginBottom: 20 }} title={<Space><ApiOutlined /> 各平台 API Key 获取指南</Space>}>
        <div style={{ fontSize: 13, lineHeight: 2.2 }}>
          <div>🔵 <strong>DeepSeek</strong>（推荐）：
            <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">platform.deepseek.com/api_keys</a>
            <br /><Text type="secondary">注册即送 500 万 token，约 ¥1/百万 token</Text>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🟢 <strong>OpenAI</strong>：
            <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">platform.openai.com/api-keys</a>
            <br /><Text type="secondary">需海外信用卡或虚拟卡充值</Text>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🟠 <strong>Anthropic (Claude)</strong>：
            <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com/settings/keys</a>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🔴 <strong>Google (Gemini)</strong>：
            <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noreferrer">aistudio.google.com/app/apikey</a>
            <br /><Text type="secondary">Gemini 2.5 Flash 有免费额度</Text>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🟣 <strong>Kimi (月之暗面)</strong>：
            <a href="https://platform.moonshot.cn/console/api-keys" target="_blank" rel="noreferrer">platform.moonshot.cn/console/api-keys</a>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🟡 <strong>Qwen (阿里通义)</strong>：
            <a href="https://dashscope.console.aliyun.com/apiKey" target="_blank" rel="noreferrer">dashscope.console.aliyun.com/apiKey</a>
          </div>
          <Divider style={{ margin: '8px 0' }} />
          <div>🟤 <strong>Ollama（本地免费）</strong>：
            安装 <code>ollama</code> 后运行 <code>ollama pull qwen2.5:7b</code>，在设置页选择"本地 Ollama"预设，无需 Key。
          </div>
        </div>
      </Card>

      {/* 常见问题 */}
      <Card title={<Space><QuestionCircleOutlined /> 常见问题</Space>}>
        <div style={{ fontSize: 13, lineHeight: 2 }}>
          <div><Text strong>Q: 为什么 Agent 没有反应？</Text><br />
            <Text type="secondary">A: 检查是否在"系统设置"中配置了 API Key 并测试连接成功。</Text></div>
          <Divider style={{ margin: '6px 0' }} />
          <div><Text strong>Q: 可以多人同时使用吗？</Text><br />
            <Text type="secondary">A: 可以。每个人注册独立账号，配置自己的 API Key，互不影响。</Text></div>
          <Divider style={{ margin: '6px 0' }} />
          <div><Text strong>Q: 忘记密码怎么办？</Text><br />
            <Text type="secondary">A: 如果管理员配置了 SMTP 邮箱，可使用密码重置功能。否则联系管理员重置。</Text></div>
          <Divider style={{ margin: '6px 0' }} />
          <div><Text strong>Q: 项目数据存储在哪？</Text><br />
            <Text type="secondary">A: 项目数据存储在服务器的 PostgreSQL 数据库中，可设置 Gitee 同步手动备份。</Text></div>
          <Divider style={{ margin: '6px 0' }} />
          <div><Text strong>Q: 支持哪些编程语言？</Text><br />
            <Text type="secondary">A: Agent 支持 Python、JavaScript/TypeScript、Go、Rust、Java 等主流语言，取决于所选模型的能力。</Text></div>
        </div>
      </Card>
    </div>
  );
};

export default Guide;
