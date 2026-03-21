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

### Optional runtime routing settings

These control which model/profile a specific runtime path uses, plus how that path falls back if its primary target fails:

| Variable | Default | Description |
|---|---|---|
| `LOCAL_MODEL` | (empty) | Model id for the local runtime profile |
| `LOCAL_LLM_API_BASE` | (empty) | API base for the local runtime profile |
| `LOCAL_RUNTIME_PATHS` | (empty) | Comma-separated runtime paths or glob patterns that should prefer the local profile |
| `RUNTIME_PROFILE_PREFERENCES` | (empty) | Semicolon-separated `runtime_path=profile_a|profile_b` preference chains; `runtime_path` may be exact or a glob |
| `RUNTIME_POLICY_INTENTS` | (empty) | Semicolon-separated `runtime_path=intent_a|intent_b` policy intents such as `local_first`, `fast`, `cheap`, `reasoning`, or `tool_use`; `runtime_path` may be exact or a glob |
| `RUNTIME_POLICY_SCORES` | (empty) | Semicolon-separated `runtime_path=intent_a:weight|intent_b:weight` entries that weight matched policy intents when ranking fallback and alternate targets |
| `RUNTIME_MODEL_OVERRIDES` | (empty) | Comma-separated `runtime_path=model` or `runtime_path=profile:model` entries; `runtime_path` may be exact or a glob |
| `RUNTIME_FALLBACK_OVERRIDES` | (empty) | Semicolon-separated `runtime_path=model_a|model_b` fallback chains; `runtime_path` may be exact or a glob |
| `PROVIDER_CAPABILITY_OVERRIDES` | (empty) | Semicolon-separated `model_or_glob=capability_a|capability_b` tags used when matching `RUNTIME_POLICY_INTENTS` |
| `FALLBACK_MODEL` | (empty) | Legacy single fallback target |
| `FALLBACK_MODELS` | (empty) | Comma-separated ordered global fallback chain |
| `FALLBACK_LLM_API_BASE` | (empty) | Optional API base override for fallback calls |

Examples:

```bash
LOCAL_RUNTIME_PATHS=chat_agent,session_consolidation,daily_briefing
RUNTIME_PROFILE_PREFERENCES=chat_agent=local|default;session_consolidation=local|default
RUNTIME_POLICY_INTENTS=chat_agent=local_first|reasoning|tool_use;session_title_generation=fast|cheap
RUNTIME_POLICY_SCORES=chat_agent=reasoning:5|tool_use:4;session_title_generation=fast:5|cheap:3
RUNTIME_MODEL_OVERRIDES=chat_agent=default:openai/gpt-4.1-mini,session_consolidation=default:openai/gpt-4o-mini
RUNTIME_FALLBACK_OVERRIDES=chat_agent=openai/gpt-4.1-mini|openai/gpt-4.1-nano;session_title_generation=openai/gpt-4o-mini|openai/gpt-4.1-mini
PROVIDER_CAPABILITY_OVERRIDES=openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;openai/gpt-4o-mini=fast|cheap;openai/gpt-4.1-mini=reasoning|tool_use
```

Pattern-based examples for dynamic runtime paths:

```bash
LOCAL_RUNTIME_PATHS=mcp_*
RUNTIME_PROFILE_PREFERENCES=mcp_*=local|default
RUNTIME_POLICY_INTENTS=mcp_*=local_first|tool_use
RUNTIME_POLICY_SCORES=mcp_*=tool_use:5
RUNTIME_MODEL_OVERRIDES=mcp_*=openai/gpt-4.1-mini,mcp_github_actions=local:ollama/coder
RUNTIME_FALLBACK_OVERRIDES=mcp_*=openai/gpt-4.1-mini|openai/gpt-4.1-nano;mcp_github_actions=openai/gpt-4o-mini|openai/gpt-4.1-mini
PROVIDER_CAPABILITY_OVERRIDES=openai/gpt-4.1-mini=reasoning|tool_use;openai/gpt-4o-mini=fast|cheap
```

Current built-in runtime paths include `chat_agent`, `onboarding_agent`, `strategist_agent`, `context_window_summary`, `session_title_generation`, `session_consolidation`, `daily_briefing`, `evening_review`, `activity_digest`, and `weekly_activity_review`.

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
| `ACTIVITY_DIGEST_HOUR` | `20` | Hour for the daily screen activity digest |
| `WEEKLY_REVIEW_HOUR` | `18` | Hour for the Sunday weekly activity review |
| `SCREEN_OBSERVATION_RETENTION_DAYS` | `90` | Days to keep screen observations before cleanup |
| `SCHEDULER_ENABLED` | `true` | Enable/disable background scheduler |

### Optional observer settings

These control the observer system and proactive behavior:

| Variable | Default | Description |
|---|---|---|
| `PROACTIVITY_LEVEL` | `3` | Proactivity scale (1–5, higher = more proactive) |
| `OBSERVER_GIT_REPO_PATH` | (empty) | Path to a git repo for commit-aware context |
| `DEEP_WORK_APPS` | (empty) | Comma-separated extra app keywords that trigger deep work state |
| `DEFAULT_CAPTURE_MODE` | `on_switch` | Screenshot frequency: `on_switch` (app changes only), `balanced` (+ every 5 min), `detailed` (+ every 1 min) |

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

## Start the local direct stack

This is the recommended path for browser development because it always loads the repo-root `.env.dev` before starting services.

```bash
./manage.sh -e dev local up
```

This starts:
- **backend** (`localhost:8004`) — FastAPI + uvicorn reload
- **frontend** (`localhost:3001`) — Vite dev server

Useful local commands:

```bash
./manage.sh -e dev local status
./manage.sh -e dev local logs backend
./manage.sh -e dev local logs frontend
./manage.sh -e dev local down
```

Defaults for the local direct stack:

| Setting | Default |
|---|---|
| Frontend port | `3001` |
| Backend port | `8004` |
| Workspace dir | `/tmp/seraph-dev-data` |
| LLM log dir | `/tmp/seraph-dev-logs` |

Override them when needed:

```bash
LOCAL_FRONTEND_PORT=3100 LOCAL_BACKEND_PORT=8100 ./manage.sh -e dev local up
```

## Start the Docker stack

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

## Optional: Symphony + Linear

Symphony is a separate automation service that watches a Linear project, creates
an isolated workspace per issue, launches Codex, and drives the issue through
implementation, PR creation, review, and merge. It does **not** replace the
normal Seraph app runtime.

Use the normal app stack when you want to run Seraph locally:

```bash
./manage.sh -e dev local up
```

Use Symphony when you want Linear tickets to be worked automatically.

### What is already in this repo

This repository already includes the repo-side Symphony configuration:

- `WORKFLOW.md` at the repo root
- repo-local skills under `.codex/skills/`
- workspace bootstrap helper at `.codex/worktree_init.sh`

The current workflow is configured for a Linear project and expects these team
statuses:

- `Todo`
- `In Progress`
- `Human Review`
- `Merging`
- `Rework`
- `Done`

The workflow also assumes feature branches merge into `develop`, so
`origin/develop` must exist before Symphony can publish PRs successfully.

### Requirements

| Requirement | Why |
|---|---|
| [Linear](https://linear.app/) workspace + project | Symphony polls this project for issues |
| `LINEAR_API_KEY` in the shell environment | Auth for Linear API access |
| [mise](https://mise.jdx.dev/) | Installs the Elixir/Erlang toolchain Symphony expects |
| Separate clone of `openai/symphony` | Symphony runs from its own repository |

### 1. Verify the workflow file

The root `WORKFLOW.md` tells Symphony which Linear project and repo workflow to
use. If your Linear project changes later, update the `project_slug` there.

### 2. Install mise

On macOS, the simplest path is:

```bash
brew install mise
hash -r
mise --version
```

### 3. Install and build Symphony

```bash
git clone https://github.com/openai/symphony
cd symphony/elixir
mise trust
mise install
mise exec -- mix setup
mise exec -- mix build
```

### 4. Start Symphony

Symphony currently requires an explicit acknowledgement flag because this
preview runs without the usual product guardrails:

```bash
cd symphony/elixir
mise exec -- ./bin/symphony /absolute/path/to/seraph/WORKFLOW.md \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails
```

For this repository:

```bash
cd symphony/elixir
mise exec -- ./bin/symphony /Users/bigcube/Desktop/repos/seraph/WORKFLOW.md \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails
```

### 5. Optional dashboard

If you want the Symphony web dashboard, pass `--port`:

```bash
cd symphony/elixir
mise exec -- ./bin/symphony /Users/bigcube/Desktop/repos/seraph/WORKFLOW.md \
  --port 4001 \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails
```

Use a free port. If `4000` is already taken by another process, either choose a
different port such as `4001` or omit `--port` entirely to run without the
dashboard.

### 6. Feed work through Linear

1. Create issues in the configured Linear project.
2. Move an issue to `Todo`.
3. Symphony will poll the project, claim the issue, move it to `In Progress`,
   create a workspace under `~/code/symphony-workspaces/seraph`, and start
   working the ticket.
4. When ready, it will open a PR against `develop` and move the issue through
   `Human Review`, `Merging`, and `Done`.

### Environment note

`LINEAR_API_KEY` must be available in the shell where Symphony starts. Example:

```bash
export LINEAR_API_KEY=lin_api_...
cd symphony/elixir
mise exec -- ./bin/symphony /Users/bigcube/Desktop/repos/seraph/WORKFLOW.md \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails
```

## Screen daemon

The native macOS daemon runs **outside Docker** and posts the active window context to the backend. This enables context-aware features like deep work detection.

:::warning
The daemon is a **standalone native process** — it is not managed by Docker. Stopping Docker services (`./manage.sh -e dev down`) does **not** stop the daemon. If the daemon was started with `--ocr`, it will continue capturing and analyzing screenshots even when the backend is down. Always stop the daemon explicitly when you're done.
:::

### Starting the daemon

```bash
./daemon/run.sh --verbose
```

To start the daemon automatically with `./manage.sh -e dev up -d`, set in `.env.dev`:

```bash
DAEMON_ENABLED=true
DAEMON_ARGS=--verbose
```

### Stopping the daemon

```bash
./manage.sh -e dev daemon stop
```

Or manually:

```bash
# Find the daemon process
ps aux | grep seraph_daemon | grep -v grep

# Kill it by PID
kill <pid>
```

### Daemon status

```bash
./manage.sh -e dev daemon status
```

### Daemon management commands

| Command | Description |
|---|---|
| `./manage.sh -e dev daemon start` | Start the daemon |
| `./manage.sh -e dev daemon stop` | Stop the daemon |
| `./manage.sh -e dev daemon status` | Check if the daemon is running |
| `./manage.sh -e dev daemon logs` | View daemon logs |

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

## Optional: Screen Activity Tracking

Screen analysis captures and analyzes screenshots **on context switch** (when you change apps), producing structured activity data: activity type, project, summary. Observations are persisted to the database and power daily/weekly activity digests.

**Capture Mode** controls screenshot frequency. Configure via the Settings UI:

| Mode | Behavior |
|---|---|
| **On Switch** (default) | Screenshot only when the active app changes |
| **Balanced** | On switch + periodic every 5 minutes within the same app |
| **Detailed** | On switch + periodic every 1 minute within the same app |

Blocked apps are never screenshotted in any mode. The daemon polls the backend for capture mode changes every 60 seconds — no restart needed.

Sensitive apps (password managers, banking, crypto wallets) are automatically blocked from screenshots.

```bash
# Apple Vision (local, free, ~200ms per capture)
./daemon/run.sh --ocr --verbose

# OpenRouter cloud analysis (Gemini 2.5 Flash Lite, structured JSON, ~$1.30/mo)
OPENROUTER_API_KEY=sk-or-... ./daemon/run.sh --ocr --ocr-provider openrouter --verbose

# With custom app blocklist
./daemon/run.sh --ocr --ocr-provider openrouter --blocklist-file ~/blocklist.json --verbose
```

| Provider | Pros | Cons |
|---|---|---|
| `apple-vision` (default) | Free, offline, fast (~200ms) | Text-only, no layout understanding |
| `openrouter` | Structured JSON output, understands layout | Requires API key, small cost |

### Screen analysis options

| Flag | Default | Description |
|---|---|---|
| `--ocr` | off | Enable screenshot analysis on context switch |
| `--ocr-provider` | `apple-vision` | `apple-vision` (local) or `openrouter` (cloud, structured) |
| `--ocr-model` | `google/gemini-2.5-flash-lite` | Model for OpenRouter provider |
| `--openrouter-api-key` | `$OPENROUTER_API_KEY` | API key for OpenRouter provider |
| `--blocklist-file` | (none) | Path to JSON blocklist config (extends built-in defaults) |

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

## Retired: Village Map Editor

The standalone village map editor is retired and no longer part of the supported Seraph product surface. The active repo no longer ships the `editor/` app.

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
| `mise: command not found` | `mise` is not installed | Install it with `brew install mise`, then rerun the Symphony setup |
| Symphony refuses to start and asks for an acknowledgement flag | Missing required preview-safety CLI flag | Start with `--i-understand-that-this-will-be-running-without-the-usual-guardrails` |
| Symphony fails with `:eaddrinuse` when using `--port` | The requested dashboard port is already in use | Use a different port such as `--port 4001`, or omit `--port` |
| Daemon: "No window title" / title is always None | Accessibility permission not granted | Grant in **System Settings > Privacy & Security > Accessibility** |
| Daemon: "Backend not reachable" | Backend not running or wrong URL | Start backend first; check `--url` flag |
| Daemon still capturing after stopping services | Daemon runs outside Docker and isn't stopped by `docker compose down` | Run `./manage.sh -e dev daemon stop` or `kill <pid>` |
| Daemon exits immediately | Missing PyObjC | Run `cd daemon && uv pip install -r requirements.txt` |
| `Failed to connect to MCP server` | Things3 MCP not running | Check: `curl http://localhost:9100/mcp`; ensure LaunchAgent is loaded |
| `unable to open database file` (Things3) | Full Disk Access not granted to `uvx` | Grant FDA, then restart the things-mcp service |
| Google Calendar: "credentials not found" | Missing `google_credentials.json` | Place OAuth credentials at `backend/config/google_credentials.json` |
| Google Calendar: OAuth flow fails | Container can't open browser | Copy the auth URL from container logs and open it in your host browser |
