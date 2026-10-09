"""Request grammar only; these tests confer no Source/producer authority."""
import pytest
from pydantic import ValidationError

from src.api.workflows import RepoSourceRecoveryRequest


@pytest.mark.parametrize("payload", [
    {},
    {"action": "reconcile_original_cleanup"},
    {"expected_job_revision": 1},
    {"expected_job_revision": True, "action": "reconcile_original_cleanup"},
    {"expected_job_revision": "1", "action": "reconcile_original_cleanup"},
    {"expected_job_revision": 1.0, "action": "reconcile_original_cleanup"},
    {"expected_job_revision": -1, "action": "reconcile_original_cleanup"},
    {"expected_job_revision": 1, "action": "resume"},
    {"expected_job_revision": 1, "action": "reconcile_original_cleanup", "witness": {}},
    {"expected_job_revision": 1, "action": "settle_original_host_boot_cleanup", "boot_id": "caller"},
    {"expected_job_revision": 1, "action": "reconcile_original_cleanup", "iteration_id": "caller"},
    {"expected_job_revision": 1, "action": "reconcile_original_cleanup", "path": "caller"},
])
def test_recovery_request_rejects_coercion_and_caller_proof(payload):
    with pytest.raises(ValidationError):
        RepoSourceRecoveryRequest.model_validate(payload)


@pytest.mark.parametrize("action", ["reconcile_original_cleanup", "settle_original_host_boot_cleanup"])
def test_recovery_request_carries_only_action_and_revision(action):
    value = RepoSourceRecoveryRequest.model_validate({"expected_job_revision": 0, "action": action})
    assert value.model_dump() == {"expected_job_revision": 0, "action": action}
