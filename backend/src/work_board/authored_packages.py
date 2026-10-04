"""Derived package metadata staged outside writers; never a second registry."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re
from functools import wraps
import inspect

from src.extensions.authored_adapter import load_adapter, read_member, sha256
from src.extensions.capability_pack import CapabilityPackLifecycle, capability_pack_digest, parse_capability_pack_manifest

_STAGED = ContextVar("authored_package_stage", default={})

class AuthoredIntegrityFailure(ValueError):
    def __init__(self, *, pack_id, expected_digest, record_sha256, review_sha256, observed_digest):
        super().__init__("authored_package_observed_integrity_failure")
        self.binding=dict(pack_id=pack_id,expected_digest=expected_digest,record_sha256=record_sha256,
            review_sha256=review_sha256,observed_digest=observed_digest)


@contextmanager
def integrity_fence(lifecycle):
    """Publish observed integrity denial only after releasing the stage lock."""
    try:
        with lifecycle._state_lock(shared=True):yield
    except AuthoredIntegrityFailure as failure:
        lifecycle.quarantine_authored_observation(**failure.binding)
        raise


def is_authored(capability_id):
    return type(capability_id) is str and bool(re.fullmatch(r"pack\.[a-z][a-z0-9.-]{0,79}\.[a-z][a-z0-9-]{0,31}\.v1", capability_id))


def is_tool_package(capability_id):
    return capability_id == "work.json-format.v1" or is_authored(capability_id)


@dataclass(frozen=True)
class Registration:
    adapter: object
    pointer: dict
    manifest: object

    @property
    def pin(self):
        return {key: self.pointer[key] for key in ("pack_id", "version", "digest", "goal_id", "goal_revision", "review_id",
                "authority_digest", "dependencies_digest", "owner_principal_id", "session_id")}


def load_registration(capability_id, *, lifecycle=None, state=None, original_pin=None, continuation=False):
    """Caller holds the lifecycle lock when supplying state; performs physical I/O."""
    lifecycle = lifecycle or CapabilityPackLifecycle()
    if state is None:
        with integrity_fence(lifecycle):
            return load_registration(capability_id, lifecycle=lifecycle, state=lifecycle._load(),
                                     original_pin=original_pin, continuation=continuation)
    if not is_authored(capability_id):
        raise ValueError("authored_capability_unregistered")
    package_id = capability_id[5:].rsplit(".", 2)[0]
    pointer = state.get("active", {}).get(package_id)
    if not isinstance(pointer, dict) or pointer.get("status") != "active":
        raise ValueError("authored_package_inactive")
    selected = dict(pointer)
    if original_pin is not None and selected.get("digest") != original_pin.get("digest"):
        if not continuation:
            raise ValueError("authored_package_queued_version_stale")
        record = state.get("versions", {}).get(package_id, {}).get(original_pin.get("digest"))
        if not isinstance(record, dict) or record.get("revoked"):
            raise ValueError("authored_package_original_version_revoked")
        # Only the native caller with a persisted released-process fence may
        # select this typed continuation. No new process uses this branch.
        selected = {**original_pin, "root_path": record["root_path"], "status": "active"}
    from src.extensions.capability_pack import canonical_digest
    record=state.get("versions",{}).get(package_id,{}).get(selected.get("digest"))
    review=state.get("reviews",{}).get(selected.get("review_id"))
    if not isinstance(record,dict) or not isinstance(review,dict) or selected.get("digest") in state.get("revoked",{}).get(package_id,[]):
        raise ValueError("authored_package_exact_review_required")
    root = Path(selected["root_path"])
    observed=capability_pack_digest(root)
    if observed!=selected["digest"]:
        raise AuthoredIntegrityFailure(pack_id=package_id,expected_digest=selected["digest"],
            record_sha256=canonical_digest(record),review_sha256=canonical_digest(review),observed_digest=observed)
    if not lifecycle._pointer_binding_valid(state, package_id, selected):
        raise ValueError("authored_package_exact_review_required")
    manifest = parse_capability_pack_manifest(read_member(root,"manifest.yaml",65536).decode())
    adapter = load_adapter(root, manifest)
    if adapter.capability_id != capability_id or capability_pack_digest(root) != selected["digest"]:
        raise ValueError("authored_package_digest_changed")
    if original_pin is not None and any(selected.get(key) != value for key, value in original_pin.items()):
        raise ValueError("authored_package_original_pin_changed")
    return Registration(adapter, selected, manifest)


def staged_registration(capability_id):
    registration = _STAGED.get().get(capability_id)
    if registration is None:
        raise ValueError("authored_capability_staging_required")
    return registration


@contextmanager
def registration_scope(registration):
    token = _STAGED.set({**_STAGED.get(), registration.adapter.capability_id: registration})
    try:
        yield
    finally:
        _STAGED.reset(token)


def stage_package_request(function):
    """Narrow artifact/task entry stage; built-ins keep their existing path."""
    signature = inspect.signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        request = bound.arguments.get("request")
        capability_id = getattr(request, "capability_id", None)
        if not is_authored(capability_id):
            return await function(*args, **kwargs)
        with package_scope(capability_id):
            registration=staged_registration(capability_id)
            if (getattr(request,"goal_id",None)!=registration.pointer["goal_id"] or
                getattr(request,"goal_revision",None)!=registration.pointer.get("goal_revision")):
                from src.work_board.repository import BoardError
                raise BoardError("authored_package_goal_review_stale","Review and approve the exact package for the current Goal revision")
            result = await function(*args, **kwargs)
            # Authoritative publication finishes while the lifecycle lock
            # still pins the exact staged pointer. No lock spans execution.
            await bound.arguments["db"].commit()
            return result
    return wrapped


def stage_package_readiness(function):
    signature = inspect.signature(function)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        task = signature.bind(*args, **kwargs).arguments["task"]
        if not is_authored(task.capability_id):
            return await function(*args, **kwargs)
        try:
            with package_scope(task.capability_id):
                return await function(*args, **kwargs)
        except (ValueError, OSError):
            return "authored_package_review_required", "The exact active reviewed package is unavailable"
    return wrapped


@contextmanager
def package_scope(capability_id):
    """Physical stage + lifecycle lock precede a short caller-owned DB transition."""
    if not is_authored(capability_id) or capability_id in _STAGED.get():
        yield
        return
    lifecycle = CapabilityPackLifecycle()
    with integrity_fence(lifecycle):
        registration = load_registration(capability_id, lifecycle=lifecycle, state=lifecycle._load())
        token = _STAGED.set({**_STAGED.get(), capability_id: registration})
        try:
            yield
        finally:
            _STAGED.reset(token)


def capability_spec(capability_id):
    from src.work_board.dispatcher import CapabilitySpec, REGISTERED_CAPABILITIES
    builtin = REGISTERED_CAPABILITIES.get(capability_id)
    if builtin is not None:
        return builtin
    if not is_authored(capability_id):
        return None
    registration = staged_registration(capability_id)
    return CapabilitySpec(capability_id, "1:" + registration.pointer["digest"], secret_like=False)
