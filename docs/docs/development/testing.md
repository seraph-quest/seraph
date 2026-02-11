---
sidebar_position: 5
---

# Testing Guide

Seraph has 207 automated tests (152 backend, 55 frontend) with CI running on every push and PR.

## Running Tests

### Backend

```bash
cd backend
uv sync --group dev            # Install dev dependencies (first time)
uv run pytest -v               # Run all tests with coverage
uv run pytest --no-cov         # Run without coverage (faster)
uv run pytest tests/test_session.py -v  # Run a single file
uv run pytest -k "test_create" # Run tests matching a pattern
```

Coverage is configured by default in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "--cov=src --cov-report=term-missing"
```

### Frontend

```bash
cd frontend
npm install                    # Install dependencies (first time)
npm test                       # Run all tests (single run)
npm run test:watch             # Watch mode (re-runs on changes)
npm run test:coverage          # Run with coverage report
```

Frontend tests use [Vitest](https://vitest.dev/) with jsdom, configured in `vite.config.ts`.

## Test Structure

### Backend (`backend/tests/`)

| File | Tests | Coverage |
|---|---|---|
| `test_session.py` | 18 | SessionManager — async DB-backed CRUD, history, pagination, title generation |
| `test_goals_repository.py` | 18 | GoalRepository — CRUD, tree building, dashboard stats, cascading deletes |
| `test_goals_api.py` | 10 | Goals HTTP endpoints — create, list, filter, tree, dashboard, update, delete |
| `test_sessions_api.py` | 8 | Session HTTP endpoints — list, messages, update title, delete |
| `test_profile.py` | 7 | User profile + onboarding — get/create, mark/reset complete, HTTP endpoints |
| `test_soul.py` | 7 | Soul file persistence — read/write, section update, ensure exists |
| `test_shell_tool.py` | 7 | Shell execution — success, errors, size limits, timeout, connection errors |
| `test_consolidator.py` | 5 | Memory consolidation — extract facts, soul updates, markdown fences, LLM failure |
| `test_plugin_loader.py` | 5 | Tool auto-discovery — scan, expected tools, no duplicates, caching, reload |
| `test_mcp_manager.py` | 5 | MCP server integration — connect, disconnect, failure handling |
| `test_chat_api.py` | 5 | REST chat endpoint — success, session continuity, errors |
| `test_agent.py` | 4 | Agent factory — tool count, model creation, context injection |
| `test_tool_registry.py` | 4 | Tool metadata registry — lookup, required fields, copy safety |
| `test_tools.py` | 9 | Filesystem tools, template tool, web search |
| `test_websocket.py` | 3 | WebSocket — ping/pong, invalid JSON, skip onboarding |
| `test_delivery.py` | 9 | Delivery coordinator — deliver/queue/drop routing, budget decrement, bundle formatting |
| `test_insight_queue.py` | 9 | Insight queue — enqueue, drain, peek, ordering, expiry |
| `test_observer_manager.py` | 5 | ContextManager — refresh, state transitions, budget reset |
| `test_user_state.py` | 9 | User state machine — derive_state, should_deliver, budget management |
| `test_strategist.py` | 12 | Strategist agent — JSON parsing (valid, fenced, invalid, empty, partial), agent creation |
| `test_daily_briefing.py` | 5 | Daily briefing — happy path, context/LLM failure, empty data, events in prompt |
| `test_evening_review.py` | 8 | Evening review — happy path, no goals/messages, DB/LLM failure, date filtering |

### Frontend (`frontend/src/`)

| File | Tests | Coverage |
|---|---|---|
| `stores/chatStore.test.ts` | 16 | Zustand chat store — sync actions (messages, panels, visual state) + async actions (profile, sessions, onboarding) |
| `lib/toolParser.test.ts` | 15 | Tool detection — all 5 regex patterns, fallback substring match, Phase 1/2/Things3 tools |
| `lib/animationStateMachine.test.ts` | 12 | Animation targets — tool→position mapping, facing direction, idle/thinking states |
| `stores/questStore.test.ts` | 8 | Zustand quest store — goal CRUD, tree, dashboard, filters, refresh |
| `config/constants.test.ts` | 4 | Constant integrity — tool count, position ranges, scene keys, waypoint count |

## Writing New Tests

### Backend: Using the `async_db` Fixture

All database-dependent tests use the shared `async_db` fixture from `conftest.py`. It creates an in-memory SQLite database and patches `get_session` across all modules.

```python
from src.agent.session import SessionManager

async def test_example(async_db):
    sm = SessionManager()
    session = await sm.get_or_create("test-id")
    assert session.title == "New Conversation"
```

For HTTP endpoint tests, use the `client` fixture (which depends on `async_db`):

```python
async def test_list_goals(client):
    res = await client.get("/api/goals")
    assert res.status_code == 200
```

### Frontend: Mocking Fetch

Store tests mock `globalThis.fetch` and reset store state between tests:

```typescript
import { vi, beforeEach } from "vitest";
import { useChatStore } from "./chatStore";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

beforeEach(() => {
  useChatStore.setState({ messages: [], sessionId: null });
  vi.clearAllMocks();
});
```

## What Is NOT Tested

These areas are intentionally excluded from the test suite:

- **Phaser game objects** (StudyScene, AgentSprite, UserSprite, SpeechBubble) — require WebGL context, fragile mocking
- **EventBus.ts** — single-line Phaser EventEmitter wrapper
- **Browser/Calendar/Email tools** — thin wrappers around OAuth-dependent libraries
- **LanceDB vector_store.py** — requires real embeddings model loaded
- **Full WS message streaming** — complex sync/async interaction with agent streaming; basic WS tests cover ping, error handling, and skip_onboarding

## CI/CD

Tests run automatically on every push and PR to `main` and `develop` via GitHub Actions (`.github/workflows/test.yml`).

Two parallel jobs:
- **backend-tests**: Ubuntu, Python 3.12, `uv sync --group dev`, `uv run pytest -v`
- **frontend-tests**: Ubuntu, Node 20, `npm ci`, `npm test`

Redundant runs are cancelled automatically via concurrency groups.
