---
slug: /
title: Seraph Project Constitution
---

# Seraph Project Constitution

**Document class:** canonical product and architecture contract

**Capability sequence:** [Guardian capability roadmap](./23-guardian-capability-roadmap.md)

**Completed foundation:** [Epic #736](https://github.com/seraph-quest/seraph/issues/736)

**Shipped-state owner:** [Development Status](./STATUS.md)

Seraph is a local-first proactive guardian: an operator-controlled system that
understands the operator's goals, observes permitted context, proposes and
executes useful work, remembers outcomes, and explains what it is doing.
Seraph is not itself an AGI project. It adapts its behavior and capabilities to
the operator's goals, which may include ambitious research goals.

The self-improving guardian vision means becoming more useful through explicit
feedback, verified outcomes, and reviewed, reversible changes to memory,
procedure preferences, and capability assets within their accepted contracts.
It does not grant autonomous code rewriting, permission expansion, or learning
from task success alone. Current bounded learning mechanics do not establish
measured improvement or generalized autonomous learning; their shipped scope
remains owned by Development Status. In particular, reviewed procedure
preferences and opportunity feedback follow
[ADR-015](./decisions/015-reviewed-procedure-preferences.md) and
[ADR-023](./decisions/023-evidence-bound-guardian-opportunities.md).

This constitution defines the target architecture. It does **not** claim that
every target capability is shipped. The current implementation is recorded in
[Development Status](./STATUS.md), while active work lives only in GitHub issues,
pull requests, and the Seraph Execution project.
This constitution is Seraph's sole product and accepted-target authority.

## No Evals And Provider-Free Implementation

Standing operator direction: do not propose, plan, create, or run evals unless
the operator explicitly asks for evals. No money may be spent on evals. This
includes unpaid or local campaigns, benchmarks, harness research, hidden quality
corpora, model judges, synthetic replacements, and canaries used to measure
quality or improvement. Do not turn any of these into a future capability,
migration, activation, merge, or release gate.
Capability work comes first. Any later eval requires the implemented capability
and a new explicit operator request; it is not a planned milestone, reminder,
or dependency. This direction stands until the operator changes it.

Implementation and acceptance make no paid, live, free-tier, alternate-provider,
or local-model inference calls. Ambient credentials and available credits are
not authorization; disable or intercept external inference at the transport
boundary. Ordinary isolated regression, integration, and security tests, actual
local effects and readback, and independent review remain required. They prove
the checked behavior, not model quality, learned improvement, or superiority.

Self-improvement remains a main product feature: implement reviewed, reversible
adaptation from ordinary task traces, explicit corrections, and skill or
procedure changes under the existing learning authority. It does not depend on
an eval campaign or the cancelled harness work in
[#771](https://github.com/seraph-quest/seraph/issues/771), which is closed as not
planned and excluded from completed
[#736](https://github.com/seraph-quest/seraph/issues/736).

This boundary governs development and acceptance; it does not disable ordinary
operator-approved product inference under existing consent, grants, routing,
and budgets. Historical receipts, capability-proof endpoints, and unverified
quality labels authorize neither an eval nor implementation-time model calls.
Preserve historical evidence without treating it as an active work requirement.

## Product Promise

Seraph should help an operator make sustained progress without becoming an
opaque autonomous process. It must:

- maintain explicit goals, constraints, and success measures;
- observe only paired, consented sources and show what was captured;
- turn evidence into bounded proposals, queued work, reports, and actions;
- request approval at declared trust boundaries;
- preserve durable memory, artifacts, checkpoints, and audit receipts;
- remain useful when a model or optional integration is unavailable; and
- report the effective runtime path, not a configured or aspirational one.

<!-- outcome-use-cases:start -->
- Turn goals into prioritized plans, scheduled work, and evidence-backed progress reviews.
- Monitor consented desktop context and produce searchable summaries that help the operator reflect and recover focus.
- Continuously research operator-selected topics and connect material findings to active goals and decisions.
- Execute bounded software-engineering and knowledge workflows with approvals, artifacts, checkpoints, and audit receipts.
- Continue one trusted conversation across the cockpit, paired voice, and paired messaging surfaces.
<!-- outcome-use-cases:end -->

A surface is not supported until its implementation ticket has shipped and the
status page says so.

Public entry points: [docs](https://docs.seraph.quest),
[contributing](https://github.com/seraph-quest/seraph/blob/develop/CONTRIBUTING.md),
[support](https://github.com/seraph-quest/seraph/blob/develop/SUPPORT.md),
[security](https://github.com/seraph-quest/seraph/blob/develop/SECURITY.md), and
[GitHub Discussions](https://github.com/seraph-quest/seraph/discussions).

## Four-Layer Architecture

| Layer | Responsibility | Must not own |
| --- | --- | --- |
| **Guardian kernel** | Goals, policy, planning, prioritization, intervention, memory coordination, audit, and operator-visible state | Provider-specific behavior or unbounded side effects |
| **Capability runtime** | Typed capabilities, durable jobs, checkpoints, artifacts, approvals, sandbox/policy enforcement, and bounded remote-inference admission | Product goals or hidden provider fallback |
| **Model fabric** | OpenRouter ordinary inference with explicit routing, consent, budgets and receipts; one dedicated optional NEAR HTTPS text capability under ADR-025 | Agent identity, durable state, shell orchestration, or product policy |
| **Interfaces and edges** | Browser cockpit, API, consented local or paired desktop context, voice, and paired messaging adapters | Canonical memory, authority, or an independent agent runtime |

The guardian kernel decides **why and what**. The capability runtime controls
**how work may execute**. The model fabric supplies bounded inference. Interfaces
and edges provide **where the operator interacts and what consented context is
available**.

### All-plugin Cordis Composition

[ADR-026](./decisions/026-all-plugin-cordis-architecture.md) defines the accepted
composition target: the agent loop, goals/policy/authority services, capability
execution and tools, model integration, persistence and memory, scheduling,
audit, and interface/edge adapters are Cordis plugins. Only necessary
bootstrap/composition machinery remains outside runtime plugins; shared types
and pure libraries are not independent runtime components.

The four layers describe logical responsibilities, not a non-plugin guardian
kernel or a prescribed package tree. Mandatory trusted enforcement services must
be ready before dependent work is admitted; plugin loading never grants authority
and dependency injection is not sandboxing. Reviewed authored capability packs
retain ADR-013/020 protections and are not trusted runtime plugins. All existing
provider, canonical-state, admission, consent, audit, recovery and portability
boundaries remain in force. Migration capabilities are **Planned**; the current
Python/FastAPI runtime and React cockpit are not retroactively Cordis-based.

## Locked Decisions

The following architecture decisions are normative:

1. [ADR-001: Inference-only model providers](./decisions/001-inference-only-model-providers.md)
2. [ADR-002: One-GPU serial priority scheduling](./decisions/002-one-gpu-serial-priority-scheduling.md)
3. [ADR-003: Canonical memory boundary](./decisions/003-canonical-memory-boundary.md)
4. [ADR-004: GPU core and paired Mac edge](./decisions/004-gpu-core-mac-edge-topology.md) (fixed placement superseded by ADR-008)
5. [ADR-005: Epic integration branch workflow](./decisions/005-epic-integration-branch-workflow.md)
6. [ADR-006: OpenRouter-only inference phase](./decisions/006-openrouter-only-inference-phase.md)
7. [ADR-007: Bounded Node repair and Linux process supervision](./decisions/007-bounded-node-repair-supervision.md)
8. [ADR-008: Portable core and consented context](./decisions/008-portable-core-and-consented-context.md)
9. [ADR-009: Tested repository publication](./decisions/009-tested-repository-publication.md)
10. [ADR-010: Reviewed artifact pipelines](./decisions/010-reviewed-artifact-pipelines.md)
11. [ADR-011: Finite GitHub consent, recovery and capacity closure](./decisions/011-finite-github-connection-consent.md)
12. [ADR-012: Finite durable read-only research](./decisions/012-finite-durable-readonly-research.md)
13. [ADR-013: Fixed reviewed local tool package](./decisions/013-fixed-reviewed-local-tool-package.md)
14. [ADR-014: Bounded evidence dependencies](./decisions/014-bounded-evidence-dependencies.md)
15. [ADR-015: Reviewed procedure preferences](./decisions/015-reviewed-procedure-preferences.md)
16. [ADR-016: Exact Gmail reply send and readonly reconciliation](./decisions/016-exact-gmail-reply-send.md)
17. [ADR-017: Private bounded document comparison](./decisions/017-private-bounded-document-comparison.md)
18. [ADR-018: Fixed Moltbook private browser read](./decisions/018-fixed-moltbook-private-browser-read.md)
19. [ADR-019: Exact owned-calendar reschedule](./decisions/019-exact-calendar-reschedule.md)
22. [ADR-022: Portable selected-text context](./decisions/022-portable-selected-text-context.md)
20. [ADR-020: Reviewed authored capability packages](./decisions/020-reviewed-authored-capability-packages.md)
21. [ADR-021: Exact Forgejo issue title transaction](./decisions/021-exact-forgejo-issue-title.md)
23. [ADR-023: Evidence-bound guardian opportunities](./decisions/023-evidence-bound-guardian-opportunities.md)
24. [ADR-024: Purpose-specific OpenRouter routes](./decisions/024-purpose-specific-openrouter-routes.md)
25. [ADR-025: Optional NEAR HTTPS text inference](./decisions/025-near-https-text-inference.md)
26. [ADR-026: All-plugin Cordis agent architecture](./decisions/026-all-plugin-cordis-architecture.md)
27. [ADR-027: Finite standing public goal programmes](./decisions/027-standing-public-goal-programmes.md)
28. [ADR-028: Reviewed typed task methods and reusable procedures](./decisions/028-reviewed-task-methods.md)
29. [ADR-029: Profiled browser interactions](./decisions/029-profiled-browser-interactions.md)
30. [ADR-030: Bounded iterative repository work and original-producer recovery](./decisions/030-bounded-iterative-repository-work.md)
31. [ADR-031: Bounded general documents](./decisions/031-bounded-general-documents.md)
32. [ADR-032: Repository crash recovery evidence](./decisions/032-repository-crash-recovery-evidence.md) (supersedes three ADR-030 recovery clauses)

ADR-023 defines evidence-bound Guardian opportunities and ADR-024 defines
purpose-specific OpenRouter routes. ADR-025 permits one separate optional NEAR
HTTPS text capability with explicit provider plaintext access. These accepted
decisions retain the existing authority, serial admission and accounting
boundaries. [Development Status](./STATUS.md) owns the implemented capability
scope and its limits; acceptance alone does not establish live provider quality
or availability.

Changing a locked decision requires a superseding ADR, a tracked issue, an
independent Critic/Contrarian review, and updates to every affected active doc.

## Capability Vocabulary

Use these terms consistently in APIs, UI copy, issues, and active docs:

- **capability**: a Seraph-owned, typed unit of work with declared input,
  output, permissions, limits, and receipts;
- **job**: a durable invocation of a capability;
- **artifact**: addressable output produced or consumed by a job;
- **checkpoint**: durable resumable job state that excludes unsafe secret data;
- **model route**: an inference endpoint plus model and effective routing state;
- **edge**: a paired, revocable source or interface outside the canonical core;
- **provider**: an inference or advisory integration, never Seraph's authority;
- **guardian cycle**: observe, assess, propose, approve when required, act,
  read back and record the outcome, and remember.

## Capability Status Vocabulary

Capability inventory, APIs, and operator UI use this lifecycle vocabulary:

- **Shipped**: present on `develop`, available to its declared audience, and
  supported by the named validation receipt.
- **Partial**: usable behavior exists on `develop`, with named missing boundaries.
- **Experimental**: runnable only with explicit opt-in; interfaces or persistence
  may change and the UI must say so.
- **Planned**: accepted tracked work without a shipped implementation.
- **Deprecated**: still present for migration, with its replacement and removal
  issue named.
- **Excluded**: intentionally outside Seraph's product boundary; it must not be
  advertised or silently restored as fallback.

“Configured,” a deterministic fixture, or a branch-local change is not evidence
of **Shipped**. A capability cannot be both Shipped and Planned. Experimental
and Deprecated capabilities must remain visibly distinct from supported paths.

## Document And Decision States

Material documents and decisions may use these states:

- **Target**: accepted architecture or product intent, not yet shipped.
- **Research**: evidence or an option under consideration, not a committed product contract.
- **Archived**: historical context that may contradict the current contract.
- **Blocked**: accepted work cannot proceed until a named condition changes.

Open branches describe intended post-merge truth and must be identified as such.

## Runtime Invariants

- Model providers are inference-only. Seraph does not depend on Codex CLI,
  Claude Code, or another coding-agent runtime to operate.
- Ordinary model inference uses the governed OpenRouter HTTPS route; ADR-025
  permits only the separate fixed optional NEAR HTTPS text capability. Exact
  model/modalities and upstream policy must be verified before dispatch;
  unsupported capabilities remain blocked or degraded rather than falling back
  silently.
- Remote inference uses one shared bounded admission contract with owner,
  priority, deadline, cancellation, budget, idempotency, and reconciliation
  receipts. The initial in-flight limit is one; this is an API admission bound,
  not a claim about upstream hardware concurrency. The integrated runtime has
  durable jobs, provider cost reservations and witnessed reconciliation;
  contacted unknown costs remain liabilities. See the dated
  [source/test baseline](/research/seraph-capability-baseline-2026-10-05)
  for the develop/main boundary and unresolved focused-test failures. The old
  process-local migration description is historical, not the current target.
- Canonical goals, memory, jobs, artifacts, approvals, and audit records remain
  in Seraph-owned storage. Advisory memory providers may augment recall but do
  not become authoritative.
- macOS and Linux are peer core-host targets under ADR-008. The operator chooses
  the canonical workspace host; `jupyter` is a historical deployment example.
  Consented context may originate locally or from a paired, revocable edge,
  without transferring authority. GPU hardware, local model
  weights, and a VLM wrapper are not active product prerequisites during the
  OpenRouter-only phase.
- Effective model, queue, edge, and degraded state must be operator-visible.
  Silent fallback that makes the UI lie is a contract violation.

## Safety And Authority

Observation, inference, and execution are different permissions. Pairing an
edge does not grant execution authority. Supplying a provider key does not grant
data-egress approval. A capability must declare filesystem, process, network,
credential, and external-mutation boundaries, and Seraph must fail closed when
required policy or approval is absent.

## Definition Of Product Progress

Product progress is a user-visible capability with bounded failure behavior and
evidence appropriate to its risk. Documentation, issue creation, proof-only
fixtures, and model output are not substitutes for the capability itself.
Completion requires the owning issue and project state, focused tests, live or
mocked operator receipts as appropriate, and independent review to agree.

## Documentation Ownership

- `docs/implementation/`: shipped truth, this constitution, ADRs, and durable
  operator contracts.
- `docs/research/`: evidence, alternatives, and dated comparisons; research
  cannot accept a target or claim shipping.
- `docs/docs/` (`/legacy`): archive; content may contradict the current system.
- GitHub Project, issues, and PRs: execution state; docs never mirror the queue.

Start with [Current App Guide](./12-current-app-guide.md) to run the existing app,
[Development Status](./STATUS.md) for shipped truth, and
[Documentation Contract](./08-docs-contract.md) before changing documentation.
