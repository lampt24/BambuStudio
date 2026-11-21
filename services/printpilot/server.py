#!/usr/bin/env python3
"""
PrintPilot server – Python stdlib + SQLite only.
Implements the full shared API contract from docs/printpilot/PLAN.md.

Endpoints
---------
GET  /health                     – public
GET  /api/state                  – manager token
POST /api/analyze                – manager token
GET  /api/images/{id}            – manager token
POST /api/alerts/{id}/ack        – manager token
POST /api/snapshots              – device token
POST /api/heartbeat              – device token
GET  /                           – static (web/)
GET  /<path>                     – static (web/)

Configuration (JSON file, path in env PRINTPILOT_CONFIG or first CLI arg):
  host, port, data_dir,
  manager_token, device_token,
  ai_base_url, ai_key, ai_model, vision_model,
  webhook_url
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import io
import json
import logging
import math
import mimetypes
import os
import queue
import secrets
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("printpilot")

# ---------------------------------------------------------------------------
# Constants / allowlist
# ---------------------------------------------------------------------------

# Hard limits
MAX_HTTP_BODY = 90 * 1024 * 1024        # 90 MiB
MAX_DECODED_PROJECT = 64 * 1024 * 1024  # 64 MiB
MAX_ZIP_EXPAND = 64 * 1024 * 1024       # max sum of uncompressed sizes
MAX_SINGLE_ENTRY = 32 * 1024 * 1024     # any single zip entry
MAX_JPEG_BYTES = 8 * 1024 * 1024        # snapshot image
MAX_THUMBNAIL = 4 * 1024 * 1024         # thumbnail from 3MF
HEARTBEAT_STALE_SECS = 300              # 5 min – device considered stale
RETENTION_SECS = 14 * 86400

# Allowed objectives / triggers
VALID_OBJECTIVES = {"quality", "strength", "speed", "economy"}
VALID_TRIGGERS   = {"manual", "send"}
VALID_STATUSES   = {"printing", "idle", "unknown"}

# Numeric Bambu Studio settings eligible for application: key -> (min, max, integer)
SETTINGS_ALLOWLIST: dict[str, tuple[float, float, bool]] = {
    "layer_height": (0.08, 0.28, False),
    "initial_layer_print_height": (0.1, 0.3, False),
    "wall_loops": (1, 10, True),
    "top_shell_layers": (1, 20, True),
    "bottom_shell_layers": (1, 20, True),
    "sparse_infill_density": (0, 100, False),
    "outer_wall_speed": (10, 200, False),
    "inner_wall_speed": (10, 300, False),
    "sparse_infill_speed": (10, 300, False),
    "brim_width": (0, 20, False),
}

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    required = ["host", "port", "data_dir", "manager_token", "device_token"]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Config missing required key: {key!r}")
    if isinstance(cfg["port"], bool) or not isinstance(cfg["port"], int) or not 0 <= cfg["port"] <= 65535:
        raise ValueError("Config 'port' must be integer")
    if any(not isinstance(cfg[key], str) or not cfg[key].strip() for key in ("manager_token", "device_token")):
        raise ValueError("Tokens must be non-empty strings")
    if cfg["manager_token"] == cfg["device_token"]:
        raise ValueError("manager_token and device_token must differ")
    return cfg


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS analyses (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    changes     TEXT,          -- JSON array
    warnings    TEXT,          -- JSON array
    model_info  TEXT,          -- JSON object
    file_path   TEXT,          -- private storage path
    objective   TEXT,
    trigger     TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    printer_id  TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    telemetry   TEXT,          -- JSON object
    image_path  TEXT NOT NULL,
    vision_status TEXT DEFAULT 'pending',   -- pending|complete|error|skipped
    vision_result TEXT                      -- JSON or error string
);

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    printer_id  TEXT,
    last_seen   TEXT NOT NULL,
    last_status TEXT NOT NULL,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    device_id       TEXT,
    printer_id      TEXT,
    snapshot_id     TEXT,
    source          TEXT NOT NULL,  -- 'vision'|'heartbeat'|'analyze'
    severity        TEXT NOT NULL,  -- 'warning'|'error'
    message         TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,
    acknowledged    INTEGER NOT NULL DEFAULT 0,
    webhook_sent    INTEGER NOT NULL DEFAULT 0,
    webhook_retries INTEGER NOT NULL DEFAULT 0,
    webhook_next_at TEXT
);

CREATE TABLE IF NOT EXISTS vision_queue (
    id          TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    queued_at   TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'  -- pending|processing|done|failed
);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "snapshot_id" not in {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}:
        conn.execute("ALTER TABLE alerts ADD COLUMN snapshot_id TEXT")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 3MF extraction helpers
# ---------------------------------------------------------------------------

class ExtractionError(Exception):
    pass


def _bounded_read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise ExtractionError(
            f"ZIP entry {name!r} uncompressed size {info.file_size} exceeds limit {limit}"
        )
    with zf.open(name) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ExtractionError(f"ZIP entry {name!r} exceeds limit {limit}")
    return data


def extract_3mf(raw_bytes: bytes) -> dict[str, Any]:
    """
    Safely extract geometry summary, config, and thumbnail from a 3MF zip.
    Returns a dict with keys: geometry, config, thumbnail_b64 (may be None).
    Raises ExtractionError on any problem.
    """
    if len(raw_bytes) > MAX_DECODED_PROJECT:
        raise ExtractionError(
            f"Decoded project size {len(raw_bytes)} exceeds limit {MAX_DECODED_PROJECT}"
        )

    if not zipfile.is_zipfile(io.BytesIO(raw_bytes)):
        raise ExtractionError("Project data is not a valid ZIP/3MF file")

    result: dict[str, Any] = {
        "geometry": {},
        "config": {},
        "thumbnail_b64": None,
    }

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        # Bomb check: sum of uncompressed sizes
        total_uncompressed = sum(i.file_size for i in zf.infolist())
        if total_uncompressed > MAX_ZIP_EXPAND:
            raise ExtractionError(
                f"ZIP total uncompressed {total_uncompressed} exceeds limit {MAX_ZIP_EXPAND}"
            )

        names = {n.lower(): n for n in zf.namelist()}

        # Every split .model carries geometry; keep the provider payload bounded.
        model_geometries = []
        for name in zf.namelist():
            if name.lower().endswith(".model"):
                model_geometries.append(_parse_model_geometry(_bounded_read(zf, name, MAX_SINGLE_ENTRY)))
        if model_geometries:
            geom = {"object_count": 0, "vertex_count": 0, "triangle_count": 0,
                    "build_items": 0, "models": len(model_geometries), "sample_vertices": [],
                    "sample_triangles": [], "overhang_triangles": 0}
            low, high = [math.inf] * 3, [-math.inf] * 3
            for part in model_geometries:
                for key in ("object_count", "vertex_count", "triangle_count", "build_items", "overhang_triangles"):
                    geom[key] += part[key]
                geom["unit"] = part.get("unit", geom.get("unit", "millimeter"))
                geom["sample_vertices"].extend(part["sample_vertices"][:max(0, 64 - len(geom["sample_vertices"]))])
                geom["sample_triangles"].extend(part["sample_triangles"][:max(0, 64 - len(geom["sample_triangles"]))])
                if "bbox" in part:
                    low = [min(a, b) for a, b in zip(low, part["bbox"]["min"])]
                    high = [max(a, b) for a, b in zip(high, part["bbox"]["max"])]
            if low[0] != math.inf:
                geom["bbox"] = {"min": low, "max": high}
            result["geometry"] = geom

        # --- config: Bambu/PrusaSlicer config ---
        config_data: dict[str, Any] = {}
        for cand_key in names:
            if "config" in cand_key and cand_key.endswith(".json"):
                raw = _bounded_read(zf, names[cand_key], MAX_SINGLE_ENTRY)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        config_data.update(parsed)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        # Also check Metadata/Slic3r_PE.config (ini-style)
        for cand_key in names:
            if "metadata" in cand_key and cand_key.endswith(".config"):
                raw = _bounded_read(zf, names[cand_key], MAX_SINGLE_ENTRY)
                if raw.lstrip().startswith(b"<"):
                    config_data.update(_parse_model_config(raw))
                else:
                    config_data.update(_parse_ini_config(raw.decode("utf-8", errors="replace")))
        # Bambu Studio project_settings.json
        for cand_key in names:
            if "project_settings" in cand_key and cand_key.endswith(".json"):
                raw = _bounded_read(zf, names[cand_key], MAX_SINGLE_ENTRY)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        config_data.update(parsed)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        result["config"] = config_data

        # --- thumbnail ---
        for cand_key in names:
            if cand_key.endswith(".png") or cand_key.endswith(".jpg") or cand_key.endswith(".jpeg"):
                if "thumbnail" in cand_key or "preview" in cand_key or re.search(r"metadata/plate_\d+\.(png|jpe?g)$", cand_key):
                    thumb = _bounded_read(zf, names[cand_key], MAX_THUMBNAIL)
                    if not (thumb.startswith(b"\x89PNG\r\n\x1a\n") or thumb.startswith(b"\xff\xd8")):
                        continue
                    result["thumbnail_b64"] = base64.b64encode(thumb).decode()
                    result["thumbnail_mime"] = "image/png" if thumb.startswith(b"\x89PNG") else "image/jpeg"
                    break

    return result


def _xml_root(data: bytes | str) -> ET.Element:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_SINGLE_ENTRY or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ExtractionError("Unsafe or oversized XML")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ExtractionError(f"Malformed XML: {exc}") from exc


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _parse_model_geometry(xml: bytes | str) -> dict[str, Any]:
    root = _xml_root(xml)
    if _tag(root) != "model":
        raise ExtractionError("3MF model root missing")
    geom: dict[str, Any] = {"object_count": 0, "vertex_count": 0, "triangle_count": 0,
                            "build_items": 0, "sample_vertices": [], "sample_triangles": [],
                            "overhang_triangles": 0}
    if "unit" in root.attrib:
        geom["unit"] = root.attrib["unit"][:40]
    objects = {}
    all_points = []
    for obj in root.iter():
        if _tag(obj) != "object":
            continue
        geom["object_count"] += 1
        vertices = []
        for element in obj.iter():
            kind = _tag(element)
            if kind == "vertex":
                try:
                    point = tuple(float(element.attrib[k]) for k in ("x", "y", "z"))
                except (KeyError, ValueError) as exc:
                    raise ExtractionError("Invalid vertex") from exc
                if not all(math.isfinite(v) for v in point):
                    raise ExtractionError("Non-finite vertex")
                vertices.append(point)
                geom["vertex_count"] += 1
                if len(geom["sample_vertices"]) < 64:
                    geom["sample_vertices"].append(point)
            elif kind == "triangle":
                try:
                    a, b, c = (vertices[int(element.attrib[k])] for k in ("v1", "v2", "v3"))
                except (KeyError, ValueError, IndexError) as exc:
                    raise ExtractionError("Invalid triangle index") from exc
                u = [b[i] - a[i] for i in range(3)]
                v = [c[i] - a[i] for i in range(3)]
                normal = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]]
                norm = math.sqrt(sum(n*n for n in normal))
                if norm:
                    normal = [round(n/norm, 4) for n in normal]
                    if normal[2] < -0.5:
                        geom["overhang_triangles"] += 1
                if len(geom["sample_triangles"]) < 64:
                    geom["sample_triangles"].append({"vertices": [a, b, c], "normal": normal})
                geom["triangle_count"] += 1
        objects[obj.attrib.get("id")] = vertices
        all_points.extend(vertices)
    for item in root.iter():
        if _tag(item) != "item":
            continue
        geom["build_items"] += 1
        points = objects.get(item.attrib.get("objectid"), [])
        if "transform" in item.attrib:
            try:
                m = [float(x) for x in item.attrib["transform"].split()]
            except ValueError as exc:
                raise ExtractionError("Invalid build transform") from exc
            if len(m) != 12 or not all(math.isfinite(x) for x in m):
                raise ExtractionError("Invalid build transform")
            points = [(p[0]*m[0]+p[1]*m[3]+p[2]*m[6]+m[9],
                       p[0]*m[1]+p[1]*m[4]+p[2]*m[7]+m[10],
                       p[0]*m[2]+p[1]*m[5]+p[2]*m[8]+m[11]) for p in points]
        if points:
            if geom["build_items"] == 1:
                all_points = []
            all_points.extend(points)
    if all_points:
        geom["bbox"] = {"min": [min(p[i] for p in all_points) for i in range(3)],
                        "max": [max(p[i] for p in all_points) for i in range(3)]}
    return geom


def _parse_model_config(data: bytes) -> dict[str, Any]:
    root = _xml_root(data)
    result: dict[str, Any] = {"objects": {}}
    def walk(node: ET.Element, object_id: str | None = None) -> None:
        if _tag(node) == "object" and node.attrib.get("id"):
            object_id = node.attrib["id"]
            result["objects"].setdefault(object_id, {})
        if _tag(node) == "metadata" and "key" in node.attrib:
            destination = result["objects"][object_id] if object_id else result
            destination[node.attrib["key"]] = node.attrib.get("value", (node.text or "").strip())
        for child in node:
            walk(child, object_id)
    walk(root)
    return result


def _parse_ini_config(text: str) -> dict[str, Any]:
    """Parse a simple key=value config file into a dict."""
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _sanitize_context(value: Any, depth: int = 0) -> Any:
    """Keep bounded context while excluding credentials and connection secrets."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {str(k)[:100]: _sanitize_context(v, depth + 1)
                for k, v in list(value.items())[:300]
                if not re.search(r"token|secret|password|passwd|access.code|api.key|auth|credential|private.key|wifi|ssid", str(k), re.I)}
    if isinstance(value, list):
        return [_sanitize_context(v, depth + 1) for v in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:500] if isinstance(value, str) else value
    return None


# ---------------------------------------------------------------------------
# Settings validation / allowlist enforcement
# ---------------------------------------------------------------------------

def _numeric_setting(key: str, value: Any) -> int | float:
    lo, hi, integer = SETTINGS_ALLOWLIST[key]
    if isinstance(value, bool):
        raise ValueError(f"Setting {key!r}: boolean is not numeric")
    if isinstance(value, str):
        value = value.strip()
        if key == "sparse_infill_density" and value.endswith("%"):
            value = value[:-1].strip()
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
            raise ValueError(f"Setting {key!r}: expected numeric scalar")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Setting {key!r}: expected numeric scalar") from exc
    if not math.isfinite(number) or not lo <= number <= hi or (integer and not number.is_integer()):
        raise ValueError(f"Setting {key!r}: {number} out of range [{lo}, {hi}] or not an integer")
    return int(number) if integer else number


def _nozzle_limit(settings: dict[str, Any], values: dict[str, Any]) -> bool:
    raw = settings.get("nozzle_diameter")
    if raw is None:
        return False
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, str):
        raw = raw.split(",", 1)[0].strip()
    try:
        nozzle = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid nozzle_diameter") from exc
    if not math.isfinite(nozzle) or not 0.1 <= nozzle <= 1.2:
        raise ValueError("Invalid nozzle_diameter")
    for key in ("layer_height", "initial_layer_print_height"):
        if key in values and values[key] > nozzle * 0.8 + 1e-9:
            raise ValueError(f"Setting {key!r} exceeds 80% of nozzle diameter")
    return True


def validate_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Return only validated settings from raw (submitted with analyze request).
    Drops non-applicable keys and validates Bambu numeric bounds.
    Raises ValueError on clearly invalid types.
    """
    if not isinstance(raw, dict):
        raise ValueError("'settings' must be a JSON object")
    validated: dict[str, Any] = {}
    for key in SETTINGS_ALLOWLIST.keys() & raw.keys():
        validated[key] = _numeric_setting(key, raw[key])
    return validated


def validate_changes(changes: list[Any], submitted_settings: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Validate AI-returned changes list.
    Each change must have key (in allowlist), old (must match what we sent or config), value (bounded), reason (str).
    Returns clean list; raises ValueError on structural problems.
    """
    if not isinstance(changes, list):
        raise ValueError("AI 'changes' must be a JSON array")
    result = []
    seen = set()
    for i, item in enumerate(changes):
        if not isinstance(item, dict):
            raise ValueError(f"changes[{i}] is not an object")
        if isinstance(item.get("key"), str) and item["key"] not in SETTINGS_ALLOWLIST:
            continue
        for field in ("key", "old", "value", "reason"):
            if field not in item:
                raise ValueError(f"changes[{i}] missing field {field!r}")
        key = item["key"]
        if not isinstance(key, str):
            raise ValueError(f"changes[{i}].key is not a string")
        if key not in SETTINGS_ALLOWLIST:
            # AI suggested unknown key – drop silently with warning (caller handles)
            continue
        if key in seen:
            raise ValueError(f"changes[{i}] duplicates {key!r}")
        seen.add(key)
        if key not in submitted_settings:
            raise ValueError(f"changes[{i}] key {key!r} was not submitted")
        reason = item.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(f"changes[{i}].reason is not a string")

        validated_value = _numeric_setting(key, item["value"])
        old_val = _numeric_setting(key, item["old"])
        if old_val != submitted_settings[key]:
            raise ValueError(f"changes[{i}] key={key!r} old does not match submitted value")

        result.append({
            "key": key,
            "old": old_val,
            "value": validated_value,
            "reason": reason[:2000],  # cap reason length
        })
    return result


# ---------------------------------------------------------------------------
# AI client (OpenAI-compatible)
# ---------------------------------------------------------------------------

class AIError(Exception):
    pass


def _openai_post(base_url: str, api_key: str, path: str, payload: dict) -> dict:
    """
    POST to base_url + path with Bearer auth.
    Returns parsed JSON response dict.
    Raises AIError on network/HTTP/parse failure.
    """
    parsed = urllib.parse.urlparse(base_url.rstrip("/"))
    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    prefix = parsed.path.rstrip("/")
    full_path = prefix + path

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Length": str(len(body)),
    }

    conn = None
    try:
        if scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            if port:
                conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=90)
            else:
                conn = http.client.HTTPSConnection(host, context=ctx, timeout=90)
        else:
            if port:
                conn = http.client.HTTPConnection(host, port, timeout=90)
            else:
                conn = http.client.HTTPConnection(host, timeout=90)

        conn.request("POST", full_path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise AIError("AI provider response too large")
        if resp.status not in (200, 201):
            raise AIError(f"AI provider error: HTTP {resp.status}")
        return json.loads(raw)
    except AIError:
        raise
    except Exception as exc:
        raise AIError(f"AI provider request failed: {type(exc).__name__}") from exc
    finally:
        if conn is not None:
            conn.close()


def ai_analyze(cfg: dict, extracted: dict, settings: dict, objective: str, trigger: str,
               full_settings: dict | None = None) -> dict:
    """
    Call AI for 3MF analysis.
    Returns dict with keys: summary (str), changes (list), warnings (list), model (dict).
    Raises AIError on provider failure or invalid response structure.
    """
    base_url = cfg.get("ai_base_url", "").strip()
    api_key  = cfg.get("ai_key", "").strip()
    model    = cfg.get("ai_model", "gpt-4o").strip()

    if not base_url or not api_key:
        raise AIError("AI provider not configured (ai_base_url / ai_key missing)")

    # Build system message
    allowed_keys = sorted(SETTINGS_ALLOWLIST.keys())
    system_msg = (
        "You are a 3D printing expert assistant. "
        "Analyze the provided print settings and geometry information. "
        "Return ONLY valid JSON with this exact structure:\n"
        '{"summary": "string", "changes": [...], "warnings": ["string", ...]}\n'
        "Each change object must be: "
        '{"key": "setting_key", "old": <current_value_or_null>, "value": <new_value>, "reason": "string"}\n'
        f"Only suggest changes for these keys: {allowed_keys}\n"
        "Do not include any keys outside that list. "
        "Numeric values must be within documented printer limits. "
        "Be concise. Return empty arrays if no changes needed."
    )

    geometry_summary = json.dumps(extracted.get("geometry", {}), indent=2)
    config_excerpt = json.dumps(_sanitize_context(extracted.get("config", {})), indent=2)
    settings_json = json.dumps(settings, indent=2)
    context_json = json.dumps(_sanitize_context(full_settings or {}), indent=2)

    user_parts = [
        f"Objective: {objective}",
        f"Trigger: {trigger}",
        f"Current settings:\n{settings_json}",
        f"Other effective settings (context only, never applicable):\n{context_json}",
        f"Geometry metadata:\n{geometry_summary}",
        f"3MF configuration:\n{config_excerpt}",
    ]

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    # Optionally include thumbnail for vision-capable model
    if extracted.get("thumbnail_b64") and cfg.get("ai_model_has_vision", True):
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Thumbnail preview of the model:"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{extracted.get('thumbnail_mime', 'image/png')};base64,{extracted['thumbnail_b64']}",
                        "detail": "low",
                    },
                },
            ],
        })

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "stream": False,
    }

    response = _openai_post(base_url, api_key, "/chat/completions", payload)

    # Extract content
    try:
        choices = response["choices"]
        if not choices:
            raise AIError("AI response has empty choices")
        content_str = choices[0]["message"]["content"]
        ai_data = json.loads(content_str)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AIError("AI response structure invalid") from exc

    # Validate structure
    if not isinstance(ai_data, dict):
        raise AIError("AI returned non-object JSON")
    if "summary" not in ai_data:
        raise AIError("AI response missing 'summary'")
    if not isinstance(ai_data.get("summary"), str):
        raise AIError("AI 'summary' must be a string")
    if not isinstance(ai_data.get("changes"), list) or not isinstance(ai_data.get("warnings"), list) or not all(isinstance(w, str) for w in ai_data["warnings"]):
        raise AIError("AI changes/warnings must be arrays")

    model_info = {
        "id": response.get("model", model),
        "usage": response.get("usage", {}),
    }

    return {
        "summary": ai_data["summary"][:5000],
        "changes": ai_data.get("changes", []),
        "warnings": ai_data["warnings"][:50],
        "model": model_info,
    }


def ai_vision(cfg: dict, image_b64: str, context: dict) -> dict:
    """
    Call vision model to evaluate a snapshot image.
    Returns dict with keys: ok (bool), issues (list[str]), summary (str).
    Raises AIError on failure.
    """
    base_url    = cfg.get("ai_base_url", "").strip()
    api_key     = cfg.get("ai_key", "").strip()
    vision_model = cfg.get("vision_model", "").strip() or cfg.get("ai_model", "gpt-4o").strip()

    if not base_url or not api_key:
        raise AIError("Vision AI provider not configured")

    system_msg = (
        "You are a 3D print monitoring assistant. "
        "Examine the snapshot image. "
        "Return ONLY valid JSON: "
        '{"ok": true|false, "issues": ["string",...], "summary": "string"}\n'
        "'ok' is true if the print looks normal. "
        "'issues' lists observed problems. "
        "'summary' is one sentence."
    )

    ctx_text = f"Device: {context.get('device_id','?')}, Printer: {context.get('printer_id','?')}, Status: {context.get('status','?')}"

    messages = [
        {"role": "system", "content": system_msg},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": ctx_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}",
                        "detail": "low",
                    },
                },
            ],
        },
    ]

    payload = {
        "model": vision_model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "stream": False,
    }

    response = _openai_post(base_url, api_key, "/chat/completions", payload)

    try:
        content_str = response["choices"][0]["message"]["content"]
        data = json.loads(content_str)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise AIError(f"Vision response structure invalid: {exc}") from exc

    if not isinstance(data, dict):
        raise AIError("Vision returned non-object JSON")
    if type(data.get("ok")) is not bool or not isinstance(data.get("issues"), list) or not all(isinstance(i, str) for i in data["issues"]) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise AIError("Vision result missing or invalid ok/issues/summary")
    if data.get("uncertain") is True or (data["ok"] and data["issues"]):
        raise AIError("Vision assessment uncertain")

    return {
        "ok": data["ok"],
        "issues": data["issues"][:20],
        "summary": data["summary"][:1000],
    }


# ---------------------------------------------------------------------------
# Alert / webhook helpers
# ---------------------------------------------------------------------------

def _dedup_key(source: str, device_id: str | None, message: str) -> str:
    raw = f"{source}:{device_id or ''}:{message}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def upsert_alert(conn: sqlite3.Connection, source: str, severity: str,
                 message: str, device_id: str | None = None,
                 printer_id: str | None = None, snapshot_id: str | None = None) -> str | None:
    """
    Insert alert if dedup_key not already present and unacknowledged.
    Returns new alert id or None if deduped.
    """
    dk = _dedup_key(source, device_id, message)
    existing = conn.execute(
        "SELECT id, acknowledged FROM alerts WHERE dedup_key=?", (dk,)
    ).fetchone()
    if existing:
        if not existing["acknowledged"]:
            return None  # already open, deduped
        # was acked – allow re-open with new id
    alert_id = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO alerts
           (id, created_at, device_id, printer_id, snapshot_id, source, severity, message,
            dedup_key, acknowledged, webhook_sent, webhook_retries, webhook_next_at)
           VALUES (?,?,?,?,?,?,?,?,?,0,0,0,?)""",
        (alert_id, now, device_id, printer_id, snapshot_id, source, severity, message, dk, now),
    )
    conn.commit()
    return alert_id


def send_webhook(cfg: dict, alert: sqlite3.Row, conn: sqlite3.Connection) -> None:
    """Attempt to POST alert to webhook_url. Updates DB on success/failure."""
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    payload = json.dumps({
        "id": alert["id"],
        "created_at": alert["created_at"],
        "source": alert["source"],
        "severity": alert["severity"],
        "message": alert["message"],
        "device_id": alert["device_id"],
        "printer_id": alert["printer_id"],
        "snapshot_id": alert["snapshot_id"],
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "User-Agent": "PrintPilot/1.0",
    }

    conn_h = None
    try:
        if scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            conn_h = http.client.HTTPSConnection(host, port or 443, context=ctx, timeout=15)
        else:
            conn_h = http.client.HTTPConnection(host, port or 80, timeout=15)
        conn_h.request("POST", path, body=payload, headers=headers)
        resp = conn_h.getresponse()
        resp.read()
        success = resp.status < 400
    except Exception as exc:
        log.warning("Webhook delivery failed for alert %s: %s", alert["id"], type(exc).__name__)
        success = False
    finally:
        if conn_h is not None:
            conn_h.close()

    now = _now_iso()
    if success:
        conn.execute(
            "UPDATE alerts SET webhook_sent=1 WHERE id=?", (alert["id"],)
        )
    else:
        retries = alert["webhook_retries"] + 1
        # Exponential backoff: 2^retries * 60s, max 1h
        delay = min(2 ** retries * 60, 3600)
        import datetime
        next_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + delay)
        )
        conn.execute(
            "UPDATE alerts SET webhook_retries=?, webhook_next_at=? WHERE id=?",
            (retries, next_at, alert["id"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class VisionWorker(threading.Thread):
    """Processes vision_queue rows asynchronously."""

    def __init__(self, cfg: dict, db_path: str):
        super().__init__(daemon=True, name="vision-worker")
        self.cfg = cfg
        self.db_path = db_path
        self._stop = threading.Event()

    def run(self) -> None:
        conn = open_db(self.db_path)
        conn.execute("UPDATE vision_queue SET status='pending' WHERE status='processing'")
        conn.commit()
        while not self._stop.is_set():
            try:
                self._process_one(conn)
            except Exception:
                log.exception("VisionWorker error")
            self._stop.wait(10)

    def _process_one(self, conn: sqlite3.Connection) -> None:
        now = _now_iso()
        row = conn.execute(
            """SELECT vq.id as queue_id, vq.snapshot_id, vq.attempts,
                      s.image_path, s.device_id, s.printer_id, s.status as snap_status
               FROM vision_queue vq
               JOIN snapshots s ON s.id = vq.snapshot_id
               WHERE vq.status='pending' AND vq.next_at <= ?
               ORDER BY vq.queued_at LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return

        queue_id    = row["queue_id"]
        snapshot_id = row["snapshot_id"]
        attempts    = row["attempts"]
        image_path  = row["image_path"]

        # Mark processing
        conn.execute(
            "UPDATE vision_queue SET status='processing', attempts=? WHERE id=?",
            (attempts + 1, queue_id),
        )
        conn.commit()

        try:
            with open(image_path, "rb") as fh:
                image_bytes = fh.read(MAX_JPEG_BYTES + 1)
            if len(image_bytes) > MAX_JPEG_BYTES:
                raise AIError("Snapshot image too large")
            image_b64 = base64.b64encode(image_bytes).decode()
            context = {
                "device_id": row["device_id"],
                "printer_id": row["printer_id"],
                "status": row["snap_status"],
            }
            vision_cfg = self.cfg
            if not vision_cfg.get("vision_model") and not vision_cfg.get("ai_base_url"):
                # Vision not configured – skip
                conn.execute(
                    "UPDATE snapshots SET vision_status='skipped' WHERE id=?",
                    (snapshot_id,),
                )
                conn.execute(
                    "UPDATE vision_queue SET status='done' WHERE id=?",
                    (queue_id,),
                )
                conn.commit()
                return

            result = ai_vision(vision_cfg, image_b64, context)
            conn.execute(
                "UPDATE snapshots SET vision_status='complete', vision_result=? WHERE id=?",
                (json.dumps(result), snapshot_id),
            )
            conn.execute("UPDATE vision_queue SET status='done' WHERE id=?", (queue_id,))
            conn.commit()

            # Raise alert if issues found
            if not result.get("ok"):
                issues = result.get("issues", [])
                msg = "Vision alert: " + ("; ".join(issues) if issues else result.get("summary", "Issue detected"))
                alert_id = upsert_alert(
                    conn, "vision", "warning", msg,
                    row["device_id"], row["printer_id"], snapshot_id
                )
                if alert_id:
                    alert_row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                    if alert_row:
                        send_webhook(self.cfg, alert_row, conn)

        except (AIError, OSError) as exc:
            log.warning("Vision AI error for snapshot %s: %s", snapshot_id, exc)
            if attempts + 1 >= 3:
                conn.execute(
                    "UPDATE snapshots SET vision_status='error', vision_result=? WHERE id=?",
                    (str(exc), snapshot_id),
                )
                conn.execute("UPDATE vision_queue SET status='failed' WHERE id=?", (queue_id,))
            else:
                delay = 2 ** (attempts + 1) * 30
                next_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))
                conn.execute(
                    "UPDATE vision_queue SET status='pending', next_at=? WHERE id=?",
                    (next_at, queue_id),
                )
            conn.commit()
        except Exception as exc:
            log.error("Vision worker unexpected error for snapshot %s: %s", snapshot_id, exc)
            conn.execute(
                "UPDATE snapshots SET vision_status='error', vision_result=? WHERE id=?",
                (f"Internal error: {exc}", snapshot_id),
            )
            conn.execute("UPDATE vision_queue SET status='failed' WHERE id=?", (queue_id,))
            conn.commit()


class WebhookRetryWorker(threading.Thread):
    """Retries unsent/failed webhooks on a schedule."""

    def __init__(self, cfg: dict, db_path: str):
        super().__init__(daemon=True, name="webhook-retry")
        self.cfg = cfg
        self.db_path = db_path
        self._stop = threading.Event()

    def run(self) -> None:
        conn = open_db(self.db_path)
        while not self._stop.is_set():
            try:
                self._retry(conn)
            except Exception:
                log.exception("WebhookRetryWorker error")
            self._stop.wait(60)

    def _retry(self, conn: sqlite3.Connection) -> None:
        now = _now_iso()
        rows = conn.execute(
            """SELECT * FROM alerts
               WHERE webhook_sent=0 AND acknowledged=0
                 AND webhook_retries < 10
                 AND (webhook_next_at IS NULL OR webhook_next_at <= ?)
               LIMIT 20""",
            (now,),
        ).fetchall()
        for row in rows:
            send_webhook(self.cfg, row, conn)


class HeartbeatMonitor(threading.Thread):
    """Marks stale devices and raises alerts."""

    def __init__(self, cfg: dict, db_path: str):
        super().__init__(daemon=True, name="heartbeat-monitor")
        self.cfg = cfg
        self.db_path = db_path
        self._stop = threading.Event()

    def run(self) -> None:
        conn = open_db(self.db_path)
        while not self._stop.is_set():
            try:
                self._check(conn)
            except Exception:
                log.exception("HeartbeatMonitor error")
            self._stop.wait(60)

    def _check(self, conn: sqlite3.Connection) -> None:
        prune_old_data(conn)
        stale_cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - HEARTBEAT_STALE_SECS),
        )
        rows = conn.execute(
            "SELECT * FROM devices WHERE last_seen < ?", (stale_cutoff,)
        ).fetchall()
        for row in rows:
            msg = f"Device {row['device_id']} has not sent heartbeat since {row['last_seen']}"
            alert_id = upsert_alert(
                conn, "heartbeat", "warning", msg,
                row["device_id"], row["printer_id"]
            )
            if alert_id:
                alert_row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                if alert_row:
                    send_webhook(self.cfg, alert_row, conn)


def prune_old_data(conn: sqlite3.Connection) -> None:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - RETENTION_SECS))
    for table, column in (("analyses", "file_path"), ("snapshots", "image_path")):
        rows = conn.execute(f"SELECT id, {column} FROM {table} WHERE created_at < ?", (cutoff,)).fetchall()
        for row in rows:
            try:
                os.remove(row[column])
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("Retention could not remove file for %s %s", table, row["id"])
                continue
            if table == "snapshots":
                conn.execute("DELETE FROM vision_queue WHERE snapshot_id=?", (row["id"],))
            conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
    conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class PrintPilotHandler(http.server.BaseHTTPRequestHandler):
    # Injected by server setup
    cfg: dict
    db_path: str
    data_dir: str
    web_dir: str

    # Per-connection DB (thread-local via factory)
    _db_local = threading.local()

    @property
    def db(self) -> sqlite3.Connection:
        if not getattr(self._db_local, "conn", None):
            self._db_local.conn = open_db(self.db_path)
        return self._db_local.conn

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            conn = getattr(self._db_local, "conn", None)
            if conn is not None:
                conn.close()
                self._db_local.conn = None

    # --- Auth -----------------------------------------------------------

    def _auth_manager(self) -> bool:
        return self._check_token(self.cfg["manager_token"])

    def _auth_device(self) -> bool:
        token = self._get_bearer()
        if not token:
            return False
        return (secrets.compare_digest(token, self.cfg["manager_token"]) or
                secrets.compare_digest(token, self.cfg["device_token"]))

    def _check_token(self, expected: str) -> bool:
        token = self._get_bearer()
        return token is not None and secrets.compare_digest(token, expected)

    def _get_bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    # --- Response helpers -----------------------------------------------

    def _send_json(self, code: int, data: Any) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str) -> None:
        self._send_json(code, {"error": message})

    def _read_body(self, max_size: int = MAX_HTTP_BODY) -> bytes | None:
        length_str = self.headers.get("Content-Length")
        if length_str is None:
            self._send_error(411, "Content-Length required")
            return None
        try:
            length = int(length_str)
        except ValueError:
            self._send_error(400, "Invalid Content-Length")
            return None
        if length < 0:
            self._send_error(400, "Invalid Content-Length")
            return None
        if length > max_size:
            self._send_error(413, f"Request body exceeds limit of {max_size} bytes")
            return None
        self.connection.settimeout(30)
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self._send_error(408, "Request body timed out")
            return None
        if len(body) != length:
            self._send_error(400, "Incomplete request body")
            return None
        return body

    def _parse_json_body(self, max_size: int = MAX_HTTP_BODY) -> tuple[dict | None, bool]:
        """Returns (parsed_dict, success). Sends error response on failure."""
        raw = self._read_body(max_size)
        if raw is None:
            return None, False
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_error(400, f"Invalid JSON: {exc}")
            return None, False
        if not isinstance(data, dict):
            self._send_error(400, "Request body must be a JSON object")
            return None, False
        return data, True

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler's request line can contain arbitrary query secrets.
        log.info("HTTP %s %s", self.command, urllib.parse.urlparse(self.path).path)

    # --- Routing --------------------------------------------------------

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/health":
                self._handle_health()
            elif path == "/api/state":
                self._require_manager(self._handle_state)
            elif path.startswith("/api/images/"):
                self._require_manager(lambda: self._handle_image(path))
            elif path.startswith("/"):
                self._handle_static(path)
            else:
                self._send_error(404, "Not found")
        except Exception:
            log.exception("Unhandled error in GET %s", path)
            self._send_error(500, "Internal server error")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/analyze":
                self._require_manager(self._handle_analyze)
            elif path == "/api/snapshots":
                self._require_device(self._handle_snapshots)
            elif path == "/api/heartbeat":
                self._require_device(self._handle_heartbeat)
            elif re.match(r"^/api/alerts/[^/]+/ack$", path):
                self._require_manager(lambda: self._handle_alert_ack(path))
            else:
                self._send_error(404, "Not found")
        except Exception:
            log.exception("Unhandled error in POST %s", path)
            self._send_error(500, "Internal server error")

    def _require_manager(self, fn) -> None:
        if not self._auth_manager():
            self._send_error(401, "Unauthorized: manager token required")
            return
        fn()

    def _require_device(self, fn) -> None:
        if not self._auth_device():
            self._send_error(401, "Unauthorized: device token required")
            return
        fn()

    # --- Endpoint handlers ----------------------------------------------

    def _handle_health(self) -> None:
        self._send_json(200, {"status": "ok", "time": _now_iso()})

    def _handle_state(self) -> None:
        db = self.db
        analyses = db.execute(
            """SELECT id, created_at, filename, status, summary, objective, trigger, changes, warnings, model_info
               FROM analyses ORDER BY created_at DESC LIMIT 50"""
        ).fetchall()
        snapshots = db.execute(
            """SELECT id, created_at, device_id, printer_id, captured_at,
                      status, vision_status, vision_result
               FROM snapshots ORDER BY created_at DESC LIMIT 50"""
        ).fetchall()
        alerts = db.execute(
            """SELECT id, created_at, source, severity, message,
                      device_id, printer_id, snapshot_id, acknowledged
               FROM alerts ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        devices = db.execute(
            "SELECT device_id, printer_id, last_seen, last_status, last_error FROM devices"
        ).fetchall()

        def row_to_dict(r):
            return dict(r)

        analysis_rows = []
        for row in analyses:
            item = dict(row)
            for key, fallback in (("changes", []), ("warnings", []), ("model_info", {})):
                try:
                    item[key] = json.loads(item[key]) if item[key] else fallback
                except json.JSONDecodeError:
                    item[key] = fallback
            item["model"] = item.pop("model_info")
            analysis_rows.append(item)
        snapshot_rows = []
        for row in snapshots:
            item = dict(row)
            if item["vision_status"] == "complete":
                try:
                    item["vision_result"] = json.loads(item["vision_result"])
                except (TypeError, json.JSONDecodeError):
                    item["vision_status"] = "error"
                    item["vision_result"] = None
            snapshot_rows.append(item)
        stale_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - HEARTBEAT_STALE_SECS))
        device_rows = [dict(row, stale=row["last_seen"] < stale_cutoff) for row in devices]

        cfg = self.cfg
        config_info = {
            "ai_configured":           bool(cfg.get("ai_base_url") and cfg.get("ai_key")),
            "vision_configured":       bool(cfg.get("vision_model") or cfg.get("ai_model")),
            "notifications_configured": bool(cfg.get("webhook_url")),
        }

        self._send_json(200, {
            "analyses": analysis_rows,
            "snapshots": snapshot_rows,
            "alerts":    [row_to_dict(r) for r in alerts],
            "devices": device_rows,
            "config":    config_info,
        })

    def _handle_analyze(self) -> None:
        data, ok = self._parse_json_body(MAX_HTTP_BODY)
        if not ok:
            return

        # Validate required fields
        filename = data.get("filename")
        project_b64 = data.get("project_base64")
        settings_raw = data.get("settings", {})
        objective = data.get("objective", "quality")
        trigger = data.get("trigger", "manual")

        if not filename or not isinstance(filename, str):
            self._send_error(400, "'filename' must be a non-empty string")
            return
        # Sanitize filename (no path traversal)
        filename = Path(filename).name or "project.3mf"

        if not project_b64 or not isinstance(project_b64, str):
            self._send_error(400, "'project_base64' must be a non-empty base64 string")
            return

        if not isinstance(objective, str) or objective not in VALID_OBJECTIVES:
            self._send_error(400, f"'objective' must be one of {sorted(VALID_OBJECTIVES)}")
            return
        if not isinstance(trigger, str) or trigger not in VALID_TRIGGERS:
            self._send_error(400, f"'trigger' must be one of {sorted(VALID_TRIGGERS)}")
            return
        if not isinstance(settings_raw, dict):
            self._send_error(400, "'settings' must be a JSON object")
            return

        # Decode project
        try:
            project_bytes = base64.b64decode(project_b64, validate=True)
        except Exception:
            self._send_error(400, "'project_base64' is not valid base64")
            return

        if len(project_bytes) > MAX_DECODED_PROJECT:
            self._send_error(413, f"Decoded project exceeds {MAX_DECODED_PROJECT} bytes")
            return

        # Validate settings
        try:
            settings = validate_settings(settings_raw)
            has_nozzle = _nozzle_limit(settings_raw, settings)
        except ValueError as exc:
            self._send_error(400, f"Invalid settings: {exc}")
            return

        # Extract 3MF
        try:
            extracted = extract_3mf(project_bytes)
        except ExtractionError as exc:
            self._send_error(422, f"3MF extraction failed: {exc}")
            return

        # Save file privately
        analysis_id = str(uuid.uuid4())
        now = _now_iso()
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
        file_path = os.path.join(self.data_dir, "analyses", f"{analysis_id}_{safe_name}")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            with open(file_path, "wb") as fh:
                fh.write(project_bytes)
        except OSError as exc:
            self._send_error(500, f"Failed to store project file: {exc}")
            return

        # Call AI
        try:
            ai_result = ai_analyze(self.cfg, extracted, settings, objective, trigger, settings_raw)
        except AIError as exc:
            # Return explicit error – never simulate
            self.db.execute(
                """INSERT INTO analyses
                   (id, created_at, filename, status, summary, changes, warnings,
                    model_info, file_path, objective, trigger)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (analysis_id, now, filename, "error", str(exc),
                 "[]", "[]", "{}", file_path, objective, trigger),
            )
            self.db.commit()
            self._send_json(200, {
                "id": analysis_id,
                "status": "error",
                "summary": str(exc),
                "changes": [],
                "warnings": [],
                "model": {},
            })
            return

        # Validate AI output
        try:
            validated_changes = validate_changes(ai_result["changes"], settings)
            _nozzle_limit(settings_raw, {change["key"]: change["value"] for change in validated_changes})
        except ValueError as exc:
            err_msg = f"AI returned invalid changes: {exc}"
            log.warning("Analysis %s: %s", analysis_id, err_msg)
            self.db.execute(
                """INSERT INTO analyses
                   (id, created_at, filename, status, summary, changes, warnings,
                    model_info, file_path, objective, trigger)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (analysis_id, now, filename, "error", err_msg,
                 "[]", "[]", json.dumps(ai_result.get("model", {})),
                 file_path, objective, trigger),
            )
            self.db.commit()
            self._send_json(200, {
                "id": analysis_id,
                "status": "error",
                "summary": err_msg,
                "changes": [],
                "warnings": [],
                "model": ai_result.get("model", {}),
            })
            return

        # Persist successful analysis
        self.db.execute(
            """INSERT INTO analyses
               (id, created_at, filename, status, summary, changes, warnings,
                model_info, file_path, objective, trigger)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (analysis_id, now, filename, "complete",
             ai_result["summary"],
             json.dumps(validated_changes),
            json.dumps(ai_result["warnings"] + ([] if has_nozzle else ["No nozzle diameter supplied; A1 0.4 mm nozzle assumed for context."])),
             json.dumps(ai_result["model"]),
             file_path, objective, trigger),
        )
        self.db.commit()

        self._send_json(200, {
            "id": analysis_id,
            "status": "complete",
            "summary": ai_result["summary"],
            "changes": validated_changes,
            "warnings": ai_result["warnings"] + ([] if has_nozzle else ["No nozzle diameter supplied; A1 0.4 mm nozzle assumed for context."]),
            "model": ai_result["model"],
        })

    def _handle_snapshots(self) -> None:
        data, ok = self._parse_json_body(MAX_HTTP_BODY)
        if not ok:
            return

        device_id   = data.get("device_id")
        printer_id  = data.get("printer_id")
        captured_at = data.get("captured_at")
        image_b64   = data.get("image_base64")
        status      = data.get("status", "unknown")
        telemetry   = data.get("telemetry", {})

        if not device_id or not isinstance(device_id, str):
            self._send_error(400, "'device_id' must be a non-empty string")
            return
        if not printer_id or not isinstance(printer_id, str):
            self._send_error(400, "'printer_id' must be a non-empty string")
            return
        if not captured_at or not isinstance(captured_at, str):
            self._send_error(400, "'captured_at' must be a UTC ISO8601 string")
            return
        try:
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            valid_time = captured.tzinfo is not None and captured.utcoffset().total_seconds() == 0 and abs((datetime.now(timezone.utc) - captured).total_seconds()) <= 600
        except (ValueError, OverflowError):
            valid_time = False
        if not valid_time:
            self._send_error(400, "'captured_at' must be a UTC ISO8601 datetime")
            return
        if not isinstance(status, str) or status not in VALID_STATUSES:
            self._send_error(400, f"'status' must be one of {sorted(VALID_STATUSES)}")
            return
        if not image_b64 or not isinstance(image_b64, str):
            self._send_error(400, "'image_base64' must be a non-empty base64 JPEG string")
            return
        if not isinstance(telemetry, dict):
            self._send_error(400, "'telemetry' must be a JSON object")
            return

        # Decode image
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
        except Exception:
            self._send_error(400, "'image_base64' is not valid base64")
            return

        if len(image_bytes) > MAX_JPEG_BYTES:
            self._send_error(413, f"Image exceeds {MAX_JPEG_BYTES} bytes")
            return

        # Validate JPEG magic bytes
        if not image_bytes.startswith(b"\xff\xd8"):
            self._send_error(422, "image_base64 does not appear to be a JPEG (bad magic bytes)")
            return

        # Save image
        snap_id = str(uuid.uuid4())
        now = _now_iso()
        safe_dev = re.sub(r"[^a-zA-Z0-9_-]", "_", device_id)[:32]
        image_path = os.path.join(self.data_dir, "snapshots", f"{snap_id}_{safe_dev}.jpg")
        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        try:
            with open(image_path, "wb") as fh:
                fh.write(image_bytes)
        except OSError as exc:
            self._send_error(500, f"Failed to store image: {exc}")
            return

        # Save to DB
        self.db.execute(
            """INSERT INTO snapshots
               (id, created_at, device_id, printer_id, captured_at, status,
                telemetry, image_path, vision_status)
               VALUES (?,?,?,?,?,?,?,?,'pending')""",
            (snap_id, now, device_id, printer_id, captured_at, status,
             json.dumps(telemetry), image_path),
        )

        # Update device record
        self.db.execute(
            """INSERT INTO devices (device_id, printer_id, last_seen, last_status, last_error)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(device_id) DO UPDATE SET
                 printer_id=excluded.printer_id,
                 last_seen=excluded.last_seen,
                 last_status=excluded.last_status,
                 last_error=NULL""",
            (device_id, printer_id, now, status),
        )

        # Enqueue vision evaluation (if configured)
        if self.cfg.get("ai_base_url") and self.cfg.get("ai_key"):
            self.db.execute(
                """INSERT INTO vision_queue (id, snapshot_id, queued_at, next_at)
                   VALUES (?,?,?,?)""",
                (str(uuid.uuid4()), snap_id, now, now),
            )
        else:
            # Mark skipped immediately
            self.db.execute(
                "UPDATE snapshots SET vision_status='skipped' WHERE id=?", (snap_id,)
            )

        self.db.commit()

        self._send_json(201, {"id": snap_id, "status": "accepted"})

    def _handle_heartbeat(self) -> None:
        data, ok = self._parse_json_body(4096)
        if not ok:
            return

        device_id  = data.get("device_id")
        printer_id = data.get("printer_id")
        status     = data.get("status", "unknown")
        error      = data.get("error")  # optional

        if not device_id or not isinstance(device_id, str):
            self._send_error(400, "'device_id' must be a non-empty string")
            return
        if not printer_id or not isinstance(printer_id, str):
            self._send_error(400, "'printer_id' must be a non-empty string")
            return
        if not isinstance(status, str) or status not in VALID_STATUSES:
            self._send_error(400, f"'status' must be one of {sorted(VALID_STATUSES)}")
            return
        if error is not None and not isinstance(error, str):
            self._send_error(400, "'error' must be a string if provided")
            return
        # Reject anything that looks like a secret (key/token in the payload)
        for field in ("key", "token", "secret", "password", "api_key"):
            if field in data:
                self._send_error(400, f"Field {field!r} is not allowed in heartbeat")
                return

        now = _now_iso()
        self.db.execute(
            """INSERT INTO devices (device_id, printer_id, last_seen, last_status, last_error)
               VALUES (?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 printer_id=excluded.printer_id,
                 last_seen=excluded.last_seen,
                 last_status=excluded.last_status,
                 last_error=excluded.last_error""",
            (device_id, printer_id, now, status, error),
        )
        self.db.commit()

        # Raise alert if device reported error
        if error:
            msg = f"Device {device_id} reported error: {error[:500]}"
            alert_id = upsert_alert(
                self.db, "heartbeat", "error", msg, device_id, printer_id
            )
            if alert_id:
                alert_row = self.db.execute(
                    "SELECT * FROM alerts WHERE id=?", (alert_id,)
                ).fetchone()
                if alert_row:
                    send_webhook(self.cfg, alert_row, self.db)

        self._send_json(200, {"status": "ok"})

    def _handle_image(self, path: str) -> None:
        m = re.match(r"^/api/images/([a-zA-Z0-9_-]+)$", path)
        if not m:
            self._send_error(400, "Invalid image id")
            return
        image_id = m.group(1)
        row = self.db.execute(
            "SELECT image_path FROM snapshots WHERE id=?", (image_id,)
        ).fetchone()
        if not row:
            self._send_error(404, "Image not found")
            return
        image_path = row["image_path"]
        # Security: ensure path is within data_dir
        try:
            real_path = os.path.realpath(image_path)
            real_data = os.path.realpath(self.data_dir)
            if not real_path.startswith(real_data + os.sep):
                self._send_error(403, "Access denied")
                return
        except Exception:
            self._send_error(403, "Access denied")
            return

        try:
            with open(image_path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            self._send_error(404, "Image file not found on disk")
            return
        except OSError as exc:
            self._send_error(500, f"Failed to read image: {exc}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _handle_alert_ack(self, path: str) -> None:
        m = re.match(r"^/api/alerts/([a-zA-Z0-9_-]+)/ack$", path)
        if not m:
            self._send_error(400, "Invalid alert id")
            return
        alert_id = m.group(1)
        row = self.db.execute(
            "SELECT id FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if not row:
            self._send_error(404, "Alert not found")
            return
        self.db.execute(
            "UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,)
        )
        self.db.commit()
        self._send_json(200, {"status": "acknowledged"})

    def _handle_static(self, path: str) -> None:
        # Normalize path, prevent directory traversal
        if path == "/":
            path = "/index.html"
        # Strip leading slash and clean
        rel = path.lstrip("/")
        # Disallow .. and absolute paths
        if ".." in rel or rel.startswith("/"):
            self._send_error(403, "Forbidden")
            return
        file_path = os.path.join(self.web_dir, rel)
        real_file = os.path.realpath(file_path)
        real_web  = os.path.realpath(self.web_dir)
        if not real_file.startswith(real_web + os.sep) and real_file != real_web:
            self._send_error(403, "Forbidden")
            return
        if not os.path.isfile(file_path):
            self._send_error(404, "Not found")
            return
        mime, _ = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"
        try:
            with open(file_path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_error(500, "Failed to read file")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

def make_handler_class(cfg: dict, db_path: str, data_dir: str, web_dir: str):
    class Handler(PrintPilotHandler):
        pass
    Handler.cfg      = cfg
    Handler.db_path  = db_path
    Handler.data_dir = data_dir
    Handler.web_dir  = web_dir
    return Handler


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def run_server(cfg: dict) -> None:
    data_dir = os.path.expanduser(cfg["data_dir"])
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "analyses"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "snapshots"), exist_ok=True)

    db_path = os.path.join(data_dir, "printpilot.db")
    conn = open_db(db_path)
    conn.close()

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    handler = make_handler_class(cfg, db_path, data_dir, web_dir)

    # Start background workers
    VisionWorker(cfg, db_path).start()
    WebhookRetryWorker(cfg, db_path).start()
    HeartbeatMonitor(cfg, db_path).start()

    host = cfg["host"]
    port = cfg["port"]
    server = BoundedHTTPServer((host, port), handler)
    log.info("PrintPilot server listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        config_path = os.environ.get("PRINTPILOT_CONFIG", "server.json")
    else:
        config_path = sys.argv[1]

    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        log.error("Config file not found: %s", config_path)
        sys.exit(1)
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("Config error: %s", exc)
        sys.exit(1)

    run_server(cfg)


if __name__ == "__main__":
    main()
