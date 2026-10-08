"""Python owns one trusted child, bounded anonymous pipes and positive cleanup.

This is a lifecycle host, not a policy engine, agent loop, sandbox or second lane.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import secrets
import time
from typing import Any

from .composition import CHILD_ENV, CORDIS_VERSION, CompositionBlocked, ReviewedComposition, reviewed_composition
from .protocol import CONTROL_TIMEOUT, MAX_PENDING, ProtocolError, encode_frame, read_frame

STDERR_LIMIT = 65_536


class HostBlocked(RuntimeError):
    """Only Cordis-dependent work is blocked; current Python core remains usable."""


@dataclass
class Pending:
    frame: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


class CordisHost:
    def __init__(self, *, node_path: Path | None = None):
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
        self._reaped = False
        self._cleanup_state = "not_started"
        self._cordis_disposal = "not_started"
        self._plugins: list[dict[str, Any]] = []
        self.stderr_bytes = 0

    @property
    def admitting(self) -> bool:
        return self.state == "ready" and self.process is not None and self.process.returncode is None and self._cleanup_state == "pending"

    def snapshot(self) -> dict[str, Any]:
        active_tasks = sum(task is None or not task.done() for task in self._tasks.values())
        resources = active_tasks + (1 if self.process is not None and not self._reaped else 0)
        state, reason = self.state, self.reason
        if state == "ready" and not self.admitting:
            state, reason = "blocked", reason or "child_unavailable"
        return {
            "runtime_role": "lifecycle_host", "state": state, "reason": reason,
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
            self.state, self.reason = "starting", None
            self.boot_nonce = secrets.token_hex(32)
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
                if frame["kind"] != "response" or frame["boot_nonce"] != self.boot_nonce or frame["seq"] != self._in_seq + 1:
                    raise ProtocolError("boot, kind or sequence mismatch")
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
        except (ProtocolError, OSError, ValueError, TypeError, KeyError):
            if self.state not in {"quiescing", "stopped", "cleanup_unknown"}:
                self._fence("protocol_rejected_or_pipe_lost")

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

    async def _rpc(self, method: str, *, internal: bool = False, invocation_ref: str | None = None,
                   deadline_at: int | None = None) -> dict[str, Any]:
        if not internal and not self.admitting:
            raise HostBlocked("runtime_not_ready")
        if self.process is None or self.process.stdin is None or self.reviewed is None or self.boot_nonce is None:
            raise HostBlocked("runtime_not_started")
        if self._unresolved >= MAX_PENDING:
            raise HostBlocked("rpc_capacity_exhausted")
        now = int(time.time() * 1000)
        if deadline_at is not None and (type(deadline_at) is not int or deadline_at <= now):
            raise HostBlocked("deadline_expired")
        deadline = min(now + int(CONTROL_TIMEOUT * 1000), deadline_at if deadline_at is not None else now + int(CONTROL_TIMEOUT * 1000))
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
                     "composition_epoch": None, "composition_digest": self.reviewed.composition_digest,
                     "package_digest": self.reviewed.package_digest, "deadline_at": deadline, "payload": {}}
            wire = encode_frame(frame)  # Validate before consuming a sequence.
            self._out_seq = seq
            self._pending[request_id] = Pending(frame, future)
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
            return
        if self._reaped:
            if not preserve_blocked and self._cleanup_state == "clean":
                self.state, self.reason = "stopped", None
            return
        # Admission closes before any drain/restart. No original RPC gets renewed.
        self.state = "quiescing"
        now = int(time.time() * 1000)
        drain_deadline = min([now + 10_000, *[pending.frame["deadline_at"] for pending in self._pending.values()]])
        if process.returncode is None and not preserve_blocked:
            try:
                await self._rpc("runtime.quiesce", internal=True, deadline_at=drain_deadline)
                response = await self._rpc("runtime.shutdown", internal=True, deadline_at=drain_deadline)
                payload = response["payload"]
                self._cordis_disposal = payload["cordis_disposal"]
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
        # This release owns only process-local resources. Positive OS reap proves
        # pipe/listener/timer destruction even when Cordis disposal is unconfirmed.
        if self._cleanup_state != "unknown":
            self._cleanup_state = "clean"
        if self._cleanup_state == "unknown":
            self.state, self.reason = "cleanup_unknown", "owned_cleanup_unknown"
        else:
            self.state, self.reason = ("blocked", previous_reason) if preserve_blocked else ("stopped", None)
            self._plugins = [{**plugin, "state": "stopped", "reason": None} for plugin in self._plugins]


cordis_host = CordisHost()
