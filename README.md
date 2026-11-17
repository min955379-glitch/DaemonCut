# DaemonCut — Local creator studio

DaemonCut is a local-first production console for original short-form stories, editing, and independent social publishing. The Windows workstation runs a FastAPI API and a responsive Next.js/TypeScript console; heavyweight media and model data stay in the configured **DaemonCut Development** Google Drive folder, while code and small configuration remain in Git.

## Four-phase roadmap

1. **Foundation:** hybrid Drive/Git storage, Windows setup, SQLite workspaces and queues, Gemini/Ollama generation, trend research, series canon, and the strict scene-prompt validator.
2. **Production engines:** voice modes, narration/mastering, English/Roman Urdu/Roman Hindi captions, vertical video assembly, thumbnails, and Drive asset handling.
3. **Local application services:** FastAPI, WebSocket logs, QR phone uploads, public-source inspection and original structure adaptation, PeakCut highlight scoring, persistent dictation, and Asia/Karachi scheduling.
4. **Console and publishing:** responsive three-zone console, niche/story workspaces, API Key Vault, official YouTube/TikTok/Meta OAuth adapters, independent platform publishing, and Meta ready-to-post Drive exports.

## Storage and source control

- Set `GDRIVE_DAEMONCUT_PATH` to a locally mounted Google Drive for desktop folder named `DaemonCut Development` (or `DaemonCut`). Media, renders, source footage, phone uploads, voice samples, and model weights resolve under this root through `config.resolve_drive_path(...)`.
- SQLite metadata, logs, OAuth tokens, OAuth encryption keys, dictation drafts, and temporary runtime state live locally under ignored `.daemoncut/`. OAuth tokens are encrypted at rest and written with owner-only permissions where supported.
- GitHub is for source code, scripts, and small configuration only. `.gitignore` excludes `.env`, `.daemoncut/`, media, models, rendered bundles, and frontend build/dependency folders. Never commit credentials, OAuth tokens, model weights, or media.
- Google Drive for desktop performs cloud synchronization. DaemonCut does not promise cloud sync while that client is paused or offline.

## Install and run on Windows

1. Install Python 3.11+, Node.js LTS (npm), Git for Windows, and Google Drive for desktop. Ollama and the optional local media/AI runtimes are installed separately.
2. Run `setup.bat`. It creates/reuses `.venv`, installs `requirements.txt`, asks for the Drive path, checks storage and the SQLite schema, and runs `npm install` in `frontend/` when Node.js is present. If Node.js is not installed, the API can still be installed; install Node.js LTS and rerun setup for the console.
3. Copy/edit `.env` (setup creates it from `.env.example`). Add the credentials you want to use. The Gemini default is exactly `gemini-4-hydrogen`; leaving `GEMINI_API_KEY` blank allows the configured local Ollama fallback.
4. Run `start_daemoncut.bat` for the interactive console. It opens separate FastAPI and Next.js windows:
   - Console: `http://127.0.0.1:3000`
   - API docs: `http://127.0.0.1:8000/docs`
   - Phone bridge: `http://127.0.0.1:8000/bridge`
5. `install_autostart.bat` registers an interactive per-user logon task for the **headless** `start_daemoncut.bat --boot` cycle. Boot mode starts only the API/scheduler, performs the bounded PKT upload cycle, and can cleanly self-shut down when idle or complete. For PKT wall-clock timing, set Windows to Pakistan Standard Time (UTC+05:00).

The API binds to `0.0.0.0:8000` for trusted same-Wi-Fi phone uploads; the browser console uses same-origin API/WebSocket paths proxied by Next.js. Do not expose the local studio API to the public Internet.

## Console layout and workflows

The console uses a responsive three-zone layout: a left workspace/series-bible/voice rail, a central production workspace, and a right hardware/publishing/queue inspector. Typography is IBM Plex Sans and IBM Plex Mono. The frontend color system is locked to matte obsidian `#121316`, elevated slate `#1A1D24`, border `#2B303B`, parchment `#F4EFE6`, brass `#D6AD55`, steel `#2B5B84`, navy `#102B4E`, and crimson `#C94A4A`; statuses use brass, steel, or crimson only.

Main modules include:

- **3D Cartoon Factory** and **3D Roblox Story Factory:** original scripts, unlimited-length E1/E2/E3/E3B reference prompts, live scene-prompt character counters, a hard `<= 880` character YouTube Create scene prompt ceiling, copying, Drive clip rendering/queueing, and the phone upload bridge.
- **Universal Niche Studio:** generate a strategy blueprint, a workspace-persisted Series Bible, and tailored scripts/prompts for custom genres.
- **Director Copilot:** director chat, saved Web Speech dictation with pause/resume, local Whisper audio upload, and public URL inspection/structure adaptation.
- **PeakCut:** analyze supported YouTube/Twitch/Kick sources or local Drive paths, rank highlight windows, select highlights, choose one of three vertical framing modes and English/Roman Urdu/Roman Hindi captions, and render vertical clips with short thumbnail hooks.
- **Batch Scheduler:** exact seven-episode Saturday batches, anti-repetition scan, A/B/C hook/title variants, alternating 21:30/00:30 Asia/Karachi slots, queue review, scheduler controls, and per-platform publish selection.
- **Trend Scout**, **Analytics & Health**, a live `/ws/logs` terminal, persistent Series Bible viewer, QR Wi-Fi Bridge, and local API Key Vault.

The prompt law is enforced in the backend and UI: reference-image prompts are not capped; each video scene prompt is at most 880 characters. Do not reuse third-party dialogue, distinctive scenes, or protected character designs without rights.

## Independent publishing and OAuth

Configure only the providers you intend to use. YouTube, TikTok, Facebook, and Instagram publishing are independently selectable: missing Meta credentials do not block YouTube or TikTok. The console's Key Vault never fills secret fields from a GET response; it reports only whether a key is configured. New keys are written to the ignored local `.env` file, and settings updates are restricted to a local console request.

- **YouTube:** set `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET`, then authorize from the Key Vault. Publishing uses the official YouTube Data API for video upload, optional thumbnail and SRT captions, and a first comment.
- **TikTok:** set `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`, then complete OAuth with PKCE. Posting uses the official Content Posting API.
- **Facebook / Instagram:** configure Meta app credentials, account IDs/tokens, and set `META_API_ENABLED=true` only when ready. If Meta is disabled or not connected, publish selection creates a manual ready-to-post bundle under `renders_archive/ready_to_post/<episode_id>/` with MP4, cover JPG, SRT, and formatted post copy. This fallback is independent from YouTube/TikTok.
- Set `OAUTH_REDIRECT_URI` to the registered OAuth callback. The default is `http://127.0.0.1:8000/oauth/callback` for a local workstation. Register the matching redirect with each provider.

Core settings and routes:

- Safe environment-key status/update: `GET/PUT /api/settings/env` (raw secrets are never returned).
- OAuth: `GET /api/oauth/{platform}/authorize` and `GET /oauth/callback`.
- Status and publish: `GET /api/publish/status`, `POST /api/publish/episode/{episode_id}`.
- Workspace/niche/episode APIs: `/api/workspaces`, `/api/niches`, `/api/trends`, `/api/studio/*`.
- System/scheduler/clipper/director/bridge: `/api/system`, `/api/scheduler/*`, `/api/clipper/highlights`, `/api/director/*`, `/bridge`, and `/ws/logs`.

## Models, media, and optional engines

Heavy model weights are not downloaded automatically. The setup wizard can point Ollama at `models/ollama` in Drive; restart Ollama before downloading weights. Add only licensed assets and model runtimes. FFmpeg/ffprobe, yt-dlp, Pillow, Edge TTS, and the lightweight API dependencies are listed or supported by the project; Playwright, local Whisper, Kokoro, Pocket TTS, Chatterbox, OpenCV, Vulkan/Real-ESRGAN, and GPU encoders may require separate workstation setup. Verify hardware/runtime availability locally before scheduling renders.

Useful commands:

```bat
.venv\Scripts\python.exe -m backend.config --check-runtime --check-storage
.venv\Scripts\python.exe -m backend.database --check
.venv\Scripts\python.exe -m backend.main
cd frontend && npm run dev
```

## Validation

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
cd frontend && npm run typecheck
cd frontend && npm run build
```

Publisher and API tests mock external OAuth/provider requests; they do not make real social posts. FFmpeg rendering and Windows batch launchers need a configured Windows/Drive workstation for end-to-end validation.

## GitHub sync

`sync_github.bat` checks secret/media/build ignore rules and the Git index, stages code changes on the current branch, creates a local commit when needed, then pushes only that branch to `https://github.com/min955379-glitch/DaemonCut`. It will not switch branches and refuses an unexpected `origin`. Review `git status` and the staged diff before running it.
