FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISABLE_DOCS=false \
    MATERIAL_DB_PATH=data/materials.db \
    MATERIAL_RECORD_TTL_SECONDS=3600 \
    MYSQL_HOST="" \
    MYSQL_PORT=3306 \
    MYSQL_USER="" \
    MYSQL_PASSWORD="" \
    MYSQL_DATABASE="" \
    MYSQL_CHARSET=utf8mb4 \
    ROOT_PATH=""

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[web]" \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

CMD ["sh", "-c", "uvicorn parse_video_py.web:app --host 0.0.0.0 --port 8000 --root-path \"${ROOT_PATH}\""]
