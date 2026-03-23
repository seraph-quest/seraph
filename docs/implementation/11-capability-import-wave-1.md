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

- status: pending
- validation:
  - pending
- subagent review:
  - pending

### 3. hermes-clarify-runtime-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

### 4. hermes-todo-runtime-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

### 5. hermes-session-search-v1

- status: pending
- validation:
  - pending
- subagent review:
  - pending

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
