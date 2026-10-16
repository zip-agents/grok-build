#!/bin/sh
# Runs inside the dev image, on the same Docker network as the API.
set -eu

HOST="${API_HOST:-zip-api}"
PORT="${PORT:-8000}"
base="http://${HOST}:${PORT}"

i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS "${base}/health/live" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

curl -fsS "${base}/health/live"
printf '\n'
curl -fsS "${base}/health/ready"
printf '\n'

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
printf 'hello\n' > "${tmpdir}/a.txt"
curl -fsS -D "${tmpdir}/headers.txt" -o "${tmpdir}/out.zip" \
    -F 'filename=quarterly report' \
    -F "files=@${tmpdir}/a.txt" \
    "${base}/api/v1/zip"

grep -i '^content-type: application/zip' "${tmpdir}/headers.txt"
grep -i 'filename="quarterly report.zip"' "${tmpdir}/headers.txt"
unzip -t "${tmpdir}/out.zip"
unzip -p "${tmpdir}/out.zip" a.txt > "${tmpdir}/got.txt"
cmp "${tmpdir}/a.txt" "${tmpdir}/got.txt"
printf 'smoke ok\n'
