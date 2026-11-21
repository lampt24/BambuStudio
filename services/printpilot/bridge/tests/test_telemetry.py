"""Tests for read-only MQTT telemetry and multi-printer configuration."""

import json
import struct
import unittest

from services.printpilot.bridge import telemetry as tm
from services.printpilot.bridge.config import BridgeConfig
from services.printpilot.bridge.exceptions import ConfigError


class FakeSock:
    def __init__(self, data: bytes):
        self.data = bytearray(data)

    def recv(self, n):
        chunk = bytes(self.data[:n])
        del self.data[:n]
        return chunk


def publish_packet(topic: str, payload: dict) -> bytes:
    return tm.build_publish(topic, json.dumps(payload).encode())


class TestPackets(unittest.TestCase):
    def test_connect_contains_credentials_and_flags(self):
        pkt = tm.build_connect("cid", "bblp", "code123")
        self.assertEqual(pkt[0], 0x10)
        self.assertIn(b"MQTT", pkt)
        self.assertIn(b"bblp", pkt)
        self.assertIn(b"code123", pkt)

    def test_remaining_length_multibyte(self):
        self.assertEqual(tm._remaining_length(127), b"\x7f")
        self.assertEqual(tm._remaining_length(128), b"\x80\x01")
        self.assertEqual(tm._remaining_length(16383), b"\xff\x7f")

    def test_read_and_parse_publish_roundtrip(self):
        payload = {"print": {"mc_percent": 50}}
        big = {"print": {"pad": "x" * 500, "mc_percent": 51}}
        sock = FakeSock(publish_packet("device/SN/report", payload) + publish_packet("device/SN/report", big))
        first, body = tm.read_packet(sock)
        topic, data = tm.parse_publish(first, body)
        self.assertEqual(topic, "device/SN/report")
        self.assertEqual(json.loads(data), payload)
        first, body = tm.read_packet(sock)
        self.assertEqual(json.loads(tm.parse_publish(first, body)[1])["print"]["mc_percent"], 51)

    def test_parse_publish_qos1_skips_packet_id(self):
        body = struct.pack(">H", 1) + b"t" + struct.pack(">H", 7) + b"{}"
        self.assertEqual(tm.parse_publish(0x32, body), ("t", b"{}"))

    def test_closed_socket_raises(self):
        with self.assertRaises(tm.TelemetryError):
            tm.read_packet(FakeSock(b""))

    def test_oversized_packet_rejected(self):
        header = b"\x30" + tm._remaining_length(tm.MAX_PACKET + 1)
        with self.assertRaises(tm.TelemetryError):
            tm.read_packet(FakeSock(header))

    def test_subscribe_only_report_topic(self):
        pkt = tm.build_subscribe(1, "device/SN/report")
        self.assertEqual(pkt[0], 0x82)
        self.assertIn(b"device/SN/report", pkt)


class TestNormalize(unittest.TestCase):
    REPORT = {
        "gcode_state": "RUNNING", "nozzle_temper": 244.96875, "bed_temper": 79.96875,
        "mc_percent": 97, "layer_num": 573, "total_layer_num": 595,
        "cooling_fan_speed": "15", "mc_remaining_time": 6, "subtask_name": "vase",
        "print_error": 0,
    }

    def test_normalize_fields(self):
        t = tm.normalize(self.REPORT)
        self.assertEqual(t["nozzle_temp"], 245.0)
        self.assertEqual(t["bed_temp"], 80.0)
        self.assertEqual((t["progress"], t["current_layer"], t["total_layers"]), (97, 573, 595))
        self.assertEqual(t["fan_speed"], 100)
        self.assertEqual(t["remaining_min"], 6)
        self.assertEqual(t["job_name"], "vase")

    def test_status_mapping(self):
        self.assertEqual(tm.status_from({"gcode_state": "RUNNING"}), "printing")
        self.assertEqual(tm.status_from({"gcode_state": "PAUSE"}), "paused")
        self.assertEqual(tm.status_from({"gcode_state": "FINISH"}), "idle")
        self.assertEqual(tm.status_from({"gcode_state": "RUNNING", "print_error": 50348044}), "error")
        self.assertEqual(tm.status_from({}), "unknown")

    def test_bad_values_ignored(self):
        t = tm.normalize({"nozzle_temper": "abc", "mc_percent": None})
        self.assertNotIn("nozzle_temp", t)
        self.assertNotIn("progress", t)

    def test_merge_keeps_partial_updates(self):
        client = tm.TelemetryClient("192.0.2.1", "code")
        client.merge(json.dumps({"print": self.REPORT}).encode())
        client.merge(json.dumps({"print": {"mc_percent": 98}}).encode())
        t = client.snapshot()
        self.assertEqual(t["progress"], 98)
        self.assertEqual(t["current_layer"], 573)

    def test_snapshot_empty_when_stale_or_none(self):
        client = tm.TelemetryClient("192.0.2.1", "code")
        self.assertEqual(client.snapshot(), {})
        client.merge(json.dumps({"print": self.REPORT}).encode())
        self.assertEqual(client.snapshot(max_age=-1), {})

    def test_merge_ignores_garbage(self):
        client = tm.TelemetryClient("192.0.2.1", "code")
        client.merge(b"not json")
        client.merge(b'{"info": {}}')
        self.assertEqual(client.snapshot(), {})


class TestMultiPrinterConfig(unittest.TestCase):
    def base(self, printers):
        return BridgeConfig(device_token="t", server_url="http://s", printers=printers)

    def test_single_printer_when_list_empty(self):
        cfg = BridgeConfig(device_token="t", printer_id="a1-01")
        self.assertEqual([c.printer_id for c in cfg.printer_configs()], ["a1-01"])

    def test_expands_printers_with_short_keys(self):
        cfg = self.base([
            {"id": "a1-01", "host": "192.168.1.10", "access_code": "c1", "model": "A1"},
            {"printer_id": "p1s-01", "printer_host": "192.168.1.11", "printer_access_code": "c2",
             "serial": "01P00A", "telemetry_enabled": "false"},
        ])
        a, b = cfg.printer_configs()
        self.assertEqual((a.printer_id, a.printer_host, a.printer_model), ("a1-01", "192.168.1.10", "A1"))
        self.assertEqual((b.printer_serial, b.telemetry_enabled), ("01P00A", False))
        self.assertEqual(a.server_url, "http://s")
        self.assertEqual(a.printers, [])

    def test_rejects_duplicate_and_unknown(self):
        with self.assertRaises(ConfigError):
            self.base([{"id": "x", "host": "h"}, {"id": "x", "host": "h2"}]).printer_configs()
        with self.assertRaises(ConfigError):
            self.base([{"id": "x", "hots": "typo"}]).printer_configs()

    def test_repr_redacts_access_codes(self):
        cfg = self.base([{"id": "x", "host": "h", "access_code": "supersecret"}])
        self.assertNotIn("supersecret", repr(cfg))


if __name__ == "__main__":
    unittest.main()
