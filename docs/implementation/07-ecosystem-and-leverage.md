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

## Working On Now

- [x] this workstream shipped the first operator workflow-control slice through `workflow-control-and-artifact-roundtrips-v1`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`
- [x] this workstream now ships `capability-discovery-and-activation-v1`, `starter-skill-and-workflow-packs-v1`, `workflow-history-and-replay-v1`, and `extension-debugging-and-recovery-v1`
- [x] this workstream now hands the queue forward to deeper safety, workflow history, and extension-recovery quality rather than first-pass visibility

## Still To Do On `develop`

- [ ] clearer capability discoverability and activation around tools, skills, workflows, and MCP surfaces so a fresh workspace does not feel empty despite the shipped extension/runtime foundations
- [ ] stronger default starter packs for user-invocable skills and workflows so Seraph boots with obvious useful capability instead of only the first bundled starter assets
- [ ] deeper workflow operating surfaces and richer workflow history beyond the first settings/cockpit loop, workflow-runs API, boundary-aware replay drafting, and cockpit operator surface
- [ ] clearer extension ergonomics for third-party and user-authored capabilities beyond the first cockpit-native operator surface and first recovery/debugging inspector
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
- [ ] Seraph still needs a richer "available now / blocked now / enable, install, or repair next" surface beyond the first capability overview and starter-pack operator view
- [ ] Seraph compounds capability through extensions and workflows in a way that is simple to operate
