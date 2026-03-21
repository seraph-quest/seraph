# Workstream 07: Ecosystem And Delegation

## Status On `develop`

- [ ] Workstream 07 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [08. Ecosystem And Delegation](/research/ecosystem-and-delegation)
- detailed strategy: [12. Extension Platform And MCP Strategy](/research/plugin-system-and-mcp-strategy)

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
- [x] bounded capability bootstrap that can apply safe install/repair actions for workflows, runbooks, and starter packs from the cockpit
- [x] workflow diagnostics that expose stored load errors plus richer step timing, error summaries, and recovery hints for extension debugging
- [x] threaded operator timeline surfaces for workflow runs, approvals, notifications, queued continuity, recent interventions, and surfaced failures
- [x] separate activity-ledger surfaces for workflow runs, approvals, notifications, queued continuity, recent interventions, surfaced failures, and attributed LLM spend instead of leaving autonomous work opaque
- [x] cockpit-native extension studio for workflows, skills, and MCP configs with validation, diagnostics, save flows, and repair handoff
- [x] first workflow branch/resume control with checkpoint candidates, lineage metadata, approval-gated resume plans, and resume drafts tied to existing inputs

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

## Still To Do On `develop`

- [ ] bundled capability-pack auto-install and stronger policy/dependency repair beyond the first install/recommendation, preflight/autorepair, policy-aware recovery actions, installable catalog surfaces, bounded bootstrap flow, and first extension-studio save path
- [ ] deeper workflow operating surfaces and richer workflow history beyond the current cockpit timeline, step records, branch/resume checkpoints, replay guardrails, parameterized reruns, approval-aware recovery, diagnostics endpoint, and operator terminal
- [ ] clearer extension ergonomics for third-party and user-authored capabilities beyond the cockpit-native operator surface, repair actions, live logs, runbooks, preflight surfaces, diagnostics, and first extension studio
- [ ] better leverage of delegation without making the product harder to trust or reason about

## Extension Platform Execution Rules

- every numbered item below is an internal PR-sized slice, even if multiple slices are later batched into one GitHub PR
- each slice must end with a subagent review pass for bugs, missing tests, design drift, and hallucinated assumptions before it is marked complete
- public docs, scaffolding scripts, validation tooling, and a canonical example pack are part of the architecture transition itself, not follow-up polish
- built-in declarative capabilities must migrate onto the same packaged extension model as user-authored capabilities before this program is considered complete
- trusted arbitrary-code plugins are not part of the implementation path unless the final RFC explicitly approves them
- the canonical ordered slice queue lives in [the roadmap](./00-master-roadmap.md#current-extension-platform-transition-queue); this workstream doc summarizes the same program by phase so the queue definition does not drift across docs
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

- unified extension lifecycle API
- manifest-aware extension studio
- unified workspace lifecycle UI for install, validate, health, enablement, configuration, and removal

### Phase 5: Connector Unification

- connector manifests and health hooks
- MCP packaging and install flow inside the extension platform
- managed connectors for curated high-trust integrations

### Phase 6: Reach Surface Migration

- observer-source extensions
- channel-adapter extensions

### Phase 7: Hardening And Completion

- extension permissions and approvals
- extension audit and activity visibility
- extension versioning and update flow
- legacy loader cleanup
- trusted-code-plugins RFC only after the typed extension platform is complete enough to judge whether privileged code plugins are actually needed

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
