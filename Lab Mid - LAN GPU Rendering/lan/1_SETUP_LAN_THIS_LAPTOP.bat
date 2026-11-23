@echo off
REM Run once on THIS laptop (the client). Sets the Ethernet cable IP to 192.168.1.2.
REM Asks for Administrator permission because it changes the network adapter.
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lan_setup.ps1" -Role client
pause
