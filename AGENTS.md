# Agent Guidelines

## Standing Operator Direction: No Evals

Do not propose, plan, create, or run evals unless the operator explicitly asks
for evals. This includes paid or unpaid campaigns, benchmarks, harness research,
hidden quality corpora, model judges, synthetic substitutes, and canaries used
to measure quality or improvement. Do not spend money on evals or make them a
future feature, migration, activation, merge, or release gate. Available credits,
credentials, a free tier, or a local model do not grant permission.
Capability work comes first. Any later eval requires the implemented capability
and a new explicit operator request; do not schedule a reminder or dependency.

Implementation and acceptance must make no paid, live, free-tier, alternate-
provider, or local-model inference calls and must not use ambient credentials.
Keep external inference disabled or intercepted at the transport boundary.
Ordinary isolated regression, integration, and security tests, actual local
effects/readback, and independent code review remain required; no evals does
not mean no tests. Tests establish the checked mechanics, not model quality or
competitive superiority.

Build capabilities, security, the accepted Cordis architecture, and reviewed
self-improvement from ordinary task traces, explicit corrections, and skills.
Do not revive cancelled [#771](https://github.com/seraph-quest/seraph/issues/771)
as a learning or activation dependency; the completed
[#736](https://github.com/seraph-quest/seraph/issues/736) already excludes it.
This direction does not disable product inference for ordinary operator-
approved tasks under existing consent, grants, routing, and budget contracts.
Historical receipts and runtime proof interfaces do not authorize an eval or
an inference call during implementation or acceptance. See the
[canonical boundary](docs/implementation/00-project-constitution.md#no-evals-and-provider-free-implementation).

## Start Here

Seraph is a local-first proactive guardian and operator cockpit. It owns goals,
policy, capabilities, memory, durable work and audit; model providers supply
inference only. The current implementation is a Python/FastAPI backend and
React/TypeScript cockpit with optional context and service adapters.

The accepted direction is an **all-plugin Cordis agent architecture**, defined
by [ADR-026](docs/implementation/decisions/026-all-plugin-cordis-architecture.md).
The agent loop, tools, model integration, persistence, scheduling and interfaces
will be plugins; only necessary bootstrap/composition machinery stays outside
them. Migration capabilities are **Planned**, not implemented by this guide.
Extend current owning modules until a reviewed migration milestone replaces
them; do not mix an unapproved rewrite into ordinary fixes.

Before substantial work, read the following owners and relevant ADRs:

| Document | Authority |
| --- | --- |
| [Project Constitution](docs/implementation/00-project-constitution.md) and ADRs | Sole product-definition and accepted-target authority |
| [Current App Guide](docs/implementation/12-current-app-guide.md) | Current topology, operator journeys and lifecycle |
| [Development Status](docs/implementation/STATUS.md) | Shipped/Partial truth on `develop` and proof limits |
| [Documentation Contract](docs/implementation/08-docs-contract.md) | Ownership, status vocabulary and required docs checks |
| [.agents/README.md](.agents/README.md) and role files | Delegation packets, handoffs and durable recovery |

GitHub issues, PRs and the Project own execution state; docs are not a queue or
branch tracker. `docs/research/` contains dated evidence and alternatives, never
accepted targets. `docs/docs/` is historical archive. `CLAUDE.md` is a
compatibility pointer to this guide, not a second instruction authority.

## Repository Layout

These are current paths, not a proposed Cordis package layout:

```text
backend/
  src/                 FastAPI application and Seraph runtime modules
  config/              Typed settings and configuration
  tests/               Backend tests and bounded fixtures
  scripts/             Backend validation, native-profile and CI helpers
  containers/          Optional execution-container definitions
  pyproject.toml       Python dependencies and pytest configuration
  uv.lock              Locked Python dependencies
frontend/
  src/components/      React cockpit, task inspectors, chat and settings
  src/hooks/           UI state and WebSocket hooks; colocated tests
  src/lib/             API contracts/helpers and colocated tests
  package.json         Frontend build and test commands
daemon/                Optional desktop context daemon, OCR adapters and tests
companions/            Optional browser companion (selected-text)
mcp-servers/           External-service MCP adapter implementations
examples/              Example extension/capability material
assets/                Shared source assets
artifacts/             Checked-in project artifacts, not the canonical workspace
docs/
  implementation/      Constitution, ADRs and shipped operator contracts
  research/            Dated evidence, uncertainty and alternatives
  docs/                Historical archive served under /legacy
  src/                 Documentation site components
scripts/               Repository checks and managed operations helpers
.agents/               Agent roles, packets and handoff/recovery protocol
.codex/                Repository Codex skills and environment setup
.github/               CI, issue and PR templates
manage.sh              Supported environment and service lifecycle entry point
env.dev.example        Development configuration template
env.prod.example       Production configuration template
docker-compose.*.yaml  Development and production container topology
```

Ignored `.agent-worktrees/` and `.agent-evidence/` hold durable local agent
checkouts and private receipts; create them as needed. They are not backups.

## Commands And Prerequisites

Use Python 3.12+ and `uv` for the backend. The frontend lockfile requires Node
`^20.19.0 || ^22.12.0 || >=24.0.0` through its test dependencies; docs declares
Node >=20. Native repair tests have separately pinned runtime requirements.
Docker and native sandbox/browser dependencies belong to selected execution
profiles, not CPU-core or documentation prerequisites.

These commands are grounded in `manage.sh`, package manifests and
`.github/workflows/test.yml`. A command reference is not a passing-test receipt.

| Purpose | Working directory | Command / prerequisite |
| --- | --- | --- |
| Create dev configuration | Repository root | `cp env.dev.example .env.dev` only when `.env.dev` is absent; preserve existing configuration, then set intended local workspace/auth settings |
| Install backend dependencies | `backend/` | `uv sync --locked --group dev` |
| Install frontend or docs dependencies | `frontend/` or `docs/` | `npm ci` using that directory's lockfile |
| Observe managed local app | Repository root | `./manage.sh -e dev local run`; requires dev config and backend/frontend dependencies; keep session open |
| Normal lifecycle | Repository root | `./manage.sh -e dev local up`, `./manage.sh -e dev local down`, `./manage.sh -e dev local status` |
| Follow service logs | Repository root | `./manage.sh -e dev local logs backend` or `./manage.sh -e dev local logs frontend` |
| Focused backend test example | `backend/` | `uv run pytest tests/test_model_fabric_selector.py`; choose the owning test for the change |
| Focused frontend test example | `frontend/` | `npm test -- src/hooks/useWebSocket.test.ts --maxWorkers=1` |
| Frontend typecheck and build | `frontend/` | `npm run build` runs `tsc -b` then Vite |
| Docs ownership and claim checks | Repository root | `python3 scripts/check_docs_contract.py` and `python3 scripts/check_strategy_claims.py` |
| Docs typecheck and build/link check | `docs/` | `npm run typecheck` and `npm run build` |
| Diff whitespace check | Repository root | `git diff --check` |

There is no dedicated frontend lint script or configured backend lint/typecheck
command in these manifests. Do not invent one or claim a build is a lint pass.
Use focused tests first; native/browser suites have additional profile-specific
requirements documented by their owners. Never weaken assertions, deadlines or
fixtures to make a check pass. Do not run tests against an operator workspace.

Core startup does not require a provider key. Inference requires explicit
configuration, consent, verified capability and budget; a configured route is
not proof of live provider availability. The default local browser is
`http://127.0.0.1:3001`, backend `http://127.0.0.1:8004`. Use managed lifecycle
commands, not direct `uvicorn`, Vite or `npm run dev`, unless explicitly asked.
Production requires the documented private compose/HTTPS deployment; the plain
HTTP `local` stack is development-only.

## Find The Owning Module

| Task | Start here |
| --- | --- |
| Chat, turns and streaming | `backend/src/api/chat.py`, `backend/src/api/ws.py`, `backend/src/agent/`, `frontend/src/hooks/useWebSocket.ts`, `frontend/src/components/chat/` |
| Governed inference, routes and accounting | `backend/src/model_fabric/`, `backend/src/llm_runtime.py`, `backend/src/api/model_fabric_settings.py`, `backend/config/settings.py` |
| Durable tasks, scheduling and intervention | `backend/src/work_board/`, `backend/src/scheduler/`, `backend/src/guardian/`, `backend/src/workflows/` |
| Goals, canonical memory and persistence | `backend/src/goals/`, `backend/src/memory/`, `backend/src/db/`, `backend/src/workspace/`, `backend/src/artifacts/` |
| Permissions, approvals, secrets and audit | `backend/src/auth/`, `backend/src/security/`, `backend/src/approval/`, `backend/src/vault/`, `backend/src/audit/` |
| Tools, capability packs and integrations | `backend/src/tools/`, `backend/src/native_tools/`, `backend/src/extensions/`, `backend/src/execution/`, `backend/src/integrations/`, `mcp-servers/` |
| Screenshots and selected context | `backend/src/observer/`, `backend/src/api/selected_context.py`, `daemon/`, `companions/selected-text/`; tests include `backend/tests/test_observer_screen_artifacts.py` |
| Settings and UI truth | `backend/src/api/settings.py`, `frontend/src/components/SettingsPanel.tsx`, settings subcomponents and `frontend/src/lib/` |
| Lifecycle and configuration loading | `manage.sh`, `env.dev.example`, `env.prod.example`, `backend/production_preflight.py` |

## Implementation And Runtime Rules

- Reproduce a bug or inspect its root cause before patching. Query the endpoint
  behind the visible symptom and trace the frontend binding. Distinguish chat,
  onboarding and orchestrator paths, or capture, ingestion, analysis and report
  paths. Verify configuration as loaded by the real launcher, not only on disk.
- Define user-facing behavior before implementation. Fix sibling paths in the
  same bug class and keep the smallest complete capability. Extend an existing
  function, job or UI path first, then configuration/lifecycle, then a focused
  helper, skill, or adapter. A new broad tool/API/queue needs a reason existing
  owners cannot serve it. Share contracts before parallel features introduce
  competing settings, provider or queue semantics.
- Update the owning `docs/implementation/` guide and status when runtime
  topology, lifecycle, queueing, settings or user-visible operations change.
  Keep branch-specific scope and validation in the PR; never present an open
  branch's behavior as shipped `develop` truth.
- Follow neighboring Python typing/async conventions and strict TypeScript
  contracts. Keep authority and side effects in backend owners; the cockpit
  projects state and recovery. Separate validation, policy, transport and durable
  adoption. Avoid hidden global state and import-time side effects in new seams
  intended for later plugin lifecycle ownership.
- Add dependencies through the owning manifest and lockfile only when needed.
  Do not introduce another package manager or install Cordis as part of ordinary
  fixes. The migration decides package/version and bridging strategy under ADR-026.
- Prefer existing settings/profile surfaces over raw env knobs. `manage.sh`
  sources `.env.*` as shell: quote shell-sensitive values, especially semicolons,
  and cover fragile parsing with a launcher guard or test. Never commit secrets,
  private captures, databases, credentials or private evidence; use the existing
  vault and redacted receipts. Do not print secrets to prove configuration.
- Preserve canonical workspace ownership, additive migration and backup/restore
  contracts. Do not reset operator data to repair a test. Use existing repository,
  transaction, lifecycle-lock and artifact-adoption seams; preserve their lock
  ordering and current-authority checks. A storage/plugin adapter does not
  authorize a second canonical store or migration framework.
- Ordinary text, vision and embedding inference uses governed OpenRouter.
  [ADR-025](docs/implementation/decisions/025-near-https-text-inference.md) permits
  only the separate optional NEAR HTTPS text capability, not a general route or
  fallback. Providers remain inference-only; no coding-agent runtime may replace
  Seraph authority.
- Preserve one shared bounded remote-inference lane, highest-priority ready
  admission, original deadlines/cancellation/idempotency, durable reservations,
  cost accounting and unknown-liability recovery. Its in-flight bound is one;
  do not add a lane per plugin/provider. Background analysis must not starve chat
  and requires its own current consent, budget and capability gates.
- CPU hosts remain usable without GPU/model services or the VLM wrapper.
  macOS/Linux are peer core-host targets; optional native profiles require their
  own actual platform receipts. Local inference stays inactive/blocked rather
  than being probed as a prerequisite. Historical GPU/admin diagnostics belong
  in the [Current App Guide](docs/implementation/12-current-app-guide.md#historical-develop-topology),
  not startup acceptance. SSH is administration only, never runtime transport
  or a user-required tunnel; app-network failures do not disprove operator-shell
  reachability.
- Report effective runtime/model/queue/degraded state. `/api/runtime/status`
  describes `chat_agent`; defaults belong in explicit `default_*` fields. Never
  substitute a configured provider label for the actual path. Preserve last-known
  settings through metadata failures with visible recovery; do not disable
  configuration as the fix. Bound retries and polling.
- All-plugin composition does not make enforcement optional. Mandatory trusted
  policy/authority services fail closed if absent, unhealthy or disposed. Typed
  dependencies and lifecycle cleanup are required; dependency injection is not
  sandboxing. Runtime plugins, reviewed authored packs (ADR-013/020), and
  external/MCP adapters have different trust boundaries. Loading/installing a
  plugin grants no observation, egress, execution or learning authority.

## Intake, Branches And Project Flow

Every non-trivial change to code, docs truth, workflow, runtime, strategy,
tickets, settings, tests or operator-visible state needs tracked work before
completion. Search open **and closed** issues, reuse/refine a genuine match, or
create one parent batch issue. Capture request/symptom, intended behavior,
root cause or plan, acceptance criteria and validation. Emergency investigation
may precede intake, but no commit, PR, merge or completion claim precedes the
issue unless the user explicitly waives ticket creation. Tiny unrelated edits
need their own tracking; directly related mechanical edits may share a ticket.

- Confirm a `feat/` or `fix/` branch before execution. Never commit directly to
  `develop` or `main`. Normally branch from and target current `develop`.
- Follow an applicable epic integration ADR when the owning issue requires it.
  [ADR-005](docs/implementation/decisions/005-epic-integration-branch-workflow.md)
  defines the completed #736 epic's workflow; it does not make that historical
  integration branch the base for new unrelated work.
- Use one complete milestone/substantial batch and aggregate **ready** PR,
  never a draft unless explicitly requested. Internal commits and issue
  checklists may represent smaller slices; do not open partial micro-PRs.
- For a user-directed stacked train, first branch from `develop`, then each
  branch from its predecessor and target the predecessor. Complete all selected
  batches before reporting the stack ready; integrate in order. Only promote
  `develop` to `main` on explicit user request.

The lead owns and verifies these Project transitions:

| Event | Required fields/state |
| --- | --- |
| Create/refine issue | Set `Queue`, `Lane`, `Priority`, `Size`, `Status=Todo`, `Code Review=Not Ready`, `PR=Not Ready` |
| Start active work | `Status=In Progress`, `Queue=Now` |
| Open aggregate PR | Link parent issue; `PR=Open`, `Code Review=Pending` |
| Independent review | `Code Review=Running`, then `Changes Requested` or `Passed` according to evidence |
| Merge | `PR=Merged`, `Status=Done` |

Keep the issue as the Project item and use linked PRs, not a duplicate PR item.
The parent checklist owns slices. Create children only for separate ownership,
blockers, independent acceptance or reprioritization. Children may have their
own Queue/Status, but keep PR/Code Review Not Ready unless they own a PR; never
mirror one aggregate PR across every child. Verify field mutations with IDs.

Reuse scoped GitHub/Git approvals. Write multiline bodies with `apply_patch` to
a workspace file, then use `--body-file`; avoid shell-expanded inline bodies.
Skip and report optional receipts that cannot use existing approvals. Request
only narrow reusable permission when required to complete the task.

## Team, Review And Recovery

For substantial planning, roadmap, architecture, implementation or review work,
Codex is team lead. This includes strategy/docs-truth changes, two or more
modules, security, memory, runtime, agent behavior, tracking, or validation
beyond a single narrow check.

- Before execution, form a team that fits the risks. Delegate substantial
  implementation to a suitable worker; the lead plans, sequences, integrates
  and verifies. Direct implementation is limited to surgical/emergency work or
  unavailable delegation, with the reason stated. Agents must not revert
  unrelated edits, rewrite strategy, broaden scope or reorder milestones.
- Every delegation specifies role/owner, scope/non-goals, files/surfaces,
  acceptance, proof, expected output and timeout/fallback. Use `.agents/` packet
  and handoff formats. Verify material agent claims before commits, PRs, Project
  mutations, roadmap decisions or completion statements.
- Use an independent Critic/Contrarian for every non-trivial plan, roadmap,
  competitive analysis, architecture/security/memory change and PR-sized slice.
  Review before issue creation, PR creation, merge, roadmap finalization or
  public superiority claims. Self-approval is not a substitute; if agents are
  unavailable, run separately named passes and state the limitation.
- The critic checks unsupported claims, current official sources and dates for
  unstable technical/competitive claims, acceptance/proof gaps, trust/privacy/
  memory boundaries, scope and duplicate issues, docs/code/Project contradictions
  and false completion. Code claims need paths/lines; GitHub claims need IDs.
  Accept, reject with rationale, or defer findings to tracked work and record
  their disposition. No material finding may disappear silently.
- Every PR-sized slice is reviewed before merge. Every substantive pushed
  update requires fresh independent cumulative or follow-up review before
  claiming review passed or asking to merge, including tests, docs truth and
  workflow changes. Record team, verification and material findings (or explicit
  no-findings) in the PR and affected implementation docs when shipped truth or
  workflow changes. Do not claim review from a stale revision.
- Use repository-local ignored `.agent-worktrees/` and `.agent-evidence/`, with
  private evidence permissions. Do not use `/tmp` for retained work/evidence.
  Disposable private fixtures may use temporary storage when the runtime
  contract requires; retain needed receipts before cleanup. Checkpoint source
  frequently and push recoverable feature-branch checkpoints; never push private
  data. Handoffs record exact commit/base, ownership and validation. After an
  interruption verify surviving work and rerun missing receipts. A checkpoint
  is not review, completion or permission for a partial PR.

## Validation And Completion

Prefer observable receipts to plausible code inspection. For live checks start
with managed status and `/health`; use authenticated `/api/runtime/status` and
`/api/settings/artifact-storage` for runtime/settings truth. An unauthenticated
denial is not a broken service. If sandboxed localhost fails while the host app
should be running, retry the same probe with proper approval before claiming it
is down. Keep local, mocked, hosted and historical live-provider proof distinct.
Apply the standing no-evals and provider-free implementation boundary above to
every check; an effective-route receipt uses intercepted inference transport.

| Change | Required proof |
| --- | --- |
| Topology/lifecycle | Managed status, relevant health and logs/live URL |
| Chat/model routing | API or WebSocket effective-route receipt; transcript persistence when turns change |
| Streaming UI | Backend frames, frontend reducer/render test, live or mocked delta/final receipt |
| Settings/UI truth | Endpoint payload and frontend binding/component test |
| Scheduling/inference admission | Priority/non-starvation, shared serial bound and relevant durable recovery tests |
| Screenshot/OpenRouter vision | Policy/admission/ingestion tests and operator-visible state; no local wrapper readiness prerequisite |
| Docs/workflow/architecture | Owning-doc links, path/command and contradiction checks, both docs scripts and docs typecheck/build; no runtime-change claim |
| GitHub/Project | Duplicate search, issue/PR/item IDs and post-mutation field readback |
| Security/privacy/trust | Focused negative/fail-closed proof and explicit residual risk |

Name skipped checks, reasons and residual risk. Hosted CI is advisory for
ordinary feature/fix PRs when runners/dependencies are noisy; required local
checks and relevant runtime probes must pass. Fix deterministic in-scope product
failures; track unrelated infrastructure/stale-suite work separately. At
`develop` → `main` or release, CI failures become blockers: inspect, fix real
regressions/stale tests and rerun affected local and hosted checks.

A capability is **Shipped** only with an observable vertical slice, not a
registry entry, settings surface, deterministic scenario or benchmark endpoint:

1. Stable identity and typed inputs/outputs.
2. Declared permissions, limits, policy, approvals and runtime dependencies.
3. Bounded accepted job with owner, priority, retry/cancel and idempotency.
4. Real execution through the governed runtime.
5. Durable artifact/checkpoint/audit/effective-route receipts.
6. Verification or external readback of the intended outcome.
7. Explicit canonical-memory update or explicit no-learning result.
8. Operator-visible success, degraded, blocked and recovery states.

Mark non-applicable elements explicitly. Proactive behavior must prove goal or
standing intent → candidate → admission/priority → approval/reservation →
execution → evidence/readback → recorded outcome → governed memory update. Periodic
model calls or delivered messages alone do not satisfy that contract.

Keep feature batches focused on the actual capability with its necessary proof;
do not substitute broad proof scaffolding or docs reconciliation for delivery.
Completion means issue/PR state, focused validation, relevant runtime/UI proof,
critic disposition and merge/Project state agree, unless explicitly scoped to
local work or a ready PR. Never claim a file, test, service, issue, review or
Project state changed without a confirming tool receipt.
