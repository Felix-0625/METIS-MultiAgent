import React from 'react';
import { Progress, Tag, Button, Collapse, List, Badge, Tooltip } from 'antd';
import { ReloadOutlined, CheckCircleOutlined, ExclamationCircleOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { PhaseInfo } from './types';

interface PlanReview {
  review_id: string;
  reviewer_expert_id: string;
  reviewer_role: string;
  round: number;
  concern: string;
  response: string;
  outcome: 'resolved' | 'overridden';
  justification?: string;
}

interface ReviewChainProps {
  phases: PhaseInfo[];
  confirmedPhases: Set<string>;
  completedCount: number;
  onRefresh: () => void;
}

const ReviewChain: React.FC<ReviewChainProps> = ({ phases, confirmedPhases, completedCount, onRefresh }) => {
  // 收集所有阶段的审阅链数据
  const phasesWithReviews = phases.filter(p => {
    const reviews = (p as any).plan_reviews;
    return reviews && Array.isArray(reviews) && reviews.length > 0;
  });

  return (
    <div style={{ background: '#fff', borderRadius: 8, padding: '14px 16px', border: '1px solid #e8e8e8', boxShadow: '0 1px 2px rgba(0,0,0,0.04)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <span style={{ fontWeight: 600, fontSize: 14 }}>项目阶段进度 · 专家审阅链</span>
        <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>刷新</Button>
      </div>
      <Progress
        percent={Math.round(completedCount / phases.length * 100)}
        format={() => `${completedCount}/${phases.length} 阶段完成`}
        strokeColor={{ '0%': '#1677ff', '100%': '#52c41a' }}
        style={{ margin: '4px 0' }}
      />
      <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        {phases.map((p, i) => {
          const reviews: PlanReview[] = (p as any).plan_reviews || [];
          const hasOverridden = reviews.some(r => r.outcome === 'overridden');
          const status = (p as any).review_chain_status || 'pending';
          return (
            <Tag
              key={p.phase_id}
              color={confirmedPhases.has(p.phase_id) || p.status === 'completed' ? 'success' : p.status === 'active' ? 'processing' : 'default'}
              style={{ fontSize: 11 }}
            >
              {i + 1}. {p.name}
              {reviews.length > 0 && (
                <span style={{ marginLeft: 4, fontSize: 9 }}>
                  ({status === 'resolved' ? '已审阅' : `${reviews.length}条审阅`})
                </span>
              )}
              {hasOverridden && <ExclamationCircleOutlined style={{ color: '#faad14', marginLeft: 4, fontSize: 10 }} />}
            </Tag>
          );
        })}
      </div>

      {/* 专家审阅链可视化 (5.3.1) */}
      {phasesWithReviews.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#595959', marginBottom: 8 }}>
            专家审阅链
          </div>
          {phasesWithReviews.map(phase => {
            const reviews: PlanReview[] = (phase as any).plan_reviews || [];
            // 按 expert 分组
            const expertMap = new Map<string, PlanReview[]>();
            reviews.forEach(r => {
              const key = r.reviewer_expert_id || r.reviewer_role;
              if (!expertMap.has(key)) expertMap.set(key, []);
              expertMap.get(key)!.push(r);
            });

            return (
              <Collapse key={phase.phase_id} size="small" ghost items={[{
                key: phase.phase_id,
                label: (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                    <span style={{ fontWeight: 600 }}>{phase.name}</span>
                    <Tag color="blue" style={{ fontSize: 10 }}>{reviews.length} 条审阅</Tag>
                    {(phase as any).review_chain_status === 'resolved' && (
                      <Tag icon={<CheckCircleOutlined />} color="success" style={{ fontSize: 10 }}>已收敛</Tag>
                    )}
                  </div>
                ),
                children: (
                  <div style={{ paddingLeft: 12 }}>
                    {Array.from(expertMap.entries()).map(([expertKey, expertReviews]) => {
                      const maxRound = Math.max(...expertReviews.map(r => r.round));
                      const hasOverridden = expertReviews.some(r => r.outcome === 'overridden');
                      const isResolved = expertReviews.every(r => r.outcome === 'resolved');
                      return (
                        <div key={expertKey} style={{
                          marginBottom: 8, padding: '8px 10px',
                          background: hasOverridden ? '#fffbe6' : '#f6ffed',
                          border: `1px solid ${hasOverridden ? '#ffe58f' : '#b7eb8f'}`,
                          borderRadius: 6,
                        }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                            <span style={{ fontWeight: 600, fontSize: 11 }}>{expertKey}</span>
                            <Tag color={isResolved ? 'success' : 'warning'} style={{ fontSize: 10 }}>
                              {isResolved ? '已确认无异议' : `round ${maxRound}/3`}
                            </Tag>
                            {hasOverridden && (
                              <Tag color="red" style={{ fontSize: 10 }}>overridden</Tag>
                            )}
                          </div>
                          <List size="small" dataSource={expertReviews.sort((a, b) => a.round - b.round)} renderItem={review => (
                            <List.Item style={{ padding: '2px 0', fontSize: 11 }}>
                              <div style={{ width: '100%' }}>
                                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                                  <Badge status={review.outcome === 'resolved' ? 'success' : 'warning'} />
                                  <span style={{ color: '#595959' }}>Round {review.round}:</span>
                                  <span style={{ color: '#262626' }}>{review.concern}</span>
                                </div>
                                {review.response && (
                                  <div style={{ color: '#8c8c8c', fontSize: 10, paddingLeft: 20 }}>
                                    回应: {review.response}
                                  </div>
                                )}
                                {(review.outcome === 'overridden' && review.justification) && (
                                  <div style={{ color: '#faad14', fontSize: 10, paddingLeft: 20 }}>
                                    推进理由: {review.justification}
                                  </div>
                                )}
                              </div>
                            </List.Item>
                          )} />
                        </div>
                      );
                    })}
                  </div>
                ),
              }]} />
            );
          })}
        </div>
      )}
    </div>
  );
};

export default ReviewChain;
