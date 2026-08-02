# AI Multi-Agent System Backend

FROM python:3.10-slim

WORKDIR /app

# 安装 PostgreSQL 备份/恢复工具与 Python 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 所有非 PostgreSQL 运行状态位于同一个可挂载根目录。
ENV METIS_DATA_DIR=/var/data
RUN mkdir -p memory projects skills logs data data/chat_history \
    /var/data/memory /var/data/pools /var/data/projects /var/data/snapshots

# 安全：创建非 root 用户运行应用（防容器逃逸）
RUN groupadd -r appgroup && useradd -r -g appgroup -m appuser \
    && chown -R appuser:appgroup /app /var/data
USER appuser

# 暴露端口
EXPOSE 8000

# 生产模式：单 worker（系统使用进程内字典存储状态，多 worker 会导致状态不一致）
# 如需横向扩展，需先将 projects/execution_status 迁移到 Redis
# --ws-max-size 限制 WebSocket 消息最大 10MB
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws-max-size", "10485760", "--timeout-keep-alive", "120"]
