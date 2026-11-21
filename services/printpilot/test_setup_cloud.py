import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from setup_cloud import CloudClient, SetupError, choose_device, write_config


class FakeResponse:
    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.data[:size]


class FakeOpener:
    def __init__(self, results):
        self.results = iter(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class SetupCloudTests(unittest.TestCase):
    def test_email_login_and_device_listing_use_fake_cloud(self):
        opener = FakeOpener([
            {"success": True},
            {"accessToken": "cloud-secret"},
            {"devices": [{"dev_id": "01ABC", "name": "A1", "dev_access_code": "printer-secret"}]},
        ])
        client = CloudClient(opener=opener)
        token = client.email_login("user@example.com", lambda prompt: "123456")
        devices = client.devices(token)
        self.assertEqual(devices[0]["dev_id"], "01ABC")
        self.assertEqual(opener.requests[0].full_url, "https://api.bambulab.com/v1/user-service/user/sendemail/code")
        self.assertEqual(opener.requests[0].data, b'{"email": "user@example.com", "type": "codeLogin"}')
        self.assertEqual(opener.requests[1].data, b'{"account": "user@example.com", "code": "123456"}')
        self.assertEqual(opener.requests[2].get_header("Authorization"), "Bearer cloud-secret")

    def test_unsupported_login_and_redirect_fail_without_following(self):
        client = CloudClient(opener=FakeOpener([{"success": True}, {"loginType": "tfa"}]))
        with self.assertRaisesRegex(SetupError, "additional verification"):
            client.email_login("user@example.com", lambda prompt: "123456")
        redirect = HTTPError("https://api.bambulab.com/bind", 302, "redirect", {}, None)
        client = CloudClient(opener=FakeOpener([redirect]))
        with self.assertRaisesRegex(SetupError, "refusing to forward"):
            client.devices("cloud-secret")

    def test_device_selection_does_not_print_access_code(self):
        import io
        output = io.StringIO()
        device = {"name": "A1", "dev_id": "01ABC", "dev_access_code": "printer-secret"}
        self.assertIs(choose_device([device], lambda prompt: "1", output), device)
        self.assertNotIn("printer-secret", output.getvalue())

    def test_config_update_preserves_unrelated_secrets_and_sets_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "bridge.json"
            original = {"device_token": "keep-secret", "server_url": "http://private",
                        "printer_port": 6000, "printer_host": "old"}
            config_path.write_text(json.dumps(original))
            os.chmod(config_path, 0o644)
            device = {"dev_id": "01ABC", "dev_access_code": "printer-secret"}
            write_config(config_path, "192.0.2.4", device)
            updated = json.loads(config_path.read_text())
            self.assertEqual(updated["printer_host"], "192.0.2.4")
            self.assertEqual(updated["printer_id"], "01ABC")
            self.assertEqual(updated["printer_access_code"], "printer-secret")
            self.assertEqual(updated["device_token"], "keep-secret")
            self.assertEqual(updated["server_url"], "http://private")
            self.assertEqual(updated["printer_port"], 6000)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_bad_config_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "bridge.json"
            config_path.write_text("[]")
            before = config_path.read_bytes()
            with self.assertRaises(SetupError):
                write_config(config_path, "192.0.2.4", {"dev_id": "x", "dev_access_code": "y"})
            self.assertEqual(config_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
