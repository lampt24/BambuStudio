"""Evidence the AI analysis is grounded on.

* focused_settings: the settings that decide print quality, never cut by a size cap;
* object_metrics: per-object shape numbers (height, bed contact, overhang area by angle, ...);
* render_views: six orthographic/iso views with a mm grid and an overhang heat map (PNG);
* material/printer knowledge from installed Bambu profiles plus a small curated table;
* evaluate: deterministic print-risk rules whose findings are facts, not AI guesses;
* shape_signature / similar_feedback: reviews of other models with a similar shape.

Python stdlib only.
"""
from __future__ import annotations

import array
import json
import math
import os
import re
import struct
import zlib
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Settings that matter for quality (grouped; always sent, in this order)
# ---------------------------------------------------------------------------

PRIORITY_KEYS: dict[str, list[str]] = {
    "printer": ["printer_model", "printer_settings_id", "printer_structure", "nozzle_diameter", "nozzle_type",
                "nozzle_hrc", "curr_bed_type", "printer_variant"],
    "process": ["print_settings_id", "layer_height", "initial_layer_print_height", "line_width",
                "outer_wall_line_width", "inner_wall_line_width", "sparse_infill_line_width",
                "wall_loops", "top_shell_layers", "bottom_shell_layers", "top_shell_thickness",
                "bottom_shell_thickness", "sparse_infill_density", "sparse_infill_pattern",
                "top_surface_pattern", "wall_generator", "wall_sequence", "seam_position",
                "detect_thin_wall", "only_one_wall_top", "elefant_foot_compensation",
                "xy_hole_compensation", "xy_contour_compensation", "ironing_type", "print_sequence"],
    "support_adhesion": ["enable_support", "support_type", "support_style", "support_threshold_angle",
                         "support_on_build_plate_only", "support_top_z_distance", "support_interface_top_layers",
                         "brim_type", "brim_width", "brim_object_gap", "skirt_loops", "raft_layers",
                         "enable_prime_tower"],
    "speed_accel": ["outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
                    "top_surface_speed", "initial_layer_speed", "initial_layer_infill_speed", "gap_infill_speed",
                    "bridge_speed", "travel_speed", "enable_overhang_speed", "overhang_1_4_speed",
                    "overhang_2_4_speed", "overhang_3_4_speed", "overhang_4_4_speed", "default_acceleration",
                    "outer_wall_acceleration", "inner_wall_acceleration", "top_surface_acceleration",
                    "initial_layer_acceleration", "bridge_flow"],
    "filament": ["filament_settings_id", "filament_type", "filament_vendor", "filament_colour", "filament_density",
                 "filament_flow_ratio", "filament_max_volumetric_speed", "nozzle_temperature",
                 "nozzle_temperature_initial_layer", "nozzle_temperature_range_low", "nozzle_temperature_range_high",
                 "hot_plate_temp", "hot_plate_temp_initial_layer", "textured_plate_temp",
                 "textured_plate_temp_initial_layer", "cool_plate_temp", "eng_plate_temp", "supertack_plate_temp",
                 "temperature_vitrification", "required_nozzle_HRC", "pressure_advance", "enable_pressure_advance",
                 "chamber_temperatures"],
    "cooling_retraction": ["fan_min_speed", "fan_max_speed", "fan_cooling_layer_time", "slow_down_layer_time",
                           "slow_down_min_speed", "overhang_fan_speed", "overhang_fan_threshold",
                           "close_fan_the_first_x_layers", "additional_cooling_fan_speed", "reduce_fan_stop_start_freq",
                           "retraction_length", "retraction_speed", "z_hop", "filament_retraction_length",
                           "filament_z_hop", "wipe"],
}
_SECRET = re.compile(r"token|secret|password|passwd|access.code|api.key|auth|credential|private.key|wifi|ssid", re.I)


def _trim(value: Any) -> Any:
    if isinstance(value, list):
        return [_trim(v) for v in value[:8]]
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def focused_settings(*sources: dict) -> dict[str, dict]:
    """Quality-relevant settings by group; later sources (Studio's effective config) win."""
    merged: dict[str, Any] = {}
    for src in sources:
        if isinstance(src, dict):
            merged.update({k: v for k, v in src.items() if isinstance(k, str) and not _SECRET.search(k)})
    out: dict[str, dict] = {}
    for group, keys in PRIORITY_KEYS.items():
        values = {k: _trim(merged[k]) for k in keys if k in merged and merged[k] not in (None, "", [])}
        if values:
            out[group] = values
    return out


def as_list(value: Any) -> list:
    """Vector option as a list: JSON list, Studio string vector ('"PLA";"PETG"') or number vector ('245,220')."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if ";" in value or value.startswith('"'):
            return [p.strip().strip('"') for p in value.split(";")]
        if "," in value and not value.strip().startswith("#"):
            return [p.strip() for p in value.split(",")]
    return [value]


def scalar(value: Any, index: int = 0) -> Any:
    """index-th element (else the first) of per-filament / per-extruder vectors."""
    items = as_list(value)
    if not items:
        return None
    return items[index] if len(items) > index else items[0]


def number(value: Any, index: int = 0) -> float | None:
    value = scalar(value, index)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        value = value.strip().rstrip("%")
        if value in ("", "nil"):
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# ---------------------------------------------------------------------------
# Per-object shape metrics
# ---------------------------------------------------------------------------

NZ_45 = -math.sin(math.radians(45))   # face normal z below this: overhang steeper than 45 deg from vertical
NZ_60 = -math.sin(math.radians(60))
NZ_CEILING = -0.985                    # near-horizontal downward faces: bridges / ceilings


def _internal_faces(v, t) -> set[int]:
    """Triangle indices shared by two touching parts (same vertices, any order): not real surface."""
    seen: dict[tuple, int] = {}
    internal: set[int] = set()
    for i in range(0, len(t), 3):
        key = tuple(sorted((round(v[t[i + k] * 3], 3), round(v[t[i + k] * 3 + 1], 3), round(v[t[i + k] * 3 + 2], 3))
                           for k in range(3)))
        other = seen.get(key)
        if other is None:
            seen[key] = i
        else:
            internal.add(i)
            internal.add(other)
    return internal


def object_metrics(obj: dict) -> dict[str, Any]:
    """Shape numbers of one placed object (vertices already in build-plate coordinates, mm).

    Multi-part objects (one part per colour) touch each other: faces two parts share, and
    horizontal faces lying on another part's top face, are interfaces, not overhangs.
    """
    v, t = obj["verts"], obj["tris"]
    zs = v[2::3]
    xs, ys = v[0::3], v[1::3]
    if not zs:
        return {"name": obj["name"], "label": obj["label"], "empty": True}
    zmin, zmax = min(zs), max(zs)
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    internal = _internal_faces(v, t)
    contact = over45 = over60 = surface = vol6 = 0.0
    cx0 = cy0 = math.inf
    cx1 = cy1 = -math.inf
    lowest60 = None
    down_levels: dict[int, float] = {}   # horizontal downward faces (not on the bed) by z level
    up_levels: dict[int, float] = {}     # horizontal upward faces by z level
    contact_z = zmin + 0.05
    for i in range(0, len(t), 3):
        a, b, c = t[i] * 3, t[i + 1] * 3, t[i + 2] * 3
        ax, ay, az = v[a], v[a + 1], v[a + 2]
        bx, by, bz = v[b], v[b + 1], v[b + 2]
        cx, cy, cz = v[c], v[c + 1], v[c + 2]
        vol6 += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
        if i in internal:
            continue
        ux, uy, uz = bx - ax, by - ay, bz - az
        wx, wy, wz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if not length:
            continue
        area = length / 2
        surface += area
        nzn = nz / length
        if nzn > -NZ_CEILING:
            level = round((az + bz + cz) / 3 / 0.02)
            up_levels[level] = up_levels.get(level, 0.0) + area
            continue
        if nzn >= NZ_45:
            continue
        top = az if az > bz else bz
        top = top if top > cz else cz
        if nzn < -0.99 and top <= contact_z:
            contact += area
            cx0, cx1 = min(cx0, ax, bx, cx), max(cx1, ax, bx, cx)
            cy0, cy1 = min(cy0, ay, by, cy), max(cy1, ay, by, cy)
            continue
        if nzn < NZ_CEILING:
            level = round((az + bz + cz) / 3 / 0.02)
            down_levels[level] = down_levels.get(level, 0.0) + area
            continue
        over45 += area
        if nzn < NZ_60:
            over60 += area
            low = min(az, bz, cz) - zmin
            if low > 0.3 and (lowest60 is None or low < lowest60):
                lowest60 = low
    ceiling = 0.0
    for level, down in down_levels.items():
        resting = max(up_levels.get(level + d, 0.0) for d in (-1, 0, 1))
        residual = down - resting  # a colour layer lying on the part below is no bridge
        if residual > 0.5:
            ceiling += residual
            surface -= min(down, resting) * 2
            low = level * 0.02 - zmin
            if low > 0.3 and (lowest60 is None or low < lowest60):
                lowest60 = low
        else:
            surface -= down * 2
    over45 += ceiling
    over60 += ceiling
    height = zmax - zmin
    dx, dy = xmax - xmin, ymax - ymin
    contact_dims = (cx1 - cx0, cy1 - cy0) if contact else (0.0, 0.0)
    base = min(contact_dims) if contact else min(dx, dy)
    volume = abs(vol6) / 6
    surface = max(surface, 1e-9)
    r = lambda x, n=1: round(x, n)
    return {
        "label": obj["label"], "name": obj["name"],
        "size_mm": [r(dx), r(dy), r(height)],
        "bottom_z_mm": r(zmin, 2),
        "position_mm": [r((xmin + xmax) / 2), r((ymin + ymax) / 2)],
        "bed_contact_mm2": r(contact),
        "bed_contact_dims_mm": [r(contact_dims[0]), r(contact_dims[1])],
        "bed_contact_ratio": r(contact / (dx * dy), 3) if dx * dy else 0,
        "slenderness": r(height / max(base, 0.1), 2),  # height / narrowest base side
        "surface_mm2": r(surface),
        "solid_volume_cm3": r(volume / 1000, 2),
        "mean_thickness_mm": r(2 * volume / surface, 2),
        "overhang_45_mm2": r(over45), "overhang_60_mm2": r(over60), "ceiling_mm2": r(ceiling),
        "overhang_45_pct": r(100 * over45 / surface, 1),
        "overhang_60_pct": r(100 * over60 / surface, 1),
        "lowest_steep_overhang_mm": r(lowest60, 1) if lowest60 is not None else None,
        "triangles": len(t) // 3,
        "filaments": sorted(obj.get("filaments") or []),
    }


def plate_summary(metrics: list[dict]) -> dict[str, Any]:
    real = [m for m in metrics if not m.get("empty")]
    if not real:
        return {"objects": 0}
    gaps = []
    for i, a in enumerate(real):
        for b in real[i + 1:]:
            ax, ay, bx, by = a["position_mm"] + b["position_mm"]
            dist = math.hypot(ax - bx, ay - by) - (max(a["size_mm"][:2]) + max(b["size_mm"][:2])) / 2
            gaps.append(dist)
    return {"objects": len(real),
            "max_height_mm": max(m["size_mm"][2] for m in real),
            "min_gap_mm": round(min(gaps), 1) if gaps else None,
            "total_solid_volume_cm3": round(sum(m["solid_volume_cm3"] for m in real), 2)}


# ---------------------------------------------------------------------------
# Rendering (orthographic, z-buffer, flat shading) -> PNG
# ---------------------------------------------------------------------------

OBJECT_COLORS = [(170, 186, 200), (196, 182, 160), (170, 200, 170), (200, 170, 196), (160, 196, 200), (205, 200, 160)]
OVER45_COLOR, OVER60_COLOR, CONTACT_COLOR = (245, 160, 40), (225, 55, 45), (60, 120, 225)
BG, GRID, GRID_AXIS = (250, 250, 250), (222, 226, 230), (170, 176, 184)
VIEWS = [  # title, forward (camera -> scene), up
    ("ISO", (1.0, 1.0, -0.8), (0, 0, 1)),
    ("FRONT", (0, 1, 0), (0, 0, 1)),
    ("RIGHT", (-1, 0, 0), (0, 0, 1)),
    ("BACK", (0, -1, 0), (0, 0, 1)),
    ("TOP", (0, 0, -1), (0, 1, 0)),
    ("BOTTOM", (0, 0, 1), (0, 1, 0)),
]

# 5x7 glyphs, rows top->bottom, 5 bits each (MSB = left)
_FONT = {
    "A": "0E 11 11 1F 11 11 11", "B": "1E 11 11 1E 11 11 1E", "C": "0E 11 10 10 10 11 0E", "D": "1E 11 11 11 11 11 1E",
    "E": "1F 10 10 1E 10 10 1F", "F": "1F 10 10 1E 10 10 10", "G": "0E 11 10 17 11 11 0F", "H": "11 11 11 1F 11 11 11",
    "I": "0E 04 04 04 04 04 0E", "J": "07 02 02 02 02 12 0C", "K": "11 12 14 18 14 12 11", "L": "10 10 10 10 10 10 1F",
    "M": "11 1B 15 15 11 11 11", "N": "11 11 19 15 13 11 11", "O": "0E 11 11 11 11 11 0E", "P": "1E 11 11 1E 10 10 10",
    "Q": "0E 11 11 11 15 12 0D", "R": "1E 11 11 1E 14 12 11", "S": "0F 10 10 0E 01 01 1E", "T": "1F 04 04 04 04 04 04",
    "U": "11 11 11 11 11 11 0E", "V": "11 11 11 11 11 0A 04", "W": "11 11 11 15 15 15 0A", "X": "11 11 0A 04 0A 11 11",
    "Y": "11 11 0A 04 04 04 04", "Z": "1F 01 02 04 08 10 1F", "0": "0E 11 13 15 19 11 0E", "1": "04 0C 04 04 04 04 0E",
    "2": "0E 11 01 02 04 08 1F", "3": "1F 02 04 02 01 11 0E", "4": "02 06 0A 12 1F 02 02", "5": "1F 10 1E 01 01 11 0E",
    "6": "06 08 10 1E 11 11 0E", "7": "1F 01 02 04 08 08 08", "8": "0E 11 11 0E 11 11 0E", "9": "0E 11 11 0F 01 02 0C",
    " ": "00 00 00 00 00 00 00", "-": "00 00 00 1F 00 00 00", ".": "00 00 00 00 00 0C 0C", ":": "00 0C 0C 00 0C 0C 00",
    "#": "0A 0A 1F 0A 1F 0A 0A", "/": "01 01 02 04 08 10 10", ">": "08 04 02 01 02 04 08", "(": "02 04 08 08 08 04 02",
    ")": "08 04 02 02 02 04 08", "%": "18 19 02 04 08 13 03", "=": "00 00 1F 00 1F 00 00",
}


class _Canvas:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.px = bytearray(bytes(BG) * (width * height))

    def set(self, x: int, y: int, color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(color)

    def rect(self, x0, y0, x1, y1, color) -> None:
        row = bytes(color) * max(0, min(x1, self.w) - max(x0, 0))
        for y in range(max(y0, 0), min(y1, self.h)):
            i = (y * self.w + max(x0, 0)) * 3
            self.px[i:i + len(row)] = row

    def text(self, x: int, y: int, text: str, color=(40, 44, 52), scale: int = 2, box=None) -> None:
        text = text.upper()
        if box:
            self.rect(x - 3, y - 3, x + len(text) * 6 * scale + 1, y + 7 * scale + 3, box)
        for k, ch in enumerate(text):
            rows = _FONT.get(ch, _FONT[" "]).split()
            for ry, hexrow in enumerate(rows):
                bits = int(hexrow, 16)
                for rx in range(5):
                    if bits & (16 >> rx):
                        for sy in range(scale):
                            for sx in range(scale):
                                self.set(x + (k * 6 + rx) * scale + sx, y + ry * scale + sy, color)

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _norm(v):
    length = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / length for c in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _cluster(v, t, cell: float):
    """Vertex-clustering decimation to about one pixel: only for drawing, metrics use the full mesh."""
    keep: dict[tuple, int] = {}
    remap = array.array("I", bytes(4 * (len(v) // 3)))
    out_v = array.array("f")
    inv = 1.0 / cell
    for i in range(len(v) // 3):
        key = (int(v[3 * i] * inv), int(v[3 * i + 1] * inv), int(v[3 * i + 2] * inv))
        j = keep.get(key)
        if j is None:
            j = keep[key] = len(out_v) // 3
            out_v.extend((v[3 * i], v[3 * i + 1], v[3 * i + 2]))
        remap[i] = j
    out_t, kept = array.array("I"), []
    seen = set()
    for f in range(len(t) // 3):
        a, b, c = remap[t[3 * f]], remap[t[3 * f + 1]], remap[t[3 * f + 2]]
        if a == b or b == c or a == c:
            continue
        key = (a, b, c) if a < b and a < c else ((b, c, a) if b < c else (c, a, b))
        if key in seen:
            continue
        seen.add(key)
        out_t.extend((a, b, c))
        kept.append(f)
    return out_v, out_t, kept


def render_views(scene: list[dict], panel: int = 320, colors: list | None = None) -> bytes:
    """2x3 sheet: ISO, FRONT, RIGHT / BACK, TOP, BOTTOM with the same mm scale in every panel.

    Faces steeper than 45/60 degrees from vertical are orange/red, faces on the bed blue,
    object numbers match the labels in the metrics table; grid spacing is printed per panel.
    Painter's algorithm with back-face culling (meshes are closed), spans filled per scanline.
    colors: RGB per 1-based filament slot (project filament colours) for faces that are not
    overhang/contact; falls back to one colour per object.
    """
    xs = [x for o in scene for x in o["verts"][0::3]]
    if not xs:
        raise ValueError("nothing to render")
    ys = [y for o in scene for y in o["verts"][1::3]]
    zs = [z for o in scene for z in o["verts"][2::3]]
    lo, hi = [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]
    del xs, ys, zs
    center = [(lo[k] + hi[k]) / 2 for k in range(3)]
    diag = math.sqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3))) or 1.0
    scale = panel * 0.82 / diag
    grid = next((g for g in (1, 2, 5, 10, 20, 50, 100) if diag / g <= 24), 200)
    cols, header = 3, 26
    canvas = _Canvas(panel * cols, (panel + header) * 2)

    # Flatten all objects; per face: 3 vertex indices, unit normal, colour class
    vx, vy, vz = array.array("f"), array.array("f"), array.array("f")
    faces = []
    cell = diag / panel
    for oi, obj in enumerate(scene):
        v, t, kept = _cluster(obj["verts"], obj["tris"], cell)
        face_fil = obj.get("tri_filaments")
        base = len(vx)
        vx.extend(v[0::3]); vy.extend(v[1::3]); vz.extend(v[2::3])
        zmin = min(v[2::3]) if v else 0
        own = OBJECT_COLORS[oi % len(OBJECT_COLORS)]
        for i in range(0, len(t), 3):
            a, b, c = t[i] + base, t[i + 1] + base, t[i + 2] + base
            ux, uy, uz = vx[b] - vx[a], vy[b] - vy[a], vz[b] - vz[a]
            wx, wy, wz = vx[c] - vx[a], vy[c] - vy[a], vz[c] - vz[a]
            nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if not length:
                continue
            nx, ny, nz = nx / length, ny / length, nz / length
            if nz < -0.99 and max(vz[a], vz[b], vz[c]) <= zmin + 0.05:
                kind = CONTACT_COLOR
            elif nz < NZ_60:
                kind = OVER60_COLOR
            elif nz < NZ_45:
                kind = OVER45_COLOR
            elif colors and face_fil is not None:
                slot = face_fil[kept[i // 3]]
                kind = colors[slot - 1] if 0 < slot <= len(colors) else own
            else:
                kind = own
            faces.append((a, b, c, nx, ny, nz, kind))

    shades: dict[tuple, bytes] = {}
    for vi, (title, forward, up) in enumerate(VIEWS):
        f = _norm(forward)
        r = _norm(_cross(f, up))
        u = _cross(r, f)
        ox, oy = (vi % cols) * panel, (vi // cols) * (panel + header) + header
        half = panel / 2
        px, width = canvas.px, canvas.w
        cx0, cy0, cz0 = center
        sx = [half + ((x - cx0) * r[0] + (y - cy0) * r[1] + (z - cz0) * r[2]) * scale for x, y, z in zip(vx, vy, vz)]
        sy = [half - ((x - cx0) * u[0] + (y - cy0) * u[1] + (z - cz0) * u[2]) * scale for x, y, z in zip(vx, vy, vz)]
        sd = [(x - cx0) * f[0] + (y - cy0) * f[1] + (z - cz0) * f[2] for x, y, z in zip(vx, vy, vz)]

        def project(p):
            d = (p[0] - cx0, p[1] - cy0, p[2] - cz0)
            return (half + (d[0] * r[0] + d[1] * r[1] + d[2] * r[2]) * scale,
                    half - (d[0] * u[0] + d[1] * u[1] + d[2] * u[2]) * scale)

        # mm grid on the bed plane (and a vertical one for side views), drawn first
        g0x, g1x = math.floor(lo[0] / grid - 2) * grid, math.ceil(hi[0] / grid + 2) * grid
        g0y, g1y = math.floor(lo[1] / grid - 2) * grid, math.ceil(hi[1] / grid + 2) * grid
        lines = [((x, g0y, 0), (x, g1y, 0)) for x in _frange(g0x, g1x, grid)] + \
                [((g0x, y, 0), (g1x, y, 0)) for y in _frange(g0y, g1y, grid)]
        if title in ("FRONT", "BACK"):
            lines += [((g0x, center[1], z), (g1x, center[1], z)) for z in _frange(0, math.ceil(hi[2] / grid + 1) * grid, grid)]
        elif title == "RIGHT":
            lines += [((center[0], g0y, z), (center[0], g1y, z)) for z in _frange(0, math.ceil(hi[2] / grid + 1) * grid, grid)]
        for a3, b3 in lines:
            axis = title in ("FRONT", "BACK", "RIGHT") and a3[2] == 0 == b3[2]
            _line(canvas, ox, oy, panel, project(a3), project(b3), GRID_AXIS if axis else GRID)

        visible = []
        for face in faces:
            facing = -(face[3] * f[0] + face[4] * f[1] + face[5] * f[2])
            if facing > 0:
                a, b, c = face[0], face[1], face[2]
                visible.append((sd[a] + sd[b] + sd[c], face, facing))
        visible.sort(key=lambda item: -item[0])  # far first
        for _, (a, b, c, _nx, _ny, _nz, kind), facing in visible:
            key = (kind, int(facing * 16))
            color = shades.get(key)
            if color is None:
                shade = 0.40 + 0.60 * (key[1] / 16)
                color = shades[key] = bytes((int(kind[0] * shade), int(kind[1] * shade), int(kind[2] * shade)))
            x0, y0, x1, y1, x2, y2 = sx[a], sy[a], sx[b], sy[b], sx[c], sy[c]
            ymin = y0 if y0 < y1 else y1
            ymin = ymin if ymin < y2 else y2
            ymax = y0 if y0 > y1 else y1
            ymax = ymax if ymax > y2 else y2
            r0 = int(ymin + 0.5)
            r1 = int(ymax - 0.5)
            if r1 < r0:  # thinner than a scanline: one pixel at the centroid
                px_x, px_y = int((x0 + x1 + x2) / 3), int((y0 + y1 + y2) / 3)
                if 0 <= px_x < panel and 0 <= px_y < panel:
                    i = ((oy + px_y) * width + ox + px_x) * 3
                    px[i:i + 3] = color
                continue
            if r0 < 0:
                r0 = 0
            if r1 >= panel:
                r1 = panel - 1
            for row in range(r0, r1 + 1):
                yc = row + 0.5
                hits = []
                if (y0 <= yc) != (y1 <= yc):
                    hits.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
                if (y1 <= yc) != (y2 <= yc):
                    hits.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
                if (y2 <= yc) != (y0 <= yc):
                    hits.append(x2 + (yc - y2) * (x0 - x2) / (y0 - y2))
                if len(hits) < 2:
                    continue
                left, right = (hits[0], hits[1]) if hits[0] < hits[1] else (hits[1], hits[0])
                c0 = int(left + 0.5)
                c1 = int(right - 0.5)
                if c1 < c0:
                    c1 = c0 = int((left + right) / 2)
                if c0 < 0:
                    c0 = 0
                if c1 >= panel:
                    c1 = panel - 1
                if c1 < c0:
                    continue
                i = ((oy + row) * width + ox + c0) * 3
                px[i:i + (c1 - c0 + 1) * 3] = color * (c1 - c0 + 1)

        if title in ("ISO", "TOP") and len(scene) > 1:
            for obj in scene:
                v = obj["verts"]
                if not v:
                    continue
                count = len(v) // 3
                lx, ly = project((sum(v[0::3]) / count, sum(v[1::3]) / count, max(v[2::3])))
                canvas.text(ox + int(lx) - 6, oy + int(ly) - 8, str(obj["label"]), (255, 255, 255), 2, box=(40, 44, 52))
        canvas.rect(ox, oy - header, ox + panel, oy - header + 1, (200, 204, 210))
        canvas.rect(ox, oy - header, ox + 1, oy + panel, (200, 204, 210))
        canvas.text(ox + 8, oy - header + 6, f"{title}  GRID {grid}MM", (40, 44, 52), 2)
    return canvas.png()


def _frange(a: float, b: float, step: float):
    n = int(round((b - a) / step))
    return [a + i * step for i in range(n + 1)][:400]


def _line(canvas: _Canvas, ox: int, oy: int, panel: int, a, b, color) -> None:
    x0, y0, x1, y1 = a[0], a[1], b[0], b[1]
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    if steps > 4 * panel:
        steps = 4 * panel
    for s in range(steps + 1):
        t = s / steps
        x, y = int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t)
        if 0 <= x < panel and 0 <= y < panel:
            canvas.set(ox + x, oy + y, color)


# ---------------------------------------------------------------------------
# Material and printer knowledge
# ---------------------------------------------------------------------------

# Curated, conservative shop knowledge per base polymer (Vietnamese notes go to the AI as context).
MATERIALS: dict[str, dict[str, Any]] = {
    "PLA": {"hygroscopic": 1, "enclosure": False, "notes": [
        "PLA cần quạt mạnh (thường 80–100%), mềm ở ~55–60°C: phòng nóng hoặc buồng kín đóng cửa dễ làm chảy xệ.",
        "Lớp nhỏ/thời gian lớp ngắn cần slow_down_layer_time đủ lớn để kịp nguội."]},
    "PETG": {"hygroscopic": 2, "enclosure": False, "notes": [
        "PETG dễ kéo sợi (stringing) và bám đầu phun: nhiệt quá cao, rút sợi ngắn hay di chuyển nhiều giữa các object làm nặng thêm.",
        "Quạt vừa phải (thường 30–60%); quạt quá mạnh làm yếu liên kết lớp, quá yếu làm chảy phần treo/cầu.",
        "PETG dính rất chặt trên bàn PEI trơn (High Temp Plate): nên dùng Textured PEI hoặc keo/lớp tách.",
        "Phần treo và cầu nối kém hơn PLA ở cùng góc; tốc độ quá nhanh dễ thiếu nhựa do độ nhớt cao."]},
    "PET": {"hygroscopic": 2, "enclosure": False, "notes": ["Tương tự PETG; hút ẩm, cần sấy trước khi in nếu kéo sợi."]},
    "PCTG": {"hygroscopic": 2, "enclosure": False, "notes": ["Gần PETG, dai hơn; cũng dễ kéo sợi."]},
    "ABS": {"hygroscopic": 1, "enclosure": True, "notes": [
        "ABS co ngót mạnh: cong vênh góc và nứt lớp khi gió/lạnh; cần buồng kín, quạt thấp, brim cho vật lớn."]},
    "ASA": {"hygroscopic": 1, "enclosure": True, "notes": ["ASA như ABS (cong vênh, nứt lớp) nhưng chịu UV; cần buồng kín."]},
    "PC": {"hygroscopic": 2, "enclosure": True, "notes": ["PC nhiệt rất cao, co ngót và hút ẩm; cần buồng kín, bàn nóng, sấy kỹ."]},
    "PA": {"hygroscopic": 3, "enclosure": True, "notes": ["Nylon hút ẩm rất mạnh: phải sấy và in từ hộp khô; cong vênh, cần buồng kín."]},
    "PAHT": {"hygroscopic": 3, "enclosure": True, "notes": ["PAHT như PA, hút ẩm mạnh, cần sấy và buồng kín."]},
    "PPA": {"hygroscopic": 3, "enclosure": True, "notes": ["Hút ẩm mạnh, cần sấy và buồng kín."]},
    "PP": {"hygroscopic": 0, "enclosure": True, "notes": ["PP bám bàn rất kém (cần bàn/ băng PP), cong vênh mạnh."]},
    "TPU": {"hygroscopic": 2, "enclosure": False, "ams": False, "notes": [
        "TPU phải in chậm (lưu lượng thấp), rút sợi ngắn; không nạp qua AMS (trừ loại 'TPU for AMS')."]},
    "PVA": {"hygroscopic": 3, "enclosure": False, "notes": ["PVA hút ẩm cực mạnh, chỉ dùng làm support."]},
    "BVOH": {"hygroscopic": 3, "enclosure": False, "notes": ["Hút ẩm cực mạnh."]},
    "HIPS": {"hygroscopic": 1, "enclosure": True, "notes": ["Như ABS, cần buồng kín."]},
}
PRINTERS: dict[str, dict[str, Any]] = {  # bed_slinger: bed moves in Y (tall parts wobble); enclosed: has chamber
    "A1": {"bed_slinger": True, "enclosed": False, "ams": "AMS lite"},
    "A1 mini": {"bed_slinger": True, "enclosed": False, "ams": "AMS lite"},
    "P1P": {"bed_slinger": False, "enclosed": False},
    "P1S": {"bed_slinger": False, "enclosed": True}, "P2S": {"bed_slinger": False, "enclosed": True},
    "X1": {"bed_slinger": False, "enclosed": True}, "X1 Carbon": {"bed_slinger": False, "enclosed": True},
    "X1E": {"bed_slinger": False, "enclosed": True}, "H2D": {"bed_slinger": False, "enclosed": True},
    "H2D Pro": {"bed_slinger": False, "enclosed": True}, "H2S": {"bed_slinger": False, "enclosed": True},
    "H2C": {"bed_slinger": False, "enclosed": True}, "X2D": {"bed_slinger": False, "enclosed": True},
}
BED_KEYS = {"Textured PEI Plate": "textured_plate_temp", "High Temp Plate": "hot_plate_temp",
            "Cool Plate": "cool_plate_temp", "Engineering Plate": "eng_plate_temp", "Supertack Plate": "supertack_plate_temp"}


def polymer_of(filament_type: Any, name: Any = "") -> str | None:
    text = f"{scalar(filament_type) or ''} {scalar(name) or ''}".upper()
    for key in sorted(MATERIALS, key=len, reverse=True):
        if re.search(r"(?<![A-Z])" + re.escape(key) + r"(?![A-Z])", text):
            return key
    return None


def printer_of(model: Any) -> tuple[str | None, dict]:
    text = str(scalar(model) or "").replace("Bambu Lab", "").strip()
    for key in sorted(PRINTERS, key=len, reverse=True):
        if text.lower() == key.lower() or text.lower().startswith(key.lower() + " "):
            return key, PRINTERS[key]
    return (text or None), {}


_PROFILE_CACHE: dict[str, dict[str, dict]] = {}


def filament_profiles(root: str | os.PathLike) -> dict[str, dict]:
    folder = Path(os.environ.get("PRINTPILOT_PROFILE_DIR", str(Path(root) / "resources/profiles/BBL/filament")))
    key = str(folder)
    if key not in _PROFILE_CACHE:
        presets: dict[str, dict] = {}
        for path in folder.rglob("*.json") if folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                presets[data["name"]] = data
        _PROFILE_CACHE[key] = presets
    return _PROFILE_CACHE[key]


def material_profile(root: str | os.PathLike, preset_name: Any, extra: dict | None = None) -> dict[str, Any] | None:
    """Resolved values of an installed filament preset (vendor-tuned numbers, not guesses).

    extra: presets from the PrintPilot filament library (user presets inheriting system ones).
    """
    presets = {**filament_profiles(root), **(extra or {})}
    name = str(scalar(preset_name) or "")
    if name not in presets:
        return None

    def resolve(n, seen=()):
        data = presets.get(n)
        if not data or n in seen or len(seen) > 12:
            return {}
        return {**resolve(data.get("inherits"), (*seen, n)), **data}

    p = resolve(name)
    out = {"preset": name, "vendor": scalar(p.get("filament_vendor")), "type": scalar(p.get("filament_type"))}
    for k in ("nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
              "nozzle_temperature_range_high", "filament_max_volumetric_speed", "fan_min_speed", "fan_max_speed",
              "overhang_fan_speed", "slow_down_layer_time", "filament_density", "temperature_vitrification",
              "required_nozzle_HRC", "filament_flow_ratio", "filament_retraction_length", "pressure_advance",
              *BED_KEYS.values()):
        value = number(p.get(k))
        if value is not None:
            out[k] = value
    return out


# ---------------------------------------------------------------------------
# Deterministic print-risk rules
# ---------------------------------------------------------------------------

def _finding(code, severity, message, obj=None, **evidence):
    item = {"code": code, "severity": severity, "message": message, "evidence": evidence}
    if obj is not None:
        item["object"] = obj["label"]
        item["object_name"] = obj["name"]
    return item


def line_width(settings: dict, key: str, nozzle: float) -> float:
    value = scalar(settings.get(key))
    if isinstance(value, str) and value.strip().endswith("%"):
        pct = number(value)
        return nozzle * pct / 100 if pct else nozzle * 1.05
    width = number(value)
    if not width:
        width = number(settings.get("line_width")) or nozzle * 1.05
    return width


SPEED_WIDTH = {"outer_wall_speed": "outer_wall_line_width", "inner_wall_speed": "inner_wall_line_width",
               "sparse_infill_speed": "sparse_infill_line_width", "internal_solid_infill_speed": "internal_solid_infill_line_width",
               "top_surface_speed": "top_surface_line_width"}


def flow_caps(settings: dict, max_flow: float | None, nozzle: float) -> dict[str, dict]:
    """mm/s each speed can actually reach before the filament's max volumetric speed caps it."""
    layer = number(settings.get("layer_height"))
    if not max_flow or not layer:
        return {}
    out = {}
    for speed_key, width_key in SPEED_WIDTH.items():
        speed = number(settings.get(speed_key))
        width = line_width(settings, width_key, nozzle)
        cap = max_flow / (width * layer)
        out[speed_key] = {"set": speed, "cap": round(cap, 1), "flow_mm3s": round(speed * width * layer, 1) if speed else None}
    return out


def evaluate(objects: list[dict], ctx: dict) -> list[dict]:
    """Findings from geometry + settings + material + printer. ctx keys: settings, material, profile,
    printer_key, printer, nozzle, used_filaments, ams (or None), ams_age_s."""
    s = ctx.get("settings", {})
    material = ctx.get("material")
    knowledge = MATERIALS.get(material or "", {})
    profile = ctx.get("profile") or {}
    printer = ctx.get("printer") or {}
    printer_key = ctx.get("printer_key")
    nozzle = ctx.get("nozzle") or 0.4
    out: list[dict] = []
    support = str(scalar(s.get("enable_support"))) in ("1", "true", "True")
    brim_type = str(scalar(s.get("brim_type")) or "")
    brim_width = number(s.get("brim_width")) or 0
    no_brim = brim_type == "no_brim" or (brim_type not in ("auto_brim", "brim_ears") and brim_width <= 0)

    for o in objects:
        if o.get("empty"):
            continue
        h = o["size_mm"][2]
        if o["bottom_z_mm"] > 0.05:
            out.append(_finding("floating", "critical", f"Object {o['label']} không chạm bàn in (đáy ở {o['bottom_z_mm']} mm).", o,
                                bottom_z_mm=o["bottom_z_mm"]))
        if h >= 15 and (o["slenderness"] >= 4 or (o["bed_contact_mm2"] and o["bed_contact_mm2"] < 150 and h >= 40)):
            msg = f"Object {o['label']} cao {h} mm trên đáy hẹp (tỉ lệ cao/đáy {o['slenderness']}): dễ rung, lệch lớp hoặc đổ."
            if printer.get("bed_slinger"):
                msg += f" {printer_key} di chuyển bàn theo trục Y nên vật cao mảnh bị lắc mạnh hơn: giảm gia tốc/tốc độ tường ngoài, xoay vật cho cạnh dài theo Y."
            if no_brim:
                msg += " Brim đang tắt."
            out.append(_finding("tall_slender", "warn", msg, o, height_mm=h, slenderness=o["slenderness"],
                                bed_contact_mm2=o["bed_contact_mm2"], bed_slinger=bool(printer.get("bed_slinger")), brim_off=no_brim))
        if h > 5 and (o["bed_contact_mm2"] < 80 or o["bed_contact_ratio"] < 0.12):
            out.append(_finding("small_contact", "warn",
                                f"Object {o['label']} chỉ tiếp xúc bàn {o['bed_contact_mm2']} mm² ({round(100 * o['bed_contact_ratio'])}% diện tích đáy): dễ bong lớp đầu" +
                                (" — brim đang tắt." if no_brim else "."), o, bed_contact_mm2=o["bed_contact_mm2"],
                                bed_contact_ratio=o["bed_contact_ratio"], brim_off=no_brim))
        steep = o["overhang_60_mm2"]
        if steep >= max(25.0, 0.02 * o["surface_mm2"]) and (o["lowest_steep_overhang_mm"] or 0) > 1:
            if not support:
                out.append(_finding("overhang_unsupported", "warn",
                                    f"Object {o['label']} có {steep} mm² mặt treo dốc hơn 60° (thấp nhất ở {o['lowest_steep_overhang_mm']} mm) mà support đang tắt.",
                                    o, overhang_60_mm2=steep, overhang_60_pct=o["overhang_60_pct"], support=False))
            else:
                out.append(_finding("overhang_supported", "info",
                                    f"Object {o['label']} có {steep} mm² mặt treo >60°; support đang bật (vùng đỏ trên ảnh).", o,
                                    overhang_60_mm2=steep, support=True))
        elif o["overhang_45_pct"] >= 15 and not support:
            out.append(_finding("overhang_moderate", "info",
                                f"Object {o['label']} có {o['overhang_45_pct']}% bề mặt treo 45–60°: cần làm mát tốt và tốc độ phần treo thấp.",
                                o, overhang_45_pct=o["overhang_45_pct"]))
        if o["ceiling_mm2"] >= 100:
            sev = "warn" if material in ("PETG", "PET", "ABS", "ASA", "PC") else "info"
            out.append(_finding("bridges", sev, f"Object {o['label']} có {o['ceiling_mm2']} mm² mặt trần/cầu nối gần nằm ngang: cần quạt đủ và tốc độ cầu thấp.",
                                o, ceiling_mm2=o["ceiling_mm2"], material=material))
        if o["mean_thickness_mm"] and o["mean_thickness_mm"] < 2.2 * nozzle or min(o["size_mm"]) < 2 * nozzle * 2.5:
            out.append(_finding("thin_features", "info", f"Object {o['label']} mỏng (độ dày trung bình ~{o['mean_thickness_mm']} mm, cạnh nhỏ nhất {min(o['size_mm'])} mm): chi tiết nhỏ cần tốc độ tường ngoài thấp, kiểm tra detect_thin_wall.",
                                o, mean_thickness_mm=o["mean_thickness_mm"], min_size_mm=min(o["size_mm"])))
        if max(o["size_mm"]) < 20 and h >= 5:
            out.append(_finding("short_layers", "info", f"Object {o['label']} nhỏ ({o['size_mm']} mm): mỗi lớp in rất nhanh, nhựa chưa kịp nguội — in nhiều bản cùng lúc hoặc tăng slow_down_layer_time.",
                                o, size_mm=o["size_mm"]))

    real = [o for o in objects if not o.get("empty")]
    if material and knowledge.get("enclosure") and printer and not printer.get("enclosed"):
        out.append(_finding("needs_enclosure", "critical", f"{material} cần buồng kín nhưng {printer_key} là máy hở: rủi ro cong vênh và nứt lớp rất cao.",
                            material=material, printer=printer_key))
    name = str(scalar(s.get("filament_settings_id")) or "")
    abrasive = bool(re.search(r"(?<![A-Z])(CF|GF)(?![A-Z])", f"{scalar(s.get('filament_type'))} {name}".upper()))
    nozzle_type = str(ctx.get("nozzle_type") or scalar(s.get("nozzle_type")) or "")
    if abrasive and nozzle_type and "hardened" not in nozzle_type and "tungsten" not in nozzle_type:
        out.append(_finding("abrasive_nozzle", "critical", f"Nhựa có sợi carbon/thủy tinh mà đầu phun là {nozzle_type}: mòn đầu phun rất nhanh, cần đầu phun thép cứng.",
                            nozzle_type=nozzle_type, filament=name or scalar(s.get("filament_type"))))
    elif abrasive and not nozzle_type:
        out.append(_finding("abrasive_nozzle_unknown", "warn", "Nhựa có sợi carbon/thủy tinh: chỉ in với đầu phun thép cứng (không xác định được loại đầu phun hiện tại).",
                            filament=name or scalar(s.get("filament_type"))))
    if material == "TPU" and "for ams" not in name.lower() and printer.get("ams"):
        out.append(_finding("tpu_ams", "warn", f"TPU không nạp qua {printer['ams']}: dùng giá cuộn ngoài.", material=material))
    if knowledge.get("hygroscopic", 0) >= 2:
        humid = ctx.get("humidity")
        if humid and humid.get("wet"):
            out.append(_finding("wet_filament", "warn", f"{material} hút ẩm và AMS báo độ ẩm cao ({humid['text']}): sấy nhựa trước khi in, nếu không sẽ kéo sợi và bề mặt sần.",
                                material=material, **humid))
        else:
            out.append(_finding("hygroscopic", "info", f"{material} hút ẩm: nếu cuộn đã mở lâu, sấy trước khi in (kéo sợi, bọt, bề mặt sần là dấu hiệu ẩm).",
                                material=material, ams_humidity=(humid or {}).get("text")))
    bed = str(scalar(s.get("curr_bed_type")) or "")
    bed_key = BED_KEYS.get(bed)
    if bed_key and profile.get(bed_key) == 0:
        out.append(_finding("bed_unsupported", "critical", f"Profile nhựa {profile.get('preset')} không hỗ trợ {bed} (nhiệt bàn = 0).",
                            bed=bed, preset=profile.get("preset")))
    if material in ("PETG", "PET", "PCTG") and bed == "High Temp Plate":
        out.append(_finding("petg_smooth_pei", "warn", "PETG trên bàn PEI trơn (High Temp Plate) dính rất chặt, có thể làm hỏng bề mặt bàn: dùng keo hoặc Textured PEI.",
                            bed=bed, material=material))
    temp = number(s.get("nozzle_temperature"))
    low, high = profile.get("nozzle_temperature_range_low"), profile.get("nozzle_temperature_range_high")
    if temp and low and high and not low <= temp <= high:
        out.append(_finding("nozzle_temp_range", "warn", f"Nhiệt đầu phun {temp:g}°C nằm ngoài khoảng {low:g}–{high:g}°C của profile {profile.get('preset')}.",
                            nozzle_temperature=temp, range=[low, high]))
    max_flow = profile.get("filament_max_volumetric_speed") or number(s.get("filament_max_volumetric_speed"))
    capped = {k: v for k, v in flow_caps(s, max_flow, nozzle).items() if v["set"] and v["set"] > v["cap"] * 1.05}
    if capped:
        out.append(_finding("flow_capped", "info", "Một số tốc độ vượt giới hạn lưu lượng của nhựa; Studio sẽ tự hạ xuống mức trần: " +
                            ", ".join(f"{k} {v['set']:g}→~{v['cap']:g} mm/s" for k, v in capped.items()) + ".",
                            max_volumetric_speed=max_flow, speeds=capped))
    if material in ("PETG", "PET", "PCTG", "TPU") and len(real) >= 4:
        out.append(_finding("many_objects_stringing", "info", f"{len(real)} object trên plate: đầu phun di chuyển qua lại nhiều, {material} dễ kéo sợi giữa các object.",
                            objects=len(real), material=material))
    used = ctx.get("used_filaments") or []
    if len(used) > 1:
        out.append(_finding("multi_material", "info", f"Plate dùng {len(used)} nhựa/màu: thêm thời gian đổi màu và nhựa xả bỏ; mặt tiếp giáp giữa màu là điểm yếu cơ học.",
                            filaments=used))
    out.extend(ams_findings(ctx))
    out.extend(slice_findings(ctx))
    out.extend(context_findings(ctx, objects))
    order = {"critical": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])


def ams_findings(ctx: dict) -> list[dict]:
    ams = ctx.get("ams")
    project = ctx.get("project_filaments") or []
    if ams is None:
        if ctx.get("printer_requested"):
            return [_finding("printer_unknown", "info", "Không có dữ liệu AMS mới của máy được chọn; không đối chiếu được nhựa đang lắp.",
                             printer=ctx.get("printer_requested"))]
        return []
    loaded = []
    for unit in ams.get("units", []):
        for slot in unit.get("slots", []):
            if slot.get("present"):
                loaded.append({"where": f"{unit.get('name') or 'AMS'} khay {slot.get('id', 0) + 1}",
                               "material": polymer_of(slot.get("material"), slot.get("filament_name")), "raw": slot.get("material")})
    ext = ams.get("external")
    if ext and ext.get("present"):
        loaded.append({"where": "cuộn ngoài", "material": polymer_of(ext.get("material"), ext.get("filament_name")), "raw": ext.get("material")})
    out = []
    for fil in project:
        if not fil.get("material"):
            continue
        if not any(item["material"] == fil["material"] for item in loaded):
            out.append(_finding("filament_not_loaded", "warn",
                                f"Project dùng {fil['material']} ({fil.get('preset') or 'nhựa #' + str(fil['index'])}) nhưng máy {ctx.get('printer_label') or ''} chưa lắp {fil['material']} "
                                f"(đang có: {', '.join(sorted({i['raw'] or '?' for i in loaded})) or 'trống'}).",
                                filament_index=fil["index"], material=fil["material"], loaded=[i["where"] + ": " + str(i["raw"]) for i in loaded]))
    return out


def humidity_state(ams: dict | None) -> dict | None:
    """AMS humidity: percent when the unit reports it, else Studio's level (5 = dry ... 1 = wet)."""
    if not ams:
        return None
    worst = None
    for unit in ams.get("units", []):
        raw, level = unit.get("humidity_raw"), unit.get("humidity")
        if isinstance(raw, (int, float)) and raw > 0:
            state = {"text": f"{raw:g}%", "wet": raw >= 40, "score": raw}
        elif isinstance(level, int) and 1 <= level <= 5:
            state = {"text": f"mức {level}/5 (5 = khô)", "wet": level <= 2, "score": (6 - level) * 20}
        else:
            continue
        if worst is None or state["score"] > worst["score"]:
            worst = state
    if worst:
        worst = {k: v for k, v in worst.items() if k != "score"}
    return worst


# ---------------------------------------------------------------------------
# Similar-shape history
# ---------------------------------------------------------------------------

def shape_signature(objects: list[dict], material: str | None, printer_key: str | None) -> dict | None:
    real = [o for o in objects if not o.get("empty")]
    if not real:
        return None
    return {"max_height": max(o["size_mm"][2] for o in real),
            "max_slenderness": max(o["slenderness"] for o in real),
            "overhang_60_pct": max(o["overhang_60_pct"] for o in real),
            "min_contact": min(o["bed_contact_mm2"] for o in real),
            "objects": len(real), "material": material, "printer": printer_key}


def shape_distance(a: dict, b: dict) -> float:
    def lg(x):
        return math.log(max(float(x or 0), 0.0) + 1.0)
    return (abs(lg(a["max_height"]) - lg(b["max_height"])) / 1.0
            + abs(lg(a["max_slenderness"]) - lg(b["max_slenderness"])) / 0.7
            + abs(a["overhang_60_pct"] - b["overhang_60_pct"]) / 15.0
            + abs(lg(a["min_contact"]) - lg(b["min_contact"])) / 1.5) / 4


def similar_feedback(db, key: str | None, signature: dict | None, limit: int = 6) -> list[dict]:
    """Reviewed prints of other models whose shape is close, same material and printer family."""
    if not signature or not signature.get("material"):
        return []
    rows = db.execute("SELECT id, model_key, shape_signature FROM analyses WHERE shape_signature IS NOT NULL "
                      "AND model_key IS NOT NULL ORDER BY created_at DESC LIMIT 500").fetchall()
    best: dict[str, float] = {}
    for row in rows:
        if row["model_key"] == key:
            continue
        try:
            other = json.loads(row["shape_signature"])
        except (TypeError, ValueError):
            continue
        if not isinstance(other, dict) or other.get("material") != signature["material"]:
            continue
        if signature.get("printer") and other.get("printer") and other["printer"] != signature["printer"]:
            continue
        dist = shape_distance(signature, other)
        if dist <= 0.35 and dist < best.get(row["model_key"], math.inf):
            best[row["model_key"]] = dist
    out = []
    for model_key, dist in sorted(best.items(), key=lambda kv: kv[1]):
        for run in db.execute("SELECT * FROM print_runs WHERE model_key=? AND review IS NOT NULL ORDER BY started_at DESC LIMIT 3",
                              (model_key,)).fetchall():
            item = dict(run)
            item["review"] = json.loads(item["review"])
            item["settings"] = json.loads(item["settings"]) if item.get("settings") else {}
            item["relation"] = "similar_shape"
            item["similarity"] = round(1 - dist, 2)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Orientation search
# ---------------------------------------------------------------------------

_DIR_NAMES = {(1, 0, 0): "phải (+X)", (-1, 0, 0): "trái (−X)", (0, 1, 0): "sau (+Y)", (0, -1, 0): "trước (−Y)",
              (0, 0, 1): "trên (+Z)", (0, 0, -1): "dưới (−Z)"}
DOWN_DIRECTIONS = [d for d in ((x, y, z) for x in (-1, 0, 1) for y in (-1, 0, 1) for z in (-1, 0, 1)) if d != (0, 0, 0)]


def _rotation_for(d) -> tuple[float, float]:
    """Degrees (about X, then about Y) that turn direction d (object frame) to straight down."""
    dx, dy, dz = _norm(d)
    a = math.atan2(dy, dz)
    r = math.hypot(dy, dz)
    b = math.atan2(dx, -r)
    return math.degrees(a), math.degrees(b)


def _rotated(obj: dict, d) -> dict:
    a, b = (math.radians(x) for x in _rotation_for(d))
    ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
    v = obj["verts"]
    out = array.array("f")
    for i in range(0, len(v), 3):
        x, y, z = v[i], v[i + 1], v[i + 2]
        y, z = y * ca - z * sa, y * sa + z * ca
        x, z = x * cb + z * sb, -x * sb + z * cb
        out.extend((x, y, z))
    zmin = min(out[2::3])
    for i in range(2, len(out), 3):
        out[i] -= zmin
    return {"label": obj["label"], "name": obj["name"], "verts": out, "tris": obj["tris"], "filaments": obj.get("filaments")}


def orientation_cost(m: dict, bed_slinger: bool) -> float:
    surface = max(m["surface_mm2"], 1e-6)
    support = 100 * (m["overhang_60_mm2"] + 0.3 * (m["overhang_45_mm2"] - m["overhang_60_mm2"])) / surface
    slender = max(0.0, m["slenderness"] - 3) * (15 if bed_slinger else 10)
    adhesion = 8 if m["bed_contact_mm2"] < 80 else 0
    return support + slender + adhesion + (0.05 * m["size_mm"][2] if bed_slinger else 0)


def suggest_orientation(obj: dict, bed_slinger: bool = False, budget: int = 8000) -> dict | None:
    """Best of 26 'which side faces the bed' options when it clearly beats the current one."""
    v = obj["verts"]
    if not v:
        return None
    xs, ys, zs = v[0::3], v[1::3], v[2::3]
    diag = math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2) or 1.0
    small_v, small_t = v, obj["tris"]
    cell = diag / 60
    while len(small_t) // 3 > budget and cell < diag:
        small_v, small_t, _ = _cluster(v, obj["tris"], cell)
        cell *= 1.5
    small = {"label": obj["label"], "name": obj["name"], "verts": small_v, "tris": small_t}
    results = []
    for d in DOWN_DIRECTIONS:
        m = object_metrics(_rotated(small, d))
        if m.get("empty"):
            continue
        results.append((orientation_cost(m, bed_slinger), d, m))
    current = next((r for r in results if r[1] == (0, 0, -1)), None)
    if not current:
        return None
    best = min(results, key=lambda r: (round(r[0], 3), sum(abs(c) for c in r[1])))
    gain_support = current[2]["overhang_60_mm2"] - best[2]["overhang_60_mm2"]
    gain_slender = current[2]["slenderness"] - best[2]["slenderness"]
    if best[1] == current[1] or best[0] > current[0] * 0.7 or (gain_support < 20 and gain_slender < 1):
        return None
    d = best[1]
    faces = [_DIR_NAMES[tuple(d[i] if i == k else 0 for i in range(3))] for k in range(3) if d[k]]
    rx, ry = _rotation_for(d)
    pick = lambda m: {k: m[k] for k in ("overhang_60_mm2", "overhang_60_pct", "bed_contact_mm2", "slenderness", "size_mm")}
    return {"object": obj["label"], "object_name": obj["name"],
            "down_face": " + ".join(faces), "rotate_x_deg": round(rx, 1), "rotate_y_deg": round(ry, 1),
            "current": pick(current[2]), "suggested": pick(best[2]),
            "support_reduction_mm2": round(gain_support, 1)}


def orientation_findings(suggestions: list[dict]) -> list[dict]:
    out = []
    for s in suggestions:
        cur, new = s["current"], s["suggested"]
        out.append({"code": "orientation", "severity": "warn" if s["support_reduction_mm2"] >= 50 or cur["slenderness"] >= 4 else "info",
                    "object": s["object"], "object_name": s["object_name"],
                    "message": (f"Object {s['object']}: úp mặt {s['down_face']} xuống bàn (xoay X {s['rotate_x_deg']:g}°, rồi Y {s['rotate_y_deg']:g}°) "
                                f"— phần treo >60° {cur['overhang_60_mm2']:g}→{new['overhang_60_mm2']:g} mm², chạm bàn {cur['bed_contact_mm2']:g}→{new['bed_contact_mm2']:g} mm², "
                                f"cao/đáy {cur['slenderness']:g}→{new['slenderness']:g}. Có thể dùng công cụ 'Lay on face' trong Studio."),
                    "evidence": s})
    return out


# ---------------------------------------------------------------------------
# Post-slice, spool, room and maintenance checks
# ---------------------------------------------------------------------------

def slice_summary(stats: dict | None) -> dict | None:
    if not isinstance(stats, dict):
        return None
    fils = [f for f in stats.get("filaments", []) if isinstance(f, dict)]
    layers = [t for t in stats.get("layer_times_s", []) if isinstance(t, (int, float))]
    return {"time_h": round((stats.get("time_s") or 0) / 3600, 2), "layers": len(layers),
            "min_layer_s": min(layers[1:]) if len(layers) > 1 else None,
            "filaments": [{"index": f.get("index"), "weight_g": round(f.get("weight_g") or 0, 1),
                           "model_mm3": round(f.get("model_mm3") or 0), "support_mm3": round(f.get("support_mm3") or 0),
                           "waste_mm3": round((f.get("flush_mm3") or 0) + (f.get("tower_mm3") or 0))} for f in fils],
            "filament_changes": stats.get("filament_changes"), "flush_changes": stats.get("flush_changes")}


def slice_findings(ctx: dict) -> list[dict]:
    stats = ctx.get("slice_stats")
    if not isinstance(stats, dict):
        return []
    s, profile = ctx.get("settings", {}), ctx.get("profile") or {}
    out = []
    layers = [t for t in stats.get("layer_times_s", []) if isinstance(t, (int, float))][1:]
    threshold = number(s.get("slow_down_layer_time")) or profile.get("slow_down_layer_time")
    if threshold and layers:
        fast = [t for t in layers if t < threshold * 0.95]
        if len(fast) >= 3:
            out.append(_finding("fast_layers", "warn",
                                f"{len(fast)} lớp in nhanh hơn {threshold:g} s dù Studio đã giảm tốc (nhanh nhất {min(fast):g} s): nhựa chưa kịp nguội, "
                                "dễ chảy/biến dạng ở đỉnh — in nhiều bản cùng lúc hoặc thêm vật đệm.",
                                fast_layers=len(fast), min_layer_s=min(fast), slow_down_layer_time=threshold))
    fils = [f for f in stats.get("filaments", []) if isinstance(f, dict)]
    model = sum(f.get("model_mm3") or 0 for f in fils)
    waste = sum((f.get("flush_mm3") or 0) + (f.get("tower_mm3") or 0) for f in fils)
    support = sum(f.get("support_mm3") or 0 for f in fils)
    total_g = sum(f.get("weight_g") or 0 for f in fils)
    volume = sum(f.get("volume_mm3") or 0 for f in fils) or 1
    if model and waste / model > 0.5 and waste / volume * total_g > 5:
        out.append(_finding("purge_waste", "warn",
                            f"Nhựa xả bỏ khi đổi màu + tháp xả ≈ {waste / volume * total_g:.0f} g, bằng {100 * waste / model:.0f}% nhựa của model "
                            f"({stats.get('filament_changes') or 0} lần đổi): gộp màu, giảm flush hoặc in nhiều vật cùng plate để tận dụng.",
                            waste_g=round(waste / volume * total_g, 1), waste_ratio=round(waste / model, 2)))
    if model and support / model > 0.3:
        out.append(_finding("support_heavy", "info", f"Support chiếm {100 * support / model:.0f}% thể tích model: xem gợi ý hướng đặt để giảm support.",
                            support_ratio=round(support / model, 2)))
    return out


def context_findings(ctx: dict, objects: list[dict]) -> list[dict]:
    out = []
    material = ctx.get("material")
    level = MATERIALS.get(material or "", {}).get("hygroscopic", 0)
    stats = ctx.get("slice_stats") if isinstance(ctx.get("slice_stats"), dict) else {}
    need = {}
    for f in stats.get("filaments", []) if stats else []:
        fil = next((p for p in ctx.get("project_filaments") or [] if p.get("index") == f.get("index")), None)
        if fil and fil.get("material"):
            need[fil["material"]] = need.get(fil["material"], 0) + (f.get("weight_g") or 0)
    spools = ctx.get("spools") or []
    for poly, grams in need.items():
        matching = [sp for sp in spools if sp.get("polymer") == poly and sp.get("remaining_g") is not None]
        if matching and max(sp["remaining_g"] for sp in matching) < grams:
            out.append(_finding("spool_short", "critical",
                                f"Cần ~{grams:.0f} g {poly} nhưng cuộn {poly} đang lắp chỉ còn {max(sp['remaining_g'] for sp in matching):.0f} g: thay cuộn hoặc bật tự đổi cuộn.",
                                needed_g=round(grams, 1), spools=[{"name": sp["name"], "remaining_g": sp["remaining_g"]} for sp in matching]))
    for sp in spools:
        if sp.get("needs_drying") and (not need or sp.get("polymer") in need or sp.get("polymer") == material):
            out.append(_finding("spool_needs_drying", "warn",
                                f"Cuộn «{sp['name']}» ({sp.get('polymer')}) đã {sp['days_since_dry']} ngày kể từ lần sấy/mở (khuyến nghị sấy lại sau {sp['dry_after_days']} ngày).",
                                spool=sp["name"], days=sp["days_since_dry"]))
    env = ctx.get("environment")
    printer = ctx.get("printer") or {}
    if env:
        hum, temp = env.get("humidity"), env.get("temperature")
        if hum is not None and hum >= 60 and level >= 2:
            out.append(_finding("room_humid", "warn", f"Phòng ẩm {hum:g}% với {material} hút ẩm: để nhựa trong hộp khô/AMS kín, sấy nếu kéo sợi.",
                                humidity=hum, material=material))
        if temp is not None and temp < 18 and (MATERIALS.get(material or "", {}).get("enclosure") or not printer.get("enclosed")):
            out.append(_finding("room_cold", "warn", f"Phòng lạnh {temp:g}°C: lớp đầu dễ bong và vật dễ cong vênh, tránh gió điều hòa thổi vào máy, cân nhắc brim.",
                                temperature=temp))
        if temp is not None and temp > 30 and material == "PLA" and printer.get("enclosed"):
            out.append(_finding("room_hot", "warn", f"Phòng nóng {temp:g}°C, máy kín in PLA: mở cửa/nắp trên để tránh nghẹt đầu phun và chảy xệ.",
                                temperature=temp))
    for task in ctx.get("maintenance") or []:
        if task["task"] == "clean_bed" and (task["due"] or (task["ratio"] >= 0.8 and any(
                not o.get("empty") and o["bed_contact_mm2"] < 150 for o in objects))):
            out.append(_finding("maintenance_bed", "warn" if task["due"] else "info",
                                f"Đã {task['hours']:g} giờ in từ lần vệ sinh bàn gần nhất (chu kỳ {task['interval_hours']:g} h): vệ sinh bàn trước khi in để lớp đầu bám chắc.",
                                hours=task["hours"], interval=task["interval_hours"]))
        if task["task"] == "nozzle" and task["due"]:
            out.append(_finding("maintenance_nozzle", "warn", f"Đầu phun đã qua {task['hours']:g} giờ in quy đổi (chu kỳ {task['interval_hours']:g} h): kiểm tra mòn trước khi in chi tiết đẹp.",
                                hours=task["hours"]))
    return out
