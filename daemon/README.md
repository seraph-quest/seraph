# Seraph Native macOS Daemon

Lightweight polling daemon that captures the active window (app name + window title) and posts it to the Seraph backend. Runs natively on macOS — outside Docker.

## Running the Full Project

Seraph has three components: **backend + frontend** (Docker) and the **screen daemon** (native macOS).

### Prerequisites

- Docker Desktop
- [uv](https://docs.astral.sh/uv/) package manager
- An [OpenRouter](https://openrouter.ai/) API key (set in `.env.dev`)

### 1. Configure environment

Copy and edit the env file — at minimum set your `OPENROUTER_API_KEY`:

```bash
# .env.dev already exists with defaults — edit the API key
vim .env.dev
```

### 2. Start backend + frontend (Docker)

```bash
./manage.sh -e dev up -d
```

This starts three containers:
- **backend-dev** (`localhost:8004`) — FastAPI + uvicorn
- **sandbox-dev** — snekbox (sandboxed shell execution)
- **frontend-dev** (`localhost:3000`) — Vite dev server

### 3. Start the screen daemon (native macOS, separate terminal)

```bash
./daemon/run.sh --verbose
```

The daemon runs outside Docker and posts active window context to the backend every few seconds.

### Optional: Things3 MCP integration

If you use Things3 for task management, start the MCP server on your host:

```bash
THINGS_MCP_TRANSPORT=http THINGS_MCP_PORT=9100 uvx things-mcp
```

The backend connects to it via `http://host.docker.internal:9100/mcp` (configured in `.env.dev`).

### Resetting everything

To start completely fresh (useful for testing onboarding), run from the project root:

```bash
./reset.sh        # Resets dev environment (default)
./reset.sh prod   # Resets prod environment
```

This stops all containers, prunes Docker images/volumes, deletes all persistent data (database, memories, soul file, logs), and rebuilds from scratch. You'll also want to clear browser localStorage (`localStorage.clear()` in DevTools).

## Prerequisites

- macOS 13+ (Ventura or later)
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Quick Start

```bash
./daemon/run.sh
```

This syncs dependencies and starts the daemon in one command.

## Manual Install

```bash
cd daemon
uv pip install -r requirements.txt
```

## Run

```bash
uv run python seraph_daemon.py
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `http://localhost:8004` | Backend base URL |
| `--interval` | `5` | Poll interval in seconds |
| `--idle-timeout` | `300` | Seconds of inactivity before skipping POSTs |
| `--verbose` | off | Log every context POST |

Example with all options:

```bash
python seraph_daemon.py --url http://localhost:8004 --interval 3 --idle-timeout 600 --verbose
```

### Screen Analysis Options

Screen analysis is opt-in and runs **on context switch** (not a timer). When you switch apps, the daemon captures and analyzes the screen to produce structured activity data (activity type, project, summary). Blocked apps (password managers, banking, etc.) are never screenshotted.

| Flag | Default | Description |
|------|---------|-------------|
| `--ocr` | off | Enable screenshot analysis on context switch |
| `--ocr-provider` | `apple-vision` | Provider: `apple-vision` (local) or `openrouter` (cloud, structured JSON) |
| `--ocr-model` | `google/gemini-2.5-flash-lite` | Model for OpenRouter provider |
| `--openrouter-api-key` | `$OPENROUTER_API_KEY` | API key for OpenRouter provider |
| `--blocklist-file` | (none) | Path to JSON blocklist config (extends built-in defaults) |
| `--ocr-interval` | (deprecated) | Ignored — OCR now runs on context switch |

Examples:

```bash
# Local analysis — Apple Vision framework (free, offline, ~200ms per capture)
./daemon/run.sh --ocr --verbose

# Cloud analysis — OpenRouter with Gemini (structured JSON, ~$1.30/mo at ~150 switches/day)
OPENROUTER_API_KEY=sk-or-... ./daemon/run.sh --ocr --ocr-provider openrouter --verbose

# With custom blocklist
./daemon/run.sh --ocr --ocr-provider openrouter --blocklist-file ~/blocklist.json --verbose
```

### App Blocklist

Sensitive apps are never screenshotted. Built-in blocked apps include: password managers (1Password, Bitwarden, LastPass, etc.), banking apps, crypto wallets, and Signal.

Custom blocklist config (`--blocklist-file`):
```json
{
  "blocked_apps": ["TikTok", "Instagram"],
  "allowed_apps": ["Signal"]
}
```

- `blocked_apps`: added to defaults (case-insensitive substring match)
- `allowed_apps`: removed from defaults (overrides)

## Permissions

### Accessibility (required for window titles)

The daemon uses AppleScript to read the frontmost window title, which requires the Accessibility permission.

**To grant:**
1. Open **System Settings > Privacy & Security > Accessibility**
2. Click the **+** button
3. Add your terminal app (Terminal.app, iTerm2, Warp, etc.)
4. Toggle it **on**

This is a one-time grant — macOS will not nag you about it again.

### Screen Recording (required for `--ocr`)

OCR mode captures screenshots to extract visible text. This requires the Screen Recording permission.

**To grant:**
1. Open **System Settings > Privacy & Security > Screen & System Audio Recording** (or **Screen Recording** on older macOS)
2. Click the **+** button
3. Add your terminal app
4. Toggle it **on**

**Note:** Starting with macOS Sequoia (15.0), the system shows a monthly confirmation prompt: *"[App] has been recording your screen. Do you want to continue allowing this?"* This cannot be suppressed. If permission is revoked, the daemon logs a warning and continues in window-only mode.

### What is NOT needed (without `--ocr`)
- **Full Disk Access** — not needed.
- **Input Monitoring** — not needed. Idle detection uses `CGEventSourceSecondsSinceLastEventType` which only returns a duration, not individual keystrokes.

## What Data is Captured

| Captured | Example | How |
|----------|---------|-----|
| App name | `VS Code` | `NSWorkspace` (no permission) |
| Window title | `main.py — seraph` | AppleScript (Accessibility) |
| Idle duration | `312.5` seconds | `CGEventSource` (no permission) |

**Not captured:** keystrokes, clipboard, file contents. Screenshots are only captured in memory when `--ocr` is enabled and are never written to disk.

The daemon posts to `POST /api/observer/context`:
```json
{
  "active_window": "VS Code — main.py",
  "observation": {
    "app": "VS Code",
    "window_title": "main.py",
    "activity": "coding",
    "project": "seraph",
    "summary": "Editing Python file in VS Code",
    "details": ["file: main.py", "language: Python"]
  },
  "switch_timestamp": 1700000000.0
}
```

Without `--ocr`, the payload is simpler (no `observation` field):
```json
{
  "active_window": "VS Code — main.py"
}
```

## How It Works

1. Every `--interval` seconds, checks if the user is idle (no input for `--idle-timeout` seconds)
2. If idle, skips the POST and logs once
3. If active, reads the frontmost app name and window title
4. If the value has changed since the last POST, sends it to the backend
5. If the backend is unreachable, logs a warning and continues polling

## Running as a launchd Service (Optional)

To run the daemon automatically on login, create `~/Library/LaunchAgents/ai.seraph.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.seraph.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/seraph/daemon/seraph_daemon.py</string>
    <string>--verbose</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/seraph-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/seraph-daemon.log</string>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/ai.seraph.daemon.plist
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| "No window title" / title is always None | Accessibility permission not granted | Grant permission in System Settings (see above) |
| "Backend not reachable" warnings | Backend not running or wrong URL | Start backend with `./manage.sh -e dev up -d` |
| Daemon exits immediately | Python missing PyObjC | Run `uv pip install -r requirements.txt` |
| High CPU usage | Interval too low | Increase `--interval` (default 5s is fine) |
