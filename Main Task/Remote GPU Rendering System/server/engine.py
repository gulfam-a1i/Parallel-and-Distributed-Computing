"""
The part of the worker that actually does the work.

Two kinds of job are supported:

  transcode   - FFmpeg with NVENC (h264_nvenc / hevc_nvenc), CUDA decode
  cuda_matmul - a dense matrix-multiply benchmark on the GPU via PyTorch

If the worker is started with --allow-cpu-fallback and no usable GPU is
found, the same jobs run on the CPU (libx264/libx265, NumPy). That is only
meant for testing the pipeline on a machine without an NVIDIA card; the
WELCOME message tells the client which mode it is in.
"""

import logging
import time

from common import ffmpeg as ff

log = logging.getLogger("engine")

MATMUL_SIZES = (1024, 2048, 4096, 8192)
MATMUL_DTYPES = ("float32", "float16")


class Engine:
    def __init__(self, allow_cpu_fallback=False, hw_decode=True):
        self.ffmpeg = ff.find_binary("ffmpeg")
        self.ffprobe = ff.find_binary("ffprobe")
        self.hw_decode = hw_decode
        self.allow_cpu_fallback = allow_cpu_fallback

        listed = ff.available_encoders(self.ffmpeg)
        self.encoders = {}                         # codec label -> encoder actually used
        for codec, (gpu_enc, cpu_enc) in ff.CODECS.items():
            if gpu_enc in listed and ff.nvenc_works(gpu_enc, self.ffmpeg):
                self.encoders[codec] = gpu_enc
            elif allow_cpu_fallback and cpu_enc in listed:
                self.encoders[codec] = cpu_enc
        self.gpu_encode = any(e.endswith("_nvenc") for e in self.encoders.values())
        if not self.gpu_encode:
            self.hw_decode = False

        self.torch = None
        self.cuda_device = None
        try:
            import torch
            if torch.cuda.is_available():
                self.torch = torch
                self.cuda_device = torch.cuda.get_device_name(0)
        except Exception:                          # torch missing or broken install
            pass

        log.info("encoders: %s", self.encoders or "none")
        log.info("CUDA compute: %s", self.cuda_device or ("CPU fallback" if allow_cpu_fallback else "unavailable"))

    # ---------------------------------------------------------------
    def capabilities(self):
        kinds = []
        if self.encoders:
            kinds.append("transcode")
        if self.torch or self.allow_cpu_fallback:
            kinds.append("cuda_matmul")
        return {
            "job_kinds": kinds,
            "encoders": self.encoders,
            "gpu_encode": self.gpu_encode,
            "cuda_device": self.cuda_device,
            "resolutions": list(ff.RESOLUTIONS),
            "presets": list(ff.PRESETS),
            "matmul_sizes": list(MATMUL_SIZES),
        }

    def validate(self, kind, cfg):
        if kind == "transcode":
            clean = ff.validate_job(cfg)
            if clean["codec"] not in self.encoders:
                raise ValueError("this worker has no encoder for %s" % clean["codec"])
            return clean
        if kind == "cuda_matmul":
            if not (self.torch or self.allow_cpu_fallback):
                raise ValueError("this worker has no CUDA device")
            size = int(cfg.get("size", 4096))
            iters = int(cfg.get("iterations", 50))
            dtype = str(cfg.get("dtype", "float32"))
            if size not in MATMUL_SIZES:
                raise ValueError("matrix size must be one of %s" % (MATMUL_SIZES,))
            if not 1 <= iters <= 1000:
                raise ValueError("iterations must be 1-1000")
            if dtype not in MATMUL_DTYPES:
                raise ValueError("dtype must be float32 or float16")
            return {"size": size, "iterations": iters, "dtype": dtype}
        raise ValueError("unknown job kind %r" % kind)

    # ---------------------------------------------------------------
    def transcode(self, cfg, src, dst, on_progress, on_log, cancel_event):
        info = ff.probe(src, self.ffprobe)
        on_log("input: %dx%d %s, %.2f s @ %.2f fps"
               % (info["width"], info["height"], info["codec"], info["duration"], info["fps"]))
        encoder = self.encoders[cfg["codec"]]
        cmd = ff.build_command(cfg, src, dst, encoder, self.ffmpeg, hw_decode=self.hw_decode)
        on_log("encoder: %s  preset: %s  bitrate: %d kbps"
               % (encoder, cfg["preset"], cfg["bitrate_kbps"]))
        log.debug("cmd: %s", " ".join(cmd))
        render_s = ff.run_with_progress(cmd, info["duration"], on_progress, on_log, cancel_event)
        return {"encoder": encoder, "input": info, "render_s": round(render_s, 3)}

    def matmul(self, cfg, on_progress, on_log, cancel_event):
        n, iters = cfg["size"], cfg["iterations"]
        flops_per_iter = 2 * n ** 3
        if self.torch:
            torch = self.torch
            dtype = getattr(torch, cfg["dtype"])
            dev = torch.device("cuda")
            a = torch.randn(n, n, device=dev, dtype=dtype)
            b = torch.randn(n, n, device=dev, dtype=dtype)
            torch.matmul(a, b)                     # warm-up (cuBLAS init)
            torch.cuda.synchronize()
            backend = "CUDA (%s)" % self.cuda_device
            step = lambda: torch.matmul(a, b)
            sync = torch.cuda.synchronize
        else:
            import numpy as np
            dtype = np.float32 if cfg["dtype"] == "float32" else np.float16
            a = np.random.rand(n, n).astype(dtype)
            b = np.random.rand(n, n).astype(dtype)
            backend = "CPU / NumPy (fallback)"
            step = lambda: a @ b
            sync = lambda: None

        on_log("matmul %dx%d %s x%d on %s" % (n, n, cfg["dtype"], iters, backend))
        started = time.perf_counter()
        report_every = max(1, iters // 100)
        for i in range(1, iters + 1):
            if cancel_event.is_set():
                raise InterruptedError("job cancelled")
            step()
            if i % report_every == 0 or i == iters:
                sync()
                elapsed = time.perf_counter() - started
                rate = i / elapsed
                on_progress({
                    "percent": round(i * 100.0 / iters, 2),
                    "iteration": i,
                    "gflops": round(flops_per_iter * i / elapsed / 1e9, 1),
                    "eta_s": round((iters - i) / rate, 1) if rate else None,
                })
        sync()
        elapsed = time.perf_counter() - started
        gflops = flops_per_iter * iters / elapsed / 1e9
        on_log("finished in %.3f s  ->  %.1f GFLOP/s" % (elapsed, gflops))
        return {"backend": backend, "render_s": round(elapsed, 3), "gflops": round(gflops, 1)}
