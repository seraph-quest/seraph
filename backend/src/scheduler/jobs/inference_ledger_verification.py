"""Periodic full verification of the inference cost ledger (#1086)."""

from __future__ import annotations


async def run_inference_ledger_verification() -> None:
    """Re-verify every ledger row off the writer lock; latch accounting on failure."""
    from src.workflows.job_runtime import durable_job_repository

    await durable_job_repository.verify_inference_ledger_in_background()
