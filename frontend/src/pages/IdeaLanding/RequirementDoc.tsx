/**
 * RequirementDoc — 阶段 2：需求文档预览 + 编辑
 *
 * 核心能力：
 * - 结构化文档预览（项目目标、用户画像、功能清单 P0/P1/P2）
 * - 可导出 Markdown
 * - 一键复制文档内容
 */

import React from 'react';
import { Button, Tooltip } from 'antd';
import { CopyOutlined, DownloadOutlined, FileTextOutlined } from '@ant-design/icons';
import { message as antMessage } from 'antd';
import { parseDocSections } from './types';

interface RequirementDocProps {
  requirementsDoc: string;
  convTitle: string;
}

const RequirementDoc: React.FC<RequirementDocProps> = ({ requirementsDoc, convTitle }) => {
  const sections = parseDocSections(requirementsDoc);

  const handleCopyDoc = () => {
    navigator.clipboard.writeText(requirementsDoc).then(() => {
      antMessage.success('文档已复制到剪贴板');
    }).catch(() => {
      antMessage.error('复制失败，请手动选择文本复制');
    });
  };

  const handleDownload = () => {
    const blob = new Blob([requirementsDoc], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${convTitle || '需求文档'}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>
      {/* 操作按钮 */}
      <div style={{
        display: 'flex', justifyContent: 'flex-end', gap: 4,
        marginBottom: 12, flexShrink: 0,
      }}>
        <Tooltip title="复制文档">
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={handleCopyDoc} />
        </Tooltip>
        <Tooltip title="下载 Markdown">
          <Button type="text" size="small" icon={<DownloadOutlined />} onClick={handleDownload} />
        </Tooltip>
      </div>

      {/* 结构化渲染各章节 */}
      {Object.keys(sections).length > 0 ? (
        Object.entries(sections).map(([title, content]) => (
          <div key={title} style={{ marginBottom: 16 }}>
            <div style={{
              fontSize: 12, fontWeight: 700, color: '#1f1f1f',
              borderLeft: '3px solid #1677ff', paddingLeft: 8,
              marginBottom: 6, background: '#f0f5ff', padding: '4px 8px', borderRadius: '0 4px 4px 0',
            }}>
              {title}
            </div>
            <div style={{
              fontSize: 12, lineHeight: 1.8, color: '#262626',
              paddingLeft: 4, whiteSpace: 'pre-wrap',
            }}>
              {content || <span style={{ color: '#bfbfbf' }}>（待补充）</span>}
            </div>
          </div>
        ))
      ) : (
        <pre style={{
          fontSize: 12, lineHeight: 1.7, whiteSpace: 'pre-wrap',
          color: '#262626', margin: 0, fontFamily: 'inherit',
        }}>
          {requirementsDoc}
        </pre>
      )}
    </div>
  );
};

export default RequirementDoc;
