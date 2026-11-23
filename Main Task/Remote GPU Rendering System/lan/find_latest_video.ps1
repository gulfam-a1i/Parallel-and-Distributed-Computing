# Prints the newest exported video.
# CapCut desktop exports to %LOCALAPPDATA%\CapCut\Videos by default (changeable in
# the Export dialog, "Export to"). Downloads / Videos / Desktop / OneDrive are searched too.
$dirs = @(
    "D:\CapCut Exports",
    (Join-Path $env:LOCALAPPDATA "CapCut\Videos"),
    [Environment]::GetFolderPath("MyVideos"),
    (Join-Path $env:USERPROFILE "Downloads"),
    [Environment]::GetFolderPath("Desktop"),
    $env:OneDrive
) | Where-Object { $_ -and (Test-Path $_) }

Get-ChildItem $dirs -Recurse -File -Include *.mp4, *.mov, *.webm -ErrorAction SilentlyContinue |
    Where-Object { $_.DirectoryName -notlike "*\renders" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
