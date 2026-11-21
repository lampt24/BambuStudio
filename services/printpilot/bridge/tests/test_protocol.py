"""Unit tests for A1 camera protocol framing, bounded reads, and fragmented inputs."""

import io
import struct
import unittest

from services.printpilot.bridge.exceptions import (
    AuthError,
    FramingError,
    IncompleteReadError,
    InvalidJpegError,
    PayloadSizeExceededError,
)
from services.printpilot.bridge.protocol import (
    AUTH_PACKET_LEN,
    AUTH_PACKET_MAGIC,
    AUTH_PACKET_TYPE,
    FRAME_HEADER_LEN,
    JPEG_EOI,
    JPEG_SOI,
    build_auth_packet,
    parse_frame_header,
    read_exact,
    read_frame,
    validate_jpeg,
)


class FragmentedStream:
    """Simulates a socket/stream that yields small fragmented chunks."""

    def __init__(self, data: bytes, chunk_size: int = 1):
        self.data = data
        self.chunk_size = chunk_size
        self.offset = 0

    def recv(self, max_bytes: int) -> bytes:
        if self.offset >= len(self.data):
            return b""
        read_len = min(max_bytes, self.chunk_size, len(self.data) - self.offset)
        chunk = self.data[self.offset : self.offset + read_len]
        self.offset += read_len
        return chunk

    read = recv


class TestProtocolFraming(unittest.TestCase):

    def test_build_auth_packet_valid(self):
        access_code = "12345678"
        packet = build_auth_packet(access_code, username="bblp")

        self.assertEqual(len(packet), AUTH_PACKET_LEN)
        magic, p_type, flags, res, user_raw, pass_raw = struct.unpack(
            "<IIII32s32s", packet
        )
        self.assertEqual(magic, AUTH_PACKET_MAGIC)
        self.assertEqual(p_type, AUTH_PACKET_TYPE)
        self.assertEqual(flags, 0)
        self.assertEqual(res, 0)
        self.assertEqual(user_raw.rstrip(b"\x00"), b"bblp")
        self.assertEqual(pass_raw.rstrip(b"\x00"), b"12345678")

    def test_build_auth_packet_empty_code(self):
        with self.assertRaises(AuthError):
            build_auth_packet("")

    def test_build_auth_packet_oversized_code(self):
        with self.assertRaises(AuthError):
            build_auth_packet("a" * 33)

    def test_parse_frame_header_valid(self):
        header = struct.pack("<IIII", 1024, 1, 0, 0)
        size, itrack, r1, r2 = parse_frame_header(header)
        self.assertEqual(size, 1024)
        self.assertEqual(itrack, 1)
        self.assertEqual(r1, 0)
        self.assertEqual(r2, 0)

    def test_parse_frame_header_invalid_length(self):
        with self.assertRaises(FramingError):
            parse_frame_header(b"\x00" * 15)
        with self.assertRaises(FramingError):
            parse_frame_header(b"\x00" * 17)

    def test_parse_frame_header_oversized_payload(self):
        header = struct.pack("<IIII", 10 * 1024 * 1024, 0, 0, 0)
        with self.assertRaises(PayloadSizeExceededError):
            parse_frame_header(header, max_payload_size=5 * 1024 * 1024)

    def test_parse_frame_header_too_small_payload(self):
        header = struct.pack("<IIII", 3, 0, 0, 0)
        with self.assertRaises(FramingError):
            parse_frame_header(header, min_payload_size=4)

    def test_read_exact_full(self):
        data = b"Hello, world!"
        stream = io.BytesIO(data)
        result = read_exact(stream, len(data))
        self.assertEqual(result, data)

    def test_read_exact_zero_bytes(self):
        stream = io.BytesIO(b"abc")
        self.assertEqual(read_exact(stream, 0), b"")

    def test_read_exact_fragmented_single_bytes(self):
        data = b"Fragmented transmission testing bytes"
        stream = FragmentedStream(data, chunk_size=1)
        result = read_exact(stream, len(data))
        self.assertEqual(result, data)

    def test_read_exact_fragmented_varied_chunks(self):
        data = b"X" * 1000
        stream = FragmentedStream(data, chunk_size=7)
        result = read_exact(stream, len(data))
        self.assertEqual(result, data)

    def test_read_exact_premature_eof(self):
        stream = io.BytesIO(b"short")
        with self.assertRaises(IncompleteReadError):
            read_exact(stream, 10)

    def test_validate_jpeg_markers(self):
        valid = JPEG_SOI + b"image data" + JPEG_EOI
        self.assertTrue(validate_jpeg(valid))

        self.assertFalse(validate_jpeg(b"\x00\x00data\xff\xd9"))
        self.assertFalse(validate_jpeg(b"\xff\xd8data\x00\x00"))
        self.assertFalse(validate_jpeg(b"\xff\xd8\xff"))

    def test_read_frame_fragmented_success(self):
        jpeg_payload = JPEG_SOI + b"BambuLabFrameContent" * 100 + JPEG_EOI
        header = struct.pack("<IIII", len(jpeg_payload), 0, 0, 0)
        raw_stream_data = header + jpeg_payload

        # Fragment stream into 3-byte chunks to stress fragmentation
        stream = FragmentedStream(raw_stream_data, chunk_size=3)
        frame_data, meta = read_frame(stream, max_payload_size=len(jpeg_payload) + 100)

        self.assertEqual(frame_data, jpeg_payload)
        self.assertEqual(meta["payload_size"], len(jpeg_payload))
        self.assertEqual(meta["itrack"], 0)

    def test_read_frame_invalid_jpeg_raises(self):
        bad_payload = b"NOT_A_JPEG_FILE_DATA_HERE"
        header = struct.pack("<IIII", len(bad_payload), 0, 0, 0)
        stream = io.BytesIO(header + bad_payload)

        with self.assertRaises(InvalidJpegError):
            read_frame(stream, validate=True)

    def test_read_frame_invalid_jpeg_allowed_when_validation_disabled(self):
        bad_payload = b"RAW_DATA_NO_VALIDATION"
        header = struct.pack("<IIII", len(bad_payload), 0, 0, 0)
        stream = io.BytesIO(header + bad_payload)

        data, meta = read_frame(stream, validate=False)
        self.assertEqual(data, bad_payload)

    def test_read_frame_oversized_bounds_enforced(self):
        # Header announces 10MB payload
        header = struct.pack("<IIII", 10 * 1024 * 1024, 0, 0, 0)
        stream = io.BytesIO(header + b"\x00" * 100)

        with self.assertRaises(PayloadSizeExceededError):
            read_frame(stream, max_payload_size=1 * 1024 * 1024)

    def test_read_frame_header_truncated_raises_incomplete(self):
        stream = io.BytesIO(b"\x00" * 8)  # Only 8 bytes instead of 16
        with self.assertRaises(IncompleteReadError):
            read_frame(stream)

    def test_read_frame_payload_truncated_raises_incomplete(self):
        jpeg_prefix = JPEG_SOI + b"partial data"
        header = struct.pack("<IIII", 100, 0, 0, 0)  # promises 100 bytes
        stream = io.BytesIO(header + jpeg_prefix)     # only provides 14 bytes

        with self.assertRaises(IncompleteReadError):
            read_frame(stream)


if __name__ == "__main__":
    unittest.main()
