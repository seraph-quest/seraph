"""Real admission signer lifetime for explicit native SQLite test callers."""
import pytest_asyncio


class AdmissionSignerLifetime:
    """Track only this fixture's real singleton start, never a borrowed owner."""

    def __init__(self, service):
        self.service = service
        self._owns_start = False

    async def start(self):
        if self.service.started:
            return
        try:
            await self.service.start()
        finally:
            # Also clean up a real start that fails after marking itself live.
            self._owns_start = self.service.started

    async def close(self):
        if self._owns_start:
            self._owns_start = False
            await self.service.stop()


@pytest_asyncio.fixture
async def native_admission_lifecycle(task_runtime, monkeypatch):
    from config.settings import settings
    from src.work_board.historical_method import historical_method_service

    lifetime = AdmissionSignerLifetime(historical_method_service)
    try:
        # task_runtime has already seeded the actual disposable Root and SQLite.
        # A live owner, including one with an unavailable key, is borrowed intact.
        if not historical_method_service.started:
            monkeypatch.setattr(settings, "capability_journal_secret", "native-admission-disposable-key")
        await lifetime.start()
        yield historical_method_service
    finally:
        await lifetime.close()
