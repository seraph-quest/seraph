---
sidebar_position: 1
---

# Phase 1 — Persistent Identity

**Goal**: Seraph remembers. The human exists between sessions.

**Status**: Implemented

---

## 1.1 Database Layer

**Packages**: `sqlmodel>=0.0.22`, `aiosqlite>=0.21.0`, `alembic>=1.14.0`

**Files**:
```
backend/src/db/
  __init__.py
  engine.py          # async engine + session factory
  models.py          # all SQLModel table classes
```

**`engine.py`** — Async SQLite engine:
```python
# Connection: sqlite+aiosqlite:///data/seraph.db
# Settings: check_same_thread=False, expire_on_commit=False
# Lifespan: create tables on startup via create_app() lifespan
```

**`models.py`** — Database tables:

| Table | Key Columns | Purpose |
|-------|------------|---------|
| `Session` | id, title, created_at, updated_at | Chat session metadata |
| `Message` | id, session_id (FK), role, content, metadata_json, step_number, tool_used, created_at | All chat messages |
| `Memory` | id, content, category, source_session_id, embedding_id, created_at | Long-term memory entries |
| `Goal` | id, parent_id (FK self), path, level, title, description, status, domain, start_date, due_date, sort_order, created_at, updated_at | Hierarchical goal tree |
| `UserProfile` | id (singleton), name, soul_text, preferences_json, onboarding_completed, created_at, updated_at | User identity |

**Goal hierarchy** uses materialized path + adjacency list:
- `parent_id` for direct parent reference
- `path` string (e.g., `/v1/a3/q7/m2/`) for ancestor/descendant queries via `LIKE`
- `level` enum: vision / annual / quarterly / monthly / weekly / daily
- `domain` enum: productivity / performance / health / influence / growth
- Fixed max depth of 6 — SQLite recursive CTEs are fast at this depth

**`app.py`** — Async lifespan for DB init:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()  # create tables
    yield
    await close_db()
```

---

## 1.2 Chat Persistence

**Modified**: `backend/src/agent/session.py`
- Replaced in-memory `dict[str, Session]` with DB-backed `SessionManager`
- `get_or_create()` → async, queries/creates in SQLite
- `add_message()` → writes to `Message` table
- `get_history_text()` → queries last N messages from DB (configurable window, default 50)

**Modified**: `backend/src/api/ws.py` and `backend/src/api/chat.py`
- Session operations are `await`-able
- No protocol changes — WS message format stayed the same

**API endpoints**:
- `GET /api/sessions` — List all sessions with titles and last message preview
- `GET /api/sessions/{id}/messages` — Paginated message history
- `DELETE /api/sessions/{id}` — Delete a session
- `PATCH /api/sessions/{id}` — Update session title

**Frontend**:
- `frontend/src/components/chat/SessionList.tsx` — Sidebar showing past sessions
- `frontend/src/stores/chatStore.ts` — `sessions[]`, `activeSessionId`, `loadSessions()`, `switchSession()`; last selected session persisted to `localStorage` (`seraph_last_session_id`) and restored on WS connect
- `frontend/src/hooks/useWebSocket.ts` — Sends stored `session_id` on reconnect; restores last session messages on connect
- `frontend/src/components/chat/ChatPanel.tsx` — Session switcher in dialog frame, maximize (▲/▼) and close (✕) buttons

---

## 1.3 Soul & Memory System

**Packages**: `lancedb>=0.17.0`, `sentence-transformers>=3.4.0`

**Files**:
```
backend/src/memory/
  __init__.py
  soul.py            # Soul file read/write + system prompt injection
  embedder.py        # Sentence-transformer wrapper (lazy-loaded singleton)
  vector_store.py    # LanceDB operations (add, search, consolidate)
  consolidator.py    # Session → long-term memory extraction
```

**Soul system** (`soul.py`):
- Reads/writes `data/soul.md` — markdown with user identity, values, goals summary
- Injected into agent system prompt on every `create_agent()` call
- Tools: `view_soul()`, `update_soul(section, content)`

**Embedding service** (`embedder.py`):
- Model: `all-MiniLM-L6-v2` (22M params, 384 dimensions, ~80MB)
- Lazy-loaded singleton — first call loads model, subsequent calls reuse

**Vector store** (`vector_store.py`):
- LanceDB at `data/lance/`
- 384-dimension vectors
- `add_memory(text, category, source)` — embed + insert
- `search(query, top_k=5, category_filter=None)` → relevant memories
- Categories: `fact`, `preference`, `pattern`, `goal`, `reflection`

**Memory consolidation** (`consolidator.py`):
- After each conversation: summarize via LLM, extract facts/preferences/decisions
- Embed summaries and store in LanceDB
- Update soul.md if significant new information emerged
- Runs as background task (non-blocking)

**Modified**: `backend/src/agent/factory.py`
- `create_agent()` includes soul content, relevant memories, and goal progress in prompt

---

## 1.4 Goal Hierarchy & Quest System

**Files**:
```
backend/src/goals/
  __init__.py
  repository.py    # CRUD operations for Goal table
```

**API endpoints**:
- `GET /api/goals` — Full goal tree (filtered by level/domain/status)
- `POST /api/goals` — Create a goal
- `PATCH /api/goals/{id}` — Update goal (title, status, reparent)
- `DELETE /api/goals/{id}` — Delete goal and descendants
- `GET /api/goals/tree` — Nested tree structure
- `GET /api/goals/dashboard` — Per-domain progress stats

**Agent tools**:
- `create_goal(title, level, domain, parent_id, description, due_date)`
- `update_goal(goal_id, status, title)`
- `get_goals(level, domain, status)`
- `get_goal_progress()` — Summary across all domains

---

## 1.5 User Avatar & Quest Log UI

**Files**:
```
frontend/src/game/objects/UserSprite.ts
frontend/src/stores/questStore.ts
frontend/src/components/quest/
  QuestPanel.tsx
  GoalTree.tsx
  DomainStats.tsx
frontend/src/components/HudButtons.tsx
frontend/src/components/SettingsPanel.tsx
frontend/src/components/chat/DialogFrame.tsx
```

**UserSprite**: Clickable avatar near house-2, blue tunic, hover glow, emits `"toggle-quest-log"`

**QuestPanel layout** (side panel, right side):
```
┌─────────────────────────┐
│ QUEST LOG          [▲][✕]│
├─────────────────────────┤
│ ▸ Productivity  ████░ 72%│
│ ▸ Performance   ███░░ 58%│
│ ▸ Health        ██░░░ 41%│
│ ▸ Influence     ████░ 68%│
│ ▸ Growth        █████ 90%│
├─────────────────────────┤
│ ACTIVE QUESTS           │
│ ├─ Launch startup (Q3)  │
│ │  └─ Build MVP ███░ 75%│
│ └─ Read 24 books        │
│    └─ Feb: 2/2 ✓        │
└─────────────────────────┘
```

**Panel controls**:
- **DialogFrame** — Shared RPG frame component with optional maximize (▲/▼) and close (✕) buttons
- **Maximize**: Chat panel expands to nearly full screen (16px margins) with smooth CSS transition
- **Close**: Hides the panel; floating HUD buttons appear at bottom-left to reopen
- **HudButtons** — RPG-styled "Chat" / "Quests" / "Settings" buttons visible when respective panels are closed
- **SettingsPanel** — Standalone overlay panel (bottom-right) with restart onboarding option and version info

**Interaction**: Click Seraph sprite → chat panel toggle, Click User sprite → quest log toggle, or use ✕/HUD buttons

---

## 1.6 Onboarding Flow

**File**: `backend/src/agent/onboarding.py`
- RPG-themed onboarding conversation
- Discovers: name, role, top goals, what a great week looks like, obstacles
- Uses `update_soul` and `create_goal` tools during conversation
- After ~3 exchanges, marks onboarding complete

**Modified**: `backend/src/api/ws.py`
- First message checks onboarding status
- Routes to onboarding agent if not completed
- After 6+ messages, marks complete and switches to normal agent

---

## Implementation Order (as executed)

1. Database layer (engine.py, models.py) — foundation
2. Chat persistence (DB-backed SessionManager) — validates DB layer
3. Session API + frontend session list — complete chat UX
4. Soul system (soul.md, embedder, vector store) — memory foundation
5. Memory consolidation (background extraction) — connects chat to memory
6. Goal data model + API (repository, endpoints, tools) — quest system
7. User avatar + Quest panel UI — visual goal interface
8. Onboarding flow — ties it all together

## Verification Checklist

- [x] Start Docker, verify `seraph.db` created in data volume
- [x] Send messages via chat, restart backend, verify messages persist
- [x] Check session list API returns past conversations
- [x] Switch sessions in frontend, verify history loads correctly
- [x] Verify soul.md created after onboarding conversation
- [x] Send several conversations, verify vector search returns relevant memories
- [x] Create goals via chat, verify quest panel shows them with progress
- [x] Click User avatar → quest panel opens; click Seraph → chat opens
- [x] TypeScript compiles clean
- [x] All 10 tools register
- [x] All 18 routes verified
