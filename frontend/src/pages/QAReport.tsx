/**
 * 质检报告 v3
 * - 从 GET /qc/results 读取真实质检结果（不再依赖 tasks/list）
 * - 触发质检后立即刷新
 * - 四层质检：QA / Perf / Sec / UXO，每层显示分数、问题列表、建议
 */

import React, { useEffect, useState } from 'react';
import {
  Card, Row, Col, Tag, Progress, Statistic, Badge,
  Button, Tabs, Alert, Empty, Spin, List, Collapse, Tooltip, message,
} from 'antd';
import {
  CheckCircleOutlined, CloseCircleOutlined, ExperimentOutlined,
  ThunderboltOutlined, SecurityScanOutlined, SafetyCertificateOutlined,
  BugOutlined, RocketOutlined, ReloadOutlined, ClockCircleOutlined,
  WarningOutlined, EyeOutlined, PlayCircleOutlined,
} from '@ant-design/icons';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import { API_BASE_URL } from '../services/apiBase';

const API = API_BASE_URL;
const { Panel } = Collapse;

// 四层质检维度配置
const QC_DIMS: Record<string, { label: string; icon: React.ReactNode; color: string; desc: string }> = {
  qa:   { label: '功能测试 (QA)',   icon: <ExperimentOutlined />,        color: '#1890ff', desc: '测试用例执行、回归测试、功能覆盖率' },
  perf: { label: '性能测试 (Perf)', icon: <ThunderboltOutlined />,       color: '#fa8c16', desc: '负载测试、瓶颈分析、Core Web Vitals' },
  sec:  { label: '安全审计 (Sec)',  icon: <SecurityScanOutlined />,      color: '#f5222d', desc: '漏洞扫描、合规检查、密钥泄露检测' },
  uxo:  { label: '体验优化 (UXO)',  icon: <SafetyCertificateOutlined />, color: '#52c41a', desc: 'UI/UX审查、等待时间检测、交互流程优化' },
};

const QAReportPage: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const [loading, setLoading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [qcData, setQcData] = useState<any>(null);   // GET /qc/results 返回值
  const [project, setProject] = useState<any>(null);
  const [supervisorFeedback, setSupervisorFeedback] = useState('');

  const fetchData = async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const [qcRes, projRes] = await Promise.all([
        axios.get(`${API}/projects/${projectId}/qc/results`).catch(() => ({ data: null })),
        axios.get(`${API}/projects/${projectId}`).catch(() => ({ data: {} })),
      ]);
      setQcData(qcRes.data);
      setProject(projRes.data);
    } finally {
      setLoading(false);
    }
  };

  const fetchSupervisorFeedback = async () => {
    if (!projectId) return;
    try {
      const res = await axios.get(`${API}/projects/${projectId}/chat-history/supervisor`);
      const msgs = res.data.messages || [];
      const qcMsg = [...msgs].reverse().find((m: any) =>
        m.role === 'assistant' && (
          m.content?.includes('质检') || m.content?.includes('QA') ||
          m.content?.includes('安全') || m.content?.includes('性能')
        )
      );
      if (qcMsg) setSupervisorFeedback(qcMsg.content?.slice(0, 400) || '');
    } catch { /* 静默 */ }
  };

  const triggerAll = async () => {
    if (!projectId) return;
    setTriggering(true);
    try {
      const res = await axios.post(`${API}/projects/${projectId}/qc/trigger-all`);
      const failures = (res.data?.triggered || []).filter((item: any) => item?.error);
      if (failures.length > 0) {
        message.error(`${failures.length} 个子项目质检启动失败，请查看结果并重试`);
      } else {
        message.success('质检已完成，已刷新真实结果');
      }
      await fetchData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '触发质检失败');
    } finally {
      setTriggering(false);
    }
  };

  useEffect(() => {
    fetchData();
    fetchSupervisorFeedback();
  }, [projectId]);

  // 15s 自动刷新
  useEffect(() => {
    const t = setInterval(() => { fetchData(); }, 15000);
    return () => clearInterval(t);
  }, [projectId]);

  const summary = qcData?.summary || {};
  const subprojects: any[] = qcData?.subprojects || [];
  const triggered = summary.triggered === true;

  // ── 综合概览 Tab ──────────────────────────────────────────────────────────
  const SummaryTab = () => (
    <div className="space-y-4">
      {/* Supervisor 反馈 */}
      {supervisorFeedback && (
        <Alert
          type={summary.failed > 0 ? 'warning' : 'success'}
          icon={<EyeOutlined />}
          showIcon
          message={<span className="font-medium text-sm">🔍 Supervisor 质检反馈</span>}
          description={<span className="text-xs">{supervisorFeedback}</span>}
        />
      )}

      {/* 未触发提示 */}
      {!triggered && (
        <Alert
          type="info"
          showIcon
          message="质检尚未触发"
          description={
            <span className="text-sm">
              请在执行期完成后点击「触发质检」按钮，系统将对所有子项目执行四层质检（QA / Perf / Sec / UXO）。
            </span>
          }
          action={
            <Button
              type="primary"
              size="small"
              icon={<PlayCircleOutlined />}
              loading={triggering}
              onClick={triggerAll}
            >
              立即触发质检
            </Button>
          }
        />
      )}

      {/* 四维度汇总卡片 */}
      <Row gutter={12}>
        {Object.entries(QC_DIMS).map(([key, dim]) => {
          // 统计该维度在所有子项目中的结果
          let dimPassed = 0, dimFailed = 0, dimTotal = 0;
          let dimScoreSum = 0;
          subprojects.forEach(sp => {
            const check = sp.checks?.[key];
            if (check) {
              dimTotal++;
              if (check.passed) dimPassed++; else dimFailed++;
              dimScoreSum += check.score ?? 100;
            }
          });
          const avgScore = dimTotal > 0 ? Math.round(dimScoreSum / dimTotal) : 0;

          return (
            <Col span={6} key={key}>
              <Card size="small" className="text-center">
                <div className="text-2xl mb-1" style={{ color: dim.color }}>{dim.icon}</div>
                <div className="text-xs font-medium mb-1">{dim.label}</div>
                {dimTotal === 0 ? (
                  <Tag color="default">未触发</Tag>
                ) : (
                  <>
                    <div className="text-xl font-bold" style={{ color: dimFailed > 0 ? '#ff4d4f' : '#52c41a' }}>
                      {avgScore}分
                    </div>
                    <Tag color={dimFailed > 0 ? 'error' : 'success'} className="mt-1">
                      {dimFailed > 0 ? `${dimFailed} 项未通过` : '全部通过'}
                    </Tag>
                  </>
                )}
              </Card>
            </Col>
          );
        })}
      </Row>

      {/* 总体评估 */}
      <Card size="small" title="总体评估">
        {!triggered ? (
          <Empty description="质检尚未触发" />
        ) : (
          <div className="space-y-3">
            <Row gutter={16}>
              <Col span={6}>
                <Statistic title="检测项总数" value={summary.total || 0} />
              </Col>
              <Col span={6}>
                <Statistic
                  title="通过"
                  value={summary.passed || 0}
                  valueStyle={{ color: '#52c41a' }}
                  prefix={<CheckCircleOutlined />}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="未通过"
                  value={summary.failed || 0}
                  valueStyle={{ color: summary.failed > 0 ? '#ff4d4f' : '#52c41a' }}
                  prefix={summary.failed > 0 ? <BugOutlined /> : <CheckCircleOutlined />}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="综合评分"
                  value={summary.overall_score || 0}
                  suffix="分"
                  valueStyle={{ color: (summary.overall_score || 0) >= 80 ? '#52c41a' : '#ff4d4f' }}
                />
              </Col>
            </Row>
            <Progress
              percent={summary.total > 0 ? Math.round((summary.passed / summary.total) * 100) : 0}
              strokeColor={summary.failed > 0 ? '#ff4d4f' : '#52c41a'}
              format={p => `${p}% 通过`}
            />
          </div>
        )}
      </Card>

      {/* 各子项目质检状态 */}
      {subprojects.length > 0 && triggered && (
        <Card size="small" title="各子项目质检状态">
          {subprojects.map(sp => {
            const checks = sp.checks || {};
            const checkList = Object.values(checks) as any[];
            const spPassed = checkList.filter(c => c.passed).length;
            const spFailed = checkList.filter(c => !c.passed).length;
            const spScore = checkList.length > 0
              ? Math.round(checkList.reduce((s, c) => s + (c.score ?? 100), 0) / checkList.length)
              : 0;

            return (
              <div key={sp.id} className="flex items-center justify-between py-2 border-b last:border-0">
                <div>
                  <span className="text-sm font-medium">{sp.name}</span>
                  <Tag className="ml-2" color={sp.status === 'completed' ? 'success' : 'default'}>{sp.status}</Tag>
                </div>
                <div className="flex items-center gap-2">
                  {checkList.length === 0 ? (
                    <Tag color="default">未质检</Tag>
                  ) : (
                    <>
                      <span className="text-xs text-gray-500">{spScore}分</span>
                      {spPassed > 0 && <Tag color="success">{spPassed} 通过</Tag>}
                      {spFailed > 0 && <Tag color="error">{spFailed} 未通过</Tag>}
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );

  // ── 单维度 Tab ────────────────────────────────────────────────────────────
  const DimTab: React.FC<{ dimKey: string }> = ({ dimKey }) => {
    const dim = QC_DIMS[dimKey];
    // 收集该维度所有子项目的检测结果
    const dimResults = subprojects
      .map(sp => ({ sp, check: sp.checks?.[dimKey] }))
      .filter(x => x.check);

    const passed = dimResults.filter(x => x.check.passed).length;
    const failed = dimResults.filter(x => !x.check.passed).length;
    const avgScore = dimResults.length > 0
      ? Math.round(dimResults.reduce((s, x) => s + (x.check.score ?? 100), 0) / dimResults.length)
      : 0;

    return (
      <div className="space-y-3">
        <Alert
          type="info"
          showIcon={false}
          message={<span className="text-xs text-gray-500">{dim.desc}</span>}
        />
        <Row gutter={12}>
          <Col span={8}>
            <Card size="small"><Statistic title="检测子项目数" value={dimResults.length} /></Card>
          </Col>
          <Col span={8}>
            <Card size="small">
              <Statistic title="通过" value={passed} valueStyle={{ color: '#52c41a' }} prefix={<CheckCircleOutlined />} />
            </Card>
          </Col>
          <Col span={8}>
            <Card size="small">
              <Statistic
                title="平均分"
                value={avgScore}
                suffix="分"
                valueStyle={{ color: avgScore >= 80 ? '#52c41a' : '#ff4d4f' }}
              />
            </Card>
          </Col>
        </Row>

        {dimResults.length === 0 ? (
          <Empty description={`${dim.label} 尚未触发，请点击「触发质检」`} />
        ) : (
          <Collapse size="small">
            {dimResults.map(({ sp, check }) => (
              <Panel
                key={sp.id}
                header={
                  <div className="flex items-center justify-between w-full pr-4">
                    <span className="font-medium text-sm">{sp.name}</span>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400">{check.score ?? 100}分</span>
                      <Tag color={check.passed ? 'success' : 'error'} icon={check.passed ? <CheckCircleOutlined /> : <CloseCircleOutlined />}>
                        {check.passed ? '通过' : '未通过'}
                      </Tag>
                      {check.veto && <Tag color="red">⛔ 已否决</Tag>}
                    </div>
                  </div>
                }
              >
                <div className="space-y-2">
                  <Progress
                    percent={check.score ?? 100}
                    strokeColor={check.passed ? '#52c41a' : '#ff4d4f'}
                    size="small"
                  />
                  {check.issues?.length > 0 && (
                    <div>
                      <div className="text-xs font-medium text-red-500 mb-1">⚠ 发现问题：</div>
                      <List
                        size="small"
                        dataSource={check.issues}
                        renderItem={(issue: string) => (
                          <List.Item className="py-1">
                            <span className="text-xs text-red-400">• {issue}</span>
                          </List.Item>
                        )}
                      />
                    </div>
                  )}
                  {check.suggestions?.length > 0 && (
                    <div>
                      <div className="text-xs font-medium text-blue-500 mb-1">💡 改进建议：</div>
                      <List
                        size="small"
                        dataSource={check.suggestions}
                        renderItem={(s: string) => (
                          <List.Item className="py-1">
                            <span className="text-xs text-blue-400">• {s}</span>
                          </List.Item>
                        )}
                      />
                    </div>
                  )}
                  {!check.issues?.length && !check.suggestions?.length && (
                    <div className="text-xs text-green-500">✅ 未发现问题</div>
                  )}
                  {check.checked_at && (
                    <div className="text-xs text-gray-300">
                      检测时间：{new Date(check.checked_at * 1000).toLocaleString('zh-CN')}
                    </div>
                  )}
                </div>
              </Panel>
            ))}
          </Collapse>
        )}
      </div>
    );
  };

  const tabItems = [
    {
      key: 'summary',
      label: <span><RocketOutlined /> 综合概览</span>,
      children: <SummaryTab />,
    },
    ...Object.entries(QC_DIMS).map(([key, dim]) => {
      // 计算该维度未通过数量（用于 badge）
      const failCount = subprojects.filter(sp => sp.checks?.[key] && !sp.checks[key].passed).length;
      return {
        key,
        label: (
          <span>
            {dim.icon} {dim.label}
            {failCount > 0 && (
              <Badge count={failCount} size="small" style={{ marginLeft: 4, backgroundColor: '#ff4d4f' }} />
            )}
          </span>
        ),
        children: <DimTab dimKey={key} />,
      };
    }),
  ];

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">🔍 质检报告</h2>
        <div className="flex gap-2">
          <Button
            icon={<PlayCircleOutlined />}
            size="small"
            type="primary"
            loading={triggering}
            onClick={triggerAll}
            style={{ backgroundColor: '#1890ff' }}
          >
            触发质检
          </Button>
          <Button icon={<ReloadOutlined />} size="small" loading={loading} onClick={fetchData}>
            刷新
          </Button>
        </div>
      </div>

      {triggered && summary.total > 0 && (
        <Alert
          type={summary.failed > 0 ? 'warning' : 'success'}
          showIcon
          message={
            summary.failed > 0
              ? `发现 ${summary.failed} 项质检未通过，综合评分 ${summary.overall_score} 分`
              : `当前已记录的质检项通过，综合评分 ${summary.overall_score} 分；仍须通过最终整体质检与运行验收，不能直接签核`
          }
        />
      )}

      <Spin spinning={loading}>
        <Tabs items={tabItems} size="small" />
      </Spin>
    </div>
  );
};

export default QAReportPage;
