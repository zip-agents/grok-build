# Every check runs in the dev image. `docker build .` is the runtime API image.
# `make check` and `make image-smoke` are the full runs. The other targets are
# the same steps one at a time.

IMAGE ?= zip-service:dev
DEV_IMAGE ?= zip-service:check
PY = zip_service tests scripts
DEV_RUN = docker run --rm --env PYTHONDONTWRITEBYTECODE=1 $(DEV_IMAGE)
COV_DIR := $(CURDIR)/.coverage-data
COV_RUN = docker run --rm \
	--env PYTHONDONTWRITEBYTECODE=1 \
	--env COVERAGE_FILE=/coverage/.coverage \
	--volume "$(COV_DIR):/coverage" \
	$(DEV_IMAGE)
COV_WARN = docker run --rm \
	--user "$(shell id -u):$(shell id -g)" \
	--env HOME=/tmp \
	--env COVERAGE_DIFF_BASE \
	--volume "$(CURDIR):/repo" \
	--volume "$(COV_DIR):/coverage:ro" \
	$(DEV_IMAGE) \
	python /opt/coverage_warn.py --repo /repo --xml /coverage/coverage.xml

.DEFAULT_GOAL := help

.PHONY: help dev-image image image-run image-smoke run \
	lint format format-check typecheck test-fast test-slow test check coverage-warn

help:
	@printf '%s\n' \
		'full runs:' \
		'  check          lint, format-check, typecheck, both suites, coverage warn' \
		'  image-smoke    build the API image and smoke-test it' \
		'one step at a time:' \
		'  dev-image      docker build --target dev' \
		'  lint           ruff check' \
		'  format-check   ruff format --check' \
		'  format         ruff format (writes the tree through a volume)' \
		'  typecheck      mypy' \
		'  test-fast      pytest -m "not slow"' \
		'  test-slow      pytest -m slow' \
		'  test           both suites, then warn on new uncovered lines' \
		'  coverage-warn  warn from the coverage report make test wrote' \
		'  image          docker build .   (runtime API image)' \
		'  run            serve that image, read-only, on port 8000'

dev-image:
	docker build --target dev -t $(DEV_IMAGE) .

image:
	docker build -t $(IMAGE) .

lint: dev-image
	$(DEV_RUN) ruff check $(PY)

format-check: dev-image
	$(DEV_RUN) ruff format --check $(PY)

format: dev-image
	docker run --rm \
		--user "$(shell id -u):$(shell id -g)" \
		--env HOME=/tmp \
		--volume "$(CURDIR):/work" \
		--workdir /work \
		$(DEV_IMAGE) ruff format --no-cache $(PY)

typecheck: dev-image
	$(DEV_RUN) mypy

test-fast: dev-image
	$(DEV_RUN) pytest -m "not slow"

test-slow: dev-image
	$(DEV_RUN) pytest -m slow

test:
	docker build --target dev -t $(DEV_IMAGE) .
	rm -rf "$(COV_DIR)"
	mkdir -p "$(COV_DIR)"
	$(COV_RUN) pytest -m "not slow" --cov=zip_service --cov-append --cov-report=
	$(COV_RUN) pytest -m slow --cov=zip_service --cov-append \
		--cov-report=term-missing --cov-report=xml:/coverage/coverage.xml
	$(COV_WARN)

coverage-warn: dev-image
	$(COV_WARN)

check:
	docker build --target dev -t $(DEV_IMAGE) .
	$(DEV_RUN) ruff check $(PY)
	$(DEV_RUN) ruff format --check $(PY)
	$(DEV_RUN) mypy
	rm -rf "$(COV_DIR)"
	mkdir -p "$(COV_DIR)"
	$(COV_RUN) pytest -m "not slow" --cov=zip_service --cov-append --cov-report=
	$(COV_RUN) pytest -m slow --cov=zip_service --cov-append \
		--cov-report=term-missing --cov-report=xml:/coverage/coverage.xml
	$(COV_WARN)

run: image
	docker run --rm --read-only --publish 8000:8000 --env PORT=8000 $(IMAGE)

image-run: run

image-smoke: image dev-image
	IMAGE=$(IMAGE) DEV_IMAGE=$(DEV_IMAGE) sh scripts/smoke.sh
