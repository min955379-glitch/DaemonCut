@echo off
setlocal EnableExtensions
title DaemonCut Setup
cd /d "%~dp0"
rem One-click launcher: runs the full automatic setup with an unrestricted
rem execution policy, streaming output to this console window.
rem The window is kept open at the end so you can always read the result.
echo.
echo ===============================================================
echo   DaemonCut - one-click setup
echo ===============================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DaemonCut Setup.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo Setup did not finish successfully ^(exit %RC%^).
    echo See the messages above, and the log in %USERPROFILE%\.daemoncut\setup_logs\.
) else (
    echo If the service windows did not open, double-click "DaemonCut Start.lnk" in this folder.
)
echo.
echo -----------------------------------------------------------------
pause
endlocal & exit /b %RC%
