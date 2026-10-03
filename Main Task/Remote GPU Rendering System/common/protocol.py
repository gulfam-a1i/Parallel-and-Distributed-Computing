"""
Wire protocol shared by the client and the worker.

Every control message is a frame:

    +----------------+---------------------------+
    | 4 bytes, BE u32 |  UTF-8 JSON object        |
    |  header length  |  {"type": "...", ...}     |
    +----------------+---------------------------+

Bulk data (the input video, the rendered output) is never wrapped in JSON.
The sender first announces it in a control message that carries the exact
byte count and a SHA-256 digest, and then writes the raw bytes straight onto
the socket. The receiver reads exactly that many bytes, hashes them on the
fly and rejects the transfer if either the length or the digest is wrong.

See docs/PROTOCOL.md for the full message sequence.
"""

import hashlib
import hmac
import json
import os
import select
import socket
import struct
import threading
import time

PROTOCOL_VERSION = 1
DEFAULT_PORT = 5050

HEADER = struct.Struct(">I")
MAX_HEADER_BYTES = 1 * 1024 * 1024        # a control frame bigger than this is garbage
CHUNK_SIZE = 1024 * 1024                  # 1 MiB per send/recv for bulk transfers

# The worker sends a HEARTBEAT this often while a job is queued or running.
HEARTBEAT_INTERVAL = 2.0
# The client gives up on a connection after this much silence.
HEARTBEAT_TIMEOUT = 15.0


# --------------------------------------------------------------------------
# Message types
# --------------------------------------------------------------------------
class Msg:
    # handshake
    HELLO = "HELLO"
    CHALLENGE = "CHALLENGE"
    AUTH = "AUTH"
    WELCOME = "WELCOME"
    PING = "PING"
    PONG = "PONG"
    # job submission
    SUBMIT = "SUBMIT"
    READY = "READY"            # worker is ready to receive the input bytes
    UPLOAD_OK = "UPLOAD_OK"
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    LOG = "LOG"
    HEARTBEAT = "HEARTBEAT"
    COMPLETE = "COMPLETE"
    CANCEL = "CANCEL"
    CANCELLED = "CANCELLED"
    # result retrieval / resume
    FETCH = "FETCH"
    RESULT = "RESULT"          # followed by raw output bytes
    RECEIVED = "RECEIVED"
    ATTACH = "ATTACH"
    JOB_STATE = "JOB_STATE"
    BYE = "BYE"
    ERROR = "ERROR"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class ProtocolError(Exception):
    """The peer sent something that does not follow the protocol."""


class ConnectionLost(ConnectionError):
    """The socket closed or went silent in the middle of an exchange."""


class IntegrityError(Exception):
    """A bulk transfer arrived with the wrong size or checksum."""


class RemoteError(Exception):
    """The peer replied with an ERROR frame."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def sha256_file(path, chunk=CHUNK_SIZE):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def auth_digest(token, nonce):
    """Challenge-response so the shared token never crosses the network."""
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def check_auth(token, nonce, digest):
    return hmac.compare_digest(auth_digest(token, nonce), str(digest))


def tune_socket(sock):
    """TCP options used on both ends."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # small frames go out immediately
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)   # let the OS notice dead peers
    for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):          # Linux only; harmless to skip elsewhere
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Channel
# --------------------------------------------------------------------------
class Channel:
    """
    Thin wrapper around a connected TCP socket.

    Sends are serialised with a lock because on the worker the session thread
    and the job thread (progress updates) both write to the same socket.
    Receives are expected to happen from a single thread.
    """

    def __init__(self, sock, io_timeout=30.0):
        self.sock = sock
        self.io_timeout = io_timeout
        self._send_lock = threading.Lock()
        self.closed = False
        self.peer = "peer"
        try:
            name = sock.getpeername()
            if isinstance(name, tuple):
                self.peer = "%s:%d" % name[:2]
        except OSError:
            pass
        sock.settimeout(io_timeout)

    # ---- low level -------------------------------------------------------
    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                part = self.sock.recv(min(n - len(buf), CHUNK_SIZE))
            except socket.timeout:
                raise ConnectionLost("timed out waiting for data from %s" % self.peer)
            except OSError as e:
                raise ConnectionLost(str(e))
            if not part:
                raise ConnectionLost("connection closed by %s" % self.peer)
            buf.extend(part)
        return bytes(buf)

    def _sendall(self, data):
        try:
            self.sock.sendall(data)
        except socket.timeout:
            raise ConnectionLost("timed out sending to %s" % self.peer)
        except OSError as e:
            raise ConnectionLost(str(e))

    # ---- control frames --------------------------------------------------
    def send(self, mtype, **fields):
        fields["type"] = mtype
        body = json.dumps(fields, separators=(",", ":")).encode()
        with self._send_lock:
            self._sendall(HEADER.pack(len(body)) + body)

    def recv(self):
        (length,) = HEADER.unpack(self._recv_exact(HEADER.size))
        if length == 0 or length > MAX_HEADER_BYTES:
            raise ProtocolError("bad frame length %d" % length)
        try:
            msg = json.loads(self._recv_exact(length).decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProtocolError("frame is not valid JSON: %s" % e)
        if not isinstance(msg, dict) or "type" not in msg:
            raise ProtocolError("frame has no type field")
        return msg

    def poll(self, timeout):
        """True if a frame (or EOF) is waiting to be read."""
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            raise ConnectionLost("socket is no longer usable")
        return bool(r)

    def expect(self, *types):
        """Receive one frame and make sure it is one of the given types."""
        msg = self.recv()
        if msg["type"] == Msg.ERROR:
            raise RemoteError(msg.get("code", "error"), msg.get("message", ""))
        if types and msg["type"] not in types:
            raise ProtocolError("expected %s, got %s" % ("/".join(types), msg["type"]))
        return msg

    # ---- bulk data -------------------------------------------------------
    def send_file(self, path, on_progress=None):
        """Write the raw bytes of `path`. The size/hash must already be announced."""
        total = os.path.getsize(path)
        sent = 0
        started = time.perf_counter()
        last_report = 0.0
        with self._send_lock, open(path, "rb") as f:
            while True:
                block = f.read(CHUNK_SIZE)
                if not block:
                    break
                self._sendall(block)
                sent += len(block)
                now = time.perf_counter()
                if on_progress and (now - last_report > 0.1 or sent == total):
                    on_progress(sent, total, now - started)
                    last_report = now
        return time.perf_counter() - started

    def recv_file(self, dest, size, expected_sha256, on_progress=None):
        """
        Read exactly `size` bytes into `dest`, verifying the SHA-256 digest.

        Data goes to `dest + ".part"` first and is only renamed once the hash
        matches, so a dropped connection never leaves a half-written file that
        looks complete.
        """
        tmp = dest + ".part"
        h = hashlib.sha256()
        received = 0
        started = time.perf_counter()
        last_report = 0.0
        try:
            with open(tmp, "wb") as f:
                while received < size:
                    want = min(CHUNK_SIZE, size - received)
                    try:
                        block = self.sock.recv(want)
                    except socket.timeout:
                        raise ConnectionLost("transfer stalled after %d of %d bytes" % (received, size))
                    except OSError as e:
                        raise ConnectionLost(str(e))
                    if not block:
                        raise ConnectionLost("connection dropped after %d of %d bytes" % (received, size))
                    f.write(block)
                    h.update(block)
                    received += len(block)
                    now = time.perf_counter()
                    if on_progress and (now - last_report > 0.1 or received == size):
                        on_progress(received, size, now - started)
                        last_report = now
            digest = h.hexdigest()
            if digest != expected_sha256:
                raise IntegrityError("checksum mismatch: expected %s..., got %s..."
                                     % (expected_sha256[:12], digest[:12]))
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return time.perf_counter() - started

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
