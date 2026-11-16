@echo off
setlocal EnableExtensions DisableDelayedExpansion
title DaemonCut - safe GitHub sync
cd /d "%~dp0"
set "ROOT=%CD%"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "EXPECTED_REMOTE=https://github.com/min955379-glitch/DaemonCut.git"

if not exist "%ROOT%\.git" (
    echo ERROR: This folder is not a Git checkout. Run setup.bat in the cloned DaemonCut repository.
    exit /b 2
)
if not exist "%PYTHON%" (
    echo ERROR: The project virtual environment was not found. Run setup.bat before syncing.
    exit /b 2
)

for /f "usebackq delims=" %%B in (`git symbolic-ref --quiet --short HEAD 2^>nul`) do set "BRANCH=%%B"
if not defined BRANCH (
    echo ERROR: Git is in detached-HEAD mode. This script will not switch branches.
    exit /b 3
)
set "ORIGIN_URL="
for /f "usebackq delims=" %%U in (`git remote get-url origin 2^>nul`) do set "ORIGIN_URL=%%U"
if not defined ORIGIN_URL (
    echo Origin is not configured; adding the specified DaemonCut repository.
    git remote add origin "%EXPECTED_REMOTE%"
    if errorlevel 1 (
        echo ERROR: Could not configure origin as %EXPECTED_REMOTE%.
        exit /b 3
    )
    set "ORIGIN_URL=%EXPECTED_REMOTE%"
)
if /I not "%ORIGIN_URL%"=="%EXPECTED_REMOTE%" if /I not "%ORIGIN_URL%"=="https://github.com/min955379-glitch/DaemonCut" (
    echo ERROR: origin points to "%ORIGIN_URL%", not the required %EXPECTED_REMOTE%.
    echo Update origin deliberately, then rerun this script. No branch was changed.
    exit /b 3
)

echo Verifying secret, database, and media ignore rules...
"%PYTHON%" -c "import subprocess,sys; paths=['.env','.daemoncut/daemoncut.sqlite3','renders_archive/daemoncut-safety-check.mp4','source_media/phone_uploads/daemoncut-safety-check.mp4','source_media/phone_uploads/daemoncut-safety-check.png','frontend/node_modules/daemoncut-safety-check/package.json','frontend/.next/daemoncut-safety-check.bin']; missing=[p for p in paths if subprocess.run(['git','check-ignore','--no-index','--quiet','--',p]).returncode!=0]; print('Ignored:', ', '.join(paths) if not missing else ''); print('ERROR: .gitignore does not exclude: '+', '.join(missing) if missing else ''); sys.exit(bool(missing))"
if errorlevel 1 (
    echo ERROR: Resolve the .gitignore check before staging or pushing.
    exit /b 4
)

echo Staging source and lightweight configuration on branch %BRANCH%...
git add --all
if errorlevel 1 (
    echo ERROR: Git could not stage the workspace changes.
    exit /b 5
)

"%PYTHON%" -c "import pathlib,subprocess,sys; raw=subprocess.run(['git','ls-files','-z','--cached'],check=True,stdout=subprocess.PIPE).stdout; names=[p.decode('utf-8','surrogateescape').replace('\\','/').casefold() for p in raw.split(b'\\0') if p]; media={'.mp4','.m4v','.mov','.mkv','.avi','.webm','.wmv','.flv','.mxf','.ts','.3gp','.ogv','.jpg','.jpeg','.png','.gif','.webp','.heic','.avif','.tif','.tiff','.wav','.flac','.mp3','.m4a','.aac','.ogg','.opus','.aiff','.gguf','.onnx','.safetensors','.sqlite','.sqlite3','.db','.pem','.key'}; bad=[p for p in names if p=='.env' or (p.startswith('.env.') and p!='.env.example') or pathlib.PurePosixPath(p).suffix in media or p.startswith(('.daemoncut/','frontend/node_modules/','frontend/.next/','source_media/','renders_archive/','voice_vault/','models/'))]; print('ERROR: tracked secret/media paths: '+', '.join(bad) if bad else 'Tracked-file safety check passed.'); sys.exit(bool(bad))"
if errorlevel 1 (
    echo ERROR: A secret, database, or heavy media file is tracked. Unstage/remove it and retry.
    exit /b 6
)

git diff --cached --quiet
if errorlevel 2 goto :diff_error
if errorlevel 1 goto :commit_changes
echo No staged code changes to commit.
goto :push_current_branch

:commit_changes
set "COMMIT_MESSAGE=DaemonCut Phase 4 console and publishing"
set /p "COMMIT_MESSAGE=Commit message [DaemonCut Phase 4 console and publishing]: "
if not defined COMMIT_MESSAGE set "COMMIT_MESSAGE=DaemonCut Phase 4 console and publishing"
git commit -m "%COMMIT_MESSAGE%"
if errorlevel 1 (
    echo ERROR: Git could not create the commit. Check your local Git identity and staged changes.
    exit /b 7
)

:push_current_branch
echo Pushing only branch %BRANCH% to %EXPECTED_REMOTE%...
git push origin "%BRANCH%"
if errorlevel 1 (
    echo ERROR: Push failed. The local commit is preserved on branch %BRANCH%.
    echo Reconnect GitHub in Arena or authenticate GitHub Desktop, then run sync_github.bat again.
    exit /b 8
)
echo Safe sync complete for branch %BRANCH%.
exit /b 0

:diff_error
echo ERROR: Git could not inspect the staged diff.
exit /b 7
