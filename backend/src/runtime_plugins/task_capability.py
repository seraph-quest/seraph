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
        contract.update(identity={item.name: getattr(spec.identity, item.name) for item in fields(spec.identity)}, deadline_at=spec.deadline_at.isoformat(),
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
            "claim": dict(value.original_scope.witness), "callback_identity": id(value.callback_owner),
            "readback_identity": id(value.readback_witness)})
    if type(value) is ReportIdentityWitness:
        return digest({"candidate": value.candidate.candidate_digest, "task": value.task_token,
            "attempt": value.attempt_token, "input": value.input_digest, "expected": value.expected_bytes.hex()})
    if type(value) is ReportTerminalPublication:
        if type(value.terminal_witness) is ReportCancellationWitness:
            terminal_digest = _source_digest(value.terminal_witness)
        else:
            terminal_digest = {key: getattr(value.terminal_witness, key)
                for key in ("task_token", "attempt_token", "input_token", "link_token", "handoff_token", "run_token",
                    "operation_id", "operation_revision", "operation_digest", "output_reference", "output_digest")}
        return digest({"job": value.original_scope.witness["invocation_ref"], "status": value.status,
            "artifact": _source_digest(value.artifact), "terminal": terminal_digest,
            "readback": hashlib.sha256(value.readback_witness.actual).hexdigest(),
            "readback_identity": id(value.readback_witness)})
    if type(value) is ReportCancellationWitness:
        return digest({"current": _source_digest(value.current_witness), "event_id": value.event_id,
            "event_token": value.event_token})
    if type(value) is _ReportCancelInterruption:
        return digest({"candidate": value.resource.candidate.candidate_digest,
            "artifact": _source_digest(value.artifact), "event": value.event_id,
            "event_token": value.event_token, "phase": value.phase})
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
    readback_witness: Any
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


def issue_report_artifact(current_witness, output_reference, readback, closure_owner, original_scope):
    """Fixed report file producer calls after actual write/close/readback only."""
    from src.work_board.input_artifacts import _PayloadClosureOwner, _verified_payload_closure
    from src.work_board.pipeline_cpu import output_bytes, validate_native_report_readback
    from src.runtime_plugins.dispatch import OriginalServiceInvocation
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    actual = validate_native_report_readback(readback)
    if (readback.owner is not closure_owner or type(current_witness) is not ReportCurrentWitness or type(closure_owner) is not _PayloadClosureOwner
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
        actual, closure_owner, original_scope, asyncio.current_task(), readback)
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
    if (set(receipt) != {"checkpoint_id", "safe", "payload", "state_digest", "fencing_token", "recorded_at"}
        or type(receipt["fencing_token"]) is not int or receipt["fencing_token"] != 0
        or type(payload) is not dict or set(payload) != {"schema_version", "context_tag", "phase", "job_id",
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


def validated_terminal_report_source(run):
    """Historic metadata locator only; current/physical Memory owners recheck."""
    from src.workflows.job_runtime import _digest
    from .contracts import sha as validate_sha
    candidate = read_report_candidate(run)
    if run.status != "succeeded" or run.attempt_count != 1:
        raise ValueError("verified terminal report required")
    history = _journal(run.checkpoint_receipts_json)
    from .dispatch import _receipt_witness
    claims = [item for item in history if str(item.get("checkpoint_id", "")).startswith("runtime-service-invocation:")
        and item.get("checkpoint_id") not in REPORT_CHECKPOINT_IDS]
    if len(claims) != 1:
        raise ValueError("original report claim required")
    claim = _receipt_witness(claims[0])
    birth = next(item for item in history if item["checkpoint_id"] == REPORT_CHECKPOINT_IDS[0])
    if (claim["invocation_ref"] != run.run_identity or claim["attempt_count"] != 1
        or claim["fencing_token"] != run.fencing_token or claim["origin_method"] != "tasks.admit"
        or claim["native_branch"] != "artifact" or claim["host_boot_nonce"] != birth["payload"]["host_boot_nonce"]
        or claim["composition_binding_digest"] != candidate["composition_binding_digest"]
        or claim["input_digest"] != candidate["input_digest"] or claim["authority_digest"] != candidate["authority_digest"]
        or claim["run_fingerprint"] != candidate["run_fingerprint"]
        or claim["original_deadline_at"] != int(datetime.fromisoformat(candidate["cutoff"]).timestamp() * 1000)):
        raise ValueError("original terminal report claim changed")
    common = {"schema_version", "context_tag", "phase", "job_id", "candidate_digest", "no_learning"}
    fields_by_phase = {
        "source": common | {"task_id", "attempt_id", "input_id", "payload_sha256", "producer_sha256"},
        "invoke": common | {"output_reference", "output_sha256", "size_bytes", "file_closed", "readback_verified"},
        "outcome": common | {"output_reference", "output_sha256", "callback_closed", "file_closed", "readback_verified"}}
    phases, indices = {}, []
    for phase, expected_fields in fields_by_phase.items():
        selected = [(index, item) for index, item in enumerate(history)
            if item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[REPORT_PHASES.index(phase)]]
        if len(selected) != 1:
            raise ValueError("complete verified report history required")
        index, receipt = selected[0]
        value = receipt.get("payload")
        if (set(receipt) != {"checkpoint_id", "safe", "payload", "state_digest", "fencing_token", "recorded_at"}
            or type(receipt["fencing_token"]) is not int
            or type(value) is not dict or set(value) != expected_fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["context_tag"] != REPORT_CONTEXT_TAG or value["phase"] != phase
            or value["job_id"] != run.run_identity or value["no_learning"] is not True or receipt.get("safe") is not True
            or receipt.get("state_digest") != _digest(value) or receipt.get("fencing_token") != run.fencing_token
            or value["candidate_digest"] != digest(candidate)):
            raise ValueError("verified report phase changed")
        phases[phase] = value
        indices.append(index)
    if indices != sorted(indices) or any(item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[4] for item in history):
        raise ValueError("verified report history order changed")
    source, invoked, outcome = (phases[phase] for phase in ("source", "invoke", "outcome"))
    if any(source[key] != candidate[key] for key in ("task_id", "attempt_id", "input_id", "payload_sha256", "producer_sha256")):
        raise ValueError("verified report source changed")
    sha = invoked["output_sha256"]
    validate_sha(sha)
    expected = f"artifacts/work-board/evidence/{candidate['task_id']}-{candidate['attempt_id']}-{sha}.txt"
    if (invoked["output_reference"] != expected or outcome["output_reference"] != expected
        or outcome["output_sha256"] != sha or invoked["file_closed"] is not True
        or invoked["readback_verified"] is not True or outcome["file_closed"] is not True
        or outcome["callback_closed"] is not True or outcome["readback_verified"] is not True
        or type(invoked["size_bytes"]) is not int or not 1 <= invoked["size_bytes"] <= 65536):
        raise ValueError("verified report output changed")
    artifacts, effects = _journal(run.artifact_receipts_json), _journal(run.effect_receipts_json)
    if (len(artifacts) != 1 or len(effects) != 1 or artifacts[0].get("file_path") != expected
        or artifacts[0].get("content_sha256") != sha or artifacts[0].get("exists") is not True
        or artifacts[0].get("artifact_type") != "evidence_local_report" or artifacts[0].get("producer") != REPORT
        or artifacts[0].get("size_bytes") != invoked["size_bytes"]
        or effects[0].get("target_path") != expected or effects[0].get("content_sha256") != sha
        or effects[0].get("effect_type") != "evidence_cpu_output" or effects[0].get("receipt_kind") != "readback"
        or effects[0].get("status") != "succeeded" or effects[0].get("fencing_token") != run.fencing_token):
        raise ValueError("verified report artifact/readback unavailable")
    return {"candidate": candidate, "output_reference": expected, "output_sha256": sha,
        "size_bytes": invoked["size_bytes"]}


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
    await _recheck_report_birth_link(db, task, attempt, metadata)
    if (task.task_id != metadata["task_id"] or attempt.attempt_id != metadata["attempt_id"]
        or attempt.fencing_token != metadata["board_fencing_token"]
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


async def stage_report_current(db, task, attempt, inputs: Mapping[str, Any], *, _cancel_identity=False) -> ReportCurrentWitness:
    """Read real dossier/input files before entering the protected writer."""
    from src.guardian.opportunity_plans import stage_accepted_plan_task
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.pipelines import owned, row_token, validate_cpu_binding
    from src.work_board.review import stage_pipeline_producer_readback
    if task.capability_id != REPORT or type(task) is not WorkBoardTask or type(attempt) is not WorkBoardAttempt:
        raise BoardError("pipeline_task_changed", "Exact report Task and Attempt required", status_code=409)
    source = await stage_accepted_plan_task(db, task, attempt=attempt)
    if not _cancel_identity:
        await validate_cpu_binding(db, task, attempt, inputs, source_witness=source)
    else:
        from src.work_board.pipelines import task_guard
        if attempt.cancel_requested_at is None or attempt.ended_at is not None:
            raise BoardError("pipeline_task_changed", "Original cancellation intent required", status_code=409)
        await task_guard(db, task, attempt=attempt, source_witness=source)
    current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id)
        .execution_options(populate_existing=True))
    active = await db.get(WorkBoardAttempt, attempt.attempt_id, populate_existing=True)
    if current is None or active is None:
        raise BoardError("pipeline_task_changed", "Original report Task or Attempt unavailable", status_code=409)
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    resolved = await resolve_input_artifact_for_task(db, owner, artifact_id=current.input_artifact_id,
        capability_id=REPORT, goal_id=current.goal_id, goal_revision=current.goal_revision)
    if canonical_bytes(resolved.input) != canonical_bytes(dict(inputs)):
        raise BoardError("pipeline_input_changed", "Original report input changed", status_code=409)
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    producer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == model.producer_task_ref)
        .execution_options(populate_existing=True))
    if producer is None:
        raise BoardError("pipeline_source_changed", "Original report dossier unavailable", status_code=409)
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
        row = (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == identifier)
            .execution_options(populate_existing=True)) if cls is WorkBoardTask
            else await db.get(cls, identifier, populate_existing=True))
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


async def _recheck_report_birth_link(db, task, attempt, metadata, *, cancel_identity=False):
    """Prove only the canonical first board link from sealed birth tokens."""
    from src.work_board.pipelines import row_token
    if attempt.workflow_run_id is None:
        if (row_token(task) != metadata["task_token"] or row_token(attempt) != metadata["attempt_token"]
            or task.task_revision != metadata["task_revision"]):
            raise BoardError("pipeline_task_changed", "Original report birth changed", status_code=409)
        return
    events = (await db.scalars(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == task.owner_principal_id,
        WorkBoardEvent.owner_session_id == task.owner_session_id,
        WorkBoardEvent.kind == "attempt.linked"))).all()
    selected = [event for event in events if json.loads(event.metadata_json).get("attempt_id") == attempt.attempt_id]
    cancel_event = None
    if cancel_identity:
        cancel_event = await _report_cancel_event(db, task, attempt, metadata)
    expected_revision = metadata["task_revision"] + (2 if cancel_event is not None else 1)
    if (attempt.workflow_run_id != metadata["job_id"] or task.task_revision != expected_revision
        or len(selected) != 1 or json.loads(selected[0].metadata_json) != {
            "attempt_id": attempt.attempt_id, "workflow_run_id": metadata["job_id"],
            "task_revision": metadata["task_revision"] + 1}):
        raise BoardError("pipeline_task_changed", "Original report link changed", status_code=409)
    from src.db.models import WorkflowRunState
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == metadata["job_id"])
        .execution_options(populate_existing=True))
    if run is None or task.idempotency_binding != run.idempotency_binding:
        raise BoardError("pipeline_task_changed", "Original report link binding changed", status_code=409)
    before_task = task.model_copy(update={"task_revision": metadata["task_revision"],
        "updated_at": datetime.fromisoformat(metadata["task_updated_at"]),
        "idempotency_binding": metadata["task_idempotency_binding"]})
    before_attempt = attempt.model_copy(update={"workflow_run_id": None, "cancel_requested_at": None,
        "updated_at": datetime.fromisoformat(metadata["attempt_updated_at"])})
    if row_token(before_task) != metadata["task_token"] or row_token(before_attempt) != metadata["attempt_token"]:
        raise BoardError("pipeline_task_changed", "Original report link changed another field", status_code=409)


async def _report_cancel_event(db, task, attempt, metadata):
    events = (await db.scalars(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == task.owner_principal_id,
        WorkBoardEvent.owner_session_id == task.owner_session_id,
        WorkBoardEvent.kind == "attempt.cancel_requested"))).all()
    selected = [event for event in events if json.loads(event.metadata_json).get("attempt_id") == attempt.attempt_id]
    if len(selected) != 1 or attempt.cancel_requested_at is None:
        raise BoardError("pipeline_task_changed", "Unique original report cancel intent required", status_code=409)
    event = selected[0]
    value = json.loads(event.metadata_json)
    if (set(value) != {"cancel_key", "attempt_id", "workflow_run_id", "task_revision", "recovery_action",
        "request_identity", "board_fence", "lease_owner"}
        or value["cancel_key"] != f"work-board-cancel:{task.task_id}:{attempt.attempt_id}"
        or value["workflow_run_id"] != metadata["job_id"] or value["task_revision"] != metadata["task_revision"] + 2
        or value["recovery_action"] != "reconcile_external_effect" or value["board_fence"] != attempt.fencing_token
        or value["lease_owner"] != attempt.lease_owner or event.actor_principal_id != task.owner_principal_id
        or event.actor_session_id != task.owner_session_id):
        raise BoardError("pipeline_task_changed", "Original report cancel intent changed", status_code=409)
    return event


@dataclass(frozen=True)
class ReportIdentityWitness:
    candidate: ReportAdmissionCandidate
    task_token: str
    attempt_token: str
    input_digest: str
    expected_bytes: bytes
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


def _identity_witness(candidate, task, attempt, inputs):
    from src.work_board.pipelines import row_token
    from src.workflows.job_runtime import _composition_fingerprint, _digest
    spec = candidate.original_spec
    expected = {"job_id": spec.identity.job_id, "owner_principal_id": spec.identity.owner_principal_id,
        "owner_kind": "user", "job_kind": REPORT, "capability_version": "1",
        "operator_session_id": spec.operator_session_id, "goal_id": spec.goal_id,
        "goal_revision": spec.goal_revision, "plan_revision": spec.plan_revision,
        "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
        "run_fingerprint": _composition_fingerprint(spec, _digest(spec.inputs)),
        "idempotency_scope": spec.identity.idempotency_scope, "idempotency_key": spec.identity.idempotency_key}
    proof = ReportIdentityWitness(candidate, row_token(task), row_token(attempt), digest(dict(inputs)), canonical_bytes(expected))
    return _issue_source(proof, (candidate,))


def report_identity_for_projection(task, attempt, inputs, projection):
    from src.work_board.pipelines import row_token
    proof = projection.get("_report_identity")
    if type(proof) is not ReportIdentityWitness:
        raise BoardError("pipeline_task_changed", "Original staged report identity required", status_code=409)
    _require_source(proof, (proof.candidate,))
    _require_source(proof.candidate, (proof.candidate.original_spec, proof.candidate.current_witness))
    if (proof.task_token != row_token(task) or proof.attempt_token != row_token(attempt)
        or proof.input_digest != digest(dict(inputs))):
        raise BoardError("pipeline_task_changed", "Original staged report identity changed", status_code=409)
    return json.loads(proof.expected_bytes)


async def _restage_original_candidate(db, task, attempt, inputs, run):
    from src.auth.service import authenticate_principal
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.work_board.pipeline_cpu import spec_for
    from src.workflows.job_runtime import _composition_fingerprint, _digest
    metadata = read_report_candidate(run)
    cancelled = attempt.cancel_requested_at is not None
    current = await stage_report_current(db, task, attempt, inputs, _cancel_identity=cancelled)
    await _recheck_report_birth_link(db, task, attempt, metadata, cancel_identity=cancelled)
    principal = await authenticate_principal(task.owner_principal_id, db=db)
    original = spec_for(task, attempt, inputs, deadline=datetime.fromisoformat(metadata["cutoff"]))
    authority = {**original.declared_authority,
        "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)}
    original = replace(original, declared_authority=authority,
        composition_binding=RuntimeCompositionBinding.from_json(run.composition_binding_json),
        run_fingerprint=digest({"task_ref": task.task_id, "attempt_ref": attempt.attempt_id,
            "inputs": original.inputs, "authority": authority}))
    if (_digest(original.inputs) != metadata["input_digest"] or _digest(authority) != metadata["authority_digest"]
        or _composition_fingerprint(original, _digest(original.inputs)) != metadata["run_fingerprint"]):
        raise BoardError("pipeline_input_changed", "Original report spec changed", status_code=409)
    return _issue_source(ReportAdmissionCandidate(original, current, canonical_bytes(metadata)), (original, current))


async def lookup_original_report_binding(task, attempt, inputs, jobs, session_provider):
    from src.db.models import WorkflowRunState
    from src.work_board.pipeline_cpu import job_id
    async with session_provider() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id(task, attempt))
            .execution_options(populate_existing=True))
        if run is None:
            return None
        candidate = await _restage_original_candidate(db, task, attempt, inputs, run)
        proof = _identity_witness(candidate, task, attempt, inputs)
    projection = await jobs.get_job(job_id(task, attempt))
    return {**projection, "_report_identity": proof}


async def recheck_report_claim(db, run, host_boot_nonce, *, candidate):
    if type(candidate) is not ReportAdmissionCandidate:
        raise BoardError("pipeline_input_changed", "Actual staged report claim source required", status_code=409)
    metadata = read_report_candidate(run)
    birth = next(item for item in _journal(run.checkpoint_receipts_json)
        if item["checkpoint_id"] == REPORT_CHECKPOINT_IDS[0])
    if metadata != candidate.metadata() or birth["payload"]["host_boot_nonce"] != host_boot_nonce:
        raise BoardError("pipeline_input_changed", "Original report claim source changed", status_code=409)
    await validate_report_spec(db, candidate.original_spec, candidate, host_boot_nonce)
    preflight_report_journal(_journal(run.checkpoint_receipts_json))


class _ReportResource:
    """Private original invocation owner; never restored from journal JSON."""
    def __init__(self, candidate, *, jobs, session_provider):
        _require_source(candidate, (candidate.original_spec, candidate.current_witness))
        self.candidate = candidate
        self.jobs = jobs
        self.session_provider = session_provider
        self.scope = None
        self.callback = None
        self.file_writer = None
        self.artifact = None
        self.terminal = None
        self.cancel_interruption = None
        self._identity = id(self)
        self._candidate_identity = candidate


def _report_resource(scope):
    from .dispatch import OriginalServiceInvocation
    resource = scope.native_report_resource if type(scope) is OriginalServiceInvocation else None
    if (type(resource) is not _ReportResource or resource._identity != id(resource)
        or resource.scope is not scope or resource.candidate is not resource._candidate_identity):
        raise BoardError("pipeline_task_changed", "Original report resource unavailable", status_code=409)
    _require_source(resource.candidate, (resource.candidate.original_spec, resource.candidate.current_witness))
    return resource


def _register_report_resource(jobs, resource):
    """Reserve one bounded locator before claim; a locator grants no authority."""
    if (type(resource) is not _ReportResource or resource._identity != id(resource)
        or resource.candidate is not resource._candidate_identity):
        raise BoardError("pipeline_task_changed", "Original report owner required", status_code=409)
    _require_source(resource.candidate, (resource.candidate.original_spec, resource.candidate.current_witness))
    retained = getattr(jobs, "_native_report_resources", None)
    if retained is None:
        retained = jobs._native_report_resources = {}
    key = resource.candidate.metadata()["job_id"]
    if key in retained:
        if retained[key] is not resource:
            raise BoardError("pipeline_task_changed", "Original report owner cannot be replaced", status_code=409)
        return
    if len(retained) >= 32:
        raise BoardError("pipeline_task_changed", "Original report resource capacity unavailable", status_code=409)
    retained[key] = resource


def _release_report_resource(jobs, resource, projection):
    artifact = resource.artifact
    if (projection["job_id"] != resource.candidate.metadata()["job_id"]
        or projection["status"] not in {"succeeded", "cancelled"} or resource.terminal is None
        or artifact is None or resource.callback is not artifact.callback_owner
        or not resource.callback.done() or resource.callback.cancelled() or resource.callback.exception() is not None):
        raise BoardError("pipeline_output_unverified", "Original report terminal closure required", status_code=409)
    validate_report_terminal_request(resource.terminal, projection["job_id"], projection["status"])
    from src.work_board.input_artifacts import _verified_payload_closure
    _verified_payload_closure(artifact.closure_owner)
    retained = jobs._native_report_resources
    if retained.get(projection["job_id"]) is not resource:
        raise BoardError("pipeline_task_changed", "Original report owner locator changed", status_code=409)
    del retained[projection["job_id"]]


async def _recheck_report_scope(db, run, scope):
    from .dispatch import _witness, _ms
    from .ownership import validate_invocation
    from src.auth.service import authenticate_principal
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    resource = _report_resource(scope)
    original = dict(scope.witness)
    if (_witness(run, scope) != original or run.job_kind != REPORT or run.status != "running"
        or run.attempt_count != 1 or run.lease_owner != original["lease_owner"]
        or run.fencing_token != original["fencing_token"] or run.input_digest != original["input_digest"]
        or run.authority_digest != original["authority_digest"] or run.run_fingerprint != original["run_fingerprint"]
        or _ms(run.deadline_at) != original["original_deadline_at"]
        or original["original_deadline_at"] <= int(datetime.now(timezone.utc).timestamp() * 1000)
        or run.lease_expires_at is None
        or run.lease_expires_at.replace(tzinfo=run.lease_expires_at.tzinfo or timezone.utc) <= datetime.now(timezone.utc)
        or run.composition_binding_json != scope.binding.to_json()
        or read_report_candidate(run) != resource.candidate.metadata()):
        raise BoardError("pipeline_task_changed", "Original report invocation changed", status_code=409)
    now = datetime.now(timezone.utc)
    root = await db.scalar(select(OperatorSession).where(OperatorSession.id == run.operator_session_id,
        OperatorSession.principal_id == run.owner_principal_id, OperatorSession.revoked_at.is_(None),
        OperatorSession.replaced_by_id.is_(None), OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now))
    operator = await authenticate_principal(run.owner_principal_id, db=db)
    if root is None or json.loads(run.declared_authority_json).get("grants") != sorted(
        str(getattr(grant, "value", grant)) for grant in operator.principal.grants):
        raise BoardError("pipeline_root_changed", "Original report Root or grants changed", status_code=409)
    await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
        owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
        session_id=run.operator_session_id, authority=json.loads(run.declared_authority_json))
    await validate_invocation(db, scope.binding)
    return resource


async def recheck_report_current(db, run, *, phase, witness, file_path, content_digest):
    if phase not in {"artifact", "readback"} or type(witness) is not ReportArtifactWitness:
        raise BoardError("pipeline_output_unverified", "Actual report artifact witness required", status_code=409)
    _require_source(witness, (witness.current_witness, witness.closure_owner, witness.original_scope, witness.callback_owner))
    from src.work_board.input_artifacts import _verified_payload_closure
    _verified_payload_closure(witness.closure_owner)
    resource = await _recheck_report_scope(db, run, witness.original_scope)
    if (resource.artifact is not witness or resource.callback is not witness.callback_owner
        or file_path != witness.output_reference or content_digest != witness.output_digest):
        raise BoardError("pipeline_output_unverified", "Original report artifact changed", status_code=409)
    active = await db.get(WorkBoardAttempt, witness.current_witness.attempt_id, populate_existing=True)
    if active is not None and active.cancel_requested_at is not None:
        await _raise_original_cancel_interruption(db, run, resource, witness, phase)
    task, attempt = await recheck_report_witness(db, witness=witness.current_witness)
    await _recheck_report_birth_link(db, task, attempt, resource.candidate.metadata())
    history = _journal(run.checkpoint_receipts_json)
    if not any(item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[1] for item in history):
        raise BoardError("pipeline_output_unverified", "Original report source admission required", status_code=409)
    preflight_report_journal(history)


async def prepare_report_publication(db, run, *, phase, candidate, original_scope):
    resource = await _recheck_report_scope(db, run, original_scope)
    metadata = resource.candidate.metadata()
    if phase == "source":
        if candidate is not resource.candidate.current_witness or type(candidate) is not ReportCurrentWitness:
            raise BoardError("pipeline_input_changed", "Actual report checkpoint source required", status_code=409)
        task, attempt = await recheck_report_witness(db, witness=candidate)
        await _recheck_report_birth_link(db, task, attempt, metadata)
        payload = {"schema_version": 1, "context_tag": REPORT_CONTEXT_TAG, "phase": phase,
            "job_id": run.run_identity, "candidate_digest": resource.candidate.candidate_digest,
            "task_id": candidate.task_id, "attempt_id": candidate.attempt_id,
            "input_id": candidate.input_id, "payload_sha256": hashlib.sha256(candidate.input_payload).hexdigest(),
            "producer_sha256": candidate.producer_witness.content_sha256, "no_learning": True}
    elif phase == "invoke":
        if candidate is not resource.artifact or type(candidate) is not ReportArtifactWitness:
            raise BoardError("pipeline_output_unverified", "Actual report invocation required", status_code=409)
        await recheck_report_current(db, run, phase="readback", witness=candidate,
            file_path=candidate.output_reference, content_digest=candidate.output_digest)
        artifacts = _journal(run.artifact_receipts_json)
        effects = _journal(run.effect_receipts_json)
        if (not any(item.get("file_path") == candidate.output_reference and item.get("exists") is True
            and item.get("content_sha256") == candidate.output_digest for item in artifacts)
            or not any(item.get("target_path") == candidate.output_reference
                and item.get("content_sha256") == candidate.output_digest and item.get("receipt_kind") == "readback"
                and item.get("status") == "succeeded" for item in effects)):
            raise BoardError("pipeline_output_unverified", "Actual report readback required", status_code=409)
        payload = {"schema_version": 1, "context_tag": REPORT_CONTEXT_TAG, "phase": phase,
            "job_id": run.run_identity, "candidate_digest": resource.candidate.candidate_digest,
            "output_reference": candidate.output_reference, "output_sha256": candidate.output_digest,
            "size_bytes": len(candidate.output_bytes), "file_closed": True, "readback_verified": True, "no_learning": True}
    else:
        raise ValueError("fixed report source or invocation phase required")
    receipt, _next = _issue_publication(db, run, phase=phase, source=candidate, payload=payload)
    return receipt


@dataclass(frozen=True)
class ReportTerminalPublication:
    original_scope: Any
    artifact: ReportArtifactWitness
    terminal_witness: Any
    readback_witness: Any
    status: str
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class ReportCancellationWitness:
    current_witness: ReportCurrentWitness
    event_id: int
    event_token: str
    _issuance: Any = field(default=None, init=False, repr=False, compare=False)


class _ReportCancelInterruption(BoardError):
    def __init__(self, resource, artifact, event_id, event_token, phase):
        super().__init__("pipeline_task_changed", "Original report cancellation fenced late adoption", status_code=409)
        self.resource = resource
        self.artifact = artifact
        self.event_id = event_id
        self.event_token = event_token
        self.phase = phase
        self._issuance = None


async def _raise_original_cancel_interruption(db, run, resource, artifact, phase):
    from src.work_board.pipelines import row_token, task_guard
    from src.work_board.review import recheck_pipeline_producer_readback
    current = artifact.current_witness
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == current.task_id)
        .execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, current.attempt_id, populate_existing=True)
    if task is None or attempt is None:
        raise BoardError("pipeline_task_changed", "Original report cancellation Task or Attempt unavailable", status_code=409)
    await _recheck_report_birth_link(db, task, attempt, resource.candidate.metadata(), cancel_identity=True)
    for cls, identifier, token in ((WorkBoardInputArtifact, current.input_id, current.input_token),
        (WorkBoardLink, current.link_id, current.link_token), (WorkBoardHandoff, current.handoff_id, current.handoff_token)):
        row = await db.get(cls, identifier, populate_existing=True)
        if row is None or row_token(row) != token:
            raise BoardError("pipeline_input_changed", "Report source changed during cancellation", status_code=409)
    operation, value = await task_guard(db, task, attempt=attempt,
        workspace_identity=current.workspace_identity, source_witness=current.source_witness)
    if (operation.revision != current.operation_revision or operation.proposal_digest != current.operation_digest
        or value["accepted_digest"] != current.accepted_digest):
        raise BoardError("pipeline_plan_changed", "Report plan changed during cancellation", status_code=409)
    await recheck_pipeline_producer_readback(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
        witness=current.producer_witness)
    event = await _report_cancel_event(db, task, attempt, resource.candidate.metadata())
    error = _ReportCancelInterruption(resource, artifact, event.event_id, row_token(event), phase)
    _issue_source(error, (resource, artifact))
    resource.cancel_interruption = error
    raise error


def validate_report_terminal_request(terminal, job_id, to_status):
    from src.work_board.pipelines import PipelineTerminalWitness
    expected = PipelineTerminalWitness if to_status == "succeeded" else ReportCancellationWitness
    if (type(terminal) is not ReportTerminalPublication or terminal.status != to_status
        or to_status not in {"succeeded", "cancelled"} or type(terminal.terminal_witness) is not expected
        or terminal.original_scope.witness["invocation_ref"] != job_id):
        raise BoardError("pipeline_output_unverified", "Original report terminal source required", status_code=409)
    _require_source(terminal, (terminal.original_scope, terminal.artifact, terminal.terminal_witness))
    resource = _report_resource(terminal.original_scope)
    if resource.terminal is not terminal or resource.artifact is not terminal.artifact:
        raise BoardError("pipeline_output_unverified", "Original report terminal owner changed", status_code=409)


async def prepare_report_terminal_publication(db, run, *, terminal, to_status):
    validate_report_terminal_request(terminal, run.run_identity, to_status)
    artifact = terminal.artifact
    resource = await _recheck_report_scope(db, run, terminal.original_scope)
    from src.work_board.pipeline_cpu import validate_native_report_readback
    if (resource.callback is not artifact.callback_owner or not resource.callback.done()
        or resource.callback.cancelled() or resource.callback.exception() is not None
        or resource.file_writer is None or not resource.file_writer.done() or resource.file_writer.cancelled()
        or resource.file_writer.exception() is not None
        or terminal.readback_witness.owner is not artifact.closure_owner
        or validate_native_report_readback(terminal.readback_witness) != artifact.output_bytes):
        raise BoardError("pipeline_output_unverified", "Actual report callback and readback closure required", status_code=409)
    if to_status == "succeeded":
        await recheck_report_current(db, run, phase="readback", witness=artifact,
            file_path=artifact.output_reference, content_digest=artifact.output_digest)
    else:
        await _recheck_report_cancel_source(db, run, terminal.terminal_witness, terminal.original_scope)
        from src.work_board.input_artifacts import _verified_payload_closure
        _require_source(artifact, (artifact.current_witness, artifact.closure_owner, artifact.original_scope, artifact.callback_owner))
        _verified_payload_closure(artifact.closure_owner)
        if not any(item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[2] for item in _journal(run.checkpoint_receipts_json)):
            interruption = resource.cancel_interruption
            if type(interruption) is not _ReportCancelInterruption:
                raise BoardError("pipeline_output_unverified", "Original cancellation interruption required", status_code=409)
            _require_source(interruption, (resource, artifact))
            if (interruption.event_id != terminal.terminal_witness.event_id
                or interruption.event_token != terminal.terminal_witness.event_token
                or interruption.phase not in {"artifact", "readback"}):
                raise BoardError("pipeline_task_changed", "Original cancellation interruption changed", status_code=409)
    if to_status == "succeeded" and not any(item.get("checkpoint_id") == REPORT_CHECKPOINT_IDS[2] for item in _journal(run.checkpoint_receipts_json)):
        raise BoardError("pipeline_output_unverified", "Original report invocation receipt required", status_code=409)
    phase = "outcome" if to_status == "succeeded" else "cleanup"
    payload = {"schema_version": 1, "context_tag": REPORT_CONTEXT_TAG, "phase": phase,
        "job_id": run.run_identity, "candidate_digest": resource.candidate.candidate_digest,
        "output_reference": artifact.output_reference, "output_sha256": artifact.output_digest,
        "callback_closed": True, "file_closed": True, "readback_verified": True, "no_learning": True}
    _receipt, journal = _issue_publication(db, run, phase=phase, source=terminal, payload=payload)
    return journal


async def _recheck_report_cancel_source(db, run, witness, scope):
    from src.work_board.pipelines import row_token, task_guard
    from src.work_board.review import recheck_pipeline_producer_readback
    _require_source(witness, (witness.current_witness,))
    current = witness.current_witness
    _require_source(current, (current.source_witness, current.producer_witness))
    rows = []
    for cls, identifier, token in ((WorkBoardTask, current.task_id, current.task_token),
        (WorkBoardAttempt, current.attempt_id, current.attempt_token),
        (WorkBoardInputArtifact, current.input_id, current.input_token), (WorkBoardLink, current.link_id, current.link_token),
        (WorkBoardHandoff, current.handoff_id, current.handoff_token)):
        row = (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == identifier)
            .execution_options(populate_existing=True)) if cls is WorkBoardTask
            else await db.get(cls, identifier, populate_existing=True))
        if row is None or row_token(row) != token:
            raise BoardError("pipeline_task_changed", "Original report cancellation source changed", status_code=409)
        rows.append(row)
    task, attempt, artifact, link, handoff = rows
    resource = _report_resource(scope)
    metadata = resource.candidate.metadata()
    event = await _report_cancel_event(db, task, attempt, metadata)
    if (event.event_id != witness.event_id or row_token(event) != witness.event_token or attempt.ended_at
        or attempt.workflow_run_id != run.run_identity or artifact.bound_task_id != task.task_id
        or hashlib.sha256(current.input_payload).hexdigest() != artifact.payload_sha256
        or link.current_handoff_id != handoff.handoff_id):
        raise BoardError("pipeline_task_changed", "Original report cancellation binding changed", status_code=409)
    await _recheck_report_birth_link(db, task, attempt, metadata, cancel_identity=True)
    operation, value = await task_guard(db, task, attempt=attempt,
        workspace_identity=current.workspace_identity, source_witness=current.source_witness)
    if (operation.revision != current.operation_revision or operation.proposal_digest != current.operation_digest
        or value["accepted_digest"] != current.accepted_digest or value["deadline_at"] != current.operation_deadline):
        raise BoardError("pipeline_plan_changed", "Original report cancellation plan changed", status_code=409)
    await recheck_pipeline_producer_readback(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
        witness=current.producer_witness)


async def cancel_report_job(jobs, job_id, *, original_scope, cancel_candidate, cleanup_witness, expected_revision):
    validate_report_terminal_request(cancel_candidate, job_id, "cancelled")
    if cancel_candidate.original_scope is not original_scope or cancel_candidate.terminal_witness is not cleanup_witness:
        raise BoardError("pipeline_task_changed", "Original report cancellation owner changed", status_code=409)
    async def cancel_check(db, run):
        await _recheck_report_scope(db, run, original_scope)
        await _recheck_report_cancel_source(db, run, cleanup_witness, original_scope)
    return await jobs.transition_job(job_id, "cancelled", owner=original_scope.witness["lease_owner"],
        fencing_token=original_scope.witness["fencing_token"], expected_state="running", expected_revision=expected_revision,
        reason="operator_cancelled", cancellation_authority_check=cancel_check, _native_report_terminal=cancel_candidate)


async def settle_report_job(jobs, job_id, *, original_scope, outcome_candidate, terminal_witness):
    from src.work_board.pipelines import recheck_cpu_terminal_locked
    validate_report_terminal_request(outcome_candidate, job_id, "succeeded")
    if outcome_candidate.original_scope is not original_scope or outcome_candidate.terminal_witness is not terminal_witness:
        raise BoardError("pipeline_output_unverified", "Original report settlement source changed", status_code=409)
    async def terminal_check(db, run):
        await recheck_cpu_terminal_locked(db, run, witness=terminal_witness)
    return await jobs.transition_job(job_id, "succeeded", owner=original_scope.witness["lease_owner"],
        fencing_token=original_scope.witness["fencing_token"], expected_state="running",
        result={"status": "succeeded", "no_learning": True, "output_sha256": outcome_candidate.artifact.output_digest},
        terminal_authority_check=terminal_check, _native_report_terminal=outcome_candidate)


async def dispatch_report_service(dispatcher, frame, call_scope):
    try:
        return await _dispatch_report_service(dispatcher, frame, call_scope)
    except _ReportCancelInterruption as original:
        from .contracts import blocked
        resource = _report_resource(call_scope.original)
        _require_source(original, (resource, resource.artifact))
        if resource.cancel_interruption is not original or original.resource is not resource:
            raise
        return blocked("native_report_cancelled_before_adoption")


async def _dispatch_report_service(dispatcher, frame, call_scope):
    from .contracts import succeeded
    from src.work_board.pipeline_cpu import invoke_native_report
    scope = call_scope.original
    resource = _report_resource(scope)
    method = frame["method"]
    metadata = resource.candidate.metadata()
    job_id = metadata["job_id"]
    if method == "tasks.cancel":
        if (frame["payload"].get("job_ref") != job_id or resource.terminal is None
            or resource.terminal.status != "cancelled"):
            raise BoardError("pipeline_task_changed", "Original report cancellation closure required", status_code=409)
        projection = await dispatcher.jobs.cancel_task_capability(job_id, original_scope=scope,
            cancel_candidate=resource.terminal, cleanup_witness=resource.terminal.terminal_witness,
            expected_revision=frame["payload"]["expected_revision"])
        return succeeded(method, {"job_ref": job_id, "revision": projection["revision"], "state": projection["status"]})
    async with dispatcher.jobs._session() as db:
        run, _claim, _binding = await dispatcher._current_in_db(db, job_id, method, scope)
        await _recheck_report_scope(db, run, scope)
        preflight_report_journal(_journal(run.checkpoint_receipts_json))
        if method != "tasks.cancel":
            task, attempt = await recheck_report_witness(db, witness=resource.candidate.current_witness)
            await _recheck_report_birth_link(db, task, attempt, metadata)
    candidate_digest = resource.candidate.candidate_digest
    if method == "tasks.admit":
        if frame["payload"] != {"request_ref": report_ref(job_id, method, candidate_digest)}:
            raise BoardError("pipeline_input_changed", "Original report admission locator changed", status_code=409)
        from .bridge import cordis_host
        projection = await dispatcher.jobs.admit_task_capability_job(resource.candidate.original_spec,
            candidate=resource.candidate, host=cordis_host)
        if projection["job_id"] != job_id or projection["attempt_count"] != 1:
            raise BoardError("pipeline_task_changed", "Original report admission replay changed", status_code=409)
        return succeeded(method, {"job_ref": job_id, "revision": projection["revision"],
            "state": projection["status"], "replayed": True})
    if method == "tasks.checkpoint":
        if frame["payload"] != {"checkpoint_ref": report_ref(job_id, method, candidate_digest)}:
            raise BoardError("pipeline_input_changed", "Original report checkpoint locator changed", status_code=409)
        projection = await dispatcher.jobs.record_task_capability_checkpoint(job_id, original_scope=scope,
            checkpoint_candidate=resource.candidate.current_witness)
        return succeeded(method, {"receipt_ref": REPORT_CHECKPOINT_IDS[1], "revision": projection["revision"]})
    if method == "capabilities.invoke":
        if frame["payload"] != {"request_ref": report_ref(job_id, method, candidate_digest)} or resource.callback is not None:
            raise BoardError("pipeline_task_changed", "Original report invocation cannot be replayed", status_code=409)
        resource.callback = asyncio.current_task()
        witness = await invoke_native_report(resource.candidate.current_witness, scope, jobs=dispatcher.jobs)
        await dispatcher.jobs.record_task_capability_invocation(job_id, original_scope=scope, invocation_witness=witness)
        return succeeded(method, {"receipt_ref": REPORT_CHECKPOINT_IDS[2], "artifact_refs": ["report-output:" + witness.output_digest]})
    if method == "tasks.settle":
        if frame["payload"] != {"outcome_ref": report_ref(job_id, method, candidate_digest)} or resource.terminal is None:
            raise BoardError("pipeline_output_unverified", "Original report outcome locator required", status_code=409)
        projection = await dispatcher.jobs.settle_task_capability(job_id, original_scope=scope,
            outcome_candidate=resource.terminal, terminal_witness=resource.terminal.terminal_witness)
        return succeeded(method, {"job_ref": job_id, "revision": projection["revision"], "state": projection["status"]})
    raise BoardError("pipeline_task_changed", "Original report cancellation closure unavailable", status_code=409)


async def execute_report(task, attempt, inputs, *, jobs, runner, deadline, admission_only, session_provider):
    from .bridge import cordis_host as host
    from .ownership import bind_invocation
    from .dispatch import capture_original_scope, NativeServiceBlocked
    from src.db.models import WorkflowRunState
    from src.work_board.pipeline_cpu import job_id, read_native_report_output
    from src.work_board.pipelines import stage_cpu_terminal
    if not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_report_host_unavailable")
    original_job_id = job_id(task, attempt)
    async with session_provider() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == original_job_id))
        if run is None:
            binding = await bind_invocation(db, method="tasks.admit", native_branch="artifact",
                goal_bound=True, reviewed_composition=host.reviewed)
            candidate = await stage_report_admission(db, task, attempt, inputs, deadline=deadline, binding=binding)
        else:
            candidate = await _restage_original_candidate(db, task, attempt, inputs, run)
    if run is None:
        projection = await jobs.admit_task_capability_job(candidate.original_spec, candidate=candidate, host=host)
    else:
        projection = await jobs.get_job(original_job_id)
    if admission_only or projection["status"] != "accepted":
        return {**projection, "_report_identity": _identity_witness(candidate, task, attempt, inputs),
            "admission_only": admission_only}
    resource = _ReportResource(candidate, jobs=jobs, session_provider=session_provider)
    _register_report_resource(jobs, resource)
    await jobs.queue_job(original_job_id, expected_revision=projection["revision"], reason="evidence_cpu_board_linked")
    claim = await jobs.claim_service_job(original_job_id, host=host, owner=runner,
        lease_seconds=30, native_report_candidate=candidate)
    scope = replace(capture_original_scope(claim, host), native_report_resource=resource)
    resource.scope = scope
    for method, field_name in (("tasks.admit", "request_ref"), ("tasks.checkpoint", "checkpoint_ref"),
        ("capabilities.invoke", "request_ref")):
        result = await host.request_service(method, {field_name: report_ref(original_job_id, method, candidate.candidate_digest)},
            original_scope=scope)
        if result["status"] != "succeeded":
            raise NativeServiceBlocked(result["reason_code"])
    remaining = max(0, (scope.deadline_at / 1000) - datetime.now(timezone.utc).timestamp())
    async with asyncio.timeout(remaining):
        await asyncio.shield(resource.callback)
    artifact = resource.artifact
    terminal_witness = await stage_cpu_terminal(task, attempt, inputs, output_reference=artifact.output_reference,
        output_digest=artifact.output_digest, session_provider=session_provider)
    terminal = ReportTerminalPublication(scope, artifact, terminal_witness,
        read_native_report_output(artifact.output_reference, artifact.output_digest, artifact.closure_owner), "succeeded")
    _issue_source(terminal, (scope, artifact, terminal_witness))
    resource.terminal = terminal
    result = await host.request_service("tasks.settle",
        {"outcome_ref": report_ref(original_job_id, "tasks.settle", candidate.candidate_digest)}, original_scope=scope)
    if result["status"] != "succeeded":
        raise NativeServiceBlocked(result["reason_code"])
    projection = await jobs.get_job(original_job_id)
    _release_report_resource(jobs, resource, projection)
    return {**projection, "_report_identity": _identity_witness(candidate, task, attempt, inputs), "admission_only": False}


async def cancel_report_execution(task, attempt, inputs, projection, *, jobs, session_provider, reason):
    """Only the retained native callback/file owner can prove cancellation."""
    from .bridge import cordis_host as host
    from src.work_board.pipeline_cpu import job_id, read_native_report_output, validate_native_report_readback
    from src.work_board.pipelines import row_token
    key = job_id(task, attempt)
    resource = getattr(jobs, "_native_report_resources", {}).get(key)
    if (type(resource) is not _ReportResource or resource.scope is None or resource.callback is None
        or not host.admitting or host.boot_nonce != resource.scope.host_boot_nonce):
        return [{"job_id": key, "status": "unknown_external_effect", "reason": "original_report_closure_unavailable"}], False
    scope = resource.scope
    _report_resource(scope)
    remaining = max(0, scope.deadline_at / 1000 - datetime.now(timezone.utc).timestamp())
    try:
        async with asyncio.timeout(remaining):
            if resource.file_writer is not None:
                await asyncio.shield(resource.file_writer)
            await asyncio.shield(resource.callback)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        return [{"job_id": key, "status": "unknown_external_effect", "reason": "original_report_callback_cancelled"}], False
    except Exception:
        # Retain the actual original tasks and their exceptions. A timeout or
        # failed owner is uncertainty, never permission to cancel or replay.
        return [{"job_id": key, "status": "unknown_external_effect", "reason": "original_report_closure_unproven"}], False
    artifact = resource.artifact
    if artifact is None:
        return [{"job_id": key, "status": "unknown_external_effect", "reason": "original_report_file_closure_unavailable"}], False
    from src.work_board.input_artifacts import _verified_payload_closure
    _require_source(artifact, (artifact.current_witness, artifact.closure_owner, artifact.original_scope, artifact.callback_owner))
    _verified_payload_closure(artifact.closure_owner)
    readback = read_native_report_output(artifact.output_reference, artifact.output_digest, artifact.closure_owner)
    actual = validate_native_report_readback(readback)
    if actual != artifact.output_bytes:
        raise BoardError("pipeline_output_unverified", "Original cancelled report readback changed", status_code=409)
    async with session_provider() as db:
        current = await stage_report_current(db, task, attempt, inputs, _cancel_identity=True)
        event = await _report_cancel_event(db, task, attempt, resource.candidate.metadata())
        cleanup = ReportCancellationWitness(current, event.event_id, row_token(event))
        _issue_source(cleanup, (current,))
    terminal = ReportTerminalPublication(scope, artifact, cleanup, readback, "cancelled")
    _issue_source(terminal, (scope, artifact, cleanup))
    resource.terminal = terminal
    original = await jobs.get_job(key)
    result = await host.request_service("tasks.cancel",
        {"job_ref": key, "expected_revision": original["revision"]}, original_scope=scope)
    if result["status"] != "succeeded" or result["value"]["state"] != "cancelled":
        return [{"job_id": key, "status": "unknown_external_effect", "reason": "original_report_cancel_unconfirmed"}], False
    closed = await jobs.get_job(key)
    _release_report_resource(jobs, resource, closed)
    return [{"job_id": key, "status": "cancelled", "reason": reason, "no_learning": True}], True
