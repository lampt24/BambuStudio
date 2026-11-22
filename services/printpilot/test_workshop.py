"""Auto-link, spools, maintenance, room sensor, calibration, vision failure/review, orientation, slice checks."""
import base64
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import evolution
import filaments
import insight
import server
import workshop
from server import clean_slice_stats, open_db
from test_insight import CONFIG, COLUMN, CUBE, T_SHAPE, obj, project
from test_server import DEVICE_TOKEN, ServerTestCase, _jreq, _req

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATS = {"time_s": 3600, "layer_times_s": [20, 9, 3, 2.5, 2.8, 12], "filament_changes": 0, "flush_changes": 0,
         "filaments": [{"index": 1, "volume_mm3": 16000, "model_mm3": 15000, "support_mm3": 1000, "tower_mm3": 0,
                        "flush_mm3": 0, "weight_g": 20.0}], "role_times_s": {"Outer wall": 600}}


def fresh_db():
    return open_db(os.path.join(tempfile.mkdtemp(prefix="pp_ws_"), "printpilot.db"))


class Orientation(unittest.TestCase):
    def test_rotation_maps_direction_down(self):
        for d in workshop_dirs():
            a, b = (math.radians(x) for x in insight._rotation_for(d))
            x, y, z = insight._norm(d)
            y, z = y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)
            x, z = x * math.cos(b) + z * math.sin(b), -x * math.sin(b) + z * math.cos(b)
            self.assertAlmostEqual(x, 0, places=6)
            self.assertAlmostEqual(y, 0, places=6)
            self.assertAlmostEqual(z, -1, places=6)

    def test_suggestions(self):
        t = insight.suggest_orientation(T_SHAPE, True)
        self.assertEqual((t["down_face"], t["suggested"]["overhang_60_mm2"], t["suggested"]["bed_contact_mm2"]), ("trên (+Z)", 0, 400))
        column = insight.suggest_orientation(COLUMN, True)
        self.assertLess(column["suggested"]["slenderness"], 2)
        self.assertIsNone(insight.suggest_orientation(CUBE, True))
        self.assertIsNone(insight.suggest_orientation(obj((0, 0, 0, 20, 20, 2), (8, 8, 2, 12, 12, 12)), True))
        finding = insight.orientation_findings([t])[0]
        self.assertEqual((finding["code"], finding["severity"]), ("orientation", "warn"))
        self.assertIn("Lay on face", finding["message"])


def workshop_dirs():
    return insight.DOWN_DIRECTIONS


class SliceAndContext(unittest.TestCase):
    def test_slice_findings(self):
        stats = dict(STATS, filaments=[{"index": 1, "volume_mm3": 30000, "model_mm3": 10000, "support_mm3": 4000,
                                        "tower_mm3": 6000, "flush_mm3": 10000, "weight_g": 37}], filament_changes=40)
        found = {f["code"]: f for f in insight.slice_findings({"slice_stats": stats, "settings": {"slow_down_layer_time": "8"}})}
        self.assertEqual(found["fast_layers"]["evidence"]["fast_layers"], 3)
        self.assertGreater(found["purge_waste"]["evidence"]["waste_g"], 15)
        self.assertIn("support_heavy", found)
        self.assertEqual(insight.slice_findings({"slice_stats": None}), [])
        summary = insight.slice_summary(STATS)
        self.assertEqual((summary["time_h"], summary["layers"], summary["min_layer_s"]), (1.0, 6, 2.5))

    def test_context_findings(self):
        spool = workshop.describe_spool({"name": "TINMORRY PETG ECO đen", "material": "PETG", "remaining_g": 12,
                                         "dried_at": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))})
        self.assertTrue(spool["needs_drying"])
        ctx = {"material": "PETG", "slice_stats": STATS, "spools": [spool],
               "project_filaments": [{"index": 1, "material": "PETG"}], "printer": insight.PRINTERS["A1"],
               "environment": {"temperature": 16, "humidity": 70},
               "maintenance": [{"task": "clean_bed", "due": True, "ratio": 1.2, "hours": 30, "interval_hours": 25}]}
        codes = {f["code"] for f in insight.context_findings(ctx, [insight.object_metrics(CUBE)])}
        self.assertTrue({"spool_short", "spool_needs_drying", "room_humid", "room_cold", "maintenance_bed"} <= codes)
        hot = insight.context_findings({"material": "PLA", "printer": insight.PRINTERS["P1S"],
                                        "environment": {"temperature": 33, "humidity": 40}}, [])
        self.assertEqual([f["code"] for f in hot], ["room_hot"])

    def test_clean_slice_stats(self):
        dirty = {"time_s": 10, "layer_times_s": [1, "x", -2, float("nan"), 3], "filaments": [{"index": 1, "weight_g": 5},
                 {"index": 99}, "bad"], "role_times_s": {"a" * 100: 5}, "filament_changes": True}
        clean = clean_slice_stats(dirty)
        self.assertEqual(clean["layer_times_s"], [1, 3])
        self.assertEqual([f["index"] for f in clean["filaments"]], [1])
        self.assertEqual(clean["filament_changes"], 0)
        self.assertEqual(len(next(iter(clean["role_times_s"]))), 40)
        self.assertIsNone(clean_slice_stats("x"))
        self.assertIsNone(clean_slice_stats({}))


class Spools(unittest.TestCase):
    def test_crud_and_validation(self):
        db = fresh_db()
        a = workshop.save_spool(db, {"name": "PETG đen", "material": "PETG", "total_g": 1000, "printer_id": "p1", "ams_id": 0, "slot_id": 1})
        self.assertEqual(a["remaining_g"], 1000)
        b = workshop.save_spool(db, {"name": "PETG trắng", "material": "PETG", "total_g": 1000, "printer_id": "p1", "ams_id": 0, "slot_id": 1})
        spools = {s["name"]: s for s in workshop.list_spools(db)}
        self.assertIsNone(spools["PETG đen"]["slot_id"])          # one spool per tray
        self.assertEqual(spools["PETG trắng"]["slot_id"], 1)
        self.assertEqual(workshop.mark_dried(db, a["id"], "2026-11-01")["dried_at"], "2026-11-01")
        for bad in ({"name": ""}, {"name": "x", "total_g": 0}, {"name": "x", "total_g": 10, "remaining_g": 20},
                    {"name": "x", "colour": "red"}, {"name": "x", "printer_id": "p1"}, {"name": "x", "opened_at": "21/11"}):
            with self.assertRaises(ValueError):
                workshop.save_spool(db, bad)
        workshop.archive_spool(db, b["id"])
        self.assertEqual([s["name"] for s in workshop.list_spools(db)], ["PETG đen"])
        with self.assertRaises(LookupError):
            workshop.mark_dried(db, "missing")


class Maintenance(unittest.TestCase):
    def test_hours_intervals_and_abrasive(self):
        db = fresh_db()
        for i, (hours, material) in enumerate([(10, "PLA"), (20, "PETG-CF")]):
            db.execute("INSERT INTO print_runs (id,printer_id,started_at,ended_at,status,duration_seconds,material) VALUES (?,?,?,?,?,?,?)",
                       (f"r{i}", "p1", "2026-11-01T00:00:00Z", "2026-11-01T10:00:00Z", "completed", hours * 3600, material))
        db.commit()
        tasks = {t["task"]: t for t in workshop.maintenance_status(db, "p1")}
        self.assertEqual(tasks["clean_bed"]["hours"], 30)
        self.assertTrue(tasks["clean_bed"]["due"])
        self.assertEqual(tasks["nozzle"]["hours"], 70)            # CF hours count triple
        done = workshop.maintenance_done(db, "p1", "clean_bed", "rửa xà phòng")
        self.assertEqual((done["hours"], done["due"]), (0, False))
        workshop.set_interval(db, "p1", "nozzle", 50)
        self.assertTrue(next(t for t in workshop.maintenance_status(db, "p1") if t["task"] == "nozzle")["due"])
        for bad in (0, 10000, True, "5"):
            with self.assertRaises(ValueError):
                workshop.set_interval(db, "p1", "nozzle", bad)
        with self.assertRaises(ValueError):
            workshop.maintenance_done(db, "p1", "nope")


class Environment(unittest.TestCase):
    def test_readings(self):
        db = fresh_db()
        workshop.record_reading(db, {"sensor_id": "room-1", "temperature": 24.5, "humidity": 65})
        workshop.record_reading(db, {"sensor_id": "a1-box", "printer_id": "p1", "temperature": 28, "humidity": 50})
        self.assertEqual(workshop.latest_environment(db, "p1")["sensor_id"], "a1-box")
        self.assertEqual(workshop.latest_environment(db, "p2")["sensor_id"], "room-1")
        self.assertEqual(len(workshop.environment_overview(db)["sensors"]), 2)
        for bad in ({"sensor_id": "x y", "temperature": 1}, {"sensor_id": "x"}, {"sensor_id": "x", "humidity": 120},
                    {"sensor_id": "x", "temperature": float("nan")}, {"sensor_id": "x", "temperature": True}):
            with self.assertRaises(ValueError):
                workshop.record_reading(db, bad)


class Calibration(unittest.TestCase):
    def test_guide_and_save(self):
        db = fresh_db()
        profile = insight.material_profile(ROOT, "TINMORRY PETG ECO @BBL A1")
        ams = {"units": [{"id": 0, "name": "AMS 1", "slots": [{"id": 0, "present": True, "material": "PETG", "flow_k": 0.024, "cali_idx": 3}]}]}
        guide = workshop.calibration_guide(profile, ams, "PETG")
        steps = {s["key"]: s for s in guide["steps"]}
        self.assertEqual((steps["nozzle_temperature"]["suggest"]["from"], steps["nozzle_temperature"]["suggest"]["to"]), (220, 270))
        self.assertEqual(steps["pressure_advance"]["suggest"]["current_on_printer"][0]["k"], 0.024)
        saved = workshop.save_calibration(ROOT, db, {"preset": "TINMORRY PETG ECO @BBL A1", "printer_model": "A1",
                                                     "values": {"nozzle_temperature": 240, "pressure_advance": 0.024, "filament_flow_ratio": 0.97}})
        self.assertEqual(saved["name"], "TINMORRY PETG ECO hiệu chỉnh @BBL A1")
        row = filaments.library_rows(db)[0]
        self.assertEqual((row["body"]["pressure_advance"], row["body"]["enable_pressure_advance"]), (["0.024"], ["1"]))
        # second run updates the same calibrated preset
        workshop.save_calibration(ROOT, db, {"preset": saved["name"], "values": {"nozzle_temperature": 248}})
        rows = filaments.library_rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["body"]["nozzle_temperature"], rows[0]["body"]["pressure_advance"]), (["248"], ["0.024"]))
        # export flattens the PrintPilot-only TINMORRY parent onto its Bambu base for stock Studio
        item = filaments.export_items(ROOT, db, [], [rows[0]["id"]])[0]
        self.assertEqual(item["inherits"], "Generic PETG @BBL A1")
        self.assertEqual((item["nozzle_temperature"], item["filament_max_volumetric_speed"]), (["248"], ["12"]))
        for bad in ({"preset": "", "values": {"nozzle_temperature": 1}}, {"preset": "TINMORRY PETG ECO @BBL A1", "values": {"gcode": 1}},
                    {"preset": "Nope", "values": {"nozzle_temperature": 240}}):
            with self.assertRaises(ValueError):
                workshop.save_calibration(ROOT, db, bad)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM calibration_log").fetchone()[0], 2)


def fake_completion(content):
    return lambda *a, **k: {"choices": [{"message": {"content": json.dumps(content)}}]}


class Vision(unittest.TestCase):
    CFG = {"ai_base_url": "http://ai.invalid", "ai_key": "k"}

    def test_failure_parse_and_decision(self):
        content = {"ok": False, "issues": ["Nhựa rối"], "summary": "Hỏng", "failure": {"kind": "spaghetti", "confidence": 0.95}}
        with patch("server._openai_post", side_effect=fake_completion(content)):
            result = server.ai_vision(self.CFG, "aGk=", {"status": "printing"})
        self.assertEqual(result["failure"], {"kind": "spaghetti", "confidence": 0.95})
        self.assertEqual(workshop.failure_decision(result, None), "spaghetti")
        weak = {"failure": {"kind": "detached", "confidence": 0.7}}
        self.assertIsNone(workshop.failure_decision(weak, None))
        self.assertEqual(workshop.failure_decision(weak, {"failure": {"kind": "detached", "confidence": 0.65}}), "detached")
        self.assertIsNone(workshop.failure_decision({"failure": {"kind": "none", "confidence": 1}}, None))

    def setup_worker(self, action):
        db = fresh_db()
        db.execute("INSERT INTO printers (printer_id, device_id, last_seen, status) VALUES ('p1','d1',?,'printing')", (server._now_iso(),))
        db.commit()
        worker = server.VisionWorker({**self.CFG, "failure_action": action}, ":memory:")
        return db, worker

    def test_pause_and_notify(self):
        row = {"printer_id": "p1", "device_id": "d1"}
        result = {"failure": {"kind": "detached", "confidence": 0.93}}
        for action, expect_cmd in (("pause", 1), ("notify", 0), ("off", 0)):
            db, worker = self.setup_worker(action)
            with patch("server.send_webhook"):
                worker._check_failure(db, row, result, "snap1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM printer_commands WHERE command='pause'").fetchone()[0], expect_cmd)
            alerts = db.execute("SELECT severity, message FROM alerts WHERE source='failure'").fetchall()
            self.assertEqual(len(alerts), 0 if action == "off" else 1)
            if action == "pause":
                self.assertIn("TẠM DỪNG", alerts[0]["message"])
                with patch("server.send_webhook"):
                    worker._check_failure(db, row, result, "snap2")  # open alert: no second pause
                self.assertEqual(db.execute("SELECT COUNT(*) FROM printer_commands").fetchone()[0], 1)

    def test_post_print_review(self):
        db, worker = self.setup_worker("notify")
        ended = server._now_iso()
        db.execute("INSERT INTO print_runs (id,printer_id,started_at,ended_at,status,job_name) VALUES ('r1','p1',?,?,'completed','vase')",
                   (ended, ended))
        db.commit()
        review = {"issues": ["Tơ nhựa", "Không có"], "rating": 3, "summary": "Có tơ nhựa nhẹ", "confidence": 0.8, "visible": True}
        with patch("server._openai_post", side_effect=fake_completion(review)), patch("server.send_webhook"):
            worker._review_finished(db, {"printer_id": "p1", "device_id": "d1", "snap_created": server._now_iso()}, "aGk=")
        stored = json.loads(db.execute("SELECT ai_review FROM print_runs WHERE id='r1'").fetchone()[0])
        self.assertEqual((stored["issues"], stored["rating"]), (["Tơ nhựa"], 3))
        alert = db.execute("SELECT severity, message FROM alerts WHERE source='review'").fetchone()
        self.assertEqual(alert["severity"], "info")
        self.assertIn("Tơ nhựa", alert["message"])
        with patch("server._openai_post") as again:
            worker._review_finished(db, {"printer_id": "p1", "device_id": "d1", "snap_created": server._now_iso()}, "aGk=")
        again.assert_not_called()  # one review per job


class WorkshopAPI(ServerTestCase):
    SERIAL = "03919D000000001"

    def beat(self, status, telemetry, printer="a1-01"):
        code, _ = _jreq(self._port, "POST", "/api/heartbeat", dict(device_id="d", printer_id=printer, status=status,
                                                                     telemetry=telemetry, printer_model="A1"), DEVICE_TOKEN)
        self.assertEqual(code, 200)

    def analyze(self, **extra):
        fake = fake_completion({"summary": "ok", "changes": [], "warnings": []})
        with patch.dict(self._cfg, {"ai_base_url": "http://ai.invalid", "ai_key": "k"}), patch("server._openai_post", side_effect=fake):
            return _jreq(self._port, "POST", "/api/analyze",
                         dict(filename="vase.3mf", project_base64=base64.b64encode(project(CONFIG)).decode(), **extra))

    def test_send_links_next_job_and_consumes_spool(self):
        ams = {"units": [{"id": 0, "name": "AMS 1", "slots": [{"id": 1, "present": True, "material": "PETG", "rfid": False}]}],
               "active_ams_id": 0, "active_slot_id": 1}
        self.beat("idle", {"serial": self.SERIAL, "ams": ams})
        code, spool = _jreq(self._port, "POST", "/api/spools", {"name": "PETG đen", "material": "PETG", "total_g": 1000,
                                                                 "printer_id": "a1-01", "ams_id": 0, "slot_id": 1})
        self.assertEqual(code, 200)
        code, analysis = self.analyze(trigger="send", printer_serial=self.SERIAL, slice_stats=STATS)
        self.assertEqual(code, 200)
        self.assertEqual(analysis["insights"]["slice"]["filaments"][0]["weight_g"], 20)
        self.assertEqual(analysis["insights"]["spools"][0]["name"], "PETG đen")
        self.beat("printing", {"serial": self.SERIAL, "task_id": "1", "subtask_id": "7", "job_name": "vase", "gcode_state": "RUNNING", "ams": ams})
        self.beat("idle", {"serial": self.SERIAL, "gcode_state": "FINISH", "ams": ams})
        run = json.loads(_req(self._port, "GET", "/api/prints").read())["prints"][0]
        self.assertEqual((run["analysis_id"], run["linkage_method"], run["linkage_confidence"]), (analysis["id"], "studio_send", "high"))
        self.assertEqual(run["settings_source"], "studio_send")
        self.assertEqual(run["used_settings"]["layer_height"], "0.2")
        self.assertEqual(run["spool_usage"][0]["grams"], 20)
        spools = json.loads(_req(self._port, "GET", "/api/spools").read())
        self.assertEqual(spools["spools"][0]["remaining_g"], 980)
        self.assertEqual(spools["slots"][0]["label"], "AMS 1 khay 2")
        # A job on another printer is not claimed by this send
        self.analyze(trigger="send", printer_serial=self.SERIAL)
        self.beat("printing", {"task_id": "2", "subtask_id": "8", "job_name": "other", "gcode_state": "RUNNING"}, printer="a1-02")
        other = next(r for r in json.loads(_req(self._port, "GET", "/api/prints").read())["prints"] if r["printer_id"] == "a1-02")
        self.assertIsNone(other["analysis_id"])

    def test_stale_send_not_linked(self):
        self.beat("idle", {"serial": self.SERIAL})
        _, analysis = self.analyze(trigger="send", printer_serial=self.SERIAL)
        db = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        db.execute("UPDATE pending_sends SET created_at='2026-01-01T00:00:00Z'")
        db.commit()
        db.close()
        self.beat("printing", {"serial": self.SERIAL, "task_id": "1", "subtask_id": "7", "gcode_state": "RUNNING"})
        self.assertIsNone(json.loads(_req(self._port, "GET", "/api/prints").read())["prints"][0]["analysis_id"])

    def test_endpoints(self):
        self.beat("idle", {"serial": self.SERIAL})
        self.assertEqual(_jreq(self._port, "POST", "/api/environment", {"sensor_id": "room", "temperature": 22, "humidity": 55}, DEVICE_TOKEN)[0], 200)
        self.assertEqual(_jreq(self._port, "POST", "/api/environment", {"sensor_id": "room", "temperature": 22})[0], 200)  # manager token also allowed
        self.assertEqual(_req(self._port, "POST", "/api/environment", b"{}", token=None).status, 401)
        env = json.loads(_req(self._port, "GET", "/api/environment").read())
        self.assertEqual(env["sensors"][0]["latest"]["temperature"], 22)
        maint = json.loads(_req(self._port, "GET", "/api/maintenance").read())
        self.assertEqual(maint["printers"][0]["printer_id"], "a1-01")
        self.assertEqual(_jreq(self._port, "POST", "/api/maintenance/a1-01/clean_bed/done", {"note": "IPA"})[0], 200)
        self.assertEqual(_jreq(self._port, "POST", "/api/maintenance/a1-01/clean_bed/interval", {"hours": 40})[0], 200)
        self.assertEqual(_jreq(self._port, "POST", "/api/maintenance/nope/clean_bed/done", {})[0], 404)
        self.assertEqual(_jreq(self._port, "POST", "/api/maintenance/a1-01/bogus/done", {})[0], 400)
        guide = json.loads(_req(self._port, "GET", "/api/calibration/guide?preset=TINMORRY%20PETG%20ECO%20%40BBL%20A1&printer_id=a1-01").read())
        self.assertEqual(len(guide["steps"]), 5)
        self.assertEqual(_req(self._port, "GET", "/api/calibration/guide?preset=nope").status, 404)
        code, saved = _jreq(self._port, "POST", "/api/calibration", {"preset": "TINMORRY PETG ECO @BBL A1", "values": {"nozzle_temperature": 240}})
        self.assertEqual((code, saved["preset"]["name"]), (200, "TINMORRY PETG ECO hiệu chỉnh @BBL A1"))
        code, spool = _jreq(self._port, "POST", "/api/spools", {"name": "PLA trắng", "material": "PLA", "total_g": 1000})
        self.assertEqual(_jreq(self._port, "POST", f"/api/spools/{spool['spool']['id']}/dried", {})[0], 200)
        self.assertEqual(_jreq(self._port, "POST", f"/api/spools/{spool['spool']['id']}/archive", {})[0], 200)
        self.assertEqual(_jreq(self._port, "POST", "/api/spools/00000000-0000-0000-0000-000000000000/dried", {})[0], 404)

    def test_maintenance_reminder_alert(self):
        self.beat("idle", {})
        db = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        db.execute("INSERT INTO print_runs (id,printer_id,started_at,ended_at,status,duration_seconds) VALUES "
                   "('r','a1-01','2026-11-01T00:00:00Z','2026-11-02T00:00:00Z','completed',?)", (30 * 3600,))
        db.commit()
        monitor = server.HeartbeatMonitor(self._cfg, os.path.join(self._tmpdir, "printpilot.db"))
        with patch("server.send_webhook"):
            monitor._maintenance_reminders(db)
            monitor._maintenance_reminders(db)
        alerts = db.execute("SELECT message FROM alerts WHERE source='maintenance' AND acknowledged=0").fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertIn("Vệ sinh bàn", alerts[0]["message"])
        db.close()
        _jreq(self._port, "POST", "/api/maintenance/a1-01/clean_bed/done", {})
        db = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts WHERE source='maintenance' AND acknowledged=0").fetchone()[0], 0)
        db.close()

    def test_settings(self):
        code, _ = _jreq(self._port, "POST", "/api/settings", {"failure_action": "pause", "post_print_review": False})
        self.assertEqual(code, 200)
        self.assertEqual(_jreq(self._port, "POST", "/api/settings", {"failure_action": "explode"})[0], 400)


if __name__ == "__main__":
    unittest.main()
