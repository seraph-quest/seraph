"""Fixed document-build producer on the original C1 child and private row."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import uuid
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sqlalchemy import select
from src.db.models import WorkBoardTask, WorkBoardAttempt
from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import digest
from src.work_board.repository import BoardError
from src.work_board.pipelines import now, utc

_SEAL = object()

_Reference = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")]
_Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_U63 = Annotated[int, Field(ge=1, le=2**63-1)]
_STDOUT_MAX = 2*4194304+65536+12+4096


class DocumentSupervisionV1(BaseModel):
    """Private physical facts, never a callback outcome or execution grant."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal["document-supervision.v1"]
    build_id: str = Field(min_length=36, max_length=36, pattern=r"^[a-f0-9-]{36}$")
    original_build_revision: _U63
    original_build_metadata_digest: _Hash
    capacity_digest: _Hash
    native_binding_digest: _Hash
    task_id: _Reference
    attempt_id: _Reference
    parent_job_id: _Reference
    child_job_id: _Reference
    child_fence: _U63
    generation: Literal[1]
    nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    supervisor_pid: int = Field(ge=1, le=2**31-1)
    parser_pid: int = Field(ge=1, le=2**31-1)
    supervisor_exit: int = Field(ge=-2**31, le=2**31-1)
    stdin_closed: Literal[True]
    stdout_eof: Literal[True]
    supervisor_wait_reaped: Literal[True]
    stdout_size: int = Field(ge=0, le=_STDOUT_MAX)
    stdout_sha256: _Hash
    parser_witness_name: str = Field(min_length=45, max_length=45, pattern=r"^[a-f0-9]{32}\.witness\.json$")
    parser_witness_size: int = Field(ge=1, le=4096)
    parser_witness_sha256: _Hash
    parser_witness_device: int = Field(ge=0, le=2**64-1)
    parser_witness_inode: int = Field(ge=0, le=2**64-1)
    parser_witness_mode: Literal[33152]
    parser_witness_uid: int = Field(ge=0, le=2**32-1)
    parser_witness_nlink: Literal[1]

    @model_validator(mode="before")
    @classmethod
    def exact_primitive_types(cls, value):
        if type(value) is not dict:
            raise ValueError("closed supervision object required")
        boolean_fields = {"stdin_closed","stdout_eof","supervisor_wait_reaped"}
        integer_fields = {"original_build_revision","child_fence","generation","supervisor_pid",
            "parser_pid","supervisor_exit","stdout_size","parser_witness_size","parser_witness_device",
            "parser_witness_inode","parser_witness_mode","parser_witness_uid","parser_witness_nlink"}
        if any(key in value and type(value[key]) is not bool for key in boolean_fields) or any(
                key in value and type(value[key]) is not int for key in integer_fields):
            raise ValueError("exact supervision primitive types required")
        return value


def supervision_maximum():
    maximum = {"version": "document-supervision.v1", "build_id": "f"*36,
        "original_build_revision": 2**63-1, "original_build_metadata_digest": "f"*64,
        "capacity_digest": "f"*64, "native_binding_digest": "f"*64,
        **{key: "x"*128 for key in ("task_id", "attempt_id", "parent_job_id", "child_job_id")},
        "child_fence": 2**63-1, "generation": 1, "nonce": "f"*32,
        "supervisor_pid": 2**31-1, "parser_pid": 2**31-1, "supervisor_exit": -2**31,
        "stdin_closed": True, "stdout_eof": True, "supervisor_wait_reaped": True,
        "stdout_size": _STDOUT_MAX, "stdout_sha256": "f"*64,
        "parser_witness_name": "f"*32+".witness.json", "parser_witness_size": 4096,
        "parser_witness_sha256": "f"*64, "parser_witness_device": 2**64-1,
        "parser_witness_inode": 2**64-1, "parser_witness_mode": 33152,
        "parser_witness_uid": 2**32-1, "parser_witness_nlink": 1}
    return {"receipt": DocumentSupervisionV1.model_validate(maximum).model_dump(mode="json"), "mac": "f"*64}


SUPERVISION_MAX_BYTES = 1781


@dataclass(frozen=True)
class _SupervisionProducer:
    process: object
    receipt_json: str
    workspace_digest: str
    child_revision: int
    child_effect_digest: str
    child_artifact_digest: str
    child_checkpoint_digest: str
    rendered_digest: str | None
    _seal: object = field(repr=False)
    _issued_id: int = field(default=0, repr=False)


def _supervision_mac(body):
    from src.work_board.document_build_storage import _review_mac
    return _review_mac({"document_supervision": body})


def _supervision(value):
    import hmac
    try:
        raw = value["supervision"]
        if type(raw) is not dict or set(raw) != {"receipt", "mac"}:
            raise ValueError()
        body = DocumentSupervisionV1.model_validate(raw["receipt"]).model_dump(mode="json")
        if type(raw["mac"]) is not str or not hmac.compare_digest(raw["mac"], _supervision_mac(body)):
            raise ValueError()
        return body
    except (ValueError, TypeError, KeyError):
        raise BoardError("document_build_supervision_unavailable", "The original source-owned outer closure is required", status_code=409) from None


@dataclass(frozen=True)
class _BuildCancelContext:
    child_id: str
    current_identity: str
    entry: object
    witness: object
    _seal: object = field(repr=False)
    _issued_id: int = field(default=0, repr=False)


async def _cancel_context(db, parent, task, attempt, child):
    from src.workflows.general_task_guard import _cancel_witness, child_binding
    from src.workflows.job_runtime import _digest
    witness = _cancel_witness(parent, task, attempt)
    children = list((await db.scalars(select(type(child)).where(type(child).parent_job_id == parent.run_identity))).all())
    if set(item.run_identity for item in children) != set(witness.original_manifest.admitted_invocation_ids):
        raise BoardError("document_build_cancel_changed", "The complete original stop set is required", status_code=409)
    for entry in witness.children:
        row = next(item for item in children if item.run_identity == entry.original_binding.invocation_id)
        if (child_binding(row) != entry.original_binding or row.revision != entry.current_child_revision
                or row.fencing_token != entry.current_child_fence or row.attempt_count != entry.original_attempt_count
                or row.lease_owner or row.lease_expires_at
                or _digest(json.loads(row.effect_receipts_json)) != entry.effect_digest
                or _digest(json.loads(row.artifact_receipts_json)) != entry.artifact_digest
                or _digest(json.loads(row.checkpoint_receipts_json)) != entry.checkpoint_digest):
            raise BoardError("document_build_cancel_changed", "The cancellation-frozen native rows changed", status_code=409)
    entry = next((item for item in witness.children if item.original_binding.invocation_id == child.run_identity), None)
    if entry is None:
        raise BoardError("document_build_cancel_changed", "The original child is absent from its stop set", status_code=409)
    from src.work_board.document_capacity import _identity
    result = _BuildCancelContext(child.run_identity, _identity(child), entry, witness, _SEAL)
    object.__setattr__(result, "_issued_id", id(result))
    return result


async def _build_context(db, row, value, child):
    from src.db.models import WorkflowRunState, WorkBoardInputArtifact
    from src.workflows.general_task_guard import child_binding, read_manifest, assert_original_parent_authority
    if child is None:
        raise BoardError("document_build_binding_changed", "The original native child is required", status_code=409)
    binding = child_binding(child)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
    source = await db.get(WorkBoardInputArtifact, task.input_artifact_id) if task else None
    if (parent is None or task is None or attempt is None or source is None
            or row.bound_task_id != task.task_id or attempt.task_id != task.task_id
            or attempt.workflow_run_id != parent.run_identity or child.parent_job_id != parent.run_identity
            or task.owner_principal_id != row.owner_principal_id or task.owner_session_id != row.owner_session_id
            or child.owner_principal_id != row.owner_principal_id or child.operator_session_id != row.owner_session_id
            or binding.original_root_id != row.owner_session_id or binding.owner_principal_id != row.owner_principal_id
            or source.bound_task_id != task.task_id or source.payload_sha256 != task.typed_input_digest
            or binding.original_envelope_digest != task.typed_input_digest
            or source.owner_principal_id != row.owner_principal_id or source.owner_session_id != row.owner_session_id
            or binding.input_digest != digest({"build_ref": "document-build:"+row.artifact_id, "spec_digest":value["spec_digest"]})):
        raise BoardError("document_build_binding_changed", "The original canonical build ownership changed", status_code=409)
    assert_original_parent_authority(parent)
    manifest = read_manifest(parent)
    if (manifest is None or manifest.creation_digest != binding.creation_digest
            or manifest.original_envelope_digest != binding.original_envelope_digest
            or manifest.selected_grant_digest != binding.selected_grant_digest
            or manifest.admitted_invocation_ids != [child.run_identity]):
        raise BoardError("document_build_binding_changed", "The fixed original one-child build manifest changed", status_code=409)
    from src.work_board.contracts import DocumentBuildReview
    review = DocumentBuildReview.model_validate(value.get("accepted_review"))
    import hmac
    facts = review.binding.model_dump(mode="json")
    from src.work_board.document_build_storage import _review_mac
    if (not hmac.compare_digest(review.mac, _review_mac(facts))
            or facts["task_id"] != task.task_id or facts["task_revision"] != value.get("accepted_task_revision")
            or facts["descriptor_digest"] != binding.descriptor_digest or facts["build_id"] != row.artifact_id
            or facts["spec_digest"] != value["spec_digest"] or facts["selection_digest"] != value["selection_digest"]
            or facts["root_authority"] != value["root_authority"]):
        raise BoardError("document_build_binding_changed", "The original signed build selection changed", status_code=409)
    cancellation = await _cancel_context(db, parent, task, attempt, child) if attempt.cancel_requested_at else None
    return binding, parent, task, attempt, cancellation


def _rendered_digest(rendered):
    from hashlib import sha256
    return digest({"editable_sha256":sha256(rendered.editable).hexdigest(),
        "pdf_sha256":sha256(rendered.pdf).hexdigest() if rendered.pdf is not None else None,
        "editable_media":rendered.editable_media, "editable_extension":rendered.editable_extension,
        "warnings":rendered.warnings, "source_refs":rendered.source_refs})


def validate_rendered_supervision(producer, rendered, *, row, value):
    if (type(producer) is not _SupervisionProducer or producer._seal is not _SEAL
            or producer._issued_id != id(producer) or producer.rendered_digest is None
            or producer.rendered_digest != _rendered_digest(rendered)):
        raise BoardError("document_build_supervision_unavailable", "The retained actual renderer output is required", status_code=409)
    raw = json.loads(producer.receipt_json)
    body = _supervision(value)
    if (value["supervision"] != raw or body["build_id"] != row.artifact_id
            or body["capacity_digest"] != digest(value.get("renderer_binding"))
            or producer.workspace_digest != digest(value["root"])):
        raise BoardError("document_build_supervision_changed", "The original output producer belongs to another build", status_code=409)


def _issue_supervision(process, row, value, capacity, binding, original_child, snapshot, stdout,
        *, stdin_closed, stdout_eof, waited, rendered):
    """Called only by the retained producer after its actual pipe/wait path."""
    from hashlib import sha256
    if (snapshot is None or original_child is None or stdin_closed is not True
            or stdout_eof is not True or waited is not True or process.returncode is None
            or not process.stdout.at_eof() or not process.stdin.is_closing()
            or len(stdout) > _STDOUT_MAX):
        raise BoardError("document_build_supervision_unavailable", "Actual original pipe and supervisor closure is required", status_code=409)
    _witness, reap, facts, _raw = _reap_witness(row,value,capacity,original_child,details=True)
    body = DocumentSupervisionV1.model_validate({"version":"document-supervision.v1",
        "build_id":row.artifact_id,"original_build_revision":snapshot[0],
        "original_build_metadata_digest":snapshot[1],"capacity_digest":digest(capacity),
        "native_binding_digest":digest(binding.model_dump(mode="json")),
        "task_id":binding.task_id,"attempt_id":binding.attempt_id,"parent_job_id":binding.parent_job_id,
        "child_job_id":binding.invocation_id,"child_fence":capacity["child_fence"],
        "generation":capacity["generation"],"nonce":capacity["nonce"],"supervisor_pid":process.pid,
        "parser_pid":original_child["parser_pid"],"supervisor_exit":process.returncode,
        "stdin_closed":True,"stdout_eof":True,"supervisor_wait_reaped":True,
        "stdout_size":len(stdout),"stdout_sha256":sha256(stdout).hexdigest(),
        "parser_witness_name":reap["witness_name"],"parser_witness_size":reap["witness_size"],
        "parser_witness_sha256":reap["witness_sha256"],**facts}).model_dump(mode="json")
    result = _SupervisionProducer(process,json.dumps({"receipt":body,"mac":_supervision_mac(body)},
        sort_keys=True,separators=(",",":")),snapshot[2],snapshot[3],snapshot[4],snapshot[5],snapshot[6],
        _rendered_digest(rendered) if rendered is not None else None,_SEAL)
    object.__setattr__(result,"_issued_id",id(result))
    return result


async def validate_supervision_publication(db, row, value, producer):
    from src.db.models import WorkflowRunState
    from src.work_board.document_capacity import _records
    from src.work_board.documents import current_source_root
    import asyncio
    if (type(producer) is not _SupervisionProducer or producer._seal is not _SEAL
            or producer._issued_id != id(producer) or type(producer.process) is not asyncio.subprocess.Process):
        raise BoardError("document_build_supervision_unavailable", "Only the original retained invocation may publish closure", status_code=409)
    raw = json.loads(producer.receipt_json)
    body = DocumentSupervisionV1.model_validate(raw["receipt"]).model_dump(mode="json")
    if (row.artifact_id != body["build_id"] or row.revision != body["original_build_revision"]
            or row.metadata_digest != body["original_build_metadata_digest"]
            or value.get("supervision_max_bytes") != SUPERVISION_MAX_BYTES
            or producer.process.pid != body["supervisor_pid"] or producer.process.returncode != body["supervisor_exit"]
            or digest(value["root"]) != producer.workspace_digest):
        raise BoardError("document_build_supervision_changed", "The original build closure CAS changed", status_code=409)
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == body["child_job_id"]))
    binding, _parent, _task, _attempt, cancellation = await _build_context(db, row, value, child)
    capacity = value.get("renderer_binding")
    validate_capacity_record(child, capacity, _cancel_context=cancellation)
    records = _records(child)
    original_child = records.get("document-child")
    if (digest(capacity) != body["capacity_digest"] or digest(binding.model_dump(mode="json")) != body["native_binding_digest"]
            or original_child is None or records.get("document-capacity") != capacity
            or original_child["supervisor_pid"] != body["supervisor_pid"] or original_child["parser_pid"] != body["parser_pid"]
            or value.get("live_writer") != {"token":body["nonce"], "slot":"renderer"}
            or any(body[key] != capacity[target] for key,target in (("task_id","task_id"),("attempt_id","attempt_id"),
                ("parent_job_id","parent_job_id"),("child_job_id","job_id"),("child_fence","child_fence"),("generation","generation"),("nonce","nonce")))):
        raise BoardError("document_build_supervision_changed", "The original process/capacity binding changed", status_code=409)
    if cancellation is None:
        from src.workflows.job_runtime import _digest
        if (child.revision != producer.child_revision or _digest(json.loads(child.effect_receipts_json)) != producer.child_effect_digest
                or _digest(json.loads(child.artifact_receipts_json)) != producer.child_artifact_digest
                or _digest(json.loads(child.checkpoint_receipts_json)) != producer.child_checkpoint_digest):
            raise BoardError("document_build_supervision_changed", "The retained invocation journal changed", status_code=409)
    else:
        entry = cancellation.entry
        if (entry.original_revision != producer.child_revision or entry.original_claim_fence != body["child_fence"]
                or entry.effect_digest != producer.child_effect_digest or entry.artifact_digest != producer.child_artifact_digest
                or entry.checkpoint_digest != producer.child_checkpoint_digest):
            raise BoardError("document_build_supervision_changed", "The frozen original invocation changed", status_code=409)
    root = await current_source_root(db, WorkBoardOwner(principal_id=row.owner_principal_id,session_id=row.owner_session_id), None)
    from src.work_board.document_build_storage import _digest as storage_digest
    if value["root_authority"] != storage_digest({"id":root.id,"principal":root.principal_id,
            "token_hash":root.token_hash,"absolute":utc(root.absolute_expires_at).isoformat()}):
        raise BoardError("document_build_supervision_changed", "The original current Root changed", status_code=409)
    if raw["mac"] != _supervision_mac(body):
        raise BoardError("document_build_supervision_changed", "The source closure seal changed", status_code=409)
    return raw


async def validate_outputless_retirement(db, owner, row, value):
    """Observe the existing complete protected stop; never create terminality."""
    from src.db.models import WorkflowRunState, InferenceCostReservation
    from src.workflows.general_task_guard import read_manifest
    from src.workflows.job_runtime import _job_has_unsafe_effects
    from src.workflows.general_task_accounting import entry_for
    if (row.owner_principal_id != owner.principal_id or row.owner_session_id != owner.session_id
            or not row.bound_task_id or value.get("output") or value.get("output_manifest_digest")
            or any(slot in value.get("sources", {}) for slot in ("editable","pdf","output-manifest"))):
        raise BoardError("document_build_outputless_unverified", "Only the original fully cancelled outputless build may retire", status_code=409)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == row.bound_task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == row.bound_task_id)
        .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
    children = list((await db.scalars(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == attempt.workflow_run_id))).all()) if attempt else []
    if len(children) != 1:
        raise BoardError("document_build_outputless_unverified", "The exact original fixed child set is required", status_code=409)
    child = children[0]
    binding, parent, actual_task, actual_attempt, cancellation = await _build_context(db,row,value,child)
    if (task is None or task.task_id != actual_task.task_id or attempt.attempt_id != actual_attempt.attempt_id
            or cancellation is None or cancellation.witness.state != "fully_cancelled"
            or str(getattr(task.status,"value",task.status)) != "blocked"
            or task.block_reason != "general_task_native_cancel_fully_cancelled"
            or attempt.ended_at is None or attempt.outcome != "cancelled" or parent.status != "cancelled"
            or cancellation.entry.effect_debt or _job_has_unsafe_effects(json.loads(child.effect_receipts_json))):
        raise BoardError("document_build_outputless_unverified", "The existing fully cancelled Task/Attempt and settled stop set are required", status_code=409)
    manifest = read_manifest(parent)
    costs = list((await db.scalars(select(InferenceCostReservation).where(
        (InferenceCostReservation.job_id.in_([parent.run_identity,child.run_identity]))
        | InferenceCostReservation.evidence_json.contains(manifest.group_id)))).all())
    for cost in costs:
        entry = entry_for(cost)
        if (cost.job_id in {parent.run_identity,child.run_identity}
                or (entry is not None and entry.get("group",{}).get("group_id") == manifest.group_id)):
            if cost.state not in {"released","settled"} or (cost.state == "settled" and cost.actual_cost_microusd != 0):
                raise BoardError("document_build_outputless_unverified", "Original cost debt cannot retire the build", status_code=409)
    from src.work_board.document_capacity import _records
    records = _records(child)
    entry = cancellation.entry
    if entry.original_attempt_count == 0:
        arguments = json.loads(child.arguments_json)
        artifacts = json.loads(child.artifact_receipts_json)
        original_input = (type(artifacts) is list and len(artifacts) == 1
            and artifacts[0].get("artifact_type") == "general_task_tool_input"
            and artifacts[0].get("producer") == "agent.task.v1"
            and artifacts[0].get("run_id") == parent.run_identity
            and artifacts[0].get("artifact_id") == arguments.get("typed_input_ref", "").removeprefix("general-task-input:")
            and artifacts[0].get("content_sha256") == arguments.get("typed_input_digest"))
        if (entry.original_claim_fence != 0 or child.attempt_count != 0
                or json.loads(child.effect_receipts_json) or not original_input
                or any(key in records for key in ("document-capacity","document-child","document-reaped"))
                or value.get("renderer_binding") or value.get("live_writer") or value.get("supervision") or value.get("reap")):
            raise BoardError("document_build_outputless_unverified", "The original zero-claim/no-contact stop is required", status_code=409)
    else:
        capacity = value.get("renderer_binding")
        validate_capacity_record(child,capacity,_cancel_context=cancellation)
        original_child = records.get("document-child")
        if (original_child is None or value.get("live_writer") is not None or not _authenticated_reap(value,capacity)):
            raise BoardError("document_build_outputless_unverified", "Both positively closed original process owners are required", status_code=409)
        _fresh_supervised_reap(row,value,capacity,original_child)
    return task, attempt


def descriptor():
    from src.work_board.contracts import ToolDescriptor
    from src.tools.policy import get_task_policy_snapshot
    from src.native_tools.registry import get_tool_metadata
    from src.work_board.document_build_storage import PROFILE_DIGEST, OUTPUT_LIMIT
    hash_schema = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    handle = {"type": "string", "minLength": 55, "maxLength": 60}
    artifact = {"type": "object", "properties": {"artifact_ref": handle, "sha256": hash_schema,
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": OUTPUT_LIMIT},
        "media_type": {"type": "string", "enum": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/pdf"]}},
        "required": ["artifact_ref", "sha256", "size_bytes", "media_type"], "additionalProperties": False}
    output = {"type": "object", "properties": {"editable_artifact": artifact,
        "pdf_artifact": {"anyOf": [artifact, {"type": "null"}]},
        "source_refs": {"type": "array", "maxItems": 16, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 512}},
        "warnings": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 200}}},
        "required": ["editable_artifact", "pdf_artifact", "source_refs", "warnings"], "additionalProperties": False}
    return ToolDescriptor(tool_id="document_build", version="1",
        input_schema={"type": "object", "properties": {
            "build_ref": {"type": "string", "minLength": 51, "maxLength": 51}, "spec_digest": hash_schema},
            "required": ["build_ref", "spec_digest"], "additionalProperties": False}, output_schema=output,
        effects=["owner_private_read", "local_compute", "owner_private_artifact_write"],
        permissions=["capability_execute", "document_local_use", "document_private_artifact_write"],
        deadline=30, verifier="document_build_private_readback.v1", policy_digest=digest({
            "profile": PROFILE_DIGEST, "family": "document.build.v1", "generation": 1,
            "slots": ["editable", "pdf", "output-manifest"], "file_bytes": OUTPUT_LIMIT,
            "metadata": get_tool_metadata("document_build"), "policy": get_task_policy_snapshot()}))


def check_envelope(envelope):
    selected = envelope.task_input.document_build
    if selected is None:
        if envelope.plan and any(step.tool_id == "document_build" for step in envelope.plan.steps):
            raise BoardError("document_build_binding_required", "Use the exact private build preparation", status_code=422)
        return
    tool = descriptor()
    limits = envelope.task_input.limits
    if (envelope.task_input.document_source is not None or envelope.task_input.inference_egress_acknowledged
            or envelope.task_input.evidence_refs or envelope.task_input.intent != "Build the reviewed local document specification"
            or limits.max_steps != 1 or limits.max_inference_calls or limits.max_cost_microusd
            or limits.max_outstanding_children or limits.wall_seconds > 60
            or envelope.plan is None or len(envelope.plan.steps) != 1
            or envelope.plan.steps[0].step_id != "build" or envelope.plan.steps[0].tool_id != "document_build"
            or envelope.plan.steps[0].depends_on or envelope.descriptors != [tool]
            or envelope.plan.steps[0].input != {"build_ref": selected.build_ref, "spec_digest": selected.spec_digest}
            or envelope.plan.steps[0].output_contract != tool.output_schema
            or envelope.task_input.requested_output != tool.output_schema):
        raise BoardError("document_build_local_plan_required", "Use the fixed zero-inference private build plan", status_code=422)


async def validate_acceptance(db, owner, task, envelope, review):
    from src.work_board import document_build_storage as storage
    check_envelope(envelope)
    binding = envelope.task_input.document_build
    if binding is None:
        if review is not None:
            raise BoardError("document_build_review_unexpected", "This task has no private build", status_code=422)
        return
    if review is None:
        raise BoardError("document_build_review_required", "Review the exact private specification before accepting", status_code=409)
    row, value = await storage.owned(db, owner, binding.build_ref.split(":", 1)[1])
    if storage.build_binding(row, value) != binding.model_dump(mode="json") or row.bound_task_id != task.task_id:
        raise BoardError("document_build_binding_changed", "The immutable original build changed", status_code=409)
    approved = await storage.verify_review(db, owner, row, value, review, task=task, descriptor=descriptor())
    value["accepted_review"] = approved
    value["accepted_task_revision"] = task.task_revision
    storage.persist(row, value)
    await db.flush()


@dataclass(frozen=True)
class BuildCandidate:
    service: object
    registry: object
    job_id: str
    child_identity: str
    parent_journal: str
    task_revision: int
    attempt_fence: int
    priority: int
    task_input_artifact_id: str
    build_id: str
    build_revision: int
    build_metadata_digest: str
    binding: object
    envelope: object
    descriptor_digest: str
    policy_digest: str
    _seal: object = field(repr=False)
    _issued_id: int = field(default=0, repr=False)


async def stage_candidate(db, run, *, service=None):
    """Preclaim reads references only; never decrypt the specification."""
    from src.workflows.general_task_guard import assert_general_task_child_phase_current, child_binding
    from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input, read_current_native_envelope
    from src.work_board import document_build_storage as storage
    from src.work_board.document_capacity import _identity
    from src.work_board.general_task import GeneralTaskService
    from src.native_tools.task_adapters import ToolRegistry
    if service is None:
        from src.work_board.dispatcher import _dispatcher
        service = _dispatcher.general_tasks
    if type(service) is not GeneralTaskService or not service.started or type(service.registry) is not ToolRegistry or not service.registry.started:
        raise BoardError("document_build_native_owner_required", "Restore the actual native build owner", status_code=503)
    await assert_general_task_child_phase_current(db, run)
    native = child_binding(run)
    parent = await db.scalar(select(type(run)).where(type(run).run_identity == native.parent_job_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == native.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == native.attempt_id))
    from src.work_board.channel_capture import check_current_captured_task_source
    await check_current_captured_task_source(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id), task)
    private = read_bound_native_tool_input(run, native)
    envelope = await read_current_native_envelope(db, parent, task, attempt)
    check_envelope(envelope)
    tool = descriptor()
    registered = {item.tool_id: item for item in service.registry.descriptors()}.get("document_build")
    producer = service.registry.compile_capacity(tool)
    if (registered != tool or producer.approval_possible is not False or private.tool_id != "document_build" or native.descriptor_digest != digest(tool.model_dump(mode="json"))
            or private.inputs != envelope.plan.steps[0].input or run.priority != task.priority
            or json.loads(run.declared_authority_json).get("document_build_priority") != task.priority
            or json.loads(run.declared_authority_json).get("document_build_input_artifact_id") != task.input_artifact_id
            or run.attempt_count or run.fencing_token or run.lease_owner or run.lease_expires_at
            or json.loads(run.effect_receipts_json)):
        raise BoardError("document_build_candidate_changed", "The genuine original fixed build child is required", status_code=409)
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    binding = envelope.task_input.document_build
    row, value = await storage.owned(db, owner, binding.build_ref.split(":", 1)[1])
    await storage.authority(db, owner, row, value, metadata_only=True)
    if storage.build_binding(row, value) != binding.model_dump(mode="json") or row.bound_task_id != task.task_id:
        raise BoardError("document_build_binding_changed", "The private build is bound to another original task", status_code=409)
    result = BuildCandidate(service, service.registry, run.run_identity, _identity(run), parent.checkpoint_receipts_json,
        task.task_revision, attempt.fencing_token, task.priority, task.input_artifact_id, row.artifact_id, row.revision, row.metadata_digest,
        native, envelope, native.descriptor_digest, tool.policy_digest, _SEAL)
    object.__setattr__(result, "_issued_id", id(result))
    await recheck_candidate(db, run, result)
    return result


async def recheck_candidate(db, run, proof):
    """Original metadata/phase/permission recheck, without private spec I/O."""
    from src.workflows.general_task_guard import assert_general_task_child_phase_current
    from src.work_board.document_capacity import _identity
    from src.work_board import document_build_storage as storage
    if (type(proof) is not BuildCandidate or proof._seal is not _SEAL or proof._issued_id != id(proof)
            or proof.job_id != run.run_identity or proof.child_identity != _identity(run)
            or not proof.service.started or proof.service.registry is not proof.registry or not proof.registry.started
            or digest(descriptor().model_dump(mode="json")) != proof.descriptor_digest):
        raise BoardError("document_build_candidate_changed", "The private original producer witness changed", status_code=409)
    await assert_general_task_child_phase_current(db, run)
    parent = await db.scalar(select(type(run)).where(type(run).run_identity == proof.binding.parent_job_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == proof.binding.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == proof.binding.attempt_id))
    if (parent.checkpoint_receipts_json != proof.parent_journal or task.task_revision != proof.task_revision
            or task.priority != proof.priority or run.priority != proof.priority or attempt.fencing_token != proof.attempt_fence
            or run.status not in {"accepted", "queued"} or run.attempt_count or run.fencing_token
            or run.lease_owner or run.lease_expires_at or json.loads(run.effect_receipts_json)):
        raise BoardError("document_build_candidate_changed", "The original unclaimed native phase changed", status_code=409)
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    row, value = await storage.owned(db, owner, proof.build_id, revision=proof.build_revision)
    await storage.authority(db, owner, row, value, metadata_only=True)
    approved = value.get("accepted_review")
    from src.work_board.contracts import DocumentBuildReview
    try:
        review = DocumentBuildReview.model_validate(approved)
        facts = review.binding.model_dump(mode="json")
        import hmac
        if not hmac.compare_digest(review.mac, storage._review_mac(facts)):
            raise ValueError()
        if (row.metadata_digest != proof.build_metadata_digest or row.bound_task_id != task.task_id
                or storage.build_binding(row, value) != proof.envelope.task_input.document_build.model_dump(mode="json")
                or utc(datetime.fromisoformat(facts["expires_at"])) <= now()
                or facts["task_id"] != task.task_id or facts["task_revision"] != value.get("accepted_task_revision")
                or facts["plan_revision"] != 1 or facts["descriptor_digest"] != proof.descriptor_digest
                or facts["policy_digest"] != proof.policy_digest or facts["root_authority"] != value["root_authority"]
                or facts["spec_digest"] != value["spec_digest"] or facts["selection_digest"] != value["selection_digest"]):
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise BoardError("document_build_review_changed", "The original accepted private build review changed", status_code=409) from None
    return row, value


class DocumentBuildPreclaimHeld(Exception):
    """Only the actual sealed callback can establish a no-contact queued wait."""
    def __init__(self, claim, code):
        self.claim, self.code = claim, code
        self._seal = _SEAL
        super().__init__(code)


@dataclass(frozen=True)
class DocumentBuildClaim:
    jobs: object
    candidate: BuildCandidate
    snapshot: object
    nonce: str
    _seal: object = field(repr=False)
    _issued_id: int = field(default=0, repr=False)

    async def __call__(self, db, run):
        from src.work_board.document_capacity import assert_capacity
        from src.work_board import document_build_storage as storage
        validate_claim(self.jobs, run, self)
        row, value = await recheck_candidate(db, run, self.candidate)
        try:
            await assert_capacity(db, snapshot=self.snapshot, run=run)
        except BoardError as error:
            if error.code in {"document_parser_capacity_held", "document_higher_priority_ready"}:
                raise DocumentBuildPreclaimHeld(self, error.code) from None
            raise
        binding = self.candidate.binding
        capacity = {"version": "document-capacity-build.v1", "profile": "document-build-renderer.v1",
            "job_id": run.run_identity, "input_digest": run.input_digest, "task_id": binding.task_id,
            "attempt_id": binding.attempt_id, "parent_job_id": binding.parent_job_id,
            "child_binding_digest": digest(binding.model_dump(mode="json")),
            "input_artifact_id": self.candidate.task_input_artifact_id, "build_id": row.artifact_id,
            "build_revision": value["spec_revision"], "generation": value["generation"], "nonce": self.nonce,
            "descriptor_digest": self.candidate.descriptor_digest, "policy_digest": self.candidate.policy_digest,
            "original_deadline": min(utc(run.deadline_at), now() + timedelta(seconds=35)).isoformat(), "child_fence": run.fencing_token + 1}
        try:
            assert_prelaunch_headroom(row, value, capacity)
        except BoardError as error:
            if error.code == "document_build_metadata_headroom_full":
                raise DocumentBuildPreclaimHeld(self, error.code) from None
            raise
        history = json.loads(run.checkpoint_receipts_json)
        if any(item.get("checkpoint_id") in {"document-capacity", "document-reaped"} for item in history):
            raise BoardError("document_original_attempt_recovery_required", "Reconcile the original renderer; never relaunch", status_code=409)
        history.append({"checkpoint_id": "document-capacity", "payload": capacity, "safe": True})
        run.checkpoint_receipts_json = json.dumps(history, sort_keys=True, separators=(",", ":"))
        value.update({"live_writer": {"token": self.nonce, "slot": "renderer"}, "renderer_binding": capacity,
            "phase": "rendering", "supervision_max_bytes": SUPERVISION_MAX_BYTES})
        storage.persist(row, value)
        await db.flush()


def validate_claim(jobs, run, claim):
    if (type(claim) is not DocumentBuildClaim or claim._seal is not _SEAL or claim._issued_id != id(claim)
            or DocumentBuildClaim.__call__ is not _CLAIM_CALL or DocumentBuildClaim.__call__.__code__ is not _CLAIM_CODE
            or claim.jobs is not jobs or claim.candidate.job_id != run.run_identity
            or run.job_kind != "general_task_native_tool_v1" or json.loads(run.arguments_json).get("tool_id") != "document_build"):
        raise BoardError("document_build_preclaim_required", "The exact private build claim producer is required", status_code=409)


async def stage_claim(service, jobs, binding):
    from src.work_board.document_capacity import stage_capacity
    async with jobs._session() as db:
        run = await jobs._fetch(db, binding.invocation_id)
        proof = await stage_candidate(db, run, service=service)
        snapshot = await stage_capacity(db, service=service)
    result = DocumentBuildClaim(jobs, proof, snapshot, uuid.uuid4().hex, _SEAL)
    object.__setattr__(result, "_issued_id", id(result))
    return result


_CLAIM_CALL = DocumentBuildClaim.__call__
_CLAIM_CODE = _CLAIM_CALL.__code__


async def queued_wait_result(jobs, binding, error):
    if (type(error) is not DocumentBuildPreclaimHeld or error._seal is not _SEAL
            or type(error.claim) is not DocumentBuildClaim or error.claim._issued_id != id(error.claim)
            or error.claim.jobs is not jobs or error.claim.candidate.binding != binding):
        return None
    from src.workflows.general_task_guard import assert_general_task_child_phase_current
    async with jobs._session() as db:
        child = await jobs._fetch(db, binding.invocation_id)
        validate_claim(jobs, child, error.claim)
        if (child.status != "queued" or child.attempt_count or child.fencing_token or child.lease_owner
                or child.lease_expires_at or json.loads(child.effect_receipts_json)
                or any(item.get("checkpoint_id") in {"document-capacity", "document-reaped"}
                    for item in json.loads(child.checkpoint_receipts_json))):
            return None
        await assert_general_task_child_phase_current(db, child)
        parent = await jobs._fetch(db, binding.parent_job_id)
        if parent.checkpoint_receipts_json != error.claim.candidate.parent_journal:
            return None
    return {"verified": False, "status": "queued", "reason": error.code,
        "unknown_effect": False, "no_learning": True, "native_execution": True}


_CAPACITY_FIELDS = {"version", "profile", "job_id", "input_digest", "task_id", "attempt_id", "parent_job_id",
    "child_binding_digest", "input_artifact_id", "build_id", "build_revision", "generation", "nonce",
    "descriptor_digest", "policy_digest", "original_deadline", "child_fence"}


def validate_capacity_record(run, capacity, *, _cancel_context=None):
    from src.workflows.general_task_guard import child_binding
    import re
    binding = child_binding(run)
    original_fence = run.fencing_token
    if _cancel_context is not None:
        from src.work_board.document_capacity import _identity
        if (type(_cancel_context) is not _BuildCancelContext or _cancel_context._seal is not _SEAL
                or _cancel_context._issued_id != id(_cancel_context) or _cancel_context.child_id != run.run_identity
                or _cancel_context.current_identity != _identity(run) or _cancel_context.entry.original_attempt_count != 1):
            raise BoardError("document_build_cancel_changed", "The protected original cancellation context is required", status_code=409)
        original_fence = _cancel_context.entry.original_claim_fence
    if (set(capacity) != _CAPACITY_FIELDS or capacity["version"] != "document-capacity-build.v1"
            or capacity["profile"] != "document-build-renderer.v1" or capacity["job_id"] != run.run_identity
            or capacity["input_digest"] != run.input_digest or capacity["task_id"] != binding.task_id
            or capacity["attempt_id"] != binding.attempt_id or capacity["parent_job_id"] != binding.parent_job_id
            or capacity["child_binding_digest"] != digest(binding.model_dump(mode="json"))
            or capacity["descriptor_digest"] != binding.descriptor_digest
            or capacity["input_artifact_id"] != json.loads(run.declared_authority_json).get("document_build_input_artifact_id")
            or type(capacity["generation"]) is not int or capacity["generation"] != 1
            or type(capacity["build_revision"]) is not int or capacity["build_revision"] < 1
            or type(capacity["child_fence"]) is not int or capacity["child_fence"] != original_fence
            or not re.fullmatch(r"[0-9a-f]{32}", str(capacity["nonce"]))
            or utc(datetime.fromisoformat(capacity["original_deadline"])) > utc(run.deadline_at)):
        raise BoardError("document_capacity_inventory_invalid", "The original fixed build reservation changed", status_code=409)


async def invocation_scope(db, principal, job_id, fence, inputs, *, staged_envelope=None):
    from src.workflows.job_runtime import DurableJobRepository
    from src.workflows.general_task_guard import assert_general_task_child_current, child_binding
    from src.work_board.general_task_runtime_artifacts import read_current_native_envelope
    from src.work_board import document_build_storage as storage
    from src.work_board.document_capacity import _records
    jobs = DurableJobRepository()
    child = await jobs._fetch(db, job_id)
    if (not principal or not principal.authenticated or principal.revoked
            or principal.principal_id != child.owner_principal_id or principal.session_id != child.operator_session_id
            or principal.operator_session_id != child.operator_session_id or principal.job_id != job_id
            or child.status != "running" or child.attempt_count != 1 or child.fencing_token != fence):
        raise BoardError("document_build_native_owner_changed", "The original claimed native owner is required", status_code=409)
    await assert_general_task_child_current(db, child)
    binding = child_binding(child)
    parent = await jobs._fetch(db, binding.parent_job_id)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id))
    if staged_envelope is None:
        envelope = await read_current_native_envelope(db, parent, task, attempt)
    else:
        # The original physical envelope was read outside this SQL writer.
        # Current phase checks above bind the canonical input row; its exact
        # content digest prevents a copied/changed envelope granting delivery.
        envelope = staged_envelope
        from src.work_board.input_artifacts import INPUT_ARTIFACT_SCHEMA_VERSION
        original_payload = {"schema_version": INPUT_ARTIFACT_SCHEMA_VERSION,
            "capability_id": "agent.task.v1", "input": envelope.model_dump(mode="json", exclude_none=True)}
        if digest(original_payload) != binding.original_envelope_digest:
            raise BoardError("document_build_binding_changed", "The staged original envelope changed", status_code=409)
    check_envelope(envelope)
    if inputs != envelope.plan.steps[0].input:
        raise BoardError("document_build_binding_changed", "The original private build input changed", status_code=409)
    records = _records(child)
    capacity = records.get("document-capacity")
    if capacity is None or "document-reaped" in records:
        raise BoardError("document_original_attempt_recovery_required", "The original unreaped renderer reservation is required", status_code=409)
    validate_capacity_record(child, capacity)
    row, value = await storage.owned(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), capacity["build_id"])
    await storage.authority(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), row, value, metadata_only=True)
    if (row.bound_task_id != task.task_id or task.input_artifact_id != capacity["input_artifact_id"]
            or value.get("renderer_binding") != capacity
            or value.get("live_writer") != {"token": capacity["nonce"], "slot": "renderer"}
            or storage.build_binding(row, value) != envelope.task_input.document_build.model_dump(mode="json")
            or capacity["policy_digest"] != descriptor().policy_digest
            or utc(datetime.fromisoformat(capacity["original_deadline"])) <= now()):
        raise BoardError("document_build_capacity_changed", "The exact original renderer reservation changed", status_code=409)
    return child, binding, row, value, capacity, envelope


def _reap_witness(row, value, capacity, child_binding, *, details=False):
    import os
    from src.work_board import document_pairs as sources
    from src.work_board.input_artifacts import _open_input_artifact_parent, _private_input_file_metadata
    path = sources.source_path(row, value, "spec").parent / (capacity["nonce"] + ".witness.json")
    parent, leaf = _open_input_artifact_parent(path, create=False)
    fd = -1
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        facts = os.fstat(fd)
        import stat
        if not _private_input_file_metadata(facts) or facts.st_mode != stat.S_IFREG | 0o600 or not 1 <= facts.st_size <= 4096:
            raise ValueError("private renderer witness metadata changed")
        chunks = bytearray()
        while len(chunks) <= 4096:
            chunk = os.read(fd, 4097-len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        raw = bytes(chunks)
        if len(raw) != facts.st_size:
            raise ValueError("private renderer witness size changed")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)
    witness = json.loads(raw)
    exact = {key: capacity[key] for key in ("job_id", "input_digest", "generation", "nonce")}
    if (set(witness) != {*exact, "supervisor_pid", "parser_pid", "parser_exit", "wait_reaped", "reason"}
            or any(witness.get(key) != val for key, val in exact.items())
            or witness.get("wait_reaped") is not True or type(witness.get("parser_exit")) is not int
            or any(type(witness.get(key)) is not int or witness[key] <= 0
                or witness[key] != child_binding[key] for key in ("supervisor_pid", "parser_pid"))):
        raise BoardError("document_build_reap_changed", "The exact original process must positively reap", status_code=409)
    reap = {"wait_reaped": True, "witness_sha256": sources.sha256(raw),
        "witness_name": leaf, "witness_size": len(raw)}
    if details:
        return witness, reap, {"parser_witness_device":facts.st_dev,"parser_witness_inode":facts.st_ino,
            "parser_witness_mode":facts.st_mode,"parser_witness_uid":facts.st_uid,"parser_witness_nlink":facts.st_nlink}, raw
    return witness, reap


def _fresh_supervised_reap(row, value, capacity, original_child):
    body = _supervision(value)
    witness, reap, facts, _raw = _reap_witness(row, value, capacity, original_child, details=True)
    if (any(body[key] != val for key,val in facts.items())
            or body["parser_witness_sha256"] != reap["witness_sha256"]
            or body["parser_witness_size"] != reap["witness_size"] or body["parser_witness_name"] != reap["witness_name"]
            or body["supervisor_pid"] != original_child["supervisor_pid"] or body["parser_pid"] != original_child["parser_pid"]
            or body["capacity_digest"] != digest(capacity) or body["nonce"] != capacity["nonce"]):
        raise BoardError("document_build_reap_changed", "The fresh original private witness changed", status_code=409)
    reap["supervision_digest"] = digest(value["supervision"])
    reap["mac"] = _supervision_mac({"physical_reap":reap,"capacity_digest":digest(capacity)})
    return witness, reap


def _authenticated_reap(value, capacity):
    import hmac
    reap = value.get("reap")
    if type(reap) is not dict or set(reap) != {"wait_reaped","witness_sha256","witness_name","witness_size","supervision_digest","mac"}:
        return False
    body = _supervision(value)
    unsigned = {key:val for key,val in reap.items() if key != "mac"}
    return (reap["wait_reaped"] is True and reap["witness_sha256"] == body["parser_witness_sha256"]
        and reap["witness_name"] == body["parser_witness_name"] and reap["witness_size"] == body["parser_witness_size"]
        and reap["supervision_digest"] == digest(value["supervision"])
        and type(reap["mac"]) is str and hmac.compare_digest(reap["mac"],
            _supervision_mac({"physical_reap":unsigned,"capacity_digest":digest(capacity)})))


async def held_capacity(db, child):
    """Observe an authentic build-only release without changing native debt."""
    from src.db.models import WorkBoardInputArtifact
    from src.work_board.document_capacity import _records
    from src.work_board import document_build_storage as storage
    records = _records(child)
    capacity = records.get("document-capacity")
    if capacity is None:
        if any(key in records for key in ("document-child","document-reaped")):
            raise BoardError("document_capacity_inventory_invalid", "Original process facts lack capacity", status_code=409)
        return False
    row = await db.get(WorkBoardInputArtifact,capacity.get("build_id"))
    if row is None or row.capability_id != storage.CAPABILITY:
        raise BoardError("document_capacity_inventory_invalid", "Original build metadata is required", status_code=409)
    value = storage.metadata(row)
    _binding,_parent,_task,_attempt,cancellation = await _build_context(db,row,value,child)
    validate_capacity_record(child,capacity,_cancel_context=cancellation)
    if value.get("renderer_binding") != capacity:
        raise BoardError("document_capacity_inventory_invalid", "The same build reservation is required", status_code=409)
    if value.get("live_writer") is not None or value.get("supervision") is None:
        return True
    return not _authenticated_reap(value,capacity)


def prelaunch_metadata_projection(row, value, capacity):
    """Reserve the complete finite future metadata peak before any claim."""
    from src.work_board import document_build_storage as storage
    from copy import deepcopy
    projected = deepcopy(value)
    media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    refs = list(value.get("source_binding", {}).get("citation_refs", [])) if value.get("source_binding") else []
    def artifact(slot, selected_media):
        return {"artifact_ref":f"document-build:{row.artifact_id}:{slot}","sha256":"f"*64,
            "size_bytes":storage.OUTPUT_LIMIT,"media_type":selected_media}
    projected.update({"supervision":supervision_maximum(),"supervision_max_bytes":SUPERVISION_MAX_BYTES,
        "renderer_binding":capacity,"live_writer":{"token":capacity["nonce"],"slot":"renderer"},
        "pending":storage.max_pending_inventory(),"output_manifest_digest":"f"*64,
        "output":{"editable_artifact":artifact("editable",media),"pdf_artifact":artifact("pdf","application/pdf"),
            "source_refs":refs,"warnings":["document_pdf_render_unavailable"]},
        "reap":{"wait_reaped":True,"witness_sha256":"f"*64,"witness_name":"f"*32+".witness.json",
            "witness_size":4096,"supervision_digest":"f"*64,"mac":"f"*64},
        "phase":"cleanup_tombstone","reason":"document_original_process_quiescent",
        "retirement_key":"x"*128,"retirement_mode":"terminal_outputless"})
    pending_peak = deepcopy(projected)
    pending_peak.pop("output")
    pending_peak.pop("output_manifest_digest")
    # The SAME adoption writer consumes pending before installing output
    # receipts. These two inventories can never coexist in persisted metadata.
    projected.pop("pending")
    for slot in ("editable","pdf","output-manifest"):
        projected["sources"][slot] = {"cipher_sha256":"f"*64,"cipher_size":storage._cipher_limit(slot)}
    return max((pending_peak,projected),key=lambda candidate:len(storage.sources.canonical(candidate)))


def assert_prelaunch_headroom(row, value, capacity):
    from src.work_board import document_pairs as sources
    if len(sources.canonical(supervision_maximum())) != SUPERVISION_MAX_BYTES:
        raise BoardError("document_build_metadata_headroom_changed", "Restore the exact fixed supervision codec", status_code=409)
    projected = prelaunch_metadata_projection(row, value, capacity)
    if len(sources.canonical(projected)) > 8192:
        raise BoardError("document_build_metadata_headroom_full", "The original build cannot reserve its complete bounded cleanup metadata", status_code=409)


async def invoke(principal, job_id, fencing_token, inputs):
    """One actual supervised renderer, with positive original reap before release."""
    import asyncio
    import os
    from pathlib import Path
    import struct
    import sys
    from sqlalchemy import text
    from src.workflows.job_runtime import DurableJobRepository
    from src.work_board import document_build_storage as storage, document_pairs as sources
    from src.work_board.input_artifacts import _open_input_artifact_parent
    from src.work_board.document_build_renderer import RenderResult
    from src.work_board.document_capacity import _records
    jobs = DurableJobRepository()
    process = None
    parent_fd = -1
    async with jobs._session() as db:
        child, binding, row, value, capacity, envelope = await invocation_scope(db, principal, job_id, fencing_token, inputs)
        if "document-child" in _records(child):
            raise BoardError("document_original_attempt_recovery_required", "Reconcile the original process; never respawn", status_code=409)
        spec = storage.spec_read(row, value)
        selected = storage.selection_read(row, value)
        raw_spec = sources.canonical(spec.model_dump(mode="json"))
        raw_selected = sources.canonical(selected) if selected else b""
        parent_fd, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "spec"), create=False)
    deadline = utc(datetime.fromisoformat(capacity["original_deadline"]))
    def remaining(maximum):
        seconds = min(maximum, (deadline - now()).total_seconds())
        if seconds <= 0:
            raise BoardError("document_build_deadline", "The original renderer cutoff expired", status_code=409)
        return seconds
    async def read_exact(size):
        try:
            data = await asyncio.wait_for(process.stdout.readexactly(size), remaining(30))
        except asyncio.IncompleteReadError as error:
            stdout_capture.extend(error.partial)
            raise
        stdout_capture.extend(data)
        return data
    child_binding = None
    stdin_closed = stdout_eof = waited = False
    stdout_capture = bytearray()
    source_snapshot = None
    rendered = None
    producer = None
    try:
        wire = {key: capacity[key] for key in ("job_id", "input_digest", "generation", "nonce")}
        process = await asyncio.create_subprocess_exec(sys.executable, "-I",
            str(Path(__file__).with_name("document_compare_supervisor.py")), str(parent_fd),
            sources.canonical(wire).decode(), str(deadline.timestamp()), "document-build",
            env={}, close_fds=True, pass_fds=(parent_fd,), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
        ready = await asyncio.wait_for(process.stdout.readline(), remaining(5))
        stdout_capture.extend(ready)
        packet = json.loads(ready)
        if (len(ready) > 4096 or packet.get("state") != "ready" or packet.get("binding") != wire
                or packet.get("nonce") != capacity["nonce"] or packet.get("network_denied") is not True
                or packet.get("profile") != "document-build-renderer.v1"
                or packet.get("supervisor_pid") != process.pid or type(packet.get("parser_pid")) is not int):
            raise BoardError("document_build_profile_changed", "The actual confined renderer self-check failed", status_code=409)
        child_binding = {**capacity, "supervisor_pid": packet["supervisor_pid"], "parser_pid": packet["parser_pid"]}
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            child, _binding, _row, _value, fresh, _envelope = await invocation_scope(db, principal, job_id, fencing_token, inputs, staged_envelope=envelope)
            if fresh != capacity or "document-child" in _records(child):
                raise BoardError("document_build_capacity_changed", "The original process delivery binding changed", status_code=409)
            history = json.loads(child.checkpoint_receipts_json)
            history.append({"checkpoint_id": "document-child", "payload": child_binding, "safe": True})
            child.checkpoint_receipts_json = sources.canonical(history).decode()
            child.revision += 1
            source_snapshot = (row.revision, row.metadata_digest, digest(value["root"]), child.revision,
                digest(json.loads(child.effect_receipts_json)), digest(json.loads(child.artifact_receipts_json)),
                digest(json.loads(child.checkpoint_receipts_json)))
            await db.commit()
        process.stdin.write(struct.pack("!II", len(raw_spec), len(raw_selected)) + raw_spec + raw_selected)
        await asyncio.wait_for(process.stdin.drain(), remaining(5))
        process.stdin.close()
        await asyncio.wait_for(process.stdin.wait_closed(), remaining(5))
        stdin_closed = True
        header = await read_exact(12)
        editable_size, pdf_size, meta_size = struct.unpack("!III", header)
        if not 0 <= editable_size <= storage.OUTPUT_LIMIT or not 0 <= pdf_size <= storage.OUTPUT_LIMIT or not 1 <= meta_size <= 65536:
            raise BoardError("document_build_output_bound", "The actual fixed renderer frame exceeded its bounds", status_code=409)
        raw = await read_exact(editable_size + pdf_size + meta_size)
        extra = await asyncio.wait_for(process.stdout.read(1), remaining(5))
        stdout_capture.extend(extra)
        if extra:
            raise BoardError("document_build_output_bound", "The actual renderer returned extra bytes", status_code=409)
        stdout_eof = True
        exit_code = await asyncio.wait_for(process.wait(), remaining(5))
        waited = True
        if exit_code != 0:
            raise BoardError("document_build_process_failed", "The original supervisor failed", status_code=409)
        witness, reap = _reap_witness(row, value, capacity, child_binding)
        metadata = json.loads(raw[editable_size + pdf_size:])
        if (set(metadata) != {"status", "editable_media", "editable_extension", "warnings", "source_refs", "profile", "provider_contacts", "no_learning"}
                or metadata["status"] != "succeeded" or metadata["profile"] != "document-build-renderer.v1"
                or metadata["provider_contacts"] != 0 or metadata["no_learning"] is not True
                or not editable_size or witness["parser_exit"] != 0 or witness["reason"] is not None):
            raise BoardError("document_build_render_failed", "The bounded original rendering failed", status_code=409)
        rendered = RenderResult(raw[:editable_size], metadata["editable_media"], metadata["editable_extension"],
            raw[editable_size:editable_size + pdf_size] or None, metadata["warnings"], metadata["source_refs"])
        producer = _issue_supervision(process, row, value, capacity, binding, child_binding,
            source_snapshot, stdout_capture, stdin_closed=stdin_closed, stdout_eof=stdout_eof,
            waited=waited, rendered=rendered)
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            row, value = await storage.owned(db, WorkBoardOwner(principal_id=binding.owner_principal_id,
                session_id=binding.original_root_id), capacity["build_id"])
            await storage.publish_supervision(db, row, value, producer)
            await db.commit()
        async with jobs._session() as db:
            _child, _binding, row, value, fresh, _envelope = await invocation_scope(db, principal, job_id, fencing_token, inputs)
            staged = storage.prepare_publications(row, value, rendered, task_id=binding.task_id,
                attempt_id=binding.attempt_id, job_id=job_id, fence=fencing_token,
                profile_digest=storage.PROFILE_DIGEST, producer=producer)
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            _child, _binding, row, value, fresh, _envelope = await invocation_scope(db, principal, job_id, fencing_token, inputs, staged_envelope=envelope)
            staged = await storage.reserve_publications(db, row, value, staged)
            await db.commit()
        readback = storage.publish_publications(row, value, staged)
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            child, _binding, row, value, fresh, _envelope = await invocation_scope(db, principal, job_id, fencing_token, inputs, staged_envelope=envelope)
            if fresh != capacity or _records(child).get("document-child") != child_binding:
                raise BoardError("document_build_reap_changed", "The original renderer delivery changed", status_code=409)
            witness, reap = _fresh_supervised_reap(row, value, capacity, child_binding)
            output = storage.adopt_outputs(row, value, staged, readback=readback,
                native_binding=capacity, reap=reap)
            history = json.loads(child.checkpoint_receipts_json)
            history.append({"checkpoint_id": "document-reaped", "payload": {"binding": child_binding,
                "witness_sha256": reap["witness_sha256"], "wait_reaped": True, "parser_exit": witness["parser_exit"]}, "safe": True})
            child.checkpoint_receipts_json = sources.canonical(history).decode()
            child.revision += 1
            await db.commit()
        return output
    except BaseException:
        # This explicit original read/close/wait path may establish physical
        # closure; the fallback finally wait below never issues a producer.
        if producer is None and source_snapshot is not None:
            try:
                if not stdin_closed:
                    process.stdin.close()
                    await asyncio.wait_for(process.stdin.wait_closed(), remaining(5))
                    stdin_closed = True
                while not stdout_eof:
                    extra = await asyncio.wait_for(process.stdout.read(65536), remaining(5))
                    if len(stdout_capture)+len(extra) > _STDOUT_MAX:
                        raise ValueError("bounded original stdout exceeded")
                    stdout_capture.extend(extra)
                    stdout_eof = not extra
                await asyncio.wait_for(process.wait(), remaining(5))
                waited = True
                producer = _issue_supervision(process,row,value,capacity,binding,child_binding,source_snapshot,
                    stdout_capture,stdin_closed=stdin_closed,stdout_eof=stdout_eof,waited=waited,rendered=None)
                async with jobs._session() as db:
                    await db.execute(text("BEGIN IMMEDIATE"))
                    row,value = await storage.owned(db,WorkBoardOwner(principal_id=binding.owner_principal_id,
                        session_id=binding.original_root_id),capacity["build_id"])
                    await storage.publish_supervision(db,row,value,producer)
                    await db.commit()
            except (Exception, asyncio.CancelledError):
                pass  # Unproven closure retains both durable capacity markers.
        raise
    finally:
        # An interrupted waiter never frees either durable marker. Only the
        # original positive persisted witness can authorize reconciliation.
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.shield(asyncio.wait_for(process.wait(), max(.001, (deadline - now()).total_seconds())))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if parent_fd >= 0:
            os.close(parent_fd)


async def validate_output_readback(db, task, attempt, child, row, value):
    """Completed readback matches the original child and actual C1 result."""
    from src.work_board.document_capacity import _records, _held
    from src.workflows.general_task_guard import child_binding
    from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input
    from src.work_board import document_build_storage as storage
    binding = child_binding(child)
    records = _records(child)
    capacity = records.get("document-capacity")
    if (capacity is None or _held(child) or binding.task_id != task.task_id or binding.attempt_id != attempt.attempt_id
            or capacity != value.get("renderer_binding") or capacity["build_id"] != row.artifact_id
            or capacity["input_artifact_id"] != task.input_artifact_id
            or records["document-reaped"]["witness_sha256"] != value["reap"].get("witness_sha256")
            or child.status not in {"succeeded", "degraded"} or child.attempt_count != 1):
        raise BoardError("document_build_output_unverified", "The exact completed native renderer readback is required", status_code=409)
    private = read_bound_native_tool_input(child, binding)
    if private.inputs != {"build_ref": "document-build:" + row.artifact_id, "spec_digest": value["spec_digest"]}:
        raise BoardError("document_build_output_unverified", "The original private input changed", status_code=409)
    effects = json.loads(child.effect_receipts_json)
    from src.workflows.job_runtime import _job_has_unsafe_effects
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    content = json.dumps({"step_id": binding.step_id, "output": value["output"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    from hashlib import sha256
    content_digest = sha256(content).hexdigest()
    key = digest([child.run_identity, binding.plan_digest, binding.step_id])
    path = f"artifacts/work-board/general-tasks/{key}-{content_digest}.json"
    artifacts = [item for item in json.loads(child.artifact_receipts_json)
        if item.get("artifact_type") == "general_task_step" and item.get("file_path") == path
        and item.get("content_sha256") == content_digest and item.get("size_bytes") == len(content)]
    readbacks = [item for item in effects if item.get("effect_type") == "general_tool_call"
        and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"
        and item.get("effect_id") == "general:" + binding.step_id + ":" + str(child.fencing_token)
        and item.get("fencing_token") == child.fencing_token
        and item.get("target_path") == "general-step:" + digest([child.run_identity, binding.step_id])
        and item.get("content_sha256") == content_digest
        and item.get("details", {}).get("verified") is True
        and item.get("details", {}).get("input_digest") == binding.input_digest
        and item.get("details", {}).get("file_path") == path]
    if (len(artifacts) != 1 or len(readbacks) != 1 or _job_has_unsafe_effects(effects)
            or _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
                expected_digest=content_digest, expected_size=len(content)) != content):
        raise BoardError("document_build_output_unverified", "The original C1 callback has no verified readback", status_code=409)


async def settled_output_authority(db, child, output):
    """Metadata-only original owner gate for the C1 artifact/readback writer."""
    from src.workflows.general_task_guard import assert_general_task_child_current, child_binding
    from src.work_board.document_capacity import _records, _held
    from src.work_board import document_build_storage as storage
    await assert_general_task_child_current(db, child)
    binding = child_binding(child)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    records = _records(child)
    capacity = records.get("document-capacity")
    if capacity is None or _held(child) or utc(datetime.fromisoformat(capacity["original_deadline"])) <= now():
        raise BoardError("document_build_output_unverified", "The original renderer cutoff and positive reap are required", status_code=409)
    row, value = await storage.owned(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), capacity["build_id"])
    await storage.authority(db, WorkBoardOwner(principal_id=task.owner_principal_id,
        session_id=task.owner_session_id), row, value, metadata_only=True)
    if (row.bound_task_id != task.task_id or value.get("renderer_binding") != capacity
            or value.get("live_writer") is not None or value.get("output") != output
            or value.get("phase") not in {"completed", "degraded"}
            or value.get("reap", {}).get("witness_sha256") != records["document-reaped"]["witness_sha256"]):
        raise BoardError("document_build_output_unverified", "The actual original private output changed", status_code=409)


async def reconcile_reap(jobs, owner, build_id, operator, *, expected_revision, expected_metadata_digest):
    """Reduce physical capacity only; preserve every native stop/outcome row."""
    from sqlalchemy import text
    from src.work_board import document_build_storage as storage
    from src.work_board.document_capacity import _records, _identity
    async with jobs._session() as db:
        row, value = await storage.owned(db, owner, build_id, revision=expected_revision)
        await storage.cleanup_authority(db, owner, row, value, operator)
        if row.metadata_digest != expected_metadata_digest:
            raise BoardError("document_build_reap_changed", "Reload the original build metadata", status_code=409)
        capacity = value.get("renderer_binding")
        if not capacity:
            if value.get("live_writer") or value.get("supervision") or value.get("reap"):
                raise BoardError("document_build_reap_changed", "The original capacity markers disagree", status_code=409)
            return {"cleanup_proven": True, "build_revision": row.revision,
                "metadata_digest": row.metadata_digest, "no_learning": True}
        child = await jobs._fetch(db, capacity["job_id"])
        _binding, parent, task, attempt, cancellation = await _build_context(db, row, value, child)
        validate_capacity_record(child, capacity, _cancel_context=cancellation)
        records = _records(child)
        if records.get("document-capacity") != capacity or records.get("document-child") is None:
            raise BoardError("document_build_reap_unavailable", "The original process reservation is required", status_code=409)
        # The persisted source-owned outer receipt is mandatory even on replay.
        _supervision(value)
        original = (_identity(child), parent.revision, parent.checkpoint_receipts_json,
            task.task_revision, digest(attempt.model_dump(mode="json")), row.revision, row.metadata_digest)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row, value = await storage.owned(db, owner, build_id, revision=expected_revision)
        await storage.cleanup_authority(db, owner, row, value, operator)
        child = await jobs._fetch(db, capacity["job_id"])
        _binding, parent, task, attempt, cancellation = await _build_context(db, row, value, child)
        if ((_identity(child), parent.revision, parent.checkpoint_receipts_json,
                task.task_revision, digest(attempt.model_dump(mode="json")), row.revision, row.metadata_digest) != original
                or row.metadata_digest != expected_metadata_digest or value.get("renderer_binding") != capacity):
            raise BoardError("document_build_reap_changed", "The original cleanup CAS changed", status_code=409)
        validate_capacity_record(child, capacity, _cancel_context=cancellation)
        records = _records(child)
        _witness, reap = _fresh_supervised_reap(row, value, capacity, records["document-child"])
        if value.get("live_writer") is None:
            if value.get("reap") != reap or not _authenticated_reap(value, capacity):
                raise BoardError("document_build_reap_changed", "The two original capacity markers disagree", status_code=409)
        else:
            if value["live_writer"] != {"token":capacity["nonce"], "slot":"renderer"}:
                raise BoardError("document_build_reap_changed", "The original live writer changed", status_code=409)
            value.update({"live_writer": None, "reap": reap, "phase":"unknown",
                "reason":"document_original_process_quiescent"})
            storage.persist(row, value)
        await db.commit()
        return {"cleanup_proven":True, "build_revision":row.revision,
            "metadata_digest":row.metadata_digest, "no_learning":True}
