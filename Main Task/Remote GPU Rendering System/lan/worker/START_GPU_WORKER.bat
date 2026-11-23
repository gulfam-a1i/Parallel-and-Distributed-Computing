@echo off
REM ==== Run this on the GPU laptop (double-click). ====
REM 1) sets the LAN cable IP to 192.168.1.1 and opens port 5050 (asks for Admin once)
REM 2) checks Python + FFmpeg + the GPU encoder
REM 3) starts the render worker and waits for jobs from the other laptop
cd /d "%~dp0"
set RENDER_TOKEN=lanrender2026
set WORKER_PORT=5050

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator permission to configure the LAN cable...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============ 1/4  LAN cable setup ============
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lan_setup.ps1" -Role worker -Port %WORKER_PORT%
if %errorlevel% neq 0 (pause & exit /b 1)

echo.
echo ============ 2/4  Python ============
where python >nul 2>&1 || (echo Python not found. Install Python 3.9+ from python.org and tick "Add to PATH". & pause & exit /b 1)
python --version

echo.
echo ============ 3/4  FFmpeg ============
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo FFmpeg not found - trying winget (needs internet once^)...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    echo Close this window and run START_GPU_WORKER.bat again so the new PATH is picked up.
    pause & exit /b 1
)
ffmpeg -hide_banner -encoders 2>nul | findstr /i "nvenc qsv amf"
python -m pip install --disable-pip-version-check -q -r requirements-server.txt 2>nul || echo (pip offline - psutil/numpy optional, continuing)
python -m server.hardware

echo.
echo ============ 4/4  Starting worker on 192.168.1.1:%WORKER_PORT% ============
echo Leave this window open. Jobs from the other laptop will appear below.
echo Press Ctrl+C to stop.
echo.
python -m server --host 0.0.0.0 --port %WORKER_PORT% --token %RENDER_TOKEN%
pause
