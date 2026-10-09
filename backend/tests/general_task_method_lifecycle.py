"""Real admission signer lifetime for explicit native SQLite test callers."""
import pytest_asyncio


@pytest_asyncio.fixture
async def native_admission_lifecycle(task_runtime, monkeypatch):
    from config.settings import settings
    from src.work_board.historical_method import historical_method_service

    # task_runtime has already seeded the actual disposable Root and SQLite.
    # Configure the existing server-key source only in this explicit fixture.
    monkeypatch.setattr(settings, "capability_journal_secret", "native-admission-disposable-key")
    try:
        await historical_method_service.start()
        yield historical_method_service
    finally:
        await historical_method_service.stop()
