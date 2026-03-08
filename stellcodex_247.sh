#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORCH_SCRIPT="${ROOT_DIR}/stellcodex_247_orchestrator.py"
OUTPUT_ROOT="${STELLCODEX_OUTPUT_ROOT:-/root/stellcodex_output}"
PID_FILE="${OUTPUT_ROOT}/stellcodex_247.pid"
LOG_FILE="${OUTPUT_ROOT}/orchestrator.log"
ACTION="${1:-status}"

mkdir -p "${OUTPUT_ROOT}"

read_pid_file() {
  cat "${PID_FILE}" 2>/dev/null || true
}

find_loop_pid() {
  ps -eo pid=,comm=,args= | awk -v script="${ORCH_SCRIPT}" '
    $2 == "bash" && index($0, " -lc ") > 0 && index($0, "while true; do python3") > 0 && index($0, script) > 0 { print $1; exit }
  '
}

is_running() {
  local pid
  pid="$(read_pid_file)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi

  pid="$(find_loop_pid || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    echo "${pid}" > "${PID_FILE}"
    return 0
  fi
  rm -f "${PID_FILE}"
  return 1
}

case "${ACTION}" in
  start)
    if is_running; then
      echo "already_running pid=$(read_pid_file)"
      exit 0
    fi
    LOOP_INTERVAL="${STELLCODEX_LOOP_INTERVAL_SECONDS:-3600}"
    nohup bash -lc "set +e; while true; do python3 \"${ORCH_SCRIPT}\"; sleep \"${LOOP_INTERVAL}\"; done" >>"${LOG_FILE}" 2>&1 &
    echo "$!" > "${PID_FILE}"
    echo "started pid=$(read_pid_file)"
    ;;
  stop)
    if is_running; then
      kill "$(read_pid_file)" >/dev/null 2>&1 || true
      rm -f "${PID_FILE}"
      echo "stopped"
    else
      rm -f "${PID_FILE}"
      echo "not_running"
    fi
    ;;
  status)
    if is_running; then
      echo "running pid=$(read_pid_file)"
    else
      echo "stopped"
      exit 1
    fi
    ;;
  run-once)
    exec python3 "${ORCH_SCRIPT}"
    ;;
  tail)
    exec tail -n "${TAIL_LINES:-200}" -f "${LOG_FILE}"
    ;;
  *)
    echo "Usage: $0 {start|stop|status|run-once|tail}"
    exit 1
    ;;
esac
