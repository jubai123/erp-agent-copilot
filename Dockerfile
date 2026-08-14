# syntax=docker/dockerfile:1
# Multi-stage build for the ERP Agent Copilot image.
#
# One image serves all four processes — the default CMD runs the API, and
# infra/docker-compose.yml picks worker / erp_simulator / mcp_gateway by
# overriding `command`. The build stage resolves the locked uv.lock into a venv
# and installs the project editable (same import paths as local `uv run`, so
# `apps.api.main` etc. resolve); the runtime stage is a slim Python image that
# runs as a non-root user.

# --- Build stage: resolve uv.lock and install the project into a venv. ------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependency layer first, so source edits rebuild without re-resolving deps.
# The jieba sdist (~1GB, bundles a LAC model) is often truncated mid-download
# on unstable networks; uv's cache mount resumes across attempts, so a bounded
# retry converges instead of failing the whole build.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    bash -c 'for i in 1 2 3 4 5; do uv sync --frozen --no-dev --no-install-project && exit 0; echo "uv sync attempt $i failed, retrying"; sleep 5; done; exit 1'

# Install the project itself (editable) now that the source is present.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Runtime stage: copy the venv + source; run as non-root. ----------------
FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin app

# The editable install's .pth entries point into /app/src and /app, so the
# whole tree (venv + source) must land at the same path as in the build stage.
COPY --from=build --chown=app:app /app /app

USER app

EXPOSE 8000

# Default entrypoint: the API. compose overrides per process.
CMD ["uvicorn", "apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
