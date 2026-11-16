@echo off
setlocal EnableExtensions DisableDelayedExpansion
title DaemonCut - Creator Console
cd /d "%~dp0"
set "ROOT=%CD%"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "LOG_DIR=%ROOT%\.daemoncut\logs"
set "LOG_FILE=%LOG_DIR%\launcher.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
if not exist "%PYTHON%" (
    echo DaemonCut is not installed. Run setup.bat first.
    >>"%LOG_FILE%" echo [%date% %time%] ERROR: virtual environment missing; run setup.bat.
    exit /b 2
)

if /I "%~1"=="--boot" goto :boot_mode

echo DaemonCut interactive startup: Next.js console + FastAPI backend.
echo [%date% %time%] Interactive Phase 4 startup requested.>>"%LOG_FILE%"
where node >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js is required for the interactive console. Install Node.js LTS, rerun setup.bat, then try again.
    exit /b 2
)
where npm >nul 2>&1
if errorlevel 1 (
    echo ERROR: npm was not found. Repair the Node.js LTS installation and retry.
    exit /b 2
)
if not exist "%ROOT%\frontend\node_modules" (
    echo ERROR: Frontend packages are missing. Run setup.bat to install them.
    exit /b 2
)

"%PYTHON%" -m backend.config --check-runtime --check-storage >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :storage_error
"%PYTHON%" -m backend.maintenance --execute >>"%LOG_FILE%" 2>&1
if errorlevel 1 echo [%date% %time%] WARNING: cache cleanup reported an error; continuing startup.>>"%LOG_FILE%"
"%PYTHON%" -m backend.database --check >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :database_error

if not exist "%ROOT%\backend\main.py" (
    echo ERROR: backend\main.py is missing. See .daemoncut\logs\launcher.log.
    exit /b 5
)

start "DaemonCut API - 127.0.0.1:8000" /D "%ROOT%" cmd /k ""%PYTHON%" -m backend.main"
timeout /t 2 /nobreak >nul
start "DaemonCut Console - 127.0.0.1:3000" /D "%ROOT%\frontend" cmd /k "npm run dev -- --hostname 0.0.0.0"
echo.
echo DaemonCut is starting in two windows.
echo Creator console: http://127.0.0.1:3000
echo FastAPI docs:    http://127.0.0.1:8000/docs
echo Phone bridge:    http://127.0.0.1:8000/bridge
echo Close the API and Console command windows to stop the local services.
exit /b 0

:boot_mode
set "SCHEDULER_BOOT_AUTORUN=true"
echo [%date% %time%] Headless PKT boot run enabled; no browser console will be started.>>"%LOG_FILE%"
"%PYTHON%" -m backend.config --check-runtime --check-storage >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :storage_error
"%PYTHON%" -m backend.maintenance --execute >>"%LOG_FILE%" 2>&1
if errorlevel 1 echo [%date% %time%] WARNING: cache cleanup reported an error; continuing boot run.>>"%LOG_FILE%"
"%PYTHON%" -m backend.database --check >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :database_error

echo [%date% %time%] Starting backend.main in headless boot mode.>>"%LOG_FILE%"
"%PYTHON%" -m backend.main >>"%LOG_FILE%" 2>&1
set "APP_EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Headless backend.main exited with code %APP_EXIT_CODE%.>>"%LOG_FILE%"
exit /b %APP_EXIT_CODE%

:storage_error
echo [%date% %time%] ERROR: Drive storage configuration failed; no app was started.>>"%LOG_FILE%"
echo Storage validation failed. Configure GDRIVE_DAEMONCUT_PATH in .env and rerun setup.bat.
exit /b 3

:database_error
echo [%date% %time%] ERROR: SQLite initialization failed.>>"%LOG_FILE%"
echo Database initialization failed. See .daemoncut\logs\launcher.log.
exit /b 4
