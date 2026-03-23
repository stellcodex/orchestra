from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException

from runtime_app.lib.config import settings


def request_decision(file_id: str, *, timeout: int = 15) -> dict[str, Any]:
    payload = json.dumps({"file_id": file_id}).encode("utf-8")
    request = Request(
        f"{settings.stell_ai_base_url.rstrip('/')}/decide",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            data: Any = json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="ignore").strip() or "STELL.AI rejected the request"
        raise HTTPException(status_code=exc.code, detail=detail)
    except URLError as exc:
        raise HTTPException(status_code=503, detail=f"STELL.AI unavailable: {exc.reason}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="STELL.AI returned an invalid decision payload")
    return data
