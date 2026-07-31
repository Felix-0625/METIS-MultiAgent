import { API_BASE_URL } from './apiBase';

/**
 * WebSocket 实时通信服务（单例）
 * 
 * 提供按项目频道的 WebSocket 连接管理、心跳检测、断线重连（指数退避）、
 * 事件监听机制。连接成功后上层可停止 HTTP 轮询；断线后自动切回轮询兜底。
 */

type WsCallback = (payload: any) => void;

class WebSocketService {
  private ws: WebSocket | null = null;
  private projectId: string | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private listeners: Map<string, Set<WsCallback>> = new Map();
  private _connected: boolean = false;
  private _reconnectAttempts: number = 0;
  private readonly MAX_RECONNECT_DELAY = 16000; // 16s
  private readonly BASE_DELAY = 1000;          // 1s
  private readonly PING_INTERVAL = 30000;      // 30s
  private wsUrl: string = '';

  get connected(): boolean {
    return this._connected;
  }

  get currentProjectId(): string | null {
    return this.projectId;
  }

  connect(projectId: string) {
    if (this.ws && this._connected && this.projectId === projectId) {
      return; // 已连接相同项目，无需重复连接
    }

    this.disconnect();
    this.projectId = projectId;

    const apiBase = API_BASE_URL;
    // 浏览器自动携带 HttpOnly Cookie，无需显式传递 token
    this.wsUrl = apiBase.replace(/^http/, 'ws') + `/ws/${projectId}`;

    try {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = () => this.handleOpen();
      this.ws.onmessage = (event) => this.handleMessage(event.data);
      this.ws.onerror = () => this.handleError();
      this.ws.onclose = () => this.handleClose();
    } catch (error) {
      console.warn('[WS] 创建连接失败，将使用轮询兜底', error);
      this._connected = false;
    }
  }

  disconnect() {
    this.clearTimers();
    this._connected = false;
    this._reconnectAttempts = 0;

    if (this.ws) {
      this.ws.onopen = null;
      this.ws.onmessage = null;
      this.ws.onerror = null;
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  on(eventType: string, callback: WsCallback): () => void {
    if (!this.listeners.has(eventType)) {
      this.listeners.set(eventType, new Set());
    }
    this.listeners.get(eventType)!.add(callback);
    return () => {
      this.listeners.get(eventType)?.delete(callback);
    };
  }

  off(eventType: string, callback: WsCallback) {
    this.listeners.get(eventType)?.delete(callback);
  }

  // ── 私有方法 ──

  private handleOpen() {
    this._connected = true;
    this._reconnectAttempts = 0;
    console.log(`[WS] 已连接 project=${this.projectId}`);
    this.startPing();
    this.emit('connected', { projectId: this.projectId });
  }

  private handleMessage(rawData: string) {
    try {
      const data = JSON.parse(rawData);
      if (data.type === 'pong') return;
      this.emit(data.type, data.payload);
    } catch {
      // 忽略解析失败的消息
    }
  }

  private handleError() {
    console.warn('[WS] 连接错误');
    this._connected = false;
  }

  private handleClose() {
    this._connected = false;
    this.clearTimers();
    console.log('[WS] 连接已关闭');
    this.emit('disconnected', { projectId: this.projectId });
    this.attemptReconnect();
  }

  private startPing() {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send('ping');
      }
    }, this.PING_INTERVAL);
  }

  private stopPing() {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private clearTimers() {
    this.stopPing();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private attemptReconnect() {
    if (this.reconnectTimer !== null) return; // 已有重连定时器

    const delay = Math.min(
      this.BASE_DELAY * Math.pow(2, this._reconnectAttempts),
      this.MAX_RECONNECT_DELAY
    );

    this._reconnectAttempts++;
    console.log(`[WS] ${delay}ms 后尝试重连 (第${this._reconnectAttempts}次)`);

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.projectId) {
        this.connect(this.projectId);
      }
    }, delay);
  }

  private emit(eventType: string, payload: any) {
    const callbacks = this.listeners.get(eventType);
    if (callbacks) {
      callbacks.forEach(cb => {
        try { cb(payload); } catch { /* 静默忽略单个监听器异常 */ }
      });
    }
  }
}

export const wsService = new WebSocketService();
export type { WsCallback };
