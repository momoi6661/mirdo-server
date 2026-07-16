# 使用 uv 管理依赖；项目允许 Python 3.11/3.12。
# 3.12 slim 也方便在 Docker Desktop 已有基础镜像缓存时离线构建。
FROM python:3.12-slim-bookworm

# 通过 PyPI 安装 uv；这样不依赖 Docker 构建机能否访问 GHCR。
RUN python -m pip install --no-cache-dir --disable-pip-version-check uv==0.11.21

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先只复制锁文件，让依赖层可以被 Docker 缓存。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY data/knowledge ./data/knowledge
COPY data/dialogue ./data/dialogue
COPY data/tts ./data/tts
COPY docs ./docs
COPY README.md ./README.md

# runtime 目录由 compose 挂载，用来保存 SQLite、TTS 缓存和日志。
RUN mkdir -p /app/data/runtime

EXPOSE 5678

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5678/health', timeout=3).read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5678"]
