"""Named M4 governed triage acceptance cases."""

import pytest

from tests.test_work_board_m4 import (
    test_missing_openrouter_route_blocks_without_provider_call as _missing_route,
    test_proposal_accept_is_revision_goal_and_owner_fenced as _proposal_acceptance,
    test_started_duplicate_proposal_reconciles_without_second_job_or_provider as _proposal_restart,
)
from tests.test_work_board_m4_followups import test_expired_running_proposal_job_requires_reconciliation


@pytest.mark.asyncio
async def test_proposal_restart_expiry_and_acceptance(async_db, monkeypatch):
    await _proposal_restart(async_db, monkeypatch)
    test_expired_running_proposal_job_requires_reconciliation()
    await _proposal_acceptance(async_db)


@pytest.mark.asyncio
async def test_missing_openrouter_route_blocks_without_fallback(async_db, monkeypatch):
    await _missing_route(async_db, monkeypatch)
