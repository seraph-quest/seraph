"""Real singleton borrowing and cleanup against original disposable Root issuers."""
import pytest

from config.settings import settings
from src.work_board.historical_method import historical_method_service
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_document_build_native_capacity import build_admission_lifecycle
from tests.test_document_build_storage import setup
from tests.test_general_task_persistence import task_runtime
from tests.test_inference_accounting import accounting_db
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


async def finish_fixture(generator, outcome):
    if outcome == "normal":
        with pytest.raises(StopAsyncIteration):
            await generator.__anext__()
    else:
        with pytest.raises(RuntimeError, match=outcome):
            await generator.athrow(RuntimeError(outcome))


async def check_borrowed(generator_factory, monkeypatch, unavailable, outcome):
    from src.extensions import capability_execution
    from src.extensions.capability_execution import CapabilityJournalError

    service = historical_method_service
    assert not service.started
    if unavailable:
        def unavailable_key():
            raise CapabilityJournalError("fixture unavailable key source")
        monkeypatch.setattr(capability_execution, "_effect_mac_key", unavailable_key)
    await service.start()
    original_key = service.signing_key
    original_setting = settings.capability_journal_secret
    assert (original_key is None) is unavailable
    actual_stop = service.stop
    calls = []

    async def forbidden_start():
        calls.append("start")
        raise AssertionError("borrower restarted the real owner")

    async def forbidden_stop():
        calls.append("stop")
        raise AssertionError("borrower stopped the real owner")

    monkeypatch.setattr(service, "start", forbidden_start)
    monkeypatch.setattr(service, "stop", forbidden_stop)
    try:
        generator = generator_factory()
        handle = await generator.__anext__()
        # Setup failure occurs before the lazy build caller asks to start.
        if outcome != "setup_failure" and handle is not service:
            await handle.start()
        await finish_fixture(generator, outcome)
        assert service.started
        assert service.signing_key is original_key
        assert settings.capability_journal_secret == original_setting
        assert calls == []
    finally:
        await actual_stop()


async def check_owned(generator_factory, monkeypatch, outcome):
    service = historical_method_service
    assert not service.started
    actual_start, actual_stop = service.start, service.stop
    calls = []

    async def start():
        calls.append("start")
        await actual_start()

    async def stop():
        calls.append("stop")
        await actual_stop()

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "stop", stop)
    generator = generator_factory()
    handle = await generator.__anext__()
    if handle is not service:
        assert not service.started
        await handle.start()
        await handle.start()  # Repeated caller start does not re-key its owner.
    assert service.started and service.signing_key is not None
    await finish_fixture(generator, outcome)
    assert not service.started and service.signing_key is None
    assert calls == ["start", "stop"]


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("outcome", ["normal", "setup_failure", "exception_exit"])
async def test_native_fixture_borrows_original_owner(task_runtime, monkeypatch, unavailable, outcome):
    await check_borrowed(lambda: native_admission_lifecycle.__wrapped__(task_runtime, monkeypatch),
        monkeypatch, unavailable, outcome)


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("outcome", ["normal", "setup_failure", "exception_exit"])
async def test_build_fixture_borrows_original_owner(accounting_db, monkeypatch, unavailable, outcome):
    await setup(accounting_db, monkeypatch)
    await check_borrowed(lambda: build_admission_lifecycle.__wrapped__(accounting_db),
        monkeypatch, unavailable, outcome)


@pytest.mark.parametrize("outcome", ["normal", "exception_exit"])
async def test_native_fixture_closes_only_owned_start(task_runtime, monkeypatch, outcome):
    await check_owned(lambda: native_admission_lifecycle.__wrapped__(task_runtime, monkeypatch),
        monkeypatch, outcome)


@pytest.mark.parametrize("outcome", ["normal", "exception_exit"])
async def test_build_fixture_closes_only_owned_start(accounting_db, monkeypatch, outcome):
    await setup(accounting_db, monkeypatch)
    await check_owned(lambda: build_admission_lifecycle.__wrapped__(accounting_db), monkeypatch, outcome)


async def test_lazy_build_setup_failure_never_starts_signer(accounting_db):
    service = historical_method_service
    assert not service.started
    generator = build_admission_lifecycle.__wrapped__(accounting_db)
    await generator.__anext__()
    await finish_fixture(generator, "setup_failure")
    assert not service.started and service.signing_key is None
