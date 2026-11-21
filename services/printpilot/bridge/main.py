"""PrintPilot A1 Camera Bridge Main Service.

Captures camera snapshots from Bambu Lab A1 on port 6000 every 120s,
posts authenticated JPEG snapshots and heartbeats to PrintPilot server,
and handles retries, bounded reads, config-incomplete mode, and graceful shutdown.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

# Ensure repository root and package directory are in sys.path when invoked directly as a script
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _CURRENT_DIR not in sys.path:
    sys.path.insert(1, _CURRENT_DIR)

try:
    from .camera import BambuCameraClient
    from .client import PrintPilotApiClient, sanitize_message
    from .config import BridgeConfig, load_config
    from .exceptions import BridgeError, ConfigError, FingerprintMismatchError
    from .telemetry import TelemetryClient, status_from
    from .protocol import read_frame
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.camera import BambuCameraClient
        from services.printpilot.bridge.client import PrintPilotApiClient, sanitize_message
        from services.printpilot.bridge.config import BridgeConfig, load_config
        from services.printpilot.bridge.exceptions import BridgeError, ConfigError, FingerprintMismatchError
        from services.printpilot.bridge.telemetry import TelemetryClient, status_from
        from services.printpilot.bridge.protocol import read_frame
    except ImportError:
        from camera import BambuCameraClient
        from client import PrintPilotApiClient, sanitize_message
        from config import BridgeConfig, load_config
        from exceptions import BridgeError, ConfigError, FingerprintMismatchError
        from telemetry import TelemetryClient, status_from
        from protocol import read_frame

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("printpilot.bridge")


class BridgeService:
    """Service coordinator running the camera snapshot and heartbeat loop."""

    def __init__(self, config: BridgeConfig, heartbeat_only: bool = False):
        self.config = config
        self._heartbeat_only = heartbeat_only
        self._running = False
        self._last_error: Optional[str] = None
        self.camera: Optional[BambuCameraClient] = None
        self.telemetry: Optional[TelemetryClient] = None
        # Capture policy (server can override via heartbeat response)
        self._policy = {
            "capture_when_idle": False,
            "snapshot_interval_seconds": config.snapshot_interval_seconds,
        }
        self._was_active = False
        self.live: Optional["LiveStreamer"] = None

        self.client = PrintPilotApiClient(
            server_url=config.server_url,
            device_token=config.device_token,
            timeout=config.read_timeout,
        )

        if not self.config.is_printer_configured():
            self._last_error = "printer not configured"
            logger.warning(
                "Printer configuration is incomplete (waiting for IP/access code); "
                "running in config-incomplete mode with safe heartbeat."
            )
        elif self._heartbeat_only:
            logger.info("Running in heartbeat-only mode; camera capture disabled.")
        else:
            self._init_camera()
            if self.config.telemetry_enabled:
                self.telemetry = TelemetryClient(
                    host=self.config.printer_host,
                    access_code=self.config.printer_access_code,
                    serial=self.config.printer_serial,
                    cert_fingerprint=self.config.cert_fingerprint,
                    connect_timeout=self.config.connect_timeout,
                )

    def _printer_state(self):
        """(status, telemetry) from MQTT; 'unknown' when no fresh telemetry."""
        tele = self.telemetry.snapshot() if self.telemetry else {}
        return (status_from(tele) if tele else "unknown"), tele

    def _apply_policy(self, response) -> None:
        policy = response.get("policy") if isinstance(response, dict) else None
        if not isinstance(policy, dict):
            return
        idle = policy.get("capture_when_idle")
        if isinstance(idle, bool):
            self._policy["capture_when_idle"] = idle
        interval = policy.get("snapshot_interval_seconds")
        if isinstance(interval, int) and not isinstance(interval, bool) and 30 <= interval <= 3600:
            self._policy["snapshot_interval_seconds"] = interval

    def snapshot_interval(self) -> int:
        return int(self._policy["snapshot_interval_seconds"])

    def should_capture(self) -> bool:
        """Only spend camera/upload/AI work while a job is active.

        - printing / paused / error: capture
        - idle right after a job: one final capture of the finished print
        - idle otherwise: only if the server enables capture_when_idle
        - unknown (no telemetry yet): capture, since activity cannot be ruled out
        """
        status, _ = self._printer_state()
        if status in ("printing", "paused", "error"):
            self._was_active = True
            return True
        if status == "idle":
            if self._was_active:
                self._was_active = False
                return True
            return bool(self._policy["capture_when_idle"])
        return True

    def _heartbeat(self, error: Optional[str] = None) -> None:
        status, tele = self._printer_state()
        response = self.client.send_heartbeat(
            device_id=self.config.device_id,
            printer_id=self.config.printer_id,
            status=status,
            error=error,
            telemetry=tele or None,
            printer_name=self.config.printer_name,
            printer_model=self.config.printer_model,
        )
        self._apply_policy(response)

    def _init_camera(self) -> None:
        """Initialize camera client after validating printer settings."""
        self.config.validate_printer()
        self.camera = BambuCameraClient(
            host=self.config.printer_host,
            access_code=self.config.printer_access_code,
            port=self.config.printer_port,
            username=self.config.printer_username,
            cert_fingerprint=self.config.cert_fingerprint,
            ca_cert_path=self.config.ca_cert_path,
            insecure_skip_verify=self.config.insecure_skip_verify,
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            max_frame_bytes=self.config.max_frame_bytes,
            discard_initial_frame=self.config.discard_initial_frame,
        )

    def capture_and_send_snapshot(self) -> bool:
        """Capture a snapshot from camera and post to PrintPilot server.

        Preserves printer validation before camera use.
        Status is marked 'unknown' because camera alone cannot infer printer state.
        Returns True on success, False on failure.
        """
        # Preserve validation before camera use; do not try dummy host
        try:
            self.config.validate_printer()
        except ConfigError as ce:
            safe_err = "printer not configured"
            logger.warning("Printer validation failed before camera use: %s (%s)", ce, safe_err)
            self._last_error = safe_err
            self._send_error_heartbeat(safe_err)
            return False

        if self.camera is None:
            try:
                self._init_camera()
            except Exception as e:
                safe_err = sanitize_message(str(e))
                logger.error("Failed to initialize camera client: %s", safe_err)
                self._last_error = safe_err
                self._send_error_heartbeat(safe_err)
                return False

        logger.info(
            "Capturing snapshot from printer %s (%s:%d)",
            self.config.printer_id,
            self.config.printer_host,
            self.config.printer_port,
        )
        try:
            live_frame = self.live.recent_frame() if self.live else None
            if live_frame is not None:
                # Live view already holds the camera connection: reuse its newest frame
                image_bytes, meta = live_frame, {"itrack": 0, "source": "live"}
            else:
                image_bytes, meta = self.camera.capture_snapshot_with_retry(
                    retries=self.config.max_retries
                )
            # Status comes from MQTT telemetry; the camera alone cannot infer it
            status, tele = self._printer_state()
            snap_tele = dict(tele)
            snap_tele.update({"frame_size": len(image_bytes), "itrack": meta.get("itrack", 0)})
            self.client.send_with_retry(
                self.client.send_snapshot,
                device_id=self.config.device_id,
                printer_id=self.config.printer_id,
                image_bytes=image_bytes,
                status=status,
                telemetry=snap_tele,
                retries=self.config.max_retries,
            )
            # Clear last camera error on success
            self._last_error = None
            self._heartbeat()
            logger.info(
                "Snapshot successfully sent (%d bytes, itrack=%s)",
                len(image_bytes),
                meta.get("itrack"),
            )
            return True
        except FingerprintMismatchError as e:
            # Fatal security error: certificate changed or untrusted
            logger.critical("TLS certificate fingerprint verification failed: %s", e)
            self._last_error = sanitize_message(str(e))
            self._send_error_heartbeat(self._last_error)
            raise
        except Exception as e:
            safe_err = sanitize_message(str(e))
            logger.error("Failed to capture or send snapshot: %s", safe_err)
            self._last_error = safe_err
            self._send_error_heartbeat(safe_err)
            return False

    def _send_error_heartbeat(self, error_msg: str) -> None:
        """Send heartbeat marking device as unknown with error details."""
        try:
            self._heartbeat(error_msg)
        except Exception as hb_err:
            logger.warning(
                "Failed to send failure heartbeat to backend: %s",
                sanitize_message(str(hb_err)),
            )

    def send_periodic_heartbeat(self) -> None:
        """Send regular heartbeat between snapshot intervals.

        Retains active camera/config error rather than falsely clearing it by idle.
        """
        try:
            self._heartbeat(self._last_error)
        except Exception as e:
            logger.warning("Periodic heartbeat failed: %s", sanitize_message(str(e)))

    def stop(self) -> None:
        """Request loop shutdown."""
        logger.info("Stopping bridge service...")
        self._running = False
        if self.telemetry:
            self.telemetry.stop()

    def run_once(self) -> int:
        """Execute a single snapshot and heartbeat cycle, then exit."""
        if self._heartbeat_only or not self.config.is_printer_configured():
            logger.info("Executing single heartbeat cycle (--once / heartbeat-only)")
            self.send_periodic_heartbeat()
            return 0
        logger.info("Executing single snapshot cycle (--once)")
        success = self.capture_and_send_snapshot()
        return 0 if success else 1

    def run(self) -> int:
        """Run continuous monitoring loop with snapshot and heartbeat intervals."""
        self._running = True
        is_hb_only = self._heartbeat_only or not self.config.is_printer_configured()
        if is_hb_only:
            logger.info(
                "Starting PrintPilot bridge heartbeat loop (heartbeat every %ds; camera capture disabled)",
                self.config.heartbeat_interval_seconds,
            )
        else:
            logger.info(
                "Starting PrintPilot bridge loop (snapshot every %ds, heartbeat every %ds)",
                self.config.snapshot_interval_seconds,
                self.config.heartbeat_interval_seconds,
            )

        last_snapshot_time = 0.0
        last_heartbeat_time = 0.0

        if self.telemetry:
            self.telemetry.start()
            # Give MQTT a moment so the first snapshot already carries telemetry
            for _ in range(5):
                if not self._running or self.telemetry.snapshot():
                    break
                time.sleep(1)

        # Send initial heartbeat immediately upon starting
        self.send_periodic_heartbeat()
        last_heartbeat_time = time.time()

        while self._running:
            now = time.time()

            if not is_hb_only:
                # Time for snapshot?
                if now - last_snapshot_time >= self.snapshot_interval():
                    if self.should_capture():
                        self.capture_and_send_snapshot()
                        last_snapshot_time = time.time()
                        last_heartbeat_time = last_snapshot_time
                    else:
                        # Idle printer: no image; re-check soon so a new job is caught quickly
                        logger.debug("Printer %s idle; skipping snapshot", self.config.printer_id)
                        self.send_periodic_heartbeat()
                        last_heartbeat_time = time.time()
                        last_snapshot_time = (last_heartbeat_time - self.snapshot_interval()
                                              + self.config.heartbeat_interval_seconds)

                # Time for heartbeat between snapshots?
                elif now - last_heartbeat_time >= self.config.heartbeat_interval_seconds:
                    self.send_periodic_heartbeat()
                    last_heartbeat_time = time.time()
            else:
                if now - last_heartbeat_time >= self.config.heartbeat_interval_seconds:
                    self.send_periodic_heartbeat()
                    last_heartbeat_time = time.time()

            # Responsive sleep loop to react quickly to shutdown signals
            sleep_step = 1.0
            elapsed = 0.0
            target_sleep = (
                self.config.heartbeat_interval_seconds
                if is_hb_only
                else min(
                    self.config.heartbeat_interval_seconds,
                    self.snapshot_interval(),
                )
            )
            while self._running and elapsed < target_sleep:
                time.sleep(sleep_step)
                elapsed += sleep_step
                if not is_hb_only and (
                    time.time() - last_snapshot_time >= self.snapshot_interval()
                ):
                    break
                if time.time() - last_heartbeat_time >= self.config.heartbeat_interval_seconds:
                    break

        logger.info("PrintPilot bridge stopped cleanly.")
        return 0


class LiveStreamer:
    """Streams camera frames to the server while a dashboard viewer is watching."""

    EXTEND_SECONDS = 8.0

    def __init__(self, service: "BridgeService", client: PrintPilotApiClient):
        self.service = service
        self.client = client
        self._until = 0.0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._latest: Optional[tuple] = None  # (frame, monotonic time)

    def extend(self) -> None:
        with self._lock:
            self._until = time.monotonic() + self.EXTEND_SECONDS
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name=f"live-{self.service.config.printer_id}")
                self._thread.start()

    def active(self) -> bool:
        return time.monotonic() < self._until and self._thread is not None and self._thread.is_alive()

    def recent_frame(self, max_age: float = 10.0) -> Optional[bytes]:
        latest = self._latest
        if self.active() and latest and time.monotonic() - latest[1] <= max_age:
            return latest[0]
        return None

    def _run(self) -> None:
        cam = self.service.camera
        pid = self.service.config.printer_id
        if cam is None:
            return
        sock = None
        try:
            sock = cam._connect_and_authenticate()
            logger.info("Live view started for %s", pid)
            while time.monotonic() < self._until:
                frame, _meta = read_frame(sock, max_payload_size=self.service.config.max_frame_bytes, validate=True)
                self._latest = (frame, time.monotonic())
                try:
                    if not self.client.send_live_frame(pid, frame):
                        break  # server no longer wants frames
                except Exception as e:
                    logger.warning("Live frame upload failed: %s", sanitize_message(str(e)))
                    break
        except Exception as e:
            logger.warning("Live view for %s stopped: %s", pid, sanitize_message(str(e)))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            with self._lock:
                self._until = 0.0
            logger.info("Live view ended for %s", pid)


class CommandPoller:
    """Pulls queued control commands from the server and runs them over MQTT."""

    POLL_SECONDS = 3.0

    def __init__(self, config: BridgeConfig, services: list):
        self.device_id = config.device_id
        self.by_printer = {svc.config.printer_id: svc for svc in services}
        self.client = PrintPilotApiClient(server_url=config.server_url, device_token=config.device_token,
                                          timeout=config.read_timeout)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int:
        handled = 0
        response = self.client.poll_commands_full(self.device_id)
        for pid in response.get("live") or []:
            svc = self.by_printer.get(pid)
            if svc is not None and svc.camera is not None:
                if svc.live is None:
                    svc.live = LiveStreamer(svc, self.client)
                svc.live.extend()
        for cmd in response.get("commands") or []:
            if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str):
                continue
            svc = self.by_printer.get(cmd.get("printer_id"))
            params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
            if svc is None or svc.telemetry is None:
                ok, reason = False, "cầu nối không quản lý máy in này"
            else:
                logger.info("Executing %s on %s", cmd.get("command"), svc.config.printer_id)
                ok, reason = svc.telemetry.send_command(str(cmd.get("command")), params)
                if ok:
                    self._publish_new_state(svc, str(cmd.get("command")), params)
            try:
                self.client.post_command_result(cmd["id"], ok, reason)
            except Exception as e:
                logger.warning("Could not report command result: %s", sanitize_message(str(e)))
            handled += 1
        return handled

    # What telemetry should show once a command has taken effect
    EXPECTED = {
        "light": lambda p: ("light", p.get("mode")),
        "speed": lambda p: ("speed_level", p.get("level")),
        "pause": lambda p: ("gcode_state", "PAUSE"),
        "resume": lambda p: ("gcode_state", "RUNNING"),
    }

    @classmethod
    def _publish_new_state(cls, svc, command: str = "", params: Optional[dict] = None) -> None:
        """Push the printer's post-command state to the server before reporting the
        result, so the dashboard never re-reads the pre-command value."""
        try:
            tele = svc.telemetry
            expected = cls.EXPECTED.get(command)
            if expected and hasattr(tele, "wait_for"):
                key, value = expected(params or {})
                if not tele.wait_for(key, value):
                    logger.info("%s: telemetry did not show %s=%s yet", command, key, value)
            elif hasattr(tele, "refresh"):
                tele.refresh()
            heartbeat = getattr(svc, "send_periodic_heartbeat", None)
            if heartbeat:
                heartbeat()
        except Exception as e:  # never block the command result on this
            logger.warning("Could not publish post-command state: %s", sanitize_message(str(e)))

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                logger.debug("Command poll failed: %s", sanitize_message(str(e)))
            self._stop.wait(self.POLL_SECONDS)


class MultiBridge:
    """Runs one BridgeService per configured printer, each in its own thread."""

    def __init__(self, config: BridgeConfig, heartbeat_only: bool = False):
        self.services = [BridgeService(c, heartbeat_only=heartbeat_only)
                         for c in config.printer_configs()]
        self.poller = CommandPoller(config, self.services)

    def stop(self) -> None:
        self.poller.stop()
        for svc in self.services:
            svc.stop()

    def run_once(self) -> int:
        return max(svc.run_once() for svc in self.services)

    def run(self) -> int:
        threading.Thread(target=self.poller.run, daemon=True, name="command-poller").start()
        if len(self.services) == 1:
            return self.services[0].run()
        threads = [threading.Thread(target=svc.run, daemon=True, name=f"printer-{svc.config.printer_id}")
                   for svc in self.services]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
        return 0


def main(argv: Optional[list] = None) -> int:
    """CLI entrypoint for PrintPilot camera bridge."""
    parser = argparse.ArgumentParser(
        description="PrintPilot Bambu Lab A1 Camera Bridge (Android / Termux)",
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Path to external bridge JSON configuration file",
        default=None,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Capture single snapshot, send heartbeat, and exit",
    )
    parser.add_argument(
        "--heartbeat-only",
        action="store_true",
        help="Run heartbeat loop only without capturing camera snapshots",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging output",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        config = load_config(args.config, allow_incomplete=True)
        logger.info("Configuration loaded: %r", config)
    except Exception as e:
        logger.error("Configuration error: %s", sanitize_message(str(e)))
        return 1

    try:
        service = MultiBridge(config, heartbeat_only=args.heartbeat_only)
    except ConfigError as e:
        logger.error("Configuration error: %s", sanitize_message(str(e)))
        return 1

    # Attach signal handlers for graceful termination
    def handle_signal(sig, frame):
        logger.info("Received signal %s; initiating shutdown", sig)
        service.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.once:
        return service.run_once()
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
