# DaemonCut — Step-by-Step Setup and Run Guide

This guide walks you through everything needed to install DaemonCut, configure it, run it,
and verify that it works. It covers prerequisites with exact install commands, every setup
step in detail, and the validation commands to confirm a healthy build.

DaemonCut is a **local-first production console** for original short-form stories:

- **Backend:** FastAPI service (default `http://0.0.0.0:8000`) — workspaces, generation,
  clipper, scheduler, publishing, QR phone bridge, and the WebSocket log hub.
- **Console:** Next.js/TypeScript app (default `http://0.0.0.0:3000`) — the three-zone
  creator console. It proxies `/api/*`, `/bridge*`, and `/ws/*` to the backend, so the
  browser only ever talks to the console origin.
- **Storage:** source code and small config live in Git; **media, renders, model weights,
  and phone uploads live in a Google Drive folder** (`GDRIVE_DAEMONCUT_PATH`). Metadata,
  logs, and the OAuth token vault live in the ignored local `.daemoncut/` folder.

> Requirements below are split into **Required** (needed for the core studio) and
> **Optional** (needed only for specific heavy features). The core app starts and works
> without the optional stack.

---

## 1. Prerequisites

### 1.1 Required

| Tool | Minimum version | Why |
| --- | --- | --- |
| **Python** | 3.11+ | Runs the FastAPI backend and setup scripts |
| **Node.js (with npm)** | Node.js LTS (20.x or 22.x recommended) | Runs the Next.js console |
| **Git** | any recent | Source control and repo clone |
| **Google Drive for desktop** | current | Provides the synced `DaemonCut Development` folder used for media storage |

#### Windows install commands

```powershell
# Python 3.11+ (use the official installer, check "Add python.exe to PATH"):
winget install Python.Python.3.12
# or download from https://www.python.org/downloads/ and run the .exe installer

# Node.js LTS (includes npm):
winget install OpenJS.NodeJS.LTS
# verify
node --version
npm --version

# Git for Windows:
winget install Git.Git
git --version

# Google Drive for desktop:
winget install Google.Drive
```

#### Linux install commands

```bash
# Python 3.11+, Node.js LTS, Git
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
# Node.js 22 LTS via NodeSource
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version && npm --version && python3 --version
```

#### macOS install commands

```bash
# Xcode Command Line Tools (provides git + compilers)
xcode-select --install

# Python 3.11+
brew install python@3.12

# Node.js LTS
brew install node

# Google Drive for desktop (desktop client for macOS)
# Install from https://www.google.com/drive/download/
```

### 1.2 Optional (feature-specific)

Only install what you intend to use. None of these are installed by `setup.bat`, and none
are required for the console or API to start.

| Feature | What to install | Notes |
| --- | --- | --- |
| **Video rendering** | FFmpeg + ffprobe binaries | Set `FFMPEG_BINARY`/`FFPROBE_BINARY` in `.env` if not on PATH |
| **Local LLM fallback** | Ollama + models | Keep model store under `<GDRIVE_DAEMONCUT_PATH>/models/ollama` |
| **Cloud generation** | Gemini API key | Set `GEMINI_API_KEY` in `.env` or the Key Vault UI |
| **Subtitles / transcription** | faster-whisper or whisper.cpp (`whisper-cli`) | Set `WHISPER_CPP_BINARY` if used |
| **Voice modes** | Edge TTS (built-in), Kokoro, Pocket TTS, Chatterbox | Voice-cloning endpoints must bind to localhost |
| **Browser cloning/inspection** | Playwright (Python package + browser binary) | `pip install playwright && playwright install` |
| **Thumbnail acceleration** | opencv-python-headless | Optional sharpness acceleration |
| **Publishing** | YouTube/TikTok/Meta OAuth apps | Configure only the providers you use (see §7) |

---

## 2. Get the code

```bash
git clone https://github.com/min955379-glitch/DaemonCut.git
cd DaemonCut
```

You can also open the repo directly in your IDE and work from there. All commands below
assume you are at the **repository root**.

---

## 3. Install the backend and console

### 3.1 One-shot Windows setup

On Windows, run:

```powershell
setup.bat
```

`setup.bat` will:

1. Create (or reuse) the `.venv` virtual environment.
2. Install everything in `requirements.txt`.
3. Ask for the Google Drive path and record it for `.env`.
4. Check storage and the SQLite schema.
5. Run `npm install` in `frontend/` when Node.js is present.

If Node.js is not installed, the backend still installs; install Node.js LTS and rerun
`setup.bat` to get the console.

### 3.2 Manual / cross-platform install

```bash
# 1. Create and activate a Python virtual environment
python3 -m venv .venv

# 2. Install backend dependencies
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# Windows: .venv\Scripts\python -m pip install -r requirements.txt

# 3. Install console dependencies
cd frontend
npm install
cd ..
```

Verify the key packages installed:

```bash
.venv/bin/pip list | grep -Ei "fastapi|uvicorn|google-genai|pillow|edge-tts"
# expect: fastapi, uvicorn, google-genai, pillow, edge-tts
```

---

## 4. Configure `.env`

Copy the example environment file and edit it:

```bash
cp .env.example .env
# Windows: copy .env.example .env
```

### 4.1 The one required setting

`GDRIVE_DAEMONCUT_PATH` must point to a locally mounted, synced Google Drive folder whose
**name is `DaemonCut Development` or `DaemonCut`**. The app resolves all media, models,
renders, voice vault, and phone uploads under this root.

```bash
# Example (Linux/macOS)
GDRIVE_DAEMONCUT_PATH=/home/me/Google Drive/DaemonCut Development

# Example (Windows)
GDRIVE_DAEMONCUT_PATH=C:\Users\YourName\Google Drive\DaemonCut Development
```

The parent folder must exist. If you point at a different name, the storage check will
fail and no app will start.

### 4.2 Optional settings worth knowing

| Key | Purpose | Default |
| --- | --- | --- |
| `AI_PROVIDER` | `auto` uses Gemini when a key exists, otherwise Ollama | `auto` |
| `GEMINI_API_KEY` | Cloud generation key; leave blank for local Ollama fallback | blank |
| `GEMINI_MODEL_ID` | Default Gemini model | `gemini-4-hydrogen` |
| `OLLAMA_ENABLED` / `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local/offline LLM fallback | `true` / `http://127.0.0.1:11434` / `qwen2.5:7b-instruct-q4_K_M` |
| `API_HOST` / `API_PORT` | Bind address of the API | `0.0.0.0` / `8000` |
| `OAUTH_REDIRECT_URI` | OAuth callback; default is local | `http://127.0.0.1:8000/oauth/callback` |
| `SCHEDULER_AUTOSTART` | Starts the batch scheduler with the API | `true` |
| `SCHEDULER_BOOT_AUTORUN` | Headless boot-cycle mode (used by `--boot`) | `false` |
| `META_API_ENABLED` | Enable Meta publishing; `false` produces ready-to-post Drive bundles | `true` |
| `FFMPEG_BINARY` / `FFPROBE_BINARY` | FFmpeg executables | `ffmpeg` / `ffprobe` |

> `.env` is git-ignored. Never commit API keys, OAuth secrets, tokens, model weights, or
> media. Enter secrets either in `.env` or in the console's **API Key Vault** (which never
> returns raw secrets from GET responses).

---

## 5. Validate the environment

Run these three checks before starting the app. They are the same checks `start_daemoncut.bat` runs internally.

```bash
# 1. Runtime + Drive storage checks (creates folders, prints the resolved paths)
.venv/bin/python -m backend.config --check-runtime --check-storage
# Windows: .venv\Scripts\python -m backend.config --check-runtime --check-storage

# 2. SQLite database check (creates .daemoncut/ and the schema)
.venv/bin/python -m backend.database --check

# 3. Optional: cache cleanup
.venv/bin/python -m backend.maintenance --execute
```

A healthy run prints the full folder tree (models, assets, source_media, renders_archive,
`runtime: .../.daemoncut`) and the database stats (workspaces, channels, batches,
episodes). If step 1 fails, `GDRIVE_DAEMONCUT_PATH` is misconfigured.

---

## 6. Start the app

### 6.1 Windows (interactive console)

```powershell
start_daemoncut.bat
```

This opens two windows:

- **Console** — `http://127.0.0.1:3000`
- **FastAPI docs** — `http://127.0.0.1:8000/docs`
- **QR phone bridge** — `http://127.0.0.1:8000/bridge`

Close both command windows to stop the services.

### 6.2 Headless boot mode (Windows)

For a logon task that runs the scheduler and PKT upload cycle without a browser:

```powershell
start_daemoncut.bat --boot
```

### 6.3 Manual / cross-platform startup

Start the backend first, then the console:

```bash
# Terminal 1 — FastAPI backend (binds 0.0.0.0:8000)
.venv/bin/python -m backend.main

# Terminal 2 — Next.js console (binds 0.0.0.0:3000)
cd frontend
npm run dev -- --hostname 0.0.0.0
```

The Next.js dev server proxies API, bridge, and WebSocket traffic to the backend, so open
the console at `http://127.0.0.1:3000` and use it directly.

> The API binds to `0.0.0.0` so phones on the same Wi-Fi can upload via the QR bridge.
> Do **not** expose the local studio API to the public internet.

---

## 7. Configure publishing (optional)

Configure only the providers you intend to use. Missing credentials never block the
others.

### YouTube
1. Create OAuth credentials in Google Cloud Console.
2. Set `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET` (in `.env` or the Key Vault).
3. Authorize from the console's **API Key Vault** (`/api/oauth/youtube/authorize`).
4. Publishing uses the official YouTube Data API for upload, optional thumbnail/SRT
   captions, and a first comment.

### TikTok
1. Create an app in the TikTok Developer Portal.
2. Set `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
3. Complete OAuth with PKCE from the Key Vault.
4. Posting uses the official Content Posting API.

### Facebook / Instagram (Meta)
1. Create a Meta app and set `META_APP_ID`, `META_APP_SECRET`, account IDs/tokens.
2. Set `META_API_ENABLED=true` only when ready.
3. If Meta is disabled or not connected, the publish selection falls back to a **ready-to-post
   Drive bundle** under `renders_archive/ready_to_post/<episode_id>/` containing MP4, cover
   JPG, SRT, and formatted post copy. This fallback is independent of YouTube/TikTok.

Register the matching redirect URI with each provider:
`http://127.0.0.1:8000/oauth/callback` (change `OAUTH_REDIRECT_URI` if you use another).

---

## 8. Verify the build (validation suite)

Run these to confirm the repo is complete and healthy:

```bash
# Backend unit tests (133 tests) — mocks external OAuth/providers; no real posts
.venv/bin/python -m unittest discover -s tests -v

# Frontend typecheck
cd frontend && npm run typecheck

# Frontend production build
npm run build
```

Expected results:

- Tests: `Ran 133 tests ... OK`
- Typecheck: no errors
- Build: `✓ Compiled` / static prerender of `/`

### Smoke-test the running services

With the backend and console running:

```bash
curl -s -o /dev/null -w "console page: %{http_code}\n" http://127.0.0.1:3000
curl -s -o /dev/null -w "proxied api:  %{http_code}\n" http://127.0.0.1:3000/api/workspaces
curl -s -o /dev/null -w "api docs:     %{http_code}\n" http://127.0.0.1:8000/docs
curl -s -o /dev/null -w "phone bridge: %{http_code}\n" http://127.0.0.1:8000/bridge
```

All four should return `200`.

---

## 9. What was fixed in this build (known-missing files restored)

The console originally could not build because two frontend files were missing from Git.
They have been restored, committed, and pushed on branch `arena/9e1a5bba-daemoncut`:

1. **`frontend/src/lib/api.ts`** — the API client module (10 components import it).
   Exports `apiGet`, `apiPost`, `apiPut`, `platformStatusList`, `logsSocketUrl`,
   `safeStatusColor`, `MAX_YT_CREATE_CHARS = 880`, and the shared types
   (`Workspace`, `Episode`, `Channel`, `PlatformStatus`, `PlatformStatusPayload`,
   `PromptScene`, `ScriptDraft`, `LogEvent`). Without it the console fails with
   `Module not found: Can't resolve '@/lib/api'`.
2. **`frontend/tailwind.config.ts`** — the locked design system config (palette, IBM Plex
   fonts, `xs: 380px` safe-zone screen). Required by the frontend contract test and by
   Tailwind's content scan.
3. **`.gitignore` fix** — the media rule `*.ts` (intended for MPEG transport-stream files)
   also matched TypeScript sources. Scoped with `!frontend/**/*.ts` so frontend `.ts`
   files are tracked.
4. **`frontend/next-env.d.ts`** — Next.js-generated type declaration, committed per
   convention.

### Recheck: nothing else is missing

A full import scan plus the validation suite confirms the current Git build is complete:

- All frontend imports (`@/...`, relative) resolve to committed files — no missing modules.
- All backend internal imports resolve (proved by 133 passing tests and the live API).
- `backend/data/series_bible.json` and all other data files are present.
- `tsc --noEmit`, `next build`, and the 133-test suite all pass on the committed state.

---

## 10. Common issues and fixes

| Symptom | Fix |
| --- | --- |
| `DaemonCut is not installed. Run setup.bat first.` | Create the venv: `python3 -m venv .venv` and `pip install -r requirements.txt` |
| Storage check fails on startup | `GDRIVE_DAEMONCUT_PATH` must point to an existing folder named exactly `DaemonCut` or `DaemonCut Development`; its parent must exist |
| Console shows 500 / `Can't resolve '@/lib/api'` | Confirm `frontend/src/lib/api.ts` exists (added in this build); delete `frontend/.next` and restart `npm run dev` |
| Console styles missing / Tailwind warning | Confirm `frontend/tailwind.config.ts` exists and lists `./src/**/*.{ts,tsx,css}` in `content` |
| `Cannot find module './xxx.js'` / stale dev errors | Delete `frontend/.next` and restart the dev server |
| API unreachable from console | Backend must be running first (`.venv/bin/python -m backend.main`); the console proxies to `http://127.0.0.1:8000` by default |
| Node.js not found by `start_daemoncut.bat` | Install Node.js LTS, rerun `setup.bat`, then start again |
| Rendering fails | Install FFmpeg/ffprobe (and optionally the model runtimes from §1.2) and verify binaries are on PATH or set in `.env` |

---

## 11. Quick reference — all commands

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
(cd frontend && npm install)
cp .env.example .env            # then set GDRIVE_DAEMONCUT_PATH

# Checks
.venv/bin/python -m backend.config --check-runtime --check-storage
.venv/bin/python -m backend.database --check
.venv/bin/python -m backend.maintenance --execute

# Run
.venv/bin/python -m backend.main                     # backend :8000
(cd frontend && npm run dev -- --hostname 0.0.0.0)   # console :3000

# Validate
.venv/bin/python -m unittest discover -s tests -v
(cd frontend && npm run typecheck && npm run build)

# Git
git add -A
git commit -m "your message"
git push origin arena/9e1a5bba-daemoncut   # current branch
```

---

## 12. Safety and storage notes

- **Never commit** credentials, OAuth tokens, model weights, media, or `.env`. The
  `.gitignore` already excludes them; `sync_github.bat` re-checks ignore rules before
  pushing.
- SQLite metadata, logs, OAuth tokens (encrypted at rest), and temp state live in the
  ignored `.daemoncut/` folder.
- Google Drive for desktop performs the cloud sync; DaemonCut does not promise sync while
  that client is paused or offline.
- The prompt law is enforced in the backend and UI: reference-image prompts are not capped;
  each video scene prompt is at most **880 characters** (`MAX_YT_CREATE_CHARS`).
- Do not reuse third-party dialogue, distinctive scenes, or protected character designs
  without rights.
