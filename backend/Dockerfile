# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ARG SERAPH_BUILD_REVISION
LABEL org.opencontainers.image.revision=$SERAPH_BUILD_REVISION
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
COPY . /app

# Sync the project into a new environment, using the frozen lockfile
WORKDIR /app
RUN uv sync --frozen

# Install Playwright Chromium browser
RUN uv run playwright install chromium

RUN chmod 0755 /app/docker-entrypoint.sh

EXPOSE 8004
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8004", "--workers", "1", "--no-server-header"]
