#!/usr/bin/env bash
set -euo pipefail

LITELLM_URL="${LITELLM_URL:-http://localhost:4000/v1/chat/completions}"
SMOKE_TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-900}"

probe_model() {
  local model="$1"
  local prompt="$2"
  local raw

  raw="$(curl -sS --max-time "${SMOKE_TIMEOUT_SECONDS}" -H "Content-Type: application/json" -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"${prompt}\"}],\"max_tokens\":8,\"temperature\":0}" "${LITELLM_URL}")"
  python3 - <<'PY' "${model}" "${raw}"
import json
import sys

model = sys.argv[1]
raw = sys.argv[2]

try:
    payload = json.loads(raw)
except Exception:
    print(f"SMOKE_FAIL model={model} reason=invalid_json")
    raise SystemExit(1)

choices = payload.get("choices")
if not isinstance(choices, list) or not choices:
    print(f"SMOKE_FAIL model={model} reason=missing_choices")
    raise SystemExit(1)

message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
content = message.get("content", "")

if isinstance(content, list):
    text_parts = []
    for item in content:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif isinstance(item.get("content"), str):
                text_parts.append(item["content"])
    content = "\n".join(text_parts)

if not str(content).strip():
    print(f"SMOKE_FAIL model={model} reason=empty_content")
    raise SystemExit(1)

print(f"SMOKE_OK model={model}")
PY
}

probe_model "local_fast" "Reply with LOCAL_FAST_OK only."
probe_model "local_reason" "Reply with LOCAL_REASON_OK only."
