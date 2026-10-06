"""Provider-free M6 procedure journey through board dispatch and readback.

The routine and source-watch services use a temporary SQLite workspace and
deterministic local fetches. GitHub publication uses an intercepted transport,
so the reviewed routine runs against a second goal and verifies its output
without contacting a provider or external service.
"""

from __future__ import annotations

import hashlib
import json
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession
from sqlmodel import select
from sqlmodel import SQLModel

from config.settings import settings
from src.db import engine as db_engine
from src.auth.service import AuthenticatedOperator
from src.auth import service as auth_service_module
from src.db.models import (
    Goal,
    OperatorSession,
    GuardianDecisionPacket,
    GitHubFollowthroughConnection,
    WorkBoardAttempt,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.approval import repository as approval_repository_module
from src.audit import repository as audit_repository_module
from src.vault import repository as vault_repository_module
from src.guardian.source_watch import SourceWatchService
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.contracts import (
    WorkBoardLinkCreate,
    WorkBoardOwner,
    WorkBoardRoutinePublicationPrepareRequest,
    WorkBoardTaskCreate,
)
from src.work_board.repository import WorkBoardRepository
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.api import work_board as work_board_api
from src.workflows import durable_state
from src.workflows.job_runtime import durable_job_repository
from src.workflows import job_runtime
from src.workflows.routines import (
    RoutineActivateRequest,
    RoutineError,
    RoutineFromBoardCreateRequest,
    RoutineFromBoardPreviewRequest,
    RoutineInstallRequest,
    RoutineInvokeRequest,
    RoutinePackageActivationRequest,
    RoutinePackageDecisionRequest,
    RoutinePublicationRequest,
    RoutineService,
)
import src.workflows.routines as routines_module
import src.work_board.dispatcher as dispatcher_module
import src.extensions.github_followthrough as github_followthrough_module
from src.extensions.github_followthrough import (
    ACTION_CREATE_ISSUE,
    GitHubFollowthroughService,
)
from src.extensions.capability_pack import CapabilityPackLifecycle, CapabilityPackLifecycleError
from src.workflows.routines import _routine_pack_id


OWNER = "operator:root:m6-integration"
SESSION = "session:m6-integration"
GRANT = "grant:m6-provider-free"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_typed_input(workspace: Path, capability_id: str, name: str, inputs: dict) -> tuple[str, str]:
    relative = f"artifacts/work-board/{name}.json"
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"schema_version": 1, "capability_id": capability_id, "input": inputs},
        sort_keys=True,
        separators=(",", ":"),
    )
    path.write_text(content, encoding="utf-8")
    return f"workspace-json:{relative}", _sha(content)


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Use a private synchronous SQLite file behind the real async contracts.

    The repository's normal async SQLite driver is unavailable in the
    restricted test runner because its worker-thread callback never resumes
    SQLAlchemy's greenlet bridge.  This adapter preserves the production
    session contract while keeping every query, transaction, and model write
    on a real temporary SQLite database.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "workspace.sqlite"
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    sync_engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(sync_engine)

    class AsyncSessionAdapter:
        def __init__(self, session: SyncSession):
            self._session = session
            self.info = session.info

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            self._session.close()

        def add(self, instance):
            return self._session.add(instance)

        def add_all(self, instances):
            return self._session.add_all(instances)

        def expunge(self, instance):
            return self._session.expunge(instance)

        def get_bind(self):
            return self._session.get_bind()

        def in_transaction(self):
            return self._session.in_transaction()

        @property
        def new(self):
            return self._session.new

        @property
        def dirty(self):
            return self._session.dirty

        @property
        def deleted(self):
            return self._session.deleted

        async def execute(self, statement, *args, **kwargs):
            return self._session.execute(statement, *args, **kwargs)

        async def scalar(self, statement, *args, **kwargs):
            return self._session.scalar(statement, *args, **kwargs)

        async def get(self, entity, identity, *args, **kwargs):
            return self._session.get(entity, identity, *args, **kwargs)

        async def run_sync(self, function, *args, **kwargs):
            return function(self._session, *args, **kwargs)

        async def flush(self, *args, **kwargs):
            return self._session.flush(*args, **kwargs)

        async def commit(self):
            return self._session.commit()

        async def rollback(self):
            return self._session.rollback()

        async def refresh(self, instance, *args, **kwargs):
            return self._session.refresh(instance, *args, **kwargs)

        async def delete(self, instance):
            return self._session.delete(instance)

        class _NestedTransaction:
            def __init__(self, session):
                self._session = session
                self._transaction = None

            async def __aenter__(self):
                self._transaction = self._session.begin_nested()
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                if self._transaction is None:
                    return None
                if exc_type is None:
                    self._transaction.commit()
                else:
                    self._transaction.rollback()
                return None

        def begin_nested(self):
            return self._NestedTransaction(self._session)

    @asynccontextmanager
    async def get_session():
        db = AsyncSessionAdapter(SyncSession(sync_engine, expire_on_commit=False))
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.__aexit__(None, None, None)

    monkeypatch.setattr(db_engine, "get_session", get_session)
    monkeypatch.setattr(durable_state, "get_session", get_session)
    monkeypatch.setattr(job_runtime, "get_session", get_session)
    monkeypatch.setattr(approval_repository_module, "get_session", get_session)
    monkeypatch.setattr(audit_repository_module, "get_session", get_session)
    monkeypatch.setattr(vault_repository_module, "get_session", get_session)
    monkeypatch.setattr(auth_service_module, "get_session", get_session)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    # Standing public watches retain the actual original Root identity and a
    # finite reviewed grant. A data-only Goal/session fixture cannot authorize
    # reads through the current SourceWatchService authority contract.
    now = datetime.now(timezone.utc)
    with SyncSession(sync_engine) as db:
        db.add(OperatorSession(
            id=SESSION, principal_id=OWNER, token_hash=_sha("m6-provider-free-root"),
            idle_expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=2),
        ))
        db.commit()
    yield get_session, workspace
    sync_engine.dispose()


def _goal(goal_id: str, title: str) -> Goal:
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id=GRANT,
        max_outstanding_jobs=2,
        max_attempts=1,
        max_runtime_seconds=300,
        notifications_per_day=0,
        period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        period_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        timezone="UTC",
    )
    return Goal(
        id=goal_id,
        title=title,
        description="M6 provider-free source-watch integration goal",
        status="active",
        revision=1,
        proactive_enabled=True,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        admission_budget_json=serialize_admission_budget(budget),
    )


@pytest.mark.asyncio
async def test_second_goal_source_watch_executes_with_durable_readback(
    isolated_runtime,
):
    """Execute two real watches and prove the second goal is independently bound."""

    get_session, workspace = isolated_runtime
    async with get_session() as db:
        db.add_all(
            [
                _goal("goal-m6-first", "First verified research goal"),
                _goal("goal-m6-second", "Second verified research goal"),
            ]
        )

    source_versions = {
        "first": (
            "first baseline\nstable release\n",
            "first baseline\nverified release\n",
        ),
        "second": (
            "second baseline\nstable release\n",
            "second baseline\nverified release with a bounded change\n",
        ),
    }
    fetch_counts: dict[str, int] = {"first": 0, "second": 0}

    async def fetcher(source):
        key = "first" if source.source_key == "first-source" else "second"
        index = fetch_counts[key]
        fetch_counts[key] += 1
        return source_versions[key][min(index, 1)], {"content_type": "text/plain"}

    service = SourceWatchService(fetcher=fetcher)
    watch_args = {
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "expected_goal_revision": 1,
        "criteria": {
            "include_terms": [],
            "exclude_terms": [],
            "min_changed_lines": 1,
            "min_changed_chars": 1,
            "max_material_sources": 3,
        },
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "write_mode": "standing_reviewed",
        "reviewed_grant_id": GRANT,
    }
    first_watch = await service.create_watch(
        **watch_args,
        goal_id="goal-m6-first",
        sources=[
            {
                "source_key": "first-source",
                "kind": "public_https_text",
                "target": "https://example.com/m6-first.txt",
                "label": "first source",
                "priority": 3,
            }
        ],
    )
    second_watch = await service.create_watch(
        **watch_args,
        goal_id="goal-m6-second",
        sources=[
            {
                "source_key": "second-source",
                "kind": "public_https_text",
                "target": "https://example.com/m6-second.txt",
                "label": "second source",
                "priority": 3,
            }
        ],
    )

    # The first occurrence initializes each canonical baseline.  The second
    # occurrence observes a real changed source and runs the local write path.
    first_baseline = await service.run_watch(
        first_watch["id"],
        occurrence_id="first-baseline",
        expected_plan_revision=1,
        expected_owner_session_id=SESSION,
    )
    first_change = await service.run_watch(
        first_watch["id"],
        occurrence_id="first-change",
        expected_plan_revision=1,
        expected_owner_session_id=SESSION,
    )
    second_baseline = await service.run_watch(
        second_watch["id"],
        occurrence_id="second-baseline",
        expected_plan_revision=1,
        expected_owner_session_id=SESSION,
    )
    second_change = await service.run_watch(
        second_watch["id"],
        occurrence_id="second-change",
        expected_plan_revision=1,
        expected_owner_session_id=SESSION,
    )

    assert first_baseline["status"] == "baseline_initialized", first_baseline
    assert second_baseline["status"] == "baseline_initialized"
    assert first_change["status"] == "succeeded"
    assert second_change["status"] == "succeeded"
    assert fetch_counts == {"first": 2, "second": 2}
    assert first_change["job_id"] != second_change["job_id"]

    async with get_session() as db:
        second_packet = (
            await db.execute(
                select(GuardianDecisionPacket).where(
                    GuardianDecisionPacket.id == second_change["packet_id"]
                )
            )
        ).scalars().one()
        second_job = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == second_change["job_id"]
                )
            )
        ).scalars().one()

    assert second_packet.goal_id == "goal-m6-second"
    assert second_packet.goal_revision == 1
    assert second_packet.status == "succeeded"
    assert second_packet.verification_status == "passed"
    assert second_packet.memory_status == "no_learning"
    assert second_packet.dossier_artifact_id
    assert second_packet.task_artifact_id
    assert second_packet.dossier_sha256 == _sha(
        (workspace / second_packet.dossier_path).read_text()
    )
    assert second_packet.task_sha256 == _sha(
        (workspace / second_packet.task_path).read_text()
    )
    assert second_packet.outcome_json
    assert json.loads(second_packet.outcome_json)["readback"] == "passed"

    # The durable execution record, rather than the returned service mapping,
    # must carry independent verified readback receipts for both files.
    assert second_job.status == "succeeded"
    assert second_job.goal_id == "goal-m6-second"
    assert second_job.goal_revision == 1
    job_projection = await durable_job_repository.get_job(second_change["job_id"])
    assert job_projection is not None
    verified_paths = {
        str(item.get("target_path"))
        for item in job_projection["effects"]
        if item.get("receipt_kind") == "readback"
        and item.get("status") == "succeeded"
        and isinstance(item.get("details"), dict)
        and item["details"].get("verified") is True
    }
    assert verified_paths == {
        second_packet.dossier_path,
        second_packet.task_path,
    }

    # The source-watch lookup is owner/session bound and points to the second
    # goal, proving that this result was not replayed from the first journey.
    second_read = await service.get_watch(
        second_watch["id"],
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert second_read is not None
    assert second_read["goal_id"] == "goal-m6-second"
    assert second_read["latest_packet"]["id"] == second_packet.id
    assert second_read["latest_packet"]["verification_status"] == "passed"


@pytest.mark.asyncio
async def test_reviewed_routine_second_goal_runs_through_board_dispatch_and_readback(
    isolated_runtime,
    monkeypatch,
):
    """A reviewed procedure must execute and verify its second approved goal.

    The owner-bound GitHub connection uses an intercepted HTTP transport. No
    provider or external service is contacted. The real board dispatcher,
    source-watch execution, publication approval, destination request, and
    independent readback all run through their production code paths.
    """

    get_session, workspace = isolated_runtime
    first_goal = _goal("goal-m6-routine-source", "Verified source journey goal")
    second_goal = _goal("goal-m6-routine-second", "Second approved routine goal")
    async with get_session() as db:
        db.add_all([first_goal, second_goal])

    source_versions = {
        "first": ("first routine baseline\n", "first routine change\n"),
        "second": ("second routine baseline\n", "second routine verified change\n"),
    }
    fetch_counts = {"first": 0, "second": 0}

    async def fetcher(source):
        key = "first" if source.source_key == "routine-first-source" else "second"
        index = fetch_counts[key]
        fetch_counts[key] += 1
        return source_versions[key][min(index, 1)], {"content_type": "text/plain"}

    service = SourceWatchService(fetcher=fetcher)
    monkeypatch.setattr("src.guardian.source_watch.source_watch_service", service)
    monkeypatch.setattr(routines_module, "source_watch_service", service)

    watch_args = {
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "expected_goal_revision": 1,
        "criteria": {
            "include_terms": [],
            "exclude_terms": [],
            "min_changed_lines": 1,
            "min_changed_chars": 1,
            "max_material_sources": 3,
        },
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "write_mode": "standing_reviewed",
        "reviewed_grant_id": GRANT,
    }
    first_watch = await service.create_watch(
        **watch_args,
        goal_id=first_goal.id,
        sources=[
            {
                "source_key": "routine-first-source",
                "kind": "public_https_text",
                "target": "https://example.com/m6-routine-first.txt",
                "label": "first routine source",
                "priority": 3,
            }
        ],
    )
    second_watch = await service.create_watch(
        **watch_args,
        goal_id=second_goal.id,
        sources=[
            {
                "source_key": "routine-second-source",
                "kind": "public_https_text",
                "target": "https://example.com/m6-routine-second.txt",
                "label": "second routine source",
                "priority": 3,
            }
        ],
    )

    async def fake_get_connection(_self, owner_principal_id):
        assert owner_principal_id == OWNER
        return {
            "id": "intercepted-github-connection",
            "mode": "active",
            "credential_configured": True,
            "revision": 1,
            "repository": "example/repo",
        }

    async def fake_authenticate(_session_id, *, touch=False):
        assert touch is False
        return SimpleNamespace(
            session_id=SESSION,
            principal=SimpleNamespace(
                principal_id=OWNER,
                grants=("external_mutation",),
            ),
        )

    github_requests = []

    async def github_handler(request):
        github_requests.append(request.method)
        assert request.headers["authorization"] == "Bearer intercepted-test-token"
        if request.method == "POST":
            posted = json.loads(request.content)
            assert posted["title"] == "Second goal verified follow-through"
            github_result.clear()
            github_result.update(
                {
                    "number": 481,
                    "title": posted["title"],
                    "body": posted["body"],
                    "html_url": "https://github.com/example/repo/issues/481",
                }
            )
            return httpx.Response(201, json=github_result, request=request)
        assert request.method == "GET"
        assert request.url.path == "/repos/example/repo/issues/481"
        return httpx.Response(200, json=github_result, request=request)

    github_result = {}

    async def fake_resolver(hostname, port):
        assert hostname == "api.github.com"
        assert port == 443
        return ["93.184.216.34"]

    original_service_init = GitHubFollowthroughService.__init__

    def intercepted_service_init(self, *, resolver=None, transport=None, sleep=asyncio.sleep):
        original_service_init(
            self,
            resolver=fake_resolver,
            transport=httpx.MockTransport(github_handler),
            sleep=sleep,
        )

    monkeypatch.setattr(GitHubFollowthroughService, "__init__", intercepted_service_init)
    monkeypatch.setattr(github_followthrough_module, "authenticate_session", fake_authenticate)
    monkeypatch.setattr(dispatcher_module, "authenticate_session", fake_authenticate)
    # Preserve the real owner Vault and finite consent binding, not a key-is-
    # consent shortcut. Only the external GitHub HTTP boundary is intercepted.
    await vault_repository_module.vault_repository.store(
        "m6/intercepted-github-token", "intercepted-test-token", owner_principal_id=OWNER,
    )
    snapshot = await vault_repository_module.vault_repository.snapshot(
        "m6/intercepted-github-token", owner_principal_id=OWNER,
    )
    from src.extensions.github_consent import GitHubConsentRequest, issuance
    canonical_operator = await auth_service_module.authenticate_session(SESSION, touch=False)
    async with get_session() as db:
        connection = GitHubFollowthroughConnection(
                id="intercepted-github-connection",
                owner_principal_id=OWNER,
                repository="example/repo",
                vault_key="m6/intercepted-github-token",
                revision=1,
                mode="active",
            )
        for field, value in issuance(
            connection, canonical_operator,
            GitHubConsentRequest(acknowledged=True, duration_seconds=3600, actions=["github_issue_write"]),
            snapshot.binding_digest, 1,
        ).items():
            setattr(connection, field, value)
        db.add(connection)
    monkeypatch.setattr(GitHubFollowthroughService, "get_connection", fake_get_connection)

    # Initialize the source baseline, then build both source and action cards
    # through the canonical repository, dependency, dispatcher, and attempt
    # projection paths. The GitHub transport remains intercepted above.
    first_baseline = await service.run_watch(
        first_watch["id"],
        occurrence_id="m6-routine-source-baseline",
        expected_plan_revision=int(first_watch["plan_revision"]),
        expected_owner_session_id=SESSION,
    )
    assert first_baseline["status"] == "baseline_initialized", first_baseline
    board_owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    board_repository = WorkBoardRepository()
    source_input_ref, source_input_digest = _write_typed_input(
        workspace,
        "guardian.research-watch.v1",
        "m6-routine-source",
        {"watch_id": first_watch["id"], "expected_plan_revision": int(first_watch["plan_revision"])},
    )
    async with get_session() as db:
        source_created = await board_repository.create_task(
            db,
            board_owner,
            WorkBoardTaskCreate(
                title="Verified source watch",
                body="Observe the approved source and preserve its verified artifact receipt.",
                goal_id=first_goal.id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="guardian.research-watch.v1",
                typed_input_ref=source_input_ref,
                typed_input_digest=source_input_digest,
                idempotency_scope="m6-journey",
                idempotency_key="m6-routine-source-task",
            ),
        )
    source_task_id = source_created.task.task_id
    dispatcher = WorkBoardDispatcher(session_provider=get_session)
    source_dispatch = await dispatcher.run_pass()
    assert source_dispatch["claimed"] == 1
    assert source_dispatch["admitted"] == 1
    assert source_dispatch["completed"] == 1
    async with get_session() as db:
        source_detail = await board_repository.get_detail(db, board_owner, source_task_id)
    assert source_detail["task"].status is WorkBoardStatus.done
    source_attempt = source_detail["attempts"][0]
    source_job_id = str(source_attempt.workflow_run_id)
    source_job = await durable_job_repository.get_job(source_job_id)
    assert source_job is not None and source_job["status"] == "succeeded"
    async with get_session() as db:
        source_packet = (
            await db.execute(
                select(GuardianDecisionPacket).where(
                    GuardianDecisionPacket.run_identity == source_job_id
                )
            )
        ).scalars().one()
    action_input_ref, action_input_digest = _write_typed_input(
        workspace,
        "work.github-followthrough.v1",
        "m6-routine-action",
        {
            "dossier_artifact_id": source_packet.dossier_artifact_id,
            "dossier_sha256": source_packet.dossier_sha256,
            "connection_revision": 1,
            "action": ACTION_CREATE_ISSUE,
            "title": "Second goal verified follow-through",
            "body": "Verified action receipt used as a reusable procedure source.",
        },
    )
    async with get_session() as db:
        action_created = await board_repository.create_task(
            db,
            board_owner,
            WorkBoardTaskCreate(
                title="Verified GitHub action",
                body="Publish the operator-approved summary and read it back from its exact destination.",
                goal_id=first_goal.id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="work.github-followthrough.v1",
                typed_input_ref=action_input_ref,
                typed_input_digest=action_input_digest,
                idempotency_scope="m6-journey",
                idempotency_key="m6-routine-action-task",
            ),
        )
        action_task_id = action_created.task.task_id
        await board_repository.add_link(
            db,
            board_owner,
            WorkBoardLinkCreate(
                parent_task_id=source_task_id,
                child_task_id=action_task_id,
                expected_child_revision=action_created.task.task_revision,
            ),
        )
    action_prepare = await dispatcher.run_pass()
    assert action_prepare["claimed"] == 1
    async with get_session() as db:
        action_detail = await board_repository.get_detail(db, board_owner, action_task_id)
    action_attempt = action_detail["attempts"][0]
    action_job_id = str(action_attempt.workflow_run_id)
    action_job = await durable_job_repository.get_job(action_job_id)
    assert action_job is not None and action_job["status"] == "awaiting_approval"
    action_approval_id = str((action_job.get("declared_authority") or {}).get("approval_id") or "")
    assert action_approval_id
    resolved_action = await approval_repository_module.approval_repository.resolve(
        action_approval_id,
        "approved",
    )
    assert resolved_action is not None and resolved_action.status == "approved"
    action_execution = await dispatcher.run_pass()
    assert action_execution["reconciled"] >= 1
    async with get_session() as db:
        action_detail = await board_repository.get_detail(db, board_owner, action_task_id)
    assert action_detail["task"].status is WorkBoardStatus.done
    assert action_detail["attempts"][0].outcome == "verified"
    action_job = await durable_job_repository.get_job(action_job_id)
    assert action_job is not None and action_job["status"] == "succeeded"
    routines = RoutineService()
    preview_request = RoutineFromBoardPreviewRequest(
        source_task_id=source_task_id,
        action_task_id=action_task_id,
        expected_source_revision=source_detail["task"].task_revision,
        expected_action_revision=action_detail["task"].task_revision,
        name="Reviewed source-watch routine",
        idempotency_key="m6-board-routine-create",
    )
    procedure_preview = await routines.preview_from_board(
        preview_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert procedure_preview["source_refs"]["source_watch_job_id"] == source_job_id
    assert procedure_preview["source_refs"]["source_m3_job_id"] == action_job_id
    created = await routines.create_from_board(
        RoutineFromBoardCreateRequest(
            **preview_request.model_dump(),
            preview_digest=procedure_preview["preview_digest"],
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert created["status"] == "prepared"
    routine_id = str(created["routine_id"])
    install_approval_id = str(created["approval_id"])
    install_approval = await approval_repository_module.approval_repository.resolve(
        install_approval_id,
        "approved",
    )
    assert install_approval is not None and install_approval.status == "approved"
    installed = await routines.install(
        routine_id,
        RoutineInstallRequest(
            version=1,
            expected_routine_revision=1,
            approval_id=install_approval_id,
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert installed["state"] == "installed"
    installed_revision = int(installed["revision"])
    reviewed = await routines.review_package(
        routine_id,
        1,
        expected_routine_revision=installed_revision,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert reviewed["digest"] == installed["versions"][0]["installed_package_digest"]
    prepared = await routines.prepare_package_approval(
        routine_id,
        1,
        expected_routine_revision=installed_revision,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    package_approval_id = prepared["approval"]["approval_id"]
    decided = await routines.decide_package_approval(
        routine_id,
        1,
        package_approval_id,
        RoutinePackageDecisionRequest(expected_routine_revision=installed_revision, decision="approved"),
        expected_routine_revision=installed_revision,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert decided["approval"]["status"] == "approved"
    activated = await routines.activate_package(
        routine_id,
        1,
        RoutinePackageActivationRequest(
            expected_routine_revision=installed_revision,
            approval_id=package_approval_id,
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert activated["status"] == "active"
    active_routine = await routines.activate(
        routine_id,
        RoutineActivateRequest(expected_routine_revision=installed_revision, version=1),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert active_routine["state"] == "active"
    active_revision = int(active_routine["revision"])
    exported_procedure = await routines.export_procedure(
        routine_id,
        1,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert exported_procedure["kind"] == "seraph.reviewed_procedure.v1"
    assert exported_procedure["package_digest"] == reviewed["digest"]
    assert [step["id"] for step in exported_procedure["runbook"]["procedure"]["steps"]] == [
        "guardian_watch_run",
        "github_followthrough",
    ]
    exported_text = json.dumps(exported_procedure, sort_keys=True)
    assert install_approval_id not in exported_text
    assert package_approval_id not in exported_text
    assert "intercepted-github-token" not in exported_text
    assert "intercepted-test-token" not in exported_text
    with pytest.raises(RoutineError, match="routine_owner_session_mismatch"):
        await routines.export_procedure(
            routine_id,
            1,
            owner_principal_id=OWNER,
            owner_session_id="session:m6-other",
        )
    initial_publication_requests = len(github_requests)

    # Initialize the second goal's baseline.  The routine's actual board run
    # must consume the next fetch and produce the changed, verified result.
    baseline = await service.run_watch(
        second_watch["id"],
        occurrence_id="m6-routine-second-baseline",
        expected_plan_revision=int(second_watch["plan_revision"]),
        expected_owner_session_id=SESSION,
    )
    assert baseline["status"] == "baseline_initialized"

    invocation = await asyncio.wait_for(routines.invoke(
        routine_id,
        RoutineInvokeRequest(
            version=1,
            expected_routine_revision=active_revision,
            goal_id=second_goal.id,
            expected_goal_revision=1,
            source_watch_id=second_watch["id"],
            expected_watch_revision=int(second_watch["plan_revision"]),
            invocation_uuid=str(uuid.uuid4()),
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    ), timeout=15)
    assert invocation["status"] == "queued"
    task_id = invocation["task_id"]

    dispatcher = WorkBoardDispatcher(session_provider=get_session)
    first_pass = await dispatcher.run_pass()
    assert first_pass["claimed"] == 1
    assert first_pass["admitted"] == 1

    async with get_session() as db:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task_id)
                .order_by(WorkBoardAttempt.started_at.desc())
            )
        ).scalars().first()
    assert task.status is WorkBoardStatus.blocked
    assert task.block_reason == "awaiting_approval"
    assert attempt is not None
    assert attempt.ended_at is None
    assert attempt.lease_owner is None
    assert attempt.lease_expires_at is None
    parent_job_id = str(attempt.workflow_run_id)
    assert parent_job_id.startswith(f"routine-invocation:{routine_id}:")
    parent_job = await durable_job_repository.get_job(parent_job_id)
    assert parent_job is not None
    approval_id = str(
        (parent_job.get("declared_authority") or {}).get("approval_id") or ""
    )
    assert approval_id
    assert (await approval_repository_module.approval_repository.get(approval_id)).status == "pending"

    # Wait beyond the finite board lease while the exact approval remains
    # pending. The same card and open attempt must remain Blocked, with no
    # duplicate attempt or newly admitted work.
    attempt_id = str(attempt.attempt_id)
    original_created_at = attempt.created_at
    base_now = datetime.now(timezone.utc)
    dispatcher.now = lambda: base_now + timedelta(seconds=301)
    waited = await dispatcher.run_pass()
    assert waited["reconciled"] >= 1
    async with get_session() as db:
        waiting_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        waiting_attempts = (
            await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))
        ).scalars().all()
    assert waiting_task.status is WorkBoardStatus.blocked
    assert waiting_task.block_reason == "awaiting_approval"
    assert len(waiting_attempts) == 1
    assert waiting_attempts[0].attempt_id == attempt_id
    assert waiting_attempts[0].created_at == original_created_at
    assert waiting_attempts[0].ended_at is None
    assert waiting_attempts[0].lease_owner is None
    assert waiting_attempts[0].lease_expires_at is None

    # Resolve and resume the exact admitted parent. A second dispatcher pass
    # executes the real source-watch child and records its verified artifact.
    resolved = await approval_repository_module.approval_repository.resolve(
        approval_id,
        "approved",
    )
    assert resolved is not None and resolved.status == "approved"
    second_pass = await dispatcher.run_pass()
    assert second_pass["reconciled"] >= 1

    parent_job = await durable_job_repository.get_job(parent_job_id)
    assert parent_job is not None
    assert parent_job["status"] == "blocked"
    assert parent_job["failure_reason"] == "awaiting_publication_preview"
    checkpoints = parent_job.get("checkpoints") or []
    watch_checkpoint = next(
        item
        for item in checkpoints
        if item.get("checkpoint_id") == "routine:watch_readback_verified"
    )
    watch_child_id = str(watch_checkpoint["payload"]["child_job_id"])
    m1_job_id = str(watch_checkpoint["payload"]["m1_job_id"])
    assert m1_job_id.startswith(f"source-watch:{second_watch['id']}:")
    watch_child = await durable_job_repository.get_job(watch_child_id)
    m1_job = await durable_job_repository.get_job(m1_job_id)
    assert watch_child is not None and watch_child["status"] == "succeeded"
    assert m1_job is not None
    assert m1_job["status"] == "succeeded"
    assert m1_job["goal_id"] == second_goal.id
    assert m1_job["goal_revision"] == 1
    verified_readbacks = [
        effect
        for effect in m1_job.get("effects", [])
        if effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and isinstance(effect.get("details"), dict)
        and effect["details"].get("verified") is True
    ]
    assert verified_readbacks
    packet_id = str(watch_checkpoint["payload"]["packet_id"])
    async with get_session() as db:
        packet = (
            await db.execute(
                select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id)
            )
        ).scalars().one()
        final_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
    assert packet.goal_id == second_goal.id
    assert packet.status == "succeeded"
    assert packet.verification_status == "passed"
    assert packet.dossier_artifact_id and packet.task_artifact_id
    assert final_task.status is WorkBoardStatus.blocked
    assert final_task.block_reason == "awaiting_publication_preview"
    assert fetch_counts["second"] == 2

    # Exercise real credential unavailability under current finite consent.
    # Generic request grants no longer govern GitHub consent. Temporarily
    # loading the wrong local key makes canonical Vault decryption fail; no
    # ciphertext, consent, revision or admitted authority is changed. Restoring
    # that exact prerequisite permits recovery of the same uncontacted attempt.
    monkeypatch.setattr(work_board_api, "get_session", get_session)
    monkeypatch.setattr(work_board_api, "dispatcher", dispatcher)

    def publication_request(grants: tuple[str, ...]):
        now = datetime.now(timezone.utc)
        principal = TrustPrincipal(
            principal_id=OWNER,
            principal_type=PrincipalType.OPERATOR,
            grants=grants,
            session_id=SESSION,
            operator_session_id=SESSION,
        )
        return SimpleNamespace(
            state=SimpleNamespace(
                operator=AuthenticatedOperator(
                    session_id=SESSION,
                    principal=principal,
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(hours=2),
                )
            )
        )

    async with get_session() as db:
        recovery_detail = await board_repository.get_detail(db, board_owner, task_id)
    original_vault_key = settings.vault_encryption_key
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    missing_grant_request = publication_request(())
    with pytest.raises(HTTPException) as missing_grant:
        await work_board_api.prepare_work_board_routine_publication(
            request=missing_grant_request,
            task_id=task_id,
            body=WorkBoardRoutinePublicationPrepareRequest(
                expected_revision=recovery_detail["task"].task_revision,
                title="Second goal verified follow-through",
                body="This operator-approved procedure produced a verified second-goal result.",
            ),
        )
    assert missing_grant.value.status_code == 403
    assert missing_grant.value.detail["recovery_action"] == "restore_prerequisite"
    async with get_session() as db:
        blocked_detail = await board_repository.get_detail(db, board_owner, task_id)
    assert blocked_detail["task"].status is WorkBoardStatus.blocked
    assert blocked_detail["task"].block_reason == "external_mutation_grant_required"
    assert blocked_detail["attempts"][0].attempt_id == attempt_id
    assert blocked_detail["attempts"][0].ended_at is None
    assert blocked_detail["attempts"][0].lease_owner is None

    monkeypatch.setattr(settings, "vault_encryption_key", original_vault_key)
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    restored_snapshot = await vault_repository_module.vault_repository.snapshot(
        "m6/intercepted-github-token", owner_principal_id=OWNER,
    )
    assert restored_snapshot.binding_digest == snapshot.binding_digest
    async with get_session() as db:
        restored_connection = await db.get(GitHubFollowthroughConnection, "intercepted-github-connection")
        assert restored_connection.revision == 1
        assert restored_connection.consent_connection_revision == 1
        assert restored_connection.consent_revoked_at is None
    publication_response = await work_board_api.prepare_work_board_routine_publication(
        request=publication_request((AuthorityGrant.EXTERNAL_MUTATION.value,)),
        task_id=task_id,
        body=WorkBoardRoutinePublicationPrepareRequest(
            expected_revision=blocked_detail["task"].task_revision,
            title="Second goal verified follow-through",
            body="This operator-approved procedure produced a verified second-goal result.",
        ),
    )
    publication = publication_response["publication"]
    assert publication["status"] == "awaiting_approval"
    # Preparing the exact M3 approval must update the same task to a Blocked
    # publication wait and retain the same open attempt.
    await dispatcher.reconcile_linked_attempts()
    async with get_session() as db:
        publication_wait_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        publication_wait_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
            )
        ).scalars().one()
    assert publication_wait_task.status is WorkBoardStatus.blocked
    assert publication_wait_task.block_reason == "awaiting_publication_approval"
    assert publication_wait_attempt.attempt_id == attempt_id
    assert publication_wait_attempt.ended_at is None
    assert publication_wait_attempt.lease_owner is None
    m3_approval_id = str(publication.get("approval_id") or "")
    m3_job_id = str(publication.get("m3_job_id") or "")
    assert m3_approval_id and m3_job_id
    prepared_publication = await GitHubFollowthroughService()._read_prepared(
        await durable_job_repository.get_job(m3_job_id)
    )
    assert prepared_publication.job_id == m3_job_id
    assert (await approval_repository_module.approval_repository.get(m3_approval_id)).status == "pending"
    resolved_publication = await approval_repository_module.approval_repository.resolve(
        m3_approval_id,
        "approved",
    )
    assert resolved_publication is not None and resolved_publication.status == "approved"
    parent_job = await durable_job_repository.get_job(parent_job_id)
    async with get_session() as db:
        recover_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        recover_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                )
            )
        ).scalars().one()
    assert parent_job is not None
    await dispatcher.resume_routine_attempt_for_operator_recovery(
        WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        recover_task,
        recover_attempt,
        parent_job,
        expected_revision=recover_task.task_revision,
    )
    # Check the same canonical immutable consent binding used by recovery.
    from src.extensions.github_consent import require_followthrough_consent
    current_m3 = await durable_job_repository.get_job(m3_job_id)
    current_authority = current_m3["declared_authority"]
    assert current_authority["action"] == "create_issue"
    assert current_authority["repository"] == "example/repo"
    from src.extensions.github_followthrough import GitHubFollowthroughError
    contacts_before_recovery = len(github_requests)
    for action, repository in ((None, "example/repo"), ("create_comment", "example/repo"),
                               ("create_issue", "different/repository")):
        with pytest.raises(GitHubFollowthroughError):
            await require_followthrough_consent(principal=OWNER, root=SESSION,
                action=action, repository=repository,
                revision=current_authority["connection_revision"], binding=current_authority["github_consent"])
        assert len(github_requests) == contacts_before_recovery
    await require_followthrough_consent(principal=OWNER, root=SESSION,
        action=current_authority.get("action"), repository=current_authority.get("repository"),
        revision=current_authority.get("connection_revision"), binding=current_authority.get("github_consent"))
    try:
        recovered = await routines.recover(
            routine_id,
            parent_job_id,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            external_mutation_granted=True,
        )
    finally:
        await dispatcher.reconcile_linked_attempts()
    assert recovered["status"] == "succeeded", json.dumps(recovered, sort_keys=True, default=str)
    assert recovered["child"]["remote_id"] == 481
    assert github_requests[:initial_publication_requests] == ["POST", "GET"]
    assert github_requests[initial_publication_requests:] == ["POST", "GET"]
    completed_parent = await durable_job_repository.get_job(parent_job_id)
    completed_m3 = await durable_job_repository.get_job(m3_job_id)
    assert completed_parent is not None and completed_parent["status"] == "succeeded"
    assert completed_m3 is not None and completed_m3["status"] == "succeeded"
    m3_readback = next(
        effect
        for effect in completed_m3["effects"]
        if effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("verified") is True
    )
    assert m3_readback["readback_id"]
    assert m3_readback["verified_at"]
    assert m3_readback["details"]["readback_path"] == "/repos/example/repo/issues/481"
    parent_readback = next(
        effect
        for effect in completed_parent["effects"]
        if effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("verified") is True
    )
    assert parent_readback["readback_id"]
    assert parent_readback["verified_at"]
    assert parent_readback["details"]["source_readback"]["workflow_run_id"] == m3_job_id
    async with get_session() as db:
        final_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
    assert final_task.status is WorkBoardStatus.done

    # Revoke is a canonical lifecycle change. A later invocation is denied
    # after the completed result without reopening the prior task or job.
    revoked = await routines.pause_or_revoke(
        routine_id,
        state="revoked",
        expected_revision=active_revision,
        reason="m6-provider-free-receipt",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert revoked["status"] == "revoked"
    # Generic capability-pack execution is never a routine invocation path,
    # including after canonical routine revocation leaves the lifecycle
    # pointer as historical metadata.
    routine_pack_id = _routine_pack_id(routine_id, 1)
    with pytest.raises(
        CapabilityPackLifecycleError,
        match="guardian_routine_execution_requires_routine_service",
    ):
        CapabilityPackLifecycle().execute_local(
            routine_pack_id,
            goal_id=second_goal.id,
            job_id="m6-revoked-routine-generic-execution",
            domain="primary",
            source_payload={"source": "intercepted"},
            owner_principal_id=OWNER,
            session_id=SESSION,
        )
    with pytest.raises(RoutineError, match="routine_revoked_terminal"):
        await routines.invoke(
            routine_id,
            RoutineInvokeRequest(
                version=1,
                expected_routine_revision=active_revision + 1,
                goal_id=second_goal.id,
                expected_goal_revision=1,
                source_watch_id=second_watch["id"],
                expected_watch_revision=int(second_watch["plan_revision"]),
                invocation_uuid=str(uuid.uuid4()),
            ),
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
