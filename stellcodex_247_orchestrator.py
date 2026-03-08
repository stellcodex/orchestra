#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest


VERSION = "2026.03.06.1"
DEFAULT_WORKSPACE = Path("/root/workspace")
DEFAULT_OUTPUT = Path("/tmp/stellcodex_output")
DEFAULT_BACKUP_REMOTE = "gdrive:stellcodex/02_backups/orchestra_247"
CATALOG_PATH = DEFAULT_WORKSPACE / "stellcodex_v7" / "marketplace" / "catalog.json"
MODULES_PATH = DEFAULT_WORKSPACE / "ops" / "orchestra" / "state" / "stellcodex_modules_45.json"
SSOT_FILES = [
    DEFAULT_WORKSPACE / "V7_RELEASE_45_APP_FACTORY_PROMPT.md",
    DEFAULT_WORKSPACE / "_truth" / "UI_FLOW_SSOT.md",
    DEFAULT_WORKSPACE / "_truth" / "DEPLOYMENT_TOPOLOGY_SSOT.md",
    DEFAULT_WORKSPACE / "_truth" / "BACKUP_AND_DRIVE_SYNC_SSOT.md",
]

MODEL_KEY_REQUIREMENTS = {
    "gemini_conductor": "GEMINI_API_KEY",
    "codex_executor": "OPENAI_API_KEY",
    "claude_reviewer": "ANTHROPIC_API_KEY",
    "abacus_analyst": "ABACUSAI_API_KEY",
}

# 5-hour windows for paid models. Local models are effectively unlimited.
MODEL_CAPACITY = {
    "gemini_conductor": 1000,
    "codex_executor": 500,
    "claude_reviewer": 400,
    "abacus_analyst": 800,
    "local_fast": 1_000_000_000,
    "local_reason": 1_000_000_000,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(ts: Optional[datetime] = None) -> str:
    current = ts or utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    tmp.replace(path)


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def run_cmd(cmd: List[str], *, timeout: int = 120, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> Tuple[int, Dict[str, Any]]:
    body = None
    req_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urlrequest.Request(url=url, method=method, data=body, headers=req_headers)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read().decode("utf-8", errors="ignore")
            parsed = json.loads(raw) if raw.strip() else {}
            if not isinstance(parsed, dict):
                parsed = {"raw": parsed}
            return status, parsed
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {"error": raw or str(exc)}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"error": str(parsed)}
    except Exception as exc:  # pragma: no cover
        return 0, {"error": str(exc)}


@dataclass
class ModuleSpec:
    id: str
    slug: str
    name: str
    category: str
    tier: str
    enabled_by_default: bool
    routes: List[str]
    required_capabilities: List[str]
    supported_formats: List[str]


class Stellcodex247Orchestrator:
    def __init__(self, *, output_root: Path, workspace_root: Path, interval_seconds: int) -> None:
        self.output_root = output_root
        self.workspace_root = workspace_root
        self.interval_seconds = interval_seconds

        self.evidence_root = self.output_root / "evidence"
        self.backups_root = self.output_root / "backups"
        self.tmp_root = self.output_root / "tmp"

        self.report_path = self.output_root / "REPORT.md"
        self.test_results_path = self.output_root / "test_results.json"
        self.errors_log = self.output_root / "errors.log"
        self.limits_log = self.output_root / "limits.log"
        self.run_log = self.output_root / "orchestrator.log"
        self.state_path = self.output_root / "orchestrator_state.json"
        self.limit_state_path = self.output_root / "limits_state.json"
        self.backup_status_path = self.output_root / "backup_status.json"

        self.orchestra_url = os.getenv("ORCHESTRA_URL", "http://localhost:7010").rstrip("/")
        self.litellm_url = os.getenv("LITELLM_URL", "http://localhost:4000/v1").rstrip("/")
        self.llm_api_key = os.getenv("LLM_API_KEY", "dummy")
        self.limit_low_threshold = float(os.getenv("LIMIT_LOW_THRESHOLD", "0.10"))
        self.limit_window_hours = int(os.getenv("LIMIT_WINDOW_HOURS", "5"))
        self.enable_real_llm_calls = os.getenv("ENABLE_REAL_LLM_CALLS", "1") in {"1", "true", "TRUE", "yes", "YES"}
        self.self_update_mode = "enabled:auto-sync-on-each-cycle"
        self.backup_remote = (os.getenv("ORCHESTRATOR_BACKUP_REMOTE", DEFAULT_BACKUP_REMOTE) or DEFAULT_BACKUP_REMOTE).strip()

        self.model_order = ["gemini_conductor", "codex_executor", "local_fast", "local_reason"]
        self.run_started_at = to_iso()
        self.latest_smoke_result: Dict[str, Any] = {}
        self.latest_backup_results: List[Dict[str, Any]] = []
        self.last_state_payload: Dict[str, Any] = {}

    def log(self, message: str) -> None:
        append_line(self.run_log, f"[{to_iso()}] {message}")

    def log_error(self, message: str) -> None:
        append_line(self.errors_log, f"[{to_iso()}] {message}")
        self.log(f"ERROR: {message}")

    def ensure_dirs(self) -> None:
        for path in [self.output_root, self.evidence_root, self.backups_root, self.tmp_root]:
            path.mkdir(parents=True, exist_ok=True)

    def checkpoint(self, summary: str, next_step: str = "") -> None:
        script = self.workspace_root / "scripts" / "progress_checkpoint.py"
        if not script.exists():
            return
        cmd = [
            "python3",
            str(script),
            "--agent",
            "stellcodex-247-orchestrator",
            "--task",
            "7x24-runtime",
            "--status",
            "running",
            "--summary",
            summary,
            "--next-step",
            next_step,
            "--artifact",
            str(self.report_path),
            "--artifact",
            str(self.test_results_path),
        ]
        code, _, err = run_cmd(cmd, timeout=30)
        if code != 0:
            self.log_error(f"checkpoint_failed: {err.strip() or 'unknown'}")

    def load_modules(self) -> List[ModuleSpec]:
        payload = read_json(MODULES_PATH, [])
        modules: List[ModuleSpec] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                modules.append(
                    ModuleSpec(
                        id=str(row.get("id", "")),
                        slug=str(row.get("slug", "")),
                        name=str(row.get("name", "")),
                        category=str(row.get("category", "general")),
                        tier=str(row.get("tier", "paid")).lower(),
                        enabled_by_default=bool(row.get("enabled_by_default", False)),
                        routes=[str(x) for x in row.get("routes", []) if isinstance(x, str)],
                        required_capabilities=[
                            str(x) for x in row.get("required_capabilities", []) if isinstance(x, str)
                        ],
                        supported_formats=[str(x) for x in row.get("supported_formats", []) if isinstance(x, str)],
                    )
                )
            except Exception:
                continue
        return modules

    def sync_catalog(self, modules: List[ModuleSpec]) -> None:
        catalog_rows = []
        for module in modules:
            is_free = module.tier == "free"
            catalog_rows.append(
                {
                    "id": module.id,
                    "slug": module.slug,
                    "name": module.name,
                    "category": module.category,
                    "tier": "free" if is_free else "paid",
                    "enabled_by_default": bool(is_free),
                    "routes": module.routes,
                    "required_capabilities": module.required_capabilities,
                    "supported_formats": module.supported_formats,
                }
            )
        write_json(CATALOG_PATH, catalog_rows)
        self.log(f"catalog_synced count={len(catalog_rows)} path={CATALOG_PATH}")

    def check_ssot(self) -> Dict[str, Any]:
        found = [str(path) for path in SSOT_FILES if path.exists()]
        missing = [str(path) for path in SSOT_FILES if not path.exists()]
        check = {
            "checked_at": to_iso(),
            "found": found,
            "missing": missing,
            "conflicts": [],
        }
        if missing:
            check["conflicts"].append("required_source_missing")
        return check

    def _init_limit_state(self) -> Dict[str, Any]:
        now = utc_now()
        window_ends = now + timedelta(hours=self.limit_window_hours)
        models = {}
        for model, capacity in MODEL_CAPACITY.items():
            models[model] = {
                "capacity": int(capacity),
                "used": 0,
                "window_started_at": to_iso(now),
                "window_ends_at": to_iso(window_ends),
                "remaining_ratio": 1.0,
                "last_call_at": None,
                "last_status": "ready",
            }
        return {"updated_at": to_iso(now), "window_hours": self.limit_window_hours, "models": models}

    def load_limit_state(self) -> Dict[str, Any]:
        state = read_json(self.limit_state_path, {})
        if not isinstance(state, dict) or "models" not in state:
            state = self._init_limit_state()
        state.setdefault("models", {})
        for model, capacity in MODEL_CAPACITY.items():
            state["models"].setdefault(
                model,
                {
                    "capacity": int(capacity),
                    "used": 0,
                    "window_started_at": to_iso(),
                    "window_ends_at": to_iso(utc_now() + timedelta(hours=self.limit_window_hours)),
                    "remaining_ratio": 1.0,
                    "last_call_at": None,
                    "last_status": "ready",
                },
            )
        self._refresh_limit_windows(state)
        return state

    def _refresh_limit_windows(self, state: Dict[str, Any]) -> None:
        now = utc_now()
        for model_state in state.get("models", {}).values():
            ends_at = parse_iso(str(model_state.get("window_ends_at")))
            if ends_at and now <= ends_at:
                continue
            capacity = int(model_state.get("capacity", 1))
            model_state["used"] = 0
            model_state["window_started_at"] = to_iso(now)
            model_state["window_ends_at"] = to_iso(now + timedelta(hours=self.limit_window_hours))
            model_state["remaining_ratio"] = 1.0
            model_state["last_status"] = "window_reset"
            model_state["capacity"] = capacity
        state["updated_at"] = to_iso(now)

    def save_limit_state(self, state: Dict[str, Any]) -> None:
        write_json(self.limit_state_path, state)

    def model_key_available(self, model: str) -> bool:
        key_name = MODEL_KEY_REQUIREMENTS.get(model)
        if not key_name:
            return True
        value = os.getenv(key_name, "").strip()
        return bool(value and value.lower() not in {"dummy", "none", "null"})

    def remaining_ratio(self, state: Dict[str, Any], model: str) -> float:
        model_state = state.get("models", {}).get(model, {})
        capacity = max(int(model_state.get("capacity", 1)), 1)
        used = max(int(model_state.get("used", 0)), 0)
        remaining = max(capacity - used, 0)
        ratio = remaining / capacity
        model_state["remaining_ratio"] = round(ratio, 6)
        return float(model_state["remaining_ratio"])

    def select_model(self, state: Dict[str, Any], tier: str) -> Optional[str]:
        ordered = list(self.model_order)
        if tier == "free":
            # Prefer widest availability for continuous mode.
            ordered = ["local_fast", "local_reason", "gemini_conductor", "codex_executor"]
        for model in ordered:
            if not self.model_key_available(model):
                continue
            ratio = self.remaining_ratio(state, model)
            if ratio <= 0:
                continue
            if ratio < self.limit_low_threshold:
                continue
            return model
        for model in ordered:
            if model in {"local_fast", "local_reason"}:
                return model
        return None

    def record_limit_event(
        self,
        *,
        model: str,
        status: str,
        switched_to: Optional[str],
        detail: str,
        state: Dict[str, Any],
    ) -> None:
        ratio = self.remaining_ratio(state, model)
        entry = {
            "ts": to_iso(),
            "model": model,
            "remaining_ratio": ratio,
            "status": status,
            "switched_to": switched_to,
            "detail": detail,
        }
        append_line(self.limits_log, json.dumps(entry, ensure_ascii=True))

    def consume_limit(self, state: Dict[str, Any], model: str) -> None:
        model_state = state.get("models", {}).get(model, {})
        if not model_state:
            return
        model_state["used"] = int(model_state.get("used", 0)) + 1
        model_state["last_call_at"] = to_iso()
        ratio = self.remaining_ratio(state, model)
        model_state["last_status"] = "low" if ratio < self.limit_low_threshold else "ready"
        self.record_limit_event(
            model=model,
            status=model_state["last_status"],
            switched_to=None,
            detail="llm_call",
            state=state,
        )

    def call_llm_ping(self, model: str, prompt: str) -> Tuple[bool, str]:
        if not self.enable_real_llm_calls:
            return True, "simulated_llm_response"
        status, data = http_json(
            f"{self.litellm_url}/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {self.llm_api_key}"},
            payload={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 24,
            },
            timeout=15,
        )
        if status >= 400 or status == 0:
            return False, str(data.get("error") or data.get("detail") or f"http_{status}")
        try:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("text")
                )
            return True, str(content).strip()[:300]
        except Exception:
            return True, "ok"

    def run_module(self, module: ModuleSpec, state: Dict[str, Any]) -> Dict[str, Any]:
        started = to_iso()
        selected = self.select_model(state, module.tier)
        fallback = None
        llm_ok = False
        llm_output = ""
        status = "completed"

        if not selected:
            status = "deferred_limit"
            llm_output = "no_model_available"
        else:
            llm_ok, llm_output = self.call_llm_ping(
                selected, f"Module {module.slug} runtime check. Reply with a short OK summary."
            )
            self.consume_limit(state, selected)
            if not llm_ok and selected not in {"local_fast", "local_reason"}:
                fallback = self.select_model(state, "free")
                if fallback:
                    self.record_limit_event(
                        model=selected,
                        status="switch",
                        switched_to=fallback,
                        detail=f"fallback_due_to_error:{llm_output}",
                        state=state,
                    )
                    llm_ok, llm_output = self.call_llm_ping(
                        fallback,
                        f"Fallback check for module {module.slug}. Reply with short status.",
                    )
                    self.consume_limit(state, fallback)
            if not llm_ok:
                status = "degraded"

        module_tmp_dir = self.tmp_root / f"{module.slug}_{int(time.time())}"
        module_tmp_dir.mkdir(parents=True, exist_ok=True)
        artifact_tmp = module_tmp_dir / "artifact.json"
        log_tmp = module_tmp_dir / "run.log"

        artifact_payload = {
            "id": module.id,
            "slug": module.slug,
            "name": module.name,
            "tier": module.tier,
            "enabled": module.tier == "free",
            "status": status,
            "llm_model": selected,
            "llm_fallback_model": fallback,
            "llm_ok": llm_ok,
            "llm_output_excerpt": llm_output[:200],
            "started_at": started,
            "ended_at": to_iso(),
        }
        write_json(artifact_tmp, artifact_payload)
        append_line(log_tmp, f"[{to_iso()}] module={module.slug} status={status} model={selected} fallback={fallback}")
        append_line(log_tmp, f"[{to_iso()}] llm_ok={llm_ok} output={llm_output}")

        evidence_dir = self.evidence_root / f"APP_{module.slug}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if artifact_tmp.exists() and log_tmp.exists():
            shutil.move(str(artifact_tmp), str(evidence_dir / "artifact.json"))
            shutil.move(str(log_tmp), str(evidence_dir / "module.log"))
        shutil.rmtree(module_tmp_dir, ignore_errors=True)
        return artifact_payload

    def backup_output(self, phase: str) -> Dict[str, Any]:
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"backup_{timestamp}.zip"
        zip_path = self.backups_root / zip_name
        self.backups_root.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in self.output_root.rglob("*"):
                if path == zip_path or path.is_dir():
                    continue
                if self.backups_root in path.parents:
                    continue
                arcname = path.relative_to(self.output_root)
                archive.write(path, arcname.as_posix())

        remote_object = f"{self.backup_remote}/{zip_name}"
        upload_status = {
            "uploaded": False,
            "remote": self.backup_remote,
            "detail": "not_attempted",
            "checksum": {"local_md5": "", "remote_md5": "", "matched": False},
        }
        rclone = shutil.which("rclone")
        if rclone:
            _, _, _ = run_cmd([rclone, "mkdir", self.backup_remote], timeout=60)
            code, _, err = run_cmd([rclone, "copyto", str(zip_path), remote_object], timeout=1800)
            if code == 0:
                local_md5 = md5_file(zip_path)
                md5_code, md5_out, md5_err = run_cmd([rclone, "md5sum", remote_object], timeout=180)
                remote_md5 = md5_out.split()[0] if md5_out.strip() else ""
                checksum_matched = bool(md5_code == 0 and remote_md5 and local_md5 == remote_md5)
                if checksum_matched:
                    upload_status = {
                        "uploaded": True,
                        "remote": self.backup_remote,
                        "detail": "ok",
                        "checksum": {"local_md5": local_md5, "remote_md5": remote_md5, "matched": True},
                    }
                    zip_path.unlink(missing_ok=True)
                else:
                    detail = (md5_err.strip() or "checksum_mismatch")[:500]
                    upload_status = {
                        "uploaded": False,
                        "remote": self.backup_remote,
                        "detail": detail,
                        "checksum": {"local_md5": local_md5, "remote_md5": remote_md5, "matched": False},
                    }
                    self.log_error(
                        f"drive_upload_checksum_failed phase={phase} local_md5={local_md5} remote_md5={remote_md5 or 'missing'} detail={detail}"
                    )
            else:
                upload_status = {
                    "uploaded": False,
                    "remote": self.backup_remote,
                    "detail": (err.strip() or "copy_failed")[:500],
                    "checksum": {"local_md5": "", "remote_md5": "", "matched": False},
                }
                self.log_error(f"drive_upload_failed phase={phase} detail={upload_status['detail']}")
        else:
            upload_status = {
                "uploaded": False,
                "remote": self.backup_remote,
                "detail": "rclone_missing",
                "checksum": {"local_md5": "", "remote_md5": "", "matched": False},
            }
            self.log_error("drive_upload_skipped rclone_missing")

        result = {
            "phase": phase,
            "created_at": to_iso(),
            "zip_path": str(zip_path),
            "zip_name": zip_name,
            "upload": upload_status,
        }
        self.latest_backup_results.append(result)
        write_json(self.backup_status_path, {"updated_at": to_iso(), "items": self.latest_backup_results})
        return result

    def run_smoke(self) -> Dict[str, Any]:
        smoke_script = self.workspace_root / "stellcodex_v7" / "infrastructure" / "deploy" / "scripts" / "smoke_v7.sh"
        smoke_evidence = self.evidence_root / f"smoke_{utc_now().strftime('%Y%m%d_%H%M%S')}"
        smoke_evidence.mkdir(parents=True, exist_ok=True)
        stdout_path = smoke_evidence / "smoke_stdout.log"
        stderr_path = smoke_evidence / "smoke_stderr.log"

        if not smoke_script.exists():
            return {
                "status": "skipped",
                "reason": "smoke_script_missing",
                "evidence_dir": str(smoke_evidence),
                "checked_at": to_iso(),
            }

        env = os.environ.copy()
        env["EVIDENCE_DIR"] = str(smoke_evidence)
        env.setdefault("API_ORIGIN", "http://localhost:18000")
        cmd = ["bash", str(smoke_script)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            text=True,
            env=env,
            check=False,
        )
        stdout_path.write_text(proc.stdout or "", encoding="utf-8")
        stderr_path.write_text(proc.stderr or "", encoding="utf-8")

        summary_path = smoke_evidence / "smoke" / "summary.json"
        summary = read_json(summary_path, {})
        status = "passed" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            self.log_error(f"smoke_failed code={proc.returncode}")
        return {
            "status": status,
            "exit_code": proc.returncode,
            "evidence_dir": str(smoke_evidence),
            "summary": summary if isinstance(summary, dict) else {},
            "checked_at": to_iso(),
        }

    def run_windows_hook(self) -> Dict[str, Any]:
        host = os.getenv("STELLCODEX_WINDOWS_HOST", "").strip()
        if not host:
            return {"status": "skipped", "reason": "windows_host_not_configured"}
        ssh = shutil.which("ssh")
        if not ssh:
            return {"status": "skipped", "reason": "ssh_missing"}
        command = "powershell -NoProfile -Command \"New-Item -ItemType Directory -Path 'C:\\\\Stellcodex\\\\Output' -Force | Out-Null\""
        code, out, err = run_cmd([ssh, host, command], timeout=40)
        if code != 0:
            self.log_error(f"windows_hook_failed host={host} err={err.strip()}")
            return {"status": "failed", "host": host, "error": (err or out).strip()[:500]}
        return {"status": "ok", "host": host}

    def sample_evidence_files(self) -> List[str]:
        files = []
        for path in sorted(self.evidence_root.rglob("*")):
            if path.is_file():
                files.append(str(path))
            if len(files) >= 2:
                break
        return files

    def compose_report(
        self,
        *,
        ssot_check: Dict[str, Any],
        modules: List[ModuleSpec],
        run_results: Dict[str, Any],
        smoke_result: Dict[str, Any],
        windows_result: Dict[str, Any],
        planning_engine: Dict[str, Any],
    ) -> None:
        free_count = sum(1 for module in modules if module.tier == "free")
        paid_count = sum(1 for module in modules if module.tier != "free")
        evidence_samples = self.sample_evidence_files()

        test_payload = {
            "generated_at": to_iso(),
            "version": VERSION,
            "output_root": str(self.output_root),
            "self_update": {
                "status": "enabled",
                "mode": self.self_update_mode,
                "detail": "Each cycle re-syncs module inventory, catalog, limits, and reports from source files.",
            },
            "ssot_check": ssot_check,
            "modules": {
                "total": len(modules),
                "free": free_count,
                "paid": paid_count,
                "completed_free": run_results.get("completed_free", 0),
                "completed_paid": run_results.get("completed_paid", 0),
                "pending_free": run_results.get("pending_free", []),
                "pending_paid": run_results.get("pending_paid", []),
            },
            "planning_engine": planning_engine,
            "limits": read_json(self.limit_state_path, {}),
            "backups": self.latest_backup_results,
            "smoke_test": smoke_result,
            "windows_integration": windows_result,
            "evidence_samples": evidence_samples,
        }
        write_json(self.test_results_path, test_payload)

        lines = [
            "# STELLCODEX 7/24 ORCHESTRATION REPORT",
            "",
            f"- generated_at: {to_iso()}",
            f"- version: {VERSION}",
            f"- output_root: {self.output_root}",
            "",
            "## Source Validation",
            f"- V7_RELEASE_45_APP_FACTORY_PROMPT.md: {'found' if str(SSOT_FILES[0]) in ssot_check.get('found', []) else 'missing'}",
            f"- SSOT files found: {len(ssot_check.get('found', []))}",
            f"- SSOT files missing: {len(ssot_check.get('missing', []))}",
            "",
            "## Module Inventory",
            f"- total: {len(modules)}",
            f"- free(enabled): {free_count}",
            f"- paid(disabled default): {paid_count}",
            f"- completed free: {run_results.get('completed_free', 0)}",
            f"- completed paid: {run_results.get('completed_paid', 0)}",
            f"- pending free: {len(run_results.get('pending_free', []))}",
            f"- pending paid: {len(run_results.get('pending_paid', []))}",
            "",
            "## Planning Engine",
            f"- primary: {planning_engine.get('primary')}",
            f"- fallback_order: {', '.join(planning_engine.get('fallback_order', []))}",
            "",
            "## Self Update",
            "- status: enabled",
            f"- mode: {self.self_update_mode}",
            "- detail: Every cycle re-syncs module inventory and rewrites runtime outputs from source of truth files.",
            "",
            "## Smoke Test",
            f"- status: {smoke_result.get('status')}",
            f"- evidence_dir: {smoke_result.get('evidence_dir')}",
            f"- share_resolve_http: {smoke_result.get('summary', {}).get('share_resolve_http')}",
            f"- share_expire_http: {smoke_result.get('summary', {}).get('share_expire_http')}",
            f"- share_rate_limit_http: {smoke_result.get('summary', {}).get('share_rate_limit_http')}",
            "",
            "## Backup",
            f"- backup_count: {len(self.latest_backup_results)}",
        ]
        for item in self.latest_backup_results:
            lines.append(
                f"- {item.get('phase')}: {item.get('zip_name')} upload={item.get('upload', {}).get('detail')}"
            )
        lines.extend(
            [
                "",
                "## Windows Integration",
                f"- status: {windows_result.get('status')}",
                f"- detail: {windows_result.get('reason') or windows_result.get('host') or windows_result.get('error')}",
                "",
                "## Evidence Samples",
            ]
        )
        if evidence_samples:
            lines.extend([f"- {path}" for path in evidence_samples])
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## Interaction",
                'Stellcodex raporu hazir, gormek istediginiz modul veya cikti var mi?',
            ]
        )

        self.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_or_init_state(self, modules: List[ModuleSpec]) -> Dict[str, Any]:
        state = read_json(self.state_path, {})
        if isinstance(state, dict) and isinstance(state.get("active_run"), dict):
            return state
        free = [module.slug for module in modules if module.tier == "free"]
        paid = [module.slug for module in modules if module.tier != "free"]
        return {
            "version": VERSION,
            "created_at": to_iso(),
            "active_run": {
                "id": utc_now().strftime("run_%Y%m%d_%H%M%S"),
                "started_at": to_iso(),
                "phase": "free",
                "free_pending": free,
                "paid_pending": paid,
                "results": {},
            },
            "history": [],
        }

    def _planning_engine_snapshot(self, state: Dict[str, Any]) -> Dict[str, Any]:
        primary = self.select_model(state, "paid") or "local_fast"
        fallback = [model for model in self.model_order if model != primary]
        return {"primary": primary, "fallback_order": fallback, "generated_at": to_iso()}

    def run_once(self) -> int:
        self.ensure_dirs()
        load_env_file(self.workspace_root / "ops" / "orchestra" / ".env")
        self.log(f"run_started version={VERSION}")

        modules = self.load_modules()
        if len(modules) != 45:
            self.log_error(f"module_count_unexpected count={len(modules)}")

        self.sync_catalog(modules)
        ssot_check = self.check_ssot()
        limit_state = self.load_limit_state()
        run_state = self._load_or_init_state(modules)
        active = run_state.get("active_run", {})
        module_by_slug = {module.slug: module for module in modules}
        planning_engine = self._planning_engine_snapshot(limit_state)

        self.checkpoint(
            summary=f"Run basladi. Modul sayisi={len(modules)}. Primary model={planning_engine['primary']}.",
            next_step="Ucretsiz moduller ve sonra ucretli moduller islenecek.",
        )

        while active.get("free_pending"):
            slug = active["free_pending"][0]
            module = module_by_slug.get(slug)
            if not module:
                active["free_pending"].pop(0)
                continue
            result = self.run_module(module, limit_state)
            active.setdefault("results", {})[slug] = result
            active["free_pending"].pop(0)
            write_json(self.state_path, run_state)
            self.save_limit_state(limit_state)
            self.checkpoint(summary=f"Free modul tamamlandi: {slug}", next_step="Sonraki free modul islenecek.")

        if not any(item.get("phase") == "free" for item in self.latest_backup_results):
            self.backup_output("free")

        if active.get("phase") != "paid":
            active["phase"] = "paid"
            write_json(self.state_path, run_state)

        while active.get("paid_pending"):
            slug = active["paid_pending"][0]
            module = module_by_slug.get(slug)
            if not module:
                active["paid_pending"].pop(0)
                continue
            result = self.run_module(module, limit_state)
            active.setdefault("results", {})[slug] = result
            active["paid_pending"].pop(0)
            write_json(self.state_path, run_state)
            self.save_limit_state(limit_state)
            self.checkpoint(summary=f"Paid modul tamamlandi: {slug}", next_step="Sonraki paid modul islenecek.")

        if not any(item.get("phase") == "paid" for item in self.latest_backup_results):
            self.backup_output("paid")

        smoke_result = self.run_smoke()
        windows_result = self.run_windows_hook()
        self.backup_output("final")

        results_map = active.get("results", {})
        completed_free = sum(1 for slug in results_map if module_by_slug.get(slug, ModuleSpec("", "", "", "", "", False, [], [], [])).tier == "free")
        completed_paid = sum(1 for slug in results_map if module_by_slug.get(slug, ModuleSpec("", "", "", "", "", False, [], [], [])).tier != "free")

        run_results = {
            "completed_free": completed_free,
            "completed_paid": completed_paid,
            "pending_free": active.get("free_pending", []),
            "pending_paid": active.get("paid_pending", []),
        }
        self.compose_report(
            ssot_check=ssot_check,
            modules=modules,
            run_results=run_results,
            smoke_result=smoke_result,
            windows_result=windows_result,
            planning_engine=planning_engine,
        )

        history = run_state.setdefault("history", [])
        history.append(
            {
                "id": active.get("id"),
                "started_at": active.get("started_at"),
                "finished_at": to_iso(),
                "completed_free": completed_free,
                "completed_paid": completed_paid,
                "smoke_status": smoke_result.get("status"),
            }
        )
        run_state["active_run"] = {
            "id": utc_now().strftime("run_%Y%m%d_%H%M%S"),
            "started_at": to_iso(),
            "phase": "free",
            "free_pending": [module.slug for module in modules if module.tier == "free"],
            "paid_pending": [module.slug for module in modules if module.tier != "free"],
            "results": {},
        }
        write_json(self.state_path, run_state)
        self.save_limit_state(limit_state)
        self.log("run_completed")
        return 0

    def run_loop(self) -> int:
        self.ensure_dirs()
        while True:
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover
                self.log_error(f"run_loop_exception: {exc}")
            self.log(f"sleeping interval_seconds={self.interval_seconds}")
            time.sleep(max(self.interval_seconds, 30))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stellcodex 7/24 orchestrator runner")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval-seconds", type=int, default=600, help="Loop interval")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT), help="Output root")
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE), help="Workspace root")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    orchestrator = Stellcodex247Orchestrator(
        output_root=Path(args.output_root),
        workspace_root=Path(args.workspace_root),
        interval_seconds=args.interval_seconds,
    )
    if args.loop:
        return orchestrator.run_loop()
    return orchestrator.run_once()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
