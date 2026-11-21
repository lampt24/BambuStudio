"""Unit tests for PrintPilotApiClient and error sanitization."""

import io
import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from services.printpilot.bridge.client import (
    PrintPilotApiClient,
    _NoRedirect,
    sanitize_message,
)
from services.printpilot.bridge.exceptions import NetworkError, ServerApiError


class TestPrintPilotClient(unittest.TestCase):

    def test_redirect_does_not_forward_credentials(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def setUp(self):
        self.server_url = "http://127.0.0.1:8000"
        self.token = "test-device-token-secret-12345"
        self.client = PrintPilotApiClient(
            server_url=self.server_url,
            device_token=self.token,
            timeout=5.0,
        )

    def test_sanitize_message(self):
        msg = f"Failed with Bearer {self.token} and access_code 87654321"
        clean = sanitize_message(msg)
        self.assertNotIn(self.token, clean)
        self.assertNotIn("87654321", clean)
        self.assertIn("[REDACTED]", clean)

    @patch("services.printpilot.bridge.client._OPENER.open")
    def test_send_snapshot_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "snap-123", "status": "stored"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        fake_jpeg = b"\xff\xd8IMAGE_BYTES\xff\xd9"
        res = self.client.send_snapshot(
            device_id="termux-01",
            printer_id="a1-01",
            image_bytes=fake_jpeg,
            status="idle",
            telemetry={"temp": 210},
        )

        self.assertEqual(res["id"], "snap-123")
        self.assertEqual(res["status"], "stored")

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_full_url(), "http://127.0.0.1:8000/api/snapshots")
        self.assertEqual(req.headers["Authorization"], f"Bearer {self.token}")
        self.assertEqual(req.headers["Content-type"], "application/json")

        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["device_id"], "termux-01")
        self.assertEqual(body["printer_id"], "a1-01")
        self.assertEqual(body["status"], "idle")
        self.assertIn("image_base64", body)
        self.assertIn("captured_at", body)
        self.assertEqual(body["telemetry"]["temp"], 210)

    @patch("services.printpilot.bridge.client._OPENER.open")
    def test_send_heartbeat_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.client.send_heartbeat(
            device_id="termux-01",
            printer_id="a1-01",
            status="idle",
        )

        self.assertEqual(res["status"], "ok")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_full_url(), "http://127.0.0.1:8000/api/heartbeat")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["status"], "idle")
        self.assertNotIn("error", body)

    @patch("services.printpilot.bridge.client._OPENER.open")
    def test_send_heartbeat_with_sanitized_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        raw_err = f"Failed with Bearer {self.token}"
        self.client.send_heartbeat(
            device_id="termux-01",
            printer_id="a1-01",
            status="unknown",
            error=raw_err,
        )

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["status"], "unknown")
        self.assertNotIn(self.token, body["error"])
        self.assertIn("[REDACTED]", body["error"])

    @patch("services.printpilot.bridge.client._OPENER.open")
    def test_server_http_error_raises_api_error(self, mock_urlopen):
        fp = io.BytesIO(b"Unauthorized access")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/snapshots",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=fp,
        )

        with self.assertRaises(ServerApiError) as ctx:
            self.client.send_heartbeat(device_id="d1", printer_id="p1")
        self.assertIn("401", str(ctx.exception))

    @patch("services.printpilot.bridge.client._OPENER.open")
    def test_network_url_error_raises_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with self.assertRaises(NetworkError):
            self.client.send_heartbeat(device_id="d1", printer_id="p1")

    def test_retry_on_network_error(self):
        mock_fn = MagicMock()
        mock_fn.side_effect = [
            NetworkError("Connection reset"),
            {"id": "ok"},
        ]

        res = self.client.send_with_retry(mock_fn, retries=3, initial_backoff=0.01)
        self.assertEqual(res["id"], "ok")
        self.assertEqual(mock_fn.call_count, 2)


if __name__ == "__main__":
    unittest.main()
