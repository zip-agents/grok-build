# Agents and engineers

This file is the working agreement for changes to zip-service. The contract in the task document (version 1.1.0) is the behavioural spec. The README explains the choices made where that contract is silent. Do not edit the exercise PDF.

This is an HTTP API. There is no command-line tool and no installable library. The process entry is `python -m zip_service`.

## Setup

Docker. Every `make` target below runs inside an image built from this repo. The host does not need Python, uv, unzip, or 7z.

| Task | Command |
|---|---|
| Fast tests | `make test-fast` |
| Slow tests | `make test-slow` |
| Both test suites, with coverage | `make test` |
| Lint | `make lint` |
| Format check | `make format-check` |
| Format | `make format` |
| Types | `make typecheck` |
| All of the above | `make check` |
| Run the API | `make run` |
| Runtime image | `make image` |
| Read-only smoke | `make image-smoke` |

`make check` builds the dev image once, then runs ruff, the format check, mypy, and both pytest suites inside it. Coverage includes the API subprocess. After the suites, `scripts/coverage_warn.py` warns on new `zip_service` lines the run did not execute. A warning does not fail the command. `make image-smoke` builds the runtime image and the dev image, starts the API read-only, and runs the HTTP checks in the dev image. `scripts/smoke.sh` only talks to the Docker daemon. `curl`, `unzip`, and `cmp` run in the dev image via `scripts/smoke_client.sh`.

`unzip` and `7z` are installed in the dev image. Do not skip a test because a tool is missing.

`docker build .` (last stage `runtime`) is the image that serves traffic. It accepts an optional `GIT_SHA` build argument, and a build that omits the argument still succeeds. `docker build --target dev` is the image that lints and tests. Do not bake `.env` into either image. The runtime image copies `pyproject.toml`, `README.md`, and `AGENTS.md` so the docs endpoints can serve them.

## Architecture

`zip_service/__main__.py` loads settings, installs `SIGTERM` and `SIGINT`, and runs uvicorn's `_serve` so uvicorn does not replace those handlers. It is not a CLI.

`zip_service/app.py` builds the FastAPI app, routes health checks, serves the baked markdown under `/docs`, and turns framework `404` and `405` into problem JSON. `POST /api/v1/zip` is a raw ASGI endpoint in `zip_service/api/endpoint.py`. OpenAPI, Swagger, and ReDoc stay off.

One request has three pieces:

1. The event-loop task parses multipart and pushes small commands onto a bounded queue.
2. `ZipWorker` (one daemon thread) compresses into `OutputBuffer` (`zip_service/runtime/buffer.py`). `zipfile.ZipFile` does the compression. A streamed entry is opened with `force_zip64=True` so it can grow past 2 GiB without seeking back to the local header.
3. A writer task copies that buffer to the ASGI send channel in slices of at most 64 KiB.

`zip_service/api/gate.py` counts in-flight zip requests and tracks shutdown. Health checks and documentation routes never take a slot.

| Path | Role |
|---|---|
| `zip_service/__main__.py` | Process entry |
| `zip_service/app.py` | Application, health, docs, `404` and `405` |
| `zip_service/documents.py` | Fixed docs allow-list and manifest |
| `zip_service/config.py` | Environment variables and `.env` |
| `zip_service/api/` | Zip route, multipart parser, limits, problem JSON |
| `zip_service/archive/` | Archive names, `Content-Disposition`, streaming ZIP |
| `zip_service/runtime/` | Output buffer, fixed byte caps, JSON logs |

`zip_service/archive/zipstream.py` is the only module that calls `zipfile`. On a failed stream it stops forwarding bytes before `ZipFile.close()`, so the end records are not sent to the client.

## Invariants

These are not style preferences. Breaking one of them fails the contract or deadlocks a client.

| Invariant | Why |
|---|---|
| Never buffer a whole upload, and never write upload bytes to disk. | Memory must stay independent of upload size, and nothing from the request may persist. |
| Never send the ZIP end-of-central-directory record, and never send the terminating HTTP chunk, after streaming has failed. | That is how the client sees a truncated archive and a closed connection. `abandon` discards bytes before `ZipFile.close()`. Do not use `ZipFile` as a context manager on the failure path. |
| Do not send response headers before the archive name is valid and the first non-skipped file part has started. Set `tracker.streaming` before the status line is sent. | Errors before that point must be normal problem responses. A failure during the header send must not emit a second response. |
| Do not let one request's compressed output exceed `OUTPUT_BUFFER_MAX_BYTES`. | A half-duplex client would deadlock, and memory would grow with the upload. |
| Stop reading the request when that buffer and the input queue are full. | That is the backpressure. Do not block the event loop on `Queue.put`. Use `try_put` / `try_finish` and the 0.1 s poll. |
| Entry names are sanitised, never rejected. | Later files arrive after the status line. Rejection is no longer possible. |
| Open every streamed entry with `force_zip64=True`. | The size is unknown when the local header is written. Without this flag, `zipfile` rejects an entry once it passes 2 GiB. |
| Run deflate off the event loop. | Health checks and other requests must stay responsive. |
| Health checks and documentation routes do not take a concurrency slot, and a zip request over the limit is rejected immediately. | `503 overloaded` is not a queue. |
| Serve documentation from the allow-list built at startup. | A request path is never joined onto a filesystem path. The handler can read only the files baked into the image. |
| Always release the slot, including on disconnect, cancel, and error. | A leaked slot permanently reduces capacity. |
| Stdout is JSON lines only. Configuration and documentation errors go to stderr. | Log shippers parse every stdout line. |
| Real environment variables override `.env`. Do not bake `.env` into the image. | The contract's precedence rule, and secrets must not be in the image. |
| Do not install `python-multipart` or `httptools`. | The first can spool to disk. The second would change how an unfinished response is closed. The abort behaviour is verified against uvicorn's h11 implementation. |

## How to verify a change

1. `make test-fast` for any change. It rebuilds the dev image and runs pytest inside it. `make test` or `make check` also warns when a new `zip_service` line has no coverage.
2. `make typecheck` and `make lint` when signatures or imports move.
3. If you touched the buffer, the worker, timeouts, shutdown, or the socket writer, also run `make test-slow` and the integration modules in `tests/integration/test_duplex.py` and `tests/integration/test_limits.py`.
4. If you touched name rules, disposition, or config, the unit tests in `tests/unit/` are the spec. Add a case there before changing the code.
5. If you touched the image, the Makefile, or `zip_service/documents.py`, run `make image-smoke` on a machine with Docker. `tests/integration/test_docs.py` checks that each manifest entry is served byte for byte.
6. Watch a zip completion log: one JSON line per request, and no other stdout.

A change that only passes an in-process ASGI client is not done. The integration tests exist because chunk framing, idle timeouts, and process shutdown are invisible there.

## Conventions

- Python 3.12, type hints everywhere. mypy is strict on `zip_service`. Tests are not in that list. Ruff line length is 100. Run `make format` before committing. It writes through a volume, as the uid of the caller.
- Comments explain a constraint that the code cannot show on its own, such as why `force_zip64` is set before the size is known. Do not restate the contract in comments.
- New settings belong in `zip_service/config.py`, `.env.example`, and the README. Invalid configuration exits 2 with the variable name on stderr. A baked document that is missing or larger than 1 MiB exits 2 with `documentation error` on stderr.
- `README.md` and `AGENTS.md` are served at `/docs`. The portal renders CommonMark, GitHub-style tables, and strikethrough, and it shows raw HTML as text. Name the other document in prose. Do not add a relative link, raw HTML, or a diagram the portal would have to render.
- Problem codes and statuses live in `zip_service/api/problems.py`. Add a code in one place so the abort status and the HTTP status cannot drift.
- Integration tests use `tests/integration/http_util.py` and a real subprocess from `server_proc.py` (`python -m zip_service` with `PYTHONPATH` set to the repo root). Do not add httpx for these tests. Leave `[tool.coverage.run] patch = ["subprocess"]` in place so that process is measured. Do not strip `COVERAGE_FILE` or `COVERAGE_PROCESS_START` from its environment.
- Mark a test `slow` only when it moves tens of megabytes or waits on a real timeout. Idle timeouts in tests should be about 1 second.
- `ZIP_SERVICE_TEST_HOOKS` is the only test seam. Do not add a backdoor that is on by default.
- Commit in small steps that leave `make test-fast` green. Do not commit `.env`, virtualenvs, or the PDF modified.
- The runtime image stays one process, uid 10001, and able to run with a read-only root filesystem. Dependencies live in `/opt/venv` so a bind-mount of the source does not hide them.
- `zip_service/__main__.py` is the process entry, not a command-line interface. Do not add a console script or a package named `http` (it would shadow the standard library when the repo root is on `sys.path`).
