# ADR-028: Reviewed typed task methods and reusable procedures

## Status

Accepted target for #1001, extended by the same programme's #1002 reusable
procedure milestone. Each changed boundary requires its complete implementation
and independent review before activation; this decision alone does not claim a
shipped capability.

## Decision

Extend ADR-015's reviewed selection boundary with a specialized, data-only
future-task strategy. ADR-023 remains the authority boundary. Explicit operator
review may accept an immutable `TaskMethod.v1`, `ResearchStrategy.v1` or
`ProcedurePlan.v3` candidate
from the canonical task-lessons owner into a signed canonical M5 pattern memory.
A focused signed active pointer selects one version per stable owner, Goal
revision and task family. There is no second memory store, execution queue,
cost owner, generic recall effect or model-derived grant.

`TaskMethodReview` is closed: proposal_id, expected_revision, artifact_digest,
scope_digest, action accept/reject/rollback/disable/activate/delete, reason and
idempotency_key.
`ActiveMethodBinding` is closed: owner, scope, version, digest and proposal_id.
Owner contains canonical identity_id, issuer_principal_id and issuer_root_id;
scope is the existing Goal/revision/family LessonScope. The scope digest binds
the original candidate and current pointer revision/target or absent sentinel.
The final canonical writer rechecks both source and pointer CAS and atomically
writes signed pattern, pointer, proposal status and existing action audit.
Exact replay returns the original action result without renewing authority.

The existing TaskStrategyBinding wire remains unchanged. method_id is the
immutable original proposal ID; version is the accepted canonical Memory ID;
digest hashes the exact normalized closed typed candidate; typed_data contains
that candidate. Signed task_method_scope.v1 binds complete owner/Goal/family,
candidate/schema/version, proposal and original Task/attempt/fence/source-token,
scope and receipt references. Candidate bytes and accepted versions are immutable.
At most16 accepted versions exist per exact scope, with explicit review-needed
capacity exhaustion and no eviction.

CurrentMethod.resolve selects for new admission. validate_pinned separately
authenticates the exact already-admitted version, source and current Task or
programme authority without consulting the current pointer. Operator review
requires current original Goal ownership; selected recovery is read-only. A
service-owned discovery read validates the exact finite current programme grant
and stable owner, without reviving the issuing browser Root.

Rollback changes future selection to a permitted prior version or an explicit
signed baseline. Its signed rolled_back historical version remains valid only
for existing native pins. Tombstones, export redaction, explicit revocation,
changed current authority and original invocation cutoffs still stop the next
owned boundary. Rollback never recreates a grant, budget, attempt or contacted
Unknown effect. Invalid or dangling pointers are blocked, never an implicit
baseline. A never-selected absent pointer preserves the explicit ordinary
baseline.

Typed strategies affect only consumers that actually apply their fields. General
tasks apply supported typed inputs, registered tool sequences and finite guards
inside their existing PlanSpec and approvals. Public discovery applies the exact
research query, source-selection and draft fields inside its existing programme
limits and inference lane. Research dossier remains on its existing baseline
until those fields are applied and proved there. Research strategy preparation
is an explicit structured action on the same task-lessons owner, creates a new
immutable candidate, and requires an exact supported completed research Task
with verified physical native output; substring classification and correction
prose cannot create a strategy.

Method JSON is excluded from every ordinary semantic index, recall, working set,
prompt and generic M5 selection path, including rewritten metadata linked to its
canonical proposal. It can be consumed only through the dedicated typed owner.
No method contributes permissions, unclassified paths/URLs, credentials, providers,
models, tool installation/registration, executable code, grants or allowances.

### Reusable general procedures (v3)

`ProcedurePlan.v3` is a closed data grammar with exactly seven fields:

| Field | Contract |
| --- | --- |
| `source_task_id` | Exact original completed general Task. |
| `source_attempt` | Its latest ended, verified native Attempt. |
| `steps` | Complete original registered typed steps, inputs, dependencies and output contracts; no step may be dropped. |
| `parameters` | Unique server-offered ordinary scalar leaves with names, exact step/input pointers, strict schemas and original producer identity/classification digest. |
| `tool_contract_versions` | Exact original registered tool versions, input/output schemas, producer contracts, effects, permissions and verifier digest pins. |
| `output_contract` | Exact requested output contract. |
| `permissions_digest` | Commitment to the complete original tool permission/effect and producer contract set; no permission grant. |

The closed candidate wrapper carries `schema_version: ProcedurePlan.v3` and
`plan`; that discriminator is not an eighth plan field. Limits are 16 steps,
16 parameters and 16 accepted immutable versions per exact owner/Goal/family
scope. Complete canonical typed artifact JSON is bounded to 64KiB UTF-8. Full
Vault redaction must leave it exactly unchanged, and existing secret/authority
text checks apply to all bytes. Generic M5 prose retains its separate
2,000-scalar normalization limit; this contract creates no large-text bypass.

The canonical task-lessons owner stages the full original physical native
journey outside the SQL writer: original envelope and producer pins established
before contact, every admitted step/receipt/positive claim, private resolved
inputs, settled contact, closed children and complete verified output/readback.
Final publication rechecks the same sealed source witness using canonical
SQL-only source CAS. All original input leaves must be producer-classified as
safe ordinary fixed data, selectable ordinary scalar data or typed dependency
references; forbidden or unclassified leaves block the whole candidate. Native
write content is only an original typed dependency, and browser action remains
the original fixed extract/html mode. Registered MCP classification must come
from the trusted local producer declaration, never a server advertisement or
caller. Free-body literals, code/snippets, credentials, authority controls,
budgets, verifiers and source-output defaults cannot become reusable grants.

Receipts without original producer pins are ineligible with
`source_contract_review_required`. Current code cannot retrospectively certify
them. Ordinary legacy continuation retains the original descriptor/digest and
compares every existing field, allowing only newly added current producer
classification to be absent from the original. Existing v1/v2 behavior remains
unchanged.

`GET /api/work-board/tasks/{id}/save-method` inspects source eligibility and
server offers. `POST` on that route accepts only exact revision/attempt,
`parameter_selections` of offer ID/name pairs and the original idempotency key.
It creates a private immutable inert proposal; task success does not adopt it.
Separate explicit M5 review signs the exact candidate and family pointer.
Authored packages and trusted runtime plugins retain ADR-020/026's distinct
boundaries; this procedure does not register a new tool or publish a package.

`POST /api/memory/task-methods/{id}/invoke` requires the exact currently signed
general-family proposal/version/digest/pointer revision, current Goal/revision,
strict declared parameter values and fresh finite limits/egress acknowledgment.
It constructs an ordinary explicit PlanSpec. Fresh values live only at existing
`PlanStep.input` positions; no field is added to GeneralTaskInput or its envelope.
The existing consumer independently extracts those values, validates original
producer schemas, reconstructs the template and compares the complete actual
plan/DAG/fixed inputs/step outputs/requested output. Typed dependencies retain
their original symbolic references. Original and fresh resolved inputs must
validate against the exact producer schema before contact. Missing/extra names,
unresolved tokens or nonparameter drift deny publication and native execution.

Before original-key lookup or either service/repository early replay return,
the canonical immediate writer authenticates the exact current signed pointer
and compares the original stored strategy pin, complete request, input and plan.
The existing invocation audit event must prove the original whole request;
missing or corrupted proof denies replay. The writer releases before physical
staging, then publication rechecks the same pointer/source/body under its writer.
Same key A-to-B conflicts even when plans match; no derived key may create a
second Task. Exact retry returns the original Task without renewing authority,
deadline, cost allowance or approvals. New invocation uses fresh C1 Task/job/
attempt/deadline/cost and current Root/Goal/permissions/native approvals.

Disable selects the signed baseline for the whole general-task family and blocks
new method invocations without deleting evidence. Explicit activation restores
only its exact reviewed previous signed pin. V3 rollback restores the exact
eligible previous signed pointer, or signed baseline when none is eligible;
it never searches history or accepts an arbitrary version. Already admitted
pins retain their original identity under current authority/source/expiry checks.
Canonical deletion uses the existing M5 tombstone and withdraws a matching
pointer atomically; the tombstone stops subsequent in-flight boundaries too.
Deleted inspection returns safe scope/version/pointer/history/tombstone metadata
with no candidate content or invocation controls.

## Validation and consequences

Before activation, prove authenticated adoption/rejection, a subsequent real
general Task with applied fields and verified output, and a real governed public
discovery with the exact strategy pin and physical source/output readbacks.
Include restart/migration,16-version capacity, source/scope/signature/CAS tamper,
read-only recovery, generic recall exclusion, rollback and in-flight continuation
versus explicit revocation. Operator UI must show exact source/candidate/version,
applied fields, explicit baseline, blocked and recovery states. Scripted transport
proof establishes mechanics only; quality improvement remains unmeasured.

For v3, additionally prove an actual mixed research/file/registered-connector
native source and fresh reuse with changed ordinary values, distinct jobs and
approvals, literal physical readback, exact retry, same-key A-to-B denial,
pointer races during staging and both early replay paths, original invocation
event/envelope tamper denial, family disable/activation, exact prior rollback
and canonical tombstone deletion. Operator inspection must show exact source,
immutable candidate/version/digest/current pointer, bounded history and blocked
recovery. These local mechanical checks establish neither learned quality nor
live provider availability or native macOS execution.

This target uses the current Python lifecycle and canonical owners. It imports
no future composition epochs, Cordis bridge state or parallel execution ledger.
