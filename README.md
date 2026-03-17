<h1 align="center">Seraph</h1>

<p align="center">
  <strong>A proactive AI guardian with persistent memory, observation, and real-world action</strong>
</p>

<p align="center">
  <a href="https://github.com/seraph-quest/seraph/actions"><img src="https://github.com/seraph-quest/seraph/actions/workflows/test.yml/badge.svg" alt="Tests" /></a>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/react-19-61dafb" alt="React 19" />
  <img src="https://img.shields.io/badge/phaser-3.90-orange" alt="Phaser 3.90" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" />
</p>

<p align="center">
  Seraph ships today with a retro village browser UI, persistent identity, long-term memory, hierarchical goals, proactive scheduling, screen awareness, and plug-and-play MCP server integration. The current research direction is a denser guardian cockpit for power users, not a village-first shell.
</p>

---

## Quick Start

```bash
# 1. Configure
cp .env.dev.example .env.dev
# Edit .env.dev and set OPENROUTER_API_KEY=your-key-here

# 2. Launch
./manage.sh -e dev up -d

# 3. Open
open http://localhost:3000        # Current shipped browser UI
open http://localhost:8004/docs   # Swagger API docs

# 4. (Optional) Screen awareness daemon
./daemon/run.sh                   # Window tracking
./daemon/run.sh --ocr             # + OCR via Apple Vision
```

---

## Architecture

| Layer | Stack |
|-------|-------|
| **Frontend** | React 19, Vite 6, TypeScript, Tailwind CSS, Zustand, Phaser 3.90 |
| **Backend** | Python 3.12, FastAPI, uvicorn, smolagents, LiteLLM (OpenRouter) |
| **Database** | SQLite (aiosqlite) + LanceDB (vector memory) |
| **Tools** | 17 built-in tool capabilities (auto-discovered) + plug-and-play MCP servers |
| **Scheduler** | APScheduler — 9 jobs across briefings, reviews, strategist, observer cleanup, and memory/goal maintenance |
| **Daemon** | Native macOS — window tracking, optional OCR (Apple Vision / OpenRouter) |
| **Infra** | Docker Compose (backend + frontend + snekbox sandbox + http-mcp), uv |

---

## Project Structure

```
frontend/src/
  game/              Phaser 3 village scene, sprites, EventBus
  components/        React overlays — chat, quest panel, settings
  hooks/             useWebSocket, useAgentAnimation
  stores/            Zustand stores — chat, quest
  lib/               Tool parser, animation state machine
  config/            Constants, default positions, waypoints

backend/src/
  api/               REST + WebSocket endpoints (chat, sessions, goals, tools, mcp)
  agent/             smolagents factory, onboarding, strategist, session manager
  tools/             @tool implementations + MCP manager
  memory/            Soul file, LanceDB vector store, embedder, consolidator
  goals/             Hierarchical goal CRUD
  plugins/           Tool auto-discovery + registry
  scheduler/         APScheduler engine, connection manager, 9 background jobs
  observer/          Context manager, data sources, user state machine, delivery engine

daemon/              Native macOS screen daemon (window tracking + OCR)
docs/                Docusaurus docs site
```

---

## Current Embodiment Surface

```
User sends message  -->  THINKING
  Tool detected     -->  CASTING + magic effect overlay
  Response ready    -->  SPEAKING  -->  IDLE  -->  WANDERING
```

Tool use currently triggers a casting animation with a magic effect overlay at the agent's position. This is the shipped surface today, but the live docs now treat the dense guardian cockpit as the future primary interface direction.

---

## MCP Servers

Add external tool servers with zero code changes:

```bash
./mcp.sh add things3 http://host.docker.internal:9100/mcp \
  --desc "Things3 task manager"

./mcp.sh list              # View configured servers
./mcp.sh test things3      # Test connection
./mcp.sh disable things3   # Toggle without removing
./mcp.sh remove things3    # Remove entirely
```

Also available via the **Settings UI** in the browser or the **REST API** (`/api/mcp/servers`).

Config: `data/mcp-servers.json` | Example: `data/mcp-servers.example.json`

---

## Docker Management

```bash
./manage.sh -e dev up -d       # Start
./manage.sh -e dev down         # Stop
./manage.sh -e dev logs -f      # Tail logs
./manage.sh -e dev build        # Rebuild
```

---

## Development Status

Seraph no longer uses the old phase model as the live planning surface.

Canonical docs now live in:

- `docs/implementation/` — shipped state, workstreams, and current status
- `docs/research/` — product thesis and design target

Current truth:

- [x] browser UI, backend APIs, observer daemon, memory, goals, and proactive scheduler foundations are shipped
- [x] Trust Boundaries, Execution Plane, and Runtime Reliability have strong foundations on `develop`
- [x] the source-of-truth docs now target a power-user guardian cockpit as the future primary UI
- [ ] no workstream is complete yet
- [ ] Seraph still has substantial work left in presence, guardian intelligence, embodied UX, and ecosystem leverage

Start with:

- [docs/implementation/00-master-roadmap.md](docs/implementation/00-master-roadmap.md)
- [docs/implementation/STATUS.md](docs/implementation/STATUS.md)
- [docs/research/00-synthesis.md](docs/research/00-synthesis.md)
- [docs/research/10-competitive-benchmark.md](docs/research/10-competitive-benchmark.md)
- [docs/research/11-superiority-program.md](docs/research/11-superiority-program.md)
