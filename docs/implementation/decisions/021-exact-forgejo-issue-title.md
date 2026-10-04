---
title: "ADR-021: Exact Forgejo Issue Title Transaction"
---

# ADR-021: Exact Forgejo Issue Title Transaction

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#925](https://github.com/seraph-quest/seraph/issues/925), under [#899](https://github.com/seraph-quest/seraph/issues/899).

## Context

The lead accepted the fixed v3 protocol on 2026-10-04 after independent review.
Design SHA256: `d7c75d9699456250cb290290309eab1b76d51edc4e0106aa74266d08847897fb`.
Independent review SHA256: `597efee70adcf114e363e4d56ec481c1c0aef26ec23814d4b594837d479ba228`.
This accepted target does not claim shipped behavior or production account acceptance.

## Decision

## Candidate and useful outcome

Correct the literal title of ONE existing ordinary issue in ONE operator-owned
Codeberg repository, using the actual Forgejo title editor and its Save button.
Example: change a mistaken issue summary to the approved precise summary while
retaining issue identity/body/status/labels. No pull requests, issue creation,
comments, file uploads, labels, watchers, mail, payments or arbitrary forms.
Local acceptance uses a genuine isolated unmodified Forgejo v15.0.9 service,
real test account/repository/issue and actual Chromium. Live Codeberg use remains
blocked pending separately authorized account/session/site/version acceptance.

The lead accepts the upstream JS-backed UI form and disclosed provider no-CAS/idempotency gap. The existing implementation authorization covers acquiring one official pinned verified Forgejo distribution AFTER this protocol gate, without a maintainer host or new user-approval prerequisite. No executable or account operation occurs in this pass.

## Current primary research and limits

Read on 2026-10-04, public code is data, never executed during this pass:

- https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/templates/repo/issue/view_title.tmpl
  retained `upstream-source-0-r1.txt`, SHA5d527b4edde66543aee9a0f38b080ab8fdc81af8008caee7f9ed49f2a822b0d2.
  Lines7–30 bind edit permission, fixed selectors, title input, old title and
  issue-specific update URL. Input maxlength245 is stricter than server255.
- https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/web_src/js/features/repo-issue.js
  retained `upstream-source-1-r1.txt`, SHA9cbb65842796f0303b27409039f8b9737397143524959c483165f6f83981a239.
  Lines561–619 show actual edit/save event handlers, title trim, title-only POST
  and subsequent reload. A PR can trigger another write, hence ordinary issues
  only and any PR controls/layout mismatch reject.
- https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/routers/web/repo/issue.go
  retained `upstream-source-2-r1.txt`, SHAfbc9db9f691a122f394f02d3fdf9899498573827087c589331e09af22860d4da.
  Lines2253–2287: authenticated poster/write permission, title normalization,
  service mutation, JSON title response. No request idempotency key or expected
  revision is accepted here. Never claim provider CAS/exactly-once support.
- https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/services/issue/issue.go
  retained `upstream-extra-1-r1.txt`, SHA7d6d951d49a2d8dc597dc7f3861a81d0b976ca12231917e0c8234bdc6483752f.
  Lines64–101: unchanged-title short circuit, title persistence and notification
  servicing. Reversal does not remove title-change history/notifications.
- https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/web_src/js/modules/fetch.js
  retained `upstream-extra-2-r1.txt`, SHA0987793d464d7c4edcba8d95af7fa6f1aa7990ebe32aef733e1d4eafd9d981d6.
  Browser URLSearchParams uses the site's fetch wrapper. The protocol below
  resolves the absence of a token, exact authentication and timeline routes;
  this helper alone was insufficient evidence in v1.
- https://forgejo.org/docs/latest/admin/installation/binary/ : official current
  documentation identifies v16.0.5 latest/v15.0.9 LTS and SQLite setup. A versioned
  binary/signature is a prerequisite, not presently installed.
- https://forgejo.codeberg.page/docs/latest/admin/advanced/customization/ :
  upstream warns templates change incompatibly. Live Codeberg version/layout is
  unverified; this candidate therefore has an explicit version/layout gate.

Research retrieval receipts: `upstream-research-r1.json`,
`upstream-extra-research-r1.json`; web-tool raw fetch failures preceded successful
bounded operator read-only fetches. A title.go guess returned404; no false source
claim. `local-availability-r1.json` is the narrowed read-only Docker inventory.
Owning issue #925 was read completely with gh issue view; its four AC are mapped
below. No GH write occurred.

## Fixed authority and typed input

Capability `browser.forgejo-issue-title.v1`, native job
`forgejo_issue_title_v1`, output `forgejo_issue_title_receipt.v1`, no_learning.
One exact `https://codeberg.org` site profile; no caller origin/URL/selector/JS.
Fixture maps that logical origin to the actual local Forgejo TCP/TLS service
through a constructor-only test seam, never browser HTTP mocking or a user knob.

Input includes original owner/principal/current authenticated Root token binding,
active Goal ID/revision, selected site connection revision, verified numeric
provider user ID, repository numeric ID + exact owner/name, issue numeric ID +
repository-local index, original literal title/updated_at + bounded immutable
timeline tip digest, new literal title, exact approved preview digest and UUID.
Title is already stripped, NFC-normalized, nonempty, at most245 characters and
245 UTF-8 bytes; CR/LF/control/bidi controls reject, never silently normalize
after approval. Distinguish issue ID from index. Same title returns explicit
no_change/read-only preview rather than submitting. Exactly one title field.

Permissions: explicit exact-site session provisioning/read consent, and separate
unchecked exact title mutation approval disclosing public title/history and
provider notification/access side effects. Credentials never grant mutation.
Approval binds full input/source/account/version/route/body digests, ownerRoot,
Goal, connection/Vault revision/digest, original deadline and operation set.
User reviews old/new title and target before approving. No model invocation or
standing consent follows. Session consent≤900s, preview approval≤300s, one
job≤120s, all capped by original Root/Goal/consent; budget0, priority50 default,
maxAttempt1, shared browser-task-lane, bounded existing queue. No extension.

## Smallest runtime/profile extension

Public navigate/extract grammar remains unchanged. Add a fixed-site helper/native
adapter behind existing browser API/control/Inspector; reuse durable jobs,
approvals, encrypted artifact/Vault storage and pinned transport, no second
ledger, general form executor or cookie import API.

The profile owns a backend-only encrypted cookie jar bound to Root/user/site/
connection/Vault revision. Fresh browser context has no persistent storage,
no HTTP credentials/downloads/service workers/popups/permissions. Provision via
the exact source-verified Forgejo login mechanism using Vault-stored test/site
credentials and distinct consent; no reusable browser-cookie export. Strict
allowlisted cookie name/domain/path/secure/expiry, no cross-domain cookies;
current provider identity independently verified before every mutation. Logout,
revoke or credential rotation destroys usable jar and blocks reuse. No refresh
or authentication guessing; expired/login-redirect response blocks.

Forgejo editor requires JavaScript and the signed distribution’s fixed asset/bootstrap manifest. v15.0.9 has NO CSRF token: it uses Go http.CrossOriginProtection with actual Origin/Sec-Fetch-Site, as pinned below. There is no placeholder or CSRF token substitution. Cookies and password stay backend-only; all browser responses strip Set-Cookie and reject secret echoes. The actual upstream editor and its own scripts remain intact.

Allowlisted contacts: exact authenticated identity GET; exact issue HTML GET;
fixed audited same-origin static GET assets; exact current issue/read-only
timeline GETs; ONE title POST `/{owner}/{repo}/issues/{index}/title`; exact issue
reload. The protocol table below pins exact login/identity/timeline routes and authentication. No subrequest wildcard, telemetry, websocket, external scripts,
feeds, arbitrary redirects or login followed automatically. Same-origin reload
is one bounded read, never a repeat submit. Redirects can never turn a POST into
another write. Every request checks current Root/Goal/approval/connection/Vault/
policy before and after DNS/headers/chunks; production HTTPS DNS/IP pin with
logical Host/TLS SNI, mixed/private/link-local/metadata DNS denial. Constructor
loopback exception exists only for the approved local test fixture.

Caps:64 total mediated contacts including identity/assets/reload/readback,
one mutation,32 immutable version-pinned assets,16MiB asset aggregate/4MiB individual asset; 512KiB individual HTML/API
response,16KiB POST,16KiB provenance/64KiB private receipt/96KiB cipher. Exceeding
a cap blocks, never silently truncates proof or refetches/retries to finish.
Recovery window≤120s/8GETs/1MiB, capped by original live Root/current read consent;
one fresh explicit read-only acknowledgement, no POST permission or renewal.

## State, intent, submission and readback

Preview independently reads exact identity/repo/ordinary issue and literal
old title/update/timeline. After approval, claim once and reread same source;
changed account/page/title/revision/layout blocks before mutation. Verify one
title editor/input/Save/update URL, no PR or unexpected fields. Fill input and
real-click upstream Edit/Save; the mediator validates actual browser-generated
URLSearchParams title bytes against immutable approved bytes. Commit ONE intent
and contact marker under pure canonical SQLite Root/Goal/job/fence/approval CAS
BEFORE forwarding the first POST byte. Stage files/Vault/session/page proof
outside writer; hold existing lifecycle policy pin through DB-only rechecks.
Provider requests run outside SQLite. Persist bounded actual response observation
before later stale-authority rejection so contact history survives.

Success requires received positive POST response for the original intent plus
independent authenticated provider GET and actual DOM reload proving the same
numeric issue/repository/account and exact approved title; retain response and
readback digests, original intent/fence and provider timeline observation.
Do not call title equality alone a mutation receipt or exactly-once attribution.
If title-change event ID/actor/old/new fields are available from the pinned
provider timeline, retain that exact event identity; absence/ambiguous events
cannot upgrade a lost-response operation to succeeded. No seeded result rows.

Provider has NO atomic expected-revision CAS. The precontact reread cannot
prevent another edit in the last network gap. Expose this residual in preview;
any observed competing change blocks or keeps Unknown. Root must accept this
bounded named-site limitation or reject the candidate; do not claim atomic
external lost-update protection. Local proof includes a real competing edit.

Timeout/cancel/process restart after possible submission = Unknown. No reclick,
POST replay, auto-refresh, compensation or silent capacity release. Current
read-only recovery GETs report original intent + destination observed current
title/timeline and explicit attribution uncertainty. Observation does not adopt
new task Success, grant permission, reset deadline or erase original Unknown.
Duplicate execution UUID returns exact historical receipt; a different request
cannot reuse the old effect. Another identical title could have been submitted
by a different actor, so equality remains destination observation only.

Precontact cancellation with proved no-contact and positive actual browser/
transport quiescence can release its own reservation. Postcontact release
requires complete positive effect settlement plus actual transfer and Chrome
closure; absent handles/free lock alone do not prove it. Unknown stays reserved.
Cancel must preserve intent/observations; cancelled stale publication cannot
create Success/private artifact/adoption. CPU/browser lifetime cleanup bounded.

Compensation is NOT automatic undo: only a NEW preview and independently
approved reverse-title transaction on the exact same issue, with current
original-title/source checks. History/notifications remain. No reverse while
original effect attribution/current title is ambiguous; disclose that limit.

## Existing seams / tentative ownership

- `backend/src/browser/task_runner.py:2071`: real Chromium launch/context/cleanup;
  `:2128` per-request guard; keep public grammar/read behavior unchanged.
- `backend/src/browser/pinned_transport.py:500` and
  `backend/src/security/http_transport.py:186`: same pinned GET/POST transport
  already accepts bounded form bytes; add fixed mediator, not generic routes.
- `backend/src/browser/moltbook_private_read.py:168` and native helper: reuse
  patterns for immutable private readback, cleanup and outside-writer staging;
  do not reuse Moltbook credentials/connection identity for Forgejo.
- `backend/src/workflows/job_runtime.py:3766,6087,6435,3369`: existing claim,
  contact/readback effects and cancel callbacks with pure in-session authority.
- `backend/src/approval/repository.py`, `runtime.py`, `identity.py`; existing
  exact preview fingerprint/approval consumption pattern exemplified by
  `backend/src/workflows/repo_publication.py:245–314,404–413`.
- Proposed focused `browser/forgejo_issue_title.py` and native helper plus
  one existing browser API/control route and Inspector component; the frozen connection ownership below controls its model and columns.
  One focused canonical connection row is required: #918's Moltbook row is
  integration-specific and has no reusable cookie-session engine. No broad
  profile registry/session authority store. Ordinary #940/#918 unchanged.
- Existing encrypted Vault/private artifact and registry classification, no
  model-purpose authority, no general evidence learning. ADR-021 owns the accepted target.

## AC / validation map

1. Named reversible transaction: actual unmodified Forgejo fixture and exact
   title editor→POST→durable service DB→independent issue DOM/provider readback.
   Verify real service issue/timeline identity; reject if unavailable. No echo
   service or fixture-generated receipt. Live Codeberg gate remains blocked.
2. Exact preview/current authority: actual authenticated Seraph Root, finite
   Goal, Vault session, native job, exact approval and typed payload; negatives
   wrong Root/owner/account/repo/ID, expired/revoked session/approval/Goal,
   title/source drift, modified posted fields, injected content and layout/JS
   asset drift. Writer-I/O barrier proves DB-only current CAS.
3. Intent/Unknown: real response loss AFTER actual server commit, immutable
   intent and durable provider mutation retained, cold restart GET-only recovery,
   identical title from another actor remains uncertain, concurrent external
   edit retained, duplicate UUID causes zero second mutation. 404/incomplete
   timeline cannot release capacity. Cancel races before contact, in-flight and
   before publication prove original history/cleanup/no regrant.
4. Real browser/test site: actual Chrome site UI Edit/input/Save, actual source
   JS POST and exact destination reload; session isolation, redirect/private DNS,
   secrets absent from browser/storage/canonical artifacts, finite caps, cookie/
   session rotation/expiry, actual cross-origin CSRF rejection and positive process/transfer cleanup. Managed Seraph
   operator preview→approve→submit→receipt→restart→read-only recovery later uses
   only manage.sh on owned isolated ports, no providers/models/real accounts.

Retain actual service closed SQLite snapshot, Seraph SQLite/artifacts/lifecycle,
source/asset digests, stdout and privacy-safe DOM/screenshots privately. One
bounded≤120s native group per worker. No reusable cookies/token exports. Explicit
no_learning for all outcomes and no quality/exactly-once/production claim.

## Reviewed protocol provenance

Both review findings are resolved. Actual local execution and independent whole implementation review remain required. Production Codeberg account/version/site acceptance stays blocked.

## V2 exact protocol, retrieved 2026-10-04 UTC

Every source below is official `https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/`
plus the listed path unless explicitly stated otherwise. Retained per-file
hashes/URLs are in `protocol-r1` through `protocol-r6/receipts.json` and the final
source inventory. Line numbers refer to these unchanged actual bytes.

| Step | Exact protocol and fail-closed rule | Pinned source |
| --- | --- | --- |
| Login | Trusted backend GET `/user/login`, then one URL-encoded POST `/user/login` with only `user_name`, `password`, `remember=false`, no redirect/openid/2FA fields. Password stays Vault/backend, never browser. CAPTCHA, TOTP, WebAuthn, password-change or external-auth flow is unsupported/blocked, not guessed. Only a same-origin redirect Location is inspected, never blindly followed. | `routers/web/web.go:547–581`; `routers/web/auth/auth.go:179–286`; `templates/user/auth/signin_inner.tmpl:13–46` |
| Session issue/rotation | Login regenerates session and stores numeric `uid=u.ID`; encrypted jar stores only `session`, scope `/`, empty Domain, HttpOnly, Secure under HTTPS, SameSite=Lax. `remember`/redirect/i_like_gitea/locale cookies are not usable authority; strip all Set-Cookie before browser fulfilment. Any unexpected session-domain/name/scope or mid-job session replacement blocks. | `auth/auth.go:297–320,882–901`; `modules/setting/session.go:16–67`; official dependency `code.forgejo.org/go-chi/session/raw/tag/v1.1.0/session.go:432–463,504–530`; `go.mod` pins dependency v1.1.0 |
| Session expiry/logout | Seraph original finite consent≤900s caps usability regardless provider defaults86400s. Provider cookie may be a session cookie with no Expires; it does not grant unlimited Seraph use. Unsupported cookie expiry or provider login redirect blocks; no refresh/relogin in an admitted job. Explicit logout POST `/user/logout` uses its own current session-close control; provider flush/destroy, then local jar revocation even if provider response ambiguous. | `modules/setting/session.go:35–67`; `auth/auth.go:371–390`; dependency session.go:476–501 |
| Numeric session/account | Web session verifier reads int64 uid and resolves current user by numeric ID. API cookie authentication is **NOT supported**: API middleware auth group includes OAuth2/HTTPSign/Basic, not Session, and passes nil session. Use backend-only same original Vault username/password Basic ONLY on allowlisted GET `/api/v1/user` for numeric ID/login mapping, never an API write; positive password login stores that same account's uid. Verify signed-in issue navbar exact username and repeat Basic numeric identity GET before/after submission. Any rename/ID change/login page blocks. Never call cookie-only API GET an authenticated identity proof. | `services/auth/method/session.go:20–60`; `routers/api/shared/middleware.go:49–87`; `routers/api/v1/api.go:633–636`; `routers/api/v1/user/user.go:137–152`; `services/convert/user.go:49–53`; `templates/base/head_navbar.tmpl:154–168`; `services/auth/method/basic.go` retained |
| CSRF | NO real CSRF token exists in this pinned protocol. Remove v1 placeholder/substitution idea completely. Web middleware calls Go `http.CrossOriginProtection.Check` for protected title routes. Real browser POST must have Origin exactly `https://codeberg.org`, Sec-Fetch-Site exactly `same-origin`, exact same-origin Referer/document/frame/route and approved title body. Trusted transport forwards these actual validated values and adds only encrypted backend cookie; it never fabricates metadata or exploits missing-header allowance. Wrong/absent metadata blocks before contact. No exemption/trusted-origin bypass is configured. | `routers/web/web.go:159–221`; `templates/base/head_script.tmpl:6–50`; `web_src/js/modules/fetch.js`; Go1.26.0 official `https://raw.githubusercontent.com/golang/go/go1.26.0/src/net/http/csrf.go:15–29,134–174` |
| Browser HTML sanitization | Reject any real jar/password bytes in HTML/JSON/assets before fulfilment. Strip every Set-Cookie and credentials-bearing header. Do not pass a token, cookie, authorization header or login response into page state/DOM/storage. Pinned bootstrap has public app URL, asset prefix/version, PageData, language and notification settings; validate closed fields, exact origin/version and script structure before retaining/executing. Unexpected token fields or inline scripts/layout reject, not regex-delete-then-run. No synthetic form/editor or Seraph-generated submit JS. | `head_script.tmpl:6–50`; `templates/base/head.tmpl`; `footer.tmpl:16–20` |
| Ordinary issue identity | Backend-only Basic GET `/api/v1/repos/{owner}/{repo}/issues/{index}`; exact path grammar/encoded segments, no query URL injection. Require numeric issue ID/index and repository ID/full name, title/updated_at and no pull_request. Independent GET plus actual browser issue reload must agree before success. Browser authenticated issue document is `/{owner}/{repo}/issues/{index}` and title POST is that exact path plus `/title`; never `/pulls`. | `routers/api/v1/api.go:1118–1128`; `services/convert/issue.go:45–71`; `routers/web/web.go:1252–1254`; retained `UpdateIssueTitle:2253–2287` |
| Timeline | Backend-only Basic GET `/api/v1/repos/{owner}/{repo}/issues/{index}/timeline?page=1&limit=21`; reject population>20, incomplete/truncated body/pagination, never limit before matching. API includes all event types; title event `type=change_title`, numeric id, user.id, old_title/new_title, created/updated, issue_url. Exact issue event hashes `issuecomment-{id}` are visible in source-rendered DOM. Event ambiguity is observation only; positive POST+current exact GET required for normal Success. Lost response NEVER upgraded from matching timeline/title alone. | `routers/api/v1/api.go:1128`; `routers/api/v1/repo/issue_comment.go:132–225`; `modules/structs/issue_comment.go:56–84`; `services/convert/issue_comment.go:76–102`; `models/issues/comment.go:83,138,515–522`; `templates/repo/issue/view_content/comments.tmpl:242–250` |
| Mutation details | Preserve actual editor/Save source-generated POST title only; server transaction updates name and creates title-change comment. Server truncates title at255 BYTES, so v2 caps approved old/new literal title≤245 UTF-8 bytes, not the previous1024. Reject `@`, `#`, `!`, URL/reference markup in both titles to exclude cross-reference creation/removal from the supported literal title profile. Notifications/history remain disclosed. No provider revision CAS or idempotency. | `models/issues/issue_update.go:151–184`; `services/issue/issue.go:64–101`; `view_title.tmpl:22–30`; `repo-issue.js:561–619` |

### Asset bootstrap contract and one precise remaining blocker

Pinned HTML loads `/assets/js/webcomponents.js?v={AssetVersion}` and
`/assets/js/index.js?v={AssetVersion}`, CSS index/theme; public prefix is `/assets`
(`templates/base/head_script.tmpl:50`, footer:16, head_style:1–2).
`web_src/js/bootstrap.js:7` sets webpack public path from that exact prefix;
`webpack.config.js:111–123` gives index/webcomponents entrypoints. After gate,
extract embedded asset bytes from the authenticated signed distribution into a
private manifest before any job. Only exact relative names in that immutable
manifest, exact version query and matching digest are eligible; no `/assets/*`
network wildcard. ≤32 assets,4MiB individual/16MiB aggregate; HTML/API512KiB,
all contacts≤64. Missing/dynamic asset not in frozen manifest blocks. No guessed
bundle hash or runtime internet package acquisition. Source code is not itself
compiled-asset proof; runtime distribution extraction must establish that proof.

**Bootstrap blocker RESOLVED by the one lead-directed source lookup:**
Official pinned source
`https://codeberg.org/forgejo/forgejo/raw/tag/v15.0.9/web_src/js/features/repo-legacy.js`
was retrieved read-only at 2026-10-04T04:16:03.232340+00:00, 22,166 bytes,
SHA256 `b16c0614281ab00593b7d89b038816f1ce67a347390c6b2df08b01666cc103e5`.
Actual retained source is `protocol-final-r1/repo-legacy.js.txt`; exact dated
receipt is `protocol-final-r1/receipt.json`.

Complete upstream execution wiring is now demonstrated: `web_src/js/index.js:73`
imports `initRepository` from `features/repo-legacy.js`; its onDomReady callback
calls that function at line177. `repo-legacy.js:2–8` imports
`initRepoIssueTitleEdit` from the already pinned repo-issue.js; `:400–401`
requires a repository page, `:465–470` checks `.repository.view.issue` and calls
`initRepoIssueTitleEdit()`. The upstream view template renders those exact
classes and the title editor. repo-issue.js:561–619 installs the real Edit/Save
handlers and source-generated title POST. No injected initializer, patched
provider, HTTP echo or direct API mutation is needed. The earlier v2 failure to
trace the legacy import remains historically retained and is corrected by
this exact call-chain evidence, not by fabricated browser success.

This proves source wiring, not actual runtime behavior. The signed-distribution
asset extraction/digest manifest and genuine local service/Chromium Save proof
remain the mandatory implementation acceptance checks, under the finite caps
above. Unexpected layout, missing handlers or changed asset bytes must block
rather than patch or bypass. No additional source lookup is needed at this gate.

### P2 completed bounded provisioning plan

After completed protocol review/root acceptance, existing implementation
authorization covers obtaining official fixed v15.0.9 Linux-amd64 binary from
`https://code.forgejo.org/forgejo/forgejo/releases/download/v15.0.9/forgejo-15.0.9-linux-amd64`
(official mirror named at https://forgejo.org/download/), its `.asc`, and official
published digest if present; ≤200MiB binary/1MiB metadata, one retrieval each,
no latest selector, redirect to unapproved host or package installer.
Retain actual SHA256 and verify detached signature with existing `/usr/bin/gpg`
in a private repository-local keyring against the official release signing
fingerprint `EB114F5E6C0DC2BCDD183550A4B61A2DC5923710`, current official published
key. Do not import global keys or execute before signature/integrity proof;
digest metadata alone is not provenance. Official download docs retrieved today
explicitly describe this key/signature scheme. This is a future plan, not a
download/execution receipt or an invented expected artifact SHA.

Keep binary/keyring/config/database/repo/session/assets0700/0600 below repository
`.agent-evidence/925`, no system install. Bind owned loopback port only, SQLite,
local disposable username/password, remember=false, session lifetime900s,
HttpOnly/Secure/Lax scope/root as above. Source-pinned app.example.ini settings:
server HTTP_ADDR127.0.0.1/DISABLE_SSHtrue; security INSTALL_LOCKtrue and
DISABLE_WEBHOOKStrue; service DISABLE_REGISTRATIONtrue, ENABLE_NOTIFY_MAILfalse,
CAPTCHAfalse, reverse-proxy authfalse; mailer off, OpenID sign-in/signupfalse,
Actions/packages/federation/remote avatar/update-check cron off. Deny ALL
nonloopback fixture egress mechanically in addition to config, preventing
mailer/webhook/cross-reference network contacts even if an option drifts.
Generate local TLS with existing openssl or route trusted local TCP through the
constructor-only fixture transport while preserving actual logical origin and
browser security metadata; never weaken production TLS/DNS pinning.

Create a fresh disposable local account/repository/ordinary issue using supported
provider CLI/API under approved fixture bootstrap, retain actual provider DB/
readback, not inserted success/receipt rows. No maintainer host prerequisite or
new user permission request; no live account. Provider setup is finite test
orchestration, not Seraph mutation authority. Existing manage.sh owns Seraph
only; owned provider test subprocess has explicitPID/start/deadline/cleanup.

## Finding disposition and gate truth

P2 addressed: remove maintainer-host/later-user-approval prerequisite; bounded
official verified acquisition is covered AFTER protocol gate. P1 addressed:
exact session/cookie/no-token cross-origin CSRF, numeric account/API eligibility,
timeline/sanitization source and COMPLETE bootstrap call chain are pinned.
Cookie-only API authentication and invented token substitution are excluded.
Source-backed protocol has no known unresolved design row; actual local
execution remains unperformed and required in implementation. This candidate
awaits fresh independent Luna design review and root acceptance before binary
acquisition, Seraph source/ADR, runtime or account operations. Production Codeberg
account/version/effect acceptance remains gated. Worker stops here.

## Frozen connection ownership


One typed `ForgejoConnection` row owns optional provider configuration, backend-only authentication material references and finite exact-site read consent. Extend the existing SQL models/engine and adapter/control API seams. This is neither a new operator identity nor a job, approval or effect ledger. Existing Root/session, Goal, durable job, approval, policy and effect stores remain authoritative.

Freeze these fields: connection ID; original owner principal and Root session ID; monotonic configuration revision; configured/active/revoked/blocked state; fixed site profile/version; opaque credential Vault reference and digest; opaque session Vault reference and digest; verified numeric provider user ID and provider login; finite read-consent revision and expiry; owning provisioning job ID; timestamps. A credential Vault payload contains the username/password; a separate session Vault payload contains the exact allowed backend cookie jar. No plaintext password, cookie or reusable authorization header belongs in SQL, browser state, logs or artifacts. Account identity displayed to the operator is metadata, not authority.

Read consent is bound to that exact owner Root/site/account/connection, at most 900 seconds and capped by current Root expiry. Every actual provisioning, read, preview, mutation and observation has its own current finite Goal and existing accepted bounded job. The connection does not grant a Goal budget or mutation authority. Provisioning uses a focused kind in the existing durable runtime, explicit consent and bounded actual login/identity contacts. It must not turn configuration save into hidden provider contact. Mutation additionally requires the exact independently approved preview and original job fence/deadline.

Rotation, logout or revoke increments the configuration/consent revision, invalidates usable session state, and rejects stale job/control/private-read pins. Local revocation must work even when provider logout fails. Never silently refresh or relogin an admitted operation. Read-only recovery requires current original Root, current finite read consent, unchanged exact provider/credential/session pins and a new explicitly acknowledged bounded read job; it leaves original Unknown, deadline, intent and liability intact. Changed or expired authentication blocks recovery instead of renewing the old attempt.


## Consequences

The optional adapter leaves CPU core startup and public browser grammar unchanged. The provider has no atomic title revision CAS or idempotency key; Unknown effects retain liability and never replay. No learning, quality, production account, or Shipped claim follows from local proof.

## Verification

The AC/validation map above controls actual Forgejo, Chromium, native job and managed cockpit verification. Official signature verification precedes fixture execution. Source, actual private artifacts, bounded logs and immutable physical inventories must support the final independent whole review.
