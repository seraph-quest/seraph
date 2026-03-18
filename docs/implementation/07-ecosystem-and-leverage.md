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

## Working On Now

- [ ] this workstream is not the first queue item, but it is back in the near-term horizon after the first workflow runtime shipped
- [x] this workstream now shares `workflow-control-and-artifact-roundtrips-v1` after `workflow-composition-v1` shipped on `develop`

## Still To Do On `develop`

- [ ] clearer operator-facing workflow control and artifact round-tripping on top of the reusable workflow runtime
- [ ] clearer extension ergonomics for third-party and user-authored capabilities
- [ ] better leverage of delegation without making the product harder to trust or reason about

## Non-Goals

- extension sprawl without product coherence
- delegation depth for its own sake

## Acceptance Checklist

- [x] Seraph can load reusable skills and external MCP tool surfaces
- [x] Seraph can expose a specialist/delegation shape beyond a single monolithic agent
- [x] Seraph can expose reusable workflows across tools, skills, delegation, and connected MCP capabilities
- [ ] Seraph compounds capability through extensions and workflows in a way that is simple to operate
