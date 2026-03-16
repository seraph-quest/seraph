---
sidebar_position: 1
slug: /
---

# Getting Started

> Legacy note: this page now lives under `/legacy`. The canonical source of truth is `docs/implementation/` for shipped status and `docs/research/` for product/design intent.

Seraph is a proactive AI guardian with a retro 16-bit RPG village UI. A Phaser 3 canvas renders a tile-based village where an animated pixel-art avatar casts magic effects when using tools while you chat via an RPG-style dialog box. The agent has persistent identity, long-term memory, a hierarchical goal/quest system, SKILL.md plugins, and plug-and-play MCP server integration.

## Setup

See the **[Setup Guide](./setup)** for complete installation and configuration instructions.

## Architecture

- **Frontend**: React 19, Vite 6, TypeScript 5.6, Tailwind CSS 3, Zustand 5, Phaser 3.90
- **Backend**: Python 3.12, FastAPI, uvicorn, smolagents, LiteLLM (OpenRouter)
- **Database**: SQLModel + SQLite (aiosqlite), LanceDB (vector memory)
- **Tools**: 17 built-in tools (auto-discovered) + MCP integrations — web search, file I/O, shell (snekbox sandbox), browser (Playwright), soul/memory, goals, vault
- **Scheduler**: APScheduler — 9 background jobs (memory consolidation, goal check, calendar scan, strategist tick, daily briefing, evening review, activity digest, weekly activity review, screen cleanup)
- **Observer**: Native macOS daemon → context manager → user state machine → attention guardian → insight queue
- **Infra**: Docker Compose (4 services: backend, frontend, snekbox sandbox, http-mcp), uv package manager

## Project Structure

```
frontend/
├── src/
│   ├── game/            # Phaser 3 village scene, sprites, pathfinding, EventBus
│   ├── components/      # React overlays (chat, quest, settings, HUD buttons)
│   ├── hooks/           # useWebSocket, useAgentAnimation, useKeyboardShortcuts
│   ├── stores/          # Zustand stores (chat, quest)
│   ├── lib/             # Tool parser, animation state machine
│   ├── types/           # TypeScript interfaces
│   └── config/          # Constants, scene dimensions, tool names

editor/                  # Standalone village map editor (React + Vite)
├── src/
│   ├── components/      # MapCanvas, ToolBar, TilesetPanel, BuildingPanel, etc.
│   ├── stores/          # Editor state, tileset management
│   ├── lib/             # Canvas renderer, flood fill, Tiled JSON I/O
│   └── types/           # CellStack, BuildingDef, Tiled JSON schema

backend/
├── main.py              # Uvicorn entry point
├── config/settings.py   # Pydantic Settings (env vars)
├── src/
│   ├── app.py           # FastAPI app factory (lifespan: DB init, soul file)
│   ├── api/             # REST + WebSocket endpoints (chat, sessions, goals, profile, tools, mcp, skills, observer, settings, vault, catalog)
│   ├── agent/           # Agent factory, onboarding, strategist, specialists, context window, session manager
│   ├── tools/           # 16 @tool implementations (auto-discovered) + MCP manager
│   ├── plugins/         # Tool auto-discovery loader + registry
│   ├── skills/          # SKILL.md plugin loader + manager
│   ├── db/              # SQLModel engine + models (Session, Message, Goal, UserProfile, Memory, QueuedInsight, Secret)
│   ├── memory/          # Soul file, LanceDB vector store, embedder, consolidator
│   ├── goals/           # Hierarchical goal CRUD repository
│   ├── observer/        # Context manager, user state machine, delivery gate, insight queue, data sources
│   ├── vault/           # Fernet-encrypted secret store (crypto, repository)
│   └── scheduler/       # APScheduler engine, connection manager, 9 background jobs
├── data/
│   ├── skills/          # SKILL.md plugin files (YAML frontmatter + instructions)
│   └── mcp-servers.json # MCP server configuration (runtime-managed)
└── tests/

daemon/                  # Native macOS screen daemon (outside Docker)
├── seraph_daemon.py     # Dual-loop: window polling + optional OCR
├── ocr/                 # Pluggable OCR providers (Apple Vision, OpenRouter)
└── run.sh               # Quick start script
```

## Management

```bash
./manage.sh -e dev up -d      # Start dev
./manage.sh -e dev down        # Stop
./manage.sh -e dev logs -f     # Tail logs
./manage.sh -e dev build       # Rebuild
```

## MCP Server Management

```bash
./mcp.sh list                         # List configured MCP servers
./mcp.sh add <name> <url> --desc "…"  # Add a server
./mcp.sh remove <name>                # Remove a server
./mcp.sh enable <name>                # Enable a server
./mcp.sh disable <name>               # Disable a server
./mcp.sh test <name>                  # Test connectivity
```

MCP servers can also be managed via the Settings panel in the UI or the REST API (`/api/mcp/servers`).

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

The old phase labels are not the live planning surface anymore.

Use these for the current truth:

- `docs/implementation/STATUS.md`
- `docs/implementation/00-master-roadmap.md`
- the numbered workstream files under `docs/implementation/`
- the design/product thesis files under `docs/research/`

- [x] Seraph has real shipped foundations across all seven workstreams
- [x] Runtime Reliability, Trust Boundaries, and Execution Plane are the strongest foundations today
- [ ] no workstream is complete yet
- [ ] major product work is still ahead in Presence And Reach, Guardian Intelligence, Embodied UX, and Ecosystem And Leverage
