FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:3.13-slim-bookworm AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY server/app ./app
COPY server/alembic.ini ./alembic.ini
COPY server/migrations ./migrations
RUN useradd --uid 10001 --create-home procure && mkdir -p /app/data/files && chown -R procure:procure /app/data
ENV PATH="/app/.venv/bin:$PATH"

FROM base AS test
RUN uv sync --locked --no-install-project
COPY server/tests ./tests
COPY scripts/deploy /scripts/deploy
COPY deploy /deploy
RUN chown -R procure:procure /app
USER procure
CMD ["python", "-m", "pytest", "-q", "--tb=short"]

FROM base AS runtime
USER procure
EXPOSE 8000
# Run directly from the uv-managed runtime environment; do not sync dev dependencies at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
