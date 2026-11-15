"""Capture boundary negatives; genuine source publication is tested separately."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.work_board.channel_capture import (
    CaptureIntentPublication, ChannelCaptureReservationV1,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.general_task_proposal import new_group
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request

# ADR-035: pins channel-capture Work mechanics whose model step runs over the intercepted historical OpenRouter route
pytestmark = pytest.mark.openrouter_legacy


def reservation():
    owner = WorkBoardOwner(principal_id="operator:fixture", session_id="root-original")
    selected = request().model_copy(update={"plan": None, "expected_plan_revision": None})
    service = GeneralTaskService(Registry())
    service.start()
    descriptors, tool_digest = service.snapshot()
    task_input = selected.input.model_copy(update={"tool_set_digest": tool_digest})
    group = new_group(owner, task_input, descriptors, goal_revision=selected.goal_revision,
        request_key=selected.idempotency_key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    return owner, selected, service, ChannelCaptureReservationV1(
        owner_principal_id=owner.principal_id, original_root_id=owner.session_id,
        source_kind="audio", source_id="capture-original", canonical_message_id="message-original",
        conversation_session_id="conversation-original",
        source_digest="1" * 64, request_digest=digest(selected.model_dump(mode="json")),
        goal_revision=selected.goal_revision, idempotency_key=selected.idempotency_key,
        task_input=task_input, proposal_group=group)


@pytest.mark.parametrize("changes", [
    {"extra_authority": True}, {"goal_revision": True}, {"source_kind": "unpaired"},
    {"source_digest": "A" * 64}, {"original_root_id": "other-root"},
])
def test_closed_reservation_rejects_malformed_or_changed_original_identity(changes):
    original = reservation()[3]
    with pytest.raises(ValidationError):
        ChannelCaptureReservationV1.model_validate({**original.model_dump(), **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["mapping", "forged_seal", "missing"])
async def test_publication_denies_unissued_capture_before_private_read_or_source_callback(shape):
    owner, selected, service, original = reservation()
    source_check = AsyncMock()
    capture = (original.model_dump() if shape == "mapping" else
        CaptureIntentPublication(original, source_check, lambda: None, object())
        if shape == "forged_seal" else None)
    with pytest.raises(BoardError) as denied:
        await service.capture_intent(None, owner, selected, capture=capture)
    assert denied.value.code == "channel_capture_source_required"
    source_check.assert_not_called()
    assert service.registry.calls == []


# #1179 retired voice input: the two end-to-end tests that drove a live audio
# capture through the removed PTT worker and /api/audio route were deleted.
# Historical "audio" reservations stay a valid closed source kind above.
