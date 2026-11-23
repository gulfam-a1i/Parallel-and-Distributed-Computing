"""
Client side of the protocol.

WorkerClient knows nothing about the GUI. It reports what is happening
through a single `on_event(kind, data)` callback, which the GUI, the CLI and
the benchmark script each hook up in their own way.

Event kinds:
    log       {"text", "level"}
    stage     {"stage"}  connecting / uploading / queued / rendering / downloading / done
    upload    {"sent", "total", "seconds"}
    download  {"received", "total", "seconds"}
    progress  {"percent", "fps", "speed", "eta_s", ...}
    queued    {"position"}
"""

import os
import platform
import socket
import statistics
import time

from common.protocol import (PROTOCOL_VERSION, DEFAULT_PORT, HEARTBEAT_TIMEOUT, Channel, Msg,
                             ConnectionLost, IntegrityError, ProtocolError, RemoteError,
                             auth_digest, sha256_file, tune_socket)

# Messages the worker may push at any time while a job is active.
ASYNC_TYPES = {Msg.HEARTBEAT, Msg.PROGRESS, Msg.LOG, Msg.QUEUED, Msg.STARTED, Msg.JOB_STATE}


class WorkerUnavailable(Exception):
    pass


class JobCancelled(Exception):
    pass


class WorkerClient:
    def __init__(self, host, port=DEFAULT_PORT, token="", on_event=None,
                 connect_timeout=5.0, io_timeout=30.0, retries=3):
        self.host = host
        self.port = int(port)
        self.token = token
        self.on_event = on_event or (lambda kind, data: None)
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.retries = retries
        self.ch = None
        self.info = None
        self.reconnects = 0

    # ------------------------------------------------------------------
    def _emit(self, kind, **data):
        try:
            self.on_event(kind, data)
        except Exception:
            pass                                # a broken UI callback must not kill the transfer

    def log(self, text, level="info"):
        self._emit("log", text=text, level=level)

    # ------------------------------------------------------------------
    # connection + handshake
    # ------------------------------------------------------------------
    def connect(self):
        """Open the TCP connection and run HELLO -> CHALLENGE -> AUTH -> WELCOME."""
        self.close(quiet=True)
        self._emit("stage", stage="connecting")
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except socket.timeout:
            raise WorkerUnavailable("no answer from %s:%d within %.0f s - is the worker running "
                                    "and the firewall open?" % (self.host, self.port, self.connect_timeout))
        except ConnectionRefusedError:
            raise WorkerUnavailable("%s:%d refused the connection - worker not started on that port?"
                                    % (self.host, self.port))
        except OSError as e:
            raise WorkerUnavailable("cannot reach %s:%d (%s) - check the IP address and cable/Wi-Fi"
                                    % (self.host, self.port, e.strerror or e))
        tune_socket(sock)
        ch = Channel(sock, io_timeout=self.io_timeout)
        try:
            ch.send(Msg.HELLO, version=PROTOCOL_VERSION, client=platform.node())
            challenge = ch.expect(Msg.CHALLENGE)
            ch.send(Msg.AUTH, digest=auth_digest(self.token, challenge["nonce"]))
            welcome = ch.expect(Msg.WELCOME)
        except RemoteError as e:
            ch.close()
            if e.code == "auth_failed":
                raise WorkerUnavailable("worker rejected the access token")
            raise WorkerUnavailable(e.message)
        except (ConnectionLost, ProtocolError) as e:
            ch.close()
            raise WorkerUnavailable("handshake failed: %s" % e)
        self.ch = ch
        self.info = welcome
        return welcome

    def ping(self, count=5):
        """Round-trip latency over the already-open connection."""
        rtts = []
        lost = 0
        for seq in range(count):
            t0 = time.perf_counter()
            self.ch.send(Msg.PING, t=t0, seq=seq)
            try:
                pong = self._expect(Msg.PONG)
            except ConnectionLost:
                lost += 1
                continue
            if pong.get("seq") == seq:
                rtts.append((time.perf_counter() - t0) * 1000.0)
            time.sleep(0.05)
        if not rtts:
            raise WorkerUnavailable("worker did not answer any ping")
        return {
            "count": count,
            "lost": lost,
            "min_ms": round(min(rtts), 2),
            "avg_ms": round(statistics.mean(rtts), 2),
            "max_ms": round(max(rtts), 2),
            "jitter_ms": round(statistics.pstdev(rtts), 2),
        }

    def check_ready(self, kind="transcode", codec=None):
        """Raise WorkerUnavailable unless the worker can take this job right now."""
        info = self.info or {}
        caps = info.get("capabilities", {})
        if kind not in caps.get("job_kinds", []):
            raise WorkerUnavailable("worker cannot run %s jobs" % kind)
        if codec and codec not in caps.get("encoders", {}):
            raise WorkerUnavailable("worker has no encoder for %s" % codec)
        if not info.get("accepting", False):
            raise WorkerUnavailable("worker queue is full")

    def close(self, quiet=False):
        if self.ch:
            if not quiet:
                try:
                    self.ch.send(Msg.BYE)
                except (ConnectionLost, OSError):
                    pass
            self.ch.close()
            self.ch = None

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    def transcode(self, path, config, out_dir, cancel_event):
        return self._run_job("transcode", config, cancel_event, path=path, out_dir=out_dir)

    def compute(self, config, cancel_event):
        return self._run_job("cuda_matmul", config, cancel_event)

    def _run_job(self, kind, config, cancel_event, path=None, out_dir=None):
        t_start = time.perf_counter()
        timings = {}
        file_meta = None
        if path:
            t = time.perf_counter()
            file_meta = {"name": os.path.basename(path), "size": os.path.getsize(path),
                         "sha256": sha256_file(path)}
            timings["hash_s"] = time.perf_counter() - t
            self.log("input %s, %.1f MB, sha256 %s..." % (file_meta["name"], file_meta["size"] / 2 ** 20,
                                                          file_meta["sha256"][:16]))

        job_id, timings["upload_s"] = self._submit(kind, config, path, file_meta)
        complete = self._wait(job_id, cancel_event)
        timings["queue_s"] = complete.get("timing", {}).get("queue_s", 0)
        timings["render_s"] = complete.get("result", {}).get("render_s", 0)

        output_path = None
        output_meta = complete.get("output")
        if output_meta:
            output_path, timings["download_s"] = self._fetch(job_id, output_meta, out_dir)
        timings["total_s"] = time.perf_counter() - t_start
        transfer = timings.get("upload_s", 0) + timings.get("download_s", 0)
        timings["transfer_s"] = transfer
        timings["overhead_s"] = timings["total_s"] - timings["render_s"]
        self._emit("stage", stage="done")
        return {
            "job_id": job_id,
            "kind": kind,
            "config": config,
            "output_path": output_path,
            "input_bytes": file_meta["size"] if file_meta else 0,
            "output_bytes": output_meta["size"] if output_meta else 0,
            "timings": {k: round(v, 3) for k, v in timings.items()},
            "result": complete.get("result", {}),
            "reconnects": self.reconnects,
        }

    def _submit(self, kind, config, path, file_meta):
        """SUBMIT + upload, retried if the worker reports a checksum mismatch."""
        for attempt in range(1, self.retries + 1):
            try:
                self.ch.send(Msg.SUBMIT, kind=kind, config=config, file=file_meta)
                ready = self._expect(Msg.READY)
                job_id = ready["job_id"]
                t0 = time.perf_counter()
                if ready.get("upload"):
                    self._emit("stage", stage="uploading")
                    self.ch.send_file(path, lambda s, tot, secs: self._emit("upload", sent=s, total=tot,
                                                                            seconds=secs))
                self._expect(Msg.UPLOAD_OK)
            except ConnectionLost as e:
                # The worker throws away a half-received upload, so start again from scratch.
                self.log("upload interrupted: %s" % e, "error")
                if attempt == self.retries:
                    raise WorkerUnavailable("upload failed %d times, giving up" % self.retries)
                time.sleep(attempt)
                self.connect()
                self.reconnects += 1
                self.log("reconnected, re-sending the file (%d/%d)" % (attempt + 1, self.retries), "warn")
                continue
            except RemoteError as e:
                if e.code == "integrity" and attempt < self.retries:
                    self.log("worker reported a corrupted upload, retrying (%d/%d)" % (attempt + 1, self.retries),
                             "warn")
                    continue
                raise
            upload_s = time.perf_counter() - t0
            if ready.get("upload"):
                mb = file_meta["size"] / 2 ** 20
                self.log("upload verified by worker: %.1f MB in %.2f s (%.1f MB/s)"
                         % (mb, upload_s, mb / upload_s if upload_s else 0))
            self.log("job %s accepted" % job_id)
            return job_id, upload_s
        raise IntegrityError("upload failed checksum verification %d times" % self.retries)

    def _wait(self, job_id, cancel_event):
        """Follow a job until COMPLETE, reconnecting if the link drops."""
        last_heard = time.monotonic()
        cancel_sent = False
        while True:
            if cancel_event is not None and cancel_event.is_set() and not cancel_sent:
                self.log("cancelling job %s" % job_id, "warn")
                try:
                    self.ch.send(Msg.CANCEL, job_id=job_id)
                except ConnectionLost:
                    pass
                cancel_sent = True
            try:
                if not self.ch.poll(0.5):
                    if time.monotonic() - last_heard > HEARTBEAT_TIMEOUT:
                        raise ConnectionLost("no heartbeat from worker for %.0f s" % HEARTBEAT_TIMEOUT)
                    continue
                msg = self.ch.recv()
            except ConnectionLost as e:
                self.log("connection lost: %s" % e, "error")
                self._reattach(job_id)
                last_heard = time.monotonic()
                continue
            last_heard = time.monotonic()
            t = msg["type"]
            if t == Msg.PROGRESS:
                self._emit("stage", stage="rendering")
                msg.pop("type", None)
                msg.pop("job_id", None)
                self._emit("progress", **msg)
            elif t == Msg.LOG:
                self.log("[worker] " + msg.get("line", ""), "worker")
            elif t == Msg.QUEUED:
                self._emit("stage", stage="queued")
                self._emit("queued", position=msg.get("position"))
                self.log("queued at position %s" % msg.get("position"))
            elif t == Msg.STARTED:
                self._emit("stage", stage="rendering")
                self.log("job started (waited %.2f s in queue)" % msg.get("queue_s", 0))
            elif t == Msg.HEARTBEAT:
                if msg.get("state") == "queued":
                    self._emit("queued", position=msg.get("position"))
            elif t == Msg.JOB_STATE:
                self.log("re-attached: job is %s at %.0f%%" % (msg.get("state"), msg.get("percent", 0)))
                self._emit("stage", stage="rendering" if msg.get("state") == "running" else msg.get("state"))
            elif t == Msg.COMPLETE:
                r = msg.get("result", {})
                self.log("job finished on worker in %.2f s" % (r.get("render_s") or 0))
                return msg
            elif t == Msg.CANCELLED:
                raise JobCancelled("job %s was cancelled" % job_id)
            elif t == Msg.ERROR:
                raise RemoteError(msg.get("code", "error"), msg.get("message", ""))

    def _reattach(self, job_id):
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            self.log("reconnecting in %.0f s (attempt %d/%d)" % (delay, attempt, self.retries), "warn")
            time.sleep(delay)
            delay *= 2
            try:
                self.connect()
                self.ch.send(Msg.ATTACH, job_id=job_id)
                self.reconnects += 1
                self.log("reconnected, resuming job %s" % job_id)
                return
            except WorkerUnavailable as e:
                self.log(str(e), "warn")
            except ConnectionLost as e:
                self.log("reconnect failed: %s" % e, "warn")
        raise WorkerUnavailable("lost the worker and could not reconnect after %d attempts" % self.retries)

    def _fetch(self, job_id, meta, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        dest = _unique_path(os.path.join(out_dir, os.path.basename(meta["name"])))
        for attempt in range(1, self.retries + 1):
            self._emit("stage", stage="downloading")
            try:
                self.ch.send(Msg.FETCH, job_id=job_id)
                res = self._expect(Msg.RESULT)
                t0 = time.perf_counter()
                self.ch.recv_file(dest, res["size"], res["sha256"],
                                  lambda r, tot, secs: self._emit("download", received=r, total=tot, seconds=secs))
                seconds = time.perf_counter() - t0
                self.ch.send(Msg.RECEIVED, job_id=job_id, ok=True)
                mb = res["size"] / 2 ** 20
                self.log("output verified (sha256 %s...): %.1f MB in %.2f s"
                         % (res["sha256"][:16], mb, seconds))
                return dest, seconds
            except IntegrityError as e:
                self.log("downloaded file is corrupted (%s), fetching again" % e, "warn")
            except ConnectionLost as e:
                self.log("download interrupted: %s" % e, "error")
                if attempt == self.retries:
                    break
                time.sleep(attempt)
                try:
                    self.connect()
                    self.reconnects += 1
                except WorkerUnavailable as e2:
                    self.log(str(e2), "warn")
        raise WorkerUnavailable("could not download the result after %d attempts" % self.retries)

    def _expect(self, *types):
        """Like Channel.expect, but skips progress/heartbeat frames that arrive in between."""
        while True:
            msg = self.ch.recv()
            if msg["type"] in ASYNC_TYPES and msg["type"] not in types:
                continue
            if msg["type"] == Msg.ERROR:
                raise RemoteError(msg.get("code", "error"), msg.get("message", ""))
            if msg["type"] not in types:
                raise ProtocolError("expected %s, got %s" % ("/".join(types), msg["type"]))
            return msg


def _unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (stem, i, ext)):
        i += 1
    return "%s (%d)%s" % (stem, i, ext)
