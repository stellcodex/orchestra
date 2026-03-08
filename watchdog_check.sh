#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
BASE_URL="${BASE_URL:-http://localhost:7010}"
COMPOSE_FILE="${COMPOSE_FILE:-/root/workspace/ops/orchestra/docker-compose.yml}"

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

running="$(docker ps --filter name=orchestra_orchestrator --filter status=running --format '{{.Names}}' 2>/dev/null | head -1)"
if [[ -z "${running}" ]]; then
  compose_cmd -f "${COMPOSE_FILE}" up -d orchestrator litellm autopilot stellai >/dev/null 2>&1 || true
fi

if [[ -x "${ROOT_DIR}/stellcodex_watchdog_check.sh" ]]; then
  "${ROOT_DIR}/stellcodex_watchdog_check.sh" || true
fi
