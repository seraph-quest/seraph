# Seraph AI Agent - Project Context

## Project Overview
Seraph is an AI agent with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar walks between tool stations (well for web search, forge for shell, etc.) while the user chats via an RPG-style dialog box. The agent has persistent identity (soul file), long-term memory (vector embeddings), a hierarchical goal/quest system, and plug-and-play MCP server integration.

## Architecture

### Frontend (`frontend/`)
- **Stack**: React 19 + Vite 6 + TypeScript 5.6 + Tailwind CSS 3 + Zustand 5 + Phaser 3.90
- **Entry**: `src/main.tsx` → `src/App.tsx`
- **Key files**:
  - `src/game/PhaserGame.tsx` - React wrapper for Phaser instance
  - `src/game/scenes/StudyScene.ts` - Village scene (64x32 tile map, buildings, forest, day/night cycle)
  - `src/game/objects/AgentSprite.ts` - Phaser sprite for Seraph avatar
  - `src/game/objects/UserSprite.ts` - Phaser sprite for user avatar
  - `src/game/objects/SpeechBubble.ts` - Phaser speech bubble
  - `src/game/EventBus.ts` - Bridges Phaser events to React
  - `src/stores/chatStore.ts` - Zustand store (messages, sessions, connection, agent visual state, onboarding, ambient, ambientTooltip, chatMaximized, toolRegistry, session persistence via localStorage)
  - `src/stores/questStore.ts` - Zustand store (goal tree, domain progress dashboard)
  - `src/hooks/useWebSocket.ts` - Native WS connection to `ws://localhost:8004/ws/chat`, reconnect, ping, message dispatch
  - `src/hooks/useAgentAnimation.ts` - Walk-then-act state machine with timers
  - `src/lib/toolParser.ts` - Regex detection of tool names from step content (5 patterns + fallback); uses dynamic tool registry from API with static fallback
  - `src/lib/animationStateMachine.ts` - Tool → village position mapping (pixel coords + percentage fallback); dynamic lookup from tool registry for MCP tools
  - `src/config/constants.ts` - Scene dimensions, tool names, village positions, BUILDING_POSITIONS lookup, wandering waypoints
  - `src/components/chat/` - ChatPanel, SessionList, MessageList, MessageBubble, ChatInput, ThinkingIndicator, DialogFrame (RPG frame with optional maximize/close buttons)
  - `src/components/quest/` - QuestPanel, GoalTree, DomainStats
  - `src/components/SettingsPanel.tsx` - Standalone settings overlay panel (restart onboarding, MCP server management UI, version)
  - `src/components/HudButtons.tsx` - Floating RPG-styled buttons to reopen closed Chat/Quest/Settings panels + ambient state indicator dot (color-coded, pulsing)
  - `src/index.css` - CRT scanlines/vignette, pixel borders, RPG frame, chat-overlay maximized state

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
  - `src/api/mcp.py` — `GET /api/mcp/servers`, `POST /api/mcp/servers`, `PUT /api/mcp/servers/{name}`, `DELETE /api/mcp/servers/{name}`, `POST /api/mcp/servers/{name}/test`
  - `/health` — health check (defined in `src/app.py`)
- **Tools** (`src/tools/`):
  - Phase 1: `web_search`, `read_file`, `write_file`, `fill_template`, `view_soul`, `update_soul`, `create_goal`, `update_goal`, `get_goals`, `get_goal_progress`
  - Phase 2: `shell_execute`, `browse_webpage`
  - MCP: `src/tools/mcp_manager.py` — plug-and-play MCP manager; loads server config from `data/mcp-servers.json`, connects to enabled servers via `smolagents.MCPClient`. Supports runtime add/remove/toggle via API. Each server can have a village building assignment for avatar animation. Config file is auto-persisted on mutations.
- **MCP Configuration** (`data/mcp-servers.json`):
  - JSON config with `mcpServers` object: `{name: {url, enabled, building?, description?}}`
  - `data/mcp-servers.example.json` committed to repo as reference
  - Loaded on app startup from `settings.workspace_dir + "/mcp-servers.json"`
  - No config file = no MCP tools, no errors
- **Memory** (`src/memory/`):
  - `soul.py` — Persistent identity file (markdown in workspace)
  - `vector_store.py` — LanceDB vector store for long-term memory search
  - `embedder.py` — Sentence transformers for memory embeddings
  - `consolidator.py` — Background task extracting memories after each conversation
- **Goals** (`src/goals/repository.py`): SQLModel-based hierarchical goal CRUD
- **Agent** (`src/agent/`):
  - `factory.py` — Creates full agent with all tools + context (history, soul, memories)
  - `onboarding.py` — Specialized onboarding agent (limited to soul/goal tools, 5-point discovery)
  - `strategist.py` — Strategist agent factory (restricted to `view_soul`, `get_goals`, `get_goal_progress`, temp=0.4, max_steps=5) + `StrategistDecision` dataclass + `parse_strategist_response()` JSON parser
  - `session.py` — Async session manager (SQLite-backed)
- **Database** (`src/db/`): SQLModel + aiosqlite. Models: `UserProfile`, `Session`, `Message`, `Goal`, `Memory`, `QueuedInsight`
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
  - `context.py` — `CurrentContext` dataclass (time, calendar, git, goals, user state, screen, attention budget) + `to_prompt_block()`
  - `manager.py` — `ContextManager` singleton; refreshes all sources, derives user state, detects state transitions, delivers queued bundles
  - `user_state.py` — User state machine (available/deep_work/in_meeting/transitioning/away/winding_down), delivery gate (`should_deliver()`), attention budget management
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

### Infrastructure
- `docker-compose.dev.yaml` - Three services (github-mcp commented out):
  - `backend-dev` (8004:8003) — FastAPI + uvicorn, depends on sandbox
  - `sandbox-dev` — snekbox (sandboxed Python execution for `shell_execute`), `linux/amd64`, privileged, internal network only
  - `frontend-dev` (3000:5173) — Vite dev server
  - `github-mcp` (commented out) — `ghcr.io/github/github-mcp-server` only supports stdio; needs mcp-proxy or use GitHub's hosted endpoint `https://api.githubcopilot.com/mcp/`
- `manage.sh` - Docker management: `./manage.sh -e dev up -d`, `down`, `logs -f`, `build`
- `.env.dev` - `OPENROUTER_API_KEY`, model settings, `VITE_API_URL`, `VITE_WS_URL`, data/log paths, `WORKSPACE_DIR` (MCP servers configured via `data/mcp-servers.json` instead of env vars)

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
User sends message → THINKING at bench (center 50%, pixel 512,350)
  Tool detected in WS step:
  ├─ web_search         → WALKING → AT-WELL     (house-1: 192,280)
  ├─ read/write_file    → WALKING → AT-SIGNPOST  (house-2: 832,280)
  ├─ fill_template/soul/goals → WALKING → AT-BENCH (church: 512,240)
  ├─ shell_execute      → WALKING → AT-FORGE    (forge: 384,320)
  ├─ browse_webpage     → WALKING → AT-TOWER    (tower: 640,200)
  ├─ MCP tools          → WALKING → building assigned in mcp-servers.json
  └─ no tool / unknown  → stays THINKING
  WS "final" received → WALKING back → SPEAKING (3s) → IDLE → WANDERING
```
- Native tools have static targets in `animationStateMachine.ts`
- MCP tools resolved dynamically from `toolRegistry` (fetched from `GET /api/tools` on WS connect)
- Building→coords mapping: `BUILDING_DEFAULTS` in backend `registry.py`, `BUILDING_POSITIONS` in frontend `constants.ts`
Animation states: `idle`, `thinking`, `walking`, `wandering`, `at-well`, `at-signpost`, `at-bench`, `at-tower`, `at-forge`, `at-clock`, `at-mailbox`, `speaking`

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
│  update_screen_context() [partial update]             │
│  → active_window + screen_context both preserved      │
│  → to_prompt_block() includes "Screen content:…"     │
│  → strategist sees what's on screen                   │
│                                                      │
│  UserStateMachine.derive_state() [enhanced]:          │
│  if active_window matches IDE → deep_work             │
│  → delivery gate queues proactive messages             │
│  → ambient state pushed to frontend                    │
└──────────────────────────────────────────────────────┘
```

## Village Scene (Phaser)
- 64x32 tile map (1024x512px at 16px tiles), scaled 2x
- Buildings: House 1 (west), Church (center), House 2 (east), Forge, Tower, Clock, Mailbox
- Procedural forest (density 0.45, max 300 trees)
- Day/night cycle (hours 6-17 = day, 18+ = night)
- Agent wanders between 12 waypoints when idle
- User avatar positioned at home (832, 340)

## Chat Panel Controls
- **Maximize/Minimize**: ▲/▼ button on DialogFrame expands chat overlay to nearly full screen (16px margins all sides) with CSS transition
- **Close**: ✕ button on DialogFrame hides the panel
- **Reopen**: Floating HudButtons at bottom-left appear when panels are closed ("Chat", "Quests", "Settings")
- **Session persistence**: Last selected `sessionId` stored in `localStorage` (`seraph_last_session_id`); restored on page load via `switchSession()` in WS `onopen`

## Design Decisions
1. **Phaser 3 canvas** — Village scene rendered via Phaser with tile-based map. React overlays for chat/quest panels.
2. **Placeholder art** — Sprite sheet format defined (32x32 frames) for future art drop-in via PixelLab.
3. **Tool detection via regex** — 5 patterns + fallback string match on step content. Dynamic tool set from API, static fallback for native tools.
4. **No SSR, no router** — Single-view SPA, Vite dev server only.
5. **SQLite + LanceDB** — Lightweight persistence. SQLModel for structured data, LanceDB for vector similarity search.
6. **Onboarding agent** — Separate agent instance with restricted tool set for first-time user discovery.
