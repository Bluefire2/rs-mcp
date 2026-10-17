# osrs-mcp on Cloud Run. Built by Cloud Build (deploy/deploy.sh); no local Docker needed.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, so code-only changes reuse this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

# Cloud Run sets PORT; HOST must accept traffic from outside the container.
ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

RUN useradd --uid 10001 --no-create-home app
USER app

CMD ["python", "-m", "osrs_mcp"]
