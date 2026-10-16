# Original plan

This is the implementation plan chosen before the first commit. The task is the Senior Backend Engineer take-home: a streaming multipart-to-ZIP HTTP API. Tasks 1–3 only. The written AI-assisted-development answer (task 4) is out of scope. The brief PDF stays unmodified.

The contract in the task message (v1.0.0) is the behavioural spec. Where the brief and the contract differ, the contract wins. Where the contract is silent, pick a behaviour and record it in the README in one sentence. No speculative abstraction.

`README.md` describes the service as it was built. Later requests changed the layout, how Make runs, Zip64, and coverage. Those changes are not part of this plan.

## Environment

The checkout is the repo root. Python, uv, unzip, and 7z are available. Docker and Podman are not, so the image is written here and built on a machine that has Docker. Git history is part of the review: small commits, tests green at each one. Local git identity only: `Zip Service` / `dev@zip-service.local`.

## Stack and layout

Python 3.12. uv, with `uv.lock` committed. FastAPI and uvicorn. Dev tools: pytest, ruff, mypy. No `python-multipart` (it can spool bodies to disk), no `httpx`, no `python-dotenv`.

Installable package under `src/zip_service` (hatchling), typed (`py.typed`). Process entry: `python -m zip_service`. One process, on purpose, so the concurrency limit is not multiplied by workers.

Modules:

| Module | Role |
|---|---|
| `config` | Environment and `.env` |
| `jsonlog` | JSON lines on stdout |
| `constants` | Fixed byte caps and the memory-overhead helper |
| `names` | Archive name and entry-name rules |
| `disposition` | `Content-Disposition` |
| `problems` | RFC 9457 problem bodies |
| `multipart` | Streaming multipart parser |
| `buffer` | Bounded output buffer |
| `zipstream` | Streaming ZIP writer and worker thread |
| `gate` | Concurrency slots and shutdown |
| `app` | FastAPI app, health, 404 and 405 |
| `endpoint` | `POST /api/v1/zip` |
| `server` | Signals and uvicorn |

## Streaming

Do not buffer a whole upload, and do not write upload bytes to disk. Memory per request is bounded by configuration, not by upload size.

One request has three pieces:

1. The event-loop task parses multipart and pushes small chunks onto a `queue.Queue` of 4 slots.
2. One daemon thread compresses into an `OutputBuffer` capped at `OUTPUT_BUFFER_MAX_BYTES`. Writes larger than the cap are sliced so a single chunk cannot deadlock the buffer.
3. An async task copies that buffer to the ASGI send channel.

`zipfile.ZipFile` writes to an unseekable sink with `force_zip64=True`, so every local header is Zip64 (version 45). Level 0 is `ZIP_STORED`. Other levels are DEFLATE at `ZIP_COMPRESSION_LEVEL`. The central directory is written only from `close`. On failure, point the file at a discard sink, finish the open entry there, then clear ZipFile's write flags so `__del__` does not append an end record. Do not use `ZipFile` as a context manager on that path.

When the output buffer is full, stop reading the socket. If neither a request-body byte nor a response-body byte moves for `IDLE_TIMEOUT_SECONDS`, abort. Half-duplex clients succeed when the compressed output produced before the upload finishes fits in the buffer. Full-duplex clients succeed at any size within the other limits. Poll blocking waits about every 0.1 s so health checks and timeouts stay responsive. Deflate runs off the event loop. Do not use the default thread pool for the copy; it can be smaller than `MAX_CONCURRENT_REQUESTS`.

Per-instance ceiling, not a reservation:

`MAX_CONCURRENT_REQUESTS × (OUTPUT_BUFFER_MAX_BYTES + overhead)`

Overhead is one 64 KiB read, four 64 KiB queue slots, about 256 KiB for zlib, and 64 KiB for part headers. Kernel socket buffers sit on top of that and are bounded by the operating system.

## HTTP behaviour

`POST /api/v1/zip`. Streaming begins when the status line and headers are sent, no earlier than the first non-skipped file part, and only after the archive name is valid. Until then, every error is a normal problem response.

Check order for a zip request:

1. Shutting down, before the media type.
2. A non-multipart type, or multipart with no usable boundary. Neither takes a slot.
3. Acquire a concurrency slot, or return `503 overloaded` immediately with `Retry-After: 5`. The gate is a counter, not a semaphore.
4. Parse the body.

Health checks are not under `/api/v1` and never take a slot. OpenAPI, docs, and redoc are off. `redirect_slashes` is false. The success response has no `Content-Length`. uvicorn adds chunked transfer encoding. Do not set that header, or `Date`, or `Server`.

`413 too-many-files` before streaming is unreachable when `MAX_FILES` is at least 1, because streaming starts at the first real file. Keep the 413 body, test it as a body, and integration-test the later file as a mid-stream abort.

A test-only header `X-Zip-Test-Fault` is honoured when `ZIP_SERVICE_TEST_HOOKS=1`. It is not a contract setting and is absent from `.env.example`.

## Errors and logs

Problem JSON is `application/problem+json`, compact, keys in the order `type`, `title`, `status`, `detail`, `code`, `request_id`. `no-files` matches the contract text. The other titles and details are chosen in `problems.py`. `Connection: close` on problem responses. `405` keeps `Allow`.

After the status line, a failure stops the body immediately: no end-of-central-directory record, no terminating `0` chunk, connection closed. Log the code that would have been sent.

Stdout is JSON lines only. Each zip request emits one completion line: method, path, status, `duration_ms`, `bytes_in`, `bytes_out`, `files_count`, and `outcome` (`completed`, `rejected`, `aborted`, `client_disconnected`). `code` is omitted when the outcome is `completed`. Aborts log the status that would have been sent. A disconnect before any status logs status 0. A disconnect after 200 logs status 200 and `client_disconnected`. Levels: completed INFO, client-caused WARNING, internal ERROR, shutting-down INFO, overloaded WARNING.

Request id: the first `X-Request-ID` that matches `^[A-Za-z0-9._-]{1,128}$`, otherwise a UUIDv4. Echo it on every response. uvicorn's access log and banner are off.

## Configuration

Read the environment at startup. Invalid configuration exits 2 with the variable name on stderr. In development, also load `.env` from the working directory when it exists. Real environment variables win. An empty string is a set value and is validated. Missing variables use the contract defaults, so the service runs with no `.env`. `.env` is gitignored and dockerignored. `.env.example` lists every variable.

`SHUTDOWN_GRACE_SECONDS` may be 0 so tests can exit immediately. Other limits are at least 1, with no extra upper cap. `PORT` is 1–65535. `ZIP_COMPRESSION_LEVEL` is 0–9. `LOG_LEVEL` is case-insensitive.

On `SIGTERM` or `SIGINT`, fail readiness, reject new zip requests with `503 shutting-down`, let in-flight work finish until the grace period, then abort what remains and exit 0. A second signal exits promptly. uvicorn must not install its own handlers, because those close the listen socket before the 503 can be sent.

## Tests

Fast and slow are separate pytest commands. Fast is `pytest -m "not slow"`. Slow is `pytest -m slow`. Idle timeouts in tests are about 1 second.

Unit tests cover archive names, entry names and de-duplication, `Content-Disposition`, and configuration. Integration tests speak HTTP to a real `python -m zip_service` process. The client is raw asyncio sockets, so chunk framing stays visible.

The integration set covers archive bytes with `zipfile`, `unzip -t`, and `7z t`; bounded RSS and no new writable files on a large upload; half-duplex success when output fits; full-duplex success with an upload far larger than the buffer; idle abort of a stalled client; every pre-stream error code; a truncated archive with no end record and no terminating chunk; slot release on disconnect; `503 overloaded`; graceful shutdown while a request is in flight.

A missing `unzip` or `7z` fails the run.

## Container and CI

`docker build .` from the service root, no build arguments. Multi-stage `python:3.12-slim` pinned by digest. Non-root, read-only root, listen on `PORT`, no network at runtime. `HEALTHCHECK` hits `/health/live` with the standard library. Document that `docker stop`'s default 10 seconds is shorter than the 25 second grace.

GitHub Actions on pull requests, pushes to `main`, and `workflow_dispatch`. Cancel a superseded run. `contents: read`. Three jobs in parallel: lint (ruff and mypy), tests (fast then slow, with unzip and 7z installed on the runner), and image build plus a read-only smoke. The README explains each job.

## Commit order

Each commit leaves the fast tests green.

1. Package skeleton, configuration, and logging.
2. Archive names and `Content-Disposition`.
3. Multipart parser.
4. ZIP writer and output buffer.
5. HTTP app and integration tests.
6. Dockerfile, CI, README, and `AGENTS.md`.

## Silent choices to document

- Strict CRLF multipart. Preamble and part headers capped at 64 KiB. Boundary is 1–70 RFC bchars, including space. `filename*` is preferred and decoded as UTF-8, ASCII, or ISO-8859-1. `+` is not a space. No content-transfer decoding. No RFC 2231 continuations.
- Entry-name extension length is the UTF-8 bytes after the last dot. A leading dot is not an extension. Truncate on code points. De-duplicated names are not truncated again and may exceed 255 bytes. Case folding is `str.casefold()`.
- All entries share the request's UTC time, stored as DOS fields at 2 second resolution. External attributes are `0o644 << 16`.
- Problem titles and details other than `no-files` are the strings in `problems.py`.
- HTTP/1.1 only. The concurrency limit is per process.

## Invariants

- Never buffer a whole upload or write upload bytes to disk.
- Never write the ZIP end record, and never send the terminating HTTP chunk, after streaming has failed.
- Never send response headers before the archive name is valid and the first real file has started.
- Never let one request's compressed output exceed `OUTPUT_BUFFER_MAX_BYTES`.
- Entry names are sanitised, never rejected.
- Every entry is opened with `force_zip64=True`.
- Health checks do not take a concurrency slot.
- Stdout is JSON lines only.
- Always release the concurrency slot.
- Compression stays off the event loop.
- Do not `ZipFile.close()` on the failure path.
- Real environment variables override `.env`.

## If there is more time

Property tests, a multipart fuzzer, HTTP/2, request metrics, a small ZIP writer that does not touch private `ZipFile` attributes, a measured chunk size, pinned action SHAs, `pip-audit`, a Kubernetes `preStop` example, and RFC 2231 continuations.
