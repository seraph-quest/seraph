"""Python owns one trusted child, bounded anonymous pipes and positive cleanup.

This is a lifecycle host, not a policy engine, agent loop, sandbox or second lane.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Any

from .composition import CHILD_ENV, CORDIS_VERSION, CompositionBlocked, ReviewedComposition, reviewed_composition
from .protocol import MAX_PENDING, ProtocolError, encode_frame, read_frame, rpc_deadline
from .contracts import SERVICE_METHODS, validate_request, validate_result

STDERR_LIMIT = 65_536


class HostBlocked(RuntimeError):
    """Only Cordis-dependent work is blocked; current Python core remains usable."""


@dataclass
class Pending:
    frame: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    native_binding: Any = None
    native_forwarded: bool = False
    native_inference: Any = None
    native_cancel_purpose: Any = None


class CordisHost:
    def __init__(self, *, node_path: Path | None = None, service_dispatch=None, native_services=False):
        # Only a fixed native owner may resolve/dispatch canonical invocations.
        self.service_dispatch = service_dispatch
        self._native_services = native_services
        self.node_path = node_path
        self.state = "stopped"
        self.reason: str | None = "not_started"
        self.reviewed: ReviewedComposition | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.boot_nonce: str | None = None
        self._out_seq = 0
        self._in_seq = 0
        self._pending: dict[str, Pending] = {}
        self._unresolved = 0
        self._tasks: dict[str, asyncio.Task[Any] | None] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._native_inflight: set[str] = set()
        self._native_owner_loop = None
        self._native_owner_boot_nonce = None
        self._native_deadline_seals = {}
        self._reaped = False
        self._cleanup_state = "not_started"
        self._cordis_disposal = "not_started"
        self._plugins: list[dict[str, Any]] = []
        self._readiness_checked_at: int | None = None
        self.stderr_bytes = 0

    @property
    def admitting(self) -> bool:
        return self.state == "ready" and self.process is not None and self.process.returncode is None and self._cleanup_state == "pending"

    def snapshot(self) -> dict[str, Any]:
        """Cached diagnostics cannot establish current child readiness."""
        return self._snapshot(verified=False)

    def _snapshot(self, *, verified: bool) -> dict[str, Any]:
        active_tasks = sum(task is None or not task.done() for task in self._tasks.values())
        resources = active_tasks + (1 if self.process is not None and not self._reaped else 0)
        state, reason = self.state, self.reason
        if state == "ready" and not self.admitting:
            state, reason = "blocked", reason or "child_unavailable"
        readiness = "verified" if verified and self.admitting else "unknown"
        if state == "ready" and not verified:
            state, reason = "blocked", "readiness_not_checked"
        elif state in {"blocked", "cleanup_unknown"}:
            readiness = "blocked"
        supported = sorted(getattr(self.service_dispatch, "supported_methods", ()))
        return {
            "runtime_role": "lifecycle_host", "state": state, "reason": reason,
            "readiness": {"state": readiness, "checked_at": self._readiness_checked_at},
            "native_services": {"state": "partial" if supported and self.admitting else "blocked",
                "supported_methods": supported,
                "blocked_methods": sorted(SERVICE_METHODS - set(supported))},
            "profile_id": self.reviewed.profile["profile_id"] if self.reviewed else None,
            "cordis_version": CORDIS_VERSION,
            "node_version": self.reviewed.node_version if self.reviewed else None,
            "composition_digest": self.reviewed.composition_digest if self.reviewed else None,
            "package_digest": self.reviewed.package_digest if self.reviewed else None,
            "plugins": [{**plugin, "state": "blocked", "reason": reason} if state == "blocked" else dict(plugin) for plugin in self._plugins],
            "cleanup": {"state": self._cleanup_state, "process_reaped": self._reaped,
                        "resources_remaining": resources if self._cleanup_state != "unknown" else None,
                        "cordis_disposal": self._cordis_disposal},
        }

    def _task(self, name: str, coroutine: Any) -> asyncio.Task[Any]:
        if name in self._tasks:
            coroutine.close()
            raise HostBlocked("resource ownership conflict")
        self._tasks[name] = None  # Reserve before the task can start.
        task = asyncio.create_task(coroutine, name=f"cordis-owned-{name}")
        self._tasks[name] = task
        return task

    def _fence(self, reason: str) -> None:
        self.state, self.reason = "blocked", reason
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(HostBlocked(reason))
        if self.process is not None and (self._cleanup_task is None or self._cleanup_task.done()):
            self._tasks.pop("cleanup", None)
            self._cleanup_task = self._task("cleanup", self.stop(preserve_blocked=True))

    async def start(self) -> bool:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            await asyncio.shield(self._cleanup_task)
        async with self._lifecycle_lock:
            if self.admitting:
                return True
            if self._cleanup_state == "unknown":
                self.state, self.reason = "cleanup_unknown", "owned_cleanup_unknown"
                return False
            if self.process is not None and not self._reaped:
                await self._stop_owned(preserve_blocked=False)
                if self._cleanup_state != "clean":
                    return False
            try:
                self.reviewed = reviewed_composition(node_path=self.node_path)
            except CompositionBlocked as exc:
                self.state, self.reason = "blocked", exc.reason
                self._plugins = []
                return False
            if self._native_services and self.service_dispatch is None:
                # Construction has no DB/import/contact side effects. Resolve
                # the fixed owner only after the reviewed package preflight.
                from .dispatch import NativeServiceDispatcher
                self.service_dispatch = NativeServiceDispatcher()
            self.state, self.reason = "starting", None
            self.boot_nonce = secrets.token_hex(32)
            self._readiness_checked_at = None
            self._in_seq = self._out_seq = 0
            self._pending.clear()
            self._tasks.clear()
            self._cleanup_state, self._cordis_disposal = "pending", "not_started"
            self._reaped, self.stderr_bytes = False, 0
            self.process = None
            # Reserve process ownership before subprocess creation. Shield creation
            # so cancellation cannot lose the returned trusted child handle.
            spawn = self._task("spawn", asyncio.create_subprocess_exec(
                str(self.reviewed.node), str(self.reviewed.entrypoint),
                cwd=str(self.reviewed.root), env=dict(CHILD_ENV), close_fds=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=1_048_580,
            ))
            try:
                self.process = await asyncio.shield(spawn)
                self._task("stdout", self._read_loop())
                self._task("stderr", self._stderr_loop())
                response = await self._rpc("bootstrap.hello", internal=True)
                self._plugins = response["payload"]["plugins"]
                self._verify_plugins(self._plugins)
                self.state, self.reason = "ready", None
                self._native_owner_loop = asyncio.get_running_loop()
                self._native_owner_boot_nonce = self.boot_nonce
                return True
            except asyncio.CancelledError:
                self.process = await spawn
                self.state, self.reason = "blocked", "startup_cancelled"
                await self._stop_owned(preserve_blocked=True)
                raise
            except (OSError, HostBlocked, ProtocolError, asyncio.TimeoutError):
                self.state, self.reason = "blocked", self.reason or "child_failed"
                await self._stop_owned(preserve_blocked=True)
                return False

    def _verify_plugins(self, plugins: list[dict[str, Any]]) -> None:
        assert self.reviewed is not None
        expected = {plugin["id"] for plugin in self.reviewed.profile["plugins"]}
        if {plugin["id"] for plugin in plugins} != expected or any(plugin["state"] != "ready" for plugin in plugins):
            raise ProtocolError("required plugin unavailable")

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                frame = await read_frame(self.process.stdout)
                if (frame["boot_nonce"] != self.boot_nonce or frame["seq"] != self._in_seq + 1
                    or self.reviewed is None or frame["composition_digest"] != self.reviewed.composition_digest
                    or frame["package_digest"] != self.reviewed.package_digest):
                    raise ProtocolError("boot, package or sequence mismatch")
                if frame["kind"] == "request":
                    if (frame["method"] not in SERVICE_METHODS or self.service_dispatch is None
                        or not self.admitting or frame["request_id"] != f"c-{frame['seq']}"
                        or self._unresolved >= MAX_PENDING
                        or frame["deadline_at"] <= int(time.time() * 1000)):
                        raise ProtocolError("unadmitted native service request")
                    originals = [entry for entry in self._pending.values()
                        if entry.native_binding is not None and not entry.future.done() and not entry.native_forwarded
                        and all(entry.frame[key] == frame[key] for key in (
                            "method", "invocation_ref", "composition_epoch", "deadline_at",
                            "boot_nonce", "package_digest", "composition_digest"))]
                    if len(originals) != 1:
                        raise ProtocolError("native request lacks unique original scoped invocation")
                    if frame["method"] == "inference.request":
                        self._native_inference_candidate(originals[0].native_binding, frame["payload"]["request_ref"])
                    if originals[0].native_cancel_purpose is not None:
                        self._native_cancel_purpose(originals[0].native_binding, frame["payload"])
                    originals[0].native_forwarded = True
                    self._in_seq = frame["seq"]
                    self._unresolved += 1
                    name = f"service-{frame['seq']}"
                    self._task(name, self._serve_native(frame, name, originals[0].native_binding))
                    continue
                pending = self._pending.get(frame["request_id"])
                if pending is None or pending.future.done():
                    raise ProtocolError("unsolicited or duplicate response")
                request = pending.frame
                expected = "runtime.ready" if request["method"] == "bootstrap.hello" else request["method"]
                if frame["method"] != expected or any(frame[key] != request[key] for key in ("invocation_ref", "composition_epoch", "composition_digest", "package_digest", "deadline_at")) or frame["deadline_at"] <= int(time.time() * 1000):
                    raise ProtocolError("response identity or deadline mismatch")
                if frame["method"] in {"runtime.ready", "runtime.status"}:
                    self._verify_plugins(frame["payload"]["plugins"])
                self._in_seq = frame["seq"]
                pending.future.set_result(frame)
        except asyncio.CancelledError:
            raise
        except (ProtocolError, HostBlocked, OSError, ValueError, TypeError, KeyError):
            if self.state not in {"quiescing", "stopped", "cleanup_unknown"}:
                self._fence("protocol_rejected_or_pipe_lost")

    async def _serve_native(self, request: dict[str, Any], name: str, original_scope) -> None:
        """One bounded native admission/result; lost ACK never renews the job."""
        original_boot = self.boot_nonce
        try:
            from .dispatch import CalledServiceInvocation
            pending = self._pending.get(getattr(original_scope, "parent_request_id", None))
            if (type(original_scope) is not CalledServiceInvocation or pending is None
                or pending.future.done() or pending.native_binding is not original_scope
                or pending.frame["request_id"] != original_scope.parent_request_id
                or original_scope.host_boot_nonce != original_boot):
                raise ProtocolError("native parent correlation no longer active")
            remaining = max(0, (request["deadline_at"] - int(time.time() * 1000)) / 1000)
            async with asyncio.timeout(remaining):
                payload = await self.service_dispatch.dispatch(request, original_scope=original_scope)
                validate_result(request["method"], payload)
                async with self._write_lock:
                    if (self.boot_nonce != original_boot or not self.admitting
                        or int(time.time() * 1000) >= request["deadline_at"]
                        or self.process is None or self.process.stdin is None):
                        raise ProtocolError("native result lost original boot/deadline")
                    response = {**request, "kind": "response", "seq": self._out_seq + 1, "payload": payload}
                    wire = encode_frame(response)
                    self._out_seq = response["seq"]
                    self.process.stdin.write(wire)
                    await self.process.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            # No native exception message/private payload enters a child/log.
            self._fence("native_service_failed_or_ack_lost")
        finally:
            self._unresolved -= 1
            self._tasks.pop(name, None)

    def get_original_owner_loop(self):
        """Original lifecycle-loop locator; never callable or candidate authority."""
        loop = self._native_owner_loop
        if (not self.admitting or loop is None or loop.is_closed() or not loop.is_running()
            or self._native_owner_boot_nonce != self.boot_nonce):
            raise HostBlocked("native_original_owner_loop_unavailable")
        return loop

    def _ordinary_original_deadline(self, method, payload, original_scope, deadline, *, now):
        """Timing only: exact original source and purpose cannot renew a call."""
        if original_scope.deadline_at <= now:
            raise HostBlocked("native_original_call_deadline_expired")
        for key, (source, original_cutoff, _cutoff) in list(self._native_deadline_seals.items()):
            if original_cutoff <= now:
                del self._native_deadline_seals[key]
        claim_digest = hashlib.sha256(json.dumps(dict(original_scope.witness), sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        purpose_digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        source_key = (self.boot_nonce, claim_digest)
        for key, (source, _original_cutoff, _cutoff) in self._native_deadline_seals.items():
            if key[:2] == source_key and source is not original_scope:
                raise HostBlocked("native_original_deadline_source_changed")
        key = (*source_key, method, purpose_digest)
        prior = self._native_deadline_seals.get(key)
        if prior is not None:
            if prior[1] != original_scope.deadline_at:
                raise HostBlocked("native_original_deadline_source_changed")
            deadline = min(deadline, prior[2])
        else:
            if len(self._native_deadline_seals) >= MAX_PENDING * len(SERVICE_METHODS):
                raise HostBlocked("native_original_deadline_capacity_exhausted")
            self._native_deadline_seals[key] = (original_scope, original_scope.deadline_at, deadline)
        if deadline <= now:
            raise HostBlocked("native_original_call_deadline_expired")
        return deadline

    def _checked_native_inference(self, candidate, original_scope):
        try:
            from src.model_fabric.native_inference import NativeInferenceContinuation
        except ImportError as error:
            raise HostBlocked("native_inference_original_candidate_unavailable") from error
        if type(candidate) is not NativeInferenceContinuation:
            raise HostBlocked("native_inference_original_candidate_required")
        candidate.validate_host_scope(self, original_scope)
        return candidate

    def _native_inference_candidate(self, called_scope, request_ref):
        """Only the exact unresolved original pending frame retains this object."""
        from .dispatch import CalledServiceInvocation
        if type(called_scope) is not CalledServiceInvocation:
            raise HostBlocked("native_inference_original_pending_required")
        pending = self._pending.get(called_scope.parent_request_id)
        if (pending is None or pending.future.done() or pending.native_binding is not called_scope
            or pending.frame["method"] != "inference.request"
            or pending.frame["payload"] != {"request_ref": request_ref}
            or pending.frame["deadline_at"] != called_scope.deadline_at
            or called_scope.deadline_at <= int(time.time() * 1000)):
            raise HostBlocked("native_inference_original_pending_changed")
        candidate = self._checked_native_inference(pending.native_inference, called_scope.original)
        candidate.validate_called_scope(self, called_scope, request_ref)
        return candidate

    def _checked_cancel_purpose(self, purpose, original_scope, method, payload):
        if method not in {"conversation.cancel", "agent-loop.cancelTurn"}:
            raise HostBlocked("native_cancel_purpose_method_changed")
        try:
            from src.agent.native_turn_controls import validate_forward_cancel_purpose
        except ImportError as error:
            raise HostBlocked("native_cancel_original_purpose_unavailable") from error
        cutoff = validate_forward_cancel_purpose(purpose, self, original_scope, method, payload)
        from .protocol import integer
        integer(cutoff, 1)
        if cutoff > original_scope.deadline_at or cutoff <= int(time.time() * 1000):
            raise HostBlocked("native_cancel_original_purpose_expired")
        return cutoff

    def _native_cancel_purpose(self, called_scope, payload):
        from .dispatch import CalledServiceInvocation
        from src.agent.native_turn_controls import validate_called_cancel_purpose
        if type(called_scope) is not CalledServiceInvocation:
            raise HostBlocked("native_cancel_original_pending_required")
        pending = self._pending.get(called_scope.parent_request_id)
        if (pending is None or pending.future.done() or pending.native_binding is not called_scope
            or pending.native_cancel_purpose is None
            or pending.frame["method"] not in {"conversation.cancel", "agent-loop.cancelTurn"}
            or pending.frame["payload"] != payload or pending.frame["deadline_at"] != called_scope.deadline_at):
            raise HostBlocked("native_cancel_original_pending_changed")
        cutoff = self._checked_cancel_purpose(pending.native_cancel_purpose, called_scope.original,
            pending.frame["method"], payload)
        if called_scope.deadline_at > cutoff:
            raise HostBlocked("native_cancel_original_pending_changed")
        validate_called_cancel_purpose(pending.native_cancel_purpose, self, called_scope, payload)
        return pending.native_cancel_purpose

    async def request_service(self, method: str, payload: dict[str, Any], *, original_scope,
                              native_inference=None, native_cancel_purpose=None) -> dict[str, Any]:
        """Forward only an already captured original native claim scope."""
        from .dispatch import OriginalServiceInvocation
        from .ownership import RuntimeCompositionBinding
        validate_request(method, payload)
        if self.service_dispatch is None or not self.admitting:
            raise HostBlocked("native_services_unavailable")
        if (type(original_scope) is not OriginalServiceInvocation
            or type(original_scope.binding) is not RuntimeCompositionBinding
            or not original_scope.binding.allows(method)):
            raise HostBlocked("native_original_scope_missing_or_method_changed")
        if (original_scope.host_boot_nonce != self.boot_nonce or self.reviewed is None
            or original_scope.witness["package_digest"] != self.reviewed.package_digest
            or original_scope.witness["host_composition_digest"] != self.reviewed.composition_digest):
            raise HostBlocked("native_original_host_changed")
        if method == "inference.request":
            self._checked_native_inference(native_inference, original_scope)
        elif native_inference is not None:
            raise HostBlocked("native_inference_attachment_unexpected")
        if native_cancel_purpose is not None:
            self._checked_cancel_purpose(native_cancel_purpose, original_scope, method, payload)
        key = original_scope.witness["invocation_ref"]
        if key in self._native_inflight:
            raise HostBlocked("native_original_scope_already_pending")
        # The native ingress captures the scope once. A bare locator must never
        # fetch a later claim/fence or renew an old host invocation here.
        self._native_inflight.add(key)
        try:
            response = await self._rpc(method, invocation_ref=key,
                deadline_at=original_scope.deadline_at, composition_epoch=original_scope.binding.epoch_for(method),
                payload=payload, native_binding=original_scope, native_inference=native_inference,
                native_cancel_purpose=native_cancel_purpose)
            return validate_result(method, response["payload"])
        finally:
            self._native_inflight.discard(key)

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while data := await self.process.stderr.read(4096):
                self.stderr_bytes += len(data)
                if self.stderr_bytes > STDERR_LIMIT:
                    self._fence("stderr_limit_exceeded")
                    return
        except asyncio.CancelledError:
            raise
        except OSError:
            self._fence("stderr_pipe_lost")

    async def request(self, method: str = "runtime.status", *, invocation_ref: str | None = None,
                      deadline_at: int | None = None) -> dict[str, Any]:
        if not self.admitting:
            raise HostBlocked(self.reason or "runtime_not_ready")
        if method not in {"runtime.status", "invocation.cancel"}:
            raise HostBlocked("control_not_public")
        return (await self._rpc(method, invocation_ref=invocation_ref, deadline_at=deadline_at))["payload"]

    async def refresh_status(self, *, deadline_at: int | None = None) -> dict[str, Any]:
        """One current-boot readback, bounded below the browser's five seconds.

        The caller's original deadline also bounds writer admission and response;
        historical plugin details and timestamps never imply live readiness.
        """
        if not self.admitting:
            return self.snapshot()
        now = int(time.time() * 1000)
        if deadline_at is not None and type(deadline_at) is not int:
            return self.snapshot()
        deadline = min(now + 4000, deadline_at if deadline_at is not None else now + 4000)
        original_boot = self.boot_nonce
        try:
            payload = await self.request(deadline_at=deadline)
        except (HostBlocked, ProtocolError):
            return self.snapshot()
        if original_boot != self.boot_nonce or not self.admitting or int(time.time() * 1000) >= deadline:
            return self.snapshot()
        if payload["state"] != "ready":
            self._fence("runtime_not_ready")
            return self.snapshot()
        self._plugins = payload["plugins"]
        self._readiness_checked_at = int(time.time() * 1000)
        return self._snapshot(verified=True)

    async def _rpc(self, method: str, *, internal: bool = False, invocation_ref: str | None = None,
                   deadline_at: int | None = None, composition_epoch: int | None = None,
                   payload: dict[str, Any] | None = None, native_binding=None,
                   native_inference=None, native_cancel_purpose=None) -> dict[str, Any]:
        if not internal and not self.admitting:
            raise HostBlocked("runtime_not_ready")
        if self.process is None or self.process.stdin is None or self.reviewed is None or self.boot_nonce is None:
            raise HostBlocked("runtime_not_started")
        if self._unresolved >= MAX_PENDING:
            raise HostBlocked("rpc_capacity_exhausted")
        now = int(time.time() * 1000)
        if deadline_at is not None and (type(deadline_at) is not int or deadline_at <= now):
            raise HostBlocked("deadline_expired")
        purpose_deadlines = None
        if method == "inference.request":
            from .dispatch import OriginalServiceInvocation
            if type(native_binding) is not OriginalServiceInvocation or deadline_at != native_binding.deadline_at:
                raise HostBlocked("native_inference_original_scope_required")
            candidate = self._checked_native_inference(native_inference, native_binding)
            if asyncio.get_running_loop() is not self.get_original_owner_loop():
                raise HostBlocked("native_inference_original_owner_loop_changed")
            purpose_deadlines = (candidate.purpose_deadline_at, candidate.operation_deadline_at, candidate.turn_deadline_at)
        elif native_inference is not None:
            raise HostBlocked("native_inference_attachment_unexpected")
        try:
            deadline = rpc_deadline(method, now=now, original_deadline=deadline_at, purpose_deadlines=purpose_deadlines)
        except ProtocolError as error:
            raise HostBlocked(str(error)) from error
        if native_cancel_purpose is not None:
            from .dispatch import OriginalServiceInvocation
            if type(native_binding) is not OriginalServiceInvocation or deadline_at != native_binding.deadline_at:
                raise HostBlocked("native_cancel_original_scope_required")
            cutoff = self._checked_cancel_purpose(native_cancel_purpose, native_binding, method, payload)
            deadline = min(deadline, cutoff)
        if method in SERVICE_METHODS and method != "inference.request":
            from .dispatch import OriginalServiceInvocation
            if type(native_binding) is not OriginalServiceInvocation or deadline_at != native_binding.deadline_at:
                raise HostBlocked("native_original_deadline_scope_required")
            deadline = self._ordinary_original_deadline(method, payload, native_binding, deadline, now=now)
        original_boot = self.boot_nonce
        self._unresolved += 1
        future = asyncio.get_running_loop().create_future()
        request_id: str | None = None
        locked = False
        try:
            await asyncio.wait_for(self._write_lock.acquire(), max(0.001, (deadline - int(time.time() * 1000)) / 1000))
            locked = True
            if self.boot_nonce != original_boot or (not internal and not self.admitting):
                raise HostBlocked("boot_changed_or_admission_closed")
            if len(self._pending) >= MAX_PENDING:
                raise HostBlocked("rpc_capacity_exhausted")
            if deadline <= int(time.time() * 1000):
                raise HostBlocked("deadline_expired")
            seq = self._out_seq + 1
            request_id = f"r-{seq}"
            frame = {"protocol": 1, "boot_nonce": self.boot_nonce, "request_id": request_id, "seq": seq,
                     "kind": "request", "method": method, "invocation_ref": invocation_ref,
                     "composition_epoch": composition_epoch, "composition_digest": self.reviewed.composition_digest,
                     "package_digest": self.reviewed.package_digest, "deadline_at": deadline, "payload": payload if payload is not None else {}}
            wire = encode_frame(frame)  # Validate before consuming a sequence.
            self._out_seq = seq
            if native_binding is not None:
                from .dispatch import CalledServiceInvocation
                native_binding = CalledServiceInvocation(native_binding, method, composition_epoch,
                    deadline, request_id, original_boot)
            self._pending[request_id] = Pending(frame, future, native_binding,
                native_inference=native_inference, native_cancel_purpose=native_cancel_purpose)
            self.process.stdin.write(wire)
            await asyncio.wait_for(self.process.stdin.drain(), max(0.001, (deadline - int(time.time() * 1000)) / 1000))
            self._write_lock.release()
            locked = False
            return await asyncio.wait_for(future, max(0.001, (deadline - int(time.time() * 1000)) / 1000))
        except asyncio.CancelledError:
            if request_id in self._pending:
                self._fence("rpc_cancelled")
            raise
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            if request_id in self._pending:
                self._fence("rpc_failed_or_deadline_expired")
            raise HostBlocked(self.reason or "rpc_deadline_expired") from exc
        finally:
            if locked:
                self._write_lock.release()
            self._unresolved -= 1
            if request_id is not None:
                self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # Retrieve fenced write failures even before awaiting.
            elif not future.done():
                future.cancel()

    async def stop(self, *, preserve_blocked: bool = False) -> None:
        try:
            cleanup = self._cleanup_task
            if cleanup is not None and cleanup is not asyncio.current_task() and not cleanup.done():
                await asyncio.shield(cleanup)
            async with self._lifecycle_lock:
                await self._stop_owned(preserve_blocked=preserve_blocked)
            cleanup = self._cleanup_task
            if cleanup is not None and cleanup is not asyncio.current_task() and not cleanup.done():
                await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Caller cancellation must not orphan the owned child/reap obligation.
            if asyncio.current_task() is not self._cleanup_task:
                self._fence("shutdown_cancelled")
            raise

    async def _stop_owned(self, *, preserve_blocked: bool) -> None:
        previous_reason = self.reason
        process = self.process
        if process is None:
            if self._cleanup_state == "pending":
                self._cleanup_state = "clean"
            if not preserve_blocked:
                self.state, self.reason = "stopped", None
            if self._cleanup_state == "clean":
                self._native_owner_loop = self._native_owner_boot_nonce = None
                self._native_deadline_seals.clear()
            return
        if self._reaped:
            if not preserve_blocked and self._cleanup_state == "clean":
                self.state, self.reason = "stopped", None
            if self._cleanup_state == "clean":
                self._native_owner_loop = self._native_owner_boot_nonce = None
                self._native_deadline_seals.clear()
            return
        # Admission closes before any drain/restart. No original RPC gets renewed.
        self.state = "quiescing"
        shutdown_disposal_proof = None
        original_boot = self.boot_nonce
        now = int(time.time() * 1000)
        drain_deadline = min([now + 10_000, *[pending.frame["deadline_at"] for pending in self._pending.values()]])
        if process.returncode is None and not preserve_blocked:
            try:
                await self._rpc("runtime.quiesce", internal=True, deadline_at=drain_deadline)
                response = await self._rpc("runtime.shutdown", internal=True, deadline_at=drain_deadline)
                payload = response["payload"]
                self._cordis_disposal = payload["cordis_disposal"]
                if payload["resources_remaining"] == 0 and self._cordis_disposal == "confirmed":
                    # _rpc verified the exact original shutdown frame identity.
                    shutdown_disposal_proof = original_boot
                if payload["resources_remaining"] != 0 or self._cordis_disposal != "confirmed":
                    self._cleanup_state = "unknown"
                await asyncio.wait_for(process.wait(), max(0.001, min(1.0, (drain_deadline - int(time.time() * 1000)) / 1000)))
                self._reaped = True
            except (HostBlocked, ProtocolError, asyncio.TimeoutError, OSError):
                self._cordis_disposal = "unconfirmed"
        if not self._reaped:
            try:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 2)
                self._reaped = True
                if self._cordis_disposal == "not_started":
                    self._cordis_disposal = "unconfirmed"
            except (OSError, asyncio.TimeoutError):
                self._cleanup_state = "unknown"
        if process.stdin is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.stdin.wait_closed(), 2)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The OS already closed this pipe.
            except (OSError, asyncio.TimeoutError):
                self._cleanup_state = "unknown"
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(HostBlocked("runtime_stopped"))
        for name, task in self._tasks.items():
            if name != "cleanup" and task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        results = await asyncio.gather(*[task for name, task in self._tasks.items() if name != "cleanup" and task is not None and task is not asyncio.current_task()], return_exceptions=True)
        if any(isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError) for result in results):
            self._cleanup_state = "unknown"
        if not self._reaped:
            self._cleanup_state = "unknown"
        if (self._reaped and self._cleanup_state != "unknown"
            and shutdown_disposal_proof is not None and self.boot_nonce == shutdown_disposal_proof):
            self._cordis_disposal = "confirmed"
        # This release owns only process-local resources. Positive OS reap proves
        # pipe/listener/timer destruction even when Cordis disposal is unconfirmed.
        if self._cleanup_state != "unknown":
            self._cleanup_state = "clean"
            self._native_owner_loop = self._native_owner_boot_nonce = None
            self._native_deadline_seals.clear()
        if self._cleanup_state == "unknown":
            self.state, self.reason = "cleanup_unknown", "owned_cleanup_unknown"
        else:
            self.state, self.reason = ("blocked", previous_reason) if preserve_blocked else ("stopped", None)
            self._plugins = [{**plugin, "state": "stopped", "reason": None} for plugin in self._plugins]


cordis_host = CordisHost(native_services=True)
