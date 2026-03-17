import logging
from time import perf_counter

from src.audit.runtime import log_scheduler_job_event
from src.models.schemas import WSResponse

logger = logging.getLogger(__name__)


def _delivery_value(result) -> str | None:
    delivery_decision = getattr(result, "delivery_decision", None)
    if delivery_decision is not None:
        return delivery_decision.value
    return getattr(result, "value", None)


def _policy_action_value(result) -> str | None:
    action = getattr(result, "action", None)
    if action is not None:
        return action.value
    return None


async def run_goal_check() -> None:
    """Check goal progress and broadcast ambient state."""
    logger.info("goal_check started")
    started_at = perf_counter()

    try:
        from src.observer.manager import context_manager
        await context_manager.refresh()
        from src.goals.repository import goal_repository
        dashboard = await goal_repository.get_dashboard()

        total = dashboard.get("total_count", 0)
        if total == 0:
            logger.debug("goal_check: no goals to check")
            await log_scheduler_job_event(
                job_name="goal_check",
                outcome="skipped",
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "no_goals",
                },
            )
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

        result = await deliver_or_queue(
            WSResponse(
                type="ambient",
                content="",
                intervention_type="ambient",
                state=state,
                tooltip=tooltip,
            )
        )
        await log_scheduler_job_event(
            job_name="goal_check",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "state": state,
                "completion_rate": completion_rate,
                "delivery": _delivery_value(result),
                "policy_action": _policy_action_value(result),
            },
        )
        logger.info("goal_check: broadcast state=%s (%.0f%% complete)", state, completion_rate * 100)
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="goal_check",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("goal_check failed")
