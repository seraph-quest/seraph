---
id: near-https-text-inference
title: "ADR-025: Optional NEAR HTTPS text inference"
---

# ADR-025: Optional NEAR HTTPS text inference

**Status:** Target decision proposed under [#959](https://github.com/seraph-quest/seraph/issues/959), subject to independent architecture/security review and effective on the reviewed milestone merge to `develop`. The capability remains Planned. This draft does not activate provider access or claim Shipped behavior.

**Decision class:** Narrow exception to ADR-006 for one optional native capability. ADR-024's three OpenRouter slots and ordinary model routing remain unchanged.

## Context

The operator explicitly revised #959 on October 7, 2026 to standard NEAR over
HTTPS with no verified-TEE claim. This decision grants a narrow conditional
implementation exception after independent review and becomes effective on the
reviewed milestone merge to develop. The capability remains Planned. Earlier
TEE/SDK/crypto research is historical evidence, not this milestone's prerequisite.
ADR-006 ordinary routes and ADR-024's three OpenRouter slots remain unchanged.

## Decision

## Fixed scope

- Capability `inference.near-text.v1`; profile `near.text`; runtime `near_text_native`.
- Model `z-ai/glm-5.3-flash`; base `https://cloud-api.near.ai/v1` only.
- One nonstreaming user text message, no tools/history/fallback/aliases/overrides.
- Request `n=1`; require exactly one response choice, never select from multiple.
- Ordinary verified TLS, `trust_env=False`, no redirects or automatic HTTP retries.
- No NEAR SDK, native crypto dependencies, attestation or verified-TEE/E2EE claims.
- Provider receives plaintext; private input/output means Seraph artifact custody.
- No operational provider calls, paid calls, credentials, deployment, main promotion
  or harness evaluation campaigns during implementation/acceptance.

## Configuration, credential and budget

`NearTextSetup` is a closed frozen persisted object: schema_version
`seraph.near.text.v1`, enabled default false, fixed profile_id/model_id/api_base,
max_output_tokens strict integer 1..1024, timeout_seconds finite 1..45,
request_cost_bound_microusd strict integer 1..1_000_000_000,
spend_ceiling_microusd strict integer 1..1_000_000_000,
credential_ref fixed `vault:near_text_api_key`, server credential_fingerprint,
server plaintext_egress_consent_revision. Request reserve must not exceed ceiling.

`NearTextSetupInput` uses SecretStr for optional write-only api_key; otherwise
matching editable fields and exactly `plaintext_provider_egress_acknowledged`
(strict bool, false default). Fingerprint/ref/consent revision are not writable.
Blank key preserves the existing NEAR key; no OpenRouter/env-key fallback.
Enabling or changing an enabled NEAR route requires acknowledgment true with
exact current expected_policy_revision; only an unchanged previously CURRENT
enabled purpose consent may carry to the new active revision. Contact requires
enabled=true and consent bound to the current active witnessed revision.

`ModelFabricConfiguration.near_text` is optional. PUT omission preserves it;
explicit null is rejected (disable with enabled=false). Reject simultaneous
near_text and OpenRouter mutation in the first version. GET includes enabled,
fixed route, limits, shared ceiling, key presence/fingerprint, consent_current,
status/reason, tls_transport=true, tee_verified=false, e2ee=false and explicit
provider plaintext disclosure. GET performs no provider operation.
Near status enum is disabled|configuration_required|blocked|configured.
Configured means local key/current consent/accounting readiness only, never live
provider/model availability or attestation. reason_code is nullable; absent setup
projects null and UI offers a disabled-default setup form.

`deployment_spend_ceiling(configuration)` validates a unique positive shared
ceiling. Near ceiling mirrors the SAME existing deployment ledger ceiling.
When OpenRouter exists, mismatching NEAR save returns 409. Persisted mismatch
degrades configuration. Near-only bootstrap uses the existing genuinely-empty
accounting lifecycle/witness, never creates another ledger or resets liabilities.
An explicit OpenRouter shared-ceiling edit synchronizes the NEAR mirror.

One global policy epoch and revoke fence govern both providers. NEAR grants
explicitly disclose that global revoke affects OpenRouter and NEAR; preserve
the existing OpenRouter grant identity. Dedicated disable is a settings save.
Use witnessed r+1 revoked -> separate vault/accounting update -> r+2 active CAS.
Carry only previously CURRENT consent for unchanged enabled egress routes;
ceiling mirror is a shared-budget field, not a new purpose grant. Never resurrect
revoked consent. Preserve all three OpenRouter slots, proofs and credentials.
If a NEAR save explicitly regrants NEAR after global revocation, retain the
OpenRouter route objects/key/proofs but clear its revoked cloud-egress acknowledgment
and stale purpose consents; no OpenRouter route may reactivate from that save.
A disabled NEAR save from globally revoked state leaves final publication revoked.
Conversely an OpenRouter regrant cannot resurrect revoked NEAR purpose consent.
Generic capability canaries exclude NEAR. Configured readiness is not availability.

The configuration owner exposes `validate_near_text_setup`, `near_text_profile_for_setup`,
`current_near_text_policy() -> (configuration,digest)`,
`capture_near_text_credential(*, expected_revision, expected_fingerprint)` and
settings/configuration publication. Default/general selectors stay OpenRouter-only.
Accounting uses the dedicated runtime discriminator consistently at reserve/contact/finish
and current-policy recheck; no impersonation of an OpenRouter route is permitted.

## Input, transport, billing and output interfaces

`NearTextInput`: closed `seraph.near.text.input.v1`, question nonempty UTF8
1..8192 bytes, max_output_tokens strict integer 1..1024 and no greater than the
current configured NearTextSetup.max_output_tokens cap at admission/contact.
The exact input value drives max_tokens in the provider request; never silently
clamp, expand or replace it. The configured cap is an enforced upper limit.
This is a private immutable
input artifact, not public task metadata. Use existing WorkBoardTaskCreate with
input_artifact_id, UUID idempotency, authenticated server owner/Root/Goal revision
and original/current finite purpose authority. Browser question lives only in
memory; no sessionStorage/localStorage retention or automatic replay.

The transport owner exposes `model_fabric/near_text_contracts.py` input/result/receipt types and
`near_text.py` exposing async `invoke_near_text(*, context, question,
max_output_tokens, validate_current, hooks: RouteReceiptHooks)`. Hooks use the
existing typed route-receipt protocol and cannot authorize/widen provider contact.
It constructs only the fixed route,
uses the existing shared broker and returns bounded private answer/receipt. The billable evidence source is the typed
near_text_billing module bound to the original operation, reservation runtime/profile
and response digest; it cannot impersonate OpenRouter usage or manual settlement.
The dedicated preflight constructs the exact fixed TrustRequest and calls the
existing evaluate_trust with actual principal/provenance/resource/authority;
it requires native runtime/lease, current original Root/Goal/purpose, exact fixed
profile/model/URL and the witnessed policy. No forged decision IDs, allowed
booleans, model canary proofs or generic selector/credential allowlist widening.
The billing owner exposes `near_text_billing.py` typed evidence/strict billing parsing; Accounting integrates
its evidence into the existing reservation/settlement owner, never fake usage.cost
or a spoofed manual operator settlement.

One original attempt; deadline 120s INCLUDING queue; inference ≤45s; at most one
outstanding NEAR operation per owner (existing OpenRouter owner bounds unchanged).
Global provider callback peak remains 1 with priority preserved. All stages use
the original remaining deadline. Request ≤64KiB; completion wire ≤256KiB;
answer UTF8 ≤64KiB; billing wire ≤16KiB. Require identity content encoding,
count raw bytes before parsing, reject compression, redirects, malformed/oversized
JSON, model mismatch, tools or non-assistant-text output. Never log response bodies.

After ONE inference POST, at most two fixed `/v1/billing/costs` POSTs with
`{requestIds:[UUID]}`, 1s between attempts, total ≤15s and original deadline.
Official docs commit `021296c01f593e9d0521ce7a227247ce0c7f9000` specifies billing
UUID = uuid.uuid5(uuid.NAMESPACE_DNS, response body id). Require bounded nonempty
body id (maximum 256 UTF8 bytes) and compute this identity. The original operation
ID is also limited to 256 UTF8 bytes. Present inference-id must be strict UUID and
match it; malformed/mismatched header fails. Only absent header allows computed
fallback. UUID text uses hyphenated 8-4-4-4-12 form without whitespace, braces,
URN or compact alternatives; hex case is normalized to lowercase. Ignore
x-request-id. Accept exactly one matching requestId and strict
non-bool integer costNanoUsd 0..1_000_000_000_000. Warning must be absent, null or
the empty string; any other value/type rejects. Integer ceil(nanoUSD/1000) yields
microUSD. Typed evidence binds original operation, provider UUID, fixed source,
nano/micro amounts and billing-response SHA256; settlement checks original row
runtime `near_text_native`, profile `near.text` and operation binding.

Reserve is a LOCAL admission hold, not a vendor-enforced billing maximum.
Unknown/missing/invalid charge retains full liability, releases NO answer artifact
or preview, discards plaintext buffers, projects cost_liability/blocked, never Done
and never replays inference. Manual settlement reconciles debt only, cannot recover
discarded output. Over-bound genuine charge remains charged and requires existing
overrun review; no output adoption under an unreviewed overrun. No clipping or
token-price estimates. Billing after contact may
settle original liability using captured credentials; revocation cannot authorize
new inference or result adoption. Original deadlines still bound billing.

The native owner implements `work_board/near_text_native.py`, native registration/dispatcher/parser,
direct identity/input/authority/fingerprint/readback and authenticated GET
`/api/work-board/tasks/{task_id}/near-text/output`. It uses current owner/Root/Goal,
private custody, actual settled-cost readback and adoption CAS. Existing native
accepted/queued/running/succeeded and Work review/Done transitions remain authoritative;
no LLM answer-accuracy or automatic human-review approval is introduced.

`seraph.near.text.receipt.v1` is content-safe metadata in existing job/artifact/route
owners: task/attempt/job/request/operation IDs, provider/profile/model, transport
and nonverification flags, input/output/policy digests, provider billing UUID,
cost source/amount/state/ledger reference, no_learning and result artifact only
after success. No new execution table or plaintext checkpoint. The UI uses existing
settings sibling mounted in ArtifactStoragePanel, modelFabric DTOs and Work question/result UX; plaintext/cost
limitations must be visible and controls remain usable on metadata failure.


The input wire is `seraph.near.text.input.v1` with question:str and
max_output_tokens:strictint; no writable authority fields. The authenticated GET
`/api/work-board/tasks/{task_id}/near-text/output` returns bounded answer text and
the content-safe `seraph.near.text.receipt.v1` under current owner/Root/Goal, with
zero external contact. Existing native accepted/queued/running/succeeded readback
feeds existing Work Review/Done human-review transitions, never automatic LLM
answer-accuracy approval. Unknown native cost_liability retains ledger Unknown
and projects task blocked with `near_cost_readback_required`, without answer artifact.

Billing source: official nearai/docs commit
`021296c01f593e9d0521ce7a227247ce0c7f9000`, dated October 5 and read October 7, 2026.
[OpenAPI](https://raw.githubusercontent.com/nearai/docs/021296c01f593e9d0521ce7a227247ce0c7f9000/api-reference/openapi.json)
SHA256 `7a03bc46aa540a510c2d5b9826862a2b20a5beda10d2e096bf8196524d232f3e`;
[usage guide](https://raw.githubusercontent.com/nearai/docs/021296c01f593e9d0521ce7a227247ce0c7f9000/cloud/guides/usage-reporting.mdx)
SHA256 `4b4c98e15966fe2ee63e2a3a63219cc363bb7d613dbd4c8b7d7f9a5337ceb071`.
Protocol evidence does not prove live availability or operational billing.

## Verification

Focused billing/HTTP/config/native tests with actual HTTP boundary interception;
real existing SQLite/witness/accounting/private artifact/readback. Unknown, timeout,
crash, cancel/revoke/authority races, one-attempt/idempotency, shared priority/peak1,
cost warning/mismatch/overflow/overrun, unsafe override, byte caps, privacy/foreign
owner and publication CAS/credential compensation must fail closed. Existing
OpenRouter settings/accounting regressions remain mandatory; no weakening tests.
Keyless managed runtime/UI and an intercepted full native journey are required;
no provider availability or live billing claim. Fresh independent exact-pushed
cumulative diff review before readiness/merge. No tiny or draft PRs.

## Consequences

NEAR receives provider-readable plaintext; ordinary TLS and private Seraph
artifact custody do not establish TEE/E2EE confidentiality or answer accuracy.
No SDK/native verifier dependencies or invented trust pins are introduced.
Rollback disables NEAR, quiesces future contact and retains audit/reservations/
Unknown liability. No fallback or contacted inference replay. Live availability,
price and usefulness remain unverified without separately authorized operational
receipts. STATUS/current guide change only with implemented reviewed behavior.
