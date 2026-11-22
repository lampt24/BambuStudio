# PrintPilot A1 Camera Bridge (Android / Termux)

Pure Python 3 standard library bridge designed to run on a dedicated Android device (e.g., Redmi phone) in Termux. It captures periodic chamber camera snapshots from a Bambu Lab A1 / A1 mini / P1 printer over TLS port 6000 and posts them securely to the PrintPilot backend.

> [!IMPORTANT]
> **Cloud Connection & Bambu Handy (LAN Only Mode NOT Required):**
> The printer **must remain CLOUD-connected** so that the Bambu Handy mobile app continues working normally (remote printing, monitoring, and mobile notifications).
> **LAN Only mode is NOT required** for local camera snapshot reading. The printer's local TLS camera server on port 6000 remains fully accessible using the printer's Access Code while the printer stays connected to Bambu Cloud. **Do NOT enable LAN Only mode on the printer.**

> [!WARNING]
> **Firmware Compatibility Note:**
> This bridge implements the Bambu Lab local camera TLS protocol (TCP port 6000 with `bblp` authentication and binary framing). Exact firmware compatibility across various Bambu Lab firmware versions remains **unknown until hardware verification on the target printer**.

> [!CAUTION]
> **Manager-authorized controls:**
> The bridge reads camera/MQTT telemetry and executes server-queued pause, resume, stop, chamber light, speed, and material-setting commands. Material edits require fresh idle telemetry, an occupied non-RFID slot, validated profile fields, an explicit success ACK, and matching fresh telemetry readback. Unknown RFID is read-only. No arbitrary G-code is accepted.

---

## Architecture & Protocol Overview

Bambu Lab A1, A1 mini, P1P, and P1S printers do not serve standard RTSP streams. Instead, they expose a TLS-encrypted TCP streaming protocol on **port 6000**.

### Protocol Source References
The protocol implementation in this bridge is based on and verified against:
1. **Bambu Studio Local Camera Stream Client**: Bambu Studio's native connection URI format `bambu:///local/{devIP}?port=6000&user=bblp&passwd={accessCode}` (see `src/slic3r/GUI/Jobs/PrintJob.cpp`, `src/slic3r/GUI/ConnectPrinter.cpp`, and `src/slic3r/GUI/PartSkipDialog.cpp`).
2. **Bambu Networking Plugin & BambuSource**: The binary handshake and frame parsing logic used by Bambu Lab's media player pipeline.
3. **Community Protocol Specifications**: Home Assistant Bambu Lab integration (`ha-bambulab`) and `bambu-farm` protocol documentation for local port 6000 TLS framing and authentication.

### 1. Port 6000 Authentication Packet
Upon establishing a TLS connection to `{printer_ip}:6000`, the client immediately sends an **80-byte binary authentication packet**:

| Offset | Length | Type / Value | Description |
|---|---|---|---|
| `0x00` | 4 bytes | `<I` (uint32 LE) `0x00000040` | Payload size following header (64 bytes) |
| `0x04` | 4 bytes | `<I` (uint32 LE) `0x00003000` | Packet type (Auth handshake) |
| `0x08` | 4 bytes | `<I` (uint32 LE) `0x00000000` | Flags |
| `0x0C` | 4 bytes | `<I` (uint32 LE) `0x00000000` | Reserved |
| `0x10` | 32 bytes | `32s` (ASCII, null-padded) | Username: `bblp\x00...` |
| `0x30` | 32 bytes | `32s` (ASCII, null-padded) | Printer Access Code: `{dev_access_code}\x00...` |

> [!NOTE]
> The Access Code is located on the printer screen under **Settings > WLAN / Network** (it is labeled "Access Code" or "LAN Access Code", but the printer does **not** need to be in LAN-only mode).

### 2. Video Frame Framing
Following the authentication handshake, the printer streams JPEG frames. Each frame begins with a **16-byte binary header**:

| Offset | Length | Type / Value | Description |
|---|---|---|---|
| `0x00` | 4 bytes | `<I` (uint32 LE) `payload_size` | Length in bytes of the incoming JPEG payload |
| `0x04` | 4 bytes | `<I` (uint32 LE) `itrack` | Video track index (typically `0`) |
| `0x08` | 4 bytes | `<I` (uint32 LE) `reserved1` | Reserved / metadata |
| `0x0C` | 4 bytes | `<I` (uint32 LE) `reserved2` | Reserved / metadata |

Immediately following the 16-byte header are `payload_size` bytes of raw JPEG image data, demarcated by:
- Start of Image (SOI): `FF D8`
- End of Image (EOI): `FF D9`

---

## Key Features & Robustness

1. **Standard Library Only:** Zero external pip dependencies (`urllib.request`, `ssl`, `socket`, `struct`, `json`, `hashlib`, `logging`). Ideal for minimal, battery-conscious Android Termux execution.
2. **Fragmented Stream Reassembly:** Correctly buffers partial TCP/TLS socket reads (`read_exact`), handling packet fragmentation across arbitrary network chunk boundaries without stream corruption.
3. **Bounded Reads:** Guarantees memory safety by strictly enforcing `max_frame_bytes` (default 5 MiB). Any frame header advertising payloads outside legitimate bounds is rejected immediately before reading.
4. **Fresh Snapshot Guarantee:** Discards initial stale/buffered frames that the printer often pushes immediately on socket connection (`discard_initial_frame=True`), ensuring snapshots accurately reflect live print status.
5. **Accurate Telemetry Status (`unknown`):** The camera bridge alone cannot infer whether the printer is printing or idle; snapshots and heartbeats are posted with status `unknown` until MQTT telemetry is integrated.
6. **Error Retention Across Heartbeats:** Periodic heartbeats between snapshot cycles retain the last camera error rather than falsely clearing it with an `idle` state.
7. **Config-Incomplete Mode:** If the bridge is started without printer credentials (waiting for IP/access code), the service runs safely without crashing or probing dummy hosts, sending `unknown` status with error `printer not configured` to the backend.
8. **TLS Security & Fingerprint Pinning:**
   - **SHA-256 Fingerprint Pinning:** Optionally pins the exact SHA-256 certificate fingerprint of the printer's self-signed TLS cert (`cert_fingerprint`), aborting immediately on mismatch.
   - **Explicit Local CA Trust:** Allows supplying a custom CA certificate PEM (`ca_cert_path`).
   - **Unverified Opt-In:** Supports `insecure_skip_verify: true` for development with self-signed printer certificates.
9. **External Credentials (Zero Secrets in Git):** Credentials reside exclusively in an external config file (`~/.config/printpilot/bridge.json`) with `chmod 600` permissions or environment variables. All log and heartbeat messages sanitize authorization tokens and access codes.
10. **Network Retries with Backoff:** Automatically retries transient connection drops and socket timeouts with exponential backoff and jitter. Fatal security errors (fingerprint mismatches, auth errors) are never retried blindly.

---

## Directory Structure

```
services/printpilot/bridge/
├── __init__.py               # Package metadata
├── camera.py                 # BambuCameraClient with TLS and fingerprint verification
├── client.py                 # PrintPilotApiClient for /api/snapshots and /api/heartbeat
├── config.py                 # BridgeConfig and external file/env loading
├── config.example.json       # Template configuration (NO SECRETS)
├── exceptions.py             # Domain exception hierarchy
├── install_termux.sh         # Termux installer script
├── main.py                   # Main service loop and CLI entrypoint
├── protocol.py               # Binary protocol packing/unpacking and bounded read logic
├── README.md                 # This documentation
├── runit/                    # Termux runit service definition
│   ├── run                   # Service execution script
│   └── log/
│       └── run               # svlogd logger script
└── tests/                    # Comprehensive unit tests
    ├── __init__.py
    ├── test_camera.py        # TLS, fingerprint, and mock frame capture tests
    ├── test_client.py        # HTTP client, bearer auth, and error sanitization tests
    ├── test_config.py        # Config loading, redaction, and env override tests
    ├── test_main.py          # Bridge service coordinator and run_once tests
    └── test_protocol.py      # Fragmentation, bounded reads, header & auth packet tests
```

---

## Configuration Reference

Copy `config.example.json` to `~/.config/printpilot/bridge.json`:

```json
{
  "printer_host": "192.168.1.100",
  "printer_port": 6000,
  "printer_username": "bblp",
  "printer_access_code": "12345678",
  "printer_id": "a1-01",

  "server_url": "http://100.64.0.1:8000",
  "device_id": "redmi-bridge-01",
  "device_token": "your_device_bearer_token_here",

  "cert_fingerprint": null,
  "ca_cert_path": null,
  "insecure_skip_verify": true,

  "snapshot_interval_seconds": 120,
  "heartbeat_interval_seconds": 30,
  "max_frame_bytes": 5242880,
  "connect_timeout": 10.0,
  "read_timeout": 15.0,
  "max_retries": 3,
  "discard_initial_frame": true
}
```

### Environment Variable Overrides
All settings can also be set or overridden via environment variables:
- `PRINTPILOT_PRINTER_HOST`
- `PRINTPILOT_PRINTER_ACCESS_CODE`
- `PRINTPILOT_PRINTER_ID`
- `PRINTPILOT_DEVICE_ID`
- `PRINTPILOT_SERVER_URL`
- `PRINTPILOT_DEVICE_TOKEN`
- `PRINTPILOT_CERT_FINGERPRINT`
- `PRINTPILOT_CA_CERT_PATH`
- `PRINTPILOT_INSECURE_SKIP_VERIFY`
- `PRINTPILOT_SNAPSHOT_INTERVAL`

---

## Installation & Running on Termux

### 1. Run the Installer
On your Android device running Termux:
```bash
bash services/printpilot/bridge/install_termux.sh
```

The installer preserves existing configurations and service definitions, creates required directories, and installs `python` and `termux-services`.

### 2. Configure Credentials
Edit the private configuration file:
```bash
nano ~/.config/printpilot/bridge.json
chmod 600 ~/.config/printpilot/bridge.json
```

> [!TIP]
> Keep the printer connected to your home Wi-Fi and Bambu Cloud. Find the **Access Code** on the printer display under Settings > WLAN. Do **not** enable LAN Only mode.

### 3. Test Single Capture or Foreground Run
Run a test snapshot in foreground mode:
```bash
# Capture a single frame and exit
python3 -m services.printpilot.bridge.main --config ~/.config/printpilot/bridge.json --once -v

# Run heartbeat loop only (e.g. while testing backend connectivity)
python3 -m services.printpilot.bridge.main --config ~/.config/printpilot/bridge.json --heartbeat-only -v
```

### 4. Enable and Manage Runit Background Service
Termux provides `termux-services` powered by `runit`:
```bash
# Enable service at boot/startup
sv-enable printpilot-bridge

# Start service
sv up printpilot-bridge

# Check status
sv status printpilot-bridge

# Stop service
sv down printpilot-bridge

# View live logs
tail -f ~/.config/printpilot/logs/current
```

---

## Running Unit Tests

Run the full suite of unit tests directly with Python's built-in `unittest`:

```bash
python3 -m unittest discover services/printpilot/bridge/tests/
```

AMS telemetry preserves actual unit/slot IDs and nested partial updates. `ams.units`, `ams.external`, active IDs, humidity, RGBA colors, remaining percent and profile IDs are reported. `rfid_known=false` is represented conservatively as `rfid=true`. Editing while printing is disabled until a documented device capability can be verified. Task/project/subtask IDs remain available for backend print history.
