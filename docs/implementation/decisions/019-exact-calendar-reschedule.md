---
title: "ADR-019: Exact owned-calendar reschedule"
---

# ADR-019: Exact owned-calendar reschedule

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#922](https://github.com/seraph-quest/seraph/issues/922), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

## Acceptance provenance

The lead accepted the narrow v2 design on 2026-10-04 after independent review.
Design SHA256: `b12c3f8f3315dccc11abdfb9de374c5dad63d83ec47f8a84150f31f91e35aed5`.
Correction review PASS SHA256:
`eb9f1b69d2d4629a47a46927dec069b5ca41bb10f1ea7680d89b79d3f7a30a4a`.
Lead acceptance SHA256:
`fa43436fab0cf55bac2165169c8bf2dc0cd41cbc2882e77f5e33d6c83a12d2da`.
The prior P1 scope finding was accepted and fixed. Acceptance defines the target;
it does not establish implementation, live Google operations or Shipped truth.

## Context

Existing Calendar preparation is readonly. One reviewed reschedule must not
overwrite a concurrent edit, duplicate an ambiguous mutation, confuse calendar
manager access with data ownership, or renew historical authority during recovery.
Reuse Calendar connections/bindings, canonical native jobs/approvals/artifacts
and the narrow original-Root observation pattern; Gmail semantics do not become
Calendar authority.

## Decision

One literal, model-free reschedule of one selected existing owned nonrecurring
timed default event. No attendees, conference, attachments, special/unknown
event fields, labels, all-day events, create, batch, invitation, automatic undo
or learning. `calendar.event.reschedule.v1` and a separate readonly
`calendar.event.observe-reschedule.v1` use the existing native job repository.
The selected calendar/event IDs, original event ETag/full protected state,
account/connection/consent/task/input/Goal revisions and original authenticated
Root are immutable approved bindings. Existing readonly Calendar paths remain.

### Exact identity and scopes

Two new profiles require exact actual scope evidence on every refresh:

- read: `https://www.googleapis.com/auth/calendar.events.owned.readonly`,
  `https://www.googleapis.com/auth/calendar.calendarlist.readonly`, `openid`, `email`;
- write: `https://www.googleapis.com/auth/calendar.events.owned`,
  `https://www.googleapis.com/auth/calendar.calendarlist.readonly`, `openid`, `email`.

The documented email permission URL may represent `email` once; duplicate aliases,
missing/malformed/extra scopes block before UserInfo/Calendar. No broad
`calendar.readonly`, shared-event or metadata fallback. Same-token pinned UserInfo
requires stable case-sensitive sub, verified email and known issuer provenance;
pair credentials by subject/issuer, with exact email as additional drift check.
No cached scope/identity substitutes for current token evidence.

Selected `calendarList.get` supplies actual ID, current accessRole, primary,
secondary dataOwner and IANA timezone; no `calendars.get`. Require owner role
AND primary ID equal verified email or secondary dataOwner equal verified email.
Owner role alone is manager access. No email alias normalization. Event creator
self/current verified email and organizer self/selected calendar ID must agree.
Missing/ambiguous facts block. Disclose calendar-list metadata visibility
separately from owned-event access; runtime contacts remain selected-ID-only.

### Exact patch and readback

Preview old/new RFC3339 seconds/numeric offsets, explicit IANA zones and UTC
instants. Reject gaps, offset/zone mismatches, ambiguity without an explicit
offset, invalid/end-before-start/no-op times. Duration1 minute–24 hours, future
start within365 days; last fresh owner/event observations <=10 seconds at claim
and contact. Frozen full accepted resource rejects unknown fields and preserves
protected title/description/location/reminders/identity/property/exclusion state.

One private correlation key `seraphReschedule_` plus24 hex characters (41 ASCII
characters) and random256-bit value is generated once and approved. Existing
private/shared properties stay protected; source collision/over-bound maps block.
PATCH adds only that property and exact start/end, using original ETag in
If-Match, sendUpdates=none, conferenceDataVersion=0, supportsAttachments=false.
No deprecated notification flag. UI qualifies provider reminders/notification
behavior; no universal no-email guarantee. Marker is correlation, not provider
idempotency or cryptographic authorship.

One PATCH, no automatic retry/rebase on412 or response loss. Definite412 is
Conflict with proof of refusal; ambiguous contact is Unknown. An independent
readonly exact-ID GET must verify different ETag, exact new instants/timezones,
marker and unchanged full protected projection. PATCH response alone is not
success. Server updated/sequence/ETag/display URL may change; no silent content
normalization. Input/source/event JSON are bounded and duplicate/nonfinite
JSON keys/values are rejected; source is never truncated for approval.

### Native authority and finite work

One task/input and canonical WorkflowRunState checkpoint own each original
effect. Immutable versioned native intent includes original Root/Goal/attempt/
fence/approval/input/account/source/request/artifact digests and fixed effect.
Stage Vault/private bytes/identity/HTTP/evidence outside every SQL writer.
Current live Root, Goal/task/input/revisions, consent/connections, lease/fence,
staged freshness and exact empty-attachment approval are checked in a short
SQLite writer; approval consumption and intent install are atomic. A second
dispatch/contact CAS permanently spends the sole PATCH slot and invalidates
prior closure before contact. No filesystem/Vault/network/nested writer there.

Priority60, attempts1, outstanding1, original runtime<=120 seconds, approval
<=5 minutes capped by all original finite authority. HTTPS<=10 seconds, zero
redirects/retries. Persistent contacts: verification6 (two refresh/UserInfo plus
two selected lists), preview4, execution9, original combined13, recovery4.
No pagination/search. Replay keeps original request/marker/deadline/budget.
Cancel before dispatch needs actual owned transport closure and proof no PATCH
may exist. Postdispatch cancel/timeout/crash stays Unknown with original effect
and liability. Versioned closure binds actual adapters' zero active/unsettled
operations and equal started/settled counts to intent, fence and full history;
empty registry/expired lease/restart does not prove closure.

### Readonly recovery and privacy

Same still-live original Root/principal only, separate current active
RecoveryGoal/revision and explicit new finite readonly grant/auxiliary job.
Expired/replaced/security-revoked Root blocks all contacts; no new-login/email
continuity inference. Original Goal may be changed/closed but must retain its
original owner provenance. Original write deadline may be expired. Require
original nonrunning/no lease and positively bound owned transport closure.
Recovery refreshes only readonly credential and performs selected list/event
GET. Missing marker/coincident times/deleted or later-edited event cannot prove
absence or authorize replay. Current exact state can yield verified observation.

One specialized pure writer appends bounded observation history to the original
effect and completes current-fenced auxiliary atomically, checking both revisions.
Preserve original status, immutable checkpoint/dispatch, deadline, Goal,
authority/approval, effect write status, lease/fence and liabilities exactly.
History is observation, never dispatch authority or original success renewal.
No automatic reverse patch; any compensation needs a new current preview/ETag/
approval. Private encrypted artifacts use held nofollow publication and0700/
0600 modes; plaintext access and availability share current Root/task/Goal/
consent/connection checks. Revocation hides bytes and cached UI content, with
metadata/recovery retained. No inference/canonical memory write; explicit no_learning.

## Consequences

### Accepted finite write permission schema supplement

Lead acceptance of the independently reviewed supplement r2
(`826fb2d47acdd1f857984d671b864b450581b3762bed627d71b86e01839a0090`)
permits one focused `CalendarRescheduleConsent` table and its bounded indexes.
It binds server-derived owner/original Root, exact Goal/revision, read/write
profile revisions and scope/identity/Vault metadata digests, encrypted selected
calendar and event binding revisions, finite capped expiry, request/digest and
active/revoked revision. It stores permission only; the native job checkpoint
continues to own one-use intent, dispatch and original effect liability.
No legacy readonly consent is promoted or reinterpreted and there is no backfill.

One active grant per original owner/Root/event binding includes naturally
expired active rows. Expiry blocks authority without changing or renewing the
row. Explicit local revocation by the still-live original owner/Root releases
the slot even when the old Goal/grant has closed or expired; it needs no provider
contact or Vault secret read. Fresh regrant requires a new explicit UUID,
current finite active Goal, verified exact pair and freshly confirmed
acknowledgements. It cannot renew or adopt previous jobs or approvals.

CPU core and existing Calendar/Mail paths remain usable without these grants.
Strict ownership/schema/scope/closure policy can block ambiguous accounts or
old receipts. Current-state observation cannot prove historical authorship or
absence of provider notifications; owner/ACL read/write races remain bounded
and explicit. Live Google usefulness requires separately authorized proof.

## Verification

Actual auth/SQLite/Vault/native approval/jobs/artifacts/UI with stateful intercepted
Google HTTP only. Prove exact one patch/full readback, concurrent412/no retry,
scope/account/ownership/DST/unknown-field/protected-state negatives, concurrent
atomic claim rollback, stale Root/Goal/consent/lease/fence/input bytes, duplicate/
restart/cancel/response loss and actual closure. Prove readonly auxiliary with
expired original deadline/changed Goal preserves liabilities and both row CAS;
wrong Root/provenance/closure yields zero contacts. Managed production-gated
browser success/Conflict/Unknown/recovery/current-private-read states and fresh
independent whole review are required before completion. No real invitations.

Current official sources checked2026-10-04: [events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get),
[events.patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch),
[calendarList.get](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/get),
[CalendarList](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList),
[resource versions](https://developers.google.com/workspace/calendar/api/guides/version-resources),
[extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties),
[scope guide](https://developers.google.com/workspace/calendar/api/auth) and
[OpenID identity](https://developers.google.com/identity/openid-connect/openid-connect).
