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
| `DEFAULT_MODEL` | `openrouter/anthropic/claude-sonnet-4` | LLM model for the agent (any OpenRouter model) |
| `MODEL_TEMPERATURE` | `0.7` | Sampling temperature |
| `MODEL_MAX_TOKENS` | `4096` | Max tokens per response |
| `AGENT_MAX_STEPS` | `10` | Max reasoning steps per turn |
| `DEBUG` | `false` | Enable debug logging |

### Optional scheduling settings

These control when proactive features run (briefings, reviews, working hours):

| Variable | Default | Description |
|---|---|---|
| `USER_TIMEZONE` | `UTC` | Your timezone (e.g., `America/New_York`) |
| `WORKING_HOURS_START` | `9` | Hour (24h) when working hours begin |
| `WORKING_HOURS_END` | `17` | Hour (24h) when working hours end |
| `MORNING_BRIEFING_HOUR` | `8` | Hour for the daily morning briefing |
| `EVENING_REVIEW_HOUR` | `21` | Hour for the evening review |
| `MEMORY_CONSOLIDATION_INTERVAL_MIN` | `30` | Minutes between memory consolidation runs |
| `GOAL_CHECK_INTERVAL_HOURS` | `4` | Hours between goal progress checks |
| `CALENDAR_SCAN_INTERVAL_MIN` | `15` | Minutes between calendar scans |
| `STRATEGIST_INTERVAL_MIN` | `15` | Minutes between strategist ticks |
| `SCHEDULER_ENABLED` | `true` | Enable/disable background scheduler |

### Optional observer settings

These control the observer system and proactive behavior:

| Variable | Default | Description |
|---|---|---|
| `PROACTIVITY_LEVEL` | `3` | Proactivity scale (1–5, higher = more proactive) |
| `OBSERVER_GIT_REPO_PATH` | (empty) | Path to a git repo for commit-aware context |
| `DEEP_WORK_APPS` | (empty) | Comma-separated extra app keywords that trigger deep work state |

### Optional timeout settings

These control execution timeouts for agent and tool operations:

| Variable | Default | Description |
|---|---|---|
| `AGENT_CHAT_TIMEOUT` | `120` | Seconds before chat agent execution times out |
| `AGENT_STRATEGIST_TIMEOUT` | `60` | Seconds before strategist agent times out |
| `AGENT_BRIEFING_TIMEOUT` | `60` | Seconds before daily briefing / evening review times out |
| `CONSOLIDATION_LLM_TIMEOUT` | `30` | Seconds before memory consolidation LLM call times out |
| `WEB_SEARCH_TIMEOUT` | `15` | Seconds before web search times out |

### Optional context window settings

These control how conversation history is trimmed before being sent to the LLM:

| Variable | Default | Description |
|---|---|---|
| `CONTEXT_WINDOW_TOKEN_BUDGET` | `12000` | Max tokens for conversation history |
| `CONTEXT_WINDOW_KEEP_FIRST` | `2` | Always keep first N messages (preserves onboarding context) |
| `CONTEXT_WINDOW_KEEP_RECENT` | `20` | Always keep last N messages |

### Optional delegation settings

Recursive delegation is an experimental feature where the agent delegates to specialist sub-agents:

| Variable | Default | Description |
|---|---|---|
| `USE_DELEGATION` | `false` | Enable orchestrator + specialist agents mode |
| `DELEGATION_MAX_DEPTH` | `1` | Max nesting depth (1 = orchestrator → specialists) |
| `ORCHESTRATOR_MAX_STEPS` | `8` | Max delegation steps for orchestrator |

### Optional LLM call logging settings

These control per-call observability logging for all LiteLLM calls (direct and via smolagents):

| Variable | Default | Description |
|---|---|---|
| `LLM_LOG_ENABLED` | `true` | Enable LLM call logging to JSONL file |
| `LLM_LOG_CONTENT` | `false` | Include full messages and response text (large, privacy-sensitive) |
| `LLM_LOG_DIR` | `/app/logs` | Directory for log files |
| `LLM_LOG_MAX_BYTES` | `52428800` | Max bytes per log file before rotation (default 50 MB) |
| `LLM_LOG_BACKUP_COUNT` | `5` | Number of rotated log files to keep |

All settings with their defaults are defined in `backend/config/settings.py`.

## Start the stack

```bash
./manage.sh -e dev up -d
```

This starts four containers:
- **backend-dev** (`localhost:8004`) — FastAPI + uvicorn
- **sandbox-dev** — snekbox (sandboxed shell execution)
- **http-mcp** — FastMCP HTTP request tool server (internal network only)
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

# OpenRouter cloud OCR (Gemini 2.5 Flash Lite, ~$0.15/month at default 30s interval)
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
| `--ocr-model` | `google/gemini-2.5-flash-lite` | Model for OpenRouter provider |
| `--openrouter-api-key` | `$OPENROUTER_API_KEY` | API key for OpenRouter provider |

### Screen Recording permission

OCR requires the **Screen Recording** permission:

1. Open **System Settings > Privacy & Security > Screen & System Audio Recording** (or **Screen Recording** on older macOS)
2. Click **+** and add your terminal app
3. Toggle it **on**

**Note:** Starting with macOS Sequoia (15.0), the system shows a monthly confirmation prompt asking if you want to continue allowing screen recording. This cannot be suppressed. If permission is revoked, the daemon continues in window-only mode.

## Optional: Stdio MCP Proxy

The stdio proxy wraps stdio-only MCP servers (like `github-mcp-server` or `things-mcp`) as HTTP endpoints so the Dockerized backend can connect to them. It runs natively on macOS so proxied tools can access system APIs (AppleScript, URL schemes, etc.).

### Configuration

Add entries to `data/stdio-proxies.json`:

```json
{
  "proxies": {
    "things3": {
      "command": "uvx",
      "args": ["things-mcp"],
      "port": 8100,
      "enabled": true,
      "description": "Things3 task manager"
    }
  }
}
```

### Start the proxy

```bash
./manage.sh -e dev proxy start
```

Then register the proxied server with the backend:

```bash
./mcp.sh add things3 http://host.docker.internal:8100/mcp --desc "Things3 task manager"
```

To start the proxy automatically with `./manage.sh -e dev up -d`, set in `.env.dev`:

```bash
PROXY_ENABLED=true
PROXY_ARGS=--verbose
```

## Optional: MCP Servers

Seraph supports plug-and-play MCP server integration. Servers are configured in `data/mcp-servers.json` and can be managed three ways:

- **CLI**: `./mcp.sh list`, `add <name> <url>`, `remove`, `enable`, `disable`, `test` — when the backend is running, `mcp.sh` calls the API for live-reload; falls back to direct file editing when offline
- **Settings UI**: Add/remove/toggle servers in the Settings panel
- **REST API**: `GET/POST /api/mcp/servers`, `PUT/DELETE /api/mcp/servers/{name}`

No config file = no MCP tools, no errors. See `data/mcp-servers.example.json` for the format.

### Things3 Example

If you use [Things3](https://culturedcode.com/things/) for task management, Seraph can read and create tasks via MCP (22 tools).

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

4. Register the server:
   ```bash
   ./mcp.sh add things3 http://host.docker.internal:9100/mcp --desc "Things3 task manager"
   ```

5. Restart the backend and check for `Connected to MCP server: 22 tools loaded` in logs.

See **[Things3 MCP Integration](./integrations/things3-mcp)** for the full tool list, LaunchAgent management, and troubleshooting.

## Optional: Google Calendar

Seraph can read your Google Calendar as an observer data source. Upcoming events are included in the strategist's context, enabling calendar-aware proactive features (e.g., meeting detection, schedule-aware nudges). This is **not** a user-facing tool — the agent cannot create or modify calendar events directly.

**Note:** For full calendar management (create/edit/delete events), use a calendar MCP server instead.

### 1. Create OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API**
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

On the first calendar scan, the backend will initiate an OAuth consent flow in the container logs. Follow the URL to authorize read access. A token file (`google_calendar_token.json`) is created automatically in the backend data directory.

After initial authorization, the token refreshes automatically. If no credentials are configured, the calendar source silently returns empty — no errors are raised.

## Optional: GitHub MCP

GitHub MCP integration is **currently disabled** (commented out in `docker-compose.dev.yaml`). The official `github-mcp-server` image only supports stdio transport, which doesn't work in a container network.

Two options are available:

- **Stdio proxy**: Use the stdio proxy (`mcp-servers/stdio-proxy/`) to wrap `github-mcp-server` as an HTTP endpoint. Add it to `data/stdio-proxies.json`, start with `./manage.sh -e dev proxy start`, then register with `./mcp.sh add github http://host.docker.internal:<port>/mcp`.
- **GitHub's hosted MCP endpoint**: `https://api.githubcopilot.com/mcp/` — register directly with `./mcp.sh add github https://api.githubcopilot.com/mcp/` and configure a GitHub token via the Settings UI.

## Optional: SKILL.md Plugins

Skills are zero-code markdown plugins that extend the agent's behavior. Drop a `.md` file in `data/skills/` with YAML frontmatter and the agent gains new capabilities via prompt injection.

```yaml
---
name: code-review
description: Reviews code for quality and best practices
requires:
  tools: [read_file]
user_invocable: true
enabled: true
---

When asked to review code, follow these steps:
1. Read the file(s) mentioned
2. Check for security issues, performance problems, and style violations
3. Provide specific, actionable feedback
```

Skills can be managed via:
- **Settings UI**: Toggle skills on/off in the Settings panel
- **REST API**: `GET /api/skills`, `PUT /api/skills/{name}`, `POST /api/skills/reload`
- **Tool gating**: Skills with `requires.tools` only activate when required tools are available

Eight bundled skills: `daily-standup`, `code-review`, `goal-reflection`, `weekly-planner`, `morning-intention`, `evening-journal`, `moltbook` (requires `http_request`), `web-briefing` (requires `http_request` + `web_search`).

## Optional: Village Map Editor

A standalone Tiled-compatible map editor for authoring the village map:

```bash
cd editor && npm install && npm run dev
```

Opens at `http://localhost:3001`. Outputs Tiled JSON consumed directly by the frontend VillageScene. See `editor/README.md` for full documentation.

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
| Google Calendar: "credentials not found" | Missing `google_credentials.json` | Place OAuth credentials at `backend/config/google_credentials.json` |
| Google Calendar: OAuth flow fails | Container can't open browser | Copy the auth URL from container logs and open it in your host browser |
