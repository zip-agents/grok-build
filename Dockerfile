# python:3.12.15-slim-bookworm, multi-arch index digest.
# `docker build .` needs no build arguments and produces the runtime API image.
# The dev stage is only for lint, types, and tests (`docker build --target dev`).
# amd64 manifest: sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS python

FROM python AS uv
RUN pip install --no-cache-dir "uv==0.13.1"

FROM python AS deps
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /opt/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python AS deps-dev
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /opt/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --group dev --no-install-project

# Lint, typecheck, tests, and the smoke client. Not the image that serves traffic.
FROM python AS dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip p7zip-full curl git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps-dev /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY zip_service ./zip_service
COPY tests ./tests
COPY scripts ./scripts
COPY scripts/smoke_client.sh /opt/smoke_client.sh

FROM python AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin zip
COPY --from=deps --chown=zip:zip /opt/venv /opt/venv
WORKDIR /app
COPY --chown=zip:zip zip_service ./zip_service
USER zip
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.environ['PORT'], timeout=2).read()"]
ENTRYPOINT ["python", "-m", "zip_service"]
