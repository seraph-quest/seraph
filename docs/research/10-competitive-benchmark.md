---
title: 10. Competitive Benchmark
---

# 10. Competitive Benchmark

**State:** Research

**Source review date:** 2026-08-19

## Purpose And Evidence Boundary

This is a dated, source-inspected comparison of Seraph on `develop` with
OpenClaw, Hermes Agent, and IronClaw. It identifies product pressure; it does
not define Seraph's target, execution order, or shipped status. Those belong to
the [Project Constitution](/), the GitHub Project, and
[Development Status](/status).

Competitor entries are based on official documentation, repositories, parity
matrices, and releases. They prove what a project currently documents or ships
in source; they are not independent reliability or quality benchmarks. No
overall parity or superiority claim is allowed from this table.

Evidence labels used here:

- `Documented`: described by the project's official material.
- `Source-inspected`: an official implementation, release, or self-reported
  parity boundary was inspected.
- `Runtime-verified`: exercised against a running reference system by Seraph's
  maintainers.
- `Independently benchmarked`: compared under the same published task and
  scoring protocol.

This refresh is `Source-inspected`; it is not `Runtime-verified` or
`Independently benchmarked`.

## How To Read The Matrix

- `Ahead`: comparable evidence shows Seraph's shipped surface is stronger.
- `Behind`: the competitor's documented/source-inspected surface is materially
  more complete than Seraph's shipped surface.
- `At Par`: comparable evidence shows no material difference.
- `Unknown`: evidence or comparability is insufficient.

`Unknown` is the default when one side has only a deterministic receipt,
marketing claim, or differently scoped implementation.

## Current Axis Matrix

| Axis | OpenClaw | Hermes | IronClaw | Seraph evidence boundary |
| --- | --- | --- | --- | --- |
| Persistent goals and bounded follow-through | `Behind` | `Behind` | `Unknown` | Seraph has goals, strategist ticks, proactive delivery, and feedback foundations, but no shipped goal -> capability job -> verified outcome -> changed-decision loop. |
| Durable jobs, flows, and restart recovery | `Behind` | `Behind` | `Unknown` | Durable workflow foundations exist, but the universal capability job/checkpoint contract remains Partial. |
| Scheduled and event-driven proactivity | `Behind` | `Behind` | `Unknown` | Scheduled work exists; production standing-intent admission, execution, and outcome closure remain incomplete. |
| Typed execution and isolation | `Unknown` | `Unknown` | `Unknown` | All four projects document different boundaries and caveats; no shared escape/exfiltration benchmark establishes a ranking. |
| Memory governance and outcome learning | `Unknown` | `Unknown` | `Unknown` | Canonical memory and guardian learning foundations exist, but comparative recall quality and behavior change are not independently benchmarked. |
| Operator visibility and recovery | `Unknown` | `Unknown` | `Unknown` | Seraph has a dense cockpit, but task efficiency and recovery burden have no current cross-system benchmark. |
| Browser and external-system execution | `Behind` | `Behind` | `Unknown` | Seraph's catalog and proof surfaces do not substitute for broad live task completion. |
| Cross-surface reach | `Behind` | `Behind` | `Behind` | The authenticated paired Mac edge and canonical cross-surface identity are not shipped. |
| Guardian restraint and intervention quality | `Unknown` | `Unknown` | `Unknown` | Seraph has differentiated scaffolding, but no comparable live outcome study proves an advantage. |
| One-GPU priority and non-starvation | `Unknown` | `Unknown` | `Unknown` | Seraph's serial broker is an accepted target, not a shipped cross-consumer receipt. |

Every `Behind` cell is scoped only to documented surface completeness where
Seraph's own status marks the corresponding end-to-end behavior Partial or
missing. It does not score reliability, security quality, task success, or
operator outcomes. Those dimensions remain `Unknown` until the systems are run
under a shared protocol.

## Reference-System Pressure

### Hermes Agent: goal completion and operator velocity

Hermes documents direct pressure on Seraph's goal-directed execution gap.
Persistent Goals continue work until completion, pause, or a budget
boundary. Kanban adds durable tasks, dependencies, reclaim, human-unblock state,
and structured handoffs. Cron, delegation, browser/file/terminal tools, skills,
memory providers, and messaging make those loops immediately usable from a
compact operator surface.

Hermes also documents important limits. Delegated children are isolated but do
not provide universal crash-resumable child execution, and its security guide
warns that host terminal access is not an adversarial sandbox. Seraph should
copy the clarity and completeness of the goal loop, not weaken its own trust
boundary to match feature count.

Official sources reviewed:

- [Persistent Goals](https://hermes-agent.nousresearch.com/docs/user-guide/features/goals)
- [Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban/)
- [Scheduled Tasks](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Tools and Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)
- [Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers/)
- [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security/)
- [Official releases](https://github.com/NousResearch/hermes-agent/releases)

### OpenClaw: durable automation, reach, and memory governance

OpenClaw documents a broad operating surface: a gateway, Control UI,
channels, paired nodes, browser execution, plugins, skills, multi-agent routing,
heartbeats, durable automations, standing orders, and typed resumable flows.
Lobster and Task Flow are particularly relevant because they combine bounded
multi-step execution, approval checkpoints, resume state, timeouts, and operator
inspection.

Its own security material also bounds the comparison: OpenClaw is designed
around a trusted single-operator model, sandboxing is configurable rather than
universal, and its public extension ecosystem creates supply-chain pressure.
Seraph should learn from the control-plane coherence while retaining stricter
canonical memory and execution authority.

Official sources reviewed:

- [Features](https://docs.openclaw.ai/concepts/features)
- [Standing orders](https://docs.openclaw.ai/automation/standing-orders)
- [Heartbeat](https://docs.openclaw.ai/gateway/heartbeat)
- [Automations](https://docs.openclaw.ai/automation/cron-jobs)
- [Task Flow](https://docs.openclaw.ai/automation/taskflow)
- [Lobster](https://docs.openclaw.ai/tools/lobster)
- [Memory architecture](https://docs.openclaw.ai/concepts/memory-architecture)
- [Nodes](https://docs.openclaw.ai/nodes)
- [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing)
- [Security](https://docs.openclaw.ai/gateway/security)
- [Official releases](https://github.com/openclaw/openclaw/releases/)

### IronClaw: explicit execution boundaries

IronClaw provides an explicit typed execution-boundary reference: capability
permissions, WASM isolation, mediated credentials, endpoint allowlists, leak
scanning, resource limits, durable database state, and distinct authorization,
approval, reservation, dispatch, execution, and evidence phases.

Its current breadth should not be overstated. The project's own parity matrix,
self-dated as last reviewed against OpenClaw on 2026-05-02, marks parts of
browser automation, active memory, multi-agent operation, and other
OpenClaw-compatible behavior incomplete. IronClaw is therefore a useful
security-contract reference, not proof that every advertised agent loop is more
mature than Seraph's.

Official sources reviewed:

- [Official README](https://github.com/nearai/ironclaw/blob/main/README.md)
- [Contributor and architecture contract](https://github.com/nearai/ironclaw/blob/main/AGENTS.md)
- [Feature parity matrix](https://github.com/nearai/ironclaw/blob/main/FEATURE_PARITY.md)
- [Official releases](https://github.com/nearai/ironclaw/releases)

## Capability-First Implications For Seraph

The highest-leverage gap is not another capability catalog or proof endpoint.
It is one complete proactive operator journey:

```text
goal or standing intent
  -> candidate intervention
  -> deterministic admission and priority
  -> approval / reservation
  -> typed capability job
  -> real bounded execution
  -> evidence and external readback
  -> outcome evaluation
  -> governed canonical-memory update
  -> changed later decision
```

Research implications for tracked work:

1. Make the universal goal-conditioned loop the first end-to-end capability
   slice, while preserving durable jobs, global GPU arbitration, and secure
   execution as hard dependencies.
2. Measure restart recovery, cancellation, approval-to-readback correctness,
   false triggers, no-invent-work behavior, and changed later decisions.
3. Treat memory as a moat only when provenance, confidence, supersession,
   expiry, contradiction handling, false promotion, and action boundaries are
   measurable.
4. Prefer one authenticated identity across cockpit, paired edge, voice, and
   messaging over a race for raw channel count.
5. Require extensions to support provenance, staged install, update, disable,
   revoke, conformance checks, and rollback before ecosystem breadth counts as
   product progress.
6. Publish axis-specific comparable receipts before changing any claim from
   `Unknown` or `Behind` to `At Par` or `Ahead`.

The [Strategy Claim Ledger](./19-strategy-claim-ledger.md) remains the wording
gate. The [Agent Competition Truth Table](./18-agent-competition-truth-table.md)
provides historical benchmark context and should not override this later dated
source refresh.
