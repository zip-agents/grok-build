# python:3.12.15-slim-bookworm, multi-arch index digest.
# `docker build .` needs no build arguments. The index selects the host platform.
# amd64 manifest: sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python

RUN pip install --no-cache-dir "uv==0.13.1"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin zip

COPY --from=build --chown=zip:zip /app/.venv /app/.venv

USER zip
WORKDIR /app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.environ['PORT'], timeout=2).read()"]

ENTRYPOINT ["python", "-m", "zip_service"]
