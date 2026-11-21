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


class TestCapturePolicy(unittest.TestCase):
    def service(self):
        from services.printpilot.bridge.main import BridgeService
        cfg = BridgeConfig(device_token="t", server_url="http://s", device_id="d", printer_id="p")
        svc = BridgeService(cfg)  # printer unconfigured -> no camera/telemetry threads

        class Tele:
            state = {}
            def snapshot(self):
                return dict(self.state)
        svc.telemetry = Tele()
        return svc

    def test_idle_printer_skips_capture(self):
        svc = self.service()
        svc.telemetry.state = {"gcode_state": "IDLE"}
        self.assertFalse(svc.should_capture())

    def test_active_then_one_final_capture(self):
        svc = self.service()
        svc.telemetry.state = {"gcode_state": "RUNNING"}
        self.assertTrue(svc.should_capture())
        svc.telemetry.state = {"gcode_state": "FINISH"}
        self.assertTrue(svc.should_capture())   # finished print photo
        self.assertFalse(svc.should_capture())  # then stop

    def test_unknown_state_still_captures(self):
        svc = self.service()
        svc.telemetry.state = {}
        self.assertTrue(svc.should_capture())

    def test_server_policy_applied_and_validated(self):
        svc = self.service()
        svc.telemetry.state = {"gcode_state": "IDLE"}
        svc._apply_policy({"policy": {"capture_when_idle": True, "snapshot_interval_seconds": 300}})
        self.assertTrue(svc.should_capture())
        self.assertEqual(svc.snapshot_interval(), 300)
        svc._apply_policy({"policy": {"snapshot_interval_seconds": 5, "capture_when_idle": "yes"}})
        self.assertEqual(svc.snapshot_interval(), 300)
        svc._apply_policy({"status": "ok"})
        self.assertTrue(svc._policy["capture_when_idle"])


class TestCommands(unittest.TestCase):
    def test_payloads(self):
        self.assertEqual(tm.command_payload("pause", {}, "7"), {"print": {"sequence_id": "7", "command": "pause", "param": ""}})
        self.assertEqual(tm.command_payload("speed", {"level": 3}, "7")["print"]["param"], "3")
        light = tm.command_payload("light", {"mode": "on"}, "7")["system"]
        self.assertEqual((light["command"], light["led_node"], light["led_mode"]), ("ledctrl", "chamber_light", "on"))
        for bad in (("light", {"mode": "x"}), ("speed", {"level": 9}), ("gcode_line", {})):
            with self.assertRaises(ValueError):
                tm.command_payload(bad[0], bad[1], "7")

    def test_interpret_reply(self):
        self.assertEqual(tm.interpret_reply({"result": "success", "reason": "success"}), (True, "success"))
        ok, reason = tm.interpret_reply({"command": "pause", "err_code": 84033543})
        self.assertFalse(ok)
        self.assertIn("84033543", reason)
        self.assertFalse(tm.interpret_reply({"result": "fail", "reason": "busy"})[0])

    def test_send_command_matches_reply_by_sequence(self):
        import threading

        class Sock:
            def __init__(self):
                self.sent = []
            def sendall(self, data):
                self.sent.append(data)

        client = tm.TelemetryClient("192.0.2.1", "code", serial="SN")
        client._sock = Sock()

        def reply_later():
            for _ in range(100):
                if client._waiters:
                    seq = next(iter(client._waiters))
                    client.merge(json.dumps({"system": {"sequence_id": "other", "result": "fail"}}).encode())
                    client.merge(json.dumps({"system": {"sequence_id": seq, "result": "success"}}).encode())
                    return
                time.sleep(0.01)
        import time
        threading.Thread(target=reply_later).start()
        self.assertEqual(client.send_command("light", {"mode": "off"}, timeout=2), (True, "success"))
        first, body = tm.read_packet(FakeSock(client._sock.sent[0]))
        topic, payload = tm.parse_publish(first, body)
        self.assertEqual(topic, "device/SN/request")
        self.assertEqual(json.loads(payload)["system"]["led_mode"], "off")
        self.assertEqual(client._waiters, {})

    def test_send_command_without_connection_or_reply(self):
        client = tm.TelemetryClient("192.0.2.1", "code", serial="SN")
        self.assertFalse(client.send_command("pause", {})[0])

        class Sock:
            def sendall(self, data):
                pass
        client._sock = Sock()
        ok, reason = client.send_command("pause", {}, timeout=0.05)
        self.assertFalse(ok)
        self.assertIn("không phản hồi", reason)

    def test_light_and_speed_in_telemetry(self):
        t = tm.normalize({"lights_report": [{"node": "chamber_light", "mode": "on"}], "spd_lvl": 2})
        self.assertEqual((t["light"], t["speed_level"]), ("on", 2))

    def test_poller_executes_and_reports(self):
        from services.printpilot.bridge.main import CommandPoller

        class Svc:
            def __init__(self):
                self.config = BridgeConfig(device_token="t", printer_id="a1")
                self.telemetry = self
                self.calls = []
            def send_command(self, command, params):
                self.calls.append((command, params))
                return True, "success"

        class Client:
            results = []
            def poll_commands_full(self, device_id):
                return {"live": [], "commands": [
                    {"id": "c1", "printer_id": "a1", "command": "light", "params": {"mode": "on"}},
                    {"id": "c2", "printer_id": "ghost", "command": "pause", "params": {}}]}
            def post_command_result(self, cid, ok, reason):
                self.results.append((cid, ok))

        svc = Svc()
        poller = CommandPoller(BridgeConfig(device_token="t", server_url="http://s", device_id="redmi"), [svc])
        poller.client = Client()
        self.assertEqual(poller.run_once(), 2)
        self.assertEqual(svc.calls, [("light", {"mode": "on"})])
        self.assertEqual(Client.results, [("c1", True), ("c2", False)])

    def test_poller_publishes_new_state_before_result(self):
        from services.printpilot.bridge.main import CommandPoller
        order = []

        class Svc:
            def __init__(self):
                self.config = BridgeConfig(device_token="t", printer_id="a1")
                self.telemetry = self
            def send_command(self, command, params):
                order.append("command")
                return True, "success"
            def wait_for(self, key, value):
                order.append(f"wait {key}={value}")
                return True
            def send_periodic_heartbeat(self):
                order.append("heartbeat")

        class Client:
            def poll_commands_full(self, device_id):
                return {"commands": [{"id": "c1", "printer_id": "a1", "command": "light", "params": {"mode": "off"}}]}
            def post_command_result(self, cid, ok, reason):
                order.append("result")

        poller = CommandPoller(BridgeConfig(device_token="t", server_url="http://s", device_id="redmi"), [Svc()])
        poller.client = Client()
        poller.run_once()
        self.assertEqual(order, ["command", "wait light=off", "heartbeat", "result"])

    def test_refresh_waits_for_full_report(self):
        import threading
        sent = []

        class Sock:
            def sendall(self, data):
                sent.append(data)

        client = tm.TelemetryClient("192.0.2.1", "code", serial="SN")
        self.assertFalse(client.refresh(timeout=0.05))  # not connected
        client._sock = Sock()
        self.assertFalse(client.refresh(timeout=0.05))  # no reply
        def push():
            time.sleep(0.05)
            client.merge(json.dumps({"print": {"spd_lvl": 2}}).encode())   # delta only: not enough
            client.merge(json.dumps({"print": {"gcode_state": "IDLE", "lights_report": [{"node": "chamber_light", "mode": "off"}]}}).encode())
        import time
        threading.Thread(target=push).start()
        self.assertTrue(client.refresh(timeout=2))
        self.assertEqual(client.snapshot()["light"], "off")
        self.assertIn(b"pushall", sent[-1])

    def test_wait_for_retries_until_state_changes(self):
        import threading, time
        client = tm.TelemetryClient("192.0.2.1", "code", serial="SN")
        pushes = []

        class Sock:
            # The first pushall is answered with the stale state, the second with the new one
            def sendall(self, data):
                pushes.append(data)
                mode = "off" if len(pushes) == 1 else "on"
                threading.Timer(0.02, client.merge, args=(json.dumps({"print": {
                    "gcode_state": "IDLE", "lights_report": [{"node": "chamber_light", "mode": mode}]}}).encode(),)).start()
        client._sock = Sock()
        self.assertTrue(client.wait_for("light", "on", timeout=3))
        self.assertGreaterEqual(len(pushes), 2)
        self.assertFalse(client.wait_for("light", "flashing", timeout=0.3))


class TestLiveStreamer(unittest.TestCase):
    def test_streams_until_server_stops_accepting(self):
        import threading, time
        from unittest import mock
        from services.printpilot.bridge import main as bridge_main

        frames = [b"\xff\xd8" + bytes([i]) * 20 + b"\xff\xd9" for i in range(1, 6)]
        feed = iter(frames)

        class Svc:
            def __init__(self):
                self.config = BridgeConfig(device_token="t", printer_id="a1")
                self.camera = mock.Mock()
                self.camera._connect_and_authenticate.return_value = mock.Mock()

        class Client:
            def __init__(self):
                self.got = []
            def send_live_frame(self, pid, frame):
                self.got.append(frame)
                return len(self.got) < 3   # server stops wanting after 3 frames

        svc, client = Svc(), Client()
        with mock.patch.object(bridge_main, "read_frame", side_effect=lambda *a, **k: (next(feed), {})):
            streamer = bridge_main.LiveStreamer(svc, client)
            streamer.extend()
            for _ in range(100):
                if not streamer._thread.is_alive():
                    break
                time.sleep(0.01)
        self.assertEqual(client.got, frames[:3])
        self.assertFalse(streamer.active())
        self.assertIsNone(streamer.recent_frame())
        svc.camera._connect_and_authenticate.return_value.close.assert_called()

    def test_poller_starts_live_for_listed_printers(self):
        from unittest import mock
        from services.printpilot.bridge.main import CommandPoller

        class Svc:
            def __init__(self, pid):
                self.config = BridgeConfig(device_token="t", printer_id=pid)
                self.camera = object()
                self.telemetry = None
                self.live = None

        a, b = Svc("a1"), Svc("b2")
        poller = CommandPoller(BridgeConfig(device_token="t", server_url="http://s", device_id="redmi"), [a, b])
        poller.client = mock.Mock()
        poller.client.poll_commands_full.return_value = {"commands": [], "live": ["a1", "ghost"]}
        with mock.patch("services.printpilot.bridge.main.LiveStreamer") as LS:
            poller.run_once()
        LS.assert_called_once_with(a, poller.client)
        LS.return_value.extend.assert_called_once()
        self.assertIsNone(b.live)
