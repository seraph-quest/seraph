---
sidebar_position: 2
---

# Phase 2 — Capable Executor

**Goal**: Seraph can do real things — browse, execute code, manage calendar/email.

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
- Maps tool names to village metadata:
  ```python
  TOOL_METADATA = {
      "web_search":             {"building": "house-1",  "pixel_x": 192, "pixel_y": 280, "animation": "at-well"},
      "read_file":              {"building": "house-2",  "pixel_x": 832, "pixel_y": 280, "animation": "at-signpost"},
      "shell_execute":          {"building": "forge",    "pixel_x": 384, "pixel_y": 320, "animation": "at-forge"},
      "browse_webpage":         {"building": "tower",    "pixel_x": 640, "pixel_y": 200, "animation": "at-tower"},
      "get_calendar_events":    {"building": "clock",    "pixel_x": 576, "pixel_y": 340, "animation": "at-clock"},
      "create_calendar_event":  {"building": "clock",    "pixel_x": 576, "pixel_y": 340, "animation": "at-clock"},
      "read_emails":            {"building": "mailbox",  "pixel_x": 128, "pixel_y": 340, "animation": "at-mailbox"},
      "send_email":             {"building": "mailbox",  "pixel_x": 128, "pixel_y": 340, "animation": "at-mailbox"},
      # ... plus all Phase 1 tools
  }
  ```

**API endpoint**: `GET /api/tools` — returns available tools with village metadata

**Modified**: `backend/src/agent/factory.py`
- `get_tools()` now calls `discover_tools()` from plugin loader
- No manual imports — all tools auto-discovered

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

**Village element**: Forge building at position (384, 320)

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

**Village element**: Tower building at position (640, 200)

---

## 2.4 Calendar Integration

**Packages**: `gcsa>=2.6.0`, `google-auth-oauthlib>=1.2.0`

**File**: `backend/src/tools/calendar_tool.py`
- `get_calendar_events(days_ahead: int = 7) -> str` — Formatted list of upcoming events
- `create_calendar_event(title, start_time, end_time, description) -> str` — Create new event
- Uses gcsa (Google Calendar Simple API)
- OAuth credentials at `/app/config/google_credentials.json`
- Token stored at `/app/data/google_calendar_token.json`

**Village element**: Town clock at position (576, 340)

**Settings**:
- `google_credentials_path`: Path to OAuth credentials JSON
- `google_calendar_token_path`: Path to stored OAuth token

---

## 2.5 Email Integration

**Package**: `simplegmail>=4.1.0`

**File**: `backend/src/tools/email_tool.py`
- `read_emails(query: str = "", max_results: int = 10) -> str` — Search/list inbox
- `send_email(to, subject, body) -> str` — Send an email
- Shares Google OAuth credentials with calendar
- Separate token: `/app/data/google_gmail_token.json`

**Village element**: Mailbox at position (128, 340)

---

## 2.6 Village Expansion

**Modified**: `frontend/src/game/scenes/StudyScene.ts`
- 4 new buildings added to village: Forge, Tower, Clock, Mailbox
- Day/night texture variants with procedural fallback textures
- Props placement system for smaller objects (clock, mailbox)
- Extended wandering waypoints to include new building areas

**Modified**: `frontend/src/game/objects/AgentSprite.ts`
- 4 new animation states:
  - `at-forge`: Facing down, medium cycle (hammering motion)
  - `at-tower`: Facing up, slow cycle (looking/observing)
  - `at-clock`: Facing right, slow cycle (checking time)
  - `at-mailbox`: Facing left, slow cycle (reading letter)

**Modified**: `frontend/src/config/constants.ts`
- New `TOOL_NAMES`: `SHELL_EXECUTE`, `BROWSE_WEBPAGE`, `GET_CALENDAR_EVENTS`, `CREATE_CALENDAR_EVENT`, `READ_EMAILS`, `SEND_EMAIL` (plus Phase 1 soul/goal tools)
- New `POSITIONS`: forge (35%), tower (60%), clock (55%), mailbox (10%)
- New `SCENE.POSITIONS`: forge, tower, clock, mailbox with pixel coords
- Extended wandering waypoints

**Modified**: `frontend/src/lib/animationStateMachine.ts`
- Full `TOOL_TARGETS` mapping for all 16 tools to village positions and animations

---

## Implementation Order (as executed)

1. Plugin system (loader, registry) — all tools benefit from auto-discovery
2. Shell execution tool + snekbox sidecar — easiest to verify
3. Browser automation tool (Playwright) — high capability unlock
4. Calendar integration (gcsa + OAuth) — needed for Phase 3 context
5. Email integration (simplegmail) — shares OAuth with calendar
6. Village expansion (new buildings, animations) — visual polish
7. Settings + build verification — ensure everything compiles

## Verification Checklist

- [x] `shell_execute("print('hello')")` returns "hello" via snekbox
- [x] `browse_webpage("https://example.com")` returns page content
- [x] `get_calendar_events()` returns events (requires OAuth setup)
- [x] `read_emails()` returns inbox content (requires OAuth setup)
- [x] Drop a new `.py` tool file in `src/tools/`, verify auto-discovered
- [x] New tools trigger correct village building animations
- [x] 16 tools auto-discovered by plugin loader
- [x] 19 routes registered (including `GET /api/tools`)
- [x] TypeScript compiles clean
- [x] Lock file updated

## All 16 Tools (auto-discovered)

| Tool | File | Village Building | Animation |
|------|------|-----------------|-----------|
| `web_search` | web_search_tool.py | house-1 | at-well |
| `read_file` | filesystem_tool.py | house-2 | at-signpost |
| `write_file` | filesystem_tool.py | house-2 | at-signpost |
| `fill_template` | template_tool.py | church | at-bench |
| `view_soul` | soul_tool.py | church | at-bench |
| `update_soul` | soul_tool.py | church | at-bench |
| `create_goal` | goal_tools.py | church | at-bench |
| `update_goal` | goal_tools.py | church | at-bench |
| `get_goals` | goal_tools.py | church | at-bench |
| `get_goal_progress` | goal_tools.py | church | at-bench |
| `shell_execute` | shell_tool.py | forge | at-forge |
| `browse_webpage` | browser_tool.py | tower | at-tower |
| `get_calendar_events` | calendar_tool.py | clock | at-clock |
| `create_calendar_event` | calendar_tool.py | clock | at-clock |
| `read_emails` | email_tool.py | mailbox | at-mailbox |
| `send_email` | email_tool.py | mailbox | at-mailbox |
