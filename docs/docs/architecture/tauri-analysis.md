---
sidebar_position: 1
---

# Tauri Desktop App — Architecture Analysis

An evaluation of migrating Seraph from Docker + native daemon to a Tauri desktop application.

**Status**: Under consideration for future phase (Phase 5-6)

---

## What is Tauri

A Rust framework for building native desktop apps. The frontend (React/Phaser) runs in the OS's built-in webview (WebKit on macOS). A Rust backend handles native OS access. Result: a `.dmg` the user double-clicks.

**The Python constraint**: Tauri's native layer is Rust, but our entire AI stack is Python (smolagents, LiteLLM, LanceDB, sentence-transformers). The AI ecosystem lives in Python. So Tauri would be a **thin native shell** around the existing Python backend, not a replacement.

---

## Current Architecture (Docker + Daemon)

```
┌─────────────────────────────────────────────┐
│  Docker                                      │
│  ┌──────────────┐  ┌──────────────────────┐ │
│  │ frontend-dev │  │ backend-dev          │ │
│  │ React/Phaser │  │ FastAPI + smolagents │ │
│  │ port 3000    │  │ port 8004            │ │
│  └──────────────┘  └──────────────────────┘ │
│  ┌──────────────┐                            │
│  │ sandbox-dev  │                            │
│  │ snekbox      │                            │
│  └──────────────┘                            │
└─────────────────────────────────────────────┘
         ↑ HTTP
┌──────────────────┐
│ Native daemon    │  ← separate install
│ PyObjC           │
│ screen capture   │
└──────────────────┘

User opens: browser tab → localhost:3000
```

User experience: `docker-compose up`, open browser, install daemon separately. Three moving parts.

---

## Tauri Architecture

```
┌─────────────────────────────────────────────┐
│  Seraph.app (single .dmg)                    │
│                                              │
│  ┌──────────────────────────────────────┐   │
│  │ Tauri Shell (Rust)                    │   │
│  │ • System tray icon                    │   │
│  │ • Native notifications                │   │
│  │ • Screen capture (macOS APIs)         │   │
│  │ • Window tracking                     │   │
│  │ • Global hotkeys                      │   │
│  │ • Auto-start on login                 │   │
│  │ • Auto-update                         │   │
│  └──────────┬───────────────────────────┘   │
│             │                                │
│  ┌──────────▼──────────┐  ┌──────────────┐  │
│  │ Webview             │  │ Python sidecar│  │
│  │ React/Phaser        │  │ FastAPI       │  │
│  │ (native webview,    │  │ smolagents    │  │
│  │  not Chromium)      │  │ LLM, DB, etc  │  │
│  └─────────────────────┘  └──────────────┘  │
│                                              │
│  ┌──────────────┐                            │
│  │ Snekbox      │  ← optional, or use       │
│  │ (subprocess) │    native sandboxing       │
│  └──────────────┘                            │
└─────────────────────────────────────────────┘

User opens: double-click Seraph.app
```

User experience: Download `.dmg`, drag to Applications, done. One app, system tray icon, feels native.

---

## Advantages of Tauri

| Aspect | Docker + Daemon | Tauri |
|--------|----------------|-------|
| **Install** | docker-compose + daemon install script | Double-click `.dmg` |
| **User sees** | Browser tab at localhost | Native app window |
| **System tray** | No | Yes — Seraph icon always present |
| **Notifications** | Browser notifications (unreliable) | Native macOS notifications |
| **Screen capture** | Separate daemon process | Built into app (Rust calls macOS APIs) |
| **Auto-start** | Manually configure Docker + daemon | Built-in, one checkbox |
| **Auto-update** | Manual `docker pull` / `git pull` | Built-in updater, like any Mac app |
| **Resource usage** | Docker VM overhead + containers | Direct processes, lighter |
| **Ports exposed** | localhost:3000, localhost:8004 | Nothing exposed — internal IPC |
| **Offline** | Works (except LLM) | Works (except LLM) |
| **Global hotkey** | No | Yes — e.g., `Cmd+Shift+S` opens Seraph anywhere |
| **Moving parts** | 3 (Docker, frontend, daemon) | 1 (Seraph.app) |

---

## Disadvantages of Tauri

| Concern | Details |
|---------|---------|
| **Rust** | Learning curve. The Rust layer would be thin (~500 lines, OS APIs + lifecycle) but if something breaks in Rust, it's harder to debug without experience. |
| **Python bundling** | Packaging Python + all dependencies into a sidecar is non-trivial. Tools like PyInstaller or PyOxidizer work but have edge cases. ~500MB bundle size. |
| **Migration effort** | Significant refactoring: remove Docker, set up Tauri project, configure sidecar, rewrite frontend build pipeline. 1-2 weeks of work. |
| **Dev experience** | Lose Docker hot-reload. Need Tauri dev setup (`cargo tauri dev`). Python sidecar restart on changes needs custom scripting. |
| **Snekbox** | Currently runs as Docker container. Without Docker, need alternative sandboxing (subprocess with resource limits, or embed in Python sidecar). |
| **Cross-platform** | If we ever want Linux/Windows, each platform needs its own native code for screen capture, notifications, etc. |

---

## Detailed Tauri Architecture

### Rust Layer (thin — ~500 lines)

Handles only what needs native OS access:

```
What the Rust layer does:
- App lifecycle (start/stop Python sidecar)
- System tray with status icon (shows interruption mode)
- Native notifications (morning briefing, urgent alerts)
- Global hotkey (Cmd+Shift+S → bring Seraph to front)
- Screen capture via CGWindowListCreateImage
- Active window tracking via NSWorkspace
- Idle detection via CGEventSource
- Auto-update via Tauri's built-in updater
- File dialogs if needed
```

The Rust layer sends screen/window context to the Python sidecar via localhost HTTP — the same pattern as the native daemon approach, just bundled into one app.

### Python Sidecar

Exactly the current backend, running as a subprocess instead of in Docker:

```
Tauri starts → launches `python -m uvicorn main:app --port 8003`
Tauri stops  → sends SIGTERM to Python process
```

All AI code stays the same. FastAPI, smolagents, LanceDB, everything. No rewrites.

### Frontend

Identical React/Phaser code, served from Tauri's webview instead of a browser tab. Minor changes needed: asset paths, no CORS required since it's local.

### What Replaces Docker

| Docker component | Tauri equivalent |
|-----------------|-----------------|
| `backend-dev` container | Python subprocess managed by Tauri |
| `frontend-dev` container | Tauri's built-in webview |
| `sandbox-dev` (snekbox) | Subprocess with resource limits, or keep Docker just for sandbox |
| Docker networking | localhost HTTP between Rust and Python |
| `docker-compose up` | Double-click app icon |

---

## Hybrid Option: Tauri for Production, Docker for Development

Both can coexist:
- **Development**: Keep Docker (hot-reload, easy debugging, familiar workflow)
- **Production/Distribution**: Build a Tauri `.dmg` for end users that bundles everything

The code stays the same — only the packaging/deployment layer changes. This is the most pragmatic path.

---

## Assessment

**For development (building Seraph)**: Docker + native daemon is the right call. Familiar Python stack, hot-reload, easy debugging, and the daemon is lightweight.

**For distribution (giving Seraph to others)**: Tauri would be worth the investment. Nobody wants to install Docker to run a personal AI assistant. A `.dmg` with a system tray icon is the expected UX.

**Migration is non-destructive**: The Python backend stays identical. The frontend stays identical. The Rust layer is thin. This can be done as a Phase 5-6 polish step when the core product is solid.

**Recommendation**: Build on Docker now (Phases 1-4). Consider Tauri migration when:
1. The product is functionally complete and stable
2. Distribution to other users becomes a priority
3. The thin Rust layer can be scoped and built in 1-2 weeks
