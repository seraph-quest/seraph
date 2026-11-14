---
id: dsh-style-plugin-structure
title: "ADR-036: DSH-style plugin structure, pinned trust bundle and profiles"
---

# ADR-036: DSH-style plugin structure, pinned trust bundle and profiles

**Status:** Accepted target under [#1173](https://github.com/seraph-quest/seraph/issues/1173). It takes effect on the independently reviewed merge to `develop`. The runtime capabilities it names stay as recorded in [Development Status](../STATUS.md); this decision ships none of them.

**Decision class:** Refining target composition. This decision refines [ADR-026](./026-all-plugin-cordis-architecture.md) by settling three questions it left open:
- package and service granularity;
- which runtime domains end up Cordis-owned;
- how configuration is composed.

ADR-026's trust boundaries, preserved invariants and migration rules stay in force. [ADR-035](./035-near-tee-hosted-inference-only.md) (inference), ADR-013/020 (authored packs) and ADR-001/003 (provider and state ownership) are unchanged.

## Context

On 2026-11-14 the operator directed that Seraph be structured the way DeepSeek Harness (DSH) is. DSH runs on the same Cordis runtime that Seraph migrates to under ADR-026.

DSH's architecture states "There is no privileged core to patch". In DSH:
- Only the Cordis kernel (shared context, service registration, lifecycle-owned effects, typed events and layered config patches) is outside plugins.
- The session log, system prompt, tool registry with its guarded execution pipeline, agents, the agent loop and the LLM adapter seam are plugins.
- Plugins ship in bundles: a shared `dsh-base`, plus app bundles for web, headless, SDK and ACP.
- Named profiles compose bundles and config patches, and `--dump-config` prints the booted tree.
- A capability is a seam made of a service definition, its providers and its consumers. A package that plays only one of those roles is not a seam.
- Persistence, the sandbox and the approval policy are ordinary, replaceable `dsh-base` plugins.

The state of Seraph on `develop` @ `0850178d` (2026-11-14):
- Cordis owns 7 of the 20 runtime domains: agent presets, agent loop, guardian, delegation, board workers, scheduler and dispatch.
- [#1024](https://github.com/seraph-quest/seraph/issues/1024) made state, authority, audit and tasks mandatory providers, hosted in Python and gated by Cordis.
- Composition is selected by environment flags in `.env.<env>` and by the boot cutover manifest. There are no named profiles and no composition dump.
- ADR-026 never settled which of the remaining domains move.

## Decision

Adopt DSH's structure (the kernel boundary, capability seams, bundles and profiles) with one Seraph-specific rule: **trust plugins are pinned.**

### 1. Kernel boundary: what is not a plugin

Two things stay outside plugins:
- **The Cordis kernel:** context, service registration and reflection, lifecycle-owned effects, typed events, and config validation and patching.
- **Seraph's composition bootstrap:** the process entry point, composition and profile validation, the Python provider-worker host (#1024), and startup and shutdown wiring.

The bootstrap holds no policy, no agent loop and no domain state. Everything else is a plugin in a named bundle. Pure types, contracts and libraries need not be plugins (ADR-026).

### 2. Capability seams

Every capability is a seam:
- **A service definition:** a typed `seraph.<noun>.vN` contract that owns its methods, errors and data contracts.
- **One or more providers.**
- **Consumers,** often model-facing tools.

Adding or moving a capability means designing all three parts. A provider swap must not change the definition or the consumers' authority checks.

For example, a remote sandbox provider moves filesystem, subprocess and browser execution together. A NEAR end-to-end-encryption (E2EE) transport would be a provider behind `seraph.inference.v1`.

### 3. Pinned trust bundle

`seraph-trust` contains:
- **`seraph.state.v1`:** the one canonical transactional store, its schema migrations and backup/restore.
- **`seraph.authority.v1`:** principals, grants, approvals, consent and the current-authority checks.
- **`seraph.audit.v1`:** the append-only audit trail.
- **`seraph.tasks.v1`:** the durable job and effect ledger, with leases, fences and Unknown liability.
- **The inference policy and accounting part of `seraph.inference.v1`:** the shared lane, ceiling, reservations, billing evidence and liabilities.
- **The execution guard:** the pre-execution authority, approval and consent check that every capability consumer passes through.

These are plugins in form: they have definitions, providers, consumers and lifecycle. They are **pinned**, which means:

- No profile, config patch or bundle can unload, replace or reorder them, or switch them off. A composition that omits one, or names a different provider, fails validation before boot. (This is the composition-time counterpart of #1024's runtime gate.)
- Replacing a pinned provider takes a superseding ADR, a reviewed release and a forward-only migration, with backup and restore. It is never a runtime or profile choice.
- They may stay hosted in Python indefinitely. Hosting language is not ownership: Cordis consumers reach them only through their seams.
- Other plugins depend on them. They depend on no non-trust plugin.

This is the main departure from DSH, where persistence and approval policy are swappable `dsh-base` plugins. Seraph holds the operator's authority, money and audit trail, so those stay fixed.

### 4. Bundles

| Bundle | Contents | Seam/provider notes |
| --- | --- | --- |
| `seraph-trust` (pinned) | state, authority, audit, tasks, inference policy and accounting, execution guard | §3 |
| `seraph-agent` | agent loop, agent presets, guardian, delegation, board workers, system prompt assembly, tool/capability registry | The registry is `seraph.capabilities.v1`; execution always goes through the execution guard. |
| `seraph-models` | NEAR TEE text, vision and embedding slot providers | Providers behind `seraph.inference.v1`; ADR-035 allowlist and catalog rules. Holds no budget or policy of its own. |
| `seraph-capabilities` | browser, repository/filesystem, sandbox/subprocess, mail, calendar, GitHub, documents, research, source extraction, screen/audio observation | One seam each. Providers are reviewed only (ADR-026 trust table). Authored packs stay under ADR-013/020. |
| `seraph-domains` | goals, memory, artifacts, conversation, connections | Domain plugins with their own logic. Persistence goes only through `seraph.state.v1`: one canonical store, not one database per plugin. |
| `seraph-lifecycle` | scheduler, dispatch | Lifecycle plugins (#1025). The durable ledger in `seraph.tasks.v1` stays the only queue. |
| `seraph-cockpit` | web cockpit and HTTP/WebSocket API | App bundle |
| `seraph-channels` | paired channels (Telegram and others), webhooks | App bundle |
| `seraph-edge` | screen daemon, capture upload, selected-text companion | App bundle; consented-context edge (ADR-008) |
| `seraph-headless` | one-shot or automation runner without the cockpit | App bundle |

### 5. Disposition of the 20 runtime domains

| Domain | Target | Bundle |
| --- | --- | --- |
| `state`, `authority`, `audit`, `tasks` | Pinned, Python-hosted provider | `seraph-trust` |
| `inference` | Seam. Policy and accounting are pinned in trust; NEAR slot providers are Cordis-owned. | `seraph-trust` + `seraph-models` |
| `agent-presets`, `agent-loop`, `guardian`, `delegation`, `board-workers` | Cordis-owned | `seraph-agent` |
| `capabilities` | Cordis-owned registry, guarded execution | `seraph-agent` |
| `research`, `source-extraction` | Cordis-owned seams | `seraph-capabilities` |
| `goals`, `memory`, `artifacts`, `conversation`, `connections` | Cordis-owned domain plugins | `seraph-domains` |
| `scheduler`, `dispatch` | Cordis-owned lifecycle | `seraph-lifecycle` |

Moving each remaining domain is a separately tracked migration slice under ADR-026's rules. Each slice must cover cutover and rollback by epoch, fences, no replay, and operator-visible recovery.

### 6. Profiles and composition dump

A **profile** is a named, reviewed composition. It contains:
- a list of bundles, always including `seraph-trust` first;
- a validated config patch (non-secret, ID-addressed rows);
- the env-file values that the bootstrap still needs.

The initial profiles are:
- `local`: loopback single-operator. #1161 no-login is allowed; the screen daemon is optional.
- `prod`: authenticated, container topology.
- `headless`: no cockpit.

The rules:
- Patch order is profile bundles, then the profile patch, then an operator patch. A later layer can narrow but never widen an earlier one. A patch cannot expand grants, restore revoked consent, change a pinned bundle, or carry credentials (credentials stay in the vault).
- Profiles replace ad-hoc env switches over time. Each env flag that selects composition gets a profile or patch row and a removal issue.
- A read-only **composition dump** shows the booted tree: bundles, plugins, provider digests, owners, epochs, pinned markers and the effective patch.
  - Runtime status exposes it.
  - The managed launcher has a command for it, for example `./manage.sh -e dev composition dump`.
  - It never contains secrets.
- Profile changes use drain, dispose and restart (ADR-026). Hot reload is not required.

### 7. Not adopted from DSH

- **No swappable persistence, approval or sandbox enforcement** (§3). Sandbox providers are swappable only behind the pinned execution guard.
- **One transactional canonical store**, not DSH's per-session append-only JSONL log. Conversation history is a `seraph-domains` plugin over `seraph.state.v1`.
- **No community marketplace and no `plugin add` of arbitrary packages.** ADR-026's three trust boundaries stand. Authored capability packs stay ADR-013/020 packages, not trusted runtime plugins.
- **DSH's directory layout, package names, launch commands and patched Cordis fork are not adopted.**

## Verification

This decision requires:
- source, path and link checks;
- `scripts/check_docs_contract.py` and `scripts/check_strategy_claims.py`;
- the docs typecheck and build;
- an independent Critic/Contrarian review recorded on #1173.

Each implementation slice must prove its claims on actual composition:
- **Pinned bundle:** validation refuses a profile that omits or replaces a pinned bundle.
- **Profiles:** a profile patch cannot widen grants or carry secrets.
- **Composition dump:** it matches the booted tree, with no secrets.
- **Domain moves:** each moved domain passes ADR-026's journey requirements.

Acceptance of this decision is not runtime evidence.

## Consequences

- The open "which domains move" question is settled: everything except the pinned trust providers. A3.3 ([#1026](https://github.com/seraph-quest/seraph/issues/1026)) becomes the app-bundle and profile slice.
- Composition becomes visible and named instead of spread across env flags. Mistakes like a missing or wrong flag surface at validation time, not as confusing runtime state.
- Pinning trades flexibility for safety. A future alternative store or authority provider needs a superseding ADR, not a profile change.
- More seams mean more contracts to review. Every seam needs a definition, provider and consumer design, which slows small additions but keeps provider swaps safe.
- Status in [STATUS](../STATUS.md) and the [current app guide](../12-current-app-guide.md) is unchanged until slices ship.

## Inspected sources

Inspected on **2026-11-14**:

- DeepSeek Harness commit `39163dd30f703a79079e2d2446545cff3c453738` (2026-11-13), [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/architecture.md):
  - **Adopted:** the kernel/plugin boundary, the three-part capability seam, bundles, profiles with layered patches, and the composition dump.
  - **Not adopted:** see §7.
  - **Not covered by that document:** memory, MCP and skills.
- ADR-026's inspection of DSH commit `5badb15` and Cordis commit `f8ea3cd` (2026-10-08) still applies to the kernel primitives.
