---
sidebar_position: 5
---

# Testing Guide

Seraph has 624 automated tests (500 backend, 124 frontend) with CI running on every push and PR.

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
| `test_agent.py` | 8 | Agent factory — tool count, model creation, context injection |
| `test_catalog_api.py` | 9 | Catalog API — browse catalog, install skills/MCP servers |
| `test_chat_api.py` | 5 | REST chat endpoint — success, session continuity, errors |
| `test_consolidation_reliability.py` | 6 | Memory consolidation reliability — edge cases, retry behavior |
| `test_consolidator.py` | 5 | Memory consolidation — extract facts, soul updates, markdown fences, LLM failure |
| `test_context_window.py` | 17 | Token-aware context window — budget management, keep first/last, summarization |
| `test_daily_briefing.py` | 5 | Daily briefing — happy path, context/LLM failure, empty data, events in prompt |
| `test_delegation.py` | 10 | Delegation architecture — orchestrator, specialist routing, depth limits |
| `test_delivery.py` | 9 | Delivery coordinator — deliver/queue/drop routing, budget decrement, bundle formatting |
| `test_e2e_conversation.py` | 3 | End-to-end conversation flow — full agent interaction paths |
| `test_evening_review.py` | 8 | Evening review — happy path, no goals/messages, DB/LLM failure, date filtering |
| `test_goal_tree_integrity.py` | 12 | Goal tree integrity — parent-child relationships, path consistency, cascading |
| `test_goals_api.py` | 10 | Goals HTTP endpoints — create, list, filter, tree, dashboard, update, delete |
| `test_goals_repository.py` | 21 | GoalRepository — CRUD, tree building, dashboard stats, cascading deletes |
| `test_http_mcp_server.py` | 16 | HTTP MCP server — request handling, internal URL blocking, timeout, truncation |
| `test_insight_queue.py` | 12 | Insight queue — enqueue, drain, peek, ordering, expiry |
| `test_insight_queue_expiry.py` | 8 | Insight queue expiry — TTL, cleanup, edge cases |
| `test_mcp_api.py` | 3 | MCP HTTP API endpoints — list, add, remove servers |
| `test_mcp_manager.py` | 31 | MCP server integration — connect, disconnect, failure handling, token auth, env var resolution |
| `test_observer_api.py` | 7 | Observer API endpoints — state, context POST, daemon status |
| `test_observer_calendar.py` | 3 | Calendar observer source — event parsing, caching |
| `test_observer_git.py` | 6 | Git observer source — commit parsing, branch detection |
| `test_observer_goals.py` | 4 | Goals observer source — active goals summary |
| `test_observer_manager.py` | 20 | ContextManager — refresh, state transitions, budget reset |
| `test_observer_time.py` | 12 | Time observer source — time-of-day, working hours, timezone |
| `test_onboarding_edge_cases.py` | 2 | Onboarding edge cases — skip, restart |
| `test_plugin_loader.py` | 5 | Tool auto-discovery — scan, expected tools, no duplicates, caching, reload |
| `test_profile.py` | 7 | User profile + onboarding — get/create, mark/reset complete, HTTP endpoints |
| `test_scheduler.py` | 12 | Scheduler engine — job registration, start/stop, job execution |
| `test_seed_config.py` | 7 | Seed config — default MCP servers, default skills, first-run seeding |
| `test_session.py` | 23 | SessionManager — async DB-backed CRUD, history, pagination, title generation |
| `test_sessions_api.py` | 8 | Session HTTP endpoints — list, messages, update title, delete |
| `test_settings_api.py` | 6 | Settings API — interruption mode get/set |
| `test_shell_tool.py` | 7 | Shell execution — success, errors, size limits, timeout, connection errors |
| `test_skills.py` | 27 | Skills system — loading, gating, enable/disable, frontmatter parsing, API |
| `test_soul.py` | 9 | Soul file persistence — read/write, section update, ensure exists |
| `test_specialists.py` | 28 | Specialist agents — factory, tool domains, MCP specialist generation |
| `test_strategist.py` | 12 | Strategist agent — JSON parsing (valid, fenced, invalid, empty, partial), agent creation |
| `test_timeouts.py` | 5 | Execution timeouts — agent, briefing, consolidation timeouts |
| `test_tool_registry.py` | 4 | Tool metadata registry — lookup, required fields, copy safety |
| `test_tools.py` | 11 | Filesystem tools, template tool, web search |
| `test_user_state.py` | 57 | User state machine — derive_state, IDE deep work, should_deliver, budget, interruption modes |
| `test_vault_api.py` | 4 | Vault API — list keys, delete keys |
| `test_vault_crypto.py` | 4 | Vault crypto — Fernet encrypt/decrypt, key generation |
| `test_vault_repository.py` | 11 | Vault repository — store, get, list, delete, upsert |
| `test_vault_tools.py` | 7 | Vault agent tools — store_secret, get_secret, list_secrets, delete_secret |
| `test_websocket.py` | 3 | WebSocket — ping/pong, invalid JSON, skip onboarding |

### Frontend (`frontend/src/`)

| File | Tests | Coverage |
|---|---|---|
| `game/objects/SpeechBubble.test.ts` | 25 | Speech bubble — show/hide, positioning, text wrapping, timeout, animation |
| `stores/chatStore.test.ts` | 16 | Zustand chat store — sync actions (messages, panels, visual state) + async actions (profile, sessions, onboarding) |
| `game/lib/mapParsers.test.ts` | 15 | Map parsers — magic effect pool building, animation parsing, custom properties |
| `hooks/useKeyboardShortcuts.test.ts` | 14 | Keyboard shortcuts — Shift+C/Q/S, Escape, input focus exclusion |
| `lib/toolParser.test.ts` | 12 | Tool detection — regex patterns, fallback substring match, Phase 1/2/MCP tools |
| `game/objects/MagicEffect.test.ts` | 12 | Magic effects — pool cycling, spawn, fade, destroy lifecycle |
| `stores/questStore.test.ts` | 10 | Zustand quest store — goal CRUD, tree, dashboard, filters, refresh |
| `lib/animationStateMachine.test.ts` | 10 | Animation targets — tool→position mapping, facing direction, idle/thinking states |
| `hooks/useWebSocket.test.ts` | 6 | WebSocket hook — connect, reconnect, message dispatch, ping |
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

- **Phaser game objects** (VillageScene, AgentSprite, UserSprite, SpeechBubble) — require WebGL context, fragile mocking
- **EventBus.ts** — single-line Phaser EventEmitter wrapper
- **Browser tool** — thin wrapper around Playwright
- **LanceDB vector_store.py** — requires real embeddings model loaded
- **Full WS message streaming** — complex sync/async interaction with agent streaming; basic WS tests cover ping, error handling, and skip_onboarding

## CI/CD

Tests run automatically on every push and PR to `main` via GitHub Actions (`.github/workflows/test.yml`).

Two parallel jobs:
- **backend-tests**: Ubuntu, Python 3.12, `uv sync --group dev`, `uv run pytest -v`
- **frontend-tests**: Ubuntu, Node 20, `npm ci`, `npm test`

Redundant runs are cancelled automatically via concurrency groups.
