import logging

from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


async def run_goal_check() -> None:
    """Check goal progress and broadcast ambient state."""
    logger.info("goal_check started")

    try:
        from src.observer.manager import context_manager
        await context_manager.refresh()
    except Exception:
        logger.exception("goal_check: context refresh failed")
        return

    try:
        from src.goals.repository import goal_repository
        dashboard = await goal_repository.get_dashboard()
    except Exception:
        logger.exception("goal_check: dashboard query failed")
        return

    total = dashboard.get("total_count", 0)
    if total == 0:
        logger.debug("goal_check: no goals to check")
        return

    completed = dashboard.get("completed_count", 0)
    completion_rate = completed / total if total else 0

    from src.observer.delivery import deliver_or_queue

    if completion_rate < 0.3:
        state = "goal_behind"
        tooltip = "Some goals need attention — you're behind on progress."
    else:
        state = "on_track"
        tooltip = f"Goals are {int(completion_rate * 100)}% complete. Keep it up!"

    await deliver_or_queue(
        WSResponse(
            type="ambient",
            content="",
            intervention_type="ambient",
            state=state,
            tooltip=tooltip,
        )
    )
    logger.info("goal_check: broadcast state=%s (%.0f%% complete)", state, completion_rate * 100)
