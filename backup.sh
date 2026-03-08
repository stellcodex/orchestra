#!/usr/bin/env bash
set -euo pipefail

JOBS_ROOT="${1:-/root/workspace/_jobs}"
BACKUPS_DIR="${JOBS_ROOT}/backups"
LOG_DIR="${JOBS_ROOT}/logs"
TS="$(date -u +%Y%m%d_%H%M%S)"
DATE_KEY="$(date -u +%Y%m%d)"
TARGET="${BACKUPS_DIR}/${TS}"

mkdir -p "${TARGET}" "${LOG_DIR}"

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -e "${src}" ]]; then
    mkdir -p "${dst}"
    cp -r "${src}" "${dst}/"
  fi
}

copy_if_exists "/root/workspace/ops/orchestra" "${TARGET}"
copy_if_exists "/root/workspace/ops/autopilot" "${TARGET}"
copy_if_exists "/root/workspace/ops/stellai" "${TARGET}"
copy_if_exists "/root/workspace/ops/systemd" "${TARGET}"
copy_if_exists "/root/workspace/_jobs/logs" "${TARGET}"
copy_if_exists "/root/workspace/_jobs/output" "${TARGET}"

if [[ -f /root/workspace/ops/orchestra/docker-compose.yml ]]; then
  cp /root/workspace/ops/orchestra/docker-compose.yml "${TARGET}/docker-compose.yml"
fi
if [[ -f /root/workspace/ops/orchestra/.env ]]; then
  cp /root/workspace/ops/orchestra/.env "${TARGET}/.env"
fi

find "${TARGET}" -type f | sort >"${TARGET}/manifest_files.txt"

DRIVE_LOG="${LOG_DIR}/backup_drive_${TS}.log"
if command -v rclone >/dev/null 2>&1; then
  if rclone lsd gdrive: >"${DRIVE_LOG}" 2>&1; then
    REMOTE_PATH="gdrive:stellcodex-genois/01_inbox/autopilot_backups/${DATE_KEY}/${TS}"
    if rclone copy "${TARGET}" "${REMOTE_PATH}" >>"${DRIVE_LOG}" 2>&1; then
      echo "drive_sync=ok ${REMOTE_PATH}" >>"${DRIVE_LOG}"
    else
      echo "drive_sync=failed" >>"${DRIVE_LOG}"
    fi
  else
    echo "drive_sync=skipped gdrive_unavailable" >>"${DRIVE_LOG}"
  fi
else
  echo "drive_sync=skipped rclone_missing" >"${DRIVE_LOG}"
fi

echo "BACKUP_READY ${TARGET}"
