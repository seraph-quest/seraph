# The operator supplies an approved, fully qualified digest at build time.
# A mutable tag is rejected before the build so the runtime and receipt always
# identify the same base image.
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

ARG PYTEST_VERSION=8.3.5
ARG PYTHON_BASE_IMAGE

USER root
RUN echo "$PYTHON_BASE_IMAGE" | grep -Eq '@sha256:[0-9a-f]{64}$'
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git ca-certificates \
    && python -m pip install --no-cache-dir --disable-pip-version-check "pytest==${PYTEST_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 seraph \
    && useradd --uid 65532 --gid 65532 --create-home --home-dir /tmp/seraph-home --shell /usr/sbin/nologin seraph \
    && mkdir -p /opt/seraph /input \
    && chown -R 65532:65532 /opt/seraph /input

COPY backend/src/execution/repo_worker.py /opt/seraph/repo_worker.py
RUN chown 65532:65532 /opt/seraph/repo_worker.py

ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 \
    PIP_NO_INDEX=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    GIT_CONFIG_NOSYSTEM=1

USER 65532:65532
# The trusted backend supplies the complete command.  Clearing the image
# entrypoint prevents a duplicated interpreter/script when it starts the
# transfer loader or worker.
ENTRYPOINT []
