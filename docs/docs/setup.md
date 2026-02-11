---
sidebar_position: 2
---

# Setup Guide

Complete instructions for getting Seraph fully running with all features.

## Prerequisites

| Requirement | Why |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Runs backend, frontend, and sandbox containers |
| [uv](https://docs.astral.sh/uv/) | Python package manager (used by the daemon and scripts) |
| macOS 13+ (Ventura) | Required for the native screen daemon (PyObjC, AppleScript) |
| Python 3.12+ | Daemon runtime (outside Docker) |
| [OpenRouter](https://openrouter.ai/) account | LLM API access (powers the agent) |

## Configuration

All settings live in `.env.dev` at the project root. The only **required** value is your API key:

```bash
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

### Optional model settings

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_MODEL` | `openrouter/anthropic/claude-sonnet-4` | LLM model for the agent |
| `MODEL_TEMPERATURE` | `0.7` | Sampling temperature |
| `MODEL_MAX_TOKENS` | `4096` | Max tokens per response |
| `AGENT_MAX_STEPS` | `10` | Max reasoning steps per turn |

### Optional scheduling settings

These control when proactive features run (briefings, reviews, working hours):

| Variable | Default | Description |
|---|---|---|
| `USER_TIMEZONE` | `UTC` | Your timezone (e.g., `America/New_York`) |
| `WORKING_HOURS_START` | `9` | Hour (24h) when working hours begin |
| `WORKING_HOURS_END` | `17` | Hour (24h) when working hours end |
| `MORNING_BRIEFING_HOUR` | `8` | Hour for the daily morning briefing |
| `EVENING_REVIEW_HOUR` | `21` | Hour for the evening review |

All settings with their defaults are defined in `backend/config/settings.py`.

## Start the stack

```bash
./manage.sh -e dev up -d
```

This starts three containers:
- **backend-dev** (`localhost:8004`) — FastAPI + uvicorn
- **sandbox-dev** — snekbox (sandboxed shell execution)
- **frontend-dev** (`localhost:3000`) — Vite dev server

Verify everything is running:

```bash
# Health check
curl http://localhost:8004/health

# Open the UI
open http://localhost:3000

# Swagger API docs
open http://localhost:8004/docs
```

## Screen daemon

The native macOS daemon runs **outside Docker** and posts the active window context to the backend. This enables context-aware features like deep work detection.

```bash
./daemon/run.sh --verbose
```

On first run, macOS will prompt for **Accessibility** permission (needed to read window titles):

1. Open **System Settings > Privacy & Security > Accessibility**
2. Click **+** and add your terminal app (Terminal, iTerm2, Warp, etc.)
3. Toggle it **on**

This is a one-time grant. The daemon will then report the frontmost app and window title every 5 seconds.

### Daemon options

| Flag | Default | Description |
|---|---|---|
| `--url` | `http://localhost:8004` | Backend base URL |
| `--interval` | `5` | Poll interval in seconds |
| `--idle-timeout` | `300` | Seconds of inactivity before skipping POSTs |
| `--verbose` | off | Log every context POST |

## Optional: Screen OCR

OCR mode extracts visible text from the screen so the strategist agent can reason about what you're working on.

```bash
# Apple Vision (local, free, ~200ms per capture)
./daemon/run.sh --ocr --verbose

# OpenRouter cloud OCR (Gemini 2.0 Flash Lite, ~$0.09/month)
OPENROUTER_API_KEY=sk-or-... ./daemon/run.sh --ocr --ocr-provider openrouter --verbose
```

| Provider | Pros | Cons |
|---|---|---|
| `apple-vision` (default) | Free, offline, fast (~200ms) | Text-only, no layout understanding |
| `openrouter` | Understands layout and context | Requires API key, small cost |

### OCR options

| Flag | Default | Description |
|---|---|---|
| `--ocr` | off | Enable OCR screen text extraction |
| `--ocr-provider` | `apple-vision` | `apple-vision` (local) or `openrouter` (cloud) |
| `--ocr-interval` | `30` | OCR capture interval in seconds |
| `--ocr-model` | `google/gemini-2.0-flash-lite-001` | Model for OpenRouter provider |
| `--openrouter-api-key` | `$OPENROUTER_API_KEY` | API key for OpenRouter provider |

### Screen Recording permission

OCR requires the **Screen Recording** permission:

1. Open **System Settings > Privacy & Security > Screen & System Audio Recording** (or **Screen Recording** on older macOS)
2. Click **+** and add your terminal app
3. Toggle it **on**

**Note:** Starting with macOS Sequoia (15.0), the system shows a monthly confirmation prompt asking if you want to continue allowing screen recording. This cannot be suppressed. If permission is revoked, the daemon continues in window-only mode.

## Optional: Things3

If you use [Things3](https://culturedcode.com/things/) for task management, Seraph can read and create tasks via MCP (22 tools).

Quick setup:

1. Install and start the MCP server as a LaunchAgent:
   ```bash
   ./scripts/install-things-mcp.sh
   ```

2. Grant **Full Disk Access** to `uvx` (path shown by the script, typically `/opt/homebrew/bin/uvx`):
   - **System Settings > Privacy & Security > Full Disk Access** > **+** > enter the path

3. Restart the service:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.seraph.things-mcp
   ```

4. The backend connects automatically via the `THINGS_MCP_URL` already set in `.env.dev`:
   ```
   THINGS_MCP_URL=http://host.docker.internal:9100/mcp
   ```

5. Restart the backend and check for `Connected to MCP server: 22 tools loaded` in logs.

See **[Things3 MCP Integration](./integrations/things3-mcp)** for the full tool list, LaunchAgent management, and troubleshooting.

## Optional: Google Calendar & Gmail

Seraph can read/create calendar events and read/send emails via Google APIs.

### 1. Create OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API** and **Gmail API**
4. Go to **APIs & Services > Credentials** > **Create Credentials** > **OAuth client ID**
5. Application type: **Desktop app**
6. Configure the **OAuth consent screen** (External is fine for personal use; add yourself as a test user)
7. Download the credentials JSON file

### 2. Mount the credentials

Place the downloaded file at:

```
backend/config/google_credentials.json
```

This directory is volume-mounted into Docker at `/app/config/`, so the backend can access it at `/app/config/google_credentials.json`.

### 3. First-run OAuth flow

On first use of a calendar or email tool, the backend will initiate an OAuth consent flow in the container logs. Follow the URL to authorize access. Token files are created automatically:

- `google_calendar_token.json` — stored in the backend data directory
- `google_gmail_token.json` — stored in the backend data directory (separate token for Gmail)

After initial authorization, tokens refresh automatically.

## Optional: GitHub MCP

GitHub MCP integration is **currently disabled** (commented out in `docker-compose.dev.yaml`). The official `github-mcp-server` image only supports stdio transport, which doesn't work in a container network.

Workarounds being evaluated:
- **mcp-proxy**: stdio-to-HTTP bridge
- **GitHub's hosted MCP endpoint**: `https://api.githubcopilot.com/mcp/`

To track progress, see the `GITHUB_MCP_URL` setting in `.env.dev`.

## Resetting everything

To start completely fresh (useful for testing onboarding):

```bash
./reset.sh        # Reset dev environment (default)
./reset.sh prod   # Reset prod environment
```

This stops containers, prunes Docker images/volumes, deletes all persistent data (database, memories, soul file, OAuth tokens, logs), and rebuilds from scratch.

Also clear browser state:

```js
// In browser DevTools console
localStorage.clear()
```

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `curl: (7) Failed to connect to localhost:8004` | Backend not running | `./manage.sh -e dev up -d` and check `./manage.sh -e dev logs -f backend-dev` |
| `OPENROUTER_API_KEY` error in logs | Missing or invalid API key | Set `OPENROUTER_API_KEY` in `.env.dev` and rebuild: `./manage.sh -e dev up -d` |
| Daemon: "No window title" / title is always None | Accessibility permission not granted | Grant in **System Settings > Privacy & Security > Accessibility** |
| Daemon: "Backend not reachable" | Backend not running or wrong URL | Start backend first; check `--url` flag |
| Daemon exits immediately | Missing PyObjC | Run `cd daemon && uv pip install -r requirements.txt` |
| `Failed to connect to MCP server` | Things3 MCP not running | Check: `curl http://localhost:9100/mcp`; ensure LaunchAgent is loaded |
| `unable to open database file` (Things3) | Full Disk Access not granted to `uvx` | Grant FDA, then restart the things-mcp service |
| Google Calendar/Gmail: "credentials not found" | Missing `google_credentials.json` | Place OAuth credentials at `backend/config/google_credentials.json` |
| Google Calendar/Gmail: OAuth flow fails | Container can't open browser | Copy the auth URL from container logs and open it in your host browser |
