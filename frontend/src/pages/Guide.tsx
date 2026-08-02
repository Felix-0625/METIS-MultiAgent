import React from 'react';
import { Card, Collapse, Steps, Tag, Typography } from 'antd';
import {
  AuditOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CloudDownloadOutlined,
  KeyOutlined,
  PartitionOutlined,
  ProjectOutlined,
  SafetyOutlined,
  TeamOutlined,
  ToolOutlined,
} from '@ant-design/icons';

const { Title, Text } = Typography;

const cardStyle: React.CSSProperties = {
  marginBottom: 20,
  borderRadius: 16,
  border: '1px solid #e8eaf2',
  boxShadow: '0 8px 28px rgba(31, 38, 135, 0.06)',
};

const notes = [
  ['red', '重要', '大模型配置', '请自行提供模型名称、API 地址和 API Key，并先测试连接。'],
  ['red', '重要', '密钥安全', '不要在需求、对话或项目代码中填写、硬编码任何密钥。'],
  ['orange', '注意', '人工审查', '部署或交付前，请检查核心功能、权限配置和安全性。'],
  ['blue', '提示', '本地模型', '支持通过 Ollama 使用本地模型，效果取决于本机配置。'],
  ['blue', '提示', 'Gitee 备份', '可在“设置 → Gitee 同步”中绑定仓库并手动推送。'],
] as const;

const providers = [
  ['DeepSeek', 'https://platform.deepseek.com/api_keys'],
  ['OpenAI', 'https://platform.openai.com/api-keys'],
  ['Anthropic', 'https://console.anthropic.com/settings/keys'],
  ['Google Gemini', 'https://aistudio.google.com/app/apikey'],
  ['Kimi', 'https://platform.moonshot.cn/console/api-keys'],
  ['Qwen', 'https://dashscope.console.aliyun.com/apiKey'],
];

const quickStart = [
  { title: '配置大模型', description: '在设置中填写模型、API 地址和 API Key，测试连接后保存。', icon: <KeyOutlined /> },
  { title: '创建项目', description: '填写项目名称和简介，进入 PM 工作区开始需求沟通。', icon: <ProjectOutlined /> },
  { title: '确认规划', description: '与 PM 确认需求、总规划和各阶段任务，再启动执行。', icon: <TeamOutlined /> },
  { title: '执行与质量检查', description: '按阶段查看任务进度、交付文件、质量结果和返修状态。', icon: <AuditOutlined /> },
  { title: '最终验收与项目整改', description: '完成最终验收后，可下载项目或进入全栈工程师工作台继续整改。', icon: <CheckCircleOutlined /> },
];

const features = [
  [<BulbOutlined />, '想法落地', '通过连续对话整理想法、标签和初步需求。'],
  [<ProjectOutlined />, '项目规划', 'PM 协助确认需求，并生成总规划和阶段任务。'],
  [<TeamOutlined />, '多 Agent 执行', '按任务分配角色，在同一项目目录中完成交付。'],
  [<PartitionOutlined />, '阶段与质量管理', '查看进度，执行阶段检查、返修和完成确认。'],
  [<CloudDownloadOutlined />, '项目文件与交付', '查看生成文件、下载完整项目并保留交付记录。'],
  [<ToolOutlined />, '项目咨询与整改', '项目完成后继续进行咨询、功能变更和代码整改。'],
] as const;

const faqs = [
  ['任务长时间没有变化怎么办？', '先刷新页面获取最新状态，不要连续点击启动、重试或重置。确认显示失败后，再根据失败原因重试。'],
  ['质检出现警告还能继续吗？', '可以。警告属于非阻塞优化建议；只有明确错误才需要返修。'],
  ['如何下载完整项目？', '在项目管理页点击“下载”，系统会生成并下载完整项目 ZIP。'],
  ['项目数据保存在哪里？', '项目数据保存在服务器数据库和持久化存储中；下载的 ZIP 保存在你的设备上。'],
  ['项目完成后还能修改吗？', '可以。在全栈工程师工作台中进行项目咨询、功能变更和代码整改。'],
  ['如何查看生成文件？', '可在项目文件页或全栈工程师工作台查看目录和文件内容。'],
  ['如何备份项目？', '可下载完整项目 ZIP，或在设置中绑定 Gitee 仓库后手动推送。'],
];

const Guide: React.FC = () => (
  <div style={{ maxWidth: 1120, margin: '0 auto', padding: '28px 24px 56px' }}>
    <div style={{ padding: '34px 38px', marginBottom: 22, borderRadius: 20, color: '#fff', background: 'linear-gradient(135deg, #4f46e5 0%, #2563eb 55%, #0891b2 100%)', boxShadow: '0 16px 40px rgba(37, 99, 235, 0.22)' }}>
      <Title level={2} style={{ color: '#fff', margin: 0 }}>使用说明</Title>
      <Text style={{ color: 'rgba(255,255,255,.84)', fontSize: 15 }}>从模型配置到项目交付，按以下流程完成一次完整协作。</Text>
    </div>

    <Card style={cardStyle} title={<><SafetyOutlined /> 使用前注意事项</>}>
      <div style={{ display: 'grid', gap: 10 }}>
        {notes.map(([color, tag, title, text]) => (
          <div key={title} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px', borderRadius: 10, background: '#f8fafc' }}>
            <Tag color={color} style={{ margin: 0, minWidth: 44, textAlign: 'center' }}>{tag}</Tag>
            <Text strong>{title}</Text><Text type="secondary">{text}</Text>
          </div>
        ))}
      </div>
    </Card>

    <Card style={cardStyle} title={<><KeyOutlined /> 各平台 API Key 获取指南</>}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
        {providers.map(([name, url]) => (
          <a key={name} href={url} target="_blank" rel="noreferrer" style={{ padding: '14px 16px', border: '1px solid #e5e7eb', borderRadius: 12, color: '#334155', fontWeight: 600, background: '#fff' }}>
            {name} <span style={{ float: 'right', color: '#94a3b8' }}>↗</span>
          </a>
        ))}
      </div>
    </Card>

    <Card style={cardStyle} title={<><CheckCircleOutlined /> 快速上手</>}>
      <Steps direction="vertical" current={-1} items={quickStart} />
    </Card>

    <Card style={cardStyle} title={<><BulbOutlined /> 核心功能</>}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 14 }}>
        {features.map(([icon, title, desc]) => (
          <div key={title} style={{ display: 'flex', gap: 14, padding: 18, borderRadius: 14, border: '1px solid #edf0f5', background: 'linear-gradient(145deg, #fff, #f8fafc)' }}>
            <div style={{ width: 38, height: 38, flex: '0 0 38px', display: 'grid', placeItems: 'center', borderRadius: 10, color: '#4f46e5', background: '#eef2ff', fontSize: 18 }}>{icon}</div>
            <div><Text strong style={{ display: 'block', marginBottom: 5 }}>{title}</Text><Text type="secondary">{desc}</Text></div>
          </div>
        ))}
      </div>
    </Card>

    <Card style={cardStyle} title={<><AuditOutlined /> 常见问题</>}>
      <Collapse ghost items={faqs.map(([label, children], index) => ({ key: index, label: <Text strong>{label}</Text>, children: <Text type="secondary">{children}</Text> }))} />
    </Card>
  </div>
);

export default Guide;
