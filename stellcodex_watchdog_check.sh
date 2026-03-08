#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
RUNNER="${ROOT_DIR}/stellcodex_247.sh"
OUTPUT_ROOT="${STELLCODEX_OUTPUT_ROOT:-/root/stellcodex_output}"
ERRORS_LOG="${OUTPUT_ROOT}/errors.log"
WATCHDOG_LOG="${OUTPUT_ROOT}/watchdog.log"
STATE_URL="${ORCHESTRA_STATE_URL:-http://localhost:7010/state}"
HEALTH_URL="${ORCHESTRA_HEALTH_URL:-http://localhost:7010/health}"
HEALTH_MAX_TIME="${ORCHESTRA_HEALTH_MAX_TIME:-60}"
STATE_MAX_TIME="${ORCHESTRA_STATE_MAX_TIME:-60}"
RETRY_DELAY_SECONDS="${ORCHESTRA_RETRY_DELAY_SECONDS:-2}"

mkdir -p "${OUTPUT_ROOT}"

log_line() {
  local level="$1"
  local msg="$2"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '[%s] [%s] %s\n' "${ts}" "${level}" "${msg}" >> "${WATCHDOG_LOG}"
  if [[ "${level}" == "ERROR" || "${level}" == "WARN" ]]; then
    printf '[%s] %s\n' "${ts}" "${msg}" >> "${ERRORS_LOG}"
  fi
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif [[ -x "${ROOT_DIR}/docker-compose" ]]; then
    "${ROOT_DIR}/docker-compose" "$@"
  else
    docker-compose "$@"
  fi
}

probe_ok() {
  local url="$1"
  local max_time="$2"
  curl -fsS --max-time "${max_time}" "${url}" >/dev/null 2>&1
}

if ! probe_ok "${HEALTH_URL}" "${HEALTH_MAX_TIME}"; then
  sleep "${RETRY_DELAY_SECONDS}"
fi

if ! probe_ok "${HEALTH_URL}" "${HEALTH_MAX_TIME}"; then
  log_line "WARN" "orchestra_health_unreachable restarting core stack"
  compose_cmd -f "${ROOT_DIR}/docker-compose.yml" up -d orchestrator litellm autopilot stellai >/dev/null 2>&1 || true
fi

if ! "${RUNNER}" status >/dev/null 2>&1; then
  log_line "WARN" "stellcodex_247_runner_not_running starting"
  "${RUNNER}" start >/dev/null 2>&1 || log_line "ERROR" "stellcodex_247_runner_start_failed"
fi

if ! probe_ok "${STATE_URL}" "${STATE_MAX_TIME}"; then
  sleep "${RETRY_DELAY_SECONDS}"
fi

if ! probe_ok "${STATE_URL}" "${STATE_MAX_TIME}"; then
  log_line "ERROR" "orchestra_state_unreachable_after_restart"
else
  log_line "INFO" "watchdog_check_ok"
fi
