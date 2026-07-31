# METIS 部署指南

## 1. 推荐方案

目标平台为 Railway，推荐拓扑：

```text
Railway Web Service
├── Nginx：宣传页、React、API/WebSocket 反向代理
└── FastAPI：业务与 Agent 工作流

Railway PostgreSQL
└── 权威业务状态

Railway Volume：/var/data
└── 项目工作区、Agent 记忆、资源池和快照
```

当前 `Dockerfile.render` 支持平台注入的 `PORT`，可在 Railway 复用。文件名仍带有 Render，但运行结构不依赖 Render 专属 API。部署前无需复制 `docker-compose.yml` 到 Railway。

> 本文是配置说明，不代表已经完成 Railway 在线验收。

## 2. 新仓库准备

推送前确认：

- `CHANGE.md`、`ARCHITECTURE.md`、`API.md`、`DEPLOYMENT.md`、`SECURITY.md` 已纳入仓库。
- `.env`、日志、数据库、项目工作区和测试产物未纳入仓库。
- `frontend/package-lock.json` 与 `package.json` 一致。
- 后端测试、前端类型检查和构建通过。
- 新仓库默认分支和 Railway 自动部署分支正确。

## 3. Railway 配置

### 3.1 创建服务

1. 在 Railway 创建项目。
2. 添加 PostgreSQL 服务。
3. 从新 GitHub 仓库创建 Web Service。
4. 在 Web Service 设置：

```text
RAILWAY_DOCKERFILE_PATH=Dockerfile.render
```

Railway 默认只自动识别根目录的 `Dockerfile`；自定义文件名必须显式指定。

### 3.2 持久卷

为 Web Service 创建 Volume：

```text
Mount Path: /var/data
```

Volume 只在运行阶段挂载，不在镜像构建或 Pre-deploy 阶段挂载。需要访问持久文件的初始化逻辑必须放在启动流程中。

### 3.3 数据库

在 Web Service 设置引用变量：

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
REQUIRE_DATABASE_URL=true
```

如果数据库服务名称不是 `Postgres`，使用实际服务名。不要复制并硬编码数据库密码。

### 3.4 必需变量

| 变量 | 要求 |
|---|---|
| `DATABASE_URL` | 引用 Railway PostgreSQL |
| `REQUIRE_DATABASE_URL` | `true` |
| `METIS_DATA_DIR` | `/var/data` |
| `METIS_DATA_ENCRYPTION_KEY` | 独立强随机密钥 |
| `JWT_SECRET` | 至少 32 字节的强随机值 |
| `ADMIN_USERNAME` | 初始管理员名 |
| `ADMIN_PASSWORD` | 强随机初始密码 |
| `HERMES_API_URL` | 模型供应商 API 地址 |
| `HERMES_API_KEY` | 模型供应商 Key |
| `CORS_ORIGINS` | Railway 公网 HTTPS 域名 |

建议变量：

```dotenv
DB_CONNECT_TIMEOUT_SECONDS=5
DB_STATEMENT_TIMEOUT_MS=30000
DB_LOCK_TIMEOUT_MS=5000
DB_STARTUP_MAX_ATTEMPTS=6
DB_STARTUP_MAX_SECONDS=45
JWT_EXPIRY_HOURS=24
RATE_LIMIT_READ_RPM=120
RATE_LIMIT_WRITE_RPM=60
RATE_LIMIT_LOGIN_RPM=10
MAX_FAILED_ATTEMPTS=5
LOCKOUT_DURATION=300
MAX_BODY_SIZE=10485760
```

SMTP 未配置时，邮箱验证和密码重置能力会受限。运行验收默认关闭；只有隔离执行环境和凭据均配置完成后，才启用：

```dotenv
RUNTIME_ACCEPTANCE_ENABLED=true
RUNTIME_ACCEPTANCE_REQUIRED=true
```

### 3.5 网络与健康检查

1. 为 Web Service 生成公网域名。
2. 不手动覆盖 Railway 注入的 `PORT`，除非明确配置了目标端口。
3. 设置 Healthcheck Path：

```text
/health
```

`/health` 只有在应用和数据库均健康时才返回 `200`。Railway 的部署健康检查用于上线切换，不是持续监控替代品。

## 4. 首次部署验证

按顺序验证：

1. 构建日志显示使用 `Dockerfile.render`。
2. Nginx 监听 Railway 注入的 `PORT`。
3. `/health` 返回 `200` 且数据库为 healthy。
4. `/app/` 可打开。
5. 注册、登录和 `/auth/me` 正常。
6. 创建项目后重启 Web Service，项目仍存在。
7. 项目文件写入 `/var/data`，重启后仍可读取。
8. WebSocket 可连接并能在断线后恢复。
9. 模型连接测试通过。
10. 完成一次隔离的小型项目流程。

最终发布验收至少包括：

- 数据库持久化。
- Volume 持久化。
- 服务重启恢复。
- 服务端项目归档下载。
- Pre-QA、Final QA、运行验收和 Signoff。

## 5. 更新与回滚

- 生产部署前创建 PostgreSQL 和 Volume 备份。
- 数据库迁移由应用启动执行；先在测试环境验证旧数据升级。
- 回滚代码前确认旧版本兼容当前数据库结构。
- 密钥轮换后重新部署，并验证旧令牌失效和加密数据可读性。
- 不通过删除 Volume 或重建 PostgreSQL 处理普通发布故障。

## 6. 当前限制

- 尚无 Railway 专用 `railway.toml`；当前依赖 Dashboard 配置。
- `Dockerfile.render` 可复用，但名称不直观，后续可在实际 Railway 验收后改为平台中立的 `Dockerfile`。
- 单个 Web Service 内同时运行 Nginx 与 FastAPI，便于部署但限制独立扩缩容。
- 文件资源池和工作区依赖共享 Volume；未完成多副本并发验证前，保持单副本。
- Railway 在线数据库、Volume 权限、WebSocket 和完整业务流程仍需部署后实测。

## 7. 官方参考

- [Railway Dockerfile](https://docs.railway.com/builds/dockerfiles)
- [Railway PostgreSQL](https://docs.railway.com/databases/postgresql)
- [Railway Variables](https://docs.railway.com/variables)
- [Railway Volumes](https://docs.railway.com/volumes)
- [Railway Healthchecks](https://docs.railway.com/deployments/healthchecks)
