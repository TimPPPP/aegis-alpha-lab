# syntax=docker/dockerfile:1.7
#
# Aegis Alpha Lab — multi-stage image.
#   builder: installs all Python deps into a venv via uv
#   runtime: slim base + the venv + non-root user; entrypoint is the `aegis` CLI

ARG PYTHON_VERSION=3.11

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first to maximize layer caching
COPY pyproject.toml uv.lock* ./

# Resolve and install. If uv.lock is missing, fall back to lock-then-sync.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        uv sync --frozen --no-install-project --no-dev; \
    else \
        uv lock && uv sync --no-install-project --no-dev; \
    fi

# Copy source and install the project itself
COPY src ./src
COPY configs ./configs
COPY README.md LICENSE ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || uv sync --no-dev

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="aegis-alpha-lab" \
      org.opencontainers.image.source="https://github.com/timidpaper/aegis-alpha-lab" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    AEGIS_GIT_SHA="${GIT_SHA}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system aegis \
    && useradd --system --gid aegis --home /app --shell /bin/bash aegis \
    && mkdir -p /app/data \
    && chown -R aegis:aegis /app

WORKDIR /app

# Copy the prepared venv + source from the builder
COPY --from=builder --chown=aegis:aegis /opt/venv /opt/venv
COPY --from=builder --chown=aegis:aegis /app/src /app/src
COPY --from=builder --chown=aegis:aegis /app/configs /app/configs
COPY --from=builder --chown=aegis:aegis /app/pyproject.toml /app/README.md /app/LICENSE /app/

USER aegis

ENTRYPOINT ["aegis"]
CMD ["--help"]
