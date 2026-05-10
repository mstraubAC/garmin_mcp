# syntax=docker/dockerfile:1.7
#
# Multi-stage Python build for the Garmin MCP server.
# - Builder stage uses uv to resolve + install dependencies into a venv
# - Runtime stage copies only the venv + source, runs as a non-root user
# - Healthcheck hits /healthz inside the container

# ---- builder ---------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.5-python3.13-bookworm-slim@sha256:e92baaf2b696ff16448644b0c781de48605b9c0873d16a0fd27de598d17cbc8c AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Resolve deps first so the heavy install layer is cached when only source
# changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---- runtime ---------------------------------------------------------------
FROM python:3.13-slim@sha256:d49c1ff87eb98eac346fc250f52925f726eb913c43a92854246dd03c9692ad67 AS runtime

# Non-root user (uid/gid 1000) — matches the typical first non-system user
# on the host so bind-mounted dirs Just Work without chown gymnastics.
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app && \
    mkdir -p /var/lib/garmin-mcp /var/log/garmin-mcp && \
    chown -R 1000:1000 /var/lib/garmin-mcp /var/log/garmin-mcp

WORKDIR /app
COPY --from=builder --chown=1000:1000 /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    GARMIN_MCP_HOST=0.0.0.0 \
    GARMIN_MCP_PORT=8000 \
    GARMIN_MCP_DATA_PATH=/var/lib/garmin-mcp/state.db \
    GARMIN_MCP_AUDIT_DIR=/var/log/garmin-mcp

USER 1000:1000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status == 200 else 1)" \
    || exit 1

# garmin-mcp-http is the OAuth-protected production entry point; it reads
# every required setting from env vars (see deploy/env.example).
CMD ["garmin-mcp-http"]
