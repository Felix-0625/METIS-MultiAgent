/**
 * 跨阶段冲突处理区 (5.3.6)
 * 展示 Supervisor 组长发现的跨阶段接口冲突，CCB 建议方案
 */
import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Card, List, Tag, Button, message, Spin, Empty, Alert } from 'antd';
import {
  WarningOutlined, CheckCircleOutlined, CloseOutlined, AimOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { API_BASE_URL } from '../../services/apiBase';

const API = API_BASE_URL;

interface Conflict {
  replan_id: string;
  trigger: string;
  involved_phases: string[];
  reason: string;
  proposed_solution: string;
  status: string;
}

const CrossStageConflicts: React.FC = () => {
  const { id: projectId } = useParams<{ id: string }>();
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!projectId) return;
    axios.get(`${API}/projects/${projectId}/cross-stage-conflicts`)
      .then(res => setConflicts(res.data.conflicts || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [projectId]);

  if (loading) return <Spin size="small" />;

  const activeConflicts = conflicts.filter(c => c.status !== 'resolved');
  if (activeConflicts.length === 0) return null;

  return (
    <Card size="small" style={{
      border: '1px solid #faad14', background: '#fffbe6', borderRadius: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <WarningOutlined style={{ color: '#faad14', fontSize: 16 }} />
        <span style={{ fontWeight: 600, color: '#d48806' }}>
          需要处理 · 跨阶段冲突 ({activeConflicts.length})
        </span>
        <Tag color="warning">跨阶段接口冲突</Tag>
      </div>
      <Alert
        type="warning"
        message="Supervisor 组长在最终质检时发现跨阶段产出之间的接口冲突，已提交 CCB 分析。"
        style={{ marginBottom: 12 }}
        showIcon
      />
      <List size="small" dataSource={activeConflicts} renderItem={conflict => (
        <List.Item>
          <div style={{ width: '100%' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <AimOutlined style={{ color: '#faad14' }} />
              <span style={{ fontWeight: 600, fontSize: 12 }}>
                涉及阶段: {(conflict.involved_phases || []).join(', ')}
              </span>
            </div>
            <div style={{ fontSize: 11, color: '#595959', marginBottom: 4 }}>
              {conflict.reason}
            </div>
            {conflict.proposed_solution && (
              <div style={{
                fontSize: 11, color: '#262626', background: '#fff',
                padding: '6px 10px', borderRadius: 4,
                border: '1px solid #d9d9d9', marginBottom: 6,
              }}>
                <span style={{ fontWeight: 600 }}>CCB 建议方案:</span> {conflict.proposed_solution}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <Button size="small" type="primary"
                icon={<CheckCircleOutlined />}
                style={{ fontSize: 11 }}
                onClick={async () => {
                  try {
                    await axios.post(
                      `${API}/projects/${projectId}/cross-stage-conflicts/${conflict.replan_id}/accept`
                    );
                    message.success('已接受 CCB 建议方案');
                    setConflicts(prev => prev.map(c =>
                      c.replan_id === conflict.replan_id ? { ...c, status: 'resolved' } : c
                    ));
                  } catch { message.error('操作失败'); }
                }}>
                接受建议方案
              </Button>
              <Button size="small"
                icon={<CloseOutlined />}
                style={{ fontSize: 11 }}
                onClick={() => {
                  message.info('已标记为待人工处理');
                  setConflicts(prev => prev.filter(c => c.replan_id !== conflict.replan_id));
                }}>
                我有不同意见
              </Button>
            </div>
          </div>
        </List.Item>
      )} />
    </Card>
  );
};

export default CrossStageConflicts;
