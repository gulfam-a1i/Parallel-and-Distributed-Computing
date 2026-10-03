# Performance benchmark

This folder holds the script that compares rendering on the client against
offloading to the GPU worker, plus the results from running it.

- Generated results: [`results/REPORT.md`](results/REPORT.md) (written by the script, together with CSV/JSON and charts)
- Script: [`run_benchmark.py`](run_benchmark.py)

## Running it

With the worker running and the network set up (see the main README), run this
on the **client** from the project root:

```bash
python -m benchmark.run_benchmark --server 192.168.1.1 --token <token>
```

Default matrix: 480p, 720p, 1080p and 2160p inputs at 10 s and 30 s each, so
8 cases. A fuller run, with the median of three runs per case:

```bash
python -m benchmark.run_benchmark --server 192.168.1.1 --token <token> \
    --resolutions 480p,720p,1080p,2160p --durations 10,30,60 --repeats 3
```

| Option | Meaning |
|---|---|
| `--resolutions` | input resolutions to test |
| `--durations` | clip lengths in seconds (controls file size) |
| `--repeats` | runs per case, the median is reported |
| `--output same\|720p\|...` | keep input resolution (default) or downscale everything to one size |
| `--codec h264\|hevc` | codec for both sides |
| `--preset p1..p7` | NVENC preset; local x264 uses the closest equivalent |
| `--local-encoder` | override the local encoder (default `libx264` / `libx265`) |
| `--skip-local` | only time the remote path |

Test clips are generated once with FFmpeg and cached in `benchmark/clips/`
(that folder is git-ignored).

## Methodology

**Test input.** A synthetic `testsrc2` pattern with temporal film-grain noise
(`noise=alls=14:allf=t+u`) and a sine-wave audio track. A plain test pattern
compresses almost for free and makes every encoder look fast; the grain forces
real motion-search and residual coding work. The source files are encoded at
3× the target bitrate, so their size, and therefore the upload time, is
realistic.

**Same job on both sides.** Local and remote use the same command builder
(`common/ffmpeg.py`): same scaler, same target bitrate, same VBV buffer, same
AAC audio. Only the encoder changes: `libx264` on the client, `h264_nvenc` on
the worker. NVENC preset `pN` is paired with the x264 preset of similar
speed/quality (`p4 ↔ faster`, see `PRESETS`).

**What is timed.**

| Symbol | Measured as |
|---|---|
| T<sub>local</sub> | wall-clock time of the local FFmpeg process |
| T<sub>hash</sub> | client SHA-256 of the input before upload |
| T<sub>up</sub> | first input byte sent → worker confirms checksum (`UPLOAD_OK`) |
| T<sub>queue</sub> | time the job waited for a free GPU slot |
| T<sub>render</sub> | wall-clock time of the worker's FFmpeg process |
| T<sub>down</sub> | `FETCH` → output written and checksum verified |
| T<sub>remote</sub> | `SUBMIT` → verified output on disk (everything above, end to end) |

**Derived metrics.**

- End-to-end speedup **S = T<sub>local</sub> / T<sub>remote</sub>**. This is the number that matters to the user.
- GPU-only speedup **S<sub>gpu</sub> = T<sub>local</sub> / T<sub>render</sub>**, the speedup if the network were free.
- Network overhead **O = T<sub>remote</sub> − T<sub>render</sub>**, and as a share **O / T<sub>remote</sub>**.
- Effective link throughput = (input MB + output MB) × 8 / (T<sub>up</sub> + T<sub>down</sub>), in Mbit/s.
- Overall speedup = Σ T<sub>local</sub> / Σ T<sub>remote</sub> over all cases, plus the geometric mean of per-case speedups.

**Resource utilisation.** During each remote job the worker samples
`nvidia-smi` every 0.5 s (GPU %, NVENC %, VRAM, power, temperature) and `psutil`
(CPU %, RAM). The client samples its own CPU during local renders. Averages and
peaks go into the report.

**Break-even.** Offloading pays off when
T<sub>up</sub> + T<sub>render</sub> + T<sub>down</sub> < T<sub>local</sub>.
Upload time grows with file size and render time grows with pixel count ×
duration, so short, small clips over a slow link are where offloading can lose.
The report lists any case where it did.

## Notes on fair testing

- Close other heavy programs on both machines while the benchmark runs.
- Plug the laptop into power; on battery most laptops throttle the CPU and the local baseline gets slower than it really is.
- Run once over Ethernet and once over Wi-Fi with `--results results/wifi` to show how much the link changes the picture.
