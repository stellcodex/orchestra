from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import hashlib
import json
import re


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_\\-]+", text.lower())


def _chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        chunks.append(clean[start:end])
        start = end
    return chunks


def _deterministic_embedding(text: str, dims: int = 32) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    for idx in range(dims):
        byte = seed[idx % len(seed)]
        out.append(round((byte / 255.0) * 2.0 - 1.0, 6))
    return out


@dataclass
class CKIRecord:
    artifact_id: str
    drive_path: str
    checksum: str
    chunk_id: str
    chunk_text: str
    embedding_vector: list[float]
    source_link: str
    ingested_at: str


def build_cki_records(artifact_id: str, drive_path: str, text: str, source_link: str) -> list[dict]:
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunks = _chunk_text(text)
    records: list[dict] = []
    for idx, chunk in enumerate(chunks):
        record = CKIRecord(
            artifact_id=artifact_id,
            drive_path=drive_path,
            checksum=checksum,
            chunk_id=f"{artifact_id}:chunk:{idx+1:04d}",
            chunk_text=chunk,
            embedding_vector=_deterministic_embedding(chunk),
            source_link=source_link,
            ingested_at=_now_iso(),
        )
        records.append(asdict(record))
    return records


def ingest_drive_exports(jsonl_paths: Iterable[str], output_path: str) -> int:
    output = Path(output_path)
    all_records: list[dict] = []
    for path_str in jsonl_paths:
        path = Path(path_str)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            text = str(payload.get("text") or "")
            if not text.strip():
                continue
            artifact_id = str(payload.get("artifact_id") or payload.get("id") or path.stem)
            drive_path = str(payload.get("drive_path") or payload.get("path") or "")
            source_link = str(payload.get("source_link") or "")
            all_records.extend(build_cki_records(artifact_id, drive_path, text, source_link))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(all_records)
