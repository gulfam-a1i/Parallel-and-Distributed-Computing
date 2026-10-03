"""
Render on the client itself, with the same FFmpeg settings the worker uses.

This is the baseline for the benchmark ("how long would this take if I
didn't offload it?"). By default the CPU encoder is used, since the whole
point is that the client machine has no capable GPU.
"""

import os
import time

from common import ffmpeg as ff

try:
    import psutil
except ImportError:
    psutil = None


def render_local(src, config, out_dir, encoder=None, on_progress=None, on_log=None, cancel_event=None):
    cfg = ff.validate_job(config)
    ffmpeg_bin = ff.find_binary("ffmpeg")
    encoder = encoder or ff.CODECS[cfg["codec"]][1]          # libx264 / libx265
    info = ff.probe(src, ff.find_binary("ffprobe"))
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, ff.output_name(src, cfg, "local"))
    cmd = ff.build_command(cfg, src, dst, encoder, ffmpeg_bin, hw_decode=False)

    cpu_samples = []

    def progress(p):
        if psutil:
            cpu_samples.append(psutil.cpu_percent(None))
        if on_progress:
            on_progress(p)

    if psutil:
        psutil.cpu_percent(None)
    t0 = time.perf_counter()
    ff.run_with_progress(cmd, info["duration"], progress, on_log, cancel_event)
    elapsed = time.perf_counter() - t0
    return {
        "output_path": dst,
        "encoder": encoder,
        "render_s": round(elapsed, 3),
        "output_bytes": os.path.getsize(dst),
        "input": info,
        "cpu_util_avg": round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None,
        "cpu_util_peak": round(max(cpu_samples), 1) if cpu_samples else None,
    }
