"""Multi-plate Bambu projects: plate layout, per-plate geometry/preview/key, API and history."""
import base64
import io
import json
import os
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import evolution
from server import (ExtractionError, best_thumbnail, build_preview_mesh, extract_3mf, open_db,
                    plate_layout, _plate_scope)
from test_server import DEVICE_TOKEN, ServerTestCase, _jreq, _req

NS = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
PNG = b"\x89PNG\r\n\x1a\n"


def tetra(oid, height):
    return (f'<object id="{oid}" type="model"><mesh><vertices>'
            f'<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="{height}"/>'
            '</vertices><triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>'
            '<triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/></triangles></mesh></object>')


def move(x, y=10):
    return f"1 0 0 0 1 0 0 0 1 {x} {y} 0"


def project(plate2_x=(40, 320), plates=((("2", 0),), (("2", 1), ("4", 0))), renders=(1, 2)):
    """Split-model project like Studio exports: Cube x2 (plate 1, plate 2) and Tower (plate 2)."""
    items = [("2", move(10)), ("2", move(plate2_x[0])), ("4", move(plate2_x[1]))]
    main = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {NS}><resources>'
            '<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>'
            '<object id="4" type="model"><components><component p:path="/3D/Objects/object_2.model" objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>'
            '</resources><build>' + "".join(f'<item objectid="{o}" transform="{t}" printable="1"/>' for o, t in items)
            + '</build></model>')
    settings = ('<?xml version="1.0" encoding="UTF-8"?><config>'
                '<object id="2"><metadata key="name" value="Cube"/><part id="1" subtype="normal_part"/></object>'
                '<object id="4"><metadata key="name" value="Tower"/><part id="3" subtype="normal_part"/></object>')
    for index, instances in enumerate(plates, 1):
        settings += f'<plate><metadata key="plater_id" value="{index}"/><metadata key="plater_name" value="P{index}"/>'
        for oid, iid in instances:
            settings += f'<model_instance><metadata key="object_id" value="{oid}"/><metadata key="instance_id" value="{iid}"/></model_instance>'
        settings += '</plate>'
    settings += '</config>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("3D/3dmodel.model", main)
        zf.writestr("3D/Objects/object_1.model", f'<model unit="millimeter" {NS}><resources>{tetra(1, 10)}</resources><build/></model>')
        zf.writestr("3D/Objects/object_2.model", f'<model unit="millimeter" {NS}><resources>{tetra(3, 40)}</resources><build/></model>')
        zf.writestr("Metadata/model_settings.config", settings)
        zf.writestr("Metadata/project_settings.config", json.dumps({"layer_height": "0.2", "sparse_infill_density": "15%"}))
        for n in renders:
            zf.writestr(f"Metadata/plate_{n}.png", PNG + f"plate{n}".encode())
    return out.getvalue()


def triangles(mesh):
    return int.from_bytes(mesh[8:12], "little")


class PlateExtraction(unittest.TestCase):
    def test_layout_lists_plates_and_objects(self):
        with zipfile.ZipFile(io.BytesIO(project())) as zf:
            layout = plate_layout(zf, {n.lower(): n for n in zf.namelist()})
        self.assertEqual([p["index"] for p in layout], [1, 2])
        self.assertEqual(layout[1]["objects"], ["Cube", "Tower"])
        self.assertEqual(layout[1]["instances"], [("2", 1), ("4", 0)])

    def test_whole_project_unchanged_but_lists_plates(self):
        info = extract_3mf(project())
        self.assertIsNone(info["plate"])
        self.assertEqual([p["instance_count"] for p in info["plates"]], [1, 2])
        self.assertNotIn("scope", info["geometry"])

    def test_plate_geometry_covers_only_that_plate(self):
        info = extract_3mf(project(), 2)
        geom = info["geometry"]
        self.assertEqual((geom["scope"], geom["plate_index"], geom["build_items"], geom["object_count"]), ("plate", 2, 2, 2))
        self.assertEqual(geom["triangle_count"], 8)
        self.assertEqual(geom["bbox"]["min"][0], 40)
        self.assertEqual(geom["bbox"]["max"][2], 40)
        self.assertEqual(geom["project"]["plates"], 2)
        self.assertEqual(base64.b64decode(info["thumbnail_b64"]), PNG + b"plate2")
        one = extract_3mf(project(), 1)["geometry"]
        self.assertEqual((one["build_items"], one["triangle_count"], one["bbox"]["max"][2]), (1, 4, 10))

    def test_unknown_or_empty_plate_rejected(self):
        with self.assertRaises(ExtractionError):
            extract_3mf(project(), 3)
        with self.assertRaises(ExtractionError):
            extract_3mf(project(plates=((("2", 0),), ())), 2)
        with self.assertRaises(ExtractionError):  # plate lists an instance the build does not have
            extract_3mf(project(plates=((("2", 0),), (("4", 5),))), 2)

    def test_preview_mesh_per_plate(self):
        raw = project()
        self.assertEqual(triangles(build_preview_mesh(raw)), 12)
        self.assertEqual(triangles(build_preview_mesh(raw, 1)), 4)
        self.assertEqual(triangles(build_preview_mesh(raw, 2)), 8)

    def test_thumbnail_never_shows_another_plate(self):
        self.assertEqual(best_thumbnail(project(), 2)[0], PNG + b"plate2")
        self.assertEqual(best_thumbnail(project(), 1)[0], PNG + b"plate1")
        self.assertIsNone(best_thumbnail(project(renders=(1,)), 2))
        self.assertEqual(best_thumbnail(project(renders=(1,)))[0], PNG + b"plate1")

    def test_prompt_scope(self):
        self.assertIn("plate 2 of 2", _plate_scope(extract_3mf(project(), 2)))
        self.assertIn("also apply to the other plates", _plate_scope(extract_3mf(project(), 2)))
        self.assertIn("whole project with 2 plates", _plate_scope(extract_3mf(project())))


class PlateKey(unittest.TestCase):
    def key(self, raw, index):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            plate = next(p for p in plate_layout(zf, {n.lower(): n for n in zf.namelist()}) if p["index"] == index)
        return evolution.model_key(raw, plate)

    def test_plate_keys_differ_from_project_and_each_other(self):
        raw = project()
        keys = {evolution.model_key(raw), self.key(raw, 1), self.key(raw, 2)}
        self.assertEqual(len(keys), 3)
        self.assertTrue(self.key(raw, 2).startswith("geom-plate-v1:"))

    def test_plate_key_ignores_bed_position_and_other_plates(self):
        base = self.key(project(), 2)
        self.assertEqual(self.key(project(plate2_x=(340, 620)), 2), base)  # whole plate shifted
        self.assertNotEqual(self.key(project(plate2_x=(40, 330)), 2), base)  # layout on the plate changed
        # Plate 1 keeps its key when plate 2 changes.
        self.assertEqual(self.key(project(plate2_x=(40, 330)), 1), self.key(project(), 1))


class PlateAPI(ServerTestCase):
    RESULT = dict(summary="ok", changes=[dict(key="layer_height", old=0.2, value=0.16, reason="q")], warnings=[], model={})

    def analyze(self, raw, **extra):
        with patch("server.ai_analyze", return_value=self.RESULT) as ai:
            code, data = _jreq(self._port, "POST", "/api/analyze",
                               dict(filename="multi.3mf", project_base64=base64.b64encode(raw).decode(), **extra))
        return code, data, ai

    def test_validation(self):
        for bad in (0, -1, "2", 1.0, True, 65):
            self.assertEqual(self.analyze(project(), plate_index=bad)[0], 400, bad)
        code, data, ai = self.analyze(project(), plate_index=7)
        self.assertEqual(code, 422)
        ai.assert_not_called()

    def test_plate_analysis_stores_scope_and_serves_plate_assets(self):
        code, data, ai = self.analyze(project(), plate_index=2)
        self.assertEqual((code, data["status"], data["plate_index"]), (200, "complete", 2))
        self.assertEqual(len(data["plates"]), 2)
        self.assertTrue(data["model_key"].startswith("geom-plate-v1:"))
        self.assertEqual(ai.call_args.args[1]["geometry"]["plate_index"], 2)
        state = json.loads(_req(self._port, "GET", "/api/state").read())
        self.assertEqual(next(a for a in state["analyses"] if a["id"] == data["id"])["plate_index"], 2)
        self.assertEqual(_req(self._port, "GET", f"/api/analyses/{data['id']}/thumbnail").read(), PNG + b"plate2")
        resp = _req(self._port, "GET", f"/api/analyses/{data['id']}/optimized")
        self.assertEqual(resp.status, 422)
        self.assertIn("other plates", json.loads(resp.read())["error"])

    def test_whole_project_and_single_plate_share_identity(self):
        code, whole, _ = self.analyze(project())
        self.assertEqual((code, whole["plate_index"]), (200, None))
        self.assertTrue(whole["model_key"].startswith("geom-v1:"))
        single = project(plate2_x=(40, 320), plates=((("2", 0), ("2", 1), ("4", 0)),))
        _, a, _ = self.analyze(single)
        _, b, _ = self.analyze(single, plate_index=1)
        self.assertEqual((b["plate_index"], a["model_key"]), (1, b["model_key"]))
        self.assertEqual(_req(self._port, "GET", f"/api/analyses/{b['id']}/optimized").status, 200)

    def test_print_runs_record_plate_and_link_checks_it(self):
        _, plate2, _ = self.analyze(project(), plate_index=2)
        _, plate1, _ = self.analyze(project(), plate_index=1)
        beat = lambda status, t: _jreq(self._port, "POST", "/api/heartbeat",
                                       dict(device_id="d", printer_id="p", status=status, telemetry=t), DEVICE_TOKEN)
        beat("printing", dict(task_id="1", subtask_id="10", gcode_state="RUNNING", plate_index=2))
        beat("idle", dict(gcode_state="FINISH"))
        run = json.loads(_req(self._port, "GET", "/api/prints").read())["prints"][0]
        self.assertEqual(run["plate_index"], 2)
        self.assertEqual(_jreq(self._port, "POST", f"/api/prints/{run['id']}/link", dict(analysis_id=plate1["id"]))[0], 409)
        self.assertEqual(_jreq(self._port, "POST", f"/api/prints/{run['id']}/link", dict(analysis_id=plate2["id"]))[0], 200)
        linked = json.loads(_req(self._port, "GET", f"/api/analyses/{plate2['id']}/prints").read())["prints"]
        self.assertEqual([r["id"] for r in linked], [run["id"]])
        self.assertEqual(json.loads(_req(self._port, "GET", f"/api/analyses/{plate1['id']}/prints").read())["prints"], [])

    def test_bad_plate_telemetry_dropped(self):
        _jreq(self._port, "POST", "/api/heartbeat",
              dict(device_id="d", printer_id="p", status="printing",
                   telemetry=dict(task_id="1", subtask_id="10", gcode_state="RUNNING", plate_index=2.5)), DEVICE_TOKEN)
        self.assertIsNone(json.loads(_req(self._port, "GET", "/api/prints").read())["prints"][0]["plate_index"])

    def test_migration_adds_columns_to_old_db(self):
        db = open_db(os.path.join(self._tmpdir, "printpilot.db"))
        cols = {r[1] for r in db.execute("PRAGMA table_info(print_runs)")} & {r[1] for r in db.execute("PRAGMA table_info(analyses)")}
        self.assertIn("plate_index", cols)
        db.close()


if __name__ == "__main__":
    unittest.main()
