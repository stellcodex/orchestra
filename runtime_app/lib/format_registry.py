from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FormatRule:
    ext: str
    kind: str
    mode: str
    pipeline: str
    accept: bool
    display_label: str
    reject_reason: str | None = None


_RULES: tuple[FormatRule, ...] = (
    # 3D B-Rep
    FormatRule("step", "3d", "brep", "pipeline_3d_brep", True, "STEP"),
    FormatRule("stp", "3d", "brep", "pipeline_3d_brep", True, "STEP"),
    FormatRule("x_t", "3d", "brep", "pipeline_3d_brep", True, "Parasolid XT"),
    FormatRule("x_b", "3d", "brep", "pipeline_3d_brep", True, "Parasolid XB"),
    FormatRule("sat", "3d", "brep", "pipeline_3d_brep", True, "ACIS SAT"),
    FormatRule("sab", "3d", "brep", "pipeline_3d_brep", True, "ACIS SAB"),
    FormatRule("iges", "3d", "brep", "pipeline_3d_brep", True, "IGES"),
    FormatRule("igs", "3d", "brep", "pipeline_3d_brep", True, "IGES"),
    FormatRule("jt", "3d", "brep", "pipeline_3d_brep", True, "JT (B-Rep where available)"),
    FormatRule("ifc", "3d", "brep", "pipeline_3d_brep", True, "IFC (limited)"),
    # 3D mesh approximation
    FormatRule("stl", "3d", "mesh_approx", "pipeline_3d_mesh", True, "STL"),
    FormatRule("obj", "3d", "mesh_approx", "pipeline_3d_mesh", True, "OBJ"),
    FormatRule("ply", "3d", "mesh_approx", "pipeline_3d_mesh", True, "PLY"),
    FormatRule("3mf", "3d", "mesh_approx", "pipeline_3d_mesh", True, "3MF"),
    FormatRule("amf", "3d", "mesh_approx", "pipeline_3d_mesh", True, "AMF"),
    FormatRule("off", "3d", "mesh_approx", "pipeline_3d_mesh", True, "OFF"),
    FormatRule("wrl", "3d", "mesh_approx", "pipeline_3d_mesh", True, "VRML"),
    FormatRule("vrml", "3d", "mesh_approx", "pipeline_3d_mesh", True, "VRML"),
    FormatRule("dae", "3d", "mesh_approx", "pipeline_3d_mesh", True, "COLLADA"),
    # 3D visual only
    FormatRule("gltf", "3d", "visual_only", "pipeline_3d_visual", True, "glTF"),
    FormatRule("glb", "3d", "visual_only", "pipeline_3d_visual", True, "GLB"),
    # 2D
    FormatRule("dxf", "2d", "2d_only", "pipeline_2d", True, "DXF"),
    FormatRule("dwg", "2d", "2d_only", "pipeline_2d", True, "DWG (preview conversion)"),
    FormatRule("svg", "2d", "2d_only", "pipeline_2d", True, "SVG"),
    # Documents
    FormatRule("pdf", "doc", "doc", "pipeline_doc", True, "PDF"),
    FormatRule("docx", "doc", "doc", "pipeline_doc", True, "DOCX"),
    FormatRule("xlsx", "doc", "doc", "pipeline_doc", True, "XLSX"),
    FormatRule("pptx", "doc", "doc", "pipeline_doc", True, "PPTX"),
    FormatRule("odt", "doc", "doc", "pipeline_doc", True, "ODT"),
    FormatRule("ods", "doc", "doc", "pipeline_doc", True, "ODS"),
    FormatRule("odp", "doc", "doc", "pipeline_doc", True, "ODP"),
    FormatRule("rtf", "doc", "doc", "pipeline_doc", True, "RTF"),
    FormatRule("txt", "doc", "doc", "pipeline_doc", True, "TXT"),
    FormatRule("md", "doc", "doc", "pipeline_doc", True, "Markdown"),
    FormatRule("csv", "doc", "doc", "pipeline_doc", True, "CSV"),
    FormatRule("html", "doc", "doc", "pipeline_doc", True, "HTML"),
    FormatRule("htm", "doc", "doc", "pipeline_doc", True, "HTML"),
    # Archives
    FormatRule("zip", "archive", "archive_bundle", "pipeline_archive", True, "ZIP"),
    FormatRule("rar", "archive", "archive_bundle", "pipeline_archive", True, "RAR"),
    FormatRule("7z", "archive", "archive_bundle", "pipeline_archive", True, "7Z"),
    # Images
    FormatRule("png", "image", "image", "pipeline_image", True, "PNG"),
    FormatRule("jpg", "image", "image", "pipeline_image", True, "JPG"),
    FormatRule("jpeg", "image", "image", "pipeline_image", True, "JPEG"),
    FormatRule("webp", "image", "image", "pipeline_image", True, "WEBP"),
    FormatRule("bmp", "image", "image", "pipeline_image", True, "BMP"),
    FormatRule("gif", "image", "image", "pipeline_image", True, "GIF"),
    FormatRule("tif", "image", "image", "pipeline_image", True, "TIF"),
    FormatRule("tiff", "image", "image", "pipeline_image", True, "TIFF"),
    # deterministic rejects
    FormatRule("fcstd", "3d", "rejected", "reject", False, "FreeCAD FCStd", "STEP export required"),
)


_REGISTRY = {rule.ext: rule for rule in _RULES}


def extension_from_filename(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return suffix


def get_rule_by_ext(ext: str) -> FormatRule | None:
    return _REGISTRY.get((ext or "").lower().lstrip("."))


def get_rule_for_filename(filename: str) -> FormatRule | None:
    return get_rule_by_ext(extension_from_filename(filename))


def supported_rules() -> list[FormatRule]:
    return [rule for rule in _RULES if rule.accept]


def rejected_rules() -> list[FormatRule]:
    return [rule for rule in _RULES if not rule.accept]


def allowed_extensions() -> list[str]:
    return sorted({rule.ext for rule in supported_rules()})


def rejected_extensions() -> list[str]:
    return sorted({rule.ext for rule in rejected_rules()})


def is_allowed_filename(filename: str) -> bool:
    rule = get_rule_for_filename(filename)
    return bool(rule and rule.accept)


def grouped_payload() -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {
        "3d_brep": [],
        "3d_mesh_approx": [],
        "3d_visual_only": [],
        "2d_only": [],
        "doc": [],
        "archive": [],
        "image": [],
        "rejected": [],
    }
    for rule in _RULES:
        row = {"ext": rule.ext, "display_label": rule.display_label}
        if not rule.accept:
            row["reason"] = rule.reject_reason or "Unsupported format"
            groups["rejected"].append(row)
            continue
        if rule.mode == "brep":
            groups["3d_brep"].append(row)
        elif rule.mode == "mesh_approx":
            groups["3d_mesh_approx"].append(row)
        elif rule.mode == "visual_only":
            groups["3d_visual_only"].append(row)
        elif rule.mode == "2d_only":
            groups["2d_only"].append(row)
        elif rule.kind == "doc":
            groups["doc"].append(row)
        elif rule.kind == "archive":
            groups["archive"].append(row)
        elif rule.kind == "image":
            groups["image"].append(row)
    return groups


def as_public_rows() -> list[dict[str, str | bool | None]]:
    rows: list[dict[str, str | bool | None]] = []
    for rule in _RULES:
        rows.append(
            {
                "ext": rule.ext,
                "kind": rule.kind,
                "mode": rule.mode,
                "pipeline": rule.pipeline,
                "accept": rule.accept,
                "reject_reason": rule.reject_reason,
                "display_label": rule.display_label,
            }
        )
    return rows


def match_content_type(content_type: str, ext: str) -> bool:
    ctype = (content_type or "").lower().strip()
    ext = (ext or "").lower().strip().lstrip(".")
    if not ctype:
        return True

    mapping: dict[str, set[str]] = {
        "application/pdf": {"pdf"},
        "text/markdown": {"md"},
        "image/png": {"png"},
        "image/jpeg": {"jpg", "jpeg"},
        "image/webp": {"webp"},
        "image/bmp": {"bmp"},
        "image/gif": {"gif"},
        "image/tiff": {"tif", "tiff"},
        "text/plain": {"txt", "csv", "md"},
        "text/csv": {"csv"},
        "text/html": {"html", "htm"},
        "model/gltf+json": {"gltf"},
        "model/gltf-binary": {"glb"},
        "application/zip": {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "3mf"},
        "application/x-zip-compressed": {"zip"},
        "application/vnd.rar": {"rar"},
        "application/x-rar-compressed": {"rar"},
        "application/x-7z-compressed": {"7z"},
        "application/acad": {"dwg"},
        "application/x-acad": {"dwg"},
        "application/autocad_dwg": {"dwg"},
        "image/vnd.dwg": {"dwg"},
        "application/dwg": {"dwg"},
        "application/x-dwg": {"dwg"},
        "application/octet-stream": set(allowed_extensions()),
        "image/vnd.dxf": {"dxf"},
        "application/dxf": {"dxf"},
        "application/x-dxf": {"dxf"},
    }
    exact = mapping.get(ctype)
    if exact is not None:
        return ext in exact
    if ctype.startswith("image/"):
        return ext in {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "svg", "dxf"}
    return True


def infer_mime_from_bytes(head: bytes, filename: str) -> str:
    data = head or b""
    ext = extension_from_filename(filename)
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return "image/tiff"
    if data.startswith(b"PK\x03\x04"):
        if ext == "docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if ext == "xlsx":
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == "pptx":
            return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if ext in {"3mf", "odt", "ods", "odp"}:
            return "application/zip"
        if ext == "zip":
            return "application/zip"
    if data.startswith(b"Rar!\x1a\x07\x00") or data.startswith(b"Rar!\x1a\x07\x01\x00"):
        return "application/x-rar-compressed"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if b"ISO-10303-21" in data[:4096]:
        return "model/step"
    if data[:5].lower() == b"solid":
        return "model/stl"
    if data.startswith(b"ply"):
        return "model/ply"
    if data.startswith(b"OFF"):
        return "model/off"
    if b"<?xml" in data[:256] and b"<svg" in data[:2048]:
        return "image/svg+xml"
    html_head = data[:4096].lower()
    if b"<!doctype html" in html_head or b"<html" in html_head:
        return "text/html"
    if b'"asset"' in data[:4096] and b'"version"' in data[:4096]:
        return "model/gltf+json"
    return "application/octet-stream"


def supported_by_kind(kind: str) -> list[str]:
    target = (kind or "").lower()
    return sorted({rule.ext for rule in supported_rules() if rule.kind == target})


def find_mode(ext: str) -> str | None:
    rule = get_rule_by_ext(ext)
    return rule.mode if rule else None


def find_kind(ext: str) -> str | None:
    rule = get_rule_by_ext(ext)
    return rule.kind if rule else None


def to_legacy_groups() -> dict[str, list[str]]:
    groups: dict[str, set[str]] = {
        "brep": set(),
        "mesh": set(),
        "dxf": set(),
        "archive": set(),
        "images": set(),
        "rejected": set(),
    }
    for rule in _RULES:
        if not rule.accept:
            groups["rejected"].add(rule.ext)
            continue
        if rule.mode == "brep":
            groups["brep"].add(rule.ext)
        elif rule.mode in {"mesh_approx", "visual_only"}:
            groups["mesh"].add(rule.ext)
        elif rule.mode == "2d_only":
            groups["dxf"].add(rule.ext)
        elif rule.kind == "archive":
            groups["archive"].add(rule.ext)
        elif rule.kind in {"image", "doc"}:
            groups["images"].add(rule.ext)
    return {k: sorted(v) for k, v in groups.items()}
