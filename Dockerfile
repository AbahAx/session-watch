FROM node:24-bookworm-slim AS frontend
WORKDIR /build
RUN npm install --global pnpm@11.25.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SESSION_WATCH_DATA=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 watcher \
    && useradd --uid 10001 --gid watcher --no-create-home watcher \
    && mkdir /data && chown watcher:watcher /data
COPY app/ ./app/
COPY --from=frontend /build/dist/ ./frontend/dist/
USER watcher
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)"]
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log", "--no-proxy-headers"]

