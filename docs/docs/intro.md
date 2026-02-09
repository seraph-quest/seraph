---
sidebar_position: 1
slug: /intro
---

# Getting Started

Seraph is an AI agent with a retro 16-bit RPG chat UI. An animated pixel-art avatar visually acts out tool usage (walking to a computer for web search, filing cabinet for file ops, etc.) while you chat via an RPG-style dialog box.

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

- **Frontend**: React 19, Vite 6, TypeScript, Tailwind CSS, Zustand
- **Backend**: Python 3.12, FastAPI, uvicorn
- **AI**: smolagents ToolCallingAgent + LiteLLM (OpenRouter)
- **Tools**: File I/O, web search, shell execution, browser automation, calendar, email
- **Infra**: Docker Compose, uv package manager

## Project Structure

```
frontend/
├── src/
│   ├── components/      # React components (scene, chat, ui)
│   ├── hooks/           # useWebSocket, useAgentAnimation
│   ├── stores/          # Zustand state management
│   ├── lib/             # Tool parser, animation state machine
│   ├── types/           # TypeScript interfaces
│   └── config/          # Constants and env vars

backend/
├── main.py              # Uvicorn entry point
├── config/settings.py   # Pydantic Settings (env vars)
├── src/
│   ├── app.py           # FastAPI app factory
│   ├── models/schemas.py
│   ├── api/             # REST + WebSocket endpoints
│   ├── agent/           # smolagents factory + sessions
│   └── tools/           # @tool implementations
└── tests/
```

## Management

```bash
./manage.sh -e dev up -d      # Start dev
./manage.sh -e dev down        # Stop
./manage.sh -e dev logs -f     # Tail logs
./manage.sh -e dev build       # Rebuild
```
