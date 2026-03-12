---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: seraph-a8feffa30de5
  active_states:
    - Todo
    - In Progress
    - Human Review
    - Merging
    - Rework
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done
polling:
  interval_ms: 5000
workspace:
  root: ~/code/symphony-workspaces/seraph
hooks:
  after_create: |
    set -euo pipefail
    source_repo="${SOURCE_REPO_URL:-https://github.com/seraph-quest/seraph.git}"
    if git ls-remote --exit-code --heads "$source_repo" develop >/dev/null 2>&1; then
      git clone --depth 1 --branch develop "$source_repo" .
    else
      git clone --depth 1 "$source_repo" .
    fi
    if [ -f ./.codex/worktree_init.sh ]; then
      bash ./.codex/worktree_init.sh
    fi
agent:
  max_concurrent_agents: 10
  max_turns: 20
codex:
  command: codex --config shell_environment_policy.inherit=all --config model_reasoning_effort=xhigh --model gpt-5.3-codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_sandbox_policy:
    type: workspaceWrite
---

You are working on a Linear ticket `{{ issue.identifier }}` for the Seraph
repository.

{% if attempt %}
Continuation context:

- This is retry attempt #{{ attempt }} because the issue is still active.
- Resume from the current workspace state instead of restarting from scratch.
- Do not repeat already-completed investigation or validation unless new changes
  make it necessary.
{% endif %}

Issue context:
Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
Current status: {{ issue.state }}
Labels: {{ issue.labels }}
URL: {{ issue.url }}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

## Operating mode

1. This is an unattended orchestration session. Do not ask a human to do
   follow-up work unless you hit a real blocker such as missing auth,
   unavailable secrets, or a missing protected branch.
2. Work only inside the provided repository checkout.
3. Keep Linear updated as you go. Use one persistent comment headed
   `## Codex Workpad` as the source of truth for plan, validation, and handoff.
4. Use repo-local skills when relevant:
   - `linear` for raw Linear GraphQL operations.
   - `pull` to sync the branch with the integration branch before and during
     implementation.
   - `commit` for clean commits.
   - `push` to publish the branch and create or update the PR.
   - `land` when the issue reaches `Merging`.

## Branching and PR policy

- Never commit directly to `main` or `develop`.
- Work on a `feat/*` or `fix/*` branch created from `develop`.
- This repository expects feature branches to merge into `develop`, then
  `develop` merges into `main` only when a human explicitly requests it.
- If `origin/develop` does not exist yet, treat that as a blocker and record it
  in the workpad instead of opening a PR against `main`.

## Status map

- `Backlog`: out of scope for this workflow. Do not modify the issue.
- `Todo`: move to `In Progress` before active work.
- `In Progress`: implementation is active.
- `Human Review`: wait for human review or PR feedback.
- `Merging`: open `.codex/skills/land/SKILL.md` and follow it.
- `Rework`: create a fresh branch from `origin/develop` and redo the work.
- `Done`: terminal. Do nothing further.

## Execution flow

### Step 0: Check status and route

1. Fetch the issue and determine the exact current state.
2. Route immediately:
   - `Backlog` -> stop and wait.
   - `Todo` -> move to `In Progress`, then continue.
   - `In Progress` -> continue execution.
   - `Human Review` -> poll for PR review feedback and wait.
   - `Merging` -> run the `land` skill.
   - `Rework` -> create a fresh feature branch from `origin/develop`, then
     continue from the normal execution flow.
   - `Done` -> stop.
3. If a branch PR already exists and is `CLOSED` or `MERGED`, do not reuse the
   old branch. Create a fresh branch from `origin/develop` and restart.

### Step 1: Start or refresh the workpad

1. Find or create a single persistent Linear comment titled `## Codex Workpad`.
2. Reuse that same comment for every progress update; do not create a second
   workpad comment.
3. Keep this exact structure in the comment:

````md
## Codex Workpad

```text
<hostname>:<abs-path>@<short-sha>
```

### Plan

- [ ] 1. Parent task
- [ ] 2. Parent task

### Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2

### Validation

- [ ] `<command>`

### Notes

- <timestamped progress note>

### Confusions

- <only include when something was genuinely unclear>
````

4. Before changing code, update the workpad with:
   - a concise execution plan,
   - concrete acceptance criteria,
   - the validation commands you intend to run,
   - reproduction notes if the task is a bug fix.

### Step 2: Sync before implementation

1. Inspect `git status`, current branch, and current `HEAD`.
2. Run the `pull` skill before making code changes.
3. Record pull evidence in the workpad notes:
   - merge source,
   - whether the sync was clean or required conflict resolution,
   - resulting short SHA.

### Step 3: Implement

1. Reproduce the problem or inspect the existing behavior first.
2. Keep scope tight to the ticket.
3. Update the workpad after every meaningful milestone:
   - reproduction complete,
   - plan refined,
   - implementation complete,
   - validation complete,
   - PR created or updated.
4. If you discover out-of-scope work, create a separate backlog issue instead
   of expanding the current ticket.

### Step 4: Validate

Run the narrowest proof that directly covers the changed area. Use these repo
commands:

- Backend changes:
  `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test uv run pytest -v`
- Frontend changes:
  `cd frontend && npm test`
- Docs changes:
  `cd docs && npm run build`
- Editor changes:
  `cd editor && npm run build`
- Multi-area changes: run every affected command.

Validation rules:

- Do not push code until the required validation for the changed area passes.
- If validation fails, fix the issue and rerun.
- Record the exact commands and outcomes in the workpad.

### Step 5: Publish and hand off

1. Use the `push` skill to push the branch and create or update a PR targeting
   `develop`.
2. Ensure the PR is linked back to the Linear issue.
3. Ensure the PR has the `symphony` label.
4. Sweep PR feedback before handoff:
   - top-level PR comments,
   - inline review comments,
   - review summaries and requested changes.
5. Revalidate after feedback-driven changes.
6. Only move the issue to `Human Review` when:
   - acceptance criteria are complete,
   - validation is green,
   - the workpad is up to date,
   - no actionable review feedback remains,
   - the branch is pushed and the PR is open against `develop`.

### Step 6: Human Review and merge

1. In `Human Review`, do not make speculative changes. Wait for review input.
2. If feedback arrives, move the issue to `Rework` and implement the requested
   changes on a fresh attempt if needed.
3. When the issue moves to `Merging`, run the `land` skill until the PR is
   merged.
4. After merge completes, move the issue to `Done`.

## Blockers

Only stop early for real blockers such as:

- missing Linear auth,
- missing GitHub auth needed to push or inspect PRs,
- missing required secrets,
- missing `origin/develop` when a PR to `develop` is required.

When blocked:

1. Update the workpad with the exact blocker.
2. Explain why it blocks completion.
3. State the minimum human action needed to unblock it.
4. Move the issue to `Human Review` only if waiting on that external action.
