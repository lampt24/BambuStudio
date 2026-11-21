"""Exceptions for PrintPilot A1 camera bridge."""

class BridgeError(Exception):
    """Base exception for all camera bridge errors."""
    pass


class ProtocolError(BridgeError):
    """Base exception for protocol-level errors."""
    pass


class FramingError(ProtocolError):
    """Raised when frame headers or structures are malformed."""
    pass


class IncompleteReadError(ProtocolError):
    """Raised when an EOF occurs before expected bytes are fully received."""
    pass


class PayloadSizeExceededError(FramingError):
    """Raised when the announced frame payload exceeds the allowed bounded size."""
    pass


class InvalidJpegError(FramingError):
    """Raised when received frame payload lacks valid JPEG SOI/EOI markers."""
    pass


class TLSError(BridgeError):
    """Raised on TLS negotiation, trust, or certificate validation failures."""
    pass


class FingerprintMismatchError(TLSError):
    """Raised when the remote certificate SHA-256 fingerprint does not match the pinned value."""
    pass


class AuthError(ProtocolError):
    """Raised when authentication credentials or handshake fail."""
    pass


class NetworkError(BridgeError):
    """Raised on socket connection or network level failures."""
    pass


class ServerApiError(BridgeError):
    """Raised when PrintPilot backend API returns an error or fails to respond."""
    pass


class ConfigError(BridgeError):
    """Raised when configuration is missing or invalid."""
    pass
