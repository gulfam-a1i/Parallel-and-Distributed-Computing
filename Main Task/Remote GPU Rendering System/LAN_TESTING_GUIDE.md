# LAN Testing Guide: Render a CapCut Clip on the GPU Laptop

Two laptops joined by one Ethernet cable. You edit in CapCut on the **client
laptop**. The full clip is rendered on the **GPU laptop** and the finished file
comes back over the cable.

| | GPU laptop (worker) | Client laptop (CapCut) |
|---|---|---|
| Cable IP | `192.168.1.1` | `192.168.1.2` |
| What to run | `START_GPU_WORKER.bat` from `GPU_Worker_Laptop.zip` | the `.bat` files in the `lan\` folder |
| Needs | Python 3.9+, FFmpeg (full build) | Python 3.9+ |

Password (token) on both sides: `lanrender2026`. It is set in `lan\config.bat` and in `START_GPU_WORKER.bat`.

---

## Step 0: Before you start (both laptops)

1. Plug the LAN cable into both laptops. The Ethernet icon must not show a red X.
2. Check Python: open a terminal and run `python --version`. It should print 3.9 or newer.
3. **GPU laptop only:** run `ffmpeg -version`. If it says "not recognized", install FFmpeg (`winget install Gyan.FFmpeg`, which needs internet once) and open a new terminal.
4. Copy `GPU_Worker_Laptop.zip` from this project folder to the GPU laptop and extract it (right-click → Extract All).

---

## Step 1: Start the worker (GPU laptop)

Double-click `START_GPU_WORKER.bat` and click **Yes** when Windows asks for Admin.

What you should see:

```
============ 1/4  LAN cable setup ============
Ethernet adapter: Ethernet (...), link 1 Gbps
IP set to 192.168.1.1 / 255.255.255.0
Firewall rules OK.
...
============ 4/4  Starting worker on 192.168.1.1:5050 ============
INFO    engine video accelerator: NVIDIA NVENC        (or Intel Quick Sync / AMD AMF)
INFO    worker worker '<PC name>' listening on 0.0.0.0:5050
```

At this point "No reply from 192.168.1.2" is normal, because the client laptop is not set up yet.
**Leave this window open** for the whole test.

---

## Step 2: Set up the cable (client laptop, once)

In the project folder, open `lan\` and double-click
`1_SETUP_LAN_THIS_LAPTOP.bat`, then click **Yes** for Admin.

You should see `IP set to 192.168.1.2` and then
`Other laptop reachable at 192.168.1.1 over the cable.`

---

## Step 3: Check the connection (client laptop)

Double-click `lan\2_CHECK_CONNECTION.bat`.

Expected result:

```
Reply from 192.168.1.1: bytes=32 time<1ms TTL=128      (4 replies)
connected to '<PC name>'  GPU: <GPU name>  accelerator: NVIDIA NVENC  encoders: h264_nvenc, hevc_nvenc
latency  min 0.3 / avg 0.5 / max 0.9 ms  jitter 0.1 ms  lost 0/5
CONNECTION OK - ready to render.
```

At the same moment the GPU laptop window logs the connection and authentication.

---

## Step 4: Render the CapCut clip (client laptop)

1. In CapCut, click **Export** (top right). Keep **Format: MP4**, **Codec: H.264**,
   resolution **1080p**. **Export to** shows the folder; the default is
   `C:\Users\<you>\AppData\Local\CapCut\Videos`. Click **Export** and wait until it finishes.
2. Double-click `lan\3_RENDER_CAPCUT_CLIP.bat`. It finds your newest export (CapCut's
   folder, Videos, Downloads, Desktop, OneDrive), shows the path, and asks
   `Render this file? [Y/n]`. Press Enter.
   (To render a different file, drag it onto the `.bat` instead.)
3. Watch the progress:

```
upload verified by worker: 85.0 MB in 0.9 s
[worker] input: 1920x1080 h264, 45.00 s @ 30.00 fps
[worker] encoder: h264_nvenc  preset: p5  bitrate: 12000 kbps
render   [##############################] 100.0%  240 fps  8.0x
output verified (sha256 ...)
DONE. Rendered file is in: "...\renders"
```

The `renders` folder next to your clip opens automatically. The output is named
`<clip>_source_h264_<encoder>.mp4`.

For a 4K export, set `RENDER_BITRATE=35000` in `lan\config.bat` first.

---

## Step 5: Verify (show this in the demo)

| Check | How | Pass if |
|---|---|---|
| Rendered on the GPU laptop | GPU laptop: Task Manager → Performance → GPU → **Video Encode** graph during the render | graph rises while rendering |
| Whole clip came back | play the output; or `ffprobe -v error -show_entries format=duration -of csv=p=0 <file>` on input and output | same duration |
| Resolution and audio kept | `ffprobe -v error -show_entries stream=codec_name,width,height -of compact <file>` | same width × height, has an `aac` audio stream |
| File not corrupted | the log shows `upload verified` and `output verified (sha256 ...)` | both lines present |
| Network was the cable | the client log says `connected` with `192.168.1.1`; unplug Wi-Fi and repeat | still works |

**Offline test (the brief requires it):** turn Wi-Fi off on both laptops and run Steps 3–4 again.
It must still work, because the cable is the only link used.

**Fault test (optional):** pull the cable out for 2–3 s during *render*, then plug it back in.
The client reconnects and continues, and the job keeps running on the GPU laptop.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No connected Ethernet adapter found` | cable unplugged or damaged; check the port LEDs, try another cable |
| Link shows `100 Mbps` instead of `1 Gbps` | old or damaged cable; it works but is slower. Use Cat5e/Cat6 |
| Ping: `Request timed out` | run the setup `.bat` on **both** laptops; make sure each IP is set (`ipconfig`) |
| Ping works but `connection refused` | worker window is not running, or was closed |
| `timed out` on port 5050 | firewall: run `START_GPU_WORKER.bat` again as Admin (it adds the rule) |
| `authentication failed` | token differs: compare `lan\config.bat` and `START_GPU_WORKER.bat` |
| Worker says `CPU` / no hardware encoder | update the GPU driver; check `ffmpeg -hide_banner -encoders \| findstr "nvenc qsv amf"` |
| `python is not recognized` | reinstall Python from python.org with **Add to PATH** ticked |

Worker log file on the GPU laptop: `%USERPROFILE%\.render_worker\worker.log`
