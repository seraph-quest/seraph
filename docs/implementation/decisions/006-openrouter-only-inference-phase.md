---
title: "ADR-006: OpenRouter-only inference phase"
---

# ADR-006: OpenRouter-only inference phase

**Status:** Accepted for Epic #736 target; effective on merge to `develop`

**Decision class:** Superseding phase target

## Context

Epic #736 currently carries a GPU-hosted model server and VLM wrapper as
runtime prerequisites. That hardware dependency prevents the authenticated
Seraph core, canonical workspace, and operator cockpit from running on a
CPU-capable host when inference capacity is unavailable. The user has selected
OpenRouter as the only active inference gateway for the current product phase.

The decision changes the active provider target. It does not claim that
OpenRouter supports every modality or upstream policy without a capability
check, and it does not make the gateway the authority for goals, tools,
approvals, memory, or canonical state.

## Decision

For the Epic #736 implementation phase, all active model inference goes through
the governed OpenRouter HTTPS API. The active profile rejects local, Ollama,
direct OpenAI, Anthropic, and arbitrary OpenAI-compatible inference routes.
The server validates the fixed `https://openrouter.ai/api/v1` destination,
keeps the API key in the trusted backend transport, and applies explicit
upstream allowlisting, parameter requirements, fallback policy, data-collection
policy, consent, and finite cost budgets before dispatch.

The Seraph core and canonical data remain host-local. CPU parsing, image/audio
normalization, lexical indexing, tool sandboxes, authenticated APIs, and
operator UI do not require CUDA, downloaded model weights, a local model
server, or a VLM wrapper. OpenRouter is an inference gateway only; governed
tools and Telegram retain their own destination and credential policies.

The target contract for every remote request is a shared bounded admission
contract with authenticated owner and operation identity, priority, deadline,
cancellation, idempotency, cost reservation, retry rules, and
durable/observable outcome receipts. This migration implements the process-local
serial admission, identity, deadline, cancellation, and bounded retry seam.
Durable job persistence and provider cost reservation/reconciliation remain
explicit follow-up work in #743/#744; the current phase does not pretend those
properties are shipped. The initial policy allows one in-flight request and a
bounded queue; it does not claim control of OpenRouter's upstream concurrency.
Unknown or partial remote outcomes retain possible cost liability and are not
automatically replayed.

### Deployment accounting continuity (#909)

Deployment accounting is owned by one stable Seraph deployment accounting
identity, created once in canonical workspace metadata and bound to the canonical
SQLite reservation ledger. It is independent of authenticated login, verified
identity enrollment, canonical root inode/path, and goal. Existing workspace
maintenance/root-transition ownership carries that identity and all
committed/reserved/unknown ledger evidence to the adopted root and proves
continuity against the prior authoritative ledger. Root adoption,
restore/replacement, login/enrollment, period rollover, and ordinary settings
edits cannot create capacity by erasing, rebasing, or releasing ledger history
or liabilities. Only an explicitly authorized finite ceiling revision may
change prospective admission; it retains all prior evidence and each new
reservation binds that immutable revision. If prior ledger identity/high-water
evidence is absent, stale, or cannot be reconciled, paid egress remains blocked
pending bounded reconciliation; initializing a fresh ledger is not recovery.

A truly empty new deployment may initialize its accounting identity once,
under the existing managed workspace ownership fence, with an explicitly
configured positive finite ceiling. Bootstrap is never a reset of an existing
identity, ledger, or continuity witness. The trusted monotonic high-water
continuity witness lives in the existing host lifecycle sidecar outside
restorable root snapshots. The managed workspace/deployment descriptor makes
that same persistent witness accessible to the runtime, including a focused
persistent Docker bind where supported. Missing, stale, or inaccessible witness
state blocks billable inference as `accounting_continuity_unavailable` while
deterministic CPU capabilities remain usable. Supported restore/root adoption
retains or unions the latest ledger and witness under the existing maintenance
fence before promotion; an older database plus older in-root metadata cannot
authorize egress. Replacement of every trusted store by the host administrator
is outside this continuity contract.

Each reservation records an immutable calendar UTC month (`YYYY-MM`) identity
and immutable ceiling/settings revision. An explicitly authorized finite ceiling
revision changes prospective admission only; it never resets settled history,
reservations, unknown liabilities, or an operation's period identity. Period
rollover or revision changes preserve history, and all unreconciled contacted
liabilities reduce deployment capacity across every subsequent period until
authoritative provider-specific readback or bounded explicit operator settlement.
Settlement retains the original operation, reservation, contact, owner/goal,
period, bound, and cost evidence. Never-contacted reservations may release only
through canonical cancellation/expiry/fenced recovery; contacted unknown outcomes
never auto-replay. `WorkflowRunState`/`DurableJobRepository` remain the accepted
work and ledger authority, and the existing remote broker remains the sole
one-active executor.

The managed profile descriptor retains the same external lifecycle directory
when its active root changes. A different empty root cannot bootstrap a second
accounting owner; explicit maintenance rebind retains the latest ledger before
switching the binding. The trusted witness also binds the owning policy epoch
and configuration digest. Archived or directly copied settings cannot restore
provider authority; managed restore and interrupted-publication recovery leave
egress revoked pending explicit current-revision settings review.

Every observed UTC month different from the authorized month requires an
explicit exact month/accounting-revision acknowledgment before fresh admission,
including ordinary calendar rollover. Observation never grants allowance and
the trusted month high-water never decreases. After acknowledged correction,
future-attributed settled charges conservatively reduce current capacity. An
unreviewed actual charge above its request reserve blocks across all periods
and unrelated settings revisions. Only an explicit adequate reserve-field
review covering the exact settled operation sequence/revision clears that
operation's review state; it cannot cover a later-settled unknown liability.

Vision, audio, and embeddings are capability-selected. A required capability
is blocked with a visible reason until its exact model, request shape, policy,
and live route have been verified. There is no silent local or direct-vendor
fallback. Canonical memory remains local; remote embeddings are versioned by
model, dimension, and schema before an index switch, with lexical degraded
retrieval available only when clearly labeled.

Live route verification is an operation gate for dispatching a capability, not
an implementation merge gate. Provider-free tests use injected or intercepted
transports to prove policy, admission, schema, and blocked/degraded behavior
without a provider key, paid request, or external network call.

## Relationship to earlier ADRs

- This ADR narrows ADR-001's currently selectable provider families for this
  phase while retaining its inference-only boundary and Seraph-owned authority.
- It supersedes ADR-002's physical-GPU resource requirement with a bounded
  `remote_inference` resource class. Priority, cancellation, fencing, and
  reconciliation principles remain.
- It supersedes ADR-004's GPU-host inference requirement. The host may still be
  `jupyter`, but its GPU and the separate VLM repository are not product
  prerequisites. Authenticated core/edge authority boundaries remain.
- ADR-003 remains canonical-memory ownership authority.
- ADR-005 remains the epic integration-branch workflow authority.

Earlier ADRs and receipts remain historical records. Re-activating another
inference provider requires a separately reviewed target change; rollback of
an application/configuration release must not silently re-enable one.

## Consequences

- Provider outage, invalid credentials, unsupported capability, consent
  revocation, rate limiting, and budget exhaustion degrade or block inference
  while the local core remains usable.
- Screenshots and audio have separate local-capture and cloud-egress consent;
  a provider change does not upload historical backlog.
- OpenRouter upstream identity, retention, and usage are recorded as returned
  or verified. Unknown values remain unknown; the system makes no absolute
  retention or security claim.
- Existing GPU/VLM code and receipts may remain as dormant or historical
  artifacts during the migration, but active selectors, launchers, readiness
  gates, and tests cannot require or invoke them.

## Verification

The migration ticket #775 owns the versioned configuration migration, active
route guard, vision/embedding adapters, CPU-only lifecycle, and receipts.
Implementation acceptance requires negative absence tests, deterministic
provider-free tests using intercepted transports, static/configuration checks,
and keyless health receipts. Live text, vision, embedding, provider-quality,
edge, voice, and Telegram receipts are optional operational evidence; they are
not required for merge and remain externally unverified until an authorised
canary supplies them. #751/#752 own audio capability proof. #744 owns the
shared remote admission extension after #743's durable job contract. The
evolution/research work in #771 remains explicitly deferred and does not gate
this phase.
