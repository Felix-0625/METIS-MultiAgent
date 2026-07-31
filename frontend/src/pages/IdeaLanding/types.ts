/**
 * IdeaLanding — 共享类型与常量
 */

import type React from 'react';

// ─── 类型定义 ────────────────────────────────────────────────────────────────

export interface ConvListItem {
  conv_id: string;
  title: string;
  tags: string[];
  pinned: boolean;
  category: string;
  current_phase: number;
  phase_name: string;
  phase_completed: Record<string, boolean>;
  message_count: number;
  last_message: string;
  created_at: number;
  updated_at: number;
  is_active: boolean;
  has_requirements_doc: boolean;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  ts: number;
}

export interface ConvDetail {
  conv_id: string;
  title: string;
  tags: string[];
  pinned: boolean;
  category: string;
  current_phase: number;
  phase_completed: Record<string, boolean>;
  messages: { role: string; content: string }[];
  requirements_doc: string;
  context_summary: string;
}

export interface OptimizationCheck {
  label: string;
  section: string;
}

export interface OptimizationGroup {
  category: string;
  icon: React.ReactNode;
  checks: OptimizationCheck[];
}

// ─── 阶段配置 ────────────────────────────────────────────────────────────────

export const PHASES = [
  {
    key: 1,
    label: '压力测试',
    icon: '🔟',
    color: '#fa8c16',
    desc: '识别假设、盲区、矛盾，把想法压实',
  },
  {
    key: 2,
    label: '需求文档',
    icon: '📵',
    color: '#1677ff',
    desc: '生成结构化需求文档',
  },
  {
    key: 3,
    label: '优化补充',
    icon: '⭐',
    color: '#52c41a',
    desc: '逐条深化，消除歧义，产出可交付文档',
  },
];

export const CATEGORIES = ['默认', '产品', '技术', '商业', '创意', '其他'];
export const PHASE_COLORS: Record<number, string> = { 1: 'orange', 2: 'blue', 3: 'green' };

// ─── 工具函数 ────────────────────────────────────────────────────────────────

export const fmtTime = (ts: number) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
};

// 从对话列表中提取所有唯一标签
export const extractAllTags = (convList: ConvListItem[]): string[] => {
  const tagSet = new Set<string>();
  convList.forEach(c => c.tags.forEach(t => tagSet.add(t)));
  return Array.from(tagSet).sort();
};

// 解析需求文档中的各个章节，用于优化建议联动
export const parseDocSections = (doc: string): Record<string, string> => {
  const sections: Record<string, string> = {};
  const lines = doc.split('\n');
  let currentKey = '';
  let currentLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith('## ')) {
      if (currentKey) sections[currentKey] = currentLines.join('\n').trim();
      currentKey = line.replace('## ', '').trim();
      currentLines = [];
    } else {
      currentLines.push(line);
    }
  }
  if (currentKey) sections[currentKey] = currentLines.join('\n').trim();
  return sections;
};
