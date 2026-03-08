#!/usr/bin/env bash
set -euo pipefail

ORCHESTRA_URL="${ORCHESTRA_URL:-http://localhost:7010}"

if [[ "${1:-}" == "" ]]; then
  echo "Usage: ./orchestra.sh \"TASK...\""
  exit 1
fi

TASK_INPUT="$*"
PAYLOAD="$(python3 - <<'PY' "${TASK_INPUT}"
import json
import sys

raw = sys.argv[1]
speed = "eco"
task = raw
lines = raw.splitlines()
if lines:
    first = lines[0].strip()
    if first.upper().startswith("SPEED="):
        value = first.split("=", 1)[1].strip().lower()
        if value in {"eco", "max"}:
            speed = value
        task = "\n".join(lines[1:]).strip()

if not task:
    task = raw.strip()

print(json.dumps({"task": task, "speed": speed}, ensure_ascii=True))
PY
)"

curl -sS -X POST "${ORCHESTRA_URL}/orchestrate" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}"
echo
