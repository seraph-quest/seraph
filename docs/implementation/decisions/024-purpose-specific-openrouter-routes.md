---
id: purpose-specific-openrouter-routes
title: "ADR-024: Purpose-specific OpenRouter routes"
---

# ADR-024: Purpose-specific OpenRouter routes

**Status:** Target proposed for independent review under [#954](https://github.com/seraph-quest/seraph/issues/954); not effective until the documentation PR is reviewed and merged to `develop`.

**Decision class:** Additive model-fabric setup contract. ADR-006 remains the active provider boundary.

## Context

At `0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a`,
`configuration.py:validate_openrouter_setup` rejects multiple model IDs and
rejects embedding together with other capabilities. The lower-level model
fabric already has provider profiles and workload policies. This is a usable
setup limitation, not evidence that the selector has no multi-profile design.
An operator should retain chat while selecting a compatible vision or embedding
model. No new provider, router service, automatic model search or fallback is needed.

## Decision

Keep FastAPI, Pydantic, SQLModel/SQLite, the existing configuration file repository,
encrypted vault, profile/proof repositories, broker and accounting. No new service
or dependency. Extend `OpenRouterSetup` to version
`seraph.openrouter.setup.v2` with exactly three named route slots:

| Slot | Fixed profile ID | Workload binding | Required adapter and capabilities |
| --- | --- | --- | --- |
| `text` | `openrouter.text` | interactive chat, agent/strategist reasoning, report and memory synthesis | `openai_compatible_chat`, `text`; each caller additionally requires tools/streaming/structured output when actually used |
| `vision` | `openrouter.vision` | screenshot/vision analysis only | `openai_compatible_chat`, `text,vision` |
| `embedding` | `openrouter.embedding` | embedding only | `openai_compatible_embeddings`, `embedding` |

Slots may be absent/disabled independently. One exact qualified provider/model
per slot; the same model may occupy text and vision but proofs remain separately
bound to complete profile hashes. Map `interactive_chat`, `agent_reasoning`,
`report_synthesis`, `memory_synthesis` to text; `vision_analysis` to vision;
`memory_embedding` to embedding in one function `route_slot_for_task_class`.
The verified `CANONICAL_ROUTE_SPECS` includes six specialist routes and dynamic
`mcp_` specialists which resolve to `interactive_chat`; preserve that validation.
Unknown classes fail with
`route_slot_unmapped`, never text by default. The ticket includes the inspected
inventory and requires stopping if an unlisted active caller is found.

`routes` is a closed object. Each route has `model_id` (existing normalized ID),
`enabled:bool`, `capabilities:list`, `allowed_upstreams:list`, `temperature`,
`max_output_tokens`, `timeout_seconds`, `zero_data_retention`,
`request_cost_bound_microusd:int`. Retain existing validated bounds, exact
`https://openrouter.ai/api/v1`, deny retention/collection, no redirects/fallbacks,
`require_parameters=true`. Vision/embedding require ZDR and separate explicit
purpose consent. Key/credential reference is shared and write-only; one vault
entry, no per-slot keys. Cost ceiling, witness, queue bounds and max-inflight=1
are deployment-wide outside `routes`; changing slots never funds another ceiling.
No model IDs or upstream identities are invented or shipped as enabled defaults.

Example (abbreviated for readability, not a complete valid request):

```json
{"schema_version":"seraph.openrouter.setup.v2","routes":{"text":{"model_id":"operator/provider-model","enabled":true},"vision":null,"embedding":null},"max_inflight":1}
```

The literal example model is a placeholder and must be rejected if not a real
operator-selected catalog identity. Existing request bounds and required fields
remain mandatory; clients cannot submit this abbreviated example as configuration.

### API and compatibility

Keep the existing `/api/settings/model-fabric` route and its `openrouter_setup` payload;
extend their existing request/response types, not a parallel settings service.
GET projects schema v2 plus per-slot `configuration_required|blocked|ready`,
reason codes, effective model/upstream policy, capability proof expiry and
shared budget/queue state. No GET probes providers. PUT requires exact current
configuration revision and existing literal cloud-egress acknowledgment, plus
`vision_egress_acknowledged` and `embedding_egress_acknowledged` only for newly
enabled/changed corresponding slots. They are stored as revision-bound purpose
consents. Disabling/revoking a slot rejects new work immediately and preserves
contacted liabilities; no queued work may silently move to another slot.

Read v1 unchanged through a pure migration function. A single embedding-only
v1 profile maps to embedding; a non-embedding profile maps to text, and its
vision capability maps to the same exact model in vision only when the v1
policy includes the already-recorded vision consent and ZDR. Missing consent
leaves vision disabled. Preserve old revision, key ref, budgets and original
egress state; migration never contacts a model, enables a new purpose, seeds
proofs, rewrites old receipts or imports historical screenshots. Persist v2
only in one explicit reviewed save. Legacy PUT remains accepted only while the
stored schema is v1; after v2 it returns 409 `setup_schema_upgrade_required`
rather than dropping other slots. Keep a sanitized v1 rollback snapshot.

### Witnessed fail-closed publication

V2 saves do not claim cross-store atomicity. Validate the whole target first and
stage any new credential only in a validated in-memory secret buffer outside
configuration/accounting writers. Staging mutates neither the live vault nor the
process key; secret bytes are excluded from logs, configuration and checkpoints.
Require the exact
current `egress_revision` using compare-and-swap (CAS). Extend existing
`accounting_witness._publish_policy_locked` with exact expected-revision CAS;
keep its existing credential-free policy checkpoint and lifecycle witness.

For a save whose accepted current revision is `r`:

1. Publish the complete target configuration with `egress_revoked=true` at
   `r+1` through the existing witnessed policy publication. A genuinely empty
   keyless configuration uses the existing bootstrap path. This intermediate
   revision is visibly blocked and authorizes no new provider contact.
2. Only after intermediate revocation is witnessed, install the new key through
   existing `_store_setup_credential`. Then configure the existing inference-
   accounting ceiling and explicit reserve review. Existing contacted requests
   retain their captured credential; all new precontact requests remain blocked.
   Preserve existing costs and Unknown liabilities.
3. Publish the same target active at `r+2` only if CAS still matches the exact
   intermediate revoked revision, accounting continuity is valid, and readback
   proves the expected ceiling and reserve review. Purpose consent binds the
   reviewed target and its final active revision; intermediate publication
   cannot independently enable a purpose.

A concurrent save that loses CAS returns HTTP409. A fault, crash or lost
continuation leaves the configuration revoked or continuity-degraded; GET must
not project a mixed revision as ready. Existing policy/accounting checkpoints
support operator reconciliation, followed by an explicit re-save. Neither
reconciliation nor restart automatically activates or replays the save. Do not
introduce a second ledger or filesystem/network I/O in pure DB callbacks.

If final-publication outcome is uncertain, re-read configuration and witness
before compensating a staged credential. Preserve the staged key when the
exact target is active. Otherwise restore the prior key only under revoked
state and report the failure. Do not restore an active old policy automatically.
Queued requests retain exact profile hash, slot and epoch; precontact always
rechecks the current witnessed active policy and drift blocks without rerouting.
Already contacted work settles original usage/Unknown debt under the original
route. Cancellation and retry retain original IDs, deadlines and reservation
bounds.

### Proof and embedding namespaces

Capability proofs are not hardware attestations. Reuse the existing complete
profile-hash proof key, including slot identity, model, endpoint, adapter,
required parameters and security policy, plus proof expiry. Do not add the
global policy revision to that proof key. An unchanged text profile retains its
valid proof after a vision-only edit; changing a slot's complete profile hash
invalidates that slot's old proof. No fabricated proof migration or cross-slot
proof reuse is allowed, even for the same model. New contact separately checks
current global policy epoch and purpose consent; queued old-epoch requests block.
Missing/stale/failed proof
blocks only dependent workloads. Direct NEAR or any other endpoint is still
rejected by ADR-006. Slot readiness must never display verified TEE.

Reuse existing `EmbeddingMetadata.namespace`, whose identity hashes embedding
schema, provider, model and measured dimension, and the namespaced tables in
`memory/vector_store.py`. No generation pointer or separate switch API is added.
The saved embedding slot selects the target vector namespace once its exact
slot-bound proof and measured dimensions are valid. Every vector write rechecks
those bindings; it cannot mix a prior model or dimension into the target table.
Missing proof or a missing usable index yields lexical degraded retrieval, not
automatic reembedding or selection of an old namespace. Later explicitly
authorized indexing uses the new namespace through existing indexing work.
Rollback changes configuration selection only with current purpose consent,
valid slot proof, sources/tombstones and read authority; it never resurrects
deleted memory or old egress consent.

### Ownership and rollout

Modify verified files `backend/src/model_fabric/{configuration,selector,caller_context,effective_policy,repository,runtime_status}.py`,
`backend/src/api/model_fabric_settings.py`, `backend/src/llm_runtime.py`,
`backend/src/observer/screenshot_semantic_analysis.py`,
`backend/src/memory/{embedder,vector_store}.py`,
`backend/src/workflows/inference_accounting.py`,
`backend/src/workspace/accounting_witness.py`,
and `frontend/src/components/settings/OpenRouterSetupPanel.tsx` plus its shared
types in `frontend/src/lib/modelFabric.ts`.
The ticket's path inventory resolves these names before publication; changed
paths after this inspected revision are a blocker rather than permission to invent a parallel API.
No DB table is introduced for configuration. Extend stored route/proof metadata
only through existing versioned schemas.

Roll out with all new slots disabled unless exact v1 migration preserves prior
authority. UI editing text must leave other slot controls usable through partial
metadata failure. Rollback first disables v2-only slots and quiesces old work;
retain liability/witness rows and v2 metadata. An old binary unable to read v2
must be blocked until explicit sanitized v1 restoration, never ignore it.

## Verification

Prove one authenticated save, restart, independent configuration readback, text
and vision serialization and embedding-namespace isolation using intercepted
external transport. Show a text request still works mechanically while vision
has missing proof, and that a vision-only save preserves the valid unchanged
text-profile proof while blocking previously queued old-global-epoch requests.
Negative cases cover route confusion, direct-provider URL,
credential echo, consent revoke between queue/contact, stale epoch/proof,
simultaneous slot requests (at most one provider callback active), shared budget
exhaustion, Unknown costs, witnessed fail-closed save faults and populated v1
migration. Race concurrent saves at both publication steps; losing CAS returns
409, and faults after revocation remain blocked until explicit reconciliation
and re-save. Verify uncertain final-publication credential compensation from
configuration/witness readback rather than assuming publication failed.
Provider-free checks do not establish model usefulness, availability or TEE.

Record and resolve the current focused setup failures in the same milestone
before calling it complete; do not weaken assertions or authorize real provider
calls. New fields/tests are Planned until implemented. Required future local
and managed UI receipts and exact-pushed cumulative review are in the owning
milestone. This ADR introduces no claim of shipped behavior.
