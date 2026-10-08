"""Private report-only source staging; no plugin-supplied proof or dispatch."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select

from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardEvent, WorkBoardHandoff, WorkBoardInputArtifact, WorkBoardLink, WorkBoardTask
from src.work_board.contracts import WorkBoardOwner
from src.work_board.pipeline_contracts import REPORT, EvidenceConsumerInput, canonical_bytes, digest
from src.work_board.repository import BoardError


REPORT_PHASES = ("admission", "source", "invoke", "outcome", "cleanup")
REPORT_CHECKPOINT_IDS = tuple("runtime-service-invocation:task-capability:" + phase for phase in REPORT_PHASES)
REPORT_CANDIDATE_MAX_BYTES = 16384
REPORT_CONTEXT_TAG = "task-capability-report.v1"
_ADMISSION_FIELDS = frozenset({"schema_version", "profile", "capability_version", "job_id", "task_id",
    "task_revision", "task_token", "attempt_id", "attempt_token", "board_fencing_token", "board_lease_owner",
    "owner_principal_id", "original_root_id", "goal_id", "goal_revision", "operation_id", "operation_revision",
    "operation_digest", "accepted_digest", "operation_deadline", "plan_version", "input_id", "input_token",
    "typed_input_ref", "typed_input_digest", "payload_sha256", "payload_bytes", "link_id", "link_token",
    "handoff_id", "handoff_token", "producer_task_id", "producer_attempt_id", "producer_sha256",
    "workspace_digest", "input_digest", "authority_digest", "run_fingerprint", "composition_binding_digest",
    "idempotency_scope", "idempotency_key", "attempt_started_at", "task_updated_at", "task_idempotency_binding",
    "attempt_updated_at", "cutoff", "slots", "slot_max_bytes", "no_learning"})


@dataclass(frozen=True)
class _ReportSourceSeal:
    source_identity: int
    source_digest: str
    owned_sources: tuple


def _source_digest(value):
    if type(value) is ReportCurrentWitness:
        return digest({item.name: hashlib.sha256(getattr(value, item.name)).hexdigest()
            if isinstance(getattr(value, item.name), bytes) else getattr(value, item.name)
            for item in fields(value) if item.name not in {"source_witness", "producer_witness", "_issuance"}})
    if type(value) is ReportAdmissionCandidate:
        spec = value.original_spec
        contract = {item.name: getattr(spec, item.name) for item in fields(spec)}
        contract.update(identity=spec.identity.__dict__, deadline_at=spec.deadline_at.isoformat(),
            composition_binding=spec.composition_binding.binding_digest)
        return digest({"metadata": hashlib.sha256(value.metadata_bytes).hexdigest(), "spec": contract})
    if type(value) is _ReportPublicationPermission:
        return digest({"run_id": value.run_id, "phase": value.phase,
            "prior": value.prior_journal_digest, "next": value.next_journal_digest,
            "receipt": value.receipt_bytes.hex()})
    if type(value) is ReportArtifactWitness:
        return digest({"source": _source_digest(value.current_witness), "output_ref": value.output_reference,
            "output_sha256": value.output_digest, "output_bytes": hashlib.sha256(value.output_bytes).hexdigest(),
            "closure": value.closure_owner.witness.__dict__,
            "claim": dict(value.original_scope.witness), "callback_identity": id(value.callback_owner)})
    raise ValueError("fixed report source required")


def _issue_source(value, owned_sources):
    object.__setattr__(value, "_issuance", _ReportSourceSeal(id(value), _source_digest(value), tuple(owned_sources)))
    return value


def _require_source(value, owned_sources):
    seal = value._issuance
    if (type(seal) is not _ReportSourceSeal or seal.source_identity != id(value)
        or seal.source_digest != _source_digest(value) or len(seal.owned_sources) != len(owned_sources)
        or any(actual is not expected for actual, expected in zip(seal.owned_sources, owned_sources))):
        raise BoardError("pipeline_input_changed", "Original owner-issued report source required", status_code=409)


def report_ref(job_id: str, method: str, candidate_digest: str) -> str:
    if method not in {"tasks.admit", "tasks.checkpoint", "capabilities.invoke", "tasks.settle", "tasks.cancel"}:
        raise ValueError("fixed report method required")
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 128:
        raise ValueError("original report job required")
    if (type(candidate_digest) is not str or len(candidate_digest) != 64
        or any(char not in "0123456789abcdef" for char in candidate_digest)):
        raise ValueError("original report candidate digest required")
    return "task-capability:" + digest({"job": job_id, "method": method, "candidate": candidate_digest})


@dataclass(frozen=True)
class ReportCurrentWitness:
    """Actual owner staging, retained privately across its next SQL boundary."""
    task_id: str
    task_token: str
    attempt_id: str
    attempt_token: str
    input_id: str
    input_token: str
    link_id: str
    link_token: str
    handoff_id: str
    handoff_token: str
    operation_id: str
    operation_revision: int
    operation_digest: str
    accepted_digest: str
    operation_deadline: str
    workspace_identity: bytes
    source_witness: Any
    producer_witness: Any
    input_payload: bytes
    input_bytes: bytes
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class ReportAdmissionCandidate:
    """Original typed producer object; JSON is only its bounded locator."""
    original_spec: Any
    current_witness: ReportCurrentWitness
    metadata_bytes: bytes
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)

    def metadata(self):
        if len(self.metadata_bytes) > REPORT_CANDIDATE_MAX_BYTES:
            raise ValueError("report admission candidate exceeds fixed allowance")
        return json.loads(self.metadata_bytes)

    @property
    def candidate_digest(self):
        return hashlib.sha256(self.metadata_bytes).hexdigest()


@dataclass(frozen=True)
class ReportArtifactWitness:
    current_witness: ReportCurrentWitness
    output_reference: str
    output_digest: str
    output_bytes: bytes
    closure_owner: Any
    original_scope: Any
    callback_owner: Any
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


def issue_report_artifact(current_witness, output_reference, actual, closure_owner, original_scope):
    """Fixed report file producer calls after actual write/close/readback only."""
    from src.work_board.input_artifacts import _PayloadClosureOwner, _verified_payload_closure
    from src.work_board.pipeline_cpu import output_bytes
    from src.runtime_plugins.dispatch import OriginalServiceInvocation
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    if (type(current_witness) is not ReportCurrentWitness or type(closure_owner) is not _PayloadClosureOwner
        or closure_owner.witness is None or type(original_scope) is not OriginalServiceInvocation
        or type(actual) is not bytes or actual != output_bytes(REPORT, json.loads(current_witness.input_bytes))
        or hashlib.sha256(actual).hexdigest() != closure_owner.witness.payload_sha256):
        raise ValueError("actual original report file closure required")
    _require_source(current_witness, (current_witness.source_witness, current_witness.producer_witness))
    _verified_payload_closure(closure_owner)
    expected = f"artifacts/work-board/evidence/{current_witness.task_id}-{current_witness.attempt_id}-{hashlib.sha256(actual).hexdigest()}.txt"
    if (output_reference != expected or asyncio.current_task() is None
        or closure_owner._path != canonical_workspace_root(settings.workspace_dir) / expected
        or closure_owner.witness.size_bytes != len(actual)):
        raise ValueError("exact original report output owner required")
    witness = ReportArtifactWitness(current_witness, output_reference, hashlib.sha256(actual).hexdigest(),
        actual, closure_owner, original_scope, asyncio.current_task())
    return _issue_source(witness, (current_witness, closure_owner, original_scope, witness.callback_owner))


@dataclass(frozen=True)
class _ReportPublicationPermission:
    run_id: str
    phase: str
    prior_journal_digest: str
    next_journal_digest: str
    receipt_bytes: bytes
    source: Any
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


def _journal(raw):
    def closed_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report journal key")
            result[key] = value
        return result
    value = json.loads(raw or "[]", object_pairs_hook=closed_pairs)
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise ValueError("original report journal malformed")
    return value


def preflight_report_journal(journal):
    """Reserve all missing finite phase slots; never truncate existing history."""
    if type(journal) is not list or any(type(item) is not dict for item in journal):
        raise ValueError("original report history required")
    selected = [item.get("checkpoint_id") for item in journal if item.get("checkpoint_id") in REPORT_CHECKPOINT_IDS]
    if len(selected) != len(set(selected)):
        raise ValueError("duplicate protected report phase")
    missing = len(REPORT_CHECKPOINT_IDS) - len(selected)
    # One additional original service-claim receipt must fit before forwarding.
    reserve_claim = not any(str(item.get("checkpoint_id", "")).startswith("runtime-service-invocation:")
        and item.get("checkpoint_id") not in REPORT_CHECKPOINT_IDS for item in journal)
    if (len(journal) + missing + int(reserve_claim) > 50
        or len(canonical_bytes(journal)) + missing * (REPORT_CANDIDATE_MAX_BYTES + 512)
            + int(reserve_claim) * 4096 > 1048576):
        raise ValueError("original report journal capacity unavailable")
    for item in journal:
        if item.get("checkpoint_id") in REPORT_CHECKPOINT_IDS and len(canonical_bytes(item.get("payload"))) > REPORT_CANDIDATE_MAX_BYTES:
            raise ValueError("protected report phase exceeds original bound")


def _issue_publication(db, run, *, phase, source, payload):
    from src.workflows.job_runtime import _digest
    if (phase not in REPORT_PHASES or len(canonical_bytes(payload)) > REPORT_CANDIDATE_MAX_BYTES
        or not db.info.get("native_writer_started")
        or db.info.get("composition_writer_owner") != ("durable_jobs" if phase == "admission" else "finite_service")):
        raise ValueError("fixed report publication writer required")
    prior = _journal(run.checkpoint_receipts_json)
    checkpoint_id = REPORT_CHECKPOINT_IDS[REPORT_PHASES.index(phase)]
    if any(item.get("checkpoint_id") == checkpoint_id for item in prior):
        raise ValueError("original report phase already published")
    receipt = {"checkpoint_id": checkpoint_id, "safe": True, "payload": payload,
        "state_digest": _digest(payload), "fencing_token": run.fencing_token,
        "recorded_at": datetime.now(timezone.utc).isoformat()}
    next_journal = prior + [receipt]
    preflight_report_journal(next_journal)
    permission = _ReportPublicationPermission(run.run_identity, phase,
        digest(prior), digest(next_journal), canonical_bytes(receipt), source)
    _issue_source(permission, (source,))
    db.info["composition_native_task_publication"] = permission
    return receipt, next_journal


async def seal_report_admission(db, run, candidate, host_boot_nonce):
    """Birth writer calls only after mandatory original-spec/source validation."""
    if type(candidate) is not ReportAdmissionCandidate:
        raise ValueError("owner-issued report admission required")
    await validate_report_spec(db, candidate.original_spec, candidate, host_boot_nonce)
    _require_source(candidate, (candidate.original_spec, candidate.current_witness))
    metadata = candidate.metadata()
    if (run.job_kind != REPORT or run.run_identity != metadata["job_id"]
        or run.input_digest != metadata["input_digest"] or run.authority_digest != metadata["authority_digest"]
        or run.run_fingerprint != metadata["run_fingerprint"]):
        raise ValueError("original report birth changed")
    payload = {"schema_version": 1, "context_tag": REPORT_CONTEXT_TAG, "phase": "admission",
        "job_id": run.run_identity, "host_boot_nonce": host_boot_nonce,
        "candidate": metadata, "candidate_digest": candidate.candidate_digest, "no_learning": True}
    receipt, next_journal = _issue_publication(db, run, phase="admission", source=candidate, payload=payload)
    run.checkpoint_receipts_json = canonical_bytes(next_journal).decode("utf-8")
    return receipt


def validate_task_publication(db, previous, current, *, run_id, previous_journal=None, current_journal=None):
    """Exact same-writer private issuance; no bare tuple/prefix permission."""
    old_protected = {key: value for key, value in previous.items() if key in REPORT_CHECKPOINT_IDS}
    new_protected = {key: value for key, value in current.items() if key in REPORT_CHECKPOINT_IDS}
    if any(new_protected.get(key) != value for key, value in old_protected.items()):
        raise ValueError("original report phase replacement denied")
    added = [value for key, value in new_protected.items() if key not in old_protected]
    if current_journal is not None and new_protected:
        preflight_report_journal(current_journal)
    if not added:
        return ()
    permission = db.info.get("composition_native_task_publication")
    if type(permission) is not _ReportPublicationPermission:
        raise ValueError("original report publication permission required")
    _require_source(permission, (permission.source,))
    old, new = previous_journal, current_journal
    receipt = json.loads(permission.receipt_bytes)
    if (type(old) is not list or type(new) is not list or len(added) != 1 or added[0] != receipt
        or permission.run_id != run_id or digest(old) != permission.prior_journal_digest
        or digest(new) != permission.next_journal_digest or new != old + [receipt]
        or receipt["checkpoint_id"] != REPORT_CHECKPOINT_IDS[REPORT_PHASES.index(permission.phase)]):
        raise ValueError("original report publication changed")
    preflight_report_journal(new)
    return (receipt,)


def read_report_candidate(run, kind="admission"):
    """Canonical protected metadata locator; never a physical owner witness."""
    from src.runtime_plugins.contracts import ref, sha
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.workflows.job_runtime import _digest
    if kind != "admission":
        raise ValueError("original report admission locator required")
    history = _journal(run.checkpoint_receipts_json)
    selected = [item for item in history if item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[0]]
    if len(selected) != 1:
        raise ValueError("original report admission unavailable")
    receipt = selected[0]
    payload = receipt.get("payload")
    if (type(payload) is not dict or set(payload) != {"schema_version", "context_tag", "phase", "job_id",
        "host_boot_nonce", "candidate", "candidate_digest", "no_learning"}
        or payload["schema_version"] != 1 or type(payload["schema_version"]) is not int
        or payload["context_tag"] != REPORT_CONTEXT_TAG or payload["phase"] != "admission"
        or payload["no_learning"] is not True or receipt.get("safe") is not True
        or receipt.get("state_digest") != _digest(payload)):
        raise ValueError("original report admission seal changed")
    value = payload["candidate"]
    if (type(value) is not dict or set(value) != _ADMISSION_FIELDS
        or len(canonical_bytes(payload)) > REPORT_CANDIDATE_MAX_BYTES
        or payload["candidate_digest"] != hashlib.sha256(canonical_bytes(value)).hexdigest()
        or value["schema_version"] != 1 or type(value["schema_version"]) is not int
        or value["profile"] != REPORT or value["capability_version"] != "1"
        or value["slots"] != list(REPORT_CHECKPOINT_IDS) or value["slot_max_bytes"] != REPORT_CANDIDATE_MAX_BYTES
        or type(value["slot_max_bytes"]) is not int or value["no_learning"] is not True):
        raise ValueError("original report candidate shape changed")
    for key in ("job_id", "task_id", "attempt_id", "board_lease_owner", "owner_principal_id", "original_root_id",
        "goal_id", "operation_id", "input_id", "link_id", "handoff_id", "producer_task_id", "producer_attempt_id"):
        ref(value[key])
    for key in ("task_token", "attempt_token", "operation_digest", "accepted_digest", "input_token",
        "typed_input_digest", "payload_sha256", "link_token", "handoff_token", "producer_sha256", "workspace_digest",
        "input_digest", "authority_digest", "run_fingerprint", "composition_binding_digest"):
        sha(value[key])
    sha(payload["host_boot_nonce"])
    for key in ("task_revision", "board_fencing_token", "goal_revision", "operation_revision", "plan_version", "payload_bytes"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError("original report candidate integer changed")
    binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
    cutoff = datetime.fromisoformat(value["cutoff"])
    started = datetime.fromisoformat(value["attempt_started_at"])
    operation_deadline = datetime.fromisoformat(value["operation_deadline"])
    run_deadline = datetime.fromisoformat(run.deadline_at) if type(run.deadline_at) is str else run.deadline_at
    if (cutoff.tzinfo is None or started.tzinfo is None or operation_deadline.tzinfo is None
        or cutoff > min(operation_deadline, started + timedelta(seconds=30))
        or value["job_id"] != f"evidence-cpu:{value['task_id']}:{value['attempt_id']}"
        or value["idempotency_scope"] != "work-board-attempt"
        or value["idempotency_key"] != f"{value['task_id']}:{value['attempt_id']}"
        or value["typed_input_ref"] != f"workspace-json:artifacts/work-board/inputs/{value['input_id']}-{value['payload_sha256']}.json"
        or value["payload_bytes"] > 65536 or run.owner_kind != "user"
        or run.job_kind != REPORT or run.capability_version != "1" or binding.origin_method != "tasks.admit"
        or binding.native_branch != "artifact" or not binding.allows("capabilities.invoke")
        or binding.binding_digest != value["composition_binding_digest"]
        or run.run_identity != value["job_id"] or payload["job_id"] != value["job_id"]
        or run.input_digest != value["input_digest"] or run.authority_digest != value["authority_digest"]
        or run.run_fingerprint != value["run_fingerprint"] or run.owner_principal_id != value["owner_principal_id"]
        or run.operator_session_id != value["original_root_id"] or run.session_id != value["original_root_id"]
        or run.goal_id != value["goal_id"] or run.goal_revision != value["goal_revision"]
        or run_deadline.replace(tzinfo=run_deadline.tzinfo or timezone.utc) != cutoff):
        raise ValueError("original report candidate binding changed")
    preflight_report_journal(history)
    return value


def checked_report_records(run):
    """Finite canonical selectors for existing scanner; no file I/O/grant."""
    value = read_report_candidate(run)
    return {key: value[key] for key in ("task_id", "attempt_id", "input_id", "operation_id",
        "link_id", "handoff_id", "producer_task_id", "producer_attempt_id", "goal_id")}


async def stage_report_admission(db, task, attempt, inputs, *, deadline, binding) -> ReportAdmissionCandidate:
    from src.auth.service import authenticate_principal
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.work_board.pipeline_cpu import spec_for
    from src.work_board.pipelines import utc
    from src.workflows.job_runtime import _composition_fingerprint, _digest
    if (type(binding) is not RuntimeCompositionBinding or binding.origin_method != "tasks.admit"
        or binding.native_branch != "artifact" or not binding.allows("capabilities.invoke")
        or binding.host_package_digest is None):
        raise BoardError("pipeline_task_changed", "Reviewed exact report composition required", status_code=409)
    witness = await stage_report_current(db, task, attempt, inputs)
    cutoff = utc(deadline)
    if (attempt.started_at is None or cutoff <= datetime.now(timezone.utc)
        or cutoff > min(utc(datetime.fromisoformat(witness.operation_deadline)), utc(attempt.started_at) + timedelta(seconds=30))):
        raise BoardError("pipeline_expired", "Original finite report cutoff required", status_code=409)
    principal = await authenticate_principal(task.owner_principal_id, db=db)
    original = spec_for(task, attempt, inputs, deadline=cutoff)
    authority = {**original.declared_authority,
        "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)}
    original = replace(original, composition_binding=binding, declared_authority=authority,
        run_fingerprint=digest({"task_ref": task.task_id, "attempt_ref": attempt.attempt_id,
            "inputs": original.inputs, "authority": authority}))
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    metadata = {"schema_version": 1, "profile": REPORT, "capability_version": "1",
        "job_id": original.identity.job_id, "task_id": task.task_id, "task_revision": task.task_revision,
        "task_token": witness.task_token, "attempt_id": attempt.attempt_id, "attempt_token": witness.attempt_token,
        "board_fencing_token": attempt.fencing_token, "board_lease_owner": attempt.lease_owner,
        "owner_principal_id": task.owner_principal_id, "original_root_id": task.owner_session_id,
        "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "operation_id": witness.operation_id, "operation_revision": witness.operation_revision,
        "operation_digest": witness.operation_digest, "accepted_digest": witness.accepted_digest,
        "operation_deadline": witness.operation_deadline, "plan_version": model.plan_version,
        "input_id": witness.input_id, "input_token": witness.input_token,
        "typed_input_ref": task.typed_input_ref, "typed_input_digest": task.typed_input_digest,
        "payload_sha256": hashlib.sha256(witness.input_payload).hexdigest(), "payload_bytes": len(witness.input_payload),
        "link_id": witness.link_id, "link_token": witness.link_token,
        "handoff_id": witness.handoff_id, "handoff_token": witness.handoff_token,
        "producer_task_id": model.producer_task_ref, "producer_attempt_id": model.producer_attempt_ref,
        "producer_sha256": model.producer_sha256, "workspace_digest": hashlib.sha256(witness.workspace_identity).hexdigest(),
        "input_digest": _digest(original.inputs), "authority_digest": _digest(authority),
        "run_fingerprint": _composition_fingerprint(original, _digest(original.inputs)),
        "composition_binding_digest": binding.binding_digest,
        "idempotency_scope": original.identity.idempotency_scope, "idempotency_key": original.identity.idempotency_key,
        "attempt_started_at": utc(attempt.started_at).isoformat(),
        "task_updated_at": utc(task.updated_at).isoformat(), "task_idempotency_binding": task.idempotency_binding,
        "attempt_updated_at": utc(attempt.updated_at).isoformat(),
        "cutoff": cutoff.isoformat(), "slots": list(REPORT_CHECKPOINT_IDS),
        "slot_max_bytes": REPORT_CANDIDATE_MAX_BYTES, "no_learning": True}
    candidate = ReportAdmissionCandidate(original, witness, canonical_bytes(metadata))
    candidate.metadata()
    return _issue_source(candidate, (original, witness))


async def validate_report_spec(db, spec, candidate, host_boot_nonce):
    """Mandatory initial writer check against its actual staged owner object."""
    from src.auth.service import authenticate_principal
    from src.runtime_plugins.ownership import validate_invocation
    from src.workflows.job_runtime import _composition_fingerprint, _digest
    if (type(candidate) is not ReportAdmissionCandidate or spec is not candidate.original_spec
        or type(host_boot_nonce) is not str or len(host_boot_nonce) != 64
        or any(char not in "0123456789abcdef" for char in host_boot_nonce)
        or not db.info.get("native_writer_started")
        or db.info.get("composition_writer_owner") != "durable_jobs"):
        raise BoardError("pipeline_input_changed", "Original protected report producer required", status_code=409)
    _require_source(candidate, (candidate.original_spec, candidate.current_witness))
    metadata = candidate.metadata()
    now = datetime.now(timezone.utc)
    root = await db.scalar(select(OperatorSession).where(OperatorSession.id == spec.operator_session_id,
        OperatorSession.principal_id == spec.identity.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
        OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
    if root is None:
        raise BoardError("pipeline_root_changed", "Original report Root is inactive", status_code=409)
    operator = await authenticate_principal(spec.identity.owner_principal_id, db=db)
    if (spec.identity.job_kind != REPORT or spec.identity.capability_version != "1"
        or spec.identity.owner_kind != "user" or spec.session_id != spec.operator_session_id
        or spec.identity.job_id != metadata["job_id"] or spec.goal_id != metadata["goal_id"]
        or spec.goal_revision != metadata["goal_revision"] or spec.plan_revision != metadata["plan_version"]
        or spec.composition_binding is None or spec.composition_binding.binding_digest != metadata["composition_binding_digest"]
        or spec.composition_binding.origin_method != "tasks.admit" or spec.composition_binding.native_branch != "artifact"
        or not spec.composition_binding.allows("capabilities.invoke")
        or _digest(spec.inputs) != metadata["input_digest"]
        or _digest(spec.declared_authority) != metadata["authority_digest"]
        or spec.declared_authority.get("grants") != sorted(str(getattr(grant, "value", grant)) for grant in operator.principal.grants)
        or _composition_fingerprint(spec, _digest(spec.inputs)) != metadata["run_fingerprint"]
        or spec.deadline_at.isoformat() != metadata["cutoff"] or spec.deadline_at <= now
        or spec.parent_job_id is not None or spec.source_task_id is not None
        or spec.dependencies or spec.resource_claims):
        raise BoardError("pipeline_input_changed", "Original immutable report spec changed", status_code=409)
    await validate_invocation(db, spec.composition_binding)
    task, attempt = await recheck_report_witness(db, witness=candidate.current_witness)
    if (task.task_id != metadata["task_id"] or attempt.attempt_id != metadata["attempt_id"]
        or task.task_revision != metadata["task_revision"] or attempt.fencing_token != metadata["board_fencing_token"]
        or attempt.lease_owner != metadata["board_lease_owner"]):
        raise BoardError("pipeline_task_changed", "Original report admission fence changed", status_code=409)


def validate_report_admission_request(spec, candidate, host, host_boot_nonce):
    """No JSON/copy reconstruction of the original producer or host owner."""
    from src.runtime_plugins.bridge import CordisHost
    from src.workflows.job_runtime import DurableJobSpec
    if (type(candidate) is not ReportAdmissionCandidate or type(spec) is not DurableJobSpec
        or spec is not candidate.original_spec or type(candidate.current_witness) is not ReportCurrentWitness
        or type(host) is not CordisHost or not host.admitting or host.reviewed is None
        or host.boot_nonce != host_boot_nonce or spec.composition_binding is None
        or host.reviewed.package_digest != spec.composition_binding.host_package_digest
        or host.reviewed.composition_digest != spec.composition_binding.host_composition_digest):
        raise BoardError("pipeline_task_changed", "Exact original report host and producer required", status_code=409)
    _require_source(candidate, (spec, candidate.current_witness))
    _require_source(candidate.current_witness,
        (candidate.current_witness.source_witness, candidate.current_witness.producer_witness))


async def stage_report_current(db, task, attempt, inputs: Mapping[str, Any]) -> ReportCurrentWitness:
    """Read real dossier/input files before entering the protected writer."""
    from src.guardian.opportunity_plans import stage_accepted_plan_task
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.pipelines import owned, row_token, validate_cpu_binding
    from src.work_board.review import stage_pipeline_producer_readback
    if task.capability_id != REPORT or type(task) is not WorkBoardTask or type(attempt) is not WorkBoardAttempt:
        raise BoardError("pipeline_task_changed", "Exact report Task and Attempt required", status_code=409)
    source = await stage_accepted_plan_task(db, task, attempt=attempt)
    await validate_cpu_binding(db, task, attempt, inputs, source_witness=source)
    current = await db.get(WorkBoardTask, task.task_id, populate_existing=True)
    active = await db.get(WorkBoardAttempt, attempt.attempt_id, populate_existing=True)
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    resolved = await resolve_input_artifact_for_task(db, owner, artifact_id=current.input_artifact_id,
        capability_id=REPORT, goal_id=current.goal_id, goal_revision=current.goal_revision)
    if canonical_bytes(resolved.input) != canonical_bytes(dict(inputs)):
        raise BoardError("pipeline_input_changed", "Original report input changed", status_code=409)
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    producer = await db.get(WorkBoardTask, model.producer_task_ref, populate_existing=True)
    producer_witness = await stage_pipeline_producer_readback(db, owner, producer)
    if (producer_witness.content_sha256 != model.producer_sha256
        or producer_witness.output_bytes.decode("utf-8") != model.quoted_source_data
        or producer_witness.attempt_id != model.producer_attempt_ref):
        raise BoardError("pipeline_source_changed", "Original report dossier changed", status_code=409)
    link = await db.scalar(select(WorkBoardLink).where(WorkBoardLink.parent_task_id == producer.task_id,
        WorkBoardLink.child_task_id == current.task_id))
    handoff = await db.get(WorkBoardHandoff, model.handoff_ref, populate_existing=True)
    operation, value = await owned(db, owner, current.pipeline_operation_id)
    if link is None or handoff is None:
        raise BoardError("pipeline_handoff_changed", "Original report linkage unavailable", status_code=409)
    witness = ReportCurrentWitness(current.task_id, row_token(current), active.attempt_id, row_token(active),
        resolved.row.artifact_id, row_token(resolved.row), link.link_id, row_token(link),
        handoff.handoff_id, row_token(handoff), operation.proposal_id, operation.revision,
        operation.proposal_digest, value["accepted_digest"], value["deadline_at"],
        canonical_bytes(value["live_root"]), source, producer_witness, bytes(resolved.payload), canonical_bytes(resolved.input))
    return _issue_source(witness, (source, producer_witness))


async def recheck_report_witness(db, *, witness: ReportCurrentWitness):
    """SQL/current policy recheck only; never restage file proof inside a writer."""
    from src.work_board.pipelines import row_token, task_guard, utc
    from src.work_board.review import recheck_pipeline_producer_readback
    if type(witness) is not ReportCurrentWitness:
        raise BoardError("pipeline_input_changed", "Private staged report source required", status_code=409)
    _require_source(witness, (witness.source_witness, witness.producer_witness))
    rows = []
    for cls, identifier, token in ((WorkBoardTask, witness.task_id, witness.task_token),
        (WorkBoardAttempt, witness.attempt_id, witness.attempt_token),
        (WorkBoardInputArtifact, witness.input_id, witness.input_token),
        (WorkBoardLink, witness.link_id, witness.link_token),
        (WorkBoardHandoff, witness.handoff_id, witness.handoff_token)):
        row = await db.get(cls, identifier, populate_existing=True)
        if row is None or row_token(row) != token:
            raise BoardError("pipeline_input_changed", "Staged original report changed", status_code=409)
        rows.append(row)
    task, attempt, artifact, link, handoff = rows
    if (task.capability_id != REPORT or attempt.task_id != task.task_id or attempt.ended_at
        or attempt.cancel_requested_at is not None
        or attempt.lease_expires_at is None or utc(attempt.lease_expires_at) <= datetime.now(timezone.utc)
        or hashlib.sha256(witness.input_payload).hexdigest() != artifact.payload_sha256
        or len(witness.input_payload) != artifact.size_bytes
        or artifact.bound_task_id != task.task_id or task.input_artifact_id != artifact.artifact_id
        or link.current_handoff_id != handoff.handoff_id or link.child_task_id != task.task_id):
        raise BoardError("pipeline_task_changed", "Original report execution fence changed", status_code=409)
    operation, value = await task_guard(db, task, attempt=attempt,
        workspace_identity=witness.workspace_identity, source_witness=witness.source_witness)
    if (operation.proposal_id != witness.operation_id or operation.revision != witness.operation_revision
        or operation.proposal_digest != witness.operation_digest or value["accepted_digest"] != witness.accepted_digest
        or value["deadline_at"] != witness.operation_deadline):
        raise BoardError("pipeline_plan_changed", "Original report operation changed", status_code=409)
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    await recheck_pipeline_producer_readback(db, owner, witness=witness.producer_witness)
    return task, attempt
