# Workstream 07: Ecosystem And Delegation

## Status On `develop`

- [ ] Workstream 07 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [08. Ecosystem And Delegation](/research/ecosystem-and-delegation)
- detailed strategy: [12. Extension Platform And MCP Strategy](/research/plugin-system-and-mcp-strategy)
- trusted-code decision: [13. Trusted Code Plugins RFC](/research/trusted-code-plugins-rfc)
- capability import plan: [13. Hermes And OpenClaw Capability Import Plan](/research/hermes-and-openclaw-capability-import-plan)

## Shipped On `develop`

- [x] `SKILL.md`-based skill loading
- [x] MCP-powered extension surface
- [x] runtime-managed MCP server configuration
- [x] catalog/install APIs for skills and MCP servers
- [x] recursive delegation foundations behind a feature flag
- [x] dynamic specialist generation for connected MCP servers
- [x] reusable workflow definitions that can activate across native tools, skills, specialists, and connected MCP capabilities
- [x] cockpit-native operator surface for workflow availability, tools, skills, starter packs, MCP server state, blocked-state reasons, and live runtime-policy visibility
- [x] first starter-pack bundles for recommended default skills and workflows
- [x] first workflow-runs history surface with boundary-aware replay metadata and artifact lineage
- [x] searchable capability command palette for tools, skills, workflows, starter packs, MCP actions, repair actions, and installable catalog items
- [x] denser operator terminal with recommendations, runbooks, repair actions, installable catalog entries, and deeper workflow timeline visibility
- [x] guided repair and install flows for blocked skills, workflows, tools, and MCP servers instead of only static blocked-state reasons
- [x] policy-aware starter-pack repair guidance, live operator-feed status, saved runbook macros, and approval-aware workflow timeline actions in the cockpit
- [x] capability preflight and autorepair payloads for workflows, starter packs, and runbooks before execution
- [x] bounded capability bootstrap that can apply low-risk local workflow or runbook repair actions from the cockpit while leaving policy lifts, external server enables, installs, and starter-pack activation as explicit operator decisions, with starter-pack install flows now prechecking the same lifecycle approvals as direct catalog installs
- [x] workflow diagnostics that expose stored load errors plus richer step timing, error summaries, and recovery hints for extension debugging
- [x] threaded operator timeline surfaces for workflow runs, approvals, notifications, queued continuity, recent interventions, and surfaced failures
- [x] separate activity-ledger surfaces for workflow runs, approvals, notifications, queued continuity, recent interventions, surfaced failures, and attributed LLM spend instead of leaving autonomous work opaque
- [x] cockpit-native extension studio for workflows, skills, and MCP configs with validation, diagnostics, save flows, and repair handoff
- [x] first workflow branch/resume control with checkpoint candidates, lineage metadata, approval-gated resume plans, and resume drafts tied to existing inputs
- [x] typed artifact-input handoff plus checkpoint-truthful branch actions in the cockpit, so follow-on workflow drafts now respect declared artifact compatibility and failed-step controls only appear when the runtime persisted reusable checkpoint state
- [x] workflow cockpit surfaces now derive parent/peer/child branch families from persisted lineage, letting operators inspect related runs and continue the latest branch directly from the workflow surface instead of reconstructing branches manually
- [x] cockpit workflow surfaces now promote step-focus debugging, direct step/output handoff actions, bundled family next-step planning drafts, denser ancestor/peer/failure-lineage follow-through, direct family-row checkpoint drill-in, direct family-row retry/repair controls, and active-triage workflow quick actions including failure-context reuse, direct best-continuation control, direct recovery actions, and keyboard-first best-continuation/comparison follow-through, so operators can act on the hottest failed or recoverable workflow step without reconstructing that context from raw history
- [x] cockpit artifact surfaces now preserve verified source-run lineage when it is uniquely visible, related family outputs, and direct artifact follow-on control, so typed artifact-to-workflow chaining is visible as an operator workflow graph instead of only a one-off draft button
- [x] Hermes runtime-parity primitives including `execute_code`, `delegate_task`, `clarify`, `todo`, `session_search`, stronger scheduled execution, and tighter runtime security controls
- [x] packaged browser providers, messaging connectors, automation triggers, node adapters, canvas outputs, workflow runtimes, and channel routing through the extension platform instead of ad hoc side paths
- [x] operator-surface imported reach, extension governance, and LLM spend attribution by runtime path and capability family
- [x] provider-neutral source capability inventory that exposes typed public-web tools, managed authenticated connectors, explicit raw-MCP gaps, and one reusable source-evidence review skill instead of forcing provider-specific workflow glue
- [x] first adapter-backed source-evidence runtime that turns typed public-web contracts into normalized evidence bundles, exposes degraded managed-connector truth instead of implied access, and gives Seraph one reusable `collect_source_evidence` path for public discovery, explicit pages, and existing browser-session snapshots
- [x] first connector-backed authenticated source-read bridge that binds a managed connector to a live MCP runtime when the connector is enabled and configured, so typed source contracts can execute normalized `repository.read`, `work_items.read`, and `code_activity.read` evidence reads instead of staying permanently degraded
- [x] reusable connector-first source review planning and bundled source-review routines, so daily review, progress review, and goal-alignment review can compose provider-neutral mixed-source plans with explicit degraded-step truth instead of spawning bespoke provider pipelines
- [x] backend CI now also weights historically slow backend suites, runs ten isolated backend shards in GitHub Actions, and pins the real shard-runner executable contract instead of letting long-tail runtime skew dominate workflow delivery

## Working On Now

- [x] this workstream shipped the first operator workflow-control slice through `workflow-control-and-artifact-roundtrips-v1`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`
- [x] this workstream now ships `capability-discovery-and-activation-v1`, `starter-skill-and-workflow-packs-v1`, `workflow-history-and-replay-v1`, and `extension-debugging-and-recovery-v1`
- [x] this workstream now also ships `capability-preflight-and-autorepair-v1`, `threaded-operator-timeline-v1`, and `workflow-runbooks-and-parameterized-replay-v1`
- [x] this workstream now hands the queue forward to the full extension-platform transition program rather than isolated extension UX slices
- [x] the first extension-platform foundation slices now cover terminology cleanup, canonical manifests, the transitional registry seam, and structured doctor diagnostics
- [x] the extension-platform foundation now also pins one canonical on-disk package layout and package-boundary resolution rules for contribution files
- [x] the authoring path now includes first local scaffold and validation commands for capability-pack package creation instead of forcing hand-authored manifests
- [x] the authoring path now also includes first-class public docs for extension overview, package creation, manifest fields, contribution types, validation, and migration instead of leaving the new architecture trapped in research docs
- [x] the authoring path now includes one canonical in-repo example package that is validated in tests and pinned to current scaffold output so contributors and docs share the same golden reference
- [x] the runtime now loads manifest-backed skill contributions through the extension registry while still preserving legacy loose-skill compatibility during the coexistence window
- [x] the runtime now also loads manifest-backed workflow contributions through the same registry seam, including packaged workflow diagnostics and manifest-preferred duplicate resolution during the coexistence window
- [x] the runtime now also exposes a first adapter-first source-capability surface, so Seraph can inspect available typed source contracts before composing evidence-collection routines instead of assuming GitHub- or browser-specific paths
- [x] the runtime now also loads manifest-backed starter packs and explicit runbooks through the same registry seam, and the capabilities surface now publishes packaged starter-pack metadata plus extension-backed runbook entries instead of treating both surfaces as legacy-only inventory
- [x] startup/runtime loading now serves bundled default skills, workflows, starter packs, and explicit runbooks from a real `seraph.core-capabilities` package under `backend/src/defaults/extensions/`, with workspace extension packs taking precedence over bundled defaults while install/bootstrap/catalog flows and legacy loose-file coexistence are still being migrated
- [x] the backend now ships one `/api/extensions` lifecycle surface for list, inspect, validate, install, enable, disable, configure, and remove so automation no longer has to talk directly to per-surface skill/workflow/package seams even though the workspace UI still needs to adopt that API and the current `configure` step is metadata-only until typed runtime config contracts land
- [x] extension studio is now manifest-aware for installed packages, with package manifests and package-backed workflow/skill sources loaded and saved through `/api/extensions/{id}/source` instead of always collapsing edits back into loose managed-file save paths
- [x] new authored skills/workflows now land in the managed `workspace/extensions/workspace-capabilities/` package, while bundled catalog skill installs now land as manifest-backed extension packages under `workspace/extensions/`; old loose loaders remain read-only compatibility during the final cleanup window
- [x] the trusted-code-plugins RFC is now closed for the current architecture with an explicit negative decision: Seraph continues with typed extension packs, MCP, managed connectors, and bundled native tools, not a general arbitrary-code plugin runtime
- [x] the five-wave Hermes/OpenClaw capability import program is now complete on `develop`, including packaged reach parity, selective OpenClaw imports, operator-surface governance, benchmark refresh, and deterministic eval proof
- [x] `workflow-autonomy-supervision-and-artifact-control-v1` now ships reusable checkpoint-backed workflow recovery, truthful unsupported-checkpoint fallback in the runs API, declared-type artifact handoff in the cockpit, and operator-visible branch-family supervision instead of generic retry-only copy
- [x] `adapter-first-capability-contracts-v1` now ships provider-neutral source contracts, managed-connector inventory, explicit raw-MCP disclosure, and one reusable source-evidence review skill instead of forcing provider-specific reporting glue
- [x] `adapter-backed-authenticated-source-operations-v1` now ships source-adapter inventory, normalized evidence bundles, executable public-web read contracts, explicit degraded managed-connector semantics, and one reusable `collect_source_evidence` tool/API path instead of leaving source routines to stitch raw surface metadata together
- [x] `adapter-backed-authenticated-source-operations-v3` now ships reusable `plan_source_review` planning, bundled daily/progress/goal-alignment source-review starter packs and runbooks, explicit mixed-source review fallback truth, and backend CI sharding for the growing test surface
- [x] `artifact-lineage-and-follow-on-control-v1` now ships verified artifact source-run provenance with explicit unresolved-state fallback, related family outputs, richer follow-on workflow rows, and artifact-specific planning shortcuts instead of leaving typed artifact chaining buried inside workflow-only inspector actions
- [x] `artifact-lineage-and-long-running-control-v2` now ships artifact source-run open/continue plus source-failure and related-output comparison shortcuts across evidence, outputs, and inspector panes, while the backend CI surface now uses weighted shard assignment and ten backend shards to keep the growing test matrix stable

## Review Follow-Up

- local regression fixed while landing `workflow-autonomy-and-artifact-control-v1`:
  - `GET /api/workflows/runs` could return `409` for degraded later-step runs whose parent run did not persist reusable checkpoint state, because the list endpoint eagerly selected `resume_from_step` even when that checkpoint candidate was already marked unsupported
  - fixed by suppressing implicit resume-plan selection for unsupported checkpoints while keeping explicit `resume-plan` requests fail-closed with `409`
- review pass:
  - targeted review of checkpoint resume truthfulness, runs-API fallback behavior, cockpit artifact compatibility, and stale test assumptions found no remaining material issues after the list-endpoint fix and regression coverage updates
- local regression fixed while landing `adapter-first-capability-contracts-v1`:
  - the first browser-session inventory note described every discovered provider as configured, which could overstate staged or disabled providers
  - fixed by describing them as known providers instead of configured providers
- review pass:
  - direct diff review of source contract truthfulness, managed-connector readiness, raw-MCP disclosure, and skill guidance found no remaining material issues after the provider-note fix
  - Copernicus review was started for a second pass on bugs, regressions, and hallucinated assumptions, but it did not return findings before validation completed, so the PR validation should claim only the concrete local review result
- local review finding fixed while landing `adapter-backed-authenticated-source-operations-v1`:
  - typed managed connectors could advertise work-item or repository contracts without any executable runtime path, which risked implying authenticated access that did not exist yet
  - fixed by adding explicit adapter-state and degraded-reason surfaces plus next-best fallback guidance instead of treating inventory metadata as execution truth
- local review finding fixed while landing `adapter-backed-authenticated-source-operations-v2`:
  - the first authenticated adapter grading treated an unimplemented write route as if it invalidated the whole authenticated read adapter, which would have kept GitHub degraded even when every read contract was executable
  - fixed by grading managed-connector readiness against executable read coverage while still exposing unbound write routes as non-executable operations
- local review finding fixed while landing `adapter-backed-authenticated-source-operations-v3`:
  - the first source-review planner over-warned whenever a preferred typed source could not satisfy every contract in a mixed-source routine, which would have made valid connector-first review plans look broken
  - fixed by treating the preferred source as a contract-scoped preference and falling back silently to the next ready typed adapter for the contracts it does not advertise
- review pass:
  - targeted review of source-review planner truthfulness, bundled source-review routine loading, eval coverage, and backend CI timeout handling found no remaining material issues after the preferred-source warning fix and shard-script regression coverage

## Still To Do On `develop`

- [ ] bundled capability-pack auto-install and stronger policy/dependency repair beyond the current install/recommendation, preflight/autorepair, policy-aware recovery, catalog-install, and bounded bootstrap flow
- [ ] richer extension health/test surfaces and production-grade connector hardening beyond the current lifecycle API, governance queue, and deterministic eval coverage
- [ ] deeper workflow operating surfaces and richer workflow history beyond the current cockpit timeline, typed artifact handoff, artifact source/family follow-through, step records, branch-family supervision, branch comparison, family-output reuse, output-comparison drafts, family-row checkpoint drill-in, family-row retry/repair controls, checkpoint branch actions, replay guardrails, workflow runtimes, canvas outputs, and operator terminal
- [ ] clearer extension ergonomics for third-party and user-authored capabilities beyond the current operator surface, repair actions, live logs, runbooks, preflight surfaces, diagnostics, extension studio, and skill-registry loop
- [ ] better leverage of delegation without making the product harder to trust or reason about

## Extension Platform Execution Rules

- every numbered item below is an internal PR-sized slice, even if multiple slices are later batched into one GitHub PR
- each slice must end with a subagent review pass for bugs, missing tests, design drift, and hallucinated assumptions before it is marked complete
- public docs, scaffolding scripts, validation tooling, and a canonical example pack are part of the architecture transition itself, not follow-up polish
- built-in declarative capabilities must migrate onto the same packaged extension model as user-authored capabilities before this program is considered complete
- trusted arbitrary-code plugins are not part of the implementation path unless the final RFC explicitly approves them
- [the roadmap](./00-master-roadmap.md#completed-extension-platform-transition-program) preserves the completed transition program and strategic ownership; this workstream doc summarizes the same program by phase so the implementation record does not drift across docs
- active extension work should be tracked through GitHub issues, PRs, and the GitHub Project instead of a doc-owned task tracker
- the result of each mandatory subagent review pass must be rolled into the eventual GitHub PR `Validation` section before any slice is marked complete in the implementation docs

## Transition Phases

### Phase 1: Foundation

- terminology cleanup for `plugins` versus `native_tools`, connectors, and capability packs
- canonical manifest schema and typed `contributes` contract
- unified extension registry and loader
- validation and doctor outputs
- standardized package layout

### Phase 2: Authoring Path

- scaffold and validation tools for package authors
- first-class public docs for adding new capability packs
- one canonical schema-valid example package for docs, tests, and future contributors

### Phase 3: Declarative Capability Migration

- manifest-backed packaging for skills
- manifest-backed packaging for workflows
- manifest-backed packaging for runbooks and starter packs
- migration of bundled declarative defaults onto the same packaged extension model

### Phase 4: Lifecycle And Studio

- [x] unified extension lifecycle API
- [x] manifest-aware extension studio with package manifest plus package-backed workflow/skill source editing
- [x] unified workspace lifecycle UI for install, validate, health, enablement, configuration, and removal, including approval-aware lifecycle recovery through Pending approvals and the inspector

### Phase 5: Connector Unification

- [x] connector manifests and health hooks, including one normalized connector-health contract plus extension-native connector list/test endpoints
- [x] MCP packaging and install flow inside the extension platform, including package-owned MCP runtime metadata, extension-native connector enable/disable control, cockpit routing for packaged MCP test/toggle actions, raw `/api/mcp` mutation paths now blocked for extension-owned servers, and package-owned MCP definitions now read-only in Extension Studio until package-backed MCP source editing lands
- [x] managed connectors for curated high-trust integrations, including bundled managed connector packs plus config-aware enable/disable routed through the shared extension lifecycle instead of staying passive metadata

### Phase 6: Reach Surface Migration

- [x] observer-source extensions, including lifecycle-backed enable/disable for `observer_definitions`, runtime selector overrides wired into observer refresh, and connector health that now distinguishes active, disabled, invalid, and overridden packaged observer sources
- [x] channel-adapter extensions, including lifecycle-backed enable/disable for `channel_adapters`, delivery transport selector overrides wired into proactive delivery, connector health that now distinguishes active, degraded, disabled, invalid, and overridden packaged channel transports, and an explicit boundary that the concrete websocket/native delivery implementations remain core-owned even though packaged adapters now drive selector state

### Phase 7: Hardening And Completion

- [x] extension permissions and approvals
- [x] extension audit and activity visibility
- [x] extension versioning and update flow
- [x] legacy loader cleanup for primary authoring/install paths
- [x] trusted-code-plugins RFC concluded that privileged third-party code plugins stay out of scope for the current platform unless a future RFC reopens the decision under a much higher safety bar

## Required Authoring Docs And Tools

- [x] public extension overview that explains typed contributions, trust tiers, and what stays core
- [x] step-by-step guide for creating a new capability pack
- [x] manifest reference with every supported field documented
- [x] contribution-type reference for skills, workflows, runbooks, starter packs, presets, connectors, and later observer/channel adapters
- [x] validation and doctor guide for package errors and repair flows
- [x] migration guide from loose skills/workflows/MCP configs to packaged extensions
- [x] local scaffold tool for generating a new extension package
- [x] local validation tool for checking a package before install
- [x] canonical example package in-repo that docs, tests, and contributors can all rely on; it should be schema-valid immediately and become runtime-backed once the packaging slices land

## Non-Goals

- extension sprawl without product coherence
- delegation depth for its own sake

## Acceptance Checklist

- [x] Seraph can load reusable skills and external MCP tool surfaces
- [x] Seraph can expose a specialist/delegation shape beyond a single monolithic agent
- [x] Seraph can expose reusable workflows across tools, skills, delegation, and connected MCP capabilities
- [x] Seraph can round-trip workflow artifacts back into both the command surface and compatible follow-on workflow drafts
- [x] Seraph now exposes a first cockpit-native operator surface for extension and workflow state
- [x] Seraph now exposes first starter packs and workflow replay history instead of leaving capability activation entirely implicit
- [x] Seraph now has a first "available now / blocked now / enable, install, or repair next" surface instead of only starter-pack visibility
- [x] Seraph now has a first live operator console for capability state, repair actions, saved runbooks, and workflow timeline recovery
- [x] Seraph now has a first preflight/autorepair layer for workflows, starter packs, and runbooks instead of forcing blind execution attempts
- [x] Seraph now has a first bounded bootstrap layer that can safely apply capability repair/install steps rather than only describing what must change
- [ ] Seraph compounds capability through extensions and workflows in a way that is simple to operate
