"""Workshop state that improves prints over time.

* Auto-link: a Studio "send" analysis naming a printer leaves a pending send; the next job that
  starts on that printer within LINK_WINDOW_S is linked to it, with the settings Studio sliced
  with and the slice statistics (no filename guessing).
* Spools: inventory per spool (opened/dried dates, remaining grams, AMS slot); finished jobs
  subtract the sliced weight when the tray that printed is known.
* Maintenance: per-printer tasks with hour intervals counted from real print time
  (abrasive filaments wear nozzles faster).
* Room environment: readings posted by a small sensor (ESP32 + SHT31/DHT22).
* Vision helpers: failure decision for auto-pause and the post-print review issue list.
"""
from __future__ import annotations

import calendar
import json
import math
import re
import time
import uuid
from typing import Any

import insight

ISSUES = ["Bám bàn kém", "Tơ nhựa", "Lệch lớp", "Thiếu nhựa", "Bề mặt xấu", "Cong vênh", "Phần treo xấu", "Nứt lớp", "Bọt/ẩm"]
FAILURE_KINDS = {"spaghetti": "nhựa rối (spaghetti)", "detached": "vật bị bong/xô khỏi bàn",
                 "first_layer": "lớp đầu hỏng", "layer_shift": "lệch lớp", "blob": "cục nhựa bám đầu phun"}
FAILURE_ACTIONS = ("off", "notify", "pause")
LINK_WINDOW_S = 45 * 60
DRY_AFTER_DAYS = {3: 2, 2: 14}   # hygroscopic level -> days after drying/opening before re-drying is advised
# task, label, default interval in print hours, why
MAINTENANCE: list[tuple[str, str, int, str]] = [
    ("clean_bed", "Vệ sinh bàn in (nước rửa chén hoặc cồn IPA)", 25, "Dầu tay và bụi trên bàn là lý do hàng đầu làm bong lớp đầu."),
    ("nozzle", "Kiểm tra mòn / thay đầu phun", 300, "Giờ in nhựa sợi carbon/thủy tinh được tính gấp 3."),
    ("lubricate", "Vệ sinh và bôi trơn trục, ray theo hướng dẫn Bambu cho dòng máy", 200, "Trục khô gây tiếng ồn, gợn sóng bề mặt."),
    ("belts", "Kiểm tra độ căng dây đai", 500, "Dây chùng gây gợn sóng và sai kích thước."),
]
MAINT_INDEX = {task: (label, hours, why) for task, label, hours, why in MAINTENANCE}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts(text: str | None) -> float | None:
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) if text else None
    except (TypeError, ValueError):
        return None


def migrate(db) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS pending_sends (
        id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, printer_id TEXT NOT NULL, plate_index INTEGER,
        job_name TEXT, used_settings TEXT, slice_stats TEXT, created_at TEXT NOT NULL, run_id TEXT);
    CREATE TABLE IF NOT EXISTS spools (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, vendor TEXT, material TEXT, preset TEXT, colour TEXT,
        total_g REAL, remaining_g REAL, opened_at TEXT, dried_at TEXT, printer_id TEXT, ams_id INTEGER,
        slot_id INTEGER, notes TEXT, archived INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS maintenance_log (
        id TEXT PRIMARY KEY, printer_id TEXT NOT NULL, task TEXT NOT NULL, done_at TEXT NOT NULL, note TEXT);
    CREATE TABLE IF NOT EXISTS maintenance_intervals (
        printer_id TEXT NOT NULL, task TEXT NOT NULL, hours REAL NOT NULL, PRIMARY KEY (printer_id, task));
    CREATE TABLE IF NOT EXISTS environment_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sensor_id TEXT NOT NULL, printer_id TEXT, created_at TEXT NOT NULL,
        temperature REAL, humidity REAL);
    CREATE INDEX IF NOT EXISTS env_time ON environment_readings(created_at);
    CREATE TABLE IF NOT EXISTS calibration_log (
        id TEXT PRIMARY KEY, preset TEXT NOT NULL, printer_model TEXT, values_json TEXT NOT NULL, notes TEXT,
        library_id TEXT, created_at TEXT NOT NULL);
    """)
    cols = {r[1] for r in db.execute("PRAGMA table_info(print_runs)")}
    for key in ("ai_review", "used_settings", "slice_stats", "trays", "spool_usage", "last_progress"):
        if key not in cols:
            db.execute(f"ALTER TABLE print_runs ADD COLUMN {key} TEXT")


# ---------------------------------------------------------------------------
# Auto-link Studio sends to the job that starts next on that printer
# ---------------------------------------------------------------------------

USED_KEYS = [k for group in ("process", "support_adhesion", "speed_accel", "filament", "cooling_retraction")
             for k in insight.PRIORITY_KEYS[group]]


def record_send(db, analysis_id: str, printer_id: str, plate_index: int | None, job_name: str,
                effective: dict, slice_stats: dict | None) -> str:
    used = {k: effective[k] for k in USED_KEYS if k in effective}
    send_id = str(uuid.uuid4())
    db.execute("INSERT INTO pending_sends (id,analysis_id,printer_id,plate_index,job_name,used_settings,slice_stats,created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (send_id, analysis_id, printer_id, plate_index, job_name[:200],
                                            json.dumps(used), json.dumps(slice_stats) if slice_stats else None, _now()))
    db.commit()
    return send_id


def link_new_run(db, run_id: str, printer_id: str, plate_index: int | None, job_name: str, now: str) -> bool:
    """Attach a job that just started to the latest unclaimed Studio send for this printer."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((_ts(now) or time.time()) - LINK_WINDOW_S))
    row = db.execute("SELECT p.*, a.model_key FROM pending_sends p JOIN analyses a ON a.id=p.analysis_id "
                     "WHERE p.printer_id=? AND p.run_id IS NULL AND p.created_at>=? AND p.created_at<=? "
                     "ORDER BY p.created_at DESC LIMIT 1", (printer_id, cutoff, now)).fetchone()
    if not row or (plate_index and row["plate_index"] and plate_index != row["plate_index"]):
        return False
    stem = re.sub(r"\.3mf$", "", row["job_name"] or "", flags=re.I).strip().lower()
    confidence = "high" if stem and job_name and (stem in job_name.lower() or job_name.lower() in stem) else "medium"
    db.execute("UPDATE print_runs SET analysis_id=?, model_key=?, linkage_method='studio_send', linkage_confidence=?, "
               "used_settings=?, slice_stats=? WHERE id=?",
               (row["analysis_id"], row["model_key"], confidence, row["used_settings"], row["slice_stats"], run_id))
    db.execute("UPDATE pending_sends SET run_id=? WHERE id=?", (run_id, row["id"]))
    return True


def note_tray(db, run: dict, telemetry: dict) -> None:
    """Remember which AMS trays fed this job (for spool accounting) and the last progress."""
    ams = telemetry.get("ams") or {}
    trays = json.loads(run.get("trays") or "[]")
    a, s = ams.get("active_ams_id"), ams.get("active_slot_id")
    if isinstance(a, int) and isinstance(s, int) and [a, s] not in trays and len(trays) < 16:
        trays.append([a, s])
    progress = telemetry.get("progress")
    db.execute("UPDATE print_runs SET trays=?, last_progress=COALESCE(?, last_progress) WHERE id=?",
               (json.dumps(trays), str(progress) if isinstance(progress, (int, float)) else None, run["id"]))


def consume(db, run_id: str) -> list[dict]:
    """Subtract the job's filament from the spool in the tray that printed it (when unambiguous)."""
    run = db.execute("SELECT * FROM print_runs WHERE id=?", (run_id,)).fetchone()
    if not run or not run["slice_stats"] or run["spool_usage"]:
        return []
    try:
        stats = json.loads(run["slice_stats"])
    except ValueError:
        return []
    used = [f for f in stats.get("filaments", []) if isinstance(f, dict) and (f.get("weight_g") or 0) > 0]
    trays = json.loads(run["trays"] or "[]")
    share = 1.0
    if run["status"] != "completed":
        try:
            share = max(0.0, min(1.0, float(run["last_progress"] or 0) / 100))
        except ValueError:
            share = 0.0
    usage = []
    if len(used) == 1 and len(trays) == 1:
        grams = round(used[0]["weight_g"] * share, 1)
        spool = db.execute("SELECT * FROM spools WHERE printer_id=? AND ams_id=? AND slot_id=? AND archived=0",
                           (run["printer_id"], trays[0][0], trays[0][1])).fetchone()
        if spool:
            remaining = max(0.0, (spool["remaining_g"] or 0) - grams)
            db.execute("UPDATE spools SET remaining_g=?, updated_at=? WHERE id=?", (remaining, _now(), spool["id"]))
            usage.append({"spool_id": spool["id"], "name": spool["name"], "grams": grams, "remaining_g": round(remaining, 1)})
        else:
            usage.append({"spool_id": None, "grams": grams, "tray": trays[0], "note": "khay chưa gán cuộn"})
    elif used:
        usage.append({"spool_id": None, "grams": round(sum(f["weight_g"] for f in used) * share, 1),
                      "note": "nhiều nhựa/khay: chưa trừ tự động"})
    db.execute("UPDATE print_runs SET spool_usage=? WHERE id=?", (json.dumps(usage), run_id))
    return usage


# ---------------------------------------------------------------------------
# Spools
# ---------------------------------------------------------------------------

def _date(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} phải dạng YYYY-MM-DD")
    return value


def save_spool(db, data: dict) -> dict:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("Cần tên cuộn nhựa (tối đa 120 ký tự)")
    def num(key, low, high):
        value = data.get(key)
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{key} phải là số từ {low} đến {high}")
        return float(value)
    total, remaining = num("total_g", 1, 20000), num("remaining_g", 0, 20000)
    if total is not None and remaining is not None and remaining > total:
        raise ValueError("Khối lượng còn lại lớn hơn tổng")
    colour = data.get("colour") or None
    if colour and (not isinstance(colour, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour)):
        raise ValueError("Màu phải dạng #RRGGBB")
    printer_id = data.get("printer_id") or None
    ams_id, slot_id = data.get("ams_id"), data.get("slot_id")
    if printer_id is not None:
        if not isinstance(printer_id, str) or len(printer_id) > 64:
            raise ValueError("printer_id không hợp lệ")
        if type(ams_id) is not int or type(slot_id) is not int or not 0 <= ams_id <= 255 or not 0 <= slot_id <= 3:
            raise ValueError("Cần khay AMS (ams_id 0–255, slot_id 0–3) khi gán máy")
    else:
        ams_id = slot_id = None
    text = lambda k, n: (str(data.get(k)).strip()[:n] if data.get(k) not in (None, "") else None)
    row = {"name": name.strip(), "vendor": text("vendor", 60), "material": text("material", 30), "preset": text("preset", 120),
           "colour": colour, "total_g": total, "remaining_g": remaining if remaining is not None else total,
           "opened_at": _date(data.get("opened_at"), "opened_at"), "dried_at": _date(data.get("dried_at"), "dried_at"),
           "printer_id": printer_id, "ams_id": ams_id, "slot_id": slot_id, "notes": text("notes", 500)}
    now = _now()
    if printer_id is not None:  # one active spool per tray
        db.execute("UPDATE spools SET printer_id=NULL, ams_id=NULL, slot_id=NULL, updated_at=? "
                   "WHERE printer_id=? AND ams_id=? AND slot_id=? AND id<>?", (now, printer_id, ams_id, slot_id, data.get("id") or ""))
    if data.get("id"):
        if not db.execute("SELECT 1 FROM spools WHERE id=?", (data["id"],)).fetchone():
            raise LookupError("Không có cuộn này")
        db.execute("UPDATE spools SET " + ",".join(f"{k}=?" for k in row) + ",updated_at=? WHERE id=?",
                   (*row.values(), now, data["id"]))
        spool_id = data["id"]
    else:
        spool_id = str(uuid.uuid4())
        db.execute("INSERT INTO spools (id," + ",".join(row) + ",created_at,updated_at) VALUES (?," +
                   ",".join("?" * len(row)) + ",?,?)", (spool_id, *row.values(), now, now))
    db.commit()
    return spool(db, spool_id)


def spool(db, spool_id: str) -> dict:
    row = db.execute("SELECT * FROM spools WHERE id=?", (spool_id,)).fetchone()
    return describe_spool(dict(row)) if row else None


def describe_spool(row: dict, today: float | None = None) -> dict:
    today = today or time.time()
    poly = insight.polymer_of(row.get("material"), row.get("preset") or row.get("name"))
    level = insight.MATERIALS.get(poly or "", {}).get("hygroscopic", 0)
    since = row.get("dried_at") or row.get("opened_at")
    age = None
    if since:
        age = int((today - time.mktime(time.strptime(since, "%Y-%m-%d"))) // 86400)
    limit = DRY_AFTER_DAYS.get(min(level, 3)) if level >= 2 else None
    row.update({"polymer": poly, "hygroscopic": level, "days_since_dry": age,
                "needs_drying": bool(limit is not None and age is not None and age > limit),
                "dry_after_days": limit})
    return row


def list_spools(db, include_archived: bool = False) -> list[dict]:
    rows = db.execute("SELECT * FROM spools" + ("" if include_archived else " WHERE archived=0") +
                      " ORDER BY archived, printer_id IS NULL, printer_id, ams_id, slot_id, name").fetchall()
    return [describe_spool(dict(r)) for r in rows]


def mark_dried(db, spool_id: str, day: str | None = None) -> dict:
    day = _date(day, "dried_at") or time.strftime("%Y-%m-%d", time.gmtime())
    if not db.execute("UPDATE spools SET dried_at=?, updated_at=? WHERE id=?", (day, _now(), spool_id)).rowcount:
        raise LookupError("Không có cuộn này")
    db.commit()
    return spool(db, spool_id)


def archive_spool(db, spool_id: str) -> None:
    if not db.execute("UPDATE spools SET archived=1, printer_id=NULL, ams_id=NULL, slot_id=NULL, updated_at=? WHERE id=?",
                      (_now(), spool_id)).rowcount:
        raise LookupError("Không có cuộn này")
    db.commit()


def spools_for_printer(db, printer_id: str) -> list[dict]:
    return [describe_spool(dict(r)) for r in db.execute(
        "SELECT * FROM spools WHERE printer_id=? AND archived=0 ORDER BY ams_id, slot_id", (printer_id,)).fetchall()]


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

ABRASIVE = re.compile(r"(?<![A-Z])(CF|GF)(?![A-Z])")


def maintenance_status(db, printer_id: str) -> list[dict]:
    custom = {r["task"]: r["hours"] for r in db.execute("SELECT task, hours FROM maintenance_intervals WHERE printer_id=?", (printer_id,))}
    out = []
    for task, label, default, why in MAINTENANCE:
        last = db.execute("SELECT done_at FROM maintenance_log WHERE printer_id=? AND task=? ORDER BY done_at DESC LIMIT 1",
                          (printer_id, task)).fetchone()
        since = last["done_at"] if last else None
        rows = db.execute("SELECT duration_seconds, material FROM print_runs WHERE printer_id=? AND ended_at IS NOT NULL" +
                          (" AND ended_at>?" if since else ""), (printer_id, since) if since else (printer_id,)).fetchall()
        hours = 0.0
        for r in rows:
            h = (r["duration_seconds"] or 0) / 3600
            if task == "nozzle" and ABRASIVE.search(str(r["material"] or "").upper()):
                h *= 3
            hours += h
        interval = custom.get(task, default)
        out.append({"task": task, "label": label, "why": why, "interval_hours": interval, "hours": round(hours, 1),
                    "ratio": round(hours / interval, 2) if interval else 0, "due": hours >= interval,
                    "last_done": since, "tracked_since_first_print": since is None})
    return out


def maintenance_done(db, printer_id: str, task: str, note: str = "") -> dict:
    if task not in MAINT_INDEX:
        raise ValueError("Không có hạng mục bảo trì này")
    db.execute("INSERT INTO maintenance_log (id,printer_id,task,done_at,note) VALUES (?,?,?,?,?)",
               (str(uuid.uuid4()), printer_id, task, _now(), (note or "")[:300]))
    db.commit()
    return next(t for t in maintenance_status(db, printer_id) if t["task"] == task)


def set_interval(db, printer_id: str, task: str, hours: Any) -> None:
    if task not in MAINT_INDEX:
        raise ValueError("Không có hạng mục bảo trì này")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= 5000:
        raise ValueError("Chu kỳ phải từ 1 đến 5000 giờ in")
    db.execute("INSERT INTO maintenance_intervals (printer_id,task,hours) VALUES (?,?,?) "
               "ON CONFLICT(printer_id,task) DO UPDATE SET hours=excluded.hours", (printer_id, task, float(hours)))
    db.commit()


# ---------------------------------------------------------------------------
# Room environment
# ---------------------------------------------------------------------------

def record_reading(db, data: dict) -> dict:
    sensor = data.get("sensor_id")
    if not isinstance(sensor, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", sensor):
        raise ValueError("sensor_id 1–40 ký tự chữ/số/._-")
    temp, hum = data.get("temperature"), data.get("humidity")
    for key, value, low, high in (("temperature", temp, -20, 70), ("humidity", hum, 0, 100)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not low <= value <= high):
            raise ValueError(f"{key} ngoài khoảng {low}–{high}")
    if temp is None and hum is None:
        raise ValueError("Cần temperature hoặc humidity")
    printer_id = data.get("printer_id")
    if printer_id is not None and (not isinstance(printer_id, str) or len(printer_id) > 64):
        raise ValueError("printer_id không hợp lệ")
    now = _now()
    db.execute("INSERT INTO environment_readings (sensor_id,printer_id,created_at,temperature,humidity) VALUES (?,?,?,?,?)",
               (sensor, printer_id, now, temp, hum))
    db.execute("DELETE FROM environment_readings WHERE created_at<?",
               (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400)),))
    db.commit()
    return {"sensor_id": sensor, "printer_id": printer_id, "created_at": now, "temperature": temp, "humidity": hum}


def latest_environment(db, printer_id: str | None = None, max_age_s: int = 1800) -> dict | None:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max_age_s))
    row = None
    if printer_id:
        row = db.execute("SELECT * FROM environment_readings WHERE printer_id=? AND created_at>=? ORDER BY id DESC LIMIT 1",
                         (printer_id, cutoff)).fetchone()
    if not row:
        row = db.execute("SELECT * FROM environment_readings WHERE printer_id IS NULL AND created_at>=? ORDER BY id DESC LIMIT 1",
                         (cutoff,)).fetchone()
    return dict(row) if row else None


def environment_overview(db, hours: int = 24) -> dict:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    sensors = {}
    for row in db.execute("SELECT * FROM environment_readings WHERE created_at>=? ORDER BY id", (cutoff,)):
        s = sensors.setdefault(row["sensor_id"], {"sensor_id": row["sensor_id"], "printer_id": row["printer_id"], "series": []})
        s["series"].append([row["created_at"], row["temperature"], row["humidity"]])
        s["latest"] = {"created_at": row["created_at"], "temperature": row["temperature"], "humidity": row["humidity"]}
        s["printer_id"] = row["printer_id"]
    for s in sensors.values():
        if len(s["series"]) > 288:  # about one point per 5 min over a day
            step = len(s["series"]) / 288
            s["series"] = [s["series"][int(i * step)] for i in range(288)]
    return {"sensors": list(sensors.values())}


# ---------------------------------------------------------------------------
# Vision decisions
# ---------------------------------------------------------------------------

def failure_decision(current: dict | None, previous: dict | None) -> str | None:
    """Failure kind to act on: confirmed by two frames, or one very confident frame."""
    cur = (current or {}).get("failure") or {}
    kind, conf = cur.get("kind"), cur.get("confidence") or 0
    if kind not in FAILURE_KINDS or conf < 0.6:
        return None
    if conf >= 0.9:
        return kind
    prev = (previous or {}).get("failure") or {}
    return kind if prev.get("kind") == kind and (prev.get("confidence") or 0) >= 0.6 else None


# ---------------------------------------------------------------------------
# Per-filament calibration (results saved into the filament library)
# ---------------------------------------------------------------------------

CALIBRATION_KEYS = ["nozzle_temperature", "filament_flow_ratio", "pressure_advance", "filament_retraction_length",
                    "filament_max_volumetric_speed"]


def calibration_guide(profile: dict | None, ams: dict | None, material: str | None) -> dict:
    """Steps using Studio's own Calibration menu, with ranges from the vendor profile."""
    p = profile or {}
    low, high = p.get("nozzle_temperature_range_low"), p.get("nozzle_temperature_range_high")
    k_values = []
    for unit in (ams or {}).get("units", []):
        for slot in unit.get("slots", []):
            if slot.get("present") and slot.get("flow_k") is not None and (
                    not material or insight.polymer_of(slot.get("material"), slot.get("filament_name")) == material):
                k_values.append({"slot": f"{unit.get('name') or 'AMS'} khay {slot.get('id', 0) + 1}", "material": slot.get("material"),
                                 "k": slot.get("flow_k"), "cali_idx": slot.get("cali_idx")})
    retract = p.get("filament_retraction_length")
    steps = [
        {"key": "nozzle_temperature", "title": "Nhiệt độ (tháp nhiệt)",
         "how": "Bambu Studio → Calibration → Temperature. In tháp, chọn tầng có cầu nối và bề mặt đẹp nhất, ít kéo sợi nhất.",
         "suggest": {"from": low, "to": high, "step": 5, "current": p.get("nozzle_temperature")}},
        {"key": "pressure_advance", "title": "Flow Dynamics (hệ số K / pressure advance)",
         "how": "Bật 'Flow Dynamics Calibration' khi gửi in, hoặc Calibration → Flow Dynamics. Máy lưu K theo từng khay; "
                "ghi K vào đây để preset mang theo khi in máy khác.",
         "suggest": {"current_on_printer": k_values, "current": p.get("pressure_advance")}},
        {"key": "filament_flow_ratio", "title": "Hệ số dòng chảy (Flow Rate)",
         "how": "Calibration → Flow Rate (2 bước thô/tinh). Chọn ô mặt trên phẳng mịn nhất.",
         "suggest": {"current": p.get("filament_flow_ratio")}},
        {"key": "filament_retraction_length", "title": "Rút sợi (chống kéo sợi)",
         "how": "Calibration → Retraction (nếu có trong phiên bản), hoặc in mẫu kéo sợi với vài mức rút sợi. Chọn mức thấp nhất hết kéo sợi.",
         "suggest": {"current": retract, "from": max(0.2, round((retract or 0.8) - 0.6, 1)), "to": round((retract or 0.8) + 0.6, 1), "step": 0.2}},
        {"key": "filament_max_volumetric_speed", "title": "Lưu lượng tối đa",
         "how": "Calibration → Max Volumetric Speed. Ghi mức cao nhất trước khi bề mặt bị thiếu nhựa, rồi trừ 10–20% cho an toàn.",
         "suggest": {"current": p.get("filament_max_volumetric_speed")}},
    ]
    return {"material": material, "profile": p.get("preset"), "steps": steps,
            "note": "Tên mục trong menu Calibration có thể khác đôi chút giữa các phiên bản Bambu Studio."}


def calibration_name(preset: str) -> str:
    return preset.replace(" @BBL", " hiệu chỉnh @BBL", 1) if " @BBL" in preset else preset + " hiệu chỉnh"


def save_calibration(root, db, data: dict) -> dict:
    """Store measured values as a library preset (new one for a system preset, update for a custom one)."""
    import filaments
    preset = data.get("preset")
    values = data.get("values") or {}
    if not isinstance(preset, str) or not preset:
        raise ValueError("Cần preset đã hiệu chỉnh")
    if not isinstance(values, dict) or not values or any(k not in CALIBRATION_KEYS for k in values):
        raise ValueError(f"Chỉ lưu các giá trị: {', '.join(CALIBRATION_KEYS)}")
    notes = data.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > 500:
        raise ValueError("Ghi chú tối đa 500 ký tự")
    row = db.execute("SELECT * FROM filament_library WHERE name=?", (preset,)).fetchone()
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    if row and row["source"] == "custom":
        body = json.loads(row["body"])
        merged = {k: insight.number(v) for k, v in body.items() if k in filaments.CUSTOM_KEYS}
        merged.update(values)
        saved = filaments.create_custom(root, db, {"id": row["id"], "name": row["name"], "vendor": row["vendor"],
                                                   "base": row["inherits"], "values": merged,
                                                   "notes": f"{row['notes'] or ''}\nHiệu chỉnh {stamp}: {notes}".strip()[:1000]})
    else:
        presets = insight.filament_profiles(root)
        if preset not in presets:
            raise ValueError("Preset không có trong Bambu Studio hoặc thư viện (preset đã nhập từ file: hãy tạo bản tùy chỉnh trước)")
        name = calibration_name(preset)
        existing = db.execute("SELECT id FROM filament_library WHERE name=?", (name,)).fetchone()
        vendor = str(insight.scalar(presets[preset].get("filament_vendor")) or "") or \
            str(insight.scalar(filaments._resolve(presets, preset).get("filament_vendor")) or "Generic")
        saved = filaments.create_custom(root, db, {"id": existing["id"] if existing else None, "name": name, "vendor": vendor,
                                                   "base": preset, "values": values,
                                                   "notes": f"Hiệu chỉnh {stamp} trên {data.get('printer_model') or 'máy'}: {notes}".strip()})
    db.execute("INSERT INTO calibration_log (id,preset,printer_model,values_json,notes,library_id,created_at) VALUES (?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), preset, str(data.get("printer_model") or "")[:60], json.dumps(values), notes, saved["id"], _now()))
    db.commit()
    return saved
