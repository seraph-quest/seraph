---
slug: /
title: Seraph Project Constitution
---

# Seraph Project Constitution

**Document class:** canonical product and architecture contract

**Target program:** [Epic #736](https://github.com/seraph-quest/seraph/issues/736)

**Shipped-state owner:** [Development Status](./STATUS.md)

Seraph is a local-first proactive guardian: an operator-controlled system that
understands the operator's goals, observes permitted context, proposes and
executes useful work, remembers outcomes, and explains what it is doing.
Seraph is not itself an AGI project. It adapts its behavior and capabilities to
the operator's goals, which may include ambitious research goals.

This constitution defines the target architecture. It does **not** claim that
every target capability is shipped. The current implementation is recorded in
[Development Status](./STATUS.md), while active work lives only in GitHub issues,
pull requests, and the Seraph Execution project.
This constitution is Seraph's sole product and accepted-target authority.

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
| **Model fabric** | OpenRouter-only active inference for the Epic #736 phase, with explicit routing, consent, budgets, and receipts | Agent identity, durable state, shell orchestration, or product policy |
| **Interfaces and edges** | Browser cockpit, API, paired Mac observation edge, voice, and paired messaging adapters | Canonical memory, authority, or an independent agent runtime |

The guardian kernel decides **why and what**. The capability runtime controls
**how work may execute**. The model fabric supplies bounded inference. Interfaces
and edges provide **where the operator interacts and what consented context is
available**.

## Locked Decisions

The following architecture decisions are normative:

1. [ADR-001: Inference-only model providers](./decisions/001-inference-only-model-providers.md)
2. [ADR-002: One-GPU serial priority scheduling](./decisions/002-one-gpu-serial-priority-scheduling.md)
3. [ADR-003: Canonical memory boundary](./decisions/003-canonical-memory-boundary.md)
4. [ADR-004: GPU core and paired Mac edge](./decisions/004-gpu-core-mac-edge-topology.md)
5. [ADR-005: Epic integration branch workflow](./decisions/005-epic-integration-branch-workflow.md)
6. [ADR-006: OpenRouter-only inference phase](./decisions/006-openrouter-only-inference-phase.md)

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
- **edge**: a paired, revocable source or interface outside the GPU core;
- **provider**: an inference or advisory integration, never Seraph's authority;
- **guardian cycle**: observe, assess, propose, approve when required, act,
  evaluate, and remember.

## Capability Status Vocabulary

Capability inventory, APIs, and operator UI use this lifecycle vocabulary:

- **Shipped**: present on `develop`, available to its declared audience, and
  supported by the named validation receipt.
- **Partial**: usable behavior exists on `develop`, with named missing boundaries.
- **Experimental**: runnable only as an opt-in canary; interfaces or persistence
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
- **Research**: evidence or option under evaluation, not a committed product contract.
- **Archived**: historical context that may contradict the current contract.
- **Blocked**: accepted work cannot proceed until a named condition changes.

Open branches describe intended post-merge truth and must be identified as such.

## Runtime Invariants

- Model providers are inference-only. Seraph does not depend on Codex CLI,
  Claude Code, or another coding-agent runtime to operate.
- During Epic #736's active implementation phase, model inference uses only the
  governed OpenRouter HTTPS route. Exact model/modalities and upstream policy
  must be verified before dispatch; unsupported capabilities remain blocked or
  degraded rather than falling back silently.
- Remote inference uses one shared bounded admission contract with owner,
  priority, deadline, cancellation, budget, idempotency, and reconciliation
  receipts. The initial in-flight limit is one; this is an API admission bound,
  not a claim about upstream hardware concurrency. The migration branch has
  process-local admission and operator receipts; durable queue persistence and
  provider cost reservation/reconciliation remain tracked follow-up work in
  #743/#744.
- Canonical goals, memory, jobs, artifacts, approvals, and audit records remain
  in Seraph-owned storage. Advisory memory providers may augment recall but do
  not become authoritative.
- The target deployment places the authenticated Seraph core and canonical state
  on the selected host (currently `jupyter`). A Mac is a paired, revocable
  observation/interface edge, not the control plane. GPU hardware, local model
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
