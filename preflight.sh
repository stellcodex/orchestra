#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="${ROOT_DIR}:${PATH}"
JOBS_ROOT="${1:-/root/workspace/_jobs}"
LOG_DIR="${JOBS_ROOT}/logs"
REPORT_PATH="${LOG_DIR}/PREFLIGHT_REPORT.md"
SNAPSHOT_PATH="${LOG_DIR}/PREFLIGHT_SNAPSHOT.json"
EVENTS_PATH="${LOG_DIR}/events.jsonl"
BASE_URL="${BASE_URL:-http://localhost:7010}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p \
  "${JOBS_ROOT}/inbox" \
  "${JOBS_ROOT}/done" \
  "${JOBS_ROOT}/failed" \
  "${JOBS_ROOT}/deferred" \
  "${JOBS_ROOT}/output" \
  "${JOBS_ROOT}/logs" \
  "${JOBS_ROOT}/backups"

touch "${EVENTS_PATH}"

log_event() {
  local event="$1"
  local status="$2"
  local detail="${3:-}"
  python3 - <<'PY' "${EVENTS_PATH}" "${event}" "${status}" "${detail}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
event = sys.argv[2]
status = sys.argv[3]
detail = sys.argv[4]
payload = {
    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "event": event,
    "status": status,
}
if detail:
    payload["detail"] = detail
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
PY
}

capture() {
  local name="$1"
  shift
  "$@" >"${LOG_DIR}/preflight_${name}.txt" 2>&1 || true
}

log_event "preflight_started" "in_progress"

capture "uname" uname -a
capture "lsb_release" lsb_release -a
capture "lscpu" lscpu
capture "free" free -h
capture "df" df -h
capture "uptime" uptime
capture "docker_info" docker info
capture "docker_ps" docker ps
capture "compose_ps" docker-compose -f "${COMPOSE_FILE}" ps
capture "dns" getent hosts example.com
capture "curl_example" curl -I https://example.com

workspace_ok="false"
orchestra_ok="false"
[[ -d /root/workspace ]] && workspace_ok="true"
[[ -d /root/workspace/ops/orchestra ]] && orchestra_ok="true"

if command -v rclone >/dev/null 2>&1; then
  capture "rclone_lsd" rclone lsd gdrive:
else
  printf "rclone not installed\n" >"${LOG_DIR}/preflight_rclone_lsd.txt"
fi

state_before="unreachable"
# Use a lightweight TCP check + docker ps to avoid triggering slow LLM probes
_orch_container="$(docker ps --filter name=orchestra_orchestrator --filter status=running --format '{{.Names}}' 2>/dev/null | head -1)"
if [[ -n "${_orch_container}" ]]; then
  state_before="reachable"
  printf '{"ok":true,"source":"docker_ps_check","container":"%s"}\n' "${_orch_container}" >"${LOG_DIR}/preflight_orchestra_state.txt"
else
  printf '{"ok":false,"source":"docker_ps_check","error":"container_not_running"}\n' >"${LOG_DIR}/preflight_orchestra_state.txt"
fi

drive_backup_status="READY"
if ! command -v rclone >/dev/null 2>&1; then
  drive_backup_status="DRIVE_BACKUP_NOT_READY:rclone_missing"
elif ! rclone lsd gdrive: >"${LOG_DIR}/preflight_rclone_lsd.txt" 2>&1; then
  drive_backup_status="DRIVE_BACKUP_NOT_READY:gdrive_unreachable"
fi

disk_free_kb="$(df -Pk /root/workspace | awk 'NR==2 {print $4}')"
disk_gate="pass"
if [[ -z "${disk_free_kb}" ]] || [[ "${disk_free_kb}" -lt $((5 * 1024 * 1024)) ]]; then
  disk_gate="fail"
fi

docker_gate="pass"
if ! docker info >/dev/null 2>&1; then
  docker_gate="fail"
fi

state_gate="pass"
state_after="${state_before}"
if [[ "${state_before}" != "reachable" ]]; then
  # Container not running — start the stack
  docker-compose -f "${COMPOSE_FILE}" up -d orchestrator litellm autopilot >/dev/null 2>&1 || true
  sleep 15
  _orch_container_after="$(docker ps --filter name=orchestra_orchestrator --filter status=running --format '{{.Names}}' 2>/dev/null | head -1)"
  if [[ -n "${_orch_container_after}" ]]; then
    state_after="reachable_after_restart"
    printf '{"ok":true,"source":"docker_ps_check","container":"%s"}\n' "${_orch_container_after}" >"${LOG_DIR}/preflight_orchestra_state_after_restart.txt"
  else
    state_gate="fail"
    state_after="still_unreachable"
    printf '{"ok":false,"source":"docker_ps_check","error":"container_still_not_running"}\n' >"${LOG_DIR}/preflight_orchestra_state_after_restart.txt"
  fi
fi

status="PASS"
fail_reason=""
if [[ "${disk_gate}" == "fail" ]]; then
  status="FAIL"
  fail_reason="disk_free_below_5gb"
elif [[ "${docker_gate}" == "fail" ]]; then
  status="FAIL"
  fail_reason="docker_daemon_unreachable"
elif [[ "${state_gate}" == "fail" ]]; then
  status="FAIL"
  fail_reason="orchestrator_state_unreachable"
fi

{
  echo "# PREFLIGHT REPORT"
  echo
  echo "- generated_at: ${TS}"
  echo "- status: ${status}"
  echo "- drive_backup: ${drive_backup_status}"
  if [[ -n "${fail_reason}" ]]; then
    echo "- fail_reason: ${fail_reason}"
  fi
  echo
  echo "## Gates"
  echo "- disk_free_ge_5gb: ${disk_gate}"
  echo "- docker_daemon: ${docker_gate}"
  echo "- orchestrator_state: ${state_gate} (${state_after})"
  echo
  echo "## Workspace Checks"
  echo "- /root/workspace exists: ${workspace_ok}"
  echo "- /root/workspace/ops/orchestra exists: ${orchestra_ok}"
  echo

  for name in uname lsb_release lscpu free df uptime docker_info docker_ps compose_ps dns curl_example rclone_lsd orchestra_state orchestra_state_after_restart; do
    file="${LOG_DIR}/preflight_${name}.txt"
    [[ -f "${file}" ]] || continue
    echo "## ${name}"
    echo '```'
    sed -n '1,140p' "${file}"
    echo '```'
    echo
  done
} >"${REPORT_PATH}"

python3 - <<'PY' "${LOG_DIR}" "${SNAPSHOT_PATH}" "${TS}" "${status}" "${fail_reason}" "${drive_backup_status}" "${disk_free_kb}" "${workspace_ok}" "${orchestra_ok}" "${state_before}" "${state_after}" "${disk_gate}" "${docker_gate}" "${state_gate}"
import json
import re
import sys
from pathlib import Path

log_dir = Path(sys.argv[1])
snapshot_path = Path(sys.argv[2])
ts = sys.argv[3]
status = sys.argv[4]
fail_reason = sys.argv[5]
drive_backup_status = sys.argv[6]
disk_free_kb = int(sys.argv[7]) if sys.argv[7].isdigit() else -1
workspace_ok = sys.argv[8] == "true"
orchestra_ok = sys.argv[9] == "true"
state_before = sys.argv[10]
state_after = sys.argv[11]
disk_gate = sys.argv[12]
docker_gate = sys.argv[13]
state_gate = sys.argv[14]

def read(name: str) -> str:
    path = log_dir / f"preflight_{name}.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()

def extract_driver(text: str, key: str) -> str:
    pat = re.compile(rf"^{re.escape(key)}:\\s*(.+)$", re.MULTILINE)
    match = pat.search(text)
    return match.group(1).strip() if match else ""

docker_info = read("docker_info")
payload = {
    "generated_at": ts,
    "status": status,
    "fail_reason": fail_reason or None,
    "drive_backup_status": drive_backup_status,
    "gates": {
        "disk_free_ge_5gb": disk_gate == "pass",
        "docker_daemon": docker_gate == "pass",
        "orchestrator_state": state_gate == "pass",
    },
    "checks": {
        "workspace_exists": workspace_ok,
        "orchestra_exists": orchestra_ok,
        "state_before": state_before,
        "state_after": state_after,
        "disk_free_kb": disk_free_kb,
    },
    "system": {
        "uname": read("uname"),
        "lsb_release": read("lsb_release"),
        "lscpu": read("lscpu"),
        "free_h": read("free"),
        "df_h": read("df"),
        "uptime": read("uptime"),
    },
    "docker": {
        "storage_driver": extract_driver(docker_info, "Storage Driver"),
        "logging_driver": extract_driver(docker_info, "Logging Driver"),
        "info": docker_info,
        "ps": read("docker_ps"),
        "compose_ps": read("compose_ps"),
    },
    "network": {
        "dns": read("dns"),
        "curl_example": read("curl_example"),
    },
    "service": {
        "orchestra_state": read("orchestra_state"),
        "orchestra_state_after_restart": read("orchestra_state_after_restart"),
    },
    "backup": {
        "rclone_lsd_gdrive": read("rclone_lsd"),
    },
}

snapshot_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
PY

if [[ "${status}" == "FAIL" ]]; then
  log_event "preflight_failed" "failed" "${fail_reason}"
  echo "PREFLIGHT_FAIL ${fail_reason}"
  exit 1
fi

log_event "preflight_passed" "ok"
echo "PREFLIGHT_OK"
