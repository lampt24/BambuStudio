"""Shape metrics, rendering, material/printer rules, extended settings, similar history and the API."""
import array
import base64
import io
import json
import os
import struct
import sys
import time
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import evolution
import insight
import tuning
from server import build_prompt, extract_3mf, open_db, validate_settings
from test_server import DEVICE_TOKEN, ServerTestCase, _jreq, _req

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def box(x0, y0, z0, x1, y1, z1, base=0):
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6), (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5)]
    return [c for v in verts for c in v], [i + base for t in tris for i in t]


def obj(*boxes, label=1, name="part", filament=1):
    verts, tris = array.array("f"), array.array("I")
    for b in boxes:
        v, t = box(*b, base=len(verts) // 3)
        verts.extend(v)
        tris.extend(t)
    return {"label": label, "name": name, "verts": verts, "tris": tris, "filaments": {filament},
            "tri_filaments": array.array("B", [filament] * (len(tris) // 3))}


def pixels(png):
    import zlib
    width, height = struct.unpack(">II", png[16:24])
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    stride = width * 3 + 1
    return [tuple(raw[y * stride + 1 + x * 3:y * stride + 4 + x * 3]) for y in range(height) for x in range(width)]


CUBE = obj((0, 0, 0, 10, 10, 10))
COLUMN = obj((0, 0, 0, 5, 5, 60), name="column")
T_SHAPE = obj((8, 8, 0, 12, 12, 10), (0, 0, 10, 20, 20, 12), name="T")           # 400 mm2 plate on a 4x4 stem
INLAY = obj((0, 0, 0, 20, 20, 2), (5, 5, 2, 15, 15, 3), name="badge")           # colour layer resting on the base
FLOATING = obj((0, 0, 3, 10, 10, 13), name="floating")


class Metrics(unittest.TestCase):
    def test_cube(self):
        m = insight.object_metrics(CUBE)
        self.assertEqual(m["size_mm"], [10, 10, 10])
        self.assertEqual((m["bed_contact_mm2"], m["overhang_60_mm2"], m["ceiling_mm2"]), (100, 0, 0))
        self.assertAlmostEqual(m["solid_volume_cm3"], 1.0)
        self.assertEqual(m["slenderness"], 1.0)

    def test_overhang_under_t_plate(self):
        m = insight.object_metrics(T_SHAPE)
        self.assertEqual(m["bed_contact_mm2"], 16)
        self.assertEqual(m["ceiling_mm2"], 384)          # plate underside minus the stem top it rests on
        self.assertEqual(m["lowest_steep_overhang_mm"], 10)

    def test_colour_layer_is_not_an_overhang(self):
        m = insight.object_metrics(INLAY)
        self.assertEqual((m["overhang_60_mm2"], m["ceiling_mm2"]), (0, 0))
        self.assertEqual(m["size_mm"][2], 3)

    def test_shared_faces_between_parts_ignored(self):
        two = obj((0, 0, 0, 10, 10, 5), (0, 0, 5, 10, 10, 10))   # same footprint stacked: shared face z=5
        m = insight.object_metrics(two)
        self.assertEqual(m["ceiling_mm2"], 0)
        self.assertAlmostEqual(m["surface_mm2"], insight.object_metrics(CUBE)["surface_mm2"], delta=1)

    def test_plate_summary_gap(self):
        a = insight.object_metrics(CUBE)
        b = insight.object_metrics(obj((30, 0, 0, 40, 10, 10), label=2))
        self.assertEqual(insight.plate_summary([a, b])["min_gap_mm"], 20)


class Render(unittest.TestCase):
    def test_png_sheet_with_filament_colours(self):
        start = time.time()
        png = insight.render_views([CUBE, obj((20, 0, 0, 30, 10, 30), label=2, filament=2)], colors=[(255, 0, 0), (0, 255, 0)])
        self.assertLess(time.time() - start, 5)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (960, 692))
        px = pixels(png)
        self.assertTrue(any(g > 150 and r < 60 and b < 60 for r, g, b in px))  # filament 2 drawn green
        self.assertTrue(any(r > 150 and g < 60 and b < 60 for r, g, b in px))  # filament 1 drawn red

    def test_overhang_drawn_red(self):
        reds = sum(1 for r, g, b in pixels(insight.render_views([T_SHAPE])) if r > 120 and g < 60 and b < 60)
        self.assertGreater(reds, 500)

    def test_empty_scene(self):
        with self.assertRaises(ValueError):
            insight.render_views([])


class Knowledge(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(insight.as_list('"PETG";"PLA"'), ["PETG", "PLA"])
        self.assertEqual(insight.scalar("245,220", 1), "220")
        self.assertEqual(insight.scalar(["#FF0000"]), "#FF0000")
        self.assertEqual(insight.number("15%"), 15)

    def test_focused_settings_keep_late_keys_and_drop_secrets(self):
        config = {f"aaa_{i:03d}": i for i in range(600)}
        config.update({"wall_loops": "3", "support_type": "tree(auto)", "nozzle_temperature": ["245"], "print_host_api_key": "x"})
        out = insight.focused_settings(config)
        self.assertEqual(out["process"]["wall_loops"], "3")
        self.assertEqual(out["support_adhesion"]["support_type"], "tree(auto)")
        self.assertEqual(out["filament"]["nozzle_temperature"], ["245"])
        self.assertNotIn("print_host_api_key", json.dumps(out))

    def test_material_and_printer(self):
        self.assertEqual(insight.polymer_of("PETG", "TINMORRY PETG ECO @BBL A1"), "PETG")
        self.assertEqual(insight.polymer_of("PLA", "Bambu PLA-CF"), "PLA")
        self.assertEqual(insight.polymer_of("PA-CF"), "PA")
        self.assertEqual(insight.printer_of("Bambu Lab A1 mini")[0], "A1 mini")
        self.assertTrue(insight.printer_of("Bambu Lab A1")[1]["bed_slinger"])
        self.assertTrue(insight.printer_of("Bambu Lab X1 Carbon")[1]["enclosed"])

    def test_tinmorry_profile_resolves_through_bambu_base(self):
        p = insight.material_profile(ROOT, "TINMORRY PETG ECO @BBL A1")
        self.assertEqual((p["vendor"], p["type"], p["nozzle_temperature"]), ("TINMORRY", "PETG", 245))
        self.assertEqual(p["filament_max_volumetric_speed"], 12)
        self.assertEqual(p["textured_plate_temp"], 80)  # inherited from Generic PETG @BBL A1
        self.assertIsNone(insight.material_profile(ROOT, "No such preset"))

    def test_humidity(self):
        self.assertTrue(insight.humidity_state({"units": [{"humidity": 1}]})["wet"])
        self.assertFalse(insight.humidity_state({"units": [{"humidity": 5}]})["wet"])
        self.assertEqual(insight.humidity_state({"units": [{"humidity_raw": 55, "humidity": 4}]})["text"], "55%")
        self.assertIsNone(insight.humidity_state({"units": [{"humidity": None}]}))


def codes(findings):
    return {f["code"] for f in findings}


class Rules(unittest.TestCase):
    A1 = {"printer_key": "A1", "printer": insight.PRINTERS["A1"]}

    def ctx(self, **kw):
        base = {"settings": {"enable_support": "0", "brim_type": "no_brim", "layer_height": "0.2"}, "material": "PLA",
                "nozzle": 0.4, **self.A1}
        base.update(kw)
        return base

    def test_geometry_rules(self):
        objs = [insight.object_metrics(o) for o in (COLUMN, T_SHAPE, FLOATING)]
        found = insight.evaluate(objs, self.ctx())
        self.assertTrue({"tall_slender", "small_contact", "overhang_unsupported", "floating"} <= codes(found))
        tall = next(f for f in found if f["code"] == "tall_slender")
        self.assertIn("trục Y", tall["message"])
        self.assertEqual(found[0]["severity"], "critical")  # sorted by severity
        supported = insight.evaluate([insight.object_metrics(T_SHAPE)], self.ctx(settings={"enable_support": "1"}))
        self.assertIn("overhang_supported", codes(supported))

    def test_material_printer_rules(self):
        cube = [insight.object_metrics(CUBE)]
        self.assertIn("needs_enclosure", codes(insight.evaluate(cube, self.ctx(material="ABS"))))
        self.assertNotIn("needs_enclosure", codes(insight.evaluate(cube, self.ctx(material="ABS", printer_key="P1S", printer=insight.PRINTERS["P1S"]))))
        cf = self.ctx(settings={"filament_type": "PETG-CF", "filament_settings_id": "Generic PETG-CF"}, nozzle_type="stainless_steel")
        self.assertIn("abrasive_nozzle", codes(insight.evaluate(cube, cf)))
        self.assertNotIn("abrasive_nozzle", codes(insight.evaluate(cube, {**cf, "nozzle_type": "hardened_steel"})))
        self.assertIn("tpu_ams", codes(insight.evaluate(cube, self.ctx(material="TPU", settings={"filament_settings_id": "Bambu TPU 95A"}))))
        petg = self.ctx(material="PETG", settings={"curr_bed_type": "High Temp Plate"})
        self.assertTrue({"petg_smooth_pei", "hygroscopic"} <= codes(insight.evaluate(cube, petg)))
        wet = insight.evaluate(cube, {**petg, "humidity": {"text": "60%", "wet": True}})
        self.assertIn("wet_filament", codes(wet))

    def test_profile_rules(self):
        cube = [insight.object_metrics(CUBE)]
        profile = {"preset": "X", "nozzle_temperature_range_low": 230, "nozzle_temperature_range_high": 260,
                   "filament_max_volumetric_speed": 8, "eng_plate_temp": 0}
        s = {"nozzle_temperature": "215", "outer_wall_speed": "200", "outer_wall_line_width": "0.42",
             "layer_height": "0.2", "curr_bed_type": "Engineering Plate"}
        found = insight.evaluate(cube, self.ctx(settings=s, profile=profile))
        self.assertTrue({"nozzle_temp_range", "flow_capped", "bed_unsupported"} <= codes(found))
        capped = next(f for f in found if f["code"] == "flow_capped")
        self.assertAlmostEqual(capped["evidence"]["speeds"]["outer_wall_speed"]["cap"], 95.2)

    def test_ams_rules(self):
        cube = [insight.object_metrics(CUBE)]
        ams = {"units": [{"id": 0, "name": "AMS 1", "slots": [{"id": 0, "present": True, "material": "PLA"},
                                                              {"id": 1, "present": False}]}]}
        ctx = self.ctx(material="PETG", ams=ams, project_filaments=[{"index": 1, "material": "PETG", "preset": "TINMORRY PETG ECO"}])
        missing = next(f for f in insight.evaluate(cube, ctx) if f["code"] == "filament_not_loaded")
        self.assertIn("PETG", missing["message"])
        ok = self.ctx(ams=ams, project_filaments=[{"index": 1, "material": "PLA"}])
        self.assertNotIn("filament_not_loaded", codes(insight.evaluate(cube, ok)))
        unknown = self.ctx(printer_requested="A1 (.70)")
        self.assertIn("printer_unknown", codes(insight.evaluate(cube, unknown)))


class Tuning(unittest.TestCase):
    SETTINGS = {"enable_support": "0", "support_type": "normal(auto)", "seam_position": "aligned",
                "nozzle_temperature": '245,215', "nozzle_temperature_range_low": "240,190",
                "nozzle_temperature_range_high": "260,230", "curr_bed_type": "Textured PEI Plate",
                "textured_plate_temp": "80,65", "hot_plate_temp": "80,65", "fan_max_speed": "40,100"}

    def test_allowed_uses_plate_filament_and_bed(self):
        allow = tuning.allowed(self.SETTINGS, 2)
        self.assertEqual((allow["nozzle_temperature"], allow["textured_plate_temp"], allow["enable_support"]), (215, 65, 0))
        self.assertNotIn("hot_plate_temp", allow)
        self.assertNotIn("nozzle_temperature", tuning.allowed(self.SETTINGS, None))

    def test_validate(self):
        changes = [
            {"key": "enable_support", "old": 0, "value": 1, "reason": "treo", "evidence": "overhang_unsupported"},
            {"key": "support_type", "old": "normal(auto)", "value": "tree(auto)", "reason": "r"},
            {"key": "nozzle_temperature", "old": 215, "value": 250, "reason": "r"},       # outside 190-230 of filament 2
            {"key": "seam_position", "old": "nearest", "value": "back", "reason": "r"},   # old mismatch
            {"key": "hot_plate_temp", "old": 65, "value": 70, "reason": "r"},              # not the bed in use
            {"key": "sparse_infill_pattern", "old": "grid", "value": "lightning", "reason": "r"},  # absent + not allowed
            {"key": "fan_max_speed", "old": 100, "value": 80, "reason": "r"},
        ]
        valid, rejected = tuning.validate(changes, {**self.SETTINGS, "sparse_infill_pattern": "grid"}, 2, 1)
        self.assertEqual([c["key"] for c in valid], ["enable_support", "support_type", "fan_max_speed"])
        self.assertEqual(valid[0]["evidence"], "overhang_unsupported")
        self.assertEqual((valid[2]["scope"], valid[2]["filament_index"]), ("filament", 2))
        self.assertEqual(len(rejected), 4)
        self.assertTrue(any("190–230" in r for r in rejected))
        _, multi = tuning.validate([{"key": "fan_max_speed", "old": 100, "value": 80}], self.SETTINGS, None, 3)
        self.assertIn("3 nhựa", multi[0])

    def test_manual_and_project_value(self):
        self.assertIn("enable_support", tuning.as_manual([{"key": "enable_support", "old": 0, "value": 1}])[0])
        self.assertEqual(tuning.as_manual([{"key": "layer_height", "old": 0.2, "value": 0.16}]), [])
        self.assertEqual(tuning.project_value("nozzle_temperature", 250, ["245", "220"], 2), ["245", "250"])
        self.assertEqual(tuning.project_value("enable_support", 1, "0", None), "1")
        with self.assertRaises(ValueError):
            tuning.project_value("nozzle_temperature", 250, ["245"], 2)


def project(config=None, model_settings=None):
    from test_plates import project as plates_project
    raw = plates_project(plates=((("2", 0), ("2", 1), ("4", 0)),))
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w") as dst:
        for n in src.namelist():
            if n == "Metadata/project_settings.config" and config is not None:
                dst.writestr(n, json.dumps(config))
            else:
                dst.writestr(n, src.read(n))
    return out.getvalue()


CONFIG = {"layer_height": "0.2", "sparse_infill_density": "15%", "enable_support": "0", "support_type": "normal(auto)",
          "nozzle_temperature": ["245"], "nozzle_temperature_range_low": ["240"], "nozzle_temperature_range_high": ["260"],
          "filament_type": ["PETG"], "filament_settings_id": ["TINMORRY PETG ECO @BBL A1"], "filament_colour": ["#00FF00"],
          "printer_model": "Bambu Lab A1", "curr_bed_type": "Textured PEI Plate", "textured_plate_temp": ["80"]}


class Export(unittest.TestCase):
    def test_optimized_writes_extended_values(self):
        raw = project(CONFIG)
        changes = [{"key": "layer_height", "old": 0.2, "value": 0.16},
                   {"key": "enable_support", "old": 0, "value": 1, "scope": "print"},
                   {"key": "nozzle_temperature", "old": 245, "value": 250, "scope": "filament", "filament_index": 1}]
        out = evolution.optimized(raw, changes, validate_settings, extract_3mf)
        config = extract_3mf(out)["config"]
        self.assertEqual((config["layer_height"], config["enable_support"], config["nozzle_temperature"]), ("0.16", "1", ["250"]))


class AnalyzeAPI(ServerTestCase):
    def analyze(self, raw, ai_changes, **extra):
        captured = {}

        def fake_post(base_url, api_key, path, payload):
            captured["payload"] = payload
            content = {"summary": "ok", "changes": ai_changes, "warnings": []}
            return {"choices": [{"message": {"content": json.dumps(content)}}], "model": "fake"}
        cfg_patch = patch.dict(self._cfg, {"ai_base_url": "http://ai.invalid", "ai_key": "k"})
        with cfg_patch, patch("server._openai_post", side_effect=fake_post):
            code, data = _jreq(self._port, "POST", "/api/analyze",
                               dict(filename="m.3mf", project_base64=base64.b64encode(raw).decode(), **extra))
        return code, data, captured.get("payload")

    CHANGES = [{"key": "layer_height", "old": 0.2, "value": 0.16, "reason": "q", "evidence": "object 1"},
               {"key": "enable_support", "old": 0, "value": 1, "reason": "treo", "evidence": "overhang_unsupported"}]

    def test_capable_client_gets_extended_changes_and_insights(self):
        code, data, payload = self.analyze(project(CONFIG), self.CHANGES, capabilities=["settings_v2"], trigger="send")
        self.assertEqual((code, data["status"]), (200, "complete"))
        self.assertEqual([c["key"] for c in data["changes"]], ["layer_height", "enable_support"])
        self.assertEqual(data["changes"][0]["evidence"], "object 1")
        ins = data["insights"]
        self.assertEqual(len(ins["objects"]), 3)
        self.assertEqual(ins["material"]["profile"]["vendor"], "TINMORRY")
        self.assertTrue(ins["views"].endswith("/views"))
        views = _req(self._port, "GET", ins["views"])
        self.assertEqual(views.status, 200)
        self.assertTrue(views.read().startswith(b"\x89PNG"))
        user = payload["messages"][1]["content"]
        self.assertIn("Objects on the plate (measured from the mesh)", user)
        self.assertIn("enable_support", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][2]["content"][1]["image_url"]["detail"], "high")
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        self.assertEqual(state["analyses"][0]["insights"]["material"]["type"], "PETG")
        optimized = _req(self._port, "GET", f"/api/analyses/{data['id']}/optimized")
        self.assertEqual(optimized.status, 200)
        self.assertEqual(extract_3mf(optimized.read())["config"]["enable_support"], "1")

    def test_old_client_gets_extended_ideas_as_warnings(self):
        code, data, payload = self.analyze(project(CONFIG), self.CHANGES, trigger="send")
        self.assertEqual([c["key"] for c in data["changes"]], ["layer_height"])
        self.assertTrue(any("enable_support" in w for w in data["warnings"]))
        self.assertIn("Do not suggest any other key", payload["messages"][0]["content"])

    def test_printer_serial_brings_ams_state(self):
        telemetry = {"serial": "03919D000000001", "ams": {"units": [{"id": 0, "name": "AMS 1", "humidity": None,
                     "slots": [{"id": 0, "present": True, "material": "PLA", "rfid": False}]}]}}
        _jreq(self._port, "POST", "/api/heartbeat", dict(device_id="d", printer_id="a1-01", status="idle",
                                                          telemetry=telemetry, printer_model="A1"), DEVICE_TOKEN)
        code, data, payload = self.analyze(project(CONFIG), [], printer_serial="03919D000000001", trigger="send")
        self.assertEqual(code, 200)
        self.assertIn("filament_not_loaded", {f["code"] for f in data["insights"]["findings"]})
        self.assertTrue(any("chưa lắp PETG" in w for w in data["warnings"]))
        self.assertEqual(self.analyze(project(CONFIG), [], printer_serial="bad serial!")[0], 400)

    def test_similar_shape_history_reaches_the_prompt(self):
        _, first, _ = self.analyze(project(CONFIG), [])
        db = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        db.execute("UPDATE analyses SET model_key='other-model' WHERE id=?", (first["id"],))
        review = json.dumps({"rating": 2, "issues": ["stringing"], "comment": "kéo sợi nhiều", "updated_at": "now"})
        db.execute("INSERT INTO print_runs(id,analysis_id,model_key,printer_id,started_at,status,review,material) "
                   "VALUES('r1',?,'other-model','a1-01','2026-11-01T00:00:00Z','completed',?,'PETG')", (first["id"], review))
        db.commit()
        db.close()
        _, second, payload = self.analyze(project(CONFIG), [])
        similar = [r for r in second["feedback_context"] if r.get("relation") == "similar_shape"]
        self.assertEqual([r["id"] for r in similar], ["r1"])
        self.assertIn("kéo sợi nhiều", payload["messages"][1]["content"])


class Prompt(unittest.TestCase):
    def test_prompt_without_insight_still_builds(self):
        system, user = build_prompt({"geometry": {"triangles": 4}, "config": {}}, {"layer_height": 0.2}, "quality", "manual")
        self.assertIn("Geometry metadata", user)
        self.assertIn("evidence", system)


if __name__ == "__main__":
    unittest.main()


class TinmorryProfiles(unittest.TestCase):
    """The imported TINMORRY presets load as BBL system presets."""

    def test_listed_unique_and_checker_clean(self):
        import subprocess
        with open(os.path.join(ROOT, "resources/profiles/BBL.json"), encoding="utf-8") as fh:
            bbl = json.load(fh)
        self.assertGreaterEqual(tuple(int(x) for x in bbl["version"].split(".")), (2, 9, 1, 0))
        entries = [e for e in bbl["filament_list"] if e["sub_path"].startswith("filament/TINMORRY/")]
        folder = os.path.join(ROOT, "resources/profiles/BBL/filament/TINMORRY")
        self.assertEqual(len(entries), len([f for f in os.listdir(folder) if f.endswith(".json")]))
        ids = set()
        for entry in entries:
            with open(os.path.join(ROOT, "resources/profiles/BBL", entry["sub_path"]), encoding="utf-8") as fh:
                preset = json.load(fh)
            self.assertEqual(preset["name"], entry["name"])
            self.assertEqual((preset["from"], preset["instantiation"], preset["filament_vendor"]), ("system", "true", ["TINMORRY"]))
            self.assertNotIn("filament_id", preset)  # inherited from the Bambu base so the AMS knows the material
            self.assertNotIn(preset["setting_id"], ids)
            ids.add(preset["setting_id"])
        result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts/check_profiles.py"), "--vendor", "BBL"],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("0 个错误", result.stderr + result.stdout)

    def test_a1_presets_offered_for_ams(self):
        names = {p["name"] for p in evolution.profiles(ROOT, "Bambu Lab A1")}
        for expected in ("TINMORRY PETG ECO @BBL A1", "TINMORRY PLA Matte @BBL A1", "TINMORRY PETG HS @BBL A1"):
            self.assertIn(expected, names)
        petg = next(p for p in evolution.profiles(ROOT, "Bambu Lab A1") if p["name"] == "TINMORRY PETG ECO @BBL A1")
        self.assertEqual((petg["filament_id"], petg["type"]), ("GFG99", "PETG"))
