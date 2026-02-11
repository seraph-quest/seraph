# Seraph AI Agent - Project Context

## Project Overview
Seraph is an AI agent with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar walks between tool stations (well for web search, forge for shell, mailbox for email, etc.) while the user chats via an RPG-style dialog box. The agent has persistent identity (soul file), long-term memory (vector embeddings), and a hierarchical goal/quest system.

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
  - `src/stores/chatStore.ts` - Zustand store (messages, sessions, connection, agent visual state, onboarding, ambient, ambientTooltip, chatMaximized, session persistence via localStorage)
  - `src/stores/questStore.ts` - Zustand store (goal tree, domain progress dashboard)
  - `src/hooks/useWebSocket.ts` - Native WS connection to `ws://localhost:8004/ws/chat`, reconnect, ping, message dispatch
  - `src/hooks/useAgentAnimation.ts` - Walk-then-act state machine with timers
  - `src/lib/toolParser.ts` - Regex detection of tool names from step content (5 patterns + fallback)
  - `src/lib/animationStateMachine.ts` - Tool → village position mapping (pixel coords + percentage fallback)
  - `src/config/constants.ts` - Scene dimensions, tool names, village positions, wandering waypoints
  - `src/components/chat/` - ChatPanel, SessionList, MessageList, MessageBubble, ChatInput, ThinkingIndicator, DialogFrame (RPG frame with optional maximize/close buttons)
  - `src/components/quest/` - QuestPanel, GoalTree, DomainStats
  - `src/components/SettingsPanel.tsx` - Standalone settings overlay panel (restart onboarding, version)
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
  - `src/api/tools.py` — `GET /api/tools`
  - `/health` — health check (defined in `src/app.py`)
- **Tools** (`src/tools/`):
  - Phase 1: `web_search`, `read_file`, `write_file`, `fill_template`, `view_soul`, `update_soul`, `create_goal`, `update_goal`, `get_goals`, `get_goal_progress`
  - Phase 2: `shell_execute`, `browse_webpage`, `get_calendar_events`, `create_calendar_event`, `read_emails`, `send_email`
  - MCP: `src/tools/mcp_manager.py` — multi-server MCP manager; connects to named external MCP servers using `smolagents.MCPClient`. Things3 (22 tools) loaded if `THINGS_MCP_URL` is set. GitHub MCP (22 tools, mapped to tower) registered but disabled pending stdio→HTTP proxy setup (`GITHUB_MCP_URL` commented out).
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
- Polls frontmost app name (`NSWorkspace`, no permission) + window title (AppleScript, Accessibility permission) + idle seconds (`Quartz.CGEventSourceSecondsSinceLastEventType`, no permission)
- **Change detection**: skips POST if `active_window` unchanged since last POST
- **Idle detection**: skips POST if no user input for `--idle-timeout` seconds (default 300)
- POSTs to `POST /api/observer/context` with `{"active_window": "App — Title", "screen_context": null}`
- CLI args: `--url` (default `http://localhost:8004`), `--interval` (default `5`), `--idle-timeout` (default `300`), `--verbose`
- Graceful shutdown on SIGINT/SIGTERM, handles backend-down with warning + retry
- Research on future upgrades (OCR, VLMs, cloud APIs): `docs/docs/development/screen-daemon-research.md`
- **Quick start**: `./daemon/run.sh` (creates venv, installs deps, starts daemon)
- **Manual run**: `cd daemon && uv pip install -r requirements.txt && uv run python seraph_daemon.py --verbose`

### Infrastructure
- `docker-compose.dev.yaml` - Three services (github-mcp commented out):
  - `backend-dev` (8004:8003) — FastAPI + uvicorn, depends on sandbox
  - `sandbox-dev` — snekbox (sandboxed Python execution for `shell_execute`), `linux/amd64`, privileged, internal network only
  - `frontend-dev` (3000:5173) — Vite dev server
  - `github-mcp` (commented out) — `ghcr.io/github/github-mcp-server` only supports stdio; needs mcp-proxy or use GitHub's hosted endpoint `https://api.githubcopilot.com/mcp/`
- `manage.sh` - Docker management: `./manage.sh -e dev up -d`, `down`, `logs -f`, `build`
- `.env.dev` - `OPENROUTER_API_KEY`, model settings, `VITE_API_URL`, `VITE_WS_URL`, `THINGS_MCP_URL`, `GITHUB_MCP_URL` (disabled), data/log paths, `WORKSPACE_DIR`

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
  ├─ web_search         → WALKING → AT-WELL     (15%, house1: 192,280)
  ├─ read/write_file    → WALKING → AT-SIGNPOST  (85%, house2: 832,280)
  ├─ fill_template/soul/goals/things3 → WALKING → AT-BENCH (50%, church: 512,240)
  ├─ shell_execute      → WALKING → AT-FORGE    (35%, forge: 384,320)
  ├─ browse_webpage     → WALKING → AT-TOWER    (60%, tower: 640,200)
  ├─ calendar tools     → WALKING → AT-CLOCK    (55%, clock: 576,340)
  ├─ email tools        → WALKING → AT-MAILBOX  (10%, mailbox: 128,340)
  └─ no tool            → stays THINKING
  WS "final" received → WALKING back → SPEAKING (3s) → IDLE → WANDERING
```
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
┌──────────────────────────────────────────────────┐
│  daemon/seraph_daemon.py (native macOS process)  │
│                                                  │
│  NSWorkspace.frontmostApplication → app name     │
│  osascript (AppleScript) → window title          │
│  CGEventSource → idle seconds                    │
│                                                  │
│  Change detection: skip if same as last POST     │
│  Idle detection: skip if idle > 5 min            │
└────────────────────┬─────────────────────────────┘
                     │ POST /api/observer/context
                     │ {"active_window": "VS Code — main.py"}
                     ▼
┌──────────────────────────────────────────────────┐
│  Backend (Docker, localhost:8004)                 │
│                                                  │
│  context_manager.update_screen_context()         │
│  → CurrentContext.active_window updated           │
│  → preserved across refresh() cycles              │
│  → passed to derive_state(active_window=...)      │
│  → included in to_prompt_block() for strategist   │
│                                                  │
│  UserStateMachine.derive_state() [enhanced]:     │
│  if active_window matches IDE → deep_work        │
│  → delivery gate queues proactive messages        │
│  → ambient state pushed to frontend               │
└──────────────────────────────────────────────────┘
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
3. **Tool detection via regex** — 5 patterns + fallback string match on step content.
4. **No SSR, no router** — Single-view SPA, Vite dev server only.
5. **SQLite + LanceDB** — Lightweight persistence. SQLModel for structured data, LanceDB for vector similarity search.
6. **Onboarding agent** — Separate agent instance with restricted tool set for first-time user discovery.
