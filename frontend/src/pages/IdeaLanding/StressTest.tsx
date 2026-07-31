/**
 * StressTest — 阶段 1：压力测试对话展示
 *
 * 核心能力：
 * - 对话消息列表，每条消息标注当前阶段号与压力测试上下文
 * - 质疑假设、识别盲区、发现矛盾点的对话界面
 * - 支持三个阶段的不同输入提示
 */

import React from 'react';
import { Button, Empty, Tag, Tooltip } from 'antd';
import {
  SendOutlined, LoadingOutlined,
  RobotOutlined, UserOutlined,
  ReloadOutlined, BulbOutlined, RocketOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import { Input, message as antMessage } from 'antd';
import { PHASES } from './types';
import type { ChatMessage, ConvDetail } from './types';

const { TextArea } = Input;

interface StressTestProps {
  messages: ChatMessage[];
  sending: boolean;
  input: string;
  onInputChange: (value: string) => void;
  onSend: () => void;
  currentPhase: number;
  requirementsDoc: string;
  activeConvId: string | null;
  convDetail: ConvDetail | null;
  msgBoxRef: React.RefObject<HTMLDivElement>;
}

// 每条消息显示一个阶段标签（压力测试阶段标识）
const MessagePhaseTag: React.FC<{ phase: number }> = ({ phase }) => {
  const p = PHASES.find(p => p.key === phase);
  if (!p) return null;
  return (
    <Tag
      color={phase === 1 ? 'orange' : phase === 2 ? 'blue' : 'green'}
      style={{ fontSize: 10, padding: '0 4px', lineHeight: '16px', marginLeft: 6, verticalAlign: 'middle' }}
    >
      {p.icon} 阶段{phase}
    </Tag>
  );
};

const StressTest: React.FC<StressTestProps> = ({
  messages, sending, input, onInputChange, onSend,
  currentPhase, requirementsDoc, activeConvId, convDetail, msgBoxRef,
}) => {
  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSend();
    }
  };

  const handleReload = () => {
    const last = messages.filter(m => m.role === 'user').slice(-1)[0];
    if (last) onInputChange(last.content);
  };

  const phaseQuickQuestions: string[] = currentPhase === 3 ? [
    '帮我检查功能边界条件',
    '核心功能的异常场景怎么处理',
    '验收标准还需要补充什么',
    '有哪些遗漏的用户故事',
  ] : [];

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      {/* 消息列表 */}
      <div
        ref={msgBoxRef}
        style={{
          flex: 1, overflowY: 'auto', padding: '16px 20px',
          display: 'flex', flexDirection: 'column', gap: 12,
        }}
      >
        {messages.length === 0 && !activeConvId && (
          <Empty
            description={
              <div style={{ textAlign: 'center' }}>
                <BulbOutlined style={{ fontSize: 48, color: '#faad14', display: 'block', marginBottom: 12 }} />
                <p style={{ fontSize: 15, fontWeight: 500, color: '#1f1f1f' }}>有什么想法想落地？</p>
                <p style={{ fontSize: 13, color: '#8c8c8c', marginTop: 4 }}>
                  描述你的产品点子、创业设想，或任何想实现的功能
                </p>
                <p style={{ fontSize: 12, color: '#bfbfbf', marginTop: 4 }}>
                  Agent 会先帮你压测这个想法，再生成需求文档
                </p>
              </div>
            }
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            style={{ marginTop: 60 }}
          />
        )}
        {messages.length === 0 && activeConvId && (
          <div style={{ textAlign: 'center', color: '#bfbfbf', marginTop: 40 }}>
            <RocketOutlined style={{ fontSize: 36, display: 'block', marginBottom: 8 }} />
            <p style={{ fontSize: 13 }}>开始描述你的想法</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            style={{
              display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
              alignItems: 'flex-start', gap: 8,
            }}
          >
            {m.role === 'assistant' && (
              <div style={{
                width: 30, height: 30, borderRadius: '50%',
                background: '#fff7e6', border: '1px solid #ffd591',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0, marginTop: 2,
              }}>
                <RobotOutlined style={{ color: '#fa8c16', fontSize: 14 }} />
              </div>
            )}
            <div style={{
              maxWidth: '75%', borderRadius: 10, padding: '10px 14px',
              fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap',
              background: m.role === 'user' ? '#1677ff' : '#fff',
              color: m.role === 'user' ? '#fff' : '#1f1f1f',
              border: m.role === 'assistant' ? '1px solid #f0f0f0' : 'none',
              boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
            }}>
              {m.role === 'assistant' && (
                <div style={{ marginBottom: 4, display: 'flex', alignItems: 'center' }}>
                  <MessagePhaseTag phase={currentPhase} />
                </div>
              )}
              {m.content}
            </div>
            {m.role === 'user' && (
              <div style={{
                width: 30, height: 30, borderRadius: '50%',
                background: '#e6f4ff', border: '1px solid #91caff',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0, marginTop: 2,
              }}>
                <UserOutlined style={{ color: '#1677ff', fontSize: 14 }} />
              </div>
            )}
          </div>
        ))}
        {sending && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              width: 30, height: 30, borderRadius: '50%',
              background: '#fff7e6', border: '1px solid #ffd591',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <LoadingOutlined style={{ color: '#fa8c16', fontSize: 14 }} />
            </div>
            <div style={{
              background: '#fff', border: '1px solid #f0f0f0',
              borderRadius: 10, padding: '10px 14px',
              fontSize: 13, color: '#8c8c8c',
            }}>
              Agent 思考中...
            </div>
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div style={{ padding: '10px 16px', borderTop: '1px solid #f0f0f0', flexShrink: 0, background: '#fff' }}>
        {/* 阶段3快速提问 */}
        {currentPhase === 3 && requirementsDoc && (
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
            <span style={{ fontSize: 11, color: '#8c8c8c', alignSelf: 'center' }}>快捷问：</span>
            {phaseQuickQuestions.map(q => (
              <Tag
                key={q}
                style={{ cursor: 'pointer', fontSize: 11, padding: '1px 6px', lineHeight: '18px' }}
                onClick={() => onInputChange(q)}
              >
                {q}
              </Tag>
            ))}
          </div>
        )}

        {/* 输入框 + 发送按钮 */}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <TextArea
            autoSize={{ minRows: 2, maxRows: 6 }}
            value={input}
            onChange={e => onInputChange(e.target.value)}
            onKeyDown={handleKey}
            placeholder={
              currentPhase === 1
                ? '描述你的想法，Agent 会进行压力测试（Enter 发送，Shift+Enter 换行）'
                : currentPhase === 2
                  ? '确认需求内容，或请 Agent 调整某个章节...'
                  : '逐条讨论优化细节，填补边界条件...'
            }
            disabled={sending}
            style={{ flex: 1, resize: 'none', fontSize: 13 }}
          />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {messages.length > 0 && !sending && (
              <Tooltip title="重新发送上条消息">
                <Button size="small" icon={<ReloadOutlined />} onClick={handleReload} />
              </Tooltip>
            )}
            <Button
              type="primary" size="small"
              icon={<SendOutlined />}
              onClick={onSend}
              loading={sending}
              disabled={!input.trim()}
            >
              发送
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default StressTest;
