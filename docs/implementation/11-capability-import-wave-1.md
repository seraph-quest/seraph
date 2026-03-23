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
  - reviewer: `Galileo`
  - findings:
    - invalid `remove` refs originally fell through as successful clears instead of returning a not-found error
    - numeric todo refs accepted `0` and `00` even though the public contract is 1-based indexing
    - stale audit payload from a previous successful call could leak into a later error-path `tool_result`
    - nullable `items` or `item_id` inputs could be coerced into the literal string `"None"` and pollute persisted todo content
  - resolution:
    - missing todo refs now return `None` from the session layer and surface explicit not-found errors in the tool
    - numeric ref resolution now rejects non-positive indices before any lookup
    - `TodoTool` clears its cached audit payload on every early-error path, and a direct agent audit regression now proves the stale payload cannot leak
    - nullable inputs are normalized to empty strings before parsing, and a direct tool regression covers the `None` case

### 5. hermes-session-search-v1

- status: complete
- validation:
  - `cd backend && UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_session_search_tool.py tests/test_session.py tests/test_sessions_api.py tests/test_tools_api.py tests/test_agent.py -q`
  - `git diff --check`
- subagent review:
  - reviewer: `Zeno`
  - findings:
    - the first SQL title/message search draft treated `%` and `_` as live wildcard characters, so literal user queries containing those characters matched unrelated threads
    - session-search coverage was missing a direct API regression for excluding the current session from the bounded recall list
  - resolution:
    - session search now escapes `LIKE` metacharacters and uses `escape='\\'` so `%` and `_` are treated literally unless the query actually contains them
    - API and session-layer regressions now cover current-session exclusion and literal wildcard searches

### 6. hermes-bounded-memory-layer-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

### 7. hermes-user-cron-runtime-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

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
