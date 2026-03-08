from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_COOLDOWN_MINUTES = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
    temp_path.replace(path)


@dataclass
class CooldownInfo:
    model: str
    cooldown_until: str
    reset_at: Optional[str]
    reason: str


class QuotaManager:
    def __init__(self, quota_path: Path):
        self.quota_path = quota_path
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        data = _read_json(self.quota_path, {"models": {}, "updated_at": to_iso(utcnow())})
        if not isinstance(data, dict):
            data = {"models": {}, "updated_at": to_iso(utcnow())}
        data.setdefault("models", {})
        data.setdefault("updated_at", to_iso(utcnow()))
        return data

    def refresh(self) -> None:
        self.state = self._load()

    def save(self) -> None:
        self.state["updated_at"] = to_iso(utcnow())
        _write_json(self.quota_path, self.state)

    def get_model_state(self, model: str) -> Dict[str, Any]:
        models = self.state.setdefault("models", {})
        model_state = models.get(model)
        if not isinstance(model_state, dict):
            model_state = {}
        model_state.setdefault("cooldown_until", None)
        model_state.setdefault("reset_at", None)
        model_state.setdefault("last_error", None)
        model_state.setdefault("last_quota_event", None)
        models[model] = model_state
        return model_state

    def is_in_cooldown(self, model: str, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        model_state = self.get_model_state(model)
        cooldown_until = parse_iso(model_state.get("cooldown_until"))
        return bool(cooldown_until and cooldown_until > now)

    def mark_cooldown(
        self,
        model: str,
        reason: str,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        reset_at: Optional[str] = None,
    ) -> CooldownInfo:
        now = utcnow()
        reset_ts = parse_iso(reset_at)
        cooldown_until = now + timedelta(minutes=cooldown_minutes)
        if reset_ts and reset_ts > cooldown_until:
            cooldown_until = reset_ts

        model_state = self.get_model_state(model)
        model_state["cooldown_until"] = to_iso(cooldown_until)
        model_state["reset_at"] = to_iso(reset_ts) if reset_ts else None
        model_state["last_error"] = reason
        model_state["last_quota_event"] = to_iso(now)
        self.save()

        return CooldownInfo(
            model=model,
            cooldown_until=model_state["cooldown_until"],
            reset_at=model_state["reset_at"],
            reason=reason,
        )

    def clear_cooldown(self, model: str) -> None:
        model_state = self.get_model_state(model)
        model_state["cooldown_until"] = None
        model_state["reset_at"] = None
        self.save()

    def snapshot(self) -> Dict[str, Any]:
        self.refresh()
        return self.state
