@echo off
REM Renders a CapCut export on the GPU laptop over the LAN cable.
REM   - Double-click: picks your NEWEST exported video and asks to confirm.
REM     Searched: CapCut's export folder (AppData\Local\CapCut\Videos), Videos,
REM     Downloads, Desktop, OneDrive.
REM   - Or drag any video file onto this .bat to render that one.
REM The whole clip is encoded on the GPU laptop at its original resolution with audio,
REM and the result comes back, SHA-256 verified, into a "renders" folder next to the clip.
setlocal
call "%~dp0config.bat"
set "CLIP=%~1"

if not "%CLIP%"=="" goto :have_clip
echo Looking for your latest CapCut export...
for /f "usebackq delims=" %%F in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_latest_video.ps1"`) do set "CLIP=%%F"
if "%CLIP%"=="" (
    echo No video found in the CapCut export folder, Videos, Downloads, Desktop or OneDrive.
    set /p "CLIP=Drag the video file into this window and press Enter: "
    goto :have_clip
)
echo Found: "%CLIP%"
set "ANS=Y"
set /p "ANS=Render this file? [Y/n]: "
if /i not "%ANS%"=="Y" set /p "CLIP=Drag the video file into this window and press Enter: "

:have_clip
set "CLIP=%CLIP:"=%"
if not exist "%CLIP%" (echo File not found: "%CLIP%" & pause & exit /b 1)
for %%F in ("%CLIP%") do set "OUTDIR=%%~dpFrenders"
cd /d "%~dp0.."
echo Sending "%CLIP%" to GPU worker %WORKER_IP% ...
python -m client.cli --server %WORKER_IP% --port %WORKER_PORT% --token %RENDER_TOKEN% render "%CLIP%" --resolution source --codec h264 --bitrate %RENDER_BITRATE% --preset p5 --out "%OUTDIR%"
if %errorlevel%==0 (
    echo.
    echo DONE. Rendered file is in: "%OUTDIR%"
    explorer "%OUTDIR%"
) else (
    echo.
    echo RENDER FAILED - run 2_CHECK_CONNECTION.bat
)
pause
