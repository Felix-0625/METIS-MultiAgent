/**
 * API 服务层
 * 统一处理与后端的 REST 和 WebSocket 通信
 */

import axios, { AxiosInstance } from 'axios';
import { create } from 'zustand';
import { API_BASE_URL } from './apiBase';

// 统一让项目中仍在使用的裸 axios 请求也携带 HttpOnly Cookie。
// PhaseBoard 等历史页面有不少 `axios.get(`${API}/...`)` 调用，如果不设置全局
// withCredentials，自动轮询刷新时可能拿不到 Cookie，随后被误判为未登录。
axios.defaults.withCredentials = true;

// 创建 Axios 实例
// timeout 设为 120s：LLM 调用、质检、阶段审查等操作耗时远超 30s
const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000,
  withCredentials: true,  // 携带 HttpOnly Cookie（auth_token）
  headers: {
    'Content-Type': 'application/json',
  },
});

type AuthRecheckResult = 'authenticated' | 'unauthenticated' | 'unknown';

let authRecheckPromise: Promise<AuthRecheckResult> | null = null;

const recheckAuthCookie = async (): Promise<AuthRecheckResult> => {
  if (!authRecheckPromise) {
    authRecheckPromise = axios.get(`${API_BASE_URL}/auth/me`, {
      withCredentials: true,
      timeout: 15000,
    })
      .then(() => 'authenticated' as const)
      .catch((error) => {
        // 只有后端明确返回 401/403 才认为登录失效；网络抖动、超时、502 等都视为 unknown，
        // 避免自动刷新或快速切换 tab 时误跳登录页。
        const status = error.response?.status;
        return status === 401 || status === 403 ? 'unauthenticated' : 'unknown';
      })
      .finally(() => {
        authRecheckPromise = null;
      });
  }
  return authRecheckPromise;
};

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const requestUrl = error.config?.url || '';
    const isAuthMeRequest = requestUrl.includes('/auth/me');

    // 业务请求偶发 401 时先复核 Cookie，避免快速切换 Tab 触发的并发/瞬时失败误判为登出。
    if (error.response?.status === 401 && !isAuthMeRequest && !window.location.pathname.includes('/login')) {
      const authState = await recheckAuthCookie();
      if (authState === 'unauthenticated') {
        window.dispatchEvent(new CustomEvent('metis:unauthorized'));
      }
    }
    // `/auth/me` is intentionally probed while logged out; a 401/403 there is
    // an expected state transition, not an application error.
    if (!(isAuthMeRequest && (error.response?.status === 401 || error.response?.status === 403))) {
      console.error('API Error:', error);
    }
    return Promise.reject(error);
  }
);

// 给历史裸 axios 请求补同样的 401 复核逻辑，但不改变 response 结构，避免影响现有 res.data 用法。
axios.interceptors.response.use(
  (response) => response,
  async (error) => {
    const requestUrl = error.config?.url || '';
    const isAuthMeRequest = requestUrl.includes('/auth/me');
    if (error.response?.status === 401 && !isAuthMeRequest && !window.location.pathname.includes('/login')) {
      const authState = await recheckAuthCookie();
      if (authState === 'unauthenticated') {
        window.dispatchEvent(new CustomEvent('metis:unauthorized'));
      }
    }
    return Promise.reject(error);
  }
);

// ============ 认证 API ============

export const authApi = {
  // 登录（支持用户名或邮箱，后端统一字段名 login）
  login: (login: string, password: string) =>
    apiClient.post('/auth/login', { login, password }),

  logout: () => apiClient.post('/auth/logout'),

  // 自主注册（无需登录）
  register: (username: string, password: string, email: string) =>
    apiClient.post('/auth/register', { username, password, email }),

  // 邮箱验证
  verifyEmail: (email: string, code: string) =>
    apiClient.post('/auth/verify-email', { email, code }),

  // 重发验证邮件
  resendVerification: (email: string) =>
    apiClient.post('/auth/resend-verification', { email }),

  // 忘记密码
  forgotPassword: (email: string) =>
    apiClient.post('/auth/forgot-password', { email }),

  // 重置密码（通过邮件验证码）
  resetPassword: (email: string, code: string, newPassword: string) =>
    apiClient.post('/auth/reset-password', { email, code, new_password: newPassword }),

  // 修改密码（需登录）
  changePassword: (oldPassword: string, newPassword: string) =>
    apiClient.put('/auth/change-password', { old_password: oldPassword, new_password: newPassword }),

  me: () => apiClient.get('/auth/me'),

  listUsers: () => apiClient.get('/auth/users'),

  deleteUser: (userId: string) => apiClient.delete(`/auth/users/${userId}`),
};

// ============ 项目 API ============

export const projectApi = {
  // 获取项目列表
  list: () => apiClient.get('/projects'),
  
  // 获取项目详情
  get: (projectId: string) => apiClient.get(`/projects/${projectId}`),
  
  // 创建项目
  create: (data: { name: string; description: string; requirements?: string }) => 
    apiClient.post('/projects', data, {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }),
  
  // 分析需求
  analyze: (projectId: string, requirements: string) => 
    apiClient.post(`/projects/${projectId}/analyze`, { requirements }),
  
  // 设计方案
  design: (projectId: string, requirements: object) => 
    apiClient.post(`/projects/${projectId}/design`, requirements),
  
  // 获取子项目清单
  getSubprojects: (projectId: string) => 
    apiClient.get(`/projects/${projectId}/subprojects`),
  
  // 确认子项目
  confirmSubproject: (projectId: string, data: { 
    subproject_id: string; 
    confirmed: boolean; 
    modifications?: string 
  }) => apiClient.post(`/projects/${projectId}/subprojects/confirm`, data),
  
  // 生成规划书
  generatePlan: (projectId: string) => 
    apiClient.post(`/projects/${projectId}/plan/generate`),
  
  // 获取项目进度
  getProgress: (projectId: string) => 
    apiClient.get(`/projects/${projectId}/progress`),
};

// ============ 团队 API ============

export const teamApi = {
  // 组建团队
  build: (projectId: string) => apiClient.post(`/projects/${projectId}/team/build`),
  
  // 获取团队状态
  getStatus: (projectId: string) => apiClient.get(`/projects/${projectId}/team/status`),
};

// ============ 任务 API ============

export const taskApi = {
  // 创建任务（后端 POST /projects/{id}/tasks，参数通过 query string 传递）
  create: (projectId: string, data: {
    title: string;
    description: string;
    agent_type: string;
    priority?: number;
  }) => apiClient.post(
    `/projects/${projectId}/tasks`,
    null,
    { params: data }
  ),
  
  // 获取任务列表（后端 GET /projects/{id}/tasks/list）
  list: (projectId: string) => apiClient.get(`/projects/${projectId}/tasks/list`),
};

// ============ 质检 API ============

export const qaApi = {
  // 触发质检（后端 POST /projects/{id}/qc/trigger，subproject_id 通过 query string）
  trigger: (projectId: string, subprojectId: string) => 
    apiClient.post(
      `/projects/${projectId}/qc/trigger`,
      null,
      { params: { subproject_id: subprojectId } }
    ),
  
  // 获取质检结果（后端 GET /projects/{id}/qc/results/{subproject_id}）
  getResults: (projectId: string, subprojectId: string) => 
    apiClient.get(`/projects/${projectId}/qc/results/${subprojectId}`),
};

// ============ 签核 API ============

export const signoffApi = {
  // 读取当前签核门禁及阻断原因
  getStatus: (projectId: string) => apiClient.get(`/projects/${projectId}/signoff/status`),
  // 签核项目
  signoff: (projectId: string) => apiClient.post(`/projects/${projectId}/signoff`),
};

// ============ Skill 池 API ============

export const skillApi = {
  // 获取 Skill 列表
  list: () => apiClient.get('/skills'),
  
  // 导入 Skill（后端需要 name/description/version/content/source 五个字段）
  import: (data: {
    name: string;
    description: string;
    version?: string;
    content: string;
    source?: string;
  }) => apiClient.post('/skills/import', data),
  
  // 搜索 Skill（后端是 POST /skills/search，接受 { query, filters }）
  search: (query: string, filters?: object) => 
    apiClient.post('/skills/search', { query, filters }),
  
  // 删除 Skill
  delete: (skillId: string) => apiClient.delete(`/skills/${skillId}`),
  
  // 获取 Skill 池状态（后端无 PUT /skills/{id}，改为状态查询）
  status: () => apiClient.get('/skills/status'),
};


// ============ Zustand 状态管理 ============

interface AppState {
  // 项目状态
  currentProject: any | null;
  projects: any[];
  
  // 聊天状态
  messages: Array<{ id: string; role: 'user' | 'assistant'; content: string; timestamp: number }>;
  
  // 团队状态
  teamMembers: any[];
  
  // Skill 池状态
  skills: any[];
  
  // UI 状态
  sidebarCollapsed: boolean;
  activeTab: string;
  
  // Actions
  setCurrentProject: (project: any | null) => void;
  setProjects: (projects: any[]) => void;
  addMessage: (message: { role: 'user' | 'assistant'; content: string }) => void;
  clearMessages: () => void;
  setTeamMembers: (members: any[]) => void;
  setSkills: (skills: any[]) => void;
  toggleSidebar: () => void;
  setActiveTab: (tab: string) => void;
}

export const useAppStore = create<AppState>((set) => ({
  currentProject: null,
  projects: [],
  messages: [],
  teamMembers: [],
  skills: [],
  sidebarCollapsed: false,
  activeTab: 'chat',
  
  setCurrentProject: (project) => set({ currentProject: project }),
  setProjects: (projects) => set({ projects }),
  
  addMessage: (message) => set((state) => ({
    messages: [...state.messages, {
      id: Date.now().toString(),
      timestamp: Date.now(),
      ...message
    }]
  })),
  
  clearMessages: () => set({ messages: [] }),
  setTeamMembers: (members) => set({ teamMembers: members }),
  setSkills: (skills) => set({ skills }),
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  setActiveTab: (tab) => set({ activeTab: tab }),
}));

// 导出 API 客户端供直接使用
export { apiClient };
export default apiClient;
