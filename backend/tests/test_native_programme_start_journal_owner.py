"""The original private journal accepts START only from its live producer."""
import json
from types import SimpleNamespace

import pytest

from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.workspace.accounting_witness import (
    CompositionSessionGuard, ProductionWorkspaceReconciliationError,
)


def test_start_receipt_without_original_proof_is_denied():
    owner = SimpleNamespace(db=SimpleNamespace(info={}))
    receipt = {"checkpoint_id": "runtime-service-invocation:programme-start:claim",
        "state_digest": "0" * 64, "safe": True, "payload": {}}
    with pytest.raises(NativeServiceBlocked, match="native_programme_start_publication_unavailable"):
        CompositionSessionGuard._check_private_journal(owner, "[]", json.dumps([receipt]),
            run_id="job", statement=object())


def test_existing_start_cannot_be_changed_or_removed():
    owner = SimpleNamespace(db=SimpleNamespace(info={}))
    receipt = {"checkpoint_id": "runtime-service-invocation:programme-start:claim",
        "state_digest": "0" * 64, "safe": True, "payload": {}}
    before = json.dumps([receipt])
    for after in ("[]", json.dumps([dict(receipt, state_digest="1" * 64)])):
        with pytest.raises(ProductionWorkspaceReconciliationError,
                match="composition_private_journal_mutation_denied"):
            CompositionSessionGuard._check_private_journal(owner, before, after, run_id="job")


def test_retained_start_does_not_reinvoke_publication():
    owner = SimpleNamespace(db=SimpleNamespace(info={}))
    receipt = {"checkpoint_id": "runtime-service-invocation:programme-start:claim",
        "state_digest": "0" * 64, "safe": True, "payload": {}}
    journal = json.dumps([receipt])
    CompositionSessionGuard._check_private_journal(owner, journal, journal, run_id="job")
