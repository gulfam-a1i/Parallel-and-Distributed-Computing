"""
Command-line client. Handy for testing the worker without the GUI and for
scripting.

    python -m client.cli ping    --server 192.168.1.1 --token secret
    python -m client.cli render  --server 192.168.1.1 --token secret clip.mp4 --resolution 1080p
    python -m client.cli compute --server 192.168.1.1 --token secret --size 4096 --iterations 100
"""

import argparse
import json
import os
import sys
import threading

from common.protocol import DEFAULT_PORT
from client.connection import WorkerClient, WorkerUnavailable, JobCancelled


def print_event(kind, data):
    if kind == "log":
        sys.stdout.write("\r\033[K" + data["text"] + "\n")
    elif kind == "upload":
        _bar("upload  ", data["sent"] / data["total"] * 100, "%.1f MB/s" % _rate(data["sent"], data["seconds"]))
    elif kind == "download":
        _bar("download", data["received"] / data["total"] * 100, "%.1f MB/s" % _rate(data["received"], data["seconds"]))
    elif kind == "progress":
        extra = []
        if data.get("fps"):
            extra.append("%.0f fps" % data["fps"])
        if data.get("speed"):
            extra.append("%.2fx" % data["speed"])
        if data.get("gflops"):
            extra.append("%.0f GFLOP/s" % data["gflops"])
        if data.get("eta_s") is not None:
            extra.append("ETA %ds" % data["eta_s"])
        _bar("render  ", data.get("percent", 0), "  ".join(extra))
    sys.stdout.flush()


def _rate(nbytes, seconds):
    return nbytes / 2 ** 20 / seconds if seconds else 0.0


def _bar(label, pct, extra=""):
    width = 30
    filled = int(width * min(pct, 100) / 100)
    sys.stdout.write("\r\033[K%s [%s%s] %5.1f%%  %s" % (label, "#" * filled, "." * (width - filled), pct, extra))


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m client.cli")
    p.add_argument("--server", required=True, help="worker IP address")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", default=os.environ.get("RENDER_TOKEN", ""))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="handshake + latency check")

    r = sub.add_parser("render", help="offload a video transcode")
    r.add_argument("input")
    r.add_argument("--codec", default="h264", choices=["h264", "hevc"])
    r.add_argument("--resolution", default="1080p")
    r.add_argument("--bitrate", type=int, default=8000, help="kbps")
    r.add_argument("--preset", default="p4")
    r.add_argument("--container", default="mp4", choices=["mp4", "mkv"])
    r.add_argument("--out", default="renders")

    c = sub.add_parser("compute", help="run the CUDA matrix-multiply job")
    c.add_argument("--size", type=int, default=4096)
    c.add_argument("--iterations", type=int, default=50)
    c.add_argument("--dtype", default="float32", choices=["float32", "float16"])

    args = p.parse_args(argv)
    client = WorkerClient(args.server, args.port, args.token, on_event=print_event)
    cancel = threading.Event()
    try:
        info = client.connect()
        gpu = info.get("gpu") or {}
        print("connected to '%s'  GPU: %s  encoders: %s" % (
            info["worker"], gpu.get("name", "none"), ", ".join(info["capabilities"]["encoders"].values()) or "none"))
        lat = client.ping()
        print("latency  min %.2f / avg %.2f / max %.2f ms  jitter %.2f ms  lost %d/%d" % (
            lat["min_ms"], lat["avg_ms"], lat["max_ms"], lat["jitter_ms"], lat["lost"], lat["count"]))

        if args.command == "render":
            client.check_ready("transcode", args.codec)
            cfg = {"codec": args.codec, "resolution": args.resolution, "bitrate_kbps": args.bitrate,
                   "preset": args.preset, "container": args.container}
            res = client.transcode(args.input, cfg, args.out, cancel)
            print("\nsaved:", res["output_path"])
            print(json.dumps(res["timings"], indent=2))
        elif args.command == "compute":
            client.check_ready("cuda_matmul")
            res = client.compute({"size": args.size, "iterations": args.iterations, "dtype": args.dtype}, cancel)
            print("\n%s: %.1f GFLOP/s in %.2f s" % (res["result"]["backend"], res["result"]["gflops"],
                                                   res["result"]["render_s"]))
        client.close()
    except KeyboardInterrupt:
        cancel.set()
        print("\ninterrupted")
        return 130
    except (WorkerUnavailable, JobCancelled) as e:
        print("\nerror:", e)
        return 1
    except Exception as e:
        print("\nerror: %s: %s" % (type(e).__name__, e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
