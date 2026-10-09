"""Original physical readers only; these tests confer no native authority."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import select
import signal
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from config.settings import settings
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.work_board import input_artifacts, pipeline_cpu
from src.work_board.repository import BoardError
from src.runtime_plugins import inference_output
from src.agent.turn_execution import NativeTurnBlocked
from src.vault.redaction import redact_secrets_in_text_readonly
from src.workspace import production
from tests.test_native_inference_output import payload as output_payload


def private(path, data=b"native bytes"):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in path.parents:
        if parent.name in {"artifacts", "work-board", "input", "native-inference-output"} or len(parent.name) == 64:
            parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def input_path(root):
    return root / input_artifacts.INPUT_ARTIFACT_ROOT / "input.json"


def read_input(path, data, budget=None):
    return input_artifacts._safe_file_bytes(path, expected_digest=hashlib.sha256(data).hexdigest(),
        expected_size=len(data), header_budget=budget)


@pytest.mark.parametrize("kind", ["input", "output", "lifecycle", "checkpoint", "pipeline"])
def test_regular_read_and_repeated_exact_budget(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    if kind == "input":
        path = input_path(tmp_path); data = private(path)
        read = lambda budget: read_input(path, data, budget)
    elif kind == "pipeline":
        path = tmp_path / "artifacts/work-board/evidence/output.json"; data = private(path)
        read = lambda budget: pipeline_cpu.read_output(str(path.relative_to(tmp_path)), hashlib.sha256(data).hexdigest(), header_budget=budget)
    elif kind == "output":
        data = b"native output"; payload = output_payload(data)
        path = tmp_path / payload["file_ref"]; private(path, data)
        read = lambda budget: inference_output.read_output_bytes(tmp_path, payload, header_budget=budget)
    else:
        path = tmp_path / "receipt.json"; data = private(path, b'{"secret_values_included":false}')
        if kind == "checkpoint":
            read = lambda budget: production._read_private_checkpoint(path, header_budget=budget)
        else:
            read = lambda budget: production.read_lifecycle_receipt(SimpleNamespace(lifecycle_directory=tmp_path), header_budget=budget)
            monkeypatch.setattr(production, "lifecycle_receipt_path", lambda workspace: path)
    budget = HeaderReadBudget(); before = budget.remaining
    assert read(budget) is not None
    assert read(budget) is not None
    assert before - budget.remaining == 2 * (len(data) + 1)
    budget.remaining = len(data)
    def unopened(*args, **kwargs):
        raise AssertionError("budget exhaustion must precede original file open")
    if kind == "pipeline":
        def unread(*args, **kwargs):
            raise AssertionError("budget exhaustion must precede original file bytes")
        monkeypatch.setattr(os, "read", unread)
    else:
        monkeypatch.setattr(os, "open", unopened)
    with pytest.raises(HeaderBoundsError):
        read(budget)


@pytest.mark.parametrize("mutation", ["symlink", "mode", "hardlink", "size", "hash", "directory"])
def test_input_metadata_and_hash_denials(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    path = input_path(tmp_path); data = private(path)
    if mutation == "symlink":
        target = path.with_name("actual.json"); path.rename(target); path.symlink_to(target)
    elif mutation == "mode": path.chmod(0o644)
    elif mutation == "hardlink": os.link(path, path.with_name("alias.json"))
    elif mutation == "size": path.write_bytes(data + b"x")
    elif mutation == "hash": path.write_bytes(b"x" * len(data))
    else: path.unlink(); path.mkdir()
    with pytest.raises(BoardError): read_input(path, data)


@pytest.mark.parametrize("kind", ["input", "output", "checkpoint", "lifecycle"])
def test_inode_replacement_rejected(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    data = b'{"secret_values_included":false}'
    if kind == "input":
        path = input_path(tmp_path); private(path, data); read = lambda: read_input(path, data)
        error = BoardError
    elif kind == "output":
        payload = output_payload(data); path = tmp_path / payload["file_ref"]; private(path, data)
        read = lambda: inference_output.read_output_bytes(tmp_path, payload); error = NativeTurnBlocked
    else:
        path = tmp_path / "receipt.json"; private(path, data); error = production.ProductionWorkspaceError
        if kind == "checkpoint": read = lambda: production._read_private_checkpoint(path)
        else:
            monkeypatch.setattr(production, "lifecycle_receipt_path", lambda workspace: path)
            read = lambda: production.read_lifecycle_receipt(SimpleNamespace(lifecycle_directory=tmp_path))
    original = os.read; changed = False
    def replacing(fd, size):
        nonlocal changed
        result = original(fd, size)
        if not changed:
            changed = True
            replacement = path.with_name("replacement.json"); private(replacement, data); replacement.replace(path)
        return result
    monkeypatch.setattr(os, "read", replacing)
    with pytest.raises(error): read()


class SecretRows:
    def __init__(self, key):
        self.rows = [SimpleNamespace(encrypted_value=Fernet(key).encrypt(b"secret-value").decode())]
    async def execute(self, statement): return self
    def scalars(self): return iter(self.rows)


def test_vault_real_key_fallback_and_fixed_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    key = Fernet.generate_key(); private(tmp_path / ".vault-key", key)
    budget = HeaderReadBudget(); before = budget.remaining
    assert asyncio.run(redact_secrets_in_text_readonly(SecretRows(key), "secret-value", header_budget=budget)) == "[redacted secret]"
    assert before - budget.remaining == 4097
    budget.remaining = 4096
    def unopened(*args, **kwargs):
        raise AssertionError("Vault fixed reserve must precede original key open")
    monkeypatch.setattr(os, "open", unopened)
    with pytest.raises(HeaderBoundsError):
        asyncio.run(redact_secrets_in_text_readonly(SecretRows(key), "secret-value", header_budget=budget))


@pytest.mark.parametrize("kind", ["input", "output", "vault", "checkpoint", "lifecycle", "pipeline"])
def test_real_fifo_open_is_nonblocking_and_original_child_reaped(tmp_path, monkeypatch, kind):
    """Fork confines a possible reader hang; actual open entry/flags are observed."""
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    data = b'{"secret_values_included":false}'
    if kind == "input":
        path = input_path(tmp_path); private(path, data); read = lambda: read_input(path, data)
    elif kind == "output":
        payload = output_payload(data); path = tmp_path / payload["file_ref"]; private(path, data)
        read = lambda: inference_output.read_output_bytes(tmp_path, payload)
    elif kind == "pipeline":
        path = tmp_path / "artifacts/work-board/evidence/output.json"; private(path, data)
        read = lambda: pipeline_cpu.read_output(str(path.relative_to(tmp_path)), hashlib.sha256(data).hexdigest())
    elif kind == "vault":
        key = Fernet.generate_key(); path = tmp_path / ".vault-key"; private(path, key)
        read = lambda: asyncio.run(redact_secrets_in_text_readonly(SecretRows(key), "secret-value"))
    else:
        path = tmp_path / "receipt.json"; private(path, data)
        if kind == "checkpoint": read = lambda: production._read_private_checkpoint(path)
        else:
            monkeypatch.setattr(production, "lifecycle_receipt_path", lambda workspace: path)
            read = lambda: production.read_lifecycle_receipt(SimpleNamespace(lifecycle_directory=tmp_path))
    receive, send = os.pipe(); child = os.fork()
    if child == 0:
        os.close(receive); real_open = os.open; entered = False; flags_seen = 0
        def observed_open(name, flags, *args, **kwargs):
            nonlocal entered, flags_seen
            if str(name) in {str(path), path.name}:
                path.unlink(); os.mkfifo(path, 0o600)
                entered = True; flags_seen = flags
            return real_open(name, flags, *args, **kwargs)
        os.open = observed_open
        original_read = os.read
        def fifo_read_trap(fd, size):
            if __import__("stat").S_ISFIFO(os.fstat(fd).st_mode):
                raise AssertionError("original reader attempted FIFO body")
            return original_read(fd, size)
        os.read = fifo_read_trap
        try:
            try:
                result = read()
                denied = kind == "vault" and result == "[redaction unavailable]"
            except (BoardError, NativeTurnBlocked, production.ProductionWorkspaceError, ValueError): denied = True
            os.write(send, json.dumps({"entered": entered, "flags": flags_seen, "denied": denied}).encode())
            os._exit(0)
        except BaseException:
            os._exit(2)
    os.close(send); waited = False
    try:
        ready, _, _ = select.select([receive], [], [], 3)
        assert ready, "original physical reader blocked on a FIFO"
        evidence = json.loads(os.read(receive, 4096))
        pid, status = os.waitpid(child, 0); waited = True
        assert pid == child and os.waitstatus_to_exitcode(status) == 0
        assert evidence["entered"] and evidence["denied"]
        assert evidence["flags"] & os.O_NONBLOCK and evidence["flags"] & os.O_NOFOLLOW
    finally:
        os.close(receive)
        if not waited:
            os.kill(child, signal.SIGKILL)
            assert os.waitpid(child, 0)[0] == child
