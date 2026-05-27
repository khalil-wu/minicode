# MiniCode Desktop (Client)

This folder contains the desktop client shell for MiniCode.

## What it does now

- Starts a backend sidecar process (unless `MINICODE_SKIP_BACKEND=1`).
- Waits for backend health on `GET /health`.
- Loads frontend from either:
  - `MINICODE_FRONTEND_URL` (development), or
  - `../frontend/dist/index.html` (production-like local run).
- Injects runtime endpoints into the renderer via `window.__MINICODE_RUNTIME__`:
  - `apiBaseUrl`
  - `wsBaseUrl`
- Shows a dedicated startup recovery surface when backend health or renderer boot fails.
- Includes an in-app Workspace panel (file tree + editor + save + send file to assistant).

## Quick start (one-click desktop client)

Windows PowerShell:

```powershell
.\run-desktop.ps1
```

Windows CMD / double-click:

```bat
run-desktop.bat
```

These launchers will:

- install missing frontend and desktop dependencies,
- build frontend assets with desktop-friendly relative asset paths,
- start the Electron desktop client (and backend sidecar).

## Manual start

1. Build frontend once:

```powershell
npm --prefix ../frontend run build
```

2. Install desktop dependencies:

```powershell
npm install
```

3. Run desktop shell:

```powershell
npm run dev
```

If frontend is already built and you just want to open the desktop app quickly:

```powershell
npm run start
```

## Environment variables

- `MINICODE_BACKEND_HOST` (default: `127.0.0.1`)
- `MINICODE_BACKEND_PORT` (default: `8000`)
- `MINICODE_API_BASE_URL` (default: `http://<host>:<port>`)
- `MINICODE_WS_BASE_URL` (default: `ws://<host>:<port>`)
- `MINICODE_PYTHON` (default: `py` on Windows, `python3` otherwise)
- `MINICODE_FRONTEND_URL` (if set, loads frontend from dev server URL)
- `MINICODE_SKIP_BACKEND=1` (skip sidecar startup, connect to existing backend)
- `MINICODE_BACKEND_STARTUP_TIMEOUT_MS` (default: `90000`; increase for slow first boots)

## Notes

- The sidecar uses `python -m uvicorn backend.main:app` semantics.
- A startup failure window now supports retry, quit, and opening the desktop log.
- Windows packaging is wired through `electron-builder + NSIS`.

## Packaging

1. Install desktop dependencies:

```powershell
npm install
```

2. Build the Windows installer:

```powershell
npm run dist:win
```

3. For a local unpacked smoke build:

```powershell
npm run pack:dir
```

Current packaging still expects a working local Python installation for the backend sidecar.
