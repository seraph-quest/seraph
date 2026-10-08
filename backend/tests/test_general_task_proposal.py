"""Original allowance/deadline mechanics, without inference or credentials."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.work_board.contracts import TaskProposalGroupV1
from src.work_board.general_task_proposal import group_identity, new_group
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request
from src.work_board.contracts import WorkBoardOwner


def test_stable_group_identity_and_original_deadline_are_not_mutable_intent_authority():
    owner = WorkBoardOwner(principal_id="operator:fixture", session_id="root-original")
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    original = request()
    group = new_group(owner, original.input, Registry().entries, goal_revision=1,
        request_key="original", expires_at=now + timedelta(seconds=90), now=now)
    assert group.original_deadline_at == now + timedelta(seconds=90)
    changed = new_group(owner, original.input.model_copy(update={"intent": "different"}),
        Registry().entries, goal_revision=1, request_key="original",
        expires_at=now + timedelta(seconds=90), now=now)
    assert changed.group_id == group.group_id
    assert changed.initial_input_digest != group.initial_input_digest
    assert group_identity(owner, original.input.goal_ref, 2, "original") != group.group_id
    assert TaskProposalGroupV1.model_validate_json(group.model_dump_json()) == group


@pytest.mark.parametrize("changes", [{"max_inference_calls": True}, {"max_steps": 17},
    {"issued_at": "2026-10-08T00:00:00"}, {"original_deadline_at": "2026-10-08T00:00:00+01:00"},
    {"group_id": "A" * 64}, {"owner_principal_id": "x" * 129}])
def test_closed_group_rejects_coercion_and_unbounded_or_non_utc_identity(changes):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    group = new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="root-original"),
        request().input, Registry().entries, goal_revision=1, request_key="original",
        expires_at=now + timedelta(seconds=90), now=now)
    with pytest.raises(ValidationError):
        TaskProposalGroupV1.model_validate({**group.model_dump(), **changes})


def test_expired_root_cannot_mint_task_clock():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    with pytest.raises(BoardError, match="Original task authority expired"):
        new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="root-original"),
            request().input, Registry().entries, goal_revision=1, request_key="original",
            expires_at=now, now=now)
