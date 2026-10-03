"""
End-to-end tests: start a real worker on localhost and talk to it with the
real client. The worker runs with --allow-cpu-fallback so these tests also
pass on a machine without an NVIDIA GPU. Needs ffmpeg on PATH.

    python -m unittest tests.test_end_to_end -v
"""

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from common.protocol import Channel, RemoteError
from client.connection import WorkerClient, WorkerUnavailable, JobCancelled
from server.worker import WorkerServer, parse_args

TOKEN = "unit-test-token"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        args = parse_args(["--host", "127.0.0.1", "--port", str(cls.port), "--token", TOKEN,
                           "--workdir", os.path.join(cls.tmp, "worker"), "--allow-cpu-fallback",
                           "--log-file", os.path.join(cls.tmp, "worker.log")])
        cls.server = WorkerServer(args)
        cls.thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls.thread.start()
        time.sleep(0.5)
        cls.clip = os.path.join(cls.tmp, "clip.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", cls.clip],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def client(self, token=TOKEN, on_event=None):
        c = WorkerClient("127.0.0.1", self.port, token, on_event=on_event)
        c.connect()
        return c

    def cfg(self, **kw):
        base = {"codec": "h264", "resolution": "480p", "bitrate_kbps": 1500, "preset": "p4", "container": "mp4"}
        base.update(kw)
        return base

    # ------------------------------------------------------------------
    def test_handshake_and_ping(self):
        c = self.client()
        self.assertIn("transcode", c.info["capabilities"]["job_kinds"])
        lat = c.ping(5)
        self.assertEqual(lat["lost"], 0)
        c.close()

    def test_wrong_token_rejected(self):
        with self.assertRaises(WorkerUnavailable):
            self.client(token="nope")

    def test_bad_config_rejected(self):
        c = self.client()
        with self.assertRaises(RemoteError) as ctx:
            c.transcode(self.clip, self.cfg(resolution="9999p"), self.tmp, threading.Event())
        self.assertEqual(ctx.exception.code, "bad_config")
        c.close()

    def test_transcode_roundtrip(self):
        progress = []
        c = self.client(on_event=lambda k, d: progress.append(d) if k == "progress" else None)
        res = c.transcode(self.clip, self.cfg(), os.path.join(self.tmp, "out1"), threading.Event())
        c.close()
        self.assertTrue(os.path.isfile(res["output_path"]))
        self.assertGreater(len(progress), 0, "no progress updates were streamed")
        self.assertEqual(progress[-1]["percent"], 100.0)

    def test_resume_after_disconnect(self):
        """Kill the client's socket mid-render; it should reconnect, re-attach and still get the file."""
        dropped = threading.Event()
        holder = {}

        def on_event(kind, data):
            if kind == "progress" and data.get("percent", 0) > 5 and not dropped.is_set():
                dropped.set()
                holder["c"].ch.sock.shutdown(socket.SHUT_RDWR)

        c = WorkerClient("127.0.0.1", self.port, TOKEN, on_event=on_event)
        holder["c"] = c
        c.connect()
        res = c.transcode(self.clip, self.cfg(preset="p7", resolution="720p"),
                          os.path.join(self.tmp, "out2"), threading.Event())
        c.close()
        self.assertTrue(dropped.is_set())
        self.assertGreaterEqual(res["reconnects"], 1)
        self.assertTrue(os.path.isfile(res["output_path"]))

    def test_corrupted_upload_is_retried(self):
        original = Channel.send_file
        calls = {"n": 0}

        def corrupt_once(ch, path, on_progress=None):
            calls["n"] += 1
            if calls["n"] == 1:                       # flip one byte on the first attempt only
                bad = os.path.join(self.tmp, "corrupt.mp4")
                shutil.copy(path, bad)
                with open(bad, "r+b") as f:
                    f.seek(1000)
                    b = f.read(1)
                    f.seek(1000)
                    f.write(bytes([b[0] ^ 0xFF]))
                path = bad
            return original(ch, path, on_progress)

        Channel.send_file = corrupt_once
        try:
            c = self.client()
            res = c.transcode(self.clip, self.cfg(), os.path.join(self.tmp, "out3"), threading.Event())
            c.close()
        finally:
            Channel.send_file = original
        self.assertGreaterEqual(calls["n"], 2)
        self.assertTrue(os.path.isfile(res["output_path"]))

    def test_cancel(self):
        cancel = threading.Event()

        def on_event(kind, data):
            if kind == "progress" and data.get("percent", 0) > 5:
                cancel.set()

        c = self.client(on_event=on_event)
        with self.assertRaises(JobCancelled):
            c.transcode(self.clip, self.cfg(preset="p7", resolution="720p"),
                        os.path.join(self.tmp, "out4"), cancel)
        c.close()

    def test_compute_job(self):
        c = self.client()
        res = c.compute({"size": 1024, "iterations": 5, "dtype": "float32"}, threading.Event())
        c.close()
        self.assertGreater(res["result"]["gflops"], 0)


if __name__ == "__main__":
    unittest.main()
