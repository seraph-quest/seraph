# Seraph AI Agent - Project Context

## Project Overview
Seraph is an AI agent with a retro 16-bit RPG chat UI. An animated pixel-art avatar visually acts out tool usage (walking to a computer for web search, filing cabinet for file ops, etc.) while the user chats via an RPG-style dialog box.

## Current State (2026-02-07)
- **Branch**: `feat/initial-project-setup` (1 commit ahead of origin)
- **Last commit**: `f0a1d25` - "add retro 16-bit RPG chat frontend with animated avatar"
- **Both services running**: backend (port 8004), frontend (port 3000)
- **Build status**: TypeScript compiles clean, Vite build succeeds (210KB JS, 17KB CSS)

## Architecture

### Frontend (`frontend/`)
- **Stack**: React 19 + Vite 6 + TypeScript 5.6 + Tailwind CSS 3 + Zustand 5
- **Entry**: `src/main.tsx` → `src/App.tsx`
- **Key files**:
  - `src/stores/chatStore.ts` - Zustand store (messages, session, connection, agent visual state)
  - `src/hooks/useWebSocket.ts` - Native WS connection to `ws://localhost:8004/ws/chat`, reconnect, ping, message dispatch
  - `src/hooks/useAgentAnimation.ts` - Walk-then-act state machine with timers
  - `src/lib/toolParser.ts` - Regex detection of tool names from step content
  - `src/lib/animationStateMachine.ts` - Tool→position mapping (computer=15%, desk=50%, cabinet=85%)
  - `src/components/scene/` - Room, Avatar, SpeechBubble, furniture (Computer, Desk, FilingCabinet)
  - `src/components/chat/` - ChatPanel, MessageList, MessageBubble, ChatInput, ThinkingIndicator
  - `src/index.css` - CRT scanlines/vignette, pixel borders, RPG frame, sprite animations

### Backend (`backend/`)
- **Stack**: Python 3.12, FastAPI, uvicorn, smolagents, LiteLLM (OpenRouter)
- **Entry**: `main.py` → `src/app.py` (factory pattern)
- **API**: REST `/api/chat`, WebSocket `/ws/chat`, health `/health`
- **WS protocol**: Send `{type:"message", message, session_id}` → Receive `{type:"step"|"final"|"error", content, session_id, step}`
- **Tools**: `web_search`, `read_file`, `write_file`, `fill_template`
- **CORS**: Allows `localhost:3000` and `localhost:5173`

### Infrastructure
- `docker-compose.dev.yaml` - Two services: `backend-dev` (8004:8003), `frontend-dev` (3000:5173)
- `manage.sh` - Docker management: `./manage.sh -e dev up -d`, `down`, `logs -f`, `build`
- `.env.dev` - Contains `OPENROUTER_API_KEY`, model settings, `VITE_API_URL`, `VITE_WS_URL`

## Avatar Animation State Machine
```
User sends message → THINKING at desk (center 50%)
  Tool detected in WS step:
  ├─ web_search    → WALKING → AT-COMPUTER (left 15%, typing animation)
  ├─ read/write    → WALKING → AT-CABINET  (right 85%, searching animation)
  ├─ fill_template → WALKING → AT-DESK     (center 50%, writing animation)
  └─ no tool       → stays THINKING
  WS "final" received → WALKING back → SPEAKING (3s) → IDLE
```

## Design Decisions
1. **CSS-positioned divs, not canvas** - CSS transitions for walk animations, sprite states as CSS classes. Easy to swap placeholders for real pixel art later.
2. **Placeholder art** - Avatar/furniture are colored rectangles with pseudo-element details. Sprite sheet format defined (32x32 frames) for future art drop-in.
3. **Tool detection via regex** - 5 patterns + fallback string match on step content.
4. **No SSR, no router** - Single-view SPA, Vite dev server only.

## Potential Next Steps
- Replace placeholder pixel rectangles with actual sprite sheets (32x32 frames)
- Test full flow end-to-end with real OpenRouter API key
- Add step message collapsing (expand/collapse in chat)
- Mobile responsiveness
- Retro sound effects
- Push branch / open PR
