---
id: near-ai-trust-boundary-2026-10-05
slug: /near-ai-trust-boundary-2026-10-05
title: NEAR AI trust boundary — 5 October 2026
---

# NEAR AI trust boundary

**Research date:** 5 October 2026, Europe/Warsaw. **Owning request:** [#954](https://github.com/seraph-quest/seraph/issues/954). This research does not change [ADR-006](/decisions/openrouter-only-inference-phase). The original conditional M5 proposal required a reviewed exception before direct NEAR execution.

**October 7, 2026 disposition:** The operator revised #959 to standard NEAR over
HTTPS with provider plaintext disclosure and no verified-TEE claim. Proposed
[ADR-025](/decisions/near-https-text-inference) and the
[revised M5 target](/guardian-capability-roadmap#m5-verified-near-inference)
own that narrow scope. The dated research and original blocked TEE prerequisites
below remain historical evidence, not current #959 implementation requirements.

NEAR has an official client verification protocol and Python SDK. Seraph currently has no implemented NEAR attestation verifier. Selecting a NEAR upstream through OpenRouter would not establish the same protection as a client-verified direct NEAR Cloud gateway request.

## Five different support levels

| Level | Seraph answer on the inspected baseline | Evidence and limit |
| --- | --- | --- |
| 1. Direct NEAR inference | Not supported by the active OpenRouter-only phase. No direct NEAR adapter/configuration/credential setup exists in the inspected path. | [`selector.py`](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/selector.py#L47) permits only the active `openrouter` provider; [ADR-006](/decisions/openrouter-only-inference-phase) rejects arbitrary compatible endpoints. |
| 2. Reach a NEAR upstream through OpenRouter | Technically expressible through the generic governed route; actual configured/live NEAR execution in Seraph was not established. | [`selector.py`](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/selector.py#L80) fixes OpenRouter's destination and requires explicit upstream policy. [OpenRouter's public endpoint](https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints) lists `near-ai/fp8`. |
| 3. Inference inside an attested TEE | NEAR documents an attested model fleet, and a NEAR upstream can be selected through OpenRouter. Whether any actual Seraph request used an attested deployment was not established; Seraph cannot currently verify that claim. | [NEAR private inference architecture](https://docs.near.ai/cloud/private-inference) and [model verification protocol](https://docs.near.ai/cloud/verification/cloud-api/model-attestations) describe the provider boundary. OpenRouter selection/HTTPS does not deliver client verification. |
| 4. Verify inference TEE evidence in Seraph | Not implemented. No NEAR quote, gateway SPKI, E2EE or exact response-signature verification path was found in the model selector/configuration/execution and transport surfaces. | [`execution.py`](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/execution.py#L1), [`llm_runtime.py`](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/llm_runtime.py#L420). Existing admission/proof receipts are not hardware attestation. |
| 5. Host Seraph agent execution and state inside a TEE | Not established or provided by the proposed inference integration. | [Constitution](/) keeps model providers inference-only and canonical state host-local. Inference attestation does not relocate goals, tools, memory, credentials or scheduler execution. |

The repository baseline and its release boundaries are recorded in the [shared capability baseline](./seraph-capability-baseline-2026-10-05.md). No credentials, configuration or inference were changed during this research.

## Three boundaries that must stay separate

**Inference TEE:** NEAR's own model fleet uses Intel TDX confidential VMs and NVIDIA confidential GPU execution. Its Cloud gateway runs in a separate TEE. Third-party models proxied through the same gateway do not inherit NEAR model privacy guarantees. [Official architecture](https://docs.near.ai/cloud/private-inference), [model distinction](https://docs.near.ai/cloud/models).

**Client verification:** a client must verify evidence under an explicit acceptance policy and bind it to its connection and response. Standard HTTPS, an OpenAI-compatible interface, a `verifiable` catalog field and zero-retention branding do not perform those checks. [Verification policy](https://docs.near.ai/cloud/verification/reference/verification-policy).

**Agent hosting/state:** Seraph's host sees plaintext before encryption and after decryption. Its UI, local database, canonical memory, workflow artifacts, tool inputs/results and any local logs remain outside the model TEE. External integrations see the data Seraph sends them. A compromised client or host remains outside the inference guarantee. Hosting the agent inside a confidential VM would require a separate deployment, storage/key and attestation contract.

## Routes, models and prices observed

These are public metadata observations on 5 October, not inference tests, commitments or performance benchmarks.

| Route | Observed support | Operational boundary |
| --- | --- | --- |
| NEAR Cloud gateway, `https://cloud-api.near.ai/v1` | API-key authenticated OpenAI-compatible API and verification endpoints. | Preferred official route for new integrations. Attestation report retrieval requires a key but is documented as non-billable. |
| OpenRouter, `https://openrouter.ai/api/v1` | Provider slug `near-ai`; GLM 5.3 Flash endpoint `near-ai/fp8`, tools/tool-choice support and FP8 quantization. $0.105/M input, $0.35/M output, $0.0245/M cached input at the observed discount. | NEAR upstream availability is confirmed by [provider metadata](https://openrouter.ai/api/v1/providers) and [endpoint metadata](https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints). No official end-client attestation/E2EE forwarding contract was established through OpenRouter. Its TLS endpoint is separate. |
| Experimental model-direct `https://{slug}.completions.near.ai/v1` | Bypasses the gateway; experimental direct SDK exists. | [Official docs](https://docs.near.ai/cloud/experimental/direct-completions) advise against new production verification. “Direct NEAR” in M5 means its Cloud gateway, not this experimental route. |

The exact [GLM metadata endpoint](https://cloud-api.near.ai/v1/model/z-ai%2Fglm-5.3-flash) returned `metadata.providerType=vllm`, `attestationSupported=true`, `verifiable=true`, `isReady=true`, text/image inputs, text output, tools, structured outputs and reasoning. Direct prices were $0.15/M input, $0.50/M output and $0.035/M cached input. These fields declare eligibility; actual fresh attestation still must pass. Its alias list includes earlier and different-family names, so canonical `z-ai/glm-5.3-flash` plus `x-no-aliasing: true` is required. The generic `/v1/models` response does not contain every field the verifier uses.

[Specialized endpoint docs](https://docs.near.ai/cloud/guides/specialized-endpoints) and the [live catalog](https://cloud-api.near.ai/v1/models) also expose Qwen vision, embedding and reranking, FLUX image generation, Whisper transcription and a dedicated privacy classifier. Endpoint existence and advertised TEE hosting do not establish the Python Chat verifier's support for all modalities. Initial M5 excludes those endpoints, images, audio, tools and streaming. Future modality claims need separate protocol and negative-test evidence.

## What the official verification protocol establishes

| Property | Required checks | What it does not establish |
| --- | --- | --- |
| Gateway deployment | Client-generated 32-byte nonce; verified Intel quote; TCB/advisory policy; debug-mode rejection; nonce/signer binding; replay event log to RTMR3; raw measured `app_compose` hash against MRCONFIGID; accepted workload/provenance policy. | Model execution evidence. [Gateway guide](https://docs.near.ai/cloud/verification/cloud-api/gateway-attestation). |
| Model deployments | Canonical ID, `provider=near`, no aliasing; nonempty `model_attestations[]`; verify every candidate, including applicable GPU evidence, freshness, measurements and provenance. | That the report identifies the exact serving instance for the subsequent request. [Model guide](https://docs.near.ai/cloud/verification/cloud-api/model-attestations). |
| Gateway connection | Read peer SPKI and attestation on the same TLS connection; verify quote-bound SHA-256 SPKI fingerprint; inference must use that connection or the SDK's attested identity pinning contract. Refresh on connection/identity changes. | Direct client-to-model TLS binding. Browser Fetch cannot expose peer certificates. [TLS guide](https://docs.near.ai/cloud/verification/cloud-api/tls). |
| Exact response | Retain exact request and uncompressed response bytes. Explicit algorithm. Fetch signature and verify its exact payload and retained preflight signer. | Response quality, harmlessness or usefulness. [Signature guide](https://docs.near.ai/cloud/verification/cloud-api/response-signatures). |
| Software provenance | Verify Sigstore/GitHub attestation subject equals immutable image digest; accept explicit repository, build identity, workflow, ref and source revision. | A reachable build record or successful HTTP fetch alone. [Provenance guide](https://docs.near.ai/cloud/verification/cloud-api/image-provenance). |

`provider_tee` signs `<MODEL_ID>:<request_hash>:<response_hash>` and must match exactly one retained, verified model signer and algorithm. `gateway` signs `<request_hash>:<response_hash>` and matches verified gateway evidence. They prove different properties. A successful HTTP response carrying an unavailable-signature error is not verified. Missing/unknown signature kind, malformed evidence, mismatch or absence after bounded retrieval remains unverified.

Two protocol limits are explicit in the official documentation: model instances can share a signing key despite differing measured configurations; and gateway/model preflight plus one response signature does not create a complete model-to-gateway-to-final-response chain. A gateway signature cannot prove an attested model generated the answer. A provider signature does not bind its bytes to the preflight gateway/TLS evidence. [Official gap #986](https://github.com/nearai/cloud-api/issues/986) tracks the missing chain. M5 must disclose these limitations and cannot offer exact serving-instance provenance or a full-chain guarantee.

Streaming commonly produces a gateway signature because the gateway changes stream bytes. Resolving aliases and the Responses API also produce gateway signatures. Reconstructing JSON/SSE from parsed content invalidates exact-byte verification. This makes nonstreaming, canonical-ID Chat the smallest clear initial scope.

## Pinned SDK and encryption contract

Inspected official SDK commit **`b9930893a9f560e66898e1616111c5ac2241686c`**, committer **1 October 2026 05:49:09 UTC**. Python project **`nearai-inference-sdk` 0.1.0**, requires **Python 3.12+**. This identifies source, not a verified published package digest. Dependencies include pinned `httpcore==1.0.9` for its TLS handshake hook, `dcap-qvl`, Sigstore, NVIDIA verifier support, cryptography and PyNaCl. [Package source](https://github.com/nearai/inference-sdk/blob/b9930893a9f560e66898e1616111c5ac2241686c/py/pyproject.toml).

The [pinned API reference](https://github.com/nearai/inference-sdk/blob/b9930893a9f560e66898e1616111c5ac2241686c/py/docs/api-reference.md) and [verification guide](https://github.com/nearai/inference-sdk/blob/b9930893a9f560e66898e1616111c5ac2241686c/py/docs/verification-guide.md) define:

- `InferenceClient(api_key, base_url="https://cloud-api.near.ai/v1", signing_algo="ed25519", e2ee=True, attestation_cache_time_to_live_ms=0, gateway_verification=..., model_verification=..., deployment_policy=...)`;
- `GatewayVerificationOptions(include_spki_fingerprint=True, policy=..., verifiers=...)` and `ModelVerificationOptions(policy=..., verifiers=...)`;
- `await client.verify(model)` for preflight; `await client.chat.completions.create(...)` for Chat; and **explicit** `await client.verify_response(completion.id)` for the final exact-byte signature;
- `AttestationClient.fetch_model_metadata(model)` for the exact catalog metadata, rather than interpreting `/v1/models` as cryptographic proof.

Gateway verification always runs. `vllm` plus attestation support requires every returned NEAR model report to pass. Other providers use gateway-only Incognito mode; setting E2EE or a model policy rejects that mode. Invalid metadata or failed model checks do not silently downgrade.

The gateway SDK defaults E2EE **off**, TCB acceptance includes both `UpToDate` and `OutOfDate`, GPU verification is only mandatory when policy requires it, and successful attestation can be cached for 60 minutes. These defaults are not Seraph's proposed trust policy. The M5 contract requires `UpToDate`, required GPU evidence, explicit accepted deployment policies, E2EE and fresh-request verification. Exact advisory/provenance policy and accepted pins remain unresolved prerequisites.

E2EE v2 uses Ed25519-to-X25519 conversion, ECDH, HKDF-SHA256 and XChaCha20-Poly1305. The model encryption key comes only from accepted attestation, with `X-Model-Pub-Key` as routing pin and no aliasing. E2EE is required to exclude plaintext message content at the gateway; it does not conceal all metadata, token counts, model IDs, traffic timing or the client's network address. Tool definitions/calls require additional all-fields encryption and are excluded initially. OHTTP is a separate optional protocol and is also excluded initially. [Encryption contract](https://docs.near.ai/cloud/guides/e2ee-chat-completions).

The SDK yields decrypted content **before** explicit response-signature verification. Seraph must hold it in memory and expose no answer, memory update or tool effect until verification succeeds. M5 requires `provider_tee`; a valid gateway-only signature remains insufficient for that target.

## Conditional implementation and external-use gates

M5 is **blocked work**, not a shipped capability. Before implementation is unblocked, the reviewed ADR exception must fix the SDK artifact/hash, model ID, accepted gateway/model measurements and immutable images, source/build trust pins, verifier/trust-root versions, advisory policy, exact receipt schema, protocol fixture provenance and failure contract. No implementation agent may choose these trust facts or replace missing pins with permissive defaults.

Local merge acceptance can use replayable cryptographic fixtures and negative tests for missing evidence, debug/outdated TCB, GPU failures, nonce/signer/SPKI/provenance mismatch, aliases, altered bytes, unavailable signatures, cancellation and unknown-cost liability. Independent review must verify the fixtures and trust rules. A live paid request is not a merge prerequisite.

Operational use requires a separately authorized live fresh attestation and exact response-signature receipt for the approved route. Without credentials, authorized spend or accessible verification services, external operation remains unverified and visibly unavailable. Local fixture success cannot be reported as a live NEAR proof. Intel collateral, NVIDIA NRAS/JWKS and provenance retrieval add availability/latency dependencies; retries remain bounded, and unknown contacted inference retains its accounting liability.

This proposed route extends existing admission, durable jobs, approvals, accounting and operator status. It does not create another agent runtime, canonical memory store, scheduler or hosting plane.
