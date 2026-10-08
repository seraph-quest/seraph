"""One original live native read's finite private artifact and audit owners."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

from .dispatch import NativeServiceBlocked

ARTIFACT_METHODS = ("artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append")
ARTIFACT_TYPE = "native_service_read"
ARTIFACT_PREFIX = "artifacts/work-board/runtime-service-read/"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def native_read_artifact_bytes(run):
    from .read_journal import read_context
    context = read_context(run)
    if context["result"] is None:
        raise NativeServiceBlocked("native_artifact_source_unavailable")
    content = _canonical(context["result"]["payload"]).encode("utf-8")
    if not 1 <= len(content) <= 65536:
        raise NativeServiceBlocked("native_artifact_source_unsupported")
    return content


def native_read_artifact_path(run, content_digest):
    from .contracts import sha
    sha(content_digest)
    key = hashlib.sha256(run.run_identity.encode("utf-8")).hexdigest()
    return ARTIFACT_PREFIX + key + "-" + content_digest + ".json"


def artifact_trust_request(run, *, content_digest, request_ref, principal):
    """The existing trust owner decides the actual original local transfer."""
    from .read_journal import artifact_profile
    from src.security.trust_contract import (
        TrustPrincipal, TrustRequest, TrustDestination, TrustResource, TrustProvenance,
        TrustOperation, AuthorityGrant, ContentOrigin, EgressClass, DestinationClass,
        NO_OBJECT, NO_SECRET_SCOPE, NO_TRANSFORMATION, authority_scope_digest, canonical_digest,
    )
    if type(principal) is not TrustPrincipal:
        raise NativeServiceBlocked("native_artifact_original_principal_unavailable")
    principal = replace(principal, session_id=run.session_id,
        operator_session_id=run.operator_session_id, job_id=run.run_identity)
    destination = TrustDestination("native-read-artifact", DestinationClass.LOCAL_RUNTIME)
    resource = TrustResource("artifact", "native-read-result", NO_OBJECT)
    deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or timezone.utc).timestamp()
    return TrustRequest(principal=principal,
        provenance=(TrustProvenance(ContentOrigin.SERAPH_CONTROL, run.run_identity,
            content_digest, EgressClass.LOCAL_ONLY, False),), destination=destination,
        operation=TrustOperation.ARTIFACT_TRANSFER, required_grant=AuthorityGrant.ARTIFACT_TRANSFER,
        capability_id="runtime_service_read_v1", capability_version="1", data_digest=content_digest,
        secret_scope_digest=NO_SECRET_SCOPE, resource_limits_digest=canonical_digest(artifact_profile()),
        transformation_digest=NO_TRANSFORMATION,
        authority_scope_digest=authority_scope_digest(required_grant=AuthorityGrant.ARTIFACT_TRANSFER,
            capability_id="runtime_service_read_v1", destination=destination, resource=resource),
        resource=resource, session_id=run.session_id, job_id=run.run_identity, request_id=request_ref,
        attempt_id="native-read-attempt:" + str(getattr(run, "attempt_count", 0)),
        replay_id="native-read-original", decision_expires_at=deadline,
        egress_class=EgressClass.LOCAL_ONLY)


def _read_current_bytes(run):
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.work_board.repository import BoardError
    content = native_read_artifact_bytes(run)
    digest = hashlib.sha256(content).hexdigest()
    reference = native_read_artifact_path(run, digest)
    try:
        actual = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / reference,
            expected_digest=digest, expected_size=len(content))
    except (BoardError, OSError) as exc:
        raise NativeServiceBlocked("native_artifact_readback_unproven") from exc
    if actual != content:
        raise NativeServiceBlocked("native_artifact_readback_changed")
    return reference, actual, digest


def _assert_call_current(frame, original_scope):
    from .bridge import cordis_host
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if (frame["deadline_at"] <= now or frame["deadline_at"] > original_scope.deadline_at
        or not cordis_host.admitting or cordis_host.boot_nonce != original_scope.host_boot_nonce
        or cordis_host.reviewed is None
        or cordis_host.reviewed.package_digest != original_scope.witness["package_digest"]
        or cordis_host.reviewed.composition_digest != original_scope.witness["host_composition_digest"]):
        raise NativeServiceBlocked("native_artifact_original_call_unavailable")


def _audit_details(context):
    candidate = context["operations"][3]["candidate"]
    return {key: candidate[key] for key in ("original_read_method", "sealed_result_digest",
        "artifact_ref", "content_digest", "artifact_readback_ref", "artifact_readback_digest",
        "profile_digest", "no_learning")}


async def assert_artifact_owner_readback(db, run, context):
    """Completion independently rereads the bounded file and actual audit row."""
    from src.db.models import AuditEvent
    from .read_journal import audit_event_body
    reference, content, digest = _read_current_bytes(run)
    canonical = json.loads(run.artifact_receipts_json or "[]")
    effects = json.loads(run.effect_receipts_json or "[]")
    slots = context["operations"]
    for slot in slots[:3]:
        record, effect = slot["artifact_record"], slot["effect_receipt"]
        if (record["file_path"] != reference or record["content_sha256"] != digest
            or record["size_bytes"] != len(content) or record.get("exists") is not True
            or not any(row.get("artifact_id") == record["artifact_id"]
                and row.get("file_path") == reference and row.get("content_sha256") == digest for row in canonical)
            or effect not in effects or effect["target_path"] != reference
            or effect["fencing_token"] != run.fencing_token):
            raise NativeServiceBlocked("native_artifact_owner_readback_unproven")
    slot = slots[3]
    event = await db.get(AuditEvent, slot["audit_event_ref"], populate_existing=True)
    if (event is None or event.session_id != run.session_id or event.actor != "runtime"
        or event.event_type != "runtime_service_observed" or event.summary != "Native service read verified"
        or json.loads(event.details_json or "null") != _audit_details(context)
        or _digest(audit_event_body(event)) != slot["audit_event_digest"]):
        raise NativeServiceBlocked("native_artifact_audit_readback_unproven")


async def dispatch_artifact_operation(dispatcher, frame, payload, original_scope):
    """Persist the exact call intent before any physical owner operation."""
    from .read_journal import (operation_for_wire, start_operation, validate_operation,
        validate_read_policy, seal_operation)
    from .contracts import succeeded
    from src.auth.service import authenticate_principal
    from src.security.trust_contract import evaluate_trust
    from src.work_board.repository import BoardError
    from src.work_board.input_artifacts import _write_payload
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    method, call_ref = frame["method"], frame["request_id"]
    try:
        async with dispatcher.jobs._session() as db:
            run, _, _ = await dispatcher._current_in_db(db, frame["invocation_ref"], method, original_scope)
            await validate_read_policy(db, run)
            _assert_call_current(frame, original_scope)
            operation = operation_for_wire(run, method, payload)
            start_operation(db, run, operation, call_ref=call_ref)
            if method == "artifacts.stage":
                candidate = operation.candidate()
                await dispatcher.jobs.record_native_read_effect_in_session(db, run,
                    operation=operation, call_ref=call_ref, effect_type="native_read_artifact",
                    effect_id=candidate["request_ref"], status="intent",
                    target_path=native_read_artifact_path(run, candidate["content_digest"]),
                    content_sha256=candidate["content_digest"])
            await db.flush()
        async with dispatcher.jobs._session() as db:
            run, _, _ = await dispatcher._current_in_db(db, frame["invocation_ref"], method, original_scope)
            await validate_read_policy(db, run)
            context = validate_operation(run, operation, call_ref=call_ref)
            _assert_call_current(frame, original_scope)
            candidate = operation.candidate()
            if method == "audit.append":
                from src.audit.repository import audit_repository
                # Verify the preceding whole-file read before writing one real event.
                _read_current_bytes(run)
                event = await audit_repository._log_event_in_session(db,
                    event_id=candidate["event_ref"], session_id=run.session_id, actor="runtime",
                    event_type="runtime_service_observed", summary="Native service read verified",
                    details=_audit_details(context), flush=False)
                result = succeeded(method, {"receipt_ref": candidate["event_ref"], "revision": run.revision + 1})
                seal_operation(db, run, operation, call_ref=call_ref, result=result, audit_event=event)
            else:
                content = native_read_artifact_bytes(run)
                reference = native_read_artifact_path(run, candidate["content_digest"])
                current = await authenticate_principal(run.owner_principal_id, db=db)
                request = artifact_trust_request(run, content_digest=candidate["content_digest"],
                    request_ref=candidate.get("request_ref") or candidate["artifact_ref"], principal=current.principal)
                decision = evaluate_trust(request)
                if not decision.allowed:
                    raise NativeServiceBlocked("native_artifact_transfer_denied")
                if method == "artifacts.stage":
                    _write_payload(canonical_workspace_root(settings.workspace_dir) / reference, content)
                reference, actual, digest = _read_current_bytes(run)
                record = await dispatcher.jobs.record_native_read_artifact_in_session(db, run,
                    operation=operation, call_ref=call_ref, file_path=reference, content=actual,
                    trust_request=request, trust_decision=decision)
                effect_id = candidate.get("request_ref") or ("native-read-op:" + hashlib.sha256(
                    (run.run_identity + ":" + method).encode()).hexdigest())
                effect = await dispatcher.jobs.record_native_read_effect_in_session(db, run,
                    operation=operation, call_ref=call_ref, effect_type="native_read_artifact",
                    effect_id=effect_id, status="succeeded", receipt_kind="readback",
                    target_path=reference, content_sha256=digest,
                    readback_id=effect_id + ":readback", details={"no_learning": True})
                value = {"artifact_ref": record["artifact_id"], "digest": digest, "size_bytes": len(actual)}
                if method == "artifacts.adopt":
                    value["receipt_ref"] = candidate["request_ref"]
                elif method == "artifacts.read":
                    value["content"] = actual.decode("utf-8")
                result = succeeded(method, value)
                seal_operation(db, run, operation, call_ref=call_ref, result=result,
                    artifact_record=record, effect_receipt=effect)
            _assert_call_current(frame, original_scope)
            await db.flush()
            _assert_call_current(frame, original_scope)
            return result
    except (OSError, BoardError, UnicodeError) as exc:
        # The committed original intent remains unknown; a new RPC cannot replay it.
        raise NativeServiceBlocked("native_artifact_owner_operation_unproven") from exc
