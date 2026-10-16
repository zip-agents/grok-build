# Local checks for zip-service.
#
# Fast tests and slow tests are separate so a small change does not pay for
# the large-upload run. `make check` is the full local gate.

UV ?= uv
UV_RUN = $(UV) run --frozen

.DEFAULT_GOAL := help

.PHONY: help install test test-fast test-slow lint format format-check typecheck check run \
	image image-run image-smoke

help:
	@printf '%s\n' \
		'targets:' \
		'  install        uv sync --frozen --group dev' \
		'  test-fast      unit tests and integration tests except the large upload' \
		'  test-slow      bounded-memory upload (pytest -m slow)' \
		'  test           test-fast then test-slow' \
		'  lint           ruff check' \
		'  format         ruff format (rewrites files)' \
		'  format-check   ruff format --check' \
		'  typecheck      mypy' \
		'  check          lint, format-check, typecheck, and test' \
		'  run            serve on PORT (default 8000)' \
		'  image          docker build -t $$IMAGE (default zip-service:dev)' \
		'  image-run      run the image read-only on port 8000' \
		'  image-smoke    build, then health and zip smoke test'

install:
	$(UV) sync --frozen --group dev

test-fast:
	$(UV_RUN) pytest -m "not slow"

test-slow:
	$(UV_RUN) pytest -m slow

test: test-fast test-slow

lint:
	$(UV_RUN) ruff check src tests

format:
	$(UV_RUN) ruff format src tests

format-check:
	$(UV_RUN) ruff format --check src tests

typecheck:
	$(UV_RUN) mypy

check: lint format-check typecheck test

run:
	$(UV_RUN) python -m zip_service

IMAGE ?= zip-service:dev

image:
	docker build -t $(IMAGE) .

image-run:
	docker run --rm --read-only --publish 8000:8000 --env PORT=8000 $(IMAGE)

image-smoke: image
	sh scripts/smoke.sh $(IMAGE)
