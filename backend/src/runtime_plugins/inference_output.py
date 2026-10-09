"""Private output evidence for one original, live inference accounting owner.

References are evidence only. They cannot construct a continuation or resume it.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

INTENT_ID = "inference:owned-output-intent.v1"
OUTPUT_ID = "inference:owned-output.v1"
CANDIDATE_ID = "inference:original-candidate.v1"
OUTPUT_IDS = frozenset({INTENT_ID, OUTPUT_ID})
MAX_BYTES = 1048576
PREFIX = "artifacts/work-board/native-inference-output"
_SEAL = object()
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = set("schema_version operation_id accounting_job_id owner_id attempt_count fencing_token reservation_sequence reservation_binding_digest turn_job_id turn_claim_digest family_call_index host_boot_nonce composition_binding_digest payload_digest policy_digest profile_id runtime_path purpose_deadline_at operation_deadline_at turn_deadline_at output_ref file_ref content_sha256 size_bytes output_codec result_content_sha256 route_request_id route_receipt_id route_receipt_hash route_attempts_digest accounting_readback_digest actual_cost_microusd memory_status".split())


def _deny(reason="native_inference_output_unproven"):
    from src.agent.turn_execution import NativeTurnBlocked
    raise NativeTurnBlocked(reason)


def _bytes(value):
    from src.model_fabric.native_inference import _bytes as bounded_bytes
    return bounded_bytes(value)


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def output_path(job_id, digest):
    if type(job_id) is not str or not job_id or type(digest) is not str or not _SHA.fullmatch(digest):
        _deny()
    return f"{PREFIX}/{hashlib.sha256(job_id.encode()).hexdigest()}/{digest}.json"


def _parent(root, reference, *, create=False):
    """Hold each directory descriptor; never follow a symlink or trust a path."""
    parts = Path(reference).parts
    if len(parts) != 5 or "/".join(parts[:3]) != PREFIX or not _SHA.fullmatch(parts[3]) or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[4]):
        _deny("native_inference_output_path_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.getuid()}:
            _deny("native_inference_output_directory_untrusted")
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                _deny("native_inference_output_directory_untrusted")
            if create:
                os.fchmod(fd, 0o700)
            elif metadata.st_mode & 0o077:
                _deny("native_inference_output_directory_untrusted")
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def read_output_bytes(root, payload, *, header_budget=None):
    validate_payload(payload)
    if header_budget is not None:
        from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
        if type(header_budget) is not HeaderReadBudget:
            raise HeaderBoundsError("canonical_bound_not_certified")
        header_budget.debit(payload["size_bytes"] + 1)
    try:
        parent, name = _parent(root, payload["file_ref"])
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.getuid() or before.st_nlink != 1 or before.st_size != payload["size_bytes"]):
                    _deny("native_inference_output_file_untrusted")
                chunks, remaining = [], before.st_size + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                after = os.fstat(fd)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                def stable(metadata):
                    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
                        metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                if (stable(before) != stable(after) or stable(current) != stable(before)
                    or len(data) != payload["size_bytes"] or hashlib.sha256(data).hexdigest() != payload["content_sha256"]):
                    _deny("native_inference_output_bytes_changed")
                return data
            finally:
                os.close(fd)
        finally:
            os.close(parent)
    except OSError as exc:
        from src.agent.turn_execution import NativeTurnBlocked
        raise NativeTurnBlocked("native_inference_output_readback_missing") from exc


def _write_output(root, payload, data):
    validate_payload(payload)
    if len(data) != payload["size_bytes"] or hashlib.sha256(data).hexdigest() != payload["content_sha256"]:
        _deny()
    parent, name = _parent(root, payload["file_ref"], create=True)
    temporary = ".native-output-" + secrets.token_hex(24)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("output write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
    if read_output_bytes(root, payload) != data:
        _deny()


def validate_payload(payload):
    if type(payload) is not dict or set(payload) != _FIELDS:
        _deny("native_inference_output_codec_invalid")
    integers = {"schema_version", "attempt_count", "fencing_token", "reservation_sequence", "family_call_index", "purpose_deadline_at", "operation_deadline_at", "turn_deadline_at", "size_bytes", "actual_cost_microusd"}
    for key, value in payload.items():
        if key in integers:
            if type(value) is not int or value < 0:
                _deny("native_inference_output_codec_invalid")
        elif type(value) is not str or not value or len(value.encode()) > 512:
            _deny("native_inference_output_codec_invalid")
    if (payload["schema_version"] != 1 or not 0 < payload["size_bytes"] <= MAX_BYTES
        or payload["output_codec"] != "native-inference-output-bytes.v1" or payload["memory_status"] != "no_learning"
        or not re.fullmatch(r"native-output-[0-9a-f]{48}", payload["output_ref"])
        or payload["file_ref"] != output_path(payload["accounting_job_id"], payload["content_sha256"])):
        _deny("native_inference_output_codec_invalid")
    for key in ("reservation_binding_digest", "turn_claim_digest", "composition_binding_digest", "payload_digest", "policy_digest", "content_sha256", "result_content_sha256", "route_receipt_hash", "route_attempts_digest", "accounting_readback_digest"):
        if not _SHA.fullmatch(payload[key]):
            _deny("native_inference_output_codec_invalid")
    return payload


def checked_output_records(row):
    """Retained closed codec, never a constructor for live result authority."""
    from src.workflows.job_runtime import _digest as owner_digest
    history = json.loads(row["checkpoint_receipts_json"] or "[]")
    selected = [item for item in history if type(item) is dict and item.get("checkpoint_id") in OUTPUT_IDS]
    if not selected:
        return None
    identifiers = [item["checkpoint_id"] for item in selected]
    if identifiers not in ([INTENT_ID], [INTENT_ID, OUTPUT_ID]):
        _deny("native_inference_output_history_invalid")
    for item in selected:
        if (set(item) != {"checkpoint_id", "state_digest", "safe", "payload"}
            or item["safe"] is not True or item["state_digest"] != owner_digest(item["payload"])):
            _deny("native_inference_output_history_invalid")
        payload = validate_payload(item["payload"])
        if (row["job_kind"] != "model_inference_ephemeral_v1" or row["run_identity"] != payload["accounting_job_id"]
            or row["owner_principal_id"] != payload["owner_id"] or row["attempt_count"] != payload["attempt_count"]
            or row["fencing_token"] < payload["fencing_token"]):
            _deny("native_inference_output_owner_changed")
    if len(selected) == 2 and selected[0]["payload"] != selected[1]["payload"]:
        _deny("native_inference_output_history_invalid")
    if row["status"] == "succeeded" and identifiers != [INTENT_ID, OUTPUT_ID]:
        _deny("native_inference_output_seal_missing")
    return selected


def checked_candidate_record(row):
    from src.model_fabric.native_inference import validate_candidate_payload
    from src.workflows.job_runtime import _digest as owner_digest
    selected = [item for item in json.loads(row["checkpoint_receipts_json"] or "[]")
        if type(item) is dict and item.get("checkpoint_id") == CANDIDATE_ID]
    if not selected:
        return None
    if (len(selected) != 1 or set(selected[0]) != {"checkpoint_id", "state_digest", "safe", "payload"}
        or selected[0]["safe"] is not True or selected[0]["state_digest"] != owner_digest(selected[0]["payload"])):
        _deny("native_inference_candidate_history_invalid")
    payload = validate_candidate_payload(selected[0]["payload"])
    if (row["job_kind"] != "model_inference_ephemeral_v1" or row["run_identity"] != payload["accounting_job_id"]
        or row["owner_principal_id"] != payload["owner_id"] or row["attempt_count"] != payload["attempt_count"]
        or row["fencing_token"] < payload["fencing_token"]):
        _deny("native_inference_candidate_owner_changed")
    outputs = checked_output_records(row)
    if row["status"] == "succeeded" and (outputs is None or outputs[-1]["checkpoint_id"] != OUTPUT_ID):
        _deny("native_inference_output_seal_missing")
    if outputs:
        output = outputs[-1]["payload"]
        for key in payload:
            if key in output and payload[key] != output[key]:
                _deny("native_inference_candidate_output_changed")
    return selected[0]


def verify_output_envelope(data, payload):
    """Byte evidence remains private; validate every retained component digest."""
    try:
        from decimal import Decimal
        value = json.loads(data, parse_float=Decimal)
    except (ValueError, UnicodeError):
        _deny("native_inference_output_envelope_invalid")
    if (type(value) is not dict or set(value) != {"schema_version", "result_codec", "result_content", "result_content_sha256", "route_receipt", "route_attempts", "accounting_readback"}
        or value["schema_version"] != 1 or type(value["schema_version"]) is not int
        or value["result_codec"] not in {"sdk_chat_message", "direct_chat_completion", "direct_stream"}
        or _bytes(value) != data or _digest(value["result_content"]) != payload["result_content_sha256"]
        or value["result_content_sha256"] != payload["result_content_sha256"]
        or _digest(value["route_attempts"]) != payload["route_attempts_digest"]
        or _digest(value["accounting_readback"]) != payload["accounting_readback_digest"]):
        _deny("native_inference_output_envelope_invalid")
    from src.model_fabric.receipts import canonical_hash
    route = value["route_receipt"]
    if (type(route) is not dict or route.get("receipt_id") != payload["route_receipt_id"]
        or route.get("request_id") != payload["route_request_id"] or route.get("outcome") != "succeeded"
        or canonical_hash(json.loads(data)["route_receipt"]) != payload["route_receipt_hash"]
        or route.get("attempts") != value["route_attempts"]):
        _deny("native_inference_output_envelope_invalid")
    return value


class _OutputPermission:
    def __init__(self, candidate, run_id, receipt, *, seal):
        if seal is not _SEAL:
            _deny()
        self.candidate, self.run_id, self.receipt, self.seal = candidate, run_id, _bytes(receipt), seal


def validate_output_publication(db, previous, current, *, run_id):
    protected = OUTPUT_IDS | {CANDIDATE_ID}
    output_before = {key: value for key, value in previous.items() if key in protected}
    output_after = {key: value for key, value in current.items() if key in protected}
    if any(output_after.get(key) != value for key, value in output_before.items()):
        _deny("native_inference_output_replacement_denied")
    added = [value for key, value in output_after.items() if key not in output_before]
    if not added:
        return ()
    permission = db.info.get("composition_native_inference_output_permission")
    if type(permission) is not _OutputPermission or permission.seal is not _SEAL or permission.run_id != run_id or len(added) != 1 or _bytes(added[0]) != permission.receipt:
        _deny("native_inference_output_private_writer_required")
    from src.model_fabric.native_inference import validate_result_route_witnesses, validate_consumption_candidate
    candidate = permission.candidate
    if added[0]["checkpoint_id"] == CANDIDATE_ID:
        validate_consumption_candidate(candidate)
    else:
        validate_result_route_witnesses(candidate.result_witness, candidate.route_witness)
    return tuple(added)


async def consume_candidate(repository, db, *, candidate):
    from src.model_fabric.native_inference import validate_consumption_candidate, build_candidate_payload
    from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, validate_family_binding
    from src.db.models import InferenceCostReservation
    from src.workflows.job_runtime import _digest as owner_digest, _native_plain, _as_utc
    validate_consumption_candidate(candidate)
    if (not db.in_transaction() or not db.info.get("native_writer_started")
        or db.info.get("composition_guard") is None or candidate.handle.repository is not repository):
        _deny("native_inference_candidate_original_writer_required")
    execution, handle = candidate.execution, candidate.handle
    turn = await repository._fetch(db, execution.admission.job_id)
    await repository._validate_turn_completion_in_session(db, turn, original_claim=execution.claim,
        authority_check=execution.authority_check, _family_phase="append")
    families = [item for item in json.loads(turn.checkpoint_receipts_json) if item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
    if len(families) != 1 or families[0]["state_digest"] != owner_digest(families[0]["payload"]):
        _deny("native_inference_candidate_family_changed")
    family = validate_family_binding(families[0]["payload"], turn, claim_payload=_native_plain(execution.claim.checkpoint)["payload"])
    if any(item["operation_id"] == handle.request.operation_id for item in family["operations"]):
        _deny("native_inference_candidate_replayed")
    run = await repository._fetch(db, handle.job_id)
    repository._assert_lease(run, owner=handle.owner, fencing_token=handle.fence)
    reservation = await db.get(InferenceCostReservation, handle.request.operation_id, populate_existing=True)
    if reservation is None:
        _deny("native_inference_candidate_owner_changed")
    if (not handle.ephemeral or run.owner_kind != "user" or run.goal_id is not None or run.goal_revision is not None
        or int(_as_utc(run.deadline_at).timestamp() * 1000) != candidate.operation_deadline_at
        or run.owner_principal_id != handle.request.owner_id):
        _deny("native_inference_candidate_owner_changed")
    payload = build_candidate_payload(candidate, run, reservation)
    if payload["turn_claim_digest"] != family["original_claim_digest"]:
        _deny("native_inference_candidate_claim_changed")
    history = json.loads(run.checkpoint_receipts_json)
    if any(item.get("checkpoint_id") in OUTPUT_IDS | {CANDIDATE_ID} for item in history) or len(history) > 47:
        _deny("native_inference_candidate_capacity_or_replay")
    receipt = {"checkpoint_id": CANDIDATE_ID, "safe": True, "state_digest": owner_digest(payload), "payload": payload}
    if len(_bytes(receipt)) > 8192:
        _deny("native_inference_candidate_capacity_or_replay")
    db.info["composition_native_inference_output_permission"] = _OutputPermission(candidate, run.run_identity, receipt, seal=_SEAL)
    run.checkpoint_receipts_json = json.dumps([*history, receipt], sort_keys=True, separators=(",", ":"))
    run.updated_at = datetime.now(timezone.utc)
    run.revision += 1
    await db.flush()
    validate_consumption_candidate(candidate)


async def _owner(repository, db, candidate):
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation, ModelRouteReceiptRecord, ModelRouteAttemptReceiptRecord
    from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, validate_family_binding, validate_family_operation_reference
    from src.workflows.job_runtime import _effect_ledger_or_raise, _job_has_unsafe_effects, _native_plain
    from src.model_fabric.repository import _route_from_record, _attempt_from_record
    from src.model_fabric.accounting import _policy_for_runtime
    from src.model_fabric.native_inference import validate_result_route_witnesses
    validate_result_route_witnesses(candidate.result_witness, candidate.route_witness)
    handle, execution = candidate.handle, candidate.execution
    if handle.repository is not repository or not handle.ephemeral or not handle.contacted:
        _deny("native_inference_output_owner_changed")
    turn = await repository._fetch(db, execution.admission.job_id)
    await repository._validate_turn_completion_in_session(db, turn, original_claim=execution.claim,
        authority_check=execution.authority_check, _family_phase="append")
    history = json.loads(turn.checkpoint_receipts_json)
    families = [item for item in history if item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
    from src.workflows.job_runtime import _digest as owner_digest
    if len(families) != 1 or families[0]["state_digest"] != owner_digest(families[0]["payload"]):
        _deny("native_inference_output_family_changed")
    family = validate_family_binding(families[0]["payload"], turn, claim_payload=_native_plain(execution.claim.checkpoint)["payload"])
    operations = [item for item in family["operations"] if item["operation_id"] == handle.request.operation_id]
    if len(operations) != 1 or operations[0] is not family["operations"][-1]:
        _deny("native_inference_output_family_changed")
    run = await repository._fetch(db, handle.job_id)
    repository._assert_lease(run, owner=handle.owner, fencing_token=handle.fence)
    reservation = await db.get(InferenceCostReservation, handle.request.operation_id, populate_existing=True)
    if reservation is None:
        _deny()
    validate_family_operation_reference(operations[0], run, reservation)
    marker = checked_candidate_record(run.model_dump())
    from src.model_fabric.native_inference import validate_candidate_binding
    if marker is None:
        _deny("native_inference_output_candidate_missing")
    validate_candidate_binding(marker["payload"], run, reservation)
    if marker["payload"]["request_ref"] != candidate.request_ref:
        _deny("native_inference_output_candidate_changed")
    if (run.status != "running" or run.fencing_token != handle.fence or reservation.state != "settled"
        or type(reservation.actual_cost_microusd) is not int or not 0 <= reservation.actual_cost_microusd <= reservation.bound_microusd
        or _policy_for_runtime(handle.request.runtime_path)[1] != handle.policy_digest):
        _deny("native_inference_output_accounting_unproven")
    effects = _effect_ledger_or_raise(run.effect_receipts_json)
    if _job_has_unsafe_effects(effects):
        _deny()
    readbacks = [item for item in effects if item.get("receipt_kind") == "readback" and item.get("effect_type") == "inference_accounting"
        and item.get("status") == "succeeded" and item.get("target_path") == "inference_accounting:" + reservation.operation_id
        and item.get("target_digest") == hashlib.sha256(str(reservation.actual_cost_microusd).encode()).hexdigest()
        and item.get("details", {}).get("verified") is True and item.get("details", {}).get("operation_id") == reservation.operation_id
        and item.get("details", {}).get("actual_cost_microusd") == reservation.actual_cost_microusd]
    if len(readbacks) != 1:
        _deny("native_inference_output_accounting_unproven")
    account, rows = await repository._accounting_rows(db)
    repository._assert_accounting_continuity(db.info["composition_guard"].workspace, account, rows)
    receipt = candidate.route_witness.receipt
    records = list((await db.execute(select(ModelRouteReceiptRecord).where(ModelRouteReceiptRecord.receipt_id == receipt.receipt_id))).scalars())
    attempts = list((await db.execute(select(ModelRouteAttemptReceiptRecord).where(ModelRouteAttemptReceiptRecord.route_receipt_id == receipt.receipt_id).order_by(ModelRouteAttemptReceiptRecord.attempt_index))).scalars())
    if len(records) != 1:
        _deny("native_inference_output_route_unproven")
    actual = _route_from_record(records[0], tuple(_attempt_from_record(item) for item in attempts))
    if actual.receipt_hash != receipt.receipt_hash or actual.as_safe_dict() != receipt.as_safe_dict() or actual.outcome != "succeeded":
        _deny("native_inference_output_route_unproven")
    return run, reservation, family, operations[0], readbacks[0], actual


async def seal_output(repository, *, result_witness, route_witness):
    """Intent commit -> private publication -> fresh owner readback/terminal CAS."""
    from src.model_fabric.native_inference import validate_result_route_witnesses
    from src.workflows.job_runtime import _digest as owner_digest, _bounded_checkpoint_receipts
    from sqlalchemy import text, update
    from src.db.models import WorkflowRunState
    candidate = validate_result_route_witnesses(result_witness, route_witness)
    async with repository._writer_session() as db:
        if db.info.get("composition_guard") is None:
            _deny("native_inference_output_native_writer_required")
        await db.execute(text("BEGIN IMMEDIATE"))
        db.info["native_writer_started"] = True
        run, reservation, family, operation, readback, route = await _owner(repository, db, candidate)
        history = json.loads(run.checkpoint_receipts_json)
        if any(item.get("checkpoint_id") in OUTPUT_IDS for item in history):
            _deny("native_inference_output_replay_denied")
        envelope = {"schema_version": 1, "result_codec": result_witness.result_type,
            "result_content": result_witness.result_snapshot, "result_content_sha256": result_witness.result_content_sha256,
            "route_receipt": route.as_safe_dict(), "route_attempts": [item.as_safe_dict() for item in route.attempts],
            "accounting_readback": readback}
        data = _bytes(envelope)
        digest = hashlib.sha256(data).hexdigest()
        payload = {"schema_version": 1, "operation_id": reservation.operation_id, "accounting_job_id": run.run_identity,
            "owner_id": reservation.owner_id, "attempt_count": run.attempt_count, "fencing_token": run.fencing_token,
            "reservation_sequence": reservation.sequence, "reservation_binding_digest": operation["reservation_binding_digest"],
            "turn_job_id": candidate.execution.admission.job_id, "turn_claim_digest": family["original_claim_digest"],
            "family_call_index": len(family["operations"]) - 1, "host_boot_nonce": candidate.host_boot_nonce,
            "composition_binding_digest": candidate.execution.claim.binding.binding_digest,
            "payload_digest": reservation.payload_digest, "policy_digest": reservation.policy_digest,
            "profile_id": reservation.profile_id, "runtime_path": reservation.runtime_path,
            "purpose_deadline_at": candidate.purpose_deadline_at, "operation_deadline_at": candidate.operation_deadline_at,
            "turn_deadline_at": candidate.turn_deadline_at, "output_ref": "native-output-" + secrets.token_hex(24),
            "file_ref": output_path(run.run_identity, digest), "content_sha256": digest, "size_bytes": len(data),
            "output_codec": result_witness.output_codec, "result_content_sha256": result_witness.result_content_sha256,
            "route_request_id": route.request_id, "route_receipt_id": route.receipt_id, "route_receipt_hash": route.receipt_hash,
            "route_attempts_digest": _digest(envelope["route_attempts"]), "accounting_readback_digest": _digest(readback),
            "actual_cost_microusd": reservation.actual_cost_microusd, "memory_status": "no_learning"}
        validate_payload(payload)
        intent = {"checkpoint_id": INTENT_ID, "state_digest": owner_digest(payload), "safe": True, "payload": payload}
        db.info["composition_native_inference_output_permission"] = _OutputPermission(candidate, run.run_identity, intent, seal=_SEAL)
        run.checkpoint_receipts_json = json.dumps(_bounded_checkpoint_receipts([*history, intent]), sort_keys=True, separators=(",", ":"))
        run.revision += 1
        run.updated_at = datetime.now(timezone.utc)
        root = db.info["composition_guard"].workspace.host_root
        await db.flush()
    # Never retry an existing intent. A partial file does not authorize adoption.
    candidate.validate_host_scope(candidate.host, candidate.original_scope)
    try:
        _write_output(root, payload, data)
    except OSError as exc:
        from src.agent.turn_execution import NativeTurnBlocked
        raise NativeTurnBlocked("native_inference_output_publication_unproven") from exc
    async with repository._writer_session() as db:
        if db.info.get("composition_guard") is None:
            _deny("native_inference_output_native_writer_required")
        await db.execute(text("BEGIN IMMEDIATE"))
        db.info["native_writer_started"] = True
        run, _, _, _, _, _ = await _owner(repository, db, candidate)
        entries = checked_output_records(run.model_dump())
        if entries != [intent] or read_output_bytes(root, payload) != data:
            _deny("native_inference_output_intent_changed")
        verify_output_envelope(data, payload)
        sealed = {**intent, "checkpoint_id": OUTPUT_ID}
        db.info["composition_native_inference_output_permission"] = _OutputPermission(candidate, run.run_identity, sealed, seal=_SEAL)
        run.checkpoint_receipts_json = json.dumps(_bounded_checkpoint_receipts([*json.loads(run.checkpoint_receipts_json), sealed]), sort_keys=True, separators=(",", ":"))
        run.updated_at = datetime.now(timezone.utc)
        run.revision += 1
        await db.flush()
        candidate.validate_host_scope(candidate.host, candidate.original_scope)
        now = datetime.now(timezone.utc)
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == run.run_identity,
            WorkflowRunState.revision == run.revision,
            WorkflowRunState.status == "running",
            WorkflowRunState.lease_owner == candidate.handle.owner,
            WorkflowRunState.fencing_token == candidate.handle.fence,
            WorkflowRunState.lease_expires_at > now,
            WorkflowRunState.deadline_at > now,
        ).values(status="succeeded", lease_owner=None, lease_expires_at=None,
            finished_at=now, updated_at=now, revision=run.revision + 1,
            result_digest=owner_digest({"operation_id": payload["operation_id"], "output_digest": sealed["state_digest"], "memory_status": "no_learning"}),
            result_summary="Governed original inference output readback; no_learning"
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            _deny("native_inference_output_terminal_cas_changed")
    return {"receipt_ref": "native-output-receipt-" + sealed["state_digest"], "output_ref": payload["output_ref"]}
