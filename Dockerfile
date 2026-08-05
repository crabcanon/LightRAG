# syntax=docker/dockerfile:1

# Frontend build stage
# Build frontend assets on the native build platform to avoid
# cross-architecture emulation issues during multi-platform builds.
FROM --platform=$BUILDPLATFORM oven/bun:1 AS frontend-builder

WORKDIR /app

# Copy frontend source code
COPY lightrag_webui/ ./lightrag_webui/

# Build frontend assets for inclusion in the API package
RUN --mount=type=cache,target=/root/.bun/install/cache \
    cd lightrag_webui \
    && bun install --frozen-lockfile \
    && bun run build

# Python build stage - using uv for faster package installation
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_SYSTEM_PYTHON=1
ENV UV_COMPILE_BYTECODE=1

WORKDIR /app

# Install system deps (Rust is required by some wheels)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

ENV PATH="/root/.cargo/bin:/root/.local/bin:${PATH}"

# Ensure shared data directory exists for uv caches
RUN mkdir -p /root/.local/share/uv

# Copy project metadata and sources
COPY pyproject.toml .
COPY setup.py .
COPY uv.lock .

# Install base, API, and offline extras without the project to improve caching
RUN --mount=type=cache,target=/root/.local/share/uv \
    uv sync --frozen --no-dev --extra api --extra offline --no-install-project --no-editable

# Cache assets depend only on the downloader and locked dependencies, not on
# application source. Keeping this before COPY lightrag lets normal code edits
# reuse the slow tiktoken/spaCy download layer.
COPY lightrag/tools/download_cache.py /tmp/download_cache.py
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p /app/data/tiktoken \
    && /app/.venv/bin/python -m ensurepip --upgrade \
    && /app/.venv/bin/python /tmp/download_cache.py --cache-dir /app/data/tiktoken --spacy --spacy-dir /app/spacy_models || status=$?; \
    if [ -n "${status:-}" ] && [ "$status" -ne 0 ] && [ "$status" -ne 2 ]; then exit "$status"; fi

# Copy project sources after dependency layer
COPY lightrag/ ./lightrag/

# Include pre-built frontend assets from the previous stage
COPY --from=frontend-builder /app/lightrag/api/webui ./lightrag/api/webui

# Sync project in non-editable mode and ensure pip is available for runtime installs
RUN --mount=type=cache,target=/root/.local/share/uv \
    uv sync --frozen --no-dev --extra api --extra offline --no-editable \
    && /app/.venv/bin/python -m ensurepip --upgrade

# Final stage
# Pin to bookworm: keeps Python 3.12 (venv compat with the builder stage) while
# avoiding Debian trixie's perl 5.40.x exposure (CVE-2026-12087, no patch yet),
# and aligns the final Debian release with the builder (also bookworm).
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install the stable runtime package set and create the service account before
# copying application layers. Source-only changes can then reuse this apt
# layer; ownership is fixed during the copies below. libcairo2 is required by
# cairosvg for native Markdown SVG-to-PNG rasterization.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 lightrag \
    && useradd -u 1000 -g lightrag -m -d /home/lightrag -s /usr/sbin/nologin lightrag

# Gunicorn writes its default log file in WORKDIR. Only the directory itself
# needs to be writable; application trees are owned during COPY below.
RUN chown lightrag:lightrag /app

# Install uv for package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_SYSTEM_PYTHON=1

# Copy installed packages and application code
COPY --from=builder /root/.local /root/.local
COPY --chown=lightrag:lightrag --from=builder /app/.venv /app/.venv
COPY --chown=lightrag:lightrag --from=builder /app/lightrag ./lightrag
COPY --chown=lightrag:lightrag pyproject.toml .
COPY --chown=lightrag:lightrag setup.py .
COPY --chown=lightrag:lightrag uv.lock .

# Ensure the installed scripts are on PATH
ENV PATH=/app/.venv/bin:/root/.local/bin:$PATH

# The builder already produced the exact locked virtual environment. Re-running
# uv sync here would discard it and download the dependency graph again.
# Install only the pinned spaCy model wheels after copying that environment;
# these models are intentionally outside uv.lock. The bind mount exposes the
# builder's wheels without adding a separate image layer.
RUN --mount=type=bind,from=builder,source=/app/spacy_models,target=/tmp/spacy_models \
    /app/.venv/bin/python -m pip install --no-index --no-cache-dir \
        --find-links=/tmp/spacy_models zh_core_web_sm en_core_web_sm

# Create persistent data directories AFTER package installation
RUN install -d -o lightrag -g lightrag \
    /app/data/rag_storage /app/data/inputs /app/data/prompts /app/data/tiktoken

# Copy offline cache into the newly created directory
COPY --chown=lightrag:lightrag --from=builder /app/data/tiktoken /app/data/tiktoken

# Point to the prepared cache
ENV TIKTOKEN_CACHE_DIR=/app/data/tiktoken
ENV WORKING_DIR=/app/data/rag_storage
ENV INPUT_DIR=/app/data/inputs
ENV PROMPT_DIR=/app/data/prompts

# COPY --chown and install -o above keep the runtime-writable venv/cache/data
# tree owned by the fixed UID/GID without a slow recursive chown layer.

# HOME and cache dirs for the non-root user so pipmaster's runtime pip installs
# never fall back to an unwritable /root or a missing HOME.
ENV HOME=/home/lightrag \
    XDG_CACHE_HOME=/home/lightrag/.cache \
    PIP_CACHE_DIR=/home/lightrag/.cache/pip \
    UV_CACHE_DIR=/home/lightrag/.cache/uv

# Entrypoint starts as root, fixes mount ownership, then drops to lightrag.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose API port
EXPOSE 9621

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "lightrag.api.lightrag_server"]
