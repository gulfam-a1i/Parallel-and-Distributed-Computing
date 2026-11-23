@echo off
REM Checks cable -> ping -> worker handshake (GPU name, encoder, latency).
cd /d "%~dp0.."
call "%~dp0config.bat"
echo === 1. Ping %WORKER_IP% over the LAN cable ===
ping -n 4 %WORKER_IP%
echo.
echo === 2. Worker handshake on port %WORKER_PORT% ===
python -m client.cli --server %WORKER_IP% --port %WORKER_PORT% --token %RENDER_TOKEN% ping
echo.
if %errorlevel%==0 (echo CONNECTION OK - ready to render.) else (echo CONNECTION FAILED - see the message above.)
pause
