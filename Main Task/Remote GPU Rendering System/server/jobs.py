"""
Job queue for the worker.

A job outlives the TCP connection that created it. If the client drops off
in the middle of a render the job keeps going; when the client reconnects it
sends ATTACH <job_id> and starts receiving progress again (and can FETCH the
output once it is done). Finished jobs are kept for `retention` seconds so
there is time to come back for the result.
"""

import logging
import os
import queue
import shutil
import threading
import time
import uuid

from common.ffmpeg import output_name
from common.protocol import Msg, HEARTBEAT_INTERVAL, ConnectionLost, sha256_file
from server.hardware import MetricsSampler

log = logging.getLogger("jobs")


class State:
    UPLOADING = "uploading"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    FINISHED = (COMPLETE, FAILED, CANCELLED)


class Job:
    def __init__(self, kind, config, workdir, client):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.config = config
        self.client = client
        self.dir = os.path.join(workdir, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.input_path = None
        self.input_name = None
        self.output_path = None
        self.output_sha256 = None
        self.state = State.UPLOADING
        self.percent = 0.0
        self.last_progress = {}
        self.result = {}
        self.error = None
        self.created = time.time()
        self.queued_at = None
        self.started_at = None
        self.finished_at = None
        self.cancel_event = threading.Event()
        self._listeners = set()
        self._lock = threading.Lock()

    # ---- listeners (connected clients) ---------------------------------
    def attach(self, channel):
        with self._lock:
            self._listeners.add(channel)

    def detach(self, channel):
        with self._lock:
            self._listeners.discard(channel)

    def emit(self, mtype, **fields):
        """Send a message to every attached client; drop the ones that fail."""
        fields["job_id"] = self.id
        with self._lock:
            targets = list(self._listeners)
        for ch in targets:
            try:
                ch.send(mtype, **fields)
            except (ConnectionLost, OSError):
                log.info("[%s] lost listener %s, job continues", self.id, ch.peer)
                self.detach(ch)

    def snapshot(self):
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "percent": round(self.percent, 2),
            "progress": self.last_progress,
            "error": self.error,
        }

    def completion_message(self):
        msg = {"result": self.result}
        if self.output_path:
            msg["output"] = {
                "name": os.path.basename(self.output_path),
                "size": os.path.getsize(self.output_path),
                "sha256": self.output_sha256,
            }
        msg["timing"] = {
            "queue_s": round((self.started_at or 0) - (self.queued_at or 0), 3) if self.started_at else 0,
            "render_s": self.result.get("render_s"),
        }
        return msg


class JobManager:
    def __init__(self, engine, workdir, slots=1, max_queue=16, retention=1800):
        self.engine = engine
        self.workdir = workdir
        self.max_queue = max_queue
        self.retention = retention
        self.jobs = {}
        self._queue = queue.Queue()
        self._waiting = []                 # ids in queue order, for position reports
        self._lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(workdir, exist_ok=True)

        self._threads = [threading.Thread(target=self._worker_loop, name="gpu-slot-%d" % i, daemon=True)
                         for i in range(slots)]
        self._threads.append(threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True))
        self._threads.append(threading.Thread(target=self._cleanup_loop, name="cleanup", daemon=True))
        for t in self._threads:
            t.start()

    # ---- public --------------------------------------------------------
    def create(self, kind, config, client):
        job = Job(kind, config, self.workdir, client)
        with self._lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def accepting(self):
        return len(self._waiting) < self.max_queue

    def stats(self):
        with self._lock:
            running = sum(1 for j in self.jobs.values() if j.state == State.RUNNING)
        return {"queued": len(self._waiting), "running": running, "max_queue": self.max_queue}

    def enqueue(self, job):
        with self._lock:
            if len(self._waiting) >= self.max_queue:
                raise RuntimeError("queue is full")
            job.state = State.QUEUED
            job.queued_at = time.time()
            self._waiting.append(job.id)
            position = len(self._waiting)
        job.emit(Msg.QUEUED, position=position)      # before the job can possibly start
        self._queue.put(job)
        log.info("[%s] queued (%s), position %d", job.id, job.kind, position)
        return position

    def position(self, job):
        with self._lock:
            try:
                return self._waiting.index(job.id) + 1
            except ValueError:
                return 0

    def cancel(self, job):
        job.cancel_event.set()
        if job.state in (State.QUEUED, State.UPLOADING):
            with self._lock:
                if job.id in self._waiting:
                    self._waiting.remove(job.id)
            self._finish(job, State.CANCELLED)
            job.emit(Msg.CANCELLED)

    def discard(self, job):
        """Remove a job that never got its input (failed upload)."""
        with self._lock:
            self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    def shutdown(self):
        self._stop.set()
        for job in list(self.jobs.values()):
            if job.state not in State.FINISHED:
                job.cancel_event.set()
        for _ in self._threads:
            self._queue.put(None)

    # ---- internals -----------------------------------------------------
    def _finish(self, job, state, error=None):
        job.state = state
        job.error = error
        job.finished_at = time.time()

    def _worker_loop(self):
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                break
            with self._lock:
                if job.id in self._waiting:
                    self._waiting.remove(job.id)
            if job.cancel_event.is_set() or job.state != State.QUEUED:
                continue
            self._run(job)

    def _run(self, job):
        job.state = State.RUNNING
        job.started_at = time.time()
        log.info("[%s] started on %s", job.id, threading.current_thread().name)
        job.emit(Msg.STARTED, queue_s=round(job.started_at - job.queued_at, 3))

        def on_progress(p):
            job.percent = p.get("percent", job.percent)
            job.last_progress = p
            job.emit(Msg.PROGRESS, **p)

        def on_log(line):
            job.emit(Msg.LOG, line=line)

        sampler = MetricsSampler().start()
        try:
            if job.kind == "transcode":
                tag = "nvenc" if self.engine.gpu_encode else "cpu"
                job.output_path = os.path.join(job.dir, output_name(job.input_name, job.config, tag))
                job.result = self.engine.transcode(job.config, job.input_path, job.output_path,
                                                   on_progress, on_log, job.cancel_event)
                job.output_sha256 = sha256_file(job.output_path)
                job.result["output_bytes"] = os.path.getsize(job.output_path)
            else:
                job.result = self.engine.matmul(job.config, on_progress, on_log, job.cancel_event)
            job.result["metrics"] = sampler.stop()
            job.percent = 100.0
            self._finish(job, State.COMPLETE)
            log.info("[%s] complete in %.2f s", job.id, job.result.get("render_s", 0))
            job.emit(Msg.COMPLETE, **job.completion_message())
        except InterruptedError:
            sampler.stop()
            self._finish(job, State.CANCELLED)
            log.info("[%s] cancelled", job.id)
            job.emit(Msg.CANCELLED)
        except Exception as e:                       # report anything else to the client
            sampler.stop()
            self._finish(job, State.FAILED, str(e))
            log.exception("[%s] failed", job.id)
            job.emit(Msg.ERROR, code="job_failed", message=str(e))
        finally:
            if job.input_path and os.path.exists(job.input_path):
                os.remove(job.input_path)            # input is no longer needed

    def _heartbeat_loop(self):
        """Keeps idle-looking connections alive and tells clients their queue position."""
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            for job in list(self.jobs.values()):
                if job.state in (State.QUEUED, State.RUNNING):
                    job.emit(Msg.HEARTBEAT, state=job.state, percent=round(job.percent, 2),
                             position=self.position(job))

    def _cleanup_loop(self):
        while not self._stop.wait(30):
            now = time.time()
            for job in list(self.jobs.values()):
                if job.state in State.FINISHED and now - (job.finished_at or now) > self.retention:
                    log.info("[%s] expired, removing files", job.id)
                    with self._lock:
                        self.jobs.pop(job.id, None)
                    shutil.rmtree(job.dir, ignore_errors=True)
