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
  - `src/stores/chatStore.ts` - Zustand store (messages, sessions, connection, agent visual state, onboarding, ambient)
  - `src/stores/questStore.ts` - Zustand store (goal tree, domain progress dashboard)
  - `src/hooks/useWebSocket.ts` - Native WS connection to `ws://localhost:8004/ws/chat`, reconnect, ping, message dispatch
  - `src/hooks/useAgentAnimation.ts` - Walk-then-act state machine with timers
  - `src/lib/toolParser.ts` - Regex detection of tool names from step content (5 patterns + fallback)
  - `src/lib/animationStateMachine.ts` - Tool → village position mapping (pixel coords + percentage fallback)
  - `src/config/constants.ts` - Scene dimensions, tool names, village positions, wandering waypoints
  - `src/components/chat/` - ChatPanel, SessionList, MessageList, MessageBubble, ChatInput, ThinkingIndicator
  - `src/components/quest/` - QuestPanel, GoalTree, DomainStats
  - `src/index.css` - CRT scanlines/vignette, pixel borders, RPG frame

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
  - MCP: `src/tools/mcp_manager.py` — connects to external MCP servers (e.g. Things3 via `things-mcp`) using `smolagents.MCPClient`; 22 Things3 tools loaded at startup if `THINGS_MCP_URL` is set
- **Memory** (`src/memory/`):
  - `soul.py` — Persistent identity file (markdown in workspace)
  - `vector_store.py` — LanceDB vector store for long-term memory search
  - `embedder.py` — Sentence transformers for memory embeddings
  - `consolidator.py` — Background task extracting memories after each conversation
- **Goals** (`src/goals/repository.py`): SQLModel-based hierarchical goal CRUD
- **Agent** (`src/agent/`):
  - `factory.py` — Creates full agent with all tools + context (history, soul, memories)
  - `onboarding.py` — Specialized onboarding agent (limited to soul/goal tools, 5-point discovery)
  - `session.py` — Async session manager (SQLite-backed)
- **Database** (`src/db/`): SQLModel + aiosqlite. Models: `UserProfile`, `Session`, `Message`, `Goal`, `Memory`
- **CORS**: Allows `localhost:3000` and `localhost:5173`

### Infrastructure
- `docker-compose.dev.yaml` - Three services:
  - `backend-dev` (8004:8003) — FastAPI + uvicorn, depends on sandbox
  - `sandbox-dev` — snekbox (sandboxed Python execution for `shell_execute`), `linux/amd64`, privileged, internal network only
  - `frontend-dev` (3000:5173) — Vite dev server
- `manage.sh` - Docker management: `./manage.sh -e dev up -d`, `down`, `logs -f`, `build`
- `.env.dev` - `OPENROUTER_API_KEY`, model settings, `VITE_API_URL`, `VITE_WS_URL`, `THINGS_MCP_URL`, data/log paths, `WORKSPACE_DIR`

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
- Users can restart via "Restart intro" button in session list or `POST /api/user/onboarding/restart`

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

## Village Scene (Phaser)
- 64x32 tile map (1024x512px at 16px tiles), scaled 2x
- Buildings: House 1 (west), Church (center), House 2 (east), Forge, Tower, Clock, Mailbox
- Procedural forest (density 0.45, max 300 trees)
- Day/night cycle (hours 6-17 = day, 18+ = night)
- Agent wanders between 12 waypoints when idle
- User avatar positioned at home (832, 340)

## Design Decisions
1. **Phaser 3 canvas** — Village scene rendered via Phaser with tile-based map. React overlays for chat/quest panels.
2. **Placeholder art** — Sprite sheet format defined (32x32 frames) for future art drop-in via PixelLab.
3. **Tool detection via regex** — 5 patterns + fallback string match on step content.
4. **No SSR, no router** — Single-view SPA, Vite dev server only.
5. **SQLite + LanceDB** — Lightweight persistence. SQLModel for structured data, LanceDB for vector similarity search.
6. **Onboarding agent** — Separate agent instance with restricted tool set for first-time user discovery.
