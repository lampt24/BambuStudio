"""Unit tests for BambuCameraClient, fingerprint pinning, and TLS handling."""

import hashlib
import struct
import unittest
from unittest.mock import MagicMock, patch

from services.printpilot.bridge.camera import (
    BambuCameraClient,
    compute_cert_fingerprint,
    normalize_fingerprint,
)
from services.printpilot.bridge.exceptions import (
    FingerprintMismatchError,
    IncompleteReadError,
    NetworkError,
    PayloadSizeExceededError,
)
from services.printpilot.bridge.protocol import JPEG_EOI, JPEG_SOI


class TestCameraClient(unittest.TestCase):

    def setUp(self):
        self.mock_der = b"SIMULATED_DER_ENCODED_CERTIFICATE_BYTES_FOR_TESTING"
        self.sha256_fp = hashlib.sha256(self.mock_der).hexdigest()
        self.sha1_fp = hashlib.sha1(self.mock_der).hexdigest()

    def test_normalize_fingerprint(self):
        fp = "AB:CD:EF:01:23:45:67:89"
        self.assertEqual(normalize_fingerprint(fp), "abcdef0123456789")
        self.assertEqual(normalize_fingerprint("  aa bb cc  "), "aabbcc")

    def test_compute_cert_fingerprint(self):
        self.assertEqual(
            compute_cert_fingerprint(self.mock_der, "sha256"), self.sha256_fp
        )
        self.assertEqual(compute_cert_fingerprint(self.mock_der, "sha1"), self.sha1_fp)

    def test_verify_peer_fingerprint_success(self):
        client = BambuCameraClient(
            host="192.168.1.100",
            access_code="12345678",
            cert_fingerprint=self.sha256_fp,
        )
        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = self.mock_der

        # Should not raise
        client.verify_peer_fingerprint(mock_ssock)

    def test_verify_peer_fingerprint_mismatch(self):
        wrong_fp = "0000000000000000000000000000000000000000000000000000000000000000"
        client = BambuCameraClient(
            host="192.168.1.100",
            access_code="12345678",
            cert_fingerprint=wrong_fp,
        )
        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = self.mock_der

        with self.assertRaises(FingerprintMismatchError):
            client.verify_peer_fingerprint(mock_ssock)

    def test_verify_peer_fingerprint_sha1(self):
        client = BambuCameraClient(
            host="192.168.1.100",
            access_code="12345678",
            cert_fingerprint=self.sha1_fp,
        )
        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = self.mock_der

        client.verify_peer_fingerprint(mock_ssock)

    @patch("socket.create_connection")
    def test_capture_snapshot_single_frame(self, mock_create_conn):
        jpeg_payload = JPEG_SOI + b"FRAME1" + JPEG_EOI
        header = struct.pack("<IIII", len(jpeg_payload), 0, 0, 0)
        stream_bytes = header + jpeg_payload

        mock_raw_sock = MagicMock()
        mock_create_conn.return_value = mock_raw_sock

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = self.mock_der

        # Simulate recv respecting buffer bounds
        remaining = bytearray(stream_bytes)
        def mock_recv(n):
            nonlocal remaining
            if not remaining:
                return b""
            chunk = bytes(remaining[:n])
            remaining = remaining[n:]
            return chunk
        mock_ssock.recv.side_effect = mock_recv

        client = BambuCameraClient(
            host="192.168.1.100",
            access_code="12345678",
            cert_fingerprint=self.sha256_fp,
            discard_initial_frame=False,
        )

        with patch.object(client, "create_ssl_context") as mock_ctx_builder:
            mock_ctx = MagicMock()
            mock_ctx.wrap_socket.return_value = mock_ssock
            mock_ctx_builder.return_value = mock_ctx

            frame, meta = client.capture_snapshot()

            self.assertEqual(frame, jpeg_payload)
            self.assertEqual(meta["payload_size"], len(jpeg_payload))
            # Verify auth packet was sent
            self.assertTrue(mock_ssock.sendall.called)
            auth_sent = mock_ssock.sendall.call_args[0][0]
            self.assertEqual(len(auth_sent), 80)

    @patch("socket.create_connection")
    def test_capture_snapshot_discard_initial_frame(self, mock_create_conn):
        frame1 = JPEG_SOI + b"STALE_FRAME" + JPEG_EOI
        h1 = struct.pack("<IIII", len(frame1), 0, 0, 0)

        frame2 = JPEG_SOI + b"FRESH_FRAME" + JPEG_EOI
        h2 = struct.pack("<IIII", len(frame2), 0, 0, 0)

        stream_bytes = h1 + frame1 + h2 + frame2

        mock_raw_sock = MagicMock()
        mock_create_conn.return_value = mock_raw_sock

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = self.mock_der

        def mock_recv_fn():
            remaining = bytearray(stream_bytes)
            def recv(n):
                nonlocal remaining
                if not remaining:
                    return b""
                chunk = bytes(remaining[:n])
                remaining = remaining[n:]
                return chunk
            return recv

        mock_ssock.recv.side_effect = mock_recv_fn()

        client = BambuCameraClient(
            host="192.168.1.100",
            access_code="12345678",
            discard_initial_frame=True,
            insecure_skip_verify=True,
        )

        with patch.object(client, "create_ssl_context") as mock_ctx_builder:
            mock_ctx = MagicMock()
            mock_ctx.wrap_socket.return_value = mock_ssock
            mock_ctx_builder.return_value = mock_ctx

            frame, meta = client.capture_snapshot()
            self.assertEqual(frame, frame2)

    def test_retry_on_transient_failure(self):
        client = BambuCameraClient(host="192.168.1.100", access_code="12345678")

        mock_capture = MagicMock()
        jpeg_payload = JPEG_SOI + b"RECOVERED" + JPEG_EOI
        # Fails first with NetworkError, succeeds second
        mock_capture.side_effect = [
            NetworkError("Connection reset"),
            (jpeg_payload, {"payload_size": len(jpeg_payload)}),
        ]
        client.capture_snapshot = mock_capture

        frame, meta = client.capture_snapshot_with_retry(retries=3, initial_backoff=0.01)
        self.assertEqual(frame, jpeg_payload)
        self.assertEqual(mock_capture.call_count, 2)

    def test_retry_exhausted_raises(self):
        client = BambuCameraClient(host="192.168.1.100", access_code="12345678")
        mock_capture = MagicMock()
        mock_capture.side_effect = NetworkError("Persistent failure")
        client.capture_snapshot = mock_capture

        with self.assertRaises(NetworkError):
            client.capture_snapshot_with_retry(retries=2, initial_backoff=0.01)
        self.assertEqual(mock_capture.call_count, 2)

    def test_fatal_fingerprint_mismatch_not_retried(self):
        client = BambuCameraClient(host="192.168.1.100", access_code="12345678")
        mock_capture = MagicMock()
        mock_capture.side_effect = FingerprintMismatchError("Bad fingerprint")
        client.capture_snapshot = mock_capture

        with self.assertRaises(FingerprintMismatchError):
            client.capture_snapshot_with_retry(retries=3, initial_backoff=0.01)
        self.assertEqual(mock_capture.call_count, 1)


if __name__ == "__main__":
    unittest.main()
