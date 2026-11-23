@echo off
REM Builds GPU_Worker_Laptop.zip in the project folder, to copy to the GPU laptop.
cd /d "%~dp0.."
set "STAGE=%TEMP%\GPU_Worker_Laptop"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"
robocopy server "%STAGE%\server" /e /xd __pycache__ /xf *.pyc /njh /njs /nfl /ndl >nul
robocopy common "%STAGE%\common" /e /xd __pycache__ /xf *.pyc /njh /njs /nfl /ndl >nul
copy /y requirements-server.txt "%STAGE%\" >nul
copy /y lan\lan_setup.ps1 "%STAGE%\" >nul
copy /y lan\worker\START_GPU_WORKER.bat "%STAGE%\" >nul
copy /y lan\worker\README.txt "%STAGE%\" >nul
copy /y LAN_TESTING_GUIDE.md "%STAGE%\" >nul
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%' -DestinationPath 'GPU_Worker_Laptop.zip' -Force"
rmdir /s /q "%STAGE%"
echo Built: %CD%\GPU_Worker_Laptop.zip
