"""Bambu printer telemetry and manager-authorized controls over the printer's local MQTT broker.

Works while the printer stays in cloud mode (LAN Only Mode is not needed):
the local broker on port 8883 accepts user ``bblp`` + the access code shown on
the printer screen. We subscribe to ``device/<serial>/report`` and send the
``pushall`` read request.

Control commands (pause/resume/stop, light, speed) are published only when
the PrintPilot server queues them on a manager's request.

Pure standard library (minimal MQTT 3.1.1) so it runs on Termux without pip.
"""

from __future__ import annotations

import math
import re
import uuid
try:
    from .ams import normalize_ams, merge_state, material_payload, find_slot, readback_matches
except ImportError:
    from ams import normalize_ams, merge_state, material_payload, find_slot, readback_matches

import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("printpilot.telemetry")

MQTT_PORT = 8883
KEEPALIVE = 60
PUSHALL_INTERVAL = 300.0
MAX_PACKET = 1024 * 1024

# Bambu gcode_state -> PrintPilot status
_STATE_MAP = {
    "RUNNING": "printing",
    "PREPARE": "printing",
    "SLICING": "printing",
    "PAUSE": "paused",
    "FAILED": "error",
    "FINISH": "idle",
    "IDLE": "idle",
}


class TelemetryError(Exception):
    pass


# ---------------------------------------------------------------------------
# MQTT packet helpers
# ---------------------------------------------------------------------------

def _str16(value: bytes) -> bytes:
    return struct.pack(">H", len(value)) + value


def _remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _packet(first_byte: int, body: bytes) -> bytes:
    return bytes([first_byte]) + _remaining_length(len(body)) + body


def build_connect(client_id: str, username: str, password: str) -> bytes:
    flags = 0xC2  # username + password + clean session
    body = (_str16(b"MQTT") + b"\x04" + bytes([flags]) + struct.pack(">H", KEEPALIVE)
            + _str16(client_id.encode()) + _str16(username.encode()) + _str16(password.encode()))
    return _packet(0x10, body)


def build_subscribe(packet_id: int, topic: str) -> bytes:
    return _packet(0x82, struct.pack(">H", packet_id) + _str16(topic.encode()) + b"\x00")


def build_publish(topic: str, payload: bytes) -> bytes:
    return _packet(0x30, _str16(topic.encode()) + payload)


PINGREQ = b"\xc0\x00"


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise TelemetryError("connection closed by printer")
        buf.extend(chunk)
    return bytes(buf)


def read_packet(sock) -> tuple[int, bytes]:
    first = _recv_exact(sock, 1)[0]
    multiplier, length = 1, 0
    for _ in range(4):
        byte = _recv_exact(sock, 1)[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    else:
        raise TelemetryError("malformed remaining length")
    if length > MAX_PACKET:
        raise TelemetryError("packet too large")
    return first, _recv_exact(sock, length)


def parse_publish(first: int, body: bytes) -> tuple[str, bytes]:
    tlen = struct.unpack(">H", body[:2])[0]
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    offset = 2 + tlen
    if (first >> 1) & 0x03:  # QoS > 0 carries a packet id
        offset += 2
    return topic, body[offset:]


# ---------------------------------------------------------------------------
# Telemetry normalisation
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None  # drop NaN


def normalize(report: Dict[str, Any]) -> Dict[str, Any]:
    """Map Bambu ``print`` report fields to the dashboard telemetry schema."""
    out: Dict[str, Any] = {}
    state = report.get("gcode_state")
    if isinstance(state, str) and state:
        out["gcode_state"] = state
    for src, dst in (("nozzle_temper", "nozzle_temp"), ("bed_temper", "bed_temp"),
                     ("nozzle_target_temper", "nozzle_target"), ("bed_target_temper", "bed_target")):
        v = _num(report.get(src))
        if v is not None:
            out[dst] = round(v, 1)
    for src, dst in (("mc_percent", "progress"), ("layer_num", "current_layer"),
                     ("total_layer_num", "total_layers"), ("mc_remaining_time", "remaining_min"),
                     ("print_error", "print_error")):
        v = _num(report.get(src))
        if v is not None:
            out[dst] = int(v)
    fan = _num(report.get("cooling_fan_speed"))
    if fan is not None:
        # Bambu reports 0-15; show percent
        out["fan_speed"] = int(round(fan / 15 * 100)) if fan <= 15 else int(fan)
    name = report.get("subtask_name")
    if isinstance(name, str) and name:
        out["job_name"] = name[:120]
    for key in ("task_id", "subtask_id", "project_id"):
        value = report.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            out[key] = str(value)[:160]
    # Studio sends one plate per job as Metadata/plate_N.gcode; anything else stays unknown.
    gcode_file = report.get("gcode_file")
    if isinstance(gcode_file, str):
        plate = re.search(r"(?:^|/)plate_(\d{1,2})\.gcode$", gcode_file.strip(), re.I)
        if plate and int(plate.group(1)) >= 1:
            out["plate_index"] = int(plate.group(1))
    nozzle = _num(report.get("nozzle_diameter"))
    if nozzle is not None and 0.1 <= nozzle <= 1.2:
        out["nozzle_diameter"] = round(nozzle, 2)
    nozzle_type = report.get("nozzle_type")
    if isinstance(nozzle_type, str) and re.fullmatch(r"[a-z_]{1,32}", nozzle_type):
        out["nozzle_type"] = nozzle_type  # e.g. stainless_steel, hardened_steel
    out["ams"] = normalize_ams(report)
    lights = report.get("lights_report")
    if isinstance(lights, list):
        for item in lights:
            if isinstance(item, dict) and item.get("node") == "chamber_light" and item.get("mode") in ("on", "off"):
                out["light"] = item["mode"]
    speed = _num(report.get("spd_lvl"))
    if speed is not None and 1 <= speed <= 4:
        out["speed_level"] = int(speed)
    return out


SUPPORTED_COMMANDS = {"pause", "resume", "stop", "light", "speed", "ams_filament_setting"}


def command_payload(command: str, params: Dict[str, Any], seq: str) -> Dict[str, Any]:
    """MQTT request body for a whitelisted control command."""
    if command == "ams_filament_setting":
        return material_payload(params, seq)
    if command in ("pause", "resume", "stop"):
        return {"print": {"sequence_id": seq, "command": command, "param": ""}}
    if command == "light":
        mode = params.get("mode")
        if mode not in ("on", "off"):
            raise ValueError("invalid light mode")
        return {"system": {"sequence_id": seq, "command": "ledctrl", "led_node": "chamber_light",
                           "led_mode": mode, "led_on_time": 500, "led_off_time": 500,
                           "loop_times": 0, "interval_time": 0}}
    if command == "speed":
        level = params.get("level")
        if isinstance(level, bool) or level not in (1, 2, 3, 4):
            raise ValueError("invalid speed level")
        return {"print": {"sequence_id": seq, "command": "print_speed", "param": str(level)}}
    raise ValueError(f"unsupported command {command!r}")


def interpret_reply(reply: Dict[str, Any]) -> tuple[bool, str]:
    result = str(reply.get("result") or "").lower()
    err = reply.get("err_code")
    err = None if err in (None, 0, "0", "") else err
    if result == "success" and not err:
        return True, "success"
    if result in ("fail", "failed") or err:
        reason = reply.get("reason")
        detail = reason if reason and reason != "success" else ""
        try:
            hexcode = f"{int(err):08X}"
            code = f"mã {hexcode[:4]}-{hexcode[4:]} ({err})"
        except (TypeError, ValueError):
            code = f"mã {err}" if err else ""
        return False, " ".join(x for x in (detail, code) if x) or "máy in từ chối"
    return True, str(reply.get("reason") or "đã nhận")


def status_from(telemetry: Dict[str, Any]) -> str:
    if telemetry.get("print_error"):
        return "error"
    return _STATE_MAP.get(str(telemetry.get("gcode_state", "")).upper(), "unknown")


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------

def serial_from_cert(der: bytes) -> Optional[str]:
    """Bambu printer certificates carry the printer serial as the subject CN."""
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
            fh.write(ssl.DER_cert_to_PEM_cert(der))
            path = fh.name
        decoded = ssl._ssl._test_decode_cert(path)  # stdlib, no extra deps on Termux
        for rdn in decoded.get("subject", ()):
            for key, value in rdn:
                if key == "commonName" and value:
                    return value
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not decode printer certificate: %s", exc)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TelemetryClient:
    """Background reader keeping the latest merged telemetry for one printer."""

    def __init__(self, host: str, access_code: str, serial: Optional[str] = None,
                 cert_fingerprint: Optional[str] = None, port: int = MQTT_PORT,
                 connect_timeout: float = 10.0,
                 on_update: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.host = host
        self.port = port
        self.access_code = access_code
        self.serial = serial
        self.cert_fingerprint = (cert_fingerprint or "").replace(":", "").lower() or None
        self.connect_timeout = connect_timeout
        self.on_update = on_update
        self._raw: Dict[str, Any] = {}
        self._updated_at = 0.0
        self._ams_updated_at = 0.0
        self._slot_updated = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self._sock = None
        self._send_lock = threading.Lock()
        self._waiters: Dict[str, Dict[str, Any]] = {}  # sequence_id -> {"event", "reply"}
        self._material_readback = None
        self._full_report = threading.Event()  # set when a full status (pushall reply) arrives

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"telemetry-{self.host}")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, max_age: float = 180.0) -> Dict[str, Any]:
        """Latest normalised telemetry, or {} when nothing fresh is known."""
        with self._lock:
            if not self._raw or time.monotonic() - self._updated_at > max_age:
                return {}
            return self._with_identity(normalize(self._raw))

    def _with_identity(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        # Lets Studio name the printer it is sending to (serial) when several share one server.
        if self.serial and re.fullmatch(r"[A-Za-z0-9]{6,32}", self.serial):
            telemetry["serial"] = self.serial
        return telemetry

    def refresh(self, timeout: float = 3.0) -> bool:
        """Ask for a full status push and wait for it; True once it has been merged.

        Used right after a control command so the next heartbeat carries the
        printer's new state (light, speed, pause) instead of the cached one.
        """
        self._full_report.clear()
        try:
            with self._send_lock:
                if self._sock is None:
                    return False
                self._pushall(self._sock)
        except OSError:
            return False
        return self._full_report.wait(timeout)

    def wait_for(self, key: str, value: Any, timeout: float = 5.0) -> bool:
        """Refresh until normalised telemetry shows key == value (or time runs out).

        Right after a command the printer may answer pushall with its previous
        state, so a single refresh is not enough.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.snapshot().get(key) == value:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.refresh(min(1.0, left))
            if self.snapshot().get(key) == value:
                return True
            time.sleep(min(0.4, max(0.0, deadline - time.monotonic())))

    # -- internals ----------------------------------------------------------
    def send_command(self, command: str, params: Dict[str, Any], timeout: float = 10.0) -> tuple[bool, str]:
        """Publish a whitelisted command and wait for the printer's reply."""
        if command not in SUPPORTED_COMMANDS:
            return False, "lệnh không được hỗ trợ"
        seq = str(uuid.uuid4().int % 10**15)
        try:
            payload = command_payload(command, params, seq)
        except ValueError as exc:
            return False, str(exc)
        if command == "ams_filament_setting":
            if time.monotonic() - self._slot_updated.get((params['ams_id'], params['slot_id']), 0) > 30:
                # Normal idle deltas omit tags; pushall is a read request, not an edit.
                self.refresh(timeout=3.0)
            current = self.snapshot(max_age=30)
            slot = find_slot(current, params)
            if time.monotonic() - self._slot_updated.get((params['ams_id'], params['slot_id']), 0) > 30 or time.monotonic() - self._ams_updated_at > 30 or not slot or not slot['present'] or slot['rfid']:
                return False, "khe trống, cuộn RFID hoặc chưa đọc được trạng thái khe"
            if current.get('gcode_state', '').upper() not in ('IDLE', 'FINISH', 'FAILED'):
                return False, "chỉ chỉnh filament khi máy đang rảnh"
        waiter = {"event": threading.Event(), "reply": None}
        self._waiters[seq] = waiter
        try:
            with self._send_lock:
                sock = self._sock
                if sock is None:
                    return False, "cầu nối chưa kết nối MQTT tới máy in"
                sock.sendall(build_publish(f"device/{self.serial}/request", json.dumps(payload).encode()))
            if not waiter["event"].wait(timeout):
                return False, "máy in không phản hồi lệnh"
            reply = waiter["reply"] or {}
            if command != "ams_filament_setting":
                return interpret_reply(reply)
            logger.info("ams_filament_setting reply: %s", json.dumps(reply, ensure_ascii=False)[:600])
            # Native Studio treats the echoed command as the ACK (firmware often omits
            # "result"); an explicit failure or error code still rejects. Success is only
            # declared after the refreshed telemetry readback below matches.
            ok, reason = interpret_reply(reply)
            if reply.get('command') != command or not ok:
                return False, f"máy in từ chối thay đổi filament: {reason}" if not ok else "phản hồi không phải của lệnh filament"
            # Never use ACK identity fields as telemetry. Require a fresh, complete slot report.
            with self._lock:
                self._material_readback = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.refresh(min(0.5, max(0, deadline - time.monotonic())))
                with self._lock:
                    observed = self._material_readback
                if observed and readback_matches(observed, params):
                    return True, "đã cập nhật filament, dữ liệu đọc lại khớp"
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            return False, "máy đã nhận lệnh nhưng dữ liệu filament đọc lại chưa khớp thay đổi"
        except OSError as exc:
            return False, f"lỗi gửi lệnh: {exc}"
        finally:
            self._waiters.pop(seq, None)

    def merge(self, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if isinstance(data, dict) and self._waiters:
            for ns in ("print", "system"):
                body = data.get(ns)
                if isinstance(body, dict):
                    waiter = self._waiters.get(str(body.get("sequence_id", "")))
                    if waiter is not None:
                        waiter["reply"] = body
                        waiter["event"].set()
        report = data.get("print") if isinstance(data, dict) else None
        if not isinstance(report, dict):
            return
        with self._lock:
            # ACKs are not status reports and must not refresh stale cached telemetry.
            if report.get('command') == 'ams_filament_setting':
                return
            self._raw = merge_state(self._raw, report)
            if 'ams' in report or 'vt_tray' in report:
                self._material_readback = report
                self._ams_updated_at = time.monotonic()
                wire_ams = report.get('ams', {})
                if isinstance(wire_ams, dict) and isinstance(wire_ams.get('ams'), list):
                    for unit in wire_ams['ams']:
                        if not isinstance(unit, dict) or not isinstance(unit.get('tray'), list):
                            continue
                        for tray in unit['tray']:
                            if isinstance(tray, dict) and 'tag_uid' in tray:
                                try:
                                    self._slot_updated[(int(unit['id']), int(tray['id']))] = self._ams_updated_at
                                except (KeyError, ValueError, TypeError):
                                    pass
                if isinstance(report.get('vt_tray'), dict) and 'tag_uid' in report['vt_tray']:
                    self._slot_updated[(255, 0)] = self._ams_updated_at
            if any(key not in ('command', 'sequence_id', 'result', 'reason', 'err_code') for key in report):
                self._updated_at = time.monotonic()
            current = self._with_identity(normalize(self._raw))
        if "lights_report" in report and "gcode_state" in report:
            self._full_report.set()
        if self.on_update:
            try:
                self.on_update(current)
            except Exception:  # pragma: no cover
                logger.exception("telemetry callback failed")

    def _connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed; pinned by fingerprint below
        raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock = ctx.wrap_socket(raw)
        der = sock.getpeercert(True)
        if self.cert_fingerprint and hashlib.sha256(der).hexdigest() != self.cert_fingerprint:
            sock.close()
            raise TelemetryError("printer certificate fingerprint mismatch")
        if not self.serial:
            self.serial = serial_from_cert(der)
            if not self.serial:
                sock.close()
                raise TelemetryError("printer serial unknown")
        sock.sendall(build_connect(f"printpilot-{os.getpid()}", "bblp", self.access_code))
        first, body = read_packet(sock)
        if first >> 4 != 2 or len(body) < 2 or body[1] != 0:
            sock.close()
            raise TelemetryError("MQTT login rejected (check access code)")
        sock.sendall(build_subscribe(1, f"device/{self.serial}/report"))
        return sock

    def _pushall(self, sock) -> None:
        req = json.dumps({"pushing": {"sequence_id": str(int(time.time())), "command": "pushall"}})
        sock.sendall(build_publish(f"device/{self.serial}/request", req.encode()))

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._connect()
                with self._lock:
                    self._raw = {}
                    self._updated_at = 0.0
                    self._slot_updated = {}
                sock.settimeout(KEEPALIVE / 2)
                with self._send_lock:
                    self._sock = sock
                self._pushall(sock)
                last_push = time.monotonic()
                self.last_error = None
                backoff = 5.0
                logger.info("Telemetry connected to %s", self.host)
                while not self._stop.is_set():
                    try:
                        first, body = read_packet(sock)
                    except (socket.timeout, TimeoutError):
                        with self._send_lock:
                            sock.sendall(PINGREQ)
                        continue
                    if first >> 4 == 3:
                        _topic, payload = parse_publish(first, body)
                        self.merge(payload)
                    if time.monotonic() - last_push > PUSHALL_INTERVAL:
                        with self._send_lock:
                            self._pushall(sock)
                        last_push = time.monotonic()
            except (OSError, TelemetryError, ssl.SSLError, struct.error) as exc:
                self.last_error = str(exc)[:200]
                logger.warning("Telemetry %s: %s (retry in %.0fs)", self.host, self.last_error, backoff)
            finally:
                with self._send_lock:
                    self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 300.0)
