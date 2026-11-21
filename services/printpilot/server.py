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
  webhook_url, telegram_bot_token, telegram_chat_id,
  tailscale_allowed_users (list of Tailscale login names allowed to manage
  without a token), tailscale_socket
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
import socket
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
VALID_STATUSES   = {"printing", "idle", "paused", "error", "unknown"}

# Printer telemetry accepted from the bridge (MQTT read-only); anything else is dropped
_TELEMETRY_NUMERIC = {"nozzle_temp", "bed_temp", "nozzle_target", "bed_target", "progress",
                      "current_layer", "total_layers", "remaining_min", "fan_speed",
                      "print_error", "frame_size", "itrack", "speed_level"}
_TELEMETRY_TEXT = {"gcode_state": 32, "job_name": 120, "light": 8}


def clean_telemetry(raw: Any) -> dict:
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if key in _TELEMETRY_NUMERIC and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                out[key] = value
        elif key in _TELEMETRY_TEXT and isinstance(value, str):
            out[key] = value[:_TELEMETRY_TEXT[key]]
    return out

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
    users = cfg.get("tailscale_allowed_users", [])
    if not isinstance(users, list) or not all(isinstance(u, str) and u.strip() for u in users):
        raise ValueError("tailscale_allowed_users must be a list of login names")
    cfg["tailscale_allowed_users"] = [u.strip().lower() for u in users]
    return cfg


# ---------------------------------------------------------------------------
# Runtime settings (editable from the dashboard, persisted to the config file)
# ---------------------------------------------------------------------------

DEFAULT_SNAPSHOT_RETENTION_HOURS = 48
DEFAULT_SNAPSHOT_INTERVAL = 120

# key -> (kind, default). kind: str | secret | bool | int:min:max | url
SETTINGS_SCHEMA: dict[str, tuple[str, Any]] = {
    "ai_base_url": ("url", ""),
    "ai_key": ("secret", ""),
    "ai_model": ("str", ""),
    "vision_model": ("str", ""),
    "vision_enabled": ("bool", True),
    "telegram_bot_token": ("secret", ""),
    "telegram_chat_id": ("str", ""),
    "webhook_url": ("url", ""),
    "capture_when_idle": ("bool", False),
    "snapshot_interval_seconds": ("int:60:3600", DEFAULT_SNAPSHOT_INTERVAL),
    "snapshot_retention_hours": ("int:1:720", DEFAULT_SNAPSHOT_RETENTION_HOURS),
}
_settings_lock = threading.Lock()


def setting(cfg: dict, key: str) -> Any:
    kind, default = SETTINGS_SCHEMA[key]
    value = cfg.get(key, default)
    return default if value is None else value


def public_settings(cfg: dict) -> dict:
    """Settings for the dashboard; secrets are reported only as set/unset."""
    out: dict[str, Any] = {}
    for key, (kind, _default) in SETTINGS_SCHEMA.items():
        if kind == "secret":
            out[key + "_set"] = bool(str(cfg.get(key) or "").strip())
        else:
            out[key] = setting(cfg, key)
    return out


def validate_settings_patch(patch: Any) -> dict:
    """Validate a partial update. Raises ValueError with a user-facing message."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("Không có thay đổi nào")
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in SETTINGS_SCHEMA:
            raise ValueError(f"Không hỗ trợ thiết lập {key!r}")
        kind, _ = SETTINGS_SCHEMA[key]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} phải là true/false")
        elif kind.startswith("int:"):
            _, lo, hi = kind.split(":")
            if isinstance(value, bool) or not isinstance(value, int) or not int(lo) <= value <= int(hi):
                raise ValueError(f"{key} phải là số nguyên từ {lo} đến {hi}")
        else:
            if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in "\r\n\x00"):
                raise ValueError(f"{key} không hợp lệ")
            value = value.strip()
            if kind == "url" and value:
                parsed = urllib.parse.urlparse(value)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError(f"{key} phải là địa chỉ http(s)")
        clean[key] = value
    return clean


def apply_settings(cfg: dict, patch: dict) -> None:
    """Update the live config and persist it atomically (0600) when a path is known."""
    with _settings_lock:
        path = cfg.get("_config_path")
        if path:
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            on_disk.update(patch)
            tmp = f"{path}.tmp-{os.getpid()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(on_disk, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        cfg.update(patch)


def bridge_policy(cfg: dict) -> dict:
    return {
        "capture_when_idle": bool(setting(cfg, "capture_when_idle")),
        "snapshot_interval_seconds": int(setting(cfg, "snapshot_interval_seconds")),
    }


# ---------------------------------------------------------------------------
# Tailscale identity (dashboard login without a token)
# ---------------------------------------------------------------------------

DEFAULT_TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"
_WHOIS_TTL = 60.0
_whois_cache: dict[str, tuple[float, dict | None]] = {}
_whois_lock = threading.Lock()


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0):
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def tailscale_whois(ip: str, socket_path: str = DEFAULT_TAILSCALE_SOCKET) -> dict | None:
    """Return {"login": ..., "node": ...} for a tailnet IP, or None. Cached briefly."""
    now = time.monotonic()
    with _whois_lock:
        hit = _whois_cache.get(ip)
        if hit and now - hit[0] < _WHOIS_TTL:
            return hit[1]
    result = None
    conn = None
    try:
        conn = _UnixHTTPConnection(socket_path)
        conn.request("GET", "/localapi/v0/whois?addr=" + urllib.parse.quote(f"{ip}:1"),
                     headers={"Host": "local-tailscaled.sock"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            data = json.loads(body)
            login = str((data.get("UserProfile") or {}).get("LoginName") or "").lower()
            node = str((data.get("Node") or {}).get("ComputedName") or "")
            if login:
                result = {"login": login, "node": node}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("Tailscale whois failed: %s", type(exc).__name__)
        return None  # do not cache transient failures
    finally:
        if conn is not None:
            conn.close()
    with _whois_lock:
        _whois_cache[ip] = (now, result)
    return result


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

CREATE TABLE IF NOT EXISTS printers (
    printer_id  TEXT PRIMARY KEY,
    device_id   TEXT,
    name        TEXT,
    model       TEXT,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL,
    telemetry   TEXT,          -- JSON object (latest)
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS printer_commands (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    printer_id  TEXT NOT NULL,
    device_id   TEXT,
    command     TEXT NOT NULL,
    params      TEXT,              -- JSON object
    status      TEXT NOT NULL,     -- pending|sent|done|failed|expired
    actor       TEXT,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_pending ON printer_commands(status, device_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    level       TEXT NOT NULL,     -- info|warning|error
    category    TEXT NOT NULL,     -- printer|alert|notify|settings|ai|device|system
    printer_id  TEXT,
    actor       TEXT,
    message     TEXT NOT NULL,
    data        TEXT               -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_printer ON events(printer_id, created_at);

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
    alert_cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    for col in ("snapshot_id", "acked_by", "acked_at"):
        if col not in alert_cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT")
    printer_cols = {row[1] for row in conn.execute("PRAGMA table_info(printers)")}
    if "offline" not in printer_cols:
        conn.execute("ALTER TABLE printers ADD COLUMN offline INTEGER NOT NULL DEFAULT 0")
    if "job_state" not in printer_cols:  # last definite status, ignoring 'unknown'
        conn.execute("ALTER TABLE printers ADD COLUMN job_state TEXT")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Printer control (queued here, executed by the LAN bridge over MQTT)
# ---------------------------------------------------------------------------

COMMAND_TTL_SECS = 30
SPEED_LEVELS = {1: "Êm", 2: "Chuẩn", 3: "Thể thao", 4: "Siêu tốc"}
# command -> statuses in which it is allowed (None = any non-offline state)
COMMAND_RULES: dict[str, set[str] | None] = {
    "pause": {"printing"},
    "resume": {"paused"},
    "stop": {"printing", "paused", "error"},
    "light": None,
    "speed": {"printing", "paused"},
}


def describe_command(command: str, params: dict) -> str:
    if command == "pause":
        return "tạm dừng in"
    if command == "resume":
        return "tiếp tục in"
    if command == "stop":
        return "dừng in"
    if command == "light":
        return "bật đèn" if params.get("mode") == "on" else "tắt đèn"
    if command == "speed":
        return f"đổi tốc độ sang {SPEED_LEVELS.get(params.get('level'), params.get('level'))}"
    return command


def validate_command(command: Any, params: Any, printer_status: str) -> dict:
    """Returns cleaned params; raises ValueError with a user-facing message."""
    if command not in COMMAND_RULES:
        raise ValueError("Lệnh không được hỗ trợ")
    params = params if isinstance(params, dict) else {}
    clean: dict[str, Any] = {}
    if command == "light":
        if params.get("mode") not in ("on", "off"):
            raise ValueError("Chế độ đèn phải là on hoặc off")
        clean["mode"] = params["mode"]
    elif command == "speed":
        level = params.get("level")
        if isinstance(level, bool) or level not in SPEED_LEVELS:
            raise ValueError("Mức tốc độ phải từ 1 đến 4")
        clean["level"] = level
    allowed = COMMAND_RULES[command]
    if printer_status == "offline":
        raise ValueError("Máy in đang mất kết nối")
    if allowed is not None and printer_status not in allowed:
        raise ValueError(f"Không thể {describe_command(command, clean)} khi máy đang ở trạng thái này")
    return clean


# ---------------------------------------------------------------------------
# Live camera (on demand, in memory only: never stored, never sent to AI)
# ---------------------------------------------------------------------------

LIVE_VIEWER_TTL = 20          # seconds a viewer request keeps the stream wanted
LIVE_MAX_SESSION = 5 * 60     # hard cap per viewer start
LIVE_FRAME_WAIT = 2.5         # long-poll wait for a newer frame
MAX_LIVE_FRAME_BYTES = 2 * 1024 * 1024


class LiveHub:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._sessions: dict[str, dict] = {}

    def want(self, printer_id: str) -> bool:
        """Viewer heartbeat. Returns True when this starts a new stream."""
        now = time.monotonic()
        with self._cond:
            sess = self._sessions.get(printer_id)
            fresh = sess is None or sess["wanted_until"] < now or now - sess["started"] > LIVE_MAX_SESSION
            if fresh:
                sess = {"started": now, "wanted_until": 0.0, "frame": None, "seq": 0, "at": 0.0}
                self._sessions[printer_id] = sess
            sess["wanted_until"] = min(now + LIVE_VIEWER_TTL, sess["started"] + LIVE_MAX_SESSION)
            return fresh

    def stop(self, printer_id: str) -> None:
        with self._cond:
            self._sessions.pop(printer_id, None)
            self._cond.notify_all()

    def wanted(self) -> list[str]:
        now = time.monotonic()
        with self._cond:
            for pid in [p for p, s in self._sessions.items() if s["wanted_until"] < now]:
                del self._sessions[pid]
            return sorted(self._sessions)

    def put(self, printer_id: str, frame: bytes) -> bool:
        with self._cond:
            sess = self._sessions.get(printer_id)
            if sess is None or sess["wanted_until"] < time.monotonic():
                return False
            sess["frame"] = frame
            sess["seq"] += 1
            sess["at"] = time.time()
            self._cond.notify_all()
            return True

    def get(self, printer_id: str, after: int, wait: float = LIVE_FRAME_WAIT):
        """Newest frame with seq > after, waiting briefly. Returns (seq, at, bytes) or None."""
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                sess = self._sessions.get(printer_id)
                if sess and sess["frame"] is not None and sess["seq"] > after:
                    return sess["seq"], sess["at"], sess["frame"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)


LIVE = LiveHub()


# ---------------------------------------------------------------------------
# Activity log (shown in the dashboard)
# ---------------------------------------------------------------------------

EVENT_CATEGORIES = {"printer", "alert", "notify", "settings", "ai", "device", "system"}
EVENT_RETENTION_SECS = 30 * 86400


def log_event(conn: sqlite3.Connection, category: str, message: str, level: str = "info",
              printer_id: str | None = None, actor: str | None = None,
              data: dict | None = None, commit: bool = True) -> None:
    """Record an activity event. Never pass secrets in message or data."""
    try:
        conn.execute(
            "INSERT INTO events (created_at, level, category, printer_id, actor, message, data) VALUES (?,?,?,?,?,?,?)",
            (_now_iso(), level, category, printer_id, actor, message[:1000],
             json.dumps(data, ensure_ascii=False, default=str) if data else None),
        )
        if commit:
            conn.commit()
    except sqlite3.Error:
        log.exception("Could not record event")
    log.log(logging.WARNING if level in ("warning", "error") else logging.INFO,
            "event %s/%s%s: %s", category, level, f" [{printer_id}]" if printer_id else "", message)


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
        "'summary' is one sentence. "
        "If Status is idle, judge only whether the bed is ready for the next print. "
        "Write 'issues' and 'summary' in Vietnamese."
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

def _dedup_key(source: str, device_id: str | None, message: str,
               printer_id: str | None = None) -> str:
    raw = f"{source}:{device_id or ''}:{printer_id or ''}:{message}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def upsert_alert(conn: sqlite3.Connection, source: str, severity: str,
                 message: str, device_id: str | None = None,
                 printer_id: str | None = None, snapshot_id: str | None = None) -> str | None:
    """
    Insert alert if dedup_key not already present and unacknowledged.
    Returns new alert id or None if deduped.
    """
    dk = _dedup_key(source, device_id, message, printer_id)
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
    log_event(conn, "alert", f"Cảnh báo mới: {message}",
              "error" if severity in ("critical", "error") else "warning",
              printer_id, data={"alert_id": alert_id, "source": source, "severity": severity})
    return alert_id


def telegram_configured(cfg: dict) -> bool:
    return bool(str(cfg.get("telegram_bot_token", "")).strip()
                and str(cfg.get("telegram_chat_id", "")).strip())


def notifications_configured(cfg: dict) -> bool:
    return bool(str(cfg.get("webhook_url", "")).strip()) or telegram_configured(cfg)


_SEVERITY_ICON = {"critical": "\U0001F6A8", "error": "\u274C", "warning": "\u26A0\uFE0F"}


_SEVERITY_LABEL = {"critical": "NGHIÊM TRỌNG", "error": "LỖI", "warning": "CẢNH BÁO", "info": "THÔNG TIN"}


def format_telegram_text(alert: sqlite3.Row, printer_name: str | None = None) -> str:
    sev = str(alert["severity"]).lower()
    icon = _SEVERITY_ICON.get(sev, "\u2139\uFE0F")
    where = printer_name or alert["printer_id"] or alert["device_id"] or ""
    title = f"{icon} {_SEVERITY_LABEL.get(sev, sev.upper())}" + (f" · {where}" if where else "")
    lines = [title, str(alert["message"] or "")[:3000]]
    if alert["device_id"] and alert["device_id"] != where:
        lines.append(f"Cầu nối: {alert['device_id']}")
    lines.append(f"Lúc: {alert['created_at']}")
    return "\n".join(lines)


def telegram_api(token: str, method: str, payload: dict | None = None) -> tuple[bool, Any]:
    """Call the Telegram Bot API. Returns (ok, result_or_description). Never logs the token."""
    conn_h = None
    try:
        import ssl
        conn_h = http.client.HTTPSConnection(
            "api.telegram.org", 443, context=ssl.create_default_context(), timeout=15)
        body = json.dumps(payload or {}).encode()
        conn_h.request("POST", f"/bot{token}/{method}", body=body,
                       headers={"Content-Type": "application/json", "User-Agent": "PrintPilot/1.0"})
        resp = conn_h.getresponse()
        raw = resp.read(1024 * 1024)
        try:
            data = json.loads(raw)
        except ValueError:
            return False, f"HTTP {resp.status}"
        if data.get("ok"):
            return True, data.get("result")
        return False, str(data.get("description") or f"HTTP {resp.status}")[:200]
    except Exception as exc:
        return False, type(exc).__name__
    finally:
        if conn_h is not None:
            conn_h.close()


def telegram_recent_chats(token: str) -> tuple[bool, Any]:
    ok, result = telegram_api(token, "getUpdates", {"limit": 50, "timeout": 0})
    if not ok:
        return ok, result
    chats: dict[str, dict] = {}
    for upd in result or []:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if isinstance(chat, dict) and "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                or chat.get("username") or str(chat["id"])
            chats[str(chat["id"])] = {"id": str(chat["id"]), "name": str(name)[:80], "type": chat.get("type", "")}
    return True, list(chats.values())


def send_telegram(cfg: dict, alert: sqlite3.Row, printer_name: str | None = None) -> bool:
    """Send alert text via Telegram Bot API. Never logs the bot token."""
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    payload = json.dumps({"chat_id": chat_id, "text": format_telegram_text(alert, printer_name),
                          "disable_web_page_preview": True}).encode()
    conn_h = None
    try:
        import ssl
        conn_h = http.client.HTTPSConnection(
            "api.telegram.org", 443, context=ssl.create_default_context(), timeout=15)
        conn_h.request("POST", f"/bot{token}/sendMessage", body=payload,
                       headers={"Content-Type": "application/json",
                                "User-Agent": "PrintPilot/1.0"})
        resp = conn_h.getresponse()
        resp.read()
        if resp.status >= 400:
            log.warning("Telegram delivery failed for alert %s: HTTP %s", alert["id"], resp.status)
            return False
        return True
    except Exception as exc:
        log.warning("Telegram delivery failed for alert %s: %s", alert["id"], type(exc).__name__)
        return False
    finally:
        if conn_h is not None:
            conn_h.close()


def send_webhook(cfg: dict, alert: sqlite3.Row, conn: sqlite3.Connection) -> None:
    """Deliver alert to every configured channel (webhook, Telegram).
    Updates DB on success/failure."""
    if not notifications_configured(cfg):
        return
    name_row = conn.execute("SELECT name FROM printers WHERE printer_id=?",
                            (alert["printer_id"],)).fetchone() if alert["printer_id"] else None
    printer_name = name_row["name"] if name_row and name_row["name"] else None
    url = cfg.get("webhook_url", "").strip()
    if not url:
        success = send_telegram(cfg, alert, printer_name)
        _record_delivery(conn, alert, success)
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

    if telegram_configured(cfg):
        success = send_telegram(cfg, alert, printer_name) and success
    _record_delivery(conn, alert, success)


def _record_delivery(conn: sqlite3.Connection, alert: sqlite3.Row, success: bool) -> None:
    if success:
        conn.execute(
            "UPDATE alerts SET webhook_sent=1 WHERE id=?", (alert["id"],)
        )
        log_event(conn, "notify", "Đã gửi cảnh báo ra ngoài (Telegram/webhook)",
                  printer_id=alert["printer_id"], data={"alert_id": alert["id"]}, commit=False)
    else:
        retries = alert["webhook_retries"] + 1
        if retries in (1, 5, 10):  # first failure and a couple of reminders, not every retry
            log_event(conn, "notify", f"Gửi cảnh báo thất bại (lần {retries}), sẽ thử lại",
                      "warning", alert["printer_id"], data={"alert_id": alert["id"]}, commit=False)
        # Exponential backoff: 2^retries * 60s, max 1h
        delay = min(2 ** retries * 60, 3600)
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
            if not setting(vision_cfg, "vision_enabled") or (
                    not vision_cfg.get("vision_model") and not vision_cfg.get("ai_base_url")):
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

            # Raise alert only while a job is active (idle-bed remarks are noise),
            # and keep at most one open vision alert per printer: the model words
            # the same issue differently each time, so message dedup is not enough.
            active = row["snap_status"] in ("printing", "paused")
            already_open = conn.execute(
                "SELECT 1 FROM alerts WHERE source='vision' AND printer_id=? AND acknowledged=0 LIMIT 1",
                (row["printer_id"],),
            ).fetchone()
            if not result.get("ok") and active and not already_open:
                issues = result.get("issues", [])
                msg = "Camera phát hiện sự cố: " + ("; ".join(issues) if issues else result.get("summary", "Có bất thường"))
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
        removed = prune_old_data(conn, self.cfg, self.cfg.get("_data_dir"))
        if removed["snapshots"] or removed["orphans"] or removed["analyses"]:
            log_event(conn, "system", f"Tự dọn dữ liệu: {removed['snapshots'] + removed['orphans']} ảnh, "
                                      f"{removed['analyses']} lượt phân tích", data=removed)
        cutoff_iso = _iso_ago(HEARTBEAT_STALE_SECS)
        for prow in conn.execute(
                "SELECT printer_id, name FROM printers WHERE last_seen < ? AND offline=0", (cutoff_iso,)).fetchall():
            conn.execute("UPDATE printers SET offline=1 WHERE printer_id=?", (prow["printer_id"],))
            log_event(conn, "printer", f"{prow['name'] or prow['printer_id']} mất kết nối", "warning",
                      prow["printer_id"])
        stale_cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - HEARTBEAT_STALE_SECS),
        )
        rows = conn.execute(
            "SELECT * FROM devices WHERE last_seen < ?", (stale_cutoff,)
        ).fetchall()
        for row in rows:
            msg = f"Thiết bị {row['device_id']} không gửi tín hiệu từ {row['last_seen']}"
            alert_id = upsert_alert(
                conn, "heartbeat", "warning", msg,
                row["device_id"], row["printer_id"]
            )
            if alert_id:
                alert_row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                if alert_row:
                    send_webhook(self.cfg, alert_row, conn)


def _iso_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def storage_stats(conn: sqlite3.Connection, data_dir: str | None) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    size = 0
    if data_dir:
        snap_dir = os.path.join(data_dir, "snapshots")
        try:
            with os.scandir(snap_dir) as it:
                for entry in it:
                    if entry.is_file():
                        size += entry.stat().st_size
        except OSError:
            pass
    oldest = conn.execute("SELECT MIN(created_at) FROM snapshots").fetchone()[0]
    return {"snapshot_count": count, "snapshot_bytes": size, "oldest_snapshot": oldest}


def prune_old_data(conn: sqlite3.Connection, cfg: dict | None = None,
                   data_dir: str | None = None) -> dict:
    """Delete expired data. Snapshots follow snapshot_retention_hours, except
    images attached to an alert, which live as long as the alert (14 days)."""
    removed = {"snapshots": 0, "analyses": 0, "orphans": 0}
    cutoff = _iso_ago(RETENTION_SECS)
    hours = setting(cfg, "snapshot_retention_hours") if cfg is not None else DEFAULT_SNAPSHOT_RETENTION_HOURS
    snap_cutoff = _iso_ago(int(hours) * 3600)
    plans = (
        ("analyses", "file_path",
         "SELECT id, file_path FROM analyses WHERE created_at < ?", (cutoff,)),
        ("snapshots", "image_path",
         """SELECT id, image_path FROM snapshots
            WHERE (created_at < ? AND id NOT IN (SELECT snapshot_id FROM alerts WHERE snapshot_id IS NOT NULL))
               OR created_at < ?""", (snap_cutoff, cutoff)),
    )
    for table, column, query, params in plans:
        rows = conn.execute(query, params).fetchall()
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
            removed[table] += 1
    conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
    conn.execute("DELETE FROM events WHERE created_at < ?", (_iso_ago(EVENT_RETENTION_SECS),))
    conn.execute("DELETE FROM printer_commands WHERE created_at < ?", (_iso_ago(7 * 86400),))
    conn.commit()
    # Image files left behind without a database row (crash mid-write, manual edits)
    if data_dir:
        snap_dir = os.path.join(data_dir, "snapshots")
        known = {r[0] for r in conn.execute("SELECT image_path FROM snapshots")}
        grace = time.time() - 600
        try:
            with os.scandir(snap_dir) as it:
                for entry in it:
                    if (entry.is_file() and entry.name.endswith(".jpg") and entry.path not in known
                            and entry.stat().st_mtime < grace):
                        try:
                            os.remove(entry.path)
                            removed["orphans"] += 1
                        except OSError:
                            pass
        except OSError:
            pass
    return removed


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
        if self._check_token(self.cfg["manager_token"]):
            return True
        return self._tailscale_user() is not None

    def _actor(self) -> str:
        if self._check_token(self.cfg["manager_token"]):
            return "mã quản lý"
        ident = self._tailscale_user()
        return ident["login"] if ident else "không rõ"

    def _client_ip(self) -> str:
        """Peer IP; behind the local reverse proxy (Caddy), the last X-Forwarded-For hop."""
        peer = self.client_address[0]
        trusted = {"127.0.0.1", "::1", str(self.cfg.get("host", ""))}
        if peer in trusted:
            xff = self.headers.get("X-Forwarded-For", "")
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops:
                return hops[-1]
        return peer

    def _tailscale_user(self) -> dict | None:
        allowed = self.cfg.get("tailscale_allowed_users") or []
        if not allowed:
            return None
        # Ambient (IP-based) auth: state-changing requests must prove same-origin
        # so another site open in the browser cannot trigger them (CSRF).
        if self.command not in ("GET", "HEAD"):
            if self.headers.get("X-PrintPilot-Request") != "1":
                return None
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return None
        ident = tailscale_whois(self._client_ip(),
                                self.cfg.get("tailscale_socket", DEFAULT_TAILSCALE_SOCKET))
        if ident and ident["login"] in allowed:
            return ident
        return None

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
            elif path == "/api/whoami":
                self._handle_whoami()
            elif path == "/api/state":
                self._require_manager(self._handle_state)
            elif path == "/api/settings":
                self._require_manager(self._handle_settings_get)
            elif path == "/api/events":
                self._require_manager(self._handle_events)
            elif re.match(r"^/api/commands/[a-zA-Z0-9-]+$", path):
                self._require_manager(lambda: self._handle_command_status(path))
            elif re.match(r"^/api/printers/[^/]+/live/frame$", path):
                self._require_manager(lambda: self._handle_live_frame_get(path))
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
            elif path == "/api/settings":
                self._require_manager(self._handle_settings_update)
            elif path == "/api/settings/test-telegram":
                self._require_manager(self._handle_test_telegram)
            elif path == "/api/settings/telegram-chats":
                self._require_manager(self._handle_telegram_chats)
            elif path == "/api/settings/test-ai":
                self._require_manager(self._handle_test_ai)
            elif path == "/api/maintenance/cleanup":
                self._require_manager(self._handle_cleanup)
            elif re.match(r"^/api/printers/[^/]+/commands$", path):
                self._require_manager(lambda: self._handle_command_create(path))
            elif path == "/api/device/commands/poll":
                self._require_device_only(self._handle_command_poll)
            elif path == "/api/device/commands/result":
                self._require_device_only(self._handle_command_result)
            elif re.match(r"^/api/printers/[^/]+/live$", path):
                self._require_manager(lambda: self._handle_live_request(path))
            elif path == "/api/device/live/frame":
                self._require_device_only(self._handle_live_frame_put)
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

    def _require_device_only(self, fn) -> None:
        # Command execution endpoints: the device token only, never the manager token
        token = self._get_bearer()
        if not token or not secrets.compare_digest(token, self.cfg["device_token"]):
            self._send_error(401, "Unauthorized: device token required")
            return
        fn()

    def _require_device(self, fn) -> None:
        if not self._auth_device():
            self._send_error(401, "Unauthorized: device token required")
            return
        fn()

    # --- Endpoint handlers ----------------------------------------------

    # --- Printer control ------------------------------------------------

    def _expire_commands(self) -> None:
        cutoff = _iso_ago(COMMAND_TTL_SECS)
        rows = self.db.execute(
            "SELECT id, printer_id, command, params FROM printer_commands WHERE status='pending' AND created_at < ?",
            (cutoff,)).fetchall()
        for row in rows:
            self.db.execute("UPDATE printer_commands SET status='expired', updated_at=?, result=? WHERE id=?",
                            (_now_iso(), "Cầu nối không nhận lệnh kịp", row["id"]))
            log_event(self.db, "printer",
                      f"Lệnh {describe_command(row['command'], json.loads(row['params'] or '{}'))} hết hạn (cầu nối không phản hồi)",
                      "warning", row["printer_id"], commit=False)
        if rows:
            self.db.commit()

    def _handle_command_create(self, path: str) -> None:
        printer_id = urllib.parse.unquote(path.split("/")[3])
        data, ok = self._parse_json_body(4096)
        if not ok:
            return
        printer = self.db.execute(
            "SELECT printer_id, device_id, status, last_seen, name FROM printers WHERE printer_id=?",
            (printer_id,)).fetchone()
        if not printer:
            self._send_error(404, "Không tìm thấy máy in")
            return
        status = "offline" if printer["last_seen"] < _iso_ago(HEARTBEAT_STALE_SECS) else printer["status"]
        command = data.get("command")
        try:
            params = validate_command(command, data.get("params"), status)
        except ValueError as exc:
            self._send_error(400, str(exc))
            return
        self._expire_commands()
        busy = self.db.execute(
            "SELECT 1 FROM printer_commands WHERE printer_id=? AND status IN ('pending','sent') LIMIT 1",
            (printer_id,)).fetchone()
        if busy:
            self._send_error(409, "Máy đang xử lý một lệnh khác, thử lại sau vài giây")
            return
        cmd_id = str(uuid.uuid4())
        now = _now_iso()
        actor = self._actor()
        self.db.execute(
            """INSERT INTO printer_commands (id, created_at, updated_at, printer_id, device_id, command, params, status, actor)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (cmd_id, now, now, printer_id, printer["device_id"], command, json.dumps(params), "pending", actor))
        self.db.commit()
        log_event(self.db, "printer", f"Yêu cầu {describe_command(command, params)} cho {printer['name'] or printer_id}",
                  printer_id=printer_id, actor=actor, data={"command_id": cmd_id, "command": command, "params": params})
        self._send_json(202, {"id": cmd_id, "status": "pending"})

    def _handle_command_status(self, path: str) -> None:
        self._expire_commands()
        cmd_id = path.rsplit("/", 1)[1]
        row = self.db.execute(
            "SELECT id, printer_id, command, status, result, created_at, updated_at FROM printer_commands WHERE id=?",
            (cmd_id,)).fetchone()
        if not row:
            self._send_error(404, "Không tìm thấy lệnh")
            return
        self._send_json(200, dict(row))

    def _handle_command_poll(self) -> None:
        data, ok = self._parse_json_body(1024)
        if not ok:
            return
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            self._send_error(400, "'device_id' required")
            return
        self._expire_commands()
        rows = self.db.execute(
            """SELECT c.id, c.printer_id, c.command, c.params FROM printer_commands c
               WHERE c.status='pending' AND c.device_id=? ORDER BY c.created_at LIMIT 10""",
            (device_id,)).fetchall()
        now = _now_iso()
        commands = []
        for row in rows:
            self.db.execute("UPDATE printer_commands SET status='sent', updated_at=? WHERE id=?", (now, row["id"]))
            commands.append({"id": row["id"], "printer_id": row["printer_id"], "command": row["command"],
                             "params": json.loads(row["params"] or "{}")})
        if rows:
            self.db.commit()
        mine = {r["printer_id"] for r in self.db.execute(
            "SELECT printer_id FROM printers WHERE device_id=?", (device_id,))}
        self._send_json(200, {"commands": commands, "live": [p for p in LIVE.wanted() if p in mine]})

    def _handle_command_result(self) -> None:
        data, ok = self._parse_json_body(4096)
        if not ok:
            return
        cmd_id = data.get("id")
        success = data.get("ok")
        reason = data.get("reason") or ""
        if not isinstance(cmd_id, str) or not isinstance(success, bool) or not isinstance(reason, str):
            self._send_error(400, "'id', 'ok' and 'reason' required")
            return
        row = self.db.execute(
            "SELECT id, printer_id, command, params, status FROM printer_commands WHERE id=?", (cmd_id,)).fetchone()
        if not row or row["status"] != "sent":
            self._send_error(404, "Command not awaiting a result")
            return
        status = "done" if success else "failed"
        self.db.execute("UPDATE printer_commands SET status=?, result=?, updated_at=? WHERE id=?",
                        (status, reason[:300], _now_iso(), cmd_id))
        self.db.commit()
        what = describe_command(row["command"], json.loads(row["params"] or "{}"))
        if success:
            log_event(self.db, "printer", f"Máy in đã thực hiện lệnh {what}", printer_id=row["printer_id"],
                      data={"command_id": cmd_id})
        else:
            log_event(self.db, "printer", f"Máy in từ chối lệnh {what}: {reason[:200]}", "warning",
                      row["printer_id"], data={"command_id": cmd_id})
        self._send_json(200, {"status": status})

    # --- Live camera ----------------------------------------------------

    def _handle_live_request(self, path: str) -> None:
        printer_id = urllib.parse.unquote(path.split("/")[3])
        data, ok = self._parse_json_body(1024)
        if not ok:
            return
        printer = self.db.execute("SELECT name, last_seen FROM printers WHERE printer_id=?",
                                  (printer_id,)).fetchone()
        if not printer:
            self._send_error(404, "Không tìm thấy máy in")
            return
        if data.get("action") == "stop":
            LIVE.stop(printer_id)
            self._send_json(200, {"live": False})
            return
        if printer["last_seen"] < _iso_ago(HEARTBEAT_STALE_SECS):
            self._send_error(400, "Máy in đang mất kết nối")
            return
        if LIVE.want(printer_id):
            log_event(self.db, "printer", f"Bắt đầu xem camera trực tiếp {printer['name'] or printer_id}",
                      printer_id=printer_id, actor=self._actor())
        self._send_json(200, {"live": True, "renew_seconds": LIVE_VIEWER_TTL // 2})

    def _handle_live_frame_get(self, path: str) -> None:
        printer_id = urllib.parse.unquote(path.split("/")[3])
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            after = int((q.get("after") or ["0"])[0])
        except ValueError:
            after = 0
        got = LIVE.get(printer_id, after)
        if got is None:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        seq, at, frame = got
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Seq", str(seq))
        self.send_header("X-Frame-Time", f"{at:.3f}")
        self.end_headers()
        self.wfile.write(frame)

    def _handle_live_frame_put(self) -> None:
        data, ok = self._parse_json_body(MAX_LIVE_FRAME_BYTES * 2)
        if not ok:
            return
        printer_id = data.get("printer_id")
        if not isinstance(printer_id, str) or not isinstance(data.get("image_base64"), str):
            self._send_error(400, "'printer_id' and 'image_base64' required")
            return
        try:
            frame = base64.b64decode(data["image_base64"], validate=True)
        except Exception:
            self._send_error(400, "Invalid base64")
            return
        if not frame.startswith(b"\xff\xd8") or len(frame) > MAX_LIVE_FRAME_BYTES:
            self._send_error(400, "Frame must be a JPEG under 2 MiB")
            return
        accepted = LIVE.put(printer_id, frame)
        self._send_json(200, {"accepted": accepted})

    # --- Activity log ---------------------------------------------------

    def _handle_events(self) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        where, params = [], []
        printer = (q.get("printer") or [""])[0]
        if printer == "_system":
            where.append("printer_id IS NULL")
        elif printer:
            where.append("printer_id = ?")
            params.append(printer)
        category = (q.get("category") or [""])[0]
        if category:
            if category not in EVENT_CATEGORIES:
                self._send_error(400, "Unknown category")
                return
            where.append("category = ?")
            params.append(category)
        if (q.get("level") or [""])[0] == "problems":
            where.append("level IN ('warning','error')")
        before = (q.get("before") or [""])[0]
        if before:
            try:
                params.append(int(before))
                where.append("id < ?")
            except ValueError:
                self._send_error(400, "Invalid cursor")
                return
        try:
            limit = max(1, min(200, int((q.get("limit") or ["50"])[0])))
        except ValueError:
            limit = 50
        sql = ("SELECT id, created_at, level, category, printer_id, actor, message, data FROM events"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY id DESC LIMIT ?")
        rows = self.db.execute(sql, (*params, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            try:
                item["data"] = json.loads(item["data"]) if item["data"] else None
            except json.JSONDecodeError:
                item["data"] = None
            items.append(item)
        self._send_json(200, {"events": items,
                              "next": items[-1]["id"] if len(rows) > limit else None})

    # --- Settings -------------------------------------------------------

    def _handle_settings_get(self) -> None:
        self._send_json(200, {"settings": public_settings(self.cfg),
                              "storage": storage_stats(self.db, self.data_dir)})

    def _handle_settings_update(self) -> None:
        data, ok = self._parse_json_body(16 * 1024)
        if not ok:
            return
        try:
            patch = validate_settings_patch(data)
            apply_settings(self.cfg, patch)
        except ValueError as exc:
            self._send_error(400, str(exc))
            return
        except OSError:
            log.exception("Could not persist settings")
            self._send_error(500, "Không lưu được cấu hình")
            return
        log_event(self.db, "settings", "Cập nhật cài đặt: " + ", ".join(sorted(patch)),
                  actor=self._actor(), data={"keys": sorted(patch)})
        self._send_json(200, {"settings": public_settings(self.cfg)})

    def _telegram_token(self, data: dict | None) -> str:
        # Allow testing a token typed in the form before it is saved
        candidate = (data or {}).get("telegram_bot_token")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return str(self.cfg.get("telegram_bot_token") or "").strip()

    def _handle_test_telegram(self) -> None:
        data, ok = self._parse_json_body(4096)
        if not ok:
            return
        token = self._telegram_token(data)
        chat_id = str(data.get("telegram_chat_id") or self.cfg.get("telegram_chat_id") or "").strip()
        if not token or not chat_id:
            self._send_error(400, "Cần bot token và chat id")
            return
        sent, detail = telegram_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "\u2705 PrintPilot: tin nhắn thử. Cảnh báo máy in sẽ được gửi về đây.",
        })
        if sent:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(200, {"ok": False, "error": detail})

    def _handle_telegram_chats(self) -> None:
        data, ok = self._parse_json_body(4096)
        if not ok:
            return
        token = self._telegram_token(data)
        if not token:
            self._send_error(400, "Cần bot token")
            return
        found, result = telegram_recent_chats(token)
        if found:
            self._send_json(200, {"ok": True, "chats": result})
        else:
            self._send_json(200, {"ok": False, "error": result})

    def _handle_test_ai(self) -> None:
        data, ok = self._parse_json_body(1024)
        if not ok:
            return
        base = str(self.cfg.get("ai_base_url") or "").strip()
        key = str(self.cfg.get("ai_key") or "").strip()
        model = str(self.cfg.get("ai_model") or "").strip()
        if not base or not key or not model:
            self._send_error(400, "Cần địa chỉ AI, khóa và model")
            return
        started = time.monotonic()
        try:
            response = _openai_post(base, key, "/chat/completions", {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
                "max_tokens": 5,
                "stream": False,
            })
            reply = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self._send_json(200, {"ok": False, "error": "Phản hồi AI không đúng định dạng"})
            return
        except AIError as exc:
            self._send_json(200, {"ok": False, "error": str(exc)[:200]})
            return
        self._send_json(200, {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000),
                              "reply": str(reply)[:40]})

    def _handle_cleanup(self) -> None:
        data, ok = self._parse_json_body(1024)
        if not ok:
            return
        removed = prune_old_data(self.db, self.cfg, self.data_dir)
        log_event(self.db, "system", f"Dọn dữ liệu thủ công: {removed['snapshots'] + removed['orphans']} ảnh",
                  actor=self._actor(), data=removed)
        self._send_json(200, {"removed": removed, "storage": storage_stats(self.db, self.data_dir)})

    def _handle_whoami(self) -> None:
        ident = self._tailscale_user()
        if ident:
            self._send_json(200, {"authenticated": True, "method": "tailscale",
                                  "user": ident["login"], "node": ident["node"]})
        elif self._check_token(self.cfg["manager_token"]):
            self._send_json(200, {"authenticated": True, "method": "token"})
        else:
            self._send_json(200, {"authenticated": False,
                                  "tailscale_enabled": bool(self.cfg.get("tailscale_allowed_users"))})

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
                      status, telemetry, vision_status, vision_result
               FROM snapshots ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        alerts = db.execute(
            """SELECT id, created_at, source, severity, message,
                      device_id, printer_id, snapshot_id, acknowledged, acked_by, acked_at
               FROM alerts ORDER BY created_at DESC LIMIT 200"""
        ).fetchall()
        devices = db.execute(
            "SELECT device_id, printer_id, last_seen, last_status, last_error FROM devices"
        ).fetchall()
        printers = db.execute(
            """SELECT printer_id, device_id, name, model, last_seen, status, telemetry, last_error
               FROM printers ORDER BY printer_id"""
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
            try:
                item["telemetry"] = json.loads(item["telemetry"]) if item["telemetry"] else {}
            except json.JSONDecodeError:
                item["telemetry"] = {}
            if item["vision_status"] == "complete":
                try:
                    item["vision_result"] = json.loads(item["vision_result"])
                except (TypeError, json.JSONDecodeError):
                    item["vision_status"] = "error"
                    item["vision_result"] = None
            snapshot_rows.append(item)
        stale_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - HEARTBEAT_STALE_SECS))
        device_rows = [dict(row, stale=row["last_seen"] < stale_cutoff) for row in devices]
        printer_rows = []
        for row in printers:
            item = dict(row, stale=row["last_seen"] < stale_cutoff)
            try:
                item["telemetry"] = json.loads(item["telemetry"]) if item["telemetry"] else {}
            except json.JSONDecodeError:
                item["telemetry"] = {}
            printer_rows.append(item)

        cfg = self.cfg
        config_info = {
            "ai_configured":           bool(cfg.get("ai_base_url") and cfg.get("ai_key")),
            "vision_configured":       bool(setting(cfg, "vision_enabled") and (cfg.get("vision_model") or cfg.get("ai_model"))),
            "notifications_configured": notifications_configured(cfg),
        }

        self._send_json(200, {
            "analyses": analysis_rows,
            "snapshots": snapshot_rows,
            "alerts":    [row_to_dict(r) for r in alerts],
            "devices": device_rows,
            "printers": printer_rows,
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
            log_event(self.db, "ai", f"Phân tích AI lỗi cho {filename}: {str(exc)[:200]}", "error",
                      data={"analysis_id": analysis_id, "trigger": trigger})
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
                (analysis_id, now, filename, "error", err_msg[:2000],
                 "[]", "[]", json.dumps(ai_result.get("model", {})),
                 file_path, objective, trigger),
            )
            self.db.commit()
            log_event(self.db, "ai", f"AI trả về đề xuất không hợp lệ cho {filename}", "error",
                      data={"analysis_id": analysis_id, "trigger": trigger})
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
        log_event(self.db, "ai", f"AI đề xuất {len(validated_changes)} thay đổi cho {filename}",
                  data={"analysis_id": analysis_id, "trigger": trigger, "objective": objective})

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
        telemetry = clean_telemetry(telemetry)

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
        telemetry  = data.get("telemetry", {})
        name       = data.get("printer_name", "")
        model      = data.get("printer_model", "")

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
        if not isinstance(telemetry, dict):
            self._send_error(400, "'telemetry' must be a JSON object")
            return
        if not isinstance(name, str) or not isinstance(model, str):
            self._send_error(400, "'printer_name'/'printer_model' must be strings")
            return
        telemetry = clean_telemetry(telemetry)
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
        prev = self.db.execute(
            "SELECT telemetry, status, offline, job_state, name FROM printers WHERE printer_id=?", (printer_id,)
        ).fetchone()
        if not telemetry and prev and prev["telemetry"]:
            # Keep last known numbers on camera-only heartbeats; status says if fresh
            telemetry_json = prev["telemetry"]
        else:
            telemetry_json = json.dumps(telemetry)
        self.db.execute(
            """INSERT INTO printers (printer_id, device_id, name, model, last_seen, status, telemetry, last_error)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(printer_id) DO UPDATE SET
                 device_id=excluded.device_id,
                 name=COALESCE(NULLIF(excluded.name,''), printers.name),
                 model=COALESCE(NULLIF(excluded.model,''), printers.model),
                 last_seen=excluded.last_seen,
                 status=excluded.status,
                 telemetry=excluded.telemetry,
                 last_error=excluded.last_error,
                 offline=0""",
            (printer_id, device_id, name[:80], model[:40], now, status, telemetry_json, error),
        )
        if status != "unknown":
            self.db.execute("UPDATE printers SET job_state=? WHERE printer_id=?", (status, printer_id))
        self.db.commit()
        self._log_printer_transition(prev, printer_id, name, status, telemetry, device_id)

        # Printer-reported failure (MQTT print_error / FAILED state)
        print_error = telemetry.get("print_error")
        newly_failed = prev is None or prev["status"] != "error"
        if (print_error or status == "error") and newly_failed:
            label = name or printer_id
            code = f" (mã lỗi {print_error})" if print_error else ""
            msg = f"Máy in {label} báo lỗi khi in{code}"
            alert_id = upsert_alert(self.db, "printer", "critical", msg, device_id, printer_id)
            if alert_id:
                alert_row = self.db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
                if alert_row:
                    send_webhook(self.cfg, alert_row, self.db)

        # A healthy heartbeat resolves this device's earlier heartbeat alerts
        # (stale-heartbeat / reported-error); vision alerts stay for a human.
        if not error:
            self.db.execute(
                """UPDATE alerts SET acknowledged=1
                   WHERE source='heartbeat' AND device_id=? AND acknowledged=0
                     AND (printer_id=? OR message LIKE '%không gửi tín hiệu%')""",
                (device_id, printer_id),
            )
            self.db.commit()

        # Raise alert if device reported error
        if error:
            msg = f"Thiết bị {device_id} báo lỗi: {error[:500]}"
            alert_id = upsert_alert(
                self.db, "heartbeat", "error", msg, device_id, printer_id
            )
            if alert_id:
                alert_row = self.db.execute(
                    "SELECT * FROM alerts WHERE id=?", (alert_id,)
                ).fetchone()
                if alert_row:
                    send_webhook(self.cfg, alert_row, self.db)

        self._send_json(200, {"status": "ok", "policy": bridge_policy(self.cfg)})

    def _log_printer_transition(self, prev, printer_id: str, name: str, status: str,
                                telemetry: dict, device_id: str) -> None:
        label = name or (prev["name"] if prev and prev["name"] else printer_id)
        job = telemetry.get("job_name")
        job_txt = f" «{job}»" if job else ""
        if prev is None:
            log_event(self.db, "device", f"Phát hiện máy in mới: {label} (qua {device_id})",
                      printer_id=printer_id)
            return
        if prev["offline"]:
            log_event(self.db, "printer", f"{label} đã kết nối lại", printer_id=printer_id)
        before = prev["job_state"]
        if status == "unknown" or status == before:
            return
        if status == "printing":
            msg = f"{label} tiếp tục in{job_txt}" if before == "paused" else f"{label} bắt đầu in{job_txt}"
            log_event(self.db, "printer", msg, printer_id=printer_id, data={"job": job})
        elif status == "paused":
            log_event(self.db, "printer", f"{label} tạm dừng in{job_txt}", "warning", printer_id)
        elif status == "error":
            code = telemetry.get("print_error")
            log_event(self.db, "printer", f"{label} gặp lỗi khi in{job_txt}" + (f" (mã {code})" if code else ""),
                      "error", printer_id, data={"print_error": code})
        elif status == "idle" and before in ("printing", "paused", "error"):
            done = telemetry.get("gcode_state") == "FINISH" or (telemetry.get("progress") or 0) >= 100
            log_event(self.db, "printer", f"{label} {'hoàn thành' if done else 'đã dừng'} bản in{job_txt}",
                      printer_id=printer_id, data={"progress": telemetry.get("progress"), "job": job})

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
            "SELECT id, message, printer_id, acknowledged FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if not row:
            self._send_error(404, "Alert not found")
            return
        actor = self._actor()
        self.db.execute(
            "UPDATE alerts SET acknowledged=1, acked_by=?, acked_at=? WHERE id=?",
            (actor, _now_iso(), alert_id),
        )
        self.db.commit()
        if not row["acknowledged"]:
            log_event(self.db, "alert", f"Đã xử lý cảnh báo: {row['message']}",
                      printer_id=row["printer_id"], actor=actor, data={"alert_id": alert_id})
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
        # Dashboard is fully self-hosted: lock it to same-origin resources
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
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

    cfg["_data_dir"] = data_dir
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(os.path.join(data_dir, "printpilot.log"),
                                       maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                                "%Y-%m-%dT%H:%M:%SZ"))
    logging.getLogger().addHandler(file_handler)
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
    start_conn = open_db(db_path)
    log_event(start_conn, "system", "Máy chủ PrintPilot khởi động")
    start_conn.close()
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

    cfg["_config_path"] = os.path.abspath(config_path)
    run_server(cfg)


if __name__ == "__main__":
    main()
