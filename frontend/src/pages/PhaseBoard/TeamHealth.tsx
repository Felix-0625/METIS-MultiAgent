/**
 * 团队健康度页面 (5.6 设计文档)
 * - ExpertLock 逐条展示（文件级租约锁可视化）
 * - 管理层人员状态（EmployeeProfile）
 * - 失联检测（30分钟+15分钟阈值）
 * - 跨项目资源争用标注
 */
import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Card, Row, Col, Statistic, Tag, Divider, Tooltip, Spin, Empty, Badge, Collapse, List } from 'antd';
import {
  LockOutlined, ClockCircleOutlined, CheckCircleOutlined,
  WarningOutlined, TeamOutlined, UserOutlined, AimOutlined,
  DashboardOutlined, ReloadOutlined, BarChartOutlined,
  RiseOutlined, FallOutlined, ExperimentOutlined,
  ThunderboltOutlined, SecurityScanOutlined, SafetyCertificateOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../../services/apiBase';

const API = API_BASE_URL;

interface ExpertLock {
  lock_id: string;
  expert_id: string;
  project_id: string;
  task_id: string;
  file_scope: string[];
  leased_until: number;
  released_at: number | null;
  is_expired: boolean;
  agent_id?: string;
  agent_role?: string;
}

interface Employee {
  employee_id: string;
  name: string;
  role: string;
  agent_type: string;
  status: string;
  current_projects: string[];
}

interface TeamHealthData {
  execution_layer: {
    expert_id: string;
    locks: ExpertLock[];
    total_files_locked: number;
  }[];
  management_layer: Employee[];
}

const qcDimensions = ['qa', 'perf', 'sec', 'uxo'] as const;
type QcDim = typeof qcDimensions[number];

const qcDimLabels: Record<QcDim, string> = { qa: '功能测试', perf: '性能测试', sec: '安全审计', uxo: '体验优化' };
const qcDimIcons: Record<QcDim, React.ReactNode> = { qa: <ExperimentOutlined />, perf: <ThunderboltOutlined />, sec: <SecurityScanOutlined />, uxo: <SafetyCertificateOutlined /> };
const qcDimColors: Record<QcDim, string> = { qa: '#1890ff', perf: '#fa8c16', sec: '#f5222d', uxo: '#52c41a' };

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN');
}

function isDisconnected(lock: ExpertLock): boolean {
  if (!lock.leased_until || lock.is_expired) {
    const now = Date.now() / 1000;
    return now - lock.leased_until > 15 * 60;
  }
  return false;
}

const TeamHealth: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [projectMetrics, setProjectMetrics] = useState<any>(null);
  const [qcResultsSummary, setQcResultsSummary] = useState<any>(null);
  const [teamData, setTeamData] = useState<TeamHealthData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    Promise.allSettled([
      axios.get(`${API}/projects/${id}/metrics`),
      axios.get(`${API}/projects/${id}/qc/results`),
      axios.get(`${API}/projects/${id}/locks`),
    ]).then(([metricsRes, qcRes, locksRes]) => {
      if (metricsRes.status === 'fulfilled') setProjectMetrics(metricsRes.value.data);
      if (qcRes.status === 'fulfilled') setQcResultsSummary(qcRes.value.data);
      if (locksRes.status === 'fulfilled') {
        // 合并专家锁数据
        const locks = locksRes.value.data.locks || [];
        const execLayer: TeamHealthData['execution_layer'] = [];
        const expertMap: Record<string, TeamHealthData['execution_layer'][0]> = {};
        locks.forEach((lock: ExpertLock) => {
          if (!expertMap[lock.expert_id]) {
            expertMap[lock.expert_id] = { expert_id: lock.expert_id, locks: [], total_files_locked: 0 };
            execLayer.push(expertMap[lock.expert_id]);
          }
          expertMap[lock.expert_id].locks.push(lock);
          expertMap[lock.expert_id].total_files_locked += lock.file_scope?.length || 0;
        });
        setTeamData({ execution_layer: execLayer, management_layer: [] });
      }
    }).finally(() => setLoading(false));
  }, [id]);

  if (loading) return <div className="flex items-center justify-center" style={{ height: 400 }}><Spin><div style={{ height: 200 }} /></Spin></div>;

  const qcStats: Record<string, { total: number; passed: number; avgScore: number }> = {};
  qcDimensions.forEach(dim => {
    qcStats[dim] = { total: 0, passed: 0, avgScore: 0 };
    let scoreSum = 0;
    (qcResultsSummary?.subprojects || []).forEach((sp: any) => {
      const check = sp.checks?.[dim];
      if (check) { qcStats[dim].total++; if (check.passed) qcStats[dim].passed++; scoreSum += check.score ?? 100; }
    });
    if (qcStats[dim].total > 0) qcStats[dim].avgScore = Math.round(scoreSum / qcStats[dim].total);
  });

  const disconnectedExperts = teamData?.execution_layer.filter(e => e.locks.some(l => isDisconnected(l))) || [];

  return (
    <div className="space-y-4">
      {/* 需要处理区：失联检测 */}
      {disconnectedExperts.length > 0 && (
        <Card size="small" style={{ border: '1px solid #ff4d4f', background: '#fff2f0' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <WarningOutlined style={{ color: '#ff4d4f', fontSize: 16 }} />
            <span style={{ fontWeight: 600, color: '#ff4d4f' }}>需要处理 · {disconnectedExperts.length} 位专家疑似失联</span>
          </div>
          <List size="small" dataSource={disconnectedExperts} renderItem={exp => (
            <List.Item>
              <span style={{ color: '#595959' }}>专家 {exp.expert_id} 最近活动超过 15 分钟未更新</span>
              <Badge status="error" text="疑似失联" />
            </List.Item>
          )} style={{ marginTop: 8 }} />
        </Card>
      )}

      {/* 常态区：质量指标总览 */}
      {projectMetrics && (
        <Card size="small" style={{ borderRadius: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <DashboardOutlined style={{ fontSize: 16, color: '#1890ff' }} />
              <span style={{ fontWeight: 600, fontSize: 14 }}>项目质量指标总览</span>
            </div>
            <Tooltip title="来自 /metrics 和 /qc/results 接口数据">
              <InfoCircleOutlined style={{ color: '#8c8c8c', fontSize: 12 }} />
            </Tooltip>
          </div>
          <Row gutter={[12, 12]}>
            <Col xs={12} sm={12} md={6}>
              <Card size="small" hoverable><Statistic title="质量总问题数" value={projectMetrics.issues?.total || 0} valueStyle={{ color: projectMetrics.issues?.total > 0 ? '#ff4d4f' : '#52c41a' }} prefix={<BarChartOutlined />} /></Card>
            </Col>
            <Col xs={12} sm={12} md={6}>
              <Card size="small" hoverable><Statistic title="质量修复循环" value={projectMetrics.qc_fix_cycles_by_phase ? (Object.values(projectMetrics.qc_fix_cycles_by_phase) as number[]).reduce((a, b) => a + b, 0) : 0} valueStyle={{ color: '#fa8c16' }} prefix={<ReloadOutlined />} suffix="次" /></Card>
            </Col>
            <Col xs={12} sm={12} md={6}>
              <Card size="small" hoverable><Statistic title="人工介入次数" value={projectMetrics.manual_interventions || 0} valueStyle={{ color: projectMetrics.manual_interventions > 0 ? '#ff4d4f' : '#52c41a' }} prefix={<UserOutlined />} suffix="次" /></Card>
            </Col>
            <Col xs={12} sm={12} md={6}>
              <Card size="small" hoverable><Statistic title="阶段一次性通过率" value={projectMetrics.first_pass_rate != null ? Math.round(projectMetrics.first_pass_rate * 100) : 0} valueStyle={{ color: (projectMetrics.first_pass_rate || 0) >= 0.8 ? '#52c41a' : '#fa8c16' }} prefix={projectMetrics.first_pass_rate >= 0.8 ? <RiseOutlined /> : <FallOutlined />} suffix="%" /></Card>
            </Col>
          </Row>
        </Card>
      )}

      {/* 执行层：ExpertLock 逐条展示 */}
      <Card size="small" title={<span><LockOutlined style={{ marginRight: 8 }} />执行层 · 文件级租约锁 (ExpertLock)</span>} style={{ borderRadius: 8 }}>
        {!teamData || teamData.execution_layer.length === 0 ? (
          <Empty description="暂无执行专家锁记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Collapse size="small" items={teamData.execution_layer.map(exp => ({
            key: exp.expert_id,
            label: (
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <UserOutlined />
                <span style={{ fontWeight: 600 }}>{exp.locks[0]?.agent_role || exp.locks[0]?.agent_id || exp.expert_id}</span>
                <Tag color="blue">{exp.total_files_locked} 个文件锁定</Tag>
                {exp.locks.some(l => isDisconnected(l)) && <Tag color="red">疑似失联</Tag>}
              </div>
            ),
            children: (
              <List size="small" dataSource={exp.locks} renderItem={lock => (
                <List.Item>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', flexWrap: 'wrap' }}>
                    <code style={{ fontSize: 11, background: '#f5f5f5', padding: '2px 6px', borderRadius: 4 }}>
                      {lock.lock_id}
                    </code>
                    <span style={{ fontSize: 12, color: '#595959' }}>
                      {Array.isArray(lock.file_scope) ? lock.file_scope.slice(0, 3).join(', ') : String(lock.file_scope)}
                      {(Array.isArray(lock.file_scope) && lock.file_scope.length > 3) ? ` +${lock.file_scope.length - 3}` : ''}
                    </span>
                    <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Tooltip title={`租约到期: ${formatTime(lock.leased_until)}`}>
                        <Tag icon={<ClockCircleOutlined />} color={lock.is_expired ? 'red' : 'green'}>
                          {lock.is_expired ? '已过期' : `有效期至 ${new Date(lock.leased_until * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`}
                        </Tag>
                      </Tooltip>
                      {lock.released_at ? (
                        <Tag icon={<CheckCircleOutlined />} color="default">已释放</Tag>
                      ) : lock.is_expired ? (
                        <Badge status="error" />
                      ) : (
                        <Badge status="processing" />
                      )}
                      {isDisconnected(lock) && <Tag color="red">⚠ 疑似失联</Tag>}
                    </div>
                  </div>
                </List.Item>
              )} />
            ),
            extra: exp.locks.some(l => isDisconnected(l)) ? <WarningOutlined style={{ color: '#ff4d4f' }} /> : undefined,
          }))} />
        )}
      </Card>

      {/* 四层质检维度统计 (如有数据) */}
      {qcResultsSummary?.subprojects?.length > 0 && (
        <Card size="small" title={<span><AimOutlined style={{ marginRight: 8 }} />四层质检维度统计</span>} style={{ borderRadius: 8 }}>
          <Row gutter={[8, 8]}>
            {qcDimensions.map(dim => {
              const stat = qcStats[dim];
              return (
                <Col xs={12} sm={12} md={6} key={dim}>
                  <Card size="small" style={{ textAlign: 'center', background: stat.total > 0 ? '#fafafa' : '#f5f5f5' }}>
                    <div style={{ fontSize: 20, color: qcDimColors[dim], marginBottom: 4 }}>{qcDimIcons[dim]}</div>
                    <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 4 }}>{qcDimLabels[dim]}</div>
                    {stat.total > 0 ? (
                      <>
                        <div style={{ fontSize: 18, fontWeight: 700, color: stat.avgScore >= 80 ? '#52c41a' : '#ff4d4f' }}>{stat.avgScore}分</div>
                        <div style={{ fontSize: 10, color: '#8c8c8c', marginTop: 2 }}>{stat.passed}/{stat.total} 通过</div>
                      </>
                    ) : (
                      <Tag color="default" style={{ fontSize: 10 }}>未触发</Tag>
                    )}
                  </Card>
                </Col>
              );
            })}
          </Row>
        </Card>
      )}
    </div>
  );
};

export default TeamHealth;
