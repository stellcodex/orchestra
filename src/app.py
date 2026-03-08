from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from profiler import run_profile
from quota import DEFAULT_COOLDOWN_MINUTES, QuotaManager, parse_iso, to_iso, utcnow
from router import ROLE_DEFAULT_MODEL, choose_model
from scorer import default_model_profile

APP_NAME = "StellCodex Orchestra Orchestrator"
STATE_DIR = Path(os.getenv("STATE_DIR", "/app/state"))
MODEL_PROFILES_PATH = STATE_DIR / "model_profiles.json"
QUOTA_STATE_PATH = STATE_DIR / "quota_state.json"
DEFERRED_QUEUE_PATH = STATE_DIR / "deferred_queue.json"
ROUTING_EVENTS_PATH = STATE_DIR / "routing_events.jsonl"

def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name, "")
    value = str(raw).strip()
    return value if value else default


LLM_BASE_URL = _str_env("LLM_BASE_URL", "http://litellm:4000/v1").rstrip("/")
LLM_API_KEY = _str_env("LLM_API_KEY", "dummy")


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


HTTP_TIMEOUT_SECONDS = _int_env("HTTP_TIMEOUT_SECONDS", 120)
LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", 700)
LOCAL_MAX_TOKENS = _int_env("LOCAL_MAX_TOKENS", 120)
PAID_CALL_TIMEOUT_SECONDS = _int_env("PAID_CALL_TIMEOUT_SECONDS", 45)
LOCAL_CALL_TIMEOUT_SECONDS = _int_env("LOCAL_CALL_TIMEOUT_SECONDS", 20)
SCHEDULER_INTERVAL_SECONDS = _int_env("SCHEDULER_INTERVAL_SECONDS", 60)
ENABLE_OLLAMA = os.getenv("ENABLE_OLLAMA", "0") in {"1", "true", "TRUE", "yes", "YES"}

CORE_ROLE_ORDER = ["gemini", "codex", "abacus", "claude"]
ROLE_TASK_TYPE = {
    "gemini": "plan",
    "codex": "code",
    "abacus": "analysis",
    "claude": "review",
}
CORE_TASK_TYPES = {"plan", "code", "review", "analysis"}
SHADOW_TASK_TYPES = {"plan", "analysis"}
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace"))
DEFAULT_ORCHESTRATOR_PROMPTS_PATH = (
    WORKSPACE_ROOT
    / "audit"
    / "STELL_SYSTEM_CORE"
    / "05_workers"
    / "root"
    / "workspace"
    / "ops"
    / "orchestra"
    / "orchestrator"
    / "prompt_templates.json"
)
ORCHESTRATOR_PROMPTS_PATH = Path(
    os.getenv("ORCHESTRATOR_PROMPTS_PATH", str(DEFAULT_ORCHESTRATOR_PROMPTS_PATH))
)
PROMPT_TEMPLATE_CACHE: Optional[Dict[str, Any]] = None
REQUIRE_EXTERNAL_PROMPTS = os.getenv("ORCHESTRATOR_REQUIRE_EXTERNAL_PROMPTS", "1") in {
    "1",
    "true",
    "TRUE",
    "yes",
    "YES",
}

REQUIRED_PROMPT_TEMPLATE_PATHS = (
    "role_prompt_system.code",
    "role_prompt_system.review",
    "role_prompt_system.analysis",
    "role_prompt_system.ops_check",
    "role_prompt_system.doc",
    "role_prompt_system.default",
    "review_messages.system",
    "merge_messages.system",
    "degraded_output.code",
    "degraded_output.review",
    "degraded_output.analysis",
    "degraded_output.default",
)

LOCAL_MODELS = {"local_fast", "local_reason"}
PAID_MODELS = {"gemini_conductor", "codex_executor", "claude_reviewer", "abacus_analyst"}

RUNTIME_ROLE_DEFAULT_MODEL: Dict[str, str] = dict(ROLE_DEFAULT_MODEL)
TASK_FALLBACK_MODEL: Dict[str, str] = {
    "plan": "local_reason",
    "merge": "local_reason",
    "code": "local_reason",
    "review": "local_reason",
    "analysis": "local_fast",
}

MODEL_KEY_REQUIREMENTS: Dict[str, str] = {
    "gemini_conductor": "GEMINI_API_KEY",
    "codex_executor": "OPENAI_API_KEY",
    "claude_reviewer": "ANTHROPIC_API_KEY",
    "abacus_analyst": "ABACUSAI_API_KEY",
}

READINESS_CACHE_TTL_SECONDS = 60
READINESS_CACHE: Dict[str, Any] = {
    "checked_at": 0.0,
    "payload": {
        "readiness": "FAIL",
        "reason": "initializing",
        "litellm_reachable": False,
        "local_probe": {},
        "paid_probe": {},
        "checked_at": None,
        "cache_ttl_seconds": READINESS_CACHE_TTL_SECONDS,
    },
}

DISCOVERED_KEY_STATUS: Dict[str, bool] = {
    "OPENAI_API_KEY": False,
    "ANTHROPIC_API_KEY": False,
    "GEMINI_API_KEY": False,
    "ABACUSAI_API_KEY": False,
}
MODEL_REACHABILITY: Dict[str, bool] = {model: False for model in (PAID_MODELS | LOCAL_MODELS)}


class OrchestrateRequest(BaseModel):
    task: str = Field(min_length=1)
    context: Dict[str, Any] = Field(default_factory=dict)
    speed: Literal["max", "eco"] = "eco"
    pin: Dict[str, str] = Field(default_factory=dict)


class QuotaResetRequest(BaseModel):
    model: str = Field(min_length=1)
    cooldown_minutes: int = Field(default=120, ge=1, le=1440)


class LLMCallError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class QuotaExceeded(LLMCallError):
    def __init__(
        self,
        model: str,
        message: str,
        retry_after_seconds: Optional[int] = None,
        reset_at: Optional[str] = None,
    ):
        super().__init__(message=message, status_code=429)
        self.model = model
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
    temp_path.replace(path)


def _default_model_profiles() -> Dict[str, Any]:
    profiles = {
        "updated_at": to_iso(utcnow()),
        "models": {
            "gemini_conductor": {
                **default_model_profile(),
                "plan_score": 0.92,
                "analysis_score": 0.82,
                "code_score": 0.62,
            },
            "codex_executor": {
                **default_model_profile(),
                "code_score": 0.95,
                "analysis_score": 0.68,
            },
            "claude_reviewer": {
                **default_model_profile(),
                "review_score": 0.94,
                "analysis_score": 0.74,
            },
            "abacus_analyst": {
                **default_model_profile(),
                "analysis_score": 0.9,
                "review_score": 0.7,
            },
            "local_fast": {
                **default_model_profile(),
                "ops_check_score": 0.72,
                "doc_score": 0.74,
                "analysis_score": 0.55,
            },
            "local_reason": {
                **default_model_profile(),
                "analysis_score": 0.7,
                "review_score": 0.66,
                "code_score": 0.58,
            },
        },
        "last_profile_attempted": 0,
        "last_profile_succeeded": 0,
    }
    return profiles


def _default_quota_state() -> Dict[str, Any]:
    return {"updated_at": to_iso(utcnow()), "models": {}}


def _default_deferred_queue() -> Dict[str, Any]:
    return {"updated_at": to_iso(utcnow()), "items": []}


def _lookup_template_text(payload: Dict[str, Any], dotted_path: str) -> Optional[str]:
    node: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    if isinstance(node, str):
        text = node.strip()
        return text or None
    return None


def _load_prompt_templates() -> Dict[str, Any]:
    global PROMPT_TEMPLATE_CACHE
    if PROMPT_TEMPLATE_CACHE is not None:
        return PROMPT_TEMPLATE_CACHE

    if not ORCHESTRATOR_PROMPTS_PATH.exists():
        raise RuntimeError(
            "orchestrator prompt template file missing: "
            f"{ORCHESTRATOR_PROMPTS_PATH}"
        )

    payload = _read_json(ORCHESTRATOR_PROMPTS_PATH, {})
    if not isinstance(payload, dict):
        raise RuntimeError(
            "orchestrator prompt template file invalid (expected JSON object): "
            f"{ORCHESTRATOR_PROMPTS_PATH}"
        )

    if REQUIRE_EXTERNAL_PROMPTS:
        missing = [path for path in REQUIRED_PROMPT_TEMPLATE_PATHS if not _lookup_template_text(payload, path)]
        if missing:
            raise RuntimeError(
                "orchestrator prompt template missing required paths: "
                + ", ".join(missing)
            )

    PROMPT_TEMPLATE_CACHE = payload
    return PROMPT_TEMPLATE_CACHE


def _template_text(path: str) -> str:
    value = _lookup_template_text(_load_prompt_templates(), path)
    if value is None:
        raise RuntimeError(f"orchestrator prompt template missing value: {path}")
    return value


def _normalize_secret(raw: str) -> str:
    value = raw.strip().strip("'").strip('"')
    if "#" in value and not value.startswith("${"):
        value = value.split("#", 1)[0].strip()
    return value.strip()


def _looks_placeholder(value: str) -> bool:
    lower = value.strip().lower()
    if not lower:
        return True
    if lower in {"dummy", "none", "null", "changeme", "your_key_here"}:
        return True
    if lower.startswith("${") and lower.endswith("}"):
        return True
    return False


def _is_candidate_config_file(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if "docker-compose" in name:
        return True
    if "litellm.config" in name:
        return True
    if "secret" in name:
        return True
    if name.startswith("config") and path.suffix.lower() in {".yaml", ".yml", ".env", ".ini", ".toml"}:
        return True
    if path.suffix.lower() in {".env", ".yaml", ".yml", ".ini", ".toml", ".conf"}:
        return "config" in name
    return False


def _extract_keys_from_text(text: str) -> Dict[str, str]:
    key_values: Dict[str, str] = {}
    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+?)\s*$")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        key = match.group(1).strip().upper()
        value = _normalize_secret(match.group(2))
        if value and not _looks_placeholder(value):
            key_values[key] = value
    return key_values


def _discover_workspace_credentials() -> Dict[str, bool]:
    target_to_aliases = {
        "OPENAI_API_KEY": ["OPENAI_API_KEY"],
        "ANTHROPIC_API_KEY": ["ANTHROPIC_API_KEY"],
        "GEMINI_API_KEY": ["GEMINI_API_KEY"],
        "ABACUSAI_API_KEY": ["ABACUSAI_API_KEY", "ABACUS_API_KEY"],
    }

    discovered_values: Dict[str, str] = {}
    for target, aliases in target_to_aliases.items():
        for alias in aliases:
            current = _normalize_secret(os.getenv(alias, ""))
            if current and not _looks_placeholder(current):
                discovered_values[target] = current
                break

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
    max_files = 8000
    scanned = 0

    if WORKSPACE_ROOT.exists():
        for root, dirs, files in os.walk(WORKSPACE_ROOT):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for filename in files:
                if scanned >= max_files:
                    break
                path = Path(root) / filename
                if not _is_candidate_config_file(path):
                    continue
                scanned += 1
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                pairs = _extract_keys_from_text(text)
                for target, aliases in target_to_aliases.items():
                    if target in discovered_values:
                        continue
                    for alias in aliases:
                        value = pairs.get(alias.upper(), "")
                        if value and not _looks_placeholder(value):
                            discovered_values[target] = value
                            break
            if scanned >= max_files:
                break

    for key, value in discovered_values.items():
        os.environ[key] = value

    summary = {key: bool(discovered_values.get(key, "")) for key in DISCOVERED_KEY_STATUS}
    for key in summary:
        DISCOVERED_KEY_STATUS[key] = summary[key]

    print("[credential-discovery] summary")
    for key in ["GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ABACUSAI_API_KEY"]:
        status = "found" if summary.get(key) else "missing"
        print(f"[credential-discovery] {key}: {status}")

    return summary


def _configure_runtime_role_defaults(discovered: Dict[str, bool]) -> None:
    RUNTIME_ROLE_DEFAULT_MODEL["gemini"] = (
        "gemini_conductor" if discovered.get("GEMINI_API_KEY") else "local_reason"
    )
    RUNTIME_ROLE_DEFAULT_MODEL["codex"] = (
        "codex_executor" if discovered.get("OPENAI_API_KEY") else "local_reason"
    )
    RUNTIME_ROLE_DEFAULT_MODEL["claude"] = (
        "claude_reviewer" if discovered.get("ANTHROPIC_API_KEY") else "local_reason"
    )
    RUNTIME_ROLE_DEFAULT_MODEL["abacus"] = (
        "abacus_analyst" if discovered.get("ABACUSAI_API_KEY") else "local_fast"
    )


def _litellm_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}


async def _probe_model_inference(model: str, timeout_seconds: int) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=_litellm_headers(),
                json=payload,
            )
    except Exception:
        MODEL_REACHABILITY[model] = False
        return False

    if response.status_code >= 400:
        MODEL_REACHABILITY[model] = False
        return False

    try:
        parsed = response.json()
    except Exception:
        MODEL_REACHABILITY[model] = False
        return False

    ok = bool(_extract_content(parsed).strip())
    MODEL_REACHABILITY[model] = ok
    return ok


def _paid_probe_candidates(quota_state: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    models_state = quota_state.get("models", {}) if isinstance(quota_state, dict) else {}
    for model in ["gemini_conductor", "codex_executor", "claude_reviewer", "abacus_analyst"]:
        key_name = MODEL_KEY_REQUIREMENTS.get(model)
        if key_name and not DISCOVERED_KEY_STATUS.get(key_name, False):
            continue
        cooldown_until = parse_iso(
            (models_state.get(model, {}) if isinstance(models_state, dict) else {}).get("cooldown_until")
        )
        if cooldown_until and cooldown_until > utcnow():
            continue
        candidates.append(model)
    return candidates


async def _readiness_probe(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and (now - float(READINESS_CACHE.get("checked_at", 0.0))) < READINESS_CACHE_TTL_SECONDS:
        cached = READINESS_CACHE.get("payload")
        if isinstance(cached, dict):
            return cached

    payload: Dict[str, Any] = {
        "readiness": "FAIL",
        "reason": "",
        "litellm_reachable": False,
        "local_probe": {},
        "paid_probe": {},
        "checked_at": to_iso(utcnow()),
        "cache_ttl_seconds": READINESS_CACHE_TTL_SECONDS,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            models_resp = await client.get(f"{LLM_BASE_URL}/models", headers=_litellm_headers())
    except Exception:
        payload["reason"] = "litellm_unreachable"
        READINESS_CACHE["checked_at"] = now
        READINESS_CACHE["payload"] = payload
        return payload

    if models_resp.status_code >= 500:
        payload["reason"] = "litellm_error"
        READINESS_CACHE["checked_at"] = now
        READINESS_CACHE["payload"] = payload
        return payload
    if models_resp.status_code >= 400:
        payload["reason"] = f"litellm_http_{models_resp.status_code}"
        READINESS_CACHE["checked_at"] = now
        READINESS_CACHE["payload"] = payload
        return payload

    payload["litellm_reachable"] = True
    model_ids: List[str] = []
    try:
        data = models_resp.json()
        listed = data.get("data", []) if isinstance(data, dict) else []
        if isinstance(listed, list):
            model_ids = [str(item.get("id", "")) for item in listed if isinstance(item, dict)]
    except Exception:
        model_ids = []

    local_fast_ok = False
    local_reason_ok = False
    if "local_fast" in model_ids:
        local_fast_ok = await _probe_model_inference("local_fast", _model_timeout_seconds("local_fast"))
    if (not local_fast_ok) and "local_reason" in model_ids:
        local_reason_ok = await _probe_model_inference(
            "local_reason", _model_timeout_seconds("local_reason")
        )
    payload["local_probe"] = {"local_fast": local_fast_ok, "local_reason": local_reason_ok}

    quota_state = QuotaManager(QUOTA_STATE_PATH).snapshot()
    paid_probe: Dict[str, bool] = {}
    paid_ok = False
    for candidate in _paid_probe_candidates(quota_state):
        probe_ok = await _probe_model_inference(
            candidate, min(PAID_CALL_TIMEOUT_SECONDS, HTTP_TIMEOUT_SECONDS, 20)
        )
        paid_probe[candidate] = probe_ok
        if probe_ok:
            paid_ok = True
            break
    payload["paid_probe"] = paid_probe

    if paid_ok:
        payload["readiness"] = "READY"
        payload["reason"] = "paid_model_reachable"
    elif local_fast_ok or local_reason_ok:
        payload["readiness"] = "READY_LOCAL"
        payload["reason"] = "local_model_reachable"
    else:
        payload["readiness"] = "FAIL"
        payload["reason"] = "zero_models_passed_inference_probe"

    READINESS_CACHE["checked_at"] = now
    READINESS_CACHE["payload"] = payload
    return payload


async def _litellm_reachable() -> bool:
    probe = await _readiness_probe(force=False)
    return bool(probe.get("litellm_reachable", False))


def _compute_readiness(results: List[Dict[str, Any]], litellm_ok: bool) -> str:
    if not litellm_ok:
        return "FAIL"

    successful_models = {
        str(item.get("model", ""))
        for item in results
        if isinstance(item, dict)
        and item.get("role") in {"gemini", "codex", "claude", "abacus", "local_ops_check", "local_doc"}
        and item.get("status") in {"ok", "ok_fallback_error", "ok_fallback_quota"}
    }
    if any(model in PAID_MODELS for model in successful_models):
        return "READY"
    if any(model in LOCAL_MODELS for model in successful_models):
        return "READY_LOCAL"
    return "FAIL"


def _model_timeout_seconds(model: str) -> int:
    if model in LOCAL_MODELS:
        return max(LOCAL_CALL_TIMEOUT_SECONDS, 900)
    return min(HTTP_TIMEOUT_SECONDS, PAID_CALL_TIMEOUT_SECONDS)


def ensure_state_files() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PROFILES_PATH.exists():
        _write_json(MODEL_PROFILES_PATH, _default_model_profiles())
    if not QUOTA_STATE_PATH.exists():
        _write_json(QUOTA_STATE_PATH, _default_quota_state())
    if not DEFERRED_QUEUE_PATH.exists():
        _write_json(DEFERRED_QUEUE_PATH, _default_deferred_queue())
    if not ROUTING_EVENTS_PATH.exists():
        ROUTING_EVENTS_PATH.touch()


def _load_profiles() -> Dict[str, Any]:
    profiles = _read_json(MODEL_PROFILES_PATH, _default_model_profiles())
    if not isinstance(profiles, dict):
        profiles = _default_model_profiles()
    profiles.setdefault("models", {})
    return profiles


def _save_profiles(profiles: Dict[str, Any]) -> None:
    profiles["updated_at"] = to_iso(utcnow())
    _write_json(MODEL_PROFILES_PATH, profiles)


def _load_deferred_queue() -> Dict[str, Any]:
    queue = _read_json(DEFERRED_QUEUE_PATH, _default_deferred_queue())
    if not isinstance(queue, dict):
        queue = _default_deferred_queue()
    queue.setdefault("items", [])
    if not isinstance(queue["items"], list):
        queue["items"] = []
    return queue


def _save_deferred_queue(queue: Dict[str, Any]) -> None:
    queue["updated_at"] = to_iso(utcnow())
    _write_json(DEFERRED_QUEUE_PATH, queue)


def _append_routing_event(event: Dict[str, Any]) -> None:
    ROUTING_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ROUTING_EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _last_routing_event() -> Optional[Dict[str, Any]]:
    if not ROUTING_EVENTS_PATH.exists():
        return None
    last_line: Optional[str] = None
    with ROUTING_EVENTS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last_line = line
    if not last_line:
        return None
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return None


def _parse_retry_after(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    value = raw.strip()
    if value.isdigit():
        return int(value)
    return None


def _parse_reset_at(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()

    parsed_iso = parse_iso(value)
    if parsed_iso:
        return to_iso(parsed_iso)

    if value.isdigit():
        number = int(value)
        if number > 10_000_000_000:
            number = int(number / 1000)
        try:
            ts = datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError):
            return None
        return to_iso(ts)
    return None


def _extract_content(data: Dict[str, Any]) -> str:
    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""

    if isinstance(message, str):
        return message

    if isinstance(message, list):
        chunks: List[str] = []
        for block in message:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    chunks.append(block["text"])
                elif isinstance(block.get("content"), str):
                    chunks.append(block["content"])
        return "\n".join(chunks).strip()

    return str(message)


async def call_model(
    model: str,
    messages: List[Dict[str, str]],
    timeout_seconds: int,
) -> Tuple[str, Dict[str, Any]]:
    prepared_messages = messages
    if model in LOCAL_MODELS:
        prepared_messages = []
        for msg in messages:
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", ""))
            # Keep local prompts compact to avoid long CPU-bound generations.
            if len(content) > 1800:
                content = content[:1800]
            prepared_messages.append({"role": role, "content": content})

    max_tokens = LOCAL_MAX_TOKENS if model in LOCAL_MODELS else LLM_MAX_TOKENS
    payload = {
        "model": model,
        "messages": prepared_messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        MODEL_REACHABILITY[model] = False
        raise LLMCallError(
            f"Timed out after {timeout_seconds}s calling model '{model}'."
        ) from exc
    except httpx.HTTPError as exc:
        MODEL_REACHABILITY[model] = False
        raise LLMCallError(
            f"Failed to reach LLM gateway for model '{model}': {exc.__class__.__name__}."
        ) from exc

    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    if response.status_code == 429:
        MODEL_REACHABILITY[model] = True
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        reset_at = _parse_reset_at(
            response.headers.get("x-ratelimit-reset")
            or response.headers.get("x-ratelimit-reset-requests")
        )
        raise QuotaExceeded(
            model=model,
            message=f"Quota exceeded for model '{model}'.",
            retry_after_seconds=retry_after,
            reset_at=reset_at,
        )

    if response.status_code >= 400:
        MODEL_REACHABILITY[model] = False
        raise LLMCallError(
            f"LLM call failed for model '{model}' with HTTP {response.status_code}.",
            status_code=502,
        )

    data = response.json()
    output = _extract_content(data)
    MODEL_REACHABILITY[model] = True
    return output, {"latency_ms": latency_ms, "usage": data.get("usage", {})}


def _json_snippet(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _build_planner_messages(task: str, context: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are Gemini Conductor. Decompose tasks for a zero-conflict software orchestra. "
                "Return STRICT JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Task:\n"
                f"{task}\n\n"
                "Context JSON:\n"
                f"{json.dumps(context, ensure_ascii=True)}\n\n"
                "Return JSON with this schema: "
                "{\"sub_tasks\":[{\"task_type\":\"plan|code|review|analysis|ops_check|doc\","
                "\"instruction\":\"...\"}]}. Include at least plan, code, review, analysis."
            ),
        },
    ]


def _default_sub_tasks(task: str) -> List[Dict[str, str]]:
    return [
        {"task_type": "plan", "instruction": f"Build a concise implementation plan for: {task}"},
        {
            "task_type": "code",
            "instruction": (
                "Produce a minimal unified diff with correct headers, include apply commands, "
                "and include rollback notes."
            ),
        },
        {
            "task_type": "analysis",
            "instruction": "Provide structured risk analysis with actionable checks.",
        },
        {
            "task_type": "review",
            "instruction": "Review produced changes with PASS/FAIL, findings, and test commands.",
        },
    ]


def _parse_sub_tasks(plan_output: str, task: str) -> List[Dict[str, str]]:
    parsed = _json_snippet(plan_output)
    sub_tasks = []
    if parsed and isinstance(parsed.get("sub_tasks"), list):
        for item in parsed["sub_tasks"]:
            if not isinstance(item, dict):
                continue
            task_type = str(item.get("task_type", "")).strip().lower()
            instruction = str(item.get("instruction", "")).strip()
            if task_type and instruction:
                sub_tasks.append({"task_type": task_type, "instruction": instruction})

    required = {"plan", "code", "review", "analysis"}
    present = {entry["task_type"] for entry in sub_tasks}
    if not required.issubset(present):
        defaults = _default_sub_tasks(task)
        indexed = {entry["task_type"]: entry for entry in sub_tasks}
        for fallback in defaults:
            indexed.setdefault(fallback["task_type"], fallback)
        sub_tasks = [indexed[key] for key in ["plan", "code", "analysis", "review"]]

    return sub_tasks


def _role_prompt(task_type: str, instruction: str, task: str, context: Dict[str, Any]) -> List[Dict[str, str]]:
    if task_type == "code":
        system = _template_text("role_prompt_system.code")
    elif task_type == "review":
        system = _template_text("role_prompt_system.review")
    elif task_type == "analysis":
        system = _template_text("role_prompt_system.analysis")
    elif task_type in {"ops_check", "doc"}:
        system = _template_text(f"role_prompt_system.{task_type}")
    else:
        system = _template_text("role_prompt_system.default")

    user = (
        f"Task: {task}\n"
        f"Instruction: {instruction}\n"
        f"Context JSON: {json.dumps(context, ensure_ascii=True)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _review_messages(task: str, context: Dict[str, Any], code_output: str, instruction: str) -> List[Dict[str, str]]:
    system = _template_text("review_messages.system")
    user = (
        f"Task: {task}\n"
        f"Instruction: {instruction}\n"
        f"Context JSON: {json.dumps(context, ensure_ascii=True)}\n\n"
        "Codex output/diff:\n"
        f"{code_output[:4000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _merge_messages(
    task: str,
    speed: str,
    routing_decisions: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    deferred_summary: Dict[str, Any],
) -> List[Dict[str, str]]:
    system = _template_text("merge_messages.system")
    compact_results = [
        {
            "role": item.get("role"),
            "model": item.get("model"),
            "status": item.get("status"),
            "output": (item.get("output") or "")[:4000],
        }
        for item in results
    ]
    user = (
        f"Task: {task}\n"
        f"Speed: {speed}\n"
        f"Routing decisions: {json.dumps(routing_decisions, ensure_ascii=True)}\n"
        f"Results: {json.dumps(compact_results, ensure_ascii=True)}\n"
        f"Deferred: {json.dumps(deferred_summary, ensure_ascii=True)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _deterministic_degraded_output(task_type: str, task: str) -> str:
    if task_type == "code":
        return _template_text("degraded_output.code")
    if task_type == "review":
        return _template_text("degraded_output.review")
    if task_type in {"analysis", "ops_check", "doc"}:
        return _template_text("degraded_output.analysis")
    template = _template_text("degraded_output.default")
    return template.replace("{task}", task)


async def _best_effort_fallback(
    task_type: str,
    task: str,
    context: Dict[str, Any],
    quota_manager: QuotaManager,
) -> Tuple[str, str]:
    quota_state = quota_manager.snapshot()
    if task_type == "code":
        candidates = ["local_reason", "local_fast", "gemini_conductor"]
    elif task_type == "review":
        candidates = ["local_reason", "local_fast", "gemini_conductor"]
    elif task_type == "analysis":
        candidates = ["local_fast", "local_reason", "gemini_conductor"]
    elif task_type in {"plan", "merge"}:
        candidates = ["local_reason", "local_fast", "gemini_conductor"]
    else:
        candidates = ["local_reason", "local_fast", "gemini_conductor"]
    for model in candidates:
        model_state = quota_state.get("models", {}).get(model, {})
        cooldown_until = parse_iso(model_state.get("cooldown_until"))
        if cooldown_until and cooldown_until > utcnow():
            continue
        try:
            messages = _role_prompt(
                task_type=task_type,
                instruction=(
                    "Provide a degraded but usable output because the primary model is unavailable."
                ),
                task=task,
                context=context,
            )
            output, _meta = await call_model(model, messages, _model_timeout_seconds(model))
            if output.strip():
                return output, model
        except Exception:
            continue
    return _deterministic_degraded_output(task_type=task_type, task=task), "deterministic_degraded"


def _compute_retry_time(
    reset_at: Optional[str],
    retry_after_seconds: Optional[int],
    cooldown_minutes: int,
) -> str:
    base = utcnow() + timedelta(minutes=cooldown_minutes)
    if retry_after_seconds and retry_after_seconds > 0:
        base = max(base, utcnow() + timedelta(seconds=retry_after_seconds))
    parsed_reset = parse_iso(reset_at)
    if parsed_reset and parsed_reset > base:
        base = parsed_reset
    return to_iso(base)


def _enqueue_deferred(
    queue: Dict[str, Any],
    *,
    role: str,
    task_type: str,
    model: str,
    task: str,
    context: Dict[str, Any],
    reason: str,
    earliest_retry_at: str,
    messages: List[Dict[str, str]],
) -> Dict[str, Any]:
    item = {
        "id": str(uuid.uuid4()),
        "created_at": to_iso(utcnow()),
        "earliest_retry_at": earliest_retry_at,
        "role": role,
        "task_type": task_type,
        "model": model,
        "task": task,
        "context": context,
        "reason": reason,
        "messages": messages,
        "attempts": 0,
    }
    queue.setdefault("items", []).append(item)
    return item


def _pinned_model(role: str, pin: Dict[str, str]) -> Optional[str]:
    return pin.get(role)


def _role_default(role: str) -> Optional[str]:
    return RUNTIME_ROLE_DEFAULT_MODEL.get(role)


def _event_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "role": result.get("role"),
        "task_type": result.get("task_type"),
        "model": result.get("model"),
        "status": result.get("status"),
        "degraded": result.get("degraded", False),
        "deferred": bool(result.get("deferred_item")),
    }


async def _run_profile(mode: str) -> Dict[str, Any]:
    profiles = _load_profiles()

    async def _profile_call(
        model: str,
        messages: List[Dict[str, str]],
        timeout_seconds: int,
    ) -> Tuple[str, Dict[str, Any]]:
        return await call_model(model, messages, timeout_seconds)

    updated = await run_profile(
        mode=mode,
        call_model=_profile_call,
        profiles=profiles,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )
    _save_profiles(updated)
    return updated


async def _replay_due_deferred() -> Dict[str, Any]:
    quota_manager = QuotaManager(QUOTA_STATE_PATH)
    queue = _load_deferred_queue()
    now = utcnow()
    due_items = []
    retained_items = []

    for item in queue.get("items", []):
        retry_at = parse_iso(item.get("earliest_retry_at"))
        if retry_at and retry_at <= now:
            due_items.append(item)
        else:
            retained_items.append(item)

    if not due_items:
        return {"replayed": 0, "remaining": len(queue.get("items", []))}

    try:
        await _run_profile("mini")
    except Exception:
        # Best effort profiling; deferred replay should still proceed.
        pass

    replayed = 0
    for item in due_items:
        model = item.get("model", "")
        if not model:
            continue
        if quota_manager.is_in_cooldown(model):
            retained_items.append(item)
            continue

        messages = item.get("messages") or [
            {"role": "system", "content": "Replay deferred task."},
            {"role": "user", "content": item.get("task", "")},
        ]

        try:
            output, _meta = await call_model(model, messages, _model_timeout_seconds(model))
            replayed += 1
            _append_routing_event(
                {
                    "ts": to_iso(utcnow()),
                    "event": "deferred_replayed",
                    "deferred_id": item.get("id"),
                    "model": model,
                    "task_type": item.get("task_type"),
                    "output_preview": output[:200],
                }
            )
        except QuotaExceeded as exc:
            retry_at = _compute_retry_time(
                reset_at=exc.reset_at,
                retry_after_seconds=exc.retry_after_seconds,
                cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
            )
            quota_manager.mark_cooldown(
                model=model,
                reason=exc.message,
                cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
                reset_at=exc.reset_at,
            )
            item["earliest_retry_at"] = retry_at
            item["attempts"] = int(item.get("attempts", 0)) + 1
            retained_items.append(item)
        except Exception:
            item["earliest_retry_at"] = to_iso(utcnow() + timedelta(minutes=15))
            item["attempts"] = int(item.get("attempts", 0)) + 1
            retained_items.append(item)

    queue["items"] = retained_items
    _save_deferred_queue(queue)
    return {"replayed": replayed, "remaining": len(retained_items)}


async def _scheduler_loop() -> None:
    while True:
        try:
            await _replay_due_deferred()
        except Exception:
            # Scheduler must not crash the API service.
            pass
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


async def _execute_role(
    *,
    role: str,
    task_type: str,
    task: str,
    context: Dict[str, Any],
    instruction: str,
    speed: str,
    pin: Dict[str, str],
    profiles: Dict[str, Any],
    quota_manager: QuotaManager,
    queue: Dict[str, Any],
    force_messages: Optional[List[Dict[str, str]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
    quota_state = quota_manager.snapshot()
    pinned = _pinned_model(role, pin)
    default_model = _role_default(role)
    preferred = pinned or default_model
    strict_primary = task_type in CORE_TASK_TYPES and preferred is not None

    messages = force_messages or _role_prompt(
        task_type=task_type,
        instruction=instruction,
        task=task,
        context=context,
    )

    if preferred and quota_manager.is_in_cooldown(preferred) and (pinned or strict_primary):
        reason = "pinned_model_in_cooldown" if pinned else "primary_model_in_cooldown"
        retry_at = _compute_retry_time(
            reset_at=quota_state.get("models", {}).get(preferred, {}).get("reset_at"),
            retry_after_seconds=None,
            cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
        )
        deferred_item = _enqueue_deferred(
            queue,
            role=role,
            task_type=task_type,
            model=preferred,
            task=task,
            context=context,
            reason=reason,
            earliest_retry_at=retry_at,
            messages=messages,
        )
        degraded_output, degraded_model = await _best_effort_fallback(
            task_type, task, context, quota_manager
        )
        result = {
            "role": role,
            "task_type": task_type,
            "model": degraded_model if degraded_model != "deterministic_degraded" else preferred,
            "status": "deferred_primary_cooldown",
            "degraded": True,
            "output": degraded_output,
            "primary_model": preferred,
            "deferred_item": deferred_item,
        }
        decision = {
            "role": role,
            "task_type": task_type,
            "preferred": preferred,
            "selected": preferred,
            "pinned": bool(pinned),
            "decision": "defer_and_degrade",
            "reason": reason,
        }
        return result, decision, None

    route = choose_model(
        task_type=task_type,
        profiles=profiles,
        quota_state=quota_state,
        preferred_model=preferred,
    )
    if strict_primary:
        selected_model = preferred
        decision_reason = "pinned_or_strict_single_role" if pinned else "strict_single_role"
    else:
        selected_model = route.get("primary")
        decision_reason = route.get("reason", "highest_score")

    if not selected_model:
        retry_at = _compute_retry_time(
            reset_at=None,
            retry_after_seconds=None,
            cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
        )
        defer_model = preferred or (route.get("candidates", ["unassigned"])[0])
        deferred_item = _enqueue_deferred(
            queue,
            role=role,
            task_type=task_type,
            model=defer_model,
            task=task,
            context=context,
            reason="no_available_model",
            earliest_retry_at=retry_at,
            messages=messages,
        )
        degraded_output, degraded_model = await _best_effort_fallback(
            task_type, task, context, quota_manager
        )
        result = {
            "role": role,
            "task_type": task_type,
            "model": degraded_model if degraded_model != "deterministic_degraded" else defer_model,
            "status": "deferred_no_available_model",
            "degraded": True,
            "output": degraded_output,
            "primary_model": defer_model,
            "deferred_item": deferred_item,
        }
        decision = {
            "role": role,
            "task_type": task_type,
            "preferred": preferred,
            "selected": None,
            "shadow": None,
            "pinned": bool(pinned),
            "decision": "defer_and_degrade",
            "reason": "no_available_model",
            "ranked": route.get("ranked", []),
        }
        return result, decision, None

    shadow_model = None
    if speed == "max" and task_type in SHADOW_TASK_TYPES:
        candidate_shadow = route.get("shadow")
        if candidate_shadow and candidate_shadow != selected_model:
            shadow_model = candidate_shadow

    decision = {
        "role": role,
        "task_type": task_type,
        "preferred": preferred,
        "selected": selected_model,
        "shadow": shadow_model,
        "pinned": bool(pinned),
        "decision": decision_reason,
        "ranked": route.get("ranked", []),
    }

    try:
        output, meta = await call_model(
            selected_model,
            messages,
            _model_timeout_seconds(selected_model),
        )
        result = {
            "role": role,
            "task_type": task_type,
            "model": selected_model,
            "status": "ok",
            "degraded": False,
            "output": output,
            "meta": meta,
            "deferred_item": None,
        }
    except QuotaExceeded as exc:
        cooldown = quota_manager.mark_cooldown(
            model=selected_model,
            reason=exc.message,
            cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
            reset_at=exc.reset_at,
        )
        retry_at = _compute_retry_time(
            reset_at=cooldown.reset_at,
            retry_after_seconds=exc.retry_after_seconds,
            cooldown_minutes=DEFAULT_COOLDOWN_MINUTES,
        )
        deferred_item = _enqueue_deferred(
            queue,
            role=role,
            task_type=task_type,
            model=selected_model,
            task=task,
            context=context,
            reason=exc.message,
            earliest_retry_at=retry_at,
            messages=messages,
        )
        fallback_model = TASK_FALLBACK_MODEL.get(task_type)
        fallback_result: Optional[Tuple[str, Dict[str, Any]]] = None
        if (
            fallback_model
            and fallback_model != selected_model
            and not quota_manager.is_in_cooldown(fallback_model)
        ):
            try:
                fallback_result = await call_model(
                    fallback_model,
                    messages,
                    _model_timeout_seconds(fallback_model),
                )
            except Exception:
                fallback_result = None

        if fallback_result:
            fallback_output, fallback_meta = fallback_result
            decision["fallback_selected"] = fallback_model
            result = {
                "role": role,
                "task_type": task_type,
                "model": fallback_model,
                "status": "ok_fallback_quota",
                "degraded": False,
                "output": fallback_output,
                "meta": fallback_meta,
                "primary_model": selected_model,
                "deferred_item": deferred_item,
            }
        else:
            degraded_output, degraded_model = await _best_effort_fallback(
                task_type, task, context, quota_manager
            )
            result = {
                "role": role,
                "task_type": task_type,
                "model": degraded_model if degraded_model != "deterministic_degraded" else selected_model,
                "status": "degraded_quota",
                "degraded": True,
                "output": degraded_output,
                "primary_model": selected_model,
                "deferred_item": deferred_item,
            }
    except LLMCallError as exc:
        fallback_model = TASK_FALLBACK_MODEL.get(task_type)
        fallback_result: Optional[Tuple[str, Dict[str, Any]]] = None
        if (
            fallback_model
            and fallback_model != selected_model
            and not quota_manager.is_in_cooldown(fallback_model)
        ):
            try:
                fallback_result = await call_model(
                    fallback_model,
                    messages,
                    _model_timeout_seconds(fallback_model),
                )
            except Exception:
                fallback_result = None

        if fallback_result:
            fallback_output, fallback_meta = fallback_result
            decision["fallback_selected"] = fallback_model
            result = {
                "role": role,
                "task_type": task_type,
                "model": fallback_model,
                "status": "ok_fallback_error",
                "degraded": False,
                "output": fallback_output,
                "meta": fallback_meta,
                "primary_model": selected_model,
                "error": exc.message,
                "deferred_item": None,
            }
        else:
            degraded_output, degraded_model = await _best_effort_fallback(
                task_type, task, context, quota_manager
            )
            result = {
                "role": role,
                "task_type": task_type,
                "model": degraded_model if degraded_model != "deterministic_degraded" else selected_model,
                "status": "degraded_error",
                "degraded": True,
                "output": degraded_output,
                "primary_model": selected_model,
                "error": exc.message,
                "deferred_item": None,
            }

    shadow_result: Optional[Dict[str, Any]] = None
    if shadow_model and not quota_manager.is_in_cooldown(shadow_model):
        shadow_messages = [
            {
                "role": "system",
                "content": "Return a short shadow response (max 8 lines).",
            },
            {
                "role": "user",
                "content": f"Task: {task}\nInstruction: {instruction}\nType: {task_type}",
            },
        ]
        try:
            shadow_output, shadow_meta = await call_model(
                shadow_model, shadow_messages, _model_timeout_seconds(shadow_model)
            )
            shadow_result = {
                "role": f"{role}_shadow",
                "task_type": task_type,
                "model": shadow_model,
                "status": "ok",
                "degraded": False,
                "output": shadow_output,
                "meta": shadow_meta,
                "shadow": True,
            }
        except Exception:
            shadow_result = None

    return result, decision, shadow_result


app = FastAPI(title=APP_NAME, version="1.0.0")
_scheduler_task: Optional[asyncio.Task[Any]] = None


@app.on_event("startup")
async def _startup() -> None:
    global _scheduler_task
    _load_prompt_templates()
    ensure_state_files()
    discovered = _discover_workspace_credentials()
    _configure_runtime_role_defaults(discovered)
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass


@app.get("/health")
async def health() -> Dict[str, Any]:
    readiness_probe = await _readiness_probe(force=False)
    return {
        "ok": True,
        "service": APP_NAME,
        "time": to_iso(utcnow()),
        "llm_base_url": LLM_BASE_URL,
        "enable_ollama": ENABLE_OLLAMA,
        "litellm_reachable": bool(readiness_probe.get("litellm_reachable", False)),
        "readiness": readiness_probe.get("readiness", "FAIL"),
        "credential_status": DISCOVERED_KEY_STATUS,
        "role_defaults": RUNTIME_ROLE_DEFAULT_MODEL,
    }


@app.get("/state")
async def state() -> Dict[str, Any]:
    ensure_state_files()
    queue = _load_deferred_queue()
    readiness_probe = await _readiness_probe(force=False)
    return {
        "readiness": readiness_probe.get("readiness", "FAIL"),
        "readiness_probe": readiness_probe,
        "deferred_count": len(queue.get("items", [])),
        "last_routing": _last_routing_event(),
    }


@app.get("/quota")
async def quota() -> Dict[str, Any]:
    ensure_state_files()
    quota_manager = QuotaManager(QUOTA_STATE_PATH)
    return quota_manager.snapshot()


@app.post("/quota/reset")
async def quota_reset(payload: QuotaResetRequest) -> Dict[str, Any]:
    ensure_state_files()
    quota_manager = QuotaManager(QUOTA_STATE_PATH)
    quota_manager.clear_cooldown(payload.model)
    model_state = quota_manager.snapshot().get("models", {}).get(payload.model, {})
    return {
        "model": payload.model,
        "cooldown_until": model_state.get("cooldown_until"),
        "reset_at": model_state.get("reset_at"),
        "reason": "cooldown_cleared",
    }


@app.post("/profile/mini")
async def profile_mini() -> Dict[str, Any]:
    ensure_state_files()
    profiles = await _run_profile("mini")
    succeeded = int(profiles.get("last_profile_succeeded", 0))
    return {
        "mode": "mini",
        "status": "ok" if succeeded > 0 else "degraded_no_successful_calls",
        "updated_at": profiles.get("updated_at"),
        "attempted": profiles.get("last_profile_attempted", 0),
        "succeeded": succeeded,
    }


@app.post("/profile/run")
async def profile_run() -> Dict[str, Any]:
    ensure_state_files()
    profiles = await _run_profile("full")
    succeeded = int(profiles.get("last_profile_succeeded", 0))
    return {
        "mode": "full",
        "status": "ok" if succeeded > 0 else "degraded_no_successful_calls",
        "updated_at": profiles.get("updated_at"),
        "attempted": profiles.get("last_profile_attempted", 0),
        "succeeded": succeeded,
    }


@app.post("/orchestrate")
async def orchestrate(request: OrchestrateRequest) -> Dict[str, Any]:
    ensure_state_files()
    discovered = _discover_workspace_credentials()
    _configure_runtime_role_defaults(discovered)

    profiles = _load_profiles()
    quota_manager = QuotaManager(QUOTA_STATE_PATH)
    queue = _load_deferred_queue()

    routing_decisions: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    planner_messages = _build_planner_messages(request.task, request.context)
    planner_result, planner_decision, planner_shadow = await _execute_role(
        role="gemini",
        task_type="plan",
        task=request.task,
        context=request.context,
        instruction="Decompose task into subtasks with task_type labels.",
        speed=request.speed,
        pin=request.pin,
        profiles=profiles,
        quota_manager=quota_manager,
        queue=queue,
        force_messages=planner_messages,
    )
    routing_decisions.append(planner_decision)
    results.append(planner_result)
    if planner_shadow:
        results.append(planner_shadow)

    sub_tasks = _parse_sub_tasks(planner_result.get("output", ""), request.task)
    by_task_type = {entry["task_type"]: entry for entry in sub_tasks}

    async def run_role(role: str) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
        task_type = ROLE_TASK_TYPE[role]
        instruction = by_task_type.get(task_type, {}).get(
            "instruction", f"Provide {task_type} output for task."
        )
        return await _execute_role(
            role=role,
            task_type=task_type,
            task=request.task,
            context=request.context,
            instruction=instruction,
            speed=request.speed,
            pin=request.pin,
            profiles=profiles,
            quota_manager=quota_manager,
            queue=queue,
        )

    codex_task = asyncio.create_task(run_role("codex"))
    abacus_task = asyncio.create_task(run_role("abacus"))

    codex_result, codex_decision, codex_shadow = await codex_task
    abacus_result, abacus_decision, abacus_shadow = await abacus_task
    routing_decisions.extend([codex_decision, abacus_decision])
    results.extend([codex_result, abacus_result])
    if codex_shadow:
        results.append(codex_shadow)
    if abacus_shadow:
        results.append(abacus_shadow)

    review_instruction = by_task_type.get("review", {}).get(
        "instruction", "Review codex output with PASS/FAIL and tests."
    )
    review_messages = _review_messages(
        request.task,
        request.context,
        code_output=codex_result.get("output", ""),
        instruction=review_instruction,
    )
    claude_result, claude_decision, claude_shadow = await _execute_role(
        role="claude",
        task_type="review",
        task=request.task,
        context=request.context,
        instruction=review_instruction,
        speed=request.speed,
        pin=request.pin,
        profiles=profiles,
        quota_manager=quota_manager,
        queue=queue,
        force_messages=review_messages,
    )
    routing_decisions.append(claude_decision)
    results.append(claude_result)
    if claude_shadow:
        results.append(claude_shadow)

    if ENABLE_OLLAMA:
        for task_type in ["ops_check", "doc"]:
            local_role = f"local_{task_type}"
            local_result, local_decision, _local_shadow = await _execute_role(
                role=local_role,
                task_type=task_type,
                task=request.task,
                context=request.context,
                instruction=f"Provide {task_type} checklist/alternatives.",
                speed="eco",
                pin={},
                profiles=profiles,
                quota_manager=quota_manager,
                queue=queue,
            )
            routing_decisions.append(local_decision)
            results.append(local_result)

    for item in results:
        deferred_item = item.get("deferred_item")
        item["deferred"] = bool(deferred_item)
        item["earliest_retry_at"] = (
            deferred_item.get("earliest_retry_at") if isinstance(deferred_item, dict) else None
        )
        item.setdefault("degraded", False)

    _save_deferred_queue(queue)

    this_call_deferred = [item for item in results if item.get("deferred_item")]
    queue_after = _load_deferred_queue()
    deferred_summary = {
        "newly_deferred": [
            {
                "id": item["deferred_item"].get("id"),
                "role": item["deferred_item"].get("role"),
                "task_type": item["deferred_item"].get("task_type"),
                "model": item["deferred_item"].get("model"),
                "earliest_retry_at": item["deferred_item"].get("earliest_retry_at"),
                "reason": item["deferred_item"].get("reason"),
            }
            for item in this_call_deferred
        ],
        "queue_count": len(queue_after.get("items", [])),
    }

    merge_messages = _merge_messages(
        task=request.task,
        speed=request.speed,
        routing_decisions=routing_decisions,
        results=results,
        deferred_summary=deferred_summary,
    )

    readiness_probe = await _readiness_probe(force=False)
    readiness = str(readiness_probe.get("readiness", "FAIL"))
    if readiness == "FAIL":
        litellm_ok = bool(readiness_probe.get("litellm_reachable", False))
        readiness = _compute_readiness(results=results, litellm_ok=litellm_ok)

    final_output = ""
    final_model = request.pin.get("gemini", RUNTIME_ROLE_DEFAULT_MODEL.get("gemini", "gemini_conductor"))
    final_status = "ok"
    try:
        final_output, _final_meta = await call_model(
            final_model, merge_messages, _model_timeout_seconds(final_model)
        )
    except Exception:
        final_status = "degraded_error"
        final_output = (
            "MERGED PLAN\n"
            "1. Confirm scope and constraints.\n"
            "2. Apply minimal patch or patch-plan output.\n"
            "3. Run smoke tests and inspect logs.\n"
            "4. Roll back immediately on failed checks.\n"
            "\nAPPLY STEPS\n"
            "- Use Codex output if unified diff is present; otherwise follow degraded patch-plan.\n"
            "- Execute only isolated `ops/orchestra` changes.\n"
            "\nTESTS/SMOKE\n"
            "- curl -s http://localhost:7010/state\n"
            "- curl -s -X POST http://localhost:7010/orchestrate -H 'Content-Type: application/json' -d '{\"task\":\"smoke\"}'\n"
            "\nROLLBACK PLAN\n"
            "- Revert modified files in ops/orchestra and restart compose services.\n"
            f"\nREADINESS\n- {readiness}\n"
            f"\nDEFERRED SUMMARY\n- {json.dumps(deferred_summary, ensure_ascii=True)}"
        )

    event = {
        "ts": to_iso(utcnow()),
        "event": "orchestrate",
        "task_preview": request.task[:160],
        "speed": request.speed,
        "readiness": readiness,
        "routing": routing_decisions,
        "results": [_event_from_result(item) for item in results],
        "deferred_count": deferred_summary["queue_count"],
    }
    _append_routing_event(event)

    core_roles_seen = {item.get("role") for item in results}
    required_roles = {"gemini", "codex", "claude", "abacus"}
    if not required_roles.issubset(core_roles_seen):
        raise HTTPException(
            status_code=502,
            detail="Orchestration failed to produce all core role outputs.",
        )

    return {
        "task": request.task,
        "routing_decisions": routing_decisions,
        "results": results,
        "final_output": final_output,
        "final": {
            "model": final_model,
            "status": final_status,
            "readiness": readiness,
            "output": final_output,
        },
        "deferred": deferred_summary,
    }
