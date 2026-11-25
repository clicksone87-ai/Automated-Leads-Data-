# Worker image: Prefect flows + leads CLI. Python 3.12, deps installed with uv, non-root.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.11 /uv /uvx /bin/

ENV UV_HTTP_TIMEOUT=300 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LEADS_CONFIG_DIR=/app/config \
    DATA_DIR=/app/data \
    SECRETS_DIR=/app/secrets \
    BACKUPS_DIR=/app/backups

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/secrets /app/backups \
    && chown -R app:app /app/data /app/secrets /app/backups
USER app

CMD ["python", "-m", "leads.deploy"]
