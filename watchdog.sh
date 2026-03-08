#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
BASE_URL="${BASE_URL:-http://localhost:7010}"
COMPOSE_FILE="${COMPOSE_FILE:-/root/workspace/ops/orchestra/docker-compose.yml}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-60}"

while true; do
  running="$(docker ps --filter name=orchestra_orchestrator --filter status=running --format '{{.Names}}' 2>/dev/null | head -1)"
  if [[ -z "${running}" ]]; then
    docker-compose -f "${COMPOSE_FILE}" up -d orchestrator litellm autopilot >/dev/null 2>&1 || true
  fi
  sleep "${CHECK_INTERVAL_SECONDS}"
done
