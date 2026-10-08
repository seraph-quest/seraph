---
title: "ADR-010: Reviewed Artifact Pipelines"
---

# ADR-010: Reviewed Artifact Pipelines

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#914](https://github.com/seraph-quest/seraph/issues/914), under [#899](https://github.com/seraph-quest/seraph/issues/899)

## Context

Existing Work proposals, dependencies, verified handoffs and registered native
capabilities can support a finite public-source extraction, evidence dossier
and local report. The earlier accepted decision and independent review are
recorded in [issue comment 5957707692](https://github.com/seraph-quest/seraph/issues/914#issuecomment-5957707692).
Its temporary proposal, draft, implementation and receipts were lost. The
reconstruction was independently confirmed by a separate Luna MAX fidelity
review on 2026-10-03 (review SHA-256
`def11bc0b32adb3b163eb238225ed191f4455f7a65b837f8dafc7deceac89855`),
with no findings, and accepted by the lead before runtime implementation. This
does not claim the old files exist or establish new Shipped behavior.

## Decision

Use non-executable operation metadata on `WorkBoardProposal`, existing task,
attempt, job, link, handoff, artifact and verification seams beside unchanged
`procedure_v2`. Register one fixed chain: `browser.public-task.v1` →
`work.evidence-dossier.v1` → `work.local-evidence-report.v1`. Maximum plan size
is four steps, with only this three-leaf template registered. Do not add an
executable parent, arbitrary DAG runtime, model-created tool or model planner.
The two CPU leaves use no model, network, credential or subprocess capability
and record explicit `no_learning`. #911 owns publication and #923 owns exports.

Review binds the exact plan version, original authenticated owner/live root,
Goal/source revisions and permissions, typed inputs/outputs, priority and finite
limits. First admission fixes an absolute deadline of at most 300 seconds,
two attempts per leaf and six aggregate attempts; browser runtime is at most
180 seconds, CPU leaves at most 30 seconds, and outputs at most 64 KiB. Current
Goal remaining bounds narrow admission. Revisions/restarts never renew time,
counters or consumed allowance, and existing #915 typed-review guards remain.

Only exact independently verified completed output can feed an unfinished
consumer. Verify schema/content digests, immutable source attempt, original live
root/owner, current consumer Goal and source permission. The native browser
mapping is exact `service:browser-task` / `browser_public_task`. Consumer proof
recomputes canonical input and fingerprint digests against admitted immutable
parent/handoff, permissions, limits and `no_learning`; actual settled-effect
output readback precedes canonical bound-input consumption CAS. Succeeded alone
is insufficient. Missing, failed, changed or Unknown producer output blocks.

Persist a materialization reservation keyed by operation/version/producer
attempt/consumer slot before bounded private no-follow write, promotion,
independent readback and exact operation/plan/task adoption CAS. Restart recovers
the exact reservation/file binding or stays Blocked. Changed Goal/source freezes
unfinished work by CAS and requires cancellation quiescence. Preserve completed
artifacts, old attempts, immutable effects and Unknown liabilities. Source
replacement after consumer completion is rejected. For an unattempted consumer,
invalidate its old current link/handoff before materializing the new version;
update only current bindings, retaining old handoff/provenance immutably.

Expired operations remain expired. A distinct freshly reviewed finite operation
may reuse only exact independently verified completed output for unfinished
work under the same original live root/owner and current consumer Goal/source
permission. Never adopt or replay old attempts, effects, reservations or
liabilities, and never use a new plan version to renew the old operation.

Treat extracted public text as quoted untrusted structured data. Reports are
`text/plain`, displayed literally in React `<pre>`, with no instruction, HTML
or Markdown interpretation. UI uncertain-mutation recovery persists and reads
back the exact bounded request (at most 16 KiB) before POST in owner/session
scoped `sessionStorage`. Permit only exact idempotent retry on finite known
paths; corrupt/unavailable storage blocks. Do not create a fresh operation,
auto-adopt authority or move roots during recovery.

## Consequences

- Existing durable execution and operator authority remain the only runtime.
- A plan version is approval for that bounded scope, not future external effects.
- Recovery retains provenance and possible liabilities; unavailable proof blocks.
- CPU report processing remains usable without a provider key or model service.
- ADR-008 peer-host targets remain; Linux browser proof does not establish Mac
  native-adapter readiness. This target does not claim Shipped implementation.

## Verification

Require an authenticated managed Work UI journey through real Chromium, SQLite,
private handoffs, deterministic CPU dossier/report and independent artifact/effect
readback. Clearly label intercepted HTTPS source transport. Prove real quoted
prompt-injection text stays literal with zero CPU model/network/process/credential
calls and explicit `no_learning`. Cover schema/digest/owner/root/revision negatives,
plan CAS, priority, cancellation quiescence, partial-write/restart, failed/Unknown
producer, expired-operation rejection and exact distinct-operation output reuse.
Preserve #915 typed-review negatives. Review the cumulative implementation
independently before integration; record unavailable actual host/provider evidence.

The required managed UI blocker remedy is limited to one fresh contribution
index per extension list and one off-loop pending metadata worker shared by
finite GET paths, with a four-second shielded caller wait and actual thread-done
ownership through abort/queued cancellation. Mutations retain fresh canonical
checks; no response/authority cache, removed control, new env switch or deadline
extension. Verify heartbeat/concurrency/abort/recovery/exception redaction plus
actual managed UI readiness. Detailed reconstructed proposal and recovery limits
are in the [research packet](/research/fixed-artifact-pipeline-reconstruction).
