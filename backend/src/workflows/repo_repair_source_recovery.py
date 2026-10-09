"""Original Source recovery ownership; never an execution or replay grant.

Only originally registered v3 producers can enter the completion protocol.
Public request fields select an action and an optimistic revision, not proof.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from contextlib import asynccontextmanager, ExitStack
from dataclasses import dataclass, field
import weakref
from types import MappingProxyType

from src.work_board.contracts import WorkBoardOwner
from src.workflows.job_runtime import DurableJobLeaseError


_ACTIONS = frozenset({"reconcile_original_cleanup", "settle_original_host_boot_cleanup"})
_FENCES = weakref.WeakKeyDictionary()
_FENCE_SEAL = object()
_COMPLETIONS = weakref.WeakKeyDictionary()
_STOP_COMPLETIONS = weakref.WeakKeyDictionary()
_APPEND_STAGES = weakref.WeakKeyDictionary()
_KNOWNPOST_STAGES = weakref.WeakKeyDictionary()
_REGISTRATION_KEYS = frozenset({"schema", "job_id", "iteration_id", "iteration_index",
    "repository_attempt_id", "owner_principal_id", "owner_session_id", "root_fence",
    "root_authority_digest", "original_source_digest", "native_binding", "execution_digest",
    "prepared_digest", "proposal_digest", "approval_digest", "proposal_predecessor",
    "process_binding", "ready", "ready_digest", "directory_path", "admission_digest",
    "guard_path", "native_host_binding", "original_deadline_at", "execution_deadline_at",
    "monotonic_deadline", "producer_sources", "stage_identity", "fixed_admission_plan_digest",
    "source_artifact_digest", "executor_posture_digest"})
_COMPLETION_CAS_KEYS = frozenset({"before_revision", "post_revision", "iteration_id",
    "producer_registration_digest", "producer_completion_digest", "stop_digest",
    "unknown_projection_digest", "rows_digest"})


def read_registered_repository_producer(run, *, iteration_index):
    """Closed original SQL metadata only; this never grants physical authority.

    Startup may preserve the exact original lineage using this grammar. A
    completion owner must additionally stage actual registered storage/closure.
    """
    from datetime import datetime
    source = _source()
    original, work, _, _, binding, task_source = source.read_repository_original(run)
    if (source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3"
            or type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations):
        raise RepositorySourceRecoveryError("original_producer_registration_missing")
    identity = source.iteration_identity(run.run_identity, original["repository_attempt_id"],
        source._source_digest(original["original_input"]), iteration_index)
    record = source._repository_record(run, "repository:producer:" + identity)
    execution = source._repository_record(run, "repository:execution:" + identity)
    prepared = source._repository_record(run, "repository:prepared:" + identity)
    if (type(record) is not dict or set(record) != _REGISTRATION_KEYS
            or len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()) > 16 * 1024
            or execution is None or prepared is None):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    expected = {"schema": "repository.original_producer.v1", "job_id": run.run_identity,
        "iteration_id": identity, "iteration_index": iteration_index,
        "repository_attempt_id": original["repository_attempt_id"],
        "owner_principal_id": run.owner_principal_id, "owner_session_id": run.operator_session_id,
        "root_fence": run.fencing_token, "root_authority_digest": run.authority_digest,
        "original_source_digest": source._source_digest(original),
        "native_binding": binding.model_dump(mode="json"),
        "execution_digest": source._source_digest(execution),
        "prepared_digest": source._source_digest(prepared),
        "process_binding": execution["process_binding"],
        "original_deadline_at": original["original_deadline_at"],
        "source_artifact_digest": task_source.source_artifact_digest,
        "executor_posture_digest": prepared["executor_posture_digest"]}
    if any(record[key] != value or type(record[key]) is not type(value) for key, value in expected.items()):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    digests = ("root_authority_digest", "original_source_digest", "execution_digest", "prepared_digest",
        "proposal_digest", "approval_digest", "ready_digest", "admission_digest",
        "fixed_admission_plan_digest", "source_artifact_digest", "executor_posture_digest")
    if any(type(record[key]) is not str or not source._SHA.fullmatch(record[key]) for key in digests):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    ready = record["ready"]
    host = record["native_host_binding"]
    ready_keys = {"admission_digest", "public_key", "nonce", "pid", "start_identity", "boot_id",
        "directory_identity", "guard_identity"}
    host_keys = {"schema", "machine_digest", "boot_id", "pid_namespace", "workspace_path",
        "workspace_identity", "guard_path", "guard_identity", "mount_namespace", "proc_identity", "proc_mount_sha256"}
    def identity_pair(value):
        return type(value) is list and len(value) == 2 and all(type(item) is int and item >= 0 for item in value)
    if (type(ready) is not dict or set(ready) != ready_keys
            or type(host) is not dict or set(host) != host_keys
            or host["schema"] != "repository.original_producer_host.v1"
            or type(ready["pid"]) is not int or ready["pid"] <= 0
            or type(ready["start_identity"]) is not str or not ready["start_identity"].isdigit()
            or type(ready["boot_id"]) is not str or not ready["boot_id"] or len(ready["boot_id"]) > 128
            or type(ready["nonce"]) is not str or not source._SHA.fullmatch(ready["nonce"])
            or ready["admission_digest"] != record["admission_digest"]
            or source._source_digest(ready) != record["ready_digest"]
            or host["boot_id"] != ready["boot_id"] or host["guard_path"] != record["guard_path"]
            or host["guard_identity"] != ready["guard_identity"]
            or any(not identity_pair(value) for value in (ready["directory_identity"], ready["guard_identity"],
                record["stage_identity"], host["workspace_identity"], host["pid_namespace"], host["mount_namespace"], host["proc_identity"]))
            or any(type(host[key]) is not str or not source._SHA.fullmatch(host[key]) for key in ("machine_digest", "proc_mount_sha256"))
            or any(type(value) is not str or not value.startswith("/") or "\x00" in value
                for value in (record["directory_path"], record["guard_path"], host["workspace_path"]))
            or type(record["producer_sources"]) is not dict
            or set(record["producer_sources"]) != {"repo_original_producer.py", "repo_original_producer_finalizer.py",
                "repo_supervisor.py", "repo_sandbox.py", "repo_node.py", "repo_worker.py"}
            or any(type(value) is not str or not source._SHA.fullmatch(value) for value in record["producer_sources"].values())
            or type(record["monotonic_deadline"]) not in {int, float}
            or not math.isfinite(record["monotonic_deadline"]) or record["monotonic_deadline"] <= 0):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    predecessor = record["proposal_predecessor"]
    if (type(predecessor) is not dict or set(predecessor) != {"status", "revision", "last_receipt_id"}
            or predecessor["status"] != "execution_started" or type(predecessor["revision"]) is not int
            or predecessor["revision"] < 0 or predecessor["last_receipt_id"] is not None
                and (type(predecessor["last_receipt_id"]) is not str or len(predecessor["last_receipt_id"]) > 4096)):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    try:
        key = base64.b64decode(ready["public_key"], validate=True)
        cutoff = datetime.fromisoformat(record["execution_deadline_at"])
        original_cutoff = datetime.fromisoformat(record["original_deadline_at"])
        if len(key) != 32 or cutoff.tzinfo is None or original_cutoff.tzinfo is None or cutoff > original_cutoff:
            raise ValueError("invalid original registration")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepositorySourceRecoveryError("original_producer_registration_changed") from exc
    return json.loads(json.dumps(record))


def _source():
    from src.workflows import repo_repair_source
    return repo_repair_source


class RepositorySourceRecoveryError(DurableJobLeaseError):
    """A fixed public reason; private receipt contents never become messages."""

    def __init__(self, code, *, status_code=409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryRecoveryFence:
    service: object = field(repr=False)
    jobs: object = field(repr=False)
    job_id: str
    owner: WorkBoardOwner = field(repr=False)
    task: object = field(repr=False)
    lock: object = field(repr=False)
    seal: object = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _OriginalRepositoryProducerCompletionWitness:
    """Registered only after current Source and actual original bundle checks."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryCompletionAppendStage:
    """Actual active publication inputs; never a deserialized row grant."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryKnownPostCompletionStage:
    """One active signed-physical, literal-artifact verification scope."""


def assert_repository_knownpost_stage(stage, *, service, jobs, fence=None):
    import threading
    data = _KNOWNPOST_STAGES.get(stage) if type(stage) is _RepositoryKnownPostCompletionStage else None
    if (data is None or data["service"] is not service or data["jobs"] is not jobs
            or data["thread"] != threading.get_ident()
            or data["task"] is not asyncio.current_task()
            or fence is not None and data["fence"] is not fence):
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_required")
    from src.execution.repo_original_producer import assert_original_producer_completion_scope
    from src.workflows.job_runtime import _canonical
    assert_repository_recovery_fence(data["fence"], service=service, jobs=jobs,
        job_id=data["job_id"], owner=data["owner"])
    assert_original_producer_completion_scope(data["physical"])
    result = data["result"]
    if (_canonical(data["root"].model_dump(mode="json")) != data["root_json"]
            or result["status"] != data["result_status"]
            or _source()._source_digest(result["original_producer_completion"]) != data["completion_digest"]
            or result["manifest"] != result["original_producer_completion"]["manifest"]
            or result["readback"] != result["manifest"]
            or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}
                != data["output_digests"]):
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_changed")


def repository_knownpost_stage(stage):
    data = _KNOWNPOST_STAGES.get(stage) if type(stage) is _RepositoryKnownPostCompletionStage else None
    if data is None:
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_required")
    assert_repository_knownpost_stage(stage, service=data["service"], jobs=data["jobs"])
    values = {key: data[key] for key in ("root_json", "job_id", "owner", "fence", "stop_digest",
        "root_key", "current_static_digest", "result", "context_rows")}
    for key in ("cas", "registration", "unknown_projection", "cleanup_envelope"):
        values[key] = json.loads(data[key])
    return MappingProxyType(values)


def _original_repository_cleanup_projection(registration, result):
    """Exact existing native bytes; deriving a mapping issues no authority."""
    from src.workflows.job_runtime import _canonical
    binding, manifest = registration["process_binding"], result["manifest"]
    if _canonical(manifest["iteration_binding"]) != _canonical(binding):
        raise RepositorySourceRecoveryError("original_repository_knownpost_physical_changed")
    return {"schema": "RepoWorkIterationCleanup.v1",
        "job_id": binding["repository_job_id"], "attempt_id": binding["repository_attempt_id"],
        "fencing_token": binding["repository_fence"], "iteration_binding": binding,
        "process_cleanup": manifest["process_cleanup"], "supervisor_identity": manifest["supervisor_identity"],
        "supervisor_transport": manifest["supervisor_transport"], "stage_removed": manifest["stage_removed"],
        "status": "iteration_failed_quiescent" if result["status"] == "failed" else result["status"],
        "artifact_digests": {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}}


def _knownpost_original_payloads(run, registration, result, envelope):
    """Expected literal payload bytes only; this function issues no witness."""
    from src.workflows.job_runtime import _canonical
    source = _source()
    identity = registration["iteration_id"]
    execution = source._repository_record(run, "repository:execution:" + identity)
    _, work, *_ = source.read_repository_original(run)
    outputs, manifest = result["outputs"], result["manifest"]
    complete = result["original_producer_completion"]["outcome"] in {
        "completed_requested_checks", "completed_requested_check_failure"}
    status = result["status"] if complete else "held_partial"
    prefix = "workspace-json:artifacts/repo-repair/model/iteration-" + identity
    diagnostics = {"iteration_id": identity,
        "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
        "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
        "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
        "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
        "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
        "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
    return ({"artifact_ref": prefix + "-cleanup.json",
        "artifact_digest": hashlib.sha256(_canonical(envelope).encode()).hexdigest(),
        "cleanup_proven": True, "iteration_id": identity, "status": status,
        "source_completion_cas": envelope["source_completion_cas"]},
        {"artifact_ref": prefix + "-readback.json",
        "artifact_digest": hashlib.sha256(outputs["readback.json"]).hexdigest(), "status": status,
        "manifest_digest": source._source_digest(manifest), "patch_sha256": execution["patch_sha256"],
        "diagnostics_artifact_ref": prefix + "-diagnostics.json",
        "diagnostics_artifact_digest": hashlib.sha256(_canonical(diagnostics).encode()).hexdigest(),
        "command_results": source._repository_command_results(manifest,
            node=work.language_profile == "test_node") if complete else [],
        "source_completion_cas": envelope["source_completion_cas"]})


def _verify_repository_knownpost_root(run, registration, result, envelope):
    """Hash-only prior Unknown check; a returned mapping is never authority."""
    from src.workflows.job_runtime import _canonical, _digest
    from src.work_board.contracts import TaskProposalGroupV1
    source = _source()
    if (run.status != "unknown_external_effect"
            or source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3"
            or type(envelope) is not dict or set(envelope) != {
                "physical_projection", "source_completion_cas", "source_append_metadata"}):
        raise RepositorySourceRecoveryError("original_repository_knownpost_changed")
    cas = envelope["source_completion_cas"]
    stop = source._repository_record(run, "repository:stop-intent:v1")
    successor = source._repository_record(run, "repository:stop-uncertainty-successor:v1")
    if (type(cas) is not dict or set(cas) != _COMPLETION_CAS_KEYS
            or type(cas["before_revision"]) is not int or cas["before_revision"] < 0
            or type(cas["post_revision"]) is not int or cas["post_revision"] != cas["before_revision"] + 1
            or type(run.revision) is not int or run.revision != cas["post_revision"]
            or cas["iteration_id"] != registration["iteration_id"]
            or cas["producer_registration_digest"] != source._source_digest(registration)
            or cas["producer_completion_digest"] != source._source_digest(result["original_producer_completion"])
            or stop is None or cas["stop_digest"] != source._source_digest(stop)
            or any(type(cas[key]) is not str or not source._SHA.fullmatch(cas[key])
                for key in ("unknown_projection_digest", "rows_digest"))):
        raise RepositorySourceRecoveryError("original_repository_knownpost_changed")
    identity = registration["iteration_id"]
    metadata = envelope["source_append_metadata"]
    _assert_source_completion_append_metadata(run, identity, metadata)
    history = json.loads(run.checkpoint_receipts_json)
    expected_payloads = _knownpost_original_payloads(run, registration, result, envelope)
    for ordinal, (role, item, payload) in enumerate(zip(("cleanup", "readback"), metadata, expected_payloads)):
        try:
            valid_time = TaskProposalGroupV1.utc_timestamp(item["created_at"]).isoformat() == item["created_at"]
        except (ValueError, TypeError, KeyError):
            valid_time = False
        expected = {**item, "payload": payload, "state_digest": _digest(payload)}
        if (not valid_time or item["checkpoint_id"] != "repository:" + role + ":" + identity
                or len(history) < 2 or history[-2 + ordinal] != expected
                or len(_canonical(payload).encode()) > 16384):
            raise RepositorySourceRecoveryError("original_repository_knownpost_suffix_changed")
    manifest, outputs = result["manifest"], result["outputs"]
    projection = envelope["physical_projection"]
    if (type(projection) is not dict or _canonical(projection) != _canonical(_original_repository_cleanup_projection(registration, result))
            or _canonical(manifest.get("iteration_binding")) != _canonical(registration["process_binding"])
            or projection.get("process_cleanup") != manifest["process_cleanup"]
            or projection.get("artifact_digests") != {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()}
            or manifest.get("stage_removed") is not True
            or manifest.get("process_cleanup", {}).get("cleanup_proven") is not True
            or manifest.get("process_cleanup", {}).get("oracle") != "linux_subreaper_waitpid_echild"
            or any(manifest.get("supervisor_transport", {}).get(key) is not True for key in (
                "command_output_drained", "command_descriptors_closed", "original_children_waited", "no_spawn"))
            or manifest.get("supervisor_transport", {}).get("transport_kind") != "original_producer_durable_v1"):
        raise RepositorySourceRecoveryError("original_repository_knownpost_physical_changed")
    current = run.model_dump(mode="json")
    candidate = dict(current)
    candidate["revision"] = cas["before_revision"]
    candidate["checkpoint_receipts_json"] = _canonical(history[:-2])
    root_key = type(run).__tablename__ + ":" + str(run.id)
    successor_keys = {"schema", "job_id", "stop_digest", "root_key", "predecessor_digest",
        "successor_digest", "authority_digest", "fencing_token", "from_revision", "to_revision",
        "predecessor_projection", "successor_projection"}
    if (type(successor) is not dict or set(successor) != successor_keys
            or successor["schema"] != "repository.stop_uncertainty_successor.v1"
            or successor["job_id"] != run.run_identity or successor["root_key"] != root_key
            or successor["stop_digest"] != cas["stop_digest"]
            or successor["authority_digest"] != run.authority_digest
            or type(successor["fencing_token"]) is not int or successor["fencing_token"] != run.fencing_token
            or type(successor["from_revision"]) is not int or successor["from_revision"] < 0
            or type(successor["to_revision"]) is not int
            or successor["to_revision"] != successor["from_revision"] + 1
            or candidate["revision"] != successor["to_revision"]):
        raise RepositorySourceRecoveryError("original_repository_knownpost_projection_changed")
    predecessor, following = successor["predecessor_projection"], successor["successor_projection"]
    source._checked_uncertainty_columns(predecessor)
    source._checked_uncertainty_columns(following)
    reasons = {"repository_callback_closure_unproven": "reconcile_original_callback",
        "repository_process_closure_unproven": "reconcile_original_process"}
    if (predecessor["status"] != "running" or type(predecessor["lease_owner"]) is not str
            or not predecessor["lease_owner"] or predecessor["lease_expires_at"] is None
            or following["status"] != "unknown_external_effect" or following["failure_reason"] not in reasons
            or following["finished_at"] is not None or following["lease_owner"] is not None
            or following["lease_expires_at"] is not None or following["result_summary"] != "result recorded"
            or {key: candidate[key] for key in source._UNCERTAINTY_COLUMNS} != following):
        raise RepositorySourceRecoveryError("original_repository_knownpost_projection_changed")
    original, work, *_ = source.read_repository_original(run)
    matching = [index for index in range(1, work.limits.max_iterations + 1)
        if following["result_digest"] == _digest({"no_learning": True,
            "operator_action": reasons[following["failure_reason"]],
            "iteration_id": source.iteration_identity(run.run_identity, original["repository_attempt_id"],
                source._source_digest(original["original_input"]), index)})]
    predecessor_candidate = {**candidate, **predecessor}
    predecessor_digest = source._source_digest({key: value for key, value in predecessor_candidate.items()
        if key not in source._ROOT_BOOKKEEPING})
    current_digest = source._source_digest({key: value for key, value in candidate.items()
        if key not in source._ROOT_BOOKKEEPING})
    if (len(matching) != 1 or predecessor_digest != successor["predecessor_digest"]
            or type(stop.get("static_rows")) is not dict
            or predecessor_digest != stop["static_rows"].get(root_key)
            or current_digest != successor["successor_digest"]):
        raise RepositorySourceRecoveryError("original_repository_knownpost_projection_changed")
    proof = {"root_json": _canonical(candidate), "successor_json": _canonical(successor),
        "stop_digest": cas["stop_digest"], "predecessor_digest": predecessor_digest,
        "current_digest": current_digest, "revision": candidate["revision"], "fencing_token": run.fencing_token}
    if source._source_digest(proof) != cas["unknown_projection_digest"]:
        raise RepositorySourceRecoveryError("original_repository_knownpost_root_changed")
    return {"root_key": root_key, "current_static_digest": current_digest, "unknown_projection": proof}


@asynccontextmanager
async def _stage_repository_knownpost_identity(service, jobs, *, root, owner, fence, physical,
        registration, result, envelope, proof):
    """Only the original scoped verifier can register a known-post stage."""
    import threading
    from src.workflows.job_runtime import _canonical
    from src.execution.repo_original_producer import original_producer_completion_result
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=root.run_identity, owner=owner)
    if original_producer_completion_result(physical) is not result:
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_changed")
    async with jobs._session() as db:
        actual = await jobs._fetch(db, root.run_identity)
        if (_canonical(actual.model_dump(mode="json")) != _canonical(root.model_dump(mode="json"))
                or read_registered_repository_producer(actual,
                    iteration_index=registration["iteration_index"]) != registration
                or (actual.owner_principal_id, actual.operator_session_id) != (owner.principal_id, owner.session_id)):
            raise RepositorySourceRecoveryError("original_repository_knownpost_stage_changed")
    root = actual
    literal = _read_original_cleanup_envelope_if_present(service,
        "artifacts/repo-repair/model/iteration-" + registration["iteration_id"] + "-cleanup.json")
    if literal != envelope:
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_changed")
    source = _source()
    stop = source._repository_record(root, "repository:stop-intent:v1")
    original, *_ = source.read_repository_original(root)
    snapshot = json.loads(service._read_private_artifact(stop["snapshot_artifact_ref"],
        expected_digest=stop["snapshot_artifact_digest"]))
    if snapshot != {"schema": "repository.stop_snapshot.v1", "static_rows": stop["static_rows"],
            "repository_job_id": root.run_identity, "source_checkpoint_digest": source._source_digest(original)}:
        raise RepositorySourceRecoveryError("original_repository_knownpost_stop_changed")
    # Recompute from the actual row/result/literal envelope at issuance; a
    # caller-provided proof dictionary cannot register its own interpretation.
    if _verify_repository_knownpost_root(root, registration, result, envelope) != proof:
        raise RepositorySourceRecoveryError("original_repository_knownpost_stage_changed")
    stage = _RepositoryKnownPostCompletionStage()
    data = {"service": service, "jobs": jobs, "owner": owner, "fence": fence,
        "task": asyncio.current_task(), "thread": threading.get_ident(), "physical": physical,
        "root": root, "root_json": _canonical(root.model_dump(mode="json")), "job_id": root.run_identity,
        "root_key": proof["root_key"], "current_static_digest": proof["current_static_digest"],
        "stop_digest": envelope["source_completion_cas"]["stop_digest"], "context_rows": None,
        "result": result, "result_status": result["status"],
        "completion_digest": _source()._source_digest(result["original_producer_completion"]),
        "output_digests": {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()},
        "cas": _canonical(envelope["source_completion_cas"]), "registration": _canonical(registration),
        "unknown_projection": _canonical(proof["unknown_projection"]), "cleanup_envelope": _canonical(envelope)}
    _KNOWNPOST_STAGES[stage] = data
    try:
        assert_repository_knownpost_stage(stage, service=service, jobs=jobs, fence=fence)
        yield stage
    finally:
        _KNOWNPOST_STAGES.pop(stage, None)


def assert_repository_completion_append_stage(stage, *, service=None, jobs=None, fence=None):
    data = _APPEND_STAGES.get(stage) if type(stage) is _RepositoryCompletionAppendStage else None
    if (data is None or data["task"] is not asyncio.current_task()
            or service is not None and data["service"] is not service
            or jobs is not None and data["jobs"] is not jobs
            or fence is not None and data["fence"] is not fence):
        raise RepositorySourceRecoveryError("original_repository_append_stage_required")
    from src.execution.repo_original_producer import assert_original_producer_completion_scope
    from src.workflows.job_runtime import _canonical
    assert_repository_recovery_fence(data["fence"], service=data["service"], jobs=data["jobs"],
        job_id=data["job_id"], owner=data["owner"])
    assert_original_producer_completion_scope(data["physical"])
    result = data["result"]
    if (_canonical(data["root"].model_dump(mode="json")) != data["root_json"]
            or _source()._source_digest(data["work"].model_dump(mode="json")) != data["work_digest"]
            or result["status"] != data["result_status"]
            or _source()._source_digest(result["original_producer_completion"]) != data["body_digest"]
            or result["manifest"] != result["original_producer_completion"]["manifest"]
            or result["readback"] != result["manifest"]
            or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()} != data["output_digests"]):
        raise RepositorySourceRecoveryError("original_repository_append_stage_changed")


def repository_completion_append_stage(stage):
    """Protected Source inputs; the stage identity supplies all authority."""
    assert_repository_completion_append_stage(stage)
    data = _APPEND_STAGES[stage]
    values = {key: data[key] for key in ("root", "root_json", "owner", "work", "job_id", "iteration_id",
        "iteration_index", "before_revision", "journal_prefix", "inventory", "result", "status",
        "context_rows", "accounting_rows", "proposal_json", "approval_json")}
    for key in ("registration", "execution", "cas", "physical_projection", "retry_metadata"):
        values[key] = json.loads(data[key]) if data[key] is not None else None
    return MappingProxyType(values)


@asynccontextmanager
async def _stage_repository_completion_append_publication(service, jobs, *, context, owner, fence,
        physical, registration, execution, cas, result, physical_projection, status,
        proposal_json, approval_json, accounting_rows, retry_metadata=None):
    """Issue only from this original publisher's freshly rechecked epoch."""
    from src.workflows.job_runtime import _canonical
    from src.workflows import repo_repair_stop as stop
    from src.execution.repo_original_producer import original_producer_completion_result
    source = _source()
    run = context["run"]
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=run.run_identity, owner=owner)
    if original_producer_completion_result(physical) is not result:
        raise RepositorySourceRecoveryError("original_repository_append_stage_changed")
    # A caller dictionary is not provenance: obtain the protected current
    # context again through the actual Source reader before issuing identity.
    if source._repository_record(run, stop.STOP_ID) is not None:
        fresh = (await stop._context(service, jobs, job_id=run.run_identity, owner=owner)).data
    else:
        fresh = await source._repository_precontact(service, jobs, job_id=run.run_identity, owner=owner)
    if (fresh["rows"] != context["rows"]
            or _canonical(fresh["run"].model_dump(mode="json")) != _canonical(run.model_dump(mode="json"))
            or read_registered_repository_producer(fresh["run"], iteration_index=registration["iteration_index"]) != registration):
        raise RepositorySourceRecoveryError("original_repository_append_stage_epoch_changed")
    stage = _RepositoryCompletionAppendStage()
    data = {"service": service, "jobs": jobs, "owner": owner, "fence": fence,
        "task": asyncio.current_task(), "physical": physical, "root": run,
        "root_json": _canonical(run.model_dump(mode="json")), "work": context["work"],
        "work_digest": source._source_digest(context["work"].model_dump(mode="json")),
        "job_id": run.run_identity, "iteration_id": registration["iteration_id"],
        "iteration_index": registration["iteration_index"], "before_revision": run.revision,
        "journal_prefix": run.checkpoint_receipts_json,
        "inventory": tuple(source.repository_checkpoint_inventory(run, context["work"])),
        "context_rows": tuple(context["rows"]), "accounting_rows": tuple(sorted(accounting_rows.items())),
        "proposal_json": _canonical(proposal_json), "approval_json": _canonical(approval_json),
        "result": result, "result_status": result["status"], "status": status,
        "body_digest": source._source_digest(result["original_producer_completion"]),
        "output_digests": {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}}
    for key, value in (("registration", registration), ("execution", execution), ("cas", cas),
            ("physical_projection", physical_projection), ("retry_metadata", retry_metadata)):
        data[key] = _canonical(value) if value is not None else None
    _APPEND_STAGES[stage] = data
    try:
        assert_repository_completion_append_stage(stage, service=service, jobs=jobs, fence=fence)
        yield stage
    finally:
        _APPEND_STAGES.pop(stage, None)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _OriginalRepositoryStopCompletionWitness:
    """Cleanup-only authority confined to an active original physical scope."""


def assert_repository_original_stop_completion(witness, *, service, jobs,
                                               context=None, fence=None):
    from src.workflows import repo_repair_stop as stop
    data = _STOP_COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryStopCompletionWitness else None
    if (data is None or data["service"] is not service or data["jobs"] is not jobs
            or context is not None and data["context"] is not context
            or fence is not None and data["fence"] is not fence
            or data["task"] is not asyncio.current_task()):
        raise RepositorySourceRecoveryError("original_repository_stop_completion_required")
    from src.execution.repo_original_producer import assert_original_producer_completion_scope
    stop.assert_repository_stop_context(data["context"], service=service, jobs=jobs)
    assert_repository_recovery_fence(data["fence"], service=service, jobs=jobs,
        job_id=data["job_id"], owner=data["owner"])
    for identity, physical in data["physical"].items():
        try:
            assert_original_producer_completion_scope(physical)
        except (ValueError, OSError) as exc:
            raise RepositorySourceRecoveryError("original_repository_stop_completion_pending") from exc
        result = data["results"][identity]
        if (_source()._source_digest(result["original_producer_completion"]) != data["digests"][identity]
                or result["status"] != data["bindings"][identity]["status"]
                or _source()._source_digest(result["manifest"]) != data["bindings"][identity]["manifest"]
                or _source()._source_digest(result["readback"]) != data["bindings"][identity]["readback"]
                or _source()._source_digest(result.get("cleanup")) != data["bindings"][identity]["cleanup"]
                or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}
                    != data["bindings"][identity]["outputs"]):
            raise RepositorySourceRecoveryError("original_repository_stop_completion_changed")


def repository_original_stop_completion_result(witness, *, iteration_id):
    data = _STOP_COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryStopCompletionWitness else None
    if data is None:
        raise RepositorySourceRecoveryError("original_repository_stop_completion_required")
    assert_repository_original_stop_completion(witness, service=data["service"], jobs=data["jobs"])
    if iteration_id not in data["results"]:
        raise RepositorySourceRecoveryError("original_repository_stop_completion_iteration_changed")
    return data["results"][iteration_id]


def repository_original_stop_completion_cleanup_envelope(witness, *, iteration_id):
    data = _STOP_COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryStopCompletionWitness else None
    if data is None:
        raise RepositorySourceRecoveryError("original_repository_stop_completion_required")
    assert_repository_original_stop_completion(witness, service=data["service"], jobs=data["jobs"])
    if iteration_id not in data["cleanup_envelopes"]:
        raise RepositorySourceRecoveryError("original_repository_stop_completion_iteration_changed")
    return json.loads(data["cleanup_envelopes"][iteration_id])


@asynccontextmanager
async def stage_repository_original_stop_completion(service, jobs, *, context, owner, fence):
    """Already-committed full Running v3 cleanup under one active guard.

    This issues no publication, dispatch, accounting settlement or Unknown
    successor authority. The terminal Stop writer retains its own exact CAS.
    """
    from src.workflows import repo_repair_stop as stop
    from src.workflows.job_runtime import _canonical
    from src.execution.repo_original_producer import (original_producer_live_owner,
        stage_original_producer_completion, stage_original_producer_related_completion,
        original_producer_completion_result)
    stop.assert_repository_stop_context(context, service=service, jobs=jobs)
    source = _source()
    run, work = context["run"], context["work"]
    assert_repository_recovery_fence(fence, service=service, jobs=jobs,
        job_id=run.run_identity, owner=owner)
    if (run.status != "running" or source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3"
            or source._repository_record(run, "repository:stop-uncertainty-successor:v1") is not None
            or source._repository_record(run, stop.STOP_ID) is None
            or (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id)):
        raise RepositorySourceRecoveryError("original_repository_stop_completion_unavailable")
    entries = []
    for index in range(1, work.limits.max_iterations + 1):
        identity = source.iteration_identity(run.run_identity, context["original"]["repository_attempt_id"],
            source._source_digest(context["original"]["original_input"]), index)
        execution = source._repository_record(run, "repository:execution:" + identity)
        if execution is None:
            continue
        registration = read_registered_repository_producer(run, iteration_index=index)
        cleanup = source._repository_record(run, "repository:cleanup:" + identity)
        readback = source._repository_record(run, "repository:readback:" + identity)
        if (cleanup is None or readback is None or cleanup.get("cleanup_proven") is not True
                or cleanup.get("status") not in {"succeeded", "failed"}
                or readback.get("status") != cleanup["status"]
                or cleanup.get("source_completion_cas") != readback.get("source_completion_cas")):
            raise RepositorySourceRecoveryError("original_repository_stop_completion_pending")
        entries.append((identity, registration, cleanup, readback))
    if not entries or len(entries) > 3:
        raise RepositorySourceRecoveryError("original_repository_stop_completion_pending")
    async with jobs._session() as db:
        for model, key, expected in context["rows"]:
            row = await db.get(model, key, populate_existing=True)
            if row is None or _canonical(row.model_dump(mode="json")) != expected:
                raise RepositorySourceRecoveryError("original_repository_stop_completion_epoch_changed")
    latest = entries[-1]
    callback = service._iterative_process_callbacks.get(latest[0])
    live_result = None
    live_owner = None
    if callback is not None:
        if not callback.done() or callback.cancelled() or callback.exception() is not None:
            raise RepositorySourceRecoveryError("original_repository_stop_completion_pending")
        live_result = callback.result()
        try:
            live_owner = original_producer_live_owner(service, jobs, live_result)
        except (ValueError, OSError) as exc:
            raise RepositorySourceRecoveryError("original_repository_stop_completion_pending") from exc
    witness = None
    with ExitStack() as stages:
        def enter_physical(manager):
            try:
                return stages.enter_context(manager)
            except (ValueError, OSError) as exc:
                raise RepositorySourceRecoveryError("original_repository_stop_completion_pending") from exc
        primary = enter_physical(stage_original_producer_completion(latest[1],
            owner=live_owner, result=live_result))
        physical = {latest[0]: primary}
        for identity, registration, _, _ in entries[:-1]:
            try:
                physical[identity] = stage_original_producer_related_completion(primary, registration)
            except (ValueError, OSError) as exc:
                raise RepositorySourceRecoveryError("original_repository_stop_completion_pending") from exc
        results, digests, bindings, cleanup_envelopes = {}, {}, {}, {}
        for identity, registration, cleanup, readback in entries:
            try:
                result = original_producer_completion_result(physical[identity])
            except (ValueError, OSError) as exc:
                raise RepositorySourceRecoveryError("original_repository_stop_completion_pending") from exc
            body, manifest, outputs = result["original_producer_completion"], result["manifest"], result["outputs"]
            cas = cleanup["source_completion_cas"]
            if (type(cas) is not dict or set(cas) != {"before_revision", "post_revision", "iteration_id",
                    "producer_registration_digest", "producer_completion_digest", "stop_digest",
                    "unknown_projection_digest", "rows_digest"}
                    or type(cas["before_revision"]) is not int or cas["before_revision"] < 0
                    or type(cas["post_revision"]) is not int or cas["post_revision"] != cas["before_revision"] + 1
                    or cas["post_revision"] > run.revision or cas["iteration_id"] != identity
                    or cas["producer_registration_digest"] != source._source_digest(registration)
                    or cas["producer_completion_digest"] != source._source_digest(body)
                    or cas["unknown_projection_digest"] is not None
                    or cas["stop_digest"] is not None and cas["stop_digest"] != source._source_digest(source._repository_record(run, stop.STOP_ID))
                    or type(cas["rows_digest"]) is not str or not source._SHA.fullmatch(cas["rows_digest"])):
                raise RepositorySourceRecoveryError("original_repository_stop_completion_changed")
            cleanup_body = json.loads(service._read_private_artifact(cleanup["artifact_ref"], expected_digest=cleanup["artifact_digest"]))
            manifest_raw = service._read_private_artifact(readback["artifact_ref"], expected_digest=readback["artifact_digest"])
            cleanup_envelopes[identity] = _canonical(cleanup_body)
            if "physical_projection" in cleanup_body:
                if (set(cleanup_body) != {"physical_projection", "source_completion_cas", "source_append_metadata"}
                        or cleanup_body["source_completion_cas"] != cas):
                    raise RepositorySourceRecoveryError("original_repository_stop_completion_changed")
                _assert_source_completion_append_metadata(run, identity, cleanup_body["source_append_metadata"])
                cleanup_body = cleanup_body["physical_projection"]
            if (body["outcome"] not in {"completed_requested_checks", "completed_requested_check_failure"}
                    or result["status"] != cleanup["status"] or manifest_raw != outputs["readback.json"]
                    or json.loads(manifest_raw) != manifest or source._source_digest(manifest) != readback["manifest_digest"]
                    or _canonical(cleanup_body) != _canonical(_original_repository_cleanup_projection(registration, result))
                    or cleanup_body.get("iteration_binding") != registration["process_binding"]
                    or cleanup_body.get("process_cleanup") != manifest["process_cleanup"]
                    or cleanup_body.get("artifact_digests") != {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()}
                    or manifest.get("stage_removed") is not True):
                raise RepositorySourceRecoveryError("original_repository_stop_completion_changed")
            if ("source_completion_cas" in cleanup_body
                    and cleanup_body["source_completion_cas"] != cleanup["source_completion_cas"]):
                raise RepositorySourceRecoveryError("original_repository_stop_completion_changed")
            results[identity], digests[identity] = result, source._source_digest(body)
            bindings[identity] = {"status": result["status"], "manifest": source._source_digest(manifest),
                "readback": source._source_digest(result["readback"]), "cleanup": source._source_digest(result.get("cleanup")),
                "outputs": {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()}}
        witness = _OriginalRepositoryStopCompletionWitness()
        _STOP_COMPLETIONS[witness] = {"service": service, "jobs": jobs, "context": context,
            "owner": owner, "fence": fence, "task": asyncio.current_task(), "job_id": run.run_identity,
            "physical": physical, "results": results, "digests": digests, "bindings": bindings,
            "cleanup_envelopes": cleanup_envelopes}
        try:
            assert_repository_original_stop_completion(witness, service=service, jobs=jobs, context=context, fence=fence)
            yield witness
        finally:
            _STOP_COMPLETIONS.pop(witness, None)


def assert_repository_completion_witness(witness, *, service=None, jobs=None):
    data = _COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryProducerCompletionWitness else None
    if (data is None or service is not None and data["service"] is not service
            or jobs is not None and data["jobs"] is not jobs):
        raise RepositorySourceRecoveryError("original_repository_completion_witness_required")
    if data.get("knownpost_stage") is not None:
        assert_repository_knownpost_stage(data["knownpost_stage"], service=data["service"], jobs=data["jobs"])
    if data.get("scoped_publication") is not None:
        assert_repository_scoped_completion(witness, service=data["service"], jobs=data["jobs"],
            job_id=data["job_id"], owner=data["owner"])
    result = data["result"]
    if (_source()._source_digest(result["original_producer_completion"]) != data["completion_digest"]
            or result["status"] != data["result_status"]
            or result["manifest"] != result["original_producer_completion"]["manifest"]
            or result["readback"] != result["manifest"]
            or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}
                != data["output_digests"]):
        raise RepositorySourceRecoveryError("original_repository_completion_witness_changed")


def assert_repository_scoped_completion(witness, *, service, jobs, job_id, owner, fence=None):
    """Current private publication lifetime only; safe inside an SQL writer."""
    import threading
    from src.execution.repo_original_producer import assert_original_producer_completion_scope
    data = _COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryProducerCompletionWitness else None
    if (data is None or data.get("scoped_publication") is not _FENCE_SEAL
            or data["service"] is not service or data["jobs"] is not jobs
            or data["job_id"] != job_id or data["owner"] is not owner
            or fence is not None and data["fence"] is not fence
            or data["task"] is not asyncio.current_task() or data["thread"] != threading.get_ident()):
        raise RepositorySourceRecoveryError("original_repository_scoped_completion_required")
    assert_repository_recovery_fence(data["fence"], service=service, jobs=jobs, job_id=job_id, owner=owner)
    try:
        assert_original_producer_completion_scope(data["physical"])
    except ValueError as exc:
        raise RepositorySourceRecoveryError("original_repository_scoped_completion_required") from exc
    from src.workflows.job_runtime import _canonical
    result = data["result"]
    if (result["status"] != data["result_status"]
            or _canonical(result["manifest"]) != _canonical(result["original_producer_completion"]["manifest"])
            or _canonical(result["readback"]) != _canonical(result["manifest"])
            or _source()._source_digest(result["original_producer_completion"]) != data["completion_digest"]
            or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}
                != data["output_digests"]):
        raise RepositorySourceRecoveryError("original_repository_completion_witness_changed")


def repository_scoped_completion_fence(witness):
    assert_repository_completion_witness(witness)
    data = _COMPLETIONS[witness]
    assert_repository_scoped_completion(witness, service=data["service"], jobs=data["jobs"],
        job_id=data["job_id"], owner=data["owner"])
    return data["fence"]


def repository_scoped_completion_projection(witness):
    """Verified eleven-field projection DATA, never a reconstructed witness."""
    repository_scoped_completion_fence(witness)
    return json.loads(_COMPLETIONS[witness]["physical_projection_json"])


def repository_completion_scoped_binding(witness):
    repository_scoped_completion_fence(witness)
    data = _COMPLETIONS[witness]
    return MappingProxyType({"owner": data["owner"], "fence": data["fence"],
        "job_id": data["job_id"], "iteration_id": data["post_cas"]["iteration_id"],
        "context": data["context"], "committed_rows": data["committed_rows"],
        "registration": json.loads(data["registration_json"])})


def repository_completion_physical_projection(witness):
    return repository_scoped_completion_projection(witness)


def repository_completion_finalizer_state(witness):
    """Private Source-owned phase data in the existing active entry only.

    The closed Source reader/writers derive and check this state from actual
    SQL and original finite deltas. This accessor accepts no row/state DTO and
    the returned data cannot independently authorize a write.
    """
    repository_scoped_completion_fence(witness)
    return _COMPLETIONS[witness]["finalizer_state"]


async def repository_completion_recovered_wait(witness):
    """Create and retain the actual original wait before exposing its identity."""
    repository_scoped_completion_fence(witness)
    data = _COMPLETIONS[witness]
    if data["wait_issued"]:
        raise RepositorySourceRecoveryError("original_repository_completion_phase_changed")
    data["wait_issued"] = True
    registration = json.loads(data["registration_json"])
    wait = await _source()._recover_repository_wait_witness_held(data["service"], data["jobs"],
        job_id=data["job_id"], owner=data["owner"], iteration_index=registration["iteration_index"],
        _resumed=True, _completion_witness=witness)
    repository_scoped_completion_fence(witness)
    from src.workflows.general_task_guard import _assert_repository_child_wait_witness_shape
    _assert_repository_child_wait_witness_shape(wait)
    if (wait.repository_job_id != data["job_id"]
            or wait.iteration_id != registration["iteration_id"]
            or wait.iteration_index != registration["iteration_index"]
            or wait.repository_attempt_id != registration["repository_attempt_id"]
            or wait.repository_fence != registration["root_fence"]
            or wait.native_binding != data["context"]["binding"]):
        raise RepositorySourceRecoveryError("original_repository_final_source_changed")
    data["wait"] = wait
    return wait


async def repository_completion_recovered_final_source(witness):
    """Read and construct the actual final Source; prebuilt objects are absent."""
    repository_scoped_completion_fence(witness)
    data = _COMPLETIONS[witness]
    if data["wait"] is None or data["final_source_issued"]:
        raise RepositorySourceRecoveryError("original_repository_completion_phase_changed")
    data["final_source_issued"] = True
    source = _source()
    async with data["jobs"]._session() as db:
        canonical = await source.stage_repository_canonical_source(data["service"], db,
            repository_job_id=data["job_id"], native_invocation_id=data["context"]["binding"].invocation_id,
            consent_id=data["wait"]._source_binding.consent_id)
    repository_scoped_completion_fence(witness)
    source.assert_repository_canonical_source(canonical)
    from dataclasses import replace
    canonical = replace(canonical, _seal=witness, _recovered_completion=witness)
    data["final_source"] = canonical
    # Registration precedes assertion, whose recovered-seal path checks this
    # exact identity. A copy can neither occupy first binding nor fall back.
    source.assert_repository_canonical_source(canonical)
    return canonical


async def repository_completion_recovered_child_final(witness, *, evidence):
    """Derive actual evidence, then construct and register the final identity."""
    repository_scoped_completion_fence(witness)
    data = _COMPLETIONS[witness]
    if data["wait"] is None or data["final_source"] is None or data["final_witness_issued"]:
        raise RepositorySourceRecoveryError("original_repository_completion_phase_changed")
    from src.workflows.job_runtime import _canonical
    required = {"final_patch_digest", "final_manifest_digest", "final_readback_digest",
        "final_command_receipt_digest", "final_cleanup_digest", "final_accounting_digest",
        "final_artifact_id", "final_artifact_digest", "requested_check_exits_digest", "all_iteration_ids_digest"}
    if type(evidence) is not dict or set(evidence) != required:
        raise RepositorySourceRecoveryError("original_repository_final_evidence_changed")
    evidence_json = _canonical(evidence)
    data["final_witness_issued"] = True
    source = _source()
    derived = await source._validate_recovered_repository_final_evidence(data["service"], data["jobs"],
        completion_witness=witness, evidence=json.loads(evidence_json))
    repository_scoped_completion_fence(witness)
    if type(derived) is not dict or set(derived) != required or _canonical(derived) != evidence_json:
        raise RepositorySourceRecoveryError("original_repository_final_evidence_changed")
    source.assert_repository_canonical_source(data["final_source"])
    from dataclasses import replace
    final_source = replace(data["wait"]._source_binding, _final_source=data["final_source"],
        _final_evidence_json=_canonical(derived))
    from src.workflows.general_task_guard import issue_repository_child_final_witness
    final_witness = issue_repository_child_final_witness(wait_witness=data["wait"],
        source_binding=final_source, **derived)
    if (final_witness._source_binding._final_source is not data["final_source"]
            or final_witness.wait_witness is not data["wait"]):
        raise RepositorySourceRecoveryError("original_repository_final_source_changed")
    data["final_witness"] = final_witness
    return final_witness


def assert_repository_completion_final_source(final_source, final_witness=None):
    """A revoked recovered pointer can never fall back to ordinary authority."""
    witness = getattr(final_source, "_recovered_completion", None)
    if witness is None or getattr(final_source, "_seal", None) is not witness:
        raise RepositorySourceRecoveryError("original_repository_scoped_completion_required")
    repository_scoped_completion_fence(witness)
    data = _COMPLETIONS[witness]
    if (data["final_source"] is not final_source
            or final_witness is not None and data["final_witness"] is not final_witness):
        raise RepositorySourceRecoveryError("original_repository_final_source_changed")


def repository_completion_context(witness):
    """Actual staged owner context; a copied public mapping cannot call this."""
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["context"]


def repository_completion_result(witness):
    """Literal verified original outputs, used only by existing final owners."""
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["result"]


def repository_completion_cleanup_envelope(witness):
    """Exact Source-written artifact bytes, bound to an authentic completion."""
    assert_repository_completion_witness(witness)
    raw = _COMPLETIONS[witness].get("cleanup_envelope")
    if raw is None:
        raise RepositorySourceRecoveryError("original_repository_cleanup_envelope_required")
    return json.loads(raw)


def repository_completion_post_cas(witness):
    assert_repository_completion_witness(witness)
    data = _COMPLETIONS[witness]
    if data.get("post_cas") is None:
        raise RepositorySourceRecoveryError("original_repository_completion_not_committed")
    return MappingProxyType(dict(data["post_cas"]))


def repository_completion_outcome(witness):
    assert_repository_completion_witness(witness)
    data = _COMPLETIONS[witness]
    return {"job_id": data["context"]["run"].run_identity,
        "iteration_id": data["post_cas"]["iteration_id"], "status": data["status"],
        "cleanup_proven": True, "manifest_artifact_ref": data["readback"]["artifact_ref"],
        "manifest_artifact_digest": data["readback"]["artifact_digest"], "no_learning": True}


def repository_completion_committed_rows(witness):
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["committed_rows"]


@asynccontextmanager
async def _repository_recovery_fence(service, jobs, *, job_id, owner):
    """The owner holds one non-reentrant fence through staging and commit."""
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair import RepoRepairService
    if (type(service) is not RepoRepairService or service.jobs is not jobs
            or type(owner) is not WorkBoardOwner or not job_id):
        raise RepositorySourceRecoveryError("repository_source_recovery_unavailable")
    async with configuration_mutation_lock:
        fence = _RepositoryRecoveryFence(service, jobs, job_id, owner,
            asyncio.current_task(), configuration_mutation_lock, _FENCE_SEAL)
        _FENCES[fence] = True
        try:
            yield fence
        finally:
            _FENCES.pop(fence, None)


def assert_repository_recovery_fence(fence, *, service, jobs, job_id, owner):
    """Registry and exact owner/task identity, rather than lock-state alone."""
    from src.model_fabric.effective_policy import configuration_mutation_lock
    if (type(fence) is not _RepositoryRecoveryFence or fence not in _FENCES
            or fence.seal is not _FENCE_SEAL or fence.service is not service or fence.jobs is not jobs
            or fence.job_id != job_id or fence.owner != owner or fence.task is not asyncio.current_task()
            or fence.lock is not configuration_mutation_lock or not fence.lock.locked()):
        raise RepositorySourceRecoveryError("repository_source_recovery_fence_unavailable")


async def _load_recovery_original(service, jobs, *, job_id, owner, expected_job_revision, fence):
    """Current canonical metadata precedes any completion-file read."""
    from src.db.models import Goal, GoalStatus
    from src.workflows import repo_repair_source as source
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
    source._assert_task_publication_configuration(service)
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
        if run.revision != expected_job_revision:
            raise RepositorySourceRecoveryError("repository_source_recovery_stale")
        original, work, compiled, group, binding, task_source = source.read_repository_original(run)
        inventory = source.read_repository_inventory(run)
        if inventory["schema"] != "repository.checkpoint_inventory.v3":
            raise RepositorySourceRecoveryError("original_producer_registration_missing")
        goal = await db.get(Goal, group.goal_id)
        source._assert_repository_original_limits(run, goal, source._repository_policy_limits())
        if goal is None or goal.status != GoalStatus.active:
            raise RepositorySourceRecoveryError("repository_source_recovery_goal_changed")
        reservation = jobs._repo_repair_reservation_state(run)
        if (reservation is None or reservation["status"] != "held"
                or not jobs._repo_repair_reservation_matches(reservation, job_id=job_id,
                    attempt_id=original["repository_attempt_id"], fence=run.fencing_token,
                    authority_digest=run.authority_digest)
                or reservation["execution_deadline_at"] != original["original_deadline_at"]):
            raise RepositorySourceRecoveryError("repository_source_recovery_hold_changed")
        return {"run": run, "original": original, "work": work, "compiled": compiled,
            "group": group, "binding": binding, "task_source": task_source, "inventory": inventory}


async def issue_repository_source_producer(service, jobs, job, *, owner):
    """Build callbacks for one actual Source-owned, single-use execution.

    The executor's private ready registry proves its actual Popen/channel
    observation. This owner supplies canonical commit-before-ACK and current
    authorization; a ready dataclass or signature is never sufficient.
    """
    from src.execution.repo_original_producer import (
        issue_original_producer_owner, assert_original_producer_ready,
        original_producer_observation, OriginalProducerRegistrationAck, OriginalProducerCommand,
        assert_original_producer_command, original_producer_sources, canonical, digest,
    )
    from src.db.models import RepoRepairProposal, ApprovalRequest
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import _as_utc, _utc_now
    from datetime import datetime
    source = _source()
    source.assert_repo_iteration_process_binding(job.iteration_binding, job)
    loop = asyncio.get_running_loop()
    actual_owner = None
    registration_digest = None
    next_ordinal = 1

    async def register(ready):
        nonlocal registration_digest
        assert_original_producer_ready(actual_owner, ready)
        observation = original_producer_observation(actual_owner, ready)
        admission = json.loads(observation.admission_json)
        if hashlib.sha256(observation.admission_json.encode()).hexdigest() != ready.admission_digest:
            raise RepositorySourceRecoveryError("original_producer_admission_changed")
        async with _repository_recovery_fence(service, jobs, job_id=job.job_id, owner=owner):
            context = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            run = context["run"]
            if source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3":
                raise RepositorySourceRecoveryError("original_producer_registration_missing")
            identity = job.iteration_binding.iteration_id
            execution = source._repository_record(run, "repository:execution:" + identity)
            prepared = source._repository_record(run, "repository:prepared:" + identity)
            if (execution is None or prepared is None
                    or execution.get("process_binding") != job.iteration_binding.projection()
                    or execution.get("patch_sha256") != hashlib.sha256(job.patch_bytes).hexdigest()
                    or prepared.get("iteration_index") != job.iteration_binding.iteration_index):
                raise RepositorySourceRecoveryError("original_producer_execution_changed")
            async with jobs._session() as db:
                proposal = await db.get(RepoRepairProposal, execution["proposal_id"])
                approval = await db.get(ApprovalRequest, execution["approval_id"])
                if (proposal is None or approval is None or proposal.status != "execution_started"
                        or approval.status != "consumed" or proposal.workflow_run_id != job.job_id
                        or proposal.authority_digest != job.authority_digest
                        or proposal.patch_sha256 != execution["patch_sha256"]
                        or approval.fingerprint != execution["approval_fingerprint"]):
                    raise RepositorySourceRecoveryError("original_producer_approval_changed")
                proposal_json = proposal.model_dump(mode="json")
                approval_json = approval.model_dump(mode="json")
            durable = admission["original_producer"]
            host_binding = json.loads(observation.host_binding_json)
            if (durable["directory"] != observation.directory_path
                    or durable["nonce"] != ready.nonce or durable["patch_sha256"] != execution["patch_sha256"]
                    or durable["job"]["job_id"] != job.job_id
                    or durable["job"]["attempt_id"] != job.attempt_id
                    or durable["job"]["fencing_token"] != job.fencing_token
                    or durable["job"]["authority_digest"] != job.authority_digest
                    or durable["job"]["base_digest"] != job.base_digest
                    or durable["job"]["execution_deadline_at"] != job.execution_deadline_at
                    or durable["sources"] != original_producer_sources()
                    or type(admission["deadline_at"]) not in {int, float}
                    or not math.isfinite(admission["deadline_at"])
                    or host_binding.get("schema") != "repository.original_producer_host.v1"
                    or host_binding.get("boot_id") != ready.boot_id
                    or host_binding.get("guard_path") != observation.guard_path
                    or host_binding.get("guard_identity") != list(ready.guard_identity)
                    or _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at))):
                raise RepositorySourceRecoveryError("original_producer_admission_changed")
            payload = {"schema": "repository.original_producer.v1", "job_id": job.job_id,
                "iteration_id": identity, "iteration_index": job.iteration_binding.iteration_index,
                "repository_attempt_id": context["original"]["repository_attempt_id"],
                "owner_principal_id": owner.principal_id, "owner_session_id": owner.session_id,
                "root_fence": run.fencing_token, "root_authority_digest": run.authority_digest,
                "original_source_digest": source._source_digest(context["original"]),
                "native_binding": context["binding"].model_dump(mode="json"),
                "execution_digest": source._source_digest(execution),
                "prepared_digest": source._source_digest(prepared),
                "proposal_digest": source._source_digest(proposal_json),
                "approval_digest": source._source_digest(approval_json),
                "proposal_predecessor": {key: proposal_json[key] for key in (
                    "status", "revision", "last_receipt_id")},
                "process_binding": job.iteration_binding.projection(),
                "ready": ready.projection(), "ready_digest": digest(canonical(ready.projection())),
                "directory_path": observation.directory_path, "admission_digest": ready.admission_digest,
                "guard_path": observation.guard_path, "native_host_binding": host_binding,
                "original_deadline_at": context["original"]["original_deadline_at"],
                "execution_deadline_at": job.execution_deadline_at,
                "monotonic_deadline": admission["deadline_at"],
                "producer_sources": durable["sources"], "stage_identity": durable["stage_identity"],
                "fixed_admission_plan_digest": source._source_digest({key: admission[key]
                    for key in admission if key != "environment"}),
                "source_artifact_digest": context["source"].source_artifact_digest,
                "executor_posture_digest": prepared["executor_posture_digest"]}
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                await source._recheck_repository_sql(db, context)
                if _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at)):
                    raise RepositorySourceRecoveryError("original_producer_cutoff_expired")
                current = await jobs._fetch(db, job.job_id)
                live_proposal = await db.get(RepoRepairProposal, execution["proposal_id"], populate_existing=True)
                live_approval = await db.get(ApprovalRequest, execution["approval_id"], populate_existing=True)
                if (live_proposal is None or live_approval is None
                        or live_proposal.model_dump(mode="json") != proposal_json
                        or live_approval.model_dump(mode="json") != approval_json):
                    raise RepositorySourceRecoveryError("original_producer_approval_changed")
                if source._repository_record(current, "repository:producer:" + identity) is not None:
                    raise RepositorySourceRecoveryError("original_producer_registration_single_use")
                source._append_repository_record(current, "repository:producer:" + identity, payload,
                    inventory=source.repository_checkpoint_inventory(current, context["work"]))
                read_registered_repository_producer(current, iteration_index=job.iteration_binding.iteration_index)
                current.revision += 1
                await db.commit()
            registration_digest = source._source_digest(payload)
            # ACK follows canonical readback, not merely a successful commit.
            fresh = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            if (fresh["run"].revision != run.revision + 1
                    or read_registered_repository_producer(fresh["run"],
                        iteration_index=job.iteration_binding.iteration_index) != payload):
                raise RepositorySourceRecoveryError("original_producer_registration_readback_changed")
            assert_original_producer_ready(actual_owner, ready)
            return OriginalProducerRegistrationAck(registration_digest, payload["ready_digest"])

    async def authorize(command):
        nonlocal next_ordinal
        assert_original_producer_command(actual_owner, command)
        if (type(command) is not OriginalProducerCommand or registration_digest is None
                or command.registration_digest != registration_digest or type(command.ordinal) is not int
                or command.ordinal != next_ordinal or type(command.argv_digest) is not str
                or not source._SHA.fullmatch(command.argv_digest)):
            raise RepositorySourceRecoveryError("original_producer_command_changed")
        async with _repository_recovery_fence(service, jobs, job_id=job.job_id, owner=owner):
            context = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            registered = source._repository_record(context["run"], "repository:producer:" + job.iteration_binding.iteration_id)
            if (registered is None or source._source_digest(registered) != registration_digest
                    or registered["producer_sources"] != original_producer_sources()):
                raise RepositorySourceRecoveryError("original_producer_registration_changed")
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                await source._recheck_repository_sql(db, context)
                if _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at)):
                    raise RepositorySourceRecoveryError("original_producer_cutoff_expired")
                await db.commit()
        next_ordinal += 1
        return True

    def register_ready(ready):
        return asyncio.run_coroutine_threadsafe(register(ready), loop).result(timeout=10)

    def authorize_command(command):
        return asyncio.run_coroutine_threadsafe(authorize(command), loop).result(timeout=10)

    actual_owner = await issue_original_producer_owner(service, jobs, job,
        register_ready=register_ready, authorize_command=authorize_command)
    return actual_owner


async def recover_original_repository_cleanup(service, jobs, *, job_id, owner,
                                               expected_job_revision, action):
    """Two closed actions; incomplete owner dependencies remain fail-closed."""
    if (type(expected_job_revision) is not int or expected_job_revision < 0
            or type(action) is not str or action not in _ACTIONS):
        raise RepositorySourceRecoveryError("repository_source_recovery_request_invalid")
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        await _load_recovery_original(service, jobs, job_id=job_id, owner=owner,
            expected_job_revision=expected_job_revision, fence=fence)
        # Registration, completion and physical-release owners must all be
        # integrated before this selected mode can authorize either action.
        # No legacy fallback, injected callback or public DTO supplies them.
        raise RepositorySourceRecoveryError("repository_source_recovery_unavailable", status_code=503)


def _assert_source_completion_append_metadata(run, identity, metadata):
    """Literal constructor metadata binds the existing two journal wrappers."""
    from src.workflows.job_runtime import _digest
    history = json.loads(run.checkpoint_receipts_json or "[]")
    if type(metadata) is not list or len(metadata) != 2:
        raise RepositorySourceRecoveryError("original_repository_append_metadata_changed")
    for role, item in zip(("cleanup", "readback"), metadata):
        wrappers = [wrapper for wrapper in history
            if wrapper.get("checkpoint_id") == "repository:" + role + ":" + identity]
        if (type(item) is not dict or set(item) != {"checkpoint_id", "safe", "created_at"}
                or len(wrappers) != 1 or set(wrappers[0]) != {
                    "checkpoint_id", "safe", "created_at", "payload", "state_digest"}
                or {key: wrappers[0][key] for key in item} != item
                or item["safe"] is not True
                or wrappers[0]["state_digest"] != _digest(wrappers[0]["payload"])):
            raise RepositorySourceRecoveryError("original_repository_append_metadata_changed")


def _read_original_cleanup_envelope_if_present(service, relative_path):
    """Bounded literal existing artifact read; absence alone permits first issue."""
    import os
    import stat
    from pathlib import PurePosixPath
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = -1
    parent_fd = -1
    opened = False
    try:
        fd = os.open(service._workspace(), flags | os.O_DIRECTORY)
        for component in PurePosixPath(relative_path).parts[:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            metadata = os.fstat(fd)
            if metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
                raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        parent_fd = fd
        child = os.open(PurePosixPath(relative_path).name, flags, dir_fd=parent_fd)
        fd = child
        opened = True
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or metadata.st_size > 1048576):
            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        before_named = os.stat(PurePosixPath(relative_path).name, dir_fd=parent_fd, follow_symlinks=False)
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
                value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before_named) != identity(metadata):
            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(1048577)
        after_fd = os.fstat(fd)
        after_named = os.stat(PurePosixPath(relative_path).name, dir_fd=parent_fd, follow_symlinks=False)
        if (identity(after_fd) != identity(metadata) or identity(after_named) != identity(metadata)
                or len(raw) != metadata.st_size):
            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        if len(raw) > 1048576:
            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        from src.workflows.job_runtime import _canonical
        envelope = json.loads(raw)
        if (type(envelope) is not dict or set(envelope) != {
                "physical_projection", "source_completion_cas", "source_append_metadata"}
                or _canonical(envelope).encode() != raw):
            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
        return envelope
    except FileNotFoundError as exc:
        if opened:
            raise RepositorySourceRecoveryError("original_producer_artifact_changed") from exc
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise RepositorySourceRecoveryError("original_producer_artifact_changed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0 and parent_fd != fd:
            os.close(parent_fd)


async def _repository_original_accounting_rows(db, context, *, job_id):
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    source = _source()
    from src.workflows.inference_group_lookup import group_reservation_rows
    from src.workflows.inference_accounting import InferenceAccountingError
    group = context["group"]
    try:
        rows = await group_reservation_rows(db, owner_id=group.owner_principal_id,
            group_id=group.group_id, group_digest=source._source_digest(group.model_dump(mode="json")),
            original_root_id=group.owner_session_id, original_deadline_at=group.original_deadline_at, group=group)
    except InferenceAccountingError as exc:
        raise RepositorySourceRecoveryError("original_producer_accounting_changed") from exc
    root_rows = list((await db.scalars(select(InferenceCostReservation).where(
        InferenceCostReservation.job_id == job_id).limit(13))).all())
    from src.workflows.general_task_accounting import entry_for, reservation_liability
    from src.workflows.inference_accounting import InferenceAccountingError
    try:
        group_cost = sum(reservation_liability(row) for row in rows)
        root_cost = sum(reservation_liability(row) for row in root_rows)
    except InferenceAccountingError as exc:
        raise RepositorySourceRecoveryError("original_producer_accounting_changed") from exc
    if (len(rows) > group.max_inference_calls or len(root_rows) > context["work"].limits.max_iterations
            or group_cost > group.max_cost_microusd or root_cost > context["work"].limits.max_cost_microusd
            or any(row.operation_id not in {item.operation_id for item in rows} for row in root_rows)):
        raise RepositorySourceRecoveryError("original_producer_accounting_changed")
    expected_operations = {}
    for index in range(1, context["work"].limits.max_iterations + 1):
        iteration = source.iteration_identity(job_id, context["original"]["repository_attempt_id"],
            source._source_digest(context["original"]["original_input"]), index)
        prepared = source._repository_record(context["run"], "repository:prepared:" + iteration)
        response = source._repository_record(context["run"], "repository:response:" + iteration)
        accounting_record = source._repository_record(context["run"], "repository:accounting:" + iteration)
        if prepared is not None:
            expected_operations["remote:repo-work:" + iteration] = (iteration, prepared, response, accounting_record)
        if response is not None and not any(row.operation_id == response["operation_id"] for row in root_rows):
            raise RepositorySourceRecoveryError("original_producer_accounting_changed")
    for row in root_rows:
        entry = entry_for(row)
        expected = expected_operations.get(row.operation_id)
        member = entry.get("repository_binding") if entry else None
        if (expected is None or entry["role"] != "repository_iteration" or type(member) is not dict
                or member.get("repository_job_id") != job_id
                or member.get("repository_attempt_id") != context["original"]["repository_attempt_id"]
                or member.get("source_checkpoint_digest") != source._source_digest(context["original"])
                or member.get("iteration_id") != expected[0]
                or member.get("repository_fence") != context["run"].fencing_token
                or member.get("parent_task_id") != context["binding"].task_id
                or member.get("parent_attempt_id") != context["binding"].attempt_id
                or member.get("native_invocation_id") != context["binding"].invocation_id
                or member.get("iteration_index") != expected[1]["iteration_index"]
                or member.get("operation_id") != row.operation_id
                or member.get("original_max_cost_microusd") != context["work"].limits.max_cost_microusd
                or row.payload_digest != expected[1]["request_body_digest"]):
            raise RepositorySourceRecoveryError("original_producer_accounting_changed")
        _, _, response, accounting_record = expected
        if response is not None:
            if (accounting_record is None or row.state != "settled" or row.contact_started_at is None
                    or source._source_digest(row.model_dump(mode="json")) != response["accounting_digest"]
                    or accounting_record["accounting_digest"] != response["accounting_digest"]
                    or accounting_record["operation_id"] != row.operation_id
                    or accounting_record["state"] != row.state
                    or accounting_record["actual_cost_microusd"] != row.actual_cost_microusd
                    or accounting_record["bound_microusd"] != row.bound_microusd):
                raise RepositorySourceRecoveryError("original_producer_accounting_changed")
    return rows

@asynccontextmanager
async def stage_repository_knownpost_completion(service, jobs, *, job_id, owner, iteration_index,
        expected_job_revision=None):
    """Verify the one committed Unknown successor without changing any row.

    Every yielded authority expires with this physical guard and Source fence.
    The immutable first-staging rows digest remains an audit receipt.
    """
    from contextlib import ExitStack
    from src.db.models import (RepoRepairProposal, ApprovalRequest, OperatorSession,
        InferenceCostReservation, WorkBoardInputArtifact)
    from src.workflows.job_runtime import _canonical, _as_utc, _utc_now
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows import repo_repair_stop as stop_owner
    from src.execution.repo_original_producer import (original_producer_live_owner,
        stage_original_producer_completion, original_producer_completion_result)
    source = _source()
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            revision = root.revision if expected_job_revision is None else expected_job_revision
            if type(revision) is not int or revision < 0:
                raise RepositorySourceRecoveryError("repository_source_recovery_stale")
        original_context = await _load_recovery_original(service, jobs, job_id=job_id,
            owner=owner, expected_job_revision=revision, fence=fence)
        root = original_context["run"]
        registration = read_registered_repository_producer(root, iteration_index=iteration_index)
        identity = registration["iteration_id"]
        if root.status != "unknown_external_effect":
            raise RepositorySourceRecoveryError("original_repository_knownpost_changed")
        stop = source._repository_record(root, stop_owner.STOP_ID)
        cleanup = source._repository_record(root, "repository:cleanup:" + identity)
        readback = source._repository_record(root, "repository:readback:" + identity)
        if (stop is None or source._repository_record(root, "repository:stop-uncertainty-successor:v1") is None
                or cleanup is None or readback is None or cleanup.get("cleanup_proven") is not True
                or cleanup.get("source_completion_cas") != readback.get("source_completion_cas")):
            raise RepositorySourceRecoveryError("original_repository_knownpost_changed")
        async with jobs._session() as db:
            session = await db.get(OperatorSession, owner.session_id)
            if (session is None or session.principal_id != owner.principal_id or session.revoked_at is not None
                    or session.replaced_by_id or session.is_bearer_tombstone
                    or _as_utc(session.idle_expires_at) <= _utc_now()
                    or _as_utc(session.absolute_expires_at) <= _utc_now()):
                raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
        callback = service._iterative_process_callbacks.get(identity)
        live_owner = live_result = None
        if callback is not None:
            if not callback.done() or callback.cancelled() or callback.exception() is not None:
                raise RepositorySourceRecoveryError("original_repository_knownpost_pending")
            live_result = callback.result()
            try:
                live_owner = original_producer_live_owner(service, jobs, live_result)
            except (ValueError, OSError) as exc:
                raise RepositorySourceRecoveryError("original_repository_knownpost_pending") from exc
        with ExitStack() as scopes:
            try:
                physical = scopes.enter_context(stage_original_producer_completion(registration,
                    owner=live_owner, result=live_result))
                result = original_producer_completion_result(physical)
            except (ValueError, OSError) as exc:
                raise RepositorySourceRecoveryError("original_repository_knownpost_pending") from exc
            envelope = _read_original_cleanup_envelope_if_present(service,
                "artifacts/repo-repair/model/iteration-" + identity + "-cleanup.json")
            if live_owner is not None and (envelope is None
                    or _canonical(envelope["physical_projection"]) != _canonical(result["iteration_cleanup_witness"].projection())):
                raise RepositorySourceRecoveryError("original_repository_knownpost_physical_changed")
            proof = _verify_repository_knownpost_root(root, registration, result, envelope)
            payloads = _knownpost_original_payloads(root, registration, result, envelope)
            # Literal readback/diagnostics are independently read before SQL.
            for payload, ref_key, digest_key in ((payloads[0], "artifact_ref", "artifact_digest"),
                    (payloads[1], "artifact_ref", "artifact_digest"),
                    (payloads[1], "diagnostics_artifact_ref", "diagnostics_artifact_digest")):
                raw = service._read_private_artifact(payload[ref_key], expected_digest=payload[digest_key])
                if hashlib.sha256(raw).hexdigest() != payload[digest_key]:
                    raise RepositorySourceRecoveryError("original_repository_knownpost_artifact_changed")
                if ref_key == "artifact_ref" and payload is payloads[1] and raw != result["outputs"]["readback.json"]:
                    raise RepositorySourceRecoveryError("original_repository_knownpost_artifact_changed")
            async with _stage_repository_knownpost_identity(service, jobs, root=root, owner=owner,
                    fence=fence, physical=physical, registration=registration, result=result,
                    envelope=envelope, proof=proof) as stage:
                staged = await source.stage_repository_knownpost_context(service, jobs,
                    stage=stage, owner=owner, fence=fence)
                stop_owner.assert_repository_stop_context(staged, service=service, jobs=jobs)
                context = staged.data
                execution = source._repository_record(context["run"], "repository:execution:" + identity)
                async with jobs._session() as db:
                    proposal = await db.get(RepoRepairProposal, execution["proposal_id"])
                    approval = await db.get(ApprovalRequest, execution["approval_id"])
                    if proposal is None or approval is None:
                        raise RepositorySourceRecoveryError("original_producer_approval_changed")
                    proposal_json, approval_json = proposal.model_dump(mode="json"), approval.model_dump(mode="json")
                    predecessor = {**proposal_json, **registration["proposal_predecessor"]}
                    complete = result["original_producer_completion"]["outcome"] in {
                        "completed_requested_checks", "completed_requested_check_failure"}
                    if (source._source_digest(predecessor) != registration["proposal_digest"]
                            or source._source_digest(approval_json) != registration["approval_digest"]
                            or proposal_json["revision"] != registration["proposal_predecessor"]["revision"] + 1
                            or proposal_json["status"] != ("execution_verified" if complete else "execution_partial")
                            or proposal_json["last_receipt_id"] != "repository:readback:" + identity):
                        raise RepositorySourceRecoveryError("original_producer_approval_changed")
                    accounting = await _repository_original_accounting_rows(db, context, job_id=job_id)
                    accounting_rows = {row.operation_id: _canonical(row.model_dump(mode="json")) for row in accounting}
                rows = tuple(context["rows"]) + (
                    (RepoRepairProposal, execution["proposal_id"], _canonical(proposal_json)),
                    (ApprovalRequest, execution["approval_id"], _canonical(approval_json))) + tuple(
                    (InferenceCostReservation, key, raw) for key, raw in sorted(accounting_rows.items()))
                _KNOWNPOST_STAGES[stage]["context_rows"] = rows
                # Finish physical checks before the read-only IMMEDIATE fence.
                if original_producer_completion_result(physical) is not result:
                    raise RepositorySourceRecoveryError("original_repository_knownpost_changed")
                source._assert_task_publication_configuration(service)
                async with jobs._session() as db:
                    await _begin_sqlite_immediate(db)
                    assert_repository_knownpost_stage(stage, service=service, jobs=jobs, fence=fence)
                    if not await source._validate_repository_knownpost_context_sql(db, service, jobs,
                            stage=stage, context=context):
                        raise RepositorySourceRecoveryError("original_repository_knownpost_epoch_changed")
                    for model, key, expected in rows:
                        actual = await db.get(model, key, populate_existing=True)
                        if actual is None or _canonical(actual.model_dump(mode="json")) != expected:
                            raise RepositorySourceRecoveryError("original_repository_knownpost_epoch_changed")
                        if isinstance(actual, OperatorSession) and (actual.revoked_at is not None
                                or _as_utc(actual.idle_expires_at) <= _utc_now()
                                or _as_utc(actual.absolute_expires_at) <= _utc_now()):
                            raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
                        if isinstance(actual, WorkBoardInputArtifact) and _as_utc(actual.expires_at) <= _utc_now():
                            raise RepositorySourceRecoveryError("repository_source_recovery_source_expired")
                    current_accounting = await _repository_original_accounting_rows(db, context, job_id=job_id)
                    if {row.operation_id: _canonical(row.model_dump(mode="json")) for row in current_accounting} != accounting_rows:
                        raise RepositorySourceRecoveryError("original_producer_accounting_changed")
                    await db.commit()
                    for model, key, expected in rows:
                        actual = await db.get(model, key, populate_existing=True)
                        if actual is None or _canonical(actual.model_dump(mode="json")) != expected:
                            raise RepositorySourceRecoveryError("original_repository_knownpost_readback_changed")
                witness = _OriginalRepositoryProducerCompletionWitness()
                _COMPLETIONS[witness] = {"service": service, "jobs": jobs, "context": context,
                    "knownpost_stage": stage, "result": result, "post_cas": envelope["source_completion_cas"],
                    "status": result["status"] if complete else "held_partial", "committed_rows": rows,
                    "cleanup_envelope": _canonical(envelope),
                    "completion_digest": source._source_digest(result["original_producer_completion"]),
                    "result_status": result["status"], "output_digests": {
                        name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()},
                    "readback": {"artifact_ref": payloads[1]["artifact_ref"], "artifact_digest": payloads[1]["artifact_digest"]}}
                try:
                    assert_repository_completion_witness(witness, service=service, jobs=jobs)
                    yield witness
                finally:
                    _COMPLETIONS.pop(witness, None)


async def publish_original_repository_completion(service, jobs, *, job_id, owner, iteration_index,
        producer_owner=None, actual_result=None, expected_job_revision=None):
    """Keep the original live returned-witness publication behavior."""
    async with _original_repository_completion_publication(service, jobs, job_id=job_id,
            owner=owner, iteration_index=iteration_index, producer_owner=producer_owner,
            actual_result=actual_result, expected_job_revision=expected_job_revision) as witness:
        return witness


@asynccontextmanager
async def stage_original_repository_completion_publication(service, jobs, *, job_id, owner,
        iteration_index, expected_job_revision):
    """Original ownerless publication held through the private final consumer."""
    if type(expected_job_revision) is not int or expected_job_revision < 0:
        raise RepositorySourceRecoveryError("repository_source_recovery_request_invalid")
    async with _original_repository_completion_publication(service, jobs, job_id=job_id,
            owner=owner, iteration_index=iteration_index, expected_job_revision=expected_job_revision,
            _scoped=True) as witness:
        yield witness


@asynccontextmanager
async def _original_repository_completion_publication(service, jobs, *, job_id, owner, iteration_index,
        producer_owner=None, actual_result=None, expected_job_revision=None, _scoped=False):
    """One Source-owned publication for live and authentic original restart.

    Physical storage/guard staging precedes the IMMEDIATE writer. Neither a
    caller result nor canonical metadata can issue the private witness alone.
    """
    from datetime import datetime
    from sqlalchemy import select
    from src.db.models import (RepoRepairProposal, ApprovalRequest, InferenceCostReservation,
        OperatorSession, WorkBoardInputArtifact)
    from src.workflows.job_runtime import _canonical, _as_utc, _utc_now
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows import repo_repair_stop as stop_owner
    from src.execution.repo_original_producer import (
        stage_original_producer_completion, original_producer_completion_result,
    )
    source = _source()
    if (producer_owner is None) != (actual_result is None):
        raise RepositorySourceRecoveryError("original_repository_completion_owner_required")
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        source._assert_task_publication_configuration(service)
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            if expected_job_revision is not None and (type(expected_job_revision) is not int
                    or expected_job_revision < 0 or run.revision != expected_job_revision):
                raise RepositorySourceRecoveryError("repository_source_recovery_stale")
            if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
                raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
            registration = read_registered_repository_producer(run, iteration_index=iteration_index)
            existing_stop = source._repository_record(run, stop_owner.STOP_ID)
            expired = _utc_now() >= _as_utc(datetime.fromisoformat(registration["original_deadline_at"]))
        if existing_stop is not None or expired:
            reason = existing_stop["stop_reason"] if existing_stop else "deadline_exhausted"
            staged_stop = await stop_owner._context(service, jobs, job_id=job_id, owner=owner,
                limit_reason=reason if reason in stop_owner.AUTOMATIC_REASONS else None)
            staged_stop = await stop_owner._persist_repository_stop_intent_locked(service, jobs,
                context=staged_stop, owner=owner, reason=reason, fence=fence)
            context = staged_stop.data
        elif run.status == "unknown_external_effect":
            context = (await stop_owner._context(service, jobs, job_id=job_id, owner=owner)).data
        else:
            context = await source._repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        registration = read_registered_repository_producer(run, iteration_index=iteration_index)
        identity = registration["iteration_id"]
        execution = source._repository_record(run, "repository:execution:" + identity)
        async with jobs._session() as db:
            proposal = await db.get(RepoRepairProposal, execution["proposal_id"])
            approval = await db.get(ApprovalRequest, execution["approval_id"])
            if proposal is None or approval is None:
                raise RepositorySourceRecoveryError("original_producer_approval_changed")
            proposal_json, approval_json = proposal.model_dump(mode="json"), approval.model_dump(mode="json")
            if (source._source_digest(proposal_json) != registration["proposal_digest"]
                    or source._source_digest(approval_json) != registration["approval_digest"]):
                raise RepositorySourceRecoveryError("original_producer_approval_changed")
            accounting = await _repository_original_accounting_rows(db, context, job_id=job_id)
            accounting_rows = {row.operation_id: _canonical(row.model_dump(mode="json")) for row in accounting}
        # This owner context remains active across literal artifact staging and
        # the exact writer. Restart guard freedom is not an unheld observation.
        with stage_original_producer_completion(registration, owner=producer_owner, result=actual_result) as physical:
            result = original_producer_completion_result(physical)
            manifest, outputs = result["manifest"], result["outputs"]
            body = result["original_producer_completion"]
            if (_canonical(manifest.get("iteration_binding")) != _canonical(registration["process_binding"])
                    or manifest.get("stage_removed") is not True
                    or manifest.get("supervisor_transport", {}).get("transport_kind") != "original_producer_durable_v1"
                    or any(manifest.get("supervisor_transport", {}).get(key) is not True for key in (
                        "command_output_drained", "command_descriptors_closed", "original_children_waited", "no_spawn"))
                    or manifest.get("process_cleanup", {}).get("cleanup_proven") is not True
                    or manifest.get("process_cleanup", {}).get("oracle") != "linux_subreaper_waitpid_echild"):
                raise RepositorySourceRecoveryError("original_producer_cleanup_changed")
            complete = body["outcome"] in {"completed_requested_checks", "completed_requested_check_failure"}
            status = result["status"] if complete else "held_partial"
            if complete and status not in {"succeeded", "failed"}:
                raise RepositorySourceRecoveryError("original_producer_result_changed")
            command_results = source._repository_command_results(manifest,
                node=context["work"].language_profile == "test_node") if complete else []
            before = run.revision
            stop = source._repository_record(run, stop_owner.STOP_ID)
            unknown = source._repository_unknown_root_projection(run) if source._repository_record(
                run, "repository:stop-uncertainty-successor:v1") is not None else None
            cas = {"before_revision": before, "post_revision": before + 1, "iteration_id": identity,
                "producer_registration_digest": source._source_digest(registration),
                "producer_completion_digest": source._source_digest(body),
                "stop_digest": source._source_digest(stop) if stop else None,
                "unknown_projection_digest": source._source_digest({key: getattr(unknown, key)
                    for key in unknown.__dataclass_fields__}) if unknown else None,
                "rows_digest": source._source_digest({
                    "domain": "repository.source_completion_rows.v1",
                    "context": [[model.__tablename__, str(key), expected]
                        for model, key, expected in context["rows"]],
                    "proposal": [RepoRepairProposal.__tablename__, execution["proposal_id"], _canonical(proposal_json)],
                    "approval": [ApprovalRequest.__tablename__, execution["approval_id"], _canonical(approval_json)],
                    "reservations": [[InferenceCostReservation.__tablename__, key, raw]
                        for key, raw in sorted(accounting_rows.items())]})}
            projection = _original_repository_cleanup_projection(registration, result)
            if producer_owner is not None:
                # Preserve the actual original live witness bytes required by
                # the existing final writer, rather than inventing its shape.
                if _canonical(projection) != _canonical(result["iteration_cleanup_witness"].projection()):
                    raise RepositorySourceRecoveryError("original_producer_cleanup_changed")
            prefix = "artifacts/repo-repair/model/iteration-" + identity
            existing_envelope = _read_original_cleanup_envelope_if_present(service, prefix + "-cleanup.json")
            retry_metadata = None
            if existing_envelope is not None:
                original_cas = existing_envelope["source_completion_cas"]
                if (type(original_cas) is not dict or set(original_cas) != set(cas)
                        or type(original_cas["before_revision"]) is not int
                        or original_cas["before_revision"] < 0
                        or type(original_cas["post_revision"]) is not int
                        or original_cas["post_revision"] != original_cas["before_revision"] + 1
                        or any(_canonical(original_cas[key]) != _canonical(value)
                            for key, value in cas.items() if key != "rows_digest")
                        or type(original_cas["rows_digest"]) is not str
                        or not source._SHA.fullmatch(original_cas["rows_digest"])
                        or _canonical(existing_envelope["physical_projection"]) != _canonical(projection)
                        or unknown is None and original_cas["rows_digest"] != cas["rows_digest"]):
                    raise RepositorySourceRecoveryError("original_producer_orphan_epoch_changed")
                # Rows digest is the first staging audit receipt, never a current
                # authority grant. Unknown's immutable full Root anchor proves
                # unchanged prefix; current rows are freshly checked below.
                cas = original_cas
                retry_metadata = existing_envelope["source_append_metadata"]
            async with _stage_repository_completion_append_publication(service, jobs,
                    context=context, owner=owner, fence=fence, physical=physical,
                    registration=registration, execution=execution, cas=cas, result=result,
                    physical_projection=projection, status=status, proposal_json=proposal_json,
                    approval_json=approval_json, accounting_rows=accounting_rows,
                    retry_metadata=retry_metadata) as stage:
                async with source.stage_repository_completion_appends(service, jobs,
                        stage=stage, owner=owner, fence=fence) as pending:
                    metadata = source.repository_completion_append_metadata(pending, service=service, jobs=jobs, fence=fence)
                    envelope = {"physical_projection": projection, "source_completion_cas": cas,
                        "source_append_metadata": [dict(item) for item in metadata]}
                    envelope_raw = _canonical(envelope).encode()
                    cleanup_payload, readback_payload = source.bind_repository_completion_append_payloads(
                        stage, pending, service=service, jobs=jobs, fence=fence)
                    cleanup_ref, cleanup_digest = service._write_private_artifact(prefix + "-cleanup.json", envelope_raw)
                    readback_ref, readback_digest = service._write_private_artifact(prefix + "-readback.json", outputs["readback.json"])
                    diagnostics = {"iteration_id": identity, "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
                        "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
                        "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
                        "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
                        "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
                        "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
                    diagnostics_raw = _canonical(diagnostics).encode()
                    diagnostics_ref, diagnostics_digest = service._write_private_artifact(prefix + "-diagnostics.json", diagnostics_raw)
                    for ref, digest, raw in ((cleanup_ref, cleanup_digest, envelope_raw),
                            (readback_ref, readback_digest, outputs["readback.json"]),
                            (diagnostics_ref, diagnostics_digest, diagnostics_raw)):
                        if service._read_private_artifact(ref, expected_digest=digest) != raw:
                            raise RepositorySourceRecoveryError("original_producer_artifact_changed")
                    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
                    async with jobs._session() as db:
                        await _begin_sqlite_immediate(db)
                        for model, key, expected in context["rows"]:
                            current_row = await db.get(model, key, populate_existing=True)
                            if current_row is None or _canonical(current_row.model_dump(mode="json")) != expected:
                                raise RepositorySourceRecoveryError("original_producer_completion_epoch_changed")
                            if isinstance(current_row, OperatorSession) and (current_row.revoked_at is not None
                                    or _as_utc(current_row.idle_expires_at) <= _utc_now()
                                    or _as_utc(current_row.absolute_expires_at) <= _utc_now()):
                                raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
                            if isinstance(current_row, WorkBoardInputArtifact) and _as_utc(current_row.expires_at) <= _utc_now():
                                raise RepositorySourceRecoveryError("repository_source_recovery_source_expired")
                        current = await jobs._fetch(db, job_id)
                        current_unknown = source._repository_unknown_root_projection(current) if source._repository_record(
                            current, "repository:stop-uncertainty-successor:v1") is not None else None
                        if (current.revision != before or read_registered_repository_producer(current,
                                iteration_index=iteration_index) != registration or current_unknown != unknown):
                            raise RepositorySourceRecoveryError("original_producer_completion_epoch_changed")
                        current_accounting = await _repository_original_accounting_rows(db, context, job_id=job_id)
                        if {row.operation_id: _canonical(row.model_dump(mode="json")) for row in current_accounting} != accounting_rows:
                            raise RepositorySourceRecoveryError("original_producer_accounting_changed")
                        live_proposal = await db.get(RepoRepairProposal, execution["proposal_id"], populate_existing=True)
                        live_approval = await db.get(ApprovalRequest, execution["approval_id"], populate_existing=True)
                        if (live_proposal is None or live_approval is None or live_proposal.model_dump(mode="json") != proposal_json
                                or live_approval.model_dump(mode="json") != approval_json):
                            raise RepositorySourceRecoveryError("original_producer_approval_changed")
                        inventory = source.repository_checkpoint_inventory(current, context["work"])
                        source._append_repository_record(current, "repository:cleanup:" + identity,
                            cleanup_payload, inventory=inventory, _completion_append=pending[0])
                        source._append_repository_record(current, "repository:readback:" + identity,
                            readback_payload, inventory=inventory, _completion_append=pending[1])
                        current.revision += 1
                        live_proposal.status = "execution_verified" if complete else "execution_partial"
                        live_proposal.last_receipt_id = "repository:readback:" + identity
                        live_proposal.revision += 1
                        # Flush the exact intended mutations, then explicitly
                        # preserve the actual pre-SQL Root timestamp.
                        await db.flush()
                        from sqlalchemy import update
                        await db.execute(update(type(current)).where(type(current).id == current.id)
                            .values(updated_at=run.updated_at))
                        expected_journal = current.checkpoint_receipts_json
                        expected_proposal = live_proposal.model_dump(mode="json")
                        await db.commit()
                        # Exact committed bytes are captured inside the same fence,
                        # while the physical owner's guard is still held.
                        committed_rows = []
                        for model, key, original_row in context["rows"]:
                            row = await db.get(model, key, populate_existing=True)
                            if row is None:
                                raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                            await db.refresh(row)
                            row_json = row.model_dump(mode="json")
                            if getattr(row, "run_identity", None) == job_id:
                                original_json = json.loads(original_row)
                                bookkeeping = {"revision", "checkpoint_receipts_json"}
                                if (row.revision != before + 1 or row.checkpoint_receipts_json != expected_journal
                                        or {key: value for key, value in row_json.items() if key not in bookkeeping}
                                            != {key: value for key, value in original_json.items() if key not in bookkeeping}):
                                    raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                            elif _canonical(row_json) != original_row:
                                raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                            committed_rows.append((model, key, _canonical(row.model_dump(mode="json"))))
                        await db.refresh(live_proposal)
                        if live_proposal.model_dump(mode="json") != expected_proposal:
                            raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                        committed_rows.append((RepoRepairProposal, execution["proposal_id"],
                            _canonical(live_proposal.model_dump(mode="json"))))
                        committed_rows.append((ApprovalRequest, execution["approval_id"], _canonical(approval_json)))
                        committed_rows.extend((InferenceCostReservation, key, raw) for key, raw in accounting_rows.items())
            witness = _OriginalRepositoryProducerCompletionWitness()
            _COMPLETIONS[witness] = {"service": service, "jobs": jobs, "context": context,
                "result": result, "post_cas": cas, "status": status,
                "committed_rows": tuple(committed_rows), "cleanup_envelope": _canonical(envelope),
                "completion_digest": source._source_digest(body),
                "result_status": result["status"],
                "output_digests": {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()},
                "readback": {"artifact_ref": readback_ref, "artifact_digest": readback_digest}}
            if _scoped:
                import threading
                _COMPLETIONS[witness].update(scoped_publication=_FENCE_SEAL, owner=owner,
                    job_id=job_id, task=asyncio.current_task(), thread=threading.get_ident(),
                    fence=fence, physical=physical, registration_json=_canonical(registration),
                    physical_projection_json=_canonical(projection), wait=None, wait_issued=False,
                    final_source=None, final_source_issued=False, final_witness=None, final_witness_issued=False,
                    finalizer_state={"state": None})
            try:
                if stop is not None:
                    await source.stage_repository_completion_post_context(service, jobs,
                        witness=witness, owner=owner, fence=fence)
                yield witness
            finally:
                if _scoped:
                    _COMPLETIONS.pop(witness, None)
