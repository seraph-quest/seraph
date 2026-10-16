"""Bounded immutable native plan/receipt staging on existing private artifacts."""
from dataclasses import dataclass
import hashlib
import json
from weakref import WeakKeyDictionary

from src.work_board.contracts import (GeneralTaskArtifactRef, GeneralTaskEnvelope,
    GeneralTaskPlanRevisionV1, GeneralTaskStepReceiptV1, GeneralTaskToolInputV1, WorkBoardOwner,
    SpecialistEvidenceHandoffV1, SpecialistPartialResultV1)
from src.work_board.repository import BoardError

_STAGING_SEAL = object()
_CANCEL_OUTPUT_SEAL = object()
_CANCEL_OUTPUT_WITNESSES = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class NativeCancelOutputWitness:
    """Private original invocation metadata; never execution authority."""
    root_path: str
    root_identity_json: str
    binding_digest: str
    fencing_token: int
    original_intent_json: str
    seal: object


def capture_native_cancel_output_witness(binding, *, fencing_token, intent):
    from config.settings import settings
    from src.workspace import canonical_workspace_root, canonical_workspace_root_identity
    from src.work_board.general_task import digest
    root = canonical_workspace_root(settings.workspace_dir)
    identity = canonical_workspace_root_identity(root)
    if digest(identity) != binding.live_root_digest:
        raise BoardError("general_task_native_binding_changed", "Original output root required", status_code=409)
    witness = NativeCancelOutputWitness(str(root), json.dumps(identity, sort_keys=True),
        digest(binding.model_dump(mode="json")), fencing_token,
        json.dumps(intent, sort_keys=True), _CANCEL_OUTPUT_SEAL)
    # Identity registration prevents copied/replaced dataclasses from carrying
    # a source seal onto caller supplied root or intent metadata.
    _CANCEL_OUTPUT_WITNESSES[witness] = (witness.root_path, witness.root_identity_json,
        witness.binding_digest, witness.fencing_token, witness.original_intent_json)
    return witness


def release_native_cancel_output_witness(witness):
    if witness is not None:
        _CANCEL_OUTPUT_WITNESSES.pop(witness, None)


def verify_native_cancel_output_witness(witness, *, binding, fencing_token):
    from pathlib import Path
    from src.workspace import canonical_workspace_root_identity
    from src.work_board.general_task import digest
    if (type(witness) is not NativeCancelOutputWitness
        or witness.seal is not _CANCEL_OUTPUT_SEAL
        or _CANCEL_OUTPUT_WITNESSES.get(witness) != (witness.root_path,
            witness.root_identity_json, witness.binding_digest, witness.fencing_token,
            witness.original_intent_json)
        or witness.binding_digest != digest(binding.model_dump(mode="json"))
        or witness.fencing_token != fencing_token):
        raise BoardError("general_task_native_binding_changed", "Original output producer required", status_code=409)
    identity = json.loads(witness.root_identity_json)
    root = Path(witness.root_path)
    if digest(identity) != binding.live_root_digest or canonical_workspace_root_identity(root) != identity:
        raise BoardError("general_task_native_binding_changed", "Original physical output root changed", status_code=409)
    return root, json.loads(witness.original_intent_json)


def read_native_cancel_output_bytes(witness, *, binding, fencing_token,
                                   file_path, expected_digest, expected_size):
    """Read only a cancelled invocation's exact private original output."""
    import os
    import stat
    from src.work_board.general_task import digest
    from src.workspace import canonical_workspace_root_identity
    root, _intent = verify_native_cancel_output_witness(witness,
        binding=binding, fencing_token=fencing_token)
    key = digest([binding.invocation_id, binding.plan_digest, binding.step_id])
    if (type(expected_digest) is not str or len(expected_digest) != 64
        or any(c not in "0123456789abcdef" for c in expected_digest)
        or type(expected_size) is not int or not 0 < expected_size <= 65536
        or file_path != f"artifacts/work-board/general-tasks/{key}-{expected_digest}.json"):
        raise BoardError("general_task_artifact_changed", "Exact original output required", status_code=409)
    identity = json.loads(witness.root_identity_json)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptors, edges = [], []
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    def same(left, right):
        return all(getattr(left, field) == getattr(right, field) for field in fields)
    def deny():
        raise BoardError("general_task_artifact_changed", "Original physical output changed", status_code=409)
    try:
        root_fd = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(root_fd)
        root_stat = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid not in {0, os.getuid()}
            or root_stat.st_dev != identity["device"] or root_stat.st_ino != identity["inode"]
            or hashlib.sha256(str(root).encode()).hexdigest() != identity["path_digest"]):
            deny()
        parent_fd = root_fd
        components = file_path.split("/")
        for component in components[:-1]:
            child_fd = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent_fd)
            descriptors.append(child_fd)
            metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                deny()
            edges.append((parent_fd, component, child_fd, metadata))
            parent_fd = child_fd
        leaf = components[-1]
        descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077 or metadata.st_nlink != 1 or metadata.st_size != expected_size):
            deny()
        chunks, remaining = [], expected_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if (len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_digest
            or not same(metadata, os.fstat(descriptor))
            or not same(metadata, os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False))):
            deny()
        for parent_fd, component, child_fd, original in edges:
            if (not same(original, os.fstat(child_fd))
                or not same(original, os.stat(component, dir_fd=parent_fd, follow_symlinks=False))):
                deny()
        if (not same(root_stat, os.fstat(root_fd))
            or not same(root_stat, os.stat(root, follow_symlinks=False))
            or canonical_workspace_root_identity(root) != identity):
            deny()
        return raw
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
_ARTIFACT_MODELS = {"GeneralTaskPlanRevision.v1": GeneralTaskPlanRevisionV1,
    "StepReceipt.v1": GeneralTaskStepReceiptV1, "GeneralTaskToolInput.v1": GeneralTaskToolInputV1,
    "SpecialistEvidenceHandoff.v1": SpecialistEvidenceHandoffV1,"SpecialistPartialResult.v1":SpecialistPartialResultV1}
_ARTIFACT_KINDS = {"GeneralTaskPlanRevision.v1": "general_task_plan_revision",
    "StepReceipt.v1": "general_task_step_receipt", "GeneralTaskToolInput.v1": "general_task_tool_input",
    "SpecialistEvidenceHandoff.v1": "specialist_evidence_handoff","SpecialistPartialResult.v1":"specialist_partial_result"}


@dataclass(frozen=True)
class StagedTaskArtifact:
    reference: GeneralTaskArtifactRef
    file_path: str
    size_bytes: int
    producer_ref: str
    creation_digest: str
    payload: bytes
    seal: object
    registry_record: object = None


def stage_task_artifact(*, parent_job_id, creation_digest, payload):
    from src.work_board.general_task import canonical, digest
    from src.work_board.input_artifacts import _write_payload, _safe_file_bytes
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    if not isinstance(payload, (GeneralTaskPlanRevisionV1, GeneralTaskStepReceiptV1, GeneralTaskToolInputV1, SpecialistEvidenceHandoffV1,SpecialistPartialResultV1)):
        raise BoardError("general_task_artifact_schema", "Native immutable artifact schema required", status_code=409)
    if (isinstance(payload, (GeneralTaskPlanRevisionV1, GeneralTaskToolInputV1, SpecialistEvidenceHandoffV1,SpecialistPartialResultV1)) and (payload.parent_job_id != parent_job_id or payload.creation_digest != creation_digest)
        or isinstance(payload, GeneralTaskStepReceiptV1) and payload.parent_creation_digest != creation_digest):
        raise BoardError("general_task_artifact_binding", "Native artifact belongs to another creation", status_code=409)
    content = canonical(payload.model_dump(mode="json"))
    sha = hashlib.sha256(content).hexdigest()
    key = digest([parent_job_id, creation_digest, payload.schema_version, sha])
    path = f"artifacts/work-board/general-tasks/{key}-{sha}.json"
    absolute = canonical_workspace_root(settings.workspace_dir) / path
    _write_payload(absolute, content)
    if _safe_file_bytes(absolute, expected_digest=sha, expected_size=len(content)) != content:
        raise BoardError("general_task_artifact_changed", "Native artifact failed physical readback", status_code=409)
    kind = _ARTIFACT_KINDS[payload.schema_version]
    identifier = artifact_id_for(file_path=path, artifact_type=kind, producer="agent.task.v1",
        run_id=parent_job_id, content_sha256=sha)
    from src.artifacts.registry import build_artifact_record
    record = build_artifact_record(file_path=path, artifact_type=kind,
        producer='agent.task.v1', run_id=parent_job_id, content=content)
    return StagedTaskArtifact(GeneralTaskArtifactRef(artifact_id=identifier, digest=sha,
        schema_version=payload.schema_version), path, len(content), parent_job_id, creation_digest, content, _STAGING_SEAL,
        record)


def recheck_staged_task_artifact(staged, *, parent_job_id, creation_digest):
    """Check the original off-writer physical proof without filesystem access."""
    from src.work_board.general_task import digest
    from src.artifacts.registry import artifact_id_for
    if (type(staged) is not StagedTaskArtifact or staged.seal is not _STAGING_SEAL
        or staged.producer_ref != parent_job_id or staged.creation_digest != creation_digest
        or not 0 < staged.size_bytes <= 65536 or len(staged.payload) != staged.size_bytes
        or hashlib.sha256(staged.payload).hexdigest() != staged.reference.digest):
        raise BoardError('general_task_artifact_binding', 'Exact native staged artifact required', status_code=409)
    kind = _ARTIFACT_KINDS[staged.reference.schema_version]
    key = digest([parent_job_id, creation_digest, staged.reference.schema_version, staged.reference.digest])
    expected_path = f'artifacts/work-board/general-tasks/{key}-{staged.reference.digest}.json'
    record = staged.registry_record
    if (staged.file_path != expected_path or not isinstance(record, dict)
        or record.get('artifact_id') != staged.reference.artifact_id
        or record.get('content_sha256') != staged.reference.digest
        or record.get('size_bytes') != staged.size_bytes or record.get('file_path') != staged.file_path
        or record.get('producer') != 'agent.task.v1' or record.get('run_id') != parent_job_id
        or staged.reference.artifact_id != artifact_id_for(file_path=expected_path,
            artifact_type=kind, producer='agent.task.v1', run_id=parent_job_id,
            content_sha256=staged.reference.digest)):
        raise BoardError('general_task_artifact_binding', 'Native registry reference changed', status_code=409)
    parsed = _ARTIFACT_MODELS[staged.reference.schema_version].model_validate_json(staged.payload)
    if (isinstance(parsed, (GeneralTaskPlanRevisionV1, GeneralTaskToolInputV1))
        and (parsed.parent_job_id != parent_job_id or parsed.creation_digest != creation_digest)
        or isinstance(parsed, GeneralTaskStepReceiptV1) and parsed.parent_creation_digest != creation_digest):
        raise BoardError('general_task_artifact_binding', 'Native creation reference changed', status_code=409)
    return parsed, dict(record)


def stage_retained_native_artifact(reference, *, parent_job_id, creation_digest):
    """Read an existing immutable receipt; never rewrite its retained file."""
    from src.work_board.general_task import canonical, digest
    from src.artifacts.registry import build_artifact_record
    parsed = read_native_artifact_reference(reference, parent_job_id=parent_job_id, creation_digest=creation_digest)
    content = canonical(parsed.model_dump(mode='json'))
    key = digest([parent_job_id, creation_digest, reference.schema_version, reference.digest])
    path = f'artifacts/work-board/general-tasks/{key}-{reference.digest}.json'
    record = build_artifact_record(file_path=path, artifact_type=_ARTIFACT_KINDS[reference.schema_version],
        producer='agent.task.v1', run_id=parent_job_id, content=content)
    staged = StagedTaskArtifact(reference, path, len(content), parent_job_id, creation_digest,
        content, _STAGING_SEAL, record)
    recheck_staged_task_artifact(staged, parent_job_id=parent_job_id, creation_digest=creation_digest)
    return staged


def verify_staged_task_artifact(staged, *, parent_job_id, creation_digest):
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.artifacts.registry import build_artifact_record
    if (not isinstance(staged, StagedTaskArtifact) or staged.seal is not _STAGING_SEAL
        or staged.producer_ref != parent_job_id or staged.creation_digest != creation_digest
        or not 0 < staged.size_bytes <= 65536):
        raise BoardError("general_task_artifact_binding", "Exact native staged artifact required", status_code=409)
    content = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / staged.file_path,
        expected_digest=staged.reference.digest, expected_size=staged.size_bytes)
    if content != staged.payload:
        raise BoardError("general_task_artifact_changed", "Native immutable artifact changed", status_code=409)
    model = _ARTIFACT_MODELS[staged.reference.schema_version]
    parsed = model.model_validate_json(content)
    kind = _ARTIFACT_KINDS[staged.reference.schema_version]
    record = build_artifact_record(file_path=staged.file_path, artifact_type=kind,
        producer="agent.task.v1", run_id=parent_job_id, content=content)
    if record["artifact_id"] != staged.reference.artifact_id or record["content_sha256"] != staged.reference.digest:
        raise BoardError("general_task_artifact_binding", "Native registry reference changed", status_code=409)
    return parsed, record


def read_native_artifact_reference(reference, *, parent_job_id, creation_digest):
    """Resolve an opaque bounded ref by the fixed native immutable name recipe."""
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from src.artifacts.registry import artifact_id_for
    from config.settings import settings
    if not isinstance(reference, GeneralTaskArtifactRef) or reference.schema_version not in _ARTIFACT_MODELS:
        raise BoardError("general_task_artifact_schema", "Known immutable native reference required", status_code=409)
    key = digest([parent_job_id, creation_digest, reference.schema_version, reference.digest])
    path = f"artifacts/work-board/general-tasks/{key}-{reference.digest}.json"
    absolute = canonical_workspace_root(settings.workspace_dir) / path
    try:
        size = absolute.lstat().st_size
    except OSError as exc:
        raise BoardError("general_task_artifact_missing", "Inspect missing immutable task evidence", status_code=409) from exc
    if not 0 < size <= 65536:
        raise BoardError("general_task_artifact_schema", "Immutable native evidence exceeds its bound", status_code=409)
    content = _safe_file_bytes(absolute, expected_digest=reference.digest, expected_size=size)
    kind = _ARTIFACT_KINDS[reference.schema_version]
    if reference.artifact_id != artifact_id_for(file_path=path, artifact_type=kind,
        producer="agent.task.v1", run_id=parent_job_id, content_sha256=reference.digest):
        raise BoardError("general_task_artifact_binding", "Immutable native artifact identity changed", status_code=409)
    model = _ARTIFACT_MODELS[reference.schema_version]
    parsed = model.model_validate_json(content)
    if (model in {GeneralTaskPlanRevisionV1, GeneralTaskToolInputV1, SpecialistEvidenceHandoffV1,SpecialistPartialResultV1} and (parsed.parent_job_id != parent_job_id or parsed.creation_digest != creation_digest)
        or model is GeneralTaskStepReceiptV1 and parsed.parent_creation_digest != creation_digest):
        raise BoardError("general_task_artifact_binding", "Immutable native creation binding changed", status_code=409)
    return parsed


async def read_current_native_envelope(db, run, task, attempt):
    from src.workflows.general_task_guard import assert_original_parent_authority
    assert_original_parent_authority(run)
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.db.models import WorkBoardInputArtifact
    if (task.capability_id != "agent.task.v1" or attempt.task_id != task.task_id
        or attempt.workflow_run_id != run.run_identity or attempt.ended_at is not None
        or attempt.cancel_requested_at is not None or task.owner_principal_id != run.owner_principal_id
        or task.owner_session_id != run.session_id or run.operator_session_id != task.owner_session_id
        or task.goal_id != run.goal_id or task.goal_revision != run.goal_revision):
        raise BoardError("general_task_native_binding_changed", "Original native task binding changed", status_code=409)
    source = await db.get(WorkBoardInputArtifact, task.input_artifact_id,
        populate_existing=True)
    if (source is None or source.typed_input_ref != task.typed_input_ref
        or source.payload_sha256 != task.typed_input_digest):
        raise BoardError("general_task_native_binding_changed", "Original native input reference changed", status_code=409)
    from src.work_board.channel_capture import check_current_captured_task_source
    await check_current_captured_task_source(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id), task)
    resolved = await resolve_input_artifact_for_task(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
        artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
        capability_id="agent.task.v1", expected_task_id=task.task_id)
    envelope = GeneralTaskEnvelope.model_validate(resolved.input)
    if envelope.proposal_group is None:
        raise BoardError("general_task_provenance_missing", "Inspect the original task allowance", status_code=409)
    return envelope


def selected_grant_digest(envelope):
    from src.work_board.general_task import digest
    return digest([item.model_dump(mode="json") for item in envelope.descriptors])


def compile_creation_digest(parent, task, attempt, envelope):
    from src.work_board.general_task import digest
    from src.work_board.pipelines import root_binding
    from src.workflows.inference_accounting import _utc
    from src.work_board.channel_capture import _staged_source_root_for_sql, _SOURCE_FRAME
    root = dict(_staged_source_root_for_sql()) if _SOURCE_FRAME.get() is not None else root_binding()
    return digest(["general-task.creation.v1", parent.run_identity, parent.input_digest,
        parent.authority_digest, task.task_id, attempt.attempt_id, task.owner_principal_id,
        task.owner_session_id, task.goal_id, task.goal_revision, task.input_artifact_id,
        task.typed_input_digest, envelope.proposal_group.group_id,
        digest(envelope.proposal_group.model_dump(mode="json")), selected_grant_digest(envelope),
        digest(root), _utc(parent.deadline_at).isoformat(),
        envelope.proposal_group.original_deadline_at.isoformat()])


def compile_phase_digest(manifest):
    from src.work_board.general_task import digest
    return digest(["general-task.phase.v1", manifest.creation_digest, manifest.phase_revision,
        manifest.phase, manifest.plan_revision, manifest.current_plan_digest,
        manifest.admitted_invocation_ids, manifest.job_fence, manifest.board_fence])


def initial_native_manifest(parent, task, attempt, envelope):
    """Freeze revision one from the canonical original publication."""
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    group = envelope.proposal_group
    if group is None or envelope.plan is None or envelope.plan.revision != 1:
        raise BoardError("general_task_provenance_missing", "Original published plan required", status_code=409)
    manifest = GeneralTaskCurrentManifestV1(
        task_id=task.task_id, original_root_id=task.owner_session_id,
        owner_principal_id=task.owner_principal_id, attempt_id=attempt.attempt_id,
        run_id=parent.run_identity, task_revision=task.task_revision,
        manifest_revision=1, board_fence=attempt.fencing_token, job_fence=parent.fencing_token,
        original_envelope_artifact_id=task.input_artifact_id,
        original_envelope_digest=task.typed_input_digest, original_input_digest=parent.input_digest,
        selected_grant_digest=selected_grant_digest(envelope), group_id=group.group_id,
        group_digest=digest(group.model_dump(mode="json")), original_limits_digest=group.limits_digest,
        creation_digest=compile_creation_digest(parent, task, attempt, envelope),
        original_deadline_at=group.original_deadline_at, native_deadline_at=_utc(parent.deadline_at),
        phase="native_ready", phase_revision=1,
        phase_digest="0" * 64, plan_revision=1, current_plan_artifact_id=task.input_artifact_id,
        current_plan_digest=digest(envelope.plan.model_dump(mode="json")), revision_numbers=[1],
        revision_artifact_ids=[task.input_artifact_id], revision_artifact_digests=[task.typed_input_digest],
        revision_artifact_schemas=["GeneralTaskEnvelope.v1"])
    return manifest.model_copy(update={"phase_digest": compile_phase_digest(manifest)})


def compile_native_child_binding(parent, task, attempt, manifest, step, descriptor, inputs):
    """Derive one child identity from frozen native facts, never planner text."""
    from src.work_board.contracts import GeneralTaskNativeChildBindingV1
    from src.work_board.general_task import digest
    from src.work_board.pipelines import root_binding
    invocation_id = "general-tool:" + digest([manifest.creation_digest, manifest.plan_revision,
        step.step_id, digest(inputs), digest(descriptor.model_dump(mode="json"))])[:48]
    return GeneralTaskNativeChildBindingV1(parent_job_id=parent.run_identity,
        task_id=task.task_id, attempt_id=attempt.attempt_id,
        original_root_id=task.owner_session_id, owner_principal_id=task.owner_principal_id,
        goal_id=task.goal_id, goal_revision=task.goal_revision,
        original_deadline_at=manifest.original_deadline_at,
        native_deadline_at=manifest.native_deadline_at,
        original_envelope_digest=manifest.original_envelope_digest,
        parent_authority_digest=parent.authority_digest, creation_digest=manifest.creation_digest,
        creation_job_fence=parent.fencing_token, creation_board_fence=attempt.fencing_token,
        plan_revision=manifest.plan_revision, plan_digest=manifest.current_plan_digest,
        step_id=step.step_id, invocation_id=invocation_id, input_digest=digest(inputs),
        descriptor_digest=digest(descriptor.model_dump(mode="json")),
        selected_grant_digest=manifest.selected_grant_digest,
        phase_revision=manifest.phase_revision, phase_digest=manifest.phase_digest,
        live_root_digest=digest(root_binding()))


async def read_current_native_tool_input(db, child):
    """Resolve private literals only behind the current fixed child guard."""
    from src.workflows.general_task_guard import assert_general_task_child_current, child_binding
    from src.work_board.general_task import digest
    await assert_general_task_child_current(db, child)
    binding = child_binding(child)
    from src.workflows.general_task_guard import check_native_writer_source
    await check_native_writer_source(db, child)
    return read_bound_native_tool_input(child, binding)


def read_bound_native_tool_input(child, binding):
    """Physical literals after the fixed caller proves its current phase."""
    from src.work_board.general_task import digest
    arguments = json.loads(child.arguments_json)
    keys = {"step_id", "tool_id", "tool_input_digest", "descriptor_digest", "typed_input_ref", "typed_input_digest"}
    if (not isinstance(arguments, dict) or set(arguments) != keys or digest(arguments) != child.input_digest
        or arguments["step_id"] != binding.step_id
        or arguments["tool_input_digest"] != binding.input_digest
        or arguments["descriptor_digest"] != binding.descriptor_digest
        or not arguments["typed_input_ref"].startswith("general-task-input:")):
        raise BoardError("general_task_native_input_changed", "Exact private native input required", status_code=409)
    reference = GeneralTaskArtifactRef(artifact_id=arguments["typed_input_ref"].removeprefix("general-task-input:"),
        digest=arguments["typed_input_digest"], schema_version="GeneralTaskToolInput.v1")
    payload = read_native_artifact_reference(reference, parent_job_id=binding.parent_job_id,
        creation_digest=binding.creation_digest)
    if (payload.invocation_id != child.run_identity or payload.tool_id != arguments["tool_id"]
        or payload.descriptor_digest != binding.descriptor_digest
        or payload.input_digest != binding.input_digest or digest(payload.inputs) != binding.input_digest
        or not any(item.get("artifact_id") == reference.artifact_id
            and item.get("content_sha256") == reference.digest
            for item in json.loads(child.artifact_receipts_json))):
        raise BoardError("general_task_native_input_changed", "Canonical private input readback changed", status_code=409)
    return payload


async def resolve_current_native_step_inputs(db, parent, task, attempt, manifest, envelope, step):
    """Resolve dependencies from canonical successful native readbacks only."""
    from src.work_board.general_task import resolve_input, validate_schema, validate_data
    from src.work_board.general_task_native import current_plan
    plan = current_plan(manifest, envelope)
    if next((item for item in plan.steps if item.step_id == step.step_id), None) != step:
        raise BoardError("general_task_native_input_changed", "Current canonical plan step required", status_code=409)
    outputs = await read_current_native_outputs(db, parent, task, attempt, manifest, envelope,
        step.depends_on)
    inputs = resolve_input(step.input, outputs)
    from src.workflows.specialist_evidence import resolve_specialist_evidence
    inputs = await resolve_specialist_evidence(db, task, envelope, inputs)
    validate_data(inputs, dependencies=set())
    descriptors = {item.tool_id: item for item in envelope.descriptors}
    validate_schema(descriptors[step.tool_id].input_schema, inputs)
    return inputs


async def read_current_native_outputs(db, parent, task, attempt, manifest, envelope, step_ids):
    """Read retained outputs only through their original successful child receipts."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding
    from src.workflows.job_runtime import _digest
    from src.work_board.general_task import digest, validate_schema
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.work_board.general_task_native import current_plan
    plan = current_plan(manifest, envelope)
    outputs = {}
    descriptors = {item.tool_id: item for item in envelope.descriptors}
    for dependency in step_ids:
        if dependency not in manifest.step_ids:
            raise BoardError("general_task_dependency_unverified", "Canonical verified predecessor required", status_code=409)
        index = manifest.step_ids.index(dependency)
        receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version="StepReceipt.v1"), parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest)
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == receipt.child_job_id))
        if child is None:
            raise BoardError("general_task_dependency_unverified", "Canonical predecessor invocation missing", status_code=409)
        binding = child_binding(child)
        effects = json.loads(child.effect_receipts_json)
        degraded_build = child.status == "degraded" and envelope.task_input.document_build is not None
        if (receipt.status != "verified" or receipt.contact_state != "settled"
            or (child.status != "succeeded" and not degraded_build) or binding.step_id != dependency
            or binding.creation_digest != manifest.creation_digest
            or binding.selected_grant_digest != manifest.selected_grant_digest
            or binding.original_root_id != task.owner_session_id
            or receipt.input_digest != binding.input_digest
            or receipt.descriptor_digest != binding.descriptor_digest
            or receipt.effect_receipt_digest != _digest(effects)):
            raise BoardError("general_task_dependency_unverified", "Exact successful predecessor readback required", status_code=409)
        from src.workflows.specialist_lifecycle import read_fact,CLOSURE_KEY,SpecialistDelegationClosureV1
        if read_fact(child,CLOSURE_KEY,SpecialistDelegationClosureV1) is not None:
            from src.workflows.specialist_result import verify_full_delegation_result
            await verify_full_delegation_result(db,parent,child,receipt)
        references = [ref for ref in receipt.artifact_refs if ref.schema_version == "GeneralTaskOutput.v1"]
        if len(references) != 1:
            raise BoardError("general_task_dependency_unverified", "Exactly one native output artifact required", status_code=409)
        reference = references[0]
        records = [record for record in json.loads(child.artifact_receipts_json)
            if record.get("artifact_id") == reference.artifact_id and record.get("content_sha256") == reference.digest]
        key = digest([child.run_identity, binding.plan_digest, dependency])
        path = f"artifacts/work-board/general-tasks/{key}-{reference.digest}.json"
        if (len(records) != 1 or records[0].get("file_path") != path
            or type(records[0].get("size_bytes")) is not int or not 0 < records[0]["size_bytes"] <= 65536
            or reference.artifact_id != artifact_id_for(file_path=path, artifact_type="general_task_step",
                producer=child.job_kind, run_id=child.run_identity, content_sha256=reference.digest)
            or not any(effect.get("effect_type") == "general_tool_call" and effect.get("status") == "succeeded"
                and effect.get("content_sha256") == reference.digest
                and effect.get("details", {}).get("step_id") == dependency
                and effect.get("details", {}).get("verified") is True for effect in effects)):
            raise BoardError("general_task_dependency_unverified", "Canonical output identity or physical readback changed", status_code=409)
        body = json.loads(_safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
            expected_digest=reference.digest, expected_size=records[0]["size_bytes"]))
        if set(body) != {"step_id", "output"} or body["step_id"] != dependency:
            raise BoardError("general_task_dependency_unverified", "Native output identity changed", status_code=409)
        descriptor = descriptors.get(json.loads(child.arguments_json).get("tool_id"))
        if descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest:
            raise BoardError("general_task_dependency_unverified", "Original predecessor descriptor changed", status_code=409)
        validate_schema(descriptor.output_schema, body["output"])
        predecessor = next((item for item in plan.steps if item.step_id == dependency), None)
        if predecessor is None:
            raise BoardError("general_task_dependency_unverified", "Frozen predecessor step missing", status_code=409)
        validate_schema(predecessor.output_contract, body["output"])
        if degraded_build:
            from src.work_board import document_build_native, document_build_storage
            original = envelope.task_input.document_build
            if (descriptor != document_build_native.descriptor() or predecessor.tool_id != "document_build"
                    or body["output"].get("pdf_artifact") is not None or not body["output"].get("warnings")):
                raise BoardError("general_task_dependency_unverified", "The fixed verified degraded build is required", status_code=409)
            owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
            row, value = await document_build_storage.owned(db, owner, original.build_ref.split(":", 1)[1])
            await document_build_storage.authority(db, owner, row, value, metadata_only=True)
            if (document_build_storage.build_binding(row, value) != original.model_dump(mode="json")
                    or value.get("phase") != "degraded" or value.get("output") != body["output"]):
                raise BoardError("general_task_dependency_unverified", "The original private degraded output changed", status_code=409)
            await document_build_native.validate_output_readback(db, task, attempt, child, row, value)
        outputs[dependency] = body["output"]
    return outputs


async def verify_general_task_manifest(db, parent, task, attempt, manifest):
    """Current canonical envelope/group check inside the native SQL writer.

    Revision/receipt staging is separately sealed and physically verified;
    the runtime owner supplies and validates the exact current CAS counters.
    """
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    envelope = await read_current_native_envelope(db, parent, task, attempt)
    return await _verify_native_manifest_data(parent, task, attempt, manifest, envelope)


async def _verify_native_manifest_data(parent, task, attempt, manifest, envelope, *, _staged_artifacts=None):
    from src.work_board.contracts import GeneralTaskCurrentManifestV1
    from src.work_board.general_task import digest
    from src.workflows.inference_accounting import _utc
    def retained(reference):
        if _staged_artifacts is None:
            return read_native_artifact_reference(reference, parent_job_id=parent.run_identity,
                creation_digest=manifest.creation_digest)
        for staged in _staged_artifacts:
            if staged.reference == reference:
                return recheck_staged_task_artifact(staged, parent_job_id=parent.run_identity,
                    creation_digest=manifest.creation_digest)[0]
        raise BoardError('general_task_manifest_binding_changed', 'Exact staged native reference required', status_code=409)
    if not isinstance(manifest, GeneralTaskCurrentManifestV1):
        raise BoardError("general_task_manifest_invalid", "Closed native manifest required", status_code=409)
    group = envelope.proposal_group
    if (manifest.task_id != task.task_id or manifest.attempt_id != attempt.attempt_id
        or manifest.run_id != parent.run_identity or manifest.original_root_id != task.owner_session_id
        or manifest.owner_principal_id != task.owner_principal_id
        or manifest.original_envelope_artifact_id != task.input_artifact_id
        or manifest.original_envelope_digest != task.typed_input_digest
        or manifest.original_input_digest != parent.input_digest
        or manifest.selected_grant_digest != selected_grant_digest(envelope)
        or manifest.group_id != group.group_id or manifest.group_digest != digest(group.model_dump(mode="json"))
        or manifest.original_limits_digest != group.limits_digest
        or manifest.original_deadline_at != group.original_deadline_at
        or manifest.native_deadline_at != _utc(parent.deadline_at)
        or manifest.native_deadline_at > group.original_deadline_at
        or manifest.creation_digest != compile_creation_digest(parent, task, attempt, envelope)
        or manifest.phase_digest != compile_phase_digest(manifest)
        or len(manifest.admitted_invocation_ids) > group.max_steps
        or len(manifest.step_ids) > group.max_steps
        or manifest.revision_artifact_ids[0] != task.input_artifact_id
        or manifest.revision_artifact_digests[0] != task.typed_input_digest
        or manifest.revision_artifact_schemas[0] != "GeneralTaskEnvelope.v1"):
        raise BoardError("general_task_manifest_binding_changed", "Original native manifest binding changed", status_code=409)
    if manifest.plan_revision == 1 and (manifest.current_plan_artifact_id != task.input_artifact_id
        or manifest.current_plan_digest != digest(envelope.plan.model_dump(mode="json"))):
        raise BoardError("general_task_manifest_plan_changed", "Original immutable plan changed", status_code=409)
    active_plan = envelope.plan
    for index in range(1, len(manifest.revision_numbers)):
        revision = retained(GeneralTaskArtifactRef(
            artifact_id=manifest.revision_artifact_ids[index], digest=manifest.revision_artifact_digests[index],
            schema_version=manifest.revision_artifact_schemas[index]))
        if (revision.plan.revision != manifest.revision_numbers[index]
            or revision.original_envelope_digest != manifest.original_envelope_digest
            or revision.selected_grant_digest != manifest.selected_grant_digest
            or revision.original_limits_digest != manifest.original_limits_digest
            or revision.original_deadline_at != manifest.original_deadline_at):
            raise BoardError("general_task_manifest_plan_changed", "Immutable native revision binding changed", status_code=409)
        active_plan = revision.plan
    if manifest.current_plan_digest != digest(active_plan.model_dump(mode="json")) or manifest.current_plan_artifact_id != manifest.revision_artifact_ids[-1]:
        raise BoardError("general_task_manifest_plan_changed", "Current immutable revision reference changed", status_code=409)
    for index, step_id in enumerate(manifest.step_ids):
        receipt = retained(GeneralTaskArtifactRef(
            artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
            schema_version=manifest.step_receipt_schemas[index]))
        if (receipt.step_id != step_id or receipt.task_id != task.task_id or receipt.attempt_id != attempt.attempt_id
            or receipt.invocation_id not in manifest.admitted_invocation_ids
            or receipt.selected_grant_digest != manifest.selected_grant_digest):
            raise BoardError("general_task_manifest_step_changed", "Immutable native receipt binding changed", status_code=409)
    return envelope


async def verify_readonly_native_projection(db, owner, parent, task, attempt, manifest):
    """Verify retained local facts without granting execution or model egress."""
    from src.workflows.general_task_guard import assert_original_parent_authority
    from src.workflows.job_runtime import DurableJobError
    try:
        assert_original_parent_authority(parent)
    except DurableJobError as exc:
        raise BoardError("general_task_manifest_binding_changed", "Original retained authority changed", status_code=409) from exc
    from src.db.models import WorkBoardInputArtifact
    from src.work_board.input_artifacts import (_metadata_digest, _payload_path,
        _safe_file_bytes, _decode_and_validate_payload)
    if (task.owner_principal_id != owner.principal_id or task.owner_session_id != owner.session_id
        or parent.owner_principal_id != task.owner_principal_id or parent.session_id != task.owner_session_id
        or parent.operator_session_id != task.owner_session_id or parent.goal_id != task.goal_id
        or parent.goal_revision != task.goal_revision or attempt.task_id != task.task_id
        or attempt.workflow_run_id != parent.run_identity):
        raise BoardError("general_task_projection_owner_changed", "Exact canonical local Task scope required", status_code=403)
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    if (artifact is None or artifact.owner_principal_id != task.owner_principal_id
        or artifact.owner_session_id != task.owner_session_id or artifact.goal_id != task.goal_id
        or artifact.goal_revision != task.goal_revision or artifact.capability_id != "agent.task.v1"
        or artifact.bound_task_id != task.task_id or artifact.payload_sha256 != task.typed_input_digest
        or artifact.typed_input_ref != task.typed_input_ref or artifact.metadata_digest != _metadata_digest(artifact)):
        raise BoardError("general_task_projection_source_changed", "Canonical immutable source binding required", status_code=409)
    content = _safe_file_bytes(_payload_path(artifact), expected_digest=artifact.payload_sha256,
        expected_size=artifact.size_bytes)
    envelope = GeneralTaskEnvelope.model_validate(_decode_and_validate_payload(artifact, content))
    if envelope.proposal_group is None:
        raise BoardError("general_task_provenance_missing", "Original retained source binding unavailable", status_code=409)
    return await _verify_native_manifest_data(parent, task, attempt, manifest, envelope)
