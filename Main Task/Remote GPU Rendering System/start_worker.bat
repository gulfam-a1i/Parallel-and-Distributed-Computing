@echo off
REM Starts the GPU worker on Windows. Edit the token before first use.
cd /d "%~dp0"
set RENDER_TOKEN=change-me
python -m server --host 0.0.0.0 --port 5050 %*
pause
