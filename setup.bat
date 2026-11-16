@echo off
setlocal EnableExtensions DisableDelayedExpansion
title DaemonCut - Phase 4 setup
cd /d "%~dp0"
set "ROOT=%CD%"

 echo.
echo ================================================================
echo  DaemonCut Phase 4 - Creator Console + local API + Drive media storage
echo ================================================================
echo.
echo This installer needs Python 3.11 or newer, Git, and a locally
echo accessible Google Drive for desktop folder. It does not upload code,
echo install Ollama, or create a GitHub commit.
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo ERROR: The Python Launcher (py.exe) was not found. Install Python 3.11+ from python.org and retry.
    goto :failed
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 or newer was not found. Install Python 3.11+ and retry.
    goto :failed
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/8] Creating the project virtual environment...
    py -3 -m venv ".venv"
    if errorlevel 1 goto :failed
) else (
    echo [1/8] Reusing the project virtual environment.
)
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

echo [2/8] Installing Python dependencies...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%PYTHON%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :failed

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    if errorlevel 1 goto :failed
)

set "DEFAULT_DRIVE=%USERPROFILE%\Google Drive\My Drive\DaemonCut Development"
set "DRIVE_FOUND="
if exist "%USERPROFILE%\Google Drive\My Drive\" (
    set "DEFAULT_DRIVE=%USERPROFILE%\Google Drive\My Drive\DaemonCut Development"
    set "DRIVE_FOUND=1"
)
if not defined DRIVE_FOUND (
    for %%D in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
        if not defined DRIVE_FOUND if exist "%%D:\My Drive\" (
            set "DEFAULT_DRIVE=%%D:\My Drive\DaemonCut Development"
            set "DRIVE_FOUND=1"
        )
    )
)

echo.
echo Enter the local path to your Google Drive for desktop's My Drive folder.
echo The selected DaemonCut Development folder must sync to your Google account.
echo Examples: G:\My Drive\DaemonCut Development or %USERPROFILE%\Google Drive\My Drive\DaemonCut Development
echo.
set "GDRIVE_PATH="
set /p "GDRIVE_PATH=DaemonCut Development folder [%DEFAULT_DRIVE%]: "
if not defined GDRIVE_PATH set "GDRIVE_PATH=%DEFAULT_DRIVE%"
set "DAEMONCUT_GDRIVE_PATH=%GDRIVE_PATH%"

rem Update only the Drive path while preserving all other local .env settings.
"%PYTHON%" -c "from pathlib import Path; import os; p=Path('.env'); lines=p.read_text(encoding='utf-8-sig').splitlines() if p.exists() else []; key='GDRIVE_DAEMONCUT_PATH='; lines=[line for line in lines if not line.lstrip().startswith(key)]; lines.append(key+str(Path(os.environ['DAEMONCUT_GDRIVE_PATH']).expanduser().resolve())); p.write_text('\n'.join(lines)+'\n', encoding='utf-8')"
if errorlevel 1 goto :failed

echo [3/8] Creating the Google Drive folder structure and validating the path...
"%PYTHON%" -m backend.config --check-storage
if errorlevel 1 (
    echo ERROR: Storage validation failed. Check that the selected path exists beneath Google Drive for desktop and is named DaemonCut or DaemonCut Development.
    goto :failed
)
"%PYTHON%" -m backend.config --check-runtime
if errorlevel 1 goto :failed

 echo [4/8] Initializing the SQLite metadata schema...
"%PYTHON%" -m backend.database --check
if errorlevel 1 goto :failed

echo [5/8] Checking Git source-control setup...
where git >nul 2>&1
if errorlevel 1 (
    echo WARNING: Git was not found. Install Git for Windows to use GitHub source control.
) else (
    git rev-parse --is-inside-work-tree >nul 2>&1
    if errorlevel 1 (
        git init
        if errorlevel 1 goto :failed
        echo A local Git repository has been initialized. Add your GitHub remote explicitly before pushing.
    ) else (
        echo Existing Git repository detected; its branch and remote were left unchanged.
    )
)

echo [6/8] Installing the Next.js console when Node.js is available...
where node >nul 2>&1
if errorlevel 1 (
    echo WARNING: Node.js was not found. The FastAPI backend is installed; install Node.js LTS and rerun setup.bat to install the browser console.
) else (
    where npm >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Node.js is installed but npm is unavailable. Repair the Node.js installation and retry.
        goto :failed
    )
    pushd "%ROOT%\frontend"
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        popd
        echo ERROR: Frontend dependency installation failed.
        goto :failed
    )
    popd
)

echo [7/8] Configuring the optional Ollama model store...
if exist "%GDRIVE_PATH%\models\ollama" (
    choice /C YN /N /M "Set per-user OLLAMA_MODELS to Drive and OLLAMA_NUM_PARALLEL=1? [Y/N] "
    if errorlevel 2 goto :skip_ollama_env
    set "OLLAMA_CONFIG_FAILED="
    set "OLLAMA_MODELS=%GDRIVE_PATH%\models\ollama"
    setx OLLAMA_MODELS "%GDRIVE_PATH%\models\ollama" >nul
    if errorlevel 1 (
        set "OLLAMA_CONFIG_FAILED=1"
        echo WARNING: Could not set OLLAMA_MODELS. Configure it manually before pulling local models.
    )
    setx OLLAMA_NUM_PARALLEL "1" >nul
    if errorlevel 1 (
        set "OLLAMA_CONFIG_FAILED=1"
        echo WARNING: Could not set OLLAMA_NUM_PARALLEL. Configure it manually for single-request inference.
    )
    if defined OLLAMA_CONFIG_FAILED (
        echo Review the warnings above. Restart Ollama after correcting its environment.
    ) else (
        echo Ollama settings are set for future processes. Restart Ollama before downloading weights.
    )
) else (
    echo WARNING: Expected model folder was not created. Configure Ollama's model store manually.
)
:skip_ollama_env

echo [8/8] Setup complete.
echo.
echo Heavy-file root: %GDRIVE_PATH%
echo Local database/cache: %ROOT%\.daemoncut
echo Source control: code and lightweight configuration only; no commit or push was made.
echo The Google Drive desktop client performs file synchronization; DaemonCut does not copy code or issue a separate cloud upload.
echo.
choice /C YN /N /M "Install the per-user logon task now? [Y/N] "
if errorlevel 2 goto :autostart_done
call "%ROOT%\install_autostart.bat"
if errorlevel 1 echo WARNING: Setup completed, but the logon task was not installed. Run install_autostart.bat manually.
:autostart_done

echo.
echo DaemonCut Phase 4 is ready. Run start_daemoncut.bat for the Next.js console and FastAPI service.
echo Console: http://127.0.0.1:3000  ^|  API docs: http://127.0.0.1:8000/docs  ^|  Phone bridge: http://127.0.0.1:8000/bridge
echo For a background boot run, install_autostart.bat starts with the --boot watchdog flag.
echo Read README.md for the Drive layout, local AI setup, API routes, and sync_github.bat.
exit /b 0

:failed
echo.
echo Setup failed. Review the message above, fix the reported issue, and run setup.bat again.
exit /b 1
