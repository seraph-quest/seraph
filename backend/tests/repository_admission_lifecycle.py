"""Explicit lazy signer ownership for genuine repository test admission."""
import asyncio

import pytest
import pytest_asyncio

from tests.general_task_method_lifecycle import AdmissionSignerLifetime
from tests.test_general_task_planner import accounting_db


@pytest_asyncio.fixture
async def repository_admission_signer(accounting_db, monkeypatch):
    from src.model_fabric import effective_policy
    from src.work_board.historical_method import historical_method_service
    from tests import test_repo_work_original_limits as original_limits
    from tests import test_repo_work_publication_terminal_lineage as terminal_lineage

    original_lock = effective_policy.configuration_mutation_lock
    if original_lock.locked():
        raise RuntimeError("repository fixture cannot replace a held configuration lock")
    canonical_lock = asyncio.Lock()
    # The caller's same pytest stack also owns legitimate body-level changes.
    # Its finalizer restores those changes after this signer fixture closes.
    monkeypatch.setattr(effective_policy, "configuration_mutation_lock", canonical_lock)
    monkeypatch.setattr(original_limits, "configuration_mutation_lock", canonical_lock)
    monkeypatch.setattr(terminal_lineage, "configuration_mutation_lock", canonical_lock)

    factory = accounting_db[2]
    missing = object()
    previous = getattr(factory, "_repository_admission_signer", missing)
    lifetime = AdmissionSignerLifetime(historical_method_service)
    factory._repository_admission_signer = lifetime
    try:
        yield lifetime
    finally:
        held_before_cleanup = (
            original_lock.locked() or effective_policy.configuration_mutation_lock.locked()
        )
        try:
            await lifetime.close()
        finally:
            if getattr(factory, "_repository_admission_signer", missing) is lifetime:
                if previous is missing:
                    del factory._repository_admission_signer
                else:
                    factory._repository_admission_signer = previous
            if (held_before_cleanup or original_lock.locked()
                    or effective_policy.configuration_mutation_lock.locked()):
                raise RuntimeError("repository fixture ended with a held configuration lock")


@pytest.fixture(scope="module")
def repository_lock_teardown_probe():
    from src.model_fabric import effective_policy
    from tests import test_repo_work_original_limits as original_limits
    from tests import test_repo_work_publication_terminal_lineage as terminal_lineage

    original = effective_policy.configuration_mutation_lock
    limits_alias = original_limits.configuration_mutation_lock
    terminal_alias = terminal_lineage.configuration_mutation_lock
    yield
    # Module teardown follows every function-scoped pytest monkeypatch undo.
    assert effective_policy.configuration_mutation_lock is original
    assert original_limits.configuration_mutation_lock is limits_alias
    assert terminal_lineage.configuration_mutation_lock is terminal_alias
    assert not original.locked()
    assert not effective_policy.configuration_mutation_lock.locked()
    assert not limits_alias.locked()
    assert not terminal_alias.locked()


async def test_preissuer_fixture_is_lazy_and_restores_handle_on_close_error(accounting_db, monkeypatch, repository_lock_teardown_probe):
    from src.work_board.historical_method import historical_method_service

    factory = accounting_db[2]
    previous = AdmissionSignerLifetime(historical_method_service)
    factory._repository_admission_signer = previous
    generator = repository_admission_signer.__wrapped__(accounting_db, monkeypatch)
    lifetime = await generator.__anext__()
    assert not historical_method_service.started
    assert historical_method_service.signing_key is None
    actual_close = lifetime.close

    async def failed_close():
        await actual_close()
        raise RuntimeError("fixture close error")

    monkeypatch.setattr(lifetime, "close", failed_close)
    with pytest.raises(RuntimeError, match="fixture close error"):
        await generator.aclose()
    assert factory._repository_admission_signer is previous
    assert not historical_method_service.started
    del factory._repository_admission_signer


async def test_real_admission_publication_exception_closes_owned_signer(accounting_db, monkeypatch, repository_lock_teardown_probe):
    from sqlalchemy import select
    from src.db.models import WorkBoardTask
    from src.work_board.historical_method import historical_method_service
    from tests.test_repo_work_task_publication import actual_publication

    factory = accounting_db[2]
    generator = repository_admission_signer.__wrapped__(accounting_db, monkeypatch)
    await generator.__anext__()
    service = None
    try:
        factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch)
        assert historical_method_service.started
        assert historical_method_service.signing_key is not None

        async def publication_failure(*args, **kwargs):
            raise RuntimeError("before actual Task publication")

        monkeypatch.setattr(service.repository, "create_task", publication_failure)
        async with factory.accounting_sessions() as db:
            with pytest.raises(RuntimeError, match="before actual Task publication"):
                await service.create(db, owner, request.model_copy(update={"accept": True}))
            assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
    finally:
        if service is not None:
            service.stop()
        await generator.aclose()
    assert not historical_method_service.started
    assert historical_method_service.signing_key is None
    assert not hasattr(factory, "_repository_admission_signer")


async def test_real_unavailable_owner_is_borrowed_without_rekey_or_stop(accounting_db, monkeypatch, repository_lock_teardown_probe):
    from config.settings import settings
    from src.work_board.historical_method import historical_method_service
    from src.work_board.repository import BoardError
    from tests.test_repo_work_task_publication import actual_publication

    for name in ("capability_journal_secret", "capability_journal_secret_hash",
                 "operator_auth_secret", "operator_auth_secret_hash"):
        monkeypatch.setattr(settings, name, "")
    await historical_method_service.start()
    assert historical_method_service.started
    assert historical_method_service.signing_key is None
    factory = accounting_db[2]
    generator = repository_admission_signer.__wrapped__(accounting_db, monkeypatch)
    await generator.__anext__()
    service = None
    try:
        factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch)
        assert historical_method_service.signing_key is None
        async with factory.accounting_sessions() as db:
            with pytest.raises(BoardError) as denied:
                await service.create(db, owner, request.model_copy(update={"accept": True}))
        assert denied.value.code == "general_task_method_signer_unavailable"
    finally:
        if service is not None:
            service.stop()
        await generator.aclose()
        assert historical_method_service.started
        assert historical_method_service.signing_key is None
        assert not hasattr(factory, "_repository_admission_signer")
        await historical_method_service.stop()


@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_same_process_actual_supervisor_stop(accounting_db, monkeypatch,
        repository_admission_signer, repository_lock_teardown_probe, language):
    from tests.test_repo_work_task_publication import (
        test_actual_source_stop_active_supervisor_keeps_original_until_reaped,
    )

    await test_actual_source_stop_active_supervisor_keeps_original_until_reaped(
        accounting_db=accounting_db, monkeypatch=monkeypatch, language=language,
        repository_admission_signer=repository_admission_signer)


@pytest.mark.parametrize("rollback", [False, True])
async def test_same_process_actual_policy_stop_fence(accounting_db, monkeypatch,
        repository_admission_signer, repository_lock_teardown_probe, rollback):
    from tests.test_repo_work_original_limits import (
        test_actual_automatic_stop_fence_blocks_policy_mutation_through_writer,
    )

    await test_actual_automatic_stop_fence_blocks_policy_mutation_through_writer(
        accounting_db=accounting_db, monkeypatch=monkeypatch, rollback=rollback,
        repository_admission_signer=repository_admission_signer)


async def test_same_process_actual_limits_alias(accounting_db, monkeypatch,
        repository_admission_signer, repository_lock_teardown_probe):
    from tests.test_repo_work_original_limits import (
        test_actual_original_limits_drift_blocks_before_private_read,
    )

    await test_actual_original_limits_drift_blocks_before_private_read(
        accounting_db=accounting_db, monkeypatch=monkeypatch, drift="goal_revision",
        repository_admission_signer=repository_admission_signer)


async def test_same_process_actual_terminal_alias(accounting_db, monkeypatch,
        repository_admission_signer, repository_lock_teardown_probe):
    from tests.test_repo_work_publication_terminal_lineage import (
        test_actual_source_publication_requires_complete_terminal_c1_evidence,
    )

    await test_actual_source_publication_requires_complete_terminal_c1_evidence(
        accounting_db=accounting_db, monkeypatch=monkeypatch, drift="missing_board_readback",
        repository_admission_signer=repository_admission_signer)


async def test_same_process_actual_settings_fence(accounting_db, monkeypatch,
        repository_admission_signer, repository_lock_teardown_probe):
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkBoardAttempt
    from src.work_board.historical_method import historical_method_service
    from tests.test_repo_work_task_publication import (
        test_settings_put_waits_for_actual_source_publication_commit,
    )

    factory = accounting_db[2]
    missing = object()
    assert getattr(factory, "_repository_admission_signer", missing) is repository_admission_signer
    assert not historical_method_service.started
    del factory._repository_admission_signer
    try:
        await test_settings_put_waits_for_actual_source_publication_commit(
            accounting_db=accounting_db, monkeypatch=monkeypatch)
        async with factory() as db:
            tasks = list((await db.execute(select(WorkBoardTask))).scalars())
            assert len(tasks) == 1 and tasks[0].status == "triage"
            assert list((await db.execute(select(WorkBoardAttempt))).scalars()) == []
    finally:
        replacement = getattr(factory, "_repository_admission_signer", missing)
        if replacement is missing:
            factory._repository_admission_signer = repository_admission_signer
        elif replacement is not repository_admission_signer:
            raise RuntimeError("Settings proof found a foreign admission handle")
        assert not historical_method_service.started
