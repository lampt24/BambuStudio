"""Read-only Bambu printer telemetry over the printer's local MQTT broker.

Works while the printer stays in cloud mode (LAN Only Mode is not needed):
the local broker on port 8883 accepts user ``bblp`` + the access code shown on
the printer screen. We only subscribe to ``device/<serial>/report`` and send
the ``pushall`` read request; no print-control commands are ever published.

Pure standard library (minimal MQTT 3.1.1) so it runs on Termux without pip.
"""

from __future__ import annotations

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
    return f if f == f else None  # drop NaN


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
    return out


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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None

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
            return normalize(self._raw)

    # -- internals ----------------------------------------------------------
    def merge(self, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        report = data.get("print") if isinstance(data, dict) else None
        if not isinstance(report, dict):
            return
        with self._lock:
            self._raw.update(report)
            self._updated_at = time.monotonic()
            current = normalize(self._raw)
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
                sock.settimeout(KEEPALIVE / 2)
                self._pushall(sock)
                last_push = time.monotonic()
                self.last_error = None
                backoff = 5.0
                logger.info("Telemetry connected to %s", self.host)
                while not self._stop.is_set():
                    try:
                        first, body = read_packet(sock)
                    except (socket.timeout, TimeoutError):
                        sock.sendall(PINGREQ)
                        continue
                    if first >> 4 == 3:
                        _topic, payload = parse_publish(first, body)
                        self.merge(payload)
                    if time.monotonic() - last_push > PUSHALL_INTERVAL:
                        self._pushall(sock)
                        last_push = time.monotonic()
            except (OSError, TelemetryError, ssl.SSLError, struct.error) as exc:
                self.last_error = str(exc)[:200]
                logger.warning("Telemetry %s: %s (retry in %.0fs)", self.host, self.last_error, backoff)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 300.0)
