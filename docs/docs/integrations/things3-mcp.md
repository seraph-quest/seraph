---
sidebar_position: 1
---

# Things3 MCP Integration

Seraph can read and create tasks in [Things3](https://culturedcode.com/things/) via the [things-mcp](https://github.com/hald/things-mcp) MCP server. This gives the agent access to 22 Things3 tools covering inbox, today, upcoming, projects, areas, tags, search, and task creation/updates.

## Architecture

The things-mcp server runs on the **host Mac** (not inside Docker) because task creation uses the Things URL scheme via `osascript`/`open`, which requires macOS.

```
Host Mac:  uvx things-mcp  (HTTP transport, port 9100)
              |  Streamable HTTP
Docker:    backend-dev  ->  MCPManager  ->  smolagents MCPClient
              |
           ToolCallingAgent (existing @tool functions + MCP tools)
```

The backend connects to the host via `host.docker.internal:9100` (Docker Desktop for Mac provides this hostname automatically).

## Prerequisites

- **macOS** with Things3 installed
- **Python 3.10+** (for `uvx`)
- **Docker Desktop for Mac** (provides `host.docker.internal`)

## Setup

### 1. Install and start things-mcp on the host

```bash
THINGS_MCP_TRANSPORT=http THINGS_MCP_PORT=9100 uvx things-mcp
```

This starts the MCP server on port 9100 with Streamable HTTP transport. Leave this terminal running.

### 2. Configure the backend

The `THINGS_MCP_URL` environment variable is already set in `.env.dev`:

```
THINGS_MCP_URL=http://host.docker.internal:9100/mcp
```

To disable Things3 integration, set it to an empty string:

```
THINGS_MCP_URL=
```

### 3. Start Seraph

```bash
./manage.sh -e dev up -d
```

### 4. Verify

Check the backend logs for a successful MCP connection:

```bash
./manage.sh -e dev logs -f backend-dev
```

Look for:

```
Connected to MCP server: 22 tools loaded
```

If things-mcp is not running or unreachable, you'll see a warning instead — the backend still starts, just without Things3 tools:

```
WARNING - Failed to connect to MCP server at http://host.docker.internal:9100/mcp
```

## Usage

Once connected, ask Seraph anything about your Things3 tasks:

- "What's in my Things inbox?"
- "What do I have due today?"
- "Add a todo called 'Review PR #42' due tomorrow in my Work project"
- "Show me all tasks tagged 'urgent'"
- "What did I complete this week?"

The village avatar walks to the church/bench area (tasks & goals station) when using Things3 tools.

## Available Tools

All 22 tools from things-mcp are exposed to the agent:

| Tool | Description |
|------|-------------|
| `get_inbox` | Get tasks in the inbox |
| `get_today` | Get tasks due today |
| `get_upcoming` | Get upcoming scheduled tasks |
| `get_anytime` | Get anytime tasks |
| `get_someday` | Get someday tasks |
| `get_logbook` | Get completed tasks |
| `get_trash` | Get trashed tasks |
| `get_todos` | Get todos with filters |
| `get_projects` | Get all projects |
| `get_areas` | Get all areas |
| `get_tags` | Get all tags |
| `get_tagged_items` | Get items with a specific tag |
| `get_headings` | Get headings in a project/area |
| `search_todos` | Search todos by text |
| `search_advanced` | Advanced search with filters |
| `get_recent` | Get recently modified items |
| `add_todo` | Create a new todo |
| `add_project` | Create a new project |
| `update_todo` | Update an existing todo |
| `update_project` | Update an existing project |
| `show_item` | Open a Things item by ID |
| `search_items` | Search via Things URL scheme |

## Village Animation

All Things3 tools map to the **church/bench** station (the tasks & goals area):

- **Building**: church
- **Position**: (512, 240)
- **Animation**: `at-bench`

## Implementation Details

### Backend

| File | Role |
|------|------|
| `config/settings.py` | `things_mcp_url` setting (empty = disabled) |
| `src/tools/mcp_manager.py` | `MCPManager` — connects via `smolagents.MCPClient`, exposes `get_tools()` |
| `src/app.py` | Connects MCP in FastAPI lifespan (startup/shutdown) |
| `src/agent/factory.py` | Merges `discover_tools()` + `mcp_manager.get_tools()` |
| `src/plugins/registry.py` | Village animation metadata for all 22 Things3 tools |

### Frontend

| File | Role |
|------|------|
| `src/config/constants.ts` | `TOOL_NAMES` entries for Things3 tools |
| `src/lib/animationStateMachine.ts` | `TOOL_TARGETS` mapping to church/bench position |
| `src/lib/toolParser.ts` | Auto-detects Things3 tools (via `KNOWN_TOOLS` set from `TOOL_NAMES`) |

## Troubleshooting

**"Failed to connect to MCP server"**
- Ensure things-mcp is running on the host: `THINGS_MCP_TRANSPORT=http THINGS_MCP_PORT=9100 uvx things-mcp`
- Verify port 9100 is accessible: `curl http://localhost:9100/mcp`
- Check that Docker Desktop is running (provides `host.docker.internal`)

**Things3 tools not appearing in agent**
- Check `THINGS_MCP_URL` is set in `.env.dev`
- Restart the backend: `./manage.sh -e dev build && ./manage.sh -e dev up -d`

**Task creation not working**
- Things3 must be open on the Mac (URL scheme requires the app to be running)
- Check things-mcp terminal for errors

**Agent doesn't walk to church when using Things tools**
- Clear browser cache and reload — the frontend tool names may be stale
