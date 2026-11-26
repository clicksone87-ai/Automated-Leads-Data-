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

# The osmium wheel needs libexpat and the Playwright driver libatomic; the slim image has neither.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
# Headless Chromium for JavaScript-only websites. Build with --build-arg BROWSER=false
# for a smaller image; the website check then skips those pages.
ARG BROWSER=true
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN uv sync --frozen --no-dev --no-install-project $([ "$BROWSER" = true ] && echo --extra browser)
RUN if [ "$BROWSER" = true ]; then playwright install --with-deps chromium; fi

COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev $([ "$BROWSER" = true ] && echo --extra browser)

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/secrets /app/backups /home/app/.prefect \
    && chown -R app:app /app/data /app/secrets /app/backups /home/app/.prefect
USER app

CMD ["python", "-m", "leads.deploy"]
