# ── Stage 1: 构建前端 ────────────────────────────────────────────────────────
FROM node:18-alpine AS frontend-builder

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline
COPY frontend/ ./
ARG VITE_API_URL=/api
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM node:20-bookworm-slim AS node-runtime

# ── Stage 2: 最终运行镜像 ────────────────────────────────────────────────────
FROM python:3.10-slim

# 安装 Nginx、supervisor、PostgreSQL 运维工具和配置模板渲染器
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gettext-base \
    git \
    nginx \
    postgresql-client \
    supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# The platform runs deterministic pre-QA gates for generated Node/React/Express
# projects inside the Render service container. Keep a complete Node/npm runtime.
COPY --from=node-runtime /usr/local/ /usr/local/

WORKDIR /app

# 安装 Python 依赖
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码
COPY backend/ .

# ✨ 新增：预生成 skills.json 文件（兜底方案）
RUN python init_skills.py 2>/dev/null || echo "⚠️ init_skills.py failed during build, will retry on startup"

# 复制前端构建产物（React APP → /usr/share/nginx/html/app）
COPY --from=frontend-builder /app/dist /usr/share/nginx/html/app

# 复制宣传网页（首页 → /usr/share/nginx/html）
COPY 宣传网页/ /usr/share/nginx/html/

# 复制 Nginx 配置模板；启动时使用 Render 注入的 PORT 生成最终配置
COPY nginx-render.conf /etc/nginx/templates/metis.conf.template

# 复制 supervisor 配置
COPY supervisor.conf /etc/supervisor/conf.d/metis.conf

# 创建必要目录。Render 持久盘统一挂载到 /var/data。
ENV METIS_DATA_DIR=/var/data \
    HOME=/var/data
RUN mkdir -p memory projects skills logs data data/chat_history \
    /var/data/memory /var/data/pools /var/data/projects /var/data/snapshots

# FastAPI 不需要 root 权限；仅保留 supervisor/nginx master 的进程管理权限。
RUN chown -R www-data:www-data memory projects skills logs data /var/data

# ✨ 新增：确保启动脚本可执行
RUN chmod +x startup.sh

# EXPOSE 仅提供镜像元数据；Nginx 实际监听 Render 的 PORT。
EXPOSE 10000

# 先验证并渲染动态端口，再让 supervisor 成为 PID 1。
CMD PORT="${PORT:-10000}"; \
    case "$PORT" in ''|*[!0-9]*) echo "PORT must be numeric" >&2; exit 64;; esac; \
    case "$METIS_DATA_DIR" in /var/data) ;; *) echo "METIS_DATA_DIR must be /var/data" >&2; exit 64;; esac; \
    mkdir -p "$METIS_DATA_DIR/memory" "$METIS_DATA_DIR/pools" \
        "$METIS_DATA_DIR/projects" "$METIS_DATA_DIR/snapshots"; \
    chown -R www-data:www-data "$METIS_DATA_DIR"; \
    export PORT; \
    envsubst '$PORT' < /etc/nginx/templates/metis.conf.template > /etc/nginx/conf.d/default.conf; \
    exec supervisord -n -c /etc/supervisor/supervisord.conf
