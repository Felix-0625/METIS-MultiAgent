/**
 * Optimization — 阶段 3：优化补充与交付
 *
 * 核心能力：
 * - 逐条确认完善（基于需求文档的优化检查清单）
 * - 最终交付确认
 */

import React from 'react';
import { Button, Divider } from 'antd';
import {
  CheckCircleOutlined, RocketOutlined,
  StarOutlined, UserOutlined,
  ThunderboltOutlined, SafetyOutlined,
  ExperimentOutlined,
} from '@ant-design/icons';
import { message as antMessage } from 'antd';
import { parseDocSections } from './types';
import type { OptimizationGroup } from './types';

// 构建优化检查清单
const buildOptimizationChecklist = (sections: Record<string, string>): OptimizationGroup[] => [
  {
    category: '目标清晰度',
    icon: <StarOutlined style={{ color: '#faad14' }} />,
    checks: [
      { label: '项目目标是否用一句话能说清楚？', section: '项目目标' },
      { label: '目标是否可量化（有具体指标）？', section: '验收标准' },
      { label: '是否明确了"不做什么"的边界？', section: '不做什么（Out of Scope）' },
    ],
  },
  {
    category: '用户故事完整性',
    icon: <UserOutlined style={{ color: '#1677ff' }} />,
    checks: [
      { label: '每条用户故事是否有明确的角色？', section: '用户故事' },
      { label: '用户痛点是否在故事中体现？', section: '目标用户' },
      { label: '故事是否覆盖了核心使用场景？', section: '用户故事' },
    ],
  },
  {
    category: '功能优先级',
    icon: <ThunderboltOutlined style={{ color: '#52c41a' }} />,
    checks: [
      { label: 'P0 功能是否都是 MVP 必需的？', section: '核心功能清单（P0/P1/P2）' },
      { label: 'P1/P2 功能是否有明确的推迟理由？', section: '核心功能清单（P0/P1/P2）' },
      { label: '功能之间是否存在依赖关系需要说明？', section: '核心功能清单（P0/P1/P2）' },
    ],
  },
  {
    category: '约束与风险',
    icon: <SafetyOutlined style={{ color: '#ff4d4f' }} />,
    checks: [
      { label: '技术约束是否已明确（平台、性能要求）？', section: '约束条件' },
      { label: '时间和预算约束是否写入文档？', section: '约束条件' },
      { label: '验收标准是否可测试、可量化？', section: '验收标准' },
    ],
  },
  {
    category: '压力测试结论',
    icon: <ExperimentOutlined style={{ color: '#fa8c16' }} />,
    checks: [
      { label: '压力测试中提出的核心质疑是否已解答？', section: '' },
      { label: '竞品对比是否体现了差异化优势？', section: '' },
      { label: '最脆弱的假设是否已在文档中明确？', section: '' },
    ],
  },
];

interface OptimizationProps {
  requirementsDoc: string;
  currentPhase: number;
  convTitle: string;
}

const Optimization: React.FC<OptimizationProps> = ({
  requirementsDoc,
  currentPhase,
  convTitle,
}) => {
  const sections = parseDocSections(requirementsDoc);
  const optimizationChecklist = buildOptimizationChecklist(sections);
  const allDone = optimizationChecklist.every((group: OptimizationGroup) =>
    group.checks.every(check => check.section && sections[check.section])
  );

  const handleConfirmDelivery = () => {
    antMessage.success('交付确认已提交！文档可随时导出。');
  };

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>
      {/* 优化检查清单提示 */}
      <div style={{
        background: '#fffbe6', border: '1px solid #ffe58f', borderRadius: 6,
        padding: '8px 12px', marginBottom: 12, fontSize: 11, color: '#876800',
      }}>
        💡 以下是基于需求文档的结构化优化检查清单，在阶段3对话中可逐条与 Agent 确认
      </div>

      {/* 优化检查清单 */}
      {optimizationChecklist.map((group) => (
        <div key={group.category} style={{ marginBottom: 14 }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            marginBottom: 8, fontSize: 12, fontWeight: 600, color: '#1f1f1f',
          }}>
            {group.icon}
            {group.category}
          </div>
          {group.checks.map((check, idx) => {
            const hasContent = check.section && sections[check.section];
            return (
              <div key={idx} style={{
                display: 'flex', alignItems: 'flex-start', gap: 8,
                padding: '6px 8px', borderRadius: 4, marginBottom: 4,
                background: hasContent ? '#f6ffed' : '#fff',
                border: `1px solid ${hasContent ? '#b7eb8f' : '#f0f0f0'}`,
                fontSize: 11, color: '#262626', lineHeight: 1.5,
              }}>
                <span style={{
                  color: hasContent ? '#52c41a' : '#d9d9d9',
                  flexShrink: 0, marginTop: 1,
                }}>
                  {hasContent ? '✅' : '⭕'}
                </span>
                <span>{check.label}</span>
              </div>
            );
          })}
        </div>
      ))}

      <Divider style={{ margin: '8px 0' }} />

      {/* 图标说明 */}
      <div style={{ fontSize: 11, color: '#8c8c8c', textAlign: 'center', marginBottom: 12 }}>
        绿色 ✅ 表示对应章节已有内容，白色 ⭕ 表示可能需要补充
      </div>

      {/* 最终交付确认 */}
      {currentPhase >= 3 && (
        <div style={{
          background: allDone ? '#f6ffed' : '#fffbe6',
          border: `1px solid ${allDone ? '#b7eb8f' : '#ffe58f'}`,
          borderRadius: 8, padding: '12px 14px', textAlign: 'center',
        }}>
          <RocketOutlined
            style={{
              fontSize: 28, display: 'block', marginBottom: 8,
              color: allDone ? '#52c41a' : '#faad14',
            }}
          />
          <div style={{ fontSize: 13, fontWeight: 600, color: '#1f1f1f', marginBottom: 4 }}>
            最终交付确认
          </div>
          <div style={{ fontSize: 11, color: '#8c8c8c', marginBottom: 10 }}>
            {allDone
              ? '所有章节已覆盖内容，文档可交付。确认提交后将结束本轮优化。'
              : '部分章节仍有待补充，建议继续与 Agent 讨论完善。'}
          </div>
          <Button
            type="primary"
            size="small"
            icon={<CheckCircleOutlined />}
            onClick={handleConfirmDelivery}
            disabled={!allDone}
          >
            确认交付
          </Button>
        </div>
      )}
    </div>
  );
};

export default Optimization;
