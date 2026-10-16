# Agents and engineers

This file is the working agreement for changes to zip-service. The contract in the task document is the behavioural spec. `README.md` explains the choices made where that contract is silent. Do not edit the exercise PDF.

This is an HTTP API. There is no command-line tool and no installable library. The process entry is `server.py`.

## Setup

Docker. Every `make` target below runs inside an image built from this repo. The host does not need Python, uv, unzip, or 7z.

| Task | Command |
|---|---|
| Fast tests | `make test-fast` |
| Slow tests | `make test-slow` |
| Both test suites | `make test` |
| Lint | `make lint` |
| Format check | `make format-check` |
| Format | `make format` |
| Types | `make typecheck` |
| All of the above | `make check` |
| Run the API | `make run` |
| Runtime image | `make image` |
| Read-only smoke | `make image-smoke` |

`make check` builds the dev image once, then runs ruff, the format check, mypy, and both pytest suites inside it. `make image-smoke` builds the runtime image and the dev image, starts the API read-only, and runs the HTTP checks in the dev image. `scripts/smoke.sh` only talks to the Docker daemon. `curl`, `unzip`, and `cmp` run in the dev image via `scripts/smoke_client.sh`.

`unzip` and `7z` are installed in the dev image. Do not skip a test because a tool is missing.

`docker build .` (no build args, last stage `runtime`) is the image that serves traffic. `docker build --target dev` is the image that lints and tests. Do not bake `.env` into either image.

This checkout cannot run Docker or Podman. Verify image targets on a machine that can, and paste the output back.

## Architecture

`server.py` loads settings, installs `SIGTERM` and `SIGINT`, and runs uvicorn's `_serve` so uvicorn does not replace those handlers. It is not a CLI.

`app.py` builds the FastAPI app, routes health checks, and turns framework `404` and `405` into problem JSON. `POST /api/v1/zip` is a raw ASGI endpoint in `api/endpoint.py`.

One request has three pieces:

1. The event-loop task parses multipart and pushes small commands onto a bounded queue.
2. `ZipWorker` (one daemon thread) compresses into `OutputBuffer` (`runtime/buffer.py`).
3. A writer task copies that buffer to the ASGI send channel in slices of at most 64 KiB.

`api/gate.py` counts in-flight zip requests and tracks shutdown. Health checks never take a slot.

| Path | Role |
|---|---|
| `app.py` | Application, health, `404` and `405` |
| `config.py` | Environment variables and `.env` |
| `server.py` | Process entry |
| `api/` | Zip route, multipart parser, limits, problem JSON |
| `archive/` | Archive names, `Content-Disposition`, streaming ZIP |
| `runtime/` | Output buffer, fixed byte caps, JSON logs |

`archive/zipstream.py` is the only module that touches `zipfile` internals.

## Invariants

These are not style preferences. Breaking one of them fails the contract or deadlocks a client.

| Invariant | Why |
|---|---|
| Never buffer a whole upload, and never write upload bytes to disk. | Memory must stay independent of upload size, and nothing from the request may persist. |
| Never write the ZIP end-of-central-directory record, and never send the terminating HTTP chunk, after streaming has failed. | That is how the client sees a truncated archive and a closed connection. Do not `ZipFile.close()` on the failure path, and do not use `ZipFile` as a context manager there. |
| Do not send response headers before the archive name is valid and the first non-skipped file part has started. Set `tracker.streaming` before the status line is sent. | Errors before that point must be normal problem responses. A failure during the header send must not emit a second response. |
| Do not let one request's compressed output exceed `OUTPUT_BUFFER_MAX_BYTES`. | A half-duplex client would deadlock, and memory would grow with the upload. |
| Stop reading the request when that buffer and the input queue are full. | That is the backpressure. Do not block the event loop on `Queue.put`. Use `try_put` / `try_finish` and the 0.1 s poll. |
| Entry names are sanitised, never rejected. | Later files arrive after the status line. Rejection is no longer possible. |
| Every entry is opened with `force_zip64=True`. | The contract requires Zip64 extensions on every entry. |
| Run deflate off the event loop. | Health checks and other requests must stay responsive. |
| Health checks do not take a concurrency slot, and a zip request over the limit is rejected immediately. | `503 overloaded` is not a queue. |
| Always release the slot, including on disconnect, cancel, and error. | A leaked slot permanently reduces capacity. |
| Stdout is JSON lines only. Configuration errors go to stderr. | Log shippers parse every stdout line. |
| Real environment variables override `.env`. Do not bake `.env` into the image. | The contract's precedence rule, and secrets must not be in the image. |
| Do not install `python-multipart` or `httptools`. | The first can spool to disk. The second would change how an unfinished response is closed. The abort behaviour is verified against uvicorn's h11 implementation. |

## How to verify a change

1. `make test-fast` for any change. It rebuilds the dev image and runs pytest inside it.
2. `make typecheck` and `make lint` when signatures or imports move.
3. If you touched the buffer, the worker, timeouts, shutdown, or the socket writer, also run `make test-slow` and the integration modules in `tests/integration/test_duplex.py` and `tests/integration/test_limits.py`.
4. If you touched name rules, disposition, or config, the unit tests in `tests/unit/` are the spec. Add a case there before changing the code.
5. If you touched the image or the Makefile, run `make image-smoke` on a machine with Docker.
6. Watch a zip completion log: one JSON line per request, and no other stdout.

A change that only passes an in-process ASGI client is not done. The integration tests exist because chunk framing, idle timeouts, and process shutdown are invisible there.

## Conventions

- Python 3.12, type hints everywhere. mypy is strict on `app.py`, `config.py`, `server.py`, `api`, `archive`, and `runtime`. Tests are not in that list. Ruff line length is 100. Run `make format` before committing. It writes through a volume, as the uid of the caller.
- Comments explain a constraint that the code cannot show on its own, such as why `ZipFile` must not be closed. Do not restate the contract in comments.
- New settings belong in `config.py`, `.env.example`, and the README. Invalid configuration exits 2 with the variable name on stderr.
- Problem codes and statuses live in `api/problems.py`. Add a code in one place so the abort status and the HTTP status cannot drift.
- Integration tests use `tests/integration/http_util.py` and a real subprocess from `server_proc.py` (`python server.py` with `PYTHONPATH` set to the repo root). Do not add httpx for these tests.
- Mark a test `slow` only when it moves tens of megabytes or waits on a real timeout. Idle timeouts in tests should be about 1 second.
- `ZIP_SERVICE_TEST_HOOKS` is the only test seam. Do not add a backdoor that is on by default.
- Commit in small steps that leave `make test-fast` green. Do not commit `.env`, virtualenvs, or the PDF modified.
- The runtime image stays one process, uid 10001, and able to run with a read-only root filesystem. Dependencies live in `/opt/venv` so a bind-mount of the source does not hide them.
- Do not add a console script, a `__main__.py`, or a package named `http` (it would shadow the standard library when the repo root is on `sys.path`).
