from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from runtime_app.lib.backend_client import get_file_context, get_session_by_file, get_session_by_id, upsert_session
from runtime_app.lib.ids import normalize_scx_id
from runtime_app.lib.stell_ai_client import request_decision

app = FastAPI(title="Orchestra", version="1.0")

FALLBACK_CONFLICT_FLAG = "decision_fallback_used"
STATE_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
STATE_LABELS = {
    "S0": "Uploaded",
    "S1": "Converted",
    "S2": "Assembly Ready",
    "S3": "Analyzing",
    "S4": "DFM Ready",
    "S5": "Awaiting Approval",
    "S6": "Approved",
    "S7": "Share Ready",
}


class FileSyncIn(BaseModel):
    file_id: str = Field(min_length=4, max_length=64)


class SessionStartIn(BaseModel):
    file_id: str = Field(min_length=4, max_length=64)


class SessionInputIn(BaseModel):
    session_id: str
    key: str
    value: Any


class SessionActionIn(BaseModel):
    session_id: str


class ApprovalIn(BaseModel):
    session_id: str
    reason: str | None = Field(default=None, max_length=500)
    initiated_by: str | None = Field(default=None, max_length=128)


def _public_file_id(value: str) -> str:
    try:
        return normalize_scx_id(value)
    except ValueError:
        return value


def _normalize_state_code(state: str | None) -> str:
    token = str(state or "").strip().upper()
    return token if token in STATE_ORDER else "S0"


def _state_label(state: str | None) -> str:
    return STATE_LABELS.get(_normalize_state_code(state), "Uploaded")


def _submitted_inputs(session: dict[str, Any]) -> dict[str, Any]:
    raw = str(session.get("notes") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    values = payload.get("submitted_inputs")
    return values if isinstance(values, dict) else {}


def _set_submitted_input(session: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    payload = {"submitted_inputs": _submitted_inputs(session)}
    payload["submitted_inputs"][str(key)] = value
    session["notes"] = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return payload["submitted_inputs"]


def _risk_flags(decision_json: dict[str, Any] | None) -> list[str]:
    payload = decision_json if isinstance(decision_json, dict) else {}
    flags = payload.get("conflict_flags")
    if not isinstance(flags, list):
        return []
    seen: set[str] = set()
    items: list[str] = []
    for item in flags:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        items.append(token)
    return items


def _approval_required(decision_json: dict[str, Any] | None, dfm_findings: dict[str, Any] | None = None) -> bool:
    flags = _risk_flags(decision_json)
    if any(flag != FALLBACK_CONFLICT_FLAG for flag in flags):
        return True
    if isinstance(dfm_findings, dict):
        return str(dfm_findings.get("status_gate") or "").strip().upper() == "NEEDS_APPROVAL"
    return False


def _required_inputs(session: dict[str, Any], file_row: dict[str, Any], decision_json: dict[str, Any]) -> list[dict[str, Any]]:
    if str(file_row.get("status") or "").strip().lower() != "ready":
        return []
    submitted = _submitted_inputs(session)
    flags = _risk_flags(decision_json)
    items: list[dict[str, Any]] = []

    if "unknown_critical_geometry" in flags and "geometry_confirmation" not in submitted:
        items.append(
            {
                "key": "geometry_confirmation",
                "label": "Geometry confirmation",
                "input_type": "boolean",
                "required": True,
            }
        )

    if FALLBACK_CONFLICT_FLAG in flags and not str(submitted.get("manufacturing_intent") or "").strip():
        items.append(
            {
                "key": "manufacturing_intent",
                "label": "Manufacturing intent",
                "input_type": "text",
                "required": True,
            }
        )

    return items


def _dfm_findings(file_row: dict[str, Any]) -> dict[str, Any] | None:
    meta = file_row.get("meta") if isinstance(file_row.get("meta"), dict) else {}
    return meta.get("dfm_findings") if isinstance(meta.get("dfm_findings"), dict) else None


def _blocked_reasons(file_row: dict[str, Any], decision_json: dict[str, Any], required_inputs: list[dict[str, Any]]) -> list[dict[str, str]]:
    status = str(file_row.get("status") or "").strip().lower()
    reasons: list[dict[str, str]] = []
    if status == "failed":
        reasons.append({"code": "processing_failed", "message": "The file processing pipeline failed before Orchestra completed."})
    elif status != "ready":
        reasons.append({"code": "file_not_ready", "message": "The file is still processing and cannot advance yet."})

    if required_inputs:
        reasons.append({"code": "missing_required_inputs", "message": "Required inputs must be submitted before the workflow can continue."})
    elif _approval_required(decision_json, _dfm_findings(file_row)):
        reasons.append({"code": "approval_required", "message": "Approval is required before the workflow can finish."})
    return reasons


def _ensure_decision_json(file_row: dict[str, Any], session: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = file_row.get("meta") if isinstance(file_row.get("meta"), dict) else {}
    payload = session.get("decision_json") if session is not None and isinstance(session.get("decision_json"), dict) else None
    if not isinstance(payload, dict):
        payload = meta.get("decision_json") if isinstance(meta.get("decision_json"), dict) else None
    if not isinstance(payload, dict):
        payload = file_row.get("decision_json") if isinstance(file_row.get("decision_json"), dict) else None
    if not isinstance(payload, dict) or not payload.get("rule_version"):
        payload = request_decision(str(file_row.get("file_id") or ""))

    file_row["decision_json"] = payload
    file_row["meta"] = {
        **meta,
        "decision_json": payload,
        "rule_version": str(payload.get("rule_version") or meta.get("rule_version") or "v0.0"),
    }
    if session is not None:
        session["decision_json"] = payload
    return payload


def _apply_state(session: dict[str, Any], state: str, decision_json: dict[str, Any], dfm_findings: dict[str, Any] | None) -> dict[str, Any]:
    code = _normalize_state_code(state)
    session["state"] = code
    session["state_code"] = code
    session["state_label"] = _state_label(code)
    session["approval_required"] = _approval_required(decision_json, dfm_findings)
    session["status_gate"] = "NEEDS_APPROVAL" if session["approval_required"] else "PASS"
    session["risk_flags"] = _risk_flags(decision_json)
    session["decision_json"] = decision_json
    session["rule_version"] = str(decision_json.get("rule_version") or session.get("rule_version") or "v0.0")
    session["mode"] = str(decision_json.get("mode") or session.get("mode") or "visual_only")
    session["confidence"] = round(float(decision_json.get("confidence") or 0.0), 4)
    return session


def _transition_path(current: str, target: str) -> list[str]:
    raw_current = str(current or "").strip().upper()
    raw_target = str(target or "").strip().upper()
    # Reject unrecognized state codes explicitly; do not silently normalize to S0
    # so that corrupted or unknown session state is surfaced rather than hidden.
    if raw_current and raw_current not in STATE_ORDER:
        raise HTTPException(status_code=409, detail=f"invalid_current_state:{raw_current}")
    if raw_target and raw_target not in STATE_ORDER:
        raise HTTPException(status_code=409, detail=f"invalid_target_state:{raw_target}")
    current_code = _normalize_state_code(current)
    target_code = _normalize_state_code(target)
    if current_code == target_code:
        return [current_code]
    if current_code == "S5" and target_code == "S4":
        return ["S4"]
    current_index = STATE_ORDER.index(current_code)
    target_index = STATE_ORDER.index(target_code)
    if target_index < current_index:
        raise HTTPException(status_code=409, detail=f"invalid_state_transition:{current_code}->{target_code}")
    return STATE_ORDER[current_index + 1 : target_index + 1]


def _walk_to_state(session: dict[str, Any], target: str, decision_json: dict[str, Any], dfm_findings: dict[str, Any] | None) -> dict[str, Any]:
    for state in _transition_path(str(session.get("state") or "S0"), target):
        _apply_state(session, state, decision_json, dfm_findings)
    return session


def _serialize_session(file_row: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    decision_json = session.get("decision_json") if isinstance(session.get("decision_json"), dict) else {}
    return {
        "session_id": str(session.get("session_id") or ""),
        "file_id": _public_file_id(str(file_row.get("file_id") or "")),
        "state": session.get("state"),
        "state_label": session.get("state_label") or _state_label(str(session.get("state") or "S0")),
        "approval_required": bool(session.get("approval_required")),
        "risk_flags": _risk_flags(decision_json),
        "decision_json": decision_json,
    }


def _persist_session(file_row: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    return upsert_session(
        {
            "session_id": session.get("session_id"),
            "file_id": str(file_row.get("file_id") or ""),
            "state": str(session.get("state") or "S0"),
            "state_code": str(session.get("state_code") or session.get("state") or "S0"),
            "state_label": str(session.get("state_label") or _state_label(str(session.get("state") or "S0"))),
            "status_gate": str(session.get("status_gate") or "PASS"),
            "approval_required": bool(session.get("approval_required")),
            "rule_version": str(session.get("rule_version") or "v0.0"),
            "mode": str(session.get("mode") or "visual_only"),
            "confidence": float(session.get("confidence") or 0.0),
            "risk_flags": [str(item) for item in session.get("risk_flags") or [] if str(item or "").strip()],
            "decision_json": session.get("decision_json") if isinstance(session.get("decision_json"), dict) else {},
            "notes": session.get("notes"),
        }
    )


def _ensure_session(file_row: dict[str, Any]) -> dict[str, Any]:
    try:
        return get_session_by_file(str(file_row.get("file_id") or ""))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    session = {
        "file_id": str(file_row.get("file_id") or ""),
        "state": "S0",
        "state_code": "S0",
        "state_label": _state_label("S0"),
        "status_gate": "PROCESSING",
        "approval_required": False,
        "rule_version": str(file_row.get("rule_version") or "v0.0"),
        "mode": str(file_row.get("mode") or "visual_only"),
        "confidence": 0.0,
        "risk_flags": [],
        "decision_json": {},
        "notes": None,
    }
    return _persist_session(file_row, session)


def _sync_session(file_row: dict[str, Any]) -> dict[str, Any]:
    session = _ensure_session(file_row)
    decision_json = _ensure_decision_json(file_row, session=session)
    dfm_findings = _dfm_findings(file_row)
    current = _normalize_state_code(str(session.get("state") or "S0"))
    status = str(file_row.get("status") or "").strip().lower()

    if status != "ready":
        if current not in {"S6", "S7"}:
            _apply_state(session, "S0", decision_json, dfm_findings)
        session["status_gate"] = "FAILED" if status == "failed" else "PROCESSING"
        return _persist_session(file_row, session)

    required = _required_inputs(session, file_row, decision_json)
    if required:
        target = "S4"
        status_gate = "NEEDS_INPUT"
    elif _approval_required(decision_json, dfm_findings):
        target = "S5"
        status_gate = "NEEDS_APPROVAL"
    elif bool(file_row.get("active_share_exists")):
        target = "S7"
        status_gate = "PASS"
    else:
        target = "S6"
        status_gate = "PASS"

    _walk_to_state(session, target, decision_json, dfm_findings)
    session["status_gate"] = status_gate
    return _persist_session(file_row, session)


def _validate_input_value(key: str, value: Any) -> None:
    """Enforce type contracts for known required-input keys.

    Only validates keys with a defined type contract; unknown keys pass through
    so that future inputs added to _required_inputs do not silently break here.
    Raises 422 on type mismatch so callers get a clear rejection rather than
    silently storing a value that will never satisfy the completion check.
    """
    if key == "geometry_confirmation":
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_input_type", "key": key, "message": "geometry_confirmation must be a boolean"},
            )
    elif key == "manufacturing_intent":
        if not isinstance(value, str) or not str(value).strip():
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_input_type", "key": key, "message": "manufacturing_intent must be a non-empty string"},
            )


def _required_inputs_payload(file_row: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    decision_json = session.get("decision_json") if isinstance(session.get("decision_json"), dict) else {}
    required = _required_inputs(session, file_row, decision_json)
    return {
        "session_id": str(session.get("session_id") or ""),
        "file_id": _public_file_id(str(file_row.get("file_id") or "")),
        "required_inputs": required,
        "submitted_inputs": _submitted_inputs(session),
        "blocked_reasons": _blocked_reasons(file_row, decision_json, required),
    }


@app.get("/health")
def health():
    return {"status": "OK", "service": "Orchestra"}


@app.post("/files/sync")
def sync_file(data: FileSyncIn):
    file_row = get_file_context(data.file_id, include_assembly_tree=False)
    session = _sync_session(file_row)
    return _serialize_session(file_row, session)


@app.post("/sessions/start")
def start_session(data: SessionStartIn):
    return sync_file(FileSyncIn(file_id=data.file_id))


@app.get("/sessions/decision")
def get_decision(file_id: str | None = None, session_id: str | None = None):
    if not file_id and not session_id:
        raise HTTPException(status_code=400, detail="file_id or session_id is required")
    if session_id:
        session = get_session_by_id(session_id)
        fid = str(session.get("file_id") or "").strip()
        if not fid:
            raise HTTPException(status_code=422, detail="session has no associated file_id")
        file_row = get_file_context(fid, include_assembly_tree=False)
    else:
        file_row = get_file_context(str(file_id), include_assembly_tree=False)
    session = _sync_session(file_row)
    return _serialize_session(file_row, session)


@app.get("/sessions/required-inputs")
def get_required_inputs(session_id: str):
    session = get_session_by_id(session_id)
    file_row = get_file_context(str(session.get("file_id") or ""), include_assembly_tree=False)
    session = _sync_session(file_row)
    return _required_inputs_payload(file_row, session)


@app.post("/sessions/input")
def submit_input(data: SessionInputIn):
    session = get_session_by_id(data.session_id)
    file_row = get_file_context(str(session.get("file_id") or ""), include_assembly_tree=False)
    session = _sync_session(file_row)
    allowed_keys = {item["key"] for item in _required_inputs(session, file_row, session.get("decision_json") or {})}
    if data.key not in allowed_keys:
        raise HTTPException(
            status_code=409,
            detail={"code": "input_not_required", "message": "The input key is not required for the current session state."},
        )
    _validate_input_value(data.key, data.value)
    submitted = _set_submitted_input(session, data.key, data.value)
    session = _persist_session(file_row, session)
    session = _sync_session(file_row)
    return {
        "session_id": str(session.get("session_id") or ""),
        "file_id": _public_file_id(str(file_row.get("file_id") or "")),
        "state": session.get("state"),
        "state_label": session.get("state_label") or _state_label(str(session.get("state") or "S0")),
        "accepted": True,
        "submitted_inputs": submitted,
        "required_inputs": _required_inputs(session, file_row, session.get("decision_json") or {}),
    }


@app.post("/sessions/advance")
def advance_session(data: SessionActionIn):
    session = get_session_by_id(data.session_id)
    file_row = get_file_context(str(session.get("file_id") or ""), include_assembly_tree=False)
    session = _sync_session(file_row)
    required_payload = _required_inputs_payload(file_row, session)
    if required_payload["required_inputs"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_required_inputs",
                "message": "Required inputs must be submitted before advancing.",
                **required_payload,
            },
        )
    decision_json = session.get("decision_json") if isinstance(session.get("decision_json"), dict) else {}
    if _approval_required(decision_json, _dfm_findings(file_row)):
        target = "S5"
    elif bool(file_row.get("active_share_exists")):
        target = "S7"
    else:
        target = "S6"
    _walk_to_state(session, target, decision_json, _dfm_findings(file_row))
    session = _persist_session(file_row, session)
    return {
        "session_id": str(session.get("session_id") or ""),
        "file_id": _public_file_id(str(file_row.get("file_id") or "")),
        "state": session.get("state"),
        "state_label": session.get("state_label") or _state_label(str(session.get("state") or "S0")),
        "advanced": True,
        "decision_json": decision_json,
        "required_inputs": _required_inputs(session, file_row, decision_json),
        "blocked_reasons": [],
    }


@app.post("/sessions/approve")
def approve(data: ApprovalIn):
    session = get_session_by_id(data.session_id)
    file_row = get_file_context(str(session.get("file_id") or ""), include_assembly_tree=False)
    session = _sync_session(file_row)
    if _normalize_state_code(str(session.get("state") or "S0")) != "S5":
        raise HTTPException(status_code=409, detail="Approval is only valid from S5")
    decision_json = session.get("decision_json") if isinstance(session.get("decision_json"), dict) else {}
    _walk_to_state(session, "S6", decision_json, _dfm_findings(file_row))
    reason = str(data.reason or "").strip() or None
    initiator = str(data.initiated_by or "").strip() or None
    if reason or initiator:
        submitted = _submitted_inputs(session)
        if reason:
            submitted["approval_reason"] = reason
        if initiator:
            submitted["approval_initiator"] = initiator
        session["notes"] = json.dumps({"submitted_inputs": submitted}, ensure_ascii=True, sort_keys=True)
    session = _persist_session(file_row, session)
    return {
        "session_id": str(session.get("session_id") or ""),
        "file_id": _public_file_id(str(file_row.get("file_id") or "")),
        "state": session.get("state"),
        "state_label": session.get("state_label") or _state_label(str(session.get("state") or "S0")),
        "approved": True,
        "reason": reason,
        "initiated_by": initiator,
    }
