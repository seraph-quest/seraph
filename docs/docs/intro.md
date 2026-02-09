---
sidebar_position: 1
slug: /intro
---

# Getting Started

Seraph is a proactive AI guardian with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar walks between tool stations (well for web search, forge for shell, mailbox for email, etc.) while you chat via an RPG-style dialog box. The agent has persistent identity, long-term memory, and a hierarchical goal/quest system.

## Quick Start

1. Set your OpenRouter API key in `.env.dev`:
   ```
   OPENROUTER_API_KEY=your-key-here
   ```

2. Start with Docker:
   ```bash
   ./manage.sh -e dev up -d
   ```

3. Verify:
   ```bash
   curl http://localhost:8004/health
   # Open http://localhost:3000 for the retro chat UI
   # Open http://localhost:8004/docs for Swagger UI
   ```

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

## Current Status

- **Phase 0** (Foundation): Complete
- **Phase 1** (Persistent Identity): Complete — DB, soul/memory, goals, onboarding, quest UI, two-pane chat sidebar, session auto-naming, sprite tooltips, chat maximize/close controls, HUD panel buttons, session persistence (localStorage)
- **Phase 2** (Capable Executor): Complete — shell, browser, calendar, email, plugin system
- **Phase 3** (The Observer): Planned — scheduler, context awareness, proactive reasoning
