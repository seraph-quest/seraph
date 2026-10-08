"""Native plain-turn admission; the existing Python loops retain execution ownership."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import asyncio
import math
from threading import Event

from config.settings import settings
from src.models.schemas import ChatIngressEnvelope
from src.runtime_plugins.composition import ReviewedComposition
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


TURN_KIND = "conversation_turn_v1"
TURN_VERSION = "conversation-turn.v1"


class NativeTurnBlocked(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise NativeTurnBlocked("native_turn_original_time_invalid")
    return value.astimezone(timezone.utc)


def _policy_digest() -> str:
    from src.approval.runtime import get_current_approval_mode
    from src.tools.policy import get_current_mcp_policy_mode, get_current_tool_policy_mode
    return _digest({"schema_version": 1, "agent_chat_timeout": settings.agent_chat_timeout,
        "approval_mode": get_current_approval_mode(), "tool_policy_mode": get_current_tool_policy_mode(),
        "mcp_policy_mode": get_current_mcp_policy_mode()})


@dataclass(frozen=True)
class NativeTurnAdmission:
    """Server-only immutable input captured before the original ingress writer.

    A reviewed package is not a permission grant. The native writer rechecks
    the original operator and conversation and derives the composition vector.
    """
    ingress_json: str
    principal: TrustPrincipal
    reviewed_composition: ReviewedComposition
    native_route: str
    native_timeout_seconds: int
    deadline_at: datetime
    policy_config_digest: str

    @classmethod
    def capture(cls, ingress: ChatIngressEnvelope, *, principal: TrustPrincipal,
                reviewed_composition: ReviewedComposition, native_route: str):
        timeout = settings.agent_chat_timeout
        if type(timeout) is not int or timeout <= 0:
            raise NativeTurnBlocked("native_turn_timeout_invalid")
        if native_route not in {"direct_turn", "generic_turn"}:
            raise NativeTurnBlocked("native_turn_route_unsupported")
        if ingress.attachment_refs:
            raise NativeTurnBlocked("native_turn_attachment_extension_unreviewed")
        if type(reviewed_composition) is not ReviewedComposition:
            raise NativeTurnBlocked("native_turn_original_host_unavailable")
        if (not principal.authenticated or principal.revoked
            or principal.principal_type != PrincipalType.OPERATOR
            or principal.principal_id != ingress.principal_id
            or principal.session_id != ingress.session_id
            or principal.operator_session_id != ingress.operator_session_id
            or AuthorityGrant.MODEL_INFERENCE not in principal.grants):
            raise NativeTurnBlocked("native_turn_principal_unavailable")
        timeout = min(timeout, 60) if native_route == "direct_turn" else timeout
        try:
            deadline = _utc(ingress.received_at) + timedelta(seconds=timeout)
        except OverflowError as exc:
            raise NativeTurnBlocked("native_turn_timeout_invalid") from exc
        return cls(ingress.model_dump_json(), principal, reviewed_composition,
                   native_route, timeout, deadline, _policy_digest())

    @property
    def ingress(self) -> ChatIngressEnvelope:
        return ChatIngressEnvelope.model_validate_json(self.ingress_json)

    @property
    def job_id(self) -> str:
        return "conversation-turn:" + self.ingress.message_id

    @property
    def inputs(self) -> dict:
        ingress = self.ingress
        return {"schema_version": 1, "message_ref": ingress.message_id,
                "content_digest": ingress.content_digest, "native_route": self.native_route,
                "native_timeout_seconds": self.native_timeout_seconds}


async def validate_native_turn_owner(db, admission: NativeTurnAdmission) -> None:
    """Recheck current canonical authority in the original serialized writer."""
    from sqlalchemy import select
    from src.auth.service import authenticate_principal
    from src.db.models import OperatorSession, Session
    ingress = admission.ingress
    now = datetime.now(timezone.utc)
    root = await db.scalar(select(OperatorSession).where(
        OperatorSession.id == ingress.operator_session_id,
        OperatorSession.principal_id == ingress.principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now)
        .execution_options(populate_existing=True))
    if root is None:
        raise NativeTurnBlocked("native_turn_original_root_inactive")
    await authenticate_principal(ingress.principal_id, db=db)
    conversation = await db.get(Session, ingress.session_id, populate_existing=True)
    if (conversation is None or conversation.owner_principal_id != ingress.principal_id
        or ingress.conversation_id != conversation.id or ingress.thread_id != conversation.id):
        raise NativeTurnBlocked("native_turn_conversation_owner_changed")
    if conversation.continuity_task_id is not None:
        raise NativeTurnBlocked("native_turn_continuity_context_unsupported")
    if admission.deadline_at <= now:
        raise NativeTurnBlocked("native_turn_original_deadline_expired")
    if _policy_digest() != admission.policy_config_digest:
        raise NativeTurnBlocked("native_turn_original_policy_changed")


async def native_turn_spec(db, admission: NativeTurnAdmission):
    from src.runtime_plugins.ownership import bind_invocation
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
    ingress = admission.ingress
    binding = await bind_invocation(db, method="conversation.accept",
        native_branch=admission.native_route, reviewed_composition=admission.reviewed_composition)
    identity = ingress.model_dump(mode="json")
    identity.pop("received_at")
    fingerprint = _digest({"schema_version": 1, "ingress": identity,
        "job_id": admission.job_id, "original_root_id": ingress.operator_session_id,
        "goal_id": None, "goal_revision": None, "native_route": admission.native_route,
        "native_timeout_seconds": admission.native_timeout_seconds,
        "deadline_at": admission.deadline_at.isoformat(), "policy_config_digest": admission.policy_config_digest,
        "composition_binding_digest": binding.binding_digest})
    return DurableJobSpec(identity=DurableJobIdentity(job_id=admission.job_id,
        owner_kind="user", owner_principal_id=ingress.principal_id, job_kind=TURN_KIND,
        capability_version=TURN_VERSION, idempotency_scope="conversation-turn",
        idempotency_key=ingress.idempotency_key_digest), inputs=admission.inputs,
        session_id=ingress.operator_session_id, operator_session_id=ingress.operator_session_id,
        conversation_id=ingress.conversation_id, composition_binding=binding,
        declared_authority={"principal": ingress.principal_id, "owner_kind": "user",
            "session_id": ingress.operator_session_id, "conversation_id": ingress.conversation_id,
            "native_policy_digest": admission.policy_config_digest,
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in admission.principal.grants)},
        deadline_at=admission.deadline_at, max_attempts=1, run_fingerprint=fingerprint)


def available_turn_host(ingress, content):
    """Optional fixed host; extensions retain their existing unbound ingress."""
    from src.runtime_plugins.bridge import cordis_host
    if (ingress.attachment_refs or len(content.encode()) > 65536 or not cordis_host.admitting
        or cordis_host.service_dispatch is None or cordis_host.reviewed is None
        or cordis_host.process is None or cordis_host.process.returncode is not None):
        return None
    return cordis_host


async def native_turn_binding_available(admission):
    from src.db.engine import get_session
    from src.runtime_plugins.ownership import CompositionBindingError, inspect_invocation_availability
    try:
        async with get_session() as db:
            status = await inspect_invocation_availability(db, method="conversation.accept",
                native_branch=admission.native_route,
                reviewed_composition=admission.reviewed_composition)
            from src.db.models import Session
            conversation = await db.get(Session, admission.ingress.session_id)
            # Existing Task-linked context is not part of the reviewed native closure.
            # Inspect inventory first so damaged retained state never becomes fallback.
            if conversation is not None and conversation.continuity_task_id is not None:
                return False
    except CompositionBindingError as exc:
        raise NativeTurnBlocked(exc.reason_code) from exc
    if not status.available and status.reason_code != "composition_inventory_absent":
        raise NativeTurnBlocked("native_turn_binding_unavailable")
    return status.available


async def validate_native_turn_claim(db, run, admission):
    from src.db.models import Message
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    await validate_native_turn_owner(db, admission)
    ingress = admission.ingress
    binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
    stored_deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or timezone.utc)
    if (run.run_identity != admission.job_id or run.job_kind != TURN_KIND
        or run.capability_version != TURN_VERSION or run.owner_kind != "user"
        or run.owner_principal_id != ingress.principal_id
        or run.operator_session_id != ingress.operator_session_id
        or run.session_id != ingress.operator_session_id
        or run.conversation_id != ingress.conversation_id
        or stored_deadline != admission.deadline_at
        or json.loads(run.arguments_json) != admission.inputs
        or binding.origin_method != "conversation.accept"
        or binding.native_branch != admission.native_route
        or binding.host_package_digest != admission.reviewed_composition.package_digest
        or binding.host_composition_digest != admission.reviewed_composition.composition_digest):
        raise NativeTurnBlocked("native_turn_original_claim_changed")
    message = await db.get(Message, ingress.message_id, populate_existing=True)
    if (message is None or message.role != "user"
        or message.session_id != ingress.session_id
        or message.owner_principal_id != ingress.principal_id
        or message.operator_session_id != ingress.operator_session_id
        or message.conversation_id != ingress.conversation_id
        or hashlib.sha256(message.content.encode()).hexdigest() != ingress.content_digest):
        raise NativeTurnBlocked("native_turn_original_input_changed")


class _TurnGuard:
    def __init__(self, stop, root):
        self.stop, self.root = stop, root

    def is_set(self):
        return self.stop.is_set() or (self.root is not None and self.root.is_set())


@dataclass
class NativeTurnExecution:
    admission: NativeTurnAdmission
    host: object
    claim: object
    scope: object
    transport_closed: bool = False
    worker: object = None
    stop: Event = field(default_factory=Event)

    def prepare_agent(self, agent):
        from src.agent.controlled_origin import install_controlled_callback
        from src.agent.native_turn_family import prepare_generic_family
        install_controlled_callback(self, agent)
        prepare_generic_family(self, agent)

    def run_callback(self, callback, *args):
        from src.agent.controlled_origin import original_execution_context
        from src.agent.native_turn_family import original_generic_callback
        with original_execution_context(self):
            with original_generic_callback(self, callback, args):
                return callback(*args)

    async def initialize_family(self):
        from src.agent.native_turn_family import prepare_direct_family
        from src.workflows.job_runtime import durable_job_repository as jobs
        if getattr(self, "_family_initialized", False):
            raise NativeTurnBlocked("native_turn_family_already_initialized")
        if self.admission.native_route == "direct_turn":
            prepare_direct_family(self)
        async with jobs._writer_session() as db:
            await jobs.initialize_native_turn_family_in_session(db, native_execution=self)
        self._family_initialized = True

    def guard(self, root=None):
        return _TurnGuard(self.stop, root)

    def close_transport(self):
        self.transport_closed = True
        self.stop.set()

    def remaining(self):
        remaining = (self.admission.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0 or self.transport_closed:
            raise asyncio.TimeoutError("original native turn deadline expired")
        return remaining

    async def authority_check(self, db, run):
        await validate_native_turn_claim(db, run, self.admission)

    def validate_completed_exception(self, exception):
        from src.agent.exceptions import ClarificationRequired
        from src.approval.exceptions import ApprovalRequired
        self.remaining()
        if (not isinstance(exception, (ClarificationRequired, ApprovalRequired))
            or self.worker is None or not self.worker.done() or self.worker.cancelled()):
            raise NativeTurnBlocked("native_turn_controlled_completion_unproven")
        actual_exception = self.worker.exception()
        # REST propagates the original callback exception. The existing WS
        # queue worker returns that same caught object after its finally path.
        observed = actual_exception if actual_exception is not None else self.worker.result()
        if observed is not exception:
            raise NativeTurnBlocked("native_turn_controlled_exception_changed")

    async def forward(self, method, payload):
        from src.runtime_plugins.bridge import HostBlocked
        try:
            self.remaining()
        except asyncio.TimeoutError as exc:
            raise NativeTurnBlocked("native_turn_original_deadline_expired") from exc
        try:
            result = await self.host.request_service(method, payload, original_scope=self.scope)
        except HostBlocked as exc:
            raise NativeTurnBlocked("native_turn_host_unavailable") from exc
        if result.get("status") != "succeeded":
            raise NativeTurnBlocked(result.get("reason_code", "native_turn_forward_blocked"))
        return result

    async def execute(self, awaitable):
        # Shield the existing Python callback, not a second execution lane.
        # Timeout/cancel is not positive thread completion. No late output is
        # adopted because only the still-live transport calls protected append.
        try:
            timeout = self.remaining()
        except BaseException:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise
        from src.agent.controlled_origin import original_execution_context
        try:
            await self.initialize_family()
        except BaseException:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise
        with original_execution_context(self):
            task = asyncio.ensure_future(awaitable)
        self.worker = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except Exception as exc:
            from src.agent.exceptions import ClarificationRequired
            from src.approval.exceptions import ApprovalRequired
            if isinstance(exc, (ClarificationRequired, ApprovalRequired)):
                raise
            self.close_transport()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            from src.agent.native_turn_family import original_family_failure
            failure = original_family_failure(exc, self)
            if failure is not exc:
                raise failure
            raise
        except BaseException:
            self.close_transport()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise


async def claim_native_turn(admission, host, job):
    from src.workflows.job_runtime import durable_job_repository as jobs
    from src.runtime_plugins.dispatch import capture_original_scope
    async def authority_check(db, run):
        await validate_native_turn_claim(db, run, admission)
    remaining = (admission.deadline_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise NativeTurnBlocked("native_turn_original_deadline_expired")
    queued = await jobs.queue_job(admission.job_id, expected_state="accepted",
        expected_revision=job["revision"])
    claim = await jobs.claim_service_job(admission.job_id, host=host,
        owner="native-chat:" + admission.ingress.message_id,
        lease_seconds=max(1, math.ceil(remaining)), expected_revision=queued["revision"],
        claim_authority_check=authority_check)
    runtime = NativeTurnExecution(admission, host, claim, capture_original_scope(claim, host))
    await runtime.forward("conversation.accept", {"turn_ref": admission.job_id})
    await runtime.forward("agent-loop.startTurn", {"turn_ref": admission.job_id})
    return runtime
