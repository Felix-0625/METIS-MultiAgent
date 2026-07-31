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
      <ConfigProvider locale={zhCN}>
        <App />
      </ConfigProvider>
    </RouterComponent>
  </React.StrictMode>
)
