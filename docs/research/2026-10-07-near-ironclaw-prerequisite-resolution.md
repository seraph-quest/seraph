# NEAR / IronClaw prerequisite resolution — 7 October 2026

Research evidence for [#959](https://github.com/seraph-quest/seraph/issues/959), observed on **2026-10-07**, under the [documentation contract](/docs-contract) and [strategy claim ledger](19-strategy-claim-ledger.md). **#959 remains blocked:** artifact packaging and dependency resolution are feasible, and the fixed model's protocol is documented, but an approved trust policy, authentic accepted positive corpus, full dependency/runtime review and provider exception remain unfinished. This record does not adopt an SDK, trust value or provider target. [ADR-006](/decisions/openrouter-only-inference-phase) and [ADR-024](/decisions/purpose-specific-openrouter-routes) retain the governed OpenRouter routes; the proposed ADR025 exception requires separate review and adoption.

## What IronClaw actually supplies

The audit covers two distinct official histories, both retrieved on October 7:

| Snapshot | Immutable source | Date |
| --- | --- | --- |
| Current `main` | [b0b999d96781516ee05e6ba961d6f3ead900da96](https://github.com/nearai/ironclaw/commit/b0b999d96781516ee05e6ba961d6f3ead900da96) | Committed September 10 |
| Latest published `ironclaw-v1.4.1` | [b011eb3932a0e1ab8bbe833893599ca9ae4925c5](https://github.com/nearai/ironclaw/commit/b011eb3932a0e1ab8bbe833893599ca9ae4925c5) | [Published September 29](https://github.com/nearai/ironclaw/releases/tag/ironclaw-v1.4.1) |

Both implement API-key/session authentication, session renewal, model metadata and ordinary JSON Chat Completions over HTTPS with Bearer authorization. Their runtime default is `https://cloud-api.near.ai`; their default model is `deepseek-ai/DeepSeek-V4-Flash`, which cannot replace #959's fixed `z-ai/glm-5.3-flash`. The relevant send paths are [main](https://github.com/nearai/ironclaw/blob/b0b999d96781516ee05e6ba961d6f3ead900da96/crates/domains/ironclaw_llm/src/nearai_chat.rs#L397) and [release](https://github.com/nearai/ironclaw/blob/b011eb3932a0e1ab8bbe833893599ca9ae4925c5/crates/domains/ironclaw_llm/src/nearai_chat.rs#L313).

Neither inspected tree supplies the client inference verifier required by #959: E2EE, fresh quote/GPU checks, accepted deployment provenance, attested TLS/SPKI binding and exact-byte response verification before answer release are absent from those paths. The [TEE UI hook](https://github.com/nearai/ironclaw/blob/b011eb3932a0e1ab8bbe833893599ca9ae4925c5/crates/product/ironclaw_webui/frontend/src/hooks/useTeeAttestation.ts#L11), byte-identical in both snapshots, fetches hosting reports and displays fields. Its shield does not establish the inference guarantee. This conclusion concerns the inspected client trees; it does not assess independent remote-service assurance.

Endpoint/authentication setup and metadata handling are reusable concepts, subject to Seraph's existing Root, vault, consent, job and shared cost ownership. IronClaw's request logging, session resend, broader retries and URL/model overrides cannot be carried into the fixed private-text contract unchanged. No IronClaw deployment or dependency graph is required to use the separate NEAR inference SDK.

## Protocol support is documented; deployment acceptance still needs evidence

Official documentation read on October 7 describes the exact `z-ai/glm-5.3-flash` gateway path with E2EE and Ed25519 signing, `stream: false`, `x-no-aliasing`, model-report preflight and `provider_tee` response verification. This resolves the **documented protocol-support** question. The [E2EE guide](https://docs.near.ai/cloud/guides/e2ee-chat-completions), [model-attestation guide](https://docs.near.ai/cloud/verification/cloud-api/model-attestations) and [response-signature guide](https://docs.near.ai/cloud/verification/cloud-api/response-signatures) require verification of every candidate, selection of an accepted key, retention of exact request/response bytes and a unique response-signer match. Documentation examples are not authentic positive receipts or availability proof.

The guarantee must preserve its stated limits: a shared model key does not identify the exact serving instance, and the described signatures do not establish the complete model→gateway→final-response chain. Keep `exact_serving_instance_verified=false` and `complete_chain_verified=false`. Neither property is an additional prerequisite for #959's narrower target.

## Artifact and dependency progress

The official SDK source [b9930893a9f560e66898e1616111c5ac2241686c](https://github.com/nearai/inference-sdk/tree/b9930893a9f560e66898e1616111c5ac2241686c) differs from the published Python 0.1.0 source [6463614026ef594e815211d083865ef95e24a46e](https://github.com/nearai/inference-sdk/tree/6463614026ef594e815211d083865ef95e24a46e), including its structured verification result. A reviewed source-built distribution can satisfy artifact provenance; waiting for a matching upstream wheel is not inherently necessary. Its derivative identity, patch, build procedure, license and dependency closure must be explicit and approved.

An isolated candidate, `nearai-inference-sdk-seraph-candidate` version `0.1.0+seraph.b993.jwt215`, was packaged repeatedly with identical hashes on the recorded CPython 3.12.8/zlib 1.3.1 toolchain. All 39 implementation/typing files and the MIT license remain byte-identical to the pinned source. Metadata changes identify the derivative and replace the vulnerable exact `PyJWT[crypto]==2.14.0` requirement with `==2.15.0`. The trusted standard-library packager ran no downloaded build hooks; reproducibility is limited to that recorded toolchain and does not establish upstream PEP517 reproducibility.

| Candidate result | SHA256 |
| --- | --- |
| Wheel | `358011a4fd51cac370e060887a69d5306048d2178d490f0008bf4db4fa4a7784` |
| Combined candidate/backend lock | `90d942649f2c52c1b980081a41406b832a1e15a45f23dad932482ed6d83d004c` |

Actual `uv lock --no-build` resolution succeeded with 172 packages against the unchanged backend dependency baseline, adding the candidate and `multidict>=6.9.1`. It selected PyJWT 2.15.0, multidict 6.9.1, pyasn1 0.6.4, dcap-qvl 0.6.5 and Sigstore 4.5.0. Root independently checked the archive, RECORD, metadata, source bytes and solver result. Publisher advisories explain the dependency changes: [PyJWT parsing](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-42vr-xj54-vc7v), [PyJWT key handling](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-x33g-cr3x-6449) and [multidict](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925).

This establishes packaging and metadata compatibility only. No SDK was installed or imported. Native Linux/macOS compatibility, full transitive/Cargo vulnerability and license review, selected verifier/root approval, cryptographic behavior, connection binding, bounded retries and cancellation remain unproved. The candidate and lock are research inputs, outside the active product environment.

## Remaining work and owners

1. **Dependency/build owner:** review the derivative and complete the combined closure, advisory/license and isolated platform/import checks, including the newly selected DCAP version. Preserve the real SDK verifier and attested HTTP client; do not bypass dependencies or substitute a generic client. The [pinned verification guide](https://github.com/nearai/inference-sdk/blob/b9930893a9f560e66898e1616111c5ac2241686c/py/docs/verification-guide.md) is the supported mechanism to assess.
2. **Deployment/security owner, with NEAR/NVIDIA evidence providers:** derive a finite accepted gateway/model image, measurement and build-identity policy from authentic publisher evidence. Review every required compose service/image and signer/workflow/ref/source identity, UpToDate-only TCB, required GPU evidence, advisory rejection, authenticated collateral and freshness. An internal reviewed manifest is valid; accepting whatever a live deployment reports is not. Intel DER and authenticated Sigstore TUF identities can be pinned directly; NVIDIA's HTTPS/JWKS mechanism needs an explicit authenticated key-update/rotation policy rather than an invented static vendor version.
3. **Crypto owner:** finish real verification and negative mutations over authentic, redistributable evidence. Retained Intel and Sigstore samples support component work, with partial checks clearly distinguished from full appraisal. The historical NVIDIA token lacks an authenticated retained matching key. Its signed claims cannot substitute for signature verification. Under current #959 §9/10, the positive journey still needs a coherent authentic corpus binding the accepted deployment, model key, connection and encrypted request/response. Unrelated authentic components cannot be joined into that receipt.
4. **Lead and independent security/critic:** review artifact/lock/trust-manifest representations and the proposed ADR025 exception; satisfy all #959 §2 prerequisites and its existing dependency gate before product implementation. A reviewed amendment may separate component evidence from a still-blocked journey, but research cannot silently change acceptance. Subsequent implementation must retain Seraph's existing admission/accounting owners and remain disabled until separately authorized activation.

Authentic historical fixtures are acceptable for offline cryptographic checks at an explicit historical validation time, preserving original signed bytes and redistribution provenance. They must separately fail current-time freshness where expired. This does not weaken operational nonce, collateral, JWT or Root expiry. The unresolved coherent corpus and authentic deployment/key facts are evidence gaps; packaging, policy composition and verifier tests are internal engineering work. None requires an invented complete-chain or exact-instance guarantee.

## Retained review evidence

The private research receipts below were hash-checked for this record. They retain source inventories and download provenance; raw quotes, bundles and payloads are not reproduced here. The October 7 official-doc read supersedes earlier refresh failures recorded by the trust worker.

| Receipt | SHA256 |
| --- | --- |
| `IRONCLAW-SOURCE-AUDIT.md` | `bb6e9dbdde83a81fec7f9c765a8e6e05367b36fcbd2d5774f5b54042ddd59223` |
| `SDK-RESOLUTION.md` | `ffac02ad5fd286c6c833d9869c7f67a680ddf116f79b7576ca17960264cb0270` |
| `TRUST-RESOLUTION.md` | `16751e29f56e41cf587bb3172ea685db266bdd2938896588344ea33e790f2802` |
| `CANDIDATE-RESULT.md` | `dcad507aaeb33243d1b9d1d3f83165141dda04b274f6b44bebff97438db48900` |
| `packager-toolchain.json` | `0461e4b935e491565792b845648658e1d628853d1926dd1b1425c8912802a60d` |
| Candidate `HASH-MANIFEST.json` | `1b49b9c02133eb53fb1074aedbe4ff73029f2bc695c430873e5c34a9df756d3f` |
| `ROOT-CANDIDATE-READBACK.json` | `06dfe5dfc544bba699853e66170cbc7d94c5a99655d3c17387582e20e908f0ce` |

Research used public source/documentation and package metadata downloads. No credentials, provider inference, operational attestation/NRAS contact, product change or evaluation campaign was used to establish these findings. No serving-model availability, verified answer, operational trust adoption or shipped capability is claimed. The [earlier trust-boundary study](near-ai-trust-boundary-2026-10-05.md) remains background evidence, qualified by the concrete progress and remaining gaps above.
