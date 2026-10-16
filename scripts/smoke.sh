#!/bin/sh
# Host-side docker orchestration. The HTTP checks run in the dev image.
set -eu

IMAGE="${IMAGE:-zip-service:dev}"
DEV_IMAGE="${DEV_IMAGE:-zip-service:check}"
PORT="${PORT:-8000}"
NAME="${SMOKE_NAME:-zip-service-smoke}"
NET="${SMOKE_NET:-zip-service-smoke-net}"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$NAME" --network "$NET" --network-alias zip-api \
    --read-only --env "PORT=${PORT}" "$IMAGE"

if [ -n "${GIT_SHA:-}" ]; then
    expected_sha="$GIT_SHA"
else
    expected_sha=unknown
fi

docker run --rm --network "$NET" \
    --env "PORT=${PORT}" --env API_HOST=zip-api \
    --env EXPECTED_GIT_SHA="$expected_sha" \
    "$DEV_IMAGE" sh /opt/smoke_client.sh

docker stop "$NAME" >/dev/null
status=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
test "$status" -eq 0
printf 'api exit %s\n' "$status"
