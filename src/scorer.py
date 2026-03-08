from __future__ import annotations

import re
from typing import Dict

TASK_SCORE_KEYS: Dict[str, str] = {
    "plan": "plan_score",
    "merge": "plan_score",
    "code": "code_score",
    "review": "review_score",
    "analysis": "analysis_score",
    "ops_check": "ops_check_score",
    "doc": "doc_score",
}


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return round(value, 4)


def _step_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*\d+\.\s+", text))


def score_output(task_type: str, output: str) -> float:
    text = output or ""
    lower = text.lower()

    if task_type in {"plan", "merge"}:
        score = 0.15
        steps = _step_count(text)
        if 3 <= steps <= 7:
            score += 0.45
        elif steps > 0:
            score += 0.2
        if "acceptance" in lower:
            score += 0.25
        if "rollback" in lower:
            score += 0.15
        return _clamp(score)

    if task_type == "code":
        score = 0.15
        if "```diff" in lower or "diff --git" in lower or "\n@@" in text:
            score += 0.4
        if "rollback" in lower:
            score += 0.2
        if "apply" in lower or "command" in lower:
            score += 0.1
        if text.count("\n") <= 220:
            score += 0.15
        return _clamp(score)

    if task_type == "review":
        score = 0.2
        if "pass" in lower or "fail" in lower:
            score += 0.25
        if "finding" in lower or "risk" in lower:
            score += 0.25
        if "test" in lower or "smoke" in lower:
            score += 0.2
        return _clamp(score)

    if task_type == "analysis":
        score = 0.2
        bullet_count = len(re.findall(r"(?m)^\s*[-*]\s+", text))
        if bullet_count >= 3:
            score += 0.35
        if "action" in lower or "next" in lower or "check" in lower:
            score += 0.25
        if "risk" in lower or "assumption" in lower:
            score += 0.2
        return _clamp(score)

    if task_type in {"ops_check", "doc"}:
        score = 0.2
        if len(text) > 160:
            score += 0.25
        if "check" in lower or "verify" in lower or "step" in lower:
            score += 0.3
        if "rollback" in lower:
            score += 0.15
        return _clamp(score)

    return 0.5


def default_model_profile() -> Dict[str, float]:
    return {
        "plan_score": 0.5,
        "code_score": 0.5,
        "review_score": 0.5,
        "analysis_score": 0.5,
        "ops_check_score": 0.5,
        "doc_score": 0.5,
    }
