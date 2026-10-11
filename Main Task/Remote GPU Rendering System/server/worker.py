"""
Remote GPU worker daemon.

Listens on a TCP port, authenticates clients with a shared token, receives
render/compute jobs, runs them on the GPU and streams progress back.

    python -m server --token <secret>
    python -m server --help

Each client connection gets its own session thread (same model as Lab 3).
The actual GPU work happens on a fixed number of "slot" threads owned by the
JobManager, so ten connected clients still only run `--slots` encodes at once.
"""

import argparse
import logging
import logging.handlers
import os
import platform
import secrets
import signal
import socket
import sys
import threading
import time

from common.protocol import (PROTOCOL_VERSION, DEFAULT_PORT, Channel, Msg, ConnectionLost,
                             IntegrityError, ProtocolError, check_auth, tune_socket)
from common.ffmpeg import INPUT_EXTENSIONS
from server.engine import Engine
from server.hardware import gpu_info, system_info
from server.jobs import JobManager, State

log = logging.getLogger("worker")

HANDSHAKE_TIMEOUT = 10.0      # seconds a new connection has to finish HELLO/AUTH
IDLE_TIMEOUT = 600.0          # drop a connection that sends nothing and watches no job


class Session:
    """One connected client."""

    def __init__(self, server, sock, addr):
        self.server = server
        self.addr = "%s:%d" % addr[:2]
        self.ch = Channel(sock, io_timeout=30.0)
        self.watching = set()        # jobs this connection is attached to
        self.client_name = "?"

    # ------------------------------------------------------------------
    def run(self):
        thread = threading.current_thread().name
        log.info("connection from %s (%s)", self.addr, thread)
        try:
            if not self.handshake():
                return
            self.loop()
        except ConnectionLost as e:
            log.info("%s disconnected: %s", self.addr, e)
        except ProtocolError as e:
            log.warning("%s protocol error: %s", self.addr, e)
            self._try_send(Msg.ERROR, code="protocol", message=str(e))
        except Exception:
            log.exception("session %s crashed", self.addr)
        finally:
            for job in self.watching:
                job.detach(self.ch)
            self.ch.close()
            self.server.session_closed(self)
            log.info("session %s closed", self.addr)

    def _try_send(self, mtype, **fields):
        try:
            self.ch.send(mtype, **fields)
        except (ConnectionLost, OSError):
            pass

    # ------------------------------------------------------------------
    def handshake(self):
        self.ch.sock.settimeout(HANDSHAKE_TIMEOUT)
        hello = self.ch.expect(Msg.HELLO)
        if hello.get("version") != PROTOCOL_VERSION:
            self.ch.send(Msg.ERROR, code="version",
                         message="worker speaks protocol v%d, client sent v%s" % (PROTOCOL_VERSION, hello.get("version")))
            return False
        self.client_name = str(hello.get("client", "?"))[:64]

        nonce = secrets.token_hex(16)
        self.ch.send(Msg.CHALLENGE, nonce=nonce, worker=self.server.name)
        auth = self.ch.expect(Msg.AUTH)
        if not check_auth(self.server.token, nonce, auth.get("digest", "")):
            log.warning("%s failed authentication", self.addr)
            time.sleep(1.0)                        # slow down guessing
            self.ch.send(Msg.ERROR, code="auth_failed", message="wrong access token")
            return False

        self.ch.sock.settimeout(self.ch.io_timeout)
        self.ch.send(Msg.WELCOME, **self.server.describe())
        log.info("%s authenticated as '%s'", self.addr, self.client_name)
        return True

    def loop(self):
        idle_since = time.monotonic()
        while not self.server.stopping.is_set():
            if not self.ch.poll(1.0):
                busy = any(j.state not in State.FINISHED for j in self.watching)
                if not busy and time.monotonic() - idle_since > IDLE_TIMEOUT:
                    log.info("%s idle for %.0f s, closing", self.addr, IDLE_TIMEOUT)
                    return
                continue
            idle_since = time.monotonic()
            msg = self.ch.recv()
            handler = getattr(self, "on_" + msg["type"].lower(), None)
            if handler is None:
                self.ch.send(Msg.ERROR, code="unknown_message", message=msg["type"])
                continue
            if handler(msg) is False:
                return

    # ------------------------------------------------------------------
    # message handlers
    # ------------------------------------------------------------------
    def on_ping(self, msg):
        self.ch.send(Msg.PONG, t=msg.get("t"), seq=msg.get("seq"), server_time=time.time(),
                     **self.server.jobs.stats())

    def on_bye(self, msg):
        return False

    def on_submit(self, msg):
        jobs = self.server.jobs
        kind = msg.get("kind", "transcode")
        try:
            config = self.server.engine.validate(kind, msg.get("config") or {})
        except (ValueError, TypeError) as e:
            self.ch.send(Msg.ERROR, code="bad_config", message=str(e))
            return
        if not jobs.accepting():
            self.ch.send(Msg.ERROR, code="busy", message="worker queue is full, try again later")
            return

        needs_file = kind == "transcode"
        meta = msg.get("file") or {}
        if needs_file:
            name = os.path.basename(str(meta.get("name", "")))
            ext = os.path.splitext(name)[1].lower()
            size = int(meta.get("size") or 0)
            sha = str(meta.get("sha256", ""))
            if ext not in INPUT_EXTENSIONS:
                self.ch.send(Msg.ERROR, code="bad_file", message="unsupported file type %r" % ext)
                return
            if not 0 < size <= self.server.max_upload:
                self.ch.send(Msg.ERROR, code="bad_file",
                             message="file must be 1 byte to %d MB" % (self.server.max_upload // 2 ** 20))
                return
            if len(sha) != 64:
                self.ch.send(Msg.ERROR, code="bad_file", message="missing sha256")
                return

        job = jobs.create(kind, config, self.client_name)
        self.ch.send(Msg.READY, job_id=job.id, upload=needs_file)

        if needs_file:
            job.input_name = name
            job.input_path = os.path.join(job.dir, "input" + ext)
            log.info("[%s] receiving %s (%.1f MB) from %s", job.id, name, size / 2 ** 20, self.addr)
            try:
                seconds = self.ch.recv_file(job.input_path, size, sha)
            except IntegrityError as e:
                log.warning("[%s] %s", job.id, e)
                jobs.discard(job)
                self.ch.send(Msg.ERROR, code="integrity", message=str(e), job_id=job.id)
                return
            except ConnectionLost:
                log.warning("[%s] upload interrupted, discarding", job.id)
                jobs.discard(job)
                raise
            mbps = size / 2 ** 20 / seconds if seconds else 0
            log.info("[%s] upload verified, %.2f s (%.1f MB/s)", job.id, seconds, mbps)
            self.ch.send(Msg.UPLOAD_OK, job_id=job.id, bytes=size, seconds=round(seconds, 3),
                         sha256_ok=True)
        else:
            self.ch.send(Msg.UPLOAD_OK, job_id=job.id, bytes=0, seconds=0)

        job.attach(self.ch)
        self.watching.add(job)
        try:
            jobs.enqueue(job)
        except RuntimeError as e:
            jobs.discard(job)
            self.ch.send(Msg.ERROR, code="busy", message=str(e), job_id=job.id)

    def _job_or_error(self, msg):
        job = self.server.jobs.get(str(msg.get("job_id")))
        if job is None:
            self.ch.send(Msg.ERROR, code="unknown_job",
                         message="no such job (it may have expired)", job_id=msg.get("job_id"))
        return job

    def on_attach(self, msg):
        job = self._job_or_error(msg)
        if not job:
            return
        job.attach(self.ch)
        self.watching.add(job)
        log.info("[%s] %s re-attached (state %s, %.0f%%)", job.id, self.addr, job.state, job.percent)
        self.ch.send(Msg.JOB_STATE, **job.snapshot(), position=self.server.jobs.position(job))
        if job.state == State.COMPLETE:
            self.ch.send(Msg.COMPLETE, job_id=job.id, **job.completion_message())
        elif job.state == State.FAILED:
            self.ch.send(Msg.ERROR, code="job_failed", message=job.error, job_id=job.id)
        elif job.state == State.CANCELLED:
            self.ch.send(Msg.CANCELLED, job_id=job.id)

    def on_cancel(self, msg):
        job = self._job_or_error(msg)
        if job:
            log.info("[%s] cancel requested by %s", job.id, self.addr)
            self.server.jobs.cancel(job)

    def on_fetch(self, msg):
        job = self._job_or_error(msg)
        if not job:
            return
        if job.state != State.COMPLETE or not job.output_path or not os.path.exists(job.output_path):
            self.ch.send(Msg.ERROR, code="not_ready", message="job has no output (state: %s)" % job.state,
                         job_id=job.id)
            return
        size = os.path.getsize(job.output_path)
        self.ch.send(Msg.RESULT, job_id=job.id, name=os.path.basename(job.output_path),
                     size=size, sha256=job.output_sha256)
        seconds = self.ch.send_file(job.output_path)
        log.info("[%s] sent result to %s (%.1f MB in %.2f s)", job.id, self.addr, size / 2 ** 20, seconds)

    def on_received(self, msg):
        job = self.server.jobs.get(str(msg.get("job_id")))
        if job and msg.get("ok"):
            log.info("[%s] client confirmed output checksum, cleaning up", job.id)
            job.detach(self.ch)
            self.watching.discard(job)
            self.server.jobs.discard(job)


class WorkerServer:
    def __init__(self, args):
        self.name = args.name or platform.node()
        self.token = args.token
        self.host = args.host
        self.port = args.port
        self.max_upload = args.max_upload_mb * 2 ** 20
        self.max_clients = args.max_clients
        self.engine = Engine(allow_cpu_fallback=args.allow_cpu_fallback, hw_decode=not args.no_hw_decode)
        self.jobs = JobManager(self.engine, os.path.join(args.workdir, "jobs"), slots=args.slots,
                               max_queue=args.max_queue, retention=args.retention)
        self.gpu = gpu_info()
        self.system = system_info()
        self.started = time.time()
        self.stopping = threading.Event()
        self.sessions = set()
        self._lock = threading.Lock()

    def describe(self):
        caps = self.engine.capabilities()
        return {
            "worker": self.name,
            "version": PROTOCOL_VERSION,
            "gpu": self.gpu,
            "system": self.system,
            "capabilities": caps,
            "accepting": self.jobs.accepting() and bool(caps["job_kinds"]),
            "queue": self.jobs.stats(),
            "uptime_s": round(time.time() - self.started),
            "max_upload_mb": self.max_upload // 2 ** 20,
        }

    def session_closed(self, session):
        with self._lock:
            self.sessions.discard(session)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(16)
        srv.settimeout(1.0)                        # wake up regularly to check for shutdown
        log.info("worker '%s' listening on %s:%d", self.name, self.host, self.port)
        if self.gpu:
            log.info("GPU: %s (driver %s, %d MB)", self.gpu["name"], self.gpu["driver"], self.gpu["memory_mb"])
        else:
            log.warning("no NVIDIA GPU detected%s",
                        " - running in CPU fallback mode" if self.engine.allow_cpu_fallback else "")
        try:
            while not self.stopping.is_set():
                try:
                    sock, addr = srv.accept()
                except socket.timeout:
                    continue
                tune_socket(sock)
                with self._lock:
                    too_many = len(self.sessions) >= self.max_clients
                if too_many:
                    log.warning("rejecting %s: %d clients already connected", addr[0], self.max_clients)
                    Channel(sock).send(Msg.ERROR, code="busy", message="too many clients connected")
                    sock.close()
                    continue
                session = Session(self, sock, addr)
                with self._lock:
                    self.sessions.add(session)
                threading.Thread(target=session.run, name="client-%s:%d" % addr[:2], daemon=True).start()
        finally:
            srv.close()
            self.jobs.shutdown()
            log.info("worker stopped")

    def stop(self, *_):
        if not self.stopping.is_set():
            log.info("shutdown requested")
            self.stopping.set()


def setup_logging(log_file, verbose):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-6s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 2 ** 20, backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -m server", description="Remote GPU render worker")
    p.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", default=os.environ.get("RENDER_TOKEN"),
                   help="shared access token (or set RENDER_TOKEN)")
    p.add_argument("--name", help="name shown to clients (default: hostname)")
    p.add_argument("--workdir", default=os.path.join(os.path.expanduser("~"), ".render_worker"))
    p.add_argument("--slots", type=int, default=1, help="jobs that may run on the GPU at the same time")
    p.add_argument("--max-queue", type=int, default=16)
    p.add_argument("--max-clients", type=int, default=32)
    p.add_argument("--max-upload-mb", type=int, default=4096)
    p.add_argument("--retention", type=int, default=1800, help="seconds to keep finished outputs")
    p.add_argument("--allow-cpu-fallback", action="store_true",
                   help="use libx264/NumPy when no NVIDIA GPU is available (testing only)")
    p.add_argument("--no-hw-decode", action="store_true", help="decode on the CPU, encode on the GPU")
    p.add_argument("--log-file", help="also write logs here (default: <workdir>/worker.log)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.token:
        p.error("an access token is required: pass --token or set RENDER_TOKEN")
    if args.log_file is None:
        args.log_file = os.path.join(args.workdir, "worker.log")
    return args


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.log_file, args.verbose)
    server = WorkerServer(args)
    signal.signal(signal.SIGINT, server.stop)
    signal.signal(signal.SIGTERM, server.stop)
    server.serve()


if __name__ == "__main__":
    main()
