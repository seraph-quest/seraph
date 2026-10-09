"""Finite native dispatch. A canonical job locator is never invocation authority.

Only an original protected native claim witness admits a crossing. Missing
native candidates are explicit blocked branches; no JSON function/SQL/URL router.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import MappingProxyType
from collections.abc import Mapping

from .contracts import METHOD_DOMAINS, blocked, succeeded, validate_request, ref, sha
from .protocol import ProtocolError, closed, integer

WITNESS_PREFIX = "runtime-service-invocation:"
WITNESS_FIELDS = frozenset({"schema_version", "invocation_ref", "claim_ref", "checkpoint_id",
    "origin_method", "native_branch", "allowed_child_methods", "method_manifest_version", "method_manifest_digest",
    "attempt_count", "lease_owner", "fencing_token", "input_digest", "authority_digest",
    "run_fingerprint", "original_deadline_at", "package_digest", "host_composition_digest",
    "composition_binding_digest", "host_boot_nonce"})


class NativeServiceBlocked(ValueError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class OriginalServiceInvocation:
    witness: object
    binding: object
    host_boot_nonce: str
    native_turn_resource: object = None
    native_report_resource: object = None
    native_memory_report_source: object = None

    @property
    def deadline_at(self):
        return self.witness["original_deadline_at"]


@dataclass(frozen=True)
class CalledServiceInvocation:
    original: OriginalServiceInvocation
    method: str
    composition_epoch: int
    deadline_at: int
    parent_request_id: str
    host_boot_nonce: str


def _ms(value):
    if value is None:
        raise NativeServiceBlocked("native_original_deadline_missing")
    return int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp() * 1000)


def _receipt_witness(receipt):
    from src.workflows.job_runtime import _digest
    raw = receipt.get("payload")
    if isinstance(raw, Mapping):
        raw = dict(raw)
        if type(raw.get("allowed_child_methods")) is tuple:
            raw["allowed_child_methods"] = list(raw["allowed_child_methods"])
    value = closed(raw, WITNESS_FIELDS)
    if receipt.get("safe") is not True or receipt.get("state_digest") != _digest(value):
        raise NativeServiceBlocked("native_original_invocation_changed")
    integer(value["schema_version"], 2, 2)
    integer(value["attempt_count"], 1)
    integer(value["fencing_token"], 1)
    integer(value["original_deadline_at"], 1)
    for key in ("invocation_ref", "lease_owner", "claim_ref", "checkpoint_id"):
        ref(value[key])
    for key in ("input_digest", "authority_digest", "run_fingerprint", "package_digest",
                "host_composition_digest", "composition_binding_digest", "host_boot_nonce", "method_manifest_digest"):
        sha(value[key])
    if (value["origin_method"] not in METHOD_DOMAINS
        or value["checkpoint_id"] != WITNESS_PREFIX + value["claim_ref"]
        or receipt.get("checkpoint_id") != value["checkpoint_id"]
        or value["method_manifest_version"] != "runtime-service-methods.v1"
        or type(value["allowed_child_methods"]) is not list
        or not 1 <= len(value["allowed_child_methods"]) <= 34
        or value["allowed_child_methods"] != sorted(set(value["allowed_child_methods"]))
        or any(item not in METHOD_DOMAINS for item in value["allowed_child_methods"])):
        raise NativeServiceBlocked("native_original_method_unsupported")
    value = dict(value)
    value["allowed_child_methods"] = tuple(value["allowed_child_methods"])
    return value


def _witness(run, original_scope):
    entries = json.loads(run.checkpoint_receipts_json or "[]")
    selected = [entry for entry in entries if entry.get("checkpoint_id") == original_scope.witness["checkpoint_id"]]
    if len(selected) != 1:
        raise NativeServiceBlocked("native_original_invocation_missing")
    return _receipt_witness(selected[0])


def capture_original_scope(claim, host):
    """Capture only the private return of the original native claim writer."""
    from src.workflows.job_runtime import NativeServiceClaim
    from .bridge import CordisHost
    from .ownership import RuntimeCompositionBinding
    if (type(claim) is not NativeServiceClaim or type(host) is not CordisHost
        or type(claim.binding) is not RuntimeCompositionBinding or not host.admitting
        or claim._host is not host or host.reviewed is None or host.boot_nonce != claim.host_boot_nonce):
        raise NativeServiceBlocked("native_original_host_unavailable")
    witness = _receipt_witness(claim.checkpoint)
    binding = claim.binding
    if (witness["invocation_ref"] != claim.job["job_id"]
        or witness["composition_binding_digest"] != binding.binding_digest
        or witness["origin_method"] != binding.origin_method
        or witness["native_branch"] != binding.native_branch
        or witness["allowed_child_methods"] != binding.allowed_child_methods
        or witness["method_manifest_digest"] != binding.payload()["method_manifest_digest"]
        or witness["package_digest"] != binding.host_package_digest
        or witness["host_composition_digest"] != binding.host_composition_digest
        or witness["package_digest"] != host.reviewed.package_digest
        or witness["host_composition_digest"] != host.reviewed.composition_digest
        or witness["host_boot_nonce"] != host.boot_nonce):
        raise NativeServiceBlocked("native_original_host_changed")
    return OriginalServiceInvocation(MappingProxyType(witness), binding, claim.host_boot_nonce)


class NativeServiceDispatcher:
    """Current owners remain the sole source of permission and effects."""
    supported_methods = frozenset({"authority.resolve", "goals.read", "tasks.inspect",
        "conversation.read", "conversation.cancel", "agent-loop.cancelTurn",
        "agent-loop.inspectTurn", "conversation.accept", "agent-loop.startTurn",
        "conversation.append", "source-extraction.extract", "capabilities.list", "capabilities.describe",
        "connections.inspect", "memory.retrieve", "artifacts.stage", "artifacts.adopt",
        "artifacts.read", "audit.append", "inference.request", "memory.propose", "memory.applyReviewed", "memory.forget",
        "tasks.admit", "tasks.checkpoint", "capabilities.invoke", "tasks.settle", "tasks.cancel"})
    def __init__(self, *, jobs=None):
        from src.workflows.job_runtime import durable_job_repository
        self.jobs = jobs or durable_job_repository

    async def _current_in_db(self, db, invocation_ref, method, original_scope):
        from src.auth.service import authenticate_principal
        from src.db.models import OperatorSession
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        from .ownership import RuntimeCompositionBinding, validate_invocation, begin_native_writer
        from sqlalchemy import select
        if (type(original_scope) is not OriginalServiceInvocation
            or type(original_scope.binding) is not RuntimeCompositionBinding):
            raise NativeServiceBlocked("native_original_scope_missing")
        # Upgrade the existing owner session before private claims/message
        # bytes. Ordinary public reads do not establish this full proof.
        await begin_native_writer(db, owner="finite_service")
        run = await self.jobs._fetch(db, invocation_ref)
        witness = _witness(run, original_scope)
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        terminal_output_read = (method == "conversation.append" and run.job_kind == "conversation_turn_v1"
            and run.status == "succeeded")
        if (witness["invocation_ref"] != run.run_identity or not binding.allows(method)
            or binding.origin_method != witness["origin_method"] or binding.native_branch != witness["native_branch"]
            or binding.allowed_child_methods != witness["allowed_child_methods"]
            or binding.to_json() != original_scope.binding.to_json()
            or witness["host_boot_nonce"] != original_scope.host_boot_nonce
            or binding.binding_digest != witness["composition_binding_digest"]
            or run.attempt_count != witness["attempt_count"]
            or (not terminal_output_read and run.lease_owner != witness["lease_owner"])
            or run.fencing_token != witness["fencing_token"] or run.input_digest != witness["input_digest"]
            or run.authority_digest != witness["authority_digest"] or run.run_fingerprint != witness["run_fingerprint"]
            or _ms(run.deadline_at) != witness["original_deadline_at"]
            or witness["original_deadline_at"] <= int(datetime.now(timezone.utc).timestamp()*1000)):
            raise NativeServiceBlocked("native_original_attempt_changed")
        if dict(original_scope.witness) != witness:
            raise NativeServiceBlocked("native_original_scope_changed")
        await validate_invocation(db, binding)
        if run.status != "running" and not terminal_output_read:
            raise NativeServiceBlocked("native_original_job_not_running")
        if not terminal_output_read:
            self.jobs._assert_lease(run, owner=witness["lease_owner"], fencing_token=witness["fencing_token"])
        # Current baseline native service provenance is operator-root. Standing
        # programme jobs need their reviewed original durable binding; issuer
        # Root expiry must never be substituted for programme validation.
        if run.owner_kind != "user":
            raise NativeServiceBlocked("native_programme_provenance_unavailable")
        now = datetime.now(timezone.utc)
        root = await db.scalar(select(OperatorSession).where(
            OperatorSession.id == run.operator_session_id,
            OperatorSession.principal_id == run.owner_principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False),
            OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now)
            .execution_options(populate_existing=True))
        if root is None or run.operator_session_id != run.session_id:
            raise NativeServiceBlocked("native_original_root_inactive")
        await authenticate_principal(run.owner_principal_id, db=db)
        await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
            owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
            session_id=run.session_id, authority=run.declared_authority_json)
        if terminal_output_read:
            await self._validate_turn_output(db, run, original_scope)
        return run, witness, binding

    async def dispatch(self, frame, *, original_scope):
        from src.auth.service import AuthFailure
        from src.workflows.job_runtime import DurableJobError
        method = frame["method"]
        payload = validate_request(method, frame["payload"])
        if type(original_scope) is not CalledServiceInvocation:
            return blocked("native_original_scope_missing")
        call_scope = original_scope
        original_scope = call_scope.original
        try:
            if (frame["invocation_ref"] != original_scope.witness["invocation_ref"]
                or method != call_scope.method or not original_scope.binding.allows(method)
                or frame["composition_epoch"] != original_scope.binding.epoch_for(method)
                or frame["composition_epoch"] != call_scope.composition_epoch
                or frame["boot_nonce"] != original_scope.host_boot_nonce
                or frame["boot_nonce"] != call_scope.host_boot_nonce
                or frame["package_digest"] != original_scope.witness["package_digest"]
                or frame["composition_digest"] != original_scope.witness["host_composition_digest"]
                or frame["deadline_at"] != call_scope.deadline_at
                or frame["deadline_at"] > original_scope.deadline_at):
                raise NativeServiceBlocked("native_original_frame_changed")
            if method in {"tasks.admit", "tasks.checkpoint", "capabilities.invoke", "tasks.settle", "tasks.cancel"}:
                from .task_capability import dispatch_report_service
                return await dispatch_report_service(self, frame, call_scope)
            if method in {"artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append"}:
                from .read_artifacts import dispatch_artifact_operation
                return await dispatch_artifact_operation(self, frame, payload, original_scope)
            if method == "inference.request":
                from src.model_fabric.native_inference import dispatch_original_inference
                return await dispatch_original_inference(self, frame, call_scope)
            if method in {"conversation.cancel", "agent-loop.cancelTurn"}:
                from src.agent.native_turn_controls import dispatch_native_turn_cancel
                resource = original_scope.native_turn_resource
                if resource is None:
                    raise NativeServiceBlocked("native_turn_original_resource_missing")
                resource.owner._check(resource)
                purpose = resource.execution.host._native_cancel_purpose(call_scope, payload)
                async with self.jobs._session() as db:
                    return await dispatch_native_turn_cancel(self, db, frame, payload, call_scope, purpose)
            async with self.jobs._session() as db:
                run, witness, binding = await self._current_in_db(db, frame["invocation_ref"], method, original_scope)
                if method == "conversation.read":
                    from src.agent.session import session_manager
                    from src.agent.turn_execution import validate_native_turn_owner
                    resource = original_scope.native_turn_resource
                    if resource is None:
                        raise NativeServiceBlocked("native_turn_original_resource_missing")
                    resource.owner._check(resource)
                    if resource.execution.scope is not original_scope or resource.execution.stop.is_set():
                        raise NativeServiceBlocked("native_turn_original_resource_changed")
                    await validate_native_turn_owner(db, resource.admission)
                    value = await session_manager.native_turn_message_page(db,
                        execution=resource.execution, **payload)
                    return succeeded(method, value)
                if method in {"memory.propose", "memory.applyReviewed", "memory.forget"}:
                    from .memory_producer import dispatch_memory_mutation
                    return await dispatch_memory_mutation(self, db, run, witness, method, payload, original_scope)
                if method in {"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"}:
                    from .read_admission import NativeServiceReadAdmission, capability_read_projection, memory_read_projection
                    from .read_journal import read_candidate, validate_read_policy, seal_read_result
                    await validate_read_policy(db, run)
                    candidate = read_candidate(run)
                    admission = NativeServiceReadAdmission.from_candidate(candidate)
                    if candidate["method"] != method or payload != admission.wire_inputs(run.run_identity):
                        raise NativeServiceBlocked("native_read_original_inputs_changed")
                    if method in {"capabilities.list", "capabilities.describe"}:
                        value = capability_read_projection(candidate)
                    elif method == "memory.retrieve":
                        value = await memory_read_projection(db, owner_session_id=run.operator_session_id, candidate=candidate)
                    else:
                        from src.api.calendar import _native_read_connection_metadata
                        from src.work_board.contracts import WorkBoardOwner
                        value = await _native_read_connection_metadata(db,
                            WorkBoardOwner(principal_id=run.owner_principal_id, session_id=run.operator_session_id),
                            connection_id=candidate["connection_ref"], expected_revision=candidate["expected_connection_revision"])
                    result = succeeded(method, value)
                    seal_read_result(db, run, result, invocation_ref=run.run_identity, claim_ref=witness["claim_ref"])
                    await db.flush()
                    return result
                if method == "authority.resolve":
                    return succeeded(method, {"authority_ref": run.run_identity, "revision": run.revision,
                        "mode": "operator-root"})
                if method == "goals.read":
                    from src.db.models import Goal
                    goal = await db.get(Goal, run.goal_id, populate_existing=True) if run.goal_id else None
                    if goal is None:
                        return blocked("native_goal_not_bound")
                    return succeeded(method, {"goal_ref": goal.id, "revision": goal.revision,
                        "title": goal.title, "description": ""})
                if method in {"tasks.inspect", "agent-loop.inspectTurn"}:
                    target = payload.get("job_ref", payload.get("turn_ref"))
                    if target != run.run_identity:
                        return blocked("native_job_reference_not_bound")
                    records = json.loads(run.artifact_receipts_json or "[]")
                    identifiers = [item["artifact_id"] for item in records if item.get("artifact_id")]
                    if len(identifiers) > 16:
                        return blocked("native_artifact_inventory_exceeded")
                    return succeeded(method, {"job_ref": run.run_identity, "revision": run.revision,
                        "state": run.status, "artifact_refs": identifiers})
                if method in {"conversation.accept", "agent-loop.startTurn"}:
                    if payload["turn_ref"] != run.run_identity:
                        return blocked("native_turn_reference_not_bound")
                    await self._validate_turn_input(db, run, original_scope)
                    if method == "conversation.accept":
                        return succeeded(method, {"turn_ref": run.run_identity,
                            "job_ref": run.run_identity, "replayed": False})
                    return succeeded(method, {"job_ref": run.run_identity, "revision": run.revision,
                        "state": run.status})
                if method == "conversation.append":
                    output = await self._validate_turn_output(db, run, original_scope)
                    if payload["message_ref"] != output["message_ref"]:
                        return blocked("native_turn_output_reference_not_bound")
                    return succeeded(method, {"message_ref": output["message_ref"], "revision": run.revision})
            if method == "source-extraction.extract":
                value = await self._extract_public_source(frame, payload, original_scope)
                return succeeded(method, value)
            # Every remaining operation needs its reviewed canonical native
            # candidate/owner binding, never a generic handler or plugin args.
            return blocked("native_method_candidate_not_bound")
        except NativeServiceBlocked as exc:
            return blocked(exc.reason_code)
        except (AuthFailure, DurableJobError):
            return blocked("native_original_job_authority_changed")
        except (ValueError, PermissionError, KeyError, TypeError):
            return blocked("native_original_binding_unavailable")

    async def _validate_turn_input(self, db, run, scope):
        """Observe the original plain turn; never admit or execute another loop."""
        import hashlib
        from sqlalchemy import select, func
        from src.db.models import Message, Session
        from src.agent.turn_execution import _policy_digest
        from src.workflows.job_runtime import _digest
        if (run.job_kind != "conversation_turn_v1" or run.max_attempts != 1
            or scope.binding.native_branch not in {"direct_turn", "generic_turn"}
            or scope.binding.origin_method not in {"conversation.accept", "agent-loop.startTurn"}):
            raise NativeServiceBlocked("native_turn_branch_not_bound")
        inputs = closed(json.loads(run.arguments_json), {
            "schema_version", "message_ref", "content_digest", "native_route", "native_timeout_seconds"})
        integer(inputs["schema_version"], 1, 1)
        integer(inputs["native_timeout_seconds"], 1)
        ref(inputs["message_ref"])
        sha(inputs["content_digest"])
        if (_digest(inputs) != run.input_digest or inputs["native_route"] != scope.binding.native_branch
            or json.loads(run.declared_authority_json).get("native_policy_digest") != _policy_digest()):
            raise NativeServiceBlocked("native_turn_original_input_or_policy_changed")
        # Query the exact selected conversation before accessing message bytes.
        conversation = (await db.execute(select(Session.id, Session.owner_principal_id).where(
            Session.id == run.conversation_id))).one_or_none()
        if conversation is None or conversation.owner_principal_id != run.owner_principal_id:
            raise NativeServiceBlocked("native_turn_conversation_owner_changed")
        selected = await db.execute(select(Message.id, Message.role, Message.owner_principal_id,
            Message.operator_session_id, Message.session_id, Message.conversation_id, Message.thread_id,
            func.substr(Message.attachment_refs_json, 1, 3), func.substr(Message.content, 1, 65537)).where(
                Message.id == inputs["message_ref"]))
        message = selected.one_or_none()
        if (message is None or message.role != "user" or message.owner_principal_id != run.owner_principal_id
            or message.operator_session_id != run.operator_session_id
            or message.session_id != conversation.id or message.conversation_id != conversation.id
            or message.thread_id != conversation.id or message[-2] not in {None, "[]"}
            or len(message[-1].encode("utf-8")) > 65536
            or hashlib.sha256(message[-1].encode("utf-8")).hexdigest() != inputs["content_digest"]):
            raise NativeServiceBlocked("native_turn_original_message_changed")
        return inputs

    async def _validate_turn_output(self, db, run, scope):
        """Read one positively committed native output; never write/adopt it."""
        import hashlib
        from sqlalchemy import select, func
        from src.db.models import Message
        from src.workflows.job_runtime import _digest
        if run.status != "succeeded" or run.job_kind != "conversation_turn_v1":
            raise NativeServiceBlocked("native_turn_output_not_succeeded")
        inputs = await self._validate_turn_input(db, run, scope)
        matches = [entry for entry in json.loads(run.checkpoint_receipts_json or "[]")
            if entry.get("checkpoint_id") == "conversation:assistant-message"]
        if len(matches) != 1:
            raise NativeServiceBlocked("native_turn_output_proof_missing")
        receipt = matches[0]
        proof = closed(receipt.get("payload"), {"schema_version", "message_ref", "input_message_ref", "no_learning"})
        if (type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["no_learning"] is not True or receipt.get("safe") is not True
            or receipt.get("state_digest") != _digest(proof)
            or proof["input_message_ref"] != inputs["message_ref"]):
            raise NativeServiceBlocked("native_turn_output_proof_changed")
        ref(proof["message_ref"])
        # The output body is never sent to the plugin. Its proof read has the
        # same explicit bounded byte ceiling as the original plain message.
        selected = await db.execute(select(Message.id, Message.role, Message.owner_principal_id,
            Message.operator_session_id, Message.session_id, Message.conversation_id, Message.thread_id,
            func.substr(Message.attachment_refs_json, 1, 3), func.substr(Message.content, 1, 65537)).where(
                Message.id == proof["message_ref"]))
        message = selected.one_or_none()
        if (message is None or message.id == inputs["message_ref"] or message.role != "assistant"
            or message.owner_principal_id != run.owner_principal_id or message.operator_session_id != run.operator_session_id
            or message.session_id != run.conversation_id or message.conversation_id != run.conversation_id
            or message.thread_id != run.conversation_id or message[-2] not in {None, "[]"}
            or len(message[-1].encode("utf-8")) > 65536
            or hashlib.sha256(message[-1].encode("utf-8")).hexdigest() != run.result_digest):
            raise NativeServiceBlocked("native_turn_output_message_changed")
        return proof

    async def _extract_public_source(self, frame, payload, original_scope):
        """Resolve already acquired exact public bytes, never acquire on replay."""
        import hashlib
        from src.work_board.research_artifacts import read, normalized_source, NORMALIZATION
        from src.work_board.research_contracts import CHILD_KIND, SOURCE_BYTES
        from src.workflows.research_sources import current_inputs, acquire_source
        from src.workflows.research_native import checkpoint
        from src.workflows.job_runtime import _digest
        job_id = frame["invocation_ref"]
        row = await self.jobs.get_job(job_id)
        slot = payload["source_slot"]
        if row["job_kind"] != CHILD_KIND or not row["parent_job_id"]:
            raise NativeServiceBlocked("native_public_acquisition_not_bound")
        inputs = await current_inputs(self.jobs, row["parent_job_id"])
        if slot >= len(inputs.sources):
            raise NativeServiceBlocked("native_public_source_slot_unavailable")
        selected = inputs.sources[slot]
        if (selected.kind != "public_https_text" or selected.first_line != payload["first_line"]
            or selected.last_line != payload["last_line"]):
            raise NativeServiceBlocked("native_public_source_scope_changed")
        source = checkpoint(row, f"research:artifact:source:{slot}")
        intent_id = f"research:source-intent:{slot}"
        intent = checkpoint(row, intent_id)
        if (source is None or intent is None or payload["acquisition_receipt_ref"] != intent_id
            or source.get("kind") != "source" or source.get("job_id") != job_id
            or source.get("no_learning") is not True or intent.get("kind") != "public_https_text"
            or intent.get("selection_digest") != hashlib.sha256(json.dumps(selected.model_dump(), sort_keys=True).encode()).hexdigest()):
            raise NativeServiceBlocked("native_public_acquisition_receipt_missing")
        records = [item for item in row["artifacts"] if item.get("artifact_id") == payload["artifact_ref"]]
        if (len(records) != 1 or records[0].get("file_path") != source["file_path"]
            or records[0].get("content_sha256") != source["content_sha256"]
            or not any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == source["file_path"]
                and effect.get("content_sha256") == source["content_sha256"] for effect in row["effects"])):
            raise NativeServiceBlocked("native_public_artifact_readback_missing")
        # Calling the actual acquisition owner is safe ONLY after exact settled
        # acquisition exists. Its existing-output path resolves current native
        # inputs and bytes; it performs no second GET or materialization.
        acquired = await acquire_source(self.jobs, child_id=job_id,
            owner=original_scope.witness["lease_owner"], fence=original_scope.witness["fencing_token"], source_slot=slot)
        if acquired is None or acquired[1] != source:
            raise NativeServiceBlocked("native_public_acquisition_changed")
        raw = read(source["file_path"], source["content_sha256"], max_bytes=SOURCE_BYTES)
        if len(raw) != source["byte_count"]:
            raise NativeServiceBlocked("native_public_source_bytes_changed")
        evidence = normalized_source(raw, source_slot=slot, first_line=selected.first_line, last_line=selected.last_line)
        async with self.jobs._session() as db:
            _, _, binding = await self._current_in_db(db, job_id, frame["method"], original_scope)
        research_owner = next(entry for entry in binding.dependency_vector if entry.runtime_domain == "seraph.research.v1")
        return {"artifact_ref": payload["artifact_ref"], "input_digest": source["content_sha256"],
            "provider_digest": research_owner.composition_digest,
            "config_digest": _digest({"acquisition": intent, "normalization": NORMALIZATION,
                "source_bytes": SOURCE_BYTES, "first_line": selected.first_line, "last_line": selected.last_line}),
            "evidence": [{"source_ref": evidence["source_id"], "text": evidence["quoted_text"]}]}
