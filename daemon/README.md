# Seraph Native macOS Daemon

> **Partial/transitional implementation:** This daemon currently posts to the
> Mac-hosted Seraph backend. The accepted target is a paired, revocable Mac edge
> connected to the authenticated GPU-hosted core; see
> [ADR-004](../docs/implementation/decisions/004-gpu-core-mac-edge-topology.md).
> This README describes current behavior, not proof that the target edge ships.

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
It also polls for pending native Seraph notifications and displays them through macOS Notification Center when the browser surface is unavailable.

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

Screen analysis is opt-in and runs **on context switch** (not a timer). The daemon makes **no cloud model call** ([ADR-035](../docs/implementation/decisions/035-near-tee-hosted-inference-only.md)): it captures the screen, runs on-device Apple Vision OCR, and uploads the allowed capture to the local backend, which runs the governed vision analysis. Blocked apps (password managers, banking, etc.) are never screenshotted or uploaded.

| Flag | Default | Description |
|------|---------|-------------|
| `--ocr` | off | Enable screen capture on context switch |
| `--ocr-provider` | `apple-vision` | Local OCR provider: `apple-vision` (on-device). `codex-local` (local Codex CLI, which is cloud-backed) stays available only when selected explicitly and is slated for removal under ADR-035/#1147 |
| `--ocr-model` | provider default | Model for the `codex-local` provider |
| `--preserve-captures` | `$SERAPH_PRESERVE_SCREEN_CAPTURES` | Preserve allowed screenshots, redacted provider output, and normalized JSON artifacts for future re-analysis |
| `--capture-archive-dir` | `$SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR` or `~/Library/Application Support/Seraph/artifacts/screen-captures` | Durable local directory for preserved artifacts |
| `--edge-device-id` | `$SERAPH_EDGE_DEVICE_ID` | Paired edge device id used to authenticate capture uploads |
| `--edge-pairing-id` | `$SERAPH_EDGE_PAIRING_ID` | Paired edge pairing id used to authenticate capture uploads |
| `--edge-credential-file` | `$SERAPH_EDGE_CREDENTIAL_FILE` | Private (`0600`) file holding the paired edge credential. `SERAPH_EDGE_CREDENTIAL` also works; there is deliberately no flag for the raw credential |
| `--blocklist-file` | (none) | Path to JSON blocklist config (extends built-in defaults) |
| `--ocr-interval` | (deprecated) | Ignored — OCR now runs on context switch |

Examples:

```bash
# On-device OCR only (captures stay local; no upload configured)
./daemon/run.sh --ocr --verbose

# On-device OCR plus authenticated local capture upload
SERAPH_EDGE_DEVICE_ID=<device_id> SERAPH_EDGE_PAIRING_ID=<pairing_id> \
  SERAPH_EDGE_CREDENTIAL_FILE=~/.seraph/edge-credential \
  ./daemon/run.sh --ocr --blocklist-file ~/blocklist.json --verbose
```

### Local capture upload

When screen analysis is enabled and the upload is configured, each allowed capture is sent to `POST /api/observer/captures` on the **loopback** backend (`--url` must be `localhost`, `127.0.0.1` or `::1`; any other host is refused). The request carries the PNG, app name, window title, capture time, a unique `capture_id` and the bounded local analysis (Apple Vision text up to 8 KiB, or the explicitly selected `codex-local` structured result). The backend stores that analysis on the observation (summary, activity, project) so digests and reports use it until governed vision analysis exists.

- **Authentication:** the scoped paired-edge credential returned once by node pairing (`POST /api/nodes/pairings/pair`; the same credential the paired edge transport uses), sent as `Authorization: Bearer` with `X-Seraph-Node-Device` / `X-Seraph-Node-Pairing`. The daemon sends no `Origin` and no cookie; for this one path the backend then checks only the host allow-list, and the credential stays mandatory. A request that carries a cookie or an `Origin` must still pass the normal origin check, and a browser session cookie is never upload authority. The pairing's stored policy must allow capability `media.ingest`, purpose `screen_capture`, the image type and its size.
- **Backend checks:** screen analysis must be enabled; built-in blocklisted apps are rejected (a local `allowed_apps` override does not apply to uploads); only real PNG/JPEG bytes up to 8 MiB and 50 megapixels (read from the header before decoding); at most 30 uploads per minute per pairing; a repeated `capture_id` is idempotent.
- **Storage:** private (`0700` directory, `0600` file, atomic rename) under the screenshot folder's `.seraph-daemon-captures/`, which must be a real directory owned by the backend user (a symlinked store is refused and never cleaned). The observation goes through the same dedupe as folder images and is queued for the governed vision analysis job. `screen_cleanup` deletes uploaded files after the observation retention period. These files are not backed up (see #1141).
- **Bounded retries:** at most two attempts per capture (only on network or 5xx errors), then a cool-down that doubles from 30 s up to 10 min (or the server's `Retry-After`) during which captures are not uploaded. There is no upload queue or poll loop.

When an upload is stored, the context post carries only `active_window`, so the same moment is not recorded twice. Without upload configuration, the daemon keeps posting the on-device OCR observation as JSON.

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

### Automation (required for System Events)

macOS can also require Automation permission before the terminal running Seraph
may control **System Events**. When this is missing, logs or daemon status can
show `-1743`, `-10827`, or "not authorised to send Apple events".

**To grant:**
1. Open **System Settings > Privacy & Security > Automation**
2. Find the terminal app that starts Seraph
3. Enable permission to control **System Events**
4. Restart with `./manage.sh -e dev daemon start`

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

**Not captured:** keystrokes, clipboard, file contents. When `--ocr` is enabled, screenshots of allowed apps are captured only on context switch (or the periodic capture mode). `apple-vision` OCR runs on this Mac. Captures are uploaded only to the loopback backend described above; the daemon contacts no model provider. `codex-local` invokes the local `codex exec` command and writes a temporary PNG for that command, then deletes it after analysis or failure unless capture preservation is enabled. With `--preserve-captures`, allowed screenshots, redacted provider output, and normalized JSON are archived under `--capture-archive-dir`. The backend uses the same durable archive root by default, or `SCREEN_CAPTURE_ARCHIVE_DIR` when explicitly configured.

Preserved local Codex artifacts can be inspected through the backend from localhost only:

```bash
curl http://localhost:8004/api/observer/screen-artifacts
curl http://localhost:8004/api/observer/screen-artifacts/{observation_id}/codex-output
open http://localhost:8004/api/observer/screen-artifacts/{observation_id}/image
```

The daemon posts context to `POST /api/observer/context` (the `observation` is omitted when the capture upload was stored):
```json
{
  "active_window": "VS Code — main.py",
  "observation": {
    "app": "VS Code",
    "window_title": "main.py",
    "activity": "coding",
    "project": "seraph",
    "summary": "Editing Python file in VS Code",
    "details": ["file: main.py", "language: Python"],
    "capture_artifacts": {
      "id": "artifact-digest",
      "image_path": "~/Library/Application Support/Seraph/artifacts/screen-captures/2026-06-20/120000-vs-code-artifact.png",
      "codex_output_path": "~/Library/Application Support/Seraph/artifacts/screen-captures/2026-06-20/120000-vs-code-artifact.codex.txt",
      "analysis_path": "~/Library/Application Support/Seraph/artifacts/screen-captures/2026-06-20/120000-vs-code-artifact.analysis.json"
    }
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

## Native Notifications

When the browser is not connected but the daemon is alive, Seraph can now fall back to native macOS notifications for selected proactive messages. The daemon polls the durable backend outbox with a unique worker identity, records a fenced display attempt, shows it with `osascript`, then acknowledges that receipt so it is not replayed by another worker.

This is a first presence path, not a full desktop shell:
- notification delivery is best-effort and does not prove exactly-once OS display
- pending, claimed, display-attempted, acknowledged, failed, cancelled, and unknown states are durable in the backend
- an expired lease or daemon failure becomes `unknown` and requires explicit operator reconciliation before retry
- broader channel controls and richer desktop presence are still future work

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
