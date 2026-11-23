"""Unit tests for the framing and file-transfer layer (no FFmpeg needed)."""

import hashlib
import os
import socket
import tempfile
import threading
import unittest

from common.protocol import (Channel, ConnectionLost, IntegrityError, Msg, ProtocolError,
                             auth_digest, check_auth, HEADER)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        a, b = socket.socketpair()
        self.a, self.b = Channel(a, io_timeout=5), Channel(b, io_timeout=5)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.a.close()
        self.b.close()

    def _payload(self, size=3 * 1024 * 1024 + 17):
        path = os.path.join(self.tmp, "payload.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        with open(path, "rb") as f:
            return path, size, hashlib.sha256(f.read()).hexdigest()

    def test_message_roundtrip(self):
        self.a.send(Msg.PING, t=1.5, seq=3, note="héllo")
        msg = self.b.recv()
        self.assertEqual(msg, {"type": "PING", "t": 1.5, "seq": 3, "note": "héllo"})

    def test_oversized_header_rejected(self):
        self.a.sock.sendall(HEADER.pack(50 * 1024 * 1024))
        with self.assertRaises(ProtocolError):
            self.b.recv()

    def test_file_transfer_verified(self):
        path, size, sha = self._payload()
        dest = os.path.join(self.tmp, "out.bin")
        t = threading.Thread(target=self.a.send_file, args=(path,))
        t.start()
        self.b.recv_file(dest, size, sha)
        t.join()
        with open(dest, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), sha)

    def test_checksum_mismatch_leaves_no_file(self):
        path, size, _ = self._payload()
        dest = os.path.join(self.tmp, "out.bin")
        t = threading.Thread(target=self.a.send_file, args=(path,))
        t.start()
        with self.assertRaises(IntegrityError):
            self.b.recv_file(dest, size, "0" * 64)
        t.join()
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_truncated_transfer(self):
        path, size, sha = self._payload()
        dest = os.path.join(self.tmp, "out.bin")
        def half_then_die():
            with open(path, "rb") as f:
                self.a.sock.sendall(f.read(size // 2))
            self.a.sock.shutdown(socket.SHUT_WR)      # peer "crashes" half way through

        t = threading.Thread(target=half_then_die)
        t.start()
        with self.assertRaises(ConnectionLost):
            self.b.recv_file(dest, size, sha)
        t.join()
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_auth(self):
        d = auth_digest("secret", "nonce123")
        self.assertTrue(check_auth("secret", "nonce123", d))
        self.assertFalse(check_auth("wrong", "nonce123", d))


if __name__ == "__main__":
    unittest.main()
