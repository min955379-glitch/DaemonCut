@echo off
setlocal EnableExtensions DisableDelayedExpansion
title DaemonCut - user logon task
cd /d "%~dp0"
set "TASK_NAME=DaemonCut-User-Logon"
set "LAUNCHER=%~dp0start_daemoncut.bat"

if /I "%~1"=="--uninstall" goto :uninstall
if not exist "%LAUNCHER%" (
    echo ERROR: start_daemoncut.bat was not found beside this installer.
    exit /b 1
)
where schtasks >nul 2>&1
if errorlevel 1 (
    echo ERROR: Windows Task Scheduler command schtasks.exe is unavailable.
    exit /b 1
)

echo Registering %TASK_NAME% for the current Windows user.
echo It runs after interactive logon so Google Drive for desktop has a user session.
echo No administrator account or SYSTEM task is used.

rem A short logon delay gives Drive for desktop time to mount its local folder.
schtasks /Create /TN "%TASK_NAME%" /SC ONLOGON /TR "\"%LAUNCHER%\" --boot" /RL LIMITED /IT /DELAY 0000:45 /F
if errorlevel 1 (
    echo ERROR: Task Scheduler could not register the logon task.
    echo Open Task Scheduler and register start_daemoncut.bat for your own user, or rerun this script.
    exit /b 1
)
echo.
echo Task installed. It runs at each interactive user logon; Windows uses the local machine timezone.
echo For a PKT daily schedule, set Windows to Pakistan Standard Time (UTC+05:00).
echo To remove it, run: install_autostart.bat --uninstall
exit /b 0

:uninstall
schtasks /Delete /TN "%TASK_NAME%" /F
if errorlevel 1 (
    echo The task may not have existed or Windows could not remove it.
    exit /b 1
)
echo DaemonCut logon task removed.
exit /b 0
