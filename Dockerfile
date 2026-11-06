# syntax=docker/dockerfile:1
# One image: the React app is built here and served by FastAPI. Single uvicorn worker by design (in-process job
# poller); run one instance. Data lives in PostgreSQL, never in the container filesystem.

FROM node:24-bookworm-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Public browser value (AG Studio licence); optional. Never pass secrets as build args.
ARG VITE_AG_STUDIO_LICENSE_KEY=""
ENV VITE_AG_STUDIO_LICENSE_KEY=${VITE_AG_STUDIO_LICENSE_KEY}
RUN npm run build

FROM python:3.13-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
RUN pip install --no-cache-dir "uv>=0.13,<0.14"
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
COPY fixtures/ /app/fixtures/
COPY --from=web /app/frontend/dist /app/frontend/dist
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"
# Migrate, then serve on $PORT. --proxy-headers so rate limits see the client IP behind the platform proxy.
CMD ["sh", "-c", "python -m app.cli check-config && python -m app.cli migrate && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
