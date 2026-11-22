"""Filament library: catalogue, custom/imported presets, Studio bundles and the API."""
import base64
import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
import filaments
import insight
from server import open_db
from test_server import DEVICE_TOKEN, ServerTestCase, _jreq, _req

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class Library(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.db = open_db(os.path.join(tempfile.mkdtemp(prefix="pp_fil_"), "printpilot.db"))

    def custom(self, **kw):
        data = {"name": "SUNLU PETG HS @BBL A1", "vendor": "SUNLU", "base": "Generic PETG @BBL A1",
                "values": {"nozzle_temperature": 250, "filament_max_volumetric_speed": 18, "textured_plate_temp": 80}}
        data.update(kw)
        return filaments.create_custom(ROOT, self.db, data)

    def test_catalogue_for_a1(self):
        cat = filaments.catalogue(ROOT, "A1", self.db)
        self.assertEqual(cat["printer_model"], "Bambu Lab A1")
        self.assertEqual([t["type"] for t in cat["types"][:2]], ["PLA", "PETG"])
        petg = next(t for t in cat["types"] if t["type"] == "PETG")
        self.assertEqual(petg["hygroscopic"], 2)
        by_name = {p["name"]: p for t in cat["types"] for p in t["presets"]}
        self.assertTrue(by_name["TINMORRY PETG ECO @BBL A1"]["exportable"])
        self.assertFalse(by_name["Generic PETG @BBL A1"]["exportable"])
        self.assertEqual(by_name["Generic PETG @BBL A1"]["specs"]["textured_plate_temp"], 80)
        self.assertTrue(any(v["vendor"] == "SUNLU" for v in cat["vendors"]))
        self.assertTrue(next(t for t in cat["types"] if t["type"] == "PLA-CF")["abrasive"])
        self.assertNotIn("Generic PETG @BBL X1C", by_name)  # other printers' presets are not offered
        self.assertIn("Generic PETG @BBL A1", cat["bases"])
        self.assertNotIn("Generic PETG @BBL A1 0.2 nozzle", by_name)  # 0.4 mm nozzle by default
        small = {p["name"] for t in filaments.catalogue(ROOT, "A1", self.db, 0.2)["types"] for p in t["presets"]}
        self.assertIn("Generic PETG @BBL A1 0.2 nozzle", small)

    def test_create_validate_and_update(self):
        made = self.custom()
        entry = filaments.library_rows(self.db)[0]
        self.assertEqual((entry["inherits"], entry["filament_id"], entry["source"]), ("Generic PETG @BBL A1", "GFG99", "custom"))
        self.assertEqual(entry["body"]["nozzle_temperature"], ["250"])
        self.assertNotIn("textured_plate_temp", entry["body"])  # same as the base: not stored
        for bad, message in [({"name": "a/b"}, "Tên"), ({"base": "Nope"}, "gốc"), ({"vendor": ""}, "hãng"),
                             ({"values": {"nozzle_temperature": 400}}, "khoảng"), ({"values": {"gcode": 1}}, "Không hỗ trợ"),
                             ({"name": "Generic PETG @BBL A1"}, "hệ thống"), ({"colour": "red"}, "#RRGGBB"),
                             ({"values": {"nozzle_temperature": 230, "nozzle_temperature_range_low": 240}}, "khoảng nhiệt")]:
            with self.assertRaises(ValueError) as ctx:
                self.custom(**{**bad, **({} if "name" in bad else {"name": "Other @BBL A1"})})
            self.assertIn(message, str(ctx.exception))
        with self.assertRaises(ValueError):
            self.custom()  # duplicate name
        self.custom(id=made["id"], values={"nozzle_temperature": 245})
        self.assertEqual(filaments.library_rows(self.db)[0]["body"]["nozzle_temperature"], ["245"])
        cat = filaments.catalogue(ROOT, "Bambu Lab A1", self.db)
        mine = next(p for t in cat["types"] for p in t["presets"] if p.get("id") == made["id"])
        self.assertEqual((mine["specs"]["nozzle_temperature"], mine["specs"]["textured_plate_temp"]), (245, 80))
        self.assertEqual(filaments.catalogue(ROOT, "P1S", self.db)["types"] and
                         [p for t in filaments.catalogue(ROOT, "P1S", self.db)["types"] for p in t["presets"] if p.get("id")], [])

    def test_bundle_is_what_studio_imports(self):
        made = self.custom()
        items = filaments.export_items(ROOT, self.db, ["TINMORRY PETG ECO @BBL A1"], [made["id"]])
        with zipfile.ZipFile(io.BytesIO(filaments.bundle(items))) as zf:
            names = set(zf.namelist())
            self.assertIn("bundle_structure.json", names)
            structure = json.loads(zf.read("bundle_structure.json"))
            self.assertEqual(structure["bundle_type"], "filament config bundle")
            sunlu = json.loads(zf.read("SUNLU/SUNLU PETG HS @BBL A1.json"))
            tinmorry = json.loads(zf.read("TINMORRY/TINMORRY PETG ECO @BBL A1.json"))
        # Studio's import_json_presets needs a parseable version, filament_settings_id and an existing parent
        for preset in (sunlu, tinmorry):
            self.assertEqual(preset["from"], "User")
            self.assertRegex(preset["version"], r"^\d+\.\d+\.\d+\.\d+$")
            self.assertEqual(preset["filament_settings_id"], [preset["name"]])
            self.assertIn(preset["inherits"], insight.filament_profiles(ROOT))
            self.assertEqual(preset["filament_id"], "GFG99")
            self.assertNotIn("setting_id", preset)
        self.assertEqual(tinmorry["nozzle_temperature"], ["245"])
        with self.assertRaises(ValueError):
            filaments.export_items(ROOT, self.db, ["Generic PETG @BBL A1"], [])  # stock preset: nothing to import
        with self.assertRaises(LookupError):
            filaments.export_items(ROOT, self.db, [], ["missing"])

    def test_import_json_and_bundle(self):
        user_json = json.dumps({"name": "Jayo PETG @BBL A1", "inherits": "Generic PETG @BBL A1", "from": "User",
                                "version": "2.3.0.2", "filament_settings_id": ["Jayo PETG @BBL A1"],
                                "nozzle_temperature": ["240"], "filament_vendor": ["JAYO"], "not_a_studio_key": "x",
                                "filament_id": "P1234567"}).encode()
        result = filaments.import_presets(ROOT, self.db, "jayo.json", user_json)
        self.assertEqual([p["name"] for p in result["imported"]], ["Jayo PETG @BBL A1"])
        row = filaments.library_rows(self.db)[0]
        self.assertEqual((row["vendor"], row["filament_id"]), ("JAYO", "GFG99"))
        self.assertNotIn("not_a_studio_key", row["body"])
        # Re-import of our own bundle replaces by name; unknown parents / system names are skipped
        items = filaments.export_items(ROOT, self.db, [], [row["id"]])
        extra = [{"name": "Orphan", "inherits": "Unknown parent", "filament_settings_id": ["Orphan"]},
                 {"name": "Generic PETG @BBL A1", "inherits": "Generic PETG @base", "filament_settings_id": ["x"]},
                 {"name": "Root PLA", "inherits": "", "filament_type": ["PLA"], "filament_vendor": ["Acme"], "nozzle_temperature": ["210"]}]
        result = filaments.import_presets(ROOT, self.db, "mix.bbsflmt", filaments.bundle(items + extra))
        self.assertEqual(sorted(p["name"] for p in result["imported"]), ["Jayo PETG @BBL A1", "Root PLA"])
        self.assertEqual(sorted(s["name"] for s in result["skipped"]), ["Generic PETG @BBL A1", "Orphan"])
        self.assertEqual(len(filaments.library_rows(self.db)), 2)
        for name, raw in [("x.txt", b"x"), ("x.json", b"{bad"), ("x.bbsflmt", b"not zip"), ("x.json", b"[]")]:
            with self.assertRaises(ValueError):
                filaments.import_presets(ROOT, self.db, name, raw)

    def test_library_preset_feeds_analysis_profile(self):
        self.custom()
        profile = insight.material_profile(ROOT, "SUNLU PETG HS @BBL A1", filaments.library_presets(self.db))
        self.assertEqual((profile["nozzle_temperature"], profile["filament_max_volumetric_speed"], profile["type"]), (250, 18, "PETG"))
        self.assertEqual(profile["vendor"], "SUNLU")

    def test_delete(self):
        made = self.custom()
        self.assertTrue(filaments.delete(self.db, made["id"]))
        self.assertFalse(filaments.delete(self.db, made["id"]))


class LibraryAPI(ServerTestCase):
    def test_flow(self):
        _jreq(self._port, "POST", "/api/heartbeat", dict(device_id="d", printer_id="a1-01", status="idle", printer_model="A1"), DEVICE_TOKEN)
        self.assertEqual(_req(self._port, "GET", "/api/filament-library", token=None).status, 401)
        cat = json.loads(_req(self._port, "GET", "/api/filament-library").read())
        self.assertEqual(cat["printer_model"], "Bambu Lab A1")
        self.assertIn("Bambu Lab A1", cat["printer_models"])
        code, made = _jreq(self._port, "POST", "/api/filament-library", {"name": "Kingroon PETG @BBL A1", "vendor": "Kingroon",
                                                                         "base": "Generic PETG @BBL A1", "values": {"nozzle_temperature": 235}})
        self.assertEqual(code, 200)
        code, err = _jreq(self._port, "POST", "/api/filament-library", {"name": "x", "vendor": "y", "base": "nope"})
        self.assertEqual(code, 400)
        self.assertIn("gốc", err["error"])
        payload = base64.b64encode(json.dumps({"name": "Eryone PETG @BBL A1", "inherits": "Generic PETG @BBL A1",
                                               "filament_settings_id": ["Eryone PETG @BBL A1"]}).encode()).decode()
        code, imported = _jreq(self._port, "POST", "/api/filament-library/import", {"filename": "eryone.json", "data_base64": payload})
        self.assertEqual((code, len(imported["imported"])), (200, 1))
        resp = _req(self._port, "GET", f"/api/filament-library/export?id={made['preset']['id']}&name=TINMORRY%20PETG%20ECO%20%40BBL%20A1")
        self.assertEqual(resp.status, 200)
        self.assertIn(".bbsflmt", resp.getheader("Content-Disposition"))
        with zipfile.ZipFile(io.BytesIO(resp.read())) as zf:
            self.assertEqual(len([n for n in zf.namelist() if n != "bundle_structure.json"]), 2)
        self.assertEqual(_req(self._port, "GET", "/api/filament-library/export?name=Generic%20PETG%20%40BBL%20A1").status, 400)
        code, _ = _jreq(self._port, "POST", f"/api/filament-library/{made['preset']['id']}/delete", {})
        self.assertEqual(code, 200)
        self.assertEqual(_jreq(self._port, "POST", f"/api/filament-library/{made['preset']['id']}/delete", {})[0], 404)
        events = json.loads(_req(self._port, "GET", "/api/events").read())
        self.assertTrue(any(e["category"] == "filament" for e in events.get("events", [])))


if __name__ == "__main__":
    unittest.main()
