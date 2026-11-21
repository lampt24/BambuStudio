"""Bambu Lab A1 Camera TLS Client.

Handles TLS connection on port 6000, explicit CA trust, SHA-256 certificate
fingerprint pinning, authentication handshake, and bounded frame capture.
"""

import hashlib
import logging
import random
import socket
import ssl
import time
from typing import Any, Dict, Optional, Tuple

try:
    from .exceptions import (
        AuthError,
        BridgeError,
        FingerprintMismatchError,
        FramingError,
        IncompleteReadError,
        NetworkError,
        PayloadSizeExceededError,
        TLSError,
    )
    from .protocol import (
        DEFAULT_MAX_PAYLOAD_SIZE,
        DEFAULT_PORT,
        DEFAULT_USER,
        build_auth_packet,
        read_frame,
    )
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.exceptions import (
            AuthError,
            BridgeError,
            FingerprintMismatchError,
            FramingError,
            IncompleteReadError,
            NetworkError,
            PayloadSizeExceededError,
            TLSError,
        )
        from services.printpilot.bridge.protocol import (
            DEFAULT_MAX_PAYLOAD_SIZE,
            DEFAULT_PORT,
            DEFAULT_USER,
            build_auth_packet,
            read_frame,
        )
    except ImportError:
        from exceptions import (
            AuthError,
            BridgeError,
            FingerprintMismatchError,
            FramingError,
            IncompleteReadError,
            NetworkError,
            PayloadSizeExceededError,
            TLSError,
        )
        from protocol import (
            DEFAULT_MAX_PAYLOAD_SIZE,
            DEFAULT_PORT,
            DEFAULT_USER,
            build_auth_packet,
            read_frame,
        )

logger = logging.getLogger("printpilot.camera")


def normalize_fingerprint(fp: str) -> str:
    """Normalize a hex fingerprint string (remove colons, spaces, uppercase to lowercase)."""
    return fp.replace(":", "").replace(" ", "").strip().lower()


def compute_cert_fingerprint(cert_der: bytes, algo: str = "sha256") -> str:
    """Compute normalized hex fingerprint of DER-encoded certificate."""
    h = hashlib.new(algo)
    h.update(cert_der)
    return h.hexdigest().lower()


class BambuCameraClient:
    """Client for retrieving camera snapshots from Bambu Lab A1 / P1 printers."""

    def __init__(
        self,
        host: str,
        access_code: str,
        port: int = DEFAULT_PORT,
        username: str = DEFAULT_USER,
        cert_fingerprint: Optional[str] = None,
        ca_cert_path: Optional[str] = None,
        insecure_skip_verify: bool = False,
        connect_timeout: float = 10.0,
        read_timeout: float = 15.0,
        max_frame_bytes: int = DEFAULT_MAX_PAYLOAD_SIZE,
        discard_initial_frame: bool = True,
    ):
        self.host = host
        self.port = port
        self.access_code = access_code
        self.username = username
        self.cert_fingerprint = (
            normalize_fingerprint(cert_fingerprint) if cert_fingerprint else None
        )
        self.ca_cert_path = ca_cert_path
        self.insecure_skip_verify = insecure_skip_verify
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_frame_bytes = max_frame_bytes
        self.discard_initial_frame = discard_initial_frame

        if not self.cert_fingerprint and not self.ca_cert_path and not self.insecure_skip_verify:
            # Note: Printers use self-signed certificates. By default, either fingerprint pinning
            # or explicit CA trust should be used. If neither is set and insecure_skip_verify is False,
            # standard system CA trust applies (which usually fails for self-signed certs).
            logger.warning(
                "Neither cert_fingerprint nor ca_cert_path is set. TLS verification may fail with self-signed printer certs."
            )

    def create_ssl_context(self) -> ssl.SSLContext:
        """Create and configure SSLContext based on trust settings."""
        try:
            if self.insecure_skip_verify or self.cert_fingerprint:
                # When pinning fingerprints or explicitly skipping CA check, do not require CA cert validation
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            elif self.ca_cert_path:
                context = ssl.create_default_context(cafile=self.ca_cert_path)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_REQUIRED
            else:
                context = ssl.create_default_context()
                context.check_hostname = False

            return context
        except Exception as e:
            raise TLSError(f"Failed to create SSL context: {e}") from e

    def verify_peer_fingerprint(self, ssock: ssl.SSLSocket) -> None:
        """Verify server certificate against pinned SHA-256 fingerprint if configured."""
        if not self.cert_fingerprint:
            return

        der_cert = ssock.getpeercert(binary_form=True)
        if not der_cert:
            raise FingerprintMismatchError(
                "Unable to retrieve peer certificate for fingerprint verification"
            )

        # Match fingerprint length to algorithm (SHA-256: 64 hex chars, SHA-1: 40 hex chars)
        algo = "sha1" if len(self.cert_fingerprint) == 40 else "sha256"
        actual_fp = compute_cert_fingerprint(der_cert, algo=algo)

        if actual_fp != self.cert_fingerprint:
            raise FingerprintMismatchError(
                f"Certificate fingerprint mismatch: expected {self.cert_fingerprint}, got {actual_fp}"
            )
        logger.debug("TLS peer certificate fingerprint verified successfully (%s)", algo)

    def _connect_and_authenticate(self) -> ssl.SSLSocket:
        """Establish TLS socket connection and send port 6000 auth handshake."""
        ssl_ctx = self.create_ssl_context()
        raw_sock = None
        try:
            raw_sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            raw_sock.settimeout(self.read_timeout)

            ssock = ssl_ctx.wrap_socket(
                raw_sock,
                server_hostname=self.host,
                do_handshake_on_connect=True,
            )
        except ssl.SSLError as e:
            if raw_sock:
                raw_sock.close()
            raise TLSError(f"TLS handshake failed connecting to {self.host}:{self.port}: {e}") from e
        except (socket.timeout, TimeoutError) as e:
            if raw_sock:
                raw_sock.close()
            raise NetworkError(f"Connection timed out connecting to {self.host}:{self.port}") from e
        except OSError as e:
            if raw_sock:
                raw_sock.close()
            raise NetworkError(f"Network error connecting to {self.host}:{self.port}: {e}") from e

        try:
            # Fingerprint verification
            self.verify_peer_fingerprint(ssock)

            # Send 80-byte authentication packet
            auth_packet = build_auth_packet(self.access_code, username=self.username)
            ssock.sendall(auth_packet)
            return ssock
        except Exception:
            try:
                ssock.close()
            except Exception:
                pass
            raise

    def capture_snapshot(self) -> Tuple[bytes, Dict[str, Any]]:
        """Connect, authenticate, and capture a fresh JPEG snapshot frame.

        If discard_initial_frame is True, discards the first frame (which is frequently
        a stale frame buffered by the printer) and returns the second frame.
        """
        ssock = self._connect_and_authenticate()
        try:
            first_frame, first_meta = read_frame(
                ssock,
                max_payload_size=self.max_frame_bytes,
                validate=True,
            )

            if not self.discard_initial_frame:
                return first_frame, first_meta

            # Read subsequent fresh frame
            try:
                second_frame, second_meta = read_frame(
                    ssock,
                    max_payload_size=self.max_frame_bytes,
                    validate=True,
                )
                return second_frame, second_meta
            except (socket.timeout, TimeoutError, IncompleteReadError) as e:
                logger.warning(
                    "Timed out reading second frame; falling back to initial frame: %s", e
                )
                return first_frame, first_meta
        except (socket.timeout, TimeoutError) as e:
            raise NetworkError(f"Timed out receiving frame from camera: {e}") from e
        except OSError as e:
            raise NetworkError(f"Socket error reading frame from camera: {e}") from e
        finally:
            try:
                ssock.close()
            except Exception:
                pass

    def capture_snapshot_with_retry(
        self,
        retries: int = 3,
        initial_backoff: float = 1.0,
        backoff_multiplier: float = 2.0,
    ) -> Tuple[bytes, Dict[str, Any]]:
        """Capture a snapshot, retrying transient network errors with exponential backoff."""
        attempt = 0
        current_delay = initial_backoff

        while True:
            attempt += 1
            try:
                return self.capture_snapshot()
            except (FingerprintMismatchError, AuthError) as fatal_err:
                # Credential and security errors must not be retried blindly
                logger.error("Fatal camera security/auth error: %s", fatal_err)
                raise
            except (NetworkError, FramingError, IncompleteReadError) as transient_err:
                if attempt >= retries:
                    logger.error(
                        "Camera capture failed after %d attempts: %s", attempt, transient_err
                    )
                    raise
                jitter = random.uniform(0.1, 0.5)
                sleep_time = current_delay + jitter
                logger.warning(
                    "Camera capture attempt %d/%d failed (%s); retrying in %.2fs",
                    attempt,
                    retries,
                    transient_err,
                    sleep_time,
                )
                time.sleep(sleep_time)
                current_delay *= backoff_multiplier
