"""
Desktop client (CustomTkinter).

    python -m client

All network work happens on a background thread. That thread never touches
a widget; it drops events into a queue and the Tk main loop drains the queue
every 80 ms. Tkinter is not thread-safe, so this is the only safe way to
update the UI from a worker thread.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from common import ffmpeg as ff
from common.protocol import DEFAULT_PORT, RemoteError
from client.connection import WorkerClient, WorkerUnavailable, JobCancelled

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".remote_render_client.json")

PRESET_LABELS = {
    "p1": "p1  fastest",
    "p2": "p2",
    "p3": "p3",
    "p4": "p4  balanced",
    "p5": "p5",
    "p6": "p6",
    "p7": "p7  best quality",
}
RES_LABELS = {"source": "Keep source", "2160p": "2160p (4K)", "1440p": "1440p", "1080p": "1080p",
              "720p": "720p", "480p": "480p"}
CODEC_LABELS = {"h264": "H.264 / AVC", "hevc": "H.265 / HEVC"}
STAGE_TEXT = {
    "idle": "Idle",
    "connecting": "Connecting to worker",
    "uploading": "Uploading input",
    "queued": "Waiting in queue",
    "rendering": "Rendering on GPU",
    "downloading": "Downloading result",
    "done": "Finished",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

C = {                               # palette
    "bg": "#101317",
    "panel": "#171b21",
    "card": "#1d222a",
    "border": "#2a313b",
    "text": "#e6e9ee",
    "muted": "#8b95a3",
    "accent": "#3b82f6",
    "accent_hover": "#2f6bd0",
    "ok": "#22c55e",
    "warn": "#f59e0b",
    "err": "#ef4444",
    "term": "#0b0e12",
}


def _key(mapping, label):
    for k, v in mapping.items():
        if v == label:
            return k
    return label


class Card(ctk.CTkFrame):
    def __init__(self, master, title=None, **kw):
        super().__init__(master, fg_color=C["card"], corner_radius=10, border_width=1,
                         border_color=C["border"], **kw)
        self.grid_columnconfigure(0, weight=1)
        self._row = 0
        if title:
            ctk.CTkLabel(self, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=C["muted"], anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew",
                                                                 padx=14, pady=(12, 4))
            self._row = 1

    def next_row(self):
        r = self._row
        self._row += 1
        return r


class StatTile(ctk.CTkFrame):
    def __init__(self, master, label):
        super().__init__(master, fg_color=C["panel"], corner_radius=8)
        self.caption = ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=11), text_color=C["muted"])
        self.caption.pack(anchor="w", padx=12, pady=(8, 0))
        self.value = ctk.CTkLabel(self, text="-", font=ctk.CTkFont(size=20, weight="bold"),
                                  text_color=C["text"])
        self.value.pack(anchor="w", padx=12, pady=(0, 8))

    def set(self, text):
        self.value.configure(text=text)

    def set_caption(self, text):
        self.caption.configure(text=text)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Remote Render Client")
        self.geometry("1220x800")
        self.minsize(1040, 680)
        self.configure(fg_color=C["bg"])

        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.last_output_dir = None
        self.settings = self._load_settings()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_sidebar()
        self._build_main()
        self._set_stage("idle")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)
        self.log("Client ready. Enter the worker IP and press Test connection.", "muted")

    # ==================================================================
    # layout
    # ==================================================================
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0, height=58)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text="Remote Render Client", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=C["text"]).grid(row=0, column=0, padx=(20, 10), pady=14, sticky="w")
        ctk.CTkLabel(bar, text="offload video encoding and CUDA jobs to a GPU worker on the LAN",
                     text_color=C["muted"]).grid(row=0, column=1, sticky="w")
        self.status_pill = ctk.CTkLabel(bar, text="  ●  Not connected  ", corner_radius=12,
                                        fg_color=C["card"], text_color=C["muted"])
        self.status_pill.grid(row=0, column=2, padx=20)

    def _build_sidebar(self):
        side = ctk.CTkScrollableFrame(self, width=330, fg_color=C["bg"], corner_radius=0)
        side.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=14)
        side.grid_columnconfigure(0, weight=1)

        # ---- worker -----------------------------------------------------
        w = Card(side, "Worker")
        w.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.ip_var = tk.StringVar(value=self.settings.get("host", "192.168.1.1"))
        self.port_var = tk.StringVar(value=str(self.settings.get("port", DEFAULT_PORT)))
        self.token_var = tk.StringVar(value=self.settings.get("token", ""))
        self._field(w, "Server IP", ctk.CTkEntry(w, textvariable=self.ip_var))
        self._field(w, "Port", ctk.CTkEntry(w, textvariable=self.port_var, width=100), stretch=False)
        self._field(w, "Access token", ctk.CTkEntry(w, textvariable=self.token_var, show="•"))
        self.test_btn = ctk.CTkButton(w, text="Test connection", fg_color=C["panel"], hover_color=C["border"],
                                      border_width=1, border_color=C["border"], command=self.test_connection)
        self.test_btn.grid(row=w.next_row(), column=0, sticky="ew", padx=14, pady=(6, 14))

        # ---- job --------------------------------------------------------
        j = Card(side, "Job")
        j.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.kind_var = tk.StringVar(value="Video transcode")
        ctk.CTkSegmentedButton(j, values=["Video transcode", "CUDA compute"], variable=self.kind_var,
                               command=lambda _: self._switch_kind()).grid(
            row=j.next_row(), column=0, sticky="ew", padx=14, pady=(4, 10))

        self.video_frame = ctk.CTkFrame(j, fg_color="transparent")
        self.video_frame.grid(row=j.next_row(), column=0, sticky="ew")
        self.video_frame.grid_columnconfigure(0, weight=1)
        self.compute_frame = ctk.CTkFrame(j, fg_color="transparent")
        self.compute_frame.grid_columnconfigure(0, weight=1)
        self._kind_row = j._row - 1

        vf = self.video_frame
        self.input_var = tk.StringVar(value="")
        self.outdir_var = tk.StringVar(value=self.settings.get(
            "out_dir", os.path.join(os.path.expanduser("~"), "RemoteRenders")))
        self._path_field(vf, 0, "Input video", self.input_var, self.pick_input)
        self.input_info = ctk.CTkLabel(vf, text="", text_color=C["muted"], font=ctk.CTkFont(size=11),
                                       anchor="w", justify="left")
        self.input_info.grid(row=2, column=0, sticky="ew", padx=14)
        self._path_field(vf, 3, "Output folder", self.outdir_var, self.pick_outdir)

        self.codec_var = tk.StringVar(value=CODEC_LABELS["h264"])
        self.res_var = tk.StringVar(value=RES_LABELS["1080p"])
        self.bitrate_var = tk.StringVar(value="8000")
        self.preset_var = tk.StringVar(value=PRESET_LABELS["p4"])
        self._opt(vf, 5, "Codec", self.codec_var, list(CODEC_LABELS.values()))
        self._opt(vf, 7, "Resolution", self.res_var, list(RES_LABELS.values()), self._suggest_bitrate)
        self._label(vf, 9, "Bitrate (kbps)")
        ctk.CTkEntry(vf, textvariable=self.bitrate_var).grid(row=10, column=0, sticky="ew", padx=14)
        self._opt(vf, 11, "Preset", self.preset_var, list(PRESET_LABELS.values()))

        cf = self.compute_frame
        self.size_var = tk.StringVar(value="4096")
        self.iter_var = tk.StringVar(value="100")
        self.dtype_var = tk.StringVar(value="float32")
        self._opt(cf, 0, "Matrix size (N x N)", self.size_var, ["1024", "2048", "4096", "8192"])
        self._label(cf, 2, "Iterations")
        ctk.CTkEntry(cf, textvariable=self.iter_var).grid(row=3, column=0, sticky="ew", padx=14)
        self._opt(cf, 4, "Precision", self.dtype_var, ["float32", "float16"])

        btns = ctk.CTkFrame(j, fg_color="transparent")
        btns.grid(row=j._row + 1, column=0, sticky="ew", padx=14, pady=(16, 14))
        btns.grid_columnconfigure(0, weight=1)
        self.start_btn = ctk.CTkButton(btns, text="Start render", height=38, fg_color=C["accent"],
                                       hover_color=C["accent_hover"], font=ctk.CTkFont(weight="bold"),
                                       command=self.start_job)
        self.start_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ctk.CTkButton(btns, text="Cancel", width=90, height=38, fg_color=C["panel"],
                                        hover_color="#3a1d1d", border_width=1, border_color=C["border"],
                                        state="disabled", command=self.cancel_job)
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=1, sticky="nsew", padx=(6, 14), pady=14)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        # ---- worker info --------------------------------------------------
        info = Card(main, "Worker status")
        info.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.info_label = ctk.CTkLabel(info, text="Not connected yet.", justify="left", anchor="w",
                                       text_color=C["text"], font=ctk.CTkFont(family="Consolas", size=12))
        self.info_label.grid(row=info.next_row(), column=0, sticky="ew", padx=14, pady=(0, 12))

        # ---- progress -----------------------------------------------------
        prog = Card(main, "Progress")
        prog.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        head = ctk.CTkFrame(prog, fg_color="transparent")
        head.grid(row=prog.next_row(), column=0, sticky="ew", padx=14)
        head.grid_columnconfigure(0, weight=1)
        self.stage_label = ctk.CTkLabel(head, text="Idle", font=ctk.CTkFont(size=16, weight="bold"), anchor="w")
        self.stage_label.grid(row=0, column=0, sticky="w")
        self.pct_label = ctk.CTkLabel(head, text="0 %", font=ctk.CTkFont(size=16, weight="bold"))
        self.pct_label.grid(row=0, column=1, sticky="e")
        self.bar = ctk.CTkProgressBar(prog, height=12, progress_color=C["accent"])
        self.bar.set(0)
        self.bar.grid(row=prog.next_row(), column=0, sticky="ew", padx=14, pady=(6, 10))

        tiles = ctk.CTkFrame(prog, fg_color="transparent")
        tiles.grid(row=prog.next_row(), column=0, sticky="ew", padx=10)
        for i in range(4):
            tiles.grid_columnconfigure(i, weight=1, uniform="t")
        self.t_rate = StatTile(tiles, "FPS")
        self.t_speed = StatTile(tiles, "Speed")
        self.t_eta = StatTile(tiles, "ETA")
        self.t_net = StatTile(tiles, "Transfer")
        for i, t in enumerate((self.t_rate, self.t_speed, self.t_eta, self.t_net)):
            t.grid(row=0, column=i, sticky="ew", padx=4)
        self.summary_label = ctk.CTkLabel(prog, text="", justify="left", anchor="w", text_color=C["muted"],
                                          font=ctk.CTkFont(family="Consolas", size=12))
        self.summary_label.grid(row=prog.next_row(), column=0, sticky="ew", padx=14, pady=(10, 4))
        self.open_btn = ctk.CTkButton(prog, text="Open output folder", width=160, state="disabled",
                                      fg_color=C["panel"], hover_color=C["border"], border_width=1,
                                      border_color=C["border"], command=self.open_output)
        self.open_btn.grid(row=prog.next_row(), column=0, sticky="w", padx=14, pady=(0, 14))

        # ---- log terminal -------------------------------------------------
        logc = Card(main)
        logc.grid(row=2, column=0, sticky="nsew")
        logc.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(logc, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="LOG", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=C["muted"]).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(top, text="Save", width=60, height=24, fg_color=C["panel"], hover_color=C["border"],
                      command=self.save_log).grid(row=0, column=1, padx=4)
        ctk.CTkButton(top, text="Clear", width=60, height=24, fg_color=C["panel"], hover_color=C["border"],
                      command=lambda: self._term_clear()).grid(row=0, column=2)
        self.term = ctk.CTkTextbox(logc, fg_color=C["term"], text_color="#c9d1d9", corner_radius=6,
                                   font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.term.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.term.configure(state="disabled")
        for tag, color in (("muted", C["muted"]), ("ok", C["ok"]), ("warn", C["warn"]), ("error", C["err"]),
                           ("worker", "#7aa2f7"), ("time", "#4b5563")):
            self.term.tag_config(tag, foreground=color)

    # ---- small layout helpers --------------------------------------------
    def _label(self, parent, row, text):
        ctk.CTkLabel(parent, text=text, text_color=C["muted"], font=ctk.CTkFont(size=12), anchor="w").grid(
            row=row, column=0, sticky="ew", padx=14, pady=(8, 2))

    def _field(self, card, label, widget, stretch=True):
        self._label(card, card.next_row(), label)
        widget.grid(row=card.next_row(), column=0, sticky="ew" if stretch else "w", padx=14)

    def _opt(self, parent, row, label, var, values, command=None):
        self._label(parent, row, label)
        ctk.CTkOptionMenu(parent, variable=var, values=values, fg_color=C["panel"], button_color=C["border"],
                          button_hover_color=C["accent"], command=command).grid(
            row=row + 1, column=0, sticky="ew", padx=14)

    def _path_field(self, parent, row, label, var, command):
        self._label(parent, row, label)
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row + 1, column=0, sticky="ew", padx=14)
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(f, textvariable=var).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(f, text="Browse", width=70, fg_color=C["panel"], hover_color=C["border"],
                      border_width=1, border_color=C["border"], command=command).grid(row=0, column=1, padx=(6, 0))

    def _switch_kind(self):
        if self.kind_var.get() == "Video transcode":
            self.compute_frame.grid_forget()
            self.video_frame.grid(row=self._kind_row, column=0, sticky="ew")
            self.t_rate.set_caption("FPS")
            self.t_speed.set_caption("Speed")
            self.start_btn.configure(text="Start render")
        else:
            self.video_frame.grid_forget()
            self.compute_frame.grid(row=self._kind_row, column=0, sticky="ew")
            self.t_rate.set_caption("Throughput")
            self.t_speed.set_caption("Iteration")
            self.start_btn.configure(text="Start compute job")

    # ==================================================================
    # actions
    # ==================================================================
    def pick_input(self):
        path = filedialog.askopenfilename(title="Choose a video", filetypes=[
            ("Video files", " ".join("*" + e for e in ff.INPUT_EXTENSIONS)), ("All files", "*.*")])
        if not path:
            return
        self.input_var.set(path)
        try:
            info = ff.probe(path)
            size = os.path.getsize(path) / 2 ** 20
            self.input_info.configure(text="%dx%d  %s  %.1f s  %.2f fps  %.1f MB" % (
                info["width"], info["height"], info["codec"], info["duration"], info["fps"], size))
        except Exception as e:
            self.input_info.configure(text="could not read file: %s" % e)

    def pick_outdir(self):
        path = filedialog.askdirectory(title="Where should rendered files go?")
        if path:
            self.outdir_var.set(path)

    def _suggest_bitrate(self, label):
        height = ff.RESOLUTIONS.get(_key(RES_LABELS, label))
        if height in ff.DEFAULT_BITRATE:
            self.bitrate_var.set(str(ff.DEFAULT_BITRATE[height]))

    def _connection_params(self):
        host = self.ip_var.get().strip()
        if not host:
            raise ValueError("enter the worker's IP address")
        try:
            port = int(self.port_var.get())
        except ValueError:
            raise ValueError("port must be a number")
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535")
        return host, port, self.token_var.get()

    def test_connection(self):
        if self.busy:
            return
        try:
            host, port, token = self._connection_params()
        except ValueError as e:
            messagebox.showerror("Connection", str(e))
            return
        self._set_busy(True, test_only=True)
        self.log("connecting to %s:%d ..." % (host, port))
        threading.Thread(target=self._test_thread, args=(host, port, token), daemon=True).start()

    def _test_thread(self, host, port, token):
        client = WorkerClient(host, port, token, on_event=self._post)
        try:
            t0 = time.perf_counter()
            info = client.connect()
            hs = (time.perf_counter() - t0) * 1000
            lat = client.ping(5)
            client.close()
            self._post("connected", {"info": info, "latency": lat, "handshake_ms": hs})
        except Exception as e:
            self._post("conn_failed", {"error": str(e)})
        finally:
            self._post("idle", {})

    def _build_job(self):
        if self.kind_var.get() == "Video transcode":
            path = self.input_var.get().strip()
            if not path or not os.path.isfile(path):
                raise ValueError("choose an input video first")
            try:
                bitrate = int(self.bitrate_var.get())
            except ValueError:
                raise ValueError("bitrate must be a whole number (kbps)")
            cfg = ff.validate_job({
                "codec": _key(CODEC_LABELS, self.codec_var.get()),
                "resolution": _key(RES_LABELS, self.res_var.get()),
                "bitrate_kbps": bitrate,
                "preset": _key(PRESET_LABELS, self.preset_var.get()),
                "container": "mp4",
            })
            return "transcode", cfg, path
        try:
            iters = int(self.iter_var.get())
        except ValueError:
            raise ValueError("iterations must be a whole number")
        return "cuda_matmul", {"size": int(self.size_var.get()), "iterations": iters,
                               "dtype": self.dtype_var.get()}, None

    def start_job(self):
        if self.busy:
            return
        try:
            host, port, token = self._connection_params()
            kind, cfg, path = self._build_job()
        except ValueError as e:
            messagebox.showerror("Cannot start", str(e))
            return
        out_dir = self.outdir_var.get().strip() or os.path.join(os.path.expanduser("~"), "RemoteRenders")
        self._save_settings()
        self.cancel_event.clear()
        self._reset_progress()
        self._set_busy(True)
        self.log("-" * 60, "time")
        self.log("starting %s job: %s" % (kind, json.dumps(cfg)))
        threading.Thread(target=self._job_thread, args=(host, port, token, kind, cfg, path, out_dir),
                         daemon=True).start()

    def _job_thread(self, host, port, token, kind, cfg, path, out_dir):
        client = WorkerClient(host, port, token, on_event=self._post)
        try:
            info = client.connect()
            lat = client.ping(3)
            self._post("connected", {"info": info, "latency": lat, "quiet": True})
            client.check_ready(kind, cfg.get("codec"))
            if kind == "transcode":
                res = client.transcode(path, cfg, out_dir, self.cancel_event)
            else:
                res = client.compute(cfg, self.cancel_event)
            client.close()
            self._post("job_done", {"result": res, "out_dir": out_dir})
        except JobCancelled:
            self._post("job_cancelled", {})
        except (WorkerUnavailable, RemoteError) as e:
            self._post("job_failed", {"error": str(e)})
        except Exception as e:
            self._post("job_failed", {"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            client.close(quiet=True)
            self._post("idle", {})

    def cancel_job(self):
        if self.busy and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_btn.configure(state="disabled")
            self.log("cancel requested", "warn")

    def open_output(self):
        path = self.last_output_dir
        if not path or not os.path.isdir(path):
            return
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log", initialfile="render-client.log",
                                            filetypes=[("Log file", "*.log"), ("Text", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.term.get("1.0", "end"))
            self.log("log saved to %s" % path, "ok")

    # ==================================================================
    # events from the background thread
    # ==================================================================
    def _post(self, kind, data):
        self.events.put((kind, data))

    def _drain_events(self):
        try:
            for _ in range(200):                       # cap per tick so the UI stays responsive
                kind, data = self.events.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _handle(self, kind, d):
        if kind == "log":
            self.log(d["text"], d.get("level", "info"))
        elif kind == "stage":
            self._set_stage(d["stage"])
        elif kind == "upload":
            self._transfer(d["sent"], d["total"], d["seconds"], "up")
        elif kind == "download":
            self._transfer(d["received"], d["total"], d["seconds"], "down")
        elif kind == "progress":
            self._progress(d)
        elif kind == "queued":
            self.t_eta.set("#%s in queue" % d.get("position", "?"))
        elif kind == "connected":
            self._show_worker(d["info"], d["latency"], d.get("handshake_ms"), d.get("quiet"))
        elif kind == "conn_failed":
            self._pill("Unreachable", C["err"])
            self.info_label.configure(text="Could not connect:\n" + d["error"])
            self.log(d["error"], "error")
        elif kind == "job_done":
            self._job_done(d["result"], d["out_dir"])
        elif kind == "job_failed":
            self._set_stage("failed")
            self.log("job failed: " + d["error"], "error")
            messagebox.showerror("Job failed", d["error"])
        elif kind == "job_cancelled":
            self._set_stage("cancelled")
            self.log("job cancelled", "warn")
        elif kind == "idle":
            self._set_busy(False)

    def _show_worker(self, info, lat, handshake_ms, quiet):
        gpu = info.get("gpu") or {}
        caps = info.get("capabilities", {})
        mode = "GPU (NVENC)" if caps.get("gpu_encode") else "CPU fallback - no NVENC on worker"
        q = info.get("queue", {})
        lines = [
            "worker     %s   (%s)" % (info.get("worker"), info.get("system", {}).get("os", "")),
            "gpu        %s" % ("%s, driver %s, %d MB" % (gpu["name"], gpu["driver"], gpu["memory_mb"])
                               if gpu else "none detected"),
            "encoders   %s" % (", ".join("%s->%s" % kv for kv in caps.get("encoders", {}).items()) or "none"),
            "cuda       %s" % (caps.get("cuda_device") or "not available"),
            "mode       %s" % mode,
            "queue      %d waiting, %d running" % (q.get("queued", 0), q.get("running", 0)),
            "latency    %.2f ms avg  (min %.2f / max %.2f, jitter %.2f, lost %d/%d)" % (
                lat["avg_ms"], lat["min_ms"], lat["max_ms"], lat["jitter_ms"], lat["lost"], lat["count"]),
        ]
        self.info_label.configure(text="\n".join(lines))
        if info.get("accepting"):
            self._pill("Connected  %.1f ms" % lat["avg_ms"], C["ok"] if caps.get("gpu_encode") else C["warn"])
        else:
            self._pill("Worker busy", C["warn"])
        if not quiet:
            hs = " (handshake %.0f ms)" % handshake_ms if handshake_ms else ""
            self.log("connected to %s%s, latency %.2f ms" % (info.get("worker"), hs, lat["avg_ms"]), "ok")
            if not caps.get("gpu_encode"):
                self.log("worker has no working NVENC encoder - jobs will run on its CPU", "warn")

    def _progress(self, d):
        pct = d.get("percent", 0) or 0
        self.bar.set(pct / 100.0)
        self.pct_label.configure(text="%.1f %%" % pct)
        if "gflops" in d:
            self.t_rate.set("%.0f GFLOP/s" % d["gflops"])
            self.t_speed.set("%d" % d.get("iteration", 0))
        else:
            self.t_rate.set("%.0f" % d.get("fps", 0) if d.get("fps") else "-")
            self.t_speed.set("%.2fx" % d["speed"] if d.get("speed") else "-")
        eta = d.get("eta_s")
        self.t_eta.set(_fmt_secs(eta) if eta is not None else "-")

    def _transfer(self, done, total, seconds, direction):
        pct = done / total * 100 if total else 0
        rate = done / 2 ** 20 / seconds if seconds else 0
        self.bar.set(pct / 100.0)
        self.pct_label.configure(text="%.1f %%" % pct)
        arrow = "↑" if direction == "up" else "↓"
        self.t_net.set("%s %.1f MB/s" % (arrow, rate))

    def _job_done(self, res, out_dir):
        t = res["timings"]
        self._set_stage("done")
        self.bar.set(1)
        self.pct_label.configure(text="100 %")
        r = res["result"]
        if res["kind"] == "transcode":
            m = r.get("metrics", {})
            lines = [
                "total %-8s upload %-8s queue %-8s render %-8s download %s" % (
                    _fmt_secs(t["total_s"]), _fmt_secs(t.get("upload_s", 0)), _fmt_secs(t.get("queue_s", 0)),
                    _fmt_secs(t["render_s"]), _fmt_secs(t.get("download_s", 0))),
                "encoder %s   output %.1f MB   network overhead %.0f%% of total" % (
                    r.get("encoder"), res["output_bytes"] / 2 ** 20,
                    t["overhead_s"] / t["total_s"] * 100 if t["total_s"] else 0),
            ]
            if "gpu_util_avg" in m:
                lines.append("gpu util avg %.0f%% / peak %.0f%%   nvenc %s   vram peak %s MB" % (
                    m["gpu_util_avg"], m["gpu_util_peak"],
                    "%.0f%%" % m["nvenc_util_avg"] if "nvenc_util_avg" in m else "n/a",
                    int(m.get("gpu_mem_mb_peak", 0))))
            self.summary_label.configure(text="\n".join(lines))
            self.last_output_dir = out_dir
            self.open_btn.configure(state="normal")
            self.log("saved %s" % res["output_path"], "ok")
        else:
            self.summary_label.configure(text="%s\n%.1f GFLOP/s   compute %s   total %s" % (
                r.get("backend"), r.get("gflops", 0), _fmt_secs(r.get("render_s", 0)), _fmt_secs(t["total_s"])))
            self.log("compute job finished: %.1f GFLOP/s" % r.get("gflops", 0), "ok")

    # ==================================================================
    # ui state helpers
    # ==================================================================
    def log(self, text, level="info"):
        self.term.configure(state="normal")
        self.term.insert("end", time.strftime("%H:%M:%S "), "time")
        self.term.insert("end", text + "\n", level if level != "info" else ())
        self.term.see("end")
        lines = int(self.term.index("end-1c").split(".")[0])
        if lines > 3000:                                # keep the widget fast on long jobs
            self.term.delete("1.0", "%d.0" % (lines - 3000))
        self.term.configure(state="disabled")

    def _term_clear(self):
        self.term.configure(state="normal")
        self.term.delete("1.0", "end")
        self.term.configure(state="disabled")

    def _pill(self, text, color):
        self.status_pill.configure(text="  ●  %s  " % text, text_color=color)

    def _set_stage(self, stage):
        self.stage_label.configure(text=STAGE_TEXT.get(stage, stage))
        color = {"done": C["ok"], "failed": C["err"], "cancelled": C["warn"]}.get(stage, C["accent"])
        self.bar.configure(progress_color=color)
        if stage in ("uploading", "downloading", "rendering"):
            self.bar.set(0)
            self.pct_label.configure(text="0 %")

    def _reset_progress(self):
        for t in (self.t_rate, self.t_speed, self.t_eta, self.t_net):
            t.set("-")
        self.summary_label.configure(text="")
        self.open_btn.configure(state="disabled")
        self.bar.set(0)
        self.pct_label.configure(text="0 %")

    def _set_busy(self, busy, test_only=False):
        self.busy = busy
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.test_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy and not test_only else "disabled")

    # ---- settings ----------------------------------------------------------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {"host": self.ip_var.get().strip(), "port": self.port_var.get().strip(),
                "out_dir": self.outdir_var.get().strip()}
        if self.token_var.get():
            data["token"] = self.token_var.get()
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _on_close(self):
        if self.busy and not messagebox.askyesno("Quit", "A job is still running. Cancel it and quit?"):
            return
        self.cancel_event.set()
        self._save_settings()
        self.destroy()


def _fmt_secs(s):
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return "%.2fs" % s if s < 10 else "%.1fs" % s
    m, s = divmod(int(s), 60)
    return "%dm%02ds" % (m, s)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
