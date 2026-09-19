#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Runtime smoke test: build the dev image, run the Worker, assert /healthz.
# Slow (~2-4 min). Run at milestone boundaries, not on every change.
set -euo pipefail

IMAGE="knowledge-bot:dev"
CONTAINER="knowledge-bot-smoke"
PORT="8787"

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t "${IMAGE}" .
cleanup
docker run -d --name "${CONTAINER}" -p "${PORT}:8787" "${IMAGE}" >/dev/null

for _ in $(seq 1 60); do
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/healthz" || true)"
  if [ "${status}" = "200" ]; then
    echo "smoke: /healthz OK"
    exit 0
  fi
  sleep 3
done

echo "smoke: /healthz did not return 200 in time" >&2
docker logs "${CONTAINER}" 2>&1 | tail -20 >&2
exit 1
