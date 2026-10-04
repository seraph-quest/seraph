---
title: "ADR-016: Exact Gmail Reply Send And Readonly Reconciliation"
---

# ADR-016: Exact Gmail Reply Send And Readonly Reconciliation

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#921](https://github.com/seraph-quest/seraph/issues/921), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

## Context

Existing Mail provides readonly source controls and encrypted private reply
drafts. A displayed thread fallback, a draft, stored OAuth credentials or a
provider POST ID cannot establish exact approval, sender identity or delivery.
Extend existing Mail, canonical approvals and native jobs; preserve legacy
readonly behavior, portable CPU core and generic current Goal/evidence guards.

The lead accepted design V3 on 2026-10-04 after independent Luna MAX review.
Design SHA-256: `4ca802708b314c47ba65e54bc295dab1c19da3d0064e224753d09feb613a90ad`.
Review SHA-256: `4f96bd43e229a6f8bd8c08512864312a191cbe96987d37d931226dd14908d036`.
Accepted findings require present exact scopes on every new-profile refresh,
original-live-Root observation-only recovery with a separate current Goal,
one canonical send checkpoint/effect with atomic approval, and mandatory exact
outbound JSON thread binding. This is accepted target architecture, not an
implementation, live Google-account or Shipped claim.

## Decision

## One operator journey

Existing Mail settings explicitly import reply-specific read and separate send credentials with reviewed identity scopes and finite connection-verification contacts. Existing readonly connections remain usable for their existing operations but not reply-ready. Existing authenticated private draft -> fresh exact preview -> finite send consent/approval -> distinct native send action bound to the completed preparation task/draft -> one POST -> independent Sent readback. No model call is needed to send or recover.

Preview shows verified mailbox identity, one exact To, full original Subject, original source/thread and observed time/revision, References/In-Reply-To, exact normalized plain body, no attachments, finite expiry/contact limits. Server derives source facts from strict fresh provider data, never submitted recipient strings or model output. Incoming Reply-To is untrusted data shown and explicitly approved. Any draft/body/recipient/header/source/thread/connection/account/consent/Root/Goal revision drift invalidates review rather than editing approved bytes. Send is a distinct canonical action/job referencing the exact completed draft; historical draft effects remain unchanged.

Normal successful execution uses current original native Root/Goal/lease and marks success only after strict independent readback. Outcome text: **Verified in sender's Sent mailbox**. It proves neither recipient inbox delivery nor absence of a later bounce. Lost send response/contact uncertainty -> **Unknown; no resend**. UI deliberately offers finite **Reconcile original send** while original Root is still live; it shows original Unknown and a separate verified/unknown observation. Root expired/replaced -> **Unknown; recovery blocked because original session is unavailable**, zero contacts. Security revoke has a distinct **Recovery blocked: authority revoked** reason. No undo after contact, bulk reply or auto reply.

### Exact observed scope and account policy

Reply read canonical scope set: `{https://www.googleapis.com/auth/gmail.readonly, openid, email}`. Reply send: `{https://www.googleapis.com/auth/gmail.send, openid, email}`. Each refresh used by either new profile MUST return a present nonempty syntactically valid OAuth `scope` string. Parse bounded whitespace-delimited tokens, reject controls/malformed values/duplicates/extra/missing scopes. Allow only documented `email` and `https://www.googleapis.com/auth/userinfo.email` as alternative representations of the single email permission, mapping either to canonical `email`; reject both in one response as duplicate permission. No arbitrary shortening, URL rewriting, profile alias, scope prefix acceptance or broader mailbox permissions. Source documentation lists both representations for primary email access; this is a strict local allowlist policy, not a guarantee of provider refresh output.

Canonicalize and require exact equality to the selected explicit declaration on EVERY phase refresh. Omitted/null/nonstring/malformed/empty/extra evidence -> blocked BEFORE UserInfo or Gmail. Store the actual observed scope evidence/digest with the token-phase identity observation; no local declaration, cached old observation or previous token identity can fill missing evidence. Never reuse an unverified token across phases. Google may omit scope: that account remains blocked for this reply profile, without weakening this policy. Legacy readonly omitted-scope compatibility and exact existing one-scope checks are unchanged, not retroactively treated as reply evidence.

Each fixed `https://openidconnect.googleapis.com/v1/userinfo` GET uses the SAME newly scope-verified access token used for that credential's subsequent APIs. Require bounded case-sensitive provider `sub`, verified email, and known pinned Google issuer provenance (UserInfo need not return an `iss` claim). Pair both credential subjects; compare exact case-sensitive `sub` and fixed issuer. Readonly Gmail `getProfile` proves actual current mailbox, which must exactly match reviewed verified mailbox values. No case/dot/plus/alias address normalization, send-as alias, labels, login hints or caller-imported ID-token trust. Missing claims/mismatch/swap/mailbox drift blocks before send. Recheck identity/current profile before final source reads and canonical send claim. All identity requests are finite consented contacts, not implicit network on metadata GETs.

### Finite payload/contact limits

One single-recipient plain reply: no Cc/Bcc/reply-all, forward/new recipient, aliases, batch, attachments, HTML, provider draft, auto reply, new bot/OAuth/framework. Source message + bounded original thread: `<=10` messages and `<=256` KiB per response; over-limit/incomplete provider source blocks, never truncated approval. Full Subject `<=200` UTF-8 bytes, References `<=20` IDs/2 KiB, original Message-ID mandatory. Body `<=4,000` characters AND `<=8` KiB UTF-8. Final MIME `<=16` KiB. Strict one-To, expected From, single Subject/Message-ID/In-Reply-To/References; reject duplicated authority headers, controls/injection/ambiguous folding, unsupported encoding or extra MIME parts. Freeze UTF-8 text/plain, deterministic CRLF without trimming visible text, one explicit submitted base64 transfer encoding; independently decode only strictly supported readback MIME encoding/charset and compare exact canonical text. Validate actual POST bytes immediately before contact and independently parse raw readback; existing display parser is insufficient. The sole messages.send POST JSON resource MUST contain exactly the frozen MIME raw as base64url AND threadId equal byte-for-byte to the approved original provider thread ID. The actual JSON resource, MIME bytes and exact original threadId are jointly bound to the approval/request digest and revalidated immediately before contact. Missing, changed or mismatched threadId blocks BEFORE POST. Independently decoded raw readback and the returned/read Gmail JSON threadId must match that same approved original ID; a post-contact mismatch is Unknown and NEVER authorizes resend.

Preview/source review and exact approval `<=5` minutes, capped by all original finite authority. Identity checks precede final bounded provider source GETs outside writer; last source observation `<=10` seconds before intent/contact. Provider change after observation remains an explicit unavoidable read/send race; it never allows changing the frozen effect. Native deadline `<=120` seconds and original grants; max attempts=1, one outstanding original effect, no automatic redirects/retries; each HTTP `<=10` seconds.

Contact budgets count refreshes as contacts: connection verification `<=2` UserInfo+1 profile+2 refresh =5; preview `<=2` source reads+1 readonly UserInfo+1 profile+1 refresh =5; execution `<=2` source reads+2 UserInfo+1 profile+1 POST+1 exact ID get+2 refresh =9. Recovery `<=1` readonly refresh+1 readonly UserInfo+1 profile+1 list page+5 exact gets =9, auxiliary deadline `<=120` seconds. No send token refresh/contact during recovery. Every contact checks the current admitted finite grant/Root/Goal/lease and remaining count/deadline; no reset through retry or restart. At most one list page and five candidates; any nextPageToken/cap/truncation is Unknown.

### One canonical checkpoint and pure writer

Stage Vault decrypt, exact private encrypted artifact/MIME verification, identity/source/provider snapshots and native evidence-dependency filesystem work OUTSIDE the writer. The owning `WorkflowRunState` checkpoint contains versioned immutable intent metadata: original job/native task/attempt/fence/Root/Goal revisions, approval/binding/consent digests, connection revisions/account proof digest, send request digest, encrypted artifact reference and MIME/content/header digests, one persisted RFC Message-ID, one immutable send-effect ID. Private addresses/body/provider IDs are encrypted artifacts/owner-private fields, never ordinary public checkpoint text. A versioned checkpoint projection and same-row effect/readback receipts are one canonical authority, not two independently mutable dispatch claims. Generic audit/effect projections derive from the checkpoint.

Add a narrowly typed repository primitive operating on caller-owned `AsyncSession`/SQLite immediate transaction. It rechecks authenticated live original Root, original active Goal/revision/native attempt/lease/fence/evidence bindings, exact pending approved effect and finite consent/current connections, staged observations' digests/revisions/freshness and expected run revision; then atomically consumes exact approval and installs one-use intent in SAME writer/CAS. Do not invoke public `consume_approved()` or `record_effect()` that open their own sessions inside this writer. Reuse exact approval validation/attachment/sealing rules through a pure in-session consume seam. Evidence staging follows #917 stage-outside/recheck-inside; no generic guard relaxation.

Intent phase `claimed` -> one atomic dispatch-claim phase `contact_may_have_occurred` under current original Root/Goal/lease and row CAS immediately before the sole POST. This flag is never cleared. Revalidate already staged final MIME outside writer; current DB authority and contact budget must still pass. A crash after claim but before HTTP conservatively becomes Unknown. Every restart/duplicate/native retry sees the same phase and zero further POSTs. Stable RFC Message-ID is correlation, NOT provider idempotency. A POST ID is not success; one independent strict SENT/thread/Message-ID/From/To/Subject/InReplyTo/Refs/body readback settles ordinary live-native success. All native deadlines and finite writes remain one-use.

Actual native cancel before dispatch can settle cancelled only with no possible write contact and transport quiescence; post-dispatch cancel/timeout/response loss/worker loss is Unknown, no undo claim. Transport lifecycle active/unsettled operation proof is required, not guessed from a cancelled await. Stale/wrong-fence/duplicate approval/intent/request digest conflicts fail closed. No writer contacts filesystem/Vault/network or nests another writer.

### Readonly recovery: exact authority and allowed mutation

FIRST VERSION requires currently authenticated original Root/session ID and EXACT same original owner_principal_id. Check original Root current bearer binding, idle/absolute expiry, tombstone/revocation and any operator identity security revoke before any refresh/UserInfo/Gmail. Ordinary Root expiry/replacement blocks with zero contacts even if a new login has the same typed address; security revoke separately blocks. In-place bearer rotation retaining the same original live Root/principal may authenticate using its current bearer, without extending original send deadlines. No new-Root adoption/account identity framework in this milestone.

The original send deadline may have expired; original Goal may be closed or changed. Recovery uses a separate CURRENT active RecoveryGoal owned by the same original principal/Root/session and an explicit newly reviewed finite READONLY grant bound to original immutable job/effect/MIME/send request/account/read connection. It admits its own native auxiliary job with fixed identity, owner, priority, `<=120`s deadline, max attempts=1, finite contact budget and original-effect/grant request-digest dedup. Repeated same request returns that auxiliary job; a deliberate later observation requires a new finite read grant, never a new POST or write renewal. Original unknown liability is shown before approval of reads.

Stage current scoped readonly token/identity/profile and one `in:sent rfc822msgid:<original-id>` list + bounded raw candidate get/strict matching outside writer, under auxiliary current Root/RecoveryGoal lease checks and contact marker. Zero/multiple/incomplete/mismatched results remain Unknown; empty search is not absence proof. Exactly one actual SENT/thread/header/body match yields a verified observation, not recipient delivery.

Add a fixed mail-specific pure-DB readback CAS, not generic `record_effect()` or `finalize_reconciled_job()`. In one immediate writer it verifies original workflow/job kind, immutable send-effect ID, checkpoint intent/MIME/request/account/source digests, historical original Root/Goal/approval/attempt/fence bindings unchanged, original row expected revision, all original liabilities/contact marker and transport quiescence/no lease, plus CURRENT original Root/principal and current RecoveryGoal/grant/readonly connection+scope/identity revisions and auxiliary native lease/fence/evidence/budget. Original Goal must still exist with original owner binding; it need not be currently active or at historical revision. No resurrection of deleted/reassigned provenance. Original liability must be unresolved and non-running; active original transport or write lease blocks recovery.

Allowed original-row mutation ONLY: append a bounded versioned observation/readback-history entry attached to EXACT original immutable send-effect ID (readback observation identity/digest, auxiliary job/grant references, observed SENT outcome/time, private artifact ref), bump row CAS revision/update time, retain bounded prior history. Preserve original checkpoint immutable intent/dispatch flag, original effect write status, original job status, lease/deadline/Goal/approval/authority/fence and costs/liabilities EXACTLY. Do not change original terminal status to succeeded, settle costs, restore any write/evidence fence, renew expiry, rebind Goal/Root, or assert old Goal is current. This history is observation only and cannot become dispatch authority.

Complete ONLY the auxiliary recovery job under its own current Goal/lease with result **verified_sent_observation** or **unknown_observation** and explicit no_learning. Original job stays Unknown/its existing terminal status; cockpit projects original uncertainty plus independently verified observation, with the distinction visible. No competing send ledger: auxiliary job owns read execution only, original send checkpoint remains sole dispatch authority. The specialized writer must atomically append the original observation AND finalize the auxiliary current-Goal-fenced result, honoring both exact row revisions. Extract only the narrowly needed in-session auxiliary completion validation/CAS, preserving generic current Goal/evidence guards. If this composition cannot be implemented safely, stop for lead disposition rather than adopting a second independently committed settlement scheme. No stale observation may overwrite a later canonical history; crash/duplicate after this commit reads the same auxiliary outcome without further contacts.

Canonical identity-continuity seams do exist: `auth/service.py:210` accepts continuity/recovery input, `auth/ownership.py:127,144` uses `OperatorIdentity` and login binding; in-place `create_session` refresh retains Root. This is NOT sufficient authority to extend this version to a new Root: normal new sessions mint new principal/Root, and ownership migration has its own proof contract. Reported for lead only; no expansion or typed-address continuity inference.

## Consequences

- One exact single-recipient plain reply extends existing Mail controls; there
  is no second outbox/intent ledger, arbitrary HTTP, automatic reply, model call
  or learning. Send and observation are separate bounded actions.
- Existing readonly connections keep their existing scope compatibility and
  readiness. They do not become reply-ready by inference or cached identity.
- macOS and Linux remain peer CPU core-host targets. Missing optional Gmail
  authority blocks this adapter, not the core.
- Sender-Sent evidence is not recipient delivery, and RFC Message-ID is not
  provider idempotency. Search indexing delay, source-read/send races and
  contacted Unknown liabilities remain explicit.

## Verification

| Issue AC / accepted finding | Required actual proof |
| --- | --- |
| Separately consented least-privilege exact reply | Real auth+SQLite+Vault/draft/private MIME+canonical approval+native job; all explicit preview fields, one recipient and no attachments; legacy connection still reads but cannot reply; sender identity scope consent visible |
| Present observed scopes every refresh | For BOTH new profiles: omitted/null/malformed/empty/extra/duplicate/unknown alias scope -> zero UserInfo/Gmail; stale prior verified scope never fills omitted evidence; documented email alias representation accepted once, no profile/full-mail/modify/compose; unchanged legacy scope behavior |
| Token-bound identity/freshness | Same-token exact subject/pinned issuer/profile; mismatched sub/swapped credentials/verified-email drift/revocation -> zero POST; provider-current source changed before writer invalidates; stale Root/Goal/attempt/fence/approval/MIME/thread all fail immediate writer/contact |
| One canonical intent/approval | Concurrent genuine writers approve/consume/install/dispatch once; all-or-nothing rollback between approval and checkpoint CAS; duplicate same digest returns original; changed digest conflicts; no competing table/claim; actual artifact bytes retained, no fabricated receipt |
| One POST + strict readback | Native actual runtime sends inspected JSON resource containing retained approved raw MIME AND exact original threadId; missing/changed ID yields zero POST; strict To/From/Subject/thread/RFC/charset/encoding/body, duplicates/injection/truncation false-success negatives; SENT/providerID/body comparison automatic; no seeded receipt |
| Crash/duplicate/cancel/restart | Crash after intent/dispatch claim/provider contact/response loss -> same row/effect/MIME and zero additional POST; original contact liability/quiescence retained; native before-contact cancel vs after-contact Unknown; actual persisted restart |
| Recovery old deadline/Goal | SAME live original Root/principal, expired send deadline and changed/closed original Goal, separate active RecoveryGoal/grant+auxjob; actual readonly list/get; zero send refresh/POST; original status/deadline/Goal/authority/dispatch flag unchanged; only bound observation history appended and auxiliary result shown |
| Recovery authorization negative | Expired/replaced original Root, explicit security revoke, wrong/different principal, reassigned/deleted old Goal provenance, expired/changed RecoveryGoal/grant/readonly conn, active original lease/unsettled contact -> zero provider contacts; no typed-email/new-login continuity |
| Recovery result bounds | One page, `<=5` candidates, exact one match; empty/multiple/nextPageToken/mismatch/timeout -> Unknown/no resend; concurrent/restarted auxjob uses same original-effect observation identity and spent count; old liabilities not falsely settled |
| Operator/runtime/privacy | Managed isolated CPU/no accounts/nonlocal denial + synthetic Google only; real browser auth/cookies remain browser; draft->preview->canonical approval->native send/readback, Unknown->new finite read grant->original+auxiliary displayed state and restart; private artifact bytes/log inventories; explicit no_learning; no provider contact on ordinary metadata |
| Gate/regressions | Focused changed mail/approval/native canonical negative groups `<=120`s under lead's heavy-slot coordination; type/component checks, actual managed journey; honest command/exits/source/fixture hashes; independent whole cumulative critic before ready epic PR |

Original issue AC remains whole: exact approval, independently read-back sender-SENT state, finite cancel/revoke/Unknown recovery, owner/fence/idempotency/restart, synthetic actual canonical persistence/UI. New-Root recovery is explicitly excluded and visibly blocked, not implied to work. Live account/mail behavior is separately approved and unverified; Gmail search indexing delay and source read/send race remain residual risks.

## Primary sources

Checked 2026-10-04: [Gmail threading requirements](https://developers.google.com/workspace/gmail/api/guides/threads),
[Gmail send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send),
[Google scope reference](https://developers.google.com/identity/protocols/oauth2/scopes),
[OpenID Connect guide](https://developers.google.com/identity/openid-connect/openid-connect)
and [UserInfo reference](https://developers.google.com/identity/openid-connect/reference).
The local strict observed-scope policy does not claim that Google always returns
scope evidence; omission blocks the new reply profiles.
