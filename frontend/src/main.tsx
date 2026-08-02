import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, HashRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './index.css'
import './styles/theme.css'
import { isDesktop } from './platform/detect'

const isDesktopApp = isDesktop()
const RouterComponent = isDesktopApp ? HashRouter : BrowserRouter
// 生产环境使用 /app/ 前缀（Render部署），桌面端和开发环境使用 /
const routerBasename = isDesktopApp ? undefined : (import.meta.env.PROD ? '/app' : '/')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterComponent basename={routerBasename}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: {
            colorPrimary: '#2563eb',
            colorInfo: '#2563eb',
            colorSuccess: '#10b981',
            colorWarning: '#f59e0b',
            colorError: '#ef4444',
            colorText: '#172033',
            colorTextSecondary: '#64748b',
            colorBorder: '#dfe6ef',
            colorBorderSecondary: '#e8edf4',
            colorBgLayout: '#f3f6fb',
            borderRadius: 10,
            borderRadiusLG: 16,
            controlHeight: 38,
            fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
            boxShadowTertiary: '0 10px 30px rgba(15, 23, 42, 0.07)',
          },
          components: {
            Button: { borderRadius: 9, controlHeight: 38, fontWeight: 600 },
            Card: { headerHeight: 50, paddingLG: 22 },
            Menu: {
              darkItemBg: 'transparent',
              darkItemColor: 'rgba(226,232,240,.72)',
              darkItemHoverBg: 'rgba(255,255,255,.07)',
              darkItemSelectedBg: 'linear-gradient(135deg,#2563eb,#4f46e5)' as any,
              darkItemSelectedColor: '#fff',
            },
            Tabs: { inkBarColor: '#2563eb', itemSelectedColor: '#2563eb', itemHoverColor: '#1d4ed8' },
            Table: { headerBg: '#f8fafc', headerColor: '#475569', rowHoverBg: '#f8fbff' },
          },
        }}
      >
        <App />
      </ConfigProvider>
    </RouterComponent>
  </React.StrictMode>
)
