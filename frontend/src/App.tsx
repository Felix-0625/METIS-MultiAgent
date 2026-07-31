/**
 * 应用入口
 * 侧边栏：项目 / 想法落地 / 资源中心 / 设置 / 用户管理（管理员）
 * 平台感知：桌面端侧边栏不折叠，网页端可折叠
 * 认证守卫：未登录自动跳转登录页
 * Token 存储：HttpOnly Cookie（后端 Set-Cookie，前端 JS 无法读取，防 XSS）
 */

import React, { Suspense, lazy, useState, useEffect } from 'react';
import { Routes, Route, Navigate, useParams, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu } from 'antd';
import type { MenuProps } from 'antd';
import {
  BulbOutlined, SettingOutlined,
  MenuFoldOutlined, MenuUnfoldOutlined, FolderOpenOutlined, AppstoreOutlined,
  TeamOutlined, PartitionOutlined, DashboardOutlined,
  AuditOutlined, ToolOutlined, CodeOutlined,
  ArrowLeftOutlined, LogoutOutlined, UsergroupAddOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons';
import { detectPlatform } from './platform/detect';
import type { Platform } from './platform/detect';
import { apiClient, authApi } from './services/api';

const ProjectList = lazy(() => import('./pages/ProjectList'));
const ProgressBoard = lazy(() => import('./pages/ProgressBoard'));
const FileBrowser = lazy(() => import('./pages/FileBrowser'));
const ProjectAgents = lazy(() => import('./pages/ProjectAgents'));
const QAReport = lazy(() => import('./pages/QAReport'));
const Settings = lazy(() => import('./pages/Settings'));
const PMTeamChat = lazy(() => import('./pages/PMTeamChat'));
const PhaseBoard = lazy(() => import('./pages/PhaseBoard'));
const EngineerWorkspace = lazy(() => import('./pages/EngineerWorkspace'));
const TeamHealth = lazy(() => import('./pages/PhaseBoard/TeamHealth'));
const SupervisorLeaderPage = lazy(() => import('./pages/SupervisorLeaderPage'));
const ProjectAdjustment = lazy(() => import('./pages/ProjectAdjustment'));
const IdeaLanding = lazy(() => import('./pages/IdeaLanding'));
const ResourceCenter = lazy(() => import('./pages/ResourceCenter'));
const TestDashboard = lazy(() => import('./pages/TestDashboard'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const UserManagement = lazy(() => import('./pages/UserManagement'));
const Guide = lazy(() => import('./pages/Guide'));

const { Sider, Content } = Layout;

// ━━━ 辅助函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** 从 sessionStorage 读取当前用户信息（仅供 UI 显示，非安全存储） */
function getCurrentUser() {
  const userJson = sessionStorage.getItem('current_user');
  return userJson ? JSON.parse(userJson) : null;
}

/** 退出登录：调用后端 /auth/logout 清除 Cookie + 清除 sessionStorage */
async function performLogout(navigate: ReturnType<typeof useNavigate>) {
  try {
    await authApi.logout();
  } catch {}
  sessionStorage.removeItem('current_user');
  navigate('/login', { replace: true });
}

// ━━━ 项目详情布局 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const ProjectLayout: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [projectName, setProjectName] = useState('');

  useEffect(() => {
    if (!id) return;
    // 使用 withCredentials 的 axios 实例，无需手动传 token
    apiClient.get(`/projects/${id}`)
      .then((res: any) => setProjectName(res?.name || ''))
      .catch(() => {});
  }, [id]);

  const tabItems = [
    { key: 'pm-team',     label: 'PM 组长',     icon: <TeamOutlined />,      external: false },
    { key: 'phase-board', label: '阶段看板',    icon: <PartitionOutlined />,  external: false },
    { key: 'board',       label: '进度看板',    icon: <DashboardOutlined />,  external: false },
    { key: 'files',       label: '文件管理',    icon: <FolderOpenOutlined />, external: false },
    { key: 'team',        label: '团队页',      icon: <TeamOutlined />,      external: false },
    { key: 'supervisor',  label: '监督组长',    icon: <AuditOutlined />,      external: false },
    { key: 'adjustment',  label: '项目调整',    icon: <ToolOutlined />,       external: false },
    { key: 'engineer',    label: '全栈工程师',  icon: <CodeOutlined />,       external: true  },
  ];

  const activeKey = tabItems.find(t => !t.external && location.pathname.includes(`/${t.key}`))?.key || 'pm-team';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{
        position: 'sticky', top: 0, zIndex: 60,
        background: '#fff', borderBottom: '1px solid #e0e0e0',
        padding: '16px 24px 0', boxShadow: '0 2px 8px rgba(0,0,0,0.03)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <button onClick={() => navigate('/projects')} style={{
            background: '#f5f5f5', border: '1px solid #e0e0e0', borderRadius: 8,
            padding: '5px 14px', cursor: 'pointer', fontSize: 13, color: '#595959',
            display: 'flex', alignItems: 'center', gap: 6, fontWeight: 500, whiteSpace: 'nowrap',
          }}>
            <ArrowLeftOutlined style={{ fontSize: 13 }} /> 返回项目列表
          </button>
          {projectName && (
            <span style={{ fontSize: 15, fontWeight: 700, color: '#1e1b1b', letterSpacing: -0.2 }}>
              {projectName}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 20, paddingBottom: 0 }}>
          {tabItems.map(item => (
            <button
              key={item.key}
              onClick={() => item.external ? navigate(`/engineer/${id}`) : navigate(`/projects/${id}/${item.key}`)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 7,
                padding: '10px 4px 12px', fontSize: 13, fontWeight: 500,
                color: !item.external && activeKey === item.key ? '#5b5ea6'
                     : item.external ? '#7c3aed' : '#8c8c8c',
                borderBottom: !item.external && activeKey === item.key ? '2px solid #5b5ea6'
                     : '2px solid transparent', transition: 'all 0.2s ease', whiteSpace: 'nowrap',
              }}
            >
              <span style={{ fontSize: 15, display: 'flex', alignItems: 'center' }}>{item.icon}</span>
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <Routes>
          <Route path="pm-team"     element={<div style={{ height: '100%', overflow: 'hidden', padding: '16px 20px' }}><PMTeamChat /></div>} />
          <Route path="phase-board" element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><PhaseBoard /></div>} />
          <Route path="board"       element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><ProgressBoard /></div>} />
          <Route path="files"       element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><FileBrowser /></div>} />
          <Route path="supervisor"  element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><SupervisorLeaderPage /></div>} />
          <Route path="adjustment"  element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><ProjectAdjustment /></div>} />
          <Route path="team"       element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><TeamHealth /></div>} />
          <Route path="agents"      element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><ProjectAgents /></div>} />
          <Route path="qa"          element={<div style={{ height: '100%', overflowY: 'auto', padding: '16px 20px' }}><QAReport /></div>} />
          <Route path="*"           element={<Navigate to="pm-team" replace />} />
        </Routes>
      </div>
    </div>
  );
};

// ━━━ 认证守卫 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const AuthGuard: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [checking, setChecking] = useState(true);
  const [valid, setValid] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    // 仅在应用启动时验证 HttpOnly Cookie，避免每次 Tab 切换都触发鉴权请求并因瞬时失败误回登录页。
    authApi.me()
      .then(() => { setValid(true); setChecking(false); })
      .catch((error: any) => {
        const status = error?.response?.status;
        // 只有明确 401/403 才清登录态；网络抖动/后端短暂不可用时，如果本地仍有用户信息，
        // 先保持页面可用，避免自动刷新期间点击 tab 被误导向登录页。
        if (status === 401 || status === 403) {
          sessionStorage.removeItem('current_user');
          setValid(false);
        } else {
          setValid(!!sessionStorage.getItem('current_user'));
        }
        setChecking(false);
      });

    const handleUnauthorized = () => {
      sessionStorage.removeItem('current_user');
      setValid(false);
      const fromPath = window.location.pathname.replace(/^\/app(?=\/|$)/, '') || '/projects';
      navigate('/login', { replace: true, state: { from: fromPath } });
    };
    const handleAuthenticated = () => {
      setValid(true);
      setChecking(false);
    };
    window.addEventListener('metis:unauthorized', handleUnauthorized);
    window.addEventListener('metis:authenticated', handleAuthenticated);
    return () => {
      window.removeEventListener('metis:unauthorized', handleUnauthorized);
      window.removeEventListener('metis:authenticated', handleAuthenticated);
    };
  }, [navigate]);

  if (checking) return <div style={{ minHeight: '100vh', background: '#f5f5f5' }} />;

  // 已登录用户访问 /login → 重定向到项目列表，避免短暂闪烁
  if (valid && location.pathname === '/login') {
    return <Navigate to="/projects" replace />;
  }

  // 未登录且不在 /login → 重定向到登录页，state 保存当前路径供登录后回跳
  if (!valid && location.pathname !== '/login') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
};

// ━━━ 主应用 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const App: React.FC = () => {
  const [platform, setPlatform] = useState<Platform>('web');
  const [collapsed, setCollapsed] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    const p = detectPlatform();
    setPlatform(p);
    if (p === 'desktop') setCollapsed(false);
  }, []);

  const currentUser = getCurrentUser();
  const isAdmin = currentUser?.role === 'admin';


  const menuItems: MenuProps['items'] = [
    { key: '/projects',  icon: <FolderOpenOutlined />,  label: '项目' },
    { key: '/idea',      icon: <BulbOutlined />,         label: '想法落地' },
    { key: '/resources', icon: <AppstoreOutlined />,     label: '资源中心' },
    { key: '/guide',     icon: <QuestionCircleOutlined />, label: '使用说明' },
    { key: '/settings',  icon: <SettingOutlined />,      label: '设置' },
    ...(isAdmin
      ? [{ key: '/users', icon: <UsergroupAddOutlined />, label: '用户管理' }]
      : []
    ),
  ];

  const selectedKey =
    menuItems
      .map(item => (item as any).key as string)
      .sort((a, b) => b.length - a.length)
      .find(k => location.pathname.startsWith(k)) || '/projects';

  // 登录页保持独立布局，避免未认证用户看到需要登录后才能使用的导航。
  const isLoginRoute = location.pathname === '/login';

  return (
    <AuthGuard>
      {isLoginRoute ? (
        <Suspense fallback={<div style={{ minHeight: '100vh', background: '#f5f5f5' }} />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
          </Routes>
        </Suspense>
      ) : (
      <Layout className="min-h-screen">
        <Sider
          trigger={null} collapsible collapsed={collapsed} width={240}
          style={{
            position: 'fixed', height: '100vh', left: 0, top: 0, zIndex: 100,
            background: '#1c1b1f', borderRight: '1px solid rgba(255,255,255,0.06)',
            display: 'flex', flexDirection: 'column',
          }}
        >
          <div className="flex flex-col items-center justify-center select-none"
            style={{ height: 90, padding: '16px 8px 12px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
            {collapsed
              ? <span className="text-2xl font-bold" style={{ background: 'linear-gradient(135deg, #a78bfa 0%, #818cf8 100%)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>M</span>
              : (
                <div className="flex flex-col items-center leading-tight w-full">
                  <span style={{
                    fontSize: 24, fontWeight: 800, letterSpacing: 5,
                    background: 'linear-gradient(135deg, #a78bfa 0%, #818cf8 50%, #c084fc 100%)',
                    WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', marginBottom: 4,
                  }}>M E T I S</span>
                  <span style={{ fontSize: 9, color: 'rgba(255,255,255,0.35)', fontWeight: 500, letterSpacing: 2 }}>
                    AI MULTI-AGENT SYSTEM
                  </span>
                </div>
              )
            }
          </div>

          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', paddingTop: 28 }}>
            <Menu mode="inline" selectedKeys={[selectedKey]} items={menuItems}
              onClick={({ key }) => navigate(key)} className="border-0"
              style={{ fontSize: 15, background: 'transparent', color: 'rgba(255,255,255,0.55)' }}
              theme="dark" />
          </div>

          {currentUser && !collapsed && (
            <div
              data-testid="current-user-info"
              aria-label={`当前用户 ${currentUser.username}`}
              style={{
              padding: '12px 16px', borderTop: '1px solid rgba(255,255,255,0.06)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span
                data-testid="current-username"
                style={{ color: 'rgba(255,255,255,0.55)', fontSize: 13 }}
              >
                👤 {currentUser.username}
              </span>
              <button onClick={() => performLogout(navigate)} title="退出登录"
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'rgba(255,255,255,0.35)', fontSize: 16,
                  padding: '4px 8px', borderRadius: 4,
                }}>
                <LogoutOutlined />
              </button>
            </div>
          )}

          {platform === 'web' && (
            <div className="absolute bottom-4 left-0 right-0 flex justify-center cursor-pointer"
              style={{ color: 'rgba(255,255,255,0.25)' }}
              onClick={() => setCollapsed(!collapsed)}>
              {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            </div>
          )}
        </Sider>

        <Layout style={{ marginLeft: collapsed ? 80 : 240, transition: 'margin-left 0.2s' }}>
          <Content style={{ minHeight: '100vh', background: '#f5f5f5' }}>
            <Suspense fallback={<div style={{ minHeight: '100%', background: '#f5f5f5' }} />}>
            <Routes>
              <Route path="/"           element={<Navigate to="/projects" replace />} />
              <Route path="/login"      element={<LoginPage />} />
              <Route path="/users"      element={<UserManagement />} />
              <Route path="/agents"     element={<Navigate to="/resources" replace />} />
              <Route path="/experts"    element={<Navigate to="/resources" replace />} />
              <Route path="/skills"     element={<Navigate to="/resources" replace />} />
              <Route path="/ide"        element={<Navigate to="/settings" replace />} />
              <Route path="/test-board" element={<Navigate to="/dev/test-dashboard" replace />} />
              <Route path="/projects"   element={<div style={{ padding: 24, minHeight: '100vh' }}><ProjectList /></div>} />
              <Route path="/resources"  element={<ResourceCenter />} />
              <Route path="/guide"     element={<Guide />} />
              <Route path="/settings"   element={<div style={{ padding: 24, minHeight: '100vh' }}><Settings /></div>} />
              <Route path="/idea"       element={<IdeaLanding />} />
              <Route path="/dev/test-dashboard" element={<div style={{ padding: 24, minHeight: '100vh' }}><TestDashboard /></div>} />
              <Route path="/engineer/:projectId" element={<EngineerWorkspace />} />
              <Route path="/projects/:id/*" element={<div style={{ height: '100vh', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}><ProjectLayout /></div>} />
              <Route path="*"               element={<Navigate to="/projects" replace />} />
            </Routes>
            </Suspense>
          </Content>
        </Layout>
      </Layout>
      )}
    </AuthGuard>
  );
};

export default App;
