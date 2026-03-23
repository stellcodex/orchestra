from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException

from runtime_app.lib.config import settings


def _build_url(path: str, query: dict[str, Any] | None = None) -> str:
    url = f"{settings.backend_internal_base_url.rstrip('/')}/{str(path or '').lstrip('/')}"
    if query:
        encoded = urlencode(
            [(str(key), "" if value is None else str(value)) for key, value in query.items() if value is not None],
            doseq=True,
        )
        if encoded:
            url = f"{url}?{encoded}"
    return url


def _decode_json(raw: bytes | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"detail": raw.decode("utf-8", errors="ignore").strip() or "Unreadable backend response"}


def request_backend_json(
    path: str,
    *,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 10,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "X-Internal-Token": settings.internal_service_token,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(_build_url(path, query=query), data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return _decode_json(response.read())
    except HTTPError as exc:
        payload = _decode_json(exc.read())
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise HTTPException(status_code=exc.code, detail=detail or "Backend rejected the request")
    except URLError as exc:
        raise HTTPException(status_code=503, detail=f"Backend unavailable: {exc.reason}")


def get_file_context(file_id: str, *, include_assembly_tree: bool = False) -> dict[str, Any]:
    payload = request_backend_json(
        f"/files/{file_id}/context",
        query={"include_assembly_tree": str(bool(include_assembly_tree)).lower()},
        timeout=15,
    )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Backend returned an invalid file context")
    return payload


def get_session_by_file(file_id: str) -> dict[str, Any]:
    payload = request_backend_json(f"/orchestrator/sessions/by-file/{file_id}", timeout=10)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Backend returned an invalid session payload")
    return payload


def get_session_by_id(session_id: str) -> dict[str, Any]:
    payload = request_backend_json(f"/orchestrator/sessions/by-id/{session_id}", timeout=10)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Backend returned an invalid session payload")
    return payload


def upsert_session(payload: dict[str, Any]) -> dict[str, Any]:
    response = request_backend_json(
        "/orchestrator/sessions/upsert",
        method="POST",
        payload=payload,
        timeout=15,
    )
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail="Backend returned an invalid session upsert response")
    return response
