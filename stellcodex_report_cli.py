#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

OUTPUT_ROOT = Path("/root/stellcodex_output")
REPORT_PATH = OUTPUT_ROOT / "REPORT.md"
TEST_PATH = OUTPUT_ROOT / "test_results.json"
EVIDENCE_ROOT = OUTPUT_ROOT / "evidence"


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def show_module(slug: str) -> None:
    module_dir = EVIDENCE_ROOT / f"APP_{slug}"
    artifact = module_dir / "artifact.json"
    log = module_dir / "module.log"
    if not artifact.exists():
        print(f"Modul bulunamadi: {slug}")
        return
    print(f"[artifact] {artifact}")
    print(read_text(artifact)[:2000])
    if log.exists():
        print(f"[log] {log}")
        print(read_text(log)[:1200])


def main() -> int:
    print("Stellcodex raporu hazir, gormek istediginiz modul veya cikti var mi?")
    print("Komutlar: report | test | module:<slug> | exit")
    while True:
        raw = input("> ").strip()
        if raw in {"exit", "quit"}:
            return 0
        if raw == "report":
            print(read_text(REPORT_PATH) or "REPORT.md bulunamadi.")
            continue
        if raw == "test":
            payload = read_json(TEST_PATH)
            print(json.dumps(payload, indent=2, ensure_ascii=True) if payload else "test_results.json bulunamadi.")
            continue
        if raw.startswith("module:"):
            show_module(raw.split(":", 1)[1].strip())
            continue
        print("Gecersiz komut.")


if __name__ == "__main__":
    raise SystemExit(main())
