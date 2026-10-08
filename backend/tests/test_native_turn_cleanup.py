"""Actual original Future cleanup observations; no renewed turn authority."""
import asyncio
from copy import copy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from smolagents import ToolCallingAgent

from config.settings import settings
from src.agent.native_turn_family import validate_original_cleanup, assert_cancelled_family_owner_readback
from src.agent.turn_execution import NativeTurnBlocked, NativeTurnExecution
from src.db.engine import get_session
from src.db.models import WorkflowRunState
from tests.test_native_turn_transport import ScriptedModel, ControlledScriptedModel, composition_db, native_transport


def original_app():
    from src.app import create_app
    from src.agent.native_turn_controls import NativeTurnResourceOwner
    app = create_app()
    if getattr(app.state, "native_turn_resources", None) is None:
        app.state.native_turn_resources = NativeTurnResourceOwner()
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["returned", "controlled", "unknown_cost"])
async def test_actual_original_cleanup_after_stop_keeps_owner_readback(native_transport, monkeypatch, mode):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    from src.agent.controlled_origin import validate_controlled_cleanup_origin
    from src.workflows.job_runtime import durable_job_repository
    executions = []
    original_prepare = NativeTurnExecution.prepare_agent
    def observe_prepare(execution, agent):
        executions.append(execution)
        return original_prepare(execution, agent)
    monkeypatch.setattr(NativeTurnExecution, "prepare_agent", observe_prepare)
    model = ControlledScriptedModel("clarify") if mode == "controlled" else ScriptedModel()
    if mode == "unknown_cost":
        model.cost = None
    agent = ToolCallingAgent(tools=[clarify] if mode == "controlled" else [], model=model,
        max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    app = original_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]},
        headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-cleanup-" + mode})
    assert response.status_code == {"unknown_cost": 503, "controlled": 409, "returned": 200}[mode], response.text
    assert len(executions) == 1 and model.calls == 1
    execution = executions[0]
    worker = execution.worker
    assert isinstance(worker, asyncio.Future) and worker.done() and not worker.cancelled()
    execution.close_transport()
    with pytest.raises(asyncio.TimeoutError):
        execution.remaining()
    witness = validate_original_cleanup(execution, worker)
    assert witness.worker is worker and witness.execution is execution
    assert witness.outcome == ("raised" if mode == "controlled" else "returned")
    assert len(witness.operations) == 1 and witness.operations[0].consumed
    with pytest.raises(NativeTurnBlocked):
        validate_original_cleanup(copy(execution), worker)
    unrelated = asyncio.get_running_loop().create_future()
    unrelated.set_result("not the original worker")
    with pytest.raises(NativeTurnBlocked):
        validate_original_cleanup(execution, unrelated)
    async with durable_job_repository._writer_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == execution.admission.job_id))
        if mode == "unknown_cost":
            with pytest.raises(NativeTurnBlocked, match="native_turn_family_cost_liability"):
                await assert_cancelled_family_owner_readback(db, run, execution, worker)
        else:
            assert await assert_cancelled_family_owner_readback(db, run, execution, worker) is witness
    if mode == "controlled":
        origin = validate_controlled_cleanup_origin(witness.exception, cleanup_witness=witness)
        assert origin.execution is execution and origin.issued_exception is worker.exception()
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
async def test_actual_prepared_generic_empty_coroutine_has_no_cleanup_authority(native_transport, monkeypatch):
    from src.app import create_app
    original_execute = NativeTurnExecution.execute
    executions = []
    async def replace_callback(execution, awaitable):
        executions.append(execution)
        awaitable.close()
        async def unrelated():
            return "foreign result"
        return await original_execute(execution, unrelated())
    monkeypatch.setattr(NativeTurnExecution, "execute", replace_callback)
    app = original_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]},
        headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-cleanup-empty-coroutine"})
    assert response.status_code == 503, response.text
    assert len(executions) == 1 and native_transport[3].calls == 0
    execution = executions[0]
    execution.close_transport()
    with pytest.raises(NativeTurnBlocked, match="callback_completion_unproven"):
        validate_original_cleanup(execution, execution.worker)
    await app.state.native_turn_resources.shutdown()
