@echo off
REM Shared settings. RENDER_TOKEN must match START_GPU_WORKER.bat on the GPU laptop.
set WORKER_IP=192.168.1.1
set WORKER_PORT=5050
set RENDER_TOKEN=lanrender2026
REM Output bitrate in kbps for 3_RENDER_CAPCUT_CLIP.bat (12000 suits 1080p, use 35000 for 4K).
set RENDER_BITRATE=12000
