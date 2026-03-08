from __future__ import annotations

from typing import Any, Dict, List, Optional

from quota import parse_iso, utcnow
from scorer import TASK_SCORE_KEYS

ROLE_TO_TASK_TYPE: Dict[str, str] = {
    "gemini": "plan",
    "codex": "code",
    "claude": "review",
    "abacus": "analysis",
}

ROLE_DEFAULT_MODEL: Dict[str, str] = {
    "gemini": "gemini_conductor",
    "codex": "codex_executor",
    "claude": "claude_reviewer",
    "abacus": "abacus_analyst",
}

TASK_CANDIDATES: Dict[str, List[str]] = {
    "plan": ["gemini_conductor", "local_reason", "local_fast"],
    "merge": ["gemini_conductor", "local_reason", "local_fast"],
    "code": ["codex_executor", "local_reason", "local_fast"],
    "review": ["claude_reviewer", "local_reason", "local_fast"],
    "analysis": ["abacus_analyst", "local_fast", "local_reason"],
    "ops_check": ["local_fast", "local_reason", "gemini_conductor"],
    "doc": ["local_fast", "local_reason", "gemini_conductor"],
}


def _is_cooldown(quota_state: Dict[str, Any], model: str) -> bool:
    models = quota_state.get("models", {}) if isinstance(quota_state, dict) else {}
    model_state = models.get(model, {}) if isinstance(models, dict) else {}
    cooldown_raw = model_state.get("cooldown_until")
    cooldown_until = parse_iso(cooldown_raw)
    return bool(cooldown_until and cooldown_until > utcnow())


def _score_for_task(model: str, task_type: str, profiles: Dict[str, Any]) -> float:
    model_profiles = profiles.get("models", {}) if isinstance(profiles, dict) else {}
    one_model = model_profiles.get(model, {}) if isinstance(model_profiles, dict) else {}
    score_key = TASK_SCORE_KEYS.get(task_type, "analysis_score")
    score = one_model.get(score_key, 0.5)
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.5


def choose_model(
    task_type: str,
    profiles: Dict[str, Any],
    quota_state: Dict[str, Any],
    preferred_model: Optional[str] = None,
) -> Dict[str, Any]:
    candidates = list(TASK_CANDIDATES.get(task_type, ["gemini_conductor"]))
    if preferred_model and preferred_model not in candidates:
        candidates.insert(0, preferred_model)

    available: List[Dict[str, Any]] = []
    for index, model in enumerate(candidates):
        if _is_cooldown(quota_state, model):
            continue
        available.append(
            {
                "model": model,
                "score": _score_for_task(model, task_type, profiles),
                "preferred": model == preferred_model,
                "order": index,
            }
        )

    if not available:
        return {
            "primary": None,
            "shadow": None,
            "candidates": candidates,
            "task_type": task_type,
            "reason": "all_candidates_in_cooldown",
            "ranked": [],
        }

    available.sort(
        key=lambda item: (
            -item["score"],
            -int(item["preferred"]),
            item["order"],
        )
    )

    primary = available[0]["model"]
    shadow = available[1]["model"] if len(available) > 1 else None
    reason = "highest_score"
    if preferred_model and primary == preferred_model:
        reason = "preferred_or_pinned"

    return {
        "primary": primary,
        "shadow": shadow,
        "candidates": candidates,
        "task_type": task_type,
        "reason": reason,
        "ranked": available,
    }


def resolve_role_model(role: str, pin: Optional[Dict[str, str]]) -> str:
    pin = pin or {}
    return pin.get(role) or ROLE_DEFAULT_MODEL.get(role, "")
