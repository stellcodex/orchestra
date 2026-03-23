from __future__ import annotations

import re
from uuid import uuid4, UUID

SCX_ID_PREFIX = "scx_"
SCX_ID_PATTERN = re.compile(r"^scx_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def generate_scx_id() -> str:
    return format_scx_file_id(uuid4())


def is_scx_id(value: str) -> bool:
    return bool(SCX_ID_PATTERN.match(value or ""))


def normalize_scx_file_id(value: str) -> UUID:
    raw = (value or "").strip()
    if raw.lower().startswith(SCX_ID_PREFIX):
        raw = raw[len(SCX_ID_PREFIX) :]
    try:
        return UUID(raw)
    except Exception:
        raise ValueError("Invalid SCX id")


def format_scx_file_id(value: UUID | str) -> str:
    uid = value if isinstance(value, UUID) else normalize_scx_file_id(value)
    return f"{SCX_ID_PREFIX}{str(uid)}"


def normalize_scx_id(value: str) -> str:
    return format_scx_file_id(value)
