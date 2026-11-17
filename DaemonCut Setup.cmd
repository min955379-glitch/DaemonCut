@echo off
setlocal EnableExtensions
title DaemonCut Setup
cd /d "%~dp0"
rem One-click launcher: runs the full automatic setup with an unrestricted
rem execution policy, streaming output to this console window.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DaemonCut Setup.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Setup did not finish successfully ^(exit %RC%^). See the log above and .daemoncut\setup_logs\.
) else (
    echo.
    echo If the services did not open, double-click "DaemonCut Start.lnk" on this folder.
)
endlocal & exit /b %RC%
