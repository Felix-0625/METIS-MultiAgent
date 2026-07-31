<p align="center">
  <img src="https://img.shields.io/badge/Status-Active-success?style=flat-square" alt="Status" />
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react" alt="React" />
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License" />
</p>

<p align="center">
  <img src="https://readme-typing-svg.demolab.com/?font=Inter&weight=900&size=42&duration=1&pause=1&color=764BA2&center=true&vCenter=true&repeat=false&width=400&lines=M+E+T+I+S" alt="METIS" />
</p>

<p align="center"><strong>AI Multi-Agent System</strong> — 将想法交给矩阵</p>

---

## ✨ 简介

METIS 是一个由多个专业 AI Agent 协同驱动的软件项目全流程管理平台。从灵感捕捉、需求分析、团队组建、代码执行到质量检测与交付，全程由 AI 专家矩阵自动流转。

- **多智能体协作** — PM 组长、全栈工程师、监督组长、QA 测试等角色各司其职
- **阶段看板** — 可视化项目阶段，支持冲突检测、审查链、团队健康度
- **全栈工程能力** — AI 工程师独立完成代码生成、调试到部署的完整闭环
- **质量保障** — 自动化测试看板、缺陷追踪，质量可量化可追溯
- **实时协作** — PM 团队聊天、监督审查、进度同步，所有交互实时可见
- **完整认证体系** — JWT + HttpOnly Cookie + 邮箱验证 + 密码重置 + 暴力破解防护

---

## 🛠 技术栈

| 层级 | 技术 |
|:---|:---|
| 前端 | React 18 · TypeScript · Ant Design 5 · Vite 5 · Zustand |
| 桌面端 | Tauri v2 |
| 后端 | Python · FastAPI · Uvicorn · PyJWT · Pydantic |
| 数据库 | PostgreSQL（生产）· SQLite（本地开发） |
| 部署 | Docker · Caddy (HTTPS + Let's Encrypt) |
| 邮件 | SMTP（smtplib，支持 TLS） |

---

## 🚀 快速开始

### 1. 克隆 & 环境配置

```bash
git clone https://gitee.com/feline-grace/metis.git
cd metis
cp .env.example .env   # 编辑 .env 填入 API Key 等配置
```

### 2. 启动后端

```bash
cd backend
pip install -r requirements.txt
python main.py           # 默认监听 http://0.0.0.0:8000
```

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev              # Vite 开发服务器 → http://localhost:3000
```

### 4. 宣传网页（可选）

```bash
cd 宣传网页
python -m http.server 8080   # → http://localhost:8080
```

### 5. Docker 部署

```bash
docker-compose up -d     # 包含 PostgreSQL + 后端 + 前端 + Caddy HTTPS
```

---

## 🔐 认证系统

| 功能 | 说明 |
|:---|:---|
| 登录 | 支持用户名或邮箱，JWT 通过 HttpOnly Secure SameSite Cookie 下发 |
| 注册 | 开放自主注册，SMTP 配置后发送邮箱验证码；开发模式自动验证 |
| 忘记密码 | 邮件重置链接 + 一次性令牌 + 10 分钟有效期 |
| 修改密码 | 需验证旧密码，修改后所有旧 Token 立即失效 |
| 安全 | PBKDF2-SHA256 密码哈希 · 暴力破解锁定 · 用户枚举防护 · 双维度限流 |

---

## 📁 项目结构

```
├── backend/
│   ├── api/              # 27 个路由模块（auth / projects / pm / phases / …）
│   ├── agents/           # Agent 实现（PM / HR / Supervisor / Engineer / QA / …）
│   ├── core/             # 核心引擎（auth / email / orchestrator / expert_lock / …）
│   ├── models/           # Pydantic 数据模型
│   └── tests/            # 后端测试
├── frontend/
│   ├── src/pages/        # 页面组件（登录 / 项目管理 / 阶段看板 / …）
│   ├── src/services/     # API 服务层（axios + WebSocket）
│   └── src-tauri/        # Tauri 桌面端配置
├── 宣传网页/              # 产品宣传单页（GridScan 背景 + Three.js 特效）
├── prompts/              # Agent 提示词模板
├── scripts/              # 部署脚本
├── docker-compose.yml    # 容器编排
└── Caddyfile             # HTTPS 反代配置
```

---

## 📚 文档

- [使用指南](docs/USER_GUIDE.md)
- [系统架构](ARCHITECTURE.md)
- [接口文档](API.md)
- [部署指南](DEPLOYMENT.md)
- [安全策略](SECURITY.md)
- [变更日志](CHANGE.md)

---

## 🎨 宣传页效果

![预览](https://img.shields.io/badge/GridScan-Background-FF9FFC?style=flat-square)
![预览](https://img.shields.io/badge/Three.js-Shader-2F293A?style=flat-square)

- 深色地平网格线 + 粉色扫描波
- Bloom 泛光 + 色差后处理
- 鼠标跟随 3D 透视
- 登录/注册已对接真实后端 API

---

## 📄 许可证

MIT License © 2025 METIS Team
