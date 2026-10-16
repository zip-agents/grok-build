#!/bin/sh
# Build is separate (`make image`). This talks to a read-only container.
set -eu

IMAGE="${1:-zip-service:dev}"
PORT="${PORT:-8000}"
NAME="${SMOKE_NAME:-zip-service-smoke}"

docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run -d --name "$NAME" --read-only \
    --env "PORT=$PORT" \
    --publish "${PORT}:${PORT}" \
    "$IMAGE"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:${PORT}/health/live" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

curl -fsS "http://127.0.0.1:${PORT}/health/live"
printf '\n'
curl -fsS "http://127.0.0.1:${PORT}/health/ready"
printf '\n'

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"; docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

printf 'hello\n' > "$tmpdir/a.txt"
curl -fsS -D "$tmpdir/headers.txt" -o "$tmpdir/out.zip" \
    -F 'filename=quarterly report' \
    -F "files=@${tmpdir}/a.txt" \
    "http://127.0.0.1:${PORT}/api/v1/zip"

grep -i '^content-type: application/zip' "$tmpdir/headers.txt"
grep -i 'filename="quarterly report.zip"' "$tmpdir/headers.txt"
unzip -t "$tmpdir/out.zip"
unzip -p "$tmpdir/out.zip" a.txt > "$tmpdir/got.txt"
cmp "$tmpdir/a.txt" "$tmpdir/got.txt"

# Idle process: SIGTERM should drain immediately and exit 0.
docker stop "$NAME" >/dev/null

printf 'smoke ok\n'
