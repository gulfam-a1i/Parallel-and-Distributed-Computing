GPU WORKER - copy this whole folder to the GPU laptop
=====================================================

1. Plug the LAN cable into both laptops.
2. On the GPU laptop, double-click START_GPU_WORKER.bat and click Yes for Admin.
   It will:
     - give the cable IP 192.168.1.1 (Wi-Fi/internet is not touched)
     - allow port 5050 + ping in Windows Firewall
     - check Python, FFmpeg and the GPU video encoder (NVENC / Quick Sync / AMF)
     - start the worker and wait for jobs
3. Keep the window open. Each render shows: auth -> upload verified ->
   rendering -> result sent -> client verified checksum.

Needs: Python 3.9+ and FFmpeg (full build) on PATH.
Token: lanrender2026 (must match lan\config.bat on the other laptop).
Logs:  %USERPROFILE%\.render_worker\worker.log
