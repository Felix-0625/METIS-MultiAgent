import React from 'react';
import { Button, Tag, Tooltip } from 'antd';
import {
  LoadingOutlined, BugOutlined, PlayCircleOutlined,
} from '@ant-design/icons';
import { ReviewResult, Issue } from './types';

interface DefectListProps {
  review: ReviewResult;
  phaseId: string;
  projectId: string;
  isLastPhase: boolean;
  batchSubmitting: string | null;
  onBatchSubmit: (phaseId: string) => void;
  onIssueClick: (issue: Issue, phaseId: string) => void;
  onTriggerFix: (phaseId: string, issue: Issue) => void;
  triggeringFix: string | null;
  onNavigateEngineer: () => void;
}

const DefectList: React.FC<DefectListProps> = ({
  review, phaseId, projectId, isLastPhase,
  batchSubmitting, onBatchSubmit, onIssueClick, onTriggerFix, triggeringFix,
  onNavigateEngineer,
}) => {
  const activeIssues = review.issues.filter((i: any) =>
    i.status === 'open' || i.status === 'needs_manual'
  );
  const openCount = activeIssues.filter((i: any) => i.status === 'open').length;
  const fixingCount = review.issues.filter((i: any) => i.status === 'fixing').length;
  const manualCount = activeIssues.filter((i: any) => i.status === 'needs_manual').length;
  const fixedTotal = review.issues.filter((i: any) => i.status === 'fixed' || i.status === 'verified').length;

  // 以文件为单位聚合
  const fileMap = new Map<string, any[]>();
  for (const issue of activeIssues) {
    const key = issue.file_path || '（未知文件）';
    if (!fileMap.has(key)) fileMap.set(key, []);
    fileMap.get(key)!.push(issue);
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ fontSize: 11 }}>
          {openCount > 0 && <span style={{ color: '#ff7b72', marginRight: 8 }}>🔶 {openCount} 待修复</span>}
          {fixingCount > 0 && <span style={{ color: '#58a6ff', marginRight: 8 }}>🔵 {fixingCount} 修复中</span>}
          {manualCount > 0 && <span style={{ color: '#ffc53d', marginRight: 8 }}>🟛 {manualCount} 需人工</span>}
          {fixedTotal > 0 && <span style={{ color: '#3fb950', fontSize: 10 }}>✅ {fixedTotal} 已修复（已隐藏）</span>}
        </span>
        {openCount > 0 && isLastPhase && (() => {
          const totalFiles = fileMap.size;
          const batchFiles = Math.min(totalFiles, 5);
          return (
            <Button
              size="small" danger
              icon={batchSubmitting === phaseId ? <LoadingOutlined /> : <BugOutlined />}
              loading={batchSubmitting === phaseId}
              style={{ fontSize: 10, height: 22 }}
              onClick={() => onBatchSubmit(phaseId)}
            >
              一键返工（{batchFiles}/{totalFiles} 文件）
            </Button>
          );
        })()}
      </div>
      {Array.from(fileMap.entries()).map(([filePath, issues]) => {
        const hasOpen = issues.some((i: any) => i.status === 'open');
        const hasManual = issues.some((i: any) => i.status === 'needs_manual');
        const fileBorderColor = hasManual ? '#d48806' : hasOpen ? '#ff7b72' : '#58a6ff';
        const fileBgColor = hasManual ? '#2a1a00' : hasOpen ? '#2a1a1a' : '#1a2a3a';

        return (
          <div key={filePath} style={{
            marginBottom: 8, borderRadius: 4, background: fileBgColor,
            borderLeft: `3px solid ${fileBorderColor}`, overflow: 'hidden',
          }}>
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '4px 8px', borderBottom: '1px solid #30363d',
            }}>
              <span style={{ color: '#e6edf3', fontSize: 11, fontWeight: 600 }}>
                📫 {filePath}
                <span style={{ color: '#8b949e', fontWeight: 400, marginLeft: 6 }}>
                  ({issues.length} 个问题)
                </span>
                {/* 双重门状态徽标 (5.3.4) */}
                {issues.some((i: any) => i.security_status === 'open') && (
                  <Tag color="default" style={{ fontSize: 9, marginLeft: 6 }}>Sec-OPEN</Tag>
                )}
                {issues.some((i: any) => i.security_status === 'in_flight') && (
                  <Tag color="orange" style={{ fontSize: 9, marginLeft: 6 }}>Sec-检测中</Tag>
                )}
                {issues.some((i: any) => i.security_status === 'verified') && (
                  <Tag color="green" style={{ fontSize: 9, marginLeft: 6 }}>Sec-通过</Tag>
                )}
                {issues.some((i: any) => i.security_status === 'failed') && (
                  <Tag color="red" style={{ fontSize: 9, marginLeft: 6 }}>Sec-未通过</Tag>
                )}
              </span>
              {hasOpen && isLastPhase && (
                <Button size="small" danger icon={<BugOutlined />}
                  style={{ fontSize: 10, height: 20 }}
                  onClick={() => {
                    const firstOpen = issues.find((i: any) => i.status === 'open');
                    if (firstOpen) onIssueClick(firstOpen, phaseId);
                  }}>
                  反馈给PM
                </Button>
              )}
            </div>
            {issues.map((issue: any, idx: number) => {
              const isManual = issue.status === 'needs_manual';
              const isFixing = issue.status === 'fixing';
              const textColor = isManual ? '#ffc53d' : isFixing ? '#58a6ff' : '#ff7b72';
              return (
                <div key={issue.id} style={{
                  padding: '4px 8px', borderBottom: idx < issues.length - 1 ? '1px solid #21262d' : 'none',
                  display: 'flex', alignItems: 'flex-start', gap: 6,
                }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ color: textColor, fontSize: 11 }}>
                      {isManual ? '🟛' : isFixing ? '🔵' : '🔶'} {issue.message}
                    </div>
                    {issue.fix_hint && !isManual && (
                      <div style={{ color: '#8b949e', fontSize: 10, marginTop: 1 }}>建议：{issue.fix_hint}</div>
                    )}
                    {isManual && issue.needs_manual_reason && (
                      <div style={{ color: '#ffc53d', fontSize: 10, marginTop: 1 }}>⏹️ {issue.needs_manual_reason}</div>
                    )}
                     {issue.fix_rounds > 0 && (
                       <div style={{ marginTop: 2, display: 'flex', gap: 6, alignItems: 'center' }}>
                         <span style={{ color: '#8b949e', fontSize: 10 }}>已尝试 {issue.fix_rounds} 次修复</span>
                         {issue.fix_rounds >= 3 && <Tag color="red" style={{ fontSize: 9 }}>即将转人工</Tag>}
                       </div>
                     )}
                     {/* 安全门独立徽标 */}
                     {issue.security_status && issue.security_status !== 'open' && (
                       <div style={{ marginTop: 1 }}>
                         <Tag color={issue.security_status === 'verified' ? 'green' : issue.security_status === 'failed' ? 'red' : 'orange'} style={{ fontSize: 9 }}>
                           SecStatus: {issue.security_status}
                         </Tag>
                       </div>
                     )}
                     {issue.file_version_hash && (
                       <div style={{ color: '#484f58', fontSize: 9, marginTop: 1 }}>
                         ver: {String(issue.file_version_hash).slice(0, 7)}
                       </div>
                     )}
                   </div>
                  <div style={{ flexShrink: 0, display: 'flex', gap: 4, alignItems: 'center' }}>
                    {isManual && (
                      <>
                        <Tooltip title={issue.needs_manual_reason || '已超过最大自动修复次数，请人工检查代码'}>
                          <Tag color="warning" style={{ fontSize: 10, margin: 0 }}>需人工</Tag>
                        </Tooltip>
                        <Tooltip title="提交给全栈工程师：在工程师工作台进行人工整改">
                          <Button
                            size="small"
                            style={{ fontSize: 10, height: 20, borderColor: '#722ed1', color: '#722ed1' }}
                            onClick={onNavigateEngineer}
                          >
                            🜜 工程师
                          </Button>
                        </Tooltip>
                      </>
                    )}
                    {isFixing && (
                      <>
                        <Tag color="processing" style={{ fontSize: 10, margin: 0 }}>修复中</Tag>
                        <Tooltip title="PM已给出修复方案，点击触发对应 Agent 重新执行">
                          <Button size="small"
                            icon={triggeringFix === issue.id ? <LoadingOutlined /> : <PlayCircleOutlined />}
                            loading={triggeringFix === issue.id}
                            style={{ fontSize: 10, height: 20, borderColor: '#58a6ff', color: '#58a6ff' }}
                            onClick={() => onTriggerFix(phaseId, issue)}>
                            触发修复
                          </Button>
                        </Tooltip>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        );
      })}
      {activeIssues.length === 0 && (
        <div style={{ color: '#3fb950', fontSize: 11, textAlign: 'center', padding: '8px 0' }}>
          ✅ 所有问题已修复，可以进入下一阶段
        </div>
      )}
    </>
  );
};

export default DefectList;
