# DaemonCut Setup - one-click, fully automatic installer + launcher.
# Checks every requirement, clones the repo (or syncs it), installs all
# dependencies, configures .env, validates storage, creates a desktop shortcut
# with the app icon, and optionally starts the services.
#
# Usage (double-click DaemonCut Setup.cmd, or run here):
#   powershell -ExecutionPolicy Bypass -File "DaemonCut Setup.ps1"
# Optional switches:
#   -NoLaunch    run everything except starting the services
#   -CheckOnly   only verify requirements/state, change nothing
#   -NoShortcut  skip creating the desktop icon
param(
    [switch]$NoLaunch,
    [switch]$CheckOnly,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$RepoUrl       = "https://github.com/min955379-glitch/DaemonCut.git"
$RepoDirName   = "DaemonCut"
$SetupDir      = $PSScriptRoot
if (-not $SetupDir) { $SetupDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$AppName       = "DaemonCut"
$LauncherName  = "DaemonCut Setup.cmd"
$ShortcutName  = "DaemonCut"
$ShortcutIcon = $null

# Embedded launcher icon (256x256 ICO, base64) - written next to this script.
$IconB64 = "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAIAAADTED8xAAAFLUlEQVR42u3dwY1TQRBF0YoBpEmCeFiRDumQDiGwZUMA7FiM0DDY3V1VXUe6AXy538Fm7G/Hh48v0tjCQyAAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQASvT922c9FgAmrpt5hNFrMoYwek3GEKavyQzC7jVZQpi+JjMI09dkBmH9mmwgTF+TGYT1a7KBMH1NZhDWr8kGwvo12UCYviYzCOvXZANh/ZpsIKxfkw2E9WuygbB+TTYAgACwfi3t05evXQyE9WvJ4t+urIGwfm2d/hIGAKj39J9k0BKAiVh/fQNh/Tq8/lIGAFDC+h8z0AaAlVh/FwNh/WoEYLkBAJS2/gpPAmH96gVgrYFxAPwkxGOHuGn96U8Ccff6rXnVyZYCsPBk7wRgsssDoAcASwUgB4D1A3ASwKrjvgeAjTZdf+6TQFywfgOd+Qyw5OjbAzBNAAAQACMB2CUA+QCsH4AsAM/PoCsAi7zGQO4SABAA3QDY4jUG0scQ/vlXawBP7gEApRmosIfw+kcpBopMAgAlGKgzCQB02kCpSQCgcwwKTgIAnWBQdhIAaMsNxF0m0QyAeTX9UgkAAADgOgBe/6j7e6MACAAA6vXr5w8AABgNoKkBAABYBqAjAwAAWAygFwMAANgCoAsDAADYCKA+AwAA2A6gMgMAADgEoCYDAAA4CqAaAwAASABQhwEAAKQBqMAAAACSAeQyAACAEgCyGAAAQCEA5xkAAEA5ACcZAABAUQBnGAAAQGkAuw0AAEB1AFsZAABADwCbGAAAQCcAyxkAAEA/AAsZAABAVwBLGAAAQG8ATzIAAIAbADzMAAAA7gHwAAMAALgNwH8xAACAOwG8kwEAANwM4J8MAADgfgBvMAAAgCkA/soAAABmAXjFAAAAJgL4wwAAADwDAACA/wMAAIC/AgEAgPcBAADAO8EAAOCzQAAA4NOgAADgfgAAAHBHGAAAuCcYAAB8KwQAAPheIAAA8M1wAADgu0EBAMC3QwMAgN8HAAAAvxADAAB+IwwAAPxKJAAA+J1gAADwS/EAAFBr+gAAkAagzpUDAMBRANWuHAAADgGoeeUAALAdQOUrBwCAjQDqXzkAAGwB0OXKAQBgMYBeVw4AAMsAdLxyAABYA6DplQMAwOgAAAAAAAAAAAAAAAAAAAAAAAAAABgAAAAAAAAAAAAAAAAAAAAAAAAAAACAAesHAAAAAAAAAAAYsH4AAAAAAAasHwAGrB8ABqwfAAAAAIAB6wcAA9MHgIHx6weAgdHrBwCDudMHgIS5uwdAAkAAADDwhZZHHgAABAAAjgAAABwBAAA4AgAAcAQAAOAIAADAEQAAgCMAAABHAAAAjmApAGcAQPfHHwAAAHAGAADgDAAAwBkAAIAzuOimSgAAAMAZAQCAMwIAAGcEAADOaB2A6x9iAO4+IAAAAMBDDAAAHmIBwIBmPT8DIAAAEAAACAAGNOsPdAAIAAY09f0ZAAQAAAKAAQ38fAoAAoABTf14IgACgAFN/XR6+Bi6Jt+bAYAAYEBTb80L9+Np8o2pAAgABjRy/ScAMKCy6z8EgAHVXD8AAsA3NGnq+o8CYEDV1n8aAAMqtf4EAAyozvpzADCgIutPA4CB0qefD4AB5a4/HwADyp1fPgAMTB8ABqx/PAAMTB8ABqx/PAAMTB8ADEwfABLsHgAMTB8AGIweABiMHgA8TBwACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkAASABIAEgzeo30BXMaM+U4owAAAAASUVORK5CYII="

# Log file under the local runtime folder (creates it if missing).
$LogDir = Join-Path $env:USERPROFILE ".daemoncut" "setup_logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir "setup_$(Get-Date -Format yyyyMMdd_HHmmss).log"

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
    Add-Content -Path $LogFile -Value "==> $msg"
}

function Write-Ok([string]$msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
    Add-Content -Path $LogFile -Value "[OK] $msg"
}

function Write-Warn([string]$msg) {
    Write-Host "    [!] $msg" -ForegroundColor Yellow
    Add-Content -Path $LogFile -Value "[WARN] $msg"
}

function Write-Fail([string]$msg) {
    Write-Host "    [X] $msg" -ForegroundColor Red
    Add-Content -Path $LogFile -Value "[FAIL] $msg"
}

function Die([string]$msg) {
    Write-Fail $msg
    Write-Host ""
    Write-Host "Setup stopped. See: $LogFile" -ForegroundColor Red
    exit 1
}

function Run([string]$program, [string]$argLine, [string]$stepName) {
    Write-Step $stepName
    $p = Start-Process -FilePath $program -ArgumentList $argLine -NoNewWindow -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Die "$stepName failed (exit $($p.ExitCode))."
    }
    Write-Ok "$stepName completed."
}

# ---------------------------------------------------------------------------
# 0. Header
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "===============================================================" -ForegroundColor DarkCyan
Write-Host "  DaemonCut - automatic setup & launcher" -ForegroundColor DarkCyan
Write-Host "===============================================================" -ForegroundColor DarkCyan
Add-Content -Path $LogFile -Value "DaemonCut setup started $(Get-Date -Format o)"
Write-Host "  Log file: $LogFile" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 1. Requirement checks
# ---------------------------------------------------------------------------
Write-Step "Checking requirements..."

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Die "PowerShell 5.1 or newer is required. This script needs Windows PowerShell."
}
Write-Ok "PowerShell $($PSVersionTable.PSVersion.ToString())."

function Get-CommandExe([string]$name) {
    return Get-Command $name -ErrorAction SilentlyContinue
}

$py = Get-CommandExe "py"
if (-not $py) { $py = Get-CommandExe "python" }
if (-not $py) {
    Write-Warn "Python launcher not found. It will be required before the backend can run."
} else {
    $pyExe = $py.Source
    try {
        $pyVer = (& $pyExe -3 -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null)
    } catch { $pyVer = "" }
    if ($pyVer -match "^(\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
            Write-Ok "Python $pyVer found ($pyExe)."
        } else {
            Write-Warn "Python $pyVer found, but 3.11+ is required. Install Python 3.11+ from python.org."
        }
    } else {
        Write-Warn "Could not determine the Python version. Verify Python 3.11+ is installed."
    }
}

$git = Get-CommandExe "git"
if (-not $git) {
    Die "Git is required (to clone/sync the repository). Install Git for Windows, then rerun."
}
Write-Ok "Git $(& $git --version)."

$node = Get-CommandExe "node"
if (-not $node) {
    Write-Warn "Node.js not found. The console will be unavailable until Node.js LTS + npm are installed."
} else {
    $nodeVer = (& node --version 2>$null)
    $npm = Get-CommandExe "npm"
    if ($npm) {
        Write-Ok "Node $nodeVer and npm $(& npm --version 2>$null)."
    } else {
        Write-Warn "Node $nodeVer found, but npm is missing. Repair the Node.js installation."
    }
}

$driveCmd = Get-CommandExe "schtasks"
if (-not $driveCmd) { Write-Warn "schtasks.exe not found; optional autostart task will be skipped." }

# Google Drive for desktop presence (informational).
$drivePossible = $false
$gdRoot = Join-Path $env:USERPROFILE "Google Drive"
if (Test-Path $gdRoot) {
    $drivePossible = $true
} else {
    foreach ($letter in (Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name.TrimEnd(':') })) {
        if (Test-Path (Join-Path ($letter + ':\') 'My Drive')) { $drivePossible = $true; break }
    }
}
if ($drivePossible) {
    Write-Ok "A Google Drive for desktop folder was detected."
} else {
    Write-Warn "No Google Drive folder detected. You will be asked for the DaemonCut Development path."
}

if ($CheckOnly) {
    Write-Step "Check-only mode: not changing anything."
    Write-Host "  Requirements checked. Rerun without -CheckOnly to install." -ForegroundColor Cyan
    return
}

# ---------------------------------------------------------------------------
# 2. Clone or sync the GitHub repository
# ---------------------------------------------------------------------------
Write-Step "Preparing the repository..."
$RepoDir = Join-Path $SetupDir $RepoDirName

function Ensure-Repo([string]$path) {
    if (-not (Test-Path (Join-Path $path ".git"))) {
        if (Test-Path $path) {
            Die "A folder named '$RepoDirName' already exists here but is not a Git checkout. Move/remove it, then rerun."
        }
        Write-Step "Cloning $RepoUrl ..."
        git clone $RepoUrl $path
        if ($LASTEXITCODE -ne 0) { Die "git clone failed. Check your connection to GitHub." }
        Write-Ok "Repository cloned to $path."
    } else {
        Write-Step "Repository already present - checking for updates..."
        $origin = (git -C $path remote get-url origin 2>$null)
        if ($origin -notmatch "github\.com/min955379-glitch/DaemonCut") {
            Write-Warn "Origin is '$origin'; leaving it unchanged (sync_github.bat enforces the expected origin)."
        }
        git -C $path fetch --all --prune
        if ($LASTEXITCODE -ne 0) { Write-Warn "git fetch failed (offline?). Continuing with the current checkout." }
        $branch = (git -C $path symbolic-ref --quiet --short HEAD 2>$null)
        if (-not $branch) {
            Write-Warn "Detached HEAD - not pulling. Check out a branch before syncing."
        } else {
            $behind = (git -C $path rev-list --count HEAD..origin/$branch 2>$null)
            if ($behind -and [int]$behind -gt 0) {
                git -C $path pull --ff-only origin $branch
                if ($LASTEXITCODE -ne 0) { Write-Warn "git pull failed; keeping the current checkout." }
                else { Write-Ok "Updated to the latest '$branch' ($behind new commits)." }
            } else {
                Write-Ok "Repository is up to date on '$branch'."
            }
        }
    }
}

Ensure-Repo $RepoDir

# ---------------------------------------------------------------------------
# 3. Python virtual environment + backend dependencies
# ---------------------------------------------------------------------------
Write-Step "Preparing the Python environment..."
$PyExe = if ($py) { $py.Source } else { "python" }
$VenvPy = Join-Path $RepoDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    if (-not $py) { Die "Python was not found. Install Python 3.11+ from python.org and rerun." }
    Write-Step "Creating the virtual environment..."
    & $PyExe -3 -m venv (Join-Path $RepoDir ".venv")
    if ($LASTEXITCODE -ne 0) { Die "Failed to create the virtual environment." }
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Virtual environment already exists."
}

Run -program $VenvPy -argLine "-m pip install --upgrade pip" -stepName "Upgrading pip"
Run -program $VenvPy -argLine "-m pip install -r `"$RepoDir\requirements.txt`"" -stepName "Installing Python dependencies"

# ---------------------------------------------------------------------------
# 4. Frontend dependencies
# ---------------------------------------------------------------------------
if ($node) {
    $npm = (Get-CommandExe "npm").Source
    Write-Step "Installing console dependencies..."
    $npmProc = Start-Process -FilePath $npm -ArgumentList @("install", "--no-audit", "--no-fund") -WorkingDirectory (Join-Path $RepoDir "frontend") -NoNewWindow -Wait -PassThru
    if ($npmProc.ExitCode -ne 0) {
        Write-Warn "npm install failed. Run it manually in frontend\ and rerun."
    } else {
        Write-Ok "Console dependencies installed."
    }
}

# ---------------------------------------------------------------------------
# 5. .env + Drive path
# ---------------------------------------------------------------------------
Write-Step "Configuring .env..."
$EnvFile = Join-Path $RepoDir ".env"
if (-not (Test-Path $EnvFile)) {
    $Example = Join-Path $RepoDir ".env.example"
    if (Test-Path $Example) {
        Copy-Item $Example $EnvFile -Force
        Write-Ok "Created .env from .env.example."
    } else {
        Die ".env.example is missing from the repository."
    }
} else {
    Write-Ok ".env already exists (its settings are preserved)."
}

# Resolve the Drive path: use an existing configured one, auto-detect, or ask.
$drivePath = $null
if (Test-Path $EnvFile) {
    $cur = (Get-Content $EnvFile | Where-Object { $_ -match "^GDRIVE_DAEMONCUT_PATH=" } | Select-Object -First 1)
    if ($cur) { $drivePath = ($cur -split "=", 2)[1].Trim().Trim('"') }
}
if (-not $drivePath) {
    # Try to auto-detect a DaemonCut Development folder.
    $candidates = @(
        (Join-Path $gdRoot "DaemonCut Development"),
        (Join-Path $gdRoot "DaemonCut")
    )
    foreach ($letter in (Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name.TrimEnd(':') })) {
        $candidates += @(
            (Join-Path ($letter + ':\My Drive') 'DaemonCut Development'),
            (Join-Path ($letter + ':\My Drive') 'DaemonCut')
        )
    }
    $found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($found) { $drivePath = $found }
}

if (-not $drivePath) {
    Write-Host ""
    Write-Host "  Enter the full path to your Google Drive 'DaemonCut Development' folder." -ForegroundColor Yellow
    Write-Host "  Example: G:\My Drive\DaemonCut Development" -ForegroundColor DarkGray
    $input = Read-Host "DaemonCut Development folder"
    if ($input.Trim()) { $drivePath = $input.Trim() }
}
if (-not $drivePath) { Die "No DaemonCut Development path was provided." }

# Update only GDRIVE_DAEMONCUT_PATH in .env, preserving everything else.
$env:DAEMONCUT_GDRIVE_PATH = $drivePath
$setScript = @"
from pathlib import Path
import os
p = Path(r'$EnvFile')
key = 'GDRIVE_DAEMONCUT_PATH='
lines = p.read_text(encoding='utf-8-sig').splitlines() if p.exists() else []
lines = [l for l in lines if not l.lstrip().startswith(key)]
lines.append(key + str(Path(os.environ['DAEMONCUT_GDRIVE_PATH']).expanduser().resolve()))
p.write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
"@
$tmpPy = Join-Path $env:TEMP "dc_env_set.py"
Set-Content -Path $tmpPy -Value $setScript -Encoding UTF8
Run -program $VenvPy -argLine "`"$tmpPy`"" -stepName "Saving GDRIVE_DAEMONCUT_PATH"
Remove-Item $tmpPy -ErrorAction SilentlyContinue
Write-Ok "Drive path set to: $drivePath"

# ---------------------------------------------------------------------------
# 6. Validate storage + database
# ---------------------------------------------------------------------------
Write-Step "Validating storage and runtime..."
Run -program $VenvPy -argLine "-m backend.config --check-runtime --check-storage" -stepName "Storage & runtime check"
Run -program $VenvPy -argLine "-m backend.database --check" -stepName "SQLite database check"

# ---------------------------------------------------------------------------
# 7. Icon + desktop shortcut
# ---------------------------------------------------------------------------
if (-not $NoShortcut) {
    Write-Step "Creating the desktop icon..."
    $iconFile = Join-Path $SetupDir "DaemonCut.ico"
    try {
        $bytes = [Convert]::FromBase64String($IconB64)
        [IO.File]::WriteAllBytes($iconFile, $bytes)
        Write-Ok "Icon written to $iconFile"
        $ShortcutIcon = $iconFile
    } catch {
        Write-Warn "Could not write the icon file."
    }

    $lnkName = "DaemonCut.lnk"
    $lnkDir = Join-Path $SetupDir "DaemonCut Start.lnk"
    $lnkTarget = Join-Path $SetupDir $LauncherName
    if (Test-Path $lnkTarget) {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($lnkDir)
        $lnk.TargetPath = $lnkTarget
        $lnk.WorkingDirectory = $SetupDir
        $lnk.Description = "DaemonCut - one-click setup & launch"
        if ($ShortcutIcon -and (Test-Path $ShortcutIcon)) { $lnk.IconLocation = $ShortcutIcon }
        $lnk.Save()
        Write-Ok "Shortcut created: $lnkDir"
    } else {
        Write-Warn "Launcher '$LauncherName' not found next to this script; shortcut skipped."
    }
}

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "===============================================================" -ForegroundColor DarkCyan
Write-Host "  SETUP COMPLETE - everything is ready." -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor DarkCyan
Write-Host "  Repository : $RepoDir"
Write-Host "  Drive path : $drivePath"
Write-Host "  Icon       : $(if ($ShortcutIcon) { $ShortcutIcon } else { '(skipped)' })"
Write-Host "  Log        : $LogFile"
Write-Host ""

if ($NoLaunch) {
    Write-Host "  Started with -NoLaunch: services were not started." -ForegroundColor Yellow
    Write-Host "  Double-click 'DaemonCut Start.lnk' (or run $LauncherName) to launch." -ForegroundColor Cyan
    exit 0
}

# ---------------------------------------------------------------------------
# 9. Launch the services (backend + console)
# ---------------------------------------------------------------------------
Write-Step "Starting DaemonCut services..."
$backendProc = Start-Process -FilePath $VenvPy -ArgumentList @("-m", "backend.main") -WorkingDirectory $RepoDir -PassThru
Start-Sleep -Seconds 4
if ($node) {
    $npm = (Get-CommandExe "npm").Source
    $npmProc = Start-Process -FilePath $npm -ArgumentList @("run", "dev") -WorkingDirectory (Join-Path $RepoDir "frontend") -PassThru
    Write-Ok "Backend (PID $($backendProc.Id)) and console (PID $($npmProc.Id)) are starting."
} else {
    $frontProc = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "start", "DaemonCut Console", "npm", "run", "dev") -WorkingDirectory (Join-Path $RepoDir "frontend") -PassThru
    Write-Ok "Backend (PID $($backendProc.Id)) is starting. Install Node.js LTS for the console."
}

Write-Host ""
Write-Host "  Console : http://127.0.0.1:3000" -ForegroundColor Cyan
Write-Host "  API docs: http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host "  Phone   : http://127.0.0.1:8000/bridge" -ForegroundColor Cyan
Write-Host ""
Write-Host "  EVERYTHING IS DONE - DaemonCut is up and running." -ForegroundColor Green
Write-Host "  Close the API/Console windows to stop the services." -ForegroundColor DarkGray
exit 0
