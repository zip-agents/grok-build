# zip-service

HTTP service that accepts a `multipart/form-data` upload and streams back a ZIP archive. Upload bytes are not written to disk, and memory per request is bounded by configuration rather than by the size of the upload.

The behaviour is specified by the contract in the task document (version 1.0.0). This README records how to run the service and the choices made where that contract is silent.

## Run

The process is `python -m zip_service`. It listens on `0.0.0.0` and `PORT` (default `8000`). There is no command-line interface. In development, a `.env` file in the working directory is loaded when present. Real environment variables override it. Copy `.env.example` to start one. The service also starts with no `.env` at all.

The host needs Docker. Python 3.12 and the dependencies live in the image. From this directory:

```bash
make run
```

```bash
curl -sS -o out.zip -D - \
  -F 'filename=quarterly report' \
  -F 'files=@a.txt' \
  -F 'files=@a.txt' \
  -F 'files=@photo.jpg' \
  http://localhost:8000/api/v1/zip
```

## Layout

This is an HTTP API. The package is `zip_service`, and `python -m zip_service` is the process. There is no command-line interface.

| Path | Role |
|---|---|
| `zip_service/__main__.py` | Process entry: signals and uvicorn |
| `zip_service/app.py` | FastAPI application, health routes, `404` and `405` |
| `zip_service/config.py` | Environment variables and `.env` |
| `zip_service/api/` | `POST /api/v1/zip`, multipart parser, limits, problem JSON |
| `zip_service/archive/` | Archive names, `Content-Disposition`, streaming ZIP |
| `zip_service/runtime/` | Output buffer, fixed byte caps, JSON logs |

## Tests, lint, and types

`make check` builds the dev image and runs lint, the format check, mypy, and both test suites inside it. The suites record coverage of `zip_service`, including the `python -m zip_service` process the integration tests start. At the end, new lines in `zip_service` that the run did not execute are printed as warnings. The command still exits 0 when those lines exist. In GitHub Actions the same warnings are annotations on the diff. `COVERAGE_DIFF_BASE` chooses the git commit to diff against. CI sets it to the pull-request base, or to the previous commit on a push. The same steps one at a time:

```bash
make dev-image
make lint
make format-check
make typecheck
make test-fast
make test-slow
```

Those expand to:

```bash
docker build --target dev -t zip-service:check .

docker run --rm --env PYTHONDONTWRITEBYTECODE=1 zip-service:check \
  ruff check zip_service tests scripts

docker run --rm --env PYTHONDONTWRITEBYTECODE=1 zip-service:check \
  ruff format --check zip_service tests scripts

docker run --rm --env PYTHONDONTWRITEBYTECODE=1 zip-service:check mypy

docker run --rm --env PYTHONDONTWRITEBYTECODE=1 zip-service:check \
  pytest -m "not slow"

docker run --rm --env PYTHONDONTWRITEBYTECODE=1 zip-service:check \
  pytest -m slow
```

`make format` runs `ruff format` in the dev image with the tree mounted, so it can write files. `make test` is the fast suite followed by the slow suite, after one image build, and then the same coverage warning as `make check`. `make test-fast` and `make test-slow` do not collect coverage.

The coverage steps `make test` runs:

```bash
mkdir -p .coverage-data

docker run --rm \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env COVERAGE_FILE=/coverage/.coverage \
  --volume "$PWD/.coverage-data:/coverage" \
  zip-service:check \
  pytest -m "not slow" --cov=zip_service --cov-append --cov-report=

docker run --rm \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env COVERAGE_FILE=/coverage/.coverage \
  --volume "$PWD/.coverage-data:/coverage" \
  zip-service:check \
  pytest -m slow --cov=zip_service --cov-append \
    --cov-report=term-missing --cov-report=xml:/coverage/coverage.xml

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --volume "$PWD:/repo" \
  --volume "$PWD/.coverage-data:/coverage:ro" \
  zip-service:check \
  python /opt/coverage_warn.py --repo /repo --xml /coverage/coverage.xml
```

Integration tests start a real `python -m zip_service` process inside that container and speak HTTP/1.1 on a socket. `unzip` and `7z` are installed in the dev image. A missing tool fails the test rather than skipping it.

## Container

The image builds with `docker build .` from this directory. No build arguments. It runs as uid 10001, listens on `PORT`, and does not need a network at runtime. `PYTHONDONTWRITEBYTECODE=1` keeps a read-only root working without a writable `/tmp`.

`make image-smoke` builds the runtime image and the dev image, starts the API read-only, and runs the HTTP checks inside the dev image. The same steps one at a time:

```bash
docker build -t zip-service:dev .
docker build --target dev -t zip-service:check .

docker network create zip-service-smoke-net
docker run -d --name zip-service-smoke \
  --network zip-service-smoke-net --network-alias zip-api \
  --read-only --env PORT=8000 zip-service:dev

docker run --rm --network zip-service-smoke-net \
  --env PORT=8000 --env API_HOST=zip-api \
  zip-service:check sh /opt/smoke_client.sh

docker stop zip-service-smoke
docker inspect -f '{{.State.ExitCode}}' zip-service-smoke
docker rm -f zip-service-smoke
docker network rm zip-service-smoke-net
```

`make image` is only `docker build -t zip-service:dev .`. `make run` is the foreground server:

```bash
docker run --rm --read-only --publish 8000:8000 --env PORT=8000 zip-service:dev
```

`docker stop` sends `SIGTERM`. An idle process exits as soon as in-flight work has drained, so the default 10 second stop timeout is enough when nothing is uploading. An orchestrator that may interrupt a live upload must set its stop grace **above** `SHUTDOWN_GRACE_SECONDS` (default 25). Otherwise it will `SIGKILL` the process before the grace period ends.

The image is one process on purpose. Extra workers would each enforce `MAX_CONCURRENT_REQUESTS` separately.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests, on pushes to `main`, and on `workflow_dispatch`. A newer push cancels the previous run on the same ref. The workflow has `contents: read` only.

| Job | Purpose |
|---|---|
| `check` | `make check`. Builds the dev image and runs ruff, mypy, and both test suites inside it, including `unzip` and `7z`. The code under test is the code copied into the image. New `zip_service` lines with no coverage are annotations on the diff. They do not fail the job. |
| `image` | `make image-smoke`. Builds the runtime image with `docker build .` and checks health plus one archive through a second container. This is the image that serves traffic: non-root, read-only, no test tools. |

The jobs run in parallel. A broken dev image does not hide a broken runtime image. `actions/checkout` is pinned to v7.0.1. The image installs uv 0.13.1, which is the version that produced `uv.lock`.

## Design

### Why these libraries

The service is a small ASGI app: FastAPI for routing and health handlers, uvicorn for the HTTP/1.1 server, and the standard library for ZIP, compression, and multipart. `python-multipart` is not used because it can spool part bodies to disk. There is no HTTP client library in production; tests speak raw HTTP so chunked framing stays visible.

uv installs dependencies into the image. The service is not packaged as a library (`package = false`). Python 3.12 matches the pinned container image (`python:3.12.15-slim-bookworm`).

Compression runs on one daemon thread per request. The event loop only parses multipart, applies backpressure, and writes the socket, so a health check stays responsive while an archive is produced. A thread pool is not used: the default pool can be smaller than `MAX_CONCURRENT_REQUESTS`, and a blocking `Queue.put` on that pool would stall the loop.

### Streaming

The handler reads the upload as ASGI body chunks. A custom multipart parser keeps at most one read chunk (64 KiB) plus a short boundary lookahead. Preamble and part headers are capped at 64 KiB each.

The first non-skipped file part is when streaming begins. The response status and headers are sent then, after the archive name has been validated, and not before. Each file is compressed as its bytes arrive. `zipfile.ZipFile` writes to an unseekable sink. Because the size is not known yet, each entry is opened with `force_zip64=True`, which is how the standard library allows a streamed entry to grow past 2 GiB. The local header is therefore version 45. The central directory and the end-of-central-directory record are written only after the last file has been closed and the multipart epilogue has been drained. The terminating HTTP chunk is sent only after that.

On a failure after the status line, the sink stops forwarding and the archive is closed through `ZipFile`. The end records are produced and then dropped, so they never reach the client. The ASGI response is returned without `more_body=False`, which makes uvicorn close the connection without the terminating `0` chunk. The client receives a truncated archive that `unzip` rejects.

### Duplex behaviour and the output buffer

Many clients do not read the response until they have finished sending the request. Writing faster than such a client reads would block both sides.

Compressed output is stored in one in-memory buffer per request, capped at `OUTPUT_BUFFER_MAX_BYTES` (default 16 MiB). The socket writer drains that buffer in slices of at most 64 KiB. When the buffer is full, the zip thread stops. The parser's input queue (4 chunks) then fills, the parser stops calling `receive()`, and the server stops reading the socket. If neither a request-body byte nor a response-body byte moves for `IDLE_TIMEOUT_SECONDS`, the request is aborted.

Consequences, as required by the contract:

- A half-duplex client succeeds when the compressed output produced before the upload finishes fits in the buffer.
- A full-duplex client succeeds at any size within the time limits, because it drains the buffer while it sends.

Response headers do not count as progress. Both the idle timer and the total-duration timer use the code `request-timeout`.

### Memory budget

The output buffer is the only part of the per-request budget that an operator sets. The rest is fixed:

| Piece | Bytes |
|---|---|
| Socket read chunk | 65 536 |
| Input queue, 4 chunks | 262 144 |
| zlib window and ZIP bookkeeping (allowance) | 262 144 |
| Part-header cap | 65 536 |
| **Overhead** | **655 360** |

Per process, while every slot is busy:

```text
MAX_CONCURRENT_REQUESTS × (OUTPUT_BUFFER_MAX_BYTES + 655360)
```

With the defaults that is `32 × (16777216 + 655360)` = 558 137 344 bytes, about 532 MiB. This is a ceiling on what the process retains, not a reservation. Kernel socket buffers sit on top of it. They are bounded by the operating system and do not grow with the upload.

A blocked socket write holds at most one extra 64 KiB slice, because the writer never pulls more than that out of the buffer at once.

### Archive tools the output was tested with

Streamed entries use Zip64 local headers because `force_zip64=True` is set before the size is known. The Zip64 end-of-central-directory record is emitted only when a size exceeds the classic fields. Small archives therefore have Zip64 local headers and a classic end record. That is what CPython's `zipfile` produces, and it is what was tested.

Tested locally:

- UnZip 6.00 (Info-ZIP, 20 April 2009), `unzip -t`
- 7-Zip (z) 25.01, `7z t`
- 7-Zip 26.01 from p7zip, `7z t`
- Python `zipfile.ZipFile.testzip()` on 3.12.15

The dev image installs Debian `unzip` and `p7zip-full`. CI runs the same checks inside that image.

### Request handling order

For `POST /api/v1/zip`:

1. If the process is shutting down, `503 shutting-down`, before the media type is considered.
2. A non-multipart `Content-Type` is `415`. A multipart type with no usable boundary is `400 malformed-request`. Neither takes a concurrency slot.
3. Otherwise the request takes a slot or gets `503 overloaded` with `Retry-After: 5`.
4. The body is parsed. The slot is released when the request finishes, including on disconnect and on failure.

The gate is a counter, not a semaphore. Extra requests are rejected immediately. Health endpoints do not take a slot.

### Shutdown

`SIGTERM` or `SIGINT` marks the process as shutting down, fails `/health/ready`, and rejects new zip requests with `503 shutting-down`. The listen socket stays open during the grace period so those responses can be sent. uvicorn's own signal handler is not used, because it closes the listen socket immediately.

In-flight requests may finish until `SHUTDOWN_GRACE_SECONDS`. The process then cancels whatever remains and exits 0. If nothing is in flight, it exits without waiting out the grace period. A second signal cancels in-flight work immediately.

## Choices where the contract is silent

- **Problem text.** `no-files` uses the title and detail from the contract example. Every other problem uses the title and detail in `zip_service/api/problems.py`. Clients should match on `code`. Bodies are compact JSON with keys in the order `type`, `title`, `status`, `detail`, `code`, `request_id`.
- **`413` before streaming.** Streaming starts when the first non-skipped file is accepted, so with `MAX_FILES >= 1` the count cannot exceed the limit before the status line. `413 too-many-files` is still the code logged when a later file trips the limit, and the connection is aborted. The `413` problem body is implemented and unit-tested for a caller that hits it before streaming.
- **Zip64 end record.** See the tools section above.
- **Entry names after de-duplication** are not truncated again. `report (1).txt` can exceed 255 bytes. The contract's 255-byte rule applies before the ` (n)` suffix.
- **De-duplication** uses `str.casefold()` and keeps the new name's original case. `ß` and `ss` collide.
- **Truncation** is on Unicode code points, not grapheme clusters. The extension length is the UTF-8 size of the bytes after the last dot, not including the dot. A leading dot is not an extension.
- **Archive-name whitespace** is Unicode `str.strip()`. C1 controls (U+0080–U+009F) are allowed in archive names and in entry names. There is no BOM stripping.
- **Filename field.** Field names are case-sensitive. An empty or whitespace value still counts as a `filename` field and resolves to `zipped_files.zip`. A value over 1024 raw bytes, or invalid UTF-8, is `422` as soon as that part has been read. A second `filename` field is `422` at its headers. An invalid name is returned when it is detected, including when no file part follows.
- **Empty `filename=""`** on a part whose field name is not `files` is skipped. It is not `unexpected-file-field`.
- **`filename*`.** Preferred over `filename` when both are present. Decoded as RFC 5987 for UTF-8, ASCII, and ISO-8859-1. `+` is not a space. A failed `filename*` falls back to `filename`, or to an empty name (the part is then skipped). RFC 2231 `filename*0*` continuations are not reassembled.
- **Ordinary `filename` parameters** are header Latin-1 code units, decoded as UTF-8 when that is valid, otherwise left as Latin-1.
- **Multipart framing** is strict CRLF. The boundary is 1–70 RFC 2046 `bchars`, including space. The last `boundary` parameter on `Content-Type` wins. The first `Content-Disposition` wins. Folded header lines are joined. No `Content-Transfer-Encoding` decoding.
- **Preamble and part headers** over 64 KiB are a malformed body.
- **Timestamps** are the request's arrival time in UTC, stored in the DOS fields, rounded down to an even second. External attributes are `0o644 << 16`.
- **HTTP surface.** HTTP/1.1 only. `Date` and `Server` are omitted. `proxy_headers` is off. No `Content-Length` on a zip response. Error responses, including framework `404` and `405`, are `application/problem+json` with `Connection: close`. `405` keeps `Allow`. OpenAPI, Swagger, and ReDoc are off, and slash redirects are off, so an unknown path or a trailing slash is `404`. Health `503` is `{"status":"shutting-down"}`, not a problem document. Success responses may stay keep-alive.
- **Request id.** The first `X-Request-ID` that matches `^[A-Za-z0-9._-]{1,128}$` is used, with no trimming. Otherwise a UUIDv4. It is stored on the ASGI scope so `404` and `405` still echo it.
- **Logs.** JSON lines on stdout only. Configuration errors go to stderr and the process exits 2. Each zip request emits one line, message `zip request finished`. `duration_ms` is rounded to an integer. `bytes_in` is request body bytes. `bytes_out` is response body bytes, not headers and not the terminating chunk. `files_count` counts non-skipped file parts that started, including a partial one. `code` is omitted when `outcome` is `completed`.
- **Outcomes.** A full HTTP error before streaming is `rejected`. A cut stream is `aborted`, and `status` is the status that would have been sent. A client disconnect before any status is `client_disconnected` with `status` 0 and `code` `client-disconnected`. A disconnect after `200` keeps `status` 200. Shutdown of a request that still receives a complete `503` is `rejected`. Cancelling a stream at the end of grace is `aborted` with `503` and `shutting-down`. Levels: completed and shutting-down are `INFO`, client-caused failures are `WARNING`, `internal-error` is `ERROR` (the log may include `error_type`, never the exception text). A field part after streaming has begun logs one `WARNING` and does not add a second completion line. `404` and `405` log `request rejected` and are not zip completion lines. Health requests are not logged.
- **Internal errors** return the fixed problem body. Stack traces and exception messages are not sent to the client.
- **Configuration.** An empty string is a set value and is validated, not replaced by the default. Integers must match `[+-]?\d+`. `PORT` is 1–65535. `ZIP_COMPRESSION_LEVEL` is 0–9. `SHUTDOWN_GRACE_SECONDS` may be 0. Other limits are integers ≥ 1 with no upper cap. `LOG_LEVEL` is case-insensitive. The `.env` parser accepts comments, an optional `export` prefix, and matching quotes. It does not interpolate. A malformed line fails startup.
- **Test fault.** `ZIP_SERVICE_TEST_HOOKS=1` plus header `X-Zip-Test-Fault: before-stream` or `mid-stream` injects an internal error. The header is ignored unless the environment variable is set. It is not a supported setting and is absent from `.env.example`.

## Known limitations

- A client that disconnects while the parser is waiting on a full input queue, rather than inside `receive()`, is noticed within `IDLE_TIMEOUT_SECONDS`, not instantly.
- `413` is not returned as an HTTP response for an over-long file list when `MAX_FILES` is at least 1. The connection is aborted instead. See the choices above.
- De-duplicated entry names can exceed 255 bytes.
- There is no authentication, TLS, per-client rate limit, or persistence. Those are non-goals.
- The concurrency limit is per process.
- `docker stop`'s default 10 second timeout is shorter than the default grace period.
- DOS timestamps have a 2 second resolution and no time zone field.
- Multipart headers and preamble are capped at 64 KiB. Continuations of RFC 2231 parameters, and content-transfer encodings, are not supported.

## With more time

- Property tests and a multipart fuzzer around the parser and the name rules.
- A measured choice of chunk size, and a histogram of RSS under the default concurrency.
- HTTP/2, once a client that interleaves upload and download is part of the deployment.
- Request metrics (in-flight count, abort reason) without putting another format on stdout.
- `pip-audit` in CI, and a Kubernetes `preStop` example that waits longer than `SHUTDOWN_GRACE_SECONDS`.
- RFC 2231 continuation parameters, if a real client sends them.
