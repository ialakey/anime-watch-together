# syntax=docker/dockerfile:1

# ─── сборка зависимостей ─────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# сначала только метаданные — слой с зависимостями переживает правки кода
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py

RUN pip install --upgrade pip \
 && pip install ".[postgres,socks]"

# ─── рабочий образ ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL="sqlite+aiosqlite:////data/anime_watch.db"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 anime

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=anime:anime app ./app
COPY --chown=anime:anime alembic ./alembic
COPY --chown=anime:anime alembic.ini pyproject.toml README.md ./
COPY --chown=anime:anime docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /data \
 && chown anime:anime /data

USER anime
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
