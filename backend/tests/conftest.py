import copy
import importlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import time

import pytest
import pytest_asyncio
from types import SimpleNamespace
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("CAPABILITY_JOURNAL_SECRET", "test-capability-journal-secret")


def pytest_addoption(parser):
    parser.addoption(
        "--run-real-repo-sandbox",
        action="store_true",
        default=False,
        help="run the explicit rootless Docker repository-repair acceptance journey",
    )


from config.settings import settings
# Register every SQLModel table before any fixture runs ``create_all``.
# ``create_all`` creates only the tables whose models are already imported, so
# without this a table's presence depended on which test imported it first.
# ``test_hermetic_test_inputs`` keeps this list equal to the table owners in src.
TABLE_MODEL_MODULES = ("src.db.models", "src.db.task_method_models")
for _module in TABLE_MODEL_MODULES:
    importlib.import_module(_module)
from src.app import create_app
from src.audit.repository import AuditRepository, audit_repository
from src.llm_runtime import _reset_target_health
from src.memory.flush import _reset_memory_flush_state
from src.memory.snapshots import _reset_bounded_guardian_snapshot_cache
from src.utils.background import drain_tracked_tasks
from tests.support.db import (arm_canonical_engine_sentinel, create_test_database, install_database,
                              remove_test_database, take_canonical_engine_violations)

# ── Settings isolation ──────────────────────────────────

_SETTINGS_SNAPSHOT_KEY = pytest.StashKey[dict]()


def snapshot_settings() -> dict:
    """Return a deep copy of the shared settings object's field values."""
    return copy.deepcopy(settings.__dict__)


def restore_settings(snapshot: dict) -> None:
    """Return the shared settings object to ``snapshot``, in place."""
    # The top-level ``__pydantic_fields_set__`` is not restored: nothing reads
    # ``settings.model_fields_set``. Nested models such as ``repo_sandbox`` are
    # restored whole, so their own fields-set (which src does read) comes back.
    current = settings.__dict__
    for name in [name for name in current if name not in snapshot]:
        del current[name]
    for name, value in snapshot.items():
        if name not in current or current[name] != value:
            current[name] = value


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    # Taken before any fixture runs, so the restore below returns the shared
    # settings object to exactly its pre-test state.
    item.stash[_SETTINGS_SNAPSHOT_KEY] = snapshot_settings()
    take_canonical_engine_violations()
    try:
        return (yield)
    finally:
        _fail_on_canonical_engine_violations("setup")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        return (yield)
    finally:
        # Runs after every fixture finalizer, including ``monkeypatch`` undo, so a
        # direct ``settings.x = ...`` assignment (or an in-place change to a nested
        # settings model) in a test or fixture cannot leak into the next test.
        # This also undoes settings changes made by a module-, class- or
        # session-scoped fixture: such a fixture must set them per test instead.
        snapshot = item.stash.get(_SETTINGS_SNAPSHOT_KEY, None)
        if snapshot is not None:
            # Items live for the whole session; do not keep every test's copy.
            del item.stash[_SETTINGS_SNAPSHOT_KEY]
            restore_settings(snapshot)
        _fail_on_canonical_engine_violations("teardown")


# ── Database seam (#1222) ───────────────────────────────

# Any connection to the canonical engine fails the test that made it.
arm_canonical_engine_sentinel()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    try:
        return (yield)
    finally:
        _fail_on_canonical_engine_violations("the test body")


def _fail_on_canonical_engine_violations(phase: str) -> None:
    # The refusal may be swallowed by the code under test; the record is not.
    violations = take_canonical_engine_violations()
    if violations:
        pytest.fail(
            f"{len(violations)} connection(s) to the canonical database engine during {phase}; "
            "request the `db` fixture (tests/support/db.py). First attempt:\n" + violations[0],
            pytrace=False,
        )


@pytest.fixture(scope="session")
def _db_template_directory(tmp_path_factory):
    return tmp_path_factory.mktemp("db-template")


@pytest_asyncio.fixture
async def db(tmp_path_factory, _db_template_directory, monkeypatch):
    """The test's own production-configured SQLite file database.

    Installed as the default database, so every ``get_session`` caller uses
    it through the production path. Calling the handle opens a session.
    """
    database = await create_test_database(
        tmp_path_factory.mktemp("db") / "seraph-test.sqlite3",
        template_directory=_db_template_directory,
    )
    install_database(monkeypatch, database.engine)
    try:
        yield database
    finally:
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
        finally:
            await database.engine.dispose()
            # About 6 MB per test: a full local run would otherwise keep GBs.
            remove_test_database(database.path)


@pytest.fixture
def async_db(db):
    """Older name for ``db``. Accepts the retired ``indirect=["file"]`` param:
    every test database is now a file database."""
    return db


# ── App / HTTP client fixtures ──────────────────────────

@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def isolate_operator_auth_defaults(monkeypatch):
    """Reset per-process auth state and keep catalog refresh off in tests.

    Operator auth settings need no reset here: the root conftest loads no env
    file and pins the auth environment, and the settings restore above undoes
    any per-test change.
    """

    from src.api.auth import _reset_login_throttle_for_tests

    # Each test has its own database and client. Keep the process-local login
    # throttle in the same scope, while preserving throttling within a test.
    _reset_login_throttle_for_tests()
    # No background NEAR catalog reads in tests (#1178); tests drive
    # refresh_catalog_observations directly over intercepted transport.
    monkeypatch.setattr(settings, "near_tee_catalog_refresh_enabled", False)
    yield
    _reset_login_throttle_for_tests()


@pytest_asyncio.fixture
async def client(app, async_db, monkeypatch):
    """Async HTTP test client backed by the test's own database (``db``).

    Requested through ``async_db`` so tests that parametrize that name
    indirectly keep resolving it."""
    # The synthetic ASGI host ``test`` is in the default allowed hosts. Pin the
    # allowed origins to the single cockpit origin this fixture's tests use.
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
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


@pytest.fixture
def real_repo_isolation():
    """Opt a test into the host's real repository OS sandbox (#1065)."""
    from src.execution.repo_isolation import clear_probe_cache
    clear_probe_cache()
    yield
    clear_probe_cache()


@pytest.fixture(autouse=True)
def default_repo_isolation_not_isolated(request, monkeypatch):
    """Repository tests exercise the declared not-isolated path by default.

    Many of them observe host side effects of the repository command (marker
    files, detached processes), which the real sandbox correctly prevents.
    Tests that request ``real_repo_isolation`` use the actual host probe.
    """
    callspec = getattr(request.node, "callspec", None)
    if "real_repo_isolation" in request.fixturenames or (
            callspec is not None and callspec.params.get("repo_isolation") == "real"):
        from src.execution.repo_isolation import clear_probe_cache
        clear_probe_cache()
        return
    from src.execution import repo_isolation

    def not_isolated(runtime, limits, **_kwargs):
        return repo_isolation._not_isolated("bwrap_missing", limits)
    monkeypatch.setattr(repo_isolation, "probe", not_isolated)


@pytest.fixture(autouse=True)
def inference_ledger_shadow_oracle(monkeypatch):
    """#1086: every accounting test also checks bounded verification against
    the pre-#1086 full-ledger decisions, and starts from a fresh process view."""
    from src.workflows import inference_ledger
    inference_ledger.reset_process_state()
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", True)
    yield
    inference_ledger.reset_process_state()


@pytest.fixture(autouse=True)
def reset_process_singletons(monkeypatch):
    """Give each test the fresh per-process state a real app boot has.

    The observer ``context_manager`` singleton carries operator modes (tool,
    MCP, approval, interruption) and a wall-clock-derived user state; a prior
    test's refresh or mode change otherwise leaks into later tests (e.g. an
    evening refresh makes delivery "quiet", a safe tool mode hides workflow
    tools). The configuration mutation lock is loop-agnostic, but a fresh
    instance keeps a test that leaks it held from blocking later tests.
    """
    from src.model_fabric import effective_policy
    from src.observer.manager import ContextManager, context_manager
    fresh = ContextManager()
    # Reset in place: many modules hold a reference to this exact object.
    context_manager.__dict__.update(fresh.__dict__)
    monkeypatch.setattr(effective_policy, "configuration_mutation_lock", effective_policy.LoopAgnosticLock())
    # Contention backoffs from #702/#1074: a prior test's deferred touch or
    # audited empty poll must not suppress the next test's.
    from src.api import observer as observer_api
    from src.auth import service as auth_service
    monkeypatch.setattr(auth_service, "_auth_touch_retry_after", 0.0)
    monkeypatch.setattr(observer_api, "_empty_poll_audited_at", {})
    # The Vault cipher is cached from the first key it loads; each test has its
    # own workspace and key, so a key cached by a prior test must not decrypt
    # (and fail closed on) this test's Vault.
    from src.vault import crypto as vault_crypto
    monkeypatch.setattr(vault_crypto, "_fernet", None)
    yield


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


@pytest.fixture
def openrouter_legacy_reenabled(monkeypatch):
    """Explicit opt-in to the retired OpenRouter route (ADR-035).

    OpenRouter is retired by policy and no production setting can re-enable
    it. Regression tests that pin its historical mechanics (adapters, proofs,
    accounting, reconciliation shapes) request this fixture, or carry the
    ``openrouter_legacy`` marker, with transport still intercepted.
    """
    from src.model_fabric import retirement

    monkeypatch.setattr(retirement, "_OPENROUTER_RETIRED", False)


@pytest.fixture(autouse=True)
def apply_openrouter_legacy_marker(request):
    if request.node.get_closest_marker("openrouter_legacy") is not None:
        request.getfixturevalue("openrouter_legacy_reenabled")


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
        from src.llm_runtime import _log_llm_runtime_event

        runtime_path = str(kwargs.get("runtime_path") or "test_inference")
        await _log_llm_runtime_event(
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
