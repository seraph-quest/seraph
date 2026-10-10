"""Original ledger owners plus genuine selected Common33; source-only candidate."""
import pytest
import asyncio
from dataclasses import replace
from sqlalchemy import event

from src.db.engine import get_session
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, _trace_memory_numeric_charges
from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
from src.runtime_plugins.ownership import begin_native_writer
from src.runtime_plugins.bridge import cordis_host
from src.guardian.goal_discovery import goal_discovery_service
from src.work_board.research_parent import DISCOVERY_CAPABILITY
from src.guardian.goal_programmes import goal_programme_service
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.job_runtime import DurableJobRepository
from tests.test_inference_accounting import request
from tests.test_programme_prospective_live_selection import genuine_live_selection, private_outputs, SelectionObserved
from tests.test_auth_session_composition_privacy import sql_state


@pytest.fixture
def original_selection_accounting_setup(mode):
    async def populate_original_ledger():
        callbacks = []
        async def intercepted_transport():
            callbacks.append('original-ledger-callback')
            return {'usage': {'cost': '0.0000061'}}
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(
            request('original-discovery-body-budget-' + mode), intercepted_transport)
        ledger = await DurableJobRepository().inference_accounting_snapshot()
        assert ledger['status'] == 'ready'
        assert len(ledger['operations']) == 1
        assert ledger['operations'][0]['state'] == 'settled'
        assert callbacks == ['original-ledger-callback']
        return {'callbacks': len(callbacks), 'settled_count': len(ledger['operations']),
            'accounting_ready': ledger['status'] == 'ready'}
    return populate_original_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['positive'])
@pytest.mark.parametrize('mode', ['selected', 'headers_fit_body_denied', 'owner_fits_cost_denied', 'copied', 'foreign', 'default'])
async def test_original_populated_ledger_body_payer(genuine_live_selection, monkeypatch, scenario, mode):
    case = genuine_live_selection
    # Observations are diagnostics only; the original selected payer validates
    # actual current DB/Common33/checkpoint continuity after the transition.
    assert case.accounting_observation == {'callbacks': 1, 'settled_count': 1, 'accounting_ready': True}
    assert case.contacts == []
    original_sql, original_files = sql_state(case), private_outputs(case)

    observed = []
    async def boundary(**requested):
        assert requested['goal_id'] == case.goal_id
        assert requested['programme_id'] == case.programme['id']
        assert requested['grant_revision'] == case.programme['grant_revision']
        assert requested['capability_id'] == DISCOVERY_CAPABILITY
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner='finite_service', header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda conn:
                _certify_current_memory_snapshot_on_connection(conn, budget))
            programme, binding, common, identity = await connection.run_sync(lambda conn: guard._select_programme_admission(
                conn, common, service=goal_discovery_service, host=cordis_host,
                goal_id=case.goal_id, programme_id=case.programme['id'],
                grant_revision=case.programme['grant_revision']))
            current = await goal_programme_service.validate_current_binding(
                db=db, binding=binding, policy=case.policy,
                original_admission_guard=guard, identity_certificate=identity)
            assert current == programme
            assert guard._prospective_admission[0].common33 is common
            assert common.budget is guard.header_budget is budget
            assert sum(name == 'inference_cost_reservations' for name, _ in common.rows) == 1
            assert guard._original_programme_admission_owner(goal_discovery_service, cordis_host) is asyncio.current_task()
            assert 'composition_accounting_payload' not in db.info
            certified, bodies = [], []
            original_certify, original_all = HeaderReadBudget.certify, HeaderReadBudget.certify_all
            original_debit = HeaderReadBudget.debit
            body_debits = []
            def observed_debit(self, amount, *, appearance=None):
                before = self.remaining
                result = original_debit(self, amount, appearance=appearance)
                if self is budget and isinstance(appearance, tuple) and appearance[:2] in {
                        ('body', 'inference_accounting_owners'),
                        ('table-body', 'inference_cost_reservations')}:
                    body_debits.append((appearance, amount, before, self.remaining))
                return result
            monkeypatch.setattr(HeaderReadBudget, 'debit', observed_debit)
            async def certify(self, session, descriptor, row_ids):
                result = await original_certify(self, session, descriptor, row_ids)
                if self is budget and descriptor.table == 'inference_accounting_owners':
                    certified.append('owner')
                return result
            async def certify_all(self, session, descriptor):
                result = await original_all(self, session, descriptor)
                if self is budget and descriptor.table == 'inference_cost_reservations':
                    certified.append('costs')
                    if mode == 'headers_fit_body_denied':
                        budget.debit(budget.remaining, appearance=('test-owned-capacity-exhaustion',))
                return result
            monkeypatch.setattr(HeaderReadBudget, 'certify', certify)
            monkeypatch.setattr(HeaderReadBudget, 'certify_all', certify_all)
            original_get = db.get
            async def observed_get(model, identity, **kwargs):
                result = await original_get(model, identity, **kwargs)
                if mode == 'owner_fits_cost_denied' and model.__name__ == 'InferenceAccountingOwner':
                    budget.debit(budget.remaining, appearance=('test-owned-cost-capacity-exhaustion',))
                return result
            monkeypatch.setattr(db, 'get', observed_get)
            def delivered(conn, cursor, statement, parameters, context, executemany):
                normalized = ' '.join(statement.lower().split())
                for table in ('inference_accounting_owners', 'inference_cost_reservations'):
                    if normalized.startswith('select ') and ('from ' + table) in normalized:
                        # Whole ORM bodies have model-qualified columns; header SQL
                        # uses bounded expressions and quoted identifiers.
                        if table + '.' not in normalized:
                            continue
                        assert 'owner' in certified and 'costs' in certified
                        if mode != 'default':
                            expected = 'body' if table == 'inference_accounting_owners' else 'table-body'
                            assert charges[-1][0][:2] == (expected, table)
                            bound = (common.rows[(table, 'deployment')][1]
                                if table == 'inference_accounting_owners' else
                                sum(cost for (name, _), (_, cost) in common.rows.items() if name == table))
                            assert charges[-1][1] == bound
                            appearance, amount, before, after = body_debits[-1]
                            assert appearance == charges[-1][0] and amount == bound
                            assert before - after == bound
                        bodies.append(table)
            event.listen(case.engine.sync_engine, 'before_cursor_execute', delivered)
            try:
                with _trace_memory_numeric_charges(budget) as charges:
                    if mode in {'copied', 'foreign'}:
                        from src.workspace.production import ProductionWorkspaceReconciliationError
                        selected = replace(common, **({'budget': HeaderReadBudget()} if mode == 'foreign' else {}))
                        with pytest.raises(ProductionWorkspaceReconciliationError, match='programme_numeric_publication_unavailable'):
                            await guard._publication_accounting_payload(_numeric_body_certificate=selected)
                        assert certified == [] and bodies == []
                    elif mode in {'headers_fit_body_denied', 'owner_fits_cost_denied'}:
                        with pytest.raises(HeaderBoundsError, match='canonical_bound_not_certified'):
                            await guard._publication_accounting_payload(_numeric_body_certificate=common)
                        assert bodies == ([] if mode == 'headers_fit_body_denied' else ['inference_accounting_owners'])
                        assert budget.remaining == 0
                    else:
                        payload = await guard._publication_accounting_payload(
                            **({'_numeric_body_certificate': common} if mode == 'selected' else {}))
                        assert payload['witness'] and payload['secret_values_included'] is False
                        assert bodies == ['inference_accounting_owners', 'inference_cost_reservations']
            finally:
                event.remove(case.engine.sync_engine, 'before_cursor_execute', delivered)
            assert guard._original_programme_admission_owner(goal_discovery_service, cordis_host) is asyncio.current_task()
            observed.append(mode)
            raise SelectionObserved("accounting body payer observed; no admission")
    monkeypatch.setattr(goal_programme_service, 'assert_authority', boundary)
    with pytest.raises(SelectionObserved, match='^accounting body payer observed; no admission$'):
        await goal_discovery_service.admit(goal_id=case.goal_id, programme_id=case.programme['id'],
            grant_revision=case.programme['grant_revision'])
    assert observed == [mode] and goal_discovery_service._tasks == set()
    assert case.contacts == []
    assert sql_state(case) == original_sql and private_outputs(case) == original_files
