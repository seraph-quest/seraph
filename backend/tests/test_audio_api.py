from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.audio import (
    TranscriptConfirmationBody,
    _owned_job,
    cancel_audio,
    confirm_audio,
    default_audio_worker,
    get_audio,
    process_audio,
)
from src.auth.service import test_bypass_operator as _test_bypass_operator


def _audio_request(operator) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/audio/ptt/audio-owner-boundary",
            "headers": [],
            "query_string": b"",
            "state": {"operator": operator},
        }
    )


@pytest.mark.asyncio
async def test_audio_job_owner_mismatch_cannot_trigger_expiry_or_cleanup():
    request_id = "audio-owner-boundary"
    bypass_operator = _test_bypass_operator()
    attacker = replace(
        bypass_operator,
        principal=replace(bypass_operator.principal, principal_id="operator:attacker"),
    )
    request = _audio_request(attacker)
    victim_row = SimpleNamespace(
        request_id=request_id,
        owner_principal_id="operator:victim",
        operator_session_id="victim-session",
    )

    endpoint_calls = (
        lambda: get_audio(request_id, request),
        lambda: process_audio(request_id, request),
        lambda: confirm_audio(
            request_id,
            TranscriptConfirmationBody(
                transcript="private transcript",
                expected_transcript_digest="a" * 64,
            ),
            request,
        ),
        lambda: cancel_audio(request_id, request),
    )

    with (
        patch.object(default_audio_worker, "_job", new_callable=AsyncMock, return_value=victim_row) as read_row,
        patch.object(default_audio_worker, "_expire_if_needed", new_callable=AsyncMock) as expire,
        patch.object(default_audio_worker, "_cleanup_job_paths") as cleanup,
    ):
        for call in endpoint_calls:
            with pytest.raises(HTTPException) as error:
                await call()
            assert error.value.status_code == 404
            assert error.value.detail == {"code": "audio_job_not_found"}

    assert read_row.await_count == len(endpoint_calls)
    expire.assert_not_awaited()
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_audio_job_owner_match_retains_expiring_snapshot_path():
    request_id = "audio-authorized-boundary"
    operator = _test_bypass_operator()
    request = _audio_request(operator)
    row = SimpleNamespace(
        request_id=request_id,
        owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id,
    )
    snapshot = object()

    with (
        patch.object(default_audio_worker, "_job", new_callable=AsyncMock, return_value=row),
        patch.object(default_audio_worker, "_snapshot_by_request", new_callable=AsyncMock, return_value=snapshot) as read_snapshot,
    ):
        result = await _owned_job(request_id, request)

    assert result[0] is snapshot
    read_snapshot.assert_awaited_once_with(
        request_id,
        owner_principal_id=operator.principal.principal_id,
        operator_session_id=operator.session_id,
    )
