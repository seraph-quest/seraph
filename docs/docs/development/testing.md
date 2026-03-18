---
sidebar_position: 5
---

# Testing Guide

Seraph has automated backend and frontend coverage with CI running on every push and PR.

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

### Runtime Evals

For `S1-B3` reliability work, there is also a deterministic eval harness for core guardian/runtime contracts:

```bash
cd backend
uv run python -m src.evals.harness --list
uv run python -m src.evals.harness
uv run python -m src.evals.harness --scenario rest_chat_behavior
uv run python -m src.evals.harness --scenario rest_chat_approval_contract
uv run python -m src.evals.harness --scenario rest_chat_timeout_contract
uv run python -m src.evals.harness --scenario websocket_chat_behavior
uv run python -m src.evals.harness --scenario websocket_chat_approval_contract
uv run python -m src.evals.harness --scenario websocket_chat_timeout_contract
uv run python -m src.evals.harness --scenario strategist_tick_behavior
uv run python -m src.evals.harness --scenario guardian_state_synthesis
uv run python -m src.evals.harness --scenario observer_refresh_behavior
uv run python -m src.evals.harness --scenario observer_delivery_decision_behavior
uv run python -m src.evals.harness --scenario native_presence_notification_behavior
uv run python -m src.evals.harness --scenario native_desktop_shell_behavior
uv run python -m src.evals.harness --scenario intervention_policy_behavior
uv run python -m src.evals.harness --scenario guardian_feedback_loop
uv run python -m src.evals.harness --scenario provider_fallback_chain
uv run python -m src.evals.harness --scenario provider_health_reroute
uv run python -m src.evals.harness --scenario local_runtime_profile
uv run python -m src.evals.harness --scenario helper_local_runtime_paths
uv run python -m src.evals.harness --scenario context_window_summary_audit
uv run python -m src.evals.harness --scenario agent_local_runtime_profile
uv run python -m src.evals.harness --scenario delegation_local_runtime_profile
uv run python -m src.evals.harness --scenario delegated_tool_workflow_behavior
uv run python -m src.evals.harness --scenario delegated_tool_workflow_degraded_behavior
uv run python -m src.evals.harness --scenario workflow_composition_behavior
uv run python -m src.evals.harness --scenario mcp_specialist_local_runtime_profile
uv run python -m src.evals.harness --scenario embedding_runtime_audit
uv run python -m src.evals.harness --scenario vector_store_runtime_audit
uv run python -m src.evals.harness --scenario soul_runtime_audit
uv run python -m src.evals.harness --scenario vault_runtime_audit
uv run python -m src.evals.harness --scenario filesystem_runtime_audit
uv run python -m src.evals.harness --scenario runtime_model_overrides
uv run python -m src.evals.harness --scenario runtime_fallback_overrides
uv run python -m src.evals.harness --scenario runtime_profile_preferences
uv run python -m src.evals.harness --scenario runtime_path_patterns
uv run python -m src.evals.harness --scenario provider_policy_capabilities
uv run python -m src.evals.harness --scenario provider_policy_scoring
uv run python -m src.evals.harness --scenario provider_routing_decision_audit
uv run python -m src.evals.harness --scenario session_bound_llm_trace
uv run python -m src.evals.harness --scenario session_consolidation_behavior
uv run python -m src.evals.harness --scenario scheduled_local_runtime_profile
uv run python -m src.evals.harness --scenario daily_briefing_fallback
uv run python -m src.evals.harness --scenario daily_briefing_delivery_behavior
uv run python -m src.evals.harness --scenario shell_tool_runtime_audit
uv run python -m src.evals.harness --scenario browser_runtime_audit
uv run python -m src.evals.harness --scenario web_search_runtime_audit
uv run python -m src.evals.harness --scenario web_search_empty_result_audit
uv run python -m src.evals.harness --scenario observer_calendar_source_audit
uv run python -m src.evals.harness --scenario observer_git_source_audit
uv run python -m src.evals.harness --scenario observer_goal_source_audit
uv run python -m src.evals.harness --scenario observer_time_source_audit
uv run python -m src.evals.harness --scenario observer_delivery_gate_audit
uv run python -m src.evals.harness --scenario observer_delivery_transport_audit
uv run python -m src.evals.harness --scenario observer_daemon_ingest_audit
uv run python -m src.evals.harness --scenario mcp_test_api_audit
uv run python -m src.evals.harness --scenario skills_api_audit
uv run python -m src.evals.harness --scenario tool_policy_guardrails_behavior
uv run python -m src.evals.harness --scenario screen_repository_runtime_audit
uv run python -m src.evals.harness --scenario daily_briefing_degraded_memories_audit
uv run python -m src.evals.harness --scenario activity_digest_degraded_delivery_behavior
uv run python -m src.evals.harness --scenario activity_digest_degraded_summary_audit
uv run python -m src.evals.harness --scenario evening_review_degraded_delivery_behavior
uv run python -m src.evals.harness --scenario evening_review_degraded_inputs_audit
```

This runner does not call external providers. It exercises core seams with controlled mocks so REST and WebSocket chat behavior, guardian-state synthesis, guardian feedback loop behavior, intervention policy behavior, observer salience/confidence/interruption-cost behavior, observer refresh and delivery behavior, native notification fallback behavior, native desktop presence status plus the safe test-notification path, session consolidation behavior, strategist and scheduled proactive flow behavior, delegated tool-heavy workflow behavior, reusable workflow composition behavior, ordered fallback routing, health-aware provider rerouting, runtime-path profile preferences, wildcard runtime-path rules, capability-aware runtime policy intents, weighted provider policy scoring, structured routing decision auditing, session-bound helper LLM trace visibility, runtime-path primary and fallback overrides, local helper/agent/all current scheduled-job/delegation/MCP-specialist profile routing, embedding-model, vector-store, soul-file, vault-repository, and filesystem boundary failures, context-window degradation, daily-briefing, activity-digest, and evening-review degraded-input fallback auditing, tool/MCP policy guardrails, proactive delivery transport, daemon ingest, manual MCP test API auth-required/success/failure behavior, skills toggle/reload audit behavior, screen observation summary/cleanup boundary behavior, observer source availability and time/goal summaries, sandbox, browser, filesystem, and web-search timeout/empty-result auditing, tool degradation behavior, and audit visibility for strategist/helper paths stay easy to verify after reliability changes.

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
| `test_browser_tool.py` | 3 | Browser tool — blocked internal URLs plus success and timeout runtime audit logging |
| `test_chat_api.py` | 5 | REST chat endpoint — success, session continuity, errors |
| `test_consolidation_reliability.py` | 6 | Memory consolidation reliability — edge cases, retry behavior |
| `test_consolidator.py` | 5 | Memory consolidation — extract facts, soul updates, markdown fences, LLM failure |
| `test_context_window.py` | 19 | Token-aware context window — budget management, keep first/last, summarization, runtime audit logging |
| `test_activity_digest.py` | 6 | Activity digest — skip/no data, happy path, runtime path, timeout, degraded summary-input audit visibility |
| `test_daily_briefing.py` | 8 | Daily briefing — happy path, context/LLM failure, empty data, events in prompt, degraded memory-input audit visibility |
| `test_delegation.py` | 10 | Delegation architecture — orchestrator, specialist routing, depth limits |
| `test_delivery.py` | 20 | Delivery coordinator — deliver/queue/drop routing, intervention persistence, native notification fallback, budget decrement, and bundle formatting |
| `test_embedder.py` | 3 | Embedding model boundary — load success, load failure, encode failure runtime audit logging |
| `test_e2e_conversation.py` | 3 | End-to-end conversation flow — full agent interaction paths |
| `test_evening_review.py` | 10 | Evening review — happy path, no goals/messages, DB/LLM failure, date filtering, degraded-input audit visibility |
| `test_goal_tree_integrity.py` | 12 | Goal tree integrity — parent-child relationships, path consistency, cascading |
| `test_goals_api.py` | 10 | Goals HTTP endpoints — create, list, filter, tree, dashboard, update, delete |
| `test_goals_repository.py` | 21 | GoalRepository — CRUD, tree building, dashboard stats, cascading deletes |
| `test_guardian_feedback.py` | 2 | Guardian feedback repository — intervention persistence, outcome updates, explicit feedback, summary generation |
| `test_guardian_state.py` | 4 | Guardian-state synthesis — state assembly, confidence/salience labels, recent feedback injection, agent injection, strategist context |
| `test_intervention_policy.py` | 7 | Intervention policy — explicit act, bundle, defer, request-approval, stay-silent, high-interruption bundling, and low-salience suppression decisions |
| `test_http_mcp_server.py` | 16 | HTTP MCP server — request handling, internal URL blocking, timeout, truncation |
| `test_insight_queue.py` | 12 | Insight queue — enqueue, drain, peek, ordering, expiry |
| `test_insight_queue_expiry.py` | 8 | Insight queue expiry — TTL, cleanup, edge cases |
| `test_mcp_api.py` | 7 | MCP HTTP API endpoints — token update, manual server test auth/success/failure flows, and runtime audit logging |
| `test_mcp_manager.py` | 31 | MCP server integration — connect, disconnect, failure handling, token auth, env var resolution |
| `test_observer_api.py` | 19 | Observer API endpoints — state, context POST, daemon status, safe native test notification enqueue, native notification poll/ack, and explicit intervention feedback |
| `test_observer_calendar.py` | 4 | Calendar observer source — event parsing, empty/failure handling, runtime audit logging |
| `test_observer_git.py` | 7 | Git observer source — commit parsing, missing repo/reflog handling, runtime audit logging |
| `test_observer_goals.py` | 4 | Goals observer source — active goals summary and runtime audit logging |
| `test_observer_manager.py` | 26 | ContextManager — refresh, salience/confidence/interruption-cost derivation, state transitions, budget reset |
| `test_screen_observation.py` | 14 | Screen observation repository — create, backfill, summaries, cleanup, and runtime audit logging |
| `test_observer_time.py` | 14 | Time observer source — time-of-day, working hours, timezone, runtime audit logging |
| `test_onboarding_edge_cases.py` | 2 | Onboarding edge cases — skip, restart |
| `test_plugin_loader.py` | 5 | Tool auto-discovery — scan, expected tools, no duplicates, caching, reload |
| `test_profile.py` | 7 | User profile + onboarding — get/create, mark/reset complete, HTTP endpoints |
| `test_scheduler.py` | 12 | Scheduler engine — job registration, start/stop, job execution |
| `test_seed_config.py` | 7 | Seed config — default MCP servers, default skills, first-run seeding |
| `test_session.py` | 23 | SessionManager — async DB-backed CRUD, history, pagination, title generation |
| `test_sessions_api.py` | 8 | Session HTTP endpoints — list, messages, update title, delete |
| `test_settings_api.py` | 6 | Settings API — interruption mode get/set |
| `test_shell_tool.py` | 9 | Shell execution — success, errors, size limits, timeout, connection errors, runtime audit logging |
| `test_skills.py` | 30 | Skills system — loading, gating, enable/disable, frontmatter parsing, API, and runtime audit logging for toggle/reload |
| `test_soul.py` | 12 | Soul file persistence — read/write, section update, ensure exists, runtime audit logging |
| `test_specialists.py` | 30 | Specialist agents — factory, tool domains, MCP specialist generation, runtime-path routing |
| `test_strategist.py` | 12 | Strategist agent — JSON parsing (valid, fenced, invalid, empty, partial), agent creation |
| `test_timeouts.py` | 5 | Execution timeouts — agent, briefing, consolidation timeouts |
| `test_tool_registry.py` | 4 | Tool metadata registry — lookup, required fields, copy safety |
| `test_tools.py` | 20 | Filesystem tools, template tool, web search, and filesystem/web-search runtime audit logging |
| `test_workflows.py` | 12 | Workflow composition — loader, gating, sequential execution, API, metadata, and delegation exposure |
| `test_user_state.py` | 57 | User state machine — derive_state, IDE deep work, should_deliver, budget, interruption modes |
| `test_vault_api.py` | 4 | Vault API — list keys, delete keys |
| `test_vault_crypto.py` | 4 | Vault crypto — Fernet encrypt/decrypt, key generation |
| `test_vault_repository.py` | 14 | Vault repository — store, get, list, delete, upsert, and runtime audit logging for success/missing/failure paths |
| `test_vault_tools.py` | 7 | Vault agent tools — store_secret, get_secret, list_secrets, delete_secret |
| `test_vector_store.py` | 3 | Vector store boundary — add success, search empty-result, add failure runtime audit logging |
| `test_websocket.py` | 3 | WebSocket — ping/pong, invalid JSON, skip onboarding |

### Frontend (`frontend/src/`)

| File | Tests | Coverage |
|---|---|---|
| `game/objects/SpeechBubble.test.ts` | 25 | Speech bubble — show/hide, positioning, text wrapping, timeout, animation |
| `stores/chatStore.test.ts` | 16 | Zustand chat store — sync actions (messages, panels, visual state) + async actions (profile, sessions, onboarding) |
| `game/lib/mapParsers.test.ts` | 15 | Map parsers — magic effect pool building, animation parsing, custom properties |
| `components/settings/DaemonStatus.test.tsx` | 2 | Native Presence card — daemon status rendering plus safe desktop test-notification enqueue/refresh |
| `components/cockpit/layouts.test.ts` | 4 | Cockpit layout presets — default/focus/review density expectations |
| `stores/cockpitLayoutStore.test.ts` | 4 | Cockpit layout store — preset switching, inspector visibility, reset behavior |
| `hooks/useKeyboardShortcuts.test.ts` | 6 | Keyboard shortcuts — cockpit composer focus, layout switching, inspector toggle, legacy panel handling, input focus exclusion |
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
- **LanceDB vector_store.py** — requires real embeddings model loaded
- **Full WS message streaming** — complex sync/async interaction with agent streaming; basic WS tests cover ping, error handling, and skip_onboarding

## CI/CD

Tests run automatically on pushes to `develop` and `main`, plus pull requests targeting either branch, via GitHub Actions (`.github/workflows/test.yml`).

Two parallel jobs:
- **backend-tests**: Ubuntu, Python 3.12, `uv sync --group dev`, `uv run pytest -v`
- **frontend-tests**: Ubuntu, Node 20, `npm ci`, `npm test`

Redundant runs are cancelled automatically via concurrency groups.
