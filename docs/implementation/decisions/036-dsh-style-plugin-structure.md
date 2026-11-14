---
id: dsh-style-plugin-structure
title: "ADR-036: DSH-style plugin structure, pinned trust bundle and profiles"
---

# ADR-036: DSH-style plugin structure, pinned trust bundle and profiles

**Status:** Accepted target under [#1173](https://github.com/seraph-quest/seraph/issues/1173), effective on the independently reviewed merge to `develop`. The runtime capabilities it names keep the status [Development Status](../STATUS.md) gives them. This decision ships none of them.

**Decision class:** Refining target composition. It refines [ADR-026](./026-all-plugin-cordis-architecture.md) by settling three questions ADR-026 left open:
- package and service granularity;
- which runtime domains end up Cordis-owned;
- how configuration is composed.

ADR-026's trust boundaries, preserved invariants and migration rules stay in force. This ADR does not amend [ADR-035](./035-near-tee-hosted-inference-only.md); §5 keeps inference inside ADR-035's limits. ADR-008 (edge), ADR-013/020 (authored packs) and ADR-001/003 (provider and state ownership) are unchanged.

## Context

On 2026-11-14 the operator directed that Seraph be structured the way DeepSeek Harness (DSH) is. DSH is built on Cordis, the runtime Seraph is migrating to under ADR-026.

DSH's architecture document states "There is no privileged core to patch". In DSH:
- **Plugins:** the session log, system prompt, tool registry with its guarded execution pipeline, agents, agent loop and LLM adapter seam.
- **Bundles:** plugins ship in a shared `dsh-base`, plus app bundles for web, headless, SDK and ACP. `dsh-sdk-minimal` is the exception: it carries its own explicit tree and does not apply `dsh-base`.
- **Profiles:** DSH's profile launcher and app-boot layer, not the Cordis kernel, combine bundles with layered `cordis.patch` rows. `--dump-config` prints the tree that booted.
- **Capability seams:** a capability is a seam made of three parts, a service definition, its providers and its consumers. A package that has only one of these roles is not a seam.
- **Swappable trust components:** persistence, the sandbox and the approval policy are ordinary, replaceable plugins in `dsh-base`.

Seraph on `develop` at `0850178d` (2026-11-14):
- There are 20 runtime domains (`backend/src/runtime_plugins/ownership.py` `DOMAINS`). `cutover.py` can move 7 of them to Cordis:
  - `agent-presets`, `agent-loop`, `guardian`, `delegation` and `board-workers`;
  - `scheduler` and `dispatch`, which move only when the boot proves the Cordis child is running (#1025).

  Whether any given install has actually moved them depends on that install.
- [#1024](https://github.com/seraph-quest/seraph/issues/1024) keeps `state`, `authority` and `audit` permanently Python-hosted (`PYTHON_HOSTED_DOMAINS`).
  - Python's provider worker also activates `tasks` at boot, but the `tasks` owner row can still be transferred.
  - The gate on these services is Python's `get_session`. Their Cordis facades only report readiness.
- Composition is selected by environment flags in `.env.<env>` and by the boot cutover manifest. There are no named profiles and no composition dump.
- Stock Cordis 4.0.0-rc.10, the version Seraph pins, has no profile launcher. Seraph's bootstrap must build any layering itself.

## Decision

Seraph adopts DSH's structure: the kernel boundary, capability seams, bundles and profiles. It adds one rule of its own: **trust services are pinned.**

### 1. Kernel boundary: what is not a plugin

Two things are not plugins:

- **The Cordis kernel:** context, service registration and reflection, lifecycle-owned effects, typed events and config validation.
- **Seraph's composition bootstrap:**
  - the process entry point;
  - the profile layering and validation described in §6, which Seraph builds, because Cordis does not provide it;
  - the Python provider-worker host (#1024);
  - startup and shutdown wiring.

The bootstrap holds no policy, no agent loop and no domain state. Everything else is a plugin in a named bundle. Pure types, contracts and libraries do not need to be plugins (ADR-026).

### 2. Capability seams

Every capability is a seam with three parts:
- a **service definition**: a typed `seraph.<noun>.vN` contract that owns its methods, errors and data contracts;
- one or more **providers**;
- its **consumers**.

Adding or moving a capability means designing all three. A provider swap must not change the definition or the consumers' authority checks. For example, a remote sandbox provider moves filesystem, subprocess and browser execution together.

### 3. Pinned trust services

**Which services are pinned.** The `seraph-trust` bundle contains these services:

| Service | Identity | Status of the pin |
| --- | --- | --- |
| Canonical transactional store | `seraph.state.v1` | Pinned and Python-hosted today (#1024). |
| Principals, grants, approvals, consent and current-authority checks | `seraph.authority.v1` | Pinned and Python-hosted today (#1024). |
| Append-only audit trail | `seraph.audit.v1` | Pinned and Python-hosted today (#1024). |
| Durable job and effect ledger: leases, fences, Unknown liability | `seraph.tasks.v1` | **New in this ADR.** #1024 activates it at boot but leaves its owner row transferable. Freezing it at `legacy` is an implementation slice. |
| Inference policy and accounting: shared lane, ceiling, reservations, billing evidence, liabilities | `seraph.inference.v1` | **New in this ADR** as a pin (see §5). |
| Execution guard | Part of `seraph.authority.v1` | **New in this ADR** as a named responsibility; adds no new identity (see below). |
| Operator authentication | Part of `seraph.authority.v1` | **New in this ADR** as a pin (see below). |
| Credential vault | No domain row today | **New in this ADR** as a pin (see below). |

**What each new part covers:**
- **Execution guard:** the pre-execution check of authority, approval and consent that every capability consumer passes. Today `CapabilityExecutionHost` calls into it, together with the authority checks.
- **Operator authentication:** operator sessions, and the #1161 local no-login mode with its startup validation.
- **Credential vault:** vault keys and stored secrets. It is a trust service hosted by the Python provider worker. Giving it a domain row is a separate slice.

**What "pinned" means:**

- **The bootstrap enforces the pin, not Cordis.** Before boot, the composition validator rejects any profile, patch or bundle list that omits a pinned service, replaces its provider, or changes its rows. This is the composition-time counterpart of #1024's runtime gate.
- **Pinned services do not depend on the Cordis child.** They are Python-hosted, so they exist whether or not the child runs. Cordis-owned consumers block when the child is missing, as they do today.
- **Replacing a pinned provider takes a superseding ADR,** a reviewed release and a forward-only migration with backup and restore. It is never a runtime or profile choice.
- **Hosting language is not ownership.** Pinned services may stay Python-hosted indefinitely. Cordis consumers reach them only through their seams.
- **Dependencies point one way.** Other plugins depend on pinned services. Pinned services depend on no non-trust plugin.

This is the main departure from DSH, where persistence and approval policy are swappable `dsh-base` plugins. Seraph holds the operator's authority, money and audit trail.

### 4. Bundles

| Bundle | Contents | Notes |
| --- | --- | --- |
| `seraph-trust` (pinned) | the §3 services | Python-hosted |
| `seraph-agent` | agent loop, agent presets, guardian, delegation, board workers, system prompt assembly, capability registry (`seraph.capabilities.v1`) | Every execution passes the execution guard. |
| `seraph-models` | NEAR TEE text, vision and embedding slot adapters | Provider adapters under `seraph.inference.v1`, inside ADR-035 limits (§5) |
| `seraph-capabilities` | browser, repository and filesystem, sandbox and subprocess, mail, calendar, GitHub, documents, research, source extraction, screen and audio observation; MCP and other external adapters; skills, extensions and authored capability packs | One seam per capability, with three provider classes:<br/>• Reviewed trusted providers.<br/>• MCP and external adapters, which stay in ADR-026's external-adapter trust tier.<br/>• Skills, extensions and authored packs, which stay ADR-013/020 packages behind the capability registry and are never trusted runtime plugins. |
| `seraph-domains` | goals, memory, artifacts, conversation, connections | Domain plugins persist only through `seraph.state.v1`. `connections` holds credential *references*; the secrets stay in the pinned vault. |
| `seraph-lifecycle` | scheduler, dispatch | Lifecycle plugins (#1025). No new queue: the existing durable stores stay the queues (`ScheduledJob`/`ScheduledJobRun`, governed schedule occurrences, `WorkflowRunState`, `WorkBoardTask`/`WorkBoardAttempt`). |
| `seraph-cockpit` | web cockpit and HTTP/WebSocket API | App bundle |
| `seraph-channels` | paired channels (Telegram and others), webhooks | App bundle |
| `seraph-edge` | screen daemon, capture upload, selected-text companion | App bundle; consented-context edge (ADR-008) |
| `seraph-headless` | runner without the cockpit | App bundle |

### 5. Where each of the 20 runtime domains ends up

| Domain | Target | Bundle |
| --- | --- | --- |
| `state`, `authority`, `audit` | Pinned, Python-hosted (already pinned by #1024) | `seraph-trust` |
| `tasks` | Pinned, Python-hosted (new: freezing the owner row is a slice) | `seraph-trust` |
| `inference` | Pinned, Python-hosted as one domain row and one epoch. The NEAR slot adapters are `seraph-models` providers *registered under* this domain, hosted with it in Python. They do not get their own domain row. Moving them into a Cordis package would need an amendment to ADR-035 (ADR-035 currently rules out a new Cordis package); this ADR does not decide that. | `seraph-trust` + `seraph-models` |
| `agent-presets`, `agent-loop`, `guardian`, `delegation`, `board-workers` | Cordis-capable; cutover per `cutover.py` | `seraph-agent` |
| `capabilities` | Cordis-capable registry; execution through the guard | `seraph-agent` |
| `research`, `source-extraction` | Cordis-capable seams | `seraph-capabilities` |
| `goals`, `memory`, `artifacts`, `conversation`, `connections` | Cordis-capable domain plugins | `seraph-domains` |
| `scheduler`, `dispatch` | Cordis-capable, cut over only when the boot proves the child is running (#1025) | `seraph-lifecycle` |

Moving each remaining domain is its own tracked migration slice under ADR-026's rules. Each slice covers:
- cutover and rollback by epoch;
- fences;
- no replay;
- operator-visible recovery.

### 6. Profiles and composition dump

**What a profile is.** A profile is a named, reviewed composition. It contains:
- a bundle list, always starting with `seraph-trust`;
- a validated config patch: non-secret rows addressed by ID;
- the environment file the bootstrap still reads.

**The profiles.** They map onto today's launcher:

| Profile | Launcher | Deployment environment | Notes |
| --- | --- | --- | --- |
| `local` | `./manage.sh -e dev local` | `DEPLOYMENT_ENVIRONMENT=development` | Loopback |
| `prod` | `-e prod` | `production` | Authenticated, container topology |
| `headless` | either | either | No `seraph-cockpit` |

`test` remains a test-process environment only, not a profile.

**Rules for patches.**

- **Patch layers apply in a fixed order:**
  1. profile bundles;
  2. the profile patch;
  3. an operator patch.

  Each layer may only:
  - change rows of non-pinned plugins;
  - add rows that name a reviewed provider for an existing seam;
  - select values inside each row's validated schema.
- **A patch can never:**
  - touch a pinned row;
  - grant authority;
  - enable a consent-gated provider without its consent flow;
  - restore revoked consent;
  - carry credentials, which stay in the vault.

  This allow-list is stricter than DSH, where any patch row may replace or insert config.
- **Security switches stay startup-validated environment settings,** never profile or patch rows. These include the #1161 `OPERATOR_AUTH_LOCAL_NO_LOGIN` and its validation of the development environment, loopback bind and peer, and Host and Origin checks. No profile or patch can enable no-login, and `prod` can never carry it. Environment values cannot affect pinned rows beyond these startup-validated security settings.
- **Composition flags move into profiles over time.** Each environment flag that selects composition, rather than security, gets a profile or patch row and a removal issue.

**The composition dump** is read-only. It shows the tree that booted:
- bundles and plugins;
- provider digests;
- owners and epochs;
- which services are pinned;
- the effective patch.

Runtime status exposes it, and the managed launcher gets a command for it (for example `./manage.sh -e dev composition dump`). It never contains secrets.

Profile changes use drain, dispose and restart (ADR-026). Hot reload is not required.

### 7. What this ADR does not adopt from DSH

- **Swappable persistence, approval, authentication, vault or sandbox enforcement** (§3). Sandbox providers can be swapped only behind the pinned execution guard.
- **DSH's free-form patching.** §6 restricts patches to an allow-list.
- **DSH's per-session append-only JSONL log.** Seraph keeps one transactional canonical store; conversation history is a `seraph-domains` plugin over `seraph.state.v1`.
- **A community marketplace or `plugin add` of arbitrary packages.** ADR-026's three trust boundaries stand.
- **DSH's directory layout, package names, launch commands and patched Cordis fork.**
- **NEAR end-to-end encryption.** It would fit as an inference transport provider. ADR-035 currently uses no E2EE, so adding it needs its own ADR-035 amendment.

## Verification

This decision requires:
- source, path and link checks;
- `scripts/check_docs_contract.py` and `scripts/check_strategy_claims.py`;
- the docs typecheck and build;
- an independent Critic/Contrarian review, recorded on #1173.

The docs-contract `ADRS` list covers only ADR-001 to ADR-005, so its markers are not machine-checked for this ADR. The required markers are present.

Each implementation slice must prove its claims on actual composition:
- the validator refuses a profile that omits, replaces or patches a pinned service;
- the `tasks` owner row is frozen;
- a patch cannot widen grants, enable no-login or carry secrets;
- the composition dump matches the tree that booted and contains no secrets;
- each domain that moves passes ADR-026's journey requirements.

Acceptance of this decision is not runtime evidence.

## Consequences

- **The open "which domains move" question is settled:** every domain except the pinned trust services. A3.3 ([#1026](https://github.com/seraph-quest/seraph/issues/1026)) becomes the app-bundle and profile slice. Pinning `tasks` and naming the execution guard, authentication and vault as pinned are new slices.
- **Composition becomes named and visible** instead of being spread across environment flags. Mistakes surface at validation time.
- **Pinning trades flexibility for safety.** An alternative store, authority, vault or authentication provider needs a superseding ADR.
- **More seams mean more contracts to review.** That slows small additions but keeps provider swaps safe.
- **[STATUS](../STATUS.md) and the [current app guide](../12-current-app-guide.md) are unchanged** until slices ship.

## Inspected sources

Inspected on **2026-11-14**:

- **DeepSeek Harness** commit `39163dd30f703a79079e2d2446545cff3c453738` (2026-11-13), [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/architecture.md):
  - **Adopted:** the kernel and plugin boundary, the three-part capability seam, bundles, profiles with layered patches (DSH's launcher and app-boot layer), and the composition dump.
  - **Not adopted:** see §7.
  - **Not covered by that document:** memory, MCP and skills. Their mapping in §4 is Seraph's own.
- **Seraph code** at `develop` `0850178d`:
  - `backend/src/runtime_plugins/ownership.py` (`DOMAINS`, `PYTHON_HOSTED_DOMAINS`);
  - `composition.py` (`MANDATORY_PLUGINS`);
  - `worker.py`;
  - `cutover.py`.
- **ADR-026's inspection** of DSH `5badb15` and Cordis `f8ea3cd` (2026-10-08) still applies to the kernel primitives.
