#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
JOB_ROOT="${JOB_ROOT:-/root/workspace/_jobs}"
LOG_DIR="${JOB_ROOT}/logs"
EVENTS_PATH="${LOG_DIR}/events.jsonl"
BASE_URL="${BASE_URL:-http://localhost:7010}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"

mkdir -p \
  "${JOB_ROOT}/inbox" \
  "${JOB_ROOT}/done" \
  "${JOB_ROOT}/failed" \
  "${JOB_ROOT}/deferred" \
  "${JOB_ROOT}/output" \
  "${JOB_ROOT}/logs" \
  "${JOB_ROOT}/backups"
touch "${EVENTS_PATH}"

log_event() {
  local event="$1"
  local status="${2:-ok}"
  local detail="${3:-}"
  python3 - <<'PY' "${EVENTS_PATH}" "${event}" "${status}" "${detail}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "event": sys.argv[2],
    "status": sys.argv[3],
}
if sys.argv[4]:
    payload["detail"] = sys.argv[4]
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
PY
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

read_state_readiness() {
  local state_json="$1"
  python3 - <<'PY' "${state_json}"
import json
import sys

raw = sys.argv[1]
try:
    payload = json.loads(raw)
except Exception:
    print("FAIL")
    raise SystemExit(0)

value = str(payload.get("readiness", "FAIL")).strip()
print(value if value else "FAIL")
PY
}

log_event "run_started" "in_progress"

if ! "${ROOT_DIR}/preflight.sh" "${JOB_ROOT}" >/tmp/orchestra_preflight_run.log 2>&1; then
  log_event "run_failed" "failed" "preflight_failed"
  echo "DEGRADED (preflight_failed)"
  exit 1
fi

if ! docker-compose -f "${COMPOSE_FILE}" up -d --build >/tmp/orchestra_compose_up.log 2>&1; then
  log_event "run_failed" "failed" "compose_up_failed"
  echo "DEGRADED (compose_up_failed)"
  exit 1
fi

if ! wait_state 90 2; then
  log_event "run_failed" "failed" "orchestrator_unreachable"
  echo "DEGRADED (orchestrator_unreachable)"
  exit 1
fi

# Ensure previous manual cooldown state does not block local routing.
for model in local_fast local_reason gemini_conductor codex_executor claude_reviewer abacus_analyst; do
  curl -sS --max-time 10 -X POST "${BASE_URL}/quota/reset" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${model}\",\"cooldown_minutes\":120}" >/dev/null || true
done

local_smoke_reason=""
if ! "${ROOT_DIR}/scripts/smoke_local.sh" >/tmp/orchestra_local_smoke.log 2>&1; then
  local_smoke_reason="local_smoke_failed"
fi

state_json="$(curl -sS --max-time 10 "${BASE_URL}/state" || echo '{}')"
readiness="$(read_state_readiness "${state_json}")"

if [[ -n "${local_smoke_reason}" ]]; then
  log_event "run_completed" "degraded" "${local_smoke_reason}"
  echo "DEGRADED (${local_smoke_reason})"
  exit 1
fi

if [[ "${readiness}" == "READY" || "${readiness}" == "READY_LOCAL" ]]; then
  log_event "run_completed" "ok" "${readiness}"
  echo "${readiness}"
  exit 0
fi

log_event "run_completed" "degraded" "readiness_${readiness}"
echo "DEGRADED (readiness_${readiness})"
exit 1
