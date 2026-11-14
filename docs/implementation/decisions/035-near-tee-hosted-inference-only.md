---
id: near-tee-hosted-inference-only
title: "ADR-035: NEAR TEE-hosted inference only"
---

# ADR-035: NEAR TEE-hosted inference only

**Status:** Accepted under [#1148](https://github.com/seraph-quest/seraph/issues/1148), effective on the independently reviewed merge to `develop`; runtime capability **Planned** (implemented by [#1147](https://github.com/seraph-quest/seraph/issues/1147) slices PR3a–PR4). [Development Status](../STATUS.md) owns the effective route until those slices ship. No provider probe, catalog read or inference is authorized by this decision; the 2026-11-14 amendment (#1178) adds only the background, keyless, informational read of NEAR's public catalog record for enabled slots.

**Decision class:** Superseding provider target. Supersedes [ADR-006](./006-openrouter-only-inference-phase.md)'s provider choice and [ADR-024](./024-purpose-specific-openrouter-routes.md)'s OpenRouter route slots for new inference; absorbs [ADR-025](./025-near-https-text-inference.md); amends [ADR-008](./008-portable-core-and-consented-context.md), [ADR-026](./026-all-plugin-cordis-architecture.md) and [ADR-033](./033-governed-optional-audio-route.md). ADR-001's inference-only boundary, ADR-006's accounting continuity (#909), ADR-024's slot, publication, proof-key and namespace mechanics, and ADR-026's composition remain in force except where named below.

## Context

On 2026-11-13 the operator decided in chat with the lead:

1. All Seraph inference (chat, agent and guardian turns, report and memory
   synthesis, screenshot vision, embeddings) uses NEAR AI TEE-hosted models.
   OpenRouter is no longer used.
2. TEE level is **TEE-hosted models only**: NEAR's own enclave-hosted models
   per its catalog (`providerType=vllm`, `attestationSupported=true`,
   `verifiable=true`), called by canonical id with `x-no-aliasing: true` and a
   checked response model echo. Seraph does not verify attestation and uses no
   E2EE; the NEAR gateway receives plaintext.
3. The operator picks each slot's model from a closed allowlist. Nothing is
   enabled by default.
4. Delivery is stacked PRs that respect ADR-026.

ADR-025 (#959) chose standard NEAR HTTPS with **no** TEE claim because client
attestation needed an SDK and native crypto. The shift recorded here is the
operator's 2026-11-13 decision. It buys a model-selection constraint and a
dated, catalog-sourced hosting label, not new assurance: Seraph still verifies
no attestation and makes no confidentiality claim.

## Decision

### Allowlist (catalog read 2026-11-13)

Fields read from the NEAR catalog records
`https://cloud-api.near.ai/v1/model/{id}` by the lead on 2026-11-13. Every entry
except the embedding row reported `providerType=vllm`, `attestationSupported=true`
and `verifiable=true`; the embedding row comes from the catalog list (vllm,
attested) and its per-model fields are re-read by the catalog check.
Prices are USD per million input/output tokens.

| Canonical model id | Slots | Catalog facts | Conditions |
| --- | --- | --- | --- |
| `z-ai/glm-5.3-flash` | text, vision | text+image; tools, structured_outputs, reasoning, json_mode; ready; $0.08/$0.50; ctx 1,048,576 (base 100K) | Aliases span other families: `x-no-aliasing` is mandatory |
| `Qwen/Qwen3.8-27B` | text, vision | text+image; tools, structured_outputs, reasoning; ready; $0.04/$1.35; ctx 262,144; max out 65,536 | — |
| `Qwen/Qwen3-VL-30B-A3B-Instruct` | vision | text+image; structured_outputs, logprobs; ready; $0.15/$0.55; ctx 16,384; max out 8,192 | No tools: never text |
| `deepseek-ai/DeepSeek-V4.1-Flash-1017` | text, vision | text+image; tools, reasoning; $0.30/$1.20; ctx 1,048,576; alias `deepseek-ai/DeepSeek-V4-Flash` | Metadata incomplete (`isReady` absent, sampling list empty, no structured outputs). Amended 2026-11-14 (#1167): readiness is inferred from the attested, verifiable vllm record; structured output only through a current `structured_output` capability proof |
| `Qwen/Qwen3.6-35B-A3B-FP8` | text | text only; tools, json_mode, structured_outputs, reasoning; ready; $0.17/$1.10; ctx 262K | Aliases include `Qwen3-30B-A3B-Instruct-2507` |
| `Qwen/Qwen3-Embedding-0.6B` | embedding | embedding; $0.01/M (catalog list read) | Per-model fields re-read by the catalog check; before PR3d, source evidence must show that the embeddings endpoint and `/v1/billing/costs` honour `x-no-aliasing`, or the slot stays blocked |
| `openai/whisper-large-v3` | none (reserved audio) | audio | Not selectable under this ADR; see Audio |

Excluded: every proxied model (`anthropic/*`, `openai/gpt*`, `google/*`,
OpenRouter-routed) and third-party TEEs (Chutes: `deepseek/deepseek-v3.2`,
`moonshotai/kimi-*`; Tinfoil: `z-ai/glm-5.3`). Adding a model or slot requires an
amendment to this ADR with a dated catalog read. Allowlisted catalog facts are a
design basis, not live availability; the catalog check below decides dispatch.

### Slots, selector and admission

`NearTeeSetup` holds slots `text`, `vision` and `embedding`, all disabled with no
model by default. Task classes map exactly as ADR-024's
`route_slot_for_task_class`; unknown classes stay `route_slot_unmapped`, and
`audio_transcription` returns `audio_route_unavailable`. Each slot carries one
allowlisted canonical model id, temperature, required `max_input_tokens` (at
most the catalog context window), max output tokens, timeout and a request cost
bound. A request whose conservatively estimated input (images counted per the
provider's pricing) exceeds `max_input_tokens` is rejected before contact. Callers needing tools, streaming or structured output still
require that capability on the slot's proof. The base is fixed to
`https://cloud-api.near.ai/v1`; verified TLS, `trust_env=False`, no redirects,
identity encoding, byte caps and duplicate-key JSON rejection follow ADR-025.

NEAR TEE is a typed provider adapter of the existing model fabric
(`stream_completion_with_fallback` / `completion_with_fallback` → selector →
`remote_inference_admission_broker`). There is one shared broker, in-flight 1,
priority (chat before background), original deadlines, cancellation,
idempotency, reservations and the existing ledger. No fallback between slots,
models or providers, and no new lane, queue, budget, ledger or Cordis package.

### Request contract and trust disclosure

- Send the stored canonical model id with header `x-no-aliasing: true`; a request
  built without it is rejected before contact.
- The response `model` must equal the canonical id; a mismatch fails the
  operation (contact happened, so cost is still settled), and no output is adopted.
- `reasoning_content` (or an equivalent reasoning field or stream delta) is
  never answer text: it is dropped from buffers, streams, artifacts, memory,
  logs and receipts. A response with reasoning but empty answer content is an
  empty output. Reasoning tokens count against max output tokens and the cost
  bound. A reasoning control parameter is sent only when source-attested for
  that model; otherwise none is sent.
- Disclosure in settings, receipts and report labels: `TEE-hosted per NEAR
  catalog as of <receipt time>; NEAR gateway receives plaintext; attestation
  not verified by Seraph; no end-to-end encryption`. Never "verified" or
  "confidential". `tee_verified=false` and `e2ee=false` remain on every NEAR
  projection, including near_text; the separate `tee_hosted_per_catalog` label
  with its `as_of` time is the only TEE statement.

### Catalog check

`POST /api/settings/model-fabric/near-tee/catalog-check` is an explicit,
authenticated settings action bound to the current configuration revision. It
reads only `GET https://cloud-api.near.ai/v1/model/{id}` for each enabled
slot's model, makes no inference and sends no operator content; it sends the
NEAR credential only if source evidence shows the endpoint requires it. Settings
GET and PUT stay local and never read the catalog.

It writes a content-safe `seraph.near.tee.catalog.receipt.v1` per slot: slot,
canonical id, complete profile hash, `checked_at`, the fields above (provider
type, attestation/verifiable flags, readiness, modalities, features, prices,
context), response SHA-256 and pass/fail reason. A pass requires `vllm`,
attestation and verifiable true, readiness true, the slot's modality and
features (readiness: see the amendment below), and `request_cost_bound_microusd >= max_input_tokens x input price +
max_output_tokens x output price` at the receipt prices (reasoning tokens count
within `max_output_tokens`). Precontact dispatch re-validates locally (receipt
passing, same id, profile hash and configuration revision); otherwise the slot
blocks with `near_tee_catalog_receipt_stale` or the failure reason. Dispatch
never refreshes the catalog. The receipt is part of the slot's one-time setup
gate, not a capability proof, and does not expire by age (amendment
2026-11-14, #1178, below; it replaced an operator-set 1–24 h TTL). Tests
intercept this transport.

**Amendment 2026-11-14 ([#1167](https://github.com/seraph-quest/seraph/issues/1167), operator direction).**
NEAR's record for `deepseek-ai/DeepSeek-V4.1-Flash-1017` (read 2026-11-14)
omits `isReady` and lists no `structured_outputs`, while the operator's live
probe the same day showed chat, strict json_schema output, tool calling and a
`/v1/attestation/report` with gateway and model attestations. Therefore:

- Readiness: `isReady: true` passes; an explicit `isReady: false` fails; an
  **absent** `isReady` (an explicit JSON `null` counts as absent) passes only
  when the record is otherwise eligible
  (`providerType=vllm`, `attestationSupported` true, `verifiable` true). The
  receipt records `is_ready` as read and `readiness_basis`
  (`catalog_ready` or `absent_flag_attested`), and Settings says readiness was
  inferred. This applies to every slot, the embedding slot included (its
  evidence gate is unchanged).
- Structured output: the catalog's `structured_outputs` is recorded as a fact,
  not a capability. Callers needing structured output keep needing a current
  `structured_output` proof at dispatch (bound to the exact profile; never
  aged out since #1178). The vision slot sends json_schema when
  the catalog lists the feature or, when it omits it, a current passing
  json_schema `structured_output` proof on the exact profile exists and no
  newer failed json_schema check contradicts it; otherwise it uses strictly
  validated prompt-JSON. No proof and no feature means the capability is
  unavailable.
- Reasoning tokens count against `max_output_tokens`: capability checks of a
  model whose receipt lists `reasoning` send up to 1024 output tokens (bounded
  by the slot), Settings recommends a maximum output of at least 4096 for such
  models, and an answer ended by the output bound without content, or a JSON
  answer ended by it, is `near_tee_output_truncated` (cost settled, nothing
  adopted).
- Every other gate (model echo, `x-no-aliasing`, billing readback, Unknown
  liability, cost bound, modality, context, output limit) is unchanged.

**Amendment 2026-11-14 ([#1178](https://github.com/seraph-quest/seraph/issues/1178), operator decision).**
The operator directed that no time-based guard may block chat: a slot that
passed its catalog check and capability checks was blocked within 24 hours
although nothing had changed, which the operator called an outage. A model
does not suddenly lose abilities, and real failures (model removed or swapped,
outage) are caught on every request by the model echo check and the request
failing. Therefore:

- The explicit catalog check and the capability checks are a **one-time
  setup gate**, required when a slot is first set up and again only when its
  binding changes. The catalog receipt is bound to the canonical model, the
  complete profile hash (model and slot settings) and the configuration
  revision (every settings save); a capability proof is bound to the
  complete profile hash and the NEAR credential (a new key requires the checks
  again). Neither expires by age. Stored receipts that carry an `expires_at`
  and stored proofs with an `expires_at` stay valid while their binding
  matches; the capability-check `proof_ttl_seconds` field is still accepted
  and stored but is advisory for NEAR TEE slot profiles. Other profiles keep
  their proof expiry.
- A slot whose checks failed is not set up. A later failed operator-run check
  does not withdraw an earlier pass, except the #1167 rule above: a newer
  failed json_schema check withdraws the vision slot's proof-based json_schema
  mode.
- Seraph re-reads NEAR's public catalog record in the background (no key, no
  operator content, no consent, no broker lease; the catalog check's own
  timeout and byte cap) shortly after startup and then every
  `min(catalog_ttl_hours, 6)` hours; `catalog_ttl_hours` is now this refresh
  cadence hint, not an expiry. Background reads are **informational only**.
  They are stored apart from the setup receipts, update the slot's "last
  confirmed" time (and the TEE label's "as of" time), and never block
  dispatch, even when NEAR reports the model not ready, removed, not attested,
  not verifiable, repriced above the bound or with a modality or context that
  no longer fits: Settings then shows a warning ("NEAR catalog reports
  &lt;model&gt; not ready since &lt;time&gt;"). A failed read (network error, HTTP
  status, unparseable record) changes nothing and is shown as a failed
  background read; the last confirmation stays in force.
- Every other gate is unchanged: model echo, `x-no-aliasing`, billing
  readback, Unknown liability, cost bound, consent and the serial lane.
  ADR-036 is unaffected.

### Capability proofs kept

The existing manual capability proof endpoint (`POST /api/settings/model-fabric/canary`,
complete-profile-hash key; for NEAR TEE slots, credential-bound and without
age expiry since the #1178 amendment above) extends to NEAR TEE slot profiles; new
operator-facing labels call it a "capability check". After merge, the operator
may run separately consented, cost-bounded per-slot **mechanics** checks for
`text`, `streaming`, `tool_use`, `structured_output`, `vision`, `embedding`,
`health` and `latency_ms`. They check request/response mechanics only: no
quality score, corpus, judge or comparison. `latency_ms` is checked only against
the slot's own timeout and is never ranked across models. They are not
evals, are never a merge, release or code-activation gate, and run only through
the broker and ledger. As under ADR-006/024, a missing proof (or, for NEAR TEE
slots, one whose binding changed) blocks only the dependent workload at
dispatch; a NEAR TEE proof is never stale by age (#1178). Implementation tests use intercepted
transport only.

### Billing

Existing broker semantics are kept; there is no broker redesign and no new
async settler. Reserve before contact at the slot's request cost bound. NEAR
`/v1/billing/costs` readback runs inside the original admitted lease with
ADR-025's identity, parsing and bounds (at most two attempts, 1s apart, total
15s or less, within the original deadline), for non-streaming and streaming
requests alike; for streaming it runs after the stream ends, and deltas already
delivered are never retracted. A streaming request reserves the billing window
out of its original deadline: the stream is ended or cancelled no later than
16s before the deadline so readback can still run (tested). There is no
token-price estimate and no inference replay.

If no authoritative cost arrives, the operation becomes an Unknown liability
and, as today for uncertain outcomes, the serial lease stays blocked. Today
`uncertain_on_error` is set only for `openrouter` and `near.text`
(`execution.py`); PR3 extends it to the NEAR TEE provider kind, with a negative
test that a NEAR TEE contact error keeps the lease blocked. The lease stays blocked until the operator settles it through the existing fenced
reconcile or manual-settlement path. Operator impact: one Unknown NEAR cost
blocks all inference, chat included, until settled; the UI must surface the
blocked lane, the liability and the recovery action. `reconcile_settled_near_operation`
is not a billing settler: it is operator-triggered lane recovery after an
already-settled row, today bound to `near_text_native`, and is generalized to
NEAR TEE runtime paths for that recovery role only. Non-streaming routes follow
their existing adoption rule; near_text keeps ADR-025's withhold-on-Unknown rule.

### Lane configuration

Lane limits (max in-flight 1, queue and owner bounds, reserve review) and the
shared spend ceiling move to a provider-neutral `inference_lane` object outside
`openrouter_setup`. They bind the same deployment ledger and witness; moving them
is one explicit witnessed save that preserves revision, ledger and liabilities.
NEAR text and NEAR TEE mirror the lane ceiling, never fund their own.

### Credential and consent

- NEAR TEE uses its own `vault:near_api_key`: write-only, fingerprinted, no
  environment or cross-provider fallback. The near_text key is copied only by an
  explicit operator settings action.
- **near_text decision: keep, then fold by explicit action.** `inference.near-text.v1`
  stays a separate native Work capability with its fixed `z-ai/glm-5.3-flash`
  model, `vault:near_text_api_key`, receipts and ADR-025 output rules. PR3b
  rebases it on the shared NEAR client, which adds `x-no-aliasing`, the echo
  check and a catalog receipt gate bound to profile `near.text` and its profile
  hash. near_text stays excluded from the generic capability-check endpoint
  (ADR-025) and keeps reading billing inside its own lease. Folding it into the text slot
  happens only by explicit operator action under a tracked ticket. Rationale: it
  is a shipped Work-review surface with stricter adoption rules than the
  general text slot; folding now would remove behavior without operator request.
- Each slot needs a revision-bound purpose consent with an explicit
  plaintext-to-NEAR acknowledgment, published through ADR-024's revoked
  intermediate (r+1) and active final (r+2) CAS protocol. Global revoke covers
  NEAR TEE, near_text and any retained OpenRouter state; no save resurrects
  revoked consent.

### OpenRouter retirement by policy

From PR3a, no route makes new OpenRouter contact: selector, caller context,
effective policy, admission, the capability-proof endpoint, research and
planner callers, and the daemon. PR3a also publishes a route-scoped revoke
of the stored OpenRouter routes, so an older binary cannot resume contact; it
does not fence NEAR and does not block OpenRouter liability settlement. Settings
shows OpenRouter as "retired". Its configuration, credential, receipts, ledger
rows, billing readback and settlement stay readable so contacted liabilities
reconcile. Code removal is a later tracked ticket once OpenRouter liabilities
reach zero. Reactivating OpenRouter or any other provider needs a superseding ADR.

### Embeddings

The NEAR embedding slot targets a new namespace through the existing
`EmbeddingMetadata.namespace` (provider, model, measured dimension, schema).
Old OpenRouter vectors are never mixed or queried with a NEAR model. Re-indexing
is an operator-started job with a finite item count and spend bound, background
priority on the shared broker, checkpointed, cancellable and resumable, never
automatic. Until it completes, recall degrades to clearly labeled lexical search.

### Screenshots and the daemon

Daemon OCR providers that call a remote model (`openrouter`, and `codex-local`,
which drives a cloud-backed Codex CLI) are removed. Removing `codex-local`
drops its no-API-price Codex path; the operator must acknowledge that loss
before PR4 lands. The operator acknowledged the removal on 2026-11-13
("codex-local goes away"); PR4 removes it. Apple Vision on-device OCR
stays. Captures reach the backend over an authenticated local upload endpoint
that keeps app name, window title, capture time and content hash, with atomic
writes, existing dedupe and retention; there is no folder handoff and the
daemon makes no cloud call. The backend analyzes captures only through the
governed NEAR vision slot under existing screenshot consent. Screen-derived
decisions and report model labels use the effective route and catalog receipt
time, not provider-specific gates.

### Audio (ADR-033)

The OpenRouter setup.v3 audio route is never activated; audio stays the
explicit "Voice route unavailable" state. A later NEAR TEE audio slot for
`openai/whisper-large-v3` requires its own ticket amending this ADR with
source-attested endpoint, codec and pricing evidence. ADR-033's quarantine,
transcript-correction, consent and separate Task allowance invariants carry over.

### Cordis fit and rollback

Cordis `seraph.inference.v1` and guardian/agent turns keep forwarding to the
Python fabric; provider identity is not part of `inference.request`, so Cordis
contracts are unchanged. The adapter is typed, has no import-time side effects
and an owner-declared lifecycle, per ADR-026.

Rollback disables NEAR TEE slots by settings save: inference blocks or degrades
visibly and the local core stays usable. Rollback never re-enables OpenRouter.
Audit, reservations and Unknown liabilities are retained; contacted work is not
replayed. Embedding rollback follows ADR-024's namespace rules. A binary that
cannot read the new configuration stays blocked until an explicit sanitized
restore. Reverting this document restores ADR-006/024 text only through a new
reviewed superseding change.

## Verification

Docs: both docs scripts, docs typecheck/build, path/link and contradiction
checks, independent Critic review. Implementation slices intercept all NEAR
HTTP, including the catalog check, and make no live or paid calls. Negative
tests: a failed or rebound catalog receipt blocks the slot (age does not,
#1178); model echo mismatch;
missing `x-no-aliasing`; non-allowlisted model; zero OpenRouter contact on all
routes; daemon makes no cloud call; global revoke blocks NEAR; missing billing
becomes Unknown and keeps the lease blocked until fenced settlement, with the
blocked state visible; billing readback stays within the original lease and
deadline; input over `max_input_tokens` is rejected before contact; serial bound 1 with
chat priority; reasoning content never reaches answer text. A keyless managed
run shows NEAR disabled. Live NEAR use comes only after merge with explicit
operator approval.

## Consequences

The NEAR gateway sees plaintext for every inference purpose; the TEE label is
NEAR's catalog claim at a dated time, not Seraph-verified confidentiality.
Since the #1178 amendment a set-up slot is never blocked by the age of its
catalog receipt or capability checks; a model NEAR withdraws shows as a
Settings warning and fails at request time (echo or HTTP error) rather than
being blocked in advance. Model choice narrows to the allowlist; since the 2026-11-14 amendment DeepSeek
passes the catalog check with inferred readiness, and its structured output
rests on capability proofs, not catalog metadata. One Unknown NEAR cost blocks all inference,
chat included, until the operator settles it. Semantic recall is lexical until re-indexing.
Voice stays unavailable. OpenRouter code remains until its liabilities clear.
STATUS and the Current App Guide change only as slices merge.

## Inspected sources

- `develop` @ `70673963`: `backend/src/model_fabric/{configuration,near_text,accounting}.py`,
  `backend/src/api/model_fabric_settings.py` (canary endpoint, proof capabilities),
  `backend/src/llm_runtime.py`, `daemon/seraph_daemon.py`, `daemon/ocr/{openrouter,codex_local,apple_vision}.py`,
  `runtime/cordis/src/plugins/index.ts`.
- NEAR catalog `https://cloud-api.near.ai/v1/model/{id}`, read by the lead on
  2026-11-13 (not re-fetched for this ADR).
- Plan and critic dispositions for #1147, 2026-11-13.
