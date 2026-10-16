# Reviewed Cordis runtime for goal research (#1008). The `cordis` build context
# is runtime/cordis (Compose additional_contexts). Only reviewed sources and the
# lockfile are copied; dependencies come from `npm ci` and the build writes the
# hash-pinned dist/build-manifest.json the backend verifies before starting it.
# Pinned by the multi-arch OCI index digest of node:24.13.1-bookworm-slim
# (resolved from Docker Hub on 2026-10-16; linux/amd64, arm64, ppc64le, s390x).
FROM node:24.13.1-bookworm-slim@sha256:a81a03dd965b4052269a57fac857004022b522a4bf06e7a739e25e18bce45af2 AS cordis-build
WORKDIR /runtime/cordis
COPY --from=cordis package.json package-lock.json profile.json tsconfig.json ./
COPY --from=cordis scripts ./scripts
COPY --from=cordis src ./src
RUN test "$(node --version)" = "v24.13.1" && test "$(npm --version)" = "11.8.0" \
    && npm ci --ignore-scripts --no-audit --no-fund && npm run build

# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt update
# bubblewrap is the agent process sandbox (#1103); the backend probes it at
# startup and before every command and blocks commands when it cannot run.
RUN apt install gcc g++ git curl bubblewrap -y

# Playwright/Chromium system dependencies
RUN apt install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy the project into the image
ADD . /app

# Sync the project into a new environment, using the frozen lockfile
WORKDIR /app
RUN uv sync --frozen

# Install Playwright Chromium browser where the non-root runtime user reads it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install chromium

# The backend resolves the Cordis package at /runtime/cordis (parents of
# src/runtime_plugins/composition.py) and starts it with CORDIS_NODE_PATH.
COPY --from=cordis-build /usr/local/bin/node /usr/local/bin/node
COPY --from=cordis-build /runtime/cordis /runtime/cordis
ENV CORDIS_NODE_PATH=/usr/local/bin/node

# Non-root runtime user with a fixed uid/gid (#1103). Workspaces written by an
# earlier root container need a one-time ownership migration:
#   docker compose -f docker-compose.<env>.yaml run --rm --no-deps --user root \
#     <backend service> chown -R 10001:10001 /app/data /app/logs
# The backend refuses to start (workspace_ownership_mismatch) until then.
RUN groupadd --system --gid 10001 seraph \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/seraph --create-home \
       --shell /usr/sbin/nologin seraph \
    && mkdir -p /app/data /app/logs \
    && chown -R seraph:seraph /app/.venv /app/data /app/logs /home/seraph
USER seraph

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--reload"]
