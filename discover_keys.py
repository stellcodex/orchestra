#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from pathlib import Path

WORKSPACE_ROOT = Path("/root/workspace")
ORCHESTRA_DIR = WORKSPACE_ROOT / "ops" / "orchestra"
ENV_PATH = ORCHESTRA_DIR / ".env"

TARGETS = {
    "OPENAI_API_KEY": ["OPENAI_API_KEY"],
    "ANTHROPIC_API_KEY": ["ANTHROPIC_API_KEY"],
    "GEMINI_API_KEY": ["GEMINI_API_KEY"],
    "ABACUSAI_API_KEY": ["ABACUSAI_API_KEY", "ABACUS_API_KEY"],
}


def normalize(value: str) -> str:
    out = value.strip().strip('"').strip("'")
    if "#" in out and not out.startswith("${"):
        out = out.split("#", 1)[0].strip()
    return out.strip()


def is_placeholder(value: str) -> bool:
    lower = normalize(value).lower()
    if not lower:
        return True
    if lower in {"dummy", "none", "null", "changeme", "your_key_here"}:
        return True
    if lower.startswith("${") and lower.endswith("}"):
        return True
    return False


def parse_key_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pat = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+?)\s*$")
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m = pat.match(line)
        if not m:
            continue
        k = m.group(1).strip().upper()
        v = normalize(m.group(2))
        if v and not is_placeholder(v):
            out[k] = v
    return out


def is_candidate(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if "docker-compose" in name:
        return True
    if "litellm.config" in name:
        return True
    if "secret" in name:
        return True
    if "config" in name and path.suffix.lower() in {".yaml", ".yml", ".env", ".ini", ".toml", ".conf"}:
        return True
    return False


def read_existing_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_key_values(path.read_text(encoding="utf-8", errors="ignore"))


def discover_keys() -> dict[str, str]:
    found: dict[str, str] = {}

    # 1) Process environment first.
    for target, aliases in TARGETS.items():
        for alias in aliases:
            value = normalize(os.getenv(alias, ""))
            if value and not is_placeholder(value):
                found[target] = value
                break

    # 2) Existing orchestra .env (if any).
    existing = read_existing_env(ENV_PATH)
    for target, aliases in TARGETS.items():
        if target in found:
            continue
        for alias in aliases:
            value = existing.get(alias.upper(), "")
            if value and not is_placeholder(value):
                found[target] = value
                break

    # 3) Workspace scan.
    ignored_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".next",
        "dist",
        "build",
        "target",
    }
    scanned = 0
    max_files = 8000

    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            if scanned >= max_files:
                break
            path = Path(root) / filename
            if not is_candidate(path):
                continue
            scanned += 1
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            pairs = parse_key_values(text)
            for target, aliases in TARGETS.items():
                if target in found:
                    continue
                for alias in aliases:
                    v = pairs.get(alias.upper(), "")
                    if v and not is_placeholder(v):
                        found[target] = v
                        break
        if scanned >= max_files:
            break
    return found


def write_env(found: dict[str, str]) -> None:
    existing = read_existing_env(ENV_PATH)

    def val(key: str, default: str = "") -> str:
        if key in found:
            return found[key]
        if key in existing and not is_placeholder(existing[key]):
            return existing[key]
        return default

    lines = [
        f"OPENAI_API_KEY={val('OPENAI_API_KEY')}",
        f"ANTHROPIC_API_KEY={val('ANTHROPIC_API_KEY')}",
        f"GEMINI_API_KEY={val('GEMINI_API_KEY')}",
        f"ABACUSAI_API_KEY={val('ABACUSAI_API_KEY')}",
        "",
        "# Optional local:",
        "ENABLE_OLLAMA=1",
        f"OLLAMA_BASE_URL={val('OLLAMA_BASE_URL', 'http://ollama:11434')}",
        f"OLLAMA_IMAGE={normalize(os.getenv('OLLAMA_IMAGE', '')) or 'ollama/ollama:latest'}",
        "",
        "# Orchestrator:",
        f"LLM_BASE_URL={val('LLM_BASE_URL', 'http://litellm:4000/v1')}",
        f"LLM_API_KEY={val('LLM_API_KEY', 'dummy')}",
        f"HTTP_TIMEOUT_SECONDS={val('HTTP_TIMEOUT_SECONDS', '120')}",
        f"LLM_MAX_TOKENS={val('LLM_MAX_TOKENS', '700')}",
        f"LOCAL_MAX_TOKENS={val('LOCAL_MAX_TOKENS', '120')}",
        f"PAID_CALL_TIMEOUT_SECONDS={val('PAID_CALL_TIMEOUT_SECONDS', '45')}",
        f"LOCAL_CALL_TIMEOUT_SECONDS={val('LOCAL_CALL_TIMEOUT_SECONDS', '20')}",
        f"SCHEDULER_INTERVAL_SECONDS={val('SCHEDULER_INTERVAL_SECONDS', '60')}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    found = discover_keys()
    write_env(found)

    print("[auto-discovery] key status")
    for key in ["GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ABACUSAI_API_KEY"]:
        print(f"[auto-discovery] {key}: {'found' if key in found else 'missing'}")


if __name__ == "__main__":
    main()
