# Capability Import Wave 1

## Scope

Wave 1 covers the runtime-parity slices from the capability import program:

1. `hermes-execute-code-runtime-v1`
2. `hermes-delegate-task-runtime-v1`
3. `hermes-clarify-runtime-v1`
4. `hermes-todo-runtime-v1`
5. `hermes-session-search-v1`
6. `hermes-bounded-memory-layer-v1`
7. `hermes-user-cron-runtime-v1`
8. `hermes-shell-process-runtime-v1`
9. `hermes-security-controls-v1`

## Slice Log

### 1. hermes-execute-code-runtime-v1

- status: complete
- intent:
  - add an explicit Hermes-style `execute_code` runtime surface
  - keep `shell_execute` as a compatibility alias until packaged skills/workflows migrate
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_shell_tool.py tests/test_tools_api.py -q`
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_shell_tool.py tests/test_tools_api.py tests/test_agent.py tests/test_specialists.py tests/test_approval_tools.py tests/test_tool_audit.py tests/test_capabilities_api.py -q`
  - `cd docs && npm run build`
  - `git diff --check`
- subagent review:
  - reviewer: `Hooke`
  - findings:
    - exposing both `execute_code` and `shell_execute` as first-class runtime tools would create ambiguous capability surfaces
    - the slice needed approval/audit coverage through the wrapped runtime path, not only direct tool tests
    - bundled capability manifests should switch to `execute_code` immediately to avoid carrying noisy dual-tool permissions
  - resolution:
    - `shell_execute` remains a compatibility alias only and is no longer exposed as a discovered native tool
    - approval/audit integration coverage was added through the agent tool-wrapper path
    - bundled default skills/catalog/extension manifests now reference `execute_code`

### 2. hermes-delegate-task-runtime-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_delegate_task_tool.py tests/test_tools_api.py tests/test_agent.py tests/test_delegation.py tests/test_specialists.py -q`
- subagent review:
  - reviewer: `Hooke`
  - findings:
    - the first draft bypassed the existing delegation feature flag and exposed specialist delegation on the monolithic agent path
    - the first draft allowed recursive delegation paths through `workflow_runner` because specialists and workflow composition still saw `delegate_task`
    - boundary coverage was too shallow until the slice added explicit disabled and nested-delegation regressions
  - resolution:
    - `delegate_task` is only exposed through the runtime tool surface when delegation mode is enabled
    - specialists and workflow composition now strip `delegate_task`, so the specialist graph cannot recurse back into delegation
    - the tool itself now guards against nested delegation and has direct regressions for disabled and recursive paths

### 3. hermes-clarify-runtime-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_clarify_tool.py tests/test_tools_api.py tests/test_agent.py tests/test_chat_api.py tests/test_e2e_conversation.py -q`
  - `cd frontend && npm test -- src/hooks/useWebSocket.test.ts src/stores/chatStore.test.ts src/components/chat/MessageBubble.test.tsx`
  - `cd frontend && npm run build`
  - `git diff --check`
- subagent review:
  - reviewer: `Hooke`
  - findings:
    - the initial REST clarification path stranded newly created sessions because the 409 response did not carry a restorable `session_id`
    - the first pass collapsed clarification into generic assistant text, which lost typed question/reason/options structure at the UI boundary
    - the first pass lacked frontend regressions for the clarification transport and rendering path
    - the follow-up pass still failed persistence parity because stored clarification prompts reloaded as plain assistant messages instead of a first-class clarification surface
    - helper-only frontend tests were not enough until the slice covered actual restore and render behavior
  - resolution:
    - REST clarification responses now return and persist `session_id`, so fresh-thread clarification requests do not strand the session
    - clarification is a first-class native tool and frontend message role with dedicated `question`, `reason`, and `options` fields
    - backend session history now persists clarification metadata, and the chat store restores that metadata back into the dedicated clarification role on reload
    - frontend regressions now cover transport helpers, restored session history mapping, and rendered clarification options chips

### 4. hermes-todo-runtime-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_todo_tool.py tests/test_session.py tests/test_sessions_api.py tests/test_tools_api.py tests/test_agent.py -q`
  - `git diff --check`
- subagent review:
  - reviewer: `Raman`
  - follow-up reviewer: `Plato`
  - findings:
    - invalid `remove` refs originally fell through as successful clears instead of returning a not-found error
    - numeric todo refs accepted `0` and `00` even though the public contract is 1-based indexing
    - cached audit payload on the shared tool instance could race across sessions and leak stale summary/details into later `tool_result` events
    - raw todo contents were being copied into audit details, which would persist sensitive text typed into a checklist item
    - nullable `items` or `item_id` inputs could be coerced into the literal string `"None"` and pollute persisted todo content
  - resolution:
    - missing todo refs now return `None` from the session layer and surface explicit not-found errors in the tool
    - numeric ref resolution now rejects non-positive indices before any lookup
    - `TodoTool` now stores audit payload in a per-call `ContextVar` instead of a shared instance attribute, and the payload is consumed after logging so cross-session races cannot reuse it
    - audit details now keep only ids and completion state for todo items, never raw content, and direct tool plus agent regressions pin that contract
    - nullable inputs are normalized to empty strings before parsing, and a direct tool regression covers the `None` case
    - follow-up review caught a second leak through the generic audited-wrapper argument path, so `TodoTool` now publishes sanitized audit arguments for `tool_call`, `tool_result`, and `tool_failed` events and no longer echoes raw `item_id` values either

### 5. hermes-session-search-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_session_search_tool.py tests/test_session.py tests/test_sessions_api.py tests/test_tools_api.py tests/test_agent.py -q`
  - `git diff --check`
- subagent review:
  - reviewer: `Sagan`
  - findings:
    - the first SQL title/message search draft treated `%` and `_` as live wildcard characters, so literal user queries containing those characters matched unrelated threads
    - snippets were truncated from the start of the message, so a late match could be reported without showing the actual matching context
    - title-only hits ranked by `Session.updated_at`, which was also mutated by unrelated todo edits and could distort recall order
    - the REST API accepted whitespace-only search input while the tool rejected the same input as invalid
  - resolution:
    - session search now escapes `LIKE` metacharacters and uses `escape='\\'` so `%` and `_` are treated literally unless the query actually contains them
    - message snippets now center the bounded excerpt around the first match instead of blindly truncating from the start
    - title hits now rank by conversation recency instead of todo-driven `updated_at` mutations
    - API and session-layer regressions now cover current-session exclusion, literal wildcard searches, late-match snippets, todo-safe ordering, and whitespace-only query rejection

### 6. hermes-bounded-memory-layer-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_guardian_state.py tests/test_strategist_tick.py tests/test_todo_tool.py -q`
  - `git diff --check`
- subagent review:
  - reviewer: `Arendt`
  - follow-up reviewer: `Plato`
  - findings:
    - bounded recall needed to land inside `build_guardian_state()` so chat, websocket, and strategist paths all shared the same deterministic layer
    - the slice should keep bounded recall separate from vector-memory `memory_context` instead of silently overloading semantic memory semantics
    - session todos were the strongest existing low-cost working-memory signal and should be favored over history summarization
    - the slice should avoid introducing a second writable soul/profile store just to support fast recall
  - resolution:
    - `GuardianState` now carries a dedicated `bounded_memory_context` block rendered ahead of semantic memory inside the guardian-state prompt surface
    - `build_guardian_state()` now synthesizes bounded recall deterministically from the existing guardian record plus persisted session todos, so chat and strategist share the same cheap recall layer automatically
    - the implementation uses the existing soul file as the human-authored profile source and session todos as the active-work signal, without adding a parallel writable memory store
    - follow-up review caught that guardian recency still depended on `Session.updated_at`, so recent-session summaries now rank by the latest user/assistant message timestamp instead of todo-only mutations

### 7. hermes-user-cron-runtime-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_scheduled_jobs.py tests/test_scheduled_job_tools.py tests/test_scheduler.py tests/test_tools_api.py tests/test_session.py -q`
  - `git diff --check`
- subagent review:
  - reviewer: `Descartes`
  - follow-up reviewer: `Hooke`
  - findings:
    - the first implementation exposed scheduled jobs globally, so one session could list, pause, resume, update, or delete another session's persisted cron jobs
    - dynamic scheduler sync originally loaded only 200 rows and used that limited set to remove stale APScheduler jobs, which would eventually unschedule older persisted jobs once the database crossed that threshold
    - `deliver_message` runs initially persisted `result.audit_decision`, which could report `delivered` even when the delivery layer had already marked the intervention outcome as `failed`
    - startup sync could abort the whole app on one malformed persisted row because `build_cron_trigger()` / `_scheduler.add_job()` exceptions were not isolated per job
    - scheduled jobs survived session deletion, leaving orphaned cron jobs pointing at dead session ids
    - follow-up review also found a no-session impersonation path through explicit `session_id`, sync failures bubbling after a successful DB write, raw runtime error text leaking back through `last_error`, and malformed `urgency` strings crashing before the tool's normal validation path
  - resolution:
    - scheduled job tools are now scoped to the active runtime session, reject cross-session targeting, and filter list/update/delete/pause/resume operations through ownership-aware repository queries
    - scheduler sync now loads the full persisted job set, skips malformed rows instead of aborting startup, and keeps APScheduler state aligned without truncating older jobs
    - scheduled delivery runs now prefer the persisted guardian intervention outcome when one exists, so failed transport is recorded as `failed` instead of a synthetic `delivered`
    - session deletion now removes scheduled jobs bound to or created by that session, preventing orphaned cron routines from firing after thread removal
    - scheduler resync failures after DB mutation now degrade into an explicit warning message instead of raising through the tool surface
    - persisted/public scheduler errors are reduced to safe exception labels, and invalid `urgency` input now returns a normal tool error instead of crashing the tool
    - post-fix re-review requests were sent to `Descartes`, `Hooke`, and `Zeno`; those follow-up checks timed out before the slice was committed, so the recorded findings above are the last concrete reviewer feedback on this slice

### 8. hermes-shell-process-runtime-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

### 9. hermes-security-controls-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending
