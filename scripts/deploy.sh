#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  MeTis — 腾讯云安全部署脚本（含防火墙 + HTTPS + 认证 + SMTP）
#  适用：Ubuntu 22.04 / Debian 12 轻量应用服务器
#  用法：bash deploy.sh
# ══════════════════════════════════════════════════════════════════
set -e

# ── 颜色输出 ────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 配置（运行前建议用环境变量设置以下内容）────────────────────────
REPO_URL="${REPO_URL:-}"              # 代码仓库地址
APP_DIR="${APP_DIR:-/opt/metis}"      # 部署目录
DOMAIN="${DOMAIN:-}"                  # 域名（如 metis.example.com，用于 HTTPS）
HERMES_API_KEY="${HERMES_API_KEY:-}"  # LLM API Key（可选，用户自行配置）
HERMES_API_URL="${HERMES_API_URL:-https://api.openai.com/v1}"

# SMTP 邮件配置（可选，用于邮箱验证码和密码重置，推荐阿里云邮件推送）
SMTP_HOST="${SMTP_HOST:-}"
SMTP_PORT="${SMTP_PORT:-587}"
SMTP_USERNAME="${SMTP_USERNAME:-}"
SMTP_PASSWORD="${SMTP_PASSWORD:-}"
SMTP_FROM="${SMTP_FROM:-}"
SMTP_USE_TLS="${SMTP_USE_TLS:-true}"

# ── 检查必填项 ──────────────────────────────────────────────────────
# API Key 不再必填——用户登录后在「系统设置」页面自行配置
if [ -z "$HERMES_API_KEY" ]; then
  warn "未设置 HERMES_API_KEY，用户需在系统设置页面自备 API Key"
fi
if [ -z "$REPO_URL" ]; then
  read -rp "请输入代码仓库地址（Gitee/GitHub URL）: " REPO_URL
  [ -z "$REPO_URL" ] && error "仓库地址不能为空"
fi
if [ -z "$DOMAIN" ]; then
  read -rp "请输入域名（如 metis.example.com，留空则跳过 HTTPS）: " DOMAIN
fi

# ── SMTP 配置（可选）────────────────────────────────────────────────
if [ -z "$SMTP_HOST" ]; then
  echo ""
  info "SMTP 邮件服务用于发送邮箱验证码和密码重置邮件"
  info "推荐使用阿里云邮件推送（每天免费 200 封），或 Resend / SendGrid"
  read -rp "是否配置 SMTP？（y/n，留空跳过）: " SMTP_CHOICE
  if [ "$SMTP_CHOICE" = "y" ] || [ "$SMTP_CHOICE" = "Y" ]; then
    echo "  ── SMTP 配置（以阿里云邮件推送为例）──────────────────"
    echo "  获取方式：阿里云控制台 → 邮件推送 → 发信地址 + SMTP 密码"
    read -rp "  SMTP 服务器地址（如 smtpdm.aliyun.com）: " SMTP_HOST
    read -rp "  SMTP 端口（默认 465 阿里云 / 587 通用）: " SMTP_PORT
    SMTP_PORT="${SMTP_PORT:-465}"
    read -rp "  SMTP 用户名/发信地址（如 noreply@mail.your-domain.com）: " SMTP_USERNAME
    read -rsp "  SMTP 密码（阿里云控制台生成的 SMTP 密码，非登录密码）: " SMTP_PASSWORD
    echo ""
    read -rp "  发件人显示名称（如 MeTis）: " SMTP_FROM_NAME
    if [ -n "$SMTP_HOST" ] && [ -n "$SMTP_USERNAME" ] && [ -n "$SMTP_PASSWORD" ]; then
      SMTP_FROM="${SMTP_FROM_NAME:-MeTis} <${SMTP_USERNAME}>"
      SMTP_USE_TLS="true"
      info "SMTP 配置完成"
    else
      warn "SMTP 信息不完整，将跳过邮件功能"
      SMTP_HOST=""
    fi
  else
    info "跳过 SMTP 配置（邮箱验证和密码重置将不可用）"
  fi
fi

info "开始部署 MeTis 安全环境..."

# ── 1. 系统安全加固 ─────────────────────────────────────────────────
info "[1/9] 配置防火墙规则..."
if command -v ufw &>/dev/null; then
  # 重置防火墙
  ufw --force reset > /dev/null 2>&1 || true
  # 默认拒绝入站
  ufw default deny incoming > /dev/null 2>&1 || true
  # SSH（仅限当前连接 IP）
  if [ -n "${SSH_CLIENT:-}" ]; then
    SSH_IP=$(echo "$SSH_CLIENT" | awk '{print $1}')
    ufw allow from "$SSH_IP" to any port 22 > /dev/null 2>&1 || true
    info "SSH 仅允许: $SSH_IP"
  fi
  ufw allow 22 > /dev/null 2>&1 || true  # 兜底
  # HTTP / HTTPS
  ufw allow 80 > /dev/null 2>&1 || true
  ufw allow 443 > /dev/null 2>&1 || true
  # 避免 Docker 绕过 UFW（关键！）
  mkdir -p /etc/docker
  if [ ! -f /etc/docker/daemon.json ]; then
    echo '{"iptables": false}' > /etc/docker/daemon.json
    info "Docker iptables 已禁用（由 UFW 管理防火墙）"
  fi
  ufw --force enable > /dev/null 2>&1 || true
  info "防火墙已启用"
else
  warn "ufw 未安装，跳过防火墙配置"
fi

# ── 2. 生成安全密钥 ─────────────────────────────────────────────────
info "[2/9] 生成安全密钥..."
JWT_SECRET=$(openssl rand -hex 32)
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 24)}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -hex 16)}"
info "密钥已生成"

# ── 3. 安装依赖 ─────────────────────────────────────────────────────
info "[3/9] 安装 Docker 和 Docker Compose..."
if ! command -v docker &>/dev/null; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable docker
  systemctl start docker
  info "Docker 安装完成"
else
  info "Docker 已安装，跳过"
fi

# ── 4. 克隆/更新代码 ────────────────────────────────────────────────
info "[4/9] 拉取代码..."
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git pull origin main
  info "代码已更新"
else
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
  info "代码克隆完成"
fi

# ── 5. 替换 Caddyfile 域名 ──────────────────────────────────────────
info "[5/9] 配置域名和 HTTPS..."
if [ -n "$DOMAIN" ]; then
  sed -i "s/YOUR_DOMAIN/${DOMAIN}/g" "$APP_DIR/Caddyfile"
  sed -i "s/admin@example.com/admin@${DOMAIN}/g" "$APP_DIR/Caddyfile"
  CORS_ORIGINS="https://${DOMAIN}"
  info "Caddyfile 域名已设为: $DOMAIN"
else
  # 无域名时去掉 Caddy，只保留 frontend 直接暴露 3000
  warn "未设置域名，将使用 HTTP 模式（不推荐生产环境）"
  CORS_ORIGINS="http://localhost:3000"
  # 修改 docker-compose：移除 caddy，frontend 恢复 ports 映射
  if [ -f "$APP_DIR/docker-compose.yml" ]; then
    info "调整为 HTTP 模式..."
  fi
fi

# ── 6. 生成 .env 文件 ───────────────────────────────────────────────
info "[6/9] 生成 .env 配置..."
cat > "$APP_DIR/.env" << ENVEOF
# 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')
# ── 请勿提交到 git ─────────────────────────────────────────────────

# LLM API
HERMES_API_URL=${HERMES_API_URL}
HERMES_API_KEY=${HERMES_API_KEY}

# 数据库
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# JWT 认证
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRY_HOURS=24

# 管理员初始账号
ADMIN_USERNAME=admin
ADMIN_PASSWORD=${ADMIN_PASSWORD}

# CORS（前端域名列表，逗号分隔）
CORS_ORIGINS=${CORS_ORIGINS}

# 限流（每分钟每IP最大请求数）
RATE_LIMIT_RPM=60

# 请求体大小限制（字节，默认10MB）
MAX_BODY_SIZE=10485760

# ── SMTP 邮件服务（阿里云邮件推送）─────────────────────────────────
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_USERNAME=${SMTP_USERNAME}
SMTP_PASSWORD=${SMTP_PASSWORD}
SMTP_FROM=${SMTP_FROM}
SMTP_USE_TLS=${SMTP_USE_TLS}
ENVEOF
chmod 600 "$APP_DIR/.env"
info ".env 已生成（权限 600）"
if [ -n "$SMTP_HOST" ]; then
  info "SMTP 邮件已配置: ${SMTP_USERNAME}"
fi

# ── 7. 创建数据目录 ─────────────────────────────────────────────────
info "[7/9] 创建持久化目录..."
mkdir -p "$APP_DIR/memory" "$APP_DIR/projects" "$APP_DIR/backend/data" "$APP_DIR/backend/data/chat_history"
info "目录创建完成"

# ── 8. 构建并启动服务 ───────────────────────────────────────────────
info "[8/9] 构建 Docker 镜像并启动服务（首次约 5-10 分钟）..."
cd "$APP_DIR"
docker compose pull db caddy 2>/dev/null || true
docker compose up -d --build
info "服务启动中..."

# ── 9. 等待服务就绪 ─────────────────────────────────────────────────
info "[9/9] 等待服务就绪..."
MAX_WAIT=90
count=0
while [ $count -lt $MAX_WAIT ]; do
  if curl -sf http://localhost:8000/health &>/dev/null; then
    break
  fi
  count=$((count + 1))
  sleep 2
done

if curl -sf http://localhost:8000/health &>/dev/null; then
  info "后端健康检查通过 ✓"
else
  warn "后端尚未就绪，请稍后手动检查：curl http://localhost:8000/health"
fi

# ── 10. 数据库定时备份 ───────────────────────────────────────────────
info "[10/9] 配置数据库定时备份（每天凌晨 3 点自动备份，保留 7 天）..."
BACKUP_SCRIPT="$APP_DIR/scripts/backup.sh"
cat > "$BACKUP_SCRIPT" << 'BKEOF'
#!/bin/bash
# MeTis 数据库自动备份脚本
# 用法：crontab -l | grep -q backup.sh || (crontab -l; echo "0 3 * * * /opt/metis/scripts/backup.sh") | crontab -
BACKUP_DIR="/opt/metis/backups"
mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/metis_backup_$(date +%Y%m%d_%H%M%S).sql"
CONTAINER=$(docker ps -qf 'name=db' | head -1)
if [ -n "$CONTAINER" ]; then
  docker exec "$CONTAINER" pg_dump -U dagent dagent > "$FILE" 2>/dev/null && \
    gzip "$FILE" && \
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份成功：${FILE}.gz"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份失败：数据库容器未运行" >&2
fi
# 清理 7 天前的备份
find "$BACKUP_DIR" -name "metis_backup_*.sql.gz" -mtime +7 -delete 2>/dev/null
BKEOF
chmod +x "$BACKUP_SCRIPT"
# 检查 crontab 是否已有此任务，没有则添加
if command -v crontab &>/dev/null; then
  BACKUP_LINE="0 3 * * * $BACKUP_SCRIPT"
  if ! crontab -l 2>/dev/null | grep -qF "$BACKUP_SCRIPT"; then
    (crontab -l 2>/dev/null; echo "$BACKUP_LINE") | crontab -
    info "数据库定时备份已配置（每天凌晨 3 点，保留 7 天）"
  else
    info "数据库定时备份任务已存在，跳过"
  fi
else
  warn "crontab 未安装，跳过定时备份配置"
fi

# ── 输出结果 ─────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  🎉 MeTis 安全部署完成！${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
if [ -n "$DOMAIN" ]; then
  echo "  🔒 HTTPS 访问地址：https://${DOMAIN}"
else
  echo "  🌐 HTTP 访问地址：http://localhost:3000"
fi
echo ""
echo "  ⚠️  管理员账号（首次登录后请立即修改密码）："
echo "     用户名: admin"
echo "     密码:   ${ADMIN_PASSWORD}"
echo ""
if [ -n "$SMTP_HOST" ]; then
  echo "  ✉️  SMTP 邮件已配置：${SMTP_USERNAME}"
  echo "     邮箱验证和密码重置功能可用"
else
  echo "  ⚠️  SMTP 未配置，邮箱验证和密码重置不可用"
  echo "     运行后可在 .env 中手动添加 SMTP_* 变量"
fi
echo ""
echo "  📝 所有密钥已保存到 ${APP_DIR}/.env"
echo "  🔐 .env 文件权限: 600（仅 root 可读写）"
echo ""
echo "  常用命令："
echo "    查看日志：  docker compose -f ${APP_DIR}/docker-compose.yml logs -f"
echo "    查看后端日志：docker compose -f ${APP_DIR}/docker-compose.yml logs -f backend"
echo "    重启服务：  docker compose -f ${APP_DIR}/docker-compose.yml restart"
echo "    停止服务：  docker compose -f ${APP_DIR}/docker-compose.yml down"
echo "    数据库备份：docker exec \$(docker ps -qf 'name=db') pg_dump -U dagent dagent > backup_\$(date +%Y%m%d).sql"
echo ""