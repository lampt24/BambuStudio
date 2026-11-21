# PrintPilot implementation plan

Approved scope: Bambu Studio 2.9.1 fork, A1, macOS/Windows; AI settings on explicit button and before sending; VPS analysis through 9router; Redmi camera snapshots every 120 seconds; manager alerts and responsive dashboard. Keep upstream changes minimal. Do not start, pause or stop a real printer during tests.

User clarification: keep Bambu Cloud enabled so Bambu Handy can send jobs. LAN
connectivity is not LAN Only Mode. The bridge only reads camera/telemetry; firmware
compatibility must be verified. Cloud-account setup may retrieve the device access
code after the user authenticates. Jobs sent directly from Handy are monitored but
do not pass through Studio AI optimization.

## Shared API contract
Python server, SQLite, stdlib HTTP on private Tailscale listener; static HTML/CSS/JS. All /api endpoints require Authorization: Bearer token, except GET /health. Separate device token permits only POST /api/snapshots and POST /api/heartbeat. Browser keeps manager token in sessionStorage (never URL).

POST /api/analyze JSON: {filename, project_base64, settings: object, objective: quality|strength|speed|economy, trigger: manual|send}. 3MF zip includes geometry and per-object settings. Max HTTP body 90 MiB, decoded project 64 MiB, bounded ZIP expansion. Returns {id, status: complete|error, summary, changes: [{key, old, value, reason}], warnings: [], model: {…}}. Server saves submitted file privately and returns only validated numeric settings from a small allowlist; never arbitrary G-code or commands. Incoming settings omit secrets. AI gets extracted bounded geometry/config data and thumbnails when supported, not blindly raw base64 ZIP. Invalid provider output or unavailable AI returns explicit error, never simulated results.

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
