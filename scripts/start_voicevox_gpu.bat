@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_voicevox_gpu.ps1" %*
endlocal
