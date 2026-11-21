"""Unit tests for BridgeConfig and configuration loading."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.printpilot.bridge.config import BridgeConfig, load_config
from services.printpilot.bridge.exceptions import ConfigError


class TestConfig(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "test_bridge.json"

        self.valid_data = {
            "printer_host": "192.168.1.100",
            "printer_port": 6000,
            "printer_access_code": "12345678",
            "printer_id": "bambu-a1",
            "device_id": "redmi-01",
            "server_url": "http://127.0.0.1:8000",
            "device_token": "secret-token-xyz",
            "cert_fingerprint": "aa:bb:cc:dd",
            "snapshot_interval_seconds": 120,
            "heartbeat_interval_seconds": 30,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.valid_data, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_from_valid_file(self):
        cfg = load_config(str(self.config_path))
        self.assertEqual(cfg.printer_host, "192.168.1.100")
        self.assertEqual(cfg.printer_access_code, "12345678")
        self.assertEqual(cfg.printer_port, 6000)
        self.assertEqual(cfg.printer_id, "bambu-a1")
        self.assertEqual(cfg.device_id, "redmi-01")
        self.assertEqual(cfg.server_url, "http://127.0.0.1:8000")
        self.assertEqual(cfg.device_token, "secret-token-xyz")
        self.assertEqual(cfg.cert_fingerprint, "aa:bb:cc:dd")
        self.assertEqual(cfg.snapshot_interval_seconds, 120)
        self.assertEqual(cfg.heartbeat_interval_seconds, 30)
        self.assertTrue(cfg.is_printer_configured())
        cfg.printer_host = "192.168.1.50"
        self.assertTrue(cfg.is_printer_configured())

    def test_secrets_redacted_in_repr_and_dict(self):
        cfg = load_config(str(self.config_path))
        d = cfg.to_dict(redact=True)
        self.assertEqual(d["printer_access_code"], "********")
        self.assertEqual(d["device_token"], "********")

        r = repr(cfg)
        self.assertNotIn("12345678", r)
        self.assertNotIn("secret-token-xyz", r)
        self.assertIn("********", r)

    def test_missing_server_credentials_raises(self):
        incomplete_path = Path(self.temp_dir.name) / "incomplete.json"
        with open(incomplete_path, "w", encoding="utf-8") as f:
            json.dump({"printer_host": "192.168.1.100"}, f)

        # server_url and device_token are missing
        with self.assertRaises(ConfigError):
            load_config(str(incomplete_path))

    def test_config_incomplete_mode_allows_missing_printer(self):
        incomplete_printer_path = Path(self.temp_dir.name) / "waiting_printer.json"
        with open(incomplete_printer_path, "w", encoding="utf-8") as f:
            json.dump({
                "server_url": "http://127.0.0.1:8000",
                "device_token": "my-device-token",
                "device_id": "test-device",
                "printer_host": "",
                "printer_access_code": "",
            }, f)

        # allow_incomplete=True (default) allows loading while waiting for printer
        cfg = load_config(str(incomplete_printer_path), allow_incomplete=True)
        self.assertFalse(cfg.is_printer_configured())

        # allow_incomplete=False raises ConfigError
        with self.assertRaises(ConfigError):
            load_config(str(incomplete_printer_path), allow_incomplete=False)

    def test_dummy_hosts_and_placeholders(self):
        cfg = BridgeConfig(
            printer_host="192.168.1.50",
            printer_access_code="REPLACE_WITH_8_DIGIT_LAN_ACCESS_CODE",
            device_token="token",
        )
        self.assertFalse(cfg.is_printer_configured())
        with self.assertRaises(ConfigError):
            cfg.validate_printer()

        cfg.printer_host = "0.0.0.0"
        self.assertFalse(cfg.is_printer_configured())

        cfg.printer_host = "192.168.1.120"
        cfg.printer_access_code = "12345678"
        self.assertTrue(cfg.is_printer_configured())

    def test_environment_variable_overrides(self):
        env_vars = {
            "PRINTPILOT_PRINTER_HOST": "10.0.0.99",
            "PRINTPILOT_PRINTER_ACCESS_CODE": "87654321",
            "PRINTPILOT_SNAPSHOT_INTERVAL": "60",
            "PRINTPILOT_INSECURE_SKIP_VERIFY": "true",
        }
        with patch.dict(os.environ, env_vars):
            cfg = load_config(str(self.config_path))
            self.assertEqual(cfg.printer_host, "10.0.0.99")
            self.assertEqual(cfg.printer_access_code, "87654321")
            self.assertEqual(cfg.snapshot_interval_seconds, 60)
            self.assertTrue(cfg.insecure_skip_verify)

    def test_invalid_file_path_raises(self):
        with self.assertRaises(ConfigError):
            load_config("/path/to/nonexistent/file.json")


if __name__ == "__main__":
    unittest.main()
