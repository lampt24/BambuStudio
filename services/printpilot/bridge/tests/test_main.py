"""Unit tests for BridgeService coordinator and CLI."""

import unittest
from unittest.mock import MagicMock, patch

from services.printpilot.bridge.config import BridgeConfig
from services.printpilot.bridge.exceptions import FingerprintMismatchError, NetworkError
from services.printpilot.bridge.main import BridgeService, main


class TestBridgeService(unittest.TestCase):

    def setUp(self):
        self.config = BridgeConfig(
            printer_host="192.168.1.100",
            printer_access_code="12345678",
            printer_id="a1-01",
            device_id="termux-01",
            server_url="http://127.0.0.1:8000",
            device_token="dev-token",
            snapshot_interval_seconds=120,
            heartbeat_interval_seconds=30,
        )
        self.service = BridgeService(self.config)
        self.service.camera = MagicMock()
        self.service.client = MagicMock()

    def test_capture_and_send_snapshot_success(self):
        fake_frame = b"\xff\xd8FRAME\xff\xd9"
        self.service.camera.capture_snapshot_with_retry.return_value = (
            fake_frame,
            {"itrack": 0},
        )

        success = self.service.capture_and_send_snapshot()
        self.assertTrue(success)

        # Check client methods were called with status "unknown" (not "idle")
        self.assertTrue(self.service.client.send_with_retry.called)
        snap_kwargs = self.service.client.send_with_retry.call_args[1]
        self.assertEqual(snap_kwargs["status"], "unknown")

        self.assertTrue(self.service.client.send_heartbeat.called)
        hb_args = self.service.client.send_heartbeat.call_args[1]
        self.assertEqual(hb_args["status"], "unknown")
        self.assertIsNone(self.service._last_error)

    def test_capture_and_send_snapshot_failure(self):
        self.service.camera.capture_snapshot_with_retry.side_effect = NetworkError(
            "Connection refused"
        )

        success = self.service.capture_and_send_snapshot()
        self.assertFalse(success)

        # Verify error heartbeat was sent with unknown status and retained error
        self.assertTrue(self.service.client.send_heartbeat.called)
        hb_args = self.service.client.send_heartbeat.call_args[1]
        self.assertEqual(hb_args["status"], "unknown")
        self.assertIn("Connection refused", hb_args["error"])
        self.assertEqual(self.service._last_error, "Connection refused")

    def test_periodic_heartbeat_retains_last_camera_error(self):
        # 1. Camera fails
        self.service.camera.capture_snapshot_with_retry.side_effect = NetworkError(
            "Camera socket timeout"
        )
        self.service.capture_and_send_snapshot()
        self.assertEqual(self.service._last_error, "Camera socket timeout")

        # 2. Periodic heartbeat must retain last error, not clear it with idle
        self.service.client.send_heartbeat.reset_mock()
        self.service.send_periodic_heartbeat()
        self.assertTrue(self.service.client.send_heartbeat.called)
        hb_args = self.service.client.send_heartbeat.call_args[1]
        self.assertEqual(hb_args["status"], "unknown")
        self.assertEqual(hb_args["error"], "Camera socket timeout")

        # 3. Successful capture clears the error
        fake_frame = b"\xff\xd8FRAME\xff\xd9"
        self.service.camera.capture_snapshot_with_retry.side_effect = None
        self.service.camera.capture_snapshot_with_retry.return_value = (
            fake_frame,
            {"itrack": 0},
        )
        self.service.capture_and_send_snapshot()
        self.assertIsNone(self.service._last_error)

        # 4. Subsequent periodic heartbeat is clean
        self.service.client.send_heartbeat.reset_mock()
        self.service.send_periodic_heartbeat()
        hb_args = self.service.client.send_heartbeat.call_args[1]
        self.assertEqual(hb_args["status"], "unknown")
        self.assertIsNone(hb_args["error"])

    def test_config_incomplete_mode_does_not_probe_dummy_host(self):
        # Unconfigured printer (empty host / access code)
        incomplete_cfg = BridgeConfig(
            printer_host="",
            printer_access_code="",
            printer_id="a1-01",
            device_id="termux-01",
            server_url="http://127.0.0.1:8000",
            device_token="dev-token",
        )
        service = BridgeService(incomplete_cfg)
        service.client = MagicMock()

        # Camera client must not be initialized with dummy host
        self.assertIsNone(service.camera)
        self.assertEqual(service._last_error, "printer not configured")

        # Periodic heartbeat sends status unknown with safe error "printer not configured"
        service.send_periodic_heartbeat()
        hb_args = service.client.send_heartbeat.call_args[1]
        self.assertEqual(hb_args["status"], "unknown")
        self.assertEqual(hb_args["error"], "printer not configured")

        # run_once executes heartbeat and exits cleanly (0) without trying camera
        code = service.run_once()
        self.assertEqual(code, 0)

        # capture_and_send_snapshot preserves validation before camera use
        success = service.capture_and_send_snapshot()
        self.assertFalse(success)
        self.assertIsNone(service.camera)

    def test_dummy_template_host_detected_as_incomplete(self):
        # Dummy host from template
        template_cfg = BridgeConfig(
            printer_host="192.168.1.50",
            printer_access_code="REPLACE_WITH_8_DIGIT_LAN_ACCESS_CODE",
            printer_id="a1-01",
            device_id="termux-01",
            server_url="http://127.0.0.1:8000",
            device_token="dev-token",
        )
        service = BridgeService(template_cfg)
        self.assertFalse(service.config.is_printer_configured())
        self.assertIsNone(service.camera)
        self.assertEqual(service._last_error, "printer not configured")

    def test_capture_fatal_fingerprint_mismatch_raises(self):
        self.service.camera.capture_snapshot_with_retry.side_effect = (
            FingerprintMismatchError("Fingerprint invalid")
        )

        with self.assertRaises(FingerprintMismatchError):
            self.service.capture_and_send_snapshot()

    def test_run_once_success(self):
        fake_frame = b"\xff\xd8FRAME\xff\xd9"
        self.service.camera.capture_snapshot_with_retry.return_value = (
            fake_frame,
            {"itrack": 0},
        )

        code = self.service.run_once()
        self.assertEqual(code, 0)

    def test_run_once_failure(self):
        self.service.camera.capture_snapshot_with_retry.side_effect = NetworkError("Fail")
        code = self.service.run_once()
        self.assertEqual(code, 1)

    def test_stop_terminates_loop(self):
        self.service._running = True
        self.service.stop()
        self.assertFalse(self.service._running)

    @patch("services.printpilot.bridge.main.load_config")
    @patch("services.printpilot.bridge.main.BridgeService")
    def test_cli_main_once(self, mock_service_cls, mock_load_cfg):
        mock_load_cfg.return_value = self.config
        mock_inst = MagicMock()
        mock_inst.run_once.return_value = 0
        mock_service_cls.return_value = mock_inst

        code = main(["--config", "dummy.json", "--once"])
        self.assertEqual(code, 0)
        self.assertTrue(mock_inst.run_once.called)

    @patch("services.printpilot.bridge.main.load_config")
    @patch("services.printpilot.bridge.main.BridgeService")
    def test_cli_main_heartbeat_only(self, mock_service_cls, mock_load_cfg):
        mock_load_cfg.return_value = self.config
        mock_inst = MagicMock()
        mock_inst.run.return_value = 0
        mock_service_cls.return_value = mock_inst

        code = main(["--config", "dummy.json", "--heartbeat-only"])
        self.assertEqual(code, 0)
        mock_service_cls.assert_called_with(self.config, heartbeat_only=True)


if __name__ == "__main__":
    unittest.main()
