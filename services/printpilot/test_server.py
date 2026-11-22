#!/usr/bin/env python3
"""
test_server.py – unittest suite for PrintPilot server.

Covers:
  - Auth: missing/wrong/correct manager and device tokens
  - GET /health (public)
  - POST /api/analyze: malformed inputs, bad base64, bad 3MF, settings bounds,
    AI provider failure, AI invalid response structure, success path
  - POST /api/snapshots: auth, missing fields, bad image, non-JPEG, too-large
  - POST /api/heartbeat: auth, missing fields, secret field rejection
  - GET /api/state: auth, structure
  - GET /api/images/{id}: auth, not found, path traversal
  - POST /api/alerts/{id}/ack: auth, not found, success
  - Dedup: same alert twice stays one open alert
  - Webhook retry: marks sent on success, schedules retry on failure
  - Settings allowlist: numeric bounds, string enums, unknown keys dropped
  - 3MF extraction: zip bomb, too-large, invalid zip, missing entries
  - Vision queue: enqueued on snapshot, skipped when AI unconfigured
  - Heartbeat stale (HeartbeatMonitor logic)

The fake AI provider is an in-process HTTP server that returns configurable
JSON without touching any real AI endpoint.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import http.client
import http.server
import urllib.request
import uuid

# Ensure the package is importable
sys.path.insert(0, os.path.dirname(__file__))

from server import (
    SETTINGS_ALLOWLIST,
    ExtractionError,
    _dedup_key,
    _parse_ini_config,
    _parse_model_geometry,
    extract_3mf,
    load_config,
    make_handler_class,
    open_db,
    upsert_alert,
    validate_changes,
    validate_settings,
    _now_iso,
    HEARTBEAT_STALE_SECS,
    HeartbeatMonitor,
    ai_vision, AIError, VisionWorker,
    prune_old_data,
    build_preview_mesh, best_thumbnail, display_filename, settings_from_config, _paint_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MANAGER_TOKEN = "test-manager-token-abc123"
DEVICE_TOKEN  = "test-device-token-xyz789"


def _make_config(data_dir: str, ai_base_url: str = "", ai_key: str = "",
                 ai_model: str = "gpt-test", vision_model: str = "",
                 webhook_url: str = "") -> dict:
    return {
        "host": "127.0.0.1",
        "port": 0,
        "data_dir": data_dir,
        "manager_token": MANAGER_TOKEN,
        "device_token": DEVICE_TOKEN,
        "ai_base_url": ai_base_url,
        "ai_key": ai_key,
        "ai_model": ai_model,
        "vision_model": vision_model,
        "webhook_url": webhook_url,
    }


def _start_server(cfg: dict, data_dir: str) -> tuple[http.server.HTTPServer, int]:
    """Start a ThreadingHTTPServer on a random port. Returns (server, port)."""
    db_path = os.path.join(data_dir, "printpilot.db")
    web_dir = os.path.join(os.path.dirname(__file__), "web")
    os.makedirs(web_dir, exist_ok=True)
    handler = make_handler_class(cfg, db_path, data_dir, web_dir)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _req(port: int, method: str, path: str, body: bytes | None = None,
         token: str | None = MANAGER_TOKEN,
         content_type: str = "application/json") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
    conn.request(method, path, body=body, headers=headers)
    return conn.getresponse()


def _jreq(port: int, method: str, path: str, data: dict,
           token: str | None = MANAGER_TOKEN) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    resp = _req(port, method, path, body=body, token=token)
    status = resp.status
    raw = resp.read()
    return status, json.loads(raw)


def _make_minimal_3mf() -> bytes:
    """Create a minimal valid 3MF zip for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        model_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources>'
            '<object id="1" type="model">'
            '<mesh>'
            '<vertices>'
            '<vertex x="0" y="0" z="0"/>'
            '<vertex x="10" y="0" z="0"/>'
            '<vertex x="5" y="10" z="0"/>'
            '<vertex x="5" y="5" z="10"/>'
            '</vertices>'
            '<triangles>'
            '<triangle v1="0" v2="1" v3="2"/>'
            '<triangle v1="0" v2="1" v3="3"/>'
            '<triangle v1="1" v2="2" v3="3"/>'
            '<triangle v1="0" v2="2" v3="3"/>'
            '</triangles>'
            '</mesh>'
            '</object>'
            '</resources>'
            '<build><item objectid="1"/></build>'
            '</model>'
        )
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/model_settings.config", "layer_height=0.2\nperimeters=3\n")
        # Fake JPEG thumbnail (just JPEG magic + padding)
        fake_thumb = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        zf.writestr("Metadata/thumbnail.jpg", fake_thumb)
    return buf.getvalue()


def _b64_3mf() -> str:
    return base64.b64encode(_make_minimal_3mf()).decode()


# ---------------------------------------------------------------------------
# Fake AI HTTP server
# ---------------------------------------------------------------------------

class FakeAIHandler(http.server.BaseHTTPRequestHandler):
    """
    Minimal OpenAI-compatible fake. Configured via class attributes.
    Set response_body to a dict (returned as JSON in choices[0].message.content).
    Set fail=True to return HTTP 500.
    Set bad_json=True to return invalid JSON content.
    """
    response_body: dict = {
        "summary": "Looks good",
        "changes": [{"key": "layer_height", "old": 0.2, "value": 0.15, "reason": "Better resolution"}],
        "warnings": [],
    }
    fail: bool = False
    bad_json: bool = False
    bad_structure: bool = False   # missing 'summary'
    unknown_keys: bool = False    # changes include unknown setting key

    def log_message(self, fmt, *args):
        pass  # silence

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        if self.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal server error")
            return

        if self.bad_json:
            content = "not json at all"
        elif self.bad_structure:
            content = json.dumps({"no_summary_key": "oops"})
        elif self.unknown_keys:
            content = json.dumps({
                "summary": "ok",
                "changes": [
                    {"key": "unknown_dangerous_key", "value": "rm -rf /", "reason": "bad"},
                    {"key": "layer_height", "old": 0.2, "value": 0.15, "reason": "ok"},
                ],
                "warnings": [],
            })
        else:
            content = json.dumps(self.response_body)

        resp = json.dumps({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "gpt-test",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _start_fake_ai(handler_class=None) -> tuple[http.server.HTTPServer, str]:
    """Start fake AI server, returns (server, base_url)."""
    cls = handler_class or FakeAIHandler
    server = http.server.HTTPServer(("127.0.0.1", 0), cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Fake webhook server
# ---------------------------------------------------------------------------

class FakeWebhookHandler(http.server.BaseHTTPRequestHandler):
    received: list = []
    fail: bool = False

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        FakeWebhookHandler.received.append(json.loads(body))
        code = 500 if self.fail else 200
        self.send_response(code)
        self.end_headers()


def _start_fake_webhook() -> tuple[http.server.HTTPServer, str]:
    FakeWebhookHandler.received = []
    FakeWebhookHandler.fail = False
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeWebhookHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}/hook"


# ---------------------------------------------------------------------------
# Base test case with server
# ---------------------------------------------------------------------------

class ServerTestCase(unittest.TestCase):
    """Base: starts PrintPilot server with no AI."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="printpilot_test_")
        self._cfg = _make_config(self._tmpdir)
        self._server, self._port = _start_server(self._cfg, self._tmpdir)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()


class ServerWithAITestCase(unittest.TestCase):
    """Base: starts PrintPilot server with fake AI."""

    _fake_ai_handler_class = None  # override in subclasses

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="printpilot_test_ai_")
        self._ai_server, ai_url = _start_fake_ai(self._fake_ai_handler_class)
        self._cfg = _make_config(
            self._tmpdir,
            ai_base_url=ai_url,
            ai_key="fake-key",
            ai_model="gpt-test",
            vision_model="gpt-test",
        )
        self._server, self._port = _start_server(self._cfg, self._tmpdir)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._ai_server.shutdown()
        self._ai_server.server_close()


# ---------------------------------------------------------------------------
# Test: health (public)
# ---------------------------------------------------------------------------

class TestHealth(ServerTestCase):
    def test_health_no_auth(self):
        resp = _req(self._port, "GET", "/health", token=None)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data["status"], "ok")
        self.assertIn("time", data)

    def test_health_with_token(self):
        resp = _req(self._port, "GET", "/health", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 200)


# ---------------------------------------------------------------------------
# Test: auth
# ---------------------------------------------------------------------------

class TestAuth(ServerTestCase):
    def test_state_no_token(self):
        resp = _req(self._port, "GET", "/api/state", token=None)
        self.assertEqual(resp.status, 401)

    def test_state_wrong_token(self):
        resp = _req(self._port, "GET", "/api/state", token="wrong-token")
        self.assertEqual(resp.status, 401)

    def test_state_device_token_rejected(self):
        resp = _req(self._port, "GET", "/api/state", token=DEVICE_TOKEN)
        self.assertEqual(resp.status, 401)

    def test_state_manager_token_ok(self):
        resp = _req(self._port, "GET", "/api/state", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 200)

    def test_analyze_no_token(self):
        resp = _req(self._port, "POST", "/api/analyze",
                    body=b'{}', token=None)
        self.assertEqual(resp.status, 401)

    def test_analyze_device_token_rejected(self):
        resp = _req(self._port, "POST", "/api/analyze",
                    body=b'{}', token=DEVICE_TOKEN)
        self.assertEqual(resp.status, 401)

    def test_snapshots_no_token(self):
        resp = _req(self._port, "POST", "/api/snapshots",
                    body=b'{}', token=None)
        self.assertEqual(resp.status, 401)

    def test_snapshots_device_token_ok(self):
        # Build minimal valid snapshot payload
        fake_jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 100).decode()
        body = json.dumps({
            "device_id": "dev1", "printer_id": "p1",
            "captured_at": _now_iso(),
            "image_base64": fake_jpeg,
            "status": "idle",
        }).encode()
        resp = _req(self._port, "POST", "/api/snapshots", body=body, token=DEVICE_TOKEN)
        self.assertIn(resp.status, (200, 201))

    def test_snapshots_manager_token_also_ok(self):
        fake_jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 100).decode()
        body = json.dumps({
            "device_id": "dev1", "printer_id": "p1",
            "captured_at": _now_iso(),
            "image_base64": fake_jpeg,
            "status": "idle",
        }).encode()
        resp = _req(self._port, "POST", "/api/snapshots", body=body, token=MANAGER_TOKEN)
        self.assertIn(resp.status, (200, 201))

    def test_heartbeat_no_token(self):
        body = json.dumps({"device_id": "d", "printer_id": "p", "status": "idle"}).encode()
        resp = _req(self._port, "POST", "/api/heartbeat", body=body, token=None)
        self.assertEqual(resp.status, 401)

    def test_heartbeat_device_token_ok(self):
        body = json.dumps({"device_id": "d", "printer_id": "p", "status": "idle"}).encode()
        resp = _req(self._port, "POST", "/api/heartbeat", body=body, token=DEVICE_TOKEN)
        self.assertEqual(resp.status, 200)

    def test_images_no_token(self):
        resp = _req(self._port, "GET", "/api/images/nonexistent", token=None)
        self.assertEqual(resp.status, 401)

    def test_images_device_token_rejected(self):
        resp = _req(self._port, "GET", "/api/images/nonexistent", token=DEVICE_TOKEN)
        self.assertEqual(resp.status, 401)

    def test_alert_ack_no_token(self):
        resp = _req(self._port, "POST", "/api/alerts/fake-id/ack",
                    body=b'{}', token=None)
        self.assertEqual(resp.status, 401)


# ---------------------------------------------------------------------------
# Test: /api/state structure
# ---------------------------------------------------------------------------

class TestState(ServerTestCase):
    def test_state_structure(self):
        status, data = _jreq(self._port, "GET", "/api/state", {})
        # GET ignores body but _jreq sends json body; re-do as plain GET
        resp = _req(self._port, "GET", "/api/state", token=MANAGER_TOKEN)
        data = json.loads(resp.read())
        self.assertEqual(resp.status, 200)
        for key in ("analyses", "snapshots", "alerts", "devices", "config"):
            self.assertIn(key, data)
        cfg = data["config"]
        for k in ("ai_configured", "vision_configured", "notifications_configured"):
            self.assertIn(k, cfg)
        self.assertFalse(cfg["ai_configured"])
        self.assertFalse(cfg["notifications_configured"])
        self.assertRegex(cfg["web_version"], r"^[0-9a-f]{12}$")

    def test_state_deserializes_analysis_and_device_freshness(self):
        conn = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        conn.execute("INSERT INTO analyses (id,created_at,filename,status,changes,warnings,model_info) VALUES (?,?,?,?,?,?,?)",
                     ("a", _now_iso(), "x.3mf", "complete", '[{"key":"layer_height"}]', '["warn"]', '{"id":"m"}'))
        conn.execute("INSERT INTO devices (device_id,last_seen,last_status) VALUES (?,?,?)", ("d", "2020-01-01T00:00:00Z", "idle"))
        conn.commit(); conn.close()
        response = _req(self._port, "GET", "/api/state")
        state = json.loads(response.read())
        self.assertIsInstance(state["analyses"][0]["changes"], list)
        self.assertEqual(state["analyses"][0]["model"]["id"], "m")
        self.assertTrue(state["devices"][0]["stale"])


class TestVisionValidation(unittest.TestCase):
    def test_missing_ok_and_string_false_are_errors(self):
        for body in ({"issues": [], "summary": "unclear"},
                     {"ok": "false", "issues": [], "summary": "bad"}):
            class VisionHandler(FakeAIHandler):
                response_body = body
            server, url = _start_fake_ai(VisionHandler)
            try:
                with self.assertRaises(AIError):
                    ai_vision({"ai_base_url": url, "ai_key": "test", "vision_model": "vision"}, "abc", {})
            finally:
                server.shutdown()
                server.server_close()


# ---------------------------------------------------------------------------
# Test: /api/analyze input validation
# ---------------------------------------------------------------------------

class TestAnalyzeMalformedInput(ServerTestCase):
    def _post(self, payload: dict) -> tuple[int, dict]:
        return _jreq(self._port, "POST", "/api/analyze", payload)

    def test_layer_height_rejects_small_nozzle(self):
        code, data = self._post({"filename": "x.3mf", "project_base64": _b64_3mf(),
                                 "settings": {"layer_height": "0.2", "nozzle_diameter": "0.2"}})
        self.assertEqual(code, 400)
        self.assertIn("nozzle", data["error"])

    def test_nonstring_objective_rejected(self):
        code, _ = self._post({"filename": "x.3mf", "project_base64": _b64_3mf(), "objective": []})
        self.assertEqual(code, 400)

    def test_missing_filename(self):
        code, data = self._post({
            "project_base64": _b64_3mf(), "settings": {}, "objective": "quality"
        })
        self.assertEqual(code, 400)
        self.assertIn("error", data)

    def test_missing_project_base64(self):
        code, data = self._post({
            "filename": "test.3mf", "settings": {}, "objective": "quality"
        })
        self.assertEqual(code, 400)

    def test_invalid_base64(self):
        code, data = self._post({
            "filename": "test.3mf",
            "project_base64": "not!valid!base64!!!",
            "settings": {}, "objective": "quality",
        })
        self.assertEqual(code, 400)
        self.assertIn("base64", data["error"].lower())

    def test_invalid_objective(self):
        code, data = self._post({
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {}, "objective": "invalid_obj",
        })
        self.assertEqual(code, 400)
        self.assertIn("objective", data["error"])

    def test_invalid_trigger(self):
        code, data = self._post({
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {}, "objective": "quality", "trigger": "bad_trigger",
        })
        self.assertEqual(code, 400)
        self.assertIn("trigger", data["error"])

    def test_settings_not_object(self):
        code, data = self._post({
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": ["not", "an", "object"],
            "objective": "quality",
        })
        self.assertEqual(code, 400)
        self.assertIn("settings", data["error"])

    def test_not_a_zip(self):
        bad_data = base64.b64encode(b"this is not a zip file at all").decode()
        code, data = self._post({
            "filename": "bad.3mf",
            "project_base64": bad_data,
            "settings": {}, "objective": "quality",
        })
        self.assertEqual(code, 422)
        self.assertIn("3MF", data["error"])

    def test_zip_bomb_rejected(self):
        """Zip with entries totalling >64 MiB uncompressed should be rejected."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Claim a huge file_size by using ZipInfo directly
            info = zipfile.ZipInfo("bigfile.bin")
            info.file_size = 128 * 1024 * 1024  # 128 MiB claimed
            info.compress_size = 10
            # We can't easily fake stored size without writing, so write real data
            # Instead: write 65 MiB to trigger the sum check
            zf.writestr("big1.bin", b"A" * (33 * 1024 * 1024))
            zf.writestr("big2.bin", b"B" * (33 * 1024 * 1024))
        raw = buf.getvalue()
        b64 = base64.b64encode(raw).decode()
        code, data = self._post({
            "filename": "bomb.3mf",
            "project_base64": b64,
            "settings": {}, "objective": "quality",
        })
        self.assertEqual(code, 422)
        self.assertIn("exceeds", data["error"])

    def test_path_traversal_in_filename(self):
        """Filename with path separators should be sanitized, not cause errors."""
        code, data = self._post({
            "filename": "../../etc/passwd",
            "project_base64": _b64_3mf(),
            "settings": {}, "objective": "quality",
        })
        # Will error on AI not configured, but NOT a path traversal
        # Check that no 400 or 403 related to traversal
        self.assertNotEqual(code, 403)

    def test_no_content_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=5)
        conn.request(
            "POST", "/api/analyze",
            headers={"Authorization": f"Bearer {MANAGER_TOKEN}",
                     "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)  # http.client sends Content-Length: 0

    def test_body_too_large(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=5)
        conn.putrequest("POST", "/api/analyze")
        conn.putheader("Authorization", f"Bearer {MANAGER_TOKEN}")
        conn.putheader("Content-Length", str(91 * 1024 * 1024))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)

    def test_invalid_json(self):
        resp = _req(self._port, "POST", "/api/analyze",
                    body=b"{not valid json}", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.read())
        self.assertIn("JSON", data["error"])


# ---------------------------------------------------------------------------
# Test: settings allowlist bounds
# ---------------------------------------------------------------------------

class TestSettingsValidation(unittest.TestCase):
    def test_unknown_keys_dropped(self):
        result = validate_settings({"unknown_key": 999, "layer_height": 0.2})
        self.assertNotIn("unknown_key", result)
        self.assertIn("layer_height", result)

    def test_layer_height_in_bounds(self):
        result = validate_settings({"layer_height": 0.2})
        self.assertEqual(result["layer_height"], 0.2)

    def test_layer_height_too_low(self):
        with self.assertRaises(ValueError) as ctx:
            validate_settings({"layer_height": 0.01})
        self.assertIn("layer_height", str(ctx.exception))
        self.assertIn("range", str(ctx.exception))

    def test_layer_height_too_high(self):
        with self.assertRaises(ValueError):
            validate_settings({"layer_height": 1.0})

    def test_bambu_serialized_numbers(self):
        result = validate_settings({"wall_loops": "3", "sparse_infill_density": "15%"})
        self.assertEqual(result, {"wall_loops": 3, "sparse_infill_density": 15.0})

    def test_wall_loops_out_of_range(self):
        with self.assertRaises(ValueError):
            validate_settings({"wall_loops": 100})

    def test_fractional_integer_and_boolean_rejected(self):
        for value in (2.5, "2.5", True):
            with self.assertRaises(ValueError):
                validate_settings({"wall_loops": value})

    def test_sparse_infill_density_bounds(self):
        with self.assertRaises(ValueError):
            validate_settings({"sparse_infill_density": "101%"})
        result = validate_settings({"sparse_infill_density": "15%"})
        self.assertEqual(result["sparse_infill_density"], 15)

    def test_outer_wall_speed_bounds(self):
        with self.assertRaises(ValueError):
            validate_settings({"outer_wall_speed": 1.0})
        with self.assertRaises(ValueError):
            validate_settings({"outer_wall_speed": 1000.0})
        result = validate_settings({"outer_wall_speed": 150.0})
        self.assertAlmostEqual(result["outer_wall_speed"], 150.0)

    def test_settings_non_object_raises(self):
        with self.assertRaises(ValueError):
            validate_settings([1, 2, 3])  # type: ignore


# ---------------------------------------------------------------------------
# Test: validate_changes
# ---------------------------------------------------------------------------

class TestValidateChanges(unittest.TestCase):
    def test_valid_change(self):
        changes = [{"key": "layer_height", "old": 0.2, "value": 0.15, "reason": "Better"}]
        result = validate_changes(changes, {"layer_height": 0.2})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "layer_height")
        self.assertAlmostEqual(result[0]["value"], 0.15)

    def test_unknown_key_dropped(self):
        changes = [
            {"key": "malicious_key", "old": "x", "value": "injected", "reason": "bad"},
            {"key": "layer_height", "old": 0.2, "value": 0.15, "reason": "ok"},
        ]
        result = validate_changes(changes, {"layer_height": 0.2})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "layer_height")

    def test_value_out_of_bounds(self):
        changes = [{"key": "layer_height", "old": 0.2, "value": 5.0, "reason": "bad"}]
        with self.assertRaises(ValueError):
            validate_changes(changes, {"layer_height": 0.2})

    def test_old_value_out_of_bounds(self):
        changes = [{"key": "layer_height", "old": 99.0, "value": 0.2, "reason": "ok"}]
        with self.assertRaises(ValueError):
            validate_changes(changes, {"layer_height": 0.2})

    def test_missing_required_field(self):
        changes = [{"key": "layer_height", "value": 0.2}]  # missing 'reason'
        with self.assertRaises(ValueError):
            validate_changes(changes, {"layer_height": 0.2})

    def test_not_a_list(self):
        with self.assertRaises(ValueError):
            validate_changes({"key": "layer_height"}, {})  # type: ignore

    def test_reason_not_string(self):
        changes = [{"key": "layer_height", "old": 0.2, "value": 0.15, "reason": 123}]
        with self.assertRaises(ValueError):
            validate_changes(changes, {"layer_height": 0.2})

    def test_old_must_match_submitted(self):
        changes = [{"key": "layer_height", "old": 0.3, "value": 0.15, "reason": "test"}]
        with self.assertRaises(ValueError):
            validate_changes(changes, {"layer_height": 0.2})

    def test_duplicate_key_rejected(self):
        change = {"key": "layer_height", "old": 0.2, "value": 0.15, "reason": "test"}
        with self.assertRaises(ValueError):
            validate_changes([change, change], {"layer_height": 0.2})


# ---------------------------------------------------------------------------
# Test: 3MF extraction
# ---------------------------------------------------------------------------

class TestExtract3MF(unittest.TestCase):
    def test_split_models_bbox_and_thumbnails(self):
        buf = io.BytesIO()
        xml = '<model unit="millimeter"><resources><object id="1"><mesh><vertices>' \
              '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>' \
              '<vertex x="0" y="10" z="0"/></vertices><triangles>' \
              '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>' \
              '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 20 0 0"/></build></model>'
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", xml)
            zf.writestr("3D/Objects/part.model", xml)
            zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n" + b"x" * 20)
            zf.writestr("Metadata/model_settings.config", '<config><object id="1"><metadata key="name" value="part"/></object></config>')
        result = extract_3mf(buf.getvalue())
        self.assertEqual(result["geometry"]["triangle_count"], 2)
        self.assertEqual(result["geometry"]["bbox"]["min"], [20.0, 0.0, 0.0])
        self.assertIsNotNone(result["thumbnail_b64"])
        self.assertEqual(result["config"]["objects"]["1"]["name"], "part")

    def test_rejects_dtd_and_malformed_mesh(self):
        for xml in ('<!DOCTYPE model [<!ENTITY x "bad">]><model/>',
                    '<model><resources><object id="1"><mesh><vertices><vertex x="bad"/></vertices></mesh></object></resources></model>'):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("3D/3dmodel.model", xml)
            with self.assertRaises(ExtractionError):
                extract_3mf(buf.getvalue())

    def test_valid_minimal(self):
        result = extract_3mf(_make_minimal_3mf())
        self.assertIn("geometry", result)
        self.assertIn("config", result)
        self.assertGreater(result["geometry"]["vertex_count"], 0)
        self.assertGreater(result["geometry"]["triangle_count"], 0)
        self.assertIsNotNone(result["thumbnail_b64"])

    def test_not_a_zip(self):
        with self.assertRaises(ExtractionError) as ctx:
            extract_3mf(b"this is not a zip")
        self.assertIn("ZIP", str(ctx.exception))

    def test_too_large(self):
        # Create a large-claimed zip (actual bytes within 90MiB, but decoded > 64MiB)
        # We simulate by passing large bytes directly
        with self.assertRaises(ExtractionError) as ctx:
            extract_3mf(b"X" * (65 * 1024 * 1024))
        self.assertIn("exceeds", str(ctx.exception))

    def test_zip_expansion_limit(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("big1.bin", b"A" * (33 * 1024 * 1024))
            zf.writestr("big2.bin", b"B" * (33 * 1024 * 1024))
        with self.assertRaises(ExtractionError) as ctx:
            extract_3mf(buf.getvalue())
        self.assertIn("exceeds", str(ctx.exception))

    def test_no_model_entry(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "no model here")
        result = extract_3mf(buf.getvalue())
        self.assertEqual(result["geometry"], {})

    def test_config_parsed(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Metadata/model_settings.config",
                        "layer_height=0.2\nperimeters=3\n")
        result = extract_3mf(buf.getvalue())
        self.assertEqual(result["config"].get("layer_height"), "0.2")

    def test_geometry_counts(self):
        result = extract_3mf(_make_minimal_3mf())
        self.assertEqual(result["geometry"]["vertex_count"], 4)
        self.assertEqual(result["geometry"]["triangle_count"], 4)
        self.assertEqual(result["geometry"]["object_count"], 1)
        self.assertEqual(result["geometry"]["build_items"], 1)
        self.assertEqual(result["geometry"]["unit"], "millimeter")

    def test_no_thumbnail(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        result = extract_3mf(buf.getvalue())
        self.assertIsNone(result["thumbnail_b64"])


# ---------------------------------------------------------------------------
# Test: /api/snapshots
# ---------------------------------------------------------------------------

class TestSnapshots(ServerTestCase):
    def _fake_jpeg_b64(self, size: int = 200) -> str:
        return base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * size).decode()

    def _valid_payload(self) -> dict:
        return {
            "device_id": "dev1",
            "printer_id": "printer1",
            "captured_at": _now_iso(),
            "image_base64": self._fake_jpeg_b64(),
            "status": "printing",
            "telemetry": {"temp": 210},
        }

    def test_valid_snapshot(self):
        code, data = _jreq(self._port, "POST", "/api/snapshots",
                           self._valid_payload(), token=DEVICE_TOKEN)
        self.assertIn(code, (200, 201))
        self.assertIn("id", data)
        self.assertIn("status", data)

    def test_missing_device_id(self):
        p = self._valid_payload()
        del p["device_id"]
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("device_id", data["error"])

    def test_missing_printer_id(self):
        p = self._valid_payload()
        del p["printer_id"]
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)

    def test_missing_captured_at(self):
        p = self._valid_payload()
        del p["captured_at"]
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)

    def test_invalid_captured_at(self):
        p = self._valid_payload()
        p["captured_at"] = "not-a-date"
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("captured_at", data["error"])

    def test_invalid_status(self):
        p = self._valid_payload()
        p["status"] = "sleeping"
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("status", data["error"])

    def test_old_or_future_capture_rejected(self):
        for captured_at in ("2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"):
            p = self._valid_payload()
            p["captured_at"] = captured_at
            code, _ = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
            self.assertEqual(code, 400)

    def test_list_status_rejected(self):
        p = self._valid_payload()
        p["status"] = []
        code, _ = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)

    def test_invalid_base64_image(self):
        p = self._valid_payload()
        p["image_base64"] = "not!valid!base64"
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("base64", data["error"].lower())

    def test_non_jpeg_image(self):
        # PNG magic bytes
        p = self._valid_payload()
        p["image_base64"] = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50).decode()
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 422)
        self.assertIn("JPEG", data["error"])

    def test_image_too_large(self):
        big = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * (9 * 1024 * 1024)).decode()
        p = self._valid_payload()
        p["image_base64"] = big
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 413)

    def test_telemetry_non_object(self):
        p = self._valid_payload()
        p["telemetry"] = ["not", "an", "object"]
        code, data = _jreq(self._port, "POST", "/api/snapshots", p, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("telemetry", data["error"])

    def test_snapshot_stored_in_state(self):
        _jreq(self._port, "POST", "/api/snapshots",
              self._valid_payload(), token=DEVICE_TOKEN)
        resp = _req(self._port, "GET", "/api/state", token=MANAGER_TOKEN)
        state = json.loads(resp.read())
        self.assertGreater(len(state["snapshots"]), 0)
        self.assertGreater(len(state["devices"]), 0)

    def test_vision_skipped_when_no_ai(self):
        """Without AI config, vision_status should be 'skipped'."""
        code, data = _jreq(self._port, "POST", "/api/snapshots",
                           self._valid_payload(), token=DEVICE_TOKEN)
        snap_id = data["id"]
        # Give time for DB write
        time.sleep(0.1)
        db_path = os.path.join(self._tmpdir, "printpilot.db")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT vision_status FROM snapshots WHERE id=?", (snap_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "skipped")


# ---------------------------------------------------------------------------
# Test: /api/heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat(ServerTestCase):
    def test_valid_heartbeat(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "printer_id": "p1", "status": "idle"
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "ok")

    def test_missing_device_id(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "printer_id": "p1", "status": "idle"
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("device_id", data["error"])

    def test_missing_printer_id(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "status": "idle"
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)

    def test_invalid_status(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "printer_id": "p1", "status": "broken"
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertIn("status", data["error"])

    def test_secret_field_rejected(self):
        """Heartbeat must not log API keys or tokens."""
        for secret_field in ("token", "api_key", "secret", "password", "key"):
            code, data = _jreq(self._port, "POST", "/api/heartbeat", {
                "device_id": "d1", "printer_id": "p1", "status": "idle",
                secret_field: "SUPER_SECRET",
            }, token=DEVICE_TOKEN)
            self.assertEqual(code, 400, f"Expected rejection for field {secret_field!r}")
            self.assertIn(secret_field, data["error"])

    def test_error_field_accepted(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "printer_id": "p1",
            "status": "unknown", "error": "Extruder jam",
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 200)

    def test_error_field_non_string(self):
        code, data = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "printer_id": "p1",
            "status": "unknown", "error": {"nested": "object"},
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)

    def test_device_appears_in_state(self):
        _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "mydevice", "printer_id": "myprinter", "status": "printing"
        }, token=DEVICE_TOKEN)
        resp = _req(self._port, "GET", "/api/state", token=MANAGER_TOKEN)
        state = json.loads(resp.read())
        device_ids = [d["device_id"] for d in state["devices"]]
        self.assertIn("mydevice", device_ids)


class TestPrinterRename(ServerTestCase):
    def _heartbeat(self, name="Bridge X1C"):
        code, _ = _jreq(self._port, "POST", "/api/heartbeat", {
            "device_id": "d1", "printer_id": "p1", "status": "idle", "printer_name": name,
        }, token=DEVICE_TOKEN)
        self.assertEqual(code, 200)

    def _printer(self):
        state = json.loads(_req(self._port, "GET", "/api/state", token=MANAGER_TOKEN).read())
        return next(p for p in state["printers"] if p["printer_id"] == "p1")

    def test_rename_survives_heartbeat_and_reset_restores_reported(self):
        self._heartbeat()
        code, data = _jreq(self._port, "POST", "/api/printers/p1/name", {"name": "  Máy   phòng khách "})
        self.assertEqual(code, 200)
        self.assertEqual(data["name"], "Máy phòng khách")
        self._heartbeat("Bridge X1C")  # bridge keeps reporting its own name
        printer = self._printer()
        self.assertEqual(printer["name"], "Máy phòng khách")
        self.assertEqual(printer["custom_name"], "Máy phòng khách")
        self.assertEqual(printer["reported_name"], "Bridge X1C")
        code, data = _jreq(self._port, "POST", "/api/printers/p1/name", {"name": ""})
        self.assertEqual(code, 200)
        self.assertEqual(data["name"], "Bridge X1C")
        printer = self._printer()
        self.assertEqual(printer["name"], "Bridge X1C")
        self.assertIsNone(printer["custom_name"])

    def test_rename_logs_event(self):
        self._heartbeat()
        _jreq(self._port, "POST", "/api/printers/p1/name", {"name": "Bếp"})
        events = json.loads(_req(self._port, "GET", "/api/events", token=MANAGER_TOKEN).read())
        self.assertTrue(any("Bếp" in e["message"] for e in events["events"]))

    def test_rename_validation(self):
        self._heartbeat()
        for body, code in (({"name": 5}, 400), ({}, 400), ({"name": "x" * 81}, 400),
                           ({"name": "a\u0000b"}, 400)):
            got, _ = _jreq(self._port, "POST", "/api/printers/p1/name", body)
            self.assertEqual(got, code, body)
        got, _ = _jreq(self._port, "POST", "/api/printers/missing/name", {"name": "A"})
        self.assertEqual(got, 404)

    def test_rename_requires_manager(self):
        self._heartbeat()
        got, _ = _jreq(self._port, "POST", "/api/printers/p1/name", {"name": "A"}, token=DEVICE_TOKEN)
        self.assertEqual(got, 401)
        got, _ = _jreq(self._port, "POST", "/api/printers/p1/name", {"name": "A"}, token=None)
        self.assertEqual(got, 401)


# ---------------------------------------------------------------------------
# Test: /api/images
# ---------------------------------------------------------------------------

class TestImages(ServerTestCase):
    def _create_snapshot(self) -> str:
        fake_jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 200).decode()
        code, data = _jreq(self._port, "POST", "/api/snapshots", {
            "device_id": "d1", "printer_id": "p1",
            "captured_at": _now_iso(),
            "image_base64": fake_jpeg, "status": "idle",
        }, token=DEVICE_TOKEN)
        self.assertIn(code, (200, 201))
        return data["id"]

    def test_image_not_found(self):
        resp = _req(self._port, "GET", "/api/images/nonexistent-id", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 404)

    def test_image_served(self):
        snap_id = self._create_snapshot()
        resp = _req(self._port, "GET", f"/api/images/{snap_id}", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/jpeg")
        body = resp.read()
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_path_traversal_rejected(self):
        # Attempt path traversal via crafted ID
        resp = _req(self._port, "GET", "/api/images/../etc/passwd", token=MANAGER_TOKEN)
        # Should be 400 (bad id chars) or 404 – not 200 and not serve arbitrary file
        self.assertIn(resp.status, (400, 404))
        resp.read()


# ---------------------------------------------------------------------------
# Test: /api/alerts/{id}/ack
# ---------------------------------------------------------------------------

class TestAlertAck(ServerTestCase):
    def _create_alert(self) -> str:
        db_path = os.path.join(self._tmpdir, "printpilot.db")
        conn = open_db(db_path)
        aid = upsert_alert(conn, "test", "warning", "test alert", "d1", "p1")
        conn.close()
        return aid

    def test_ack_not_found(self):
        code, data = _jreq(self._port, "POST", "/api/alerts/nonexistent/ack", {})
        self.assertEqual(code, 404)

    def test_ack_success(self):
        alert_id = self._create_alert()
        code, data = _jreq(self._port, "POST", f"/api/alerts/{alert_id}/ack", {})
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "acknowledged")

    def test_ack_persisted(self):
        alert_id = self._create_alert()
        _jreq(self._port, "POST", f"/api/alerts/{alert_id}/ack", {})
        db_path = os.path.join(self._tmpdir, "printpilot.db")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT acknowledged FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)


# ---------------------------------------------------------------------------
# Test: alert dedup
# ---------------------------------------------------------------------------

class TestAlertDedup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._conn = open_db(os.path.join(self._tmpdir, "test.db"))

    def tearDown(self):
        self._conn.close()

    def test_dedup_same_alert(self):
        id1 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d1", "p1")
        id2 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d1", "p1")
        self.assertIsNotNone(id1)
        self.assertIsNone(id2)  # deduped

    def test_dedup_different_message(self):
        id1 = upsert_alert(self._conn, "vision", "warning", "Issue A", "d1", "p1")
        id2 = upsert_alert(self._conn, "vision", "warning", "Issue B", "d1", "p1")
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2)

    def test_dedup_after_ack_allows_new(self):
        id1 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d1", "p1")
        self._conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (id1,))
        self._conn.commit()
        id2 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d1", "p1")
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2)

    def test_dedup_key_different_device(self):
        id1 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d1", "p1")
        id2 = upsert_alert(self._conn, "vision", "warning", "Print failure", "d2", "p1")
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2)

    def test_vision_alert_links_snapshot(self):
        alert_id = upsert_alert(self._conn, "vision", "warning", "Spaghetti", "d1", "p1", "snapshot-123")
        self.assertEqual(self._conn.execute("SELECT snapshot_id FROM alerts WHERE id=?", (alert_id,)).fetchone()[0], "snapshot-123")

    def test_retention_removes_old_file_and_row(self):
        path = os.path.join(self._tmpdir, "old.3mf")
        with open(path, "wb") as out:
            out.write(b"old")
        self._conn.execute("INSERT INTO analyses (id,created_at,filename,status,file_path) VALUES (?,?,?,?,?)",
                           ("old", "2020-01-01T00:00:00Z", "old.3mf", "complete", path))
        self._conn.commit()
        prune_old_data(self._conn)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(self._conn.execute("SELECT id FROM analyses WHERE id='old'").fetchone())


# ---------------------------------------------------------------------------
# Test: analyze with fake AI (success path)
# ---------------------------------------------------------------------------

class TestAnalyzeWithFakeAI(ServerWithAITestCase):
    def test_successful_analysis(self):
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {"layer_height": 0.2},
            "objective": "quality",
            "trigger": "manual",
        })
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "complete")
        self.assertIn("summary", data)
        self.assertIn("changes", data)
        self.assertIsInstance(data["changes"], list)
        self.assertIn("warnings", data)
        self.assertIn("model", data)

    def test_changes_bounded(self):
        """Changes returned must all have bounded values."""
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {"layer_height": 0.2},
            "objective": "quality",
        })
        self.assertEqual(code, 200)
        for ch in data.get("changes", []):
            key = ch["key"]
            self.assertIn(key, SETTINGS_ALLOWLIST)
            bounds = SETTINGS_ALLOWLIST[key]
            if bounds is not None:
                lo, hi, _ = bounds
                self.assertGreaterEqual(ch["value"], lo)
                self.assertLessEqual(ch["value"], hi)

    def test_analysis_in_state(self):
        _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {"layer_height": 0.2}, "objective": "quality",
        })
        resp = _req(self._port, "GET", "/api/state", token=MANAGER_TOKEN)
        state = json.loads(resp.read())
        self.assertGreater(len(state["analyses"]), 0)


# ---------------------------------------------------------------------------
# Test: AI provider failure
# ---------------------------------------------------------------------------

class FailingAIHandler(FakeAIHandler):
    fail = True


class TestAnalyzeAIFailure(ServerWithAITestCase):
    _fake_ai_handler_class = FailingAIHandler

    def test_ai_failure_returns_error(self):
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {"layer_height": 0.2},
            "objective": "quality",
        })
        # Must return explicit error, not simulated results
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "error")
        self.assertIn("error", data["summary"].lower() or data.get("error", "").lower()
                      or "")
        self.assertEqual(data["changes"], [])


# ---------------------------------------------------------------------------
# Test: AI returns bad JSON content
# ---------------------------------------------------------------------------

class BadJsonAIHandler(FakeAIHandler):
    bad_json = True


class TestAnalyzeAIBadJSON(ServerWithAITestCase):
    _fake_ai_handler_class = BadJsonAIHandler

    def test_ai_bad_json_returns_error(self):
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {}, "objective": "quality",
        })
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["changes"], [])


# ---------------------------------------------------------------------------
# Test: AI returns bad structure (no 'summary')
# ---------------------------------------------------------------------------

class BadStructureAIHandler(FakeAIHandler):
    bad_structure = True


class TestAnalyzeAIBadStructure(ServerWithAITestCase):
    _fake_ai_handler_class = BadStructureAIHandler

    def test_ai_bad_structure_returns_error(self):
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {}, "objective": "quality",
        })
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "error")


# ---------------------------------------------------------------------------
# Test: AI suggests unknown keys (must be dropped/handled)
# ---------------------------------------------------------------------------

class UnknownKeysAIHandler(FakeAIHandler):
    unknown_keys = True


class TestAnalyzeAIUnknownKeys(ServerWithAITestCase):
    _fake_ai_handler_class = UnknownKeysAIHandler

    def test_unknown_keys_dropped_from_changes(self):
        code, data = _jreq(self._port, "POST", "/api/analyze", {
            "filename": "test.3mf",
            "project_base64": _b64_3mf(),
            "settings": {"layer_height": 0.2}, "objective": "quality",
        })
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "complete")
        # Only known keys should remain
        for ch in data["changes"]:
            self.assertIn(ch["key"], SETTINGS_ALLOWLIST,
                          f"Unknown key {ch['key']!r} leaked into response")
        # layer_height change should still be there
        keys = [ch["key"] for ch in data["changes"]]
        self.assertIn("layer_height", keys)
        self.assertNotIn("unknown_dangerous_key", keys)


# ---------------------------------------------------------------------------
# Test: webhook dedup/retry
# ---------------------------------------------------------------------------

class TestWebhookDedup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._wh_server, self._wh_url = _start_fake_webhook()
        self._conn = open_db(os.path.join(self._tmpdir, "test.db"))
        self._cfg = {
            "webhook_url": self._wh_url,
        }

    def tearDown(self):
        self._conn.close()
        self._wh_server.shutdown()

    def test_webhook_sent_on_new_alert(self):
        from server import send_webhook
        aid = upsert_alert(self._conn, "vision", "warning", "Alert1", "d1", "p1")
        row = self._conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        send_webhook(self._cfg, row, self._conn)
        time.sleep(0.2)
        self.assertEqual(len(FakeWebhookHandler.received), 1)
        self.assertEqual(FakeWebhookHandler.received[0]["id"], aid)

    def test_webhook_retry_scheduled_on_failure(self):
        from server import send_webhook
        FakeWebhookHandler.fail = True
        aid = upsert_alert(self._conn, "vision", "warning", "Alert2", "d1", "p1")
        row = self._conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        send_webhook(self._cfg, row, self._conn)
        time.sleep(0.2)
        updated = self._conn.execute(
            "SELECT webhook_sent, webhook_retries, webhook_next_at FROM alerts WHERE id=?",
            (aid,)
        ).fetchone()
        self.assertEqual(updated[0], 0)        # not sent
        self.assertEqual(updated[1], 1)        # 1 retry attempt
        self.assertIsNotNone(updated[2])       # next retry scheduled


# ---------------------------------------------------------------------------
# Test: heartbeat monitor stale logic
# ---------------------------------------------------------------------------

class TestHeartbeatMonitorStale(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self._conn = open_db(self._db_path)

    def tearDown(self):
        self._conn.close()

    def test_stale_device_generates_alert(self):
        # Insert device with last_seen in the past
        old_time = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - HEARTBEAT_STALE_SECS - 10)
        )
        self._conn.execute(
            """INSERT INTO devices (device_id, printer_id, last_seen, last_status)
               VALUES ('stale_dev', 'p1', ?, 'printing')""",
            (old_time,),
        )
        self._conn.commit()

        monitor = HeartbeatMonitor({"webhook_url": ""}, self._db_path)
        monitor._check(self._conn)

        alerts = self._conn.execute(
            "SELECT * FROM alerts WHERE device_id='stale_dev'"
        ).fetchall()
        self.assertGreater(len(alerts), 0)
        self.assertEqual(alerts[0]["source"], "heartbeat")
        self.assertEqual(alerts[0]["severity"], "warning")

    def test_fresh_device_no_alert(self):
        now = _now_iso()
        self._conn.execute(
            """INSERT INTO devices (device_id, printer_id, last_seen, last_status)
               VALUES ('fresh_dev', 'p1', ?, 'idle')""",
            (now,),
        )
        self._conn.commit()

        monitor = HeartbeatMonitor({"webhook_url": ""}, self._db_path)
        monitor._check(self._conn)

        alerts = self._conn.execute(
            "SELECT * FROM alerts WHERE device_id='fresh_dev'"
        ).fetchall()
        self.assertEqual(len(alerts), 0)


# ---------------------------------------------------------------------------
# Test: config loading
# ---------------------------------------------------------------------------

class TestConfigLoading(unittest.TestCase):
    def _write_config(self, d: dict) -> str:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        json.dump(d, f)
        f.close()
        return f.name

    def test_valid_config(self):
        path = self._write_config({
            "host": "0.0.0.0", "port": 8080,
            "data_dir": "/tmp/pp",
            "manager_token": "mgr", "device_token": "dev",
        })
        cfg = load_config(path)
        self.assertEqual(cfg["port"], 8080)
        os.unlink(path)

    def test_missing_key(self):
        path = self._write_config({
            "host": "0.0.0.0", "port": 8080,
            "data_dir": "/tmp/pp",
            "manager_token": "mgr",
            # missing device_token
        })
        with self.assertRaises(ValueError):
            load_config(path)
        os.unlink(path)

    def test_port_not_int(self):
        path = self._write_config({
            "host": "0.0.0.0", "port": "eight thousand",
            "data_dir": "/tmp/pp",
            "manager_token": "mgr", "device_token": "dev",
        })
        with self.assertRaises(ValueError):
            load_config(path)
        os.unlink(path)

    def test_tokens_must_differ(self):
        path = self._write_config({
            "host": "0.0.0.0", "port": 8080,
            "data_dir": "/tmp/pp",
            "manager_token": "same", "device_token": "same",
        })
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("differ", str(ctx.exception))
        os.unlink(path)

    def test_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_config("/nonexistent/path/config.json")

    def test_empty_token_rejected(self):
        path = self._write_config({
            "host": "0.0.0.0", "port": 8080,
            "data_dir": "/tmp/pp",
            "manager_token": "", "device_token": "dev",
        })
        with self.assertRaises(ValueError):
            load_config(path)
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test: static file serving
# ---------------------------------------------------------------------------

class TestStaticFiles(ServerTestCase):
    def test_missing_file_404(self):
        resp = _req(self._port, "GET", "/nonexistent.html", token=None)
        self.assertEqual(resp.status, 404)
        resp.read()

    def test_path_traversal_static(self):
        resp = _req(self._port, "GET", "/../../../etc/passwd", token=None)
        self.assertIn(resp.status, (400, 403, 404))
        resp.read()


# ---------------------------------------------------------------------------
# Test: not found routes
# ---------------------------------------------------------------------------

class TestNotFound(ServerTestCase):
    def test_unknown_post_route(self):
        resp = _req(self._port, "POST", "/api/unknown", body=b"{}", token=MANAGER_TOKEN)
        self.assertEqual(resp.status, 404)
        resp.read()


# ---------------------------------------------------------------------------
# Test: parse helpers
# ---------------------------------------------------------------------------

class TestParseHelpers(unittest.TestCase):
    def test_parse_ini_config(self):
        ini = "layer_height=0.2\nperimeters=3\n# comment\n\n;another\nkey=val"
        result = _parse_ini_config(ini)
        self.assertEqual(result["layer_height"], "0.2")
        self.assertEqual(result["perimeters"], "3")
        self.assertEqual(result["key"], "val")

    def test_parse_model_geometry(self):
        xml = """
        <model unit="millimeter">
        <object id="1"><mesh>
        <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="2" y="0" z="0"/></vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
        </mesh></object>
        <build><item objectid="1"/></build>
        </model>
        """
        g = _parse_model_geometry(xml)
        self.assertEqual(g["vertex_count"], 3)
        self.assertEqual(g["triangle_count"], 1)
        self.assertEqual(g["object_count"], 1)
        self.assertEqual(g["build_items"], 1)
        self.assertEqual(g["unit"], "millimeter")

    def test_dedup_key_deterministic(self):
        k1 = _dedup_key("vision", "d1", "msg")
        k2 = _dedup_key("vision", "d1", "msg")
        self.assertEqual(k1, k2)

    def test_dedup_key_different_inputs(self):
        k1 = _dedup_key("vision", "d1", "msg")
        k2 = _dedup_key("vision", "d2", "msg")
        self.assertNotEqual(k1, k2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

class TestTelegram(unittest.TestCase):
    def _alert(self, conn):
        from server import upsert_alert
        aid = upsert_alert(conn, "vision", "critical", "Spaghetti detected",
                           device_id="redmi-9c", printer_id="a1-01")
        return conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()

    def _db(self):
        from server import open_db
        d = tempfile.mkdtemp()
        return open_db(os.path.join(d, "t.db"))

    def test_configured_flags(self):
        from server import notifications_configured, telegram_configured
        self.assertFalse(notifications_configured({}))
        self.assertFalse(telegram_configured({"telegram_bot_token": "x"}))
        self.assertTrue(notifications_configured(
            {"telegram_bot_token": "x", "telegram_chat_id": "1"}))

    def test_send_telegram_success_marks_sent(self):
        from unittest import mock
        import server
        conn = self._db()
        alert = self._alert(conn)
        sent = {}

        class FakeConn:
            def __init__(self, host, port, context=None, timeout=None):
                sent["host"] = host
            def request(self, method, path, body=None, headers=None):
                sent["path"] = path
                sent["body"] = json.loads(body)
            def getresponse(self):
                r = mock.Mock(); r.status = 200; r.read.return_value = b"{}"; return r
            def close(self):
                pass

        cfg = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "42"}
        with mock.patch.object(server.http.client, "HTTPSConnection", FakeConn):
            server.send_webhook(cfg, alert, conn)
        self.assertEqual(sent["host"], "api.telegram.org")
        self.assertEqual(sent["path"], "/bot123:ABC/sendMessage")
        self.assertEqual(sent["body"]["chat_id"], "42")
        self.assertIn("Spaghetti detected", sent["body"]["text"])
        row = conn.execute("SELECT webhook_sent FROM alerts WHERE id=?", (alert["id"],)).fetchone()
        self.assertEqual(row["webhook_sent"], 1)

    def test_send_telegram_failure_schedules_retry_without_leaking_token(self):
        from unittest import mock
        import server
        conn = self._db()
        alert = self._alert(conn)

        class FailConn:
            def __init__(self, *a, **k): pass
            def request(self, *a, **k): raise OSError("boom")
            def close(self): pass

        cfg = {"telegram_bot_token": "SECRET-TOKEN", "telegram_chat_id": "42"}
        with mock.patch.object(server.http.client, "HTTPSConnection", FailConn), \
                self.assertLogs("printpilot", level="WARNING") as logs:
            server.send_webhook(cfg, alert, conn)
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))
        row = conn.execute("SELECT webhook_sent, webhook_retries FROM alerts WHERE id=?",
                           (alert["id"],)).fetchone()
        self.assertEqual((row["webhook_sent"], row["webhook_retries"]), (0, 1))


# ---------------------------------------------------------------------------
# Tailscale identity login
# ---------------------------------------------------------------------------

class TestTailscaleAuth(unittest.TestCase):
    """Peer 127.0.0.1 is the trusted proxy, so X-Forwarded-For names the client."""

    IDENTS = {
        "100.104.17.115": {"login": "owner@example.com", "node": "macbook"},
        "100.99.0.9": {"login": "stranger@example.com", "node": "other"},
    }

    def setUp(self):
        from unittest import mock
        import server
        self._tmpdir = tempfile.mkdtemp(prefix="printpilot_ts_")
        self._cfg = _make_config(self._tmpdir)
        self._cfg["tailscale_allowed_users"] = ["owner@example.com"]
        self._patch = mock.patch.object(server, "tailscale_whois",
                                        side_effect=lambda ip, sock=None: self.IDENTS.get(ip))
        self._patch.start()
        self._server, self._port = _start_server(self._cfg, self._tmpdir)

    def tearDown(self):
        self._patch.stop()
        self._server.shutdown()
        self._server.server_close()

    def _call(self, method, path, xff=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        headers = dict(extra or {})
        if xff:
            headers["X-Forwarded-For"] = xff
        if method == "POST":
            headers["Content-Length"] = "0"
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_allowed_user_reads_state_without_token(self):
        status, _ = self._call("GET", "/api/state", xff="100.104.17.115")
        self.assertEqual(status, 200)

    def test_whoami_reports_user(self):
        status, body = self._call("GET", "/api/whoami", xff="100.104.17.115")
        self.assertEqual((status, body["method"], body["user"]), (200, "tailscale", "owner@example.com"))

    def test_not_allowed_user_rejected(self):
        status, _ = self._call("GET", "/api/state", xff="100.99.0.9")
        self.assertEqual(status, 401)

    def test_unknown_ip_rejected(self):
        status, _ = self._call("GET", "/api/state", xff="203.0.113.5")
        self.assertEqual(status, 401)

    def test_spoofed_xff_uses_last_hop(self):
        status, _ = self._call("GET", "/api/state", xff="100.104.17.115, 100.99.0.9")
        self.assertEqual(status, 401)

    def test_write_requires_same_origin_marker(self):
        status, _ = self._call("POST", "/api/alerts/x/ack", xff="100.104.17.115")
        self.assertEqual(status, 401)
        status, _ = self._call("POST", "/api/alerts/x/ack", xff="100.104.17.115",
                               extra={"X-PrintPilot-Request": "1", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 401)
        status, _ = self._call("POST", "/api/alerts/x/ack", xff="100.104.17.115",
                               extra={"X-PrintPilot-Request": "1", "Sec-Fetch-Site": "same-origin"})
        self.assertNotEqual(status, 401)

    def test_device_endpoints_still_need_device_token(self):
        status, _ = self._call("POST", "/api/heartbeat", xff="100.104.17.115",
                               extra={"X-PrintPilot-Request": "1"})
        self.assertEqual(status, 401)

    def test_disabled_without_allowlist(self):
        self._cfg["tailscale_allowed_users"] = []
        status, _ = self._call("GET", "/api/state", xff="100.104.17.115")
        self.assertEqual(status, 401)


class TestHeartbeatAutoResolve(ServerTestCase):
    def _hb(self, **extra):
        data = {"device_id": "redmi-9c", "printer_id": "a1-01", "status": "unknown"}
        data.update(extra)
        return _jreq(self._port, "POST", "/api/heartbeat", data, token=DEVICE_TOKEN)

    def _open_alerts(self):
        resp = _req(self._port, "GET", "/api/state")
        state = json.loads(resp.read())
        return [a for a in state["alerts"] if not a["acknowledged"]]

    def test_healthy_heartbeat_resolves_previous_error(self):
        self.assertEqual(self._hb(error="printer not configured")[0], 200)
        self.assertEqual(len(self._open_alerts()), 1)
        self.assertEqual(self._hb()[0], 200)
        self.assertEqual(self._open_alerts(), [])


class TestPrinters(ServerTestCase):
    def _hb(self, printer_id, **extra):
        data = {"device_id": "redmi-9c", "printer_id": printer_id, "status": "printing"}
        data.update(extra)
        return _jreq(self._port, "POST", "/api/heartbeat", data, token=DEVICE_TOKEN)[0]

    def _state(self):
        return json.loads(_req(self._port, "GET", "/api/state").read())

    def test_multiple_printers_with_telemetry(self):
        self.assertEqual(self._hb("a1-01", printer_name="A1 bàn trái", printer_model="A1",
                                  telemetry={"nozzle_temp": 245.0, "progress": 97,
                                             "job_name": "vase", "evil": {"x": 1}}), 200)
        self.assertEqual(self._hb("p1s-01", status="idle", telemetry={"bed_temp": 25}), 200)
        printers = {p["printer_id"]: p for p in self._state()["printers"]}
        self.assertEqual(set(printers), {"a1-01", "p1s-01"})
        a1 = printers["a1-01"]
        self.assertEqual((a1["name"], a1["model"], a1["status"]), ("A1 bàn trái", "A1", "printing"))
        self.assertEqual(a1["telemetry"], {"nozzle_temp": 245.0, "progress": 97, "job_name": "vase"})
        self.assertEqual(printers["p1s-01"]["status"], "idle")

    def test_camera_only_heartbeat_keeps_last_telemetry(self):
        self._hb("a1-01", telemetry={"progress": 50})
        self._hb("a1-01", status="unknown")
        a1 = self._state()["printers"][0]
        self.assertEqual((a1["status"], a1["telemetry"]["progress"]), ("unknown", 50))

    def test_printer_error_alert_is_edge_triggered(self):
        self._hb("a1-01", status="error", telemetry={"print_error": 50348044})
        alerts = [a for a in self._state()["alerts"] if a["source"] == "printer"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("50348044", alerts[0]["message"])
        _req(self._port, "POST", f"/api/alerts/{alerts[0]['id']}/ack", body=b"").read()
        self._hb("a1-01", status="error", telemetry={"print_error": 50348044})
        open_alerts = [a for a in self._state()["alerts"]
                       if a["source"] == "printer" and not a["acknowledged"]]
        self.assertEqual(open_alerts, [])

    def test_invalid_status_rejected(self):
        self.assertEqual(self._hb("a1-01", status="exploded"), 400)


class TestVisionAlertGating(unittest.TestCase):
    """Vision alerts: only during an active job, one open alert per printer."""

    def setUp(self):
        from server import open_db
        self._tmp = tempfile.mkdtemp(prefix="pp_vision_")
        self._db = os.path.join(self._tmp, "t.db")
        self._conn = open_db(self._db)
        self._img = os.path.join(self._tmp, "s.jpg")
        with open(self._img, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")
        self._cfg = _make_config(self._tmp, ai_base_url="http://ai", ai_key="k", vision_model="v")

    def _queue(self, status, printer="a1-01"):
        sid, qid = str(uuid.uuid4()), str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO snapshots (id,created_at,device_id,printer_id,captured_at,status,image_path) VALUES (?,?,?,?,?,?,?)",
            (sid, "2026-01-01T00:00:00Z", "redmi", printer, "2026-01-01T00:00:00Z", status, self._img))
        self._conn.execute("INSERT INTO vision_queue (id,snapshot_id,queued_at,next_at) VALUES (?,?,?,?)",
                           (qid, sid, "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))
        self._conn.commit()

    def _run(self, issue):
        from unittest import mock
        import server
        with mock.patch.object(server, "ai_vision",
                               return_value={"ok": False, "issues": [issue], "summary": issue}):
            server.VisionWorker(self._cfg, self._db)._process_one(self._conn)

    def _vision_alerts(self):
        return self._conn.execute("SELECT * FROM alerts WHERE source='vision'").fetchall()

    def test_idle_printer_no_alert(self):
        self._queue("idle")
        self._run("Còn sót nhựa cũ trên bàn in")
        self.assertEqual(self._vision_alerts(), [])

    def test_printing_alert_then_dedup_by_printer(self):
        self._queue("printing")
        self._run("Có sợi nhựa rối")
        self._queue("printing")
        self._run("Phát hiện nhựa bị rối như mì")
        self.assertEqual(len(self._vision_alerts()), 1)
        self._queue("printing", printer="a1-02")
        self._run("Có sợi nhựa rối")
        self.assertEqual(len(self._vision_alerts()), 2)


class TestSettings(ServerTestCase):
    def setUp(self):
        super().setUp()
        self._cfg_path = os.path.join(self._tmpdir, "server.json")
        with open(self._cfg_path, "w") as fh:
            json.dump(dict(self._cfg, ai_key="sk-secret-123"), fh)
        self._cfg["ai_key"] = "sk-secret-123"
        self._cfg["_config_path"] = self._cfg_path

    def test_get_masks_secrets(self):
        resp = _req(self._port, "GET", "/api/settings")
        raw = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertNotIn("sk-secret-123", raw)
        data = json.loads(raw)
        self.assertTrue(data["settings"]["ai_key_set"])
        self.assertFalse(data["settings"]["telegram_bot_token_set"])
        self.assertIn("snapshot_count", data["storage"])

    def test_update_persists_with_private_mode(self):
        code, data = _jreq(self._port, "POST", "/api/settings", {
            "telegram_bot_token": "123:abc", "telegram_chat_id": "42",
            "capture_when_idle": True, "snapshot_retention_hours": 12})
        self.assertEqual(code, 200)
        self.assertTrue(data["settings"]["telegram_bot_token_set"])
        with open(self._cfg_path) as fh:
            on_disk = json.load(fh)
        self.assertEqual((on_disk["telegram_chat_id"], on_disk["snapshot_retention_hours"]), ("42", 12))
        self.assertEqual(on_disk["ai_key"], "sk-secret-123")  # untouched
        self.assertEqual(os.stat(self._cfg_path).st_mode & 0o777, 0o600)
        self.assertEqual(self._cfg["telegram_bot_token"], "123:abc")  # live

    def test_update_validation(self):
        for bad in ({"snapshot_interval_seconds": 5}, {"ai_base_url": "ftp://x"},
                    {"unknown": 1}, {"vision_enabled": "yes"}, {}):
            code, _ = _jreq(self._port, "POST", "/api/settings", bad)
            self.assertEqual(code, 400, bad)

    def test_settings_require_manager(self):
        code, _ = _jreq(self._port, "POST", "/api/settings", {"vision_enabled": False}, token=DEVICE_TOKEN)
        self.assertEqual(code, 401)
        self.assertEqual(_req(self._port, "GET", "/api/settings", token=None).status, 401)

    def test_heartbeat_returns_policy(self):
        _jreq(self._port, "POST", "/api/settings", {"capture_when_idle": True, "snapshot_interval_seconds": 300})
        code, data = _jreq(self._port, "POST", "/api/heartbeat",
                           {"device_id": "r", "printer_id": "p", "status": "idle"}, token=DEVICE_TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(data["policy"], {"capture_when_idle": True, "snapshot_interval_seconds": 300})


class TestRetention(unittest.TestCase):
    def setUp(self):
        from server import open_db
        self._dir = tempfile.mkdtemp(prefix="pp_ret_")
        os.makedirs(os.path.join(self._dir, "snapshots"))
        self._conn = open_db(os.path.join(self._dir, "t.db"))

    def _snap(self, sid, age_hours):
        path = os.path.join(self._dir, "snapshots", f"{sid}.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8")
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_hours * 3600))
        self._conn.execute(
            "INSERT INTO snapshots (id,created_at,device_id,printer_id,captured_at,status,image_path) VALUES (?,?,?,?,?,?,?)",
            (sid, created, "d", "p", created, "idle", path))
        self._conn.commit()
        return path

    def test_prunes_by_configured_hours_but_keeps_alert_evidence(self):
        from server import prune_old_data, upsert_alert
        old = self._snap("old", 30)
        evidence = self._snap("evidence", 30)
        fresh = self._snap("fresh", 1)
        upsert_alert(self._conn, "vision", "warning", "spaghetti", "d", "p", "evidence")
        removed = prune_old_data(self._conn, {"snapshot_retention_hours": 24}, self._dir)
        self.assertEqual(removed["snapshots"], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(evidence))
        self.assertTrue(os.path.exists(fresh))

    def test_removes_orphan_files(self):
        from server import prune_old_data
        orphan = os.path.join(self._dir, "snapshots", "orphan.jpg")
        with open(orphan, "wb") as fh:
            fh.write(b"x")
        os.utime(orphan, (time.time() - 3600, time.time() - 3600))
        self._snap("keep", 1)
        removed = prune_old_data(self._conn, {"snapshot_retention_hours": 24}, self._dir)
        self.assertEqual(removed["orphans"], 1)
        self.assertFalse(os.path.exists(orphan))


class TestActivityLog(ServerTestCase):
    def _hb(self, printer, status, **extra):
        data = {"device_id": "redmi", "printer_id": printer, "status": status, "printer_name": printer.upper()}
        data.update(extra)
        return _jreq(self._port, "POST", "/api/heartbeat", data, token=DEVICE_TOKEN)[0]

    def _events(self, query=""):
        return json.loads(_req(self._port, "GET", "/api/events" + query).read())

    def test_print_lifecycle_is_logged_per_printer(self):
        self._hb("a1", "idle")
        self._hb("a1", "printing", telemetry={"job_name": "vase"})
        self._hb("a1", "unknown")                      # telemetry gap: no event
        self._hb("a1", "printing", telemetry={"job_name": "vase"})
        self._hb("a1", "idle", telemetry={"gcode_state": "FINISH", "progress": 100, "job_name": "vase"})
        self._hb("b2", "idle")
        msgs = [e["message"] for e in self._events("?printer=a1")["events"]]
        self.assertEqual(msgs, ["A1 hoàn thành bản in «vase»", "A1 bắt đầu in «vase»",
                                "Phát hiện máy in mới: A1 (qua redmi)"])
        b2 = self._events("?printer=b2")["events"]
        self.assertEqual(len(b2), 1)

    def test_ack_records_actor_and_settings_hide_values(self):
        self._hb("a1", "idle", error="camera timeout")
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        alert = next(a for a in state["alerts"] if not a["acknowledged"])
        _req(self._port, "POST", f"/api/alerts/{alert['id']}/ack", body=b"").read()
        _jreq(self._port, "POST", "/api/settings", {"telegram_bot_token": "999:SECRET"})
        events = self._events()["events"]
        ack = next(e for e in events if e["message"].startswith("Đã xử lý cảnh báo"))
        self.assertEqual(ack["actor"], "mã quản lý")
        self.assertEqual(ack["printer_id"], "a1")
        raw = _req(self._port, "GET", "/api/events").read().decode()
        self.assertIn("telegram_bot_token", raw)
        self.assertNotIn("999:SECRET", raw)
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        acked = next(a for a in state["alerts"] if a["id"] == alert["id"])
        self.assertEqual(acked["acked_by"], "mã quản lý")

    def test_filters_and_pagination(self):
        for i in range(5):
            self._hb(f"p{i}", "idle")
        page1 = self._events("?limit=2&category=device")
        self.assertEqual(len(page1["events"]), 2)
        page2 = self._events(f"?limit=2&category=device&before={page1['next']}")
        self.assertEqual(len(page2["events"]), 2)
        self.assertTrue(all(e["id"] < page1["events"][-1]["id"] for e in page2["events"]))
        self.assertEqual(_req(self._port, "GET", "/api/events?category=nope").status, 400)
        self.assertEqual(_req(self._port, "GET", "/api/events", token=DEVICE_TOKEN).status, 401)

    def test_offline_and_reconnect(self):
        from server import HeartbeatMonitor, open_db
        self._hb("a1", "idle")
        conn = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        conn.execute("UPDATE printers SET last_seen='2000-01-01T00:00:00Z'")
        conn.commit()
        HeartbeatMonitor(self._cfg, os.path.join(self._tmpdir, "printpilot.db"))._check(conn)
        HeartbeatMonitor(self._cfg, os.path.join(self._tmpdir, "printpilot.db"))._check(conn)
        self._hb("a1", "idle")
        msgs = [e["message"] for e in self._events("?printer=a1")["events"]]
        self.assertEqual(msgs[:2], ["A1 đã kết nối lại", "A1 mất kết nối"])
        self.assertEqual(msgs.count("A1 mất kết nối"), 1)


class TestPrinterCommands(ServerTestCase):
    def _hb(self, status="printing", printer="a1"):
        return _jreq(self._port, "POST", "/api/heartbeat",
                     {"device_id": "redmi", "printer_id": printer, "status": status}, token=DEVICE_TOKEN)[0]

    def _cmd(self, command, params=None, printer="a1", token=MANAGER_TOKEN):
        return _jreq(self._port, "POST", f"/api/printers/{printer}/commands",
                     {"command": command, "params": params or {}}, token=token)

    def _poll(self):
        return _jreq(self._port, "POST", "/api/device/commands/poll", {"device_id": "redmi"}, token=DEVICE_TOKEN)

    def test_full_round_trip(self):
        self._hb("printing")
        code, data = self._cmd("pause")
        self.assertEqual(code, 202)
        cmd_id = data["id"]
        code, polled = self._poll()
        self.assertEqual(polled["commands"], [{"id": cmd_id, "printer_id": "a1", "command": "pause", "params": {}}])
        self.assertEqual(self._poll()[1]["commands"], [])  # delivered once
        code, _ = _jreq(self._port, "POST", "/api/device/commands/result",
                        {"id": cmd_id, "ok": True, "reason": "success"}, token=DEVICE_TOKEN)
        self.assertEqual(code, 200)
        status = json.loads(_req(self._port, "GET", f"/api/commands/{cmd_id}").read())
        self.assertEqual(status["status"], "done")
        events = json.loads(_req(self._port, "GET", "/api/events?printer=a1").read())["events"]
        self.assertTrue(any("Máy in đã thực hiện lệnh tạm dừng in" in e["message"] for e in events))
        self.assertTrue(any(e["actor"] == "mã quản lý" and e["message"].startswith("Yêu cầu tạm dừng") for e in events))

    def test_state_rules(self):
        self._hb("idle")
        self.assertEqual(self._cmd("pause")[0], 400)
        self.assertEqual(self._cmd("resume")[0], 400)
        self.assertEqual(self._cmd("stop")[0], 400)
        self.assertEqual(self._cmd("light", {"mode": "on"})[0], 202)

    def test_param_validation_and_unknown(self):
        self._hb("printing")
        self.assertEqual(self._cmd("light", {"mode": "blink"})[0], 400)
        self.assertEqual(self._cmd("speed", {"level": 9})[0], 400)
        self.assertEqual(self._cmd("gcode", {"line": "M104 S300"})[0], 400)
        self.assertEqual(self._cmd("pause", printer="nope")[0], 404)

    def test_one_command_at_a_time(self):
        self._hb("printing")
        self.assertEqual(self._cmd("speed", {"level": 3})[0], 202)
        self.assertEqual(self._cmd("pause")[0], 409)

    def test_auth_separation(self):
        self._hb("printing")
        self.assertEqual(self._cmd("pause", token=DEVICE_TOKEN)[0], 401)
        code, _ = _jreq(self._port, "POST", "/api/device/commands/poll", {"device_id": "redmi"})
        self.assertEqual(code, 401)  # manager token cannot pull commands

    def test_pending_command_expires(self):
        from server import open_db
        self._hb("printing")
        cmd_id = self._cmd("pause")[1]["id"]
        conn = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        conn.execute("UPDATE printer_commands SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (cmd_id,))
        conn.commit()
        self.assertEqual(self._poll()[1]["commands"], [])
        status = json.loads(_req(self._port, "GET", f"/api/commands/{cmd_id}").read())
        self.assertEqual(status["status"], "expired")

    def test_offline_printer_rejected(self):
        from server import open_db
        self._hb("printing")
        conn = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        conn.execute("UPDATE printers SET last_seen='2000-01-01T00:00:00Z'")
        conn.commit()
        code, data = self._cmd("light", {"mode": "on"})
        self.assertEqual(code, 400)
        self.assertIn("mất kết nối", data["error"])


class TestLiveCamera(ServerTestCase):
    JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"

    def setUp(self):
        super().setUp()
        import server
        server.LIVE._sessions.clear()
        _jreq(self._port, "POST", "/api/heartbeat",
              {"device_id": "redmi", "printer_id": "a1", "status": "printing"}, token=DEVICE_TOKEN)

    def _poll(self):
        return _jreq(self._port, "POST", "/api/device/commands/poll", {"device_id": "redmi"}, token=DEVICE_TOKEN)[1]

    def _put(self, token=DEVICE_TOKEN):
        return _jreq(self._port, "POST", "/api/device/live/frame",
                     {"printer_id": "a1", "image_base64": base64.b64encode(self.JPEG).decode()}, token=token)

    def test_stream_round_trip_and_stop(self):
        self.assertEqual(self._poll()["live"], [])
        self.assertEqual(self._put()[1]["accepted"], False)       # nobody watching: dropped
        code, data = _jreq(self._port, "POST", "/api/printers/a1/live", {})
        self.assertEqual((code, data["live"]), (200, True))
        self.assertEqual(self._poll()["live"], ["a1"])
        self.assertEqual(self._put()[1]["accepted"], True)
        resp = _req(self._port, "GET", "/api/printers/a1/live/frame?after=0")
        body = resp.read()
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "image/jpeg"))
        self.assertEqual(body, self.JPEG)
        seq = resp.getheader("X-Frame-Seq")
        t0 = time.time()
        resp = _req(self._port, "GET", f"/api/printers/a1/live/frame?after={seq}")
        resp.read()
        self.assertEqual(resp.status, 204)
        self.assertGreater(time.time() - t0, 2)
        _jreq(self._port, "POST", "/api/printers/a1/live", {"action": "stop"})
        self.assertEqual(self._poll()["live"], [])
        events = json.loads(_req(self._port, "GET", "/api/events?printer=a1").read())["events"]
        self.assertTrue(any("xem camera trực tiếp" in e["message"] for e in events))

    def test_frames_never_hit_disk(self):
        _jreq(self._port, "POST", "/api/printers/a1/live", {})
        self._put()
        self.assertEqual(os.listdir(os.path.join(self._tmpdir, "snapshots")) if os.path.isdir(
            os.path.join(self._tmpdir, "snapshots")) else [], [])

    def test_auth_and_validation(self):
        _jreq(self._port, "POST", "/api/printers/a1/live", {})
        self.assertEqual(self._put(token=MANAGER_TOKEN)[0], 401)
        self.assertEqual(_req(self._port, "GET", "/api/printers/a1/live/frame", token=DEVICE_TOKEN).status, 401)
        code, _ = _jreq(self._port, "POST", "/api/device/live/frame",
                        {"printer_id": "a1", "image_base64": base64.b64encode(b"GIF89a").decode()}, token=DEVICE_TOKEN)
        self.assertEqual(code, 400)
        self.assertEqual(_jreq(self._port, "POST", "/api/printers/ghost/live", {})[0], 404)



# ---------------------------------------------------------------------------
# Test: project preview (thumbnail, 3D mesh, download) and dashboard upload
# ---------------------------------------------------------------------------

def _make_bambu_3mf(title: str | None = "Móc khoá") -> bytes:
    """Bambu-style split 3MF: build item -> components in 3D/Objects, painted triangles."""
    core = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ' \
           'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
    meta = f'<metadata name="Title">{title}</metadata>' if title else ''
    main = (f'<model unit="millimeter" {core}>{meta}<resources>'
            '<object id="5" type="model"><components>'
            '<p:component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 2"/>'
            '<p:component p:path="/3D/Objects/object_1.model" objectid="2"/>'
            '</components></object></resources>'
            '<build><item objectid="5" transform="1 0 0 0 1 0 0 0 1 100 50 0"/></build></model>')
    part = (f'<model unit="millimeter" {core}><resources>'
            '<object id="1" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/>'
            '</vertices><triangles><triangle v1="0" v2="1" v3="2" paint_color="8"/>'
            '<triangle v1="0" v2="2" v3="1"/></triangles></mesh></object>'
            '<object id="2" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
            '</vertices><triangles><triangle v1="0" v2="1" v3="2" paint_color="0C"/></triangles></mesh></object>'
            '</resources></model>')
    settings = {"filament_colour": ["#000000", "#FB3198", "#00F1FC"], "layer_height": "0.2",
                "wall_loops": "2", "sparse_infill_density": "15%", "nozzle_diameter": ["0.4"],
                "brim_width": "5"}
    model_settings = ('<config><object id="5"><metadata key="name" value="keychain part"/>'
                      '<metadata key="extruder" value="1"/>'
                      '<part id="1" subtype="normal_part"><metadata key="extruder" value="1"/></part>'
                      '<part id="2" subtype="normal_part"><metadata key="extruder" value="2"/></part>'
                      '</object></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", main)
        zf.writestr("3D/Objects/object_1.model", part)
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        zf.writestr("Metadata/model_settings.config", model_settings)
        zf.writestr("Metadata/plate_1_small.png", b"\x89PNG\r\n\x1a\nsmall")
        zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\nplate")
    return buf.getvalue()


def _decode_preview(mesh: bytes):
    import struct
    assert mesh[:4] == b"PPM1"
    nv, nt, nc = struct.unpack("<III", mesh[4:16])
    off = 16 + -(-nc * 3 // 4) * 4
    pal = list(mesh[16:16 + nc * 3])
    pos = struct.unpack(f"<{nv * 3}f", mesh[off:off + nv * 12])
    idx = struct.unpack(f"<{nt * 3}I", mesh[off + nv * 12:off + nv * 12 + nt * 12])
    col = list(mesh[off + nv * 12 + nt * 12:])
    return nv, nt, pal, pos, idx, col


class TestPreviewMesh(unittest.TestCase):
    def test_paint_state_decoding(self):
        self.assertEqual(_paint_state("4"), 1)
        self.assertEqual(_paint_state("8"), 2)
        self.assertEqual(_paint_state("0C"), 3)
        self.assertEqual(_paint_state("1C"), 4)
        # Split into 4 children (nibble 3): three painted state 2, one unpainted
        self.assertEqual(_paint_state("08883"), 2)
        self.assertEqual(_paint_state("zz"), 0)

    def test_components_transforms_and_colors(self):
        nv, nt, pal, pos, idx, col = _decode_preview(build_preview_mesh(_make_bambu_3mf()))
        self.assertEqual((nv, nt), (6, 3))
        self.assertEqual(pal[:9], [0, 0, 0, 0xFB, 0x31, 0x98, 0x00, 0xF1, 0xFC])
        # Component transform (z+2) composed with build item (x+100, y+50)
        self.assertEqual(pos[0:3], (100.0, 50.0, 2.0))
        self.assertEqual(pos[3:6], (110.0, 50.0, 2.0))
        # painted extruder 2, unpainted part extruder 1, part 2 painted extruder 3
        self.assertEqual(col, [1, 0, 2])
        self.assertTrue(all(i < nv for i in idx))

    def test_rejects_missing_part_and_bad_index(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", '<model><resources><object id="1"><mesh><vertices>'
                        '<vertex x="0" y="0" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/>'
                        '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
        with self.assertRaises(ExtractionError):
            build_preview_mesh(buf.getvalue())
        with self.assertRaises(ExtractionError):
            build_preview_mesh(b"not a zip")

    def test_thumbnail_prefers_plate_render(self):
        data, mime = best_thumbnail(_make_bambu_3mf())
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.endswith(b"plate"))
        self.assertIsNone(best_thumbnail(b"nope"))

    def test_title_and_settings_from_project(self):
        extracted = extract_3mf(_make_bambu_3mf())
        self.assertEqual(extracted["title"], "Móc khoá")
        self.assertEqual(display_filename("07dfb774-34af-4d2e-b78a-1a94c2c9d893.3mf", extracted["title"]), "Móc khoá.3mf")
        self.assertEqual(display_filename("my part.3mf", "Other"), "my part.3mf")
        self.assertEqual(extract_3mf(_make_bambu_3mf(title=None))["title"], "keychain part")
        settings = settings_from_config(extracted["config"])
        self.assertEqual(settings["layer_height"], "0.2")
        self.assertEqual(settings["sparse_infill_density"], "15%")
        self.assertEqual(settings["nozzle_diameter"], "0.4")
        self.assertEqual(validate_settings(settings)["wall_loops"], 2)


class _RecordingAI(FakeAIHandler):
    requests: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).requests.append(json.loads(body))
        content = json.dumps({"summary": "Ổn", "warnings": [],
                              "changes": [{"key": "wall_loops", "old": 2, "value": 3, "reason": "Chắc hơn"}]})
        resp = json.dumps({"model": "gpt-test", "choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class TestUploadAndPreviewEndpoints(ServerWithAITestCase):
    _fake_ai_handler_class = _RecordingAI

    def _upload(self, filename="07dfb774-34af-4d2e-b78a-1a94c2c9d893.3mf"):
        return _jreq(self._port, "POST", "/api/analyze", {
            "filename": filename, "project_base64": base64.b64encode(_make_bambu_3mf()).decode(),
            "objective": "strength", "trigger": "upload", "settings": {}})

    def test_upload_uses_project_settings_and_vietnamese_prompt(self):
        _RecordingAI.requests = []
        status, data = self._upload()
        self.assertEqual(status, 200, data)
        self.assertNotEqual(data["status"], "error", data)
        self.assertEqual(data["changes"][0]["value"], 3)
        sent = _RecordingAI.requests[-1]["messages"]
        self.assertIn("Vietnamese", sent[0]["content"])
        self.assertIn('"wall_loops":2', sent[1]["content"])
        resp = _req(self._port, "GET", "/api/state")
        state = json.loads(resp.read())
        self.assertEqual(state["analyses"][0]["filename"], "Móc khoá.3mf")
        self.assertEqual(state["analyses"][0]["trigger"], "upload")

    def test_assets_require_manager_and_serve_file_thumbnail_mesh(self):
        _, data = self._upload()
        base = f"/api/analyses/{data['id']}"
        for kind in ("file", "thumbnail", "mesh"):
            self.assertEqual(_req(self._port, "GET", f"{base}/{kind}", token=None).status, 401)
            self.assertEqual(_req(self._port, "GET", f"{base}/{kind}", token=DEVICE_TOKEN).status, 401)
        resp = _req(self._port, "GET", f"{base}/file")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), _make_bambu_3mf())
        self.assertIn("filename*=UTF-8''M%C3%B3c%20kho%C3%A1.3mf", resp.getheader("Content-Disposition"))
        resp = _req(self._port, "GET", f"{base}/thumbnail")
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "image/png"))
        self.assertTrue(resp.read().endswith(b"plate"))
        resp = _req(self._port, "GET", f"{base}/mesh")
        self.assertEqual(resp.status, 200)
        self.assertEqual(_decode_preview(resp.read())[1], 3)
        # Second request comes from the gzip cache, served compressed when accepted
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        conn.request("GET", f"{base}/mesh", headers={"Authorization": f"Bearer {MANAGER_TOKEN}", "Accept-Encoding": "gzip"})
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        import gzip as _gzip
        self.assertEqual(_decode_preview(_gzip.decompress(resp.read()))[1], 3)

    def test_missing_analysis_and_expired_file(self):
        self.assertEqual(_req(self._port, "GET", f"/api/analyses/{uuid.uuid4()}/mesh").status, 404)
        _, data = self._upload()
        db = sqlite3.connect(os.path.join(self._tmpdir, "printpilot.db"))
        path = db.execute("SELECT file_path FROM analyses WHERE id=?", (data["id"],)).fetchone()[0]
        _req(self._port, "GET", f"/api/analyses/{data['id']}/mesh").read()
        self.assertTrue(os.path.exists(path + ".mesh.gz"))
        db.execute("UPDATE analyses SET created_at='2000-01-01T00:00:00Z'")
        db.commit()
        conn = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        prune_old_data(conn, None, self._tmpdir)
        self.assertFalse(os.path.exists(path + ".mesh.gz"))
        self.assertFalse(os.path.exists(path))

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAmsEditFirmwareBlock(ServerTestCase):
    def test_firmware_rejection_disables_material_edits(self):
        hb = {"device_id": "redmi", "printer_id": "a1", "status": "idle", "printer_name": "A1"}
        self.assertEqual(_jreq(self._port, "POST", "/api/heartbeat", hb, token=DEVICE_TOKEN)[0], 200)
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        self.assertTrue(state["printers"][0]["ams_edit_supported"])
        conn = sqlite3.connect(os.path.join(self._tmpdir, "printpilot.db"))
        conn.execute("INSERT INTO printer_commands (id, created_at, updated_at, printer_id, device_id, command, params, status, actor, result) "
                     "VALUES ('c1','2026-11-21T14:00:00Z','2026-11-21T14:00:03Z','a1','redmi','ams_filament_setting','{}','failed','x',"
                     "'máy in từ chối thay đổi filament: mã 0502-4007 (84033543)')")
        conn.commit(); conn.close()
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        self.assertFalse(state["printers"][0]["ams_edit_supported"])
        code, data = _jreq(self._port, "POST", "/api/printers/a1/commands",
                           {"command": "ams_filament_setting", "params": {"ams_id": 0, "slot_id": 0, "profile_id": "x", "color": "#ffffff"}})
        self.assertEqual(code, 409)
        self.assertIn("0502-4007", data["error"])
        # Other commands are unaffected.
        code, _ = _jreq(self._port, "POST", "/api/printers/a1/commands", {"command": "light", "params": {"mode": "on"}})
        self.assertEqual(code, 202)
