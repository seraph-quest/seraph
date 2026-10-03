---
title: "ADR-011: Finite GitHub connection mutation consent"
---

# ADR-011: Finite GitHub connection mutation consent

**Status:** Accepted target for #911. Implementation and shipped capability are
not claimed.

**Decision class:** Additive connection and capability authority contract for #911

## Context

Credentials identify an external account but do not authorize Seraph to mutate
it. Existing authenticated principals do not issue blanket external mutation
authority. The GitHub adapter's legacy principal-grant requirement therefore
blocks the real tested-repair publication journey. Effective-grants inspection
and revocation do not create missing consent.

## Proposed decision

Issue explicit finite GitHub mutation consent through the existing authenticated
connection save. Persist its owner/root, exact repository, allowlisted actions,
server-issued identity, binding digest and expiry on the existing connection row,
atomically tied to its revision. There is no parallel grant store or blanket
EXTERNAL_MUTATION addition to authenticated principals. Credential-only and
legacy configuration never issue consent.

Active save requires explicit acknowledgment, exact revision and selected finite
actions: issue creation, comment creation, Git object writes, new-ref creation and
ready-PR creation. Duration is at most one hour and never exceeds the current
authenticated root's known expiry. A fresh login root requires explicit new
consent; data continuity, token refresh and credentials never extend or transfer
consent. No merge, release or existing-ref update is added.

Connection consent authorizes a supported GitHub boundary, not a particular
publication. Every job also retains its exact approval or existing finite
standing-reviewed authority. ADR-009 publication still requires the same fresh
approval for local_host_execution and all exact named remote effects. Canonical
job, preview and approval bindings include original consent/root/revision. New
consent never adopts old jobs or unknown effects.

The existing GitHub adapter derives permission from canonical owner/root,
connection, repository, revision, action and expiry at each effect boundary and
the protected post-resolution transport handoff. Caller Booleans and cached
inventory cannot authorize writes. Scope updates are blocked while reserved;
revocation remains available, advances the local fence and retains contacted
liabilities. In-flight uncertainty is visible and is not external undo.

Reconciliation is GET-only under separately verified original effect/root/
connection identities. A read may record verified external effect truth, but
overall job/task success requires a fresh canonical check of original owner/root
and exact Goal ownership/revision before adapter finalization and task adoption.
Changed authority leaves an explicit blocked/incomplete job/task and retains the
verified effect. The current connection revision grants read-only observation
and never replaces original job, approval or write-consent bindings.
Stopped or expired mutation consent cannot authorize
POST replay; expired authentication or another fresh root cannot inherit contact
authority. Existing explicit ownership recovery remains distinct from consent.

Expose root-bound expiry/actions/effective state and explicit opt-in in existing
GitHub connection settings and the derived effective-grants inventory. Migrate
only GitHub follow-through, dispatcher, WorkBoard and routine prerequisite checks
to this exact scoped boundary; other adapters retain their existing policies.

## Consequences and verification

The implementation extends existing connection, approval, job, vault, audit and
protected HTTP seams. Future mail/calendar/browser designs may reuse the
credential-versus-consent distinction but are not accepted by this decision.

Acceptance requires a real authenticated default principal issuing finite consent,
independent metadata readback, actual tested repair, fresh publication approval,
bounded local Git and protected intercepted REST publication/readback. Missing,
legacy, expired, revoked, wrong-root/repository/revision/action and before-contact
revocation races must send zero bytes. Restart/unknown reconciliation must prove
no duplicate or replay and no new-root adoption. Independent cumulative review
is required before claiming the capability complete.
