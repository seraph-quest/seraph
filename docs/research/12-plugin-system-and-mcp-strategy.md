# 12. Plugin System And MCP Strategy

## Goal

Define a Seraph extension model that compounds capability without turning the product into an unsafe or incoherent framework.

This document answers:

- whether Seraph should build a real plugin system
- what should become an extension versus stay core
- how MCP should fit into the architecture
- whether Seraph should build a non-MCP alternative
- how to execute the migration from the current codebase

## Executive Summary

Seraph should build a real extension platform, but not as one unrestricted in-process plugin runtime.

The right model is:

- typed extensions for most user/operator-extensible capability
- MCP as one connector type, not the whole ecosystem
- a narrow first-party managed connector path for high-value integrations
- core trust/safety/runtime boundaries kept inside Seraph
- trusted code plugins deferred or heavily gated

The strongest product decision is:

1. keep `skills`, `workflows`, `runbooks`, `starter packs`, `catalog packages`, `MCP server definitions`, `observer sources`, `channels`, and `scheduled routines` as typed extension surfaces
2. keep `policy`, `approval`, `audit`, `secret handling`, `session/threading`, `workflow execution`, `capability preflight/repair`, and routing as core-owned systems
3. do not replace MCP with a second open-ended general plugin runtime
4. do add a curated non-MCP connector path for cases where Seraph needs tighter auth, UX, rollout, telemetry, and enterprise control than raw MCP provides

## Current Seraph State

Seraph already has multiple extension seams, but they are not unified under one package/runtime model.

### Existing typed extension surfaces

- `SKILL.md` skills loaded from disk:
  - `backend/src/skills/loader.py`
  - `backend/src/skills/manager.py`
- markdown-defined reusable workflows:
  - `backend/src/workflows/loader.py`
  - `backend/src/workflows/manager.py`
- runtime-managed MCP server definitions:
  - `backend/src/tools/mcp_manager.py`
  - `backend/src/api/mcp.py`
- catalog/install flows for bundled skills and MCP presets:
  - `backend/src/api/catalog.py`
  - `backend/src/api/capabilities.py`

### Existing internal code plugin surface

Seraph also has repo-local Python tool discovery:

- `backend/src/plugins/loader.py`
- `backend/src/plugins/registry.py`

Important nuance:

- this is not a general plugin system
- it only auto-discovers trusted, bundled Python tools in `backend/src/tools`
- it does not install or sandbox third-party code

That means Seraph already behaves like a multi-surface extension product, but not yet like a coherent extension platform.

## Reference Findings

## Hermes

Hermes is closest to the right shape for Seraph.

What Hermes appears to do well:

- uses typed capability systems instead of one universal plugin SDK
- treats installable skills as the main packageable surface
- keeps tools/toolsets as code-shipped runtime capability
- uses MCP as a first-class external integration path
- has explicit security controls around tools, MCP, and skill installation

Useful lesson:

- Hermes compounds through `skills + tools + MCP + presets`, not through arbitrary in-process plugins

Sources:

- https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/
- https://hermes-agent.nousresearch.com/docs/user-guide/security/

## OpenClaw

OpenClaw is broader and more flexible, but also more trust-heavy.

What OpenClaw appears to do:

- supports real code plugins
- allows plugins to register tools, routes, commands, services, context engines, and channels
- separates skills, hooks, and channels as distinct extension surfaces
- installs plugins through packages, not just local config

Useful lesson:

- OpenClaw proves that a broad plugin runtime is powerful
- it also proves that such plugins are effectively trusted code and expand the operator/security burden significantly

Sources:

- https://docs.openclaw.ai/plugins
- https://docs.openclaw.ai/plugins/agent-tools
- https://docs.openclaw.ai/automation/hooks
- https://docs.openclaw.ai/skills
- https://docs.openclaw.ai/security

## Obsidian-style plugins

The cleanest lessons here are packaging, lifecycle, and settings UX.

What Obsidian-style plugin ecosystems do well:

- manifest-driven packaging
- clean install/update/remove lifecycle
- explicit settings UI per extension
- strong host-owned lifecycle: load, enable, disable, unload
- clear distinction between host runtime and plugin contribution

Useful lesson:

- Seraph should copy the packaging and lifecycle discipline
- Seraph should not copy the "any plugin can do anything" trust model by default

Sources:

- https://docs.obsidian.md/Home
- https://github.com/obsidianmd/obsidian-sample-plugin

## MCP and Anthropic's packaging response

MCP is useful, but the ecosystem itself has already moved toward packaging and install ergonomics on top of raw MCP.

Anthropic's own response is important:

- MCP remained powerful
- raw installation was too complex
- Anthropic introduced Desktop Extensions / MCP Bundles to package local MCP servers with manifests, dependencies, config, permissions, and updates

Useful lesson:

- the problem is not that MCP is worthless
- the problem is that raw MCP alone is not a complete product surface

Source:

- https://www.anthropic.com/engineering/desktop-extensions

## MCP Investigation

## What MCP is good at

MCP is strongest as a standard connector protocol for external tool surfaces.

It gives Seraph:

- ecosystem reach
- transport-level standardization
- reusable client/server implementations
- a common abstraction for tools, prompts, and resources
- easier leverage of third-party integrations without Seraph inventing a new external protocol

For Seraph, that makes MCP valuable for:

- SaaS integrations
- local companion services
- developer tools
- cross-app connectors
- external tool surfaces not worth implementing natively

## What the current pushback actually is

I did not find strong evidence that serious teams are broadly "abandoning MCP."

What I did find is:

- strong security warnings in the official MCP documentation
- real product friction around install/auth/update/debug
- credible operator complaints about context overhead and tool-schema noise
- ecosystem movement toward packaged, curated MCP experiences rather than raw manual MCP setup

So the correct conclusion is:

- people are not mainly rejecting the protocol category
- they are pushing back on raw MCP-first product design

## Why people push back on MCP

### 1. Security and trust complexity

The official MCP security docs are explicit about several non-trivial risk classes:

- confused deputy attacks
- token passthrough anti-patterns
- SSRF during OAuth metadata discovery
- session hijacking and injected events
- local MCP server compromise
- scope minimization failures

Sources:

- https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
- https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization

Key implications for Seraph:

- MCP is not "safe by default"
- the client must enforce serious trust boundaries
- local stdio servers are particularly sensitive because the spec says stdio should use environment-provided credentials rather than HTTP auth

Official spec detail:

- authorization is optional overall
- HTTP transports should implement the authorization spec
- stdio transports should not follow that HTTP auth flow and instead retrieve credentials from the environment

Source:

- https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization

### 2. Installation and update friction

Anthropic explicitly says the same thing:

- users needed runtimes installed
- users had to edit config files manually
- dependency conflicts were common
- discovery was poor
- updates were manual and fragile

That is why Anthropic packaged MCP into `.mcpb` bundles.

Source:

- https://www.anthropic.com/engineering/desktop-extensions

### 3. Context and schema overhead

There is credible secondary criticism that large MCP tool inventories and heavy JSON schemas create a context tax:

- more tool schema tokens
- harder tool selection for smaller models
- higher latency and cost
- more distractor context

This is not a formal protocol flaw in the spec, but it is a real product concern for agent UX and spend.

Secondary source:

- https://www.mmntm.net/articles/mcp-context-tax

This matches Seraph's needs:

- we want dense, legible operator control
- we do not want raw, uncontrolled tool sprawl pushing budget and context up

### 4. Quality inconsistency across servers

MCP solves transport and shape better than it solves quality.

In practice, MCP servers vary widely on:

- schema quality
- naming quality
- auth handling
- update cadence
- operational reliability
- policy fit

That means "MCP support" is not enough. Seraph still needs:

- validation
- health checks
- policy mapping
- install/repair UX
- curated defaults

### 5. Compositional risk

Even if one MCP server seems safe in isolation, tool combinations can create unexpected risk.

Recent reporting on flaws in Anthropic's Git MCP server highlights exactly this kind of compositional danger: safe-looking servers can become dangerous when chained together with other capabilities like filesystem access.

Secondary source:

- https://www.techradar.com/pro/security/anthropics-official-git-mcp-server-had-some-worrying-security-flaws-this-is-what-happened-next

This fits a broader Seraph rule:

- capability safety must be evaluated compositionally, not just per extension

## Should Seraph replace MCP with plugins?

No.

That would create the wrong architecture.

The better framing is:

- MCP is one extension transport
- Seraph needs a broader extension platform above it

So the right answer is:

- do not replace MCP with a custom general plugin protocol
- do not make MCP the only external-capability model either

## What Seraph should build instead

Seraph should build a typed extension platform with three trust tiers.

## Trust Tier 1: Safe declarative extensions

These should be the default user/operator-extensible surfaces.

Types:

- skills
- workflows
- runbooks
- starter packs
- catalog/installable capability packs
- provider presets
- prompt/personality packs
- scheduled routines/jobs
- observer source definitions where possible

Properties:

- markdown, yaml, or json only
- validated before save/install
- installable, enable/disable, update/remove
- explicit compatibility and permission declarations
- no arbitrary code execution

## Trust Tier 2: Connector extensions

These bridge Seraph to external systems.

Types:

- MCP server definitions
- managed first-party connectors
- observer source connectors
- delivery/channel connectors
- workspace/surface adapters such as Obsidian-style integrations

Properties:

- explicit auth/config schema
- explicit permissions
- health checks
- bounded telemetry and audit
- ideally out-of-process or transport-bounded

This is where MCP belongs.

## Trust Tier 3: Trusted code plugins

These should exist only later, and only under strict rules.

Types:

- new runtime-native tools
- deep host extensions
- custom in-process integrations that cannot fit typed or connector models

Properties:

- signed, bundled, or explicitly trusted only
- no default marketplace exposure in v1
- stronger review and policy gates

This should not be Seraph's main extension story.

## What should become extensions

The earlier plugin discussion should be expanded into the actual Seraph capability types:

- skills
- workflows
- runbooks
- starter packs
- installable catalog packages
- MCP server definitions
- managed connector definitions
- observer source connectors
- delivery/channel connectors
- provider presets
- prompt/personality bundles
- scheduled routines/jobs

Important additions versus the earlier rough list:

- observer sources are a first-class capability type
- scheduled routines/jobs are a first-class capability type
- installable capability packs are a first-class capability type

## What must stay core

These systems define Seraph's trust boundary and should stay host-owned:

- policy engine
- approval engine
- audit and activity ledger model
- secret-ref handling
- session and thread model
- world model and guardian state core synthesis
- workflow execution engine
- capability preflight and repair engine
- routing/provider control
- extension lifecycle enforcement

Extensions should contribute capabilities inside those systems, not replace them.

## Recommended Seraph Package Model

Seraph should introduce one extension manifest and one package/install lifecycle.

Example:

```yaml
id: seraph.research-briefing
version: 2026.3.21
display_name: Research Briefing
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: bundled
contributes:
  skills:
    - skills/web-briefing.md
  workflows:
    - workflows/web-brief-to-file.md
  runbooks:
    - runbooks/research-briefing.yaml
  mcp_servers:
    - mcp/http-request.json
permissions:
  tools:
    - web_search
    - write_file
  network: true
```

This lets one package contribute multiple typed surfaces without inventing a universal runtime plugin contract.

## Recommended Lifecycle

Every extension should support:

1. install
2. validate
3. review permissions
4. enable/disable
5. configure
6. test/health check
7. update
8. remove

The UI should present one unified extension/operator surface even when the underlying extension type differs.

## Managed connectors as the alternative to raw MCP

Seraph should add a second connector path, but only for curated, high-value systems.

This should be a managed connector model, not a second open ecosystem.

Use managed connectors when Seraph wants:

- first-party auth UX
- better enterprise rollout/control
- stronger telemetry and audit
- smoother install/update/repair
- stricter permission mapping
- tighter support guarantees

Use MCP when Seraph wants:

- broad compatibility
- faster integration with existing ecosystems
- external tool reuse
- lower implementation cost for long-tail integrations

Rule of thumb:

- if the value is ecosystem breadth, use MCP
- if the value is trust, enterprise operability, and polished first-party UX, use a managed connector

## What Seraph should not do

- do not build a second open-ended connector protocol that competes with MCP
- do not make arbitrary third-party Python code the main extension story
- do not let extensions own approval, policy, or audit logic
- do not let package installation bypass capability preflight or secret handling
- do not let MCP servers appear as trusted just because they follow the protocol

## Recommended Execution Plan

## Phase 1: Clarify today's architecture

Goal:

- reduce naming confusion and establish the new model without changing behavior first

Work:

- rename the internal `plugins/` concept in code/docs to something like `native_tools` or `bundled_tools`
- document current extension types explicitly in API and docs
- define the extension manifest schema

PR slices:

1. `extension-model-terminology-v1`
2. `extension-manifest-schema-v1`

## Phase 2: Package the declarative surfaces

Goal:

- move from loose files to versioned extension packages

Work:

- package skills, workflows, runbooks, and starter packs under one manifest
- make bundled defaults ship as real extension packages
- support local install/update/remove for capability packs

PR slices:

3. `capability-packaging-v1`
4. `bundled-capability-packs-v1`
5. `extension-lifecycle-ui-v1`

## Phase 3: Normalize connector surfaces

Goal:

- treat MCP and non-MCP connectors as siblings under one extension UI

Work:

- define connector manifests
- package MCP server definitions and setup flows
- add managed connector abstraction for first-party integrations
- keep the same cockpit lifecycle for both

PR slices:

6. `connector-manifest-and-health-v1`
7. `mcp-package-and-install-flow-v1`
8. `managed-connectors-v1`

## Phase 4: Extend input/output reach cleanly

Goal:

- move observer and channel reach into the same typed platform

Work:

- observer source connectors
- delivery/channel adapters
- workspace/surface adapters

PR slices:

9. `observer-source-extensions-v1`
10. `channel-adapter-extensions-v1`

## Phase 5: Reassess trusted code plugins

Goal:

- decide later whether trusted code plugins are actually necessary

Work:

- only after phases 1-4 prove insufficient
- if needed, introduce signed or explicitly trusted code plugins with strong policy gates

PR slice:

11. `trusted-code-plugins-rfc-v1`

## Product recommendation

The recommended product contract is:

- Seraph is an extension platform built around typed capability contributions
- MCP is one external connector path inside that platform
- managed connectors are the alternative to raw MCP for curated first-party experiences
- the core runtime remains the owner of safety, policy, audit, approval, and threading

This gives Seraph:

- better safety than a broad plugin runtime
- better product coherence than raw file-based extensibility
- better install/update UX than raw MCP
- better enterprise operability than unmanaged connectors
- better ecosystem reach than a purely first-party integration strategy

## Decision

Seraph should change from:

- separate skills/workflows/MCP/catalog surfaces

to:

- one extension platform with typed contributions

But it should not change into:

- one broad arbitrary-code plugin runtime

That is the crucial architectural boundary.

## Sources

- Seraph code and docs:
  - `backend/src/skills/loader.py`
  - `backend/src/skills/manager.py`
  - `backend/src/workflows/loader.py`
  - `backend/src/workflows/manager.py`
  - `backend/src/tools/mcp_manager.py`
  - `backend/src/api/catalog.py`
  - `backend/src/api/capabilities.py`
- Hermes:
  - https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/
  - https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/
  - https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
  - https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/
  - https://hermes-agent.nousresearch.com/docs/user-guide/security/
- OpenClaw:
  - https://docs.openclaw.ai/plugins
  - https://docs.openclaw.ai/plugins/agent-tools
  - https://docs.openclaw.ai/automation/hooks
  - https://docs.openclaw.ai/skills
  - https://docs.openclaw.ai/security
- Obsidian:
  - https://docs.obsidian.md/Home
  - https://github.com/obsidianmd/obsidian-sample-plugin
- MCP:
  - https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization
  - https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
  - https://www.anthropic.com/engineering/desktop-extensions
- Secondary sources used as supporting evidence, not protocol truth:
  - https://www.mmntm.net/articles/mcp-context-tax
  - https://www.techradar.com/pro/security/anthropics-official-git-mcp-server-had-some-worrying-security-flaws-this-is-what-happened-next
