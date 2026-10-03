"""
Local vs. remote rendering benchmark.

For every (resolution, duration) pair this script:
  1. generates a test clip (cached in benchmark/clips/),
  2. renders it on this machine with the CPU encoder,
  3. offloads the same job to the worker and times every phase,
and writes the numbers to benchmark/results/ as CSV, JSON, a Markdown
report and two charts.

Run from the project root:

    python -m benchmark.run_benchmark --server 192.168.1.1 --token <secret>
    python -m benchmark.run_benchmark --server 192.168.1.1 --token <secret> \
        --resolutions 720p,1080p,2160p --durations 10,30,60 --repeats 3
"""

import argparse
import csv
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
import sys
import threading

from common import ffmpeg as ff
from client.connection import WorkerClient, WorkerUnavailable
from client.local_render import render_local

try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
CLIP_DIR = os.path.join(HERE, "clips")
RESULT_DIR = os.path.join(HERE, "results")

SIZES = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
         "1440p": (2560, 1440), "2160p": (3840, 2160)}


# --------------------------------------------------------------------------
# test clips
# --------------------------------------------------------------------------
def make_clip(res, seconds, fps=30):
    """
    Synthetic clip with moving test pattern + film-grain noise.

    The noise matters: a clean test pattern compresses almost for free and
    makes every encoder look fast. Grain forces real motion estimation work,
    closer to camera footage. The mezzanine is encoded at a high bitrate so the
    file size (and therefore upload time) is realistic too.
    """
    os.makedirs(CLIP_DIR, exist_ok=True)
    path = os.path.join(CLIP_DIR, "test_%s_%ds.mp4" % (res, seconds))
    if os.path.exists(path):
        return path
    w, h = SIZES[res]
    mezz_kbps = ff.DEFAULT_BITRATE[h] * 3
    print("  generating %s ..." % os.path.basename(path), flush=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d:duration=%d" % (w, h, fps, seconds),
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=%d" % seconds,
           "-vf", "noise=alls=14:allf=t+u",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "%dk" % mezz_kbps,
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", path + ".tmp.mp4"]
    subprocess.run(cmd, check=True)
    os.replace(path + ".tmp.mp4", path)
    return path


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
def client_environment():
    env = {"host": platform.node(), "os": "%s %s" % (platform.system(), platform.release()),
           "cpu": platform.processor() or platform.machine(), "python": platform.python_version()}
    if psutil:
        env["cpu_cores"] = psutil.cpu_count(logical=True)
        env["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    return env


# --------------------------------------------------------------------------
# one test case
# --------------------------------------------------------------------------
def run_case(args, clip, res, seconds, out_dir):
    out_res = res if args.output == "same" else args.output
    h = ff.RESOLUTIONS.get(out_res) or SIZES[res][1]
    cfg = {"codec": args.codec, "resolution": out_res,
           "bitrate_kbps": ff.DEFAULT_BITRATE[h], "preset": args.preset, "container": "mp4"}
    size_mb = os.path.getsize(clip) / 2 ** 20
    row = {"clip": os.path.basename(clip), "resolution": res, "duration_s": seconds,
           "input_mb": round(size_mb, 2), "output_res": cfg["resolution"], "bitrate_kbps": cfg["bitrate_kbps"]}

    local_runs, remote_runs = [], []
    for rep in range(args.repeats):
        if not args.skip_local:
            print("  local  run %d/%d ..." % (rep + 1, args.repeats), end=" ", flush=True)
            r = render_local(clip, cfg, out_dir, encoder=args.local_encoder)
            print("%.2f s" % r["render_s"])
            local_runs.append(r)
            os.remove(r["output_path"])

        print("  remote run %d/%d ..." % (rep + 1, args.repeats), end=" ", flush=True)
        client = WorkerClient(args.server, args.port, args.token)
        client.connect()
        r = client.transcode(clip, cfg, out_dir, threading.Event())
        client.close()
        t = r["timings"]
        print("%.2f s  (upload %.2f, render %.2f, download %.2f)" % (
            t["total_s"], t["upload_s"], t["render_s"], t.get("download_s", 0)))
        remote_runs.append(r)
        os.remove(r["output_path"])

    med = lambda vals: round(statistics.median(vals), 3) if vals else None
    if local_runs:
        row["local_encoder"] = local_runs[0]["encoder"]
        row["local_s"] = med([r["render_s"] for r in local_runs])
        row["local_cpu_avg"] = med([r["cpu_util_avg"] for r in local_runs if r["cpu_util_avg"] is not None])
    tm = lambda k: med([r["timings"].get(k, 0) for r in remote_runs])
    row.update({
        "remote_encoder": remote_runs[0]["result"].get("encoder"),
        "hash_s": tm("hash_s"),
        "upload_s": tm("upload_s"),
        "queue_s": tm("queue_s"),
        "render_s": tm("render_s"),
        "download_s": tm("download_s"),
        "remote_total_s": tm("total_s"),
        "output_mb": round(remote_runs[0]["output_bytes"] / 2 ** 20, 2),
    })
    m = remote_runs[-1]["result"].get("metrics", {})
    for k in ("gpu_util_avg", "gpu_util_peak", "nvenc_util_avg", "gpu_mem_mb_peak", "gpu_power_w_avg",
              "cpu_util_avg"):
        if k in m:
            row["worker_" + k] = m[k]

    moved_mb = row["input_mb"] + row["output_mb"]
    transfer = row["upload_s"] + row["download_s"]
    row["throughput_mbps"] = round(moved_mb * 8 / transfer, 1) if transfer else None       # megabits/s
    row["overhead_s"] = round(row["remote_total_s"] - row["render_s"], 3)
    row["overhead_pct"] = round(row["overhead_s"] / row["remote_total_s"] * 100, 1)
    if local_runs:
        row["speedup"] = round(row["local_s"] / row["remote_total_s"], 2)
        row["speedup_gpu_only"] = round(row["local_s"] / row["render_s"], 2)
    return row


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------
def write_csv(rows, path):
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def make_charts(rows, out_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not installed, skipping charts")
        return []

    surface, ink, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    blue, orange, aqua, yellow = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": grid, "axes.labelcolor": muted,
                         "xtick.color": muted, "ytick.color": muted, "text.color": ink})
    labels = ["%s / %ds" % (r["resolution"], r["duration_s"]) for r in rows]
    charts = []
    has_local = all(r.get("local_s") for r in rows)

    # -- 1. where the time goes ---------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 0.62 * len(rows) * (2 if has_local else 1) + 1.6), facecolor=surface)
    ax.set_facecolor(surface)
    y, ticks, tick_labels = 0, [], []
    bar_h = 0.38
    for r, lab in zip(rows, labels):
        left = 0
        for key, color in (("upload_s", aqua), ("queue_s", muted), ("render_s", blue), ("download_s", yellow)):
            v = r.get(key) or 0
            if v > 0:
                ax.barh(y, v, left=left, height=bar_h, color=color, edgecolor=surface, linewidth=2)
                left += v
        ax.text(left, y, "  %.1f s" % r["remote_total_s"], va="center", color=muted, fontsize=9)
        ticks.append(y)
        tick_labels.append(lab + "  remote")
        if has_local:
            y += bar_h + 0.06
            ax.barh(y, r["local_s"], height=bar_h, color=orange, edgecolor=surface, linewidth=2)
            ax.text(r["local_s"], y, "  %.1f s" % r["local_s"], va="center", color=muted, fontsize=9)
            ticks.append(y)
            tick_labels.append(lab + "  local")
        y += 0.9
    ax.set_yticks(ticks, tick_labels)
    ax.invert_yaxis()
    ax.set_xlabel("seconds (lower is better)")
    ax.grid(axis="x", color=grid, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    from matplotlib.patches import Patch
    handles = [Patch(color=aqua, label="upload"), Patch(color=muted, label="queue"),
               Patch(color=blue, label="GPU render"), Patch(color=yellow, label="download")]
    if has_local:
        handles.append(Patch(color=orange, label="local CPU render"))
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=len(handles),
              frameon=False, fontsize=9)
    ax.set_title("End-to-end time per test case", loc="left", pad=28, fontsize=12, color=ink)
    fig.tight_layout()
    p = os.path.join(out_dir, "time_breakdown.png")
    fig.savefig(p, dpi=150, facecolor=surface)
    plt.close(fig)
    charts.append(p)

    # -- 2. speedup ---------------------------------------------------------
    if has_local:
        fig, ax = plt.subplots(figsize=(9, 3.6), facecolor=surface)
        ax.set_facecolor(surface)
        xs = range(len(rows))
        vals = [r["speedup"] for r in rows]
        ax.bar(xs, vals, width=0.55, color=blue, edgecolor=surface, linewidth=2)
        for x, v in zip(xs, vals):
            ax.text(x, v, "%.2fx" % v, ha="center", va="bottom", color=ink, fontsize=9)
        ax.set_ylim(0, max(max(vals), 1.0) * 1.18)
        ax.axhline(1.0, color=muted, linewidth=1, linestyle="--")
        ax.text(len(rows) - 0.5, 1.0, " break-even", va="bottom", ha="right", color=muted, fontsize=8)
        ax.set_xticks(list(xs), labels, rotation=0 if len(rows) < 7 else 30)
        ax.set_ylabel("speedup (local / remote)")
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.set_title("End-to-end speedup from offloading", loc="left", fontsize=12, color=ink)
        fig.tight_layout()
        p = os.path.join(out_dir, "speedup.png")
        fig.savefig(p, dpi=150, facecolor=surface)
        plt.close(fig)
        charts.append(p)
    return charts


def write_report(rows, meta, charts, path):
    has_local = all(r.get("local_s") for r in rows)
    w = meta["worker"]
    gpu = w.get("gpu") or {}
    lat = meta["latency"]
    L = []
    L.append("# Benchmark results\n")
    L.append("Generated by `benchmark/run_benchmark.py` on %s. All numbers below are measured, "
             "median of %d run(s) per case.\n" % (meta["date"], meta["repeats"]))

    L.append("## Test environment\n")
    L.append("| | Client (local baseline) | Worker (remote) |")
    L.append("|---|---|---|")
    c = meta["client"]
    L.append("| Host | %s | %s |" % (c["host"], w.get("worker")))
    L.append("| OS | %s | %s |" % (c["os"], w.get("system", {}).get("os", "?")))
    L.append("| CPU | %s (%s threads) | %s (%s threads) |" % (
        c["cpu"], c.get("cpu_cores", "?"), w.get("system", {}).get("cpu", "?"), w.get("system", {}).get("cpu_cores", "?")))
    L.append("| RAM | %s GB | %s GB |" % (c.get("ram_gb", "?"), w.get("system", {}).get("ram_gb", "?")))
    L.append("| GPU | - | %s |" % ("%s, driver %s, %d MB" % (gpu["name"], gpu["driver"], gpu["memory_mb"]) if gpu else "none"))
    if has_local:
        L.append("| Encoder | %s | %s |" % (rows[0].get("local_encoder"), rows[0].get("remote_encoder")))
    L.append("")
    L.append("Network: %s:%d, round-trip latency %.2f ms avg (min %.2f, max %.2f, jitter %.2f ms, %d/%d lost).\n" % (
        meta["server"], meta["port"], lat["avg_ms"], lat["min_ms"], lat["max_ms"], lat["jitter_ms"],
        lat["lost"], lat["count"]))
    L.append("Job settings: codec `%s`, preset `%s`, bitrate per resolution from `common/ffmpeg.py` "
             "(`DEFAULT_BITRATE`), AAC audio 160 kbps.\n" % (meta["codec"], meta["preset"]))

    L.append("## Results\n")
    hdr = ["Case", "Input MB"]
    if has_local:
        hdr += ["Local s"]
    hdr += ["Upload s", "Render s", "Download s", "Remote total s", "Overhead %", "Link Mbit/s"]
    if has_local:
        hdr += ["Speedup", "GPU-only speedup"]
    L.append("| " + " | ".join(hdr) + " |")
    L.append("|" + "---|" * len(hdr))
    for r in rows:
        cells = ["%s, %d s" % (r["resolution"], r["duration_s"]), "%.1f" % r["input_mb"]]
        if has_local:
            cells += ["%.2f" % r["local_s"]]
        cells += ["%.2f" % r["upload_s"], "%.2f" % r["render_s"], "%.2f" % (r["download_s"] or 0),
                  "%.2f" % r["remote_total_s"], "%.0f" % r["overhead_pct"],
                  "%.0f" % r["throughput_mbps"] if r.get("throughput_mbps") else "-"]
        if has_local:
            cells += ["**%.2fx**" % r["speedup"], "%.2fx" % r["speedup_gpu_only"]]
        L.append("| " + " | ".join(cells) + " |")
    L.append("")

    util_keys = [k for k in ("local_cpu_avg", "worker_cpu_util_avg", "worker_gpu_util_avg", "worker_gpu_util_peak",
                             "worker_nvenc_util_avg", "worker_gpu_mem_mb_peak", "worker_gpu_power_w_avg")
                 if any(k in r for r in rows)]
    if util_keys:
        names = {"local_cpu_avg": "Client CPU % (local run)", "worker_cpu_util_avg": "Worker CPU %",
                 "worker_gpu_util_avg": "GPU % avg", "worker_gpu_util_peak": "GPU % peak",
                 "worker_nvenc_util_avg": "NVENC % avg", "worker_gpu_mem_mb_peak": "VRAM MB peak",
                 "worker_gpu_power_w_avg": "GPU W avg"}
        L.append("### Resource utilisation\n")
        L.append("| Case | " + " | ".join(names[k] for k in util_keys) + " |")
        L.append("|" + "---|" * (len(util_keys) + 1))
        for r in rows:
            L.append("| %s, %d s | " % (r["resolution"], r["duration_s"]) +
                     " | ".join("%.0f" % r[k] if r.get(k) is not None else "-" for k in util_keys) + " |")
        L.append("")

    for p in charts:
        L.append("![%s](%s)\n" % (os.path.splitext(os.path.basename(p))[0].replace("_", " "), os.path.basename(p)))

    L.append("## Summary\n")
    tot_remote = sum(r["remote_total_s"] for r in rows)
    tot_render = sum(r["render_s"] for r in rows)
    L.append("- Total remote time %.1f s, of which %.1f s was rendering and %.1f s (%.0f%%) was transfer, "
             "hashing and queueing." % (tot_remote, tot_render, tot_remote - tot_render,
                                         (tot_remote - tot_render) / tot_remote * 100))
    if has_local:
        tot_local = sum(r["local_s"] for r in rows)
        geo = statistics.geometric_mean([r["speedup"] for r in rows])
        best = max(rows, key=lambda r: r["speedup"])
        worst = min(rows, key=lambda r: r["speedup"])
        L.append("- Overall speedup (sum of local times / sum of remote times): **%.2fx** "
                 "(%.1f s local vs %.1f s remote)." % (tot_local / tot_remote, tot_local, tot_remote))
        L.append("- Geometric mean of per-case speedups: %.2fx." % geo)
        L.append("- Best case: %s / %d s at %.2fx. Worst case: %s / %d s at %.2fx." % (
            best["resolution"], best["duration_s"], best["speedup"],
            worst["resolution"], worst["duration_s"], worst["speedup"]))
        slower = [r for r in rows if r["speedup"] < 1]
        if slower:
            L.append("- Offloading was slower than rendering locally for: %s. In these cases the network "
                     "and transfer overhead outweighed the render time saved." % ", ".join(
                         "%s/%ds" % (r["resolution"], r["duration_s"]) for r in slower))
        else:
            L.append("- Offloading was faster than local rendering in every case tested.")
    L.append("")
    L.append("Raw data: `%s`, `%s`." % (os.path.basename(meta["csv"]), os.path.basename(meta["json"])))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m benchmark.run_benchmark")
    p.add_argument("--server", required=True)
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--token", default=os.environ.get("RENDER_TOKEN", ""))
    p.add_argument("--resolutions", default="480p,720p,1080p,2160p")
    p.add_argument("--durations", default="10,30", help="clip lengths in seconds")
    p.add_argument("--codec", default="h264", choices=["h264", "hevc"])
    p.add_argument("--preset", default="p4")
    p.add_argument("--output", default="same", help="'same' = keep input resolution, or e.g. 720p")
    p.add_argument("--local-encoder", default=None, help="default: libx264 / libx265")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--skip-local", action="store_true", help="only measure the remote path")
    p.add_argument("--results", default=RESULT_DIR)
    args = p.parse_args(argv)

    resolutions = [r.strip() for r in args.resolutions.split(",") if r.strip()]
    durations = [int(d) for d in args.durations.split(",") if d.strip()]
    for r in resolutions:
        if r not in SIZES:
            p.error("unknown resolution %s (choose from %s)" % (r, ", ".join(SIZES)))

    os.makedirs(args.results, exist_ok=True)
    scratch = os.path.join(HERE, "clips", "_out")

    print("checking worker %s:%d ..." % (args.server, args.port))
    try:
        c = WorkerClient(args.server, args.port, args.token)
        worker = c.connect()
        latency = c.ping(10)
        c.check_ready("transcode", args.codec)
        c.close()
    except WorkerUnavailable as e:
        print("worker not available:", e)
        return 1
    gpu = worker.get("gpu") or {}
    print("worker '%s', GPU %s, encoder %s, latency %.2f ms" % (
        worker["worker"], gpu.get("name", "none"), worker["capabilities"]["encoders"].get(args.codec),
        latency["avg_ms"]))
    if not worker["capabilities"].get("gpu_encode"):
        print("WARNING: worker is in CPU fallback mode - results will not show GPU performance")

    rows = []
    for res in resolutions:
        for secs in durations:
            print("\n[%s, %d s]" % (res, secs))
            clip = make_clip(res, secs)
            rows.append(run_case(args, clip, res, secs, scratch))

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(args.results, "benchmark_%s.csv" % stamp)
    json_path = os.path.join(args.results, "benchmark_%s.json" % stamp)
    meta = {"date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "server": args.server, "port": args.port,
            "repeats": args.repeats, "codec": args.codec, "preset": args.preset, "client": client_environment(),
            "worker": worker, "latency": latency, "csv": csv_path, "json": json_path}
    write_csv(rows, csv_path)
    with open(json_path, "w") as f:
        json.dump({"meta": meta, "rows": rows}, f, indent=2)
    charts = make_charts(rows, args.results)
    report = os.path.join(args.results, "REPORT.md")
    write_report(rows, meta, charts, report)
    print("\nresults written to %s" % args.results)
    print("  report: %s" % report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
