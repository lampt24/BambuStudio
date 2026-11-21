"""Configuration management for PrintPilot A1 camera bridge.

Supports external configuration files (JSON) and environment variable overrides.
Never commits or logs secrets in the repository or console.
"""

from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .exceptions import ConfigError
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.exceptions import ConfigError
    except ImportError:
        from exceptions import ConfigError

logger = logging.getLogger("printpilot.config")

# Standard Termux and Linux configuration paths
DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "printpilot" / "bridge.json",
    Path("/data/data/com.termux/files/home/.config/printpilot/bridge.json"),
    Path.cwd() / "bridge.json",
]


@dataclass
class BridgeConfig:
    """Configuration settings for PrintPilot camera bridge."""

    # Printer settings
    printer_host: str = ""
    printer_access_code: str = ""
    printer_id: str = "a1-01"
    printer_port: int = 6000
    printer_username: str = "bblp"

    # PrintPilot Server settings
    server_url: str = "http://127.0.0.1:8000"
    device_token: str = ""
    device_id: str = "redmi-bridge-01"

    # Security & TLS settings
    cert_fingerprint: Optional[str] = None
    ca_cert_path: Optional[str] = None
    insecure_skip_verify: bool = False

    # Timing & Performance
    snapshot_interval_seconds: int = 120
    heartbeat_interval_seconds: int = 30
    max_frame_bytes: int = 5 * 1024 * 1024  # 5 MiB
    connect_timeout: float = 10.0
    read_timeout: float = 15.0
    max_retries: int = 3
    discard_initial_frame: bool = True

    @staticmethod
    def is_dummy_host(host: str) -> bool:
        """Check if host is a known dummy/placeholder host."""
        if not host:
            return False
        clean = host.strip().lower()
        return (
            clean in ("0.0.0.0", "localhost", "example.com", "placeholder")
            or clean.startswith("replace_")
        )

    def validate_printer(self) -> None:
        """Validate printer-specific configuration before camera operations."""
        errors: List[str] = []
        if not self.printer_host or not self.printer_host.strip():
            errors.append("printer_host cannot be empty")
        elif self.is_dummy_host(self.printer_host):
            errors.append(f"printer_host is using dummy/placeholder address: {self.printer_host}")

        if not self.printer_access_code or not self.printer_access_code.strip():
            errors.append("printer_access_code cannot be empty")
        elif self.printer_access_code.strip().startswith("REPLACE_"):
            errors.append("printer_access_code contains placeholder value")

        if not self.printer_id or not self.printer_id.strip():
            errors.append("printer_id cannot be empty")
        if self.printer_port <= 0 or self.printer_port > 65535:
            errors.append(f"Invalid printer_port: {self.printer_port}")

        if errors:
            raise ConfigError(f"Printer configuration invalid: {'; '.join(errors)}")

    def is_printer_configured(self) -> bool:
        """Check whether printer connection settings are fully configured."""
        try:
            self.validate_printer()
            return True
        except ConfigError:
            return False

    def validate(self, require_printer: bool = True) -> None:
        """Validate required configuration values.

        If require_printer is False, printer credentials may be missing (config-incomplete mode).
        """
        errors: List[str] = []

        if not self.device_id:
            errors.append("device_id cannot be empty")
        if not self.server_url:
            errors.append("server_url cannot be empty")
        if not self.device_token:
            errors.append("device_token cannot be empty")

        if self.snapshot_interval_seconds <= 0:
            errors.append("snapshot_interval_seconds must be positive")
        if self.heartbeat_interval_seconds <= 0:
            errors.append("heartbeat_interval_seconds must be positive")
        if self.max_frame_bytes < 1024:
            errors.append("max_frame_bytes must be at least 1024 bytes")

        if require_printer:
            try:
                self.validate_printer()
            except ConfigError as pe:
                errors.append(str(pe))

        if errors:
            raise ConfigError(f"Configuration validation failed: {'; '.join(errors)}")

    def to_dict(self, redact: bool = True) -> Dict[str, Any]:
        """Convert config to dictionary, optionally redacting sensitive credentials."""
        d = asdict(self)
        if redact:
            if d.get("printer_access_code"):
                d["printer_access_code"] = "********"
            if d.get("device_token"):
                d["device_token"] = "********"
        return d

    def __repr__(self) -> str:
        safe = self.to_dict(redact=True)
        items = [f"{k}={v!r}" for k, v in safe.items()]
        return f"BridgeConfig({', '.join(items)})"


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def load_config(
    config_path: Optional[str] = None, allow_incomplete: bool = True
) -> BridgeConfig:
    """Load configuration from specified path, default search locations, or environment variables.

    If allow_incomplete is True, allows missing/unconfigured printer credentials so the service
    can start and report status to PrintPilot backend while waiting for printer setup.
    """
    raw_config: Dict[str, Any] = {}

    target_file: Optional[Path] = None
    if config_path:
        target_file = Path(config_path).expanduser().resolve()
        if not target_file.exists():
            raise ConfigError(f"Specified configuration file does not exist: {target_file}")
    elif os.environ.get("PRINTPILOT_BRIDGE_CONFIG"):
        target_file = Path(os.environ["PRINTPILOT_BRIDGE_CONFIG"]).expanduser().resolve()
        if not target_file.exists():
            raise ConfigError(f"PRINTPILOT_BRIDGE_CONFIG file does not exist: {target_file}")
    else:
        for p in DEFAULT_CONFIG_PATHS:
            expanded = p.expanduser()
            if expanded.exists():
                target_file = expanded
                break

    if target_file and target_file.exists():
        logger.info("Loading configuration from %s", target_file)
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                raw_config = json.load(f)
        except Exception as e:
            raise ConfigError(f"Failed to read/parse JSON config file {target_file}: {e}") from e

    # Apply environment variable overrides
    env_mappings = {
        "PRINTPILOT_PRINTER_HOST": ("printer_host", str),
        "PRINTPILOT_PRINTER_PORT": ("printer_port", int),
        "PRINTPILOT_PRINTER_ACCESS_CODE": ("printer_access_code", str),
        "PRINTPILOT_PRINTER_USERNAME": ("printer_username", str),
        "PRINTPILOT_PRINTER_ID": ("printer_id", str),
        "PRINTPILOT_DEVICE_ID": ("device_id", str),
        "PRINTPILOT_SERVER_URL": ("server_url", str),
        "PRINTPILOT_DEVICE_TOKEN": ("device_token", str),
        "PRINTPILOT_CERT_FINGERPRINT": ("cert_fingerprint", str),
        "PRINTPILOT_CA_CERT_PATH": ("ca_cert_path", str),
        "PRINTPILOT_INSECURE_SKIP_VERIFY": ("insecure_skip_verify", _parse_bool),
        "PRINTPILOT_SNAPSHOT_INTERVAL": ("snapshot_interval_seconds", int),
        "PRINTPILOT_HEARTBEAT_INTERVAL": ("heartbeat_interval_seconds", int),
        "PRINTPILOT_MAX_FRAME_BYTES": ("max_frame_bytes", int),
        "PRINTPILOT_CONNECT_TIMEOUT": ("connect_timeout", float),
        "PRINTPILOT_READ_TIMEOUT": ("read_timeout", float),
        "PRINTPILOT_MAX_RETRIES": ("max_retries", int),
        "PRINTPILOT_DISCARD_INITIAL_FRAME": ("discard_initial_frame", _parse_bool),
    }

    for env_var, (cfg_key, conv_fn) in env_mappings.items():
        if env_var in os.environ:
            val = os.environ[env_var]
            try:
                raw_config[cfg_key] = conv_fn(val)
            except Exception as e:
                raise ConfigError(f"Failed to parse environment variable {env_var}={val!r}: {e}") from e

    # Construct and validate BridgeConfig
    try:
        config = BridgeConfig(
            printer_host=raw_config.get("printer_host", ""),
            printer_access_code=raw_config.get("printer_access_code", ""),
            printer_id=raw_config.get("printer_id", ""),
            printer_port=int(raw_config.get("printer_port", 6000)),
            printer_username=raw_config.get("printer_username", "bblp"),
            server_url=raw_config.get("server_url", "http://127.0.0.1:8000"),
            device_token=raw_config.get("device_token", ""),
            device_id=raw_config.get("device_id", "redmi-bridge-01"),
            cert_fingerprint=raw_config.get("cert_fingerprint"),
            ca_cert_path=raw_config.get("ca_cert_path"),
            insecure_skip_verify=_parse_bool(raw_config.get("insecure_skip_verify", False)),
            snapshot_interval_seconds=int(raw_config.get("snapshot_interval_seconds", 120)),
            heartbeat_interval_seconds=int(raw_config.get("heartbeat_interval_seconds", 30)),
            max_frame_bytes=int(raw_config.get("max_frame_bytes", 5 * 1024 * 1024)),
            connect_timeout=float(raw_config.get("connect_timeout", 10.0)),
            read_timeout=float(raw_config.get("read_timeout", 15.0)),
            max_retries=int(raw_config.get("max_retries", 3)),
            discard_initial_frame=_parse_bool(raw_config.get("discard_initial_frame", True)),
        )
    except Exception as e:
        raise ConfigError(f"Error constructing BridgeConfig: {e}") from e

    config.validate(require_printer=not allow_incomplete)
    return config
