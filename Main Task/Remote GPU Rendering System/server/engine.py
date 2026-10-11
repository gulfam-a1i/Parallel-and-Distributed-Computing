"""
The part of the worker that actually does the work.

Two kinds of job are supported:

  transcode   - FFmpeg on a hardware video encoder
  cuda_matmul - a dense matrix-multiply benchmark on the GPU via PyTorch

Encoder selection (first one that passes a test encode wins):

  1. NVIDIA NVENC      h264_nvenc / hevc_nvenc, CUDA decode
  2. Intel Quick Sync  h264_qsv   / hevc_qsv
  3. AMD AMF           h264_amf   / hevc_amf
  4. CPU               libx264 / libx265, only with --allow-cpu-fallback

`--encoder` forces one family, e.g. to compare Quick Sync against the CPU
on the same machine. The WELCOME message tells the client what is in use.
"""

import logging
import time

from common import ffmpeg as ff

log = logging.getLogger("engine")

MATMUL_SIZES = (1024, 2048, 4096, 8192)
MATMUL_DTYPES = ("float32", "float16")


class Engine:
    def __init__(self, allow_cpu_fallback=False, hw_decode=True, prefer="auto"):
        self.ffmpeg = ff.find_binary("ffmpeg")
        self.ffprobe = ff.find_binary("ffprobe")
        self.hw_decode = hw_decode
        self.allow_cpu_fallback = allow_cpu_fallback or prefer == "cpu"

        listed = ff.available_encoders(self.ffmpeg)
        families = ff.HW_ORDER if prefer == "auto" else ((prefer,) if prefer in ff.HW_FAMILIES else ())
        self.encoders = {}                         # codec label -> encoder actually used
        for codec, (_, cpu_enc) in ff.CODECS.items():
            for fam in families:
                enc = ff.HW_FAMILIES[fam][codec]
                if enc in listed and ff.hw_encoder_works(enc, self.ffmpeg):
                    self.encoders[codec] = enc
                    break
                if enc in listed:
                    log.info("%s is in this FFmpeg build but failed the test encode (driver/hardware)", enc)
            if codec not in self.encoders and self.allow_cpu_fallback and cpu_enc in listed:
                self.encoders[codec] = cpu_enc

        fams = {ff.encoder_family(e) for e in self.encoders.values()}
        hw = [f for f in ff.HW_ORDER if f in fams]
        self.family = hw[0] if hw else "cpu"
        self.accelerator = ff.HW_FAMILIES[self.family]["name"] if hw else "CPU (software)"
        self.gpu_encode = bool(hw)
        if not self.gpu_encode:
            self.hw_decode = False

        self.torch = None
        self.torch_backend = None
        self.cuda_device = None
        try:
            import torch
            if torch.cuda.is_available():
                self.torch, self.torch_backend = torch, "cuda"
                self.cuda_device = torch.cuda.get_device_name(0)
            elif hasattr(torch, "xpu") and torch.xpu.is_available():   # Intel GPUs (PyTorch 2.5+)
                self.torch, self.torch_backend = torch, "xpu"
                self.cuda_device = "Intel XPU: %s" % torch.xpu.get_device_name(0)
        except Exception:                          # torch missing or broken install
            pass

        log.info("video accelerator: %s", self.accelerator)
        log.info("encoders: %s", self.encoders or "none")
        log.info("tensor compute: %s", self.cuda_device or ("CPU fallback" if self.allow_cpu_fallback else "unavailable"))

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
            "accelerator": self.accelerator,
            "family": self.family,
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
            dev = torch.device(self.torch_backend)
            sync = torch.cuda.synchronize if self.torch_backend == "cuda" else torch.xpu.synchronize
            a = torch.randn(n, n, device=dev, dtype=dtype)
            b = torch.randn(n, n, device=dev, dtype=dtype)
            torch.matmul(a, b)                     # warm-up (cuBLAS / oneDNN init)
            sync()
            backend = ("CUDA (%s)" % self.cuda_device) if self.torch_backend == "cuda" else self.cuda_device
            step = lambda: torch.matmul(a, b)
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
