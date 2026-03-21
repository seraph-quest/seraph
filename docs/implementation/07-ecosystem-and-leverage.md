# Workstream 07: Ecosystem And Delegation

## Status On `develop`

- [ ] Workstream 07 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [08. Ecosystem And Delegation](/research/ecosystem-and-delegation)
- detailed strategy: [12. Plugin System And MCP Strategy](/research/plugin-system-and-mcp-strategy)

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

## Full Extension Platform Transition Program

1. [ ] `extension-model-terminology-v1`:
   rename the misleading internal `plugins/` concept into clearer terms such as `native_tools`, `connector`, and `capability_pack` so the codebase and docs stop implying that Seraph already has a general arbitrary-code plugin runtime
2. [ ] `extension-manifest-schema-v1`:
   add the canonical extension manifest, schema validator, compatibility rules, and typed `contributes` contract so every later slice builds on one explicit package format
3. [ ] `extension-registry-and-loader-v1`:
   introduce one extension registry and loader abstraction that can enumerate manifests and typed contributions while preserving current skill, workflow, and MCP behavior during migration
4. [ ] `extension-validation-and-doctor-v1`:
   add structured extension validation and doctor outputs for schema errors, missing references, compatibility failures, and permission mismatches so broken packs become diagnosable before install or execution
5. [ ] `extension-package-layout-v1`:
   standardize the on-disk package structure for capability packs and connectors so one package can contribute skills, workflows, runbooks, starter packs, presets, and later connector definitions coherently
6. [ ] `extension-scaffold-tools-v1`:
   ship local scaffolding and validation tools so adding a new capability pack does not require hand-authoring manifests and directory structure from scratch
7. [ ] `extension-authoring-docs-v1`:
   publish first-class docs for creating capability packs, manifest fields, contribution types, validation, repair, and migration from the current loose-file model
8. [ ] `example-capability-pack-v1`:
   add one complete example package with at least a skill, workflow, and runbook so docs, tests, and future contributors all share one canonical reference
9. [ ] `capability-packaging-skills-v1`:
   migrate skill loading into manifest-backed capability packs with backward compatibility during the transition so skills become first-class extension contributions
10. [ ] `capability-packaging-workflows-v1`:
   migrate workflow loading into manifest-backed capability packs with validated references and metadata so workflows stop living on a separate loading path
11. [ ] `capability-packaging-runbooks-and-starter-packs-v1`:
   move runbooks and starter packs into the same manifest-backed architecture so higher-level reusable capability bundles stop being special-case inventory
12. [ ] `bundled-capability-packs-v1`:
   convert Seraph’s shipped declarative defaults into real bundled capability packs so the product uses its own extension system instead of a parallel built-in path
13. [ ] `extension-lifecycle-api-v1`:
   add one lifecycle API for install, validate, enable, disable, configure, inspect, and remove so UI and automation flows stop talking to per-surface install logic
14. [ ] `extension-studio-manifest-awareness-v1`:
   make the extension studio package-aware so authors edit manifests and package members together rather than loose skill, workflow, and MCP files in isolation
15. [ ] `extension-lifecycle-ui-v1`:
   surface the unified extension lifecycle in the workspace so install, validation, health, enablement, configuration, and removal all happen through one operator path
16. [ ] `connector-manifest-and-health-v1`:
   define the connector package shape with auth/config metadata and health/test hooks so connectors stop being an architectural exception
17. [ ] `mcp-packaging-and-install-flow-v1`:
   move MCP server definitions into the extension package model and lifecycle so MCP becomes one connector type inside the platform instead of a separate world
18. [ ] `managed-connectors-v1`:
   add the curated non-MCP connector abstraction for first-party or high-trust integrations that need stronger UX, rollout, telemetry, and auth control than raw MCP
19. [ ] `observer-source-extensions-v1`:
   package observer sources as typed extensions where appropriate so input reach fits the same architecture as skills, workflows, and connectors
20. [ ] `channel-adapter-extensions-v1`:
   package output and delivery adapters as typed extensions where appropriate so reach surfaces stop being separate one-off integration paths
21. [ ] `extension-permissions-and-approvals-v1`:
   map extension-declared permissions cleanly into policy, approval, and execution behavior so packages cannot bypass Seraph’s trust boundaries
22. [ ] `extension-audit-and-activity-v1`:
   make extension install, update, enable, disable, health, and execution visible in Activity Ledger and audit so operators can explain what changed and why
23. [ ] `extension-versioning-and-update-flow-v1`:
   add version-aware updates, compatibility checks, and bundled-vs-user-installed semantics so packages can evolve without hidden drift
24. [ ] `legacy-loader-cleanup-v1`:
   retire or demote the old parallel loaders and loose-file paths so the new extension platform becomes the only supported primary path
25. [ ] `trusted-code-plugins-rfc-v1`:
   explicitly decide whether privileged code plugins are needed at all, with a gated RFC rather than silently drifting into an arbitrary-code plugin runtime

## Required Authoring Docs And Tools

- [ ] public extension overview that explains typed contributions, trust tiers, and what stays core
- [ ] step-by-step guide for creating a new capability pack
- [ ] manifest reference with every supported field documented
- [ ] contribution-type reference for skills, workflows, runbooks, starter packs, presets, connectors, and later observer/channel adapters
- [ ] validation and doctor guide for package errors and repair flows
- [ ] migration guide from loose skills/workflows/MCP configs to packaged extensions
- [ ] local scaffold tool for generating a new extension package
- [ ] local validation tool for checking a package before install
- [ ] canonical example package in-repo that docs, tests, and contributors can all rely on

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
