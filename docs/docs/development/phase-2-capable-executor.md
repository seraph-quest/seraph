---
sidebar_position: 2
---

# Phase 2 — Capable Executor

**Goal**: Seraph can do real things — browse, execute code, search the web.

**Status**: Implemented

---

## 2.1 Plugin System

**Files**:
```
backend/src/plugins/
  __init__.py
  loader.py          # Auto-discovery from tools/ directory
  registry.py        # Tool metadata registry (name, village_position, animation)
```

**Plugin loader** (`loader.py`):
- Scans `src/tools/*.py` for `@tool` decorated functions and `Tool` subclasses
- Auto-discovers tools — no more hardcoded list in `factory.py`
- Supports hot-reload via `reload_tools()`

**Tool registry** (`registry.py`):
- Maps tool names to metadata for the `GET /api/tools` endpoint

**API endpoint**: `GET /api/tools` — returns available tools with metadata

**Modified**: `backend/src/agent/factory.py`
- `get_tools()` now calls `discover_tools()` from plugin loader + `mcp_manager.get_tools()` for MCP tools
- No manual imports — all tools auto-discovered
- MCP tools (e.g. Things3) are merged in at runtime if configured

---

## 2.2 Shell Execution Tool

**Docker service**: `snekbox` sidecar in `docker-compose.dev.yaml`:
```yaml
sandbox-dev:
  image: ghcr.io/python-discord/snekbox:latest
  ipc: none
  networks:
    - seraph-network-dev
```

**File**: `backend/src/tools/shell_tool.py`
- `shell_execute(code: str, language: str = "python") -> str`
- Calls snekbox HTTP API (`http://sandbox:8060/eval`)
- Returns stdout/stderr, handles timeouts (35s default)
- Sandboxed — no network access, limited resources

**Settings**:
- `sandbox_url`: URL of the snekbox service (default `http://sandbox:8060`)
- `sandbox_timeout`: Max execution time in seconds (default 35)

---

## 2.3 Browser Automation Tool

**Dockerfile additions**:
```dockerfile
# Playwright/Chromium system dependencies
RUN apt install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2

RUN uv run playwright install chromium
```

**Docker Compose**: `shm_size: "1gb"` on backend service

**Package**: `playwright>=1.49.0`

**File**: `backend/src/tools/browser_tool.py`
- `browse_webpage(url: str, action: str = "extract") -> str`
- Actions:
  - `extract` (default): Readable text content, scripts/nav/footer stripped
  - `html`: Raw HTML source
  - `screenshot`: Base64-encoded PNG screenshot
- Headless Chromium via Playwright async API
- 30s timeout, single-page context per call
- Real browser user agent for JavaScript-rendered pages

---

## 2.4 Calendar & Email (Removed)

> Calendar and email tools were originally planned here but were removed in favor of MCP server integrations. Calendar data is now an observer-only source (`backend/src/observer/sources/calendar_source.py`) that feeds the strategist context. Email integration can be added via MCP servers (e.g., Gmail MCP).

---

## 2.5 Village Expansion

**Modified**: `frontend/src/game/scenes/VillageScene.ts`
- Buildings dynamically parsed from Tiled map JSON (no hardcoded buildings)
- Day/night cycle with procedural lighting
- Extended wandering waypoints across village areas

**Modified**: `frontend/src/config/constants.ts`
- Tool name constants for animation mapping (e.g., `SHELL_EXECUTE`, `BROWSE_WEBPAGE`, plus Phase 1 soul/goal tools)

**Modified**: `frontend/src/lib/animationStateMachine.ts`
- Tool detection triggers casting animation with magic effect overlay

---

## Implementation Order (as executed)

1. Plugin system (loader, registry) — all tools benefit from auto-discovery
2. Shell execution tool + snekbox sidecar — easiest to verify
3. Browser automation tool (Playwright) — high capability unlock
4. Village expansion (buildings, animations) — visual polish
5. Settings + build verification — ensure everything compiles

## Verification Checklist

- [x] `shell_execute("print('hello')")` returns "hello" via snekbox
- [x] `browse_webpage("https://example.com")` returns page content
- [x] Drop a new `.py` tool file in `src/tools/`, verify auto-discovered
- [x] New tools trigger correct village casting animations
- [x] 12 tools auto-discovered by plugin loader
- [x] 18 routes registered (including `GET /api/tools`)
- [x] TypeScript compiles clean
- [x] Lock file updated

## All 12 Tools (auto-discovered)

| Tool | File |
|------|------|
| `web_search` | web_search_tool.py |
| `read_file` | filesystem_tool.py |
| `write_file` | filesystem_tool.py |
| `fill_template` | template_tool.py |
| `view_soul` | soul_tool.py |
| `update_soul` | soul_tool.py |
| `create_goal` | goal_tools.py |
| `update_goal` | goal_tools.py |
| `get_goals` | goal_tools.py |
| `get_goal_progress` | goal_tools.py |
| `shell_execute` | shell_tool.py |
| `browse_webpage` | browser_tool.py |
