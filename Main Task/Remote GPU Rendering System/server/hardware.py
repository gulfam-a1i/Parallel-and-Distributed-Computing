"""
GPU / system inspection for the worker.

Uses `nvidia-smi` instead of a Python binding so the worker has no hard
dependency beyond the NVIDIA driver itself. psutil is optional and only
used for CPU/RAM numbers.
"""

import platform
import shutil
import subprocess
import threading
import time

try:
    import psutil
except ImportError:          # metrics just get skipped
    psutil = None

_GPU_FIELDS = "utilization.gpu,utilization.encoder,memory.used,memory.total,temperature.gpu,power.draw"
_GPU_FIELDS_OLD = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


def _smi(args, timeout=5):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        res = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def gpu_info():
    """Static description of the first GPU, or None if there is no NVIDIA GPU."""
    out = _smi(["--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"])
    if not out:
        return None
    name, driver, mem = [p.strip() for p in out.splitlines()[0].split(",")[:3]]
    return {"name": name, "driver": driver, "memory_mb": int(float(mem))}


def system_info():
    info = {
        "host": platform.node(),
        "os": "%s %s" % (platform.system(), platform.release()),
        "cpu": platform.processor() or platform.machine(),
        "python": platform.python_version(),
    }
    if psutil:
        info["cpu_cores"] = psutil.cpu_count(logical=True)
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    return info


class _GpuReader:
    """Reads one utilisation sample; falls back if the driver lacks encoder stats."""

    def __init__(self):
        self.fields = _GPU_FIELDS

    def read(self):
        out = _smi(["--query-gpu=" + self.fields, "--format=csv,noheader,nounits"], timeout=3)
        if out is None and self.fields == _GPU_FIELDS:
            self.fields = _GPU_FIELDS_OLD          # older drivers: no utilization.encoder
            out = _smi(["--query-gpu=" + self.fields, "--format=csv,noheader,nounits"], timeout=3)
        if not out:
            return None
        vals = [v.strip() for v in out.splitlines()[0].split(",")]
        names = self.fields.split(",")
        sample = {}
        for n, v in zip(names, vals):
            try:
                sample[n] = float(v)
            except ValueError:
                pass                                # "[N/A]" on some cards
        return sample


class MetricsSampler:
    """
    Samples GPU and CPU usage in a background thread while a job runs and
    reports average / peak values when stopped.
    """

    def __init__(self, interval=0.5):
        self.interval = interval
        self._samples = []
        self._cpu = []
        self._ram = []
        self._stop = threading.Event()
        self._thread = None
        self._gpu = _GpuReader() if shutil.which("nvidia-smi") else None

    def start(self):
        if psutil:
            psutil.cpu_percent(None)                # prime the counter
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            if self._gpu:
                s = self._gpu.read()
                if s:
                    self._samples.append(s)
            if psutil:
                self._cpu.append(psutil.cpu_percent(None))
                self._ram.append(psutil.virtual_memory().percent)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return self.summary()

    def summary(self):
        out = {"samples": len(self._samples) or len(self._cpu)}

        def stats(values, key):
            if values:
                out[key + "_avg"] = round(sum(values) / len(values), 1)
                out[key + "_peak"] = round(max(values), 1)

        for field, key in (("utilization.gpu", "gpu_util"),
                           ("utilization.encoder", "nvenc_util"),
                           ("memory.used", "gpu_mem_mb"),
                           ("temperature.gpu", "gpu_temp_c"),
                           ("power.draw", "gpu_power_w")):
            stats([s[field] for s in self._samples if field in s], key)
        stats(self._cpu, "cpu_util")
        stats(self._ram, "ram_util")
        return out


if __name__ == "__main__":
    # quick self-check:  python -m server.hardware
    print("GPU   :", gpu_info())
    print("System:", system_info())
    m = MetricsSampler().start()
    time.sleep(2)
    print("Sample:", m.stop())
