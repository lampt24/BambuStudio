"""A1 TLS Camera Protocol Framing and Parsing.

Implements the Bambu Lab A1 / A1 mini / P1 series local camera protocol
operating over TCP/TLS on port 6000.
"""

import struct
from typing import Any, Dict, Optional, Tuple, Union

try:
    from .exceptions import (
        AuthError,
        FramingError,
        IncompleteReadError,
        InvalidJpegError,
        PayloadSizeExceededError,
    )
except (ImportError, ValueError):
    try:
        from services.printpilot.bridge.exceptions import (
            AuthError,
            FramingError,
            IncompleteReadError,
            InvalidJpegError,
            PayloadSizeExceededError,
        )
    except ImportError:
        from exceptions import (
            AuthError,
            FramingError,
            IncompleteReadError,
            InvalidJpegError,
            PayloadSizeExceededError,
        )

DEFAULT_PORT = 6000
DEFAULT_USER = "bblp"
AUTH_PACKET_MAGIC = 0x40
AUTH_PACKET_TYPE = 0x3000
AUTH_PACKET_LEN = 80
FRAME_HEADER_LEN = 16

# Sensible default payload boundaries for 1280x720 JPEG frames
DEFAULT_MAX_PAYLOAD_SIZE = 5 * 1024 * 1024  # 5 MiB
MIN_JPEG_PAYLOAD_SIZE = 4                    # At minimum SOI (2 bytes) + EOI (2 bytes)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def build_auth_packet(access_code: str, username: str = DEFAULT_USER) -> bytes:
    """Build the 80-byte binary authentication packet for port 6000.

    Layout (little-endian):
    - Offset 0  (4 bytes): Inlined Payload size = 0x40 (64 bytes)
    - Offset 4  (4 bytes): Packet Type = 0x3000
    - Offset 8  (4 bytes): Flags = 0
    - Offset 12 (4 bytes): Reserved = 0
    - Offset 16 (32 bytes): ASCII username (null-padded)
    - Offset 48 (32 bytes): ASCII LAN access code (null-padded)
    Total: 80 bytes.
    """
    if not access_code:
        raise AuthError("Printer LAN access code cannot be empty")

    user_bytes = username.encode("ascii", errors="strict")
    pass_bytes = access_code.encode("ascii", errors="strict")

    if len(user_bytes) > 32:
        raise AuthError(f"Username exceeds maximum 32 bytes (got {len(user_bytes)})")
    if len(pass_bytes) > 32:
        raise AuthError(f"Access code exceeds maximum 32 bytes (got {len(pass_bytes)})")

    # Pad with null bytes
    user_padded = user_bytes.ljust(32, b"\x00")
    pass_padded = pass_bytes.ljust(32, b"\x00")

    packet = struct.pack(
        "<IIII32s32s",
        AUTH_PACKET_MAGIC,
        AUTH_PACKET_TYPE,
        0,
        0,
        user_padded,
        pass_padded,
    )
    if len(packet) != AUTH_PACKET_LEN:
        raise FramingError(f"Auth packet unexpected length: {len(packet)} (expected {AUTH_PACKET_LEN})")
    return packet


def parse_frame_header(
    header_bytes: bytes,
    max_payload_size: int = DEFAULT_MAX_PAYLOAD_SIZE,
    min_payload_size: int = MIN_JPEG_PAYLOAD_SIZE,
) -> Tuple[int, int, int, int]:
    """Parse the 16-byte binary frame header.

    Layout (little-endian):
    - Offset 0  (4 bytes): Payload size (JPEG size)
    - Offset 4  (4 bytes): itrack / channel
    - Offset 8  (4 bytes): Reserved / flags
    - Offset 12 (4 bytes): Reserved / timestamp / seq

    Returns:
        (payload_size, itrack, reserved1, reserved2)
    """
    if len(header_bytes) != FRAME_HEADER_LEN:
        raise FramingError(
            f"Frame header must be exactly {FRAME_HEADER_LEN} bytes, got {len(header_bytes)}"
        )

    payload_size, itrack, reserved1, reserved2 = struct.unpack("<IIII", header_bytes)

    if payload_size < min_payload_size:
        raise FramingError(
            f"Frame payload size {payload_size} is smaller than minimum allowed ({min_payload_size})"
        )
    if payload_size > max_payload_size:
        raise PayloadSizeExceededError(
            f"Frame payload size {payload_size} exceeds maximum allowed ({max_payload_size})"
        )

    return payload_size, itrack, reserved1, reserved2


def read_exact(reader: Any, num_bytes: int, chunk_size: int = 4096) -> bytes:
    """Read exactly num_bytes from a socket or stream, handling fragmented reads cleanly.

    Supports readers with `.recv(n)` (socket / ssl.SSLSocket) or `.read(n)` (streams).
    Raises IncompleteReadError on early EOF.
    """
    if num_bytes < 0:
        raise ValueError(f"num_bytes must be non-negative, got {num_bytes}")
    if num_bytes == 0:
        return b""

    buf = bytearray()
    read_fn = getattr(reader, "recv", None) or getattr(reader, "read", None)
    if not callable(read_fn):
        raise TypeError(f"Reader object {type(reader)} has neither recv() nor read() method")

    while len(buf) < num_bytes:
        to_read = min(num_bytes - len(buf), chunk_size)
        chunk = read_fn(to_read)
        if not chunk:
            raise IncompleteReadError(
                f"Connection closed prematurely; received {len(buf)} of {num_bytes} bytes"
            )
        if len(chunk) > to_read:
            chunk = chunk[:to_read]
        buf.extend(chunk)

    return bytes(buf)


def validate_jpeg(data: bytes) -> bool:
    """Check if byte sequence contains valid JPEG SOI and EOI markers."""
    return len(data) >= 4 and data.startswith(JPEG_SOI) and data.endswith(JPEG_EOI)


def read_frame(
    reader: Any,
    max_payload_size: int = DEFAULT_MAX_PAYLOAD_SIZE,
    validate: bool = True,
    chunk_size: int = 4096,
) -> Tuple[bytes, Dict[str, Any]]:
    """Read a single frame from the stream with bounded read guarantees.

    1. Reads 16-byte header via read_exact.
    2. Parses and validates payload bounds.
    3. Reads exact payload_size bytes via read_exact.
    4. Validates JPEG SOI/EOI if validate=True.

    Returns:
        (image_bytes, metadata_dict)
    """
    header_bytes = read_exact(reader, FRAME_HEADER_LEN, chunk_size=chunk_size)
    payload_size, itrack, r1, r2 = parse_frame_header(
        header_bytes, max_payload_size=max_payload_size
    )

    image_bytes = read_exact(reader, payload_size, chunk_size=chunk_size)

    if validate and not validate_jpeg(image_bytes):
        raise InvalidJpegError(
            f"Received payload ({len(image_bytes)} bytes) does not have valid JPEG markers "
            f"(SOI: {image_bytes[:2].hex()}, EOI: {image_bytes[-2:].hex()})"
        )

    metadata = {
        "payload_size": payload_size,
        "itrack": itrack,
        "reserved1": r1,
        "reserved2": r2,
    }
    return image_bytes, metadata
