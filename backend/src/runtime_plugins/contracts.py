"""Finite service wire projections. References never carry authority or paths."""
from __future__ import annotations

import re
from typing import Any

from .protocol import ProtocolError, closed, integer

REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
JOB_STATES = frozenset({"accepted", "queued", "awaiting_approval", "running", "paused", "blocked", "succeeded", "degraded", "failed", "cancelled", "unknown_external_effect", "cost_liability"})
METHOD_DOMAINS = {
    **{f"{domain}.{method}": f"seraph.{domain}.v1" for domain, methods in (
        ("authority", ("resolve",)), ("goals", ("read",)),
        ("tasks", ("admit", "inspect", "cancel", "checkpoint", "settle")),
        ("capabilities", ("list", "describe", "invoke")), ("inference", ("request",)),
        ("memory", ("retrieve", "propose", "applyReviewed", "forget")),
        ("artifacts", ("read", "stage", "adopt")), ("audit", ("append",)),
        ("research", ("buildPlan", "executeAccepted")),
        ("conversation", ("accept", "append", "read", "cancel")),
        ("scheduler", ("register", "disable", "dispatchDue")),
        ("connections", ("inspect", "invokeBoundAdapter")),
        ("agent-loop", ("startTurn", "cancelTurn", "inspectTurn")),
        ("source-extraction", ("extract",)),
    ) for method in methods}
}
SERVICE_METHODS = frozenset(METHOD_DOMAINS)


def ref(value: Any) -> None:
    if type(value) is not str or REF.fullmatch(value) is None:
        raise ProtocolError("invalid canonical reference")


def sha(value: Any) -> None:
    if type(value) is not str or SHA.fullmatch(value) is None:
        raise ProtocolError("invalid digest")


def text(value: Any, bound: int) -> None:
    try:
        valid = type(value) is str and len(value.encode("utf-8")) <= bound
    except UnicodeError:
        valid = False
    if not valid:
        raise ProtocolError("invalid bounded text")


def enum(value: Any, choices) -> None:
    if type(value) is not str or value not in choices:
        raise ProtocolError("invalid finite value")


def boolean(value: Any) -> None:
    if type(value) is not bool:
        raise ProtocolError("invalid boolean")


def nullable_ref(value: Any) -> None:
    if value is not None:
        ref(value)


def refs(value: Any, bound: int) -> None:
    if type(value) is not list or len(value) > bound:
        raise ProtocolError("invalid reference inventory")
    for item in value:
        ref(item)


def object_fields(value, fields):
    closed(value, set(fields))
    for key, validate in fields.items():
        validate(value[key])


R = ref
I = lambda v: integer(v, 1)
Z = lambda v: integer(v, 0)
STATE = lambda v: enum(v, JOB_STATES)
CAP = {"capability_id": R, "version": R, "state": lambda v: enum(v, {"available", "blocked"}), "reason_code": nullable_ref}
JOB = {"job_ref": R, "revision": I, "state": STATE}
ARTIFACT = {"artifact_ref": R, "digest": sha, "size_bytes": lambda v: integer(v, 0, 65536)}

REQUESTS = {
    "authority.resolve": {}, "goals.read": {},
    "tasks.admit": {"request_ref": R}, "tasks.inspect": {"job_ref": R},
    "tasks.cancel": {"job_ref": R, "expected_revision": Z},
    "tasks.checkpoint": {"checkpoint_ref": R}, "tasks.settle": {"outcome_ref": R},
    "capabilities.list": {"cursor": nullable_ref, "limit": lambda v: integer(v, 1, 50)},
    "capabilities.describe": {"capability_id": R}, "capabilities.invoke": {"request_ref": R},
    "inference.request": {"request_ref": R},
    "memory.retrieve": {"query_ref": R, "limit": lambda v: integer(v, 1, 20)},
    "memory.propose": {"request_ref": R}, "memory.applyReviewed": {"review_ref": R}, "memory.forget": {"request_ref": R},
    "artifacts.read": {"artifact_ref": R, "max_bytes": lambda v: integer(v, 1, 65536)},
    "artifacts.stage": {"request_ref": R}, "artifacts.adopt": {"request_ref": R},
    "audit.append": {"event_ref": R}, "research.buildPlan": {"task_ref": R}, "research.executeAccepted": {"plan_ref": R},
    "conversation.accept": {"turn_ref": R}, "conversation.append": {"message_ref": R},
    "conversation.read": {"conversation_ref": R, "limit": lambda v: integer(v, 1, 100), "before_message_ref": nullable_ref},
    "conversation.cancel": {"turn_ref": R, "expected_revision": Z},
    "scheduler.register": {"request_ref": R}, "scheduler.disable": {"schedule_ref": R, "expected_revision": I},
    "scheduler.dispatchDue": {"limit": lambda v: integer(v, 1, 20)},
    "connections.inspect": {"connection_ref": R}, "connections.invokeBoundAdapter": {"operation_ref": R},
    "agent-loop.startTurn": {"turn_ref": R}, "agent-loop.cancelTurn": {"turn_ref": R, "expected_revision": Z},
    "agent-loop.inspectTurn": {"turn_ref": R},
    "source-extraction.extract": {"artifact_ref": R, "acquisition_receipt_ref": R,
        "source_slot": lambda v: integer(v, 0, 3), "first_line": lambda v: integer(v, 1, 4096), "last_line": lambda v: integer(v, 1, 4096)},
}


def rows(value, bound, fields):
    if type(value) is not list or len(value) > bound:
        raise ProtocolError("invalid bounded inventory")
    for item in value:
        object_fields(item, fields)


RESULTS = {
    "authority.resolve": {"authority_ref": R, "revision": I, "mode": lambda v: enum(v, {"operator-root", "goal-programme"})},
    "goals.read": {"goal_ref": R, "revision": I, "title": lambda v: text(v, 200), "description": lambda v: text(v, 8192)},
    "tasks.admit": {**JOB, "replayed": boolean}, "tasks.inspect": {**JOB, "artifact_refs": lambda v: refs(v, 16)},
    "tasks.cancel": JOB, "tasks.checkpoint": {"receipt_ref": R, "revision": I}, "tasks.settle": JOB,
    "capabilities.list": {"capabilities": lambda v: rows(v, 50, CAP), "next_cursor": nullable_ref}, "capabilities.describe": CAP,
    "capabilities.invoke": {"receipt_ref": R, "artifact_refs": lambda v: refs(v, 16)},
    "inference.request": {"receipt_ref": R, "output_ref": R},
    "memory.retrieve": {"records": lambda v: rows(v, 20, {"record_ref": R, "revision": I, "text": lambda s: text(s, 8192)})},
    "memory.propose": {"proposal_ref": R, "revision": I, "state": lambda v: enum(v, {"proposed", "blocked"})},
    "memory.applyReviewed": {"record_ref": R, "revision": I}, "memory.forget": {"record_ref": R, "revision": I},
    "artifacts.read": {**ARTIFACT, "content": lambda v: text(v, 65536)}, "artifacts.stage": ARTIFACT,
    "artifacts.adopt": {**ARTIFACT, "receipt_ref": R}, "audit.append": {"receipt_ref": R, "revision": I},
    "research.buildPlan": {"plan_ref": R, "revision": I}, "research.executeAccepted": JOB,
    "conversation.accept": {"turn_ref": R, "job_ref": R, "replayed": boolean},
    "conversation.append": {"message_ref": R, "revision": I},
    "conversation.read": {"messages": lambda v: rows(v, 100, {"message_ref": R, "role": lambda s: enum(s, {"user", "assistant", "step", "error"}), "content": lambda s: text(s, 8192)}), "next_cursor": nullable_ref},
    "conversation.cancel": JOB,
    "scheduler.register": {"schedule_ref": R, "revision": I, "state": lambda v: enum(v, {"active", "disabled", "blocked"})},
    "scheduler.disable": {"schedule_ref": R, "revision": I, "state": lambda v: enum(v, {"active", "disabled", "blocked"})},
    "scheduler.dispatchDue": {"job_refs": lambda v: refs(v, 20)},
    "connections.inspect": {"connection_ref": R, "revision": I, "state": lambda v: enum(v, {"ready", "blocked", "revoked"}), "reason_code": nullable_ref},
    "connections.invokeBoundAdapter": {"receipt_ref": R, "artifact_refs": lambda v: refs(v, 16)},
    "agent-loop.startTurn": JOB, "agent-loop.cancelTurn": JOB,
    "agent-loop.inspectTurn": {**JOB, "artifact_refs": lambda v: refs(v, 16)},
    "source-extraction.extract": {"artifact_ref": R, "input_digest": sha, "provider_digest": sha, "config_digest": sha,
        "evidence": lambda v: rows(v, 64, {"source_ref": R, "text": lambda s: text(s, 4096)})},
}
MEMORY_STATUS = {"memory.propose": "proposal_only", "memory.applyReviewed": "reviewed_update", "memory.forget": "forgotten"}


def validate_request(method: str, payload: Any) -> dict:
    if method not in REQUESTS:
        raise ProtocolError("unknown service method")
    object_fields(payload, REQUESTS[method])
    if method == "source-extraction.extract" and payload["last_line"] < payload["first_line"]:
        raise ProtocolError("invalid source span")
    return payload


def validate_result(method: str, payload: Any) -> dict:
    if method not in RESULTS or type(payload) is not dict:
        raise ProtocolError("unknown service result")
    if payload.get("status") == "blocked":
        object_fields(payload, {"status": lambda v: enum(v, {"blocked"}), "reason_code": R,
            "memory_status": lambda v: enum(v, {"no_learning"})})
    else:
        object_fields(payload, {"status": lambda v: enum(v, {"succeeded"}),
            "value": lambda v: object_fields(v, RESULTS[method]),
            "memory_status": lambda v: enum(v, {MEMORY_STATUS.get(method, "no_learning")})})
    return payload


def blocked(code: str) -> dict:
    ref(code)
    return {"status": "blocked", "reason_code": code, "memory_status": "no_learning"}


def succeeded(method: str, value: dict) -> dict:
    return validate_result(method, {"status": "succeeded", "value": value,
        "memory_status": MEMORY_STATUS.get(method, "no_learning")})
