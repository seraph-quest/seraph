# Seraph AI Agent

A proactive AI guardian with a retro 16-bit RPG village UI. An animated pixel-art avatar walks between tool stations in a Phaser 3 village while you chat via an RPG-style dialog box. Persistent identity (soul file), long-term memory (vector embeddings), hierarchical goal/quest system, and 16 native tools + MCP integrations across web search, file I/O, shell execution, browser automation, calendar, email, and task management (Things3).

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
- **Tools**: 16 auto-discovered tools + MCP integrations (Things3) — web search, file I/O, shell (snekbox sandbox), browser (Playwright), calendar (Google), email (Gmail), soul/memory, goals, task management
- **Infra**: Docker Compose (3 services: backend, frontend, snekbox sandbox), uv package manager

## Project Structure

```
frontend/
├── src/
│   ├── game/            # Phaser 3 village scene, sprites, EventBus
│   ├── components/      # React overlays (chat, quest, ui)
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

docs/                    # Docusaurus docs site (vision, roadmap, phase specs)
```

## Management

```bash
./manage.sh -e dev up -d      # Start dev
./manage.sh -e dev down        # Stop
./manage.sh -e dev logs -f     # Tail logs
./manage.sh -e dev build       # Rebuild
```

## Status

- **Phase 0** (Foundation): Complete — chat, Phaser village, basic tools
- **Phase 1** (Persistent Identity): Complete — DB, soul/memory, goals, onboarding, quest UI
- **Phase 2** (Capable Executor): Complete — shell, browser, calendar, email, plugin system
- **Phase 3** (The Observer): Planned — scheduler, context awareness, proactive reasoning

See [docs/](docs/) for full vision, roadmap, and phase specifications.
