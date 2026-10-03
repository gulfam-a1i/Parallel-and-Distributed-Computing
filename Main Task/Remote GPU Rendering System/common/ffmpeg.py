"""
FFmpeg helpers used on both sides.

The worker uses them to run NVENC jobs; the client uses the same command
builder for local (CPU) renders so the benchmark compares like with like:
same scaler, same bitrate, same audio settings, only the encoder differs.

Job settings coming over the network are validated against fixed lists here.
Nothing from the client is ever pasted into the FFmpeg command line as-is.
"""

import json
import os
import shutil
import subprocess
import threading
import time

# ---- allowed job settings ------------------------------------------------
RESOLUTIONS = {            # label -> output height (None = keep source)
    "source": None,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
}

CODECS = {                 # label -> (gpu encoder, cpu encoder)
    "h264": ("h264_nvenc", "libx264"),
    "hevc": ("hevc_nvenc", "libx265"),
}

# NVENC presets p1 (fastest) .. p7 (best quality). The CPU encoders get the
# closest x264/x265 preset so local vs. remote runs are roughly comparable.
PRESETS = {
    "p1": "ultrafast",
    "p2": "superfast",
    "p3": "veryfast",
    "p4": "faster",
    "p5": "fast",
    "p6": "medium",
    "p7": "slow",
}

CONTAINERS = ("mp4", "mkv")
INPUT_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts")

MIN_BITRATE_KBPS = 200
MAX_BITRATE_KBPS = 120_000

DEFAULT_BITRATE = {        # sensible defaults per output height
    480: 2000, 720: 4000, 1080: 8000, 1440: 14000, 2160: 25000,
}


class FFmpegError(RuntimeError):
    pass


def find_binary(name):
    path = shutil.which(name)
    if not path:
        raise FFmpegError("%s not found on PATH" % name)
    return path


def validate_job(cfg):
    """Return a clean copy of a transcode config or raise ValueError."""
    clean = {}
    codec = str(cfg.get("codec", "h264")).lower()
    if codec not in CODECS:
        raise ValueError("unsupported codec %r" % codec)
    clean["codec"] = codec

    res = str(cfg.get("resolution", "source")).lower()
    if res not in RESOLUTIONS:
        raise ValueError("unsupported resolution %r" % res)
    clean["resolution"] = res

    preset = str(cfg.get("preset", "p4")).lower()
    if preset not in PRESETS:
        raise ValueError("unsupported preset %r" % preset)
    clean["preset"] = preset

    try:
        bitrate = int(cfg.get("bitrate_kbps", 8000))
    except (TypeError, ValueError):
        raise ValueError("bitrate must be a number")
    if not MIN_BITRATE_KBPS <= bitrate <= MAX_BITRATE_KBPS:
        raise ValueError("bitrate must be between %d and %d kbps" % (MIN_BITRATE_KBPS, MAX_BITRATE_KBPS))
    clean["bitrate_kbps"] = bitrate

    container = str(cfg.get("container", "mp4")).lower()
    if container not in CONTAINERS:
        raise ValueError("unsupported container %r" % container)
    clean["container"] = container
    return clean


def available_encoders(ffmpeg="ffmpeg"):
    """Names of the video encoders this FFmpeg build knows about."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def nvenc_works(encoder="h264_nvenc", ffmpeg="ffmpeg"):
    """
    Being listed in `-encoders` only means FFmpeg was built with NVENC.
    Encode one tiny frame to prove the driver and GPU actually accept it.
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=256x256:d=0.1", "-frames:v", "1",
           "-c:v", encoder, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe(path, ffprobe="ffprobe"):
    """Duration (s), width, height, fps and codec of the first video stream."""
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-select_streams", "v:0", path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise FFmpegError("ffprobe failed: %s" % e)
    if res.returncode != 0:
        raise FFmpegError("ffprobe could not read the file: %s" % res.stderr.strip()[:300])
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError("no video stream found")
    v = streams[0]
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {
        "duration": duration,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "codec": v.get("codec_name", "?"),
    }


def build_command(cfg, src, dst, encoder, ffmpeg="ffmpeg", hw_decode=False):
    """
    Build the FFmpeg argument list for one transcode.

    `cfg` must already have gone through validate_job().
    """
    kbps = cfg["bitrate_kbps"]
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning"]
    if hw_decode:
        cmd += ["-hwaccel", "cuda"]          # decode on the GPU, falls back to CPU if unsupported
    cmd += ["-i", src]

    height = RESOLUTIONS[cfg["resolution"]]
    if height:
        cmd += ["-vf", "scale=-2:%d:flags=bicubic" % height]

    cmd += ["-c:v", encoder]
    if encoder.endswith("_nvenc"):
        cmd += ["-preset", cfg["preset"], "-tune", "hq", "-rc", "vbr"]
    else:
        cmd += ["-preset", PRESETS[cfg["preset"]]]
    cmd += ["-b:v", "%dk" % kbps, "-maxrate", "%dk" % int(kbps * 1.5), "-bufsize", "%dk" % (kbps * 2)]
    cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
    if cfg["container"] == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]
    return cmd


def run_with_progress(cmd, duration, on_progress=None, on_log=None, cancel_event=None):
    """
    Run FFmpeg and turn its `-progress` output into percentage callbacks.

    on_progress(dict) receives percent, fps, speed, frame, eta_s.
    on_log(str) receives every warning/error line FFmpeg prints.
    Returns the wall-clock seconds the encode took.
    """
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, errors="replace")
    stderr_tail = []

    def pump_stderr():
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            stderr_tail.append(line)
            del stderr_tail[:-30]
            if on_log:
                on_log(line)

    t = threading.Thread(target=pump_stderr, daemon=True)
    t.start()

    stats = {}
    last_sent = 0.0
    cancelled = False
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            key, _, value = line.strip().partition("=")
            stats[key] = value
            if key != "progress":            # a block of stats ends with progress=continue|end
                continue
            out_us = _to_float(stats.get("out_time_us") or stats.get("out_time_ms"))
            done_s = max(out_us / 1_000_000, 0.0)
            pct = min(done_s / duration * 100.0, 100.0) if duration > 0 else 0.0
            speed = _to_float(stats.get("speed", "0").rstrip("x"))
            eta = (duration - done_s) / speed if speed > 0 and duration > 0 else None
            now = time.perf_counter()
            if on_progress and (now - last_sent >= 0.25 or value == "end"):
                on_progress({
                    "percent": round(100.0 if value == "end" else pct, 2),
                    "fps": _to_float(stats.get("fps")),
                    "speed": speed,
                    "frame": int(_to_float(stats.get("frame"))),
                    "eta_s": round(max(eta, 0.0), 1) if eta is not None else None,
                })
                last_sent = now
    finally:
        if cancelled or (cancel_event is not None and cancel_event.is_set()):
            cancelled = True
            terminate(proc)
        proc.wait()
        t.join(timeout=2)

    if cancelled:
        raise InterruptedError("render cancelled")
    if proc.returncode != 0:
        tail = "\n".join(stderr_tail[-5:]) or "no output"
        raise FFmpegError("ffmpeg exited with code %d\n%s" % (proc.returncode, tail))
    return time.perf_counter() - started


def terminate(proc, grace=3.0):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def output_name(src_name, cfg, tag):
    stem = os.path.splitext(os.path.basename(src_name))[0]
    return "%s_%s_%s_%s.%s" % (stem, cfg["resolution"], cfg["codec"], tag, cfg["container"])
