---
title: "ADR-018: Fixed Moltbook Private Browser Read"
---

# ADR-018: Fixed Moltbook Private Browser Read

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#918](https://github.com/seraph-quest/seraph/issues/918), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

## Context

One selected Moltbook read extends the existing connection, finite authority,
durable native job and isolated Chromium boundaries. The lead accepted the
frozen design on 2026-10-04 after independent review. Design SHA256:
`4073043cb1630a5473ae80cf73d636f5e0a4ec03dedce11f7bdefb30fb68c86d`.
Review SHA256:
`a539cc7bb7f958a00b23834e57c026ae66b0ce97d349069ae745346eab3fad4d`.
Lead acceptance SHA256:
`75366d4fb53189ba928a5d34932d0a6ab2dc26dc64926dcd34fcc2e8945db388`.
[Owning acceptance](https://github.com/seraph-quest/seraph/issues/918#issuecomment-5975555992)
keeps production Home blocked pending separately authorized effect, identity
and account/site acceptance evidence. This is Target architecture, not Shipped
truth, a real-account acceptance or a claim of side-effect-free GETs.

## Decision

## Selected operator journey and effect decision

The operator reads their connected agent's own account summary and activity on
their own posts, in one authenticated Chromium document. The chosen document is
`https://www.moltbook.com/api/v1/home`; two bounded identity checks use exact
`GET https://www.moltbook.com/api/v1/agents/me` before and after that document.
The browser's navigation causes the Home request through the guarded transport;
it is not a separate API read followed by a decorative browser.

Official Home documentation also says due role briefings are returned once per
role cadence. Therefore this is **not a side-effect-free read**. Home access may
consume a due briefing delivery and produce provider access/presence bookkeeping.
The exact provider cursor mechanism is not verified. This design treats delivery
of an already assigned briefing as incidental servicing of this explicitly
requested dashboard read, not permission to execute a role or change business
objects. It requires its own initially unchecked acknowledgment, stating that a
due briefing may be delivered/consumed even though its instructions will not be
executed or included in the result. There is no automatic check-in, heartbeat,
schedule, role creation/invocation, notification disposition change, or action
follow-through. POSTS, comments, votes, follows, DMs, role configuration, and
notification mark-read remain prohibited business mutations.

This distinction is the accepted bounded architecture decision, not a claim
that GET implies no mutation. If evidence establishes that Home invokes a role
or mutates other business state, or separately authorized evidence does not support delivery bookkeeping as
incidental, this profile must remain blocked. The concrete fallback is a single
authenticated Chromium `GET /api/v1/agents/me` account document with the same
authority, isolation, citation, and cleanup contract; not a generic browser.
Production Home acceptance additionally requires confirmation of this effect
classification under separately authorized account testing. No production test
or permission to use the existing #940 registration key is included here.

## Current primary evidence and limits of the premise

Checked 2026-10-04:

* [Official skill/API documentation](https://www.moltbook.com/skill.md): retained
  official source SHA256
  `10f3c03a17cf42c79642e73e2d3bf3255743f22de5d6db215ac6c6b3cd1b28c9`;
  lines 138–144/543 authenticate `/agents/me`, 744–748 describe cadence-gated
  Home briefings, 856–930 describe Home and own-post activity, 931–944 give
  separate POST notification mutations. Remote instructions are untrusted and
  were not executed. The web reader could not parse text/markdown; this proposal
  uses the lead-retained official fetch, not a claimed successful second fetch.
* [Owner login](https://www.moltbook.com/login): current public page confirms
  email magic links and email/X claim steps. It does not establish server-side
  owner-session cookies, refresh semantics, or human ownership of a name.
* [Developer identity documentation](https://www.moltbook.com/developers):
  temporary identity-token issuance/verification is a separate POST protocol,
  not refresh authority for this bearer-key read profile; it is excluded.

Private research dependencies: `first-site-research-r1.md` SHA256
`ad42d3dac0134cd825ee424b5eb3c6c1b02dc76e16dd44b3dcd63ce7c721b9ab`,
`first-site-api-home-candidate-r1.md` SHA256
`1eb655b6472dec871fcdc4c61b2152cfbd17db8fa9511465a08af8e454f92c17`,
and `issue-intake-r1.json` SHA256
`c4b6323efea650397887cb5ce2e0b7b640575b35416de6ceca83dcfc4a3fc3cb`.
All live authority/account acceptance remains unverified. A dashboard's
localStorage owner name or the agent's `is_claimed` flag is not human-owner proof.

## Smallest extension and ownership

Base: `ebab538de904bbd6947f3aeaaf260b8d5d44c050`, persistent branch
`feat/918-moltbook-private-read`. Preserve public browser and #940 behavior.

* Add one focused fixed-profile helper, proposed
  `backend/src/browser/moltbook_private_read.py`, behind the existing Moltbook
  API/control and durable native job surfaces. Typed capability
  `browser.moltbook-private-home.v1`, fixed job kind
  `moltbook_private_home_v1`; metadata registration must wire this executor.
  No disconnected WorkBoard advertisement or second queue framework.
* Narrow extensions: `backend/src/api/moltbook.py`,
  `backend/src/integrations/moltbook_controls.py`, and existing job-resource/
  executor dispatch seams for this one named kind. Reuse the existing
  `MoltbookConnection` row and reservation; no second authority ledger/table.
* Reuse actual launch, per-request guards, positive handle closure, and native
  receipt patterns from `backend/src/browser/task_runner.py`. Extract/reuse
  narrow common primitives only where semantics match. Public transport remains
  credential-free; do not weaken its forbidden Authorization/Cookie checks.
* Reuse pinned HTTPS/lifecycle primitives from
  `backend/src/security/http_transport.py`; add a fixed-site trusted mediator
  alongside `backend/src/browser/pinned_transport.py`, not caller-defined URLs.
* Extend `frontend/src/components/settings/MoltbookConnectionPanel.tsx` and
  its existing original-job Inspector with provision/consent/read/cancel/output
  controls. Add fixed typed input/output metadata to the actual native surface.
* Register the new artifact as private in existing artifact/evidence projection
  seams, including `backend/src/memory/evidence_working_set.py`. It is not model
  context, a public pipeline input, or an ADR014 execution precondition.
* Focused tests beside the corresponding browser, Moltbook, API, and component
  tests; ADR018/constitution link and owning Guide target paragraph. No unrelated docs reconciliation.

Code premises at this base: task_runner.py:56–74 public bounds;
1516–1635 actual guarded launch/extract/readback/cleanup/finalization;
2071–2098 fresh credential-free context; 2128+ per-request dependencies;
2630+ full authority mapping. pinned_transport.py:397 DNS-global checks;
537+ pinned guarded fetch; 718+ context-wide route interception.
http_transport.py:186/275/286/333/342 bounded pinned request and closure.
moltbook_controls.py:47/52 writer/current Root; 161 finite consent;
214 disable preserving reservation; 229 canonical authority; 315 admission;
489 execution; 574 artifact/settlement. api/moltbook.py:17 strict schemas,
120 consent, 131 reads, 137 job, 157 execute, 162 output, 167 cancel.
models.py:3011 existing connection; evidence_working_set.py:264–270/467+
private-source projection. Line references describe inspection, not implemented
private-browser functionality.

## Profile provisioning and finite authority

The profile is a server-owned logical descriptor, not an imported cookie jar or
an arbitrary persistent Chromium user-data directory. Provision only from an
existing current operator-owned Vault connection whose stable agent UUID was
positively inspected through the existing #940 path. No registration, real-key
read, owner-email setup, X login, claim, or identity-token POST occurs here.
Descriptor fields bind exact owner principal, original authenticated Root,
connection ID/revision, pinned agent UUID, encrypted Vault owner/key binding,
fixed site/profile policy version and digest. Agent name is display only.

The new consent is distinct from ordinary public Moltbook reads, initially
unchecked, and explicitly authorizes this private Home document plus its fixed
identity checks and delivery-bookkeeping acknowledgment. Strict literal booleans
reject `1` and strings. Maximum consent is 300 seconds, further capped by the
original live Root and existing controlling Goal/connection authority. Credential
possession grants nothing. Consent never silently renews on reload or key access.
API key non-expiry is not Seraph session authority. No known refresh route is
needed or permitted; expiry requires a new explicit consent on a current Root.

Input is a strict server-resolved profile identity, connection/Goal revisions,
priority and request UUID; no URL, header, method, JS, cookie, prompt, or source
text. Preparation and explicit execution are separate operator actions.
Admission keeps the current durable queue, one connection reservation and the
shared browser resource ownership. Default priority 50, budget zero, max attempt
one; deadline at most 120 seconds and never beyond the original consent/Root/
Goal authority. Cleanup reserves the existing bounded 10 seconds/20% within the
original deadline. Exact same-key/body replay returns the original job and
history, never a new deadline or contact. Any changed body rejects.

## Route mediation and credential isolation

Production origin is exactly `https://www.moltbook.com:443`; fixed paths are
`/api/v1/agents/me` and `/api/v1/home`, GET only, no query, fragments, pagination,
redirect, alternate host, refresh, or subresource route. The one Home document
navigation is the sole permitted browser request. Images, frames, fetch/XHR,
popups, downloads, WebSockets, service workers, and unrecognized resource types
are blocked. JS disabled; permissions, storage_state, HTTP credentials and
cookies absent. Every context request passes mediation; no direct `route.continue`.

Resolve every production address and reject any non-global/mixed/empty answer;
connect only a validated pinned IP with the exact logical Host and TLS SNI and
normal certificate verification. Check current authority before and after DNS,
before contact, after response and before fulfilling the browser request.
Reject redirects including same-origin redirects, Set-Cookie, login HTML,
unsupported content type/encoding, challenge pages, and policy/schema drift.
No cookie forwarding, header reflection, proxy env, or server-suggested endpoint
following. Ordinary server access logging is explicitly a contact side effect.

The mediator reads the exact staged Vault credential outside writers and injects
Authorization solely into its pinned server-side HTTPS request. It never sets a
browser header, page script, browser storage, DOM, artifact, prompt, diagnostic,
or retained request header. Reject an echoed credential before browser fulfill
or artifact storage. Raw response/private evidence handling is bounded and never
retains request secrets. Cleanup erases in-memory authority and closes the owned
context; logout means local revoke/close, not an invented provider logout POST.

## Readback, field citations, and finite data

Maximum three provider contacts: GET me, ONE Home navigation, GET me; all share
one original job deadline and immutable finite call inventory. Home response loss
is not retried, refreshed, or followed with another Home. Both me UUIDs must equal
the pinned UUID under the same credential binding. Home account-name agreement
is an additional consistency check, not identity authority; Home's documented
schema does not itself supply a stable UUID. This provider linkage remains a
production acceptance limitation until tested, not hidden by a name comparison.

Allow only `your_account` name, karma and unread count, and at most ten
`activity_on_your_posts` rows with post UUID, title, community, notification count,
latest timestamp, up to four commenter names and literal preview. Discard
announcements, follow-feed, DMs, role briefings/prompts, suggested_actions,
what_to_do_next, quick_links and endpoint strings; never execute their instructions.
If required fields or identity/schema fail, block; do not invent empty success.

Each response is at most 64 KiB; aggregate 192 KiB, document projection 64 KiB,
provenance metadata 16 KiB. Private parser rejects duplicate keys/non-finite values,
depth over eight, over 1,024 nodes, and overflow arrays; no positive truncation.
This is a narrow profile parser, not a global relaxation of #940's 256-node parser.
Names 128 bytes, title 512 bytes, preview 2 KiB, community 30 bytes, timestamp
32 bytes; counts nonnegative bounded integers; karma bounded signed integer.

Chromium must load the mediated original JSON bytes. Record response SHA256 and
the actual Chromium source DOM/text SHA256 separately. Extract from that real
document and require canonical JSON equality with the verified transport payload;
do not manufacture HTML or assume Chrome's JSON viewer representation. If its
actual DOM cannot be independently parsed under the fixed schema, fail closed
and report the seam before changing the plan. Every literal result field carries
its canonical JSON pointer, source-response digest, browser-source digest,
fetch time and value digest. The durable private artifact contains only this
allowlisted projection/proof, no cookies/credentials/role text/raw headers.
Any retained bounded raw source proof is private and secret-scanned; independent
physical readback proves artifact bytes/digest/schema before canonical promotion.
Result always states `no_learning`; no measured quality or production claim.

## Current authority, atomic publication, and recovery

At every handoff bind current canonical Root/token hash and expiry/revocation,
Goal owner/revision/state, exact job/fence/lease/cancel/input/body digest, profile
policy digest, connection revision/reservation, consent scope/original expiry,
and Vault owner/encrypted binding. Stage all files, Vault/decryption, native handle
observations and artifact proof outside SQLite writers. Use the existing lifecycle/
policy publication fence outside SQL where required; preserve lock order and
bounded acquisition. The final short BEGIN IMMEDIATE writer rechecks the exact
canonical rows and digests in the same session and atomically appends protected
artifact/readback/checkpoint/outcome plus exact reservation settlement or neither.
No authentication helper, nested DB session, filesystem, DNS, Vault, or network
work inside the writer. Root/Goal change at any boundary blocks promotion.

Before each potentially contacted request, persist its immutable one-use intent
and contact marker, method/path/bodyless identity, counter and original bounds.
Record observed responses separately from admitted possible contacts. In
particular the Home marker identifies possible delivery bookkeeping. Lost Home
response yields Unknown with no automatic retry, success, clear, or new attempt.
Historical job inspection is read-only and does not reacquire authority.

Success and capacity release require positive awaited HTTP response/client
closure, no pending transport work, and actual owned context/browser/Playwright
closure within original bounds, followed by current canonical CAS. Cancellation
before/during contact preserves the inventory and observed effects, blocks further
contact, and requires known bounded cleanup. Missing handle/closure proof after
parent death remains Unknown/reserved; absent processes or locks are not proof.
An unresolved possibly consumed briefing is not settled by an empty ledger or
404. Do not introduce a new generic capacity-clear route; the fixed existing
settlement/recovery seam may release only with positive exact effect/cleanup
proof, otherwise display the named blocked limitation. No Home replay for proof.

Disable/revoke increments the existing connection revision, removes usable
profile consent, cancels only owned work and closes the ephemeral context.
Retain capacity until positive cleanup/settlement. No persistent cookies remain;
encrypted key storage alone is not usable authority. Another Root/owner cannot
adopt, execute, refresh, or read an artifact without its current private permission.

## Acceptance/proof mapping

1. **Concrete site/useful output:** operator UI displays fixed Home scope and
   incidental delivery disclosure; prepare/execute → actual Chromium JSON document
   → field-cited private artifact → independent readback/no_learning. No generic
   URL controls or claim that a fixture is production Moltbook acceptance.
2. **Private profile/session:** actual authenticated local test server with two
   dummy keys and different stable UUIDs, real Vault/config/consent/Root/Goal/native
   rows; prove cross-owner/name-collision denial, no credential in browser state/
   DOM/artifacts, expiry, rotation, revoke/logout and no usable leftover authority.
3. **Every request/effects:** real Chromium context interception and real local
   TCP server, with actual request counts. Server reproduces Home due-briefing
   delivery bookkeeping; exactly one delivery despite lost response and reload.
   Prove redirects/subrequests/business methods, private/mixed DNS, HTML,
   Set-Cookie, echoed key, schema/byte/node/array overflow fail closed. Local
   server mapping is constructor-injected test-only transport, not a production
   loopback allowlist, caller URL, or HTTP-visible bypass. Production uses pinned
   HTTPS only. HTTPMockTransport or synthetic browser receipt is insufficient.
4. **Lifecycle/recovery:** actual cancellation before launch/in DNS/in transfer/
   after response, expiry and source/Root/Goal/connection/Vault drift before
   finalization; original contact history/deadline survives restart, GET inspector
   makes zero contacts, positive cleanup releases only the exact reservation;
   lost cleanup/response stays Unknown with visible blocked recovery.
5. **Writer/ordinary regressions:** SQL-writer I/O guard and mutation barriers;
   outside-writer physical stage vs current same-session CAS negatives. Existing
   public browser remains credential-free; #940 finite reads/write-approval/
   cooldown/cancel behavior and private evidence permissions remain intact.
6. **Operator UI:** actual managed isolated profile, typed limits/current consent,
   explicit unchecked provisioning/read-bookkeeping acknowledgment, exact pending
   request/idempotency, queued/running/blocked/Unknown/cancelled/success/no_learning
   and read-only reload. No automatic execute, refresh, retry or acknowledgment.

Production human owner login/claim semantics, production Home identity linkage,
cadence/bookkeeping classification and actual account/site read acceptance remain
explicit gates requiring separately authorized evidence. Linux test-host proof
does not claim macOS host provision/readiness; both retain the same CPU core
contract. No paid/provider inference or real account action is needed for local
acceptance. All source/evidence repository-local, private files 0600/directories
0700, disposable runtime fixtures allowed only with durable retained actual bytes.

## Non-goals and review handoff

No generic authenticated browser, JS/cookie imports, role automation, scheduler,
refresh guessing, new queue/authority store, account registration, #940 key/claim
use, model prompt/source egress, memory learning, notification mutation, other
site, or permission expansion. Implementation and final independent review must specifically assess Home delivery classification,
nondecorative Chromium JSON readback, complete contact/cleanup uncertainty, and
same-canonical-authority publication. No production acceptance is implied by local implementation proof.

## Consequences

The optional fixed profile preserves the portable CPU core and existing public
browser/#940 restrictions. Uncertain Home delivery or native cleanup remains
Unknown without replay; missing production evidence blocks production only.

## Verification

The acceptance/proof mapping above governs local validation. Complete native,
private-artifact, UI and negative receipts plus a fresh independent whole review
are required; fixtures do not establish production account acceptance.
