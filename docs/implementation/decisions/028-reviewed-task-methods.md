# ADR-028: Reviewed typed task methods

## Status

Accepted target for the #1001 implementation milestone. Runtime activation is
conditional on that milestone's complete implementation and independent review;
this decision alone does not claim a shipped capability.

## Decision

Extend ADR-015's reviewed selection boundary with a specialized, data-only
future-task strategy. ADR-023 remains the authority boundary. Explicit operator
review may accept an immutable `TaskMethod.v1` or `ResearchStrategy.v1` candidate
from the canonical task-lessons owner into a signed canonical M5 pattern memory.
A focused signed active pointer selects one version per stable owner, Goal
revision and task family. There is no second memory store, execution queue,
cost owner, generic recall effect or model-derived grant.

`TaskMethodReview` is closed: proposal_id, expected_revision, artifact_digest,
scope_digest, action accept/reject/rollback, reason and idempotency_key.
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
No method contributes permissions, arbitrary paths/URLs, credentials, providers,
models, tool installation/registration, executable code, grants or allowances.

## Validation and consequences

Before activation, prove authenticated adoption/rejection, a subsequent real
general Task with applied fields and verified output, and a real governed public
discovery with the exact strategy pin and physical source/output readbacks.
Include restart/migration,16-version capacity, source/scope/signature/CAS tamper,
read-only recovery, generic recall exclusion, rollback and in-flight continuation
versus explicit revocation. Operator UI must show exact source/candidate/version,
applied fields, explicit baseline, blocked and recovery states. Scripted transport
proof establishes mechanics only; quality improvement remains unmeasured.

This target uses the current Python lifecycle and canonical owners. It imports
no future composition epochs, Cordis bridge state or parallel execution ledger.
