# syntax=docker/dockerfile:1.6
#
# 多阶段构建 linling-webui
# 阶段 1: pnpm 构建前端 → dist
# 阶段 2: python 运行时，包含 wheel + 静态资源
#
# 构建方式：docker build -f packages/webui/Dockerfile -t linling-webui .
# 运行方式：docker run -p 8787:8787 -v $(pwd)/data:/app/data linling-webui
#
# 从仓库根目录构建。

# ---------- Stage 1: frontend ----------
FROM node:20-alpine AS frontend
WORKDIR /src
RUN npm install -g pnpm@10
COPY pnpm-workspace.yaml .npmrc ./
COPY packages/webui/frontend/package.json packages/webui/frontend/pnpm-lock.yaml* ./packages/webui/frontend/
RUN cd packages/webui/frontend && pnpm install --frozen-lockfile || pnpm install
COPY packages/webui/frontend ./packages/webui/frontend
RUN cd packages/webui/frontend && pnpm build
# dist ends up in packages/webui/src/linling_webui/static/ (set by vite.config.ts)

# ---------- Stage 2: python runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LINLING_WEBUI_HOST=0.0.0.0 \
    LINLING_WEBUI_PORT=8787 \
    LINLING_WEBUI_AUTH_DB_PATH=/app/data/webui_auth.db

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv>=0.11"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY packages/core ./packages/core
COPY packages/dsl ./packages/dsl
COPY packages/agent ./packages/agent
COPY packages/tools-stdlib ./packages/tools-stdlib
COPY packages/cli ./packages/cli
COPY packages/adapters ./packages/adapters
COPY packages/webui ./packages/webui

# 从 frontend 阶段拷贝构建好的静态
COPY --from=frontend /src/packages/webui/src/linling_webui/static/ ./packages/webui/src/linling_webui/static/

RUN uv sync --frozen --all-packages --no-dev

EXPOSE 8787
VOLUME ["/app/data"]

# HEALTHCHECK: hit /api/health every 30s
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/health || exit 1

ENTRYPOINT ["uv", "run", "--no-sync", "linling"]
CMD ["serve", "webui", "--host", "0.0.0.0", "--port", "8787"]
