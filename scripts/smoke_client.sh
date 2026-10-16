#!/bin/sh
# Runs inside the dev image, on the same Docker network as the API.
set -eu

HOST="${API_HOST:-zip-api}"
PORT="${PORT:-8000}"
base="http://${HOST}:${PORT}"
expected_sha="${EXPECTED_GIT_SHA:-unknown}"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

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

curl -fsS -D "${tmpdir}/manifest.headers" -o "${tmpdir}/manifest.json" \
    "${base}/docs/manifest.json"
grep -i '^content-type: application/json' "${tmpdir}/manifest.headers"
python - "$tmpdir/manifest.json" "$expected_sha" "$tmpdir/paths.txt" <<'PY'
import json
import re
import sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
if doc["service"] != "zip-service" or doc["contract_version"] != "1.1.0":
    raise SystemExit(f"unexpected manifest: {doc}")
if doc["version"] != "1.2.0" or doc["revision"] != expected:
    raise SystemExit(f"revision {doc['revision']!r} != {expected!r}")
if doc.get("openapi") != "/docs/openapi.json":
    raise SystemExit(f"unexpected openapi path: {doc.get('openapi')!r}")
slugs = [item["slug"] for item in doc["documents"]]
if slugs[:2] != ["readme", "agents"]:
    raise SystemExit(f"unexpected documents: {slugs}")
path_re = re.compile(r"^/docs/[A-Za-z0-9._-]{1,64}\.md$")
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    for item in doc["documents"]:
        if path_re.fullmatch(item["path"]) is None:
            raise SystemExit(f"unexpected document path: {item['path']}")
        handle.write(item["path"] + "\n")
PY

while IFS= read -r doc_path; do
    curl -fsS -D "${tmpdir}/doc.headers" -o "${tmpdir}/doc.md" "${base}${doc_path}"
    grep -i '^content-type: text/markdown; charset=utf-8' "${tmpdir}/doc.headers"
    name=${doc_path#/docs/}
    cmp "${tmpdir}/doc.md" "/app/${name}"
done < "${tmpdir}/paths.txt"

curl -fsS -D "${tmpdir}/openapi.headers" -o "${tmpdir}/openapi.json" \
    "${base}/docs/openapi.json"
grep -i '^content-type: application/json' "${tmpdir}/openapi.headers"
python - "$tmpdir/openapi.json" <<'PY'
import json
import sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
if "/api/v1/zip" not in doc.get("paths", {}):
    raise SystemExit("openapi document has no /api/v1/zip")
if "servers" in doc or "security" in doc:
    raise SystemExit("openapi document must not declare servers or security")
if len(json.dumps(doc).encode()) > 1024 * 1024:
    raise SystemExit("openapi document exceeds 1 MiB")
PY

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
