# ── Stage 1: 构建 ──────────────────────────────────────────────────────────────
FROM node:18-alpine AS builder

WORKDIR /app

# 先复制 package 文件，利用 Docker 层缓存
COPY package.json package-lock.json ./
RUN npm ci --prefer-offline

# 复制源码并构建
COPY . .
# VITE_API_URL 在构建时注入（/api 会被 nginx 反代到 backend:8000）
ARG VITE_API_URL=/api
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

# ── Stage 2: 运行（Nginx 静态服务）────────────────────────────────────────────
FROM nginx:alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html/app

# Nginx 配置：SPA 路由 + 反代 /api → backend:8000
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
