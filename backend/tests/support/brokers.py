"""The remote-inference broker in tests (#1223).

Production reaches its one remote-inference lane through
``get_remote_inference_admission_broker()``. The root conftest installs a
fresh broker for every test (``fresh_inference_broker``), so a lease left
held, Unknown or quarantined by one test can never block another, and no test
uses the process's import-time broker.

A test that needs its own broker (a restarted process, a saturated fake)
installs it with ``use_inference_broker``; every caller sees it at once, so no
list of import sites has to be patched.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.model_fabric import remote_inference_admission
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker


def use_inference_broker(monkeypatch: pytest.MonkeyPatch, broker: Any) -> Any:
    """Install ``broker`` as the process lane for the rest of this test."""

    monkeypatch.setattr(remote_inference_admission, "_process_broker", broker)
    return broker


def new_inference_broker() -> RemoteInferenceAdmissionBroker:
    """A broker configured like the one production builds at import."""

    return RemoteInferenceAdmissionBroker(durable_accounting=True)


@pytest.fixture
def fresh_inference_broker(monkeypatch: pytest.MonkeyPatch) -> RemoteInferenceAdmissionBroker:
    """A new broker installed as the process lane for this test."""

    return use_inference_broker(monkeypatch, new_inference_broker())


__all__ = ["fresh_inference_broker", "new_inference_broker", "use_inference_broker"]
