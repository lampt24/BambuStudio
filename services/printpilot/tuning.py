"""Extended settings the AI may change (settings_v2): support, seam, infill pattern, brim type,
nozzle/bed temperature and cooling of the filament the plate uses.

The original numeric allowlist in server.SETTINGS_ALLOWLIST stays the v1 contract. v2 changes are
only returned to clients that declare the "settings_v2" capability (new Studio builds) and to
dashboard analyses (applied through the optimized 3MF download). Older clients get them as
warnings to apply by hand, never as changes they cannot parse.

Change shape: {key, old, value, reason, evidence, scope: "print"|"filament", filament_index?}
"""
from __future__ import annotations

import math
from typing import Any

CAPABILITY = "settings_v2"

# kind: number | int | bool | enum ; scope: print (process preset) | filament (per-filament vector)
SPECS: dict[str, dict[str, Any]] = {
    "enable_support": {"scope": "print", "kind": "bool"},
    "support_type": {"scope": "print", "kind": "enum", "choices": ["normal(auto)", "tree(auto)"]},
    "support_threshold_angle": {"scope": "print", "kind": "int", "min": 1, "max": 90},
    "seam_position": {"scope": "print", "kind": "enum", "choices": ["aligned", "nearest", "back", "random"]},
    "sparse_infill_pattern": {"scope": "print", "kind": "enum",
                              "choices": ["grid", "gyroid", "cubic", "crosshatch", "zig-zag", "adaptivecubic", "honeycomb"]},
    "brim_type": {"scope": "print", "kind": "enum", "choices": ["auto_brim", "outer_only", "brim_ears", "no_brim"]},
    "outer_wall_acceleration": {"scope": "print", "kind": "int", "min": 500, "max": 12000},
    "top_surface_speed": {"scope": "print", "kind": "number", "min": 10, "max": 300},
    "initial_layer_speed": {"scope": "print", "kind": "number", "min": 10, "max": 100},
    "nozzle_temperature": {"scope": "filament", "kind": "int", "min": 170, "max": 320, "profile_range": True},
    "nozzle_temperature_initial_layer": {"scope": "filament", "kind": "int", "min": 170, "max": 320, "profile_range": True},
    "fan_min_speed": {"scope": "filament", "kind": "int", "min": 0, "max": 100},
    "fan_max_speed": {"scope": "filament", "kind": "int", "min": 0, "max": 100},
    "overhang_fan_speed": {"scope": "filament", "kind": "int", "min": 0, "max": 100},
    "slow_down_layer_time": {"scope": "filament", "kind": "int", "min": 1, "max": 60},
    "textured_plate_temp": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Textured PEI Plate"},
    "textured_plate_temp_initial_layer": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Textured PEI Plate"},
    "hot_plate_temp": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "High Temp Plate"},
    "hot_plate_temp_initial_layer": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "High Temp Plate"},
    "cool_plate_temp": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Cool Plate"},
    "cool_plate_temp_initial_layer": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Cool Plate"},
    "eng_plate_temp": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Engineering Plate"},
    "eng_plate_temp_initial_layer": {"scope": "filament", "kind": "int", "min": 0, "max": 120, "bed": "Engineering Plate"},
}


def _pick(value: Any, index: int) -> Any:
    from insight import as_list
    items = as_list(value)
    return items[index] if 0 <= index < len(items) else (items[0] if len(items) == 1 else None)


def normalize(key: str, value: Any) -> Any:
    """Typed value for a v2 key; ValueError when it is not acceptable for the key at all."""
    spec = SPECS[key]
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(value, bool):
            return 1 if value else 0
        if value in (0, 1, "0", "1", "true", "false"):
            return 1 if value in (1, "1", "true") else 0
        raise ValueError(f"{key} must be 0/1")
    if kind == "enum":
        if isinstance(value, str) and value in spec["choices"]:
            return value
        raise ValueError(f"{key} must be one of {spec['choices']}")
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{key} must be finite")
    if kind == "int":
        if number != int(number):
            raise ValueError(f"{key} must be an integer")
        return int(number)
    return round(number, 3)


def current_values(settings: dict, filament_index: int | None) -> dict[str, Any]:
    """Current typed values of v2 keys (filament keys from the plate's single filament)."""
    out = {}
    for key, spec in SPECS.items():
        if key not in settings:
            continue
        if spec["scope"] == "filament":
            if filament_index is None:
                continue
            raw = _pick(settings[key], filament_index - 1)
        else:
            raw = _pick(settings[key], 0)
        try:
            out[key] = normalize(key, raw)
        except ValueError:
            continue
    return out


def allowed(settings: dict, filament_index: int | None) -> dict[str, Any]:
    """Keys the AI may change now, with their current values (bed keys only for the bed in use)."""
    bed = str(_pick(settings.get("curr_bed_type"), 0) or "")
    current = current_values(settings, filament_index)
    return {k: v for k, v in current.items() if not SPECS[k].get("bed") or SPECS[k]["bed"] == bed}


def bounds(key: str, settings: dict, filament_index: int | None) -> tuple[float, float] | None:
    spec = SPECS[key]
    if spec["kind"] in ("bool", "enum"):
        return None
    low, high = spec["min"], spec["max"]
    if spec.get("profile_range") and filament_index:
        try:
            plow = float(_pick(settings.get("nozzle_temperature_range_low"), filament_index - 1))
            phigh = float(_pick(settings.get("nozzle_temperature_range_high"), filament_index - 1))
            if 100 <= plow <= phigh <= 400:
                low, high = max(low, plow), min(high, phigh)
        except (TypeError, ValueError):
            pass
    return low, high


def validate(changes: list, settings: dict, filament_index: int | None, used_filaments: int) -> tuple[list, list]:
    """(valid v2 changes, Vietnamese warnings for rejected ones). Unknown keys are ignored here."""
    valid, rejected = [], []
    allow = allowed(settings, filament_index)
    seen = set()
    for item in changes:
        if not isinstance(item, dict) or item.get("key") not in SPECS:
            continue
        key = item["key"]
        spec = SPECS[key]
        reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
        evidence = item.get("evidence") if isinstance(item.get("evidence"), str) else ""
        label = f"{key} → {item.get('value')!r}"
        if key in seen:
            continue
        seen.add(key)
        if spec["scope"] == "filament" and filament_index is None:
            why = (f"plate dùng {used_filaments} nhựa" if used_filaments > 1 else "không xác định được nhựa của plate")
            rejected.append(f"Đề xuất {label} không tự áp dụng vì {why}; chỉnh tay cho đúng nhựa. {reason}".strip())
            continue
        if key not in allow:
            rejected.append(f"Đề xuất {label} bị bỏ: thông số không áp dụng cho cấu hình hiện tại (ví dụ khác loại bàn in).")
            continue
        try:
            value = normalize(key, item.get("value"))
            old = normalize(key, item.get("old"))
        except ValueError as exc:
            rejected.append(f"Đề xuất {label} bị bỏ: {exc}.")
            continue
        if old != allow[key]:
            rejected.append(f"Đề xuất {label} bị bỏ: giá trị cũ không khớp cấu hình ({allow[key]!r}).")
            continue
        limit = bounds(key, settings, filament_index)
        if limit and not limit[0] <= value <= limit[1]:
            rejected.append(f"Đề xuất {label} bị bỏ: ngoài giới hạn an toàn {limit[0]:g}–{limit[1]:g}.")
            continue
        if value == old:
            continue
        change = {"key": key, "old": old, "value": value, "reason": reason[:2000], "scope": spec["scope"]}
        if evidence:
            change["evidence"] = evidence[:600]
        if spec["scope"] == "filament":
            change["filament_index"] = filament_index
        valid.append(change)
    return valid, rejected


def as_manual(changes: list) -> list[str]:
    """v2 suggestions for clients that cannot apply them: readable warnings."""
    out = []
    for item in changes:
        if isinstance(item, dict) and item.get("key") in SPECS:
            out.append(f"Đề xuất chỉnh tay: {item['key']} {item.get('old')!r} → {item.get('value')!r}. {item.get('reason', '')}".strip())
    return out


def project_value(key: str, value: Any, current: Any, filament_index: int | None) -> Any:
    """Value to write into Metadata/project_settings.config for an optimized 3MF."""
    text = str(value)
    if SPECS[key]["scope"] == "filament":
        if not isinstance(current, list) or not filament_index or filament_index > len(current):
            raise ValueError(f"Project has no filament #{filament_index} for {key}")
        out = list(current)
        out[filament_index - 1] = text
        return out
    return text
