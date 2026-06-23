FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROOT_PATH=""

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[web]"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn parse_video_py.web:app --host 0.0.0.0 --port 8000 --root-path \"${ROOT_PATH}\""]
