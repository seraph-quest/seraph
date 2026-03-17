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

## Working On Now

- [ ] this workstream is not the repo-wide active focus while Guardian Intelligence leads the current 10-PR horizon
- [x] this workstream owns `workflow-composition-v1` in the master 10-PR queue

## Still To Do On `develop`

- [ ] stronger workflow composition on top of current skills, tools, and delegation
- [ ] clearer extension ergonomics for third-party and user-authored capabilities
- [ ] better leverage of delegation without making the product harder to trust or reason about

## Non-Goals

- extension sprawl without product coherence
- delegation depth for its own sake

## Acceptance Checklist

- [x] Seraph can load reusable skills and external MCP tool surfaces
- [x] Seraph can expose a specialist/delegation shape beyond a single monolithic agent
- [ ] Seraph compounds capability through extensions and workflows in a way that is simple to operate
