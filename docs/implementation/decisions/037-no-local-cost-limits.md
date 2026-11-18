---
id: no-local-cost-limits
title: "ADR-037: No local cost limits; the provider account holds them"
---

# ADR-037: No local cost limits; the provider account holds them

**Status:** Accepted under [#1284](https://github.com/seraph-quest/seraph/issues/1284), 2026-11-18, effective on the independently reviewed merge to `develop`. It is delivered by two stacked pull requests that are released together; both are delivered (see Delivery). [Development Status](../STATUS.md) owns what each merged slice ships.

**Decision class:** Superseding policy. Supersedes the budget, reserve, period and overrun clauses of [ADR-006](./006-openrouter-only-inference-phase.md), [ADR-012](./012-finite-durable-readonly-research.md), [ADR-024](./024-purpose-specific-openrouter-routes.md), [ADR-025](./025-near-https-text-inference.md), [ADR-027](./027-standing-public-goal-programmes.md), [ADR-030](./030-bounded-iterative-repository-work.md) and [ADR-035](./035-near-tee-hosted-inference-only.md), and the per-task and per-programme dollar allowances. Every other clause of those decisions stays in force.

## Context

Seraph kept its own money rules on top of the provider's: a deployment spend
ceiling, a per-request reserve, a monthly period review, an overrun latch with
its reserve review, "budget exhausted" and "no budget set" states, and per-task
and per-programme dollar allowances. Any one of them could stop chat, research
or background work, and an answer could be discarded because its cost was above
the reserve or because NEAR's billing lookup returned nothing.

On 2026-11-18 the operator directed in #1284: "Remove any cost constraints,
they are set via the api provider side." The operator then approved the design
for #1284 with four decisions, listed below.

## Decision

Seraph never blocks, pauses or refuses inference because of cost. Spending
limits live in the provider (NEAR) account. Seraph records what each request
cost as history and display only, for example "$3.20 used this month". No
recorded amount is compared with a limit.

The ledger stays, with a narrower job. Each provider call still writes one
durable **request record**, which:

- gives at-most-once execution of an operation;
- binds the consent revision that is rechecked at contact;
- keeps a contact denial durable across restarts;
- records the charge when the provider reports it.

A request whose charge NEAR does not report is recorded as "not priced yet". It
holds neither the lane nor the job, and its answer is delivered. The optional
"record the charge" action fills in the amount; nothing waits on it.

### Superseded

- ADR-006, 024, 025 and 035: the deployment spend ceiling, its mirrors and
  readback, the per-slot and per-route request cost bound, the request reserve
  and its review, the monthly period review and clock correction, the overrun
  latch, budget exhaustion, and the rule that a missing or high charge holds the
  lane or withholds the answer.
- ADR-012: research funding against a cost allowance, slot bound or ceiling, and
  the overrun clauses of the shared contact guard. The atomic two-row funding,
  the policy recheck and the durable never-contacted denial stay.
- ADR-027: the programme inference ceiling, zero-budget blocking and the
  generation spend allowance.
- ADR-030: the original-cost terms of the repository ceilings and the dollar
  part of the shared group limit. Calls, iterations, time and cutoff stay.
- Per-task, per-communication, per-delegation and per-programme dollar
  allowances.

### Kept

None of these is a cost limit:

- operator consent and egress revocation, with the consent digest rechecked
  under the contact writer;
- the slot allowlist, catalog binding, capability proofs and the attestation
  disclosure;
- one shared serial lane with `max_inflight` 1, priority, original deadlines,
  cancellation and idempotency;
- queue and count bounds (`max_queued`, `max_outstanding_per_owner`,
  `max_retries`, per-task call ceilings, step and item limits);
- the workspace deployment binding, as a consent fence: it stops a restored,
  copied or moved workspace from switching a revoked egress consent back on, so
  `manage.sh local accounting-rebind` stays;
- a capability pack's `0 = may not use inference`, as an authority flag that is
  never compared with spend.

### Operator-approved decisions

1. Per-task and per-programme dollar allowances are removed too.
2. Programmes saved with a $0 allowance are paused on upgrade, not started. The
   pause is keyed on the stored value, not on a date, and an explicit resume
   lets them run.
3. The deployment binding stays a block. Its wording changes from cost to
   consent.
4. The capability pack "0 = may not use inference" flag stays.

### Migration

Nothing on disk is rewritten or deleted to drop a limit. Old settings files
load with their cost fields, which are read past and dropped on the next
ordinary save, so the stored configuration digest and the operator's consent
stay current. Old ledger rows, settings history entries and spend totals stay
as recorded. API requests that still carry a removed dollar field are accepted
and the field is ignored.

## Delivery

Two stacked pull requests under #1284, released together. Both are delivered:

1. The shared lane, Settings and chat: no ceiling, reserve, period review,
   overrun latch or budget state; the request record and spend display; this
   ADR.
2. Work allowances and a display-only ledger:
   - Per-task, per-group, specialist, communication-source, repository-repair
     and delegation dollar allowances are removed; call counts, step limits,
     deadlines and wall time stay. Create requests accept and ignore dollar
     fields; stored envelopes are unchanged and still verify.
   - Standing goal programmes have no inference allowance and new programmes
     store none. A one-time startup step pauses every programme that is not
     ended and whose stored `max_inference_microusd` is exactly 0 (reason
     `programme_paused_no_allowance`, note "Paused: it had no inference
     allowance. Resume it to run."), keyed on the stored value, not a date.
     Programmes with a positive stored allowance continue without a limit.
     `programme_zero_budget` and `programme_allowance_exhausted` are gone.
   - The `cost_liability` job status is no longer produced. It stays a valid,
     readable status for jobs already in it; their reconcile/settle control
     keeps working and settling is optional. Approval resume no longer
     compares `budget_digest` (the column is kept; new jobs write null).
   - Ledger continuity is display-only. A digest, totals, witness or row
     mismatch no longer blocks inference: the ledger reseals from its current
     content, appends a `history_gap` entry to the account history, and the
     snapshot reports `history_verified: false` ("Spend history may be
     incomplete since &lt;date&gt;") until the next clean full verification. The
     background verifier reseals instead of latching. The deployment binding
     and provider-policy record verification still block
     (`provider_policy_continuity_unavailable`). The "liability regression"
     refusals in restore and rebind become warnings; spend history is still
     carried. Lane recovery after manual settlement
     (`reconcile_settled_near_operation`) is deleted because nothing holds the
     lane.

## Consequences and risks

- Nothing in Seraph stops a runaway loop from spending. The bounds that remain
  are one request in flight, per-task call ceilings, deadlines and output-token
  limits. If the NEAR account has no limit, there is no limit. Settings says
  where limits are set.
- Ledger totals move forward to schema version 2. An older build refuses them,
  so rolling the application back needs a database from before the upgrade.
- A retry after a crash in the middle of a call may send the request again and
  be charged twice. Idempotency still prevents double execution of the same
  operation; a retry is a new operation.
- Ledger continuity is display-only, so an edited or rolled-back ledger is
  reported as a history gap rather than refused. Recorded spend can therefore
  be incomplete; it is never used to limit anything.

## Verification

Docs: both docs scripts, docs typecheck and build, and a grep of active docs
for the removed reason codes. Implementation: provider-free tests with
intercepted transport, including a request admitted with no ceiling or slot
bound, an old settings file and ledger loading unchanged, an unpriced request
leaving the lane free, a month change admitted with no review, and a static
test that no removed cost reason code is raised or mapped in `backend/src` or
`frontend/src`. Independent review is required before merge.
