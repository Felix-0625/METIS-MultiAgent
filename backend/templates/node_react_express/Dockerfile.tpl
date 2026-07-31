FROM node:22-alpine AS frontend-build
WORKDIR /app
COPY frontend/package*.json ./frontend/
RUN if [ -f frontend/package-lock.json ]; then \
      npm ci --prefix frontend; \
    else \
      npm install --prefix frontend --no-audit --no-fund; \
    fi
COPY frontend ./frontend
RUN npm run build --prefix frontend

FROM node:22-alpine AS runtime
ENV NODE_ENV=production PORT={{PORT}}
WORKDIR /app
COPY backend/package*.json ./backend/
RUN if [ -f backend/package-lock.json ]; then \
      npm ci --omit=dev --prefix backend; \
    else \
      npm install --omit=dev --prefix backend --no-audit --no-fund; \
    fi \
    && npm cache clean --force
COPY backend ./backend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN chown -R node:node /app
USER node
EXPOSE {{PORT}}
CMD ["npm", "start", "--prefix", "backend"]
