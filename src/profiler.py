from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from scorer import TASK_SCORE_KEYS, score_output

CallModel = Callable[[str, List[Dict[str, str]], int], Awaitable[Tuple[str, Dict[str, Any]]]]
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
REQUIRE_EXTERNAL_PROMPTS = os.getenv("ORCHESTRATOR_REQUIRE_EXTERNAL_PROMPTS", "1") in {
    "1",
    "true",
    "TRUE",
    "yes",
    "YES",
}

MODEL_TASKS: Dict[str, List[str]] = {
    "gemini_conductor": ["plan", "analysis"],
    "codex_executor": ["code"],
    "claude_reviewer": ["review"],
    "abacus_analyst": ["analysis"],
    "local_fast": ["ops_check", "doc"],
    "local_reason": ["analysis", "review"],
}

REQUIRED_MICRO_TASK_TYPES = {"plan", "code", "review", "analysis", "ops_check", "doc"}


def _load_prompt_templates() -> Dict[str, Any]:
    if not ORCHESTRATOR_PROMPTS_PATH.exists():
        raise RuntimeError(
            "orchestrator prompt template file missing: "
            f"{ORCHESTRATOR_PROMPTS_PATH}"
        )
    try:
        payload = json.loads(ORCHESTRATOR_PROMPTS_PATH.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(payload, dict):
            raise RuntimeError(
                "orchestrator prompt template file invalid (expected JSON object): "
                f"{ORCHESTRATOR_PROMPTS_PATH}"
            )
        return payload
    except Exception:
        raise


def _resolve_micro_tasks() -> Dict[str, List[str]]:
    payload = _load_prompt_templates()
    external = payload.get("micro_tasks_mini")
    if not isinstance(external, dict):
        raise RuntimeError("orchestrator prompt template missing object: micro_tasks_mini")

    merged: Dict[str, List[str]] = {}
    for task_type, prompts in external.items():
        if not isinstance(task_type, str) or not isinstance(prompts, list):
            continue
        normalized = [str(item).strip() for item in prompts if str(item).strip()]
        if normalized:
            merged[task_type] = normalized

    missing = [task_type for task_type in sorted(REQUIRED_MICRO_TASK_TYPES) if not merged.get(task_type)]
    if REQUIRE_EXTERNAL_PROMPTS and missing:
        raise RuntimeError(
            "orchestrator prompt template missing micro_tasks_mini entries: "
            + ", ".join(missing)
        )
    return merged


def _resolve_profile_system_prompt() -> str:
    external = _load_prompt_templates().get("profile")
    if not isinstance(external, dict):
        raise RuntimeError("orchestrator prompt template missing object: profile")
    system = str(external.get("system") or "").strip()
    if not system:
        raise RuntimeError("orchestrator prompt template missing value: profile.system")
    return system


MICRO_TASKS_MINI: Dict[str, List[str]] = _resolve_micro_tasks()
PROFILE_SYSTEM_PROMPT = _resolve_profile_system_prompt()

MICRO_TASKS_FULL: Dict[str, List[str]] = {
    task_type: prompts + [
        f"Produce another example for {task_type} with strict structure and rollback details."
    ]
    for task_type, prompts in MICRO_TASKS_MINI.items()
}


def _avg(values: List[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


async def run_profile(
    mode: str,
    call_model: CallModel,
    profiles: Dict[str, Any],
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    prompts_by_type = MICRO_TASKS_FULL if mode == "full" else MICRO_TASKS_MINI

    model_profiles = profiles.setdefault("models", {})
    attempted = 0
    succeeded = 0

    for model, task_types in MODEL_TASKS.items():
        model_state = model_profiles.setdefault(model, {})
        per_type_scores: Dict[str, List[float]] = {}

        for task_type in task_types:
            prompts = prompts_by_type.get(task_type, [])
            for prompt in prompts:
                attempted += 1
                messages = [
                    {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
                try:
                    output, _meta = await call_model(model, messages, timeout_seconds)
                except Exception:
                    continue
                succeeded += 1
                per_type_scores.setdefault(task_type, []).append(score_output(task_type, output))

        for task_type, scores in per_type_scores.items():
            if not scores:
                continue
            score_key = TASK_SCORE_KEYS.get(task_type, "analysis_score")
            model_state[score_key] = _avg(scores)

        model_state["last_profile_mode"] = mode
        model_state["last_profile_attempted"] = attempted
        model_state["last_profile_succeeded"] = succeeded

    profiles["last_profile_attempted"] = attempted
    profiles["last_profile_succeeded"] = succeeded
    return profiles
