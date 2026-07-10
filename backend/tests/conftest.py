import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch
import time

import pytest
import pytest_asyncio
from types import SimpleNamespace
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/seraph-test")
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
os.environ.setdefault("OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS", "true")

from config.settings import settings
from src.app import create_app
from src.audit.repository import AuditRepository, audit_repository
from src.llm_runtime import _reset_target_health
from src.db.engine import _ensure_search_indexes
from src.memory.flush import _reset_memory_flush_state
from src.memory.snapshots import _reset_bounded_guardian_snapshot_cache
from src.utils.background import drain_tracked_tasks

# Every place get_session is imported — use the local attribute name.
_PATCH_TARGETS = [
    "src.db.engine.get_session",
    "src.auth.service.get_session",
    "src.agent.session.get_session",
    "src.approval.repository.get_session",
    "src.goals.repository.get_session",
    "src.audit.repository.get_session",
    "src.model_fabric.repository.get_session",
    "src.profile.service.get_db",
    "src.api.settings.get_db",  # aliased: `import get_session as get_db`
    "src.api.observer.get_session",
    "src.scheduler.jobs.memory_consolidation.get_session",
    "src.scheduler.jobs.screenshot_observation_digest.get_session",
    "src.scheduler.scheduled_jobs.get_session",
    "src.observer.insight_queue.get_session",
    "src.observer.screenshot_folder_source.get_session",
    "src.guardian.feedback.get_session",
    "src.vault.repository.get_session",
    "src.observer.screen_repository.get_session",
    "src.memory.repository.get_session",
    "src.memory.decay.get_session",
    "src.memory.flush.get_session",
    "src.memory.hybrid_retrieval.get_session",
    "src.workflows.durable_state.get_session",
    "src.workflows.production_workflow_guarantees.get_session",
]


# ── In-memory async DB fixture ──────────────────────────

@pytest_asyncio.fixture
async def async_db():
    """Provide an in-memory SQLite engine with all tables created.

    Patches ``get_session`` in every module that imports it so the test
    database is used transparently.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_search_indexes(conn)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    patches = []
    for target in _PATCH_TARGETS:
        p = patch(target, _get_session)
        p.start()
        patches.append(p)

    yield _get_session

    teardown_error: Exception | None = None
    try:
        await drain_tracked_tasks(timeout_seconds=5.0)
    except Exception as exc:
        teardown_error = exc
    finally:
        for p in patches:
            p.stop()
        await engine.dispose()
    if teardown_error is not None:
        raise teardown_error


# ── App / HTTP client fixtures ──────────────────────────

@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client(app, async_db):
    """Async HTTP test client backed by the in-memory DB."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.run.return_value = "Mocked agent response"
    return agent


@pytest.fixture(autouse=True)
def reset_llm_target_health():
    _reset_target_health()
    yield
    _reset_target_health()


@pytest.fixture(autouse=True)
def stub_vlm_runtime_probe():
    async def _probe(*, timeout_seconds: float = 0.75):
        return {
            "checked": False,
            "reachable": False,
            "reason": "test_stub",
            "health": {"checked": False, "ok": False, "status_code": None, "error": ""},
            "backend_health": {"checked": False, "ok": False, "status_code": None, "error": ""},
            "queue_status": {"checked": False, "ok": False, "status_code": None, "error": ""},
            "chat_proxy": {"checked": False, "ok": False, "status_code": None, "error": ""},
        }

    with patch("src.vlm_runtime.probe_effective_vlm_runtime", _probe):
        yield


@pytest.fixture(autouse=True)
def reset_bounded_snapshot_cache():
    _reset_bounded_guardian_snapshot_cache()
    yield
    _reset_bounded_guardian_snapshot_cache()


@pytest.fixture(autouse=True)
def reset_memory_flush_cache():
    _reset_memory_flush_state()
    yield
    _reset_memory_flush_state()


def _restore_audit_repository_methods() -> None:
    audit_repository.log_event = AuditRepository.log_event.__get__(
        audit_repository,
        AuditRepository,
    )
    audit_repository.list_events = AuditRepository.list_events.__get__(
        audit_repository,
        AuditRepository,
    )


@pytest.fixture(autouse=True)
def reset_audit_repository_method_patches():
    _restore_audit_repository_methods()
    yield
    _restore_audit_repository_methods()


@pytest.fixture(autouse=True)
def clear_ambient_screenshot_analysis_provider():
    with (
        patch.object(settings, "screen_analysis_provider", ""),
        patch.object(settings, "local_vlm_base_url", ""),
    ):
        yield


@pytest.fixture(autouse=True)
def ensure_test_workspace_dir():
    Path(os.environ["WORKSPACE_DIR"]).mkdir(parents=True, exist_ok=True)
@pytest.fixture
def mocked_canonical_inference_context(monkeypatch):
    """Keep legacy caller tests focused while adoption tests exercise real identity binding."""
    from src.security.trust_contract import canonical_digest

    def context_for_payload(*_args, **kwargs):
        runtime_path = str(_args[0]) if _args else "test_inference"
        return SimpleNamespace(
            request_id="test-inference-request",
            data_digest=canonical_digest(kwargs.get("payload")),
            runtime_path=runtime_path,
            deadline_at=time.time() + float(kwargs.get("timeout_seconds", 300)),
            workload=SimpleNamespace(value="test"),
            egress_class=SimpleNamespace(value="local_only"),
        )
    targets = (
        "src.agent.context_window.build_canonical_inference_context",
        "src.agent.direct_chat.build_canonical_inference_context",
        "src.agent.session.build_canonical_inference_context",
        "src.agent.strategist.build_canonical_inference_context",
        "src.memory.pipeline.extract.build_canonical_inference_context",
        "src.observer.screenshot_semantic_analysis.build_canonical_inference_context",
        "src.scheduler.jobs.activity_digest.build_canonical_inference_context",
        "src.scheduler.jobs.daily_briefing.build_canonical_inference_context",
        "src.scheduler.jobs.end_of_day_goal_report.build_canonical_inference_context",
        "src.scheduler.jobs.evening_review.build_canonical_inference_context",
        "src.scheduler.jobs.screenshot_observation_digest.build_canonical_inference_context",
        "src.scheduler.jobs.weekly_activity_review.build_canonical_inference_context",
    )
    for target in targets:
        monkeypatch.setattr(target, context_for_payload)

    async def completion_transport_stub(**kwargs):
        import asyncio
        import inspect
        import litellm

        result = litellm.completion(
            messages=kwargs["messages"],
            temperature=kwargs["temperature"],
            max_tokens=kwargs["max_tokens"],
        )
        if inspect.isawaitable(result):
            timeout = kwargs.get("timeout")
            result = await asyncio.wait_for(result, timeout=timeout) if timeout is not None else await result
        from src.llm_runtime import _log_llm_runtime_event_sync

        runtime_path = str(kwargs.get("runtime_path") or "test_inference")
        _log_llm_runtime_event_sync(
            event_type="llm_primary_success",
            summary="Legacy caller fixture completed through the governed boundary",
            details={
                "runtime_path": runtime_path,
                "request_id": "test-inference-request",
                "used_fallback": False,
            },
            request_id="test-inference-request",
        )
        return result

    completion_targets = (
        "src.llm_runtime.completion_with_fallback",
        "src.memory.consolidator.completion_with_fallback",
        "src.agent.strategist.completion_with_fallback",
        "src.scheduler.jobs.activity_digest.completion_with_fallback",
        "src.scheduler.jobs.daily_briefing.completion_with_fallback",
        "src.scheduler.jobs.end_of_day_goal_report.completion_with_fallback",
        "src.scheduler.jobs.evening_review.completion_with_fallback",
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        "src.scheduler.jobs.weekly_activity_review.completion_with_fallback",
    )
    for target in completion_targets:
        monkeypatch.setattr(target, completion_transport_stub)
    return context_for_payload


@pytest.fixture
def mocked_vlm_model_fabric_adoption(monkeypatch, mocked_canonical_inference_context):
    """Let legacy HTTP adapter tests stay transport-focused."""
    async def passthrough(*, context, profile, transport):
        from src.model_fabric.contracts import transport_endpoint

        candidate = SimpleNamespace(endpoint=transport_endpoint(profile))
        return await transport(candidate, False)

    monkeypatch.setattr(
        "src.observer.screenshot_semantic_analysis._run_governed_vlm_adapter",
        passthrough,
    )
    return mocked_canonical_inference_context
