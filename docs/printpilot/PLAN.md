# PrintPilot implementation plan

Approved scope: Bambu Studio 2.9.1 fork, A1, macOS/Windows; AI settings on explicit button and before sending; VPS analysis through 9router; Redmi camera snapshots every 120 seconds; manager alerts and responsive dashboard. Keep upstream changes minimal. Do not start, pause or stop a real printer during tests.

User clarification: keep Bambu Cloud enabled so Bambu Handy can send jobs. LAN
connectivity is not LAN Only Mode. The bridge only reads camera/telemetry; firmware
compatibility must be verified. Cloud-account setup may retrieve the device access
code after the user authenticates. Jobs sent directly from Handy are monitored but
do not pass through Studio AI optimization.

## Shared API contract
Python server, SQLite, stdlib HTTP on private Tailscale listener; static HTML/CSS/JS. All /api endpoints require Authorization: Bearer token, except GET /health. Separate device token permits only POST /api/snapshots and POST /api/heartbeat. Browser keeps manager token in sessionStorage (never URL).

POST /api/analyze JSON: {filename, project_base64, settings: object, objective: quality|strength|speed|economy, trigger: manual|send, plate_index?: 1..64}. 3MF zip includes geometry and per-object settings. Multi-plate projects: Studio sends the plate being printed (omitted for Print all). The server reads plates from Metadata/model_settings.config; with plate_index, AI geometry, thumbnail, 3D preview and model_key cover only that plate (unknown/empty plate -> 422). A single-plate project keeps the whole-project model_key. Print runs record plate_index from the printer's gcode_file (plate_N.gcode); linking a run to an analysis of a different plate -> 409. Optimized 3MF download is refused (422) for a plate-only analysis of a multi-plate project, because settings are project-wide. Max HTTP body 90 MiB, decoded project 64 MiB, bounded ZIP expansion. Returns {id, status: complete|error, summary, changes: [{key, old, value, reason}], warnings: [], model: {…}}. Server saves submitted file privately and returns only validated numeric settings from a small allowlist; never arbitrary G-code or commands. Incoming settings omit secrets. AI gets extracted bounded geometry/config data and thumbnails when supported, not blindly raw base64 ZIP. Invalid provider output or unavailable AI returns explicit error, never simulated results.

POST /api/snapshots JSON: {device_id, printer_id, captured_at: UTC ISO8601, image_base64: JPEG, status: printing|idle|unknown, telemetry: object}. Returns {id, status}. Persist picture; vision evaluation must be asynchronous or bounded. No fresh image means unknown, never healthy. POST /api/heartbeat {device_id, printer_id, status, error?}; no secret logs.
GET /api/state returns {analyses: [], snapshots: [], alerts: [], devices: [], config: {ai_configured, vision_configured, notifications_configured}}. Array entries have id/created_at and appropriate summary/status fields. GET /api/images/{id} serves authenticated JPEG. POST /api/alerts/{id}/ack sets acknowledged. Notification webhook configured only server-side; deduplicate errors and retry failed deliveries.

## Tasks and ownership
- [ ] Server: services/printpilot/server.py + test_server.py + server config example. Auth, bounded input, SQLite, extraction, AI JSON validation, vision, alerts. Test unauthorized requests, malformed ZIP/AI data, settings bounds, dedup and provider failure using local fake provider.
- [ ] Dashboard: services/printpilot/web/ only. Approved navy/green Fira design, real API data, token login, overview camera and assessment, analyses diffs, alert ack, device freshness, no fake metrics. Desktop/mobile keyboard accessible.
- [ ] Redmi: services/printpilot/bridge/ only. Python stdlib A1 TLS camera port 6000 auth/framing, configurable cert fingerprint, 120s loop, heartbeat, bounded JPEG, network retries and Termux service scripts; document firmware compatibility unknown until hardware verification. Tests for fragmented reads and bad frames. Credentials live outside repo.
- [ ] Studio: src/slic3r/GUI/AI/ plus minimal menu/send hooks and CMake entry. Export 3MF without secrets; no UI thread network blocking; show changes, explicit apply, re-slice before print; network failure offers explicit proceed/cancel. Snapshot signature prevents stale apply. Existing Http/nlohmann/wx only.
- [ ] Integration/deployment: independent services, private tokens, startup/restart, deployment docs, macOS/Windows workflow with short artifact retention. Verify server and bridge startup on actual hosts; CI build required for native clients.

## Verification and rollout
Run runnable Python checks, browser smoke check when browser available, inspect all upstream diffs, build native clients via GitHub Actions. Deploy server bound only to Tailscale, bridge to Redmi Termux. Check authenticated health/state, real provider analysis, device heartbeat and real camera when credentials supplied. Do not label untested builds or missing camera/notifications operational. Keep service logs/credentials out of Git. No unrelated service changes.

## Grounded analysis (shape, material, printer, history)

`POST /api/analyze` also accepts `capabilities: ["settings_v2"]` (Studio builds that can apply
extended settings), `printer_serial` (Studio's selected printer; matched to the bridge telemetry
`serial`) or `printer_id` (dashboard). Before calling the AI the server (`insight.py`):

- measures every placed object of the plate: size, bed contact area, height/base ratio, overhang
  area above 45°/60°, bridges/ceilings, mean thickness; faces shared by touching colour parts are
  interfaces, not overhangs;
- renders six views (ISO/FRONT/RIGHT/BACK/TOP/BOTTOM) with a mm grid, filament colours and an
  overhang heat map (orange 45–60°, red >60°, blue on the bed); sent to the AI at high detail and
  served at `GET /api/analyses/{id}/views`;
- resolves the installed filament preset (vendor values: temperature range, max volumetric speed,
  plate temperatures) and adds curated notes per polymer; knows bed-slinger/open printers;
- runs deterministic rules (floating/tall-slender/small-contact/unsupported overhang/bridges/thin,
  enclosure, abrasive nozzle, TPU in AMS, hygroscopic + AMS humidity, PETG on smooth PEI, bed
  unsupported, nozzle temperature outside the profile, speeds above the flow limit, filament not
  loaded in the selected printer's AMS); critical/warn findings are added to the warnings;
- sends quality-relevant settings by group (never truncated) and reviews of the same model plus
  models with a similar shape (`shape_signature`), same material and printer family.

The response and stored analysis carry `insights` (objects, findings, material, printer, views).
Every change may carry `evidence`.

Extended settings (`tuning.py`, mirrored in `PrintPilotValidation.hpp`): enable_support,
support_type, support_threshold_angle, seam_position, sparse_infill_pattern, brim_type,
outer_wall_acceleration, top_surface_speed, initial_layer_speed and, for the plate's single
filament, nozzle temperatures (bounded by the profile range), fans, slow_down_layer_time and the
bed temperature of the bed in use. Changes are `{key, old, value, reason, evidence, scope,
filament_index}`. They reach only `settings_v2` clients and dashboard uploads (written into the
optimized 3MF); other clients get them as warnings. Studio applies filament changes only while the
Filament tab edits that filament, otherwise it lists them as "set by hand".

## Filament profiles

TINMORRY's official presets are imported as BBL system presets under
`resources/profiles/BBL/filament/TINMORRY/` by `scripts/printpilot/import_tinmorry.py`
(source commits and skipped files in `scripts/printpilot/tinmorry_manifest.json`). They inherit
`filament_id` from the Bambu base so printers/AMS recognise the material, and get their own
`setting_id`. BBL.json is versioned 02.09.01.00 so installs reload the vendor folder and Bambu's
02.09.00.xx online updates do not replace it. To refresh: clone both TINMORRY repositories, run
the script, then `python3 scripts/check_profiles.py --vendor BBL`. The server reads the same folder,
so deployments must ship `resources/profiles/BBL/filament` including `TINMORRY/`.

## Filament library (dashboard tab "Nhựa in")

`filaments.py`, manager auth:

- `GET /api/filament-library?printer_model=A1&nozzle=0.4` (or `printer_id`): every installed
  filament preset compatible with that printer and nozzle, grouped by material type (common types
  first, with notes: hygroscopic, needs enclosure, abrasive), vendors, and the library entries.
  Presets of stock vendors are marked available in Studio; TINMORRY and library presets are
  exportable.
- `POST /api/filament-library` `{name, vendor, base, values, colour?, notes?, id?}`: create or
  update a custom preset (values from the spool label on top of an installed Bambu base; bounded
  keys only).
- `POST /api/filament-library/import` `{filename, data_base64}`: Studio `.json` presets or
  `.bbsflmt`/`.zip` bundles; presets whose parent is not installed, or that reuse a system name, are
  skipped with a reason.
- `POST /api/filament-library/{id}/delete`.
- `GET /api/filament-library/export?name=...&id=...`: `.bbsflmt` filament config bundle of Studio
  user presets (`inherits` a system preset, `filament_id` of that base so the AMS recognises the
  material). Import in Bambu Studio with File > Import > Import Configs.

Library presets also feed the analysis: a project using one of them gets its values as the
filament profile.

## Workshop: learning from real prints (`workshop.py`)

- **Auto-link.** When Studio sends a plate for printing with an analysis (`trigger: send`, printer
  known), the server records a pending send with the sliced settings and statistics
  (`slice_stats`: time, layers, grams per filament, colour changes). The next job that starts on
  that printer within 45 minutes (and on the same plate when both are known) is linked to the
  analysis (`linkage_method: studio_send`; confidence high when the job name matches the file,
  otherwise medium). Manager linking still overrides it.
- **Spools.** `GET /api/spools` (spools + AMS trays from telemetry), `POST /api/spools`
  `{name, vendor?, material?, preset?, colour?, total_g?, remaining_g?, opened_at?, dried_at?,
  printer_id?, ams_id?, slot_id?, notes?, id?}` (one active spool per tray),
  `POST /api/spools/{id}/dried`, `POST /api/spools/{id}/archive`. Finished runs subtract the slice
  grams from the spool in the tray that printed. Hygroscopic materials get a "dry again" flag
  (2 days for very hygroscopic, 14 days for hygroscopic). The analysis warns when the loaded spool
  is too light for the plate or needs drying.
- **Maintenance.** `GET /api/maintenance` gives per-printer tasks (clean bed 25 h, nozzle 300 h with
  CF/GF hours ×3, lubricate 200 h, belts 500 h) counted from real print hours.
  `POST /api/maintenance/{printer}/{task}/done` and `.../interval {hours}`. The heartbeat monitor
  raises a deduplicated `maintenance` alert (and Telegram) when a task is due; "done" acknowledges it.
- **Room environment.** Device token: `POST /api/environment {sensor_id, temperature?, humidity?,
  printer_id?}` (e.g. ESP32 + SHT31 every 5 minutes; readings kept 7 days).
  `GET /api/environment` returns the last 24 h per sensor. A fresh reading (≤ 30 min) is part of
  the analysis context (humid room + hygroscopic filament, cold room on an open printer).
- **Calibration.** `GET /api/calibration/guide?preset=&printer_id=` lists the steps of Studio's own
  Calibration menu with ranges from the profile and the K values the AMS reports.
  `POST /api/calibration {preset, values, notes?}` (nozzle_temperature, filament_flow_ratio,
  pressure_advance, filament_retraction_length, filament_max_volumetric_speed) saves them into a
  library preset ("… hiệu chỉnh @BBL …" for a system preset, in place for a custom one).
- **Vision.** During printing a failure (first-layer detach, spaghetti, knocked-off part) is acted on
  when two frames agree or one frame has confidence ≥ 0.9: critical alert + Telegram; setting
  `failure_action` (off | notify | pause, default notify) can also pause the printer through the
  bridge command queue. With
  `post_print_review` the first idle frame after a run is reviewed and pre-fills the print review.
- **Orientation and slice checks.** The analysis tries 26 "face down" orientations per object and
  reports better ones (support area saved, bed contact, height; bed-slingers prefer low parts).
  When the plate is sliced, minimum layer time, filament changes and flow are checked against the
  filament limits.

Dashboard: spools and calibration live in "Nhựa in"; the "Bảo trì" tab shows maintenance and room
sensors.
