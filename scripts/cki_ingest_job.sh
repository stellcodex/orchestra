#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUT_GLOB="${1:-${ROOT_DIR}/state/drive_exports/*.jsonl}"
OUTPUT_PATH="${2:-${ROOT_DIR}/state/cki_index.json}"

python3 - <<'PY' "$INPUT_GLOB" "$OUTPUT_PATH"
from glob import glob
import sys
from orchestrator.ingestion.cki_ingest import ingest_drive_exports

paths = sorted(glob(sys.argv[1]))
count = ingest_drive_exports(paths, sys.argv[2])
print(f"cki_records_written={count}")
PY
