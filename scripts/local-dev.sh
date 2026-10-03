#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

NETWORK=knowledge-bot-dev
OLLAMA_CONTAINER=knowledge-bot-ollama
APP_CONTAINER=knowledge-bot-local
DATA_VOLUME=knowledge-bot-data
OLLAMA_VOLUME=knowledge-bot-ollama-data
OLLAMA_IMAGE=ollama/ollama:0.35.1
APP_IMAGE=knowledge-bot:local

wait_for_ollama() {
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Ollama did not become ready" >&2
  return 1
}

ensure_network() {
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
  docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 || docker volume create "$DATA_VOLUME" >/dev/null
  docker volume inspect "$OLLAMA_VOLUME" >/dev/null 2>&1 || docker volume create "$OLLAMA_VOLUME" >/dev/null
}

start_ollama() {
  ensure_network
  if ! docker container inspect "$OLLAMA_CONTAINER" >/dev/null 2>&1; then
    docker run -d --name "$OLLAMA_CONTAINER" --network "$NETWORK" \
      -p 11434:11434 -v "$OLLAMA_VOLUME:/root/.ollama" "$OLLAMA_IMAGE" >/dev/null
  elif [ "$(docker inspect -f '{{.State.Running}}' "$OLLAMA_CONTAINER")" != true ]; then
    docker start "$OLLAMA_CONTAINER" >/dev/null
  fi
  wait_for_ollama
}

# The System-One decision service is the local Ollama container itself. This
# script never downloads or trains a model at dev-up time: it checks that the
# configured service answers, and otherwise stops with the setup instruction.
check_decision_service() {
  local backend base_url probe model
  backend=$(sed -n 's/^DECISION_BACKEND=//p' .env.local 2>/dev/null | tail -1)
  [ "$backend" = "systemone" ] || return 0
  base_url=$(sed -n 's/^DECISION_BASE_URL=//p' .env.local 2>/dev/null | tail -1)
  base_url=${base_url:-http://$OLLAMA_CONTAINER:11434}
  model=$(sed -n 's/^DECISION_MODEL=//p' .env.local 2>/dev/null | tail -1)
  model=${model:-tev1:0.8b}
  # The app calls the service by its Docker-network name, which does not
  # resolve on the host: probe the same host:port it publishes instead.
  probe=${base_url/$OLLAMA_CONTAINER/127.0.0.1}
  if curl -fsS "${probe%/}/health" >/dev/null 2>&1 ||
     curl -fsS -X POST "${probe%/}/v1/systemone" \
       -H 'content-type: application/json' \
       -d "{\"model\":\"$model\",\"state\":{\"current_message\":\"ping\",\"candidate_questions\":[]},\"questions\":{\"probe\":{\"type\":\"noul\",\"instructions\":\"Is this text empty?\",\"criteria\":{\"true\":\"It is empty.\",\"false\":\"It has content.\"}}}}" \
       >/dev/null 2>&1; then
    echo "decision service reachable at ${base_url}"
    return 0
  fi
  cat >&2 <<'MSG'
The decision service is not answering at DECISION_BASE_URL.
Run `make dev-bootstrap` once to fetch the decision model, then `make dev-up`
again. This script never downloads or trains a model; see docs/operations.md
for the runtime check.
MSG
  return 1
}

rebuild_app() {
  ensure_network
  docker build -f Dockerfile.local -t "$APP_IMAGE" .
  docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
  "$0" up
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "local app did not become ready" >&2
  return 1
}

e2e_local() {
  rebuild_app
  docker exec "$APP_CONTAINER" env RUN_LOCAL_AI_E2E=1 .venv/bin/pytest -m smoke
}

bootstrap() {
  start_ollama
  docker exec "$OLLAMA_CONTAINER" ollama list | grep -q 'embeddinggemma' || docker exec "$OLLAMA_CONTAINER" ollama pull embeddinggemma
  docker exec "$OLLAMA_CONTAINER" ollama list | grep -q 'gemma3:270m' || docker exec "$OLLAMA_CONTAINER" ollama pull gemma3:270m
  # Decision model for the System-One route (/v1/systemone, Ollama >= 0.35).
  docker exec "$OLLAMA_CONTAINER" ollama list | grep -q 'tev1' || docker exec "$OLLAMA_CONTAINER" ollama pull tev1:0.8b
  docker build -f Dockerfile.local -t "$APP_IMAGE" .
  "$0" migrate
  docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
  "$0" up
}

migrate() {
  docker run --rm --network "$NETWORK" --env-file .env.local \
    -v "$DATA_VOLUME:/data" "$APP_IMAGE" \
    .venv/bin/python -m knowledge_bot.infrastructure.local.migrate
}

up() {
  start_ollama
  check_decision_service
  "$0" migrate
  if ! docker container inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    docker run -d --name "$APP_CONTAINER" --network "$NETWORK" \
      --env-file .env.local -v "$DATA_VOLUME:/data" -p 8000:8000 \
      "$APP_IMAGE" >/dev/null
  elif [ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER")" != true ]; then
    docker start "$APP_CONTAINER" >/dev/null
  fi
}

down() {
  docker rm -f "$APP_CONTAINER" "$OLLAMA_CONTAINER" >/dev/null 2>&1 || true
}

logs() {
  docker logs -f "$APP_CONTAINER"
}

shell() {
  docker exec -it "$APP_CONTAINER" /bin/bash
}

reset() {
  if [ "${CONFIRM:-}" != 1 ]; then
    echo "dev-reset is destructive; rerun with CONFIRM=1" >&2
    exit 2
  fi
  down
  docker volume rm "$DATA_VOLUME" >/dev/null
  docker volume create "$DATA_VOLUME" >/dev/null
}

seed() {
  # The CLI requires an explicit base URL, and a local one is not a live
  # remote call, so seeding the local stack needs no authorization.
  docker exec "$APP_CONTAINER" .venv/bin/kb seed \
    --base-url http://127.0.0.1:8000 --qa data/seed/bot_self_qa.json
}

case "${1:-}" in
  bootstrap) bootstrap ;;
  rebuild-app) rebuild_app ;;
  e2e-local) e2e_local ;;
  up) up ;;
  down) down ;;
  logs) logs ;;
  shell) shell ;;
  reset) reset ;;
  migrate) migrate ;;
  seed) seed ;;
  *) echo "usage: $0 {bootstrap|rebuild-app|e2e-local|up|down|logs|shell|reset|migrate|seed}" >&2; exit 2 ;;
esac
