import { useEffect, useState } from 'react';
import { wsService } from '../../../services/websocket';

interface UseWebSocketReturn {
  connected: boolean;
  stateVersion: number;
}

export const useWebSocket = (
  projectId: string | undefined,
  onPhasesUpdate: () => void,
  onAgentsUpdate: () => void,
  onMetricsUpdate: () => void,
  onQcUpdate: () => void,
) => {
  const [connected, setConnected] = useState(false);
  const [stateVersion, setStateVersion] = useState(0);

  useEffect(() => {
    if (!projectId) return;

    // 连接 WebSocket
    wsService.connect(projectId);

    // 订阅事件
    const unsubConnected = wsService.on('connected', () => {
      console.log('[PhaseBoard] WebSocket 已连接');
      setConnected(true);
    });

    const unsubDisconnected = wsService.on('disconnected', () => {
      console.log('[PhaseBoard] WebSocket 已断开');
      setConnected(false);
    });

    const unsubPhases = wsService.on('phases_updated', (payload: any) => {
      if (payload && payload.version) {
        setStateVersion(prev => {
          if (payload.version > prev) {
            onPhasesUpdate();
            return payload.version;
          }
          return prev;
        });
      } else {
        onPhasesUpdate();
      }
    });

    const unsubAgents = wsService.on('agents_updated', () => {
      onAgentsUpdate();
    });

    const unsubMetrics = wsService.on('metrics_updated', () => {
      onMetricsUpdate();
    });

    const unsubQc = wsService.on('qc_updated', () => {
      onQcUpdate();
    });

    // 轮询兜底（WebSocket 未连接时每 5 秒轮询一次）
    const pollInterval = setInterval(() => {
      if (!wsService.connected) {
        onPhasesUpdate();
        onAgentsUpdate();
        onMetricsUpdate();
        onQcUpdate();
      }
    }, 5000);

    // 清理
    return () => {
      clearInterval(pollInterval);
      unsubConnected();
      unsubDisconnected();
      unsubPhases();
      unsubAgents();
      unsubMetrics();
      unsubQc();
      wsService.disconnect();
    };
  }, [projectId, onPhasesUpdate, onAgentsUpdate, onMetricsUpdate, onQcUpdate]);

  return {
    connected,
    stateVersion,
  };
};
