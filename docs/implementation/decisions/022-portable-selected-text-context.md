---
title: "ADR-022: Portable selected-text context"
---

# ADR-022: Portable selected-text context

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#926](https://github.com/seraph-quest/seraph/issues/926), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

**Extends:** [ADR-008](./008-portable-core-and-consented-context.md) with one
optional selected ordinary browser-text adapter. Host portability and other
capture adapters retain their separate readiness and proof requirements.

## Acceptance provenance

The lead accepted the bounded candidate and explicit pairing, artifact, replay,
lock and task-locator decisions on 2026-10-04 after independent review. Candidate
SHA256: `cbbceff6e6c7cb621b09a86a38b12ecc538dafea49ffa379255c6f6165933fe3`.
Design review PASS: `471cef442291f9eb742d3c8f3a6c860d7b007935fe4c0c569c02b203e1e4d19f`.
Task-locator review PASS:
`d0646a477ed6f57c1673a8989f59179a1c615ac58a541ff7920a02e75e65b1b2`.
The lead's corrections replace the candidate's executable-input artifact reuse
and unresolved Root discovery. This acceptance defines a target; it is not
implementation approval, macOS proof or Shipped truth.

## Context

The operator needs to attach an explicitly selected documentation paragraph to
an owned task without granting screenshot observation, model analysis or memory
authority. Existing paired bearer authentication does not sign request bytes;
general node upload creates a ScreenObservation and has a different purpose.
Executable WorkBoardInputArtifact rows must not become private attachments.

## Decision

One optional Chromium Manifest V3 companion deliberately captures ordinary
top-frame DOM text after a real action, context-menu or command gesture. It
offers offline local preview, deletion-only redaction and explicit send.
`work.context.selected_text.v1`, native kind `selected_context_v1`, accepts only
this closed adapter profile. It never reads clipboard, screenshots, arbitrary
uploads, background observations, model output or native region captures.
Missing or unsupported adapters are visibly blocked; the macOS/Linux peer core
remains usable without Chrome, a capture service, GPU or local model.

### Browser permission and privacy boundary

Use only activeTab, scripting, contextMenus and trusted session storage, plus
the explicit approved core API origin. No all_urls, persistent content scripts,
allFrames, broad tabs/webRequest, native messaging or background reading.
Transport code fixes the exact core origin and selected-context API paths;
Chrome host permissions are origin grants, not path-restricted authority.

An isolated fixed script checks the actual single noncollapsed top-frame range
and document identity. Reject editable/password/form/autocomplete-sensitive,
shadow/custom-element, hidden/inert and declared sensitive surfaces; unsupported
frames, PDF/internal/file/data/blob/incognito and navigation changes fail closed.
Require an explicitly reviewed ordinary source origin, retain no query/fragment
or credentials, and bound traversal to 4096 nodes and selected UTF8 to 32 KiB.
The site controls its DOM: these checks cannot guarantee ordinary prose has no
secrets or prove source authorship. Local operator review/redaction is required.
Preview is inert text, in trusted extension memory/session context for at most
five minutes. No durable raw draft, page messaging secrets, automatic retry or
network contact before an explicit selected-context action.

### Current original authority and signed two-stage send

The authenticated Task inspector publishes a focused permission projection in
the existing pairing entry, binding the exact original owner/Root, active Task
and Goal revisions, current pair generation and expiry capped at 120 seconds
and their authority deadlines. Old principal-only pairings grant no such
permission. Companion target discovery uses this current metadata; a supplied
Root is never authoritative and pairing does not grant execution or egress.

After preview, a domain-separated HMAC-SHA256 over canonical closed-schema
metadata binds device/pair generation, owner/Root, Task/Goal revisions, capture
UUID, source/document revision, adapter/build, exact UTF8 digest/size, expiry
and request UUID. Derive its key with the selected-context key domain; distinct
metadata/check/read/upload domains prevent reuse on generic edge routes.
MAC possession proves the paired credential, not human consent or origin truth.
Reject unknown/duplicate fields, ambiguous encodings and over 48 KiB envelopes.

Signed metadata without text admits one canonical paused native job. The live
original Root approves its exact source, digest and size through existing native
approval. Companion explicitly checks that ticket and sends only matching signed
bytes. No automatic polling/send, second pending queue or consent ledger.
Jobs are priority 60, runtime 15 seconds, max attempts 1, outstanding 1 per owner;
retain at most 2 MiB selected text per owner. Changed authority rejects rather than
retargeting or renewing the ticket.

### Canonical identity, locator and pure publication

Run identity derives from owner principal plus capture UUID in a fixed domain,
independent of mutable Root/Task/Goal/pair/request UUID. The same admission writer
does exact lookup, replay/conflict/tombstone validation and bounded quota checks.
Never delete/reset the native row: expired, revoked, discarded or malformed
captures cannot resurrect under a new request or Goal. New capture means a new
deliberate gesture and UUID.

Add nullable WorkflowRunState.source_task_id and a composite
`(job_kind, owner_principal_id, operator_session_id, source_task_id)` index through
an additive idempotent migration. Legacy rows stay NULL. This is metadata only;
it cannot grant authority or reuse candidate_id. Task discovery constrains exact
kind/owner/originalRoot/Task, with stable cursor pagination of at most 32 rows.
Every result must equal immutable authority before private access. Exact capture
lookup is independent of pagination and never scans JSON for identity.

Stage filesystem, crypto, MAC and owner Vault proof outside SQLite writers. Hold
the existing extensions-state shared nonblocking lock during pair staging and
short pure publication/read CAS. Every exclusive writer of the same lock must
also be nonblocking and return a typed visible busy result. Pure writers compare
current SQL Secret identity/binding/revoked_at, Root/Task/Goal/pair pins and staged
file digest; no filesystem, decryption, Vault or lock operation occurs inside.
Vault-before-JSON rotation must lose the SQL binding check. Pair changes never
restore old capture authority.

### Private D1 result, readback and discard

Publish a focused encrypted private file and canonical native metadata/receipts;
do not reuse executable inputs or replace Task.input_artifact_id. Text remains
untrusted D1 context: instruction_authority=false, analysis_eligible=false and
no_learning. No ScreenObservation, model call, automatic evidence ingestion or
memory update. Generic job artifact/input/evidence/model paths deny this producer.
Current-owner Task Inspector readback rechecks all current authority and immutable
locator/pair/Vault/file proofs before decrypting. Clear cached plaintext on denial
or owner/Task change. Discard first commits an irreversible native tombstone,
then performs bounded outside-writer credential/file cleanup. Failed cleanup is
visible and retryable locally without renewing read/send authority. Encrypted
audit remnants may remain; no physical erasure guarantee.

## Consequences

This adds one useful optional attachment journey, not a generic observation or
plugin framework. Mac/Linux core parity does not imply native Mac capture or
actual companion execution on both platforms. Offline stale drafts remain local
and require fresh explicit inspection or reselect/discard, never retargeting.

## Verification

Prove an actual loaded Linux Chrome extension gesture, offline preview/redaction,
managed authenticated Root/Task/Goal/pair binding, native admission/approval,
signed upload, encrypted physical readback and tombstone-first discard. Retain
actual SQLite/Vault/artifact bytes and source-attributed raw receipts. Prove MAC
tampering/replay, stale/foreign authority, pair/Vault-before-JSON races, expiry,
protected DOM, generic private-ingestion denial, cleanup failure, concurrent
admission and non-resurrection. Verify populated-database migration and indexed
query plan. Record unavailable macOS runtime proof honestly. No live account,
provider, model, GPU or Mac device is a prerequisite for local acceptance.

Official Chrome activeTab, scripting, contextMenus, match-pattern and storage
documentation and the W3C Selection API were checked on 2026-10-04 in the reviewed
design. Recheck unstable API claims when implementing their actual adapter.
