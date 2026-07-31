import React, { useState } from 'react';
import { Alert, Button, Tag, Tooltip, message } from 'antd';
import {
  BugOutlined, ReloadOutlined, CheckCircleOutlined,
  OrderedListOutlined, LoadingOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { ChatMsg, ExpertReq, API } from './types';

interface PlanSidebarProps {
  messages: ChatMsg[];
  phaseDesc: string;
  phaseId: string;
  projectId: string;
  onStartPhase: () => void;
  starting: boolean;
  phaseStarted: boolean;
  initialExpertReqs?: ExpertReq[];
  initialPlanGenerated?: boolean;
  onRefresh: () => void;
  chatHeight: number;
}

const PlanSidebar: React.FC<PlanSidebarProps> = ({
  messages,
  phaseDesc,
  phaseId,
  projectId,
  onStartPhase,
  starting,
  phaseStarted,
  initialExpertReqs = [],
  initialPlanGenerated = false,
  onRefresh,
  chatHeight,
}) => {
  const [expertReqs, setExpertReqs] = useState<ExpertReq[]>(initialExpertReqs);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [planGenerated, setPlanGenerated] = useState(initialPlanGenerated || initialExpertReqs.length > 0);
  const [planError, setPlanError] = useState('');
  const [planWarnings, setPlanWarnings] = useState<string[]>([]);

  React.useEffect(() => {
    setExpertReqs(initialExpertReqs);
    setPlanGenerated(initialPlanGenerated || initialExpertReqs.length > 0);
  }, [phaseId, initialExpertReqs, initialPlanGenerated]);

  const fixPlans = React.useMemo(() => {
    if (!phaseStarted) return [];
    const tagged = messages.filter(m => m.role === 'assistant' && m.type === 'pm_fix_plan');
    if (tagged.length > 0) return tagged.map(m => m.content).slice(-5);
    return messages
      .filter(m =>
        m.role === 'assistant' &&
        !m.type &&
        (m.content.includes('返工指令') || m.content.includes('修改点')) &&
        !m.content.includes('质检报告') &&
        !m.content.includes('综合评分') &&
        !m.content.includes('Layer') &&
        !m.content.includes('检查模式')
      )
      .map(m => m.content)
      .slice(-5);
  }, [messages, phaseStarted]);

  const generatePlan = async () => {
    setLoadingPlan(true);
    setPlanError('');
    setPlanWarnings([]);
    try {
      const userRequirements = messages
        .filter(msg => msg.role === 'user' && msg.content.trim())
        .slice(-6)
        .map(msg => msg.content.trim())
        .join('\n');
      const res: any = await axios.post(`${API}/projects/${projectId}/phases/${phaseId}/plan-experts`, {
        phase_description: phaseDesc,
        user_requirements: userRequirements,
      });
      const rawReqs = Array.isArray(res.data?.expert_requirements)
        ? res.data.expert_requirements
        : [];
      const validReqs = rawReqs.filter((req: any) =>
        req && typeof req.task_name === 'string' && req.task_name.trim()
        && typeof req.required_role === 'string' && req.required_role.trim()
      );
      if (validReqs.length === 0) {
        throw new Error('规划结果缺少有效任务，请重试');
      }
      setExpertReqs(validReqs);
      setPlanGenerated(true);
      const warnings = Array.isArray(res.data?.warnings) ? res.data.warnings : [];
      setPlanWarnings(warnings);
      message.success(res.data?.auto_corrected
        ? `阶段规划已自动校正并生成 ${validReqs.length} 个锁定任务`
        : `阶段PM已生成 ${validReqs.length} 个专家需求规划`);
    } catch (e: any) {
      const detail = e.response?.data?.detail;
      const detailText = typeof detail === 'string'
        ? detail
        : [detail?.message, ...(Array.isArray(detail?.violations) ? detail.violations : [])]
          .filter(Boolean).join('；');
      const errorText = detailText || e.message || '阶段规划生成失败';
      setPlanError(errorText);
      message.error(errorText);
    } finally {
      setLoadingPlan(false);
    }
  };

  if (phaseStarted) {
    const hasFixPlans = fixPlans.length > 0;
    return (
      <div style={{
        width: 240, flexShrink: 0, border: `2px solid ${hasFixPlans ? '#1677ff' : '#d9d9d9'}`,
        borderRadius: 8, background: '#fafafa', display: 'flex', flexDirection: 'column',
        height: chatHeight, transition: 'border-color 0.3s',
      }}>
        <div style={{
          padding: '7px 10px', background: hasFixPlans ? '#e6f4ff' : '#f5f5f5',
          borderBottom: `1px solid ${hasFixPlans ? '#91caff' : '#e8e8e8'}`,
          display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0,
        }}>
          <BugOutlined style={{ color: hasFixPlans ? '#1677ff' : '#8c8c8c', fontSize: 12 }} />
          <span style={{ fontSize: 12, fontWeight: 600, color: '#262626', flex: 1 }}>PM修复方案</span>
          <Tooltip title="刷新（从PM对话中重新提取）">
            <Button type="text" size="small" icon={<ReloadOutlined />} onClick={onRefresh}
              style={{ color: '#8c8c8c', padding: '0 4px', height: 20, fontSize: 11 }} />
          </Tooltip>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px', minHeight: 0 }}>
          {!hasFixPlans ? (
            <div style={{ color: '#bfbfbf', fontSize: 11, textAlign: 'center', marginTop: 24 }}>
              反馈问题给PM后<br />修复方案将在此显示<br />
              <span style={{ fontSize: 10 }}>（点击「一键反馈全部给PM」）</span>
            </div>
          ) : (
            fixPlans.map((plan, i) => (
              <div key={i} style={{
                fontSize: 11, color: '#262626', padding: '6px 8px', marginBottom: 6,
                background: '#fff', border: '1px solid #d6e4ff', borderRadius: 4,
                lineHeight: 1.6, whiteSpace: 'pre-wrap',
              }}>
                {plan.length > 300 ? plan.slice(0, 300) + '...' : plan}
              </div>
            ))
          )}
        </div>
        <div style={{ padding: '8px 10px', borderTop: '1px solid #e8e8e8', flexShrink: 0 }}>
          <Button block size="small" disabled style={{ color: '#52c41a', borderColor: '#52c41a', fontSize: 11 }}>
            <CheckCircleOutlined /> 阶段进行中
          </Button>
        </div>
      </div>
    );
  }

  const hasReqs = expertReqs.length > 0;
  const borderColor = hasReqs ? '#52c41a' : '#d9d9d9';
  return (
    <div style={{
      width: 240, flexShrink: 0, border: `2px solid ${borderColor}`,
      borderRadius: 8, background: '#fafafa', display: 'flex', flexDirection: 'column',
      height: chatHeight, transition: 'border-color 0.3s',
    }}>
      <div style={{
        padding: '7px 10px', background: hasReqs ? '#f6ffed' : '#f5f5f5',
        borderBottom: `1px solid ${hasReqs ? '#b7eb8f' : '#e8e8e8'}`,
        display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0,
      }}>
        <OrderedListOutlined style={{ color: hasReqs ? '#52c41a' : '#8c8c8c', fontSize: 12 }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: '#262626', flex: 1 }}>阶段规划</span>
        <Tooltip title="重新生成规划">
          <Button type="text" size="small" icon={<ReloadOutlined />} onClick={onRefresh}
            style={{ color: '#8c8c8c', padding: '0 4px', height: 20, fontSize: 11 }} />
        </Tooltip>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px', minHeight: 0 }}>
        {planError && (
          <Alert
            type="error"
            showIcon
            message="阶段规划未生成"
            description={planError}
            style={{ marginBottom: 8, fontSize: 11 }}
          />
        )}
        {planWarnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="阶段规划已自动校正"
            description={planWarnings.join('；')}
            style={{ marginBottom: 8, fontSize: 11 }}
          />
        )}
        {!planGenerated ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 10 }}>
            <div style={{ color: '#8c8c8c', fontSize: 11, textAlign: 'center', lineHeight: 1.6 }}>
              与阶段PM沟通后<br />点击下方按钮生成<br />结构化专家规划
            </div>
            <Button size="small" type="dashed"
              icon={loadingPlan ? <LoadingOutlined /> : <OrderedListOutlined />}
              loading={loadingPlan} onClick={generatePlan}
              style={{ fontSize: 11, borderColor: '#1677ff', color: '#1677ff' }}>
              生成阶段规划
            </Button>
          </div>
        ) : expertReqs.length === 0 ? (
          <div style={{ color: '#ff4d4f', fontSize: 11, textAlign: 'center', marginTop: 24 }}>
            规划生成失败，请重试
          </div>
        ) : (
          expertReqs.map((req, i) => {
            const taskName = String(req?.task_name || '未命名任务');
            const requiredRole = String(req?.required_role || '执行专家');
            const taskDescription = String(req?.task_description || '');
            const implementationMethod = String(
              req?.implementation_method || req?.implementation || '',
            );
            const techStack = Array.isArray(req?.tech_stack) ? req.tech_stack : [];
            const responsibilities = Array.isArray(req?.responsibilities) ? req.responsibilities : [];
            const personnelAllocation = Array.isArray(req?.personnel_allocation)
              ? req.personnel_allocation
              : [];
            const criteria = Array.isArray(req?.acceptance_criteria) ? req.acceptance_criteria : [];
            return (
            <div key={req?.task_id || `${phaseId}-task-${i}`} style={{
              marginBottom: 8, padding: '6px 8px', background: '#fff',
              border: '1px solid #d9f7be', borderRadius: 4, fontSize: 11,
            }}>
              <div style={{ fontWeight: 600, color: '#262626', marginBottom: 2, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span>{i + 1}. {taskName}</span>
                <Tag style={{ fontSize: 9, margin: 0, padding: '0 4px' }}
                  color={req.priority === 'high' ? 'red' : req.priority === 'low' ? 'default' : 'blue'}>
                  {req.priority === 'high' ? '高' : req.priority === 'low' ? '低' : '中'}
                </Tag>
              </div>
              <div style={{ color: '#1677ff', fontSize: 10, marginBottom: 2 }}>
                所需角色: {requiredRole}
              </div>
              {taskDescription && (
                <div style={{ color: '#595959', fontSize: 10, lineHeight: 1.4 }}>
                  任务细节: {taskDescription}
                </div>
              )}
              {implementationMethod && (
                <div style={{ color: '#595959', fontSize: 10, lineHeight: 1.4 }}>
                  实现方式: {implementationMethod}
                </div>
              )}
              {techStack.length > 0 && (
                <div style={{ color: '#595959', fontSize: 10, lineHeight: 1.4 }}>
                  技术栈: {techStack.join('、')}
                </div>
              )}
              {responsibilities.length > 0 && (
                <div style={{ color: '#595959', fontSize: 10, lineHeight: 1.4 }}>
                  职责: {responsibilities.join('；')}
                </div>
              )}
              {(req?.personnel_count || personnelAllocation.length > 0) && (
                <div style={{ color: '#595959', fontSize: 10, lineHeight: 1.4 }}>
                  人员分配: {
                    personnelAllocation.length > 0
                      ? personnelAllocation.join('；')
                      : `${req.personnel_count} 人`
                  }
                </div>
              )}
              {criteria.length > 0 && (
                <div style={{ marginTop: 3, color: '#52c41a', fontSize: 10 }}>
                  验收条件: {criteria.map(String).join('；')}
                </div>
              )}
            </div>
          );})
        )}
      </div>
      <div style={{ padding: '8px 10px', borderTop: `1px solid ${hasReqs ? '#b7eb8f' : '#e8e8e8'}`, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {planGenerated && (
          <Button block size="small" icon={loadingPlan ? <LoadingOutlined /> : <ReloadOutlined />}
            loading={loadingPlan} onClick={generatePlan}
            style={{ fontSize: 11, borderColor: '#52c41a', color: '#52c41a' }}>
            重新生成规划
          </Button>
        )}
        <Button block size="small" type="primary"
          icon={starting ? <LoadingOutlined /> : <CheckCircleOutlined />}
          loading={starting} onClick={onStartPhase}
          disabled={!planGenerated || starting}
          style={{ fontSize: 11 }}>
          {starting ? '启动中...' : '开始执行阶段'}
        </Button>
      </div>
    </div>
  );
};

export default PlanSidebar;
