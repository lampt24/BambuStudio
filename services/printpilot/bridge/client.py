"""PrintPilot Backend API Client.

Handles authenticated POST /api/snapshots and POST /api/heartbeat requests
using standard library urllib.request.
"""

import base64
from datetime import datetime, timezone
import json
import logging
import random
import re
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

try:
    from .exceptions import NetworkError, ServerApiError
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.exceptions import NetworkError, ServerApiError
    except ImportError:
        from exceptions import NetworkError, ServerApiError

logger = logging.getLogger("printpilot.client")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())

# Regex to sanitize tokens and access codes from error strings
SENSITIVE_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"access_code['\":\s]+[A-Za-z0-9]+", re.IGNORECASE),
    re.compile(r"token['\":\s]+[A-Za-z0-9._\-]+", re.IGNORECASE),
]


def sanitize_message(msg: str) -> str:
    """Strip tokens or sensitive strings from log/error messages."""
    clean = str(msg)
    for pat in SENSITIVE_PATTERNS:
        clean = pat.sub("[REDACTED]", clean)
    return clean


class PrintPilotApiClient:
    """Client for posting camera snapshots and device heartbeats to PrintPilot backend."""

    def __init__(
        self,
        server_url: str,
        device_token: str,
        timeout: float = 15.0,
        user_agent: str = "PrintPilot-A1-Bridge/0.1.0",
    ):
        self.server_url = server_url.rstrip("/")
        self.device_token = device_token
        self.timeout = timeout
        self.user_agent = user_agent

    def _make_request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send JSON POST request to PrintPilot server with Bearer auth."""
        url = f"{self.server_url}{endpoint}"
        body_bytes = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.device_token}",
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with _OPENER.open(req, timeout=self.timeout) as response:
                resp_bytes = response.read(1024 * 1024 + 1)
                if len(resp_bytes) > 1024 * 1024:
                    raise ServerApiError("Server response exceeds 1 MiB")
                if not resp_bytes:
                    return {}
                try:
                    return json.loads(resp_bytes.decode("utf-8"))
                except json.JSONDecodeError as e:
                    raise ServerApiError(
                        f"Non-JSON response from {endpoint}: {resp_bytes[:100]!r}"
                    ) from e
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read(200).decode("utf-8", errors="replace")
            except Exception:
                pass
            sanitized_body = sanitize_message(err_body)
            msg = f"HTTP {e.code} {e.reason} from {endpoint}: {sanitized_body}"
            logger.error("PrintPilot server API error: %s", msg)
            raise ServerApiError(msg) from e
        except urllib.error.URLError as e:
            clean_reason = sanitize_message(str(e.reason))
            msg = f"Network connection error to {url}: {clean_reason}"
            logger.error(msg)
            raise NetworkError(msg) from e
        except TimeoutError as e:
            msg = f"Request timed out connecting to {url}"
            logger.error(msg)
            raise NetworkError(msg) from e
        except OSError as e:
            clean_err = sanitize_message(str(e))
            msg = f"Socket error communicating with {url}: {clean_err}"
            logger.error(msg)
            raise NetworkError(msg) from e

    def send_snapshot(
        self,
        device_id: str,
        printer_id: str,
        image_bytes: bytes,
        status: str = "unknown",
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Post JPEG snapshot to POST /api/snapshots.

        Payload JSON:
        {
          "device_id": str,
          "printer_id": str,
          "captured_at": UTC ISO8601,
          "image_base64": str,
          "status": "printing" | "idle" | "unknown",
          "telemetry": object
        }
        """
        captured_at = datetime.now(timezone.utc).isoformat()
        image_base64 = base64.b64encode(image_bytes).decode("ascii")

        payload = {
            "device_id": device_id,
            "printer_id": printer_id,
            "captured_at": captured_at,
            "image_base64": image_base64,
            "status": status,
            "telemetry": telemetry or {},
        }

        logger.info(
            "Sending snapshot for printer %s (device %s, %d bytes)",
            printer_id,
            device_id,
            len(image_bytes),
        )
        return self._make_request("/api/snapshots", payload)

    def send_heartbeat(
        self,
        device_id: str,
        printer_id: str,
        status: str = "unknown",
        error: Optional[str] = None,
        telemetry: Optional[Dict[str, Any]] = None,
        printer_name: str = "",
        printer_model: str = "",
    ) -> Dict[str, Any]:
        """Post heartbeat status to POST /api/heartbeat.

        Payload JSON:
        {
          "device_id": str,
          "printer_id": str,
          "status": "printing" | "idle" | "unknown",
          "error": optional str (secrets redacted)
        }
        """
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "printer_id": printer_id,
            "status": status,
        }
        if error:
            payload["error"] = sanitize_message(error)
        if telemetry:
            payload["telemetry"] = telemetry
        if printer_name:
            payload["printer_name"] = printer_name[:80]
        if printer_model:
            payload["printer_model"] = printer_model[:40]

        logger.debug(
            "Sending heartbeat for printer %s (device %s, status=%s)",
            printer_id,
            device_id,
            status,
        )
        return self._make_request("/api/heartbeat", payload)

    def send_with_retry(
        self,
        fn,
        *args,
        retries: int = 3,
        initial_backoff: float = 1.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """Execute client method with retries and exponential backoff."""
        attempt = 0
        delay = initial_backoff
        while True:
            attempt += 1
            try:
                return fn(*args, **kwargs)
            except NetworkError as e:
                if attempt >= retries:
                    raise
                jitter = random.uniform(0.1, 0.4)
                logger.warning(
                    "Network error on attempt %d/%d (%s); retrying in %.2fs",
                    attempt,
                    retries,
                    e,
                    delay + jitter,
                )
                time.sleep(delay + jitter)
                delay *= 2.0
            except ServerApiError:
                # HTTP errors (e.g. 401 unauthorized, 400 bad request) should not retry blindly
                raise
