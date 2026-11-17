@echo off
setlocal EnableExtensions
title DaemonCut
cd /d "%~dp0"
rem Lightweight launcher: starts the already-installed DaemonCut app
rem (backend + console) without re-running setup. Window stays open.
echo.
echo ===============================================================
echo   DaemonCut - starting...
echo ===============================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DaemonCut Setup.ps1" -QuickStart
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo DaemonCut could not start ^(exit %RC%^).
    echo If DaemonCut has not been set up yet, run "DaemonCut Setup.cmd" first.
)
echo.
pause
endlocal & exit /b %RC%
