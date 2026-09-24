#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

NETWORK=knowledge-bot-dev
OLLAMA_CONTAINER=knowledge-bot-ollama
APP_CONTAINER=knowledge-bot-local
DATA_VOLUME=knowledge-bot-data
OLLAMA_VOLUME=knowledge-bot-ollama-data
OLLAMA_IMAGE=ollama/ollama:0.11.10
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

bootstrap() {
  start_ollama
  docker exec "$OLLAMA_CONTAINER" ollama list | grep -q 'embeddinggemma' || docker exec "$OLLAMA_CONTAINER" ollama pull embeddinggemma
  docker exec "$OLLAMA_CONTAINER" ollama list | grep -q 'gemma3:270m' || docker exec "$OLLAMA_CONTAINER" ollama pull gemma3:270m
  docker build -f Dockerfile.local -t "$APP_IMAGE" .
  "$0" migrate
  "$0" up
}

migrate() {
  docker run --rm --network "$NETWORK" --env-file .env.local \
    -v "$DATA_VOLUME:/data" "$APP_IMAGE" \
    .venv/bin/python -m knowledge_bot.infrastructure.local.migrate
}

up() {
  start_ollama
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
  docker exec "$APP_CONTAINER" .venv/bin/kb seed --qa data/seed/bot_self_qa.json
}

case "${1:-}" in
  bootstrap) bootstrap ;;
  up) up ;;
  down) down ;;
  logs) logs ;;
  shell) shell ;;
  reset) reset ;;
  migrate) migrate ;;
  seed) seed ;;
  *) echo "usage: $0 {bootstrap|up|down|logs|shell|reset|migrate|seed}" >&2; exit 2 ;;
esac
