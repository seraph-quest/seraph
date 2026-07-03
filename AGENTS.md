# Agent Guidelines

## What Seraph Is

Seraph is a local-first operator cockpit and agent runtime. The repo spans a
FastAPI backend, React cockpit/settings UI, scheduler jobs, screen observation
storage, local model routing, VLM screenshot analysis, reports, skills,
workflows, and external service adapters.

The current development topology is part of the product contract, not incidental
developer setup:

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> GPU VLM wrapper  http://192.168.1.26:8001
  -> GPU model server http://192.168.1.26:8000/v1
```

The VLM wrapper is run through Docker on the GPU server from
`/home/pawel/repos/vlm-screenshot-server`. The GPU model server is a separate
process on the same machine and may already be running. Seraph agents are
responsible for Seraph and the VLM wrapper lifecycle. SSH is for GPU
administration only: inventory, Docker deploy/restart, process inspection, and
logs. Seraph runtime traffic must go over the documented HTTP APIs.

Operator-shell reachability is authoritative for this topology. A normal
Terminal-launched receipt on July 3, 2026 proved
`ssh -o BatchMode=yes -o ConnectTimeout=5 jupyter true` exits `0` even though
Codex/Desktop-launched direct SSH can report `No route to host` for the same
alias. Seraph should not require the user to set up an SSH tunnel to reach the
GPU VLM API. If Codex/Desktop commands report `No route to host` or connection
failures for `192.168.1.26` while the operator shell can reach `jupyter`, treat
that as a Codex/app network limitation until proven otherwise. Document it in
the ticket receipt, but do not redesign the Seraph runtime around a tunnel.

Codex GPU administration is a separate access path from Seraph runtime traffic.
Use `ssh jupyter` only for GPU-host inventory and maintenance. That path has
confirmed host `jupyter`, user `pawel`, and
`/home/pawel/repos/vlm-screenshot-server`. Do not translate this admin route
into a Seraph user requirement or a `.env.dev` runtime base URL.

Two properties shape most Seraph decisions:

- Runtime truth must be operator-visible. If chat, screenshots, reports, or
  settings use local Gemma/VLM/GPU paths, the UI and APIs must say that, not a
  stale default model or fallback provider.
- Work should be queued, bounded, and priority-aware. One GPU means no fantasy
  parallelism. Active GPU work is allowed to finish; the next accepted job must
  be the highest-priority ready job, and background screenshot work should keep
  the GPU busy when higher-priority work is absent.

## Contribution Rubric

### What We Want

- Fix real reported behavior and the whole bug class. Reproduce the symptom on
  the current branch, identify the line or contract that makes it happen, and
  cover sibling paths that would fail the same way.
- Preserve Seraph's runtime contracts. Settings, status badges, scheduler
  receipts, API health, and docs must agree with the actually running backend,
  VLM wrapper, and GPU edge.
- Prefer explicit operational receipts over plausible code inspection. For
  lifecycle, model routing, Docker/VLM, queueing, or settings work, prove the
  live endpoint or command path before saying it works.
- Keep the core narrow and capability at the right layer. Extend existing
  scheduler jobs, settings APIs, runtime profiles, skills, or wrappers before
  adding new broad agent surfaces.
- Make failures visible and bounded. A missing Docker wrapper, broken GPU edge,
  invalid env value, stale settings fetch, or unavailable metadata path should
  fail loudly enough for the operator to recover without killing the app.
- Document shipped truth in `docs/implementation/` when runtime topology,
  workflow contracts, settings behavior, queueing, or user-visible operations
  change.

### What We Do Not Want

- Silent fallbacks that make the UI lie. Do not show OpenRouter, Grok, Codex, or
  any other default when the effective runtime path is local Gemma/VLM, and do
  not hide missing local runtime proof behind "configured" labels.
- New raw env knobs as the first solution for operator behavior. Prefer existing
  settings surfaces, runtime profile contracts, managed scripts, or documented
  config groups. If an env var is necessary, quote shell-sensitive values in
  `.env.*` and add a launcher guard or test when parsing would be fragile.
- Detached process tricks that only work in one terminal. Use the repo lifecycle
  commands and verify the managed status. In Codex/Desktop managed shells,
  foreground `local run` is the reliable observation mode.
- Poll loops, schedulers, or retries that can DoS the backend, starve chat, or
  leave the GPU idle while accepted work exists.
- "Fixes" that remove the feature instead of preserving the contract. If
  settings metadata is slow, keep controls usable with last-known state; do not
  solve it by disabling configuration.
- Claims that issues, project fields, PR review, services, or tests changed
  unless a tool confirmed the change.

## Standard Intake For User Requests

Every non-trivial user request that changes code, docs truth, workflow,
runtime behavior, strategy, tickets, settings, tests, or operator-visible state
must go through the tracked work route before it is called done.

If there is no existing ticket:

1. Search open and closed issues for the same bug, capability, workflow, or
   batch scope.
2. Reuse or refine an existing issue when it genuinely matches.
3. Create a new tracked issue only when no suitable issue exists.
4. Set or verify the GitHub Project fields required by Project Board Flow.
5. Use the issue body or a comment to capture the symptom/request, intended
   behavior, root cause or plan, acceptance criteria, and validation plan.

Emergency investigation can happen before ticket creation when the app is down
or evidence would disappear, but no commit, PR, merge, or completion claim may
happen before the issue exists unless the user explicitly says not to create a
ticket.

If the request is a bug report, reproduce or inspect the root cause before
patching. If the request is a feature or UX change, define the user-facing
behavior first, then implement the smallest complete version that satisfies it.
Tiny mechanical edits may share an existing ticket or PR only when they are
directly related; otherwise create a separate tracked issue so the board reflects
the actual work being done.

Do not mark work complete from code changes alone. Completion means the ticket
and PR are updated, focused tests or checks were run, runtime/UI behavior was
verified when relevant, critic findings were handled, and merge/project state is
accurate unless the user explicitly scoped completion to local implementation.

## Verify The Premise Before Fixing

Before treating something as a bug, verify both the symptom and the intended
design:

- Check the live surface the user sees. If the screenshot shows a status label,
  query the endpoint that feeds that label and inspect the frontend binding.
- Check runtime configuration as loaded by the actual launcher, not just the
  file on disk. Values with semicolons, quotes, shell expansion, Docker env
  files, and process managers can change what the backend receives.
- Trace the intended path before patching. For chat, distinguish direct local
  chat, onboarding, orchestrator, tool-using agent, and fallback routes. For
  screenshots, distinguish folder scan, pending observation storage, VLM
  analysis, digest/report synthesis, and settings summaries.
- Verify local services with concrete probes:

```bash
./manage.sh -e dev local status
curl -sS http://127.0.0.1:8004/health
curl -sS http://127.0.0.1:8004/api/runtime/status
curl -sS http://127.0.0.1:8004/api/settings/artifact-storage
curl -sS http://192.168.1.26:8001/health
curl -sS http://192.168.1.26:8001/health/backend
curl -sS http://192.168.1.26:8001/queue/status
```

If sandboxed localhost checks fail but the app is supposed to be running on the
host, rerun the same probe with the proper approval instead of assuming the
service is down.

If approved Codex/Desktop probes still fail against the GPU LAN address while
the operator shell succeeds, ask for or use an operator-shell receipt from the
same environment that launches Seraph. Do not count a local SSH forward as the
product proof; use it only to inspect GPU-side state.

For GPU-server inventory from Codex, `ssh jupyter` is acceptable only as the GPU
administration route. Use it to inspect
`/home/pawel/repos/vlm-screenshot-server`, Docker Compose, running model
processes, listeners, logs, and firewall state. Keep any output clearly labeled
as admin evidence, not direct Seraph runtime acceptance.

## Footprint Ladder For New Capability

Choose the smallest durable surface that solves the problem:

1. Extend an existing function, endpoint, scheduler job, or UI state path.
2. Extend an existing runtime profile, settings API, or managed script.
3. Add a focused helper module behind an existing API or job.
4. Add a skill or documented operator workflow.
5. Add a plugin/MCP/service adapter when the capability is optional or
   integration-specific.
6. Add a new core tool, broad API, or global scheduler lane only when the
   capability is fundamental and cannot fit the layers above.

When multiple features want the same category of behavior, design the shared
contract first. Do not merge one-off settings panels, queue semantics, provider
switches, or lifecycle paths that will fight each other later.

Feature-first discipline: do not spend a user-facing feature batch building
proof scaffolding, broad docs reconciliation, or claim gates while the actual
capability is still missing. Ship the capability with the focused receipts it
needs, then track broader proof work separately when necessary.

## Runtime And Lifecycle Rules

- Use `./manage.sh -e dev local run` for live observation in managed Codex
  sessions. Use `./manage.sh -e dev local up/down/status/logs` for normal local
  lifecycle. Do not start backend/frontend directly with `uvicorn`, `npm run
  dev`, or Vite unless the user explicitly asks.
- Keep `.env.*` shell-safe. Any value containing semicolons must be quoted
  because `manage.sh` sources env files as shell.
- Runtime status must report the effective path for the current operator
  surface. `/api/runtime/status` should describe `chat_agent`, while default
  model/provider values belong in explicit `default_*` fields.
- The Docker VLM wrapper health is not the same as GPU backend health. Check
  both `/health` and `/health/backend`.
- One-GPU scheduling is serial at the GPU. Seraph may keep a tiny feeder window
  to avoid idle time, but queue priority determines the next job.
- Background screenshot analysis must not block interactive chat. Chat and
  onboarding routes using local Gemma must be configured alongside screenshot
  and report routes.
- Settings pages must remain usable through partial metadata failures. Preserve
  last-known values and surface degraded metadata instead of disabling controls
  or crashing the modal.

## Module Routing Map

- Chat and WebSocket turns: `backend/src/api/ws.py`,
  `backend/src/api/chat.py`, `backend/src/agent/`.
- Local runtime/provider selection: `backend/src/local_runtime_profiles.py`,
  `backend/config/settings.py`, runtime status endpoints, and `.env.*`.
- Scheduler and GPU/VLM queue behavior: `backend/src/scheduler/`,
  `backend/src/observer/`, and VLM wrapper integration points.
- Screenshot folder and semantic analysis: `backend/src/observer/`,
  `backend/tests/test_observer_screen_artifacts.py`, and screenshot
  intelligence tests.
- Settings metadata and operator controls: `backend/src/api/settings.py`,
  `frontend/src/components/SettingsPanel.tsx`, and settings subcomponents.
- Chat transcript and streaming UI: `frontend/src/hooks/useWebSocket.ts`,
  `frontend/src/components/chat/`, and related tests.
- Lifecycle scripts and env loading: `manage.sh`, `env.dev.example`,
  `env.prod.example`, Docker/VLM wrapper docs.
- Shipped-truth docs: `docs/implementation/`; target-shape and evidence docs:
  `docs/research/`; historical/archive docs: `docs/docs/`.

Prefer these extension points before adding new broad modules or parallel UI
surfaces.

## Validation Matrix

| Change type | Required proof |
| --- | --- |
| Runtime topology or lifecycle | `./manage.sh -e dev local status`, relevant `/health` checks, and logs or live URL receipt. |
| Chat/local model routing | API or WebSocket probe proving effective provider/model path, plus transcript persistence check when turn behavior changes. |
| Streaming chat UI | Backend frame test, frontend reducer/rendering test, and live or mocked delta/final receipt. |
| Settings/UI truth | Endpoint payload inspection plus frontend binding or component test. |
| Scheduler/GPU queue | Priority/non-starvation test and proof that one-GPU serial semantics are preserved. |
| Screenshot/VLM analysis | Wrapper `/health`, backend `/health/backend`, ingestion/analysis tests, and operator-visible status receipt. |
| Docs-only workflow change | Link to owning doc, contradiction scan for stale guidance, and no claims of runtime change. |
| GitHub/project mutation | Duplicate issue search, issue/PR/project item IDs, and field verification after mutation. |
| Security/privacy/trust boundary | Focused negative tests or proof of fail-closed behavior, plus explicit residual risk. |

Skipped checks must be named with a reason and residual risk. Do not substitute
generic "tests pass" for the proof surface above.

## Git Branching Strategy

**Never commit directly to `develop` or `main`.**
**Never create draft pull requests.**
**Pull requests should complete whole milestones or batch-sized work, not tiny slices.**
**When the user directs a stacked batch train, do all selected board batches as stacked ready PRs and report back only when the stack is complete.**

1. **Feature/fix branches**: Always create a `feat/` or `fix/` branch for your work. By default branch from `develop`.
2. **Stacked batch trains**: If the user explicitly asks to stack board batches, create the first batch branch from `develop`, then create each following batch branch from the previous batch branch. Open each PR against the previous branch so the stack can be reviewed and merged in order.
3. **Merge to develop**: For normal unstacked work, merge the feature branch into `develop` via PR. For stacked trains, merge the stack in order until the first branch lands in `develop`.
4. **Merge to main**: Only merge `develop` into `main` when explicitly requested by the user.
5. **Ready PRs only**: Pull requests must be opened ready for review unless the user explicitly requests a draft.
6. **Batch scope**: Default PR scope is a complete milestone or substantial batch. Use issue checklists, child issues, and internal commits for slices, but keep the team working until the batch acceptance criteria are complete.

### CI And Release Gates

- Hosted CI is not the authoritative gate for ordinary feature/fix PRs. When
  GitHub Actions is slow, flaky, or blocked by runner/dependency noise, do not
  burn the batch trying to make CI perfect; run the required local validation
  from the Validation Matrix and record the local receipts in the PR.
- Do not ignore local failures. Focused local tests and relevant runtime probes
  must pass before implementation is called complete or a merge is requested.
- If hosted CI exposes a real product regression or deterministic test failure
  in the changed scope, fix it in the current PR. If it is unrelated
  infrastructure or stale test-suite behavior, document it as deferred
  release-gate work instead of expanding the feature PR indefinitely.
- CI failures become release blockers at the `develop` -> `main` boundary.
  Before merging `develop` into `main` or cutting a new product release,
  inspect current CI failures, fix real regressions or stale CI tests, and
  rerun the affected local and hosted checks.

```text
feat/my-feature  ->  develop  ->  main
fix/my-bugfix    ->  develop  ->  main

feat/batch-one   -> develop
feat/batch-two   -> feat/batch-one
feat/batch-three -> feat/batch-two
```

## Docs And Execution Contract

- `docs/research/` is the target-shape, evidence, and comparative-truth layer.
- `docs/implementation/` is the shipped-truth and strategic implementation layer for `develop`.
- `docs/docs/` is the archive and historical layer.
- The GitHub Project is the execution layer.
- GitHub issues and PRs are the active work-tracking layer.
- PR bodies carry branch-specific scope, validation, and review receipts.
- Do not use docs as a live queue, branch tracker, or kanban mirror.

## Team Lead Operating Model

For substantial planning, roadmap, architecture, implementation, or review work,
Codex acts as the team lead.

Substantial work includes any task that changes strategy or docs truth, touches
two or more modules, affects security, memory, runtime, agent behavior, project
tracking, or requires validation beyond a single narrow check.

- The lead owns the plan, decomposition, sequencing, scope boundaries, tradeoff
  calls, and final synthesis.
- The lead must create or update the agent team to fit the task, plan, and risk
  profile before execution starts.
- The agent team should fit the work. Typical roles include Planner, Explorer,
  Worker, Security, Memory, Docs, Integrator, and Critic/Contrarian. If subagent
  tooling is unavailable, the lead must run separate named passes and state that
  limitation.
- The lead must delegate bounded work to agents with explicit ownership, file or
  module scope, acceptance criteria, proof requirements, and expected output.
- The lead must plan batches around whole milestones or substantial milestone
  slices, then keep the team working until the batch is complete rather than
  opening partial PRs for individual micro-slices.
- The lead must not directly implement substantial feature slices when a
  suitable worker agent can own them; the lead coordinates, reviews, integrates,
  and decides. The lead may directly implement small surgical changes, emergency
  fixes, or work where delegation tooling is unavailable, but must state the
  reason.
- If agent capacity or tooling prevents delegation, the lead must state that
  limitation and keep any direct edits tightly scoped.
- Before execution starts, the lead must confirm the branch is not `develop` or
  `main` and follows the `feat/` or `fix/` branch rule.
- Delegated agents are not alone in the codebase. They must not revert unrelated
  edits, rewrite strategy, broaden scope, or change milestone order without lead
  direction.
- The lead owns GitHub Project correctness when creating or refining tracked
  work, including the fields defined in Project Board Flow.
- The lead must verify material subagent claims before using them for code
  changes, commits, PRs, project updates, issue updates, release notes, roadmap
  decisions, or strategic claims.
- PR bodies for substantial work should summarize the agent team used, the
  Critic/Contrarian result, and the verification performed.

### Agent Team Execution Packet

Every delegated task must include:

- role and owner name
- scope and non-goals
- files, modules, or surfaces owned
- acceptance criteria
- proof required before the result can be used
- expected output format
- timeout or fallback behavior

Use `.agents/README.md` and the role files in `.agents/` for reusable packet and
handoff formats.

### Required Critic / Contrarian Role

Every non-trivial plan, roadmap, competitive analysis, architecture change,
security-sensitive change, memory change, or PR-sized slice must include at
least one Critic/Contrarian agent pass.

The Critic/Contrarian must be independent from the worker assumptions. No
same-pass self-approval: the critic receives the plan, diff, evidence, or issue
set and produces a separate critique.

The Critic/Contrarian agent checks:

- hallucinations, weak evidence, and unsupported competitive claims
- stale assumptions about current products, agents, models, APIs, or security posture
- missing current-source verification for any competitive or modern technical claim likely to have changed; temporally unstable claims about competitors, models, APIs, security posture, releases, or current product capabilities require official/current source URLs and dates
- missing acceptance criteria, proof, evals, or operator-visible receipts
- security, privacy, memory, and trust-boundary gaps
- scope creep, vague milestones, duplicate issues, and time-bounded roadmap drift; before creating issues, search open and closed issues for similar scope
- contradictions between docs, GitHub Project state, issues, PRs, and shipped behavior where relevant
- weak evidence standards: competitive claims need source URLs, code claims need file paths or line numbers, and GitHub/Project claims need issue, PR, or project item IDs
- false completion claims; do not imply files, issues, tests, project fields, or PR state changed unless tools confirm it

The lead must run the Critic/Contrarian pass before irreversible project actions
such as issue creation, PR creation, branch merge, roadmap finalization, or
public superiority claims.

The lead should incorporate the critique, explicitly reject it with rationale,
or turn it into follow-up issues before finalizing. Record the disposition as
accepted, rejected, or deferred in the PR body, issue comment, final response, or
relevant docs.

## Project Board Flow

- When a tracked issue is created or refined, set `Queue`, `Lane`, `Priority`,
  `Size`, `Status=Todo`, `Code Review=Not Ready`, and `PR=Not Ready`.
- Default batch mode: use one parent batch issue as the project item and one
  aggregate PR linked to that parent issue.
- Track internal slices in the parent batch issue itself by default. That parent
  issue checklist is the authoritative slice list unless a slice becomes its own
  child issue.
- Create child slice issues only when a slice has separate ownership, is a
  blocker, has independent acceptance criteria, or could be reprioritized
  separately.
- If child slice issues exist, they may carry their own `Queue` and `Status`,
  but keep `PR=Not Ready` and `Code Review=Not Ready` unless they get their own
  PR. Do not mirror one aggregate PR across every child issue.
- When work starts, set `Status=In Progress` and move `Queue=Now` if the task is
  active now.
- The issue remains the project item. Use built-in linked pull requests for the
  PR relationship instead of creating a second standalone project item for the
  same tracked work.
- When an aggregate PR opens, link it to the parent batch issue, set that issue
  `PR=Open`, and set that issue `Code Review=Pending`.
- While review is running, set `Code Review=Running`, then move to
  `Changes Requested` or `Passed`.
- When the PR merges, set `PR=Merged` and `Status=Done`.

## Review Rule

- Every PR-sized slice must be reviewed before merge. Non-trivial PR-sized
  slices require an independent subagent review.
- Every pushed update to an open PR that changes behavior, security/privacy
  posture, runtime wiring, settings, docs truth, tests, or workflow contract must
  receive a fresh independent Critic/Contrarian pass before the lead claims the
  PR is reviewed, updates the PR as review-passed, or asks to merge.
- Small follow-up commits are not exempt when they affect the same PR's
  acceptance criteria or operator-visible behavior. Treat them as part of the
  PR-sized slice and re-run the critic on the cumulative diff or the changed
  follow-up scope.
- A lead's own named "critic pass" is not a substitute when subagent tooling is
  available. Use an independent subagent critic; only fall back to a separate
  self-run critic pass when subagent tooling is unavailable, and state that
  limitation in the PR/final response.
- Verify subagent claims before acting on them.
- Record material review findings, or an explicit no-findings result, in the PR
  body and in affected implementation docs when the slice changes shipped truth
  or workflow contract.
- Do not merge a PR until material review findings are either fixed, explicitly
  rejected with rationale, or turned into tracked follow-up work.
