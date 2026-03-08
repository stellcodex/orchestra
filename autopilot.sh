#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
ACTION="${1:-start}"
JOBS_ROOT="${2:-/root/workspace/_jobs}"
LOG_DIR="${JOBS_ROOT}/logs"
PID_FILE="${LOG_DIR}/watchdog.pid"
BASE_URL="${BASE_URL:-http://localhost:7010}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"

ensure_jobs() {
  mkdir -p \
    "${JOBS_ROOT}/inbox" \
    "${JOBS_ROOT}/done" \
    "${JOBS_ROOT}/failed" \
    "${JOBS_ROOT}/deferred" \
    "${JOBS_ROOT}/output" \
    "${JOBS_ROOT}/logs" \
    "${JOBS_ROOT}/backups"
  touch "${JOBS_ROOT}/logs/events.jsonl"
}

wait_state() {
  local tries="${1:-60}"
  local sleep_s="${2:-2}"
  local i
  for ((i=1; i<=tries; i++)); do
    if curl -fsS --max-time 6 "${BASE_URL}/state" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${sleep_s}"
  done
  return 1
}

start_watchdog() {
  mkdir -p "${LOG_DIR}"
  if [[ -f "${PID_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      return 0
    fi
  fi
  nohup "${ROOT_DIR}/watchdog.sh" >"${LOG_DIR}/watchdog.log" 2>&1 &
  echo "$!" >"${PID_FILE}"
}

stop_watchdog() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${PID_FILE}"
  fi
}

case "${ACTION}" in
  start)
    ensure_jobs
    "${ROOT_DIR}/preflight.sh" "${JOBS_ROOT}" >/tmp/orchestra_autopilot_preflight.log 2>&1
    docker-compose -f "${COMPOSE_FILE}" up -d --build ollama litellm orchestrator autopilot stellai >/tmp/orchestra_autopilot_up.log 2>&1
    wait_state 90 2 || true
    start_watchdog
    echo "AUTOPILOT_STARTED jobs_root=${JOBS_ROOT}"
    ;;
  stop)
    stop_watchdog
    docker-compose -f "${COMPOSE_FILE}" stop autopilot >/tmp/orchestra_autopilot_stop.log 2>&1 || true
    echo "AUTOPILOT_STOPPED"
    ;;
  status)
    docker-compose -f "${COMPOSE_FILE}" ps
    if [[ -f "${PID_FILE}" ]]; then
      echo "watchdog_pid=$(cat "${PID_FILE}")"
    else
      echo "watchdog_pid=none"
    fi
    ;;
  tail)
    docker-compose -f "${COMPOSE_FILE}" logs -f --tail=200 autopilot orchestrator litellm
    ;;
  *)
    echo "Usage: ./autopilot.sh {start|status|stop|tail} [jobs_root]"
    exit 1
    ;;
esac
