"""Screen observation cleanup — deletes observations older than retention period."""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)


async def run_screen_cleanup() -> None:
    """Delete screen observations older than the configured retention period."""
    try:
        from src.observer.screen_repository import screen_observation_repo

        deleted = await screen_observation_repo.cleanup_old(
            settings.screen_observation_retention_days
        )
        if deleted > 0:
            logger.info(
                "screen_cleanup: removed %d observations older than %d days",
                deleted,
                settings.screen_observation_retention_days,
            )
    except Exception:
        logger.exception("screen_cleanup failed")
