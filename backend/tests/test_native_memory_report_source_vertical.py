"""Genuine accepted report Source; mocked external browser/HTTP edges only.

This proves canonical pipeline provenance, not Chromium or native Memory success.
"""
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from config.settings import settings
from src.db.engine import get_session as canonical_session
from tests.test_browser_task_runtime import FakeBrowser, FakePage, FakeRoute, FakeRequest, FakeLocator
from tests.test_guardian_opportunity_plan_vertical import _actual_plan_journey
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


NODE = Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node')


class FulfilledText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class ByteDerivedPage(FakePage):
    async def goto(self, url, **kwargs):
        route = FakeRoute()
        self.url = url
        await self.route_handler(route, FakeRequest(url))
        assert not route.aborted and route.fulfilled is not None
        body = route.fulfilled['body']
        self.fulfilled_bytes = body if isinstance(body, bytes) else body.encode()
        parser = FulfilledText()
        parser.feed(self.fulfilled_bytes.decode('utf-8'))
        self.body_text = ' '.join(' '.join(parser.parts).split())
        return SimpleNamespace(status=route.fulfilled['status'])

    def locator(self, selector):
        assert selector == 'body'
        return FakeLocator(self.body_text)


@pytest.mark.asyncio
async def test_actual_accepted_report_source_with_mocked_browser_edge(accounting_db, real_auth, monkeypatch, record_property):
    from src.api import auth
    from src.db import engine as original
    from src.db.engine import override_session_factory
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState
    from src.browser.task_runner import BrowserTaskRunner
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.workflows.job_runtime import durable_job_repository
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board import pipelines
    from src.work_board.pipeline_cpu import read_output
    from src.work_board.review import native_report_memory_metadata

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, 'engine', engine)
    monkeypatch.setattr(original, '_db_path', str(root / 'seraph.db'))
    monkeypatch.setattr(original, 'async_session_factory', factory)
    # Rebind the historical ledger fixture to the original session owner;
    # composition publication/cleanup must remain genuine after activation.
    for target in ('src.workflows.job_runtime.get_session', 'src.workflows.durable_state.get_session',
                   'src.db.engine.get_session', 'src.auth.service.get_session',
                   'src.model_fabric.repository.get_session', 'src.api.settings.get_db',
                   'src.vault.repository.get_session', 'src.audit.repository.get_session',
                   'src.work_board.dispatcher.get_session', 'src.api.work_board.get_session',
                   'src.api.calendar.get_session', 'src.goals.repository.get_session',
                   'src.agent.session.get_session', 'src.approval.repository.get_session'):
        monkeypatch.setattr(target, canonical_session)
    factory.accounting_sessions = canonical_session
    await original.init_db()
    async with factory.accounting_sessions() as db:
        names = set((await db.execute(text('SELECT name FROM sqlite_schema'))).scalars())
        assert {'operator_principal_required_insert', 'operator_principal_required_update'} <= names
        assert len([name for name in names if name.startswith('session_recall_')]) == 15

    sessions, browsers = [], []
    create = auth.create_session
    async def observe_session(*args, **kwargs):
        result = await create(*args, **kwargs)
        sessions.append(result)
        return result
    monkeypatch.setattr(auth, 'create_session', observe_session)
    def launch():
        browser = FakeBrowser({})
        browser.context.page = ByteDerivedPage({})
        browsers.append(browser)
        return browser
    constructor = BrowserTaskRunner.__init__
    def external_browser(self, **kwargs):
        kwargs.setdefault('browser_launcher', launch)
        constructor(self, **kwargs)
    monkeypatch.setattr(BrowserTaskRunner, '__init__', external_browser)

    reviewed = reviewed_composition(node_path=NODE)
    workspace = ProductionWorkspace(host_root=root)
    host = CordisHost(node_path=NODE, service_dispatch=NativeServiceDispatcher(jobs=durable_job_repository))
    monkeypatch.setattr('src.runtime_plugins.bridge.cordis_host', host)
    with override_session_factory(factory):
        with maintenance_fence(workspace):
            async with original.get_session() as db:
                await begin_native_writer(db, owner='composition_maintenance', fresh=True)
                await initialize_fresh_deployment(db, composition_digests={domain: reviewed.composition_digest for domain in DOMAINS})
        try:
            assert await host.start(), host.snapshot()
            await _actual_plan_journey(accounting_db, real_auth, monkeypatch, 'public-evidence-report')
            assert len(sessions) == 1 and browsers and all(browser.closed for browser in browsers)
            token, operator = sessions[0]
            assert token
            owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
            async with factory.accounting_sessions() as db:
                task = (await db.scalars(select(WorkBoardTask).where(WorkBoardTask.capability_id == 'work.local-evidence-report.v1'))).one()
                attempt = (await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))).one()
                assert task.status == WorkBoardStatus.done
                output = await pipelines.verified_output(db, owner, task)
                content = read_output(output['file_path'], output['content_sha256'])
                run = (await db.scalars(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).one()
                metadata = await native_report_memory_metadata(db, task, attempt, run)
                record_property('actual_report_source', json.dumps({'task_id': task.task_id, 'attempt_id': attempt.attempt_id,
                    'input_ref': task.typed_input_ref, 'input_digest': task.typed_input_digest,
                    'report_sha256': hashlib.sha256(content).hexdigest(), 'metadata': metadata}, default=str))
                assert b'Stable public line A relevant new public release' in content
        finally:
            await host.stop(preserve_blocked=host.state == 'blocked')
            if host._cleanup_task is not None:
                await host._cleanup_task
            assert host.snapshot()['cleanup']['process_reaped'] is True
