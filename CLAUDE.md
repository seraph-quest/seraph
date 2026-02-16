# Seraph AI Agent - Project Context

## Contributing Guidelines
- Always include tests in a PR where they bring value. If you add or modify functionality, add corresponding tests.
- Always update CLAUDE.md documentation when changes in a PR affect the project architecture, API surface, new modules, or design decisions.

## Project Overview
Seraph is an AI agent with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar casts magic effects when using tools while the user chats via an RPG-style dialog box. The agent has persistent identity (soul file), long-term memory (vector embeddings), a hierarchical goal/quest system, and plug-and-play MCP server integration.

## Architecture

### Frontend (`frontend/`)
- **Stack**: React 19 + Vite 6 + TypeScript 5.6 + Tailwind CSS 3 + Zustand 5 + Phaser 3.90
- **Entry**: `src/main.tsx` → `src/App.tsx`
- **Key files**:
  - `src/game/main.ts` - Phaser game factory (`StartGame(parent)`) called by PhaserGame
  - `src/game/PhaserGame.tsx` - React wrapper for Phaser instance
  - `src/game/scenes/VillageScene.ts` - Village scene (dynamic Tiled JSON map, buildings with interiors, forest, day/night cycle, magic effects)
  - `src/game/objects/AgentSprite.ts` - Phaser sprite for Seraph avatar
  - `src/game/objects/UserSprite.ts` - Phaser sprite for user avatar
  - `src/game/objects/NpcSprite.ts` - NPC sprite with wandering behavior, supports character + enemy types
  - `src/game/objects/SpeechBubble.ts` - Phaser speech bubble
  - `src/game/objects/MagicEffect.ts` - Animated spell overlay (pool-cycled, spawned during tool use, faded on idle)
  - `src/game/lib/Pathfinder.ts` - A* pathfinding wrapper (easystarjs); diagonal movement, path simplification, grid swap for interiors
  - `src/game/lib/mapParsers.ts` - `buildMagicEffectPool()` — parses magic effects from Tiled map JSON custom properties
  - `src/game/EventBus.ts` - Bridges Phaser events to React
  - `src/stores/chatStore.ts` - Zustand store (messages, sessions, connectionStatus, agentVisual, isAgentBusy, sessionId, onboarding, ambient, ambientTooltip, chatMaximized, chatPanelOpen, questPanelOpen, settingsPanelOpen, toolRegistry, magicEffectPoolSize, debugWalkability, session persistence via localStorage)
  - `src/stores/questStore.ts` - Zustand store (goals, goalTree, dashboard, loading)
  - `src/hooks/useWebSocket.ts` - Native WS connection to `ws://localhost:8004/ws/chat`, reconnect, ping, message dispatch
  - `src/hooks/useAgentAnimation.ts` - Walk-then-act state machine with timers
  - `src/hooks/useKeyboardShortcuts.ts` - Keyboard shortcut handler (Shift+C/Q/S, Escape)
  - `src/lib/toolParser.ts` - Regex detection of tool names from step content (6 patterns + fallback); uses dynamic tool registry from API with static fallback
  - `src/lib/animationStateMachine.ts` - Tool → casting animation mapping; triggers magic effects on tool use
  - `src/config/constants.ts` - Scene dimensions, tool names, default agent position
  - `src/components/chat/` - ChatPanel, ChatSidebar, SessionList, MessageList, MessageBubble, ChatInput, ThinkingIndicator, DialogFrame (RPG frame with optional maximize/close buttons)
  - `src/components/quest/` - QuestPanel (search/filter by title, level, domain), GoalTree, GoalForm, DomainStats
  - `src/components/settings/InterruptionModeToggle.tsx` - Focus/Balanced/Active mode toggle for proactive message delivery
  - `src/components/settings/DaemonStatus.tsx` - Screen observer daemon status indicator (polls `/api/observer/daemon-status` every 10s, shows connected/disconnected + active window)
  - `src/components/SettingsPanel.tsx` - Standalone settings overlay panel (restart onboarding, interruption mode, daemon status, skills management, Discover catalog, MCP server management UI with 4-state status + inline token config, version)
  - `src/components/HudButtons.tsx` - Floating RPG-styled buttons to reopen closed Chat/Quest/Settings panels + ambient state indicator dot (color-coded, pulsing)
  - `src/index.css` - CRT scanlines/vignette, pixel borders, RPG frame, chat-overlay maximized state

### Village Map Editor (`editor/`)
- **Stack**: React 19 + Vite 6 + TypeScript 5.6 + Tailwind CSS 3 + Zustand 5
- **Entry**: `src/main.tsx` → `src/App.tsx`
- **Layout**: 3-column (ToolBar | MapCanvas | panels: LayerPanel, TilesetPanel, ObjectPanel, BuildingPanel)
- **Key directories**:
  - `src/components/` — MapCanvas, ToolBar, LayerPanel, TilesetPanel, ObjectPanel, NPCBrowser, AnimationDefiner, MenuBar, BuildingPanel, Tooltip
  - `src/stores/editorStore.ts` — Map data (`layers: CellStack[][]`), editor state, viewport, undo/redo (100 levels)
  - `src/stores/tilesetStore.ts` — Tileset loading, animation groups/lookup, walkability flags, sprite cache, recent selections
  - `src/hooks/` — useCanvasInteraction, useKeyboardShortcuts
  - `src/lib/` — canvas-renderer, flood-fill, map-io (Tiled JSON 1.10 import/export), sprite-registry, stamps, tileset-loader, undo
  - `src/types/editor.ts` — `CellStack` (`number[]`), `BuildingDef`, `BuildingFloor`, `BuildingPortal`, `TileAnimationGroup`
  - `src/types/map.ts` — Tiled JSON schema types
- **Features**:
  - 6 tools: Hand, Brush, Eraser, Fill, Object, Walkability + 3 view toggles
  - 5 tile layers (ground, terrain, buildings, decorations, treetops) + 1 object layer
  - 33 tilesets (16px tiles) organized by category (Village, World, Dungeon, Interior, Animations)
  - Tile stacking: `CellStack = number[]` — multiple tiles per cell per layer, serialized as Tiled sublayers (`layer__N`)
  - Tile animations: define frame sequences per anchor tile, optional `isMagicEffect` flag for spell pool
  - Object placement + NPC browser (212 characters + 53 enemies)
  - Building interiors: multi-floor editor with portal placement (entry, stairs_up, stairs_down)
  - Per-tile walkability painting, persisted per tileset
  - Undo/redo (100 levels)
- **Map I/O**: Tiled JSON 1.10 compatible; sublayer serialization for stacked cells; custom properties for buildings, magic effects, spawn points
- **Assets**: Proxied from `../frontend/public/assets/` (tilesets, spritesheets shared with game)
- **Session persistence**: Zustand persist middleware → localStorage
- **Quick start**: `cd editor && npm install && npm run dev` → `http://localhost:3001`
- See `editor/README.md` for detailed documentation

### Backend (`backend/`)
- **Stack**: Python 3.12, FastAPI, uvicorn, smolagents, LiteLLM (OpenRouter)
- **Entry**: `main.py` → `src/app.py` (factory pattern with lifespan: init DB, ensure soul file)
- **API routes** (`src/api/router.py`):
  - `src/api/chat.py` — `POST /api/chat` (REST chat, secondary to WS)
  - `src/api/ws.py` — `WS /ws/chat` (primary streaming interface)
  - `src/api/sessions.py` — `GET /api/sessions`, `GET/PATCH/DELETE /api/sessions/{id}`, `GET /api/sessions/{id}/messages`
  - `src/api/goals.py` — `GET /api/goals`, `GET /api/goals/tree`, `GET /api/goals/dashboard`, `POST /api/goals`, `PATCH/DELETE /api/goals/{id}`
  - `src/api/profile.py` — `GET /api/user/profile`, `POST /api/user/onboarding/skip`, `POST /api/user/onboarding/restart`
  - `src/api/tools.py` — `GET /api/tools` (returns building metadata per tool for dynamic frontend registration)
  - `src/api/mcp.py` — `GET /api/mcp/servers`, `POST /api/mcp/servers`, `PUT /api/mcp/servers/{name}`, `DELETE /api/mcp/servers/{name}`, `POST /api/mcp/servers/{name}/test`, `POST /api/mcp/servers/{name}/token`
  - `src/api/skills.py` — `GET /api/skills`, `PUT /api/skills/{name}`, `POST /api/skills/reload`
  - `src/api/catalog.py` — `GET /api/catalog` (browse catalog with install status), `POST /api/catalog/install/{name}` (install skill or MCP server from catalog)
  - `src/api/observer.py` — `GET /api/observer/state`, `POST /api/observer/context` (receives daemon screen data), `GET /api/observer/daemon-status` (daemon heartbeat connectivity check), `POST /api/observer/refresh` (debug)
  - `src/api/settings.py` — `GET /api/settings/interruption-mode`, `PUT /api/settings/interruption-mode`
  - `/health` — health check (defined in `src/app.py`)
- **Tools** (`src/tools/`):
  - Phase 1: `web_search`, `read_file`, `write_file`, `fill_template`, `view_soul`, `update_soul`, `create_goal`, `update_goal`, `get_goals`, `get_goal_progress`
  - Phase 2: `shell_execute`, `browse_webpage`
  - MCP: `src/tools/mcp_manager.py` — plug-and-play MCP manager; loads server config from `data/mcp-servers.json`, connects to enabled servers via `smolagents.MCPClient`. Supports runtime add/remove/toggle via API. Config file is auto-persisted on mutations. Tracks granular status per server (`connected`/`disconnected`/`auth_required`/`error`). `set_token()` stores auth tokens directly in config and reconnects.
- **Tool Discovery** (`src/plugins/`):
  - `loader.py` — `discover_tools()` auto-discovers all `@tool`-decorated functions from `src/tools/`
  - `registry.py` — `get_tool_metadata()` returns tool descriptions for frontend registration
  - `factory.py` uses `discover_tools()` + `mcp_manager.get_tools()` to assemble the full tool set
- **MCP Configuration** (`data/mcp-servers.json`):
  - JSON config with `mcpServers` object: `{name: {url, enabled, description?, headers?, auth_hint?}}`
  - `data/mcp-servers.example.json` committed to repo as reference
  - `src/defaults/mcp-servers.default.json` — seed config template; copied to workspace on first run if no config exists
  - Loaded on app startup from `settings.workspace_dir + "/mcp-servers.json"`
  - First run seeds from `mcp-servers.default.json` (2 entries: `http-request`, `github` — both `enabled: false`)
  - **Auth headers**: Servers can include `"headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}` — env var patterns `${VAR}` are resolved at connect time via `_resolve_env_vars()`. Raw templates stored in config, resolved values never persisted.
  - **Auth token UI**: `POST /api/mcp/servers/{name}/token` stores a `Bearer` token directly in headers config. Frontend `TokenConfigForm` provides inline token setup with save-and-test flow. Unresolved `${VAR}` patterns detected at connect time → status set to `auth_required`.
  - **Server status**: Each server tracks `status` (`connected`|`disconnected`|`auth_required`|`error`), `status_message`, and `auth_hint` (user guidance from config). Frontend shows 4-state indicator: gray (disabled), green (connected), yellow pulsing (auth needed), red (error).
  - No config file and no default = no MCP tools, no errors
- **Skills** (`src/skills/`): SKILL.md plugin ecosystem — zero-code markdown plugins
  - `loader.py` — `Skill` dataclass, YAML frontmatter parser, directory scanner (`load_skills()`)
  - `manager.py` — Singleton `skill_manager`; runtime enable/disable, tool gating, config persistence via `data/skills-config.json`
  - Skills are `.md` files in `data/skills/` with YAML frontmatter (`name`, `description`, `requires.tools`, `user_invocable`, `enabled`) and a markdown body of instructions
  - On agent creation, active skill instructions are injected into the system prompt (after soul/memory, before conversation history)
  - Tool gating: skills with `requires.tools` are only loaded if all required tools are available
  - Runtime state: `GET /api/skills` lists all, `PUT /api/skills/{name}` toggles, `POST /api/skills/reload` re-scans directory
  - Disabled skills tracked in `data/skills-config.json`, survives reload
  - 8 bundled skills in `src/defaults/skills/`: `daily-standup.md`, `code-review.md`, `goal-reflection.md`, `weekly-planner.md`, `morning-intention.md`, `evening-journal.md`, `moltbook.md` (requires `http_request`), `web-briefing.md` (requires `http_request` + `web_search`)
  - Default skills are seeded to workspace `skills/` directory on first run (existing files never overwritten)
  - MCP-dependent skills (`moltbook`, `web-briefing`) auto-deactivate via tool gating when `http_request` tool unavailable
- **Memory** (`src/memory/`):
  - `soul.py` — Persistent identity file (markdown in workspace)
  - `vector_store.py` — LanceDB vector store for long-term memory search
  - `embedder.py` — Sentence transformers for memory embeddings
  - `consolidator.py` — Background task extracting memories after each conversation
- **Goals** (`src/goals/repository.py`): SQLModel-based hierarchical goal CRUD
- **Agent** (`src/agent/`):
  - `factory.py` — Creates flat agent with all tools + context (soul, memory, observer, skills); also creates orchestrator with managed specialists when delegation is enabled via `build_agent()`
  - `specialists.py` — Specialist agent factories for delegation mode (memory_keeper, goal_planner, web_researcher, file_worker, MCP specialists); tool domain mapping; `build_all_specialists()`
  - `onboarding.py` — Specialized onboarding agent (limited to soul/goal tools, 5-point discovery)
  - `strategist.py` — Strategist agent factory (restricted to `view_soul`, `get_goals`, `get_goal_progress`, temp=0.4, max_steps=5) + `StrategistDecision` dataclass + `parse_strategist_response()` JSON parser
  - `context_window.py` — Token-aware context window builder (keep first N + last M, summarize middle; reads defaults from settings)
  - `session.py` — Async session manager (SQLite-backed)
- **Database** (`src/db/`): SQLModel + aiosqlite. Models: `UserProfile` (includes `interruption_mode`), `Session`, `Message`, `Goal`, `Memory`, `QueuedInsight`. Enums: `GoalLevel`, `GoalDomain`, `GoalStatus`, `MemoryCategory`
- **Scheduler** (`src/scheduler/`):
  - `engine.py` — APScheduler setup, job registration on app lifespan
  - `connection_manager.py` — WebSocket broadcast manager (`ws_manager`)
  - `jobs/memory_consolidation.py` — Consolidate recent sessions into long-term memory (every 30 min)
  - `jobs/goal_check.py` — Check goal progress, detect stalled goals (every 4 hours)
  - `jobs/calendar_scan.py` — Poll calendar for upcoming events (every 15 min)
  - `jobs/strategist_tick.py` — Periodic strategic reasoning via restricted agent; refreshes context, runs strategist agent, parses decision, routes through `deliver_or_queue` (every 15 min)
  - `jobs/daily_briefing.py` — Morning briefing via LiteLLM; gathers soul/calendar/goals/memories, delivers with `is_scheduled=True` (cron 8 AM)
  - `jobs/evening_review.py` — Evening reflection via LiteLLM; counts today's messages/completed goals, delivers with `is_scheduled=True` (cron 9 PM)
- **Observer** (`src/observer/`):
  - `context.py` — `CurrentContext` dataclass (time, calendar, git, goals, user state, screen, attention budget, `last_daemon_post` heartbeat) + `to_prompt_block()`
  - `manager.py` — `ContextManager` singleton; refreshes all sources, derives user state, detects state transitions, delivers queued bundles
  - `user_state.py` — `UserStateMachine` with 6 states (available/deep_work/in_meeting/transitioning/away/winding_down), `InterruptionMode` enum (focus/balanced/active), `DeliveryDecision` enum (deliver/queue/drop), delivery gate (`should_deliver()`), attention budget management
  - `delivery.py` — `deliver_or_queue()` routes proactive messages through the attention guardian; `deliver_queued_bundle()` drains queue on state transitions
  - `insight_queue.py` — DB-backed queue for insights held during blocked states (24h expiry)
  - `user_state.py` also includes `_DEEP_WORK_APPS` — IDE/terminal app names that trigger `deep_work` state when detected in `active_window` (priority between calendar and transition detection)
  - `sources/` — `time_source.py`, `calendar_source.py`, `git_source.py`, `goal_source.py`, `screen_source.py` (daemon API contract + implementation status)
- **CORS**: Allows `localhost:3000` and `localhost:5173`

### Native Daemon (`daemon/`)
- **Stack**: Python 3.12, PyObjC, httpx
- **Entry**: `seraph_daemon.py` — runs natively on macOS (outside Docker)
- **Dual-loop architecture**: window polling (every 5s) + optional OCR loop (every 30s, `--ocr` flag)
- Polls frontmost app name (`NSWorkspace`, no permission) + window title (AppleScript, Accessibility permission) + idle seconds (`Quartz.CGEventSourceSecondsSinceLastEventType`, no permission)
- **Change detection**: skips POST if `active_window` unchanged since last POST; OCR loop also skips if extracted text unchanged
- **Idle detection**: skips POST if no user input for `--idle-timeout` seconds (default 300); both loops respect idle
- POSTs to `POST /api/observer/context` — window loop sends `{"active_window": "App — Title"}`, OCR loop sends `{"screen_context": "extracted text"}` (partial updates, neither clobbers the other)
- **CLI args**: `--url` (default `http://localhost:8004`), `--interval` (default `5`), `--idle-timeout` (default `300`), `--verbose`
- **OCR CLI args** (opt-in): `--ocr` (enable), `--ocr-provider` (`apple-vision` | `openrouter`), `--ocr-interval` (default `30`), `--ocr-model` (default `google/gemini-2.0-flash-lite-001`), `--openrouter-api-key` (or `OPENROUTER_API_KEY` env var)
- **OCR providers** (`daemon/ocr/`): pluggable provider pattern — `base.py` (ABC + `OCRResult` dataclass), `screenshot.py` (Quartz screenshot capture), `apple_vision.py` (local VNRecognizeTextRequest, ~200ms), `openrouter.py` (cloud vision model, ~$0.09/mo)
- **Permissions**: Accessibility (window titles, one-time grant) + Screen Recording (only for `--ocr`, Sequoia monthly nag)
- **Privacy**: screenshots exist only as in-memory bytes, never written to disk
- Graceful shutdown on SIGINT/SIGTERM, handles backend-down with warning + retry
- Research on future upgrades (OCR, VLMs, cloud APIs): `docs/docs/development/screen-daemon-research.md`
- **Quick start**: `./daemon/run.sh` (creates venv, installs deps, starts daemon)
- **Quick start with OCR**: `./daemon/run.sh --ocr --verbose`
- **Manual run**: `cd daemon && uv pip install -r requirements.txt && uv run python seraph_daemon.py --verbose`

### HTTP MCP Server (`mcp-servers/http-request/`)
- **Stack**: Python 3.12, FastMCP, httpx
- **Entry**: `server.py` — single `http_request` tool via FastMCP (port 9200, streamable-http transport)
- **Tool**: `http_request(method, url, headers?, body?, timeout?)` → `{status, headers, body}`
- **Methods**: GET, POST, PUT, DELETE, PATCH, HEAD
- **Security**: `_is_internal_url()` blocks localhost, private IPs (10.x, 172.16.x, 192.168.x), .local/.internal hostnames
- **Timeout**: clamped 1-60s, default 30s
- **Response cap**: body truncated at 50,000 chars
- **Docker**: Python 3.12-slim, internal network only (no exposed ports), backend connects at `http://http-mcp:9200/mcp`

### Infrastructure
- `docker-compose.dev.yaml` - Four services (github-mcp commented out):
  - `backend-dev` (8004:8003) — FastAPI + uvicorn, depends on sandbox
  - `sandbox-dev` — snekbox (sandboxed Python execution for `shell_execute`), `linux/amd64`, privileged, internal network only
  - `http-mcp` — FastMCP HTTP request tool server (internal network only, port 9200); built from `mcp-servers/http-request/`
  - `frontend-dev` (3000:5173) — Vite dev server
  - `github-mcp` (commented out) — `ghcr.io/github/github-mcp-server` only supports stdio; needs mcp-proxy or use GitHub's hosted endpoint `https://api.githubcopilot.com/mcp/`
- `manage.sh` - Docker + daemon management: `./manage.sh -e dev up -d` (also starts daemon if `DAEMON_ENABLED=true`), `down` (also stops daemon), `logs -f`, `build`, `daemon start|stop|status|logs`
- `mcp.sh` - MCP server CLI management: `./mcp.sh list`, `add <name> <url> [--desc D]`, `remove <name>`, `enable <name>`, `disable <name>`, `test <name>`. Edits `data/mcp-servers.json` directly via `jq`. Requires `jq` (`brew install jq`).
- `.env.dev` - `OPENROUTER_API_KEY`, model settings, `VITE_API_URL`, `VITE_WS_URL`, data/log paths, `WORKSPACE_DIR`, `DAEMON_ENABLED`, `DAEMON_ARGS` (MCP servers configured via `data/mcp-servers.json` instead of env vars)
- **Timeout settings** (`config/settings.py`):
  - `agent_chat_timeout: int = 120` — REST + WS chat agent execution
  - `agent_strategist_timeout: int = 60` — strategist tick agent
  - `agent_briefing_timeout: int = 60` — daily briefing + evening review LiteLLM calls
  - `consolidation_llm_timeout: int = 30` — memory consolidation LiteLLM call
  - `web_search_timeout: int = 15` — DDGS web search per-call
- **Context window settings** (`config/settings.py`):
  - `context_window_token_budget: int = 12000` — max tokens for conversation history
  - `context_window_keep_first: int = 2` — always keep first N messages
  - `context_window_keep_recent: int = 20` — always keep last N messages
- **Delegation settings** (`config/settings.py`):
  - `use_delegation: bool = False` — feature flag for orchestrator + specialists mode
  - `delegation_max_depth: int = 1` — max nesting depth (1 = orchestrator → specialists)
  - `orchestrator_max_steps: int = 8` — max delegation steps for orchestrator
- **Scheduler settings** (`config/settings.py`):
  - `scheduler_enabled: bool = True` — enable/disable all background jobs
  - `memory_consolidation_interval_min: int = 30`
  - `goal_check_interval_hours: int = 4`
  - `calendar_scan_interval_min: int = 15`
  - `strategist_interval_min: int = 15`
  - `morning_briefing_hour: int = 8`
  - `evening_review_hour: int = 21`
- **Observer / proactivity settings** (`config/settings.py`):
  - `proactivity_level: int = 3` — default proactivity dial
  - `user_timezone: str = "UTC"`
  - `working_hours_start: int = 9`, `working_hours_end: int = 17`
  - `observer_git_repo_path: str = ""` — git repo to monitor
  - `deep_work_apps: str = ""` — custom app names that trigger deep_work state
- **Sandbox / browser settings** (`config/settings.py`):
  - `sandbox_url: str = "http://sandbox:8060"`, `sandbox_timeout: int = 35`
  - `browser_timeout: int = 30`

## WebSocket Protocol
- **Client sends**: `{type: "message" | "ping" | "skip_onboarding", message, session_id}`
- **Server sends**: `{type: "step" | "final" | "error" | "pong" | "proactive" | "ambient", content, session_id, step, ...}`
  - `proactive` messages include: `urgency`, `intervention_type` ("alert" | "advisory" | "nudge"), `reasoning`
  - `ambient` messages include: `state` ("idle" | "has_insight" | "goal_behind" | "on_track" | "waiting"), `tooltip`
- On WS connect, server sends a `proactive`/`advisory` welcome message if onboarding is incomplete

## Onboarding Flow
- New users get a specialized onboarding agent (limited to soul/goal tools)
- Welcome message sent on WS connect explaining the process
- After ~3 exchanges (6 messages), onboarding auto-completes and full agent unlocks
- Users can skip via "Skip intro >>" button or `skip_onboarding` WS message
- Users can restart via "Restart intro" in the Settings panel or `POST /api/user/onboarding/restart`

## Avatar Animation State Machine
```
User sends message → THINKING (center 50%)
  Tool detected in WS step → CASTING + MagicEffect overlay spawned at agent (cycled from pool)
  No tool / unknown → stays THINKING
  WS "final" received → SPEAKING (3s) → IDLE → WANDERING
```
- Tool use triggers a casting animation with a magic effect overlay (no building-walking)
- Magic effects: `MagicEffect` instances spawned from `magicEffectPool` (loaded from map), destroyed/faded on final answer
- Animation states: `idle`, `thinking`, `walking`, `wandering`, `casting`, `speaking`

## Delegation Architecture (Feature-Flagged)
When `use_delegation=True`, the agent uses recursive delegation:
- Orchestrator (no tools) → delegates to specialists (domain tools)
- 4 built-in specialists: memory_keeper, goal_planner, web_researcher, file_worker
- 1 MCP specialist per enabled MCP server (auto-generated)
- Onboarding and strategist agents remain flat
- Skills inject at orchestrator level; tool gating aggregates all specialists' tools
- WS steps show "Delegating to X: task" for specialist calls
- Frontend animation triggers on specialist names via toolParser regex

## Proactive Message Flow
```
Scheduler jobs (strategist_tick / daily_briefing / evening_review)
  │ WSResponse
  ▼
deliver_or_queue()  ← attention guardian (Phase 3.3)
  ├─ DELIVER → ws_manager.broadcast()
  │   ├─ proactive (alert/advisory) → opens chat + addMessage
  │   ├─ proactive (nudge) → EventBus "agent-nudge" → SpeechBubble 5s
  │   └─ ambient → setAmbientState + setAmbientTooltip → HudButtons dot
  ├─ QUEUE → insight_queue (DB) → bundle on state transition
  └─ DROP → log only
```
- `strategist_tick`: runs restricted smolagents agent, parses JSON decision, routes via `deliver_or_queue`
- `daily_briefing`: gathers context + soul + memories, calls LiteLLM, delivers with `is_scheduled=True` (bypasses gate)
- `evening_review`: counts today's messages/completed goals, calls LiteLLM, delivers with `is_scheduled=True`
- Frontend ambient indicator: colored dot in HudButtons (`goal_behind`=red pulsing, `on_track`=green, `has_insight`=yellow pulsing, `waiting`=blue pulsing)
- Frontend nudge: `agent-nudge` EventBus → SpeechBubble shows for 5s then auto-hides (with timer cleanup)

## Screen Daemon Data Flow
```
┌──────────────────────────────────────────────────────┐
│  daemon/seraph_daemon.py (native macOS process)       │
│                                                      │
│  poll_loop (every 5s):                                │
│    NSWorkspace → app name                            │
│    osascript → window title                          │
│    POST {"active_window": "VS Code — main.py"}       │
│                                                      │
│  ocr_loop (every 30s, if --ocr):                     │
│    CGWindowListCreateImage → PNG bytes               │
│    → OCR provider (apple-vision or openrouter)       │
│    POST {"screen_context": "extracted text..."}       │
└────────────────────┬─────────────────────────────────┘
                     │ POST /api/observer/context
                     ▼
┌──────────────────────────────────────────────────────┐
│  Backend (Docker, localhost:8004)                      │
│                                                      │
│  update_screen_context() [partial update + heartbeat]  │
│  → active_window + screen_context both preserved      │
│  → last_daemon_post timestamp recorded on every POST  │
│  → to_prompt_block() includes "Screen content:…"     │
│  → chat agent + strategist both see screen context    │
│                                                      │
│  UserStateMachine.derive_state() [enhanced]:          │
│  if active_window matches IDE → deep_work             │
│  → delivery gate queues proactive messages             │
│  → ambient state pushed to frontend                    │
└──────────────────────────────────────────────────────┘
```

## Village Scene (Phaser)
- Dynamic map size loaded from Tiled JSON (default 40x40, camera bounds set from `map.widthInPixels`/`heightInPixels`), scaled 2x
- Tile stacking via sublayers: layer names with `__N` suffix (e.g. `terrain__2`) share base layer depth; all sublayers checked for collision
- **Buildings**: Dynamically parsed from Tiled map JSON `buildings` custom property via `parseBuildings()`. No buildings are hardcoded — the map editor defines building zones, interior floors, and portals.
- **Building interiors**: `enterBuilding()` hides exterior zone tiles, `renderInteriorFloor()` creates interior container at depth 2.5, `changeFloor()` switches floors, `exitBuilding()` restores exterior; portal detection (`checkPortalCollision()`) in update loop for entry/stairs
- **Magic effects**: `magicEffectPool` loaded from map custom property `magic_effects`; `parseMagicEffects()` loads animation spritesheets; `handleCastEffect()` spawns overlay at agent position; destroyed/faded on final answer
- **WASD/arrow key movement**: User avatar moves tile-by-tile with collision checking (`handlePlayerInput()` in update loop); blocked during tween
- **Character sprite assignment**: Spawn point objects with `sprite_sheet` property (format `Character_XXX_Y`) parsed to `SpriteConfig { key, colOffset }`; `createCharSheetAnimations()` builds directional animations from 16-column sheets
- Procedural forest (density 0.45, max 300 trees)
- Day/night cycle (hours 6-17 = day, 18+ = night)
- Agent wanders between walkable tiles when idle (A* pathfinding via `Pathfinder`)
- User avatar positioned at home spawn point

## Chat Panel Controls
- **Maximize/Minimize**: ▲/▼ button on DialogFrame expands chat overlay to nearly full screen (16px margins all sides) with CSS transition
- **Close**: ✕ button on DialogFrame hides the panel
- **Reopen**: Floating HudButtons at bottom-left appear when panels are closed ("Chat", "Quests", "Settings")
- **Keyboard shortcuts**: Shift+C (chat), Shift+Q (quests), Shift+S (settings), Escape (close). Shift modifier prevents conflict with WASD avatar movement. Ignored when focus is in INPUT/TEXTAREA.
- **Quest search/filter**: QuestPanel includes text search, level dropdown, and domain dropdown. Client-side recursive filter preserves parent chain when descendants match.
- **Session persistence**: Last selected `sessionId` stored in `localStorage` (`seraph_last_session_id`); restored on page load via `switchSession()` in WS `onopen`
- **Font sizing**: Minimum 9px throughout all panels (Press Start 2P pixel font). Headers/labels 10-11px, body text 10-11px, secondary/meta text 9-10px.

## Design Decisions
1. **Phaser 3 canvas** — Village scene rendered via Phaser with tile-based map. React overlays for chat/quest panels.
2. **Pixel art pipeline** — Sprite sheet format defined (16-column sheets, 32x32 frames). PixelLab MCP integration available for character/tile generation.
3. **Tool detection via regex** — 6 patterns + fallback string match on step content. Dynamic tool set from API, static fallback for native tools.
4. **No SSR, no router** — Single-view SPA, Vite dev server only.
5. **SQLite + LanceDB** — Lightweight persistence. SQLModel for structured data, LanceDB for vector similarity search.
6. **Onboarding agent** — Separate agent instance with restricted tool set for first-time user discovery.
7. **Tile stacking via CellStack** — `number[]` per cell per layer allows overlapping tiles (e.g. decoration on terrain). Serialized as Tiled sublayers (`layer__N`) for format compatibility.
8. **Editor as standalone app** — Separate Vite app (`editor/`) with own stores and components; shares tileset/sprite assets from `frontend/public/assets/` via proxy. Outputs Tiled JSON consumed directly by VillageScene.
9. **SKILL.md plugins** — Zero-code markdown files with YAML frontmatter. Drop in `data/skills/`, agent gains capabilities via prompt injection. Tool gating ensures skills only activate when required tools exist. Runtime enable/disable via API + Settings UI.
10. **Bundled HTTP MCP server** — Self-hosted FastMCP server (`mcp-servers/http-request/`) exposing `http_request` tool for arbitrary REST API calls. Runs as Docker service on internal network. Security: blocks internal/private IPs, clamps timeout 1-60s.
11. **Discover catalog** — Curated catalog (`src/defaults/skill-catalog.json`) of skills and MCP servers. Browse in Settings UI, one-click install. Catalog API (`GET /api/catalog`, `POST /api/catalog/install/{name}`) copies bundled skill files or adds MCP config entries. All MCP entries install as `enabled: false`.
12. **MCP seed config** — On first startup, `src/defaults/mcp-servers.default.json` is copied to workspace if no config exists. Ships with `http-request` and `github` entries (both disabled). Existing configs are never overwritten.
13. **Bundled defaults in `src/defaults/`** — Static/reference files (catalog, MCP default config, skill templates) live under `src/defaults/` to avoid being hidden by the Docker workspace volume mount at `/app/data`. Skills are seeded to workspace on first run.
