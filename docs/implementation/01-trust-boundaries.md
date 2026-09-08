---
title: 01. Security, Trust, And Data Egress
---

# 01. Security, Trust, And Data Egress

**Document class:** Partial shipped truth and branch contract for
[#738](https://github.com/seraph-quest/seraph/issues/738).

**Threat-model date:** 2026-07-10.

**Target authority:** [Project Constitution](./00-project-constitution.md).

This document defines the shared security decision that Seraph ingress, model,
capability, artifact, secret, approval, audit, and recovery paths must adopt.
It distinguishes existing enforcement from metadata and deterministic fixtures.
It does **not** claim that authenticated LAN ingress, unified model egress,
artifact migration, or universal capability-host adoption is shipped.
Security and trust wording remains bounded by the
[Strategy Claim Ledger](/research/strategy-claim-ledger); this page claims only
the named covered paths and explicitly listed branch contract.

The machine-readable adoption inventory is
[`security-enforcement-coverage.json`](./security-enforcement-coverage.json).
That inventory must move a boundary to `enforced_in_738` only when an executable
choke point and focused negative test exist. Downstream adoption remains owned by
issues #740, #741, #742, #744, #747, and #754.

This branch includes the provider-neutral evaluator in
`backend/src/security/trust_contract.py` and its focused contract tests. It is an
enforceable shared decision primitive, not proof that existing runtime adapters
already invoke it.

## Security Objective

Seraph must make an explicit, versioned decision before data or authority crosses
a trust boundary. Missing identity, provenance, data classification, destination,
authority, or policy state denies the operation. A model response, external
document, paired device, provider key, or previous approval never grants authority
by itself.

The protected outcomes are:

- operator goals, conversations, memory, reports, and observations stay within
  their declared disclosure boundary;
- source code, files, browser state, external accounts, and devices change only
  through declared capabilities and approvals;
- secrets reach only an approved field and destination after the model has
  finished planning;
- jobs, artifacts, approvals, and audit records preserve provenance and cannot be
  replayed under a wider boundary; and
- failures degrade visibly without silently selecting a more permissive route.

## Threat Model

### Assumptions

- Seraph is initially a single-operator product, not a multi-tenant service.
- The GPU host administrator is trusted to administer the machine. SSH remains an
  administrative path, not product transport.
- The LAN reduces exposure but is not trusted as an authorization mechanism.
- Local and remote model output is untrusted input to policy and capability code.
- Remote providers and external services are not trusted to enforce Seraph policy.
- A paired edge or channel is revocable and may later be stolen or compromised.

Public-Internet exposure and multi-tenant authorization are outside Epic #736.
They must not be inferred from this contract.

### Threat actors

| Actor | Representative abuse |
| --- | --- |
| Anonymous or malicious LAN peer | Reads state, submits work, opens a WebSocket, or floods resources |
| Compromised browser/session | CSRF, forged origin, stolen session, replayed mutation |
| Compromised edge or channel | Sends stale observations, replays commands, uploads hostile artifacts |
| Hostile external content | Indirect prompt injection, secret exfiltration, SSRF, policy override |
| Compromised model endpoint or hostile model output | Fabricates approval, changes destination, requests excessive authority |
| Malicious capability, extension, connector, or MCP server | Escapes filesystem/process/network bounds or returns secret material |
| Compromised dependency or sibling host process | Reads workspace, credentials, runtime state, or local service traffic |
| Stale durable state | Replays an approval, checkpoint, job, or artifact after policy drift |
| Operator error or stolen operator credentials | Approves the wrong target, exposes data, or performs destructive recovery |
| Resource-exhaustion actor | Starves chat, fills storage, or monopolizes CPU/GPU/process slots |

### Protected assets

- operator identity, goals, conversations, memory, reports, and corrections;
- raw screenshots, window metadata, voice, messages, and device presence;
- source repositories, filesystem contents, browser cookies, and external accounts;
- vault values and keys, provider keys, pairing credentials, and session tokens;
- model prompts, responses, route decisions, and redaction records;
- jobs, checkpoints, artifacts, approval lineage, and audit integrity;
- backups, restore material, and migration manifests; and
- CPU, GPU, storage, process, and network availability.

### Entry points and trust boundaries

```text
browser -> reverse proxy -> REST/WebSocket -> backend
Mac edge or messaging channel -> paired ingress -> backend
scheduler/service principal -> job runner -> capability or model route
external page/document/repository -> content parser -> model context
backend -> local or remote model endpoint -> untrusted model output
model output -> planner -> policy decision -> capability host
capability host -> filesystem/process/browser/network/connector
vault -> destination-bound secret injection -> declared host/field
artifact ingress -> quarantine/storage -> consumer/export
job/checkpoint/approval -> replay or resume under current policy
backup archive -> verified restore -> single canonical runtime
administrator/SSH -> host lifecycle, never Seraph application traffic
```

Pairing, authentication, inference, observation, secret use, and execution are
separate permissions. Crossing one boundary does not imply permission for another.

## `seraph.trust.v1` Decision Contract

Every protected operation produces a decision envelope before the side effect.
The canonical schema identifier is `seraph.trust.v1`.

| Field | Required meaning |
| --- | --- |
| `policy_version` | Exact contract version, initially `seraph.trust.v1` |
| `principal` | Authenticated/revoked state, principal type, explicit grants, and exact session/job binding |
| `session_id` / `job_id` | Exact execution identity; at least one is required and must match the principal |
| `operation` / `required_grant` | Requested operation plus the exact authority grant required by the principal/operation matrix |
| `capability_id` / `capability_version` | Exact executable capability contract |
| `resource` | Typed resource target, identifier, and canonical object digest |
| `input_data_class` | `local_only`, `cloud_allowed_redacted`, or `cloud_allowed_full` |
| `provenance` | Operator, edge, external content, memory, model, or capability source |
| `destination` | Endpoint, host, account, filesystem root, process, or artifact consumer |
| `data_digest` / `secret_scope_digest` | Canonical lowercase SHA-256 metadata or the declared no-secret sentinel; never raw content |
| `resource_limits_digest` | Canonical limits binding or the declared no-limits sentinel |
| `transformation_digest` | Canonical redaction/transformation binding or the declared no-transformation sentinel |
| `authority_scope_digest` | Canonical binding of grant, capability, destination, and resource target |
| `request_id` / `attempt_id` / `replay_id` | Distinct bounded references used for attempt and replay enforcement |
| `decision_expires_at` | Decision validity bounded to at most 300 seconds |
| `approval` | Exact binding across request, attempt, replay, identity, authority, target, transformation, expiry, and consumption |
| `decision` | `allow`, `deny`, or `require_approval` |
| `reason_codes` | Stable machine-readable explanation |
| `audit` | Receipt identity plus persisted/durable declarations and membership in the evaluator's authoritative verified-receipt input |
| `recovery_class` | Exactly `none`, `retry`, `resume`, `compensate`, `quarantine`, or `irreversible` |

Raw secrets, full prompt bodies, cookies, tokens, and private artifact contents are
not decision fields. Receipts contain references, hashes, counts, and redacted
summaries only.

### Default decision table

| Condition | Decision |
| --- | --- |
| Required field or known policy version is missing | Deny |
| Principal is anonymous, revoked, expired, or not authorized for the action | Deny |
| Destination is absent, unresolved, private when prohibited, or outside an allowlist | Deny |
| Data or secret scope is wider than the destination permits | Deny |
| Capability, policy, destination, or input changed after approval | Require fresh approval or deny |
| External content requests policy change, approval, secret use, or execution | Ignore that authority request and independently authorize the resulting action |
| Side effect is destructive, external, secret-bearing, privileged, or above its cost limit | Require scoped approval unless an explicit narrower policy already authorizes it |
| Audit durability is required but unavailable | Deny before execution |
| Current policy and recorded replay boundary differ | Deny resume; start a fresh authorized job |
| All required boundaries and approvals match | Allow and emit the redacted decision/audit lineage |

Invalid configuration must not normalize to the most permissive mode. Internal
scheduled work uses a named service principal; absence of a user session is not
authorization.

## Data-Egress Semantics

| Data class | Local route | Remote route | Fallback |
| --- | --- | --- | --- |
| `local_only` | Allowed when the local destination and capability policy match | Denied | Wait, degrade visibly, or request an explicit reclassification; never silently send remotely |
| `cloud_allowed_redacted` | Allowed | Allowed only after a named transformation reports removed fields and no unresolved secrets | Only to another compatible route with the same transformed payload and policy |
| `cloud_allowed_full` | Allowed | Allowed to the explicitly selected/approved provider and purpose | Only to a compatible approved destination; record actual route |

Classification follows every derived prompt and artifact. Summarization does not
automatically declassify source material. Redaction must occur before network I/O;
a failed egress decision sends zero request bytes. Route receipts record the actual
provider/model, destination class, transformation, fallback, latency, cost estimate
when available, and degradation without including prompt contents.

Capability network scopes currently bind a host and path, not an arbitrary service
port. An explicit non-default HTTP(S) port is therefore denied before transport
until the capability contract declares and enforces a port-specific grant.

Provider credentials configure a route but do not approve data disclosure. The
strict-local synchronous path now accepts only an explicit local runtime profile
using HTTP(S) `localhost` or a literal RFC1918, IPv6 ULA, loopback, or link-local
address. It rejects suffix-based and single-label hostnames, public or unspecified
addresses, and local-profile spoofing before transport. Resolver pinning, redirect
revalidation, universal classification, and remote-route adoption remain #740.

## External-Content Invariants

External content may contribute evidence, never authority. Prompts alone cannot
make this guarantee; enforcement occurs after model output and before every
capability call.

- Content cannot change policy, identity, approval, destination, or secret scope.
- Tool/capability requests derived from content are authorized as new requests.
- Encoded instructions, cross-tool instructions, and retrieved memory keep their
  provenance.
- Private-network and metadata-service destinations deny unless a specific local
  capability policy requires and permits them.
- Active files, archives, downloads, and uploads remain quarantined until their
  artifact policy allows a consumer.
- Detection metadata or a hostile-content fixture is not proof of runtime blocking.

## Secret Invariants

- Models receive opaque references or non-secret metadata, not secret values.
- Secret resolution occurs immediately before an authorized capability boundary.
- A reference is bound to principal/session, purpose, field, destination host,
  secret version, and expiry; reuse outside that scope denies.
- Secret values must not appear in prompts, streaming output, errors, logs, audit,
  artifacts, checkpoints, reports, or operator receipts.
- A capability returning resolved secret material fails closed and quarantines the
  result.
- Revocation prevents future resolution. Incident recovery rotates affected
  credentials; restoring an old backup does not reactivate revoked authority.

The existing secret-reference wrapper enforces field- and destination-scoped
resolution on covered tool paths. Universal capability adoption belongs to #747,
and vault-key/backup handling belongs to #742.

## Approval And Audit Invariants

An approval binds the principal, exact session/job, capability and version,
request/attempt/replay identities, normalized input hash, destination/account,
authority and resource target, data class, transformation, secret scope,
resource/cost limit, policy version, decision expiry, approval expiry, and
consumption mode. Meaningful drift invalidates it.
Revocation blocks future and queued use. An approval for observation, inference,
or pairing never becomes execution authority.

Privileged or irreversible effects require a persisted, durable pre-execution
audit identity or transactional outbox. A receipt string and caller-supplied
`persisted`/`durable` booleans are insufficient: the evaluator also requires the
receipt in its authoritative `verified_audit_receipt_ids` input. This branch
enforces that input contract but does not query durable storage itself; repository
verification and adapter adoption remain #747. Best-effort post-execution logging
is insufficient for privileged effects. Audit visibility is not authorization.

## Artifact And Recovery Invariants

Artifacts declare owner, provenance, data class, content type, active-content
status, hash, size, allowed consumers/exports, retention, and deletion state.
Archive traversal, symlink escape, content-type mismatch, integrity failure, or an
undeclared consumer causes denial or quarantine. #742 owns durable migration,
storage, backup, and restore adoption; #747 owns capability-produced and consumed
artifact enforcement.

Recovery classes are precise:

- `retry`: same authorized idempotent operation and boundary;
- `resume`: checkpoint is complete and its recorded boundary still matches;
- `compensate`: a declared inverse action exists, with its own authorization;
- `quarantine`: isolate output/state and require diagnosis or fresh approval; and
- `irreversible`: no rollback claim; require stronger pre-execution confirmation.

“Rollback” never promises reversal of an email, published message, disclosed
secret, or another irreversible external effect.

## Current Enforcement Status

This table describes the repository baseline plus this branch. “Real” means code
on a covered path makes a decision. It does not mean universal adoption.

| Surface | Current evidence | Status and gap |
| --- | --- | --- |
| Shared v1 decision evaluator | `backend/src/security/trust_contract.py`, `backend/tests/test_trust_contract.py` | Real branch-local enforcement of grants, principal/operation matrix, exact session/job and target scope, canonical digests, bounded attempt/replay/expiry checks, verified-audit input, typed recovery, and approval drift; replay sets and verified audit receipts are caller-authoritative, with atomic/repository adoption downstream |
| Strict-local inference candidate gate | `backend/src/llm_runtime.py`, `backend/tests/test_llm_runtime.py` | Real on `completion_with_fallback_sync(local_runtime_only=True)` before primary/fallback transport for trusted local provider kind plus `localhost`/literal private address; free-form `local` capability spoofing denies, while DNS pinning, redirects, and general model-fabric adoption are #740 |
| Scheduled workflow service identity | `backend/src/approval/runtime.py`, `backend/src/scheduler/scheduled_jobs.py`, `backend/tests/test_scheduled_jobs.py` | Real on the scheduled-workflow path with explicit `SERVICE`, exact session/job, and `CAPABILITY_EXECUTE`; universal service/capability adoption is #747 |
| Protected tool authority gate | `backend/src/tools/approval.py`, `backend/tests/test_approval_tools.py` | Real v1 enforcement for wrapped high-risk/forced tools; missing session or principal denies before approval lookup or execution, while direct non-factory callers remain #747 migration work |
| Agent-factory executable authority gate | `backend/src/tools/approval.py`, `backend/src/agent/factory.py`, `backend/tests/test_agent.py` | Partial real enforcement for factory-returned data-bound tools and workflows: the shared trust decision runs before wrapped dispatch, while pure conversation helpers and direct non-factory callers remain outside this slice |
| Onboarding executable authority gate | `backend/src/tools/approval.py`, `backend/src/agent/onboarding.py`, `backend/tests/test_tool_audit.py` | Real on onboarding guardian-state and explicitly scoped webpage tools: missing session or principal denies before wrapped dispatch, while pure conversation behavior remains ungated and other direct non-factory callers remain #747 migration work |
| Governed artifact builder | `backend/src/artifacts/registry.py`, `backend/tests/test_artifact_registry.py` | Real opt-in exact content decision with canonical input digests and digested principal/source/provenance receipts; legacy producers remain visibly `legacy_unclassified` for #742/#747 migration |
| Secret runtime-authority gate | `backend/src/tools/secret_ref_tools.py`, `backend/src/security/trust_contract.py`, `backend/tests/test_secret_ref_tools.py` | Real preflight of `CREDENTIAL_EGRESS` through the shared principal/operation matrix before secret resolution, followed by capability evaluation; missing/under-scoped authority, `SERVICE`, and paired-edge principals deny even when grant strings are present, while full destination-bound durable credential approval/audit/replay remains #747 |
| Secret-reference field/host boundary | `backend/src/tools/secret_ref_tools.py`, `backend/src/vault/refs.py` | Real on wrapped paths; universal host adoption is #747 |
| Filesystem/process covered paths | `backend/src/tools/filesystem_tool.py`, `backend/src/tools/process_tools.py`, `backend/src/security/secure_host.py` | Partial real enforcement; capability manifests/resource isolation are #747 |
| Workflow approval-context replay | `backend/src/workflows/`, `backend/tests/test_workflows.py` | Real boundary-drift blocking on covered workflow resumes |
| Tool policy and approval wrappers | `backend/src/tools/policy.py`, `backend/src/tools/approval.py` | Protected `ApprovalTool` paths require explicit session and principal; direct, unwrapped, low-risk, and permissive-default paths remain #747 migration work |
| Tool audit wrapper | `backend/src/tools/audit.py` | Real best-effort logging on covered session paths; privileged audit durability is not universal |
| Secure-host reports and hostile corpora | `backend/src/security/`, `backend/src/extensions/`, `backend/src/evals/` | Mostly metadata or deterministic fixtures unless linked to a named executable choke point |
| HTTP, WebSocket, observer, edge/channel ingress | `backend/src/api/router.py`, `backend/src/api/ws.py`, `backend/src/api/observer.py` | Contract only; authenticated adoption is #741 |
| Local/remote model data egress | `backend/src/llm_runtime.py`, `backend/src/vlm_runtime.py` | The named strict-local synchronous candidate path is enforced above; universal classification, remote-route adoption, and zero-byte denial proof remain #740 |
| Artifact classification, migration, backup, restore | workflow/artifact metadata and workspace storage | Partial metadata; durable enforcement is #742 and #747 |
| Cumulative cross-surface gate | operator/eval surfaces | Planned under #754; existing bounded fixtures are not the epic release gate |

## Critic Disposition

The first independent cumulative review returned **Changes Requested**. Its
findings were accepted: authentication is now separate from explicit authority;
automatic session-to-operator synthesis was removed; scheduled workflows use a
bounded service/job identity; digest fields and governed artifact receipts are
canonicalized; the strict-local hostname shortcut was removed; and the decision,
approval, audit, recovery, attempt, replay, expiry, transformation, and target
bindings were strengthened. A follow-up independent review also returned
**Changes Requested**. Those findings were accepted: session-only protected tool
and secret access now denies, caller booleans no longer self-verify audit
durability, and free-form `local` capability metadata no longer makes a profile
eligible for strict-local routing. Cross-layer downstream ownership is split in
the coverage matrix. A third independent review returned **Changes Requested**
after reproducing a service-principal credential-egress bypass. That finding was
accepted and fixed: the secret wrapper now calls the shared
`principal_operation_reason` used by the full evaluator for an actual
`CREDENTIAL_EGRESS` preflight, so a grant string cannot bypass the principal-type
operation matrix.

On 2026-07-10, a fresh post-fix independent review passed with **no material
findings**, supported by 251 focused and 10 adjacent validation receipts. Resolver
pinning, redirects, and universal model routing remain #740; authenticated ingress
remains #741; and full destination-bound credential approval, durable audit, and
atomic replay adoption remain #747. Interactive privileged tools remain
fail-closed until authoritative principals are supplied.

## Adoption Matrix

The JSON coverage matrix is authoritative for boundary ownership and adoption
status. Its allowed milestone statuses are:

- `enforced_in_738`: executable decision at a named choke point plus a focused
  negative test on this branch;
- `contract_only`: normative contract exists but no runtime enforcement is claimed;
- `owned_by_downstream`: implementation and proof are assigned to the named issue.

Every entry also classifies existing evidence as `real`, `partial`, `metadata`,
`fixture`, or `none`. Metadata and fixtures cannot be promoted to enforcement.

## Migration And Security-Preserving Rollback

1. Introduce `seraph.trust.v1` alongside legacy metadata and inventory all call
   sites without widening legacy authority.
2. Adapt model, ingress, workspace/artifact, and capability choke points in their
   owning issues. A boundary becomes enforced only with its negative tests.
3. Treat legacy approvals, checkpoints, secret refs, and jobs missing v1 lineage as
   ineligible for privileged replay. Start fresh under current policy.
4. Keep policy and data schema versions in receipts so mixed-version state fails
   closed rather than selecting a permissive default.
5. Roll back code and schema only to a release that understands the stored policy
   version. Preserve audit history, quarantine incompatible queued work, invalidate
   sessions/approvals/secret refs created after the rollback point, and rotate any
   credential implicated by an incident.
6. #754 verifies cumulative migration and rollback. Re-enabling an unauthenticated,
   unclassified, or unaudited path is not an acceptable rollback.

## Residual Risks

- The v1 evaluator is branch-local and is not a universal runtime choke point;
  downstream adapters must adopt it before their boundaries are enforced.
- Protected tool and secret-ref wrappers deny missing session/principal authority.
  Authenticated ingress (#741) and universal capability adoption (#747) must bind
  authoritative principals for all remaining callers and capability classes.
- The scheduled-workflow service principal is enforced only on that named path;
  other internal/service callers remain #747 adoption work.
- Governed artifact records are opt-in; existing producers remain explicitly
  unclassified until #742/#747 migrates them.
- Secret-ref runtime authority now fails closed on its covered wrapper, but the
  complete destination-bound, repository-backed credential approval/audit/replay
  contract remains #747 work.
- LAN authentication and paired identity are not shipped until #741.
- Model egress classification is not universal until #740.
- Strict-local hostname rejection is enforced, but resolver pinning and redirect
  revalidation remain #740 work.
- Verified audit receipt membership and replay rejection are evaluator inputs, not
  repository-backed atomic reservations. Durable audit lookup and replay
  reservation remain #747, with cumulative recovery proof in #754.
- Capability, sandbox, audit-durability, and resource-limit adoption are not
  universal until #747.
- Artifact, backup, restore, and vault-key migration remain owned by #742.
- Prompt-injection resistance reduces authority escalation; it cannot guarantee a
  model interprets hostile text correctly.
- A host administrator or compromised host can bypass in-process controls. This
  milestone does not provide hardware-backed isolation.
- Existing benchmark/receipt modules contain useful fixtures but do not establish
  production security outside their named covered paths.
- Cumulative interaction, availability, restart, and security-preserving rollback
  proof remains owned by #754.

## Validation Plan

Each owning issue must add executable proof at its actual choke point. The
cumulative gate requires:

- schema/property tests for missing fields, unknown policy versions, and default
  denial;
- recording-endpoint tests showing denied egress sends zero bytes;
- secret canaries across prompts, streams, errors, logs, audit, and artifacts;
- approval expiry, replay, revocation, destination drift, capability drift, and
  policy drift tests;
- hostile content flowing through the real capability authorization path;
- anonymous, forged-origin, replayed, expired, and revoked ingress negatives;
- artifact traversal, integrity, active-content, consumer, deletion, backup, and
  restore negatives;
- audit-storage failure behavior for privileged effects;
- migration/restart/rollback drills with explicit residual risk; and
- independent security review before each owning PR and before Epic #736 lands.

For this documentation slice run:

```bash
python3 -m json.tool docs/implementation/security-enforcement-coverage.json
jq -e '([.entries[].id] | length == (unique | length)) and ([.entries[].milestone_status] - .status_values | length == 0) and ([.entries[].existing_evidence] - .evidence_values | length == 0)' docs/implementation/security-enforcement-coverage.json
python3 scripts/check_docs_contract.py
python3 scripts/check_strategy_claims.py
cd docs && npm run typecheck && npm run build
```
