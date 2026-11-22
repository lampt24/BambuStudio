"""Filament library: catalogue of installed presets per printer, custom/imported presets, and
.bbsflmt bundles Bambu Studio imports (File > Import > Import Configs).

* Catalogue: every instantiable filament preset of resources/profiles/BBL compatible with the
  printer, grouped by material type with curated notes. Presets of vendors PrintPilot adds
  (TINMORRY) are not in stock Bambu Studio, so they are exportable; stock ones are listed as
  already available.
* Library (SQLite table filament_library): presets created from a form (vendor values on top of a
  Bambu base) or uploaded (.json user preset or .bbsflmt bundle). Stored as Studio user-preset
  diffs: {inherits: <system preset>, ...changed keys} or a root preset (inherits "").
* Export: user-preset JSON files in a filament config bundle, the format Studio itself exports.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import insight

STUDIO_VERSION = "02.09.01.00"   # parsed by Studio's Semver like BBL.json; any valid version imports
ADDED_VENDORS = {"TINMORRY"}     # system presets in PrintPilot only, absent from stock Bambu Studio
MAX_UPLOAD = 8 * 1024 * 1024
MAX_PRESETS_PER_UPLOAD = 200
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Common material types first, in this order (others follow alphabetically).
TYPES: list[tuple[str, str, str]] = [
    ("PLA", "PLA", "Dễ in nhất, bề mặt đẹp, cứng nhưng giòn; mềm ở ~55–60°C. Hợp đồ trưng bày, mô hình, móc khóa."),
    ("PETG", "PETG", "Dai, chịu nhiệt ~70°C và ẩm tốt hơn PLA; dễ kéo sợi. Hợp chi tiết chức năng, hộp, giá đỡ."),
    ("TPU", "TPU (dẻo)", "Đàn hồi như cao su; in chậm, rút sợi ngắn. Hợp ốp lưng, gioăng, bánh xe."),
    ("TPU-AMS", "TPU cho AMS", "TPU cứng hơn, nạp được qua AMS."),
    ("ABS", "ABS", "Chịu nhiệt ~95°C, gia công được; co ngót mạnh, cần buồng kín."),
    ("ASA", "ASA", "Như ABS nhưng chịu UV/ngoài trời; cần buồng kín."),
    ("PLA-CF", "PLA sợi carbon", "Cứng, bề mặt mờ đẹp; mài mòn, cần đầu phun thép cứng."),
    ("PETG-CF", "PETG sợi carbon", "Cứng, ít cong vênh hơn PETG thường; cần đầu phun thép cứng."),
    ("PET-CF", "PET sợi carbon", "Cứng, chịu nhiệt cao; hút ẩm, cần sấy và đầu phun thép cứng."),
    ("PC", "PC", "Rất bền và chịu nhiệt ~110°C; nhiệt in cao, cần buồng kín."),
    ("PA", "Nylon (PA)", "Dai, chịu mài mòn; hút ẩm rất mạnh, cần sấy và buồng kín."),
    ("PA-CF", "Nylon sợi carbon", "Cứng và bền nhất nhóm phổ thông; hút ẩm, cần buồng kín và thép cứng."),
    ("PCTG", "PCTG", "Gần PETG, dai và trong hơn."),
    ("PVA", "PVA (support tan nước)", "Support tan trong nước cho PLA; hút ẩm cực mạnh."),
    ("HIPS", "HIPS", "Support tan trong limonene cho ABS."),
    ("PP", "PP", "Dẻo, kháng hóa chất; bám bàn rất kém."),
]
TYPE_ORDER = {key: i for i, (key, _, _) in enumerate(TYPES)}
TYPE_TEXT = {key: (label, desc) for key, label, desc in TYPES}

# Overrides a custom preset may set, with bounds. All stored as single-element string vectors.
CUSTOM_KEYS: dict[str, tuple[float, float]] = {
    "nozzle_temperature": (150, 350), "nozzle_temperature_initial_layer": (150, 350),
    "nozzle_temperature_range_low": (150, 350), "nozzle_temperature_range_high": (150, 350),
    "textured_plate_temp": (0, 120), "textured_plate_temp_initial_layer": (0, 120),
    "hot_plate_temp": (0, 120), "hot_plate_temp_initial_layer": (0, 120),
    "cool_plate_temp": (0, 120), "cool_plate_temp_initial_layer": (0, 120),
    "eng_plate_temp": (0, 120), "eng_plate_temp_initial_layer": (0, 120),
    "filament_max_volumetric_speed": (1, 60), "filament_flow_ratio": (0.8, 1.2),
    "fan_min_speed": (0, 100), "fan_max_speed": (0, 100), "overhang_fan_speed": (0, 100),
    "slow_down_layer_time": (0, 60), "filament_density": (0.8, 2.5), "filament_cost": (0, 10_000_000),
    "pressure_advance": (0, 2), "filament_retraction_length": (0, 10),
}
SPEC_KEYS = ["nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
             "nozzle_temperature_range_high", "textured_plate_temp", "hot_plate_temp", "cool_plate_temp",
             "eng_plate_temp", "filament_max_volumetric_speed", "filament_flow_ratio", "fan_min_speed",
             "fan_max_speed", "filament_density"]
STRIP_ON_IMPORT = {"from", "version", "setting_id", "base_id", "user_id", "updated_time", "is_custom_defined",
                   "filament_settings_id", "name", "inherits", "instantiation", "type", "compatible_prints",
                   "compatible_prints_condition"}


def migrate(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS filament_library (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, vendor TEXT, filament_type TEXT,
        inherits TEXT, body TEXT NOT NULL, filament_id TEXT, source TEXT NOT NULL,
        notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def _resolve(presets: dict, name: str, seen: tuple = ()) -> dict:
    data = presets.get(name)
    if not data or name in seen or len(seen) > 12:
        return {}
    return {**_resolve(presets, data.get("inherits") or "", (*seen, name)), **data}


def model_of(printer_model: str | None) -> str:
    text = str(printer_model or "").strip()
    if text and not text.lower().startswith("bambu lab"):
        text = "Bambu Lab " + text
    return text


def compatible(resolved: dict, printer_model: str, nozzle: float | None = None) -> bool:
    """Preset lists this printer model (and nozzle size when given) in compatible_printers."""
    wanted = model_of(printer_model).lower()
    for printer in resolved.get("compatible_printers") or []:
        found = re.fullmatch(r"(.*?) (\d+(?:\.\d+)?) nozzle", str(printer))
        model, size = (found.group(1), float(found.group(2))) if found else (str(printer), None)
        if model.lower() == wanted and (nozzle is None or size is None or abs(size - nozzle) < 1e-6):
            return True
    return False


def specs(resolved: dict) -> dict:
    out = {}
    for key in SPEC_KEYS:
        value = insight.number(resolved.get(key))
        if value is not None:
            out[key] = value
    return out


def _known_keys(root: str | os.PathLike) -> set[str]:
    path = Path(root) / "src" / "libslic3r" / "PrintConfig.cpp"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    keys = set(re.findall(r'this->add(?:_nullable)?\("([A-Za-z0-9_]+)"', source))
    for generated in ("filament_extruder_override_keys", "filament_overhang_override_keys"):
        block = re.search(generated + r" = \{(.*?)\};", source, re.S)
        if block:
            keys.update(re.findall(r'"(filament_[a-z0-9_]+)"', block.group(1)))
    return keys


_KNOWN: dict[str, set[str]] = {}


def known_keys(root) -> set[str]:
    key = str(root)
    if key not in _KNOWN:
        _KNOWN[key] = _known_keys(root)
    return _KNOWN[key]


def library_rows(db) -> list[dict]:
    rows = db.execute("SELECT * FROM filament_library ORDER BY vendor, name").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["body"] = json.loads(item["body"] or "{}")
        out.append(item)
    return out


def library_presets(db) -> dict[str, dict]:
    """Library presets as Studio preset dicts (for resolving profiles during analysis)."""
    return {row["name"]: {"name": row["name"], "inherits": row["inherits"] or "",
                          "filament_vendor": [row["vendor"] or ""], **row["body"]} for row in library_rows(db)}


def catalogue(root, printer_model: str, db, nozzle: float | None = 0.4) -> dict:
    """Presets usable on printer_model (with that nozzle) grouped by material type, plus the library."""
    presets = insight.filament_profiles(root)
    groups: dict[str, list] = {}
    vendors: dict[str, int] = {}
    for name, data in presets.items():
        if data.get("instantiation") != "true":
            continue
        resolved = _resolve(presets, name)
        if not compatible(resolved, printer_model, nozzle):
            continue
        vendor = str(insight.scalar(resolved.get("filament_vendor")) or "Generic")
        ftype = str(insight.scalar(resolved.get("filament_type")) or "Khác")
        vendors[vendor] = vendors.get(vendor, 0) + 1
        groups.setdefault(ftype, []).append({
            "name": name, "vendor": vendor, "type": ftype, "source": "system",
            "in_stock_studio": vendor not in ADDED_VENDORS, "exportable": vendor in ADDED_VENDORS,
            "base": data.get("inherits") if vendor in ADDED_VENDORS else None, "specs": specs(resolved)})
    for row in library_rows(db):
        merged = {**presets, row["name"]: {"inherits": row["inherits"] or "", **row["body"]}}
        resolved = _resolve(merged, row["name"])
        if row["inherits"] and not compatible(resolved, printer_model, nozzle):
            continue
        ftype = row["filament_type"] or str(insight.scalar(resolved.get("filament_type")) or "Khác")
        vendor = row["vendor"] or "Khác"
        vendors[vendor] = vendors.get(vendor, 0) + 1
        groups.setdefault(ftype, []).append({
            "id": row["id"], "name": row["name"], "vendor": vendor, "type": ftype, "source": row["source"],
            "in_stock_studio": False, "exportable": True, "base": row["inherits"], "notes": row["notes"],
            "specs": specs(resolved), "updated_at": row["updated_at"]})
    types = []
    for ftype in sorted(groups, key=lambda t: (TYPE_ORDER.get(t, 99), t)):
        label, desc = TYPE_TEXT.get(ftype, (ftype, ""))
        poly = insight.polymer_of(ftype)
        knowledge = insight.MATERIALS.get(poly or "", {})
        # own presets first, then ones PrintPilot adds (not in stock Studio), stock vendors, Generic last
        items = sorted(groups[ftype], key=lambda p: (0 if p["source"] != "system" else 1 if p["exportable"] else
                                                     3 if p["vendor"] == "Generic" else 2, p["vendor"].lower(), p["name"].lower()))
        types.append({"type": ftype, "label": label, "description": desc, "popular": ftype in TYPE_ORDER,
                      "hygroscopic": knowledge.get("hygroscopic"), "needs_enclosure": knowledge.get("enclosure"),
                      "abrasive": bool(re.search(r"(CF|GF)$", ftype)), "notes": knowledge.get("notes", []),
                      "presets": items})
    bases = sorted(p["name"] for t in types for p in t["presets"] if p["source"] == "system" and p["in_stock_studio"])
    return {"printer_model": model_of(printer_model), "nozzle": nozzle, "types": types,
            "vendors": [{"vendor": v, "presets": n} for v, n in sorted(vendors.items(), key=lambda kv: (-kv[1], kv[0]))],
            "bases": bases}


def base_specs(root, base: str) -> dict | None:
    presets = insight.filament_profiles(root)
    if base not in presets:
        return None
    resolved = _resolve(presets, base)
    return {"base": base, "type": insight.scalar(resolved.get("filament_type")),
            "vendor": insight.scalar(resolved.get("filament_vendor")), "specs": specs(resolved)}


# ---------------------------------------------------------------------------
# Creating and importing library presets
# ---------------------------------------------------------------------------

def clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or _BAD_NAME.search(name):
        raise ValueError("Tên preset 1–120 ký tự, không chứa \\ / : * ? \" < > |")
    return re.sub(r"\s+", " ", name.strip())


def _check_new_name(root, db, name: str, replace_id: str | None = None) -> None:
    if name in insight.filament_profiles(root):
        raise ValueError(f"«{name}» trùng tên preset hệ thống; hãy đặt tên khác.")
    row = db.execute("SELECT id FROM filament_library WHERE name=?", (name,)).fetchone()
    if row and row["id"] != replace_id:
        raise ValueError(f"Thư viện đã có preset «{name}».")


def create_custom(root, db, data: dict) -> dict:
    """Preset from a form: vendor values on top of an installed Bambu base preset."""
    name = clean_name(data.get("name"))
    vendor = str(data.get("vendor") or "").strip()[:60]
    if not vendor or _BAD_NAME.search(vendor):
        raise ValueError("Cần tên hãng nhựa hợp lệ.")
    base = data.get("base")
    presets = insight.filament_profiles(root)
    if not isinstance(base, str) or base not in presets or presets[base].get("instantiation") != "true":
        raise ValueError("Preset gốc không có trong Bambu Studio.")
    resolved = _resolve(presets, base)
    values = data.get("values") or {}
    if not isinstance(values, dict):
        raise ValueError("values phải là object")
    body: dict[str, Any] = {}
    for key, raw in values.items():
        if key not in CUSTOM_KEYS:
            raise ValueError(f"Không hỗ trợ thông số {key}")
        if raw in (None, ""):
            continue
        low, high = CUSTOM_KEYS[key]
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} phải là số") from exc
        if not low <= number <= high:
            raise ValueError(f"{key} phải trong khoảng {low:g}–{high:g}")
        current = insight.number(resolved.get(key))
        if current is not None and abs(current - number) < 1e-9:
            continue
        body[key] = [f"{number:g}"]
    low = insight.number(body.get("nozzle_temperature_range_low") or resolved.get("nozzle_temperature_range_low"))
    high = insight.number(body.get("nozzle_temperature_range_high") or resolved.get("nozzle_temperature_range_high"))
    temp = insight.number(body.get("nozzle_temperature") or resolved.get("nozzle_temperature"))
    if low and high and (low > high or (temp and not low <= temp <= high)):
        raise ValueError("Nhiệt đầu phun phải nằm trong khoảng nhiệt thấp–cao.")
    if "pressure_advance" in body:
        body["enable_pressure_advance"] = ["1"]
    colour = data.get("colour")
    if colour:
        if not isinstance(colour, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour):
            raise ValueError("Màu phải dạng #RRGGBB")
        body["default_filament_colour"] = [colour.upper()]
    notes = data.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > 1000:
        raise ValueError("Ghi chú tối đa 1000 ký tự")
    if notes:
        body["filament_notes"] = notes
    replace_id = data.get("id") if isinstance(data.get("id"), str) else None
    _check_new_name(root, db, name, replace_id)
    return _store(db, name=name, vendor=vendor, ftype=insight.scalar(resolved.get("filament_type")), inherits=base,
                  body=body, filament_id=resolved.get("filament_id"), source="custom", notes=notes, replace_id=replace_id)


def _store(db, *, name, vendor, ftype, inherits, body, filament_id, source, notes, replace_id=None) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if replace_id:
        if not db.execute("SELECT 1 FROM filament_library WHERE id=?", (replace_id,)).fetchone():
            raise LookupError("Preset not found")
        db.execute("UPDATE filament_library SET name=?,vendor=?,filament_type=?,inherits=?,body=?,filament_id=?,notes=?,updated_at=? WHERE id=?",
                   (name, vendor, ftype, inherits, json.dumps(body), filament_id, notes, now, replace_id))
        entry_id = replace_id
    else:
        entry_id = str(uuid.uuid4())
        db.execute("INSERT INTO filament_library (id,name,vendor,filament_type,inherits,body,filament_id,source,notes,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (entry_id, name, vendor, ftype, inherits, json.dumps(body), filament_id, source, notes, now, now))
    db.commit()
    return {"id": entry_id, "name": name, "vendor": vendor, "type": ftype, "base": inherits, "source": source}


def _value_ok(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= 4000
    if isinstance(value, list):
        return len(value) <= 64 and all(isinstance(v, str) and len(v) <= 400 for v in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_upload(filename: str, raw: bytes) -> list[dict]:
    """Preset dicts from a Studio .json preset or a .bbsflmt/.zip bundle."""
    if len(raw) > MAX_UPLOAD:
        raise ValueError("File quá lớn (tối đa 8 MB)")
    lower = filename.lower()
    found: list[dict] = []
    if lower.endswith(".json"):
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("File JSON không hợp lệ") from exc
        found = data if isinstance(data, list) else [data]
    elif lower.endswith((".bbsflmt", ".zip")):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                if sum(i.file_size for i in zf.infolist()) > 32 * MAX_UPLOAD:
                    raise ValueError("Bundle giải nén quá lớn")
                for info in zf.infolist():
                    if not info.filename.lower().endswith(".json") or info.filename.endswith("bundle_structure.json"):
                        continue
                    try:
                        found.append(json.loads(zf.read(info).decode("utf-8-sig")))
                    except (UnicodeDecodeError, ValueError):
                        continue
        except zipfile.BadZipFile as exc:
            raise ValueError("Bundle không phải file zip hợp lệ") from exc
    else:
        raise ValueError("Chỉ nhận file .json, .bbsflmt hoặc .zip xuất từ Bambu Studio")
    presets = [p for p in found if isinstance(p, dict) and isinstance(p.get("name"), str)]
    if not presets:
        raise ValueError("Không tìm thấy preset nhựa nào trong file")
    if len(presets) > MAX_PRESETS_PER_UPLOAD:
        raise ValueError(f"Tối đa {MAX_PRESETS_PER_UPLOAD} preset mỗi lần nhập")
    return presets


def import_presets(root, db, filename: str, raw: bytes) -> dict:
    """Store uploaded filament presets; returns {imported:[...], skipped:[{name, reason}]}."""
    presets = insight.filament_profiles(root)
    known = known_keys(root)
    imported, skipped = [], []
    for data in parse_upload(filename, raw):
        name = data.get("name", "")
        try:
            name = clean_name(name)
            if not any(k in data for k in ("filament_settings_id", "filament_type", "filament_vendor")) and \
                    not (data.get("inherits") in presets):
                raise ValueError("không phải preset nhựa")
            inherits = data.get("inherits") or ""
            if inherits and inherits not in presets:
                raise ValueError(f"preset gốc «{inherits}» không có trong Bambu Studio")
            if not inherits and not insight.scalar(data.get("filament_type")):
                raise ValueError("preset gốc tự do phải có filament_type")
            body = {k: v for k, v in data.items() if k not in STRIP_ON_IMPORT and k != "filament_id"
                    and (not known or k in known) and _value_ok(v)}
            resolved = _resolve({**presets, name: {"inherits": inherits, **body}}, name)
            vendor = str(insight.scalar(data.get("filament_vendor")) or insight.scalar(resolved.get("filament_vendor")) or "Khác")[:60]
            existing = db.execute("SELECT id FROM filament_library WHERE name=?", (name,)).fetchone()
            if name in presets:
                raise ValueError("trùng tên preset hệ thống")
            fid = resolved.get("filament_id") if inherits else (data.get("filament_id") if isinstance(data.get("filament_id"), str) else None)
            imported.append(_store(db, name=name, vendor=vendor, ftype=insight.scalar(resolved.get("filament_type")),
                                   inherits=inherits, body=body, filament_id=fid, source="import",
                                   notes=f"Nhập từ {Path(filename).name}", replace_id=existing["id"] if existing else None))
        except ValueError as exc:
            skipped.append({"name": str(name)[:120], "reason": str(exc)})
    return {"imported": imported, "skipped": skipped}


def delete(db, entry_id: str) -> bool:
    cur = db.execute("DELETE FROM filament_library WHERE id=?", (entry_id,))
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Export for Bambu Studio
# ---------------------------------------------------------------------------

def user_preset(name: str, inherits: str, body: dict, vendor: str, filament_id: str | None) -> dict:
    """A Studio user preset (what Studio writes for a preset saved from a system one)."""
    preset = {"name": name, "inherits": inherits, "from": "User", "version": STUDIO_VERSION,
              "filament_settings_id": [name], "filament_vendor": [vendor]}
    if filament_id:
        preset["filament_id"] = filament_id
    preset.update({k: v for k, v in body.items() if k not in ("filament_vendor", "filament_settings_id")})
    return preset


def export_items(root, db, names: list[str], ids: list[str]) -> list[dict]:
    presets = insight.filament_profiles(root)
    out = []
    for name in names:
        data = presets.get(name)
        vendor = str(insight.scalar((data or {}).get("filament_vendor")) or "")
        if not data or vendor not in ADDED_VENDORS:
            raise ValueError(f"«{name}» không cần xuất (đã có sẵn trong Bambu Studio) hoặc không tồn tại")
        body = {k: v for k, v in data.items() if k not in {"type", "name", "inherits", "from", "setting_id", "instantiation", "filament_vendor"}}
        resolved = _resolve(presets, name)
        out.append(user_preset(name, data["inherits"], body, vendor, resolved.get("filament_id")))
    for entry_id in ids:
        row = db.execute("SELECT * FROM filament_library WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise LookupError(f"Không có preset {entry_id}")
        inherits, body = row["inherits"] or "", json.loads(row["body"])
        parent = presets.get(inherits)
        if parent and str(insight.scalar(parent.get("filament_vendor")) or "") in ADDED_VENDORS:
            # stock Studio lacks the PrintPilot-only parent: carry its values and inherit its Bambu base
            meta = {"type", "name", "inherits", "from", "setting_id", "instantiation", "filament_vendor"}
            body = {**{k: v for k, v in parent.items() if k not in meta}, **body}
            inherits = parent.get("inherits") or ""
        out.append(user_preset(row["name"], inherits, body, row["vendor"] or "Khác", row["filament_id"]))
    if not out:
        raise ValueError("Chưa chọn preset nào")
    return out


def bundle(items: list[dict]) -> bytes:
    """Filament config bundle (.bbsflmt) in Studio's own export layout."""
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    by_vendor: dict[str, list[str]] = {}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        used = set()
        for item in items:
            vendor = _BAD_NAME.sub("_", str(insight.scalar(item.get("filament_vendor")) or "PrintPilot"))
            path = vendor + "/" + _BAD_NAME.sub("_", item["name"]) + ".json"
            if path in used:
                continue
            used.add(path)
            zf.writestr(path, json.dumps(item, indent=4, ensure_ascii=False))
            by_vendor.setdefault(vendor, []).append(path)
        structure = {"bundle_id": f"printpilot_{stamp}", "bundle_type": "filament config bundle",
                     "filament_name": items[0]["name"] if len(items) == 1 else "PrintPilot filaments",
                     "filament_vendor": [{"vendor": v, "filament_path": p} for v, p in by_vendor.items()],
                     "version": STUDIO_VERSION}
        zf.writestr("bundle_structure.json", json.dumps(structure, indent=4, ensure_ascii=False))
    return out.getvalue()
