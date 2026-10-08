"""Positive native callback/process quiescence evidence; no authority or lifecycle."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any


def process_identity() -> dict[str, Any]:
    # Linux process identity is read only when the explicit runtime asks.
    pid = os.getpid()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    stat = Path(f'/proc/{pid}/stat').read_text()
    start = stat[stat.rfind(')') + 2:].split()[19]
    namespace = os.stat('/proc/self/ns/pid').st_ino
    return {'boot_id': boot, 'pid': pid, 'pid_start_ticks': start, 'pid_namespace': namespace}


def positive_owner_death(witness: dict[str, Any]) -> str | None:
    current = process_identity()
    if not isinstance(witness.get('boot_id'), str) or not isinstance(witness.get('pid'), int) or not isinstance(witness.get('pid_start_ticks'), str) or not isinstance(witness.get('pid_namespace'), int):
        return None
    if current['boot_id'] != witness['boot_id']:
        return 'linux_boot_changed'
    if current['pid_namespace'] != witness['pid_namespace']:
        return None  # Absence in another PID namespace proves nothing.
    try:
        stat = Path(f"/proc/{witness['pid']}/stat").read_text()
    except FileNotFoundError:
        return 'positive_process_death'
    fields = stat[stat.rfind(')') + 2:].split()
    if fields[19] != witness['pid_start_ticks'] or fields[0] == 'Z':
        return 'positive_process_death'
    return None


class NativeCallbackOwners:
    def __init__(self) -> None:
        self.nonce = uuid.uuid4().hex
        self.callbacks: dict[str, tuple[asyncio.Task, dict[str, Any]]] = {}
        self.awaited: set[asyncio.Task] = set()

    def bind(self, job_id: str, witness: dict[str, Any]) -> None:
        task = asyncio.current_task()
        if task is None or job_id in self.callbacks:
            raise RuntimeError('The original native callback is already bound')
        self.callbacks[job_id] = (task, dict(witness))

    def proof(self, job_id: str, witness: dict[str, Any]) -> str | None:
        original = self.callbacks.get(job_id)
        if original and original[1] == witness:
            task = original[0]
            if task.done() and task in self.awaited:
                return 'owned_positive_close'
            return None
        return positive_owner_death(witness)
