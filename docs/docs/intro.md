---
sidebar_position: 1
slug: /
---

# Getting Started

Seraph is a proactive AI guardian with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar casts magic effects when using tools while you chat via an RPG-style dialog box. The agent has persistent identity, long-term memory, and a hierarchical goal/quest system.

## Setup

See the **[Setup Guide](./setup)** for complete installation and configuration instructions.

## Architecture

- **Frontend**: React 19, Vite 6, TypeScript, Tailwind CSS, Zustand, Phaser 3.90
- **Backend**: Python 3.12, FastAPI, uvicorn, smolagents, LiteLLM (OpenRouter)
- **Database**: SQLModel + SQLite (aiosqlite), LanceDB (vector memory)
- **Tools**: 16 auto-discovered tools + MCP integrations — web search, file I/O, shell (snekbox sandbox), browser (Playwright), calendar (Google), email (Gmail), soul/memory, goals, Things3 (via MCP)
- **Infra**: Docker Compose (3 services: backend, frontend, snekbox sandbox), uv package manager

## Project Structure

```
frontend/
├── src/
│   ├── game/            # Phaser 3 village scene, sprites, EventBus
│   ├── components/      # React overlays (chat sidebar + messages, quest, ui)
│   ├── hooks/           # useWebSocket, useAgentAnimation
│   ├── stores/          # Zustand stores (chat, quest)
│   ├── lib/             # Tool parser, animation state machine
│   ├── types/           # TypeScript interfaces
│   └── config/          # Constants, scene dimensions, tool names

backend/
├── main.py              # Uvicorn entry point
├── config/settings.py   # Pydantic Settings (env vars)
├── src/
│   ├── app.py           # FastAPI app factory (lifespan: DB init, soul file)
│   ├── models/schemas.py
│   ├── api/             # REST + WebSocket endpoints (chat, sessions, goals, profile, tools)
│   ├── agent/           # smolagents factory, onboarding agent, session manager
│   ├── tools/           # 16 @tool implementations (auto-discovered) + MCP tools
│   ├── db/              # SQLModel engine + models (Session, Message, Goal, UserProfile, Memory)
│   ├── memory/          # Soul file, LanceDB vector store, embedder, consolidator
│   ├── goals/           # Hierarchical goal CRUD repository
│   └── plugins/         # Tool auto-discovery loader + registry
└── tests/
```

## Management

```bash
./manage.sh -e dev up -d      # Start dev
./manage.sh -e dev down        # Stop
./manage.sh -e dev logs -f     # Tail logs
./manage.sh -e dev build       # Rebuild
```

## Testing

```bash
# Backend (from backend/)
cd backend
uv run pytest -v              # Run all tests with coverage

# Frontend (from frontend/)
cd frontend
npm test                       # Run all tests
npm run test:watch             # Watch mode
npm run test:coverage          # With coverage report
```

See [Testing Guide](./development/testing) for full details.

## Current Status

- **Phase 0** (Foundation): Complete — chat, WebSocket streaming, Phaser village, day/night cycle
- **Phase 1** (Persistent Identity): Complete — DB, soul/memory, goals, onboarding, quest UI, two-pane chat sidebar, session auto-naming, sprite tooltips, chat maximize/close controls, HUD panel buttons, session persistence (localStorage)
- **Phase 2** (Capable Executor): Complete — shell, browser, calendar, email, plugin system, MCP integrations (Things3)
- **Phase 3** (The Observer): Complete (3.1–3.5) — background scheduler, context awareness, user state machine, proactive strategist, daily briefing, evening review, attention guardian, insight queue, ambient/nudge frontend, native macOS screen daemon
- **Phase 4** (The Network): Planned — SKILL.md ecosystem, multi-channel messaging, workflows, multi-agent, voice, canvas, remote access
