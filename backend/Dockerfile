# Reviewed Cordis runtime for goal research (#1008). The `cordis` build context
# is runtime/cordis (Compose additional_contexts). Only reviewed sources and the
# lockfile are copied; dependencies come from `npm ci` and the build writes the
# hash-pinned dist/build-manifest.json the backend verifies before starting it.
# Tag-pinned: no registry access was available to resolve a digest.
FROM node:24.13.1-bookworm-slim AS cordis-build
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
RUN apt install gcc g++ git curl -y

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

# Install Playwright Chromium browser
RUN uv run playwright install chromium

# The backend resolves the Cordis package at /runtime/cordis (parents of
# src/runtime_plugins/composition.py) and starts it with CORDIS_NODE_PATH.
# Follow-up: pin node:24.13.1-bookworm-slim by digest once a registry is reachable.
COPY --from=cordis-build /usr/local/bin/node /usr/local/bin/node
COPY --from=cordis-build /runtime/cordis /runtime/cordis
ENV CORDIS_NODE_PATH=/usr/local/bin/node

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--reload"]
