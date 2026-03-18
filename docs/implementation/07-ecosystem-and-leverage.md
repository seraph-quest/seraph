# Workstream 07: Ecosystem And Delegation

## Status On `develop`

- [ ] Workstream 07 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [08. Ecosystem And Delegation](/research/ecosystem-and-delegation)

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

## Working On Now

- [x] this workstream shipped the first operator workflow-control slice through `workflow-control-and-artifact-roundtrips-v1`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`
- [x] this workstream now ships `capability-discovery-and-activation-v1`, `starter-skill-and-workflow-packs-v1`, `workflow-history-and-replay-v1`, and `extension-debugging-and-recovery-v1`
- [x] this workstream now also ships `capability-command-palette-v1`, `capability-pack-install-and-recommendations-v1`, `capability-repair-and-install-flows-v1`, `extension-debugging-and-recovery-v2`, and `operator-terminal-and-runbooks-v1`
- [x] this workstream now hands the queue forward to deeper auto-install, debugging quality, and thread-aware workflow control rather than first-pass visibility

## Still To Do On `develop`

- [ ] bundled capability-pack auto-install and stronger policy/dependency repair beyond the first install/recommendation and manual recovery actions
- [ ] deeper workflow operating surfaces and richer workflow history beyond the first cockpit timeline, replay guardrails, and operator terminal
- [ ] clearer extension ergonomics for third-party and user-authored capabilities beyond the new cockpit-native operator surface, repair actions, and runbooks
- [ ] better leverage of delegation without making the product harder to trust or reason about

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
- [ ] Seraph compounds capability through extensions and workflows in a way that is simple to operate
