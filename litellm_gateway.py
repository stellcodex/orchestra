from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import httpx
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse

CONFIG_PATH = Path(os.getenv("LITELLM_CONFIG_PATH", "/app/config/litellm.config.yaml"))
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("LITELLM_GATEWAY_TIMEOUT_SECONDS", "20"))
MAX_LOCAL_TOKENS = int(os.getenv("LITELLM_GATEWAY_MAX_LOCAL_TOKENS", "32"))

app = FastAPI(title="LiteLLM Local Gateway", version="1.0.0")
MODEL_MAP: Dict[str, Dict[str, Any]] = {}


def _load_config() -> Dict[str, Dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return {}
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    model_list = raw.get("model_list", [])
    if not isinstance(model_list, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for item in model_list:
        if not isinstance(item, dict):
            continue
        model_name = str(item.get("model_name", "")).strip()
        params = item.get("litellm_params", {})
        if not model_name or not isinstance(params, dict):
            continue
        out[model_name] = params
    return out


def _is_local_model(params: Dict[str, Any]) -> bool:
    model = str(params.get("model", "")).strip()
    return model.startswith("ollama/")


def _build_local_request(alias: str, params: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    model = str(params.get("model", "")).strip()
    ollama_model = model.split("/", 1)[1] if "/" in model else model
    requested = payload.get("max_tokens", payload.get("max_completion_tokens", 16))
    try:
        max_tokens = max(1, min(int(requested), MAX_LOCAL_TOKENS))
    except Exception:
        max_tokens = min(16, MAX_LOCAL_TOKENS)
    return {
        "model": ollama_model,
        "messages": payload.get("messages", []),
        "temperature": payload.get("temperature", 0),
        "max_tokens": max_tokens,
    }


def _local_base_url(params: Dict[str, Any]) -> str:
    base = str(params.get("api_base", "http://orchestra_ollama:11434")).rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _synthetic_completion(alias: str, payload: Dict[str, Any], note: str) -> Dict[str, Any]:
    messages = payload.get("messages", [])
    prompt_preview = ""
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            prompt_preview = str(last.get("content", "")).strip().replace("\n", " ")
    prompt_preview = prompt_preview[:120]
    text = f"{alias} local fallback: {note}. {prompt_preview}".strip()
    return {
        "id": "chatcmpl-local-fallback",
        "object": "chat.completion",
        "created": 1677610602,
        "model": alias,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": max(1, len(text.split())), "total_tokens": 0},
    }


@app.on_event("startup")
async def startup() -> None:
    global MODEL_MAP
    MODEL_MAP = _load_config()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "models": sorted(MODEL_MAP.keys())}


@app.get("/v1/models")
async def models() -> Dict[str, Any]:
    data = [
        {"id": model_name, "object": "model", "created": 1677610602, "owned_by": "openai"}
        for model_name in sorted(MODEL_MAP.keys())
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(payload: Dict[str, Any]) -> JSONResponse:
    alias = str(payload.get("model", "")).strip()
    if not alias or alias not in MODEL_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"Unknown model alias: {alias}", "type": "invalid_model", "code": "404"}},
        )

    params = MODEL_MAP[alias]
    if not _is_local_model(params):
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"Provider for model '{alias}' is unavailable in local mode.",
                    "type": "provider_unavailable",
                    "code": "429",
                }
            },
        )

    target_payload = _build_local_request(alias, params, payload)
    target_url = f"{_local_base_url(params)}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                target_url,
                json=target_payload,
                headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
            )
    except Exception:
        return JSONResponse(status_code=200, content=_synthetic_completion(alias, payload, "backend_unreachable"))

    if response.status_code >= 400:
        return JSONResponse(status_code=200, content=_synthetic_completion(alias, payload, "backend_error"))

    try:
        parsed = response.json()
    except Exception:
        return JSONResponse(status_code=200, content=_synthetic_completion(alias, payload, "backend_invalid_json"))

    return JSONResponse(status_code=200, content=parsed)
