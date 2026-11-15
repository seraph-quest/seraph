"""Actual common configuration publication against passive Home handles."""
import asyncio
from threading import Event

import pytest
from sqlalchemy import event

from src.operator.home_projection import home_projection
from src.model_fabric.configuration import (ModelFabricConfiguration,
    read_model_fabric_configuration,write_model_fabric_configuration)
from src.workspace.accounting_witness import maintenance_accounting_lock
from tests.test_home_continuation import accounting_db,home_setup
from tests.test_inference_accounting import setup_configuration
from tests.test_general_task_planner import forbid_external_inference


async def test_actual_publication_failure_reuse_and_legacy_owner(accounting_db,monkeypatch,forbid_external_inference):
    from src.workspace import accounting_witness
    setup_configuration()
    client,operator = await home_setup(accounting_db,monkeypatch)
    try:
        configured = read_model_fabric_configuration()
        before = home_projection.read_context()[-1]
        with maintenance_accounting_lock(accounting_db[0]) as workspace:
            write_model_fabric_configuration(configured,publication_workspace=workspace,
                expected_revision=configured.egress_revision)
        assert home_projection.read_context()[-1]!=before
        assert home_projection.read_context()[1].digest
        with monkeypatch.context() as scoped:
            def fail_write(*args,**kwargs):
                raise OSError('isolated configuration publication failure')
            scoped.setattr(accounting_witness,'_write_configuration_file',fail_write)
            with pytest.raises(OSError):
                write_model_fabric_configuration(configured)
        assert home_projection.read_context()[1] is None
        write_model_fabric_configuration(configured)
        assert home_projection.read_context()[1] is not None
        # The original legacy publication branch has no accounting lock/egress.
        write_model_fabric_configuration(ModelFabricConfiguration())
        assert home_projection.read_context()[1].blocked_reason=='provider_policy_unavailable'
    finally:
        await client.aclose()
        home_projection.stop()


async def test_actual_publication_serializes_and_midread_invalidates(accounting_db,monkeypatch,forbid_external_inference):
    from src.workspace import accounting_witness
    from src.goals.repository import GoalRepository
    setup_configuration()
    client,operator = await home_setup(accounting_db,monkeypatch)
    await GoalRepository().create('Private concurrency goal',owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id)
    configured = read_model_fabric_configuration()
    entered,release = Event(),Event()
    actual = accounting_witness._publish_policy_locked
    entries = []
    def publication(*args,**kwargs):
        entries.append(1)
        if len(entries)==1:
            entered.set()
            assert release.wait(10),'test did not release actual publication'
        return actual(*args,**kwargs)
    first = second = None
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(accounting_witness,'_publish_policy_locked',publication)
            first = asyncio.create_task(asyncio.to_thread(write_model_fabric_configuration,configured))
            assert await asyncio.to_thread(entered.wait,5)
            assert home_projection.read_context()[1] is None
            response = await client.get('/api/operator/continuation')
            assert response.status_code==200,response.text
            assert response.json()['programme_status']['state']=='blocked'
            second = asyncio.create_task(asyncio.to_thread(write_model_fabric_configuration,configured))
            from src.workspace.production import ProductionWorkspaceReconciliationError
            with pytest.raises(ProductionWorkspaceReconciliationError):
                await asyncio.wait_for(second,5)
            assert len(entries)==1
            release.set()
            await asyncio.wait_for(first,15)
            await asyncio.to_thread(write_model_fabric_configuration,configured)
            assert len(entries)==2 and home_projection.read_context()[1] is not None
        changed = []
        def change_during_read(connection,cursor,statement,parameters,context,many):
            if not changed and statement.lstrip().startswith('WITH'):
                changed.append(True)
                write_model_fabric_configuration(configured)
        event.listen(accounting_db[1].sync_engine,'before_cursor_execute',change_during_read)
        try:
            response = await client.get('/api/operator/continuation')
            assert response.status_code==409 and response.json()['detail']['code']=='continuation_stale'
            assert changed
        finally:
            event.remove(accounting_db[1].sync_engine,'before_cursor_execute',change_during_read)
    finally:
        release.set()
        if first or second:
            await asyncio.gather(*(job for job in (first,second) if job is not None),return_exceptions=True)
        await client.aclose()
        home_projection.stop()
