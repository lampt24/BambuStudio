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
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.camera import BambuCameraClient
        from services.printpilot.bridge.client import PrintPilotApiClient, sanitize_message
        from services.printpilot.bridge.config import BridgeConfig, load_config
        from services.printpilot.bridge.exceptions import BridgeError, ConfigError, FingerprintMismatchError
    except ImportError:
        from camera import BambuCameraClient
        from client import PrintPilotApiClient, sanitize_message
        from config import BridgeConfig, load_config
        from exceptions import BridgeError, ConfigError, FingerprintMismatchError

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
            image_bytes, meta = self.camera.capture_snapshot_with_retry(
                retries=self.config.max_retries
            )
            # Send snapshot to PrintPilot server with status 'unknown' (camera alone cannot infer printer state)
            self.client.send_with_retry(
                self.client.send_snapshot,
                device_id=self.config.device_id,
                printer_id=self.config.printer_id,
                image_bytes=image_bytes,
                status="unknown",
                telemetry={"frame_size": len(image_bytes), "itrack": meta.get("itrack", 0)},
                retries=self.config.max_retries,
            )
            # Clear last camera error on success
            self._last_error = None
            # Send heartbeat marking device status unknown (camera alone cannot infer printer state)
            self.client.send_heartbeat(
                device_id=self.config.device_id,
                printer_id=self.config.printer_id,
                status="unknown",
            )
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
            self.client.send_heartbeat(
                device_id=self.config.device_id,
                printer_id=self.config.printer_id,
                status="unknown",
                error=error_msg,
            )
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
            self.client.send_heartbeat(
                device_id=self.config.device_id,
                printer_id=self.config.printer_id,
                status="unknown",
                error=self._last_error,
            )
        except Exception as e:
            logger.warning("Periodic heartbeat failed: %s", sanitize_message(str(e)))

    def stop(self) -> None:
        """Request loop shutdown."""
        logger.info("Stopping bridge service...")
        self._running = False

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

        # Send initial heartbeat immediately upon starting
        self.send_periodic_heartbeat()
        last_heartbeat_time = time.time()

        while self._running:
            now = time.time()

            if not is_hb_only:
                # Time for snapshot?
                if now - last_snapshot_time >= self.config.snapshot_interval_seconds:
                    self.capture_and_send_snapshot()
                    last_snapshot_time = time.time()
                    last_heartbeat_time = last_snapshot_time

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
                    self.config.snapshot_interval_seconds,
                )
            )
            while self._running and elapsed < target_sleep:
                time.sleep(sleep_step)
                elapsed += sleep_step
                if not is_hb_only and (
                    time.time() - last_snapshot_time >= self.config.snapshot_interval_seconds
                ):
                    break
                if time.time() - last_heartbeat_time >= self.config.heartbeat_interval_seconds:
                    break

        logger.info("PrintPilot bridge stopped cleanly.")
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

    service = BridgeService(config, heartbeat_only=args.heartbeat_only)

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
